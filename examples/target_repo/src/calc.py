"""A tiny calculator module — the demo target idle-loop extends."""

from __future__ import annotations


def add(a: float, b: float) -> float:
    """Return the sum of two numbers."""
    return a + b


def is_even(n: int) -> bool:
    """Return True if ``n`` is even."""
    return n % 2 == 0
