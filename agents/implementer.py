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
    "suite to confirm it passes. Commit your work on the current branch. When the "
    "acceptance criteria are met and tests pass, stop."
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

    def _prompt(self, ticket: Ticket, feedback: str | None = None) -> str:
        criteria = ticket.acceptance_criteria or []
        ac = "\n".join(f"- {c}" for c in criteria) if criteria else "(none listed)"
        allow = ", ".join(self.config.guards.path_allowlist) or "(none)"
        if self._allow_sensitive(ticket):
            deny = ", ".join(self.config.guards.path_denylist) or "(none)"
            scope_rule = _ALLOW_SENSITIVE.format(deny=deny)
        else:
            scope_rule = _DENY_SENSITIVE
        parts = [
            _SYSTEM,
            f"--- TICKET #{ticket.number}: {ticket.title} ---\n{ticket.body}",
            f"Acceptance criteria:\n{ac}",
            f"Scope allowlist (primary paths you may touch): {allow}",
            scope_rule,
        ]
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
        """
        try:
            self._start_branch(repo_dir, branch)
            result = self.harness.run(self._prompt(ticket, feedback), cwd=repo_dir)
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
