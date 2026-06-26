"""Tests for the implementer agent — every external boundary mocked.

The Anthropic client is a scripted fake; ``subprocess.run`` (git + bash) is
monkeypatched. No network, no real git, no real shell.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from agents.implementer import Implementer
from config import Config


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
def tool_use_response(command="echo hi", in_tok=1000, out_tok=1000):
    block = SimpleNamespace(type="tool_use", name="bash", id="t1", input={"command": command})
    return SimpleNamespace(
        stop_reason="tool_use",
        content=[block],
        usage=SimpleNamespace(input_tokens=in_tok, output_tokens=out_tok),
    )


def end_turn_response(in_tok=500, out_tok=500):
    block = SimpleNamespace(type="text", text="done")
    return SimpleNamespace(
        stop_reason="end_turn",
        content=[block],
        usage=SimpleNamespace(input_tokens=in_tok, output_tokens=out_tok),
    )


class ScriptedClient:
    def __init__(self, responses, loop=False):
        self._responses = list(responses)
        self._loop = loop
        self._i = 0
        self.calls = 0
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kw):
        self.calls += 1
        if self._i >= len(self._responses):
            if self._loop:
                self._i = 0
            else:
                raise AssertionError("scripted client exhausted")
        resp = self._responses[self._i]
        self._i += 1
        return resp


def make_fake_run(bash_returncode=0, bash_out="ok\n", diff="+added\n", names="src/feature.py\n"):
    def fake_run(cmd, *args, **kwargs):
        if cmd and cmd[0] == "git":
            if "--name-only" in cmd:
                out = names
            elif "diff" in cmd:
                out = diff
            else:  # checkout etc.
                out = ""
            return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")
        if cmd and cmd[0] == "bash":
            return subprocess.CompletedProcess(cmd, bash_returncode, stdout=bash_out, stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    return fake_run


def cfg(**over) -> Config:
    c = Config(repo="o/n")
    if "no_progress_limit" in over:
        c.budget.no_progress_limit = over["no_progress_limit"]
    if "max_iterations" in over:
        c.budget.max_iterations = over["max_iterations"]
    return c


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
def test_run_completes_and_accounts_cost(monkeypatch, tmp_path):
    monkeypatch.setattr(subprocess, "run", make_fake_run())
    client = ScriptedClient([tool_use_response(), end_turn_response()])
    impl = Implementer(cfg(), client=client)
    from models import Ticket

    ticket = Ticket(number=1, title="t", body="b", acceptance_criteria=["x"])
    result = impl.run(ticket, str(tmp_path), "idle/issue-1")

    assert result.iterations == 2
    assert result.branch == "idle/issue-1"
    assert result.files_changed == ["src/feature.py"]
    assert "+added" in result.diff
    assert not result.no_progress and not result.error
    # input 1500, output 1500 -> 1500/1e6*5 + 1500/1e6*25 = 0.0075 + 0.0375
    assert result.cost_usd == pytest.approx(0.045, rel=1e-3)
    assert result.input_tokens == 1500 and result.output_tokens == 1500


def test_safe_path_rejects_traversal(tmp_path):
    impl = Implementer(cfg(), client=object())
    inside = impl._safe_path(str(tmp_path), "src/x.py")
    assert str(inside).startswith(str(tmp_path.resolve()))
    with pytest.raises(ValueError):
        impl._safe_path(str(tmp_path), "../escape.py")


def test_no_progress_detector_bails(monkeypatch, tmp_path):
    # Bash always fails (same error) and the diff never changes -> no progress.
    monkeypatch.setattr(
        subprocess, "run", make_fake_run(bash_returncode=1, bash_out="boom\n", diff="+same\n")
    )
    client = ScriptedClient([tool_use_response(command="false")], loop=True)
    impl = Implementer(cfg(no_progress_limit=3, max_iterations=20), client=client)
    from models import Ticket

    ticket = Ticket(number=2, title="t", body="b", acceptance_criteria=["x"])
    result = impl.run(ticket, str(tmp_path), "idle/issue-2")

    assert result.no_progress is True
    assert result.iterations == 3  # bailed at the no-progress limit


def test_run_never_raises_on_client_error(tmp_path):
    class BoomClient:
        def __init__(self):
            self.messages = SimpleNamespace(create=self._boom)

        def _boom(self, **kw):
            raise RuntimeError("api down")

    impl = Implementer(cfg(), client=BoomClient())
    from models import Ticket

    ticket = Ticket(number=3, title="t", body="b", acceptance_criteria=["x"])
    result = impl.run(ticket, str(tmp_path), "idle/issue-3")
    assert result.error  # captured, not raised
    assert result.branch == "idle/issue-3"


def test_editor_tool_confined_to_repo(monkeypatch, tmp_path):
    monkeypatch.setattr(subprocess, "run", make_fake_run())
    impl = Implementer(cfg(), client=object())
    # create then view a file inside the repo
    out, err = impl._run_editor(str(tmp_path), "create", {"path": "src/new.py", "file_text": "x=1\n"})
    assert not err and "created" in out
    out, err = impl._run_editor(str(tmp_path), "view", {"path": "src/new.py"})
    assert not err and "x=1" in out
    # traversal is rejected
    out, err = impl._run_editor(str(tmp_path), "create", {"path": "../evil.py", "file_text": "bad"})
    assert err and "escapes" in out
