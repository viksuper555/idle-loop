"""Tests for guards/estimate.py — the pre-loop cost estimator and gate.

No network, no real subprocess, no anthropic. The optional costlog sibling is
faked by injecting a stub module into ``sys.modules`` only where a test needs
the cost-log surface; otherwise the estimator runs purely on its heuristic.
"""

from __future__ import annotations

import sys
import types

import pytest

from config import Config, Pricing, Triage
from config import Estimator as EstimatorCfg
from guards.base import GuardContext
from guards.estimate import EstimateGuard, Estimator
from models import EstimateResult, Ticket, format_tokens


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def make_ticket(
    number: int = 1,
    title: str = "Add feature",
    body: str = "Implement the thing as described in the spec.",
    criteria: list[str] | None = None,
) -> Ticket:
    return Ticket(
        number=number,
        title=title,
        body=body,
        acceptance_criteria=list(criteria or []),
    )


def make_config(threshold: float = 50.0, default_cpi: float = 2.0) -> Config:
    cfg = Config(repo="owner/name", cost_log_path="cost_log.jsonl")
    cfg.triage = Triage(auto_threshold_usd=threshold)
    cfg.pricing = Pricing(default_cost_per_iteration_usd=default_cpi)
    cfg.estimator = EstimatorCfg(use_learned=False, min_rows_for_learned=20)
    return cfg


def install_fake_costlog(monkeypatch, **funcs) -> None:
    """Inject a stub ``costlog`` module exposing the given callables."""
    module = types.ModuleType("costlog")
    for name, fn in funcs.items():
        setattr(module, name, fn)
    monkeypatch.setitem(sys.modules, "costlog", module)


# --------------------------------------------------------------------------- #
# Feature extraction
# --------------------------------------------------------------------------- #
def test_no_criteria_is_ambiguous_and_no_tests():
    est = Estimator(make_config())
    feats = est.extract_features(make_ticket(criteria=[], body="x"))
    assert feats.n_criteria == 0
    assert feats.needs_tests is False
    # 0 criteria (+0.4), short body (+0.2), no repo match (+0.1)
    assert feats.ambiguity_score >= 0.7


def test_criteria_imply_tests():
    est = Estimator(make_config())
    feats = est.extract_features(make_ticket(criteria=["a", "b"]))
    assert feats.needs_tests is True
    assert feats.n_criteria == 2


def test_repo_tree_match_counts_files_and_lowers_ambiguity():
    est = Estimator(make_config())
    ticket = make_ticket(
        title="Fix the payments parser",
        body="Update the payments module and the parser helper. " * 5,
        criteria=["one"],
    )
    tree = ["src/payments.py", "src/parser.py", "src/unrelated.py", "README.md"]
    feats = est.extract_features(ticket, repo_tree=tree)
    assert feats.est_files >= 2  # payments + parser matched
    assert feats.ambiguity_score < 0.4  # matched a path, long body, has criteria


def test_est_files_clamped_to_at_least_one():
    est = Estimator(make_config())
    feats = est.extract_features(
        make_ticket(body="nothing matches here", criteria=[]),
        repo_tree=["src/zzz.py", "src/qqq.py"],
    )
    assert feats.est_files >= 1


# --------------------------------------------------------------------------- #
# Monotonicity
# --------------------------------------------------------------------------- #
def test_more_criteria_raises_cost_and_iterations():
    est = Estimator(make_config())
    small = est.estimate(make_ticket(criteria=["a"]))
    large = est.estimate(make_ticket(criteria=[f"c{i}" for i in range(8)]))
    assert large.estimated_iterations > small.estimated_iterations
    assert large.estimated_cost > small.estimated_cost


def test_more_criteria_monotonic_sequence():
    est = Estimator(make_config())
    prev = -1.0
    for n in (0, 1, 3, 6, 10):
        result = est.estimate(make_ticket(criteria=[f"c{i}" for i in range(n)]))
        assert result.estimated_cost > prev
        prev = result.estimated_cost


# --------------------------------------------------------------------------- #
# Band rendering
# --------------------------------------------------------------------------- #
def test_band_format_with_margin():
    result = EstimateResult(
        estimated_cost=30.0,
        estimated_iterations=10.0,
        confidence=0.3,
        features=make_ticket().acceptance_criteria and None,  # placeholder unused
        margin=20.0,
    )
    assert result.band() == "~$30 ± $20"


def test_band_format_no_margin():
    result = EstimateResult(
        estimated_cost=12.4,
        estimated_iterations=4.0,
        confidence=0.9,
        features=None,
        margin=0.0,
    )
    assert result.band() == "~$12"


def test_estimate_produces_band_string():
    est = Estimator(make_config())
    result = est.estimate(make_ticket(criteria=["a", "b"]))
    assert result.band().startswith("~$")


# --------------------------------------------------------------------------- #
# Cost-per-iteration from the cost log
# --------------------------------------------------------------------------- #
def test_uses_measured_cost_per_iteration(monkeypatch, tmp_path):
    log = str(tmp_path / "cost_log.jsonl")
    install_fake_costlog(
        monkeypatch,
        measured_cost_per_iteration=lambda path: 10.0,
        read_all=lambda path: [],
        most_similar=lambda feats, path: None,
    )
    est = Estimator(make_config(default_cpi=2.0), cost_log_path=log)
    result = est.estimate(make_ticket(criteria=["a"]))
    # Measured 10.0/iter should dominate over the 2.0 default prior.
    expected = result.estimated_iterations * 10.0
    assert result.estimated_cost == pytest.approx(round(expected, 2))


def test_falls_back_to_default_cost_per_iteration(monkeypatch):
    install_fake_costlog(
        monkeypatch,
        measured_cost_per_iteration=lambda path: None,
        read_all=lambda path: [],
        most_similar=lambda feats, path: None,
    )
    est = Estimator(make_config(default_cpi=2.0))
    result = est.estimate(make_ticket(criteria=["a"]))
    expected = result.estimated_iterations * 2.0
    assert result.estimated_cost == pytest.approx(round(expected, 2))


def test_no_costlog_module_uses_default(monkeypatch):
    # Ensure no stub is present: a missing costlog must not raise.
    monkeypatch.setitem(sys.modules, "costlog", None)
    est = Estimator(make_config(default_cpi=3.0))
    # import costlog -> None in sys.modules raises ImportError, handled gracefully.
    result = est.estimate(make_ticket(criteria=["a"]))
    assert result.estimated_cost > 0


def test_similarity_cost_blends_into_estimate(monkeypatch):
    sim = types.SimpleNamespace(actual_cost=4.0)
    install_fake_costlog(
        monkeypatch,
        measured_cost_per_iteration=lambda path: None,
        read_all=lambda path: [],
        most_similar=lambda feats, path: sim,
    )
    est = Estimator(make_config(default_cpi=2.0))
    feats = est.extract_features(make_ticket(criteria=["a"]))
    assert feats.similarity_cost == 4.0
    result = est.estimate(make_ticket(criteria=["a"]))
    heuristic = result.estimated_iterations * 2.0
    blended = 0.5 * heuristic + 0.5 * 4.0
    assert result.estimated_cost == pytest.approx(round(blended, 2))


# --------------------------------------------------------------------------- #
# Confidence
# --------------------------------------------------------------------------- #
def test_confidence_in_unit_range():
    est = Estimator(make_config())
    result = est.estimate(make_ticket(criteria=["a", "b"]))
    assert 0.0 <= result.confidence <= 1.0


def test_more_rows_increase_confidence(monkeypatch):
    cfg = make_config()
    cfg.estimator = EstimatorCfg(use_learned=False, min_rows_for_learned=10)

    def with_rows(n):
        rows = [object()] * n
        install_fake_costlog(
            monkeypatch,
            measured_cost_per_iteration=lambda path: None,
            read_all=lambda path, rows=rows: rows,
            most_similar=lambda feats, path: None,
        )
        return Estimator(cfg).estimate(make_ticket(criteria=["a"])).confidence

    assert with_rows(10) > with_rows(0)


# --------------------------------------------------------------------------- #
# EstimateGuard
# --------------------------------------------------------------------------- #
def _ctx(ticket: Ticket, cfg: Config) -> GuardContext:
    return GuardContext(ticket=ticket, config=cfg)


def test_guard_passes_below_threshold():
    cfg = make_config(threshold=1000.0)
    guard = EstimateGuard(cfg)
    ticket = make_ticket(criteria=["a", "b"])
    res = guard.check(_ctx(ticket, cfg))
    assert res.passed is True
    assert isinstance(res.details["estimate"], EstimateResult)
    assert res.details["band"].startswith("~$")


def test_guard_fails_above_threshold():
    cfg = make_config(threshold=1.0, default_cpi=5.0)
    guard = EstimateGuard(cfg)
    ticket = make_ticket(criteria=[f"c{i}" for i in range(10)])
    res = guard.check(_ctx(ticket, cfg))
    assert res.passed is False
    assert "over threshold" in res.reason
    # Details present on failure too, so the orchestrator can comment it.
    assert isinstance(res.details["estimate"], EstimateResult)
    assert res.details["band"] in res.reason or res.details["band"].startswith("~$")


def test_guard_name_is_estimate():
    assert EstimateGuard(make_config()).name == "estimate"


def test_guard_uses_injected_estimator():
    cfg = make_config()

    class StubEstimator:
        def estimate(self, ticket, repo_tree=None):
            return EstimateResult(
                estimated_cost=999.0,
                estimated_iterations=100.0,
                confidence=0.5,
                features=None,
                margin=10.0,
            )

    guard = EstimateGuard(cfg, estimator=StubEstimator())
    res = guard.check(_ctx(make_ticket(), cfg))
    assert res.passed is False
    assert res.details["estimate"].estimated_cost == 999.0


def test_guard_fails_closed_on_estimator_error():
    cfg = make_config()

    class Boom:
        def estimate(self, ticket, repo_tree=None):
            raise RuntimeError("kaboom")

    from guards.base import run_guard

    guard = EstimateGuard(cfg, estimator=Boom())
    res = run_guard(guard, _ctx(make_ticket(), cfg))
    assert res.passed is False


# --------------------------------------------------------------------------- #
# Learned path
# --------------------------------------------------------------------------- #
def test_learned_skipped_when_disabled(monkeypatch):
    # use_learned False -> _try_learned returns None even with rows + sklearn.
    cfg = make_config()
    cfg.estimator = EstimatorCfg(use_learned=False, min_rows_for_learned=2)
    est = Estimator(cfg)
    assert est._try_learned(est.extract_features(make_ticket()), rows=100) is None


def test_learned_skipped_when_too_few_rows():
    cfg = make_config()
    cfg.estimator = EstimatorCfg(use_learned=True, min_rows_for_learned=50)
    est = Estimator(cfg)
    assert est._try_learned(est.extract_features(make_ticket()), rows=3) is None


def test_learned_path_fits_when_sklearn_present(monkeypatch, tmp_path):
    pytest.importorskip("sklearn")
    pytest.importorskip("numpy")

    cfg = make_config()
    cfg.estimator = EstimatorCfg(use_learned=True, min_rows_for_learned=4)

    # Synthetic rows: cost strictly increases with n_criteria.
    def make_rec(n):
        return types.SimpleNamespace(
            features={
                "n_criteria": n,
                "est_files": n,
                "needs_tests": True,
                "ambiguity_score": 0.0,
            },
            actual_cost=float(n * 5),
            actual_iterations=n + 1,
        )

    records = [make_rec(n) for n in (1, 2, 3, 4, 5, 6)]
    install_fake_costlog(
        monkeypatch,
        read_all=lambda path: records,
        measured_cost_per_iteration=lambda path: None,
        most_similar=lambda feats, path: None,
    )
    est = Estimator(cfg, cost_log_path=str(tmp_path / "log.jsonl"))
    feats = est.extract_features(make_ticket(criteria=["a", "b", "c"]))
    learned = est._try_learned(feats, rows=len(records))
    assert learned is not None
    cost, iters = learned
    assert cost > 0
    assert iters >= 1.0


# --------------------------------------------------------------------------- #
# Deterministic helpers (pricing + token formatting + estimate source)
# --------------------------------------------------------------------------- #
def test_cost_for_tokens_blends_input_and_output_rates():
    p = Pricing(input_per_mtok=5.0, output_per_mtok=25.0)
    assert p.cost_for_tokens(1_000_000, 1_000_000) == pytest.approx(30.0)
    assert p.cost_for_tokens(0, 0) == 0.0
    assert p.cost_for_tokens(1_000_000, 200_000) == pytest.approx(10.0)


@pytest.mark.parametrize(
    "n,expected",
    [
        (0, "0"),
        (950, "950"),
        (8000, "8k"),
        (2500, "2.5k"),
        (46000, "46k"),
        (1_230_000, "1.23M"),
    ],
)
def test_format_tokens(n, expected):
    assert format_tokens(n) == expected


def test_heuristic_estimate_declares_no_token_budget():
    # The estimator itself is dollar-only: source "heuristic", zero token budget.
    est = Estimator(make_config()).estimate(make_ticket(criteria=["a", "b"]))
    assert est.source == "heuristic"
    assert est.estimated_tokens == 0
