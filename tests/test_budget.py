"""Tests for guards.budget — detect_no_progress and BudgetGuard."""

from __future__ import annotations

from config import Config
from guards.base import GuardContext
from guards.budget import BudgetGuard, detect_no_progress, effective_caps
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
def _ctx(
    impl: ImplementationResult | None, config: Config, labels: list[str] | None = None
) -> GuardContext:
    ticket = Ticket(number=1, title="t", body="b", labels=labels or [])
    return GuardContext(ticket=ticket, config=config, implementation=impl)


def _config() -> Config:
    cfg = Config(repo="owner/name")
    cfg.budget.max_iterations = 10
    cfg.budget.per_ticket_cap_usd = 20.0
    cfg.budget.override_max_iterations = 40
    cfg.budget.override_per_ticket_cap_usd = 80.0
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


# --------------------------------------------------------------------------- #
# Budget override (idle:allow-budget) — ticket #28
# --------------------------------------------------------------------------- #
def test_effective_caps_default_without_label():
    cfg = _config()
    ticket = Ticket(number=1, title="t", body="b")
    assert effective_caps(cfg, ticket) == (10, 20.0)


def test_effective_caps_raised_with_label():
    cfg = _config()
    ticket = Ticket(number=1, title="t", body="b", labels=["idle:allow-budget"])
    assert effective_caps(cfg, ticket) == (40, 80.0)


def test_effective_caps_no_ticket_is_default():
    cfg = _config()
    assert effective_caps(cfg, None) == (10, 20.0)


def test_budget_override_allows_exceeding_default_caps():
    # iterations 11 (> default 10) and $30 (> default $20) but under the override
    # ceilings (40 / $80) -> passes with the label, where it would otherwise fail.
    cfg = _config()
    impl = ImplementationResult(branch="b", iterations=11, cost_usd=30.0)
    res = BudgetGuard(cfg).check(_ctx(impl, cfg, labels=["idle:allow-budget"]))
    assert res.passed is True


def test_budget_without_override_label_is_unchanged():
    # Same overspend, no label -> the default caps still park it.
    cfg = _config()
    impl = ImplementationResult(branch="b", iterations=11, cost_usd=30.0)
    res = BudgetGuard(cfg).check(_ctx(impl, cfg))
    assert res.passed is False


def test_budget_override_still_capped_at_override_ceiling():
    # The override raises the wall, it does not remove it: past the override
    # ceiling the guard fails again.
    cfg = _config()
    over_iters = ImplementationResult(branch="b", iterations=41, cost_usd=5.0)
    assert BudgetGuard(cfg).check(_ctx(over_iters, cfg, ["idle:allow-budget"])).passed is False
    over_cost = ImplementationResult(branch="b", iterations=5, cost_usd=80.01)
    assert BudgetGuard(cfg).check(_ctx(over_cost, cfg, ["idle:allow-budget"])).passed is False
