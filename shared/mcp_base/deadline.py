"""Shared deadline utility — threads a hard wall-clock budget through every network op.

A single ``Deadline`` is built once at the top of ``_find_core`` (or any orchestrator) and
passed down as an optional ``deadline=`` kwarg into every helper.  Each helper computes its
own per-op socket/HTTP timeout as ``deadline.op(default_s)`` — this is always
``min(default_s, remaining)``, so an op can *never* run longer than the remaining budget.

Usage::

    from mcp_base.deadline import Deadline

    dl = Deadline(10.0)          # 10-second global budget
    timeout = dl.op(20.0)        # min(20, remaining)  —  always ≤ remaining budget
    dl.expired()                 # True once time is up
    dl.remaining()               # seconds left (never negative)

    # Thread safely into a helper that accepts an optional deadline:
    result = some_helper(..., deadline=dl)

    # Helpers that don't know about Deadline yet can fall back to a plain timeout:
    t = dl.op(30.0)              # clamp 30 → whatever is left
    some_legacy_call(timeout=t)
"""
from __future__ import annotations

import time


class Deadline:
    """A monotonic wall-clock deadline that propagates a hard budget through nested calls."""

    __slots__ = ("_end",)

    def __init__(self, budget_s: float) -> None:
        self._end = time.monotonic() + max(0.0, float(budget_s))

    # ------------------------------------------------------------------
    # Factory helpers
    # ------------------------------------------------------------------

    @classmethod
    def of(
        cls,
        d: "Deadline | float | None" = None,
        default_budget_s: float = 10.0,
    ) -> "Deadline":
        """Accept a ``Deadline``, a plain number of seconds, or ``None`` (→ ``default_budget_s``).

        Callers can write ``DL = Deadline.of(deadline, 10.0)`` and handle all three cases
        without an ``isinstance`` chain.
        """
        if isinstance(d, Deadline):
            return d
        if d is not None:
            return cls(float(d))
        return cls(default_budget_s)

    # ------------------------------------------------------------------
    # Core API
    # ------------------------------------------------------------------

    def remaining(self) -> float:
        """Seconds remaining.  Always ≥ 0 (never negative)."""
        return max(0.0, self._end - time.monotonic())

    def expired(self) -> bool:
        """``True`` once the deadline has passed."""
        return time.monotonic() >= self._end

    def op(self, default_s: float, floor: float = 0.5) -> float:
        """Return ``min(default_s, remaining)``, floored at ``floor`` (default 0.5 s).

        Use this to clamp any per-operation timeout to whatever budget is left::

            r = requests.get(url, timeout=deadline.op(20.0))
        """
        return max(floor, min(default_s, self.remaining()))

    def child(self, fraction: float) -> "Deadline":
        """Create a child deadline using a fraction of the remaining budget.

        Useful when dividing budget across parallel branches::

            phase1 = deadline.child(0.4)   # 40% of remaining
            phase2 = deadline.child(0.4)   # another 40%  (independent clocks)
        """
        return Deadline(self.remaining() * max(0.0, min(1.0, fraction)))

    def __repr__(self) -> str:
        r = self.remaining()
        return f"<Deadline remaining={r:.2f}s {'EXPIRED' if r == 0 else 'active'}>"
