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

import logging

from config import Config
from guards.base import GuardContext
from models import GuardResult, Ticket

log = logging.getLogger("guards.budget")


def effective_caps(config: Config, ticket: Ticket | None) -> tuple[int, float]:
    """Per-ticket ``(max_iterations, per_ticket_cap_usd)`` for ``ticket``.

    Raised to the configured override ceilings when the ticket carries the
    ``labels.allow_budget`` override; otherwise the default caps. The global cap
    is never part of this — it stays the loop-wide hard stop.
    """
    b = config.budget
    if ticket is not None and ticket.has_label(config.labels.allow_budget):
        return b.override_max_iterations, b.override_per_ticket_cap_usd
    return b.max_iterations, b.per_ticket_cap_usd


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
        max_iterations, per_ticket_cap_usd = effective_caps(self.config, ctx.ticket)
        if (max_iterations, per_ticket_cap_usd) != (
            budget.max_iterations,
            budget.per_ticket_cap_usd,
        ):
            log.info(
                "#%s budget override active: max_iterations %d->%d, "
                "per_ticket_cap $%.2f->$%.2f (global cap $%.2f still applies)",
                ctx.ticket.number,
                budget.max_iterations,
                max_iterations,
                budget.per_ticket_cap_usd,
                per_ticket_cap_usd,
                budget.global_cap_usd,
            )

        if impl.no_progress:
            return GuardResult.fail(
                self.name,
                "no-progress detector tripped",
                iterations=impl.iterations,
                cost_usd=impl.cost_usd,
            )

        if impl.iterations > max_iterations:
            return GuardResult.fail(
                self.name,
                f"iterations {impl.iterations} over max {max_iterations}",
                iterations=impl.iterations,
                cost_usd=impl.cost_usd,
            )

        if impl.cost_usd > per_ticket_cap_usd:
            return GuardResult.fail(
                self.name,
                f"ticket cost ${impl.cost_usd:.2f} over cap "
                f"${per_ticket_cap_usd:.2f}",
                iterations=impl.iterations,
                cost_usd=impl.cost_usd,
            )

        return GuardResult(
            name=self.name,
            passed=True,
            reason=f"within budget: {impl.iterations} iters, ${impl.cost_usd:.2f}",
            details={"cost_usd": impl.cost_usd, "iterations": impl.iterations},
        )
