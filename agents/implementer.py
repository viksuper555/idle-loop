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

_SYSTEM = (
    "You are an autonomous implementer working a single GitHub ticket on an "
    "isolated git branch inside the current working directory. Read the ticket "
    "and its acceptance criteria, plan briefly, then implement the change. WRITE "
    "AND UPDATE TESTS as you go — every new behavior needs a test, and run the "
    "suite to confirm it passes. Stay strictly within the scope allowlist given; "
    "never touch files outside it, and never edit secrets, CI config, infra, or "
    "database migrations. Commit your work on the current branch. When the "
    "acceptance criteria are met and tests pass, stop."
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
    def _prompt(self, ticket: Ticket) -> str:
        criteria = ticket.acceptance_criteria or []
        ac = "\n".join(f"- {c}" for c in criteria) if criteria else "(none listed)"
        allow = ", ".join(self.config.guards.path_allowlist) or "(none)"
        return (
            f"{_SYSTEM}\n\n"
            f"--- TICKET #{ticket.number}: {ticket.title} ---\n"
            f"{ticket.body}\n\n"
            f"Acceptance criteria:\n{ac}\n\n"
            f"Scope allowlist (only touch paths matching these globs): {allow}\n"
        )

    # ------------------------------------------------------------------ #
    # Entry point
    # ------------------------------------------------------------------ #
    def run(self, ticket: Ticket, repo_dir: str, branch: str) -> ImplementationResult:
        """Implement ``ticket`` on ``branch`` via the harness; read back the diff."""
        try:
            self._start_branch(repo_dir, branch)
            result = self.harness.run(self._prompt(ticket), cwd=repo_dir)
        except HarnessRateLimited:
            raise  # propagate so the loop stops and the listener reschedules
        except Exception as exc:  # noqa: BLE001 - return a failed result, never raise
            return ImplementationResult(branch=branch, error=str(exc) or type(exc).__name__)

        base = self.config.merge.target_branch
        diff = self._diff(repo_dir, base)
        files_changed = self._files_changed(repo_dir, base)
        return ImplementationResult(
            branch=branch,
            diff=diff,
            files_changed=files_changed,
            iterations=result.num_turns,
            cost_usd=result.cost_usd,
            notes=(result.text or "")[:500],
            no_progress=False,
            error="harness reported an error" if result.is_error else "",
        )


__all__ = ["Implementer"]
