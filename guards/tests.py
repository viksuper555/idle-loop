"""TestsGuard — proves the change is covered and the suite is green.

Two fail-closed checks run in order:

1. If ``config.guards.require_tests_for_new_behavior`` is set, any source-code
   change that ships without an accompanying test file fails the guard
   ("new behavior without tests").
2. The test suite is run via ``python -m pytest -q`` in the repo. A nonzero
   return code fails the guard with the tail of the captured output attached.

Like every guard, doubt resolves to failure: a missing repo, a pytest crash,
or a timeout all produce a failing :class:`GuardResult` rather than an
exception escaping to the caller.
"""

from __future__ import annotations

import os
import subprocess

from config import Config
from guards.base import GuardContext
from models import GuardResult

# Extensions that are pure config/docs — changing them is never "new behavior".
_NON_SOURCE_SUFFIXES = (".md", ".yaml", ".yml", ".toml", ".cfg", ".ini", ".txt")

# How long pytest may run before the guard gives up and fails closed.
_PYTEST_TIMEOUT_S = 1800

# How many trailing characters of pytest output to surface in details.
_OUTPUT_TAIL = 4000


def is_test_file(path: str) -> bool:
    """Return ``True`` if *path* looks like a test file.

    A test file lives under a ``tests/`` directory anywhere in the path, or its
    basename matches ``test_*.py`` / ``*_test.py``.
    """
    norm = path.replace("\\", "/").strip("/")
    parts = norm.split("/")
    if "tests" in parts[:-1] or (parts and parts[0] == "tests"):
        return True
    base = os.path.basename(norm)
    return base.endswith("_test.py") or base.startswith("test_") and base.endswith(".py")


def is_source_file(path: str) -> bool:
    """Return ``True`` if *path* is source code that can introduce new behavior.

    Source code means a ``.py`` file that is neither a test file nor a pure
    config/docs artifact (``*.md`` / ``*.yaml`` / ``*.toml`` / ``*.cfg`` /
    ``*.ini`` / ``*.txt``).
    """
    norm = path.replace("\\", "/")
    lower = norm.lower()
    if lower.endswith(_NON_SOURCE_SUFFIXES):
        return False
    if not lower.endswith(".py"):
        return False
    return not is_test_file(norm)


class TestsGuard:
    """Fail-closed gate that requires tests for new behavior and a green suite."""

    __test__ = False  # not a pytest test class despite the "Test" prefix

    def __init__(self, config: Config) -> None:
        self.config = config
        self.name = "tests"

    def check(self, ctx: GuardContext) -> GuardResult:
        """Enforce test coverage for new behavior, then run the suite."""
        files = ctx.files_changed or []

        # 1) New behavior must arrive with tests.
        if self.config.guards.require_tests_for_new_behavior:
            source_changed = [p for p in files if is_source_file(p)]
            tests_touched = [p for p in files if is_test_file(p)]
            if source_changed and not tests_touched:
                return GuardResult.fail(
                    self.name,
                    "new behavior without tests",
                    source_changed=source_changed,
                )

        # 2) The suite must pass.
        try:
            proc = subprocess.run(
                ["python", "-m", "pytest", "-q"],
                cwd=ctx.repo_dir,
                capture_output=True,
                text=True,
                timeout=_PYTEST_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            return GuardResult.fail(
                self.name,
                "test suite timed out",
                timeout_s=_PYTEST_TIMEOUT_S,
            )
        except Exception as exc:  # noqa: BLE001 - fail closed on anything
            return GuardResult.fail(
                self.name,
                f"could not run test suite: {type(exc).__name__}: {exc}",
            )

        if proc.returncode != 0:
            stdout = proc.stdout or ""
            stderr = proc.stderr or ""
            return GuardResult.fail(
                self.name,
                "test suite failed",
                returncode=proc.returncode,
                stdout_tail=stdout[-_OUTPUT_TAIL:],
                stderr_tail=stderr[-_OUTPUT_TAIL:],
            )

        return GuardResult.ok(self.name, "test suite passed")
