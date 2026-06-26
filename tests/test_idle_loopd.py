"""Tests for idle_loopd — the resident daemon.

The orchestrator is a fake and time/sleep are stubbed (the daemon exposes
``_now``/``_sleep`` as overridable methods), so nothing sleeps for real, hits
the network, or invokes claude.
"""

from __future__ import annotations

import os
import types

import idle_loopd
from agents.harness import HarnessRateLimited


def _daemon(tmp_path, **kw):
    """A Daemon with a no-op fake orchestrator and a lock under tmp_path."""
    orch = types.SimpleNamespace(run=lambda: None)
    lock_path = str(tmp_path / ".idle-loop" / "idle-loopd.pid")
    return idle_loopd.Daemon(orch, lock_path=lock_path, **kw)


# --------------------------------------------------------------------------- #
# Poll loop
# --------------------------------------------------------------------------- #
def test_runs_repeated_passes_until_stopped(tmp_path):
    d = _daemon(tmp_path, interval=600)
    sleeps: list[float] = []
    d._sleep = sleeps.append  # don't really sleep
    calls = {"n": 0}

    def run():
        calls["n"] += 1
        if calls["n"] >= 3:
            d.request_stop()  # third pass asks the loop to wind down

    d.orchestrator.run = run

    rc = d.run()

    assert rc == idle_loopd.EXIT_OK
    assert calls["n"] == 3  # looped, didn't exit after one pass
    assert sleeps == [600, 600]  # slept between passes, not after the last
    assert not os.path.exists(d.lock.path)  # lock released on exit


def test_once_runs_single_pass_and_exits(tmp_path):
    d = _daemon(tmp_path, once=True)
    sleeps: list[float] = []
    d._sleep = sleeps.append
    calls = {"n": 0}
    d.orchestrator.run = lambda: calls.__setitem__("n", calls["n"] + 1)

    rc = d.run()

    assert rc == idle_loopd.EXIT_OK
    assert calls["n"] == 1  # exactly one pass
    assert sleeps == []  # no inter-pass wait
    assert not os.path.exists(d.lock.path)


def test_pass_exception_does_not_kill_the_daemon(tmp_path):
    d = _daemon(tmp_path, interval=600)
    d._sleep = lambda _s: None
    calls = {"n": 0}

    def run():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient boom")
        d.request_stop()

    d.orchestrator.run = run

    rc = d.run()

    assert rc == idle_loopd.EXIT_OK
    assert calls["n"] == 2  # survived the crash and ran again


# --------------------------------------------------------------------------- #
# Rate-limit recovery
# --------------------------------------------------------------------------- #
def test_rate_limit_sleeps_until_reset_then_resumes(tmp_path):
    d = _daemon(tmp_path, interval=600)
    d._now = lambda: 1000.0  # fixed wall clock
    sleeps: list[float] = []
    d._sleep = sleeps.append
    calls = {"n": 0}

    def run():
        calls["n"] += 1
        if calls["n"] == 1:
            raise HarnessRateLimited(reset_at=1300.0, reset_human="usage limit")
        d.request_stop()  # second pass (the resume) winds the loop down

    d.orchestrator.run = run

    rc = d.run()

    assert rc == idle_loopd.EXIT_OK
    assert calls["n"] == 2  # resumed after the limit — no exit, no cron
    # First sleep is the reset wait (1300 - 1000), then the normal inter-pass wait.
    assert sleeps == [300.0, 600]


def test_rate_limit_without_reset_falls_back_to_interval(tmp_path):
    d = _daemon(tmp_path, interval=42, once=True)
    sleeps: list[float] = []
    d._sleep = sleeps.append

    def run():
        raise HarnessRateLimited(reset_at=0, reset_human="no parseable reset")

    d.orchestrator.run = run

    d.run()

    assert sleeps == [42.0]  # unknown reset -> back off one interval


# --------------------------------------------------------------------------- #
# Signals
# --------------------------------------------------------------------------- #
def test_sigterm_stops_cleanly_and_releases_lock(tmp_path):
    d = _daemon(tmp_path, interval=600)
    d._sleep = lambda _s: None
    # Simulate the signal arriving during the pass.
    d.orchestrator.run = d.request_stop

    rc = d.run()

    assert rc == idle_loopd.EXIT_OK
    assert not os.path.exists(d.lock.path)


def test_install_signal_handlers_routes_to_request_stop(tmp_path, monkeypatch):
    d = _daemon(tmp_path)
    registered: dict = {}
    monkeypatch.setattr(
        idle_loopd.signal, "signal", lambda sig, handler: registered.__setitem__(sig, handler)
    )

    d.install_signal_handlers()

    assert registered[idle_loopd.signal.SIGINT] == d.request_stop
    assert registered[idle_loopd.signal.SIGTERM] == d.request_stop
    registered[idle_loopd.signal.SIGTERM]()  # invoking it flips the stop flag
    assert d._stop is True


# --------------------------------------------------------------------------- #
# Single-instance PID lock
# --------------------------------------------------------------------------- #
def test_lock_refused_when_held_by_live_pid(tmp_path, monkeypatch):
    path = str(tmp_path / ".idle-loop" / "idle-loopd.pid")
    os.makedirs(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("424242")
    monkeypatch.setattr(idle_loopd, "pid_alive", lambda _pid: True)

    assert idle_loopd.PidLock(path).acquire() is False  # live holder -> refuse


def test_lock_reclaimed_when_pid_is_stale(tmp_path, monkeypatch):
    path = str(tmp_path / ".idle-loop" / "idle-loopd.pid")
    os.makedirs(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("424242")  # a dead PID
    monkeypatch.setattr(idle_loopd, "pid_alive", lambda _pid: False)

    lock = idle_loopd.PidLock(path)
    assert lock.acquire() is True  # stale -> reclaimed
    with open(path, encoding="utf-8") as fh:
        assert fh.read().strip() == str(os.getpid())  # now ours
    lock.release()
    assert not os.path.exists(path)


def test_lock_acquired_when_free(tmp_path):
    path = str(tmp_path / ".idle-loop" / "idle-loopd.pid")
    lock = idle_loopd.PidLock(path)
    assert lock.acquire() is True
    assert os.path.exists(path)


def test_daemon_refuses_to_start_when_locked(tmp_path, monkeypatch):
    d = _daemon(tmp_path)
    os.makedirs(os.path.dirname(d.lock.path))
    with open(d.lock.path, "w", encoding="utf-8") as fh:
        fh.write("424242")
    monkeypatch.setattr(idle_loopd, "pid_alive", lambda _pid: True)
    calls = {"n": 0}
    d.orchestrator.run = lambda: calls.__setitem__("n", calls["n"] + 1)

    rc = d.run()

    assert rc == idle_loopd.EXIT_LOCKED
    assert calls["n"] == 0  # never ran a pass


# --------------------------------------------------------------------------- #
# CLI wiring
# --------------------------------------------------------------------------- #
def test_build_daemon_wires_args_and_factory(tmp_path):
    args = idle_loopd.build_parser().parse_args(
        ["--once", "--interval", "5", "--repo-dir", str(tmp_path)]
    )
    made: dict = {}

    def factory(config, repo_dir):
        made["repo_dir"] = repo_dir
        return types.SimpleNamespace(run=lambda: None)

    d = idle_loopd._build_daemon(args, orchestrator_factory=factory)

    assert d.once is True
    assert d.interval == 5
    assert made["repo_dir"] == str(tmp_path)
    assert d.lock.path == os.path.join(str(tmp_path), idle_loopd.LOCK_PATH)
