"""Tests for guards.scope.ScopeGuard and its glob matcher."""

from __future__ import annotations

from config import Config, Guards, Labels
from guards.base import GuardContext
from guards.scope import ScopeGuard, match_any, match_path
from models import Ticket


def _config(**guard_kwargs: object) -> Config:
    cfg = Config(repo="owner/name")
    cfg.guards = Guards(
        max_diff_lines=guard_kwargs.get("max_diff_lines", 400),  # type: ignore[arg-type]
        max_files=guard_kwargs.get("max_files", 15),  # type: ignore[arg-type]
        path_allowlist=guard_kwargs.get(  # type: ignore[arg-type]
            "path_allowlist", ["src/**", "tests/**"]
        ),
        path_denylist=guard_kwargs.get(  # type: ignore[arg-type]
            "path_denylist", ["**/secrets/**", ".github/**"]
        ),
    )
    cfg.labels = Labels()
    return cfg


def _ticket(labels: list[str] | None = None) -> Ticket:
    return Ticket(number=1, title="t", body="", labels=labels or [])


def _diff(files: list[str], lines_each: int = 2) -> str:
    """Build a minimal unified diff touching the given files."""
    parts: list[str] = []
    for f in files:
        parts.append(f"diff --git a/{f} b/{f}")
        parts.append(f"--- a/{f}")
        parts.append(f"+++ b/{f}")
        parts.append("@@ -0,0 +1 @@")
        for i in range(lines_each):
            parts.append(f"+line {i}")
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# Glob matcher
# --------------------------------------------------------------------------- #
def test_match_path_double_star_crosses_slash():
    assert match_path("a/b/secrets/key.txt", "**/secrets/**")
    assert match_path("src/deep/nested/file.py", "src/**")
    assert match_path(".github/workflows/ci.yml", ".github/**")


def test_match_path_single_star_within_segment():
    assert match_path("src/foo.py", "src/*.py")
    assert not match_path("src/sub/foo.py", "src/*.py")  # * does not cross /
    assert match_path("foo.py", "*.py")
    assert not match_path("foo.txt", "*.py")


def test_match_any():
    assert match_any("tests/test_x.py", ["src/**", "tests/**"])
    assert not match_any("lib/x.py", ["src/**", "tests/**"])


# --------------------------------------------------------------------------- #
# ScopeGuard
# --------------------------------------------------------------------------- #
def test_in_scope_small_diff_passes():
    cfg = _config()
    files = ["src/app.py", "tests/test_app.py"]
    ctx = GuardContext(
        ticket=_ticket(), config=cfg, files_changed=files, diff=_diff(files)
    )
    res = ScopeGuard(cfg).check(ctx)
    assert res.passed, res.reason


def test_too_many_files_fails():
    cfg = _config(max_files=2)
    files = ["src/a.py", "src/b.py", "src/c.py"]
    ctx = GuardContext(
        ticket=_ticket(), config=cfg, files_changed=files, diff=_diff(files)
    )
    res = ScopeGuard(cfg).check(ctx)
    assert not res.passed
    assert "3 files" in res.reason and "max 2" in res.reason
    assert res.details["files"] == files


def test_too_many_diff_lines_fails():
    cfg = _config(max_diff_lines=3)
    files = ["src/a.py"]
    ctx = GuardContext(
        ticket=_ticket(), config=cfg, files_changed=files, diff=_diff(files, lines_each=10)
    )
    res = ScopeGuard(cfg).check(ctx)
    assert not res.passed
    assert "changed lines" in res.reason
    assert res.details["changed_lines"] == 10


def test_diff_headers_not_counted():
    # +++/--- header lines must not count toward the changed-line total.
    cfg = _config(max_diff_lines=2)
    files = ["src/a.py"]
    # 2 real changed lines + the 2 header lines; should pass at limit 2.
    ctx = GuardContext(
        ticket=_ticket(), config=cfg, files_changed=files, diff=_diff(files, lines_each=2)
    )
    res = ScopeGuard(cfg).check(ctx)
    assert res.passed, res.reason


def test_sensitive_github_path_fails_without_label():
    cfg = _config()
    files = [".github/workflows/ci.yml"]
    ctx = GuardContext(
        ticket=_ticket(), config=cfg, files_changed=files, diff=_diff(files)
    )
    res = ScopeGuard(cfg).check(ctx)
    assert not res.passed
    assert "sensitive path" in res.reason
    assert cfg.labels.allow_sensitive in res.reason


def test_sensitive_secrets_path_fails_without_label():
    cfg = _config()
    files = ["app/secrets/token.txt"]
    ctx = GuardContext(
        ticket=_ticket(), config=cfg, files_changed=files, diff=_diff(files)
    )
    res = ScopeGuard(cfg).check(ctx)
    assert not res.passed
    assert "sensitive path" in res.reason


def test_sensitive_path_passes_with_label():
    cfg = _config()
    files = [".github/workflows/ci.yml"]
    ticket = _ticket(labels=[cfg.labels.allow_sensitive])
    ctx = GuardContext(
        ticket=ticket, config=cfg, files_changed=files, diff=_diff(files)
    )
    res = ScopeGuard(cfg).check(ctx)
    assert res.passed, res.reason


def test_sensitive_path_with_label_bypasses_allowlist():
    # .github/ is not in the allowlist, but the label makes it acceptable.
    cfg = _config(path_allowlist=["src/**", "tests/**"])
    files = [".github/workflows/ci.yml"]
    ticket = _ticket(labels=[cfg.labels.allow_sensitive])
    ctx = GuardContext(
        ticket=ticket, config=cfg, files_changed=files, diff=_diff(files)
    )
    res = ScopeGuard(cfg).check(ctx)
    assert res.passed, res.reason


def test_outside_allowlist_fails():
    cfg = _config(path_allowlist=["src/**", "tests/**"])
    files = ["lib/x.py"]
    ctx = GuardContext(
        ticket=_ticket(), config=cfg, files_changed=files, diff=_diff(files)
    )
    res = ScopeGuard(cfg).check(ctx)
    assert not res.passed
    assert "outside the path allowlist" in res.reason
    assert res.details["files"] == ["lib/x.py"]


def test_allow_sensitive_waives_allowlist_for_unlisted_path():
    # The self-modification escape hatch: a path that is neither allowlisted nor
    # denylisted (e.g. the loop's own config) is rejected for a normal ticket but
    # accepted once a human applies allow-sensitive. Without this the loop could
    # never edit its own config/scripts even WITH the label.
    cfg = _config(path_allowlist=["src/**", "tests/**"])
    files = ["idle.config.yaml"]  # neither allowlisted nor denylisted
    res = ScopeGuard(cfg).check(
        GuardContext(ticket=_ticket(), config=cfg, files_changed=files, diff=_diff(files))
    )
    assert not res.passed
    assert "outside the path allowlist" in res.reason

    ticket = _ticket(labels=[cfg.labels.allow_sensitive])
    res = ScopeGuard(cfg).check(
        GuardContext(ticket=ticket, config=cfg, files_changed=files, diff=_diff(files))
    )
    assert res.passed, res.reason


def test_allow_sensitive_still_bounded_by_file_count():
    # The trust waiver covers PATH checks only — the size bounds still apply, so
    # a labelled ticket can't smuggle in an unbounded change.
    cfg = _config(max_files=2)
    files = ["src/a.py", "src/b.py", "src/c.py"]
    ticket = _ticket(labels=[cfg.labels.allow_sensitive])
    res = ScopeGuard(cfg).check(
        GuardContext(ticket=ticket, config=cfg, files_changed=files, diff=_diff(files))
    )
    assert not res.passed
    assert "3 files" in res.reason
