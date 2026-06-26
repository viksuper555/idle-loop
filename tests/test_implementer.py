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


def fake_git(diff="+added\n", names="src/feature.py\n", checkout_rc=0):
    def run(cmd, *args, **kwargs):
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


def test_safe_path_rejects_traversal(tmp_path):
    impl = Implementer(Config(repo="o/n"), harness=FakeHarness())
    assert str(impl._safe_path(str(tmp_path), "src/x.py")).startswith(str(tmp_path.resolve()))
    with pytest.raises(ValueError):
        impl._safe_path(str(tmp_path), "../escape.py")
