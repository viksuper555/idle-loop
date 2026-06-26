"""Tests for the review agent.

Every external boundary is mocked: the Anthropic client is a fake whose
``messages.create`` returns a response shaped like the SDK's (a ``.content``
list of text blocks). No network, no real client is ever constructed.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from agents.reviewer import Reviewer, _first_text_block
from config import Config
from models import Decision, Ticket


class FakeMessages:
    """Stand-in for ``client.messages`` that records the call and replays a stub."""

    def __init__(self, response: Any) -> None:
        self._response = response
        self.calls: list[dict] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


class FakeClient:
    def __init__(self, response: Any) -> None:
        self.messages = FakeMessages(response)


def _response_with_text(text: str) -> SimpleNamespace:
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)])


def _config() -> Config:
    return Config(repo="owner/name", model="claude-opus-4-8")


def _ticket() -> Ticket:
    return Ticket(
        number=7,
        title="Add a /healthz endpoint",
        body="## Acceptance Criteria\n- Returns 200\n- Has a test",
        acceptance_criteria=["Returns 200", "Has a test"],
    )


def test_approve_parses_into_verdict() -> None:
    payload = {
        "decision": "approve",
        "summary": "all criteria met and tested",
        "comments": [],
    }
    client = FakeClient(_response_with_text(json.dumps(payload)))
    reviewer = Reviewer(_config(), client=client)

    verdict = reviewer.review(_ticket(), diff="+ def healthz(): return 200")

    assert verdict.decision == Decision.APPROVE
    assert verdict.approved is True
    assert verdict.summary == "all criteria met and tested"
    assert verdict.comments == []


def test_request_changes_parses_into_verdict_with_comments() -> None:
    payload = {
        "decision": "request_changes",
        "summary": "no test for the endpoint",
        "comments": [
            {"path": "app.py", "line": 12, "body": "missing test for /healthz"}
        ],
    }
    client = FakeClient(_response_with_text(json.dumps(payload)))
    reviewer = Reviewer(_config(), client=client)

    verdict = reviewer.review(_ticket(), diff="+ def healthz(): return 200")

    assert verdict.decision == Decision.REQUEST_CHANGES
    assert verdict.approved is False
    assert len(verdict.comments) == 1
    c = verdict.comments[0]
    assert c.path == "app.py"
    assert c.line == 12
    assert c.body == "missing test for /healthz"


def test_malformed_text_fails_closed() -> None:
    client = FakeClient(_response_with_text("this is not json {"))
    reviewer = Reviewer(_config(), client=client)

    verdict = reviewer.review(_ticket(), diff="+ x = 1")

    assert verdict.decision == Decision.REQUEST_CHANGES
    assert "failing closed" in verdict.summary


def test_empty_text_fails_closed() -> None:
    client = FakeClient(_response_with_text(""))
    reviewer = Reviewer(_config(), client=client)

    verdict = reviewer.review(_ticket(), diff="+ x = 1")

    assert verdict.decision == Decision.REQUEST_CHANGES
    assert "failing closed" in verdict.summary


def test_no_content_blocks_fails_closed() -> None:
    client = FakeClient(SimpleNamespace(content=[]))
    reviewer = Reviewer(_config(), client=client)

    verdict = reviewer.review(_ticket(), diff="+ x = 1")

    assert verdict.decision == Decision.REQUEST_CHANGES


def test_non_object_json_fails_closed() -> None:
    # Valid JSON, but a list — not a verdict object.
    client = FakeClient(_response_with_text(json.dumps([1, 2, 3])))
    reviewer = Reviewer(_config(), client=client)

    verdict = reviewer.review(_ticket(), diff="+ x = 1")

    assert verdict.decision == Decision.REQUEST_CHANGES
    assert "failing closed" in verdict.summary


def test_sdk_exception_fails_closed() -> None:
    client = FakeClient(RuntimeError("api exploded"))
    reviewer = Reviewer(_config(), client=client)

    verdict = reviewer.review(_ticket(), diff="+ x = 1")

    assert verdict.decision == Decision.REQUEST_CHANGES
    assert "failing closed" in verdict.summary


def test_create_called_with_required_params() -> None:
    payload = {"decision": "approve", "summary": "ok", "comments": []}
    client = FakeClient(_response_with_text(json.dumps(payload)))
    reviewer = Reviewer(_config(), client=client)

    reviewer.review(_ticket(), diff="+ x = 1")

    assert len(client.messages.calls) == 1
    kwargs = client.messages.calls[0]
    assert kwargs["model"] == "claude-opus-4-8"
    assert kwargs["max_tokens"] == 8000
    assert kwargs["thinking"] == {"type": "adaptive"}
    oc = kwargs["output_config"]
    assert oc["effort"] == "high"
    assert oc["format"]["type"] == "json_schema"
    schema = oc["format"]["schema"]
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["decision", "summary", "comments"]
    assert schema["properties"]["decision"]["enum"] == ["approve", "request_changes"]
    # Forbidden sampling params must never be sent (they 400 on this model).
    assert "temperature" not in kwargs
    assert "top_p" not in kwargs
    assert "budget_tokens" not in kwargs
    # No system/user leakage into params we don't expect.
    assert kwargs["system"]
    assert kwargs["messages"][0]["role"] == "user"


def test_user_content_includes_criteria_and_diff() -> None:
    payload = {"decision": "approve", "summary": "ok", "comments": []}
    client = FakeClient(_response_with_text(json.dumps(payload)))
    reviewer = Reviewer(_config(), client=client)

    reviewer.review(_ticket(), diff="UNIQUE_DIFF_MARKER_42")

    content = client.messages.calls[0]["messages"][0]["content"]
    assert "Returns 200" in content
    assert "Has a test" in content
    assert "UNIQUE_DIFF_MARKER_42" in content
    assert "#7" in content


def test_large_diff_is_truncated_with_notice() -> None:
    payload = {"decision": "request_changes", "summary": "x", "comments": []}
    client = FakeClient(_response_with_text(json.dumps(payload)))
    reviewer = Reviewer(_config(), client=client)

    huge = "a" * 200_000
    reviewer.review(_ticket(), diff=huge)

    content = client.messages.calls[0]["messages"][0]["content"]
    assert "truncated" in content
    assert len(content) < 200_000


def test_first_text_block_helper() -> None:
    resp = SimpleNamespace(
        content=[
            SimpleNamespace(type="thinking", text="(reasoning)"),
            SimpleNamespace(type="text", text="hello"),
        ]
    )
    assert _first_text_block(resp) == "hello"
    assert _first_text_block(SimpleNamespace(content=None)) == ""


def test_lazy_client_not_constructed_when_provided() -> None:
    payload = {"decision": "approve", "summary": "ok", "comments": []}
    client = FakeClient(_response_with_text(json.dumps(payload)))
    reviewer = Reviewer(_config(), client=client)
    # Accessing .client must return our fake, never construct a real one.
    assert reviewer.client is client


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
