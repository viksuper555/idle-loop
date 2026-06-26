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
        self._pr_seq = 0

    def list_ready_issues(self, label: str) -> list[Ticket]:
        return list(self.issues)

    def comment(self, number: int, body: str) -> None:
        self.comments.append((number, body))

    def add_label(self, number: int, label: str) -> None:
        self.added_labels.append((number, label))

    def remove_label(self, number: int, label: str) -> None:
        self.removed_labels.append((number, label))

    def ensure_labels(self, names: list[str]) -> None:
        self.ensured.append(list(names))

    def create_pull_request(self, title: str, head: str, base: str, body: str) -> dict:
        self._pr_seq += 1
        pr = {"number": self._pr_seq, "html_url": f"https://gh/pr/{self._pr_seq}"}
        self.prs.append({**pr, "title": title, "head": head, "base": base})
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

    def push_branch(self, repo_dir: str, branch: str) -> bool:
        self.pushed.append((repo_dir, branch))
        return self.push_ok

    def run(self, ticket: Ticket, repo_dir: str, branch: str) -> ImplementationResult:
        self.calls.append((ticket.number, repo_dir, branch))
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


def make_orch(tmp_path, *, issues, guards=None, verdict=None, impl=None, **cfg_over):
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
    assert recorded["names"] == ["idle:ready", "idle:needs-human", "idle:allow-sensitive"]


def test_branch_pushed_before_pr(tmp_path):
    orch, gh, implementer, reviewer = make_orch(
        tmp_path, issues=[ticket(1)], require_human=False
    )
    orch.process_ticket(ticket(1))
    # The branch was pushed to origin before the PR was opened.
    assert implementer.pushed and implementer.pushed[0][1] == "idle/issue-1"
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
