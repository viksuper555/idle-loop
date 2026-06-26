"""idle-loop orchestrator + CLI (SPEC §5 / §6).

Drives one iteration per ticket:

    Discover -> Estimate -> Triage -> Implement -> Guard -> PR -> Review -> Decide -> Log

The orchestrator stays thin: it owns config, the GitHub client, iteration
accounting, the global budget cap, and structured logging, and delegates the
real work to the estimator, the implementer/reviewer agents, and the guards.

Dependencies are injected (see :class:`Orchestrator`); :meth:`Orchestrator.from_config`
wires the real ones. Tests construct an Orchestrator with fakes — no network,
no Anthropic calls, no git.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import threading
import urllib.parse
from collections.abc import Sequence
from datetime import datetime

import naming
import pr_template
from agents.harness import HarnessRateLimited
from config import Config, load_config
from costlog import append_record
from github_client import GitHubClient, GitHubError
from guards.base import Guard, GuardContext, run_all
from guards.budget import BudgetGuard
from guards.estimate import Estimator
from guards.scope import ScopeGuard
from guards.security import SecurityGuard
from guards.tests import TestsGuard
from models import (
    EstimateResult,
    GuardResult,
    ImplementationResult,
    Outcome,
    ReviewVerdict,
    RunRecord,
    Ticket,
    format_tokens,
)

log = logging.getLogger("idle_loop")

SHIPPED_BY = "🤖 _Shipped by [idle-loop](https://github.com/viksuper555/idle-loop)._"

# Marker on the single sticky "cost chip" comment idle-loop maintains per issue,
# so a human watching the board sees the price without digging into the logs.
COST_CHIP_MARKER = "<!-- idle-loop:cost-chip -->"

# Exit codes the bash listener keys on.
EXIT_OK = 0
EXIT_RATE_LIMITED = 42
# Where a rate-limit reset time is persisted for the listener to read.
RATE_LIMIT_STATE = ".idle-loop/rate_limit.json"
# Directory (under repo_dir) holding the per-ticket git worktrees used when
# tickets are worked in parallel. Gitignored.
WORKTREE_DIR = ".idle-worktrees"
# State file (under repo_dir) mapping an open PR to the worktree + claude
# session it was built in, plus the last review id we've acted on. This is what
# lets `watch_reviews` resume the original context when a new review lands.
PR_WATCH_STATE = ".idle-loop/pr_watch.json"


class Orchestrator:
    """Runs the guard-railed loop over a backlog of ready tickets."""

    def __init__(
        self,
        config: Config,
        github,
        estimator: Estimator,
        implementer,
        reviewer,
        guards: Sequence[Guard],
        repo_dir: str = ".",
        logger: logging.Logger | None = None,
        planner=None,
    ) -> None:
        self.config = config
        self.github = github
        self.estimator = estimator
        self.planner = planner
        self.implementer = implementer
        self.reviewer = reviewer
        # Post-implementation guards, run in order (fail-closed).
        self.guards: list[Guard] = list(guards)
        self.repo_dir = repo_dir
        self.log = logger or log
        self._repo_tree_cache: list[str] | None = None
        # Serializes cost-log appends across parallel ticket workers.
        self._cost_log_lock = threading.Lock()
        # Serializes PR-watch state read-modify-write across parallel workers,
        # which all persist to the single central state file under repo_dir.
        self._pr_watch_lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # Construction
    # ------------------------------------------------------------------ #
    @classmethod
    def from_config(cls, config: Config, repo_dir: str = ".") -> Orchestrator:
        """Build an orchestrator with the real production dependencies."""
        # Local import keeps anthropic out of the import path for non-agent uses.
        from agents.implementer import Implementer
        from agents.planner import Planner
        from agents.reviewer import Reviewer

        estimator = Estimator(config)
        return cls(
            config=config,
            github=GitHubClient(config.repo),
            estimator=estimator,
            planner=Planner(config),
            implementer=Implementer(config),
            reviewer=Reviewer(config),
            guards=[
                ScopeGuard(config),
                TestsGuard(config),
                SecurityGuard(config),
                BudgetGuard(config),
            ],
            repo_dir=repo_dir,
        )

    # ------------------------------------------------------------------ #
    # Discovery
    # ------------------------------------------------------------------ #
    def discover(self) -> list[Ticket]:
        """Fetch open ``idle:ready`` issues, oldest first.

        Skips tickets handed to a human (``needs-human``) or already carrying an
        open idle-loop PR (``in-progress``), so neither is re-discovered and
        re-worked on a later pass (re-spending budget, risking a duplicate PR).
        ``needs-human`` is dropped by a human; ``in-progress`` is cleared when the
        PR merges/closes (see :meth:`_reap_worktrees`). Parking also drops
        ``idle:ready`` itself (see :meth:`_flag_needs_human`) — this is the belt
        to that braces.
        """
        labels = self.config.labels
        return [
            t
            for t in self.github.list_ready_issues(labels.ready)
            if not (t.has_label(labels.needs_human) or t.has_label(labels.in_progress))
        ]

    def _repo_tree(self) -> list[str] | None:
        """Best-effort list of tracked files in ``repo_dir`` (for the estimator)."""
        if self._repo_tree_cache is not None:
            return self._repo_tree_cache
        try:
            proc = subprocess.run(
                ["git", "-C", self.repo_dir, "ls-files"],
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            if proc.returncode == 0:
                self._repo_tree_cache = [
                    ln for ln in proc.stdout.splitlines() if ln.strip()
                ]
                return self._repo_tree_cache
        except Exception:  # noqa: BLE001 - estimation degrades without a tree
            pass
        return None

    # ------------------------------------------------------------------ #
    # Estimation
    # ------------------------------------------------------------------ #
    def _resolve_estimate(self, ticket: Ticket, repo_dir: str) -> EstimateResult:
        """Price a ticket deterministically via the planning pass, or fall back.

        The planner runs a cheap read-only claude session that reports a token
        budget; cost is ``pricing.cost_for_tokens(...)``. Falls back to the
        heuristic estimator when planning is disabled/unavailable, when a cheap
        pre-filter already prices the ticket far over threshold (don't pay for a
        planning pass to confirm an obvious park), or when a session already
        exists for this worktree (re-planning would clobber in-flight context).
        ``HarnessRateLimited`` from the planner propagates to stop the loop.
        """
        heuristic = self.estimator.estimate(ticket, self._repo_tree())
        if not self.config.planner.enabled or self.planner is None:
            return heuristic

        # Cheap pre-filter: an obviously-over-budget ticket parks on the heuristic
        # without paying for a planning pass.
        prefilter = (
            self.config.triage.auto_threshold_usd
            * self.config.triage.prefilter_multiplier
        )
        if heuristic.estimated_cost > prefilter:
            self.log.info(
                "#%s heuristic $%.2f over %gx threshold -> skip planning",
                ticket.number,
                heuristic.estimated_cost,
                self.config.triage.prefilter_multiplier,
            )
            return heuristic

        # Resume guard: a saved session means the implementer is mid-flight in this
        # worktree; re-planning would overwrite it. Reuse the heuristic for the chip.
        from agents.implementer import load_session

        if load_session(repo_dir) is not None:
            self.log.info(
                "#%s session exists -> skip planning, reuse heuristic", ticket.number
            )
            return heuristic

        plan = self.planner.plan(ticket, repo_dir)  # HarnessRateLimited propagates
        if plan is None:
            self.log.info("#%s planning unparseable -> heuristic fallback", ticket.number)
            return heuristic

        cost = self.config.pricing.cost_for_tokens(
            plan.estimated_input_tokens, plan.estimated_output_tokens
        )
        return EstimateResult(
            estimated_cost=round(cost, 2),
            estimated_iterations=heuristic.estimated_iterations,  # informational only
            confidence=0.85,  # deterministic source -> high, not certain
            features=heuristic.features,
            margin=round(cost * self.config.planner.margin_fraction, 2),
            estimated_input_tokens=plan.estimated_input_tokens,
            estimated_output_tokens=plan.estimated_output_tokens,
            source="planner",
        )

    # ------------------------------------------------------------------ #
    # Per-ticket processing (SPEC steps 2-9)
    # ------------------------------------------------------------------ #
    def process_ticket(
        self, ticket: Ticket, repo_dir: str | None = None, branch: str | None = None
    ) -> RunRecord:
        """Process one ticket end-to-end. Never raises (except rate-limit).

        ``repo_dir`` is the working tree the implementer commits in; it defaults
        to the orchestrator's own checkout, but is overridden with a per-ticket
        git worktree when tickets are worked in parallel. ``branch`` is the head
        branch to implement on; when omitted it is derived from the ticket (see
        :meth:`_branch_for`) — the parallel path passes it explicitly so the
        worktree and branch are named consistently.
        """
        repo_dir = repo_dir or self.repo_dir
        try:
            return self._process_ticket(ticket, repo_dir, branch)
        except HarnessRateLimited:
            raise  # stop the whole loop; the listener will reschedule
        except Exception as exc:  # noqa: BLE001 - one bad ticket must not kill the loop
            self.log.exception("ticket #%s crashed: %s", ticket.number, exc)
            self._park(
                ticket,
                f"idle-loop hit an unexpected error processing this ticket: {exc}",
            )
            return self._record(ticket, None, None, Outcome.FAILED)

    def _process_ticket(
        self, ticket: Ticket, repo_dir: str, branch: str | None = None
    ) -> RunRecord:
        # (1) Reject tickets without acceptance criteria — never guess (SPEC §4).
        if not ticket.acceptance_criteria:
            self.log.info("#%s rejected: no acceptance criteria", ticket.number)
            self._comment(
                ticket,
                "**idle-loop skipped this ticket: no acceptance criteria.**\n\n"
                "Add an `## Acceptance Criteria` checklist so the loop can verify "
                "the change against concrete, testable conditions.",
            )
            self._flag_needs_human(ticket)
            return self._record(ticket, None, None, Outcome.SKIPPED)

        # (2) Estimate — a cheap planning pass reports a deterministic token
        # budget (falls back to the heuristic), so triage acts on a grounded number.
        estimate = self._resolve_estimate(ticket, repo_dir)
        band = estimate.band()
        self.log.info(
            "#%s estimate %s (%s, conf %.2f)",
            ticket.number,
            band,
            estimate.source,
            estimate.confidence,
        )
        # Surface the price on the issue itself as a chip the moment it's known.
        self._cost_chip(ticket, estimate)

        # (3) Triage — park anything over the auto threshold (after the cheap
        # planning pass, which is the only spend so far).
        threshold = self.config.triage.auto_threshold_usd
        if estimate.estimated_cost > threshold:
            self.log.info("#%s over threshold $%g -> needs human", ticket.number, threshold)
            self._comment(
                ticket,
                f"**idle-loop estimate: {band}** — above the ${threshold:g} "
                "auto-run threshold, so parking for a human to triage.\n\n"
                + _estimate_detail(estimate),
            )
            self._flag_needs_human(ticket)
            return self._record(ticket, estimate, None, Outcome.SKIPPED)

        # (4) Implement on a fresh isolated branch (named <prefix>/issue-<id>).
        if branch is None:
            branch = self._branch_for(ticket)
        self.log.info("#%s implementing on %s", ticket.number, branch)
        impl = self.implementer.run(ticket, repo_dir, branch)
        self.log.info(
            "#%s implemented: %d files, %d iters, $%.2f%s",
            ticket.number,
            len(impl.files_changed),
            impl.iterations,
            impl.cost_usd,
            " [error]" if impl.error else "",
        )
        # Iteration done — refresh the chip with the running actual cost + tokens.
        self._cost_chip(
            ticket,
            estimate,
            spent=impl.cost_usd,
            spent_tokens=impl.input_tokens + impl.output_tokens,
        )

        # (5) Guards — fail closed, in order.
        ctx = GuardContext(
            ticket=ticket,
            config=self.config,
            repo_dir=repo_dir,
            diff=impl.diff,
            files_changed=impl.files_changed,
            implementation=impl,
        )
        results = run_all(self.guards, ctx)
        failed = next((r for r in results if not r.passed), None)
        if failed is not None:
            outcome = Outcome.FAILED if impl.error else Outcome.PARKED
            self.log.info("#%s guard '%s' failed: %s", ticket.number, failed.name, failed.reason)
            self._park(
                ticket,
                f"**Guard `{failed.name}` failed:** {failed.reason}\n\n"
                + _gates_summary(results),
            )
            return self._record(ticket, estimate, impl, outcome)

        # (6) Push the branch and open the PR linking the issue.
        pr = self._open_pr(ticket, branch, impl, results, estimate, repo_dir)
        if pr is None:
            self.log.info("#%s could not push branch / open PR; parking", ticket.number)
            self._park(
                ticket,
                "idle-loop implemented the change but could not push the branch or "
                f"open a PR (branch `{branch}`). Check the loop's git remote/permissions.",
            )
            return self._record(ticket, estimate, impl, Outcome.PARKED)

        # The issue now has an open idle-loop PR — mark it in-progress so a later
        # pass skips it (discover()) instead of re-implementing from scratch.
        # Cleared when the PR merges/closes (see _reap_worktrees).
        self._flag_in_progress(ticket)
        # Mark the fresh PR idle:listen and remember the worktree + session it was
        # built in, so a later review can resume that same claude context.
        self._listen_pr(ticket, pr, branch, repo_dir)

        # (7) Review against the acceptance criteria (separate agent/context),
        # then iterate on the SAME branch/PR: feed the reviewer's requested
        # changes back to the implementer up to budget.review_iterations times
        # before parking. The open PR updates in place on each push — no new PR.
        verdict = self.reviewer.review(ticket, impl.diff)
        self.log.info("#%s review: %s", ticket.number, verdict.decision)

        total_cost = impl.cost_usd
        total_tokens = impl.input_tokens + impl.output_tokens
        attempt = 0
        while not verdict.approved and attempt < self.config.budget.review_iterations:
            if total_cost >= self.config.budget.per_ticket_cap_usd:
                self.log.info(
                    "#%s per-ticket cap $%.2f reached before revision; parking",
                    ticket.number,
                    self.config.budget.per_ticket_cap_usd,
                )
                break
            attempt += 1
            self.log.info(
                "#%s revising on %s (review attempt %d/%d)",
                ticket.number,
                branch,
                attempt,
                self.config.budget.review_iterations,
            )
            impl = self.implementer.run(
                ticket, repo_dir, branch, feedback=_review_feedback(verdict)
            )
            total_cost += impl.cost_usd
            total_tokens += impl.input_tokens + impl.output_tokens
            self.log.info(
                "#%s revised: %d files, %d iters, $%.2f (total $%.2f)%s",
                ticket.number,
                len(impl.files_changed),
                impl.iterations,
                impl.cost_usd,
                total_cost,
                " [error]" if impl.error else "",
            )
            # Iteration done — refresh the chip with the cumulative spend + tokens.
            self._cost_chip(
                ticket, estimate, spent=total_cost, spent_tokens=total_tokens
            )

            # Re-run guards on the revised diff. A failure here (e.g. the revision
            # ballooned past max_diff_lines) parks rather than looping further —
            # a big diff is the signal to stop and let a human decide.
            ctx = GuardContext(
                ticket=ticket,
                config=self.config,
                repo_dir=repo_dir,
                diff=impl.diff,
                files_changed=impl.files_changed,
                implementation=impl,
            )
            results = run_all(self.guards, ctx)
            failed = next((r for r in results if not r.passed), None)
            if failed is not None:
                impl.cost_usd = total_cost
                self.log.info(
                    "#%s guard '%s' failed on revision: %s",
                    ticket.number,
                    failed.name,
                    failed.reason,
                )
                self._park(
                    ticket,
                    f"**Guard `{failed.name}` failed on a revision:** {failed.reason}\n\n"
                    + _gates_summary(results),
                )
                return self._record(ticket, estimate, impl, Outcome.PARKED)

            # Push the revision — the existing PR updates in place.
            if not self.implementer.push_branch(repo_dir, branch):
                impl.cost_usd = total_cost
                self.log.info("#%s could not push revision; parking", ticket.number)
                self._park(
                    ticket,
                    f"idle-loop revised the change but could not push branch `{branch}`.",
                )
                return self._record(ticket, estimate, impl, Outcome.PARKED)

            verdict = self.reviewer.review(ticket, impl.diff)
            self.log.info("#%s review: %s", ticket.number, verdict.decision)

        # Cost recorded/logged for the ticket is the cumulative spend.
        impl.cost_usd = total_cost

        # (8) Decide.
        merged = (
            verdict.approved
            and not self.config.merge.require_human
            and self._merge(pr)
        )
        if merged:
            self.log.info("#%s MERGED", ticket.number)
            self._comment(ticket, f"✅ **idle-loop merged this** (all guards green + review approved).\n\n{SHIPPED_BY}")
            return self._record(ticket, estimate, impl, Outcome.MERGED)

        # Park: either review requested changes, or human approval is required.
        self.log.info("#%s parked for human", ticket.number)
        self._flag_needs_human(ticket)
        self._comment(ticket, _park_message(verdict, pr, self.config.merge.require_human))
        return self._record(ticket, estimate, impl, Outcome.PARKED)

    # ------------------------------------------------------------------ #
    # Loop
    # ------------------------------------------------------------------ #
    def run(
        self,
        max_tickets: int | None = None,
        dry_run: bool = False,
        max_parallel: int | None = None,
    ) -> list[RunRecord]:
        """Process ready tickets until the backlog, cap, or limit is reached.

        ``max_parallel`` (defaults to ``budget.max_parallel``) caps how many
        tickets are worked concurrently — each in its own git worktree so the
        implementers never clobber one another's checkout. ``1`` is sequential.
        """
        tickets = self.discover()
        self.log.info("discovered %d ready ticket(s)", len(tickets))

        if dry_run:
            for ticket in tickets:
                band = self._dry_run_band(ticket)
                # Print to stdout so --dry-run is useful without log config.
                print(f"#{ticket.number}  {ticket.title}  {band}")
            return []

        if max_tickets is not None:
            tickets = tickets[:max_tickets]

        # Mutations begin here — only for real runs (dry-run takes no action).
        self._ensure_labels()
        # Reclaim worktrees whose PRs have since merged/closed before dispatching.
        self._reap_worktrees()

        parallel = self.config.budget.max_parallel if max_parallel is None else max_parallel
        parallel = max(1, parallel)
        try:
            if parallel == 1 or len(tickets) <= 1:
                records = self._run_sequential(tickets)
            else:
                records = self._run_parallel(tickets, parallel)
        except HarnessRateLimited as exc:
            self._on_rate_limit(exc)
            raise
        spent = sum(r.actual_cost for r in records)
        self.log.info("processed %d ticket(s); spent ~$%.2f", len(records), spent)
        return records

    def _run_sequential(self, tickets: list[Ticket]) -> list[RunRecord]:
        """Work tickets one at a time in the orchestrator's own checkout."""
        records: list[RunRecord] = []
        spent = 0.0
        for ticket in tickets:
            if spent >= self.config.budget.global_cap_usd:
                self.log.info(
                    "global cap $%g reached (spent $%.2f); stopping",
                    self.config.budget.global_cap_usd,
                    spent,
                )
                break
            record = self.process_ticket(ticket)
            records.append(record)
            spent += record.actual_cost
        return records

    def _run_parallel(self, tickets: list[Ticket], parallel: int) -> list[RunRecord]:
        """Work up to ``parallel`` tickets at once, each in its own git worktree.

        Submission is throttled by the global cost cap: once cumulative spend
        crosses ``budget.global_cap_usd`` no further tickets are dispatched, and
        already-running ones are allowed to finish. A :class:`HarnessRateLimited`
        from any worker is re-raised after the pool drains so the listener can
        reschedule.
        """
        from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

        self.log.info("working %d ticket(s), up to %d in parallel", len(tickets), parallel)

        def work(ticket: Ticket) -> RunRecord:
            # The worktree persists after the pass — it is reclaimed only once
            # the ticket's PR is merged/closed (see _reap_worktrees), so work can
            # resume in it (with saved agent context) on a later run. The branch
            # and worktree are named from the same scheme so they stay in sync.
            branch = self._branch_for(ticket)
            worktree = self._make_worktree(ticket, branch)
            return self.process_ticket(ticket, repo_dir=worktree, branch=branch)

        records: list[RunRecord] = []
        spent = 0.0
        rate_limited: HarnessRateLimited | None = None
        pending: set = set()
        queue = iter(tickets)
        cap = self.config.budget.global_cap_usd

        with ThreadPoolExecutor(max_workers=parallel) as pool:
            # Prime the pool.
            for _ in range(parallel):
                ticket = next(queue, None)
                if ticket is None:
                    break
                pending.add(pool.submit(work, ticket))

            while pending:
                done, pending = wait(pending, return_when=FIRST_COMPLETED)
                for fut in done:
                    try:
                        record = fut.result()
                    except HarnessRateLimited as exc:
                        rate_limited = exc
                        continue
                    records.append(record)
                    spent += record.actual_cost

                if rate_limited is not None:
                    continue  # stop dispatching; let running tickets drain
                if spent >= cap:
                    self.log.info(
                        "global cap $%g reached (spent $%.2f); not dispatching more",
                        cap,
                        spent,
                    )
                    continue
                # Backfill a free slot.
                for _ in range(len(done)):
                    ticket = next(queue, None)
                    if ticket is None:
                        break
                    pending.add(pool.submit(work, ticket))

        if rate_limited is not None:
            raise rate_limited
        return records

    # ------------------------------------------------------------------ #
    # Git worktrees (isolation for parallel tickets)
    # ------------------------------------------------------------------ #
    def _worktree_git(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", self.repo_dir, *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )

    def _worktree_path(self, branch: str) -> str:
        """The worktree directory for ``branch`` (its name flattened, see naming)."""
        return os.path.join(self.repo_dir, WORKTREE_DIR, naming.worktree_dir_name(branch))

    def _branch_exists(self, name: str) -> bool:
        """Whether ``name`` is already a local git head (worktrees share refs)."""
        proc = self._worktree_git("rev-parse", "--verify", "--quiet", f"refs/heads/{name}")
        return proc.returncode == 0

    def _branch_for(self, ticket: Ticket) -> str:
        """The branch to implement ``ticket`` on: ``<prefix>/issue-<id>``.

        Resumes the ticket's *own* prior branch when one already exists — its
        worktree, a persisted ``pr_watch`` record, or an open PR on it — so a
        re-processed ticket continues in place instead of starting over on a
        ``-b``/``-c`` name (sequential runs build the branch in the main checkout
        with no worktree, so worktree-presence alone missed this). Only when the
        canonical name is held by *unrelated* work (a bare colliding head with no
        idle-loop ownership marker) does it fall through to ``dedupe_branch`` for
        a free suffixed name — and branch selection then agrees with the worktree
        path, since both derive from the same resolved name.
        """
        canonical = naming.canonical_branch(ticket)
        if self._is_resumable_branch(ticket, canonical):
            self.log.info("#%s resuming existing branch %s", ticket.number, canonical)
            return canonical
        return naming.dedupe_branch(canonical, self._branch_exists)

    def _is_resumable_branch(self, ticket: Ticket, branch: str) -> bool:
        """Whether ``branch`` already carries this ticket's prior idle-loop work.

        Recognised — and tied back to the issue id — by any of: an existing
        worktree directory, a persisted ``pr_watch`` record, or an open PR on the
        branch. A bare local head with none of these is treated as unrelated work
        (so it dedupes), not a resume.
        """
        if naming.issue_number_from_branch(branch) != ticket.number:
            return False
        if os.path.isdir(self._worktree_path(branch)):
            return True
        if self._pr_watch_has_branch(branch):
            return True
        return self._open_pr_exists(branch)

    def _pr_watch_has_branch(self, branch: str) -> bool:
        """Whether any persisted PR-watch record was opened on ``branch``."""
        return any(
            rec.get("branch") == branch for rec in self._load_pr_watch().values()
        )

    def _open_pr_exists(self, branch: str) -> bool:
        """Best-effort: whether an open PR exists for head ``branch``."""
        try:
            return self.github.find_open_pr_by_head(branch) is not None
        except GitHubError as exc:
            self.log.warning("open-PR lookup for %s failed: %s", branch, exc)
            return False

    def _make_worktree(self, ticket: Ticket, branch: str) -> str:
        """Return an isolated git worktree for ``branch``, creating it if needed.

        An existing worktree is reused as-is — its branch and the agent's prior
        work are preserved so the implementer continues where it left off. A new
        worktree is checked out (detached) at the target branch so the
        implementer's own ``checkout -b <prefix>/issue-N`` starts clean.
        """
        path = self._worktree_path(branch)
        if os.path.isdir(path):
            self.log.info("#%s reusing worktree %s", ticket.number, path)
            return path
        base = self.config.merge.target_branch
        self._worktree_git("worktree", "prune")
        proc = self._worktree_git("worktree", "add", "--detach", "--force", path, base)
        if proc.returncode != 0:
            raise RuntimeError(
                f"git worktree add failed for #{ticket.number}: {proc.stderr.strip()}"
            )
        return path

    def _remove_worktree(self, path: str) -> None:
        """Tear down a ticket worktree (best-effort; never raises)."""
        self._worktree_git("worktree", "remove", "--force", path)
        self._worktree_git("worktree", "prune")

    def _reap_worktrees(self) -> None:
        """Remove worktrees whose ticket PR has been merged or closed.

        Run before dispatching a batch: any worktree whose branch has a finished
        PR is reclaimed; worktrees with an open PR (resumable) or no PR yet
        (pre-PR, possibly mid-flight) are left in place.
        """
        root = os.path.join(self.repo_dir, WORKTREE_DIR)
        if not os.path.isdir(root):
            return
        for name in sorted(os.listdir(root)):
            branch = naming.branch_from_worktree_dir(name)
            if branch is None:
                continue
            try:
                status = self.github.pr_status_for_branch(branch)
            except GitHubError as exc:
                self.log.warning("reap: PR status for %s failed: %s", branch, exc)
                continue
            if status == "done":
                self.log.info("%s PR merged/closed — reclaiming worktree", branch)
                self._remove_worktree(os.path.join(root, name))
                # The PR is finished — clear idle:in-progress on its issue so the
                # ticket can flow normally again (e.g. reopened, or follow-up work).
                issue = naming.issue_number_from_branch(branch)
                if issue is not None:
                    self._unlabel_num(issue, self.config.labels.in_progress)

    def _on_rate_limit(self, exc: HarnessRateLimited) -> None:
        """Persist the reset time and emit a marker the bash listener parses."""
        iso = datetime.fromtimestamp(exc.reset_at).isoformat(timespec="seconds")
        self.log.warning("harness rate-limited; resets at %s — %s", iso, exc.reset_human[:120])
        try:
            os.makedirs(os.path.dirname(RATE_LIMIT_STATE) or ".", exist_ok=True)
            with open(RATE_LIMIT_STATE, "w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "reset_epoch": exc.reset_at,
                        "reset_at": iso,
                        "reason": exc.reset_human[:300],
                    },
                    fh,
                )
        except OSError as err:
            self.log.warning("could not write rate-limit state: %s", err)
        # Machine-readable line for idle-listener.sh (it also reads the state file).
        print(f'IDLE_LOOP_RATE_LIMITED reset_epoch={exc.reset_at:.0f} reset_at="{iso}"', flush=True)

    def _dry_run_band(self, ticket: Ticket) -> str:
        if not ticket.acceptance_criteria:
            return "(no acceptance criteria — would skip)"
        try:
            return self.estimator.estimate(ticket, self._repo_tree()).band()
        except Exception:  # noqa: BLE001 - dry-run must never crash
            return "(estimate unavailable)"

    # ------------------------------------------------------------------ #
    # GitHub side effects (each swallows GitHubError so the loop survives)
    # ------------------------------------------------------------------ #
    def _ensure_labels(self) -> None:
        labels = self.config.labels
        try:
            self.github.ensure_labels(
                [
                    labels.ready,
                    labels.needs_human,
                    labels.allow_sensitive,
                    labels.listen,
                    labels.in_progress,
                ]
            )
        except GitHubError as exc:
            self.log.warning("ensure_labels failed (continuing): %s", exc)

    def _comment(self, ticket: Ticket, body: str) -> None:
        try:
            self.github.comment(ticket.number, body)
        except GitHubError as exc:
            self.log.warning("comment on #%s failed: %s", ticket.number, exc)

    def _label(self, ticket: Ticket, label: str) -> None:
        self._label_num(ticket.number, label)

    def _label_num(self, number: int, label: str) -> None:
        """Add ``label`` to issue/PR ``number`` (labels share the issues API)."""
        try:
            self.github.add_label(number, label)
        except GitHubError as exc:
            self.log.warning("add_label %s on #%s failed: %s", label, number, exc)

    def _comment_num(self, number: int, body: str) -> None:
        """Comment on issue/PR ``number`` (PR comments share the issues API)."""
        try:
            self.github.comment(number, body)
        except GitHubError as exc:
            self.log.warning("comment on #%s failed: %s", number, exc)

    def _unlabel(self, ticket: Ticket, label: str) -> None:
        self._unlabel_num(ticket.number, label)

    def _unlabel_num(self, number: int, label: str) -> None:
        """Remove ``label`` from issue/PR ``number`` (a missing label is fine)."""
        try:
            self.github.remove_label(number, label)
        except GitHubError as exc:
            self.log.warning("remove_label %s on #%s failed: %s", label, number, exc)

    def _flag_needs_human(self, ticket: Ticket) -> None:
        """Hand a ticket back to a human: add ``needs-human``, drop ``idle:ready``.

        Dropping ``idle:ready`` is what stops a parked ticket from being
        re-discovered (and re-worked, re-spending budget) on the next pass — the
        single place every park/skip/defer site routes through.
        """
        self._label(ticket, self.config.labels.needs_human)
        self._unlabel(ticket, self.config.labels.ready)

    def _flag_in_progress(self, ticket: Ticket) -> None:
        """Mark a ticket as having an open idle-loop PR so discover() skips it."""
        self._label(ticket, self.config.labels.in_progress)

    def _park(self, ticket: Ticket, reason: str) -> None:
        """Comment a crisp reason and flag the ticket for a human.

        Routes through :meth:`_flag_needs_human`, so parking also drops
        ``idle:ready`` and the ticket is not re-discovered on the next pass.
        """
        self._comment(ticket, f"🅿️ **idle-loop parked this ticket.**\n\n{reason}")
        self._flag_needs_human(ticket)

    def _cost_chip(
        self,
        ticket: Ticket,
        estimate: EstimateResult,
        spent: float | None = None,
        spent_tokens: int | None = None,
    ) -> None:
        """Upsert the sticky cost chip on the issue (estimate, then spend so far).

        Best-effort: a GitHub error here must never derail the loop — the chip is
        a convenience for humans watching the board, not part of the contract.
        """
        try:
            self.github.upsert_comment(
                ticket.number,
                COST_CHIP_MARKER,
                _cost_chip_body(estimate, spent, spent_tokens),
            )
        except GitHubError as exc:
            self.log.warning("cost chip on #%s failed: %s", ticket.number, exc)

    def _open_pr(
        self,
        ticket: Ticket,
        branch: str,
        impl: ImplementationResult,
        results: list[GuardResult],
        estimate: EstimateResult | None = None,
        repo_dir: str | None = None,
    ) -> dict | None:
        # Push the implementation branch first — GitHub can't open a PR for a
        # head ref that only exists locally.
        if not self.implementer.push_branch(repo_dir or self.repo_dir, branch):
            self.log.warning("#%s push of branch %s failed", ticket.number, branch)
            return None
        # Reuse an open PR for this branch if one already exists — the push above
        # has updated it in place. Creating a second PR for the same head would
        # 422 ("A pull request already exists") and wrongly park the ticket.
        try:
            existing = self.github.find_open_pr_by_head(branch)
        except GitHubError as exc:
            self.log.warning(
                "#%s lookup of open PR for %s failed: %s", ticket.number, branch, exc
            )
            existing = None
        if existing is not None:
            self.log.info(
                "#%s reusing open PR #%s on %s",
                ticket.number,
                existing.get("number"),
                branch,
            )
            return existing
        body = pr_template.render_pr_body(self._pr_content(ticket, branch, impl, results, estimate))
        try:
            pr = self.github.create_pull_request(
                title=f"[idle-loop] {ticket.title} (#{ticket.number})",
                head=branch,
                base=self.config.merge.target_branch,
                body=body,
            )
            self.log.info("#%s opened PR #%s", ticket.number, pr.get("number"))
            return pr
        except GitHubError as exc:
            self.log.warning("create_pull_request for #%s failed: %s", ticket.number, exc)
            return None

    def _pr_content(
        self,
        ticket: Ticket,
        branch: str,
        impl: ImplementationResult,
        results: list[GuardResult],
        estimate: EstimateResult | None,
    ) -> pr_template.PRContent:
        """Assemble the PR body inputs from a finished implementation."""
        badges = ""
        if estimate is not None:
            badges = _cost_badges(
                estimate,
                spent=impl.cost_usd,
                spent_tokens=impl.input_tokens + impl.output_tokens,
            )
        return pr_template.PRContent(
            issue_number=ticket.number,
            title=ticket.title,
            branch=branch,
            issue_url=ticket.url,
            acceptance_criteria=list(ticket.acceptance_criteria),
            files_changed=len(impl.files_changed),
            iterations=impl.iterations,
            cost_badges_md=badges,
            gates_md=_gates_summary(results),
            commentary=impl.notes,
        )

    def _merge(self, pr: dict | None) -> bool:
        if not pr:
            return False
        try:
            return self.github.merge_pull_request(pr["number"])
        except GitHubError as exc:
            self.log.warning("merge PR #%s failed: %s", pr.get("number"), exc)
            return False

    # ------------------------------------------------------------------ #
    # PR review watching (AC: "watch for PR reviews")
    # ------------------------------------------------------------------ #
    def _pr_watch_path(self) -> str:
        return os.path.join(self.repo_dir, PR_WATCH_STATE)

    def _load_pr_watch(self) -> dict[str, dict]:
        """Load the PR -> {worktree, session, last_review_id, ...} map (best-effort)."""
        try:
            with open(self._pr_watch_path(), encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_pr_watch(self, state: dict[str, dict]) -> None:
        """Persist the PR-watch map (best-effort; never fatal)."""
        try:
            path = self._pr_watch_path()
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(state, fh)
        except OSError as exc:
            self.log.warning("could not write PR-watch state: %s", exc)

    def _listen_pr(
        self, ticket: Ticket, pr: dict, branch: str, repo_dir: str
    ) -> None:
        """Label a freshly opened PR ``idle:listen`` and record how to resume it.

        The claude session id is read from the worktree (it was saved by the
        implementer) and stored alongside the worktree path so a later review can
        spawn an implementer that resumes the same context.
        """
        from agents.implementer import load_session

        number = pr.get("number")
        if number is None:
            return
        number = int(number)
        self._label_num(number, self.config.labels.listen)

        reviews = self._safe_list_reviews(number)
        last_review_id = max((r["id"] for r in reviews), default=0)

        with self._pr_watch_lock:
            state = self._load_pr_watch()
            state[str(number)] = {
                "issue": ticket.number,
                "branch": branch,
                "worktree": repo_dir,
                "session_id": load_session(repo_dir) or "",
                "last_review_id": last_review_id,
                "attempts": 0,
            }
            self._save_pr_watch(state)

    def _safe_list_reviews(self, number: int) -> list[dict]:
        try:
            return self.github.list_reviews(number)
        except GitHubError as exc:
            self.log.warning("list_reviews for PR #%s failed: %s", number, exc)
            return []

    @staticmethod
    def _review_actionable(review: dict) -> bool:
        """Whether a review asks for work: changes requested, or a non-empty comment.

        Approvals and dismissals carry no action — we only advance past them.
        """
        state = (review.get("state") or "").upper()
        if state == "CHANGES_REQUESTED":
            return True
        if state == "COMMENTED" and (review.get("body") or "").strip():
            return True
        return False

    def watch_reviews(self) -> list[int]:
        """Address new reviews on every open ``idle:listen`` PR. Returns PRs acted on.

        For each watched PR we fetch its reviews; any review newer than the last
        one we handled that asks for changes triggers a resume of the PR's
        original claude context (via the saved worktree session) so the
        implementer revises the branch in place. A PR is deferred to a human
        (``idle:listen`` dropped, ``idle:needs-human`` added) once it exhausts
        ``budget.review_iterations`` or a guard fails on a revision.
        """
        listen = self.config.labels.listen
        try:
            prs = self.github.list_pull_requests_by_label(listen)
        except GitHubError as exc:
            self.log.warning("listing %s PRs failed: %s", listen, exc)
            return []

        self.log.info("watching %d PR(s) labelled %s", len(prs), listen)
        state = self._load_pr_watch()
        acted: list[int] = []

        try:
            for pr in prs:
                number = pr["number"]
                rec = state.get(str(number)) or self._seed_pr_record(number)
                if rec is None:
                    continue

                reviews = self._safe_list_reviews(number)
                last_seen = int(rec.get("last_review_id", 0))
                new_reviews = [r for r in reviews if r["id"] > last_seen]
                if not new_reviews:
                    state[str(number)] = rec
                    continue

                # Advance the cursor regardless of whether any review was
                # actionable, so an approval/comment isn't re-examined next poll.
                rec["last_review_id"] = max(r["id"] for r in reviews)
                actionable = [r for r in new_reviews if self._review_actionable(r)]
                if actionable:
                    self.log.info(
                        "#PR%s: %d new actionable review(s) -> resuming context",
                        number,
                        len(actionable),
                    )
                    if self._address_pr_review(number, rec, actionable):
                        acted.append(number)
                    if rec.get("_deferred"):
                        state.pop(str(number), None)
                        continue
                state[str(number)] = rec
        except HarnessRateLimited as exc:
            # Persist progress and the reset time, then stop so the listener can
            # reschedule — exactly as the ticket pass does.
            self._save_pr_watch(state)
            self._on_rate_limit(exc)
            raise

        self._save_pr_watch(state)
        return acted

    def _seed_pr_record(self, number: int) -> dict | None:
        """Build a watch record for a PR labelled externally (no saved state).

        Derives the issue/worktree from the PR's head branch (``idle/issue-N``);
        returns ``None`` when the branch isn't one idle-loop owns, so we never try
        to resume context we don't have.
        """
        try:
            pr = self.github.get_pull_request(number)
        except GitHubError as exc:
            self.log.warning("get_pull_request #%s failed: %s", number, exc)
            return None
        branch = pr.get("head_branch", "")
        issue = naming.issue_number_from_branch(branch)
        if issue is None:
            self.log.info("#PR%s head %r not idle-owned; skipping", number, branch)
            return None
        from agents.implementer import load_session

        worktree = self._worktree_path(branch)
        return {
            "issue": issue,
            "branch": branch,
            "worktree": worktree,
            "session_id": load_session(worktree) or "",
            "last_review_id": 0,
            "attempts": 0,
        }

    def _address_pr_review(
        self, number: int, rec: dict, reviews: list[dict]
    ) -> bool:
        """Resume the PR's context and revise the branch to address ``reviews``.

        Returns whether a revision was actually run. Mutates ``rec`` in place
        (attempt count, ``_deferred`` flag). Bounded by ``review_iterations``:
        once exhausted, or if a guard fails / push fails, the PR is deferred.
        """
        if rec.get("attempts", 0) >= self.config.budget.review_iterations:
            self._defer_pr(
                number,
                rec,
                "idle-loop has used its review-revision budget on this PR; "
                "the latest review needs a human.",
            )
            return False

        try:
            ticket = self.github.get_issue(rec["issue"])
        except GitHubError as exc:
            self.log.warning("get_issue #%s for PR #%s failed: %s", rec.get("issue"), number, exc)
            return False

        worktree = rec["worktree"]
        branch = rec["branch"]
        feedback = _pr_review_feedback(reviews)
        impl = self.implementer.run(ticket, worktree, branch, feedback=feedback)
        rec["attempts"] = rec.get("attempts", 0) + 1
        self.log.info(
            "#PR%s revised: %d files, %d iters, $%.2f%s",
            number,
            len(impl.files_changed),
            impl.iterations,
            impl.cost_usd,
            " [error]" if impl.error else "",
        )

        # Re-run guards on the revised diff — a revision that breaks scope/tests
        # is deferred rather than pushed.
        ctx = GuardContext(
            ticket=ticket,
            config=self.config,
            repo_dir=worktree,
            diff=impl.diff,
            files_changed=impl.files_changed,
            implementation=impl,
        )
        results = run_all(self.guards, ctx)
        failed = next((r for r in results if not r.passed), None)
        if failed is not None:
            self._defer_pr(
                number,
                rec,
                f"a guard failed while addressing the review: **`{failed.name}`** — {failed.reason}",
            )
            return True

        if not self.implementer.push_branch(worktree, branch):
            self._defer_pr(
                number,
                rec,
                f"idle-loop revised the PR but could not push branch `{branch}`.",
            )
            return True

        self._comment(
            ticket,
            f"🤖 **idle-loop addressed the latest review on this PR** "
            f"(revision {rec['attempts']}/{self.config.budget.review_iterations}).\n\n{SHIPPED_BY}",
        )
        return True

    def _defer_pr(self, number: int, rec: dict, reason: str) -> None:
        """Hand a watched PR back to a human: drop idle:listen, flag needs-human.

        Marks ``rec`` deferred so the caller drops it from the watch state.
        """
        labels = self.config.labels
        try:
            self.github.remove_label(number, labels.listen)
        except GitHubError as exc:
            self.log.warning("remove %s from PR #%s failed: %s", labels.listen, number, exc)
        self._label_num(number, labels.needs_human)
        self._comment_num(
            number,
            f"🅿️ **idle-loop deferred this PR to a human.**\n\n{reason}\n\n{SHIPPED_BY}",
        )
        rec["_deferred"] = True
        self.log.info("#PR%s deferred to human: %s", number, reason)

    # ------------------------------------------------------------------ #
    # Cost log
    # ------------------------------------------------------------------ #
    def _record(
        self,
        ticket: Ticket,
        estimate: EstimateResult | None,
        impl: ImplementationResult | None,
        outcome: Outcome,
    ) -> RunRecord:
        if estimate is not None:
            features = estimate.features.to_dict()
            est_cost = estimate.estimated_cost
            est_iters = estimate.estimated_iterations
        else:
            try:
                features = self.estimator.extract_features(ticket).to_dict()
            except Exception:  # noqa: BLE001
                features = {}
            est_cost = 0.0
            est_iters = 0.0
        record = RunRecord(
            ticket_id=ticket.number,
            title=ticket.title,
            features=features,
            estimated_cost=est_cost,
            estimated_iterations=est_iters,
            actual_cost=impl.cost_usd if impl else 0.0,
            actual_iterations=impl.iterations if impl else 0,
            outcome=str(outcome),
        )
        try:
            with self._cost_log_lock:
                append_record(record, self.config.cost_log_path)
        except Exception as exc:  # noqa: BLE001 - logging must not crash the loop
            self.log.warning("append to cost log failed: %s", exc)
        return record


# --------------------------------------------------------------------------- #
# Formatting helpers
# --------------------------------------------------------------------------- #
def _shield_url(label: str, message: str, color: str) -> str:
    """A shields.io badge URL — a literal pill/chip rendered on the issue.

    Per shields.io's static-badge syntax, literal dashes/underscores in the
    label or message must be doubled before URL-encoding.
    """
    def enc(text: str) -> str:
        return urllib.parse.quote(text.replace("_", "__").replace("-", "--"), safe="")

    return f"https://img.shields.io/badge/{enc(label)}-{enc(message)}-{color}"


def _cost_badges(
    estimate: EstimateResult, spent: float | None, spent_tokens: int | None = None
) -> str:
    """Render the cost/token shields block: a cost badge and a sibling tokens badge.

    Before any spend each badge shows its estimate; once an iteration completes
    they show ``spent / estimate``. The token estimate is the planning pass's
    budget and reads ``n/a`` when the estimate fell back to the dollar-only
    heuristic. Shared by the sticky issue chip and the PR body.
    """
    band = estimate.band()
    est_tokens = estimate.estimated_tokens

    # Cost badge + caption.
    if spent is None:
        cost_url = _shield_url("idle-loop cost", f"est {band}", "blue")
        cost_caption = f"**idle-loop cost estimate:** {band}"
    else:
        cost_url = _shield_url(
            "idle-loop cost", f"${spent:.2f} spent / est {band}", "brightgreen"
        )
        cost_caption = f"**idle-loop cost so far:** ${spent:.2f} _(estimate {band})_"

    # Sibling tokens badge + caption. Spent tokens are always shown once known;
    # the estimate reads "n/a" when the dollar-only heuristic produced no budget.
    est_str = format_tokens(est_tokens) if est_tokens > 0 else "n/a"
    if spent_tokens is None:
        tok_msg = f"est {est_str}"
        tok_color = "blue" if est_tokens > 0 else "lightgrey"
        tok_caption = f"**tokens (est):** {est_str}"
    else:
        tok_msg = f"{format_tokens(spent_tokens)} / est {est_str}"
        tok_color = "brightgreen"
        tok_caption = f"**tokens:** {format_tokens(spent_tokens)} _(est {est_str})_"
    tok_url = _shield_url("idle-loop tokens", tok_msg, tok_color)

    return (
        f"![idle-loop cost]({cost_url})\n"
        f"![idle-loop tokens]({tok_url})\n\n"
        f"{cost_caption} · {tok_caption}"
    )


def _cost_chip_body(
    estimate: EstimateResult, spent: float | None, spent_tokens: int | None = None
) -> str:
    """The sticky cost-chip comment body: the marker plus the cost/token badges."""
    return f"{COST_CHIP_MARKER}\n{_cost_badges(estimate, spent, spent_tokens)}"


def _gates_summary(results: list[GuardResult]) -> str:
    if not results:
        return "_No guards run._"
    lines = ["**Gates:**"]
    for r in results:
        mark = "✅" if r.passed else "❌"
        lines.append(f"- {mark} `{r.name}` — {r.reason or ('passed' if r.passed else 'failed')}")
    return "\n".join(lines)


def _review_feedback(verdict: ReviewVerdict) -> str:
    """Render a reviewer verdict as actionable feedback for the implementer."""
    lines = [verdict.summary.strip()] if verdict.summary.strip() else []
    for c in verdict.comments:
        loc = c.path or "(general)"
        if c.line:
            loc += f":{c.line}"
        lines.append(f"- {loc}: {c.body}")
    return "\n".join(lines) if lines else "The reviewer requested changes."


def _pr_review_feedback(reviews: list[dict]) -> str:
    """Render one or more GitHub PR reviews as actionable implementer feedback."""
    lines: list[str] = []
    for r in reviews:
        who = r.get("user") or "a reviewer"
        state = (r.get("state") or "").replace("_", " ").lower()
        body = (r.get("body") or "").strip()
        header = f"{who} ({state}):" if state else f"{who}:"
        lines.append(f"{header} {body}" if body else header)
    joined = "\n".join(lines).strip()
    return joined or "A reviewer requested changes on the PR."


def _estimate_detail(estimate: EstimateResult) -> str:
    f = estimate.features
    return (
        "<details><summary>Estimate breakdown</summary>\n\n"
        f"- criteria: {f.n_criteria}\n"
        f"- est. files touched: {f.est_files}\n"
        f"- needs tests: {f.needs_tests}\n"
        f"- ambiguity: {f.ambiguity_score:.2f}\n"
        f"- est. iterations: {estimate.estimated_iterations:.1f}\n"
        f"- confidence: {estimate.confidence:.2f}\n"
        "</details>"
    )


def _park_message(verdict: ReviewVerdict, pr: dict | None, require_human: bool) -> str:
    pr_link = f" (PR {pr['html_url']})" if pr and pr.get("html_url") else ""
    if verdict.approved and require_human:
        head = f"🅿️ **Parked for human approval** — review approved but `merge.require_human` is on{pr_link}."
    else:
        head = f"🅿️ **Parked: reviewer requested changes**{pr_link}."
    parts = [head]
    if verdict.summary:
        parts.append(f"\n**Reviewer summary:** {verdict.summary}")
    if verdict.comments:
        parts.append("\n**Reviewer comments:**")
        for c in verdict.comments:
            loc = f"`{c.path}`" + (f":{c.line}" if c.line else "") + " — " if c.path else ""
            parts.append(f"- {loc}{c.body}")
    parts.append(f"\n{SHIPPED_BY}")
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="idle-loop",
        description="The guard-railed autonomous coding loop you can actually walk away from.",
    )
    parser.add_argument("--config", default="idle.config.yaml", help="path to idle.config.yaml")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="list ready issues with cost estimates, taking no action",
    )
    parser.add_argument(
        "--repo-dir",
        default=".",
        help="working directory where the implementer operates on branches",
    )
    parser.add_argument(
        "--max-tickets",
        type=int,
        default=None,
        help="process at most N tickets this run",
    )
    parser.add_argument(
        "--max-parallel",
        type=int,
        default=None,
        help="work at most N tickets concurrently, each in its own git worktree "
        "(default: budget.max_parallel; 1 = sequential)",
    )
    parser.add_argument(
        "--ensure-labels",
        action="store_true",
        help="create/update the idle:* labels on the repo and exit "
        "(no loop, no harness — needs only a GitHub token)",
    )
    parser.add_argument(
        "--watch-reviews",
        action="store_true",
        help="check open idle:listen PRs for new reviews and address requested "
        "changes by resuming each PR's saved context, then exit (no ticket pass)",
    )
    return parser


def ensure_labels(config: Config) -> list[str]:
    """Create the idle:* labels on the target repo. Returns the label names.

    A standalone, GitHub-token-only action (no agents, no harness) suitable for
    CI — the only loop side effect that's static and safe to run unattended.
    """
    labels = config.labels
    names = [
        labels.ready,
        labels.needs_human,
        labels.allow_sensitive,
        labels.listen,
        labels.in_progress,
    ]
    GitHubClient(config.repo).ensure_labels(names)
    log.info("ensured labels on %s: %s", config.repo, ", ".join(names))
    return names


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = load_config(args.config)

    if args.ensure_labels:
        ensure_labels(config)
        return EXIT_OK

    orch = Orchestrator.from_config(config, repo_dir=args.repo_dir)
    try:
        if args.watch_reviews:
            orch.watch_reviews()
        else:
            orch.run(
                max_tickets=args.max_tickets,
                dry_run=args.dry_run,
                max_parallel=args.max_parallel,
            )
    except HarnessRateLimited:
        # State + marker already emitted by Orchestrator._on_rate_limit.
        return EXIT_RATE_LIMITED
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
