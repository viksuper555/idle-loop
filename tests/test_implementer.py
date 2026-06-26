"""Tests for the implementer agent — harness + git fully mocked.

The Claude Code harness is a fake (returns a HarnessResult or raises
HarnessRateLimited); ``subprocess.run`` (git) is monkeypatched. No `claude`,
no real git, no network.
"""

from __future__ import annotations

import subprocess

import pytest

from agents.harness import HarnessError, HarnessRateLimited, HarnessResult
from agents.implementer import Implementer
from config import Config
from models import Ticket


class FakeHarness:
    def __init__(self, result=None, raises=None):
        self.result = result or HarnessResult(text="done", cost_usd=0.42, num_turns=3)
        self.raises = raises
        self.calls: list[dict] = []

    def run(self, prompt, cwd, **kw):
        self.calls.append({"prompt": prompt, "cwd": cwd, **kw})
        if self.raises is not None:
            raise self.raises
        return self.result


def fake_git(diff="+added\n", names="src/feature.py\n", checkout_rc=0, push_rc=0):
    def run(cmd, *args, **kwargs):
        if "push" in cmd:
            return subprocess.CompletedProcess(cmd, push_rc, stdout="", stderr="")
        if "--name-only" in cmd:
            out = names
        elif "diff" in cmd:
            out = diff
        elif "checkout" in cmd:
            return subprocess.CompletedProcess(cmd, checkout_rc, stdout="", stderr="")
        else:
            out = ""
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

    return run


def recording_git(calls, **kw):
    """A fake git that records every argv into ``calls`` for assertions."""
    inner = fake_git(**kw)

    def run(cmd, *args, **kwargs):
        calls.append(cmd)
        return inner(cmd, *args, **kwargs)

    return run


def ticket(n=1):
    return Ticket(number=n, title="t", body="b", acceptance_criteria=["x"])


def test_run_reads_cost_and_diff_from_harness(monkeypatch, tmp_path):
    monkeypatch.setattr(subprocess, "run", fake_git())
    harness = FakeHarness(HarnessResult(text="done", cost_usd=0.42, num_turns=4))
    impl = Implementer(Config(repo="o/n"), harness=harness)

    res = impl.run(ticket(1), str(tmp_path), "idle/issue-1")

    assert res.branch == "idle/issue-1"
    assert res.cost_usd == pytest.approx(0.42)
    assert res.iterations == 4
    assert res.files_changed == ["src/feature.py"]
    assert "+added" in res.diff
    assert not res.error
    # The harness was invoked once in the repo dir.
    assert harness.calls and harness.calls[0]["cwd"] == str(tmp_path)
    assert "idle/issue-1" not in harness.calls[0]["prompt"]  # branch is git-managed, not prompted


def test_run_populates_token_counts(monkeypatch, tmp_path):
    monkeypatch.setattr(subprocess, "run", fake_git())
    harness = FakeHarness(
        HarnessResult(
            text="done", cost_usd=0.42, num_turns=2, input_tokens=1200, output_tokens=340
        )
    )
    res = Implementer(Config(repo="o/n"), harness=harness).run(
        ticket(1), str(tmp_path), "idle/issue-1"
    )
    assert res.input_tokens == 1200 and res.output_tokens == 340


def test_rate_limit_propagates(monkeypatch, tmp_path):
    monkeypatch.setattr(subprocess, "run", fake_git())
    harness = FakeHarness(raises=HarnessRateLimited(reset_at=123.0, reset_human="usage limit"))
    impl = Implementer(Config(repo="o/n"), harness=harness)
    with pytest.raises(HarnessRateLimited):
        impl.run(ticket(1), str(tmp_path), "idle/issue-1")


def test_harness_error_becomes_failed_result(monkeypatch, tmp_path):
    monkeypatch.setattr(subprocess, "run", fake_git())
    harness = FakeHarness(raises=HarnessError("claude not found"))
    impl = Implementer(Config(repo="o/n"), harness=harness)
    res = impl.run(ticket(1), str(tmp_path), "idle/issue-1")
    assert res.error and res.branch == "idle/issue-1"


def test_prompt_denies_sensitive_paths_by_default(monkeypatch, tmp_path):
    monkeypatch.setattr(subprocess, "run", fake_git())
    harness = FakeHarness()
    cfg = Config(repo="o/n")
    impl = Implementer(cfg, harness=harness)
    impl.run(ticket(1), str(tmp_path), "idle/issue-1")
    prompt = harness.calls[0]["prompt"]
    assert "never edit secrets, ci config" in prompt.lower() or "do not edit secrets" in prompt.lower()
    # Without the label, denylisted globs are NOT advertised as in-scope.
    assert ".github/**" not in prompt


def test_prompt_unlocks_denylist_when_allow_sensitive(monkeypatch, tmp_path):
    monkeypatch.setattr(subprocess, "run", fake_git())
    harness = FakeHarness()
    cfg = Config(repo="o/n")
    t = Ticket(
        number=3,
        title="Remove label job",
        body="b",
        labels=[cfg.labels.ready, cfg.labels.allow_sensitive],
        acceptance_criteria=["remove the job"],
    )
    Implementer(cfg, harness=harness).run(t, str(tmp_path), "idle/issue-3")
    prompt = harness.calls[0]["prompt"]
    assert "allow-sensitive" in prompt.lower()
    # The otherwise-denylisted paths are named as in-scope for this ticket.
    assert ".github/**" in prompt


def test_prompt_includes_reviewer_feedback_on_revision(monkeypatch, tmp_path):
    monkeypatch.setattr(subprocess, "run", fake_git())
    harness = FakeHarness()
    impl = Implementer(Config(repo="o/n"), harness=harness)
    impl.run(ticket(1), str(tmp_path), "idle/issue-1", feedback="- file.py:1: cover the Y case")
    prompt = harness.calls[0]["prompt"]
    assert "reviewer requested changes" in prompt.lower()
    assert "cover the Y case" in prompt


def test_session_is_persisted_then_resumed(monkeypatch, tmp_path):
    monkeypatch.setattr(subprocess, "run", fake_git())
    harness = FakeHarness(
        HarnessResult(text="ok", cost_usd=0.1, num_turns=2, session_id="sess-abc")
    )
    impl = Implementer(Config(repo="o/n"), harness=harness)

    # First run: no prior session -> no resume; the new session id is saved.
    impl.run(ticket(1), str(tmp_path), "idle/issue-1")
    assert harness.calls[0].get("resume_session_id") is None
    assert (tmp_path / ".idle-loop" / "session").read_text(encoding="utf-8") == "sess-abc"

    # Second run (e.g. a revision): the saved session is resumed for context.
    impl.run(ticket(1), str(tmp_path), "idle/issue-1", feedback="fix X")
    assert harness.calls[1].get("resume_session_id") == "sess-abc"


def test_run_with_no_session_succeeds_from_prompt_and_progress(monkeypatch, tmp_path):
    # Cold start: no saved session. The run must still succeed end-to-end purely
    # from prompt + PROGRESS.md + diff — --resume is demoted to an optimization.
    monkeypatch.setattr(subprocess, "run", fake_git())
    (tmp_path / "PROGRESS.md").write_text(
        "# Progress — #1: t\n\n## Done\nseeded by planner\n", encoding="utf-8"
    )
    harness = FakeHarness(HarnessResult(text="done", cost_usd=0.3, num_turns=2))
    impl = Implementer(Config(repo="o/n"), harness=harness)

    res = impl.run(ticket(1), str(tmp_path), "idle/issue-1")

    assert harness.calls[0].get("resume_session_id") is None  # no resume required
    assert not res.error and "+added" in res.diff
    # The committed working memory rode along in the prompt.
    assert "CURRENT PROGRESS.md" in harness.calls[0]["prompt"]
    assert "seeded by planner" in harness.calls[0]["prompt"]


def test_progress_written_and_committed_after_turn(monkeypatch, tmp_path):
    calls: list[list[str]] = []
    monkeypatch.setattr(subprocess, "run", recording_git(calls))
    harness = FakeHarness(HarnessResult(text="implemented X", cost_usd=0.2, num_turns=2))
    impl = Implementer(Config(repo="o/n"), harness=harness)

    impl.run(ticket(7), str(tmp_path), "idle/issue-7")

    # PROGRESS.md exists at the branch root, with the required prose sections.
    body = (tmp_path / "PROGRESS.md").read_text(encoding="utf-8")
    for section in ("## Done", "## Remaining", "## Current approach", "## Files touched"):
        assert section in body
    # And it was staged + committed on the branch.
    assert ["git", "-C", str(tmp_path), "add", "PROGRESS.md"] in calls
    commits = [c for c in calls if "commit" in c]
    assert commits and any("PROGRESS.md" in c for c in commits)


def test_progress_records_reviewer_asks_on_revision(monkeypatch, tmp_path):
    monkeypatch.setattr(subprocess, "run", fake_git())
    harness = FakeHarness()
    Implementer(Config(repo="o/n"), harness=harness).run(
        ticket(1), str(tmp_path), "idle/issue-1", feedback="cover the Y case"
    )
    body = (tmp_path / "PROGRESS.md").read_text(encoding="utf-8")
    assert "## Last reviewer asks" in body and "cover the Y case" in body


def test_progress_prompt_includes_committed_contents(monkeypatch, tmp_path):
    monkeypatch.setattr(subprocess, "run", fake_git())
    (tmp_path / "PROGRESS.md").write_text(
        "# Progress\n\n## Current approach\nuse a registry\n", encoding="utf-8"
    )
    harness = FakeHarness()
    Implementer(Config(repo="o/n"), harness=harness).run(
        ticket(1), str(tmp_path), "idle/issue-1"
    )
    assert "use a registry" in harness.calls[0]["prompt"]


def test_progress_rule_states_prose_only(monkeypatch, tmp_path):
    monkeypatch.setattr(subprocess, "run", fake_git())
    harness = FakeHarness()
    Implementer(Config(repo="o/n"), harness=harness).run(
        ticket(1), str(tmp_path), "idle/issue-1"
    )
    prompt = harness.calls[0]["prompt"].lower()
    assert "prose only" in prompt and "secret" in prompt


def test_progress_helpers_roundtrip(tmp_path):
    from agents.implementer import load_progress, write_progress

    assert load_progress(str(tmp_path)) is None
    write_progress(str(tmp_path), "# Progress\n\nstuff")
    assert "stuff" in load_progress(str(tmp_path))
    write_progress(str(tmp_path), "")  # empty is a no-op, keeps prior
    assert "stuff" in load_progress(str(tmp_path))


def test_safe_path_rejects_traversal(tmp_path):
    impl = Implementer(Config(repo="o/n"), harness=FakeHarness())
    assert str(impl._safe_path(str(tmp_path), "src/x.py")).startswith(str(tmp_path.resolve()))
    with pytest.raises(ValueError):
        impl._safe_path(str(tmp_path), "../escape.py")


def test_session_helpers_roundtrip(tmp_path):
    from agents.implementer import load_session, save_session

    assert load_session(str(tmp_path)) is None
    save_session(str(tmp_path), "sess-xyz")
    assert load_session(str(tmp_path)) == "sess-xyz"
    save_session(str(tmp_path), "")  # empty id is a no-op, keeps prior
    assert load_session(str(tmp_path)) == "sess-xyz"


def test_push_branch(monkeypatch, tmp_path):
    impl = Implementer(Config(repo="o/n"), harness=FakeHarness())
    monkeypatch.setattr(subprocess, "run", fake_git(push_rc=0))
    assert impl.push_branch(str(tmp_path), "idle/issue-1") is True
    monkeypatch.setattr(subprocess, "run", fake_git(push_rc=1))
    assert impl.push_branch(str(tmp_path), "idle/issue-1") is False
