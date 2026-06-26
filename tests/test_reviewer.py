"""Tests for the review agent — harness fully mocked.

The Claude Code harness is a fake whose ``run`` returns a HarnessResult with the
verdict JSON in ``.text`` (or raises HarnessRateLimited). No `claude`, no network.
"""

from __future__ import annotations

import json

import pytest

from agents.harness import HarnessRateLimited, HarnessResult
from agents.reviewer import Reviewer
from config import Config
from models import Decision, Ticket


class FakeHarness:
    def __init__(self, text="", raises=None):
        self.text = text
        self.raises = raises
        self.calls: list[dict] = []

    def run(self, prompt, cwd, **kw):
        self.calls.append({"prompt": prompt, "cwd": cwd, **kw})
        if self.raises is not None:
            raise self.raises
        return HarnessResult(text=self.text, cost_usd=0.05, num_turns=1)


def ticket():
    return Ticket(number=1, title="t", body="b", acceptance_criteria=["a", "b"])


def reviewer(text="", raises=None):
    return Reviewer(Config(repo="o/n"), harness=FakeHarness(text=text, raises=raises))


def test_approve_parsed():
    r = reviewer(json.dumps({"decision": "approve", "summary": "all criteria met", "comments": []}))
    v = r.review(ticket(), "+diff")
    assert v.approved and v.summary == "all criteria met"


def test_request_changes_parsed():
    payload = {
        "decision": "request_changes",
        "summary": "criterion b untested",
        "comments": [{"path": "src/x.py", "line": 10, "body": "add a test"}],
    }
    v = reviewer(json.dumps(payload)).review(ticket(), "+diff")
    assert v.decision == Decision.REQUEST_CHANGES
    assert v.comments[0].path == "src/x.py" and v.comments[0].line == 10


def test_json_in_code_fence_is_extracted():
    text = 'Here is my verdict:\n```json\n{"decision": "approve", "summary": "ok", "comments": []}\n```'
    assert reviewer(text).review(ticket(), "+diff").approved


def test_unparseable_reply_fails_closed():
    v = reviewer("I think this looks fine, ship it!").review(ticket(), "+diff")
    assert v.decision == Decision.REQUEST_CHANGES  # fail closed, not approve


def test_empty_reply_fails_closed():
    assert reviewer("").review(ticket(), "+diff").decision == Decision.REQUEST_CHANGES


def test_rate_limit_propagates():
    r = reviewer(raises=HarnessRateLimited(reset_at=1.0, reset_human="usage limit"))
    with pytest.raises(HarnessRateLimited):
        r.review(ticket(), "+diff")


def test_reviewer_disallows_mutating_tools():
    h = FakeHarness(text=json.dumps({"decision": "approve", "summary": "ok", "comments": []}))
    Reviewer(Config(repo="o/n"), harness=h).review(ticket(), "+diff")
    disallowed = h.calls[0].get("disallowed_tools", [])
    assert "Edit" in disallowed and "Write" in disallowed and "Bash" in disallowed
