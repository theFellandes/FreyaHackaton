"""Be polite to the Freya platform: space out API calls and never run two practice simulations at once.

The hackathon rules forbid probing or load-testing the platform, and the kit allows one simulation per workspace at a
time. Two small tools enforce that on the builder's side:

* :class:`PoliteLimiter` - thread-safe pacing. :meth:`PoliteLimiter.wait` sleeps just long enough that two
  consecutive calls start at least ``min_interval_s`` apart (``settings.freya_min_interval_s``, default 0.25 s).
  :func:`throttle_kit` installs it in front of the kit's ``api`` function, so *every* HTTP request of the kit
  (including the several requests inside ``save_workflow``) is paced, not only the wrapper's operations.
* :func:`simulation_slot` - a process-level lock. Whoever starts a practice simulation (``run_practice``, a
  self-test, the Streamlit app) holds it for the whole run; a second simulation in this process waits for the first.
"""

from __future__ import annotations

import functools
import logging
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from builder.freya.background import check_cancelled

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_MIN_INTERVAL_S",
    "PoliteLimiter",
    "SimulationBusy",
    "shared_limiter",
    "simulation_slot",
    "throttle_kit",
]

DEFAULT_MIN_INTERVAL_S = 0.25


class PoliteLimiter:
    """Keeps consecutive calls at least ``min_interval_s`` apart (thread-safe, FIFO).

    Args:
        min_interval_s: Minimum seconds between the *starts* of two calls; ``0`` disables pacing.
        clock: Monotonic seconds source.
        sleep: Blocking sleep (injectable for tests).
    """

    def __init__(
        self,
        min_interval_s: float = DEFAULT_MIN_INTERVAL_S,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Any] = time.sleep,
    ) -> None:
        self.min_interval_s = max(0.0, float(min_interval_s))
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next_start = float("-inf")
        self.calls = 0
        self.total_wait_s = 0.0

    def wait(self) -> float:
        """Block until a call may start; returns the seconds slept (``0.0`` when no wait was needed)."""
        with self._lock:  # reserve a start time, then sleep outside the lock
            now = self._clock()
            start = max(now, self._next_start)
            self._next_start = start + self.min_interval_s
            delay = start - now
            self.calls += 1
            self.total_wait_s += delay
        if delay > 0:
            self._sleep(delay)
        return max(delay, 0.0)

    def call(self, function: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        """Wait for permission, then run ``function(*args, **kwargs)``."""
        self.wait()
        return function(*args, **kwargs)


_shared_lock = threading.Lock()
_shared: PoliteLimiter | None = None


def shared_limiter(min_interval_s: float | None = None) -> PoliteLimiter:
    """The process-wide limiter (created on first use). A given ``min_interval_s`` updates its interval.

    One limiter for the whole process matters: every :class:`~builder.freya.client.FreyaClient` shares the one
    kit module, so they must also share one pacing clock.
    """
    global _shared
    with _shared_lock:
        if _shared is None:
            _shared = PoliteLimiter(DEFAULT_MIN_INTERVAL_S if min_interval_s is None else min_interval_s)
        elif min_interval_s is not None:
            _shared.min_interval_s = max(0.0, float(min_interval_s))
        return _shared


def throttle_kit(kit: Any, limiter: PoliteLimiter) -> bool:
    """Make every ``kit.api`` call wait for ``limiter`` first (idempotent).

    The kit's own functions look ``api`` up in the module namespace at call time, so replacing the attribute paces
    all of them, including the requests made inside ``save_workflow`` / ``update_workflow``. Before each request the
    wrapper also asks :func:`builder.freya.background.check_cancelled`, so a thread whose build was cancelled (or
    whose deadline passed) stops at its next request instead of finishing a multi-request operation.

    Returns:
        ``True`` when the kit was patched now, ``False`` when it already was (or has no callable ``api``).
    """
    api = getattr(kit, "api", None)
    if not callable(api) or getattr(api, "_freya_polite", False):
        return False

    @functools.wraps(api)
    def polite_api(*args: Any, **kwargs: Any) -> Any:
        check_cancelled()  # a cancelled build (wall-clock guard) must not start another request ...
        limiter.wait()
        check_cancelled()  # ... not even after waiting for its turn
        return api(*args, **kwargs)

    polite_api._freya_polite = True  # type: ignore[attr-defined]
    kit.api = polite_api
    return True


class SimulationBusy(RuntimeError):
    """A simulation slot could not be taken (timeout, or the calling thread already holds it)."""


_simulation_lock = threading.Lock()
_simulation_owner: int | None = None


@contextmanager
def simulation_slot(timeout_s: float | None = None) -> Iterator[None]:
    """Hold the process-level "one practice simulation at a time" lock for the duration of the block.

    Args:
        timeout_s: Longest wait for a running simulation to finish; ``None`` waits as long as it takes.

    Raises:
        SimulationBusy: The wait timed out, or this thread already holds the slot (nested use would deadlock).
    """
    global _simulation_owner
    me = threading.get_ident()
    if _simulation_owner == me:
        raise SimulationBusy("this thread already runs a simulation")
    if not _simulation_lock.acquire(timeout=-1 if timeout_s is None else max(0.0, timeout_s)):
        raise SimulationBusy(f"another simulation is still running after waiting {timeout_s} s")
    _simulation_owner = me
    try:
        yield
    finally:
        _simulation_owner = None
        _simulation_lock.release()
