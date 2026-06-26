"""The implementer agent — backed by the Claude Code harness.

Instead of driving a hand-rolled tool loop over the API, the implementer hands
the whole ticket to ``claude -p`` running headlessly *inside the target repo on
an isolated branch*. Claude Code uses its own tools (edit, bash, tests) to make
the change; we manage the branch and read the resulting diff/cost back out.

No API key — auth is the Claude Code login. A usage/session limit surfaces as
:class:`HarnessRateLimited`, which propagates so the orchestrator can stop and
the listener can reschedule.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from agents.harness import ClaudeHarness, HarnessRateLimited
from config import Config
from models import ImplementationResult, Ticket

# Per-worktree file holding the claude session id, so a later invocation
# (revision round, or a subsequent loop run resuming the ticket in its
# persistent worktree) continues the same conversation. Under .idle-loop/,
# which is gitignored.
_SESSION_FILE = ".idle-loop/session"

# Committed, portable working memory. Unlike the session file this lives at the
# branch root and travels in git, so an agent on ANY machine can continue the
# ticket from committed artifacts alone — ``--resume`` is only a same-machine
# speed cache, never required for correctness. Prose only: never a secret store.
_PROGRESS_FILE = "PROGRESS.md"


def session_path(repo_dir: str) -> Path:
    """Path to the per-worktree claude session file."""
    return Path(repo_dir) / _SESSION_FILE


def progress_path(repo_dir: str) -> Path:
    """Path to the committed PROGRESS.md at the branch root."""
    return Path(repo_dir) / _PROGRESS_FILE


def load_progress(repo_dir: str) -> str | None:
    """Read the committed ``PROGRESS.md`` for ``repo_dir`` (None if absent/empty)."""
    try:
        text = progress_path(repo_dir).read_text(encoding="utf-8").strip()
        return text or None
    except OSError:
        return None


def write_progress(repo_dir: str, content: str) -> None:
    """Write ``PROGRESS.md`` at the branch root (best-effort, never fatal)."""
    if not content:
        return
    try:
        progress_path(repo_dir).write_text(content.rstrip() + "\n", encoding="utf-8")
    except OSError:
        pass  # portable memory is best-effort; a write failure is not fatal


def load_session(repo_dir: str) -> str | None:
    """Read the saved claude session id for ``repo_dir`` (None if absent/empty)."""
    try:
        sid = session_path(repo_dir).read_text(encoding="utf-8").strip()
        return sid or None
    except OSError:
        return None


def save_session(repo_dir: str, session_id: str) -> None:
    """Persist a claude session id for ``repo_dir`` (best-effort, never fatal).

    The planner seeds this file so the implementer's resume picks up the same
    session and the plan carries into implementation.
    """
    if not session_id:
        return
    try:
        path = session_path(repo_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(session_id, encoding="utf-8")
    except OSError:
        pass  # context continuity is best-effort, never fatal


_SYSTEM = (
    "You are an autonomous implementer working a single GitHub ticket on an "
    "isolated git branch inside the current working directory. Read the ticket "
    "and its acceptance criteria, plan briefly, then implement the change. WRITE "
    "AND UPDATE TESTS as you go — every new behavior needs a test, and run the "
    "suite to confirm it passes. Commit your work on the current branch. When the "
    "acceptance criteria are met and tests pass, stop."
)

# Appended always: the committed-memory contract. PROGRESS.md is the portable
# working state that lets a cold start (no resumable session) continue the ticket.
_PROGRESS_RULE = (
    "Maintain a PROGRESS.md at the branch root and commit it on this branch. It "
    "must capture, in PROSE ONLY: what's done, what's left, your current "
    "approach/hypothesis, the files you touched, and the last reviewer asks (if "
    "revising). NEVER paste raw command output, environment values, or secrets "
    "into it — it is committed and reviewed in the PR diff, not a log dump. Treat "
    "the CURRENT PROGRESS.md below (if any) as your authoritative working memory: "
    "you may have no resumable session, so the prompt plus PROGRESS.md plus the "
    "git diff must be enough to continue correctly."
)

# Appended when the ticket is NOT labelled allow-sensitive: a hard ban on
# touching sensitive paths.
_DENY_SENSITIVE = (
    "Do NOT edit secrets, CI config, infrastructure, or database migrations. If "
    "the ticket appears to require such a change, stop and leave it for a human."
)

# Appended when the ticket IS labelled allow-sensitive: the sensitive paths are
# explicitly in scope for this ticket (they are otherwise denylisted, so the
# implementer would refuse them by default — and they sit outside the allowlist,
# so they must be named here for the agent to touch them).
_ALLOW_SENSITIVE = (
    "This ticket is labelled allow-sensitive: editing otherwise-sensitive paths "
    "(CI config, infra, migrations) IS permitted where the acceptance criteria "
    "require it. The following globs are in scope for this ticket even though "
    "they normally are not: {deny}. Touch them only when a criterion needs it."
)


class Implementer:
    """Runs one ticket through the Claude Code harness on a fresh branch."""

    def __init__(self, config: Config, harness: ClaudeHarness | None = None) -> None:
        self.config = config
        self.harness = harness or ClaudeHarness(config)

    # ------------------------------------------------------------------ #
    # git seams
    # ------------------------------------------------------------------ #
    @staticmethod
    def _git(repo_dir: str, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", repo_dir, *args],
            capture_output=True,
            text=True,
            check=False,
        )

    @staticmethod
    def _safe_path(repo_dir: str, rel: str) -> Path:
        """Resolve ``rel`` under ``repo_dir``, rejecting traversal/escape."""
        root = Path(repo_dir).resolve()
        candidate = (root / rel).resolve()
        if candidate != root and root not in candidate.parents:
            raise ValueError(f"path escapes repo_dir: {rel!r}")
        return candidate

    def _start_branch(self, repo_dir: str, branch: str) -> None:
        proc = self._git(repo_dir, "checkout", "-b", branch)
        if proc.returncode != 0:
            self._git(repo_dir, "checkout", branch)

    def push_branch(self, repo_dir: str, branch: str) -> bool:
        """Push the implementation branch to origin so a PR can be opened.

        Uses ``--force-with-lease`` so a re-run of ``idle/issue-N`` updates the
        remote branch safely rather than failing on a stale ref.
        """
        proc = self._git(repo_dir, "push", "--force-with-lease", "-u", "origin", branch)
        return proc.returncode == 0

    def _diff(self, repo_dir: str, base: str) -> str:
        for args in (("diff", f"{base}...HEAD"), ("diff", "HEAD"), ("diff",)):
            proc = self._git(repo_dir, *args)
            if proc.returncode == 0 and proc.stdout.strip():
                return proc.stdout
        return ""

    def _files_changed(self, repo_dir: str, base: str) -> list[str]:
        for args in (
            ("diff", "--name-only", f"{base}...HEAD"),
            ("diff", "--name-only", "HEAD"),
            ("diff", "--name-only"),
        ):
            proc = self._git(repo_dir, *args)
            if proc.returncode == 0 and proc.stdout.strip():
                return [ln for ln in proc.stdout.splitlines() if ln.strip()]
        return []

    # ------------------------------------------------------------------ #
    # Prompt
    # ------------------------------------------------------------------ #
    def _allow_sensitive(self, ticket: Ticket) -> bool:
        return ticket.has_label(self.config.labels.allow_sensitive)

    def _prompt(
        self,
        ticket: Ticket,
        feedback: str | None = None,
        progress: str | None = None,
        progress_enabled: bool = False,
    ) -> str:
        criteria = ticket.acceptance_criteria or []
        ac = "\n".join(f"- {c}" for c in criteria) if criteria else "(none listed)"
        allow = ", ".join(self.config.guards.path_allowlist) or "(none)"
        if self._allow_sensitive(ticket):
            deny = ", ".join(self.config.guards.path_denylist) or "(none)"
            scope_rule = _ALLOW_SENSITIVE.format(deny=deny)
        else:
            scope_rule = _DENY_SENSITIVE
        parts = [_SYSTEM]
        # The committed-memory contract only applies in PROGRESS mode; in RESUME
        # mode the prior session carries context and PROGRESS.md is not used.
        if progress_enabled:
            parts.append(_PROGRESS_RULE)
        parts += [
            f"--- TICKET #{ticket.number}: {ticket.title} ---\n{ticket.body}",
            f"Acceptance criteria:\n{ac}",
            f"Scope allowlist (primary paths you may touch): {allow}",
            scope_rule,
        ]
        # In PROGRESS mode the committed working memory travels on every
        # invocation, so a run with no resumable session is self-sufficient from
        # prompt + PROGRESS.md + diff.
        if progress_enabled:
            parts.append(
                "--- CURRENT PROGRESS.md (committed working state) ---\n"
                + (progress or "(none yet — this is the first turn on this branch)")
            )
        if feedback:
            parts.append(
                "--- A REVIEWER REQUESTED CHANGES on your previous attempt ---\n"
                f"{feedback}\n\n"
                "Address every point above by REVISING your existing work on this "
                "branch — do not start over or revert. When all points are resolved "
                "and tests pass, stop."
            )
        return "\n\n".join(parts) + "\n"

    # ------------------------------------------------------------------ #
    # Committed working memory (PROGRESS.md)
    # ------------------------------------------------------------------ #
    def _render_progress(
        self,
        ticket: Ticket,
        files_changed: list[str],
        notes: str,
        feedback: str | None,
    ) -> str:
        """Render a prose PROGRESS.md from a finished turn.

        The harness agent is asked to author PROGRESS.md itself; this is the
        deterministic fallback that guarantees a committed entry exists even when
        it didn't, synthesised only from structured data (never raw output) so it
        stays prose-only and secret-free.
        """
        done = (notes or "").strip() or "Worked the ticket on this branch."
        files = (
            "\n".join(f"- {f}" for f in files_changed)
            if files_changed
            else "- (no files changed yet)"
        )
        asks = feedback.strip() if feedback else "(none — no reviewer feedback yet)"
        return (
            f"# Progress — #{ticket.number}: {ticket.title}\n\n"
            "## Done\n"
            f"{done}\n\n"
            "## Remaining\n"
            "Verify the acceptance criteria are fully met and the suite is green; "
            "otherwise continue from the approach below.\n\n"
            "## Current approach\n"
            "Implementing the ticket's acceptance criteria on this branch, writing "
            "and running tests until they pass.\n\n"
            "## Files touched\n"
            f"{files}\n\n"
            "## Last reviewer asks\n"
            f"{asks}\n"
        )

    def _commit_progress(self, repo_dir: str, ticket: Ticket) -> None:
        """Stage and commit PROGRESS.md on the current branch (best-effort)."""
        self._git(repo_dir, "add", _PROGRESS_FILE)
        self._git(
            repo_dir,
            "commit",
            "-m",
            f"docs(progress): update PROGRESS.md for #{ticket.number}",
            "--",
            _PROGRESS_FILE,
        )

    # ------------------------------------------------------------------ #
    # Entry point
    # ------------------------------------------------------------------ #
    def run(
        self,
        ticket: Ticket,
        repo_dir: str,
        branch: str,
        feedback: str | None = None,
    ) -> ImplementationResult:
        """Implement ``ticket`` on ``branch`` via the harness; read back the diff.

        When ``feedback`` is given, the implementer revises its existing work on
        the same branch to address a reviewer's requested changes rather than
        starting a fresh implementation.

        Continuity is governed by ``config.progress_memory``: in RESUME mode the
        prior claude session for this worktree is resumed so the agent keeps
        context; in PROGRESS mode the run never resumes and instead reads/commits
        a portable ``PROGRESS.md``, so a cold start on any machine is sufficient.
        """
        progress_enabled = self.config.progress_memory
        # RESUME mode continues the prior session; PROGRESS mode never resumes (it
        # must not depend on a machine-local transcript) and relies on the
        # committed PROGRESS.md instead.
        resume = None if progress_enabled else load_session(repo_dir)
        try:
            self._start_branch(repo_dir, branch)
            # In PROGRESS mode read the committed memory AFTER landing on the
            # branch, so a cold start (resume=None) still gets the full state.
            progress = load_progress(repo_dir) if progress_enabled else None
            result = self.harness.run(
                self._prompt(ticket, feedback, progress, progress_enabled),
                cwd=repo_dir,
                resume_session_id=resume,
            )
        except HarnessRateLimited:
            raise  # propagate so the loop stops and the listener reschedules
        except Exception as exc:  # noqa: BLE001 - return a failed result, never raise
            return ImplementationResult(branch=branch, error=str(exc) or type(exc).__name__)

        save_session(repo_dir, result.session_id)

        base = self.config.merge.target_branch
        notes = (result.text or "")[:500]
        if progress_enabled:
            # Author/refresh the portable memory and commit it on the branch, so
            # the full working state travels in git, not a machine-local session.
            files_changed = self._files_changed(repo_dir, base)
            write_progress(
                repo_dir, self._render_progress(ticket, files_changed, notes, feedback)
            )
            self._commit_progress(repo_dir, ticket)

        diff = self._diff(repo_dir, base)
        files_changed = self._files_changed(repo_dir, base)
        return ImplementationResult(
            branch=branch,
            diff=diff,
            files_changed=files_changed,
            iterations=result.num_turns,
            cost_usd=result.cost_usd,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            notes=notes,
            no_progress=False,
            error="harness reported an error" if result.is_error else "",
        )


__all__ = [
    "Implementer",
    "session_path",
    "load_session",
    "save_session",
    "progress_path",
    "load_progress",
    "write_progress",
]
