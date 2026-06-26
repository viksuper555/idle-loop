"""Budget guard: the loop's circuit breaker.

Two responsibilities:

* :func:`detect_no_progress` — a cheap, in-loop signal the implementer calls
  each iteration to decide whether to bail early (the model is spinning on the
  same error or producing empty diffs).
* :class:`BudgetGuard` — a post-implementation gate that fails closed when a
  ticket overran its iteration cap, its dollar cap, or tripped the no-progress
  detector.
"""

from __future__ import annotations

from config import Config
from guards.base import GuardContext
from models import GuardResult


def detect_no_progress(
    error_signatures: list[str],
    empty_diff_streak: int,
    limit: int,
) -> bool:
    """Return ``True`` when the loop appears to be making no progress.

    No progress is declared when either:

    * the last ``limit`` ``error_signatures`` are identical and there are at
      least ``limit`` of them (the model is stuck on the same failure), or
    * ``empty_diff_streak`` has reached ``limit`` (repeated empty diffs).

    A non-positive ``limit`` disables detection (always ``False``).
    """
    if limit <= 0:
        return False
    if empty_diff_streak >= limit:
        return True
    if len(error_signatures) >= limit:
        tail = error_signatures[-limit:]
        if all(sig == tail[0] for sig in tail):
            return True
    return False


class BudgetGuard:
    """Fail-closed gate over an :class:`ImplementationResult`'s spend."""

    name = "budget"

    def __init__(self, config: Config) -> None:
        self.config = config

    def check(self, ctx: GuardContext) -> GuardResult:
        """Pass only when the implementation stayed within iteration and cost caps."""
        impl = ctx.implementation
        if impl is None:
            return GuardResult.fail(self.name, "no implementation to budget-check")

        budget = self.config.budget

        if impl.no_progress:
            return GuardResult.fail(
                self.name,
                "no-progress detector tripped",
                iterations=impl.iterations,
                cost_usd=impl.cost_usd,
            )

        if impl.iterations > budget.max_iterations:
            return GuardResult.fail(
                self.name,
                f"iterations {impl.iterations} over max {budget.max_iterations}",
                iterations=impl.iterations,
                cost_usd=impl.cost_usd,
            )

        if impl.cost_usd > budget.per_ticket_cap_usd:
            return GuardResult.fail(
                self.name,
                f"ticket cost ${impl.cost_usd:.2f} over cap "
                f"${budget.per_ticket_cap_usd:.2f}",
                iterations=impl.iterations,
                cost_usd=impl.cost_usd,
            )

        return GuardResult(
            name=self.name,
            passed=True,
            reason=f"within budget: {impl.iterations} iters, ${impl.cost_usd:.2f}",
            details={"cost_usd": impl.cost_usd, "iterations": impl.iterations},
        )
