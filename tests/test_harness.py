"""Tests for the Claude Code harness runner — subprocess fully mocked."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime

import pytest

from agents.harness import (
    ClaudeHarness,
    HarnessError,
    HarnessRateLimited,
    is_rate_limited,
    parse_reset_time,
)
from config import Config


# --------------------------------------------------------------------------- #
# is_rate_limited
# --------------------------------------------------------------------------- #
def test_strong_phrase_is_rate_limited_even_on_success():
    assert is_rate_limited(0, False, "Claude usage limit reached") is True
    assert is_rate_limited(0, False, "your limit will reset at 3pm") is True


def test_weak_phrase_only_counts_on_error():
    # A ticket that merely mentions rate limiting on a SUCCESSFUL run is not a limit.
    assert is_rate_limited(0, False, "this PR adds rate limit handling") is False
    # But a weak phrase with an actual error is.
    assert is_rate_limited(1, True, "HTTP 429: too many requests") is True


def test_clean_output_is_not_rate_limited():
    assert is_rate_limited(0, False, "implemented and tests pass") is False


# --------------------------------------------------------------------------- #
# parse_reset_time
# --------------------------------------------------------------------------- #
def test_parse_ampm_time_returns_future_epoch():
    now = datetime(2026, 6, 26, 12, 0, 0).timestamp()
    epoch = parse_reset_time("limit will reset at 11:30pm", now=now)
    dt = datetime.fromtimestamp(epoch)
    assert dt.hour == 23 and dt.minute == 30
    assert epoch > now


def test_parse_rolls_to_tomorrow_when_time_already_passed():
    now = datetime(2026, 6, 26, 23, 0, 0).timestamp()
    epoch = parse_reset_time("resets at 2:10am", now=now)
    assert epoch > now
    assert epoch - now < 24 * 3600 + 60


def test_parse_falls_back_to_window_when_unparseable():
    now = 1_000_000.0
    epoch = parse_reset_time("usage limit reached, sorry", window_hours=5.0, now=now)
    assert epoch == pytest.approx(now + 5 * 3600)


def test_parse_handles_named_timezone():
    now = datetime(2026, 6, 26, 12, 0, 0).timestamp()
    epoch = parse_reset_time("reset at 2:10am (Europe/Sofia)", now=now)
    assert epoch > now and epoch - now < 24 * 3600 + 60


# --------------------------------------------------------------------------- #
# ClaudeHarness.run
# --------------------------------------------------------------------------- #
def _fake_run(returncode=0, stdout="", stderr="", record=None):
    def run(cmd, *args, **kwargs):
        if record is not None:
            record.append(cmd)
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)

    return run


def test_run_parses_success(monkeypatch, tmp_path):
    payload = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "all done",
        "total_cost_usd": 0.42,
        "num_turns": 3,
        "session_id": "s1",
    }
    cmds: list = []
    monkeypatch.setattr(subprocess, "run", _fake_run(0, json.dumps(payload), record=cmds))
    h = ClaudeHarness(Config(repo="o/n"))
    res = h.run("do it", cwd=str(tmp_path), disallowed_tools=["Bash"])

    assert res.cost_usd == pytest.approx(0.42)
    assert res.num_turns == 3 and res.text == "all done" and not res.is_error
    cmd = cmds[0]
    assert cmd[0:2] == ["claude", "-p"] and "do it" in cmd
    assert "--output-format" in cmd and "--dangerously-skip-permissions" in cmd
    assert "--model" in cmd and "claude-opus-4-8" in cmd
    assert "--disallowed-tools" in cmd and "Bash" in cmd


def test_run_raises_rate_limited(monkeypatch, tmp_path):
    payload = {
        "type": "result",
        "is_error": True,
        "result": "Claude usage limit reached. Your limit will reset at 2:10am (Europe/Sofia).",
        "total_cost_usd": 0.0,
        "num_turns": 1,
    }
    monkeypatch.setattr(subprocess, "run", _fake_run(0, json.dumps(payload)))
    h = ClaudeHarness(Config(repo="o/n"))
    with pytest.raises(HarnessRateLimited) as ei:
        h.run("do it", cwd=str(tmp_path))
    assert ei.value.reset_at > 0
    assert "usage limit" in ei.value.reset_human.lower()


def test_run_raises_harness_error_on_nonzero_without_json(monkeypatch, tmp_path):
    monkeypatch.setattr(subprocess, "run", _fake_run(1, "", "Error: something broke"))
    h = ClaudeHarness(Config(repo="o/n"))
    with pytest.raises(HarnessError):
        h.run("do it", cwd=str(tmp_path))


def test_run_raises_harness_error_when_binary_missing(monkeypatch, tmp_path):
    def boom(*a, **k):
        raise FileNotFoundError("no claude")

    monkeypatch.setattr(subprocess, "run", boom)
    h = ClaudeHarness(Config(repo="o/n"))
    with pytest.raises(HarnessError):
        h.run("do it", cwd=str(tmp_path))


def test_parse_json_tolerates_trailing_lines(monkeypatch, tmp_path):
    payload = {"type": "result", "is_error": False, "result": "ok", "total_cost_usd": 0.1, "num_turns": 1}
    out = "some stray log line\n" + json.dumps(payload)
    monkeypatch.setattr(subprocess, "run", _fake_run(0, out))
    h = ClaudeHarness(Config(repo="o/n"))
    res = h.run("x", cwd=str(tmp_path))
    assert res.text == "ok"
