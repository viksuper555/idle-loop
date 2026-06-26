"""Transcript-leak guard — block Claude session artifacts from PR diffs.

Claude Code session transcripts (``~/.claude/projects/<encoded-cwd>/<id>.jsonl``)
are unscrubbed by construction: full prompts, complete tool output (bash stdout,
file reads, any env/secrets that scrolled by) and cwd/branch metadata. They must
never be committed to a branch or pushed to a PR.

This module is the matching core behind the ``transcript-guard`` CI job. It is a
fail-closed safety net: given the PR's changed-file set it reports any path that
is a transcript or session artifact, regardless of how the leak was introduced.

Run as a script in CI::

    git diff --name-only origin/"$BASE"...HEAD | python -m guards.transcript_leak

Exits non-zero (and names the offending path[s]) when a leak is found.
"""

from __future__ import annotations

import sys

# A path is a leak when any of these hold. Each entry is (reason, predicate).
# Kept deliberately broad — defense-in-depth beats precision here.


def _is_jsonl(path: str) -> bool:
    return path.endswith(".jsonl")


def _is_claude_session_store(path: str) -> bool:
    # Matches a `.claude/projects/`-shaped path anywhere in the tree, e.g.
    # `.claude/projects/<enc>/<id>.jsonl` or a nested vendored copy.
    norm = path.replace("\\", "/")
    return ".claude/projects/" in norm or norm.startswith(".claude/projects/")


def _is_idle_session_file(path: str) -> bool:
    norm = path.replace("\\", "/")
    return norm == ".idle-loop/session" or norm.endswith("/.idle-loop/session")


def _is_idle_pr_watch(path: str) -> bool:
    norm = path.replace("\\", "/")
    return norm == ".idle-loop/pr_watch.json" or norm.endswith(
        "/.idle-loop/pr_watch.json"
    )


def leak_reason(path: str) -> str | None:
    """Return a human reason if *path* is a transcript/session artifact, else None."""
    path = path.strip()
    if not path:
        return None
    if _is_claude_session_store(path):
        return "Claude session store (.claude/projects/) — contains full prompts and tool output"
    if _is_jsonl(path):
        return "JSONL session transcript — unscrubbed prompts, tool output and metadata"
    if _is_idle_session_file(path):
        return "idle-loop local session-state file (.idle-loop/session)"
    if _is_idle_pr_watch(path):
        return "idle-loop PR-watch state file (.idle-loop/pr_watch.json)"
    return None


def scan_paths(paths: list[str]) -> list[tuple[str, str]]:
    """Return ``[(path, reason), ...]`` for every offending path in *paths*."""
    offenders: list[tuple[str, str]] = []
    for path in paths:
        reason = leak_reason(path)
        if reason is not None:
            offenders.append((path.strip(), reason))
    return offenders


def main(argv: list[str] | None = None) -> int:
    """CLI entry: paths come from argv or, if none, stdin (one per line)."""
    args = list(sys.argv[1:] if argv is None else argv)
    if args:
        paths = args
    else:
        paths = [line for line in sys.stdin.read().splitlines()]

    offenders = scan_paths(paths)
    if not offenders:
        print("transcript-guard: no session transcripts or session files in the PR diff.")
        return 0

    print(
        "transcript-guard: BLOCKED — this PR adds/touches Claude session "
        "transcript(s) or session-state file(s):",
        file=sys.stderr,
    )
    for path, reason in offenders:
        print(f"  - {path}\n      {reason}", file=sys.stderr)
    print(
        "\nSession transcripts are unscrubbed: they contain full prompts, complete "
        "tool output (bash stdout, file reads, any secrets that scrolled by) and "
        "cwd/branch metadata. They must never be committed. Remove the path(s) above "
        "from this branch (and add them to .gitignore) before merging.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
