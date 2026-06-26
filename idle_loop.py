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
    ) -> None:
        self.config = config
        self.github = github
        self.estimator = estimator
        self.implementer = implementer
        self.reviewer = reviewer
        # Post-implementation guards, run in order (fail-closed).
        self.guards: list[Guard] = list(guards)
        self.repo_dir = repo_dir
        self.log = logger or log
        self._repo_tree_cache: list[str] | None = None
        # Serializes cost-log appends across parallel ticket workers.
        self._cost_log_lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # Construction
    # ------------------------------------------------------------------ #
    @classmethod
    def from_config(cls, config: Config, repo_dir: str = ".") -> Orchestrator:
        """Build an orchestrator with the real production dependencies."""
        # Local import keeps anthropic out of the import path for non-agent uses.
        from agents.implementer import Implementer
        from agents.reviewer import Reviewer

        estimator = Estimator(config)
        return cls(
            config=config,
            github=GitHubClient(config.repo),
            estimator=estimator,
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
        """Fetch open ``idle:ready`` issues, oldest first."""
        return self.github.list_ready_issues(self.config.labels.ready)

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
    # Per-ticket processing (SPEC steps 2-9)
    # ------------------------------------------------------------------ #
    def process_ticket(self, ticket: Ticket, repo_dir: str | None = None) -> RunRecord:
        """Process one ticket end-to-end. Never raises (except rate-limit).

        ``repo_dir`` is the working tree the implementer commits in; it defaults
        to the orchestrator's own checkout, but is overridden with a per-ticket
        git worktree when tickets are worked in parallel.
        """
        repo_dir = repo_dir or self.repo_dir
        try:
            return self._process_ticket(ticket, repo_dir)
        except HarnessRateLimited:
            raise  # stop the whole loop; the listener will reschedule
        except Exception as exc:  # noqa: BLE001 - one bad ticket must not kill the loop
            self.log.exception("ticket #%s crashed: %s", ticket.number, exc)
            self._park(
                ticket,
                f"idle-loop hit an unexpected error processing this ticket: {exc}",
            )
            return self._record(ticket, None, None, Outcome.FAILED)

    def _process_ticket(self, ticket: Ticket, repo_dir: str) -> RunRecord:
        labels = self.config.labels

        # (1) Reject tickets without acceptance criteria — never guess (SPEC §4).
        if not ticket.acceptance_criteria:
            self.log.info("#%s rejected: no acceptance criteria", ticket.number)
            self._comment(
                ticket,
                "**idle-loop skipped this ticket: no acceptance criteria.**\n\n"
                "Add an `## Acceptance Criteria` checklist so the loop can verify "
                "the change against concrete, testable conditions.",
            )
            self._label(ticket, labels.needs_human)
            return self._record(ticket, None, None, Outcome.SKIPPED)

        # (2) Estimate — price before spending tokens.
        estimate = self.estimator.estimate(ticket, self._repo_tree())
        band = estimate.band()
        self.log.info(
            "#%s estimate %s (%.1f iters, conf %.2f)",
            ticket.number,
            band,
            estimate.estimated_iterations,
            estimate.confidence,
        )
        # Surface the price on the issue itself as a chip the moment it's known.
        self._cost_chip(ticket, estimate)

        # (3) Triage — park anything over the auto threshold before any tokens.
        threshold = self.config.triage.auto_threshold_usd
        if estimate.estimated_cost > threshold:
            self.log.info("#%s over threshold $%g -> needs human", ticket.number, threshold)
            self._comment(
                ticket,
                f"**idle-loop estimate: {band}** — above the ${threshold:g} "
                "auto-run threshold, so parking for a human to triage.\n\n"
                + _estimate_detail(estimate),
            )
            self._label(ticket, labels.needs_human)
            return self._record(ticket, estimate, None, Outcome.SKIPPED)

        # (4) Implement on a fresh isolated branch.
        branch = f"idle/issue-{ticket.number}"
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
        # Iteration done — refresh the chip with the running actual cost.
        self._cost_chip(ticket, estimate, spent=impl.cost_usd)

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
        pr = self._open_pr(ticket, branch, impl, results, repo_dir)
        if pr is None:
            self.log.info("#%s could not push branch / open PR; parking", ticket.number)
            self._park(
                ticket,
                "idle-loop implemented the change but could not push the branch or "
                f"open a PR (branch `{branch}`). Check the loop's git remote/permissions.",
            )
            return self._record(ticket, estimate, impl, Outcome.PARKED)

        # (7) Review against the acceptance criteria (separate agent/context),
        # then iterate on the SAME branch/PR: feed the reviewer's requested
        # changes back to the implementer up to budget.review_iterations times
        # before parking. The open PR updates in place on each push — no new PR.
        verdict = self.reviewer.review(ticket, impl.diff)
        self.log.info("#%s review: %s", ticket.number, verdict.decision)

        total_cost = impl.cost_usd
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
            self.log.info(
                "#%s revised: %d files, %d iters, $%.2f (total $%.2f)%s",
                ticket.number,
                len(impl.files_changed),
                impl.iterations,
                impl.cost_usd,
                total_cost,
                " [error]" if impl.error else "",
            )
            # Iteration done — refresh the chip with the cumulative spend.
            self._cost_chip(ticket, estimate, spent=total_cost)

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
        self._label(ticket, self.config.labels.needs_human)
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
            # resume in it (with saved agent context) on a later run.
            worktree = self._make_worktree(ticket)
            return self.process_ticket(ticket, repo_dir=worktree)

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

    def _worktree_path(self, number: int) -> str:
        return os.path.join(self.repo_dir, WORKTREE_DIR, f"issue-{number}")

    def _make_worktree(self, ticket: Ticket) -> str:
        """Return an isolated git worktree for ``ticket``, creating it if needed.

        An existing worktree for the ticket is reused as-is — its branch and the
        agent's prior work are preserved so the implementer continues where it
        left off. A new worktree is checked out (detached) at the target branch
        so the implementer's own ``checkout -b idle/issue-N`` starts clean.
        """
        path = self._worktree_path(ticket.number)
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
            if not name.startswith("issue-"):
                continue
            try:
                number = int(name[len("issue-"):])
            except ValueError:
                continue
            branch = f"idle/issue-{number}"
            try:
                status = self.github.pr_status_for_branch(branch)
            except GitHubError as exc:
                self.log.warning("reap: PR status for %s failed: %s", branch, exc)
                continue
            if status == "done":
                self.log.info("#%s PR merged/closed — reclaiming worktree", number)
                self._remove_worktree(os.path.join(root, name))

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
            self.github.ensure_labels([labels.ready, labels.needs_human, labels.allow_sensitive])
        except GitHubError as exc:
            self.log.warning("ensure_labels failed (continuing): %s", exc)

    def _comment(self, ticket: Ticket, body: str) -> None:
        try:
            self.github.comment(ticket.number, body)
        except GitHubError as exc:
            self.log.warning("comment on #%s failed: %s", ticket.number, exc)

    def _label(self, ticket: Ticket, label: str) -> None:
        try:
            self.github.add_label(ticket.number, label)
        except GitHubError as exc:
            self.log.warning("add_label %s on #%s failed: %s", label, ticket.number, exc)

    def _park(self, ticket: Ticket, reason: str) -> None:
        """Comment a crisp reason and flag the ticket for a human."""
        self._comment(ticket, f"🅿️ **idle-loop parked this ticket.**\n\n{reason}")
        self._label(ticket, self.config.labels.needs_human)

    def _cost_chip(
        self,
        ticket: Ticket,
        estimate: EstimateResult,
        spent: float | None = None,
    ) -> None:
        """Upsert the sticky cost chip on the issue (estimate, then spend so far).

        Best-effort: a GitHub error here must never derail the loop — the chip is
        a convenience for humans watching the board, not part of the contract.
        """
        try:
            self.github.upsert_comment(
                ticket.number, COST_CHIP_MARKER, _cost_chip_body(estimate, spent)
            )
        except GitHubError as exc:
            self.log.warning("cost chip on #%s failed: %s", ticket.number, exc)

    def _open_pr(
        self,
        ticket: Ticket,
        branch: str,
        impl: ImplementationResult,
        results: list[GuardResult],
        repo_dir: str | None = None,
    ) -> dict | None:
        # Push the implementation branch first — GitHub can't open a PR for a
        # head ref that only exists locally.
        if not self.implementer.push_branch(repo_dir or self.repo_dir, branch):
            self.log.warning("#%s push of branch %s failed", ticket.number, branch)
            return None
        body = (
            f"Closes #{ticket.number}\n\n"
            f"Automated implementation by **idle-loop**.\n\n"
            f"**Files changed:** {len(impl.files_changed)} | "
            f"**Iterations:** {impl.iterations} | "
            f"**Actual cost:** ${impl.cost_usd:.2f}\n\n"
            f"{_gates_summary(results)}\n\n"
            f"{SHIPPED_BY}"
        )
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

    def _merge(self, pr: dict | None) -> bool:
        if not pr:
            return False
        try:
            return self.github.merge_pull_request(pr["number"])
        except GitHubError as exc:
            self.log.warning("merge PR #%s failed: %s", pr.get("number"), exc)
            return False

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


def _cost_chip_body(estimate: EstimateResult, spent: float | None) -> str:
    """Render the sticky cost-chip comment body (carries ``COST_CHIP_MARKER``).

    Before any spend it shows the estimate band; once an iteration completes it
    shows the running actual cost alongside the original estimate.
    """
    band = estimate.band()
    if spent is None:
        message = f"est {band}"
        url = _shield_url("idle-loop cost", message, "blue")
        caption = f"**idle-loop cost estimate:** {band}"
    else:
        message = f"${spent:.2f} spent / est {band}"
        url = _shield_url("idle-loop cost", message, "brightgreen")
        caption = f"**idle-loop cost so far:** ${spent:.2f} _(estimate {band})_"
    return f"{COST_CHIP_MARKER}\n![idle-loop cost]({url})\n\n{caption}"


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
    return parser


def ensure_labels(config: Config) -> list[str]:
    """Create the idle:* labels on the target repo. Returns the label names.

    A standalone, GitHub-token-only action (no agents, no harness) suitable for
    CI — the only loop side effect that's static and safe to run unattended.
    """
    labels = config.labels
    names = [labels.ready, labels.needs_human, labels.allow_sensitive]
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
