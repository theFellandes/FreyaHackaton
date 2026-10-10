"""Tests for builder.llm.ratelimit: the token bucket (fake clock), the limiter (concurrency, buckets, penalties).

Time is simulated by ``FakeClock``: ``sleep`` advances the clock instantly, so a test that "waits 30 s" runs in
microseconds and can assert the exact wait. The few tests that need real concurrency use tiny real sleeps.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from builder.llm.ratelimit import (
    RateLimitConfig,
    RateLimiter,
    RateLimitTimeout,
    TokenBucket,
    estimate_tokens,
    parse_rate_overrides,
)


class FakeClock:
    """Monotonic clock whose async sleep just moves time forward."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def bucket(clock: FakeClock, capacity: float, refill: float) -> TokenBucket:
    return TokenBucket(capacity, refill, clock=clock, sleep=clock.sleep)


def limiter(clock: FakeClock, **config: object) -> RateLimiter:
    defaults: dict[str, object] = {
        "max_concurrency": 0,
        "rpm_per_key": 0,
        "tpm_per_key": 0,
        "min_interval_s": 0.0,
    }
    defaults.update(config)
    return RateLimiter(RateLimitConfig(**defaults), clock=clock, sleep=clock.sleep)  # type: ignore[arg-type]


async def wait_of(lim: RateLimiter, provider: str = "openai", key: str = "k1", est: int = 0) -> float:
    async with lim.slot(provider, key, est) as waited:
        return waited


# ----------------------------------------------------------------------------------------------- token bucket


async def test_bucket_starts_full_and_then_waits_for_the_refill() -> None:
    clock = FakeClock()
    b = bucket(clock, capacity=3, refill=1.0)  # one token per second

    assert [await b.acquire() for _ in range(3)] == [0.0, 0.0, 0.0]
    assert await b.acquire() == 1.0  # the fourth token is one second away
    assert clock.now == 1001.0
    assert await b.acquire() == 1.0


async def test_bucket_refill_is_capped_at_the_capacity() -> None:
    clock = FakeClock()
    b = bucket(clock, capacity=3, refill=1.0)
    for _ in range(3):
        await b.acquire()
    assert b.available() == 0.0

    clock.now += 1.5
    assert b.available() == 1.5
    clock.now += 100
    assert b.available() == 3.0


async def test_bucket_serves_a_request_larger_than_its_capacity_when_full() -> None:
    clock = FakeClock()
    b = bucket(clock, capacity=2, refill=1.0)

    assert await b.acquire(5) == 0.0  # clamped to the capacity, served from the full bucket
    assert await b.acquire(1) == 1.0  # and the bucket is empty afterwards


async def test_penalize_blocks_the_bucket_for_exactly_that_long() -> None:
    clock = FakeClock()
    b = bucket(clock, capacity=5, refill=1.0)
    b.penalize(10)

    assert b.available() == 0.0
    assert await b.acquire() == 10.0
    assert clock.now == 1010.0
    assert await b.acquire() == 0.0  # the tokens that were there before the penalty are still there


async def test_a_shorter_penalty_does_not_shorten_a_longer_one() -> None:
    clock = FakeClock()
    b = bucket(clock, capacity=5, refill=1.0)
    b.penalize(10)
    b.penalize(2)

    assert await b.acquire() == 10.0


async def test_penalize_ignores_non_positive_seconds() -> None:
    clock = FakeClock()
    b = bucket(clock, capacity=1, refill=1.0)
    b.penalize(0)
    b.penalize(-5)

    assert await b.acquire() == 0.0


async def test_acquire_refuses_to_wait_past_the_deadline_and_takes_nothing() -> None:
    clock = FakeClock()
    b = bucket(clock, capacity=1, refill=1.0)
    await b.acquire()

    with pytest.raises(RateLimitTimeout):
        await b.acquire(1, deadline=clock.now + 0.5)

    assert clock.sleeps == []  # it did not even start sleeping
    assert await b.acquire() == 1.0  # not 2.0: the refused request reserved nothing


async def test_acquire_may_wait_when_the_deadline_is_far_enough() -> None:
    clock = FakeClock()
    b = bucket(clock, capacity=1, refill=1.0)
    await b.acquire()

    assert await b.acquire(1, deadline=clock.now + 1.0) == 1.0


async def test_a_bucket_that_never_refills_times_out_when_empty() -> None:
    clock = FakeClock()
    b = bucket(clock, capacity=1, refill=0.0)
    assert await b.acquire() == 0.0

    with pytest.raises(RateLimitTimeout):
        await b.acquire()


async def test_a_cancelled_wait_gives_the_reservation_back() -> None:
    clock = FakeClock()

    sleeps_started = 0

    async def sleep(seconds: float) -> None:
        nonlocal sleeps_started
        sleeps_started += 1
        if sleeps_started == 1:  # only the first wait is cancelled; later ones behave normally
            raise asyncio.CancelledError
        await clock.sleep(seconds)

    b = TokenBucket(1, 1.0, clock=clock, sleep=sleep)
    await b.acquire()  # takes the only token
    with pytest.raises(asyncio.CancelledError):
        await b.acquire()  # reserves a token, is cancelled while waiting for it

    assert await b.acquire() == 1.0  # the cancelled reservation was refunded, so only one second is owed


async def test_concurrent_callers_are_served_in_order_at_the_refill_rate() -> None:
    b = TokenBucket(1, 100.0)  # real clock: one token every 10 ms
    done: list[int] = []

    async def worker(index: int) -> None:
        await b.acquire()
        done.append(index)

    started = time.perf_counter()
    await asyncio.gather(*(worker(i) for i in range(4)))

    assert done == [0, 1, 2, 3]
    # Three tokens had to be waited for (30 ms in total); the bound is loose because Windows timers are coarse.
    assert time.perf_counter() - started >= 0.012


def test_bucket_rejects_a_non_positive_capacity() -> None:
    with pytest.raises(ValueError):
        TokenBucket(0, 1.0)
    with pytest.raises(ValueError):
        TokenBucket(1, -1.0)


# ----------------------------------------------------------------------------------------------- configuration


def test_estimate_tokens_is_a_quarter_of_the_characters_plus_the_output_cap() -> None:
    assert estimate_tokens("a" * 40, "b" * 80, 1000) == 1030
    assert estimate_tokens("", "", 16000) == 16000
    assert estimate_tokens("abc", "d", 0) == 1


def test_parse_rate_overrides() -> None:
    parsed = parse_rate_overrides("openai=60:200000, Anthropic=30:100000;claude-code = 5:0")

    assert parsed == {"openai": (60, 200000), "anthropic": (30, 100000), "claude_code": (5, 0)}
    assert parse_rate_overrides("") == {}
    assert parse_rate_overrides(None) == {}


def test_malformed_overrides_are_skipped_or_rejected() -> None:
    assert parse_rate_overrides("openai=60,anthropic=1:2") == {"anthropic": (1, 2)}
    with pytest.raises(ValueError, match="openai=60"):
        parse_rate_overrides("openai=60", strict=True)


def test_config_from_settings_reads_the_llm_attributes() -> None:
    settings = SimpleNamespace(
        llm_max_concurrency=4,
        llm_rpm_per_key=10,
        llm_tpm_per_key=20000,
        llm_min_interval_s=1.5,
        llm_rate_overrides="openai=60:200000",
    )

    config = RateLimitConfig.from_settings(settings)

    assert config == RateLimitConfig(4, 10, 20000, 1.5, {"openai": (60, 200000)})
    assert config.limits_for("openai") == (60, 200000)
    assert config.limits_for("anthropic") == (10, 20000)


def test_config_defaults_match_the_contract() -> None:
    config = RateLimitConfig()

    assert (config.max_concurrency, config.rpm_per_key, config.tpm_per_key, config.min_interval_s) == (
        2,
        30,
        150_000,
        0.5,
    )


def test_settings_without_any_rate_attribute_mean_no_limiter() -> None:
    assert RateLimitConfig.from_settings(SimpleNamespace(llm_providers="anthropic")) is None
    assert RateLimiter.from_settings(SimpleNamespace()) is None


def test_the_real_settings_class_carries_the_contract_defaults(settings: object) -> None:
    config = RateLimitConfig.from_settings(settings)

    assert config == RateLimitConfig(2, 30, 150_000, 0.5, {})


# ----------------------------------------------------------------------------------------------- limiter


async def test_request_bucket_limits_calls_per_minute() -> None:
    clock = FakeClock()
    lim = limiter(clock, rpm_per_key=2)  # capacity 2, one new request every 30 s

    assert [await wait_of(lim) for _ in range(3)] == [0.0, 0.0, 30.0]


async def test_token_bucket_limits_estimated_tokens_per_minute() -> None:
    clock = FakeClock()
    lim = limiter(clock, tpm_per_key=6000)  # 100 tokens per second

    assert await wait_of(lim, est=6000) == 0.0
    assert await wait_of(lim, est=1000) == 10.0


async def test_pacing_spaces_two_call_starts_on_the_same_key() -> None:
    clock = FakeClock()
    lim = limiter(clock, min_interval_s=0.5)

    assert await wait_of(lim) == 0.0
    assert await wait_of(lim) == 0.5
    assert await wait_of(lim) == 0.5


async def test_keys_and_providers_have_independent_budgets() -> None:
    clock = FakeClock()
    lim = limiter(clock, rpm_per_key=1)

    assert await wait_of(lim, "openai", "k1") == 0.0
    assert await wait_of(lim, "openai", "k2") == 0.0  # another key of the same provider
    assert await wait_of(lim, "anthropic", "k1") == 0.0  # the same key text at another provider
    assert await wait_of(lim, "openai", "k1") == 60.0  # but the first key is spent


async def test_a_zero_limit_switches_that_limit_off() -> None:
    clock = FakeClock()
    lim = limiter(clock, rpm_per_key=0, tpm_per_key=0, min_interval_s=0.0)

    assert [await wait_of(lim, est=10_000_000) for _ in range(50)] == [0.0] * 50
    assert clock.sleeps == []


async def test_provider_overrides_replace_the_defaults_for_that_provider_only() -> None:
    clock = FakeClock()
    lim = limiter(clock, rpm_per_key=100, overrides={"openai": (1, 0)})

    assert await wait_of(lim, "openai") == 0.0
    assert await wait_of(lim, "openai") == 60.0
    assert [await wait_of(lim, "anthropic") for _ in range(3)] == [0.0, 0.0, 0.0]


async def test_penalize_delays_only_that_key() -> None:
    clock = FakeClock()
    lim = limiter(clock, rpm_per_key=60)
    lim.penalize("openai", "k1", 12)

    assert lim.available("openai", "k1")["requests"] == 0.0
    assert await wait_of(lim, "openai", "k2") == 0.0
    assert await wait_of(lim, "anthropic", "k1") == 0.0
    assert await wait_of(lim, "openai", "k1") == 12.0


async def test_penalize_with_non_positive_or_infinite_seconds_is_ignored() -> None:
    clock = FakeClock()
    lim = limiter(clock, rpm_per_key=60)
    lim.penalize("openai", "k1", 0)
    lim.penalize("openai", "k1", float("inf"))

    assert await wait_of(lim) == 0.0


async def test_slot_refuses_to_wait_past_the_deadline_and_keeps_the_budget() -> None:
    clock = FakeClock()
    lim = limiter(clock, rpm_per_key=1)
    await wait_of(lim)

    with pytest.raises(RateLimitTimeout):
        async with lim.slot("openai", "k1", 0, deadline=clock.now + 10):
            raise AssertionError("must not run")

    assert clock.sleeps == []
    assert lim.available("openai", "k1")["requests"] == 0.0
    assert await wait_of(lim) == 60.0  # not 120: the refused call did not eat a request


async def test_adjust_replaces_the_estimate_by_the_reported_usage() -> None:
    clock = FakeClock()
    lim = limiter(clock, tpm_per_key=6000)
    async with lim.slot("openai", "k1", 6000):
        pass
    assert lim.available("openai", "k1")["tokens"] == 0.0

    lim.adjust("openai", "k1", 6000, 1000)  # the call only used 1000 tokens: 5000 come back
    assert lim.available("openai", "k1")["tokens"] == 5000.0

    lim.adjust("openai", "k1", 1000, 3000)  # used more than estimated: the difference is charged
    assert lim.available("openai", "k1")["tokens"] == 3000.0

    lim.adjust("openai", "k1", 1000, None)  # unknown usage changes nothing
    assert lim.available("openai", "k1")["tokens"] == 3000.0


async def test_concurrency_never_exceeds_the_cap() -> None:
    lim = RateLimiter(RateLimitConfig(max_concurrency=2, rpm_per_key=0, tpm_per_key=0, min_interval_s=0.0))
    running = 0
    peak = 0

    async def job() -> None:
        nonlocal running, peak
        async with lim.slot("openai", "k1"):
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.01)
            running -= 1

    await asyncio.gather(*(job() for _ in range(6)))

    assert peak == 2
    assert lim.peak_in_flight == 2
    assert lim.in_flight == 0


async def test_a_failing_block_releases_its_concurrency_slot() -> None:
    lim = RateLimiter(RateLimitConfig(max_concurrency=1, rpm_per_key=0, tpm_per_key=0, min_interval_s=0.0))

    with pytest.raises(RuntimeError):
        async with lim.slot("openai", "k1"):
            raise RuntimeError("provider blew up")

    async with asyncio.timeout(1):
        async with lim.slot("openai", "k1"):
            pass
    assert lim.in_flight == 0


async def test_waiting_for_a_concurrency_slot_past_the_deadline_times_out_and_refunds() -> None:
    lim = RateLimiter(RateLimitConfig(max_concurrency=1, rpm_per_key=2, tpm_per_key=0, min_interval_s=0.0))
    release = asyncio.Event()

    async def holder() -> None:
        async with lim.slot("openai", "k1"):
            await release.wait()

    task = asyncio.create_task(holder())
    await asyncio.sleep(0.01)  # the holder owns the only slot (and used one of two requests)

    started = time.monotonic()
    with pytest.raises(RateLimitTimeout):
        async with lim.slot("openai", "k1", 0, deadline=time.monotonic() + 0.05):
            raise AssertionError("must not run")
    assert time.monotonic() - started < 1.0

    release.set()
    await task
    # the timed-out caller gave its request back: one of the two requests is still available
    assert lim.available("openai", "k1")["requests"] == pytest.approx(1.0, abs=0.05)


def test_one_limiter_survives_several_event_loops() -> None:
    lim = RateLimiter(RateLimitConfig(max_concurrency=1, rpm_per_key=0, tpm_per_key=0, min_interval_s=0.0))

    async def contended() -> None:
        async def job() -> None:
            async with lim.slot("openai", "k1"):
                await asyncio.sleep(0.005)

        await asyncio.gather(job(), job())

    asyncio.run(contended())
    asyncio.run(contended())  # a semaphore bound to the first loop would raise here
