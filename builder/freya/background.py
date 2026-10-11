"""Run blocking platform calls in abandonable daemon threads, and stop them from starting anything new after a cancel.

Why this exists
---------------
The kit client uses ``urllib`` (blocking, 120 s per request), so the builder runs its calls in worker threads. The
wall-clock guard of ``build.py`` cancels the *coroutine* that awaits such a call, but nothing can interrupt the thread
inside the HTTP request. Two things used to follow from that with ``asyncio.to_thread``:

* ``asyncio.run`` joins the default executor on exit and so did the interpreter, which delayed the final JSON line and
  the process exit until the blocked call returned - possibly past the 30 minute limit of the hackathon;
* the call (and the rest of ``save``: model fix, publish) went on after the build had already been reported as
  ``failed``, creating or publishing a workflow nobody had been told about.

What it does now
----------------
:func:`run_blocking` runs the function in a plain **daemon** thread (never joined at shutdown) and hands the result
back through a future, so a cancelled waiter returns at once. A :class:`CancelToken` is bound to the thread; it is
cancelled when the waiter is cancelled and it can carry an absolute deadline. :func:`check_cancelled` - called before
every request of the paced kit (:func:`builder.freya.ratelimit.throttle_kit`) and before every operation of
:class:`~builder.freya.client.FreyaClient` - raises :class:`PlatformCancelled` once the token is cancelled or expired,
so a call that was in flight when the guard fired is the last one: nothing follows it. :func:`wait_inflight` lets
``build.py`` give such a call a short grace period so that whatever it created can still be reported.

The token travels in a :class:`contextvars.ContextVar` that :func:`run_blocking` sets inside the worker thread, so
there is no global state: two builds in one process (or two tests) never cancel each other.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

__all__ = [
    "CancelToken",
    "PlatformCancelled",
    "bound_deadline",
    "bound_token",
    "check_cancelled",
    "current_token",
    "inflight_count",
    "run_blocking",
    "time_left",
    "wait_inflight",
]

T = TypeVar("T")


class PlatformCancelled(RuntimeError):
    """A platform request was not started because the build was cancelled or its deadline passed."""


class CancelToken:
    """A cancellation flag with an optional absolute deadline, safe to share between threads.

    Args:
        deadline: Instant (on ``clock``) after which :meth:`check` refuses; ``None`` for no deadline.
        clock: Monotonic seconds source (injectable for tests).
    """

    def __init__(self, deadline: float | None = None, clock: Callable[[], float] = time.monotonic) -> None:
        self.deadline = deadline
        self._clock = clock
        self._event = threading.Event()
        self.reason = ""

    def cancel(self, reason: str = "cancelled") -> None:
        """Cancel the token (idempotent; the first reason is kept)."""
        if not self._event.is_set():
            self.reason = reason
            self._event.set()

    @property
    def cancelled(self) -> bool:
        """True once :meth:`cancel` was called."""
        return self._event.is_set()

    @property
    def expired(self) -> bool:
        """True once the deadline has passed."""
        return self.deadline is not None and self._clock() >= self.deadline

    def check(self) -> None:
        """Raise :class:`PlatformCancelled` when the token is cancelled or past its deadline."""
        if self._event.is_set():
            raise PlatformCancelled(f"platform call not started: {self.reason}")
        if self.expired:
            raise PlatformCancelled("platform call not started: the build deadline has passed")

    def remaining(self) -> float | None:
        """Seconds left before the deadline (``0.0`` once cancelled or past it); ``None`` without a deadline."""
        if self._event.is_set():
            return 0.0
        if self.deadline is None:
            return None
        return max(0.0, self.deadline - self._clock())


_CURRENT: contextvars.ContextVar[CancelToken | None] = contextvars.ContextVar(
    "freya_cancel_token", default=None
)


def current_token() -> CancelToken | None:
    """The token bound to the running thread / task, if any."""
    return _CURRENT.get()


def check_cancelled() -> None:
    """Raise :class:`PlatformCancelled` when the bound token says the build is over (no token: no-op)."""
    token = _CURRENT.get()
    if token is not None:
        token.check()


#: An absolute deadline (and the clock it is measured on) bound by :func:`bound_deadline`, next to the token's own.
_DEADLINE: contextvars.ContextVar[tuple[tuple[float, Callable[[], float]], ...]] = contextvars.ContextVar(
    "freya_deadline", default=()
)


def time_left() -> float | None:
    """Seconds before the nearest bound deadline (the token's, or one set by :func:`bound_deadline`).

    ``0.0`` when the token is cancelled or a deadline has passed; ``None`` when nothing limits the running thread. The
    Freya transport caps every request's timeout with this (``min(120, time_left())``), and the client stops retrying
    when too little is left, so no request or back-off outlives the build's deadline.
    """
    candidates: list[float] = []
    token = _CURRENT.get()
    if token is not None:
        left = token.remaining()
        if left is not None:
            candidates.append(left)
    for deadline, clock in _DEADLINE.get():
        candidates.append(max(0.0, deadline - clock()))
    return min(candidates) if candidates else None


@contextmanager
def bound_deadline(
    deadline: float | None, clock: Callable[[], float] = time.monotonic
) -> Iterator[None]:
    """Limit the current context by an absolute ``deadline`` (a ``clock`` reading); ``None`` changes nothing.

    Nested deadlines add up: the nearest one counts, an inner block can never extend an outer one.
    """
    if deadline is None:
        yield
        return
    marker = _DEADLINE.set((*_DEADLINE.get(), (deadline, clock)))
    try:
        yield
    finally:
        _DEADLINE.reset(marker)


@contextmanager
def bound_token(token: CancelToken | None) -> Iterator[None]:
    """Bind ``token`` to the current context for the duration of the block (for direct, non-threaded use)."""
    marker = _CURRENT.set(token)
    try:
        yield
    finally:
        _CURRENT.reset(marker)


# ------------------------------------------------------------------------------------------------ daemon threads

_inflight: set[threading.Thread] = set()
_inflight_lock = threading.Lock()


def inflight_count() -> int:
    """How many :func:`run_blocking` threads are still running."""
    with _inflight_lock:
        return sum(1 for thread in _inflight if thread.is_alive())


async def wait_inflight(timeout_s: float, poll_s: float = 0.02) -> bool:
    """Wait up to ``timeout_s`` for every :func:`run_blocking` thread to finish; True when none is left."""
    deadline = time.monotonic() + max(0.0, timeout_s)
    while inflight_count() > 0:
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(poll_s)
    return True


def _deliver(
    loop: asyncio.AbstractEventLoop, future: asyncio.Future[Any], result: Any, error: BaseException | None
) -> None:
    """Hand a worker result to the waiting coroutine; ignore a waiter that is gone or a loop that is closed."""

    def apply() -> None:
        if future.done():  # the waiter was cancelled (the guard fired): the late result is dropped
            if error is not None and not isinstance(error, PlatformCancelled):
                logger.info(
                    "a platform call finished after it was abandoned (%s: %s)", type(error).__name__, error
                )
            return
        if error is not None:
            future.set_exception(error)
        else:
            future.set_result(result)

    try:
        loop.call_soon_threadsafe(apply)
    except RuntimeError:  # the event loop is closed: nobody is waiting any more
        logger.debug("the event loop closed before a platform call finished")


async def run_blocking(
    function: Callable[..., T], /, *args: Any, token: CancelToken | None = None, **kwargs: Any
) -> T:
    """Run ``function(*args, **kwargs)`` in a daemon thread and await its result.

    If the awaiting task is cancelled, the call returns immediately (the thread is *not* joined, now or at interpreter
    exit) and ``token`` is cancelled, so the thread's next platform request is refused instead of started.

    Args:
        function: The blocking callable (a :class:`~builder.freya.client.FreyaClient` method, a subprocess runner ...).
        token: Bound to the thread (see :func:`check_cancelled`); cancelled when the waiter is cancelled.
    """
    loop = asyncio.get_running_loop()
    future: asyncio.Future[T] = loop.create_future()
    context = contextvars.copy_context()

    def target() -> None:
        _CURRENT.set(token)
        result: Any = None
        error: BaseException | None = None
        try:
            result = function(*args, **kwargs)
        except BaseException as exc:  # delivered to the waiter, never lost in the thread
            error = exc
        finally:
            with _inflight_lock:
                _inflight.discard(threading.current_thread())
        _deliver(loop, future, result, error)

    name = getattr(function, "__qualname__", None) or getattr(function, "__name__", "call")
    thread = threading.Thread(target=lambda: context.run(target), name=f"freya-call:{name}", daemon=True)
    with _inflight_lock:
        _inflight.add(thread)
    thread.start()
    try:
        return await future
    except asyncio.CancelledError:
        if token is not None:
            token.cancel("the awaiting task was cancelled (wall-clock guard)")
        raise
