"""Tests for guards.tests.TestsGuard.

Every external boundary is mocked: subprocess.run is monkeypatched so pytest is
never actually invoked.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from config import Config
from guards.base import GuardContext
from guards.tests import TestsGuard, is_source_file, is_test_file
from models import Ticket


def _ctx(files: list[str], repo_dir: str = "/repo") -> GuardContext:
    return GuardContext(
        ticket=Ticket(number=1, title="t", body=""),
        config=Config(repo="o/n"),
        repo_dir=repo_dir,
        files_changed=files,
    )


def _fake_run(returncode: int, stdout: str = "", stderr: str = ""):
    calls: list[dict] = []

    def run(cmd, **kwargs):  # noqa: ANN001
        calls.append({"cmd": cmd, "kwargs": kwargs})
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)

    return run, calls


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "path",
    [
        "tests/test_foo.py",
        "tests/sub/whatever.py",
        "pkg/tests/helpers.py",
        "test_module.py",
        "foo_test.py",
        "a/b/test_x.py",
    ],
)
def test_is_test_file_true(path: str) -> None:
    assert is_test_file(path) is True


@pytest.mark.parametrize(
    "path",
    ["src/app.py", "models.py", "README.md", "pkg/core.py"],
)
def test_is_test_file_false(path: str) -> None:
    assert is_test_file(path) is False


@pytest.mark.parametrize(
    "path",
    ["src/app.py", "models.py", "pkg/sub/core.py"],
)
def test_is_source_file_true(path: str) -> None:
    assert is_source_file(path) is True


@pytest.mark.parametrize(
    "path",
    [
        "README.md",
        "idle.config.yaml",
        "conf.yml",
        "pyproject.toml",
        "setup.cfg",
        "tox.ini",
        "notes.txt",
        "tests/test_foo.py",
        "foo_test.py",
        "data.json",  # not .py -> not source
    ],
)
def test_is_source_file_false(path: str) -> None:
    assert is_source_file(path) is False


# --------------------------------------------------------------------------- #
# Missing-tests detection
# --------------------------------------------------------------------------- #
def test_source_without_tests_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    run, calls = _fake_run(0)
    monkeypatch.setattr(subprocess, "run", run)
    guard = TestsGuard(Config(repo="o/n"))
    res = guard.check(_ctx(["src/app.py"]))
    assert res.passed is False
    assert res.reason == "new behavior without tests"
    assert res.details["source_changed"] == ["src/app.py"]
    # We must bail before running the suite.
    assert calls == []


def test_source_with_test_proceeds_to_suite(monkeypatch: pytest.MonkeyPatch) -> None:
    run, calls = _fake_run(0)
    monkeypatch.setattr(subprocess, "run", run)
    guard = TestsGuard(Config(repo="o/n"))
    res = guard.check(_ctx(["src/app.py", "tests/test_app.py"]))
    assert res.passed is True
    assert len(calls) == 1
    assert calls[0]["cmd"] == ["python", "-m", "pytest", "-q"]
    assert calls[0]["kwargs"]["cwd"] == "/repo"


def test_only_docs_changed_no_missing_tests_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    run, calls = _fake_run(0)
    monkeypatch.setattr(subprocess, "run", run)
    guard = TestsGuard(Config(repo="o/n"))
    res = guard.check(_ctx(["README.md", "idle.config.yaml"]))
    # No source -> no missing-tests fail; suite runs and passes.
    assert res.passed is True
    assert len(calls) == 1


def test_requirement_disabled_skips_missing_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    run, calls = _fake_run(0)
    monkeypatch.setattr(subprocess, "run", run)
    cfg = Config(repo="o/n")
    cfg.guards.require_tests_for_new_behavior = False
    guard = TestsGuard(cfg)
    res = guard.check(_ctx(["src/app.py"]))
    assert res.passed is True
    assert len(calls) == 1


# --------------------------------------------------------------------------- #
# Suite pass / fail mapping
# --------------------------------------------------------------------------- #
def test_suite_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    run, _ = _fake_run(0, stdout="3 passed")
    monkeypatch.setattr(subprocess, "run", run)
    guard = TestsGuard(Config(repo="o/n"))
    res = guard.check(_ctx(["tests/test_app.py"]))
    assert res.passed is True
    assert res.reason == "test suite passed"


def test_suite_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    run, _ = _fake_run(1, stdout="boom-out", stderr="boom-err")
    monkeypatch.setattr(subprocess, "run", run)
    guard = TestsGuard(Config(repo="o/n"))
    res = guard.check(_ctx(["tests/test_app.py"]))
    assert res.passed is False
    assert res.reason == "test suite failed"
    assert res.details["returncode"] == 1
    assert "boom-out" in res.details["stdout_tail"]
    assert "boom-err" in res.details["stderr_tail"]


def test_timeout_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    def run(cmd, **kwargs):  # noqa: ANN001
        raise subprocess.TimeoutExpired(cmd, 1)

    monkeypatch.setattr(subprocess, "run", run)
    guard = TestsGuard(Config(repo="o/n"))
    res = guard.check(_ctx(["tests/test_app.py"]))
    assert res.passed is False
    assert res.reason == "test suite timed out"


def test_subprocess_error_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    def run(cmd, **kwargs):  # noqa: ANN001
        raise OSError("no python")

    monkeypatch.setattr(subprocess, "run", run)
    guard = TestsGuard(Config(repo="o/n"))
    res = guard.check(_ctx(["tests/test_app.py"]))
    assert res.passed is False
    assert "could not run test suite" in res.reason
