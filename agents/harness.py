"""Claude Code harness runner.

Drives the ``claude`` CLI in headless ``-p`` (print) mode so the agents use the
**Claude Code harness** — authenticated by your Claude login, no API key. The
implementer hands a whole ticket to ``claude -p`` and lets Claude Code edit
files / run tests with its own tools; the reviewer runs read-only.

This module also owns rate-limit handling: when the harness reports a usage /
session limit, :meth:`ClaudeHarness.run` raises :class:`HarnessRateLimited`
carrying the parsed reset time (epoch). The orchestrator turns that into a
process exit code the bash listener uses to reschedule via cron.

stdlib only (subprocess + json + datetime + re).
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

try:  # optional: better tz handling when the reset notice names one
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - zoneinfo is stdlib on 3.11+
    ZoneInfo = None  # type: ignore[assignment]


# --------------------------------------------------------------------------- #
# Errors / results
# --------------------------------------------------------------------------- #
class HarnessError(RuntimeError):
    """The claude CLI failed for a non-rate-limit reason."""


class HarnessRateLimited(Exception):
    """The Claude Code harness hit a usage / session limit.

    ``reset_at`` is a unix epoch (seconds) for when the window is expected to
    reset; ``reset_human`` is the raw notice text for logging.
    """

    def __init__(self, reset_at: float, reset_human: str = "", raw: str = "") -> None:
        super().__init__(f"claude usage limit; resets at epoch {reset_at:.0f}")
        self.reset_at = reset_at
        self.reset_human = reset_human
        self.raw = raw


@dataclass
class HarnessResult:
    """Parsed outcome of one ``claude -p`` invocation."""

    text: str = ""  # final assistant message
    cost_usd: float = 0.0  # total_cost_usd reported by Claude Code
    num_turns: int = 0
    input_tokens: int = 0  # from raw["usage"]; non-cache input tokens
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    is_error: bool = False
    session_id: str = ""
    returncode: int = 0
    raw: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Rate-limit detection + reset parsing
# --------------------------------------------------------------------------- #
_STRONG_LIMIT = (
    "usage limit",
    "limit reached",
    "limit will reset",
    "too many requests",
    "session limit",
)
_WEAK_LIMIT = ("rate limit", "resets at", "reset at", "try again", "overloaded", "429")

_TIME_AMPM = re.compile(r"(\d{1,2}):(\d{2})\s*([ap]\.?m\.?)", re.IGNORECASE)
_TIME_AMPM_HOURONLY = re.compile(r"\b(\d{1,2})\s*([ap]\.?m\.?)", re.IGNORECASE)
_TIME_ISO = re.compile(r"(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?)")
_TZ_PAREN = re.compile(r"\(([A-Za-z]+/[A-Za-z_]+)\)")


def is_rate_limited(returncode: int, is_error: bool, text: str) -> bool:
    """Heuristic: does this invocation look rate/usage limited?

    A strong phrase ("usage limit", "limit will reset", …) counts on its own. A
    weak phrase ("rate limit", "429", …) only counts when the call also errored,
    to avoid false positives on a ticket that merely *mentions* rate limiting.
    """
    t = (text or "").lower()
    if any(p in t for p in _STRONG_LIMIT):
        return True
    if (returncode != 0 or is_error) and any(p in t for p in _WEAK_LIMIT):
        return True
    return False


def parse_reset_time(text: str, *, window_hours: float = 5.0, now: float | None = None) -> float:
    """Parse the reset time from a limit notice; fall back to ``now + window``.

    Handles "resets at 2:10am (Europe/Sofia)", "4 PM", and ISO timestamps. The
    fallback (Claude's ~5h window) guarantees a usable epoch even when the notice
    is unparseable, so the listener can always reschedule.
    """
    now = time.time() if now is None else now
    text = text or ""

    iso = _TIME_ISO.search(text)
    if iso:
        try:
            dt = datetime.fromisoformat(iso.group(1).replace(" ", "T"))
            epoch = dt.timestamp()
            if epoch > now:
                return epoch
        except ValueError:
            pass

    tz = None
    tzm = _TZ_PAREN.search(text)
    if tzm and ZoneInfo is not None:
        try:
            tz = ZoneInfo(tzm.group(1))
        except Exception:  # noqa: BLE001 - unknown tz -> local time
            tz = None

    m = _TIME_AMPM.search(text)
    hour = minute = None
    if m:
        hour, minute, ampm = int(m.group(1)), int(m.group(2)), m.group(3).lower()
    else:
        mh = _TIME_AMPM_HOURONLY.search(text)
        if mh:
            hour, minute, ampm = int(mh.group(1)), 0, mh.group(2).lower()

    if hour is not None:
        if ampm.startswith("p") and hour != 12:
            hour += 12
        elif ampm.startswith("a") and hour == 12:
            hour = 0
        base = datetime.fromtimestamp(now, tz=tz)
        target = base.replace(hour=hour % 24, minute=minute, second=0, microsecond=0)
        if target.timestamp() <= now:
            target += timedelta(days=1)
        return target.timestamp()

    return now + window_hours * 3600.0


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #
class ClaudeHarness:
    """Runs the ``claude`` CLI headlessly and normalizes its JSON output."""

    def __init__(self, config, claude_bin: str | None = None) -> None:
        self.config = config
        self.claude_bin = claude_bin or config.harness.claude_bin

    def _build_cmd(
        self,
        prompt: str,
        *,
        skip_permissions: bool,
        allowed_tools: list[str] | None,
        disallowed_tools: list[str] | None,
        model: str | None,
        resume_session_id: str | None = None,
    ) -> list[str]:
        cmd = [self.claude_bin, "-p", prompt, "--output-format", self.config.harness.output_format]
        if model:
            cmd += ["--model", model]
        if resume_session_id:
            # Continue the prior conversation for this ticket, preserving context.
            cmd += ["--resume", resume_session_id]
        if skip_permissions:
            cmd.append("--dangerously-skip-permissions")
        if allowed_tools:
            cmd += ["--allowed-tools", *allowed_tools]
        if disallowed_tools:
            cmd += ["--disallowed-tools", *disallowed_tools]
        cmd += list(self.config.harness.extra_args)
        return cmd

    @staticmethod
    def _usage(raw: dict | None) -> dict[str, int]:
        """Token counts from a result object's ``usage`` block (all 0 if absent)."""
        usage = (raw or {}).get("usage") or {}

        def _int(key: str) -> int:
            try:
                return int(usage.get(key, 0) or 0)
            except (TypeError, ValueError):
                return 0

        return {
            "input_tokens": _int("input_tokens"),
            "output_tokens": _int("output_tokens"),
            "cache_creation_tokens": _int("cache_creation_input_tokens"),
            "cache_read_tokens": _int("cache_read_input_tokens"),
        }

    def run(
        self,
        prompt: str,
        cwd: str,
        *,
        skip_permissions: bool | None = None,
        allowed_tools: list[str] | None = None,
        disallowed_tools: list[str] | None = None,
        model: str | None = None,
        resume_session_id: str | None = None,
        timeout_s: int | None = None,
    ) -> HarnessResult:
        """Invoke ``claude -p`` in ``cwd``; raise :class:`HarnessRateLimited` on a limit.

        ``resume_session_id`` continues a prior ``claude`` session so the agent
        keeps its context across invocations (e.g. revision rounds, or a later
        loop run picking the ticket back up in its persistent worktree).
        """
        if skip_permissions is None:
            skip_permissions = self.config.harness.skip_permissions
        if model is None:
            model = self.config.model
        effective_timeout = (
            timeout_s if timeout_s is not None else self.config.harness.timeout_s
        )

        cmd = self._build_cmd(
            prompt,
            skip_permissions=skip_permissions,
            allowed_tools=allowed_tools,
            disallowed_tools=disallowed_tools,
            model=model,
            resume_session_id=resume_session_id,
        )

        try:
            proc = subprocess.run(
                cmd,
                cwd=cwd,
                capture_output=True,
                text=True,
                check=False,
                timeout=effective_timeout,
            )
        except FileNotFoundError as exc:
            raise HarnessError(
                f"claude CLI not found ({self.claude_bin!r}); install Claude Code "
                "or set harness.claude_bin"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise HarnessError(f"claude timed out after {effective_timeout}s") from exc

        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        raw = self._parse_json(stdout)

        text = str(raw.get("result", "")) if raw else stdout
        is_error = bool(raw.get("is_error", False)) if raw else proc.returncode != 0
        combined = "\n".join(filter(None, [text, stderr, raw.get("error", "") if raw else ""]))

        if is_rate_limited(proc.returncode, is_error, combined):
            reset_at = parse_reset_time(
                combined, window_hours=self.config.harness.rate_limit_window_hours
            )
            raise HarnessRateLimited(reset_at=reset_at, reset_human=combined.strip()[:300], raw=combined)

        if proc.returncode != 0 and not raw:
            raise HarnessError(
                f"claude exited {proc.returncode}: {stderr.strip() or stdout.strip()}"[:500]
            )

        return HarnessResult(
            text=text,
            cost_usd=float(raw.get("total_cost_usd", 0.0) or 0.0) if raw else 0.0,
            num_turns=int(raw.get("num_turns", 0) or 0) if raw else 0,
            **self._usage(raw),
            is_error=is_error,
            session_id=str(raw.get("session_id", "")) if raw else "",
            returncode=proc.returncode,
            raw=raw,
        )

    @staticmethod
    def _parse_json(stdout: str) -> dict:
        """Parse the claude JSON result; tolerate stray lines / empty output."""
        stdout = (stdout or "").strip()
        if not stdout:
            return {}
        try:
            data = json.loads(stdout)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            # Some configs emit one JSON object per line; take the last object.
            for line in reversed(stdout.splitlines()):
                line = line.strip()
                if line.startswith("{") and line.endswith("}"):
                    try:
                        data = json.loads(line)
                        if isinstance(data, dict):
                            return data
                    except json.JSONDecodeError:
                        continue
        return {}


__all__ = [
    "ClaudeHarness",
    "HarnessError",
    "HarnessRateLimited",
    "HarnessResult",
    "is_rate_limited",
    "parse_reset_time",
]
