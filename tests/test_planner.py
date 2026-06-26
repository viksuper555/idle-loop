"""Tests for the planner agent — harness fully mocked.

The harness is a fake returning a HarnessResult with the token-budget JSON in
``.text`` (or raising). No `claude`, no network. The planner must seed the
worktree session file so the implementer resumes the planning conversation.
"""

from __future__ import annotations

import json

import pytest

from agents.harness import HarnessRateLimited, HarnessResult
from agents.planner import Planner
from config import Config
from models import Ticket


class FakeHarness:
    def __init__(self, text="", session_id="sess-1", raises=None):
        self.text = text
        self.session_id = session_id
        self.raises = raises
        self.calls: list[dict] = []

    def run(self, prompt, cwd, **kw):
        self.calls.append({"prompt": prompt, "cwd": cwd, **kw})
        if self.raises is not None:
            raise self.raises
        return HarnessResult(
            text=self.text,
            cost_usd=0.05,
            num_turns=1,
            session_id=self.session_id,
            input_tokens=900,
            output_tokens=120,
        )


def ticket():
    return Ticket(number=1, title="t", body="b", acceptance_criteria=["a", "b"])


def planner(text="", session_id="sess-1", raises=None):
    h = FakeHarness(text=text, session_id=session_id, raises=raises)
    return Planner(Config(repo="o/n"), harness=h), h


def _budget_json(ein=1_000_000, eout=200_000):
    return json.dumps(
        {
            "estimated_input_tokens": ein,
            "estimated_output_tokens": eout,
            "plan": "do the thing",
            "rationale": "medium ticket",
        }
    )


def test_valid_budget_parsed(tmp_path):
    p, _ = planner(_budget_json(1_500_000, 300_000))
    res = p.plan(ticket(), str(tmp_path))
    assert res is not None
    assert res.estimated_input_tokens == 1_500_000
    assert res.estimated_output_tokens == 300_000
    assert res.total_tokens == 1_800_000
    assert res.plan_text == "do the thing"
    assert res.predicted_files == []  # absent "files" -> empty (pre-flight skips)


def test_predicted_files_parsed_and_sanitised(tmp_path):
    text = json.dumps(
        {
            "estimated_input_tokens": 1000,
            "estimated_output_tokens": 500,
            "plan": "p",
            "files": ["src/a.py", "tests/test_a.py", "", 7, None],
        }
    )
    res = planner(text)[0].plan(ticket(), str(tmp_path))
    assert res is not None
    # Only non-empty string entries survive; non-strings/blanks are dropped.
    assert res.predicted_files == ["src/a.py", "tests/test_a.py"]


def test_predicted_files_non_list_is_empty(tmp_path):
    text = json.dumps(
        {
            "estimated_input_tokens": 1000,
            "estimated_output_tokens": 500,
            "files": "src/a.py",  # not a list -> ignored
        }
    )
    res = planner(text)[0].plan(ticket(), str(tmp_path))
    assert res is not None and res.predicted_files == []


def test_seeds_session_file_for_implementer_resume(tmp_path):
    # The continuity contract: the planner writes the session id the implementer reads.
    from agents.implementer import load_session

    p, _ = planner(_budget_json(), session_id="sess-plan")
    p.plan(ticket(), str(tmp_path))
    assert load_session(str(tmp_path)) == "sess-plan"
    assert (tmp_path / ".idle-loop" / "session").read_text(encoding="utf-8") == "sess-plan"


def test_seeds_first_progress_from_plan(tmp_path):
    # The planner authors the first PROGRESS.md so the implementer's portable
    # memory exists from turn one, carrying the plan as the current approach.
    from agents.implementer import load_progress

    p, _ = planner(_budget_json())
    res = p.plan(ticket(), str(tmp_path))
    assert res is not None
    body = load_progress(str(tmp_path))
    assert body is not None
    assert "## Current approach" in body
    assert "do the thing" in body  # the plan text from _budget_json
    # Prose only — never a raw token-budget dump.
    assert "estimated_input_tokens" not in body


def test_no_progress_seeded_when_unparseable(tmp_path):
    from agents.implementer import load_progress

    p, _ = planner("no json here")
    assert p.plan(ticket(), str(tmp_path)) is None
    assert load_progress(str(tmp_path)) is None  # nothing to seed without a plan


def test_seeds_session_even_when_unparseable(tmp_path):
    from agents.implementer import load_session

    p, _ = planner("no json here", session_id="sess-plan")
    assert p.plan(ticket(), str(tmp_path)) is None  # falls back to heuristic
    assert load_session(str(tmp_path)) == "sess-plan"  # but context is still seeded


def test_json_in_code_fence_is_extracted(tmp_path):
    text = "Here is the budget:\n```json\n" + _budget_json(800_000, 100_000) + "\n```"
    res = planner(text)[0].plan(ticket(), str(tmp_path))
    assert res is not None and res.estimated_input_tokens == 800_000


def test_unparseable_returns_none(tmp_path):
    assert planner("ship it, looks cheap")[0].plan(ticket(), str(tmp_path)) is None


def test_missing_keys_returns_none(tmp_path):
    p, _ = planner(json.dumps({"plan": "x"}))
    assert p.plan(ticket(), str(tmp_path)) is None


def test_zero_budget_returns_none(tmp_path):
    assert planner(_budget_json(0, 0))[0].plan(ticket(), str(tmp_path)) is None


def test_negative_budget_returns_none(tmp_path):
    assert planner(_budget_json(-5, 100))[0].plan(ticket(), str(tmp_path)) is None


def test_rate_limit_propagates(tmp_path):
    p, _ = planner(raises=HarnessRateLimited(reset_at=1.0, reset_human="usage limit"))
    with pytest.raises(HarnessRateLimited):
        p.plan(ticket(), str(tmp_path))


def test_runs_read_only_in_repo_dir(tmp_path):
    p, h = planner(_budget_json())
    p.plan(ticket(), str(tmp_path))
    call = h.calls[0]
    assert call["cwd"] == str(tmp_path)  # the real worktree, not a tempdir
    disallowed = call.get("disallowed_tools", [])
    assert "Edit" in disallowed and "Write" in disallowed and "Bash" in disallowed


def test_uses_planner_model_when_set(tmp_path):
    cfg = Config(repo="o/n")
    cfg.planner.model = "claude-haiku-4-5"
    h = FakeHarness(text=_budget_json())
    Planner(cfg, harness=h).plan(ticket(), str(tmp_path))
    assert h.calls[0].get("model") == "claude-haiku-4-5"


def test_inherits_config_model_when_planner_model_unset(tmp_path):
    cfg = Config(repo="o/n")  # planner.model None -> inherit config.model
    h = FakeHarness(text=_budget_json())
    Planner(cfg, harness=h).plan(ticket(), str(tmp_path))
    assert h.calls[0].get("model") == cfg.model
