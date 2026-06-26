"""The review agent — a deliberately separate role from the implementer.

The reviewer is a fresh, skeptical context whose *only* job is to decide
whether a diff actually satisfies a ticket's acceptance criteria — not whether
the code merely looks plausible. It never shares state, prompt, or instance
with the implementer; the orchestrator constructs it independently and feeds it
the implementer's output as untrusted input. This separation is the point:
the agent that wrote the code is the worst judge of whether it works.

Like the guards, the reviewer fails closed. Any refusal, empty response, or
parse error yields a ``request_changes`` verdict rather than a false approval.
"""

from __future__ import annotations

import json
from typing import Any

import anthropic

from config import Config
from models import Decision, ReviewVerdict, Ticket

# Cap the diff we ship to the model so a runaway change can't blow the context
# window. The reviewer is told explicitly when truncation has happened so it
# treats a clipped diff as unverified (and therefore not approvable).
_MAX_DIFF_CHARS = 60_000

# JSON schema the model must conform to. Mirrors ReviewVerdict / ReviewComment.
_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["decision", "summary", "comments"],
    "properties": {
        "decision": {
            "type": "string",
            "enum": ["approve", "request_changes"],
        },
        "summary": {"type": "string"},
        "comments": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["path", "line", "body"],
                "properties": {
                    "path": {"type": "string"},
                    "line": {"type": "integer"},
                    "body": {"type": "string"},
                },
            },
        },
    },
}

_SYSTEM = """You are a skeptical, independent code reviewer. You did NOT write \
this code, and your sole job is to decide whether the submitted diff ACTUALLY \
satisfies the ticket's acceptance criteria — not whether the code merely looks \
plausible or well-written.

Be adversarial about evidence. For every acceptance criterion, ask: is it \
demonstrably implemented AND covered by a test or other proof in this diff?

- Report `request_changes` if ANY acceptance criterion is unmet, untested, \
only partially implemented, or addressed only by code that looks right but is \
not exercised.
- Approve ONLY when every acceptance criterion is demonstrably met.
- If the diff was truncated, treat the unseen portion as unverified and do not \
approve on faith.

Put one concrete comment per problem, anchored to a file path and line where \
you can, explaining what is missing and what would satisfy the criterion."""


def _safe_default() -> ReviewVerdict:
    """The fail-closed verdict used whenever we cannot trust the model output."""
    return ReviewVerdict(
        decision=Decision.REQUEST_CHANGES,
        summary="reviewer could not produce a verdict; failing closed",
        comments=[],
    )


def _truncate_diff(diff: str) -> str:
    """Clip an oversized diff, flagging the clip so the model stays honest."""
    if len(diff) <= _MAX_DIFF_CHARS:
        return diff
    head = diff[:_MAX_DIFF_CHARS]
    omitted = len(diff) - _MAX_DIFF_CHARS
    return (
        f"{head}\n\n"
        f"... [diff truncated: {omitted} characters omitted; the remainder "
        f"was NOT shown to you — treat unseen changes as unverified] ..."
    )


def _format_criteria(criteria: list[str]) -> str:
    if not criteria:
        return "(no explicit acceptance criteria were parsed from the ticket)"
    return "\n".join(f"- {c}" for c in criteria)


class Reviewer:
    """A separate review-agent context. Constructed independently of the implementer.

    The Anthropic client is created lazily so importing this module never
    requires an API key; tests pass a fake ``client``.
    """

    def __init__(self, config: Config, client: Any | None = None) -> None:
        self.config = config
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = anthropic.Anthropic()
        return self._client

    def _build_user_content(self, ticket: Ticket, diff: str) -> str:
        return (
            f"# Ticket #{ticket.number}: {ticket.title}\n\n"
            f"## Description\n{ticket.body or '(no description)'}\n\n"
            f"## Acceptance criteria\n{_format_criteria(ticket.acceptance_criteria)}\n\n"
            f"## Unified diff under review\n```diff\n{_truncate_diff(diff)}\n```\n\n"
            "Decide whether this diff demonstrably satisfies every acceptance "
            "criterion above, and return your verdict in the required schema."
        )

    def review(self, ticket: Ticket, diff: str) -> ReviewVerdict:
        """Review ``diff`` against ``ticket``'s acceptance criteria.

        Returns a :class:`ReviewVerdict`. Fails closed (``request_changes``) on
        any refusal, empty response, or malformed output.
        """
        try:
            response = self.client.messages.create(
                model=self.config.model,
                max_tokens=8000,
                thinking={"type": "adaptive"},
                output_config={
                    "effort": "high",
                    "format": {"type": "json_schema", "schema": _SCHEMA},
                },
                system=_SYSTEM,
                messages=[
                    {"role": "user", "content": self._build_user_content(ticket, diff)}
                ],
            )
        except Exception:  # noqa: BLE001 - fail closed on any SDK/transport error
            return _safe_default()

        text = _first_text_block(response)
        if not text:
            return _safe_default()

        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            return _safe_default()
        if not isinstance(data, dict):
            return _safe_default()

        try:
            return ReviewVerdict.from_dict(data)
        except Exception:  # noqa: BLE001 - any shape we didn't expect -> fail closed
            return _safe_default()


def _first_text_block(response: Any) -> str:
    """Pull the first text block out of an Anthropic-style response."""
    content = getattr(response, "content", None)
    if not content:
        return ""
    for block in content:
        if getattr(block, "type", None) == "text":
            return getattr(block, "text", "") or ""
    return ""
