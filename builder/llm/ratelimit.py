"""Client-side rate limiting for LLM calls.

Why: the builder talks to LLM providers with a handful of API keys and several graph nodes can ask at once. Without
a limiter the first burst earns a 429, the key is put on cooldown and the build stalls (or burns credits retrying).
The limiter keeps the builder politely *below* the provider limits so a 429 stays the exception:

* one global :class:`asyncio.Semaphore` caps how many provider calls are in flight (``llm_max_concurrency``);
* every ``(provider, key)`` pair has three :class:`TokenBucket` objects: a request bucket (``rpm``), a token bucket
  (``tpm``) and a pacing bucket (one call per ``llm_min_interval_s``);
* :meth:`RateLimiter.penalize` blocks a key's buckets after a real 429 (Retry-After or the cooldown setting).

The buckets use *reservations*: a caller reserves its share at once (the balance may go negative) and sleeps
until the reservation is due. That keeps waiters in FIFO order without a lock and makes the wait computable, so a
caller can refuse to wait past a deadline (:class:`RateLimitTimeout`) instead of sleeping and then failing. A slot
reserves in all of a key's buckets first and then sleeps for the longest of the waits (not their sum).

Typical use (done by :class:`builder.llm.gateway.LLMGateway` for every attempt)::

    async with limiter.slot("openai", key, estimate_tokens(system, user, max_tokens), deadline) as waited:
        reply = await provider.complete_text(...)

A limit of ``0`` disables that limit. Everything takes injectable ``clock`` / ``sleep`` so tests run in zero time.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import re
import time
import weakref
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_MAX_CONCURRENCY",
    "DEFAULT_MIN_INTERVAL_S",
    "DEFAULT_RPM_PER_KEY",
    "DEFAULT_TPM_PER_KEY",
    "RateLimitConfig",
    "RateLimitTimeout",
    "RateLimiter",
    "TokenBucket",
    "estimate_tokens",
    "parse_rate_overrides",
]

DEFAULT_MAX_CONCURRENCY = 2
DEFAULT_RPM_PER_KEY = 30
DEFAULT_TPM_PER_KEY = 150_000
DEFAULT_MIN_INTERVAL_S = 0.5

#: ``Settings`` attribute names the limiter reads; an object with none of them gets no limiter at all.
SETTINGS_FIELDS = (
    "llm_max_concurrency",
    "llm_rpm_per_key",
    "llm_tpm_per_key",
    "llm_min_interval_s",
    "llm_rate_overrides",
)

_OVERRIDE_RE = re.compile(r"^\s*(?P<provider>[A-Za-z0-9_\-]+)\s*=\s*(?P<rpm>\d+)\s*:\s*(?P<tpm>\d+)\s*$")


class RateLimitTimeout(TimeoutError):
    """Waiting for the rate limit would pass the caller's deadline (nothing was consumed)."""


# --------------------------------------------------------------------------------------------------------------
# Token bucket
# --------------------------------------------------------------------------------------------------------------


class TokenBucket:
    """A refilling bucket with reservations.

    ``capacity`` tokens at most; ``refill_per_s`` tokens are added per second. :meth:`acquire` reserves tokens
    immediately and sleeps until the reservation is due, so concurrent callers are served in order and the average
    rate never exceeds ``refill_per_s``. A request larger than the capacity is clamped to the capacity (it is
    served when the bucket is full).

    Args:
        capacity: Maximum (and initial) number of tokens; must be positive.
        refill_per_s: Tokens added per second; ``0`` means the bucket never refills.
        clock: Monotonic seconds source.
        sleep: Async sleep (injectable for tests).
    """

    def __init__(
        self,
        capacity: float,
        refill_per_s: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        if refill_per_s < 0:
            raise ValueError("refill_per_s must not be negative")
        self.capacity = float(capacity)
        self.refill_per_s = float(refill_per_s)
        self._clock = clock
        self._sleep = sleep
        self._tokens = float(capacity)
        # Time up to which the balance is already accounted for. It lies in the future while the bucket is
        # penalised: nothing is added (and nothing can be taken) before it.
        self._last = clock()

    def _refill(self, now: float) -> None:
        if now > self._last:
            self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.refill_per_s)
            self._last = now

    def available(self) -> float:
        """Tokens that can be taken right now without waiting (never negative; ``0`` while penalised)."""
        now = self._clock()
        self._refill(now)
        if now < self._last:
            return 0.0
        return max(0.0, self._tokens)

    def reserve(self, amount: float, deadline: float | None = None) -> float:
        """Take ``amount`` (clamped to the capacity) now and return the seconds until it is due (``0.0`` = now).

        The caller is responsible for waiting that long (or for :meth:`refund` -ing when it gives up).

        Raises:
            RateLimitTimeout: The wait would pass ``deadline`` (or the bucket never refills); nothing was taken.
        """
        amount = min(max(float(amount), 0.0), self.capacity)
        now = self._clock()
        self._refill(now)
        deficit = amount - self._tokens
        if deficit <= 0:
            extra = 0.0
        elif self.refill_per_s > 0:
            extra = deficit / self.refill_per_s
        else:
            raise RateLimitTimeout("the bucket never refills")
        wait = max(self._last - now, 0.0) + extra
        if deadline is not None and now + wait > deadline:
            raise RateLimitTimeout(f"waiting {wait:.1f}s for the rate limit would pass the deadline")
        self._tokens -= amount
        return wait

    def refund(self, amount: float) -> None:
        """Give back tokens that were reserved but not used (a cancelled or failed call)."""
        self._tokens = min(self.capacity, self._tokens + max(float(amount), 0.0))

    def consume(self, amount: float) -> None:
        """Charge extra usage afterwards (the balance may go negative: later callers wait for it)."""
        self._tokens -= max(float(amount), 0.0)

    async def acquire(self, amount: float = 1.0, deadline: float | None = None) -> float:
        """Reserve ``amount`` tokens, sleeping until they are due.

        Args:
            amount: Tokens to take (clamped to the capacity).
            deadline: Monotonic time (same clock) the caller must not wait past.

        Returns:
            Seconds waited (``0.0`` when the tokens were available).

        Raises:
            RateLimitTimeout: The wait would pass ``deadline``; nothing was taken.
        """
        wait = self.reserve(amount, deadline)
        if wait <= 0:
            return 0.0
        try:
            await self._sleep(wait)
        except BaseException:  # cancelled while waiting: give the reservation back
            self.refund(min(max(float(amount), 0.0), self.capacity))
            raise
        return wait

    def penalize(self, seconds: float) -> None:
        """Hand out no tokens before ``now + seconds`` (after a 429). Longer penalties win over shorter ones."""
        if seconds <= 0:
            return
        now = self._clock()
        self._refill(now)
        self._last = max(self._last, now + seconds)


# --------------------------------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------------------------------


def parse_rate_overrides(
    text: str | Mapping[str, Any] | None, *, strict: bool = False
) -> dict[str, tuple[int, int]]:
    """Parse ``"openai=60:200000,anthropic=30:100000"`` into ``{"openai": (60, 200000), ...}``.

    Each entry is ``provider=rpm:tpm``; entries are separated by commas, semicolons or whitespace. A ``0`` disables
    that limit for the provider. Provider names are lower-cased with ``-`` turned into ``_``.

    Args:
        text: The setting value (or an already parsed mapping, which is normalised).
        strict: Raise :class:`ValueError` on a malformed entry instead of skipping it with a warning.
    """
    result: dict[str, tuple[int, int]] = {}
    if not text:
        return result
    if isinstance(text, Mapping):
        for provider, pair in text.items():
            rpm, tpm = pair
            result[str(provider).strip().lower().replace("-", "_")] = (int(rpm), int(tpm))
        return result
    for raw in re.split(r"[,;\n]+", text):
        if not raw.strip():
            continue
        match = _OVERRIDE_RE.match(raw)
        if match is None:
            if strict:
                raise ValueError(f"invalid rate override {raw.strip()!r}; expected provider=rpm:tpm")
            logger.warning("ignoring malformed LLM rate override %r (expected provider=rpm:tpm)", raw.strip())
            continue
        provider = match["provider"].lower().replace("-", "_")
        result[provider] = (int(match["rpm"]), int(match["tpm"]))
    return result


@dataclass(frozen=True)
class RateLimitConfig:
    """Limits of the LLM rate limiter (see :func:`parse_rate_overrides` for ``overrides``).

    Attributes:
        max_concurrency: Provider calls in flight at the same time, over all providers (``0`` = unlimited).
        rpm_per_key: Requests per minute per ``(provider, key)`` (``0`` = unlimited).
        tpm_per_key: Estimated tokens per minute per ``(provider, key)`` (``0`` = unlimited).
        min_interval_s: Minimum seconds between two call starts on the same key (``0`` = no pacing).
        overrides: ``provider -> (rpm, tpm)`` replacing the defaults for that provider.
    """

    max_concurrency: int = DEFAULT_MAX_CONCURRENCY
    rpm_per_key: int = DEFAULT_RPM_PER_KEY
    tpm_per_key: int = DEFAULT_TPM_PER_KEY
    min_interval_s: float = DEFAULT_MIN_INTERVAL_S
    overrides: dict[str, tuple[int, int]] = field(default_factory=dict)

    def limits_for(self, provider: str) -> tuple[int, int]:
        """``(rpm, tpm)`` that apply to ``provider``."""
        return self.overrides.get(provider, (self.rpm_per_key, self.tpm_per_key))

    @classmethod
    def from_settings(cls, settings: Any) -> RateLimitConfig | None:
        """Read the ``llm_*`` rate attributes from any settings object.

        Returns:
            The config, or ``None`` when ``settings`` defines none of the rate attributes (ad-hoc settings objects
            of tests keep the old, unlimited behaviour; the real ``Settings`` always defines them).
        """
        if not any(getattr(settings, name, None) is not None for name in SETTINGS_FIELDS):
            return None

        def get(name: str, default: Any) -> Any:
            value = getattr(settings, name, None)
            return default if value is None else value

        return cls(
            max_concurrency=max(0, int(get("llm_max_concurrency", DEFAULT_MAX_CONCURRENCY))),
            rpm_per_key=max(0, int(get("llm_rpm_per_key", DEFAULT_RPM_PER_KEY))),
            tpm_per_key=max(0, int(get("llm_tpm_per_key", DEFAULT_TPM_PER_KEY))),
            min_interval_s=max(0.0, float(get("llm_min_interval_s", DEFAULT_MIN_INTERVAL_S))),
            overrides=parse_rate_overrides(get("llm_rate_overrides", "")),
        )


def estimate_tokens(system: str, user: str, max_tokens: int) -> int:
    """Pessimistic token estimate of one call: ``(len(system) + len(user)) // 4 + max_tokens``."""
    return (len(system or "") + len(user or "")) // 4 + max(0, int(max_tokens))


def _fingerprint(key: Any) -> str:
    """Stable identifier of a key for bucket lookup (``ApiKey.fingerprint``, a string, or a hash of ``str``)."""
    if isinstance(key, str):
        return key
    fingerprint = getattr(key, "fingerprint", None)
    if isinstance(fingerprint, str) and fingerprint:
        return fingerprint
    return hashlib.sha256(str(key).encode("utf-8")).hexdigest()[:12]


# --------------------------------------------------------------------------------------------------------------
# Limiter
# --------------------------------------------------------------------------------------------------------------


class _KeyBuckets:
    """The buckets of one ``(provider, key)``; ``None`` for a limit that is switched off."""

    def __init__(self, rpm: int, tpm: int, min_interval_s: float, clock: Any, sleep: Any) -> None:
        self.requests = TokenBucket(rpm, rpm / 60.0, clock, sleep) if rpm > 0 else None
        self.tokens = TokenBucket(tpm, tpm / 60.0, clock, sleep) if tpm > 0 else None
        self.pacing = TokenBucket(1, 1.0 / min_interval_s, clock, sleep) if min_interval_s > 0 else None

    def all(self) -> list[TokenBucket]:
        return [bucket for bucket in (self.pacing, self.requests, self.tokens) if bucket is not None]


class RateLimiter:
    """Concurrency cap plus per-key request / token / pacing buckets. See the module docstring.

    Args:
        config: The limits (default :class:`RateLimitConfig`).
        clock: Monotonic seconds source (the same clock deadlines are expressed in).
        sleep: Async sleep.
    """

    def __init__(
        self,
        config: RateLimitConfig | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        self.config = config or RateLimitConfig()
        self._clock = clock
        self._sleep = sleep
        self._buckets: dict[tuple[str, str], _KeyBuckets] = {}
        # A semaphore is tied to the event loop it first waits in; keep one per loop so that a limiter outlives
        # an ``asyncio.run`` (graph runs, tests).
        self._semaphores: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = (
            weakref.WeakKeyDictionary()
        )
        self.in_flight = 0
        self.peak_in_flight = 0

    @classmethod
    def from_settings(
        cls,
        settings: Any,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> RateLimiter | None:
        """Limiter for ``settings`` or ``None`` when it defines no rate attributes (see ``RateLimitConfig``)."""
        config = RateLimitConfig.from_settings(settings)
        return None if config is None else cls(config, clock, sleep)

    # ------------------------------------------------------------------------------------------------ buckets

    def _for(self, provider: str, key: Any) -> _KeyBuckets:
        ident = (provider, _fingerprint(key))
        buckets = self._buckets.get(ident)
        if buckets is None:
            rpm, tpm = self.config.limits_for(provider)
            buckets = _KeyBuckets(rpm, tpm, self.config.min_interval_s, self._clock, self._sleep)
            self._buckets[ident] = buckets
        return buckets

    def _semaphore(self) -> asyncio.Semaphore | None:
        if self.config.max_concurrency <= 0:
            return None
        loop = asyncio.get_running_loop()
        semaphore = self._semaphores.get(loop)
        if semaphore is None:
            semaphore = asyncio.Semaphore(self.config.max_concurrency)
            self._semaphores[loop] = semaphore
        return semaphore

    # ------------------------------------------------------------------------------------------------ public

    @asynccontextmanager
    async def slot(
        self, provider: str, key: Any, est_tokens: int = 0, deadline: float | None = None
    ) -> AsyncIterator[float]:
        """Wait for permission to start one provider call; hold a concurrency slot while the block runs.

        Args:
            provider: Provider name (selects the limits).
            key: The :class:`~builder.llm.keys.ApiKey` (or any identifier string) the call will use.
            est_tokens: Estimated tokens of the call (:func:`estimate_tokens`); charged to the token bucket.
            deadline: Monotonic time the caller must not wait past.

        Yields:
            Seconds spent waiting (rate buckets plus the concurrency queue).

        Raises:
            RateLimitTimeout: The wait would pass ``deadline``; no budget stays consumed.
        """
        started = self._clock()
        buckets = self._for(provider, key)
        taken: list[tuple[TokenBucket, float]] = []
        try:
            # Reserve in every bucket at once and sleep for the longest wait (not for the sum of the waits).
            wait = 0.0
            for bucket, amount in (
                (buckets.pacing, 1.0),
                (buckets.requests, 1.0),
                (buckets.tokens, float(max(est_tokens, 0))),
            ):
                if bucket is None:
                    continue
                wait = max(wait, bucket.reserve(amount, deadline))
                taken.append((bucket, min(amount, bucket.capacity)))
            if wait > 0:
                await self._sleep(wait)
            semaphore = self._semaphore()
            if semaphore is not None:
                await self._enter(semaphore, deadline)
        except BaseException:  # refused, cancelled or timed out: give every reservation back
            for bucket, amount in taken:
                bucket.refund(amount)
            raise
        self.in_flight += 1
        self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
        try:
            yield max(0.0, self._clock() - started)
        finally:
            self.in_flight -= 1
            if semaphore is not None:
                semaphore.release()

    async def _enter(self, semaphore: asyncio.Semaphore, deadline: float | None) -> None:
        if deadline is None or not semaphore.locked():
            await semaphore.acquire()
            return
        timeout = deadline - self._clock()
        if timeout <= 0:
            raise RateLimitTimeout("no concurrency slot before the deadline")
        try:
            await asyncio.wait_for(semaphore.acquire(), timeout)
        except TimeoutError as exc:
            raise RateLimitTimeout("no concurrency slot before the deadline") from exc

    def penalize(self, provider: str, key: Any, seconds: float) -> None:
        """Block the key's buckets for ``seconds`` (after a rate-limit error; ``Retry-After`` or the cooldown)."""
        if seconds <= 0 or not math.isfinite(seconds):
            return
        for bucket in self._for(provider, key).all():
            bucket.penalize(seconds)

    def adjust(self, provider: str, key: Any, estimated: int, actual: int | None) -> None:
        """Replace the estimate of a finished call by the tokens the provider reported (no-op when unknown)."""
        if actual is None:
            return
        bucket = self._for(provider, key).tokens
        if bucket is None:
            return
        estimated = min(max(estimated, 0), int(bucket.capacity))
        if actual < estimated:
            bucket.refund(estimated - actual)
        elif actual > estimated:
            bucket.consume(actual - estimated)

    def available(self, provider: str, key: Any) -> dict[str, float | None]:
        """Remaining budget of a key (diagnostics): ``requests``, ``tokens`` and ``pacing`` (``None`` = off)."""
        buckets = self._for(provider, key)
        return {
            "requests": buckets.requests.available() if buckets.requests else None,
            "tokens": buckets.tokens.available() if buckets.tokens else None,
            "pacing": buckets.pacing.available() if buckets.pacing else None,
        }
