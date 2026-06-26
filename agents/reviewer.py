"""The reviewer agent — backed by the Claude Code harness.

A DISTINCT role and context from the implementer: its only job is to decide
whether the diff actually satisfies the ticket's acceptance criteria — not
whether the code merely looks plausible. It runs ``claude -p`` headlessly in an
isolated temp directory with mutating tools disallowed, so it reasons over the
diff (passed in the prompt) and returns a structured JSON verdict without
touching the repo. Like the guards, it fails closed: any unparseable reply
yields ``request_changes``.

No API key — auth is the Claude Code login. A usage/session limit propagates as
:class:`HarnessRateLimited`.
"""

from __future__ import annotations

import json
import re
import tempfile

from agents.harness import ClaudeHarness
from config import Config
from models import Decision, ReviewVerdict

_READ_ONLY_DISALLOWED = ["Edit", "Write", "NotebookEdit", "Bash"]

_PROMPT = """You are a strict, independent code reviewer. Your ONLY job is to decide whether \
the diff below actually satisfies the ticket's acceptance criteria — not whether the code \
merely looks plausible. Request changes if ANY acceptance criterion is unmet, untested, or \
only partially implemented. Approve ONLY when every criterion is demonstrably met and tested.

--- TICKET #{number}: {title} ---
{body}

Acceptance criteria:
{criteria}

--- DIFF ---
{diff}

Respond with ONLY a single JSON object (no prose, no markdown fences) of the form:
{{"decision": "approve" | "request_changes", "summary": "<one or two sentences>", \
"comments": [{{"path": "<file>", "line": <int or null>, "body": "<what to fix>"}}]}}"""

_MAX_DIFF_CHARS = 60000


class Reviewer:
    """Judges a diff against a ticket's acceptance criteria via the harness."""

    def __init__(self, config: Config, harness: ClaudeHarness | None = None) -> None:
        self.config = config
        self.harness = harness or ClaudeHarness(config)

    def _build_prompt(self, ticket, diff: str) -> str:
        criteria = ticket.acceptance_criteria or []
        ac = "\n".join(f"- {c}" for c in criteria) if criteria else "(none listed)"
        if len(diff) > _MAX_DIFF_CHARS:
            diff = diff[:_MAX_DIFF_CHARS] + "\n... [diff truncated] ..."
        return _PROMPT.format(
            number=ticket.number, title=ticket.title, body=ticket.body, criteria=ac, diff=diff
        )

    def review(self, ticket, diff: str) -> ReviewVerdict:
        """Return the reviewer's verdict; fail closed on any parse problem.

        Lets :class:`HarnessRateLimited` propagate (do not swallow it).
        """
        with tempfile.TemporaryDirectory(prefix="idle-review-") as workdir:
            result = self.harness.run(
                self._build_prompt(ticket, diff),
                cwd=workdir,
                disallowed_tools=_READ_ONLY_DISALLOWED,
            )
        data = _extract_json(result.text)
        if data is None:
            return ReviewVerdict(
                decision=Decision.REQUEST_CHANGES,
                summary="reviewer did not return a parseable verdict; failing closed",
                comments=[],
            )
        return ReviewVerdict.from_dict(data)


_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_OBJECT = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json(text: str) -> dict | None:
    """Pull a JSON object out of the model's reply (tolerating fences/prose)."""
    if not text:
        return None
    text = text.strip()
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        pass
    fence = _FENCE.search(text)
    if fence:
        try:
            data = json.loads(fence.group(1))
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            return None
    obj = _OBJECT.search(text)
    if obj:
        try:
            data = json.loads(obj.group(0))
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            return None
    return None


__all__ = ["Reviewer"]
