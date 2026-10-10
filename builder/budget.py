"""Wall-clock budget of one build.

The hackathon allows at most 30 minutes per brief, self-tests included, and build time is the second tie-break. The
graph therefore creates one :class:`Budget` at start-up (``settings.build_time_budget_s``, 28 minutes by default, a
safety margin below the limit) and every stage asks it before starting work that might not finish in time:

>>> budget = Budget(1680)
>>> budget.allows(STAGE_ESTIMATES_S["practice"])  # enough time left for a practice run, keeping 120 s in reserve?
True

The budget uses a monotonic clock (immune to wall-clock jumps); :meth:`Budget.deadline` returns the monotonic
instant at which the budget runs out, which the LLM gateway uses to cap call timeouts.

:data:`STAGE_ESTIMATES_S` holds pessimistic durations of each stage, measured on practice runs; routing code uses
them to skip optional work (self-tests, repairs) when it would not fit.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import Final

#: Reserve kept free by default when asking :meth:`Budget.allows` (finalize, export and trace need a little time).
DEFAULT_RESERVE_S: Final[float] = 120.0

#: Pessimistic duration of each builder stage in seconds (read-only).
STAGE_ESTIMATES_S: Final[Mapping[str, float]] = MappingProxyType(
    {
        "understand": 240.0,
        "extract": 300.0,
        "prose": 240.0,
        "compile": 20.0,
        "remote_validate": 30.0,
        "deploy": 90.0,
        "smoke": 90.0,
        "walkthrough": 180.0,
        "practice": 780.0,
        "agentic_build": 1100.0,
        "repair": 360.0,
    }
)


class Budget:
    """A fixed amount of wall-clock time that starts running when the object is created.

    Args:
        total_s: Size of the budget in seconds; must be positive (``math.inf`` means "unlimited").
        clock: Monotonic seconds source; injectable so tests can move time by hand.

    Raises:
        ValueError: If ``total_s`` is not a positive number.
    """

    def __init__(self, total_s: float, clock: Callable[[], float] = time.monotonic) -> None:
        total = float(total_s)
        if math.isnan(total) or total <= 0:
            raise ValueError(f"budget must be a positive number of seconds, got {total_s!r}")
        self.total_s = total
        self._clock = clock
        self._start = clock()

    def elapsed(self) -> float:
        """Seconds spent since the budget was created."""
        return max(0.0, self._clock() - self._start)

    def remaining(self) -> float:
        """Seconds left (never negative)."""
        return max(0.0, self.total_s - self.elapsed())

    def expired(self) -> bool:
        """True once no time is left."""
        return self.remaining() <= 0.0

    def allows(self, seconds: float, reserve_s: float = DEFAULT_RESERVE_S) -> bool:
        """True when ``seconds`` of work still fit while keeping ``reserve_s`` seconds free at the end.

        Args:
            seconds: Expected duration of the work (for a stage: ``STAGE_ESTIMATES_S[stage]``).
            reserve_s: Time that must remain afterwards (finalize, export, trace).
        """
        if seconds < 0 or reserve_s < 0:
            raise ValueError("seconds and reserve_s must not be negative")
        return self.remaining() - reserve_s >= seconds

    def allows_stage(self, stage: str, reserve_s: float = DEFAULT_RESERVE_S) -> bool:
        """:meth:`allows` with the estimate of a named stage from :data:`STAGE_ESTIMATES_S`.

        Raises:
            KeyError: If the stage has no estimate.
        """
        return self.allows(estimate(stage), reserve_s)

    def deadline(self) -> float:
        """The instant (on the budget's monotonic clock) at which the budget runs out."""
        return self._start + self.total_s

    def snapshot(self) -> dict[str, float]:
        """Elapsed / remaining / total seconds, rounded, for traces and graph state."""
        return {
            "total_s": round(self.total_s, 3),
            "elapsed_s": round(self.elapsed(), 3),
            "remaining_s": round(self.remaining(), 3),
        }

    def __repr__(self) -> str:
        return f"Budget(total_s={self.total_s:g}, remaining_s={self.remaining():.1f})"


def estimate(stage: str) -> float:
    """The pessimistic duration of ``stage`` in seconds.

    Raises:
        KeyError: With the list of known stages when ``stage`` has no estimate.
    """
    try:
        return STAGE_ESTIMATES_S[stage]
    except KeyError:
        known = ", ".join(sorted(STAGE_ESTIMATES_S))
        raise KeyError(f"no time estimate for stage {stage!r}; known stages: {known}") from None


__all__ = ["DEFAULT_RESERVE_S", "STAGE_ESTIMATES_S", "Budget", "estimate"]
