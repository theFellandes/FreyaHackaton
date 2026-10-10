"""Tests for builder.budget: elapsed/remaining arithmetic, reserve handling, deadline and stage estimates."""

from __future__ import annotations

import math

import pytest

from builder.budget import DEFAULT_RESERVE_S, STAGE_ESTIMATES_S, Budget, estimate
from tests.conftest import FakeClock


def test_stage_estimates_match_the_contract() -> None:
    assert dict(STAGE_ESTIMATES_S) == {
        "understand": 240,
        "extract": 300,
        "prose": 240,
        "compile": 20,
        "remote_validate": 30,
        "deploy": 90,
        "smoke": 90,
        "walkthrough": 180,
        "practice": 780,
        "agentic_build": 1100,
        "repair": 360,
    }
    with pytest.raises(TypeError):
        STAGE_ESTIMATES_S["practice"] = 1  # type: ignore[index]  # read-only


def test_elapsed_remaining_and_expiry_follow_the_clock(fake_clock: FakeClock) -> None:
    budget = Budget(600, clock=fake_clock)
    assert budget.elapsed() == 0
    assert budget.remaining() == 600
    assert not budget.expired()

    fake_clock.advance(250)
    assert budget.elapsed() == 250
    assert budget.remaining() == 350

    fake_clock.advance(350)
    assert budget.remaining() == 0
    assert budget.expired()

    fake_clock.advance(100)
    assert budget.elapsed() == 700
    assert budget.remaining() == 0  # never negative


def test_allows_keeps_the_reserve_free(fake_clock: FakeClock) -> None:
    budget = Budget(1000, clock=fake_clock)
    assert DEFAULT_RESERVE_S == 120
    assert budget.allows(880)  # 1000 - 120 = 880 exactly fits
    assert not budget.allows(880.01)
    assert budget.allows(1000, reserve_s=0)
    fake_clock.advance(500)
    assert budget.allows(380)
    assert not budget.allows(381)
    assert budget.allows(400, reserve_s=100)


def test_allows_rejects_negative_inputs(fake_clock: FakeClock) -> None:
    budget = Budget(100, clock=fake_clock)
    with pytest.raises(ValueError):
        budget.allows(-1)
    with pytest.raises(ValueError):
        budget.allows(1, reserve_s=-5)


def test_allows_stage_uses_estimates(fake_clock: FakeClock) -> None:
    budget = Budget(1680, clock=fake_clock)
    assert budget.allows_stage("practice")  # 780 + 120 <= 1680
    fake_clock.advance(800)
    assert not budget.allows_stage("practice")  # 880 left - 120 reserve < 780
    assert budget.allows_stage("smoke")
    with pytest.raises(KeyError, match="known stages"):
        budget.allows_stage("teleport")
    assert estimate("deploy") == 90


def test_deadline_is_on_the_budget_clock(fake_clock: FakeClock) -> None:
    budget = Budget(1680, clock=fake_clock)
    assert budget.deadline() == 1000.0 + 1680
    fake_clock.advance(10)
    assert budget.deadline() == 1000.0 + 1680  # fixed instant
    assert budget.deadline() - fake_clock() == pytest.approx(budget.remaining())


@pytest.mark.parametrize("total", [0, -1, float("nan")])
def test_invalid_totals_are_rejected(total: float) -> None:
    with pytest.raises(ValueError):
        Budget(total)


def test_infinite_budget_never_expires(fake_clock: FakeClock) -> None:
    budget = Budget(math.inf, clock=fake_clock)
    fake_clock.advance(10**9)
    assert not budget.expired()
    assert budget.allows(10**8)


def test_snapshot_and_repr(fake_clock: FakeClock) -> None:
    budget = Budget(100, clock=fake_clock)
    fake_clock.advance(12.3456)
    assert budget.snapshot() == {"total_s": 100.0, "elapsed_s": 12.346, "remaining_s": 87.654}
    assert repr(budget) == "Budget(total_s=100, remaining_s=87.7)"


def test_default_clock_is_monotonic_and_real() -> None:
    budget = Budget(60)
    assert 0 <= budget.elapsed() < 5
    assert budget.remaining() <= 60
