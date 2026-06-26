"""Tests for guards.budget — detect_no_progress and BudgetGuard."""

from __future__ import annotations

from config import Config
from guards.base import GuardContext
from guards.budget import BudgetGuard, detect_no_progress
from models import ImplementationResult, Ticket


# --------------------------------------------------------------------------- #
# detect_no_progress
# --------------------------------------------------------------------------- #
def test_no_progress_repeated_identical_errors():
    sigs = ["ImportError:x", "ImportError:x", "ImportError:x"]
    assert detect_no_progress(sigs, empty_diff_streak=0, limit=3) is True


def test_no_progress_identical_tail_after_varied_history():
    # Only the last `limit` must match; earlier noise is ignored.
    sigs = ["A", "B", "Boom", "Boom", "Boom"]
    assert detect_no_progress(sigs, empty_diff_streak=0, limit=3) is True


def test_no_progress_not_enough_errors():
    sigs = ["same", "same"]
    assert detect_no_progress(sigs, empty_diff_streak=0, limit=3) is False


def test_no_progress_mixed_recent_errors():
    sigs = ["a", "b", "c"]
    assert detect_no_progress(sigs, empty_diff_streak=0, limit=3) is False


def test_no_progress_last_differs():
    sigs = ["same", "same", "different"]
    assert detect_no_progress(sigs, empty_diff_streak=0, limit=3) is False


def test_no_progress_empty_diff_streak_reached():
    assert detect_no_progress([], empty_diff_streak=3, limit=3) is True


def test_no_progress_empty_diff_streak_over():
    assert detect_no_progress([], empty_diff_streak=5, limit=3) is True


def test_no_progress_empty_diff_streak_below():
    assert detect_no_progress([], empty_diff_streak=2, limit=3) is False


def test_no_progress_empty_inputs():
    assert detect_no_progress([], empty_diff_streak=0, limit=3) is False


def test_no_progress_zero_limit_disabled():
    # A non-positive limit disables detection.
    assert detect_no_progress(["x", "x", "x"], empty_diff_streak=99, limit=0) is False


# --------------------------------------------------------------------------- #
# BudgetGuard
# --------------------------------------------------------------------------- #
def _ctx(impl: ImplementationResult | None, config: Config) -> GuardContext:
    ticket = Ticket(number=1, title="t", body="b")
    return GuardContext(ticket=ticket, config=config, implementation=impl)


def _config() -> Config:
    cfg = Config(repo="owner/name")
    cfg.budget.max_iterations = 10
    cfg.budget.per_ticket_cap_usd = 20.0
    return cfg


def test_budget_passes_within_caps():
    cfg = _config()
    impl = ImplementationResult(branch="b", iterations=5, cost_usd=12.5)
    res = BudgetGuard(cfg).check(_ctx(impl, cfg))
    assert res.passed is True
    assert res.name == "budget"
    assert res.details == {"cost_usd": 12.5, "iterations": 5}


def test_budget_passes_at_exact_caps():
    cfg = _config()
    impl = ImplementationResult(branch="b", iterations=10, cost_usd=20.0)
    res = BudgetGuard(cfg).check(_ctx(impl, cfg))
    assert res.passed is True


def test_budget_fails_on_none_implementation():
    cfg = _config()
    res = BudgetGuard(cfg).check(_ctx(None, cfg))
    assert res.passed is False
    assert "no implementation" in res.reason


def test_budget_fails_on_no_progress():
    cfg = _config()
    impl = ImplementationResult(branch="b", iterations=3, cost_usd=5.0, no_progress=True)
    res = BudgetGuard(cfg).check(_ctx(impl, cfg))
    assert res.passed is False
    assert "no-progress" in res.reason


def test_budget_fails_on_too_many_iterations():
    cfg = _config()
    impl = ImplementationResult(branch="b", iterations=11, cost_usd=5.0)
    res = BudgetGuard(cfg).check(_ctx(impl, cfg))
    assert res.passed is False
    assert "iterations" in res.reason


def test_budget_fails_on_cost_over_cap():
    cfg = _config()
    impl = ImplementationResult(branch="b", iterations=5, cost_usd=20.01)
    res = BudgetGuard(cfg).check(_ctx(impl, cfg))
    assert res.passed is False
    assert "over cap" in res.reason
    assert "20.01" in res.reason


def test_budget_no_progress_precedence_over_caps():
    # no_progress is checked before the cap comparisons.
    cfg = _config()
    impl = ImplementationResult(
        branch="b", iterations=99, cost_usd=999.0, no_progress=True
    )
    res = BudgetGuard(cfg).check(_ctx(impl, cfg))
    assert res.passed is False
    assert "no-progress" in res.reason
