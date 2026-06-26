"""Guard interface and shared context.

Every guard fails closed: ``check()`` returns a :class:`GuardResult` whose
``passed`` is ``True`` only when the guard is satisfied. Any exception raised
inside a guard MUST be treated by the caller as a failure (see
:func:`run_guard`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from config import Config
from models import GuardResult, ImplementationResult, Ticket


@dataclass
class GuardContext:
    """Everything a guard might need.

    Pre-loop guards (estimate) only rely on ``ticket`` + ``config``.
    Post-implementation guards (scope, tests, security) additionally rely on
    ``repo_dir``, ``diff``, and ``files_changed``. ``budget`` reads the running
    iteration/cost accounting off ``implementation`` plus the in-loop signals.
    """

    ticket: Ticket
    config: Config
    repo_dir: str = "."
    diff: str = ""
    files_changed: list[str] = field(default_factory=list)
    implementation: ImplementationResult | None = None
    # In-loop signals for the budget no-progress detector.
    error_signatures: list[str] = field(default_factory=list)
    empty_diff_streak: int = 0


@runtime_checkable
class Guard(Protocol):
    """A fail-closed gate. ``name`` is stable and appears in logs/PR bodies."""

    name: str

    def check(self, ctx: GuardContext) -> GuardResult:  # pragma: no cover - protocol
        ...


def run_guard(guard: Guard, ctx: GuardContext) -> GuardResult:
    """Run a guard, converting any exception into a fail-closed result."""
    try:
        return guard.check(ctx)
    except Exception as exc:  # noqa: BLE001 - fail closed on anything
        return GuardResult.fail(
            getattr(guard, "name", guard.__class__.__name__),
            f"guard raised {type(exc).__name__}: {exc}",
        )


def run_all(guards: list[Guard], ctx: GuardContext) -> list[GuardResult]:
    """Run guards in order; stop at the first failure (fail closed)."""
    results: list[GuardResult] = []
    for guard in guards:
        res = run_guard(guard, ctx)
        results.append(res)
        if not res.passed:
            break
    return results
