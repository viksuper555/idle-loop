"""Integration tests for the orchestrator (idle_loop.Orchestrator).

Everything external is faked — no network, no Anthropic, no git, no pytest
subprocess. Guards are simple pass/fail fakes so we exercise the orchestration
logic (triage, parking, merge gate, global cap, cost logging) in isolation.
"""

from __future__ import annotations

import json

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

    def list_ready_issues(self, label: str) -> list[Ticket]:
        return list(self.issues)

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

    def create_pull_request(self, title: str, head: str, base: str, body: str) -> dict:
        self._pr_seq += 1
        pr = {"number": self._pr_seq, "html_url": f"https://gh/pr/{self._pr_seq}"}
        self.prs.append({**pr, "title": title, "head": head, "base": base, "body": body})
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
        ["idle:ready", "idle:needs-human", "idle:allow-sensitive", "idle:listen"]
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


def test_guard_failure_parks_without_pr_or_merge(tmp_path):
    guards = [FakeGuard("scope", False, "touches 99 files > max 15")]
    orch, gh, implementer, reviewer = make_orch(
        tmp_path, issues=[ticket(1)], guards=guards, require_human=False
    )
    rec = orch.process_ticket(ticket(1))
    assert rec.outcome == Outcome.PARKED
    assert gh.prs == []  # guards run before PR; nothing opened
    assert gh.merged == []
    assert reviewer.calls == []  # never reached review
    assert (1, "idle:needs-human") in gh.added_labels
    assert any("scope" in body for _, body in gh.comments)


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
def _plan(ein=1_000_000, eout=200_000) -> PlanResult:
    return PlanResult(
        estimated_input_tokens=ein,
        estimated_output_tokens=eout,
        plan_text="p",
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
    # Pretend the canonical branch already exists -> dedupe to "-b".
    orch._branch_exists = lambda name: name == "feature/issue-1"
    orch.process_ticket(ticket(1))
    assert implementer.calls[0][2] == "feature/issue-1-b"


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
