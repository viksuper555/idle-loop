"""Scope guard: keep autonomous diffs small and inside the sandbox.

The :class:`ScopeGuard` enforces four bounds, in order, using the limits in
``config.guards`` (``max_files``, ``max_diff_lines``, ``path_allowlist``,
``path_denylist``):

1. Too many files touched.
2. Too many changed lines in the unified diff.
3. A changed file matches a *denylist* glob (sensitive path) and the ticket
   lacks the ``allow_sensitive`` label.
4. A changed file matches no *allowlist* glob.

It fails closed: any doubt -> :meth:`GuardResult.fail`, never an exception to
the caller for an expected condition.
"""

from __future__ import annotations

import re

from config import Config
from guards.base import GuardContext
from models import GuardResult


def _glob_to_regex(pattern: str) -> str:
    """Translate a glob into an anchored regex source.

    ``**`` matches across ``/`` (``.*``); ``*`` matches within a single path
    segment (``[^/]*``); every other character is escaped literally.
    """
    out: list[str] = []
    i = 0
    n = len(pattern)
    while i < n:
        c = pattern[i]
        if c == "*":
            if i + 1 < n and pattern[i + 1] == "*":
                out.append(".*")
                i += 2
            else:
                out.append("[^/]*")
                i += 1
        else:
            out.append(re.escape(c))
            i += 1
    return "".join(out)


def match_path(path: str, pattern: str) -> bool:
    """Return ``True`` if ``path`` matches the glob ``pattern`` in full."""
    return re.fullmatch(_glob_to_regex(pattern), path) is not None


def match_any(path: str, patterns: list[str]) -> bool:
    """Return ``True`` if ``path`` matches at least one glob in ``patterns``."""
    return any(match_path(path, p) for p in patterns)


def _count_diff_lines(diff: str) -> int:
    """Count added/removed lines in a unified diff, excluding ``+++``/``---`` headers."""
    count = 0
    for line in diff.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+") or line.startswith("-"):
            count += 1
    return count


class ScopeGuard:
    """Bounds the size and reach of an autonomous change set."""

    name = "scope"

    def __init__(self, config: Config) -> None:
        self.config = config

    def check(self, ctx: GuardContext) -> GuardResult:
        guards = ctx.config.guards
        files = list(ctx.files_changed)

        # 1) File-count bound.
        if len(files) > guards.max_files:
            return GuardResult.fail(
                self.name,
                f"touches {len(files)} files > max {guards.max_files}",
                files=files,
            )

        # 2) Changed-line bound.
        changed_lines = _count_diff_lines(ctx.diff)
        if changed_lines > guards.max_diff_lines:
            return GuardResult.fail(
                self.name,
                f"diff has {changed_lines} changed lines > max {guards.max_diff_lines}",
                changed_lines=changed_lines,
            )

        allow_sensitive = ctx.ticket.has_label(ctx.config.labels.allow_sensitive)

        # 3) & 4) Per-file path checks.
        for path in files:
            sensitive = match_any(path, guards.path_denylist)
            if sensitive:
                if not allow_sensitive:
                    return GuardResult.fail(
                        self.name,
                        f"edits sensitive path {path} "
                        f"(needs {ctx.config.labels.allow_sensitive} label)",
                        files=[path],
                    )
                # Sensitive path explicitly allowed by label: bypasses the
                # allowlist check too.
                continue

            if not match_any(path, guards.path_allowlist):
                return GuardResult.fail(
                    self.name,
                    f"{path} is outside the path allowlist",
                    files=[path],
                )

        return GuardResult.ok(self.name)
