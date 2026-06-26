"""Artefact naming for idle-loop: branches, worktrees, and the back-parsing
needed to reconcile them.

One scheme, one place. Every branch idle-loop creates looks like::

    <prefix>/issue-<id>[-<dedupe-suffix>]

where ``prefix`` is one of :data:`PREFIXES` (chosen from the ticket's labels /
title, falling back to ``feature``) and the optional dedupe suffix (``-b``,
``-c``, ...) disambiguates a name already taken by an unrelated branch. The
worktree directory mirrors the branch with ``/`` flattened to ``-`` so it is a
single path component (``feature/issue-7`` -> ``feature-issue-7``).

stdlib only — kept dependency-free so the orchestrator, guards, and tests can
all import it without cycles.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from models import Ticket

# The allowed branch prefixes (AC: feature, bugfix, chore, docs).
PREFIXES: tuple[str, ...] = ("feature", "bugfix", "chore", "docs")
DEFAULT_PREFIX = "feature"

# Prefixes we still recognise when *parsing* an existing artefact back to a
# branch, beyond PREFIXES — "idle" is the legacy scheme (idle/issue-N) that
# predates this ticket; worktrees created under it must still reap cleanly.
_PARSE_PREFIXES: tuple[str, ...] = (*PREFIXES, "idle")

# Maps a substring found in a ticket label to a branch prefix. First hit wins,
# checked in declaration order so the more specific words come first.
_LABEL_HINTS: tuple[tuple[str, str], ...] = (
    ("bug", "bugfix"),
    ("fix", "bugfix"),
    ("doc", "docs"),
    ("chore", "chore"),
    ("maintenance", "chore"),
    ("depend", "chore"),
    ("feature", "feature"),
    ("enhancement", "feature"),
    ("feat", "feature"),
)

# Maps a Conventional-Commits type in a ticket *title* (``fix: ...``,
# ``docs(scope): ...``) to a branch prefix.
_TITLE_TYPE: dict[str, str] = {
    "fix": "bugfix",
    "bugfix": "bugfix",
    "docs": "docs",
    "doc": "docs",
    "feat": "feature",
    "feature": "feature",
    "chore": "chore",
    "build": "chore",
    "ci": "chore",
    "refactor": "chore",
    "perf": "chore",
    "test": "chore",
    "style": "chore",
}

_TITLE_TYPE_RE = re.compile(r"^\s*([a-zA-Z]+)(?:\([^)]*\))?!?\s*:")
_ISSUE_NUM_RE = re.compile(r"issue-(\d+)")


def prefix_for_ticket(ticket: Ticket) -> str:
    """Pick a branch prefix from the ticket's labels, then title, then default.

    Labels are the strongest signal (a maintainer tagged it on purpose); a
    Conventional-Commits ``type:`` in the title is the fallback; everything else
    is a ``feature``.
    """
    for label in ticket.labels:
        low = label.lower()
        for hint, prefix in _LABEL_HINTS:
            if hint in low:
                return prefix

    m = _TITLE_TYPE_RE.match(ticket.title or "")
    if m:
        prefix = _TITLE_TYPE.get(m.group(1).lower())
        if prefix:
            return prefix

    return DEFAULT_PREFIX


def canonical_branch(ticket: Ticket) -> str:
    """The undeduped branch name for ``ticket``: ``<prefix>/issue-<id>``."""
    return f"{prefix_for_ticket(ticket)}/issue-{ticket.number}"


def _dedupe_suffixes():
    """Yield branch dedupe suffixes: b, c, ... z, then aa, ab, ... (the base
    name itself is the implicit 'a', so we start at 'b')."""
    import string

    letters = string.ascii_lowercase
    yield from letters[1:]  # b..z
    for first in letters:  # aa..zz, should we ever need that many
        for second in letters:
            yield first + second


def dedupe_branch(base: str, exists: Callable[[str], bool]) -> str:
    """Return ``base`` if free, else ``base-b``, ``base-c``, ... until ``exists``
    reports the candidate is unused.

    ``exists`` is the collision oracle (e.g. "is this a local git head?").
    """
    if not exists(base):
        return base
    for suffix in _dedupe_suffixes():
        candidate = f"{base}-{suffix}"
        if not exists(candidate):
            return candidate
    raise RuntimeError(f"could not find a free branch name for {base!r}")  # pragma: no cover


def worktree_dir_name(branch: str) -> str:
    """Flatten a branch name to a single-component worktree directory name.

    Only the first ``/`` is flattened — branch names carry exactly one.
    """
    return branch.replace("/", "-", 1)


def branch_from_worktree_dir(name: str) -> str | None:
    """Reconstruct the branch a worktree directory was created for.

    Inverse of :func:`worktree_dir_name` for any recognised prefix; legacy bare
    ``issue-<n>`` directories map back to the old ``idle/issue-<n>`` branch.
    Returns ``None`` for a directory we don't own.
    """
    for prefix in _PARSE_PREFIXES:
        head = f"{prefix}-"
        if name.startswith(head):
            return f"{prefix}/{name[len(head):]}"
    if name.startswith("issue-"):  # legacy: idle/issue-N flattened to issue-N
        return f"idle/{name}"
    return None


def issue_number_from_branch(branch: str) -> int | None:
    """Extract the issue id from a branch name, or ``None`` if it carries none.

    Lenient by design: matches the ``issue-<n>`` segment under any prefix so it
    keeps working for legacy ``idle/`` branches and human-created ones.
    """
    m = _ISSUE_NUM_RE.search(branch or "")
    return int(m.group(1)) if m else None
