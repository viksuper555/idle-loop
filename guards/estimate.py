"""Pre-loop cost estimation — the signature feature (SPEC §7).

Prices a ticket *before* any tokens are spent so the orchestrator can triage by
ROI. The :class:`Estimator` scores cheap, explainable proxies (criteria count,
files touched, needs-tests flag, ambiguity) into ``estimated_iterations`` and
multiplies by a measured (or default) cost-per-iteration to get a dollar figure,
rendered honestly as a band such as ``~$30 ± $20``.

The :class:`EstimateGuard` wraps the estimator as a fail-closed gate: it passes
when ``estimated_cost <= config.triage.auto_threshold_usd`` and always stashes
the :class:`EstimateResult` and band string in ``result.details`` so the
orchestrator can print or comment them.

A learned v2 path (regression over logged rows) lives behind the same interface
and is engaged only when ``config.estimator.use_learned`` is set, enough rows
exist, and numpy + scikit-learn happen to be importable. It degrades silently to
the heuristic otherwise — sklearn/numpy are never hard requirements.
"""

from __future__ import annotations

import re

from config import Config
from guards.base import GuardContext
from models import EstimateFeatures, EstimateResult, GuardResult, Ticket

# --------------------------------------------------------------------------- #
# Tunables
# --------------------------------------------------------------------------- #
_MAX_EST_FILES = 25
_PATH_SPLIT = re.compile(r"[/._\-]+")
_WORD = re.compile(r"[a-z0-9]+")
_STRONG_KEYWORDS = ("module", "class", "file", "endpoint", "schema")
_VAGUE_WORDS = (
    "maybe",
    "should",
    "possibly",
    "etc",
    "unclear",
    "tbd",
    "figure out",
    "somehow",
    "probably",
    "not sure",
)
_TEST_WORDS = ("test", "tests", "coverage")
# Path tokens too generic to count as a meaningful file match.
_STOPWORD_TOKENS = frozenset(
    {
        "the",
        "a",
        "an",
        "to",
        "of",
        "in",
        "on",
        "for",
        "and",
        "or",
        "is",
        "py",
        "js",
        "ts",
        "src",
        "test",
        "tests",
        "index",
        "main",
        "init",
        "app",
        "lib",
        "utils",
        "util",
    }
)


def _clamp(value: float, lo: float, hi: float) -> float:
    """Clamp ``value`` into the inclusive ``[lo, hi]`` range."""
    return max(lo, min(hi, value))


def _tokenize(text: str) -> set[str]:
    """Lowercased alphanumeric word set of ``text``."""
    return set(_WORD.findall(text.lower()))


class Estimator:
    """Heuristic (v1) / optionally learned (v2) per-ticket cost estimator."""

    def __init__(self, config: Config, cost_log_path: str | None = None) -> None:
        self.config = config
        self.cost_log_path = cost_log_path or config.cost_log_path

    # ------------------------------------------------------------------ #
    # Feature extraction
    # ------------------------------------------------------------------ #
    def extract_features(
        self, ticket: Ticket, repo_tree: list[str] | None = None
    ) -> EstimateFeatures:
        """Score a ticket on cheap, explainable proxies (no tokens spent)."""
        n_criteria = len(ticket.acceptance_criteria)
        text = f"{ticket.title}\n{ticket.body}".lower()
        words = _tokenize(text)

        matched_path = False
        if repo_tree:
            est_files, matched_path = self._est_files_from_tree(repo_tree, words)
        else:
            est_files = self._est_files_from_text(n_criteria, text)

        needs_tests = any(w in words for w in _TEST_WORDS) or n_criteria > 0

        ambiguity = self._ambiguity(n_criteria, text, repo_tree, matched_path)

        features = EstimateFeatures(
            n_criteria=n_criteria,
            est_files=est_files,
            needs_tests=needs_tests,
            ambiguity_score=round(ambiguity, 3),
        )
        features.similarity_cost = self._similarity_cost(features)
        return features

    def _est_files_from_tree(
        self, repo_tree: list[str], words: set[str]
    ) -> tuple[int, bool]:
        """Count distinct repo files whose path tokens appear in the ticket."""
        hits = 0
        for path in repo_tree:
            tokens = {
                t
                for t in _PATH_SPLIT.split(path.lower())
                if t and t not in _STOPWORD_TOKENS
            }
            if tokens & words:
                hits += 1
        matched = hits > 0
        return int(_clamp(max(1, hits), 1, _MAX_EST_FILES)), matched

    def _est_files_from_text(self, n_criteria: int, text: str) -> int:
        """Fallback file estimate when no repo tree is available."""
        base = max(1, round(n_criteria / 2))
        keyword_bonus = sum(1 for kw in _STRONG_KEYWORDS if kw in text)
        return int(_clamp(base + keyword_bonus, 1, _MAX_EST_FILES))

    def _ambiguity(
        self,
        n_criteria: int,
        text: str,
        repo_tree: list[str] | None,
        matched_path: bool,
    ) -> float:
        """Compute an ambiguity penalty in ``0..1`` (0 clear, 1 vague)."""
        score = 0.0
        if n_criteria == 0:
            score += 0.4
        vague_hits = sum(text.count(w) for w in _VAGUE_WORDS)
        score += _clamp(0.1 * vague_hits, 0.0, 0.3)
        if len(text) < 200:
            score += 0.2
        if not repo_tree or not matched_path:
            score += 0.1
        return _clamp(score, 0.0, 1.0)

    def _similarity_cost(self, features: EstimateFeatures) -> float | None:
        """Measured actual cost of a near-identical past ticket, if any."""
        most_similar = self._costlog_fn("most_similar")
        if most_similar is None:
            return None
        try:
            record = most_similar(features, self.cost_log_path)
        except Exception:  # noqa: BLE001 - estimation never raises to the caller
            return None
        if record is None:
            return None
        try:
            return float(record.actual_cost)
        except (AttributeError, TypeError, ValueError):
            return None

    # ------------------------------------------------------------------ #
    # Estimation
    # ------------------------------------------------------------------ #
    def estimate(
        self, ticket: Ticket, repo_tree: list[str] | None = None
    ) -> EstimateResult:
        """Price a ticket: cost, iterations, confidence, and an honest band."""
        features = self.extract_features(ticket, repo_tree)
        rows = self._row_count()

        learned = self._try_learned(features, rows)
        if learned is not None:
            estimated_cost, estimated_iterations = learned
        else:
            estimated_iterations = self._heuristic_iterations(features)
            cost_per_iter = self._cost_per_iteration()
            estimated_cost = estimated_iterations * cost_per_iter
            if features.similarity_cost is not None:
                estimated_cost = 0.5 * estimated_cost + 0.5 * features.similarity_cost

        confidence = self._confidence(rows, features.ambiguity_score)
        margin = self._margin(estimated_cost, confidence)

        return EstimateResult(
            estimated_cost=round(estimated_cost, 2),
            estimated_iterations=round(estimated_iterations, 2),
            confidence=round(confidence, 3),
            features=features,
            margin=round(margin, 2),
        )

    def _heuristic_iterations(self, features: EstimateFeatures) -> float:
        """Transparent iteration estimate from the scored features."""
        raw = (
            2.0
            + 1.0 * features.n_criteria
            + 0.7 * features.est_files
            + (1.5 if features.needs_tests else 0.0)
            + 4.0 * features.ambiguity_score
        )
        ceiling = self.config.budget.max_iterations * 1.5
        return _clamp(raw, 1.0, ceiling)

    def _cost_per_iteration(self) -> float:
        """Measured cost/iteration from the log, else the configured prior."""
        measured = self._costlog_fn("measured_cost_per_iteration")
        if measured is not None:
            try:
                value = measured(self.cost_log_path)
            except Exception:  # noqa: BLE001 - never raise out of estimation
                value = None
            if value is not None and value > 0:
                return float(value)
        return self.config.pricing.default_cost_per_iteration_usd

    def _confidence(self, rows: int, ambiguity: float) -> float:
        """Higher with more logged rows and lower ambiguity; clamped to 0..1."""
        min_rows = max(1, self.config.estimator.min_rows_for_learned)
        data_term = 0.4 * _clamp(rows / min_rows, 0.0, 1.0)
        clarity_term = 0.3 * (1.0 - ambiguity)
        return _clamp(0.3 + data_term + clarity_term, 0.0, 1.0)

    def _margin(self, estimated_cost: float, confidence: float) -> float:
        """The +/- band: wider when confidence is low (floor at 0.4x cost)."""
        margin = estimated_cost * (1.0 - confidence)
        floor = 0.4 * estimated_cost
        if confidence < 0.5:
            margin = max(margin, floor)
        return max(0.0, margin)

    # ------------------------------------------------------------------ #
    # Cost-log access (lazy — costlog may not be importable in isolation)
    # ------------------------------------------------------------------ #
    @staticmethod
    def _costlog_fn(name: str):
        """Resolve a costlog function lazily; return None if unavailable."""
        try:
            import costlog  # local import: tolerate a missing sibling module
        except Exception:  # noqa: BLE001 - estimation degrades gracefully
            return None
        return getattr(costlog, name, None)

    def _row_count(self) -> int:
        """Number of rows currently in the cost log (0 if unreadable)."""
        read_all = self._costlog_fn("read_all")
        if read_all is None:
            return 0
        try:
            return len(read_all(self.cost_log_path))
        except Exception:  # noqa: BLE001 - missing/corrupt log -> no data
            return 0

    # ------------------------------------------------------------------ #
    # v2 learned path (optional; never a hard dependency)
    # ------------------------------------------------------------------ #
    def _try_learned(
        self, features: EstimateFeatures, rows: int
    ) -> tuple[float, float] | None:
        """Fit a tiny regression on logged rows and predict (cost, iters).

        Returns ``None`` — falling back to the heuristic — unless the learned
        path is enabled, enough rows exist, numpy + scikit-learn import, and the
        fit succeeds. Imports are lazy so neither dep is ever required.
        """
        if not self.config.estimator.use_learned:
            return None
        if rows < self.config.estimator.min_rows_for_learned:
            return None
        read_all = self._costlog_fn("read_all")
        if read_all is None:
            return None
        try:
            import numpy as np  # noqa: F401 - presence check + used below
            from sklearn.linear_model import LinearRegression
        except ImportError:
            return None

        try:
            records = read_all(self.cost_log_path)
            samples_x: list[list[float]] = []
            cost_y: list[float] = []
            iters_y: list[float] = []
            for rec in records:
                feats = rec.features or {}
                samples_x.append(
                    [
                        float(feats.get("n_criteria", 0)),
                        float(feats.get("est_files", 0)),
                        1.0 if feats.get("needs_tests") else 0.0,
                        float(feats.get("ambiguity_score", 0.0)),
                    ]
                )
                cost_y.append(float(rec.actual_cost))
                iters_y.append(float(rec.actual_iterations))
            if len(samples_x) < self.config.estimator.min_rows_for_learned:
                return None

            x_train = np.asarray(samples_x, dtype=float)
            row = np.asarray(
                [
                    [
                        float(features.n_criteria),
                        float(features.est_files),
                        1.0 if features.needs_tests else 0.0,
                        float(features.ambiguity_score),
                    ]
                ],
                dtype=float,
            )
            cost_pred = float(
                LinearRegression().fit(x_train, np.asarray(cost_y)).predict(row)[0]
            )
            iters_pred = float(
                LinearRegression().fit(x_train, np.asarray(iters_y)).predict(row)[0]
            )
        except Exception:  # noqa: BLE001 - any fit failure -> heuristic fallback
            return None

        ceiling = self.config.budget.max_iterations * 1.5
        return max(0.0, cost_pred), _clamp(iters_pred, 1.0, ceiling)


class EstimateGuard:
    """Fail-closed pre-loop gate: estimated cost vs the auto-triage threshold."""

    name = "estimate"

    def __init__(self, config: Config, estimator: Estimator | None = None) -> None:
        self.config = config
        self.estimator = estimator or Estimator(config)

    def check(self, ctx: GuardContext) -> GuardResult:
        """Pass when ``estimated_cost <= triage.auto_threshold_usd``.

        Always records the :class:`EstimateResult` under ``details["estimate"]``
        and its band under ``details["band"]`` (on pass and fail) so the
        orchestrator can surface the number. Fails closed on any error.
        """
        result = self.estimator.estimate(ctx.ticket)
        band = result.band()
        threshold = self.config.triage.auto_threshold_usd
        details = {"estimate": result, "band": band}
        if result.estimated_cost <= threshold:
            return GuardResult(
                name=self.name,
                passed=True,
                reason=f"estimated {band} within threshold ${threshold:g}",
                details=details,
            )
        return GuardResult(
            name=self.name,
            passed=False,
            reason=f"estimated {band} over threshold ${threshold:g}",
            details=details,
        )
