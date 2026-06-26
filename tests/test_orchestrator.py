"""Integration tests for the orchestrator (idle_loop.Orchestrator).

Everything external is faked — no network, no Anthropic, no git, no pytest
subprocess. Guards are simple pass/fail fakes so we exercise the orchestration
logic (triage, parking, merge gate, global cap, cost logging) in isolation.
"""

from __future__ import annotations

import json
import os

import pytest

import idle_loop
from agents.harness import HarnessRateLimited
from agents.planner import PlanResult
from config import Config
from idle_loop import EXIT_RATE_LIMITED, Orchestrator
from models import (
    Decision,
    GuardResult,
    ImplementationResult,
    Outcome,
    ReviewComment,
    ReviewVerdict,
    Ticket,
)


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class FakeGitHub:
    def __init__(self, issues: list[Ticket]):
        self.issues = issues
        self.comments: list[tuple[int, str]] = []
        self.added_labels: list[tuple[int, str]] = []
        self.removed_labels: list[tuple[int, str]] = []
        self.prs: list[dict] = []
        self.merged: list[int] = []
        self.ensured: list[list[str]] = []
        # Sticky comments, keyed by (issue number, marker) -> latest body.
        self.sticky: dict[tuple[int, str], str] = {}
        self.upsert_calls: list[tuple[int, str, str]] = []
        self._pr_seq = 0
        # PR-review watching fixtures.
        self.labelled_prs: dict[str, list[dict]] = {}  # label -> [{number, title}]
        self.reviews: dict[int, list[dict]] = {}  # pr number -> [review dicts]
        self.pr_details: dict[int, dict] = {}  # pr number -> get_pull_request payload
        # Open PRs keyed by head branch, for find_open_pr_by_head (the reuse path).
        self.open_pr_by_head: dict[str, dict] = {}

    def list_ready_issues(self, label: str) -> list[Ticket]:
        # Mirror GitHubClient: the issues endpoint filters to the requested label.
        return [t for t in self.issues if label in t.labels]

    def find_open_pr_by_head(self, branch: str) -> dict | None:
        return self.open_pr_by_head.get(branch)

    def get_issue(self, number: int) -> Ticket:
        for t in self.issues:
            if t.number == number:
                return t
        return Ticket(number=number, title=f"#{number}", body="", labels=[])

    def list_pull_requests_by_label(self, label: str) -> list[dict]:
        return list(self.labelled_prs.get(label, []))

    def list_reviews(self, number: int) -> list[dict]:
        return list(self.reviews.get(number, []))

    def get_pull_request(self, number: int) -> dict:
        return self.pr_details.get(
            number, {"number": number, "head_branch": "", "state": "open", "html_url": ""}
        )

    def comment(self, number: int, body: str) -> None:
        self.comments.append((number, body))

    def upsert_comment(self, number: int, marker: str, body: str) -> None:
        # Mirror GitHubClient.upsert_comment: one sticky comment per marker.
        self.upsert_calls.append((number, marker, body))
        self.sticky[(number, marker)] = body

    def add_label(self, number: int, label: str) -> None:
        self.added_labels.append((number, label))

    def remove_label(self, number: int, label: str) -> None:
        self.removed_labels.append((number, label))

    def ensure_labels(self, names: list[str]) -> None:
        self.ensured.append(list(names))

    def create_pull_request(
        self, title: str, head: str, base: str, body: str, draft: bool = False
    ) -> dict:
        self._pr_seq += 1
        pr = {"number": self._pr_seq, "html_url": f"https://gh/pr/{self._pr_seq}"}
        self.prs.append(
            {**pr, "title": title, "head": head, "base": base, "body": body, "draft": draft}
        )
        return pr

    def merge_pull_request(self, number: int, method: str = "squash") -> bool:
        self.merged.append(number)
        return True

    def default_branch(self) -> str:
        return "main"


class FakeImplementer:
    def __init__(self, result: ImplementationResult, push_ok: bool = True):
        self.result = result
        self.push_ok = push_ok
        self.calls: list[tuple[int, str, str]] = []
        self.pushed: list[tuple[str, str]] = []
        self.feedbacks: list[str | None] = []

    def push_branch(self, repo_dir: str, branch: str) -> bool:
        self.pushed.append((repo_dir, branch))
        return self.push_ok

    def run(
        self, ticket: Ticket, repo_dir: str, branch: str, feedback: str | None = None
    ) -> ImplementationResult:
        self.calls.append((ticket.number, repo_dir, branch))
        self.feedbacks.append(feedback)
        # Return a copy with the actual branch name filled in.
        r = self.result
        return ImplementationResult(
            branch=branch,
            diff=r.diff,
            files_changed=list(r.files_changed),
            iterations=r.iterations,
            cost_usd=r.cost_usd,
            input_tokens=r.input_tokens,
            output_tokens=r.output_tokens,
            notes=r.notes,
            no_progress=r.no_progress,
            error=r.error,
        )


class FakeReviewer:
    def __init__(self, verdict: ReviewVerdict):
        self.verdict = verdict
        self.calls: list[int] = []

    def review(self, ticket: Ticket, diff: str) -> ReviewVerdict:
        self.calls.append(ticket.number)
        return self.verdict


class FakePlanner:
    def __init__(self, result: PlanResult | None = None, raises=None):
        self.result = result
        self.raises = raises
        self.calls: list[tuple[int, str]] = []

    def plan(self, ticket: Ticket, repo_dir: str) -> PlanResult | None:
        self.calls.append((ticket.number, repo_dir))
        if self.raises is not None:
            raise self.raises
        return self.result


class FakeGuard:
    def __init__(self, name: str, passed: bool, reason: str = ""):
        self.name = name
        self._passed = passed
        self._reason = reason

    def check(self, ctx) -> GuardResult:  # noqa: ANN001 - test fake
        return GuardResult(name=self.name, passed=self._passed, reason=self._reason)


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #
def make_config(tmp_path, **over) -> Config:
    cfg = Config(repo="viksuper555/idle-loop")
    cfg.cost_log_path = str(tmp_path / "cost_log.jsonl")
    cfg.merge.require_human = over.get("require_human", False)
    if "threshold" in over:
        cfg.triage.auto_threshold_usd = over["threshold"]
    if "global_cap" in over:
        cfg.budget.global_cap_usd = over["global_cap"]
    # Default tests to the sequential path; parallel tests opt in explicitly.
    cfg.budget.max_parallel = over.get("max_parallel", 1)
    return cfg


def ticket(number=1, title="Add feature", criteria=("does X", "does Y"), body="A clear, well-specified ticket body that explains exactly what to build and why, with enough detail to be unambiguous.") -> Ticket:
    return Ticket(
        number=number,
        title=title,
        body=body,
        labels=["idle:ready"],
        url=f"https://gh/issues/{number}",
        acceptance_criteria=list(criteria),
    )


def good_impl(cost=10.0) -> ImplementationResult:
    return ImplementationResult(
        branch="idle/issue-1",
        diff="+print('x')\n",
        files_changed=["src/feature.py", "tests/test_feature.py"],
        iterations=3,
        cost_usd=cost,
        input_tokens=1000,
        output_tokens=500,
    )


def make_orch(tmp_path, *, issues, guards=None, verdict=None, impl=None, planner=None, **cfg_over):
    from guards.estimate import Estimator

    cfg = make_config(tmp_path, **cfg_over)
    gh = FakeGitHub(issues)
    estimator = Estimator(cfg, cost_log_path=cfg.cost_log_path)
    implementer = FakeImplementer(impl or good_impl())
    reviewer = FakeReviewer(verdict or ReviewVerdict(Decision.APPROVE, summary="lgtm"))
    guards = guards if guards is not None else [
        FakeGuard("scope", True),
        FakeGuard("tests", True),
        FakeGuard("security", True),
        FakeGuard("budget", True),
    ]
    orch = Orchestrator(
        config=cfg,
        github=gh,
        estimator=estimator,
        planner=planner,
        implementer=implementer,
        reviewer=reviewer,
        guards=guards,
        repo_dir=str(tmp_path),  # not a git repo -> estimator gets no tree (hermetic)
    )
    return orch, gh, implementer, reviewer


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
def test_dry_run_lists_issues_and_takes_no_action(tmp_path, capsys):
    orch, gh, implementer, reviewer = make_orch(tmp_path, issues=[ticket(1), ticket(2)])
    records = orch.run(dry_run=True)
    out = capsys.readouterr().out
    assert "#1" in out and "#2" in out and "~$" in out
    assert records == []
    # No side effects whatsoever.
    assert gh.comments == [] and gh.added_labels == [] and gh.prs == []
    assert gh.ensured == [] and gh.merged == []
    assert implementer.calls == [] and reviewer.calls == []


def test_run_ensures_idle_labels_before_processing(tmp_path):
    # A normal run() must sync the idle:* labels before touching tickets — the
    # CI no longer owns this, so the loop itself guarantees they exist.
    orch, gh, implementer, reviewer = make_orch(
        tmp_path, issues=[ticket(1)], require_human=False
    )
    orch.run()
    assert gh.ensured == [
        [
            "idle:ready",
            "idle:needs-human",
            "idle:allow-sensitive",
            "idle:listen",
            "idle:in-progress",
            "idle:allow-budget",
        ]
    ]


def test_ticket_without_acceptance_criteria_is_skipped(tmp_path):
    orch, gh, implementer, reviewer = make_orch(tmp_path, issues=[ticket(1, criteria=[])])
    rec = orch.process_ticket(ticket(1, criteria=[]))
    assert rec.outcome == Outcome.SKIPPED
    assert implementer.calls == []  # never implemented
    assert any("acceptance criteria" in body.lower() for _, body in gh.comments)
    assert (1, "idle:needs-human") in gh.added_labels


def test_over_threshold_estimate_parks_before_implementation(tmp_path):
    orch, gh, implementer, reviewer = make_orch(
        tmp_path, issues=[ticket(1)], threshold=1.0
    )
    rec = orch.process_ticket(ticket(1))
    assert rec.outcome == Outcome.SKIPPED
    assert rec.estimated_cost > 1.0
    assert implementer.calls == []  # priced out before spending tokens
    assert (1, "idle:needs-human") in gh.added_labels
    assert any("estimate" in body.lower() for _, body in gh.comments)


def test_happy_path_merges_when_approved_and_no_human_required(tmp_path):
    orch, gh, implementer, reviewer = make_orch(
        tmp_path, issues=[ticket(1)], require_human=False
    )
    rec = orch.process_ticket(ticket(1))
    assert rec.outcome == Outcome.MERGED
    assert len(gh.prs) == 1
    assert gh.merged == [1]
    assert reviewer.calls == [1]
    assert rec.actual_cost == 10.0 and rec.actual_iterations == 3


def test_posthoc_guard_failure_opens_draft_pr_not_stranded(tmp_path):
    # A post-implementation guard failure on committed work must open a DRAFT PR
    # (work + reason, needs-human), never park with the work stranded locally.
    guards = [FakeGuard("tests", False, "pytest failed: 2 failures")]
    orch, gh, implementer, reviewer = make_orch(
        tmp_path, issues=[ticket(1)], guards=guards, require_human=False
    )
    rec = orch.process_ticket(ticket(1))
    assert rec.outcome == Outcome.PARKED
    assert implementer.calls  # the work WAS done (spent) ...
    assert len(gh.prs) == 1  # ... so a PR exists — not stranded
    assert gh.prs[0]["draft"] is True  # opened as a draft (blocked)
    assert implementer.pushed[0][1] == "feature/issue-1"  # branch pushed
    assert gh.merged == []
    assert reviewer.calls == []  # never reached review
    assert (1, "idle:needs-human") in gh.added_labels
    assert any("tests" in body for _, body in gh.comments)


def test_reviewer_request_changes_parks_open_pr(tmp_path):
    verdict = ReviewVerdict(
        Decision.REQUEST_CHANGES,
        summary="criterion 2 unmet",
        comments=[ReviewComment(body="no test for Y", path="src/feature.py", line=10)],
    )
    orch, gh, implementer, reviewer = make_orch(
        tmp_path, issues=[ticket(1)], verdict=verdict, require_human=False
    )
    rec = orch.process_ticket(ticket(1))
    assert rec.outcome == Outcome.PARKED
    assert len(gh.prs) == 1  # PR was opened, then parked
    assert gh.merged == []
    assert (1, "idle:needs-human") in gh.added_labels
    assert any("criterion 2 unmet" in body for _, body in gh.comments)


class SequenceReviewer:
    """Returns a scripted sequence of verdicts, repeating the last one."""

    def __init__(self, verdicts):
        self.verdicts = list(verdicts)
        self.calls: list[int] = []

    def review(self, ticket: Ticket, diff: str) -> ReviewVerdict:
        self.calls.append(ticket.number)
        i = min(len(self.calls) - 1, len(self.verdicts) - 1)
        return self.verdicts[i]


def test_reviewer_changes_then_approve_revises_same_branch_and_merges(tmp_path):
    # request_changes first, approve on the revision.
    reviewer = SequenceReviewer(
        [
            ReviewVerdict(
                Decision.REQUEST_CHANGES,
                summary="add the test",
                comments=[ReviewComment(body="cover Y", path="src/feature.py", line=10)],
            ),
            ReviewVerdict(Decision.APPROVE, summary="now good"),
        ]
    )
    orch, gh, implementer, _ = make_orch(
        tmp_path, issues=[ticket(1)], require_human=False
    )
    orch.reviewer = reviewer

    rec = orch.process_ticket(ticket(1))

    assert rec.outcome == Outcome.MERGED
    assert len(gh.prs) == 1  # one PR, updated in place — not a second PR
    assert len(implementer.calls) == 2  # initial + one revision
    assert implementer.feedbacks[0] is None  # initial pass, no feedback
    assert implementer.feedbacks[1] and "cover Y" in implementer.feedbacks[1]
    assert len(implementer.pushed) >= 2  # initial push + revision push


def test_reviewer_persistent_changes_parks_after_review_iterations(tmp_path):
    verdict = ReviewVerdict(Decision.REQUEST_CHANGES, summary="still wrong")
    orch, gh, implementer, _ = make_orch(
        tmp_path, issues=[ticket(1)], verdict=verdict, require_human=False
    )
    orch.config.budget.review_iterations = 2

    rec = orch.process_ticket(ticket(1))

    assert rec.outcome == Outcome.PARKED
    assert len(gh.prs) == 1
    assert gh.merged == []
    # initial implementation + 2 bounded revision attempts, then park.
    assert len(implementer.calls) == 3


def test_approved_but_require_human_does_not_merge(tmp_path):
    orch, gh, implementer, reviewer = make_orch(
        tmp_path, issues=[ticket(1)], require_human=True
    )
    rec = orch.process_ticket(ticket(1))
    assert rec.outcome == Outcome.PARKED
    assert len(gh.prs) == 1
    assert gh.merged == []  # require_human gates the merge
    assert (1, "idle:needs-human") in gh.added_labels


def test_global_cap_stops_the_loop(tmp_path):
    # Each ticket costs $10; cap is $5 -> stop after the first.
    orch, gh, implementer, reviewer = make_orch(
        tmp_path,
        issues=[ticket(1), ticket(2), ticket(3)],
        require_human=False,
        global_cap=5.0,
    )
    records = orch.run()
    assert len(records) == 1
    assert records[0].outcome == Outcome.MERGED


def test_parallel_processes_all_tickets_in_isolated_worktrees(tmp_path):
    # 3 tickets, 2 at a time. Fake the worktree helpers (no real git) and assert
    # each ticket is processed in its own worktree path.
    orch, gh, implementer, reviewer = make_orch(
        tmp_path,
        issues=[ticket(1), ticket(2), ticket(3)],
        require_human=False,
        max_parallel=2,
    )
    made: list[int] = []
    removed: list[str] = []

    def fake_make(ticket, branch):
        path = str(tmp_path / f"wt-{ticket.number}")
        made.append(ticket.number)
        return path

    orch._make_worktree = fake_make
    orch._remove_worktree = lambda p: removed.append(p)

    records = orch.run()

    assert {r.ticket_id for r in records} == {1, 2, 3}
    assert all(r.outcome == Outcome.MERGED for r in records)
    assert sorted(made) == [1, 2, 3]  # one worktree per ticket
    # Worktrees persist past the pass — reclaimed later by reap, not here.
    assert removed == []
    # Each implementer run got a distinct per-ticket worktree, not the shared dir.
    repo_dirs = {repo_dir for _, repo_dir, _ in implementer.calls}
    assert repo_dirs == {str(tmp_path / "wt-1"), str(tmp_path / "wt-2"), str(tmp_path / "wt-3")}


def test_reap_removes_only_finished_pr_worktrees(tmp_path):
    orch, gh, implementer, reviewer = make_orch(tmp_path, issues=[])
    root = tmp_path / ".idle-worktrees"
    for n in (1, 2, 3):
        (root / f"issue-{n}").mkdir(parents=True)
    # #1 merged/closed -> reap; #2 still open -> keep; #3 no PR yet -> keep.
    status = {"idle/issue-1": "done", "idle/issue-2": "open", "idle/issue-3": "none"}
    gh.pr_status_for_branch = lambda branch: status[branch]
    removed: list[str] = []
    orch._remove_worktree = lambda p: removed.append(p)

    orch._reap_worktrees()

    assert removed == [str(root / "issue-1")]


def test_max_parallel_one_uses_sequential_path(tmp_path):
    # max_parallel=1 must not create worktrees at all.
    orch, gh, implementer, reviewer = make_orch(
        tmp_path, issues=[ticket(1), ticket(2)], require_human=False, max_parallel=1
    )
    called = []
    orch._make_worktree = lambda t, b: called.append(t.number) or str(tmp_path)
    records = orch.run()
    assert len(records) == 2
    assert called == []  # sequential path never makes a worktree


def test_run_records_are_appended_to_cost_log(tmp_path):
    import costlog

    orch, gh, implementer, reviewer = make_orch(
        tmp_path, issues=[ticket(1), ticket(2)], require_human=False
    )
    records = orch.run()
    assert len(records) == 2
    rows = costlog.read_all(str(tmp_path / "cost_log.jsonl"))
    assert len(rows) == 2
    assert {r.outcome for r in rows} == {"merged"}
    assert all(r.actual_iterations == 3 for r in rows)


def test_process_ticket_never_raises_on_crash(tmp_path):
    class BoomImplementer:
        def run(self, *a, **k):
            raise RuntimeError("boom")

    orch, gh, implementer, reviewer = make_orch(tmp_path, issues=[ticket(1)])
    orch.implementer = BoomImplementer()
    rec = orch.process_ticket(ticket(1))
    assert rec.outcome == Outcome.FAILED
    assert (1, "idle:needs-human") in gh.added_labels


class RateLimitedImplementer:
    def run(self, ticket, repo_dir, branch):
        raise HarnessRateLimited(reset_at=2_000_000_000.0, reset_human="usage limit; resets 2:10am")


def test_rate_limit_stops_loop_and_writes_state(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(idle_loop, "RATE_LIMIT_STATE", str(tmp_path / "rate_limit.json"))
    orch, gh, implementer, reviewer = make_orch(
        tmp_path, issues=[ticket(1), ticket(2)], require_human=False
    )
    orch.implementer = RateLimitedImplementer()

    with pytest.raises(HarnessRateLimited):
        orch.run()

    # Loop stopped immediately — no PR, no merge, no second ticket.
    assert gh.prs == [] and gh.merged == []
    # Reset time persisted for the listener, and the marker line printed.
    data = json.loads((tmp_path / "rate_limit.json").read_text())
    assert data["reset_epoch"] == 2_000_000_000.0
    assert "IDLE_LOOP_RATE_LIMITED" in capsys.readouterr().out


def test_main_returns_rate_limited_exit_code(tmp_path, monkeypatch):
    class Boom:
        def run(self, **k):
            raise HarnessRateLimited(reset_at=1.0, reset_human="limit")

    monkeypatch.setattr(idle_loop, "load_config", lambda p: Config(repo="o/n"))
    monkeypatch.setattr(
        idle_loop.Orchestrator,
        "from_config",
        classmethod(lambda cls, config, repo_dir=".": Boom()),
    )
    rc = idle_loop.main(["--repo-dir", str(tmp_path)])
    assert rc == EXIT_RATE_LIMITED


def test_ensure_labels_syncs_without_running_loop(monkeypatch):
    recorded: dict = {}

    class FakeGH:
        def __init__(self, repo):
            recorded["repo"] = repo

        def ensure_labels(self, names):
            recorded["names"] = list(names)

    def boom_from_config(*a, **k):  # the loop must NOT be constructed
        raise AssertionError("ensure-labels must not build the orchestrator")

    monkeypatch.setattr(idle_loop, "load_config", lambda p: Config(repo="o/n"))
    monkeypatch.setattr(idle_loop, "GitHubClient", FakeGH)
    monkeypatch.setattr(idle_loop.Orchestrator, "from_config", classmethod(boom_from_config))

    rc = idle_loop.main(["--ensure-labels"])
    assert rc == idle_loop.EXIT_OK
    assert recorded["repo"] == "o/n"
    assert recorded["names"] == [
        "idle:ready",
        "idle:needs-human",
        "idle:allow-sensitive",
        "idle:listen",
        "idle:in-progress",
        "idle:allow-budget",
    ]


def test_cost_chip_posted_on_estimate_then_updated_with_spend(tmp_path):
    # The chip is a sticky comment: first the estimate, then the running cost.
    orch, gh, implementer, reviewer = make_orch(
        tmp_path, issues=[ticket(1)], require_human=False
    )
    rec = orch.process_ticket(ticket(1))
    assert rec.outcome == Outcome.MERGED

    key = (1, idle_loop.COST_CHIP_MARKER)
    # Exactly one sticky chip survives (upserted in place, not duplicated).
    assert key in gh.sticky
    # First upsert was the estimate; the final body shows actual spend.
    assert gh.upsert_calls[0][1] == idle_loop.COST_CHIP_MARKER
    assert "cost estimate" in gh.upsert_calls[0][2]
    final = gh.sticky[key]
    assert "cost so far" in final
    assert "$10.00" in final  # good_impl() costs $10
    assert "img.shields.io" in final  # rendered as a chip/badge


def test_cost_chip_posted_even_when_priced_out(tmp_path):
    # Over the auto threshold: never implemented, but the estimate chip still
    # surfaces the price on the issue.
    orch, gh, implementer, reviewer = make_orch(
        tmp_path, issues=[ticket(1)], threshold=1.0
    )
    rec = orch.process_ticket(ticket(1))
    assert rec.outcome == Outcome.SKIPPED
    body = gh.sticky[(1, idle_loop.COST_CHIP_MARKER)]
    assert "cost estimate" in body and "img.shields.io" in body
    # No spend was incurred, so no "cost so far" update happened.
    assert all("cost so far" not in b for _, _, b in gh.upsert_calls)


def test_cost_chip_tracks_each_revision(tmp_path):
    # request_changes then approve: the chip's spend must reflect cumulative cost
    # across both iterations, updated after each one.
    reviewer = SequenceReviewer(
        [
            ReviewVerdict(Decision.REQUEST_CHANGES, summary="fix"),
            ReviewVerdict(Decision.APPROVE, summary="ok"),
        ]
    )
    orch, gh, implementer, _ = make_orch(
        tmp_path, issues=[ticket(1)], impl=good_impl(cost=4.0), require_human=False
    )
    orch.reviewer = reviewer
    orch.process_ticket(ticket(1))

    # Estimate + two iteration updates (initial $4, revision -> $8 total).
    spend_updates = [b for _, _, b in gh.upsert_calls if "cost so far" in b]
    assert "$4.00" in spend_updates[0]
    final = gh.sticky[(1, idle_loop.COST_CHIP_MARKER)]
    assert "$8.00" in final
    # Tokens are cumulative too: 2 x (1000 + 500) = 3000 -> "3k".
    assert "3k" in final


def test_dry_run_posts_no_cost_chip(tmp_path):
    orch, gh, implementer, reviewer = make_orch(tmp_path, issues=[ticket(1)])
    orch.run(dry_run=True)
    assert gh.upsert_calls == [] and gh.sticky == {}


# --------------------------------------------------------------------------- #
# Deterministic planner-based estimate
# --------------------------------------------------------------------------- #
def _plan(ein=1_000_000, eout=200_000, files=None) -> PlanResult:
    return PlanResult(
        estimated_input_tokens=ein,
        estimated_output_tokens=eout,
        plan_text="p",
        predicted_files=files or [],
        session_id="s",
    )


def test_planner_estimate_is_deterministic_and_shown_on_chip(tmp_path):
    # cost = cost_for_tokens(1M, 200k) = 1.0*5 + 0.2*25 = $10; tokens est = 1.2M.
    planner = FakePlanner(_plan(1_000_000, 200_000))
    orch, gh, implementer, _ = make_orch(
        tmp_path, issues=[ticket(1)], require_human=False, planner=planner
    )
    rec = orch.process_ticket(ticket(1))
    assert rec.outcome == Outcome.MERGED
    assert planner.calls == [(1, str(tmp_path))]
    assert rec.estimated_cost == pytest.approx(10.0)  # deterministic from tokens
    final = gh.sticky[(1, idle_loop.COST_CHIP_MARKER)]
    assert "idle-loop tokens" in final and "est 1.20M" in final


def test_planner_failure_falls_back_to_heuristic(tmp_path):
    planner = FakePlanner(result=None)  # unparseable budget -> None
    orch, gh, implementer, _ = make_orch(
        tmp_path, issues=[ticket(1)], require_human=False, planner=planner
    )
    rec = orch.process_ticket(ticket(1))
    assert rec.outcome == Outcome.MERGED
    assert planner.calls == [(1, str(tmp_path))]  # the planner WAS tried
    # Heuristic estimate carries no token budget -> the tokens badge reads "n/a".
    assert "est n/a" in gh.sticky[(1, idle_loop.COST_CHIP_MARKER)]


def test_resume_guard_skips_planning_when_session_exists(tmp_path):
    # A pre-existing session means the implementer is mid-flight; don't re-plan.
    from agents.implementer import save_session

    save_session(str(tmp_path), "sess-existing")
    planner = FakePlanner(_plan())
    orch, gh, implementer, _ = make_orch(
        tmp_path, issues=[ticket(1)], require_human=False, planner=planner
    )
    orch.process_ticket(ticket(1))
    assert planner.calls == []  # planning skipped to preserve the live session


def test_prefilter_skips_planning_for_obvious_park(tmp_path):
    # threshold=1.0 -> prefilter=$2.0; the heuristic prices a normal ticket well
    # above that, so the ticket parks without paying for a planning pass.
    planner = FakePlanner(_plan())
    orch, gh, implementer, _ = make_orch(
        tmp_path, issues=[ticket(1)], threshold=1.0, planner=planner
    )
    rec = orch.process_ticket(ticket(1))
    assert rec.outcome == Outcome.SKIPPED
    assert planner.calls == []  # never planned


def test_planner_rate_limit_propagates(tmp_path):
    planner = FakePlanner(raises=HarnessRateLimited(reset_at=1.0, reset_human="usage limit"))
    orch, gh, implementer, _ = make_orch(
        tmp_path, issues=[ticket(1)], require_human=False, planner=planner
    )
    with pytest.raises(HarnessRateLimited):
        orch.process_ticket(ticket(1))


def test_dry_run_does_not_invoke_planner(tmp_path):
    planner = FakePlanner(_plan())
    orch, gh, implementer, _ = make_orch(tmp_path, issues=[ticket(1)], planner=planner)
    orch.run(dry_run=True)
    assert planner.calls == []


# --------------------------------------------------------------------------- #
# Pre-flight scope: escalate on a predicted violation, never spend (#32)
# --------------------------------------------------------------------------- #
def test_preflight_scope_denylist_escalates_without_spending(tmp_path):
    # Planner predicts a denylisted path -> escalate BEFORE implementing.
    planner = FakePlanner(_plan(files=["infra/deploy.tf"]))
    orch, gh, implementer, _ = make_orch(tmp_path, issues=[ticket(1)], planner=planner)
    rec = orch.process_ticket(ticket(1))
    assert rec.outcome == Outcome.SKIPPED
    assert planner.calls == [(1, str(tmp_path))]  # the cheap plan ran ...
    assert implementer.calls == []  # ... but no implementation budget was spent
    assert gh.prs == []  # no work, so no PR (the only valid "no PR" outcome)
    assert (1, "idle:needs-human") in gh.added_labels
    assert any("allow-sensitive" in body for _, body in gh.comments)


def test_preflight_scope_outside_allowlist_escalates(tmp_path):
    planner = FakePlanner(_plan(files=["lib/x.py"]))  # neither allow- nor denylisted
    orch, gh, implementer, _ = make_orch(tmp_path, issues=[ticket(1)], planner=planner)
    rec = orch.process_ticket(ticket(1))
    assert rec.outcome == Outcome.SKIPPED
    assert implementer.calls == []


def test_preflight_scope_waived_by_allow_sensitive(tmp_path):
    # The allow-sensitive label opts past the path bound: implement as normal.
    t = ticket(1)
    t.labels = ["idle:ready", "idle:allow-sensitive"]
    planner = FakePlanner(_plan(files=["infra/deploy.tf"]))
    orch, gh, implementer, _ = make_orch(
        tmp_path, issues=[t], planner=planner, require_human=False
    )
    rec = orch.process_ticket(t)
    assert implementer.calls  # the violation is waived -> work proceeds
    assert rec.outcome == Outcome.MERGED


def test_preflight_scope_allows_in_allowlist_predicted_files(tmp_path):
    # Predicted files inside the allowlist -> no escalation, happy path runs.
    planner = FakePlanner(_plan(files=["src/feature.py", "tests/test_feature.py"]))
    orch, gh, implementer, _ = make_orch(
        tmp_path, issues=[ticket(1)], planner=planner, require_human=False
    )
    rec = orch.process_ticket(ticket(1))
    assert rec.outcome == Outcome.MERGED
    assert implementer.calls  # implemented normally
    assert len(gh.prs) == 1 and gh.prs[0]["draft"] is False


def test_branch_pushed_before_pr(tmp_path):
    orch, gh, implementer, reviewer = make_orch(
        tmp_path, issues=[ticket(1)], require_human=False
    )
    orch.process_ticket(ticket(1))
    # The branch was pushed to origin before the PR was opened.
    assert implementer.pushed and implementer.pushed[0][1] == "feature/issue-1"
    assert len(gh.prs) == 1 and gh.merged == [1]


def test_failed_push_parks_without_pr(tmp_path):
    orch, gh, implementer, reviewer = make_orch(
        tmp_path, issues=[ticket(1)], require_human=False
    )
    implementer.push_ok = False  # simulate a push failure
    rec = orch.process_ticket(ticket(1))
    assert rec.outcome == Outcome.PARKED
    assert gh.prs == [] and gh.merged == []  # no PR opened, nothing merged
    assert reviewer.calls == []  # never reached review
    assert (1, "idle:needs-human") in gh.added_labels


# --------------------------------------------------------------------------- #
# Unified artefacts: branch prefixes, dedupe, PR template (ticket #15)
# --------------------------------------------------------------------------- #
def test_branch_prefix_derived_from_ticket_label(tmp_path):
    bug = ticket(1)
    bug.labels = ["idle:ready", "bug"]
    orch, gh, implementer, reviewer = make_orch(
        tmp_path, issues=[bug], require_human=False
    )
    orch.process_ticket(bug)
    # The branch (and so the worktree) follows <prefix>/issue-<id>.
    assert implementer.pushed[0][1] == "bugfix/issue-1"


def test_branch_dedupes_when_canonical_name_is_taken(tmp_path):
    orch, gh, implementer, reviewer = make_orch(
        tmp_path, issues=[ticket(1)], require_human=False
    )
    # Canonical head exists but carries NO idle-loop ownership marker (no
    # worktree, no pr_watch, no open PR) -> unrelated work -> dedupe to "-b".
    orch._branch_exists = lambda name: name == "feature/issue-1"
    orch.process_ticket(ticket(1))
    assert implementer.calls[0][2] == "feature/issue-1-b"


# --------------------------------------------------------------------------- #
# Resume on the existing branch instead of deduping to -b/-c (#26)
# --------------------------------------------------------------------------- #
def test_branch_for_resumes_existing_branch_via_worktree(tmp_path):
    # A worktree for the canonical name means we are resuming that effort —
    # reuse the exact branch even though the head already exists (no -b).
    orch, gh, _, _ = make_orch(tmp_path, issues=[ticket(1)])
    canonical = "feature/issue-1"
    os.makedirs(orch._worktree_path(canonical))
    orch._branch_exists = lambda name: name == canonical
    assert orch._branch_for(ticket(1)) == canonical


def test_branch_for_resumes_via_open_pr_without_worktree(tmp_path):
    # The observed bug: #N built on feature/issue-N in the MAIN checkout (no
    # worktree) with an open PR. A later pass must resume it, not dedupe to -b.
    orch, gh, _, _ = make_orch(tmp_path, issues=[ticket(1)])
    canonical = "feature/issue-1"
    gh.open_pr_by_head[canonical] = {"number": 7, "html_url": "https://gh/pr/7"}
    orch._branch_exists = lambda name: name == canonical  # head exists, no worktree
    assert orch._branch_for(ticket(1)) == canonical


def test_branch_for_resumes_via_pr_watch_record(tmp_path):
    orch, gh, _, _ = make_orch(tmp_path, issues=[ticket(1)])
    canonical = "feature/issue-1"
    orch._save_pr_watch({"5": {"issue": 1, "branch": canonical, "worktree": "x"}})
    orch._branch_exists = lambda name: name == canonical
    assert orch._branch_for(ticket(1)) == canonical


def test_branch_for_dedupes_when_collision_is_unrelated_work(tmp_path):
    # Canonical head taken, but no ownership marker ties it to this issue ->
    # a genuine collision with unrelated work -> dedupe to a free -b name.
    orch, gh, _, _ = make_orch(tmp_path, issues=[ticket(1)])
    canonical = "feature/issue-1"
    orch._branch_exists = lambda name: name == canonical
    assert orch._branch_for(ticket(1)) == "feature/issue-1-b"


def test_resumed_branch_and_worktree_path_agree(tmp_path):
    # AC: branch selection and worktree selection must agree for a resumed
    # ticket — the worktree the loop would use is the one that already exists.
    orch, gh, _, _ = make_orch(tmp_path, issues=[ticket(1)])
    canonical = "feature/issue-1"
    os.makedirs(orch._worktree_path(canonical))
    orch._branch_exists = lambda name: name == canonical
    branch = orch._branch_for(ticket(1))
    assert os.path.isdir(orch._worktree_path(branch))  # no orphan worktree


def test_pr_body_runs_through_the_template(tmp_path):
    orch, gh, implementer, reviewer = make_orch(
        tmp_path, issues=[ticket(1)], require_human=False
    )
    orch.process_ticket(ticket(1))
    body = gh.prs[0]["body"]
    assert "Closes #1" in body
    assert "- [ ] does X" in body  # acceptance criteria checklist
    assert "img.shields.io" in body  # cost estimate + actual chips
    assert "Generated by" in body and "idle]" in body  # PLG footer
    assert "Gates:" in body


# --------------------------------------------------------------------------- #
# PR review watching (ticket #7)
# --------------------------------------------------------------------------- #
def _review(id_, state, body="", user="alice"):
    return {"id": id_, "state": state, "body": body, "user": user, "submitted_at": ""}


def test_open_pr_marks_listen_and_records_watch_state(tmp_path):
    # Opening a PR labels it idle:listen and records how to resume its context.
    from agents.implementer import save_session

    save_session(str(tmp_path), "sess-pr")  # the implementer "saved" a session
    orch, gh, implementer, reviewer = make_orch(
        tmp_path, issues=[ticket(1)], require_human=True
    )
    rec = orch.process_ticket(ticket(1))
    assert rec.outcome == Outcome.PARKED  # require_human keeps the PR open
    pr_number = gh.prs[0]["number"]
    assert (pr_number, "idle:listen") in gh.added_labels

    state = orch._load_pr_watch()
    saved = state[str(pr_number)]
    assert saved["issue"] == 1
    assert saved["branch"] == "feature/issue-1"
    assert saved["worktree"] == str(tmp_path)
    assert saved["session_id"] == "sess-pr"  # auto-saved on PR creation
    assert saved["last_review_id"] == 0  # no reviews yet


def _seed_watch(orch, pr_number, *, issue=1, branch="idle/issue-1", worktree=None,
                last_review_id=0, attempts=0):
    state = orch._load_pr_watch()
    state[str(pr_number)] = {
        "issue": issue,
        "branch": branch,
        "worktree": worktree or orch.repo_dir,
        "session_id": "sess",
        "last_review_id": last_review_id,
        "attempts": attempts,
    }
    orch._save_pr_watch(state)


def test_watch_reviews_addresses_new_changes_requested(tmp_path):
    orch, gh, implementer, reviewer = make_orch(tmp_path, issues=[ticket(1)])
    gh.labelled_prs["idle:listen"] = [{"number": 5, "title": "x"}]
    gh.reviews[5] = [_review(100, "CHANGES_REQUESTED", body="add a test for Y")]
    _seed_watch(orch, 5, last_review_id=0)

    acted = orch.watch_reviews()

    assert acted == [5]
    # The implementer was resumed in the PR's worktree with the review feedback.
    assert implementer.calls == [(1, str(tmp_path), "idle/issue-1")]
    assert implementer.feedbacks[-1] and "add a test for Y" in implementer.feedbacks[-1]
    assert (str(tmp_path), "idle/issue-1") in implementer.pushed
    # Cursor advanced + attempt counted, so the same review won't re-trigger.
    state = orch._load_pr_watch()
    assert state["5"]["last_review_id"] == 100
    assert state["5"]["attempts"] == 1


def test_watch_reviews_ignores_approval(tmp_path):
    orch, gh, implementer, reviewer = make_orch(tmp_path, issues=[ticket(1)])
    gh.labelled_prs["idle:listen"] = [{"number": 5, "title": "x"}]
    gh.reviews[5] = [_review(100, "APPROVED", body="lgtm")]
    _seed_watch(orch, 5, last_review_id=0)

    acted = orch.watch_reviews()

    assert acted == []
    assert implementer.calls == []  # nothing to address
    # But the cursor still advances so we don't re-examine the approval.
    assert orch._load_pr_watch()["5"]["last_review_id"] == 100


def test_watch_reviews_no_new_reviews_is_a_noop(tmp_path):
    orch, gh, implementer, reviewer = make_orch(tmp_path, issues=[ticket(1)])
    gh.labelled_prs["idle:listen"] = [{"number": 5, "title": "x"}]
    gh.reviews[5] = [_review(100, "CHANGES_REQUESTED", body="fix")]
    _seed_watch(orch, 5, last_review_id=100)  # already handled review 100

    acted = orch.watch_reviews()

    assert acted == [] and implementer.calls == []


def test_watch_reviews_defers_after_review_iterations(tmp_path):
    orch, gh, implementer, reviewer = make_orch(tmp_path, issues=[ticket(1)])
    orch.config.budget.review_iterations = 2
    gh.labelled_prs["idle:listen"] = [{"number": 5, "title": "x"}]
    gh.reviews[5] = [_review(101, "CHANGES_REQUESTED", body="still wrong")]
    _seed_watch(orch, 5, last_review_id=100, attempts=2)  # budget exhausted

    orch.watch_reviews()

    assert implementer.calls == []  # no further revision
    assert (5, "idle:listen") in gh.removed_labels  # handed back to a human
    assert (5, "idle:needs-human") in gh.added_labels
    # Deferred PRs drop out of the watch state.
    assert "5" not in orch._load_pr_watch()


def test_watch_reviews_defers_when_guard_fails_on_revision(tmp_path):
    guards = [FakeGuard("scope", False, "diff too large")]
    orch, gh, implementer, reviewer = make_orch(
        tmp_path, issues=[ticket(1)], guards=guards
    )
    gh.labelled_prs["idle:listen"] = [{"number": 5, "title": "x"}]
    gh.reviews[5] = [_review(100, "CHANGES_REQUESTED", body="fix")]
    _seed_watch(orch, 5, last_review_id=0)

    acted = orch.watch_reviews()

    assert acted == [5]  # a revision was attempted
    assert implementer.calls  # implementer ran
    assert (5, "idle:listen") in gh.removed_labels  # but guard failure defers it
    assert (5, "idle:needs-human") in gh.added_labels


def test_watch_reviews_seeds_unknown_pr_from_branch(tmp_path):
    # A PR labelled idle:listen with no saved state is seeded from its head branch.
    orch, gh, implementer, reviewer = make_orch(tmp_path, issues=[ticket(1)])
    gh.labelled_prs["idle:listen"] = [{"number": 9, "title": "x"}]
    gh.pr_details[9] = {
        "number": 9,
        "head_branch": "idle/issue-1",
        "state": "open",
        "html_url": "",
    }
    gh.reviews[9] = [_review(100, "CHANGES_REQUESTED", body="seed me")]

    acted = orch.watch_reviews()

    assert acted == [9]
    assert implementer.calls == [(1, orch._worktree_path("idle/issue-1"), "idle/issue-1")]


def test_watch_reviews_skips_non_idle_branch(tmp_path):
    orch, gh, implementer, reviewer = make_orch(tmp_path, issues=[ticket(1)])
    gh.labelled_prs["idle:listen"] = [{"number": 9, "title": "x"}]
    gh.pr_details[9] = {
        "number": 9,
        "head_branch": "feature/not-ours",
        "state": "open",
        "html_url": "",
    }
    gh.reviews[9] = [_review(100, "CHANGES_REQUESTED", body="nope")]

    acted = orch.watch_reviews()

    assert acted == [] and implementer.calls == []


def test_main_watch_reviews_flag_calls_watch(tmp_path, monkeypatch):
    called = {}

    class Spy:
        def watch_reviews(self):
            called["watched"] = True

        def run(self, **k):  # must not be called
            raise AssertionError("run() must not be called for --watch-reviews")

    monkeypatch.setattr(idle_loop, "load_config", lambda p: Config(repo="o/n"))
    monkeypatch.setattr(
        idle_loop.Orchestrator, "from_config", classmethod(lambda cls, config, repo_dir=".": Spy())
    )
    rc = idle_loop.main(["--watch-reviews", "--repo-dir", str(tmp_path)])
    assert rc == idle_loop.EXIT_OK
    assert called.get("watched") is True


# --------------------------------------------------------------------------- #
# Unified pass: one run() works tickets AND services reviews (#25)
# --------------------------------------------------------------------------- #
def test_single_pass_services_reviews_and_works_tickets(tmp_path):
    # One run() must both work a ready ticket and address an actionable review on
    # an open idle:listen PR — no separate --watch-reviews invocation.
    orch, gh, implementer, reviewer = make_orch(
        tmp_path, issues=[ticket(1)], require_human=False
    )
    # An open idle:listen PR (for a different issue #2) with a changes-requested
    # review waiting to be serviced.
    gh.labelled_prs["idle:listen"] = [{"number": 5, "title": "x"}]
    gh.reviews[5] = [_review(100, "CHANGES_REQUESTED", body="address Y")]
    _seed_watch(orch, 5, issue=2, branch="idle/issue-2", worktree=str(tmp_path))

    records = orch.run()

    # Ticket #1 was worked this pass (a PR opened)...
    assert [r.ticket_id for r in records] == [1]
    assert len(gh.prs) == 1
    # ...and PR #5's review was serviced in the SAME pass (its branch revised).
    touched = {call[0] for call in implementer.calls}
    assert touched == {1, 2}
    assert (str(tmp_path), "idle/issue-2") in implementer.pushed


def test_run_drives_watch_reviews_each_pass(tmp_path):
    # Even with no listen PRs, run() invokes watch_reviews exactly once per pass.
    orch, gh, implementer, reviewer = make_orch(
        tmp_path, issues=[ticket(1)], require_human=False
    )
    calls = {"n": 0}
    real = orch.watch_reviews

    def spy():
        calls["n"] += 1
        return real()

    orch.watch_reviews = spy
    orch.run()
    assert calls["n"] == 1


def test_dry_run_does_not_service_reviews(tmp_path):
    # dry-run takes no action — reviews must not be serviced.
    orch, gh, implementer, reviewer = make_orch(tmp_path, issues=[ticket(1)])
    calls = {"n": 0}
    orch.watch_reviews = lambda: calls.__setitem__("n", calls["n"] + 1) or []
    orch.run(dry_run=True)
    assert calls["n"] == 0


# --------------------------------------------------------------------------- #
# Parked-ticket lifecycle: drop idle:ready, in-progress label, PR reuse (#27)
# --------------------------------------------------------------------------- #
def test_park_clears_ready_so_ticket_is_not_rediscovered(tmp_path):
    # Parking must drop idle:ready, else the ticket is re-worked every pass.
    guards = [FakeGuard("scope", False, "touches 99 files > max 15")]
    orch, gh, implementer, reviewer = make_orch(
        tmp_path, issues=[ticket(1)], guards=guards, require_human=False
    )
    rec = orch.process_ticket(ticket(1))
    assert rec.outcome == Outcome.PARKED
    assert (1, "idle:needs-human") in gh.added_labels
    assert (1, "idle:ready") in gh.removed_labels  # no longer "ready"


def test_skip_without_criteria_clears_ready(tmp_path):
    # The no-acceptance-criteria skip is a park site too — it must clear ready.
    orch, gh, implementer, reviewer = make_orch(tmp_path, issues=[ticket(1, criteria=[])])
    orch.process_ticket(ticket(1, criteria=[]))
    assert (1, "idle:ready") in gh.removed_labels


def test_over_threshold_skip_clears_ready(tmp_path):
    orch, gh, implementer, reviewer = make_orch(
        tmp_path, issues=[ticket(1)], threshold=1.0
    )
    orch.process_ticket(ticket(1))
    assert (1, "idle:ready") in gh.removed_labels


def test_discover_skips_needs_human_and_in_progress(tmp_path):
    ready = ticket(1)  # idle:ready only -> discovered
    parked = ticket(2)
    parked.labels = ["idle:ready", "idle:needs-human"]  # handed to a human
    in_prog = ticket(3)
    in_prog.labels = ["idle:ready", "idle:in-progress"]  # has an open idle-loop PR
    orch, gh, _, _ = make_orch(tmp_path, issues=[ready, parked, in_prog])

    discovered = orch.discover()

    assert [t.number for t in discovered] == [1]


def test_discover_redispatches_budget_override_parked_ticket(tmp_path):
    # A ticket parked on a limit (needs-human, idle:ready already dropped) that a
    # human then labels idle:allow-budget is re-dispatched to continue.
    plain = ticket(1)  # normal ready ticket
    parked = ticket(2)
    parked.labels = ["idle:needs-human", "idle:allow-budget"]  # parked + overridden
    orch, gh, _, _ = make_orch(tmp_path, issues=[plain, parked])

    discovered = orch.discover()

    assert {t.number for t in discovered} == {1, 2}  # the override waives the skip


def test_budget_override_does_not_bypass_global_cap(tmp_path):
    # The override raises per-ticket caps only — the loop-wide global cap is the
    # hard backstop and still stops dispatch.
    a, b = ticket(1), ticket(2)
    a.labels = ["idle:ready", "idle:allow-budget"]
    b.labels = ["idle:ready", "idle:allow-budget"]
    orch, gh, _, _ = make_orch(
        tmp_path, issues=[a, b], require_human=False, global_cap=5.0
    )
    records = orch.run()  # each ticket costs $10 (good_impl); cap is $5
    assert len(records) == 1  # stopped after the first despite the override


def test_open_pr_marks_issue_in_progress(tmp_path):
    # Opening a PR labels the *issue* idle:in-progress so a later pass skips it.
    orch, gh, implementer, reviewer = make_orch(
        tmp_path, issues=[ticket(1)], require_human=True
    )
    orch.process_ticket(ticket(1))
    assert (1, "idle:in-progress") in gh.added_labels


def test_open_pr_reuses_existing_pr_instead_of_creating_duplicate(tmp_path):
    # A branch that already has an open PR must reuse it — never create a second
    # one (which GitHub 422s) and never park for "could not open PR".
    orch, gh, implementer, reviewer = make_orch(
        tmp_path, issues=[ticket(1)], require_human=False
    )
    gh.open_pr_by_head["feature/issue-1"] = {
        "number": 99,
        "html_url": "https://gh/pr/99",
    }
    rec = orch.process_ticket(ticket(1))

    assert rec.outcome == Outcome.MERGED
    assert gh.prs == []  # no duplicate PR created
    assert gh.merged == [99]  # the existing PR was the one acted on
    assert implementer.pushed[0][1] == "feature/issue-1"  # branch still pushed


def test_reap_clears_in_progress_on_finished_pr(tmp_path):
    orch, gh, implementer, reviewer = make_orch(tmp_path, issues=[])
    root = tmp_path / ".idle-worktrees"
    (root / "issue-1").mkdir(parents=True)
    gh.pr_status_for_branch = lambda branch: "done"
    orch._remove_worktree = lambda p: None

    orch._reap_worktrees()

    # The finished PR's issue (#1, from branch idle/issue-1) gets in-progress cleared.
    assert (1, "idle:in-progress") in gh.removed_labels


# --------------------------------------------------------------------------- #
# Per-agent GitHub identities (#29)
# --------------------------------------------------------------------------- #
class RecordingClient:
    """A GitHub client stand-in tagged with a username, recording its comments."""

    def __init__(self, user: str):
        self.user = user
        self.comments: list[tuple[int, str]] = []

    def comment(self, number: int, body: str) -> None:
        self.comments.append((number, body))

    def upsert_comment(self, number: int, marker: str, body: str) -> None:
        self.comments.append((number, body))


def test_each_agent_comments_under_its_own_identity(tmp_path):
    # With per-agent identities configured, comments from different agents land
    # on different clients -> different usernames on the thread.
    from agents.identity import IMPLEMENTER, LOOP, PLANNER, REVIEWER, IdentityRouter

    orch, gh, implementer, reviewer = make_orch(
        tmp_path, issues=[ticket(1)], require_human=True
    )
    clients = {
        a: RecordingClient(a) for a in (PLANNER, REVIEWER, LOOP, IMPLEMENTER)
    }
    orch.identities = IdentityRouter(gh, clients)

    rec = orch.process_ticket(ticket(1))
    assert rec.outcome == Outcome.PARKED  # require_human keeps it open

    # The planner priced it (cost chip); the reviewer posted the park verdict.
    assert clients[PLANNER].comments, "planner posted the cost chip"
    assert clients[REVIEWER].comments, "reviewer posted the review verdict"
    # Distinct agents -> distinct usernames.
    users_who_posted = {c.user for c in clients.values() if c.comments}
    assert len(users_who_posted) >= 2
    assert {PLANNER, REVIEWER} <= users_who_posted


def test_default_identity_unchanged_when_not_configured(tmp_path):
    # No per-agent clients -> every agent shares the default (current behaviour):
    # the cost chip still lands on the one default client.
    orch, gh, implementer, reviewer = make_orch(
        tmp_path, issues=[ticket(1)], require_human=True
    )
    orch.process_ticket(ticket(1))
    assert (1, idle_loop.COST_CHIP_MARKER) in gh.sticky  # posted via the default
