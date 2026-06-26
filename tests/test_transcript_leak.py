"""Tests for guards.transcript_leak — the CI transcript/session-leak matcher."""

from __future__ import annotations

import io

import pytest

from guards.transcript_leak import leak_reason, main, scan_paths


# --------------------------------------------------------------------------- #
# leak_reason — positive matches
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "path",
    [
        "session.jsonl",
        "logs/2026-06-26.jsonl",
        ".claude/projects/encoded-cwd/abc123.jsonl",
        ".claude/projects/foo/bar.txt",
        "vendor/.claude/projects/x/y.json",
        ".idle-loop/session",
        "sub/dir/.idle-loop/session",
        ".idle-loop/pr_watch.json",
        "sub/.idle-loop/pr_watch.json",
    ],
)
def test_offending_paths_flagged(path: str) -> None:
    assert leak_reason(path) is not None


# --------------------------------------------------------------------------- #
# leak_reason — clean paths pass
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "path",
    [
        "src/idle_loop.py",
        "README.md",
        "tests/test_transcript_leak.py",
        ".github/workflows/idle-loop.yml",
        "config.py",
        "data/results.json",  # plain json, not pr_watch / session store
        "notes/session.txt",  # 'session' in name but not the state file
    ],
)
def test_clean_paths_pass(path: str) -> None:
    assert leak_reason(path) is None


def test_blank_path_is_not_a_leak() -> None:
    assert leak_reason("") is None
    assert leak_reason("   ") is None


# --------------------------------------------------------------------------- #
# scan_paths
# --------------------------------------------------------------------------- #
def test_scan_paths_returns_only_offenders() -> None:
    paths = ["README.md", "a.jsonl", "src/x.py", ".idle-loop/session"]
    offenders = scan_paths(paths)
    flagged = {p for p, _ in offenders}
    assert flagged == {"a.jsonl", ".idle-loop/session"}
    # Every offender carries an actionable reason string.
    assert all(reason for _, reason in offenders)


# --------------------------------------------------------------------------- #
# main — exit codes + actionable output
# --------------------------------------------------------------------------- #
def test_main_passes_clean_diff(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["README.md", "src/idle_loop.py"]) == 0


def test_main_fails_on_jsonl_and_names_file(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["leak.jsonl"]) == 1
    err = capsys.readouterr().err
    assert "leak.jsonl" in err
    # Actionable: explains *why* transcripts must not be committed.
    assert "never be committed" in err
    assert "transcript" in err.lower()


def test_main_fails_on_session_store(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([".claude/projects/enc/abc.jsonl"]) == 1
    assert ".claude/projects/enc/abc.jsonl" in capsys.readouterr().err


def test_main_fails_on_idle_session_files(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main([".idle-loop/session"]) == 1
    assert main([".idle-loop/pr_watch.json"]) == 1


def test_main_reads_stdin_when_no_args(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("README.md\nleak.jsonl\n"))
    assert main([]) == 1
    assert "leak.jsonl" in capsys.readouterr().err
