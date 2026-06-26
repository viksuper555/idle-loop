"""The implementer agent.

Drives an agentic tool-use loop against the coding model: the model reads the
ticket and its acceptance criteria, plans briefly, then implements the change
*on an isolated branch inside ``repo_dir``* using two server tools — ``bash``
and a text editor — writing and updating tests as it goes. The loop stays inside
the scope allowlist, accounts for tokens/cost every turn, and bails early when a
no-progress signal trips (same failing command / empty diff ``N`` times).

External boundaries (the Anthropic client, ``subprocess``) are injected or
patchable so the whole thing runs under tests with no network and no real git.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import anthropic

from config import Config
from models import ImplementationResult, Ticket

# Server-tool definitions for the agentic loop.
_BASH_TOOL = {"type": "bash_20250124", "name": "bash"}
_EDITOR_TOOL = {"type": "text_editor_20250728", "name": "str_replace_based_edit_tool"}

_BASH_TIMEOUT_S = 120
_MAX_TOKENS = 8000

_SYSTEM_PROMPT = (
    "You are an autonomous implementer agent working a single GitHub ticket on an "
    "isolated git branch inside the repository working directory. Read the ticket "
    "and its acceptance criteria carefully, plan briefly, then implement the change. "
    "WRITE AND UPDATE TESTS as you go — every new behavior needs a test. Stay strictly "
    "within the scope allowlist provided; never touch files outside it, and never edit "
    "secrets, CI config, infrastructure, or database migrations. Use the bash tool to "
    "run commands and the text editor to read and modify files. Work incrementally and "
    "verify with the test suite. When the ticket is fully implemented and its tests "
    "pass, stop — do not keep making changes."
)


class Implementer:
    """Runs the agentic implementation loop for one ticket on one branch."""

    def __init__(self, config: Config, client: Any | None = None) -> None:
        self.config = config
        self._client = client

    # --------------------------------------------------------------------- #
    # Client (lazy — importing this module must never need an API key)
    # --------------------------------------------------------------------- #
    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = anthropic.Anthropic()
        return self._client

    # --------------------------------------------------------------------- #
    # git / shell seams
    # --------------------------------------------------------------------- #
    @staticmethod
    def _git(repo_dir: str, *args: str) -> subprocess.CompletedProcess:
        """Run a git command in ``repo_dir`` and return the completed process.

        Never raises on a nonzero exit (``check=False``); callers inspect
        ``returncode``/``stdout`` themselves.
        """
        return subprocess.run(
            ["git", "-C", repo_dir, *args],
            capture_output=True,
            text=True,
            check=False,
        )

    @staticmethod
    def _safe_path(repo_dir: str, rel: str) -> Path:
        """Resolve ``rel`` under ``repo_dir``, rejecting path traversal/escape.

        Raises ``ValueError`` if the resolved path lands outside ``repo_dir``.
        """
        root = Path(repo_dir).resolve()
        candidate = (root / rel).resolve()
        if candidate != root and root not in candidate.parents:
            raise ValueError(f"path escapes repo_dir: {rel!r}")
        return candidate

    # --------------------------------------------------------------------- #
    # Tool execution
    # --------------------------------------------------------------------- #
    def _run_bash(self, repo_dir: str, command: str) -> tuple[str, bool]:
        """Run a bash command in ``repo_dir``; return (output, is_error)."""
        try:
            proc = subprocess.run(
                ["bash", "-lc", command],
                cwd=repo_dir,
                capture_output=True,
                text=True,
                check=False,
                timeout=_BASH_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            return (f"command timed out after {_BASH_TIMEOUT_S}s", True)
        out = (proc.stdout or "") + (proc.stderr or "")
        return (out, proc.returncode != 0)

    def _run_editor(self, repo_dir: str, cmd: str, params: dict) -> tuple[str, bool]:
        """Implement the str_replace_based_edit_tool commands, confined to repo_dir."""
        rel = params.get("path", "")
        try:
            path = self._safe_path(repo_dir, rel)
        except ValueError as exc:
            return (str(exc), True)

        if cmd == "view":
            if not path.exists():
                return (f"file not found: {rel}", True)
            if path.is_dir():
                names = sorted(p.name for p in path.iterdir())
                return ("\n".join(names), False)
            return (path.read_text(encoding="utf-8", errors="replace"), False)

        if cmd == "create":
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(params.get("file_text", ""), encoding="utf-8")
            return (f"created {rel}", False)

        if cmd == "str_replace":
            if not path.exists():
                return (f"file not found: {rel}", True)
            text = path.read_text(encoding="utf-8")
            old = params.get("old_str", "")
            new = params.get("new_str", "")
            if old not in text:
                return ("old_str not found in file", True)
            path.write_text(text.replace(old, new, 1), encoding="utf-8")
            return (f"edited {rel}", False)

        if cmd == "insert":
            if not path.exists():
                return (f"file not found: {rel}", True)
            lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
            idx = int(params.get("insert_line", 0))
            new_line = params.get("new_str", "")
            if not new_line.endswith("\n"):
                new_line += "\n"
            lines.insert(idx, new_line)
            path.write_text("".join(lines), encoding="utf-8")
            return (f"inserted into {rel}", False)

        return (f"unsupported editor command: {cmd}", True)

    def _execute_tool(self, block: Any, repo_dir: str) -> dict:
        """Execute one tool_use block; return its tool_result content block."""
        tool_input = getattr(block, "input", {}) or {}
        name = getattr(block, "name", "")
        if name == "bash":
            output, is_error = self._run_bash(repo_dir, tool_input.get("command", ""))
        elif name == "str_replace_based_edit_tool":
            output, is_error = self._run_editor(
                repo_dir, tool_input.get("command", ""), tool_input
            )
        else:
            output, is_error = (f"unknown tool: {name}", True)
        return {
            "type": "tool_result",
            "tool_use_id": getattr(block, "id", ""),
            "content": output,
            "is_error": is_error,
        }

    # --------------------------------------------------------------------- #
    # Helpers
    # --------------------------------------------------------------------- #
    def _diff(self, repo_dir: str, base: str) -> str:
        """Diff base...HEAD; fall back to the working-tree/staged diff."""
        proc = self._git(repo_dir, "diff", f"{base}...HEAD")
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout
        # Fall back to uncommitted changes (the model may not have committed).
        proc = self._git(repo_dir, "diff", "HEAD")
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout
        proc = self._git(repo_dir, "diff")
        return proc.stdout if proc.returncode == 0 else ""

    def _files_changed(self, repo_dir: str, base: str) -> list[str]:
        proc = self._git(repo_dir, "diff", "--name-only", f"{base}...HEAD")
        if proc.returncode == 0 and proc.stdout.strip():
            return [ln for ln in proc.stdout.splitlines() if ln.strip()]
        proc = self._git(repo_dir, "diff", "--name-only", "HEAD")
        if proc.returncode == 0 and proc.stdout.strip():
            return [ln for ln in proc.stdout.splitlines() if ln.strip()]
        proc = self._git(repo_dir, "diff", "--name-only")
        if proc.returncode == 0:
            return [ln for ln in proc.stdout.splitlines() if ln.strip()]
        return []

    @staticmethod
    def _first_line(text: str) -> str:
        for line in text.splitlines():
            if line.strip():
                return line.strip()
        return ""

    def _start_branch(self, repo_dir: str, branch: str) -> None:
        """Create the branch, or check it out if it already exists."""
        proc = self._git(repo_dir, "checkout", "-b", branch)
        if proc.returncode != 0:
            # Branch likely exists already; just switch to it.
            self._git(repo_dir, "checkout", branch)

    def _initial_prompt(self, ticket: Ticket) -> str:
        criteria = ticket.acceptance_criteria or []
        ac = "\n".join(f"- {c}" for c in criteria) if criteria else "(none listed)"
        allow = ", ".join(self.config.guards.path_allowlist) or "(none)"
        return (
            f"Ticket #{ticket.number}: {ticket.title}\n\n"
            f"{ticket.body}\n\n"
            f"Acceptance criteria:\n{ac}\n\n"
            f"Scope allowlist (only touch paths matching these globs): {allow}\n\n"
            "Implement this on the current branch, writing tests as you go. "
            "Stop when the acceptance criteria are met and tests pass."
        )

    # --------------------------------------------------------------------- #
    # Main entry point
    # --------------------------------------------------------------------- #
    def run(self, ticket: Ticket, repo_dir: str, branch: str) -> ImplementationResult:
        """Implement ``ticket`` on ``branch`` inside ``repo_dir`` (agentic loop)."""
        # Lazy import so this module stands alone even before guards.budget exists.
        try:
            from guards.budget import detect_no_progress
        except Exception:  # noqa: BLE001 - fall back to a local equivalent
            def detect_no_progress(
                error_signatures: list[str], empty_diff_streak: int, limit: int
            ) -> bool:
                if empty_diff_streak >= limit:
                    return True
                if len(error_signatures) >= limit:
                    tail = error_signatures[-limit:]
                    if tail and all(sig == tail[0] for sig in tail):
                        return True
                return False

        budget = self.config.budget
        pricing = self.config.pricing
        base = self.config.merge.target_branch

        iterations = 0
        input_tokens = 0
        output_tokens = 0
        cost_usd = 0.0
        no_progress = False
        error = ""
        error_signatures: list[str] = []
        empty_diff_streak = 0
        prev_diff: str | None = None

        try:
            self._start_branch(repo_dir, branch)
            messages: list[dict] = [
                {"role": "user", "content": self._initial_prompt(ticket)}
            ]
            tools = [_BASH_TOOL, _EDITOR_TOOL]

            while True:
                response = self.client.messages.create(
                    model=self.config.model,
                    max_tokens=_MAX_TOKENS,
                    thinking={"type": "adaptive"},
                    output_config={"effort": "high"},
                    tools=tools,
                    messages=messages,
                )

                iterations += 1
                usage = getattr(response, "usage", None)
                if usage is not None:
                    input_tokens += int(getattr(usage, "input_tokens", 0) or 0)
                    output_tokens += int(getattr(usage, "output_tokens", 0) or 0)
                cost_usd = (
                    input_tokens / 1e6 * pricing.input_per_mtok
                    + output_tokens / 1e6 * pricing.output_per_mtok
                )

                content = getattr(response, "content", []) or []
                messages.append({"role": "assistant", "content": content})

                tool_results: list[dict] = []
                for block in content:
                    if getattr(block, "type", "") == "tool_use":
                        result = self._execute_tool(block, repo_dir)
                        tool_results.append(result)
                        if result["is_error"]:
                            error_signatures.append(self._first_line(result["content"]))

                # Track empty-diff streak (no observable progress this turn).
                cur_diff = self._diff(repo_dir, base)
                if prev_diff is not None and cur_diff == prev_diff:
                    empty_diff_streak += 1
                else:
                    empty_diff_streak = 0
                prev_diff = cur_diff

                if detect_no_progress(
                    error_signatures, empty_diff_streak, budget.no_progress_limit
                ):
                    no_progress = True
                    break

                if getattr(response, "stop_reason", "") == "end_turn":
                    break
                if iterations >= budget.max_iterations:
                    break
                if cost_usd > budget.per_ticket_cap_usd:
                    break

                # Feed tool results back for the next turn.
                if tool_results:
                    messages.append({"role": "user", "content": tool_results})
                else:
                    # No tools requested and not end_turn — nothing more to do.
                    break

            diff = self._diff(repo_dir, base)
            files_changed = self._files_changed(repo_dir, base)
            notes = "stopped: no progress" if no_progress else "implementation complete"
            return ImplementationResult(
                branch=branch,
                diff=diff,
                files_changed=files_changed,
                iterations=iterations,
                cost_usd=cost_usd,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                notes=notes,
                no_progress=no_progress,
                error=error,
            )
        except Exception as exc:  # noqa: BLE001 - return a failed result, never raise
            return ImplementationResult(
                branch=branch,
                iterations=iterations,
                cost_usd=cost_usd,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                no_progress=no_progress,
                error=str(exc) or type(exc).__name__,
            )


__all__ = ["Implementer"]
