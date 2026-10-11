"""Tests for builder.freya.ratelimit (polite pacing, simulation lock) and how FreyaClient uses them.

No network and no real waiting beyond a few milliseconds: the pacing tests use a fake clock and sleep, the kit is a
stand-in object (or the real ``freya.py`` with its ``api`` replaced), and nothing talks to Freya.
"""

from __future__ import annotations

import importlib.util
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import builder.freya.client as client_mod
import builder.freya.ratelimit as rl
from builder.freya.background import CancelToken, PlatformCancelled, bound_token
from builder.freya.client import FreyaClient
from builder.freya.ratelimit import (
    PoliteLimiter,
    SimulationBusy,
    shared_limiter,
    simulation_slot,
    throttle_kit,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


class FakeClock:
    def __init__(self) -> None:
        self.now = 500.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def polite(interval: float = 0.25) -> tuple[PoliteLimiter, FakeClock]:
    clock = FakeClock()
    return PoliteLimiter(interval, clock=clock, sleep=clock.sleep), clock


class SpyLimiter(PoliteLimiter):
    """Counts waits without sleeping."""

    def __init__(self) -> None:
        super().__init__(0.0)
        self.waits = 0

    def wait(self) -> float:
        self.waits += 1
        return 0.0


# ----------------------------------------------------------------------------------------------- PoliteLimiter


def test_consecutive_calls_start_at_least_the_interval_apart() -> None:
    limiter, clock = polite(0.25)

    assert [limiter.wait() for _ in range(3)] == [0.0, 0.25, 0.25]
    assert clock.sleeps == [0.25, 0.25]
    assert limiter.calls == 3
    assert limiter.total_wait_s == 0.5


def test_a_slow_caller_is_not_delayed() -> None:
    limiter, clock = polite(0.25)
    limiter.wait()
    clock.now += 1.0  # the previous call took a second

    assert limiter.wait() == 0.0
    assert clock.sleeps == []


def test_a_partly_elapsed_interval_only_waits_for_the_rest() -> None:
    limiter, clock = polite(0.25)
    limiter.wait()
    clock.now += 0.1

    assert limiter.wait() == pytest.approx(0.15)


def test_zero_interval_never_sleeps() -> None:
    limiter, clock = polite(0.0)

    assert [limiter.wait() for _ in range(5)] == [0.0] * 5
    assert clock.sleeps == []


def test_call_waits_then_runs_the_function() -> None:
    limiter, clock = polite(0.25)

    assert limiter.call(lambda a, b=0: a + b, 1, b=2) == 3
    assert limiter.call(lambda: "x") == "x"
    assert clock.sleeps == [0.25]


def test_threads_are_spaced_out_too() -> None:
    limiter = PoliteLimiter(0.02)
    stamps: list[float] = []
    lock = threading.Lock()

    def worker() -> None:
        limiter.wait()
        with lock:
            stamps.append(time.perf_counter())

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    gaps = [b - a for a, b in zip(sorted(stamps), sorted(stamps)[1:], strict=False)]
    assert len(stamps) == 5
    assert min(gaps) >= 0.012  # 20 ms apart, minus Windows timer slack


def test_a_delayed_wakeup_does_not_admit_queued_callers_together() -> None:
    """Advance past both old reservation slots while one caller sleeps; the next caller must still wait."""
    clock = FakeClock()
    sleeping = threading.Event()
    release_sleep = threading.Event()
    queued = threading.Event()
    first_returned = threading.Event()
    stamps: list[float] = []
    errors: list[BaseException] = []

    def sleep(seconds: float) -> None:
        clock.sleeps.append(seconds)
        if threading.current_thread().name == "delayed-first":
            sleeping.set()
            assert release_sleep.wait(2)
        else:
            # Keep the observation of the first admission ahead of the fake clock's next advance.
            assert first_returned.wait(2)
            clock.now += seconds

    limiter = PoliteLimiter(0.25, clock=clock, sleep=sleep)
    limiter.wait()  # first admission at 500.0

    def worker(first: bool) -> None:
        try:
            if not first:
                queued.set()
            limiter.wait()
            stamps.append(clock())
        except BaseException as exc:
            errors.append(exc)
        finally:
            if first:
                first_returned.set()

    first = threading.Thread(target=worker, args=(True,), name="delayed-first")
    second = threading.Thread(target=worker, args=(False,), name="queued-second")
    first.start()
    try:
        assert sleeping.wait(2)
        clock.now += 1.0  # the sleeper wakes much later than the reserved 500.25 start
        second.start()
        assert queued.wait(2)
    finally:
        release_sleep.set()
        first.join(2)
        if second.ident is not None:
            second.join(2)
    assert not first.is_alive() and not second.is_alive()
    assert errors == []
    assert sorted(stamps) == [501.0, 501.25]
    assert clock.sleeps == [0.25, 0.25]
    assert limiter.calls == 3


def test_an_early_wakeup_waits_out_the_remaining_interval() -> None:
    clock = FakeClock()

    def sleep(seconds: float) -> None:
        clock.sleeps.append(seconds)
        clock.now += seconds / 2 if len(clock.sleeps) == 1 else seconds

    limiter = PoliteLimiter(0.25, clock=clock, sleep=sleep)
    limiter.wait()
    assert limiter.wait() == 0.25
    assert clock.sleeps == [0.25, 0.125]


def test_pacing_does_not_hold_the_lock_for_the_whole_request() -> None:
    limiter, clock = polite(0.25)
    inside_first = threading.Event()
    second_ran = threading.Event()
    release_first = threading.Event()

    def long_request() -> None:
        inside_first.set()
        assert release_first.wait(2)

    first = threading.Thread(target=lambda: limiter.call(long_request))
    first.start()
    try:
        assert inside_first.wait(2)
        limiter.call(second_ran.set)
        assert second_ran.is_set() and first.is_alive()
        assert clock.sleeps == [0.25]
    finally:
        release_first.set()
        first.join(2)
    assert not first.is_alive()


def test_shared_limiter_is_one_object_and_takes_the_latest_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rl, "_shared", None)

    first = shared_limiter(0.4)
    assert first.min_interval_s == 0.4
    assert shared_limiter() is first
    assert shared_limiter(0.1) is first
    assert first.min_interval_s == 0.1


def test_the_real_settings_class_defaults_to_a_quarter_second(settings: Any) -> None:
    assert settings.freya_min_interval_s == 0.25


# ----------------------------------------------------------------------------------------------- throttle_kit


def test_throttled_api_waits_before_every_call_and_passes_everything_through() -> None:
    limiter = SpyLimiter()
    seen: list[tuple[Any, ...]] = []

    def api(method: str, path: str, body: Any = None, **params: Any) -> str:
        seen.append((method, path, body, params))
        return "result"

    kit = SimpleNamespace(api=api)

    assert throttle_kit(kit, limiter) is True
    assert kit.api("POST", "/x", {"a": 1}, workspaceId="w") == "result"
    assert kit.api("GET", "/y") == "result"
    assert limiter.waits == 2
    assert seen == [("POST", "/x", {"a": 1}, {"workspaceId": "w"}), ("GET", "/y", None, {})]


def test_throttling_twice_does_not_stack_waits() -> None:
    limiter = SpyLimiter()
    kit = SimpleNamespace(api=lambda *a, **k: None)

    assert throttle_kit(kit, limiter) is True
    assert throttle_kit(kit, limiter) is False
    kit.api("GET", "/x")

    assert limiter.waits == 1


def test_cancellation_during_pacing_still_prevents_the_http_request() -> None:
    clock = FakeClock()
    token = CancelToken(clock=clock)
    sent: list[str] = []

    def sleep(seconds: float) -> None:
        clock.sleep(seconds)
        token.cancel("cancelled while pacing")

    limiter = PoliteLimiter(0.25, clock=clock, sleep=sleep)
    limiter.wait()
    kit = SimpleNamespace(api=lambda *a, **k: sent.append("request"))
    throttle_kit(kit, limiter)
    with bound_token(token), pytest.raises(PlatformCancelled, match="while pacing"):
        kit.api("GET", "/synthetic")
    assert sent == []
    assert clock.sleeps == [0.25]
    limiter.wait()  # cancellation did not leave the pacing lock held


def test_a_kit_without_an_api_function_is_left_alone() -> None:
    assert throttle_kit(SimpleNamespace(), SpyLimiter()) is False
    assert throttle_kit(SimpleNamespace(api="not callable"), SpyLimiter()) is False


def test_every_http_call_inside_the_real_kit_is_paced() -> None:
    """The real freya.py looks ``api`` up in its own namespace, so patching it paces its inner calls too."""
    spec = importlib.util.spec_from_file_location("freya_kit_pacing_test", REPO_ROOT / "freya.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    requests: list[tuple[str, str]] = []

    def fake_api(method: str, path: str, body: Any = None, **params: Any) -> Any:
        requests.append((method, path))
        return {"workspaces": [{"id": "ws_1"}]} if path == "/api/v2/workspaces" else {"valid": True}

    module.api = fake_api
    module._ws = None
    limiter = SpyLimiter()
    assert throttle_kit(module, limiter) is True

    module.workspace_id()  # one request inside
    module.validate_workflow("name", {"initial_node": "a", "nodes": {}})  # another one

    assert [path for _, path in requests] == ["/api/v2/workspaces", "/api/v2/workflow"]
    assert limiter.waits == 2


# ----------------------------------------------------------------------------------------------- simulation lock


def test_a_second_simulation_waits_for_the_first() -> None:
    order: list[str] = []
    first_inside = threading.Event()
    release_first = threading.Event()

    def first() -> None:
        with simulation_slot():
            order.append("first in")
            first_inside.set()
            release_first.wait(2)
            order.append("first out")

    def second() -> None:
        first_inside.wait(2)
        with simulation_slot():
            order.append("second in")

    threads = [threading.Thread(target=first), threading.Thread(target=second)]
    for thread in threads:
        thread.start()
    first_inside.wait(2)
    time.sleep(0.05)  # give the second thread time to (wrongly) get in
    assert order == ["first in"]
    release_first.set()
    for thread in threads:
        thread.join(2)

    assert order == ["first in", "first out", "second in"]


def test_a_waiting_simulation_gives_up_after_its_timeout() -> None:
    inside = threading.Event()
    release = threading.Event()

    def holder() -> None:
        with simulation_slot():
            inside.set()
            release.wait(2)

    thread = threading.Thread(target=holder)
    thread.start()
    inside.wait(2)
    try:
        with pytest.raises(SimulationBusy, match="still running"), simulation_slot(timeout_s=0.05):
            raise AssertionError("must not run")
    finally:
        release.set()
        thread.join(2)


def test_the_slot_is_released_after_an_error_and_cannot_be_nested() -> None:
    with pytest.raises(RuntimeError, match="boom"), simulation_slot():
        raise RuntimeError("boom")

    # The slot is free again; taking it twice from one thread is refused instead of deadlocking.
    with simulation_slot(), pytest.raises(SimulationBusy, match="already"), simulation_slot():
        raise AssertionError("nesting would deadlock")


def test_the_client_exposes_the_same_slot() -> None:
    client = FreyaClient(None, SimpleNamespace(), limiter=False)

    # The client's slot is the process-level one: taking it again (here from the same thread) is refused.
    with client.simulation_slot(), pytest.raises(SimulationBusy), simulation_slot(timeout_s=0.01):
        raise AssertionError("the slot must already be taken")


# ----------------------------------------------------------------------------------------------- FreyaClient use


def test_an_injected_kit_is_paced_once_per_attempt_when_a_limiter_is_passed() -> None:
    limiter = SpyLimiter()
    attempts = [OSError("down"), OSError("down"), {"id": "call_1"}]

    def get_call(call_id: str) -> Any:
        outcome = attempts.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    client = FreyaClient(
        None, SimpleNamespace(get_call=get_call), limiter=limiter, read_retries=2, retry_backoff_s=0.0
    )

    assert client.get_call("call_1") == {"id": "call_1"}
    assert limiter.waits == 3  # two failed attempts and the successful one


def test_an_injected_kit_is_not_paced_by_default() -> None:
    kit = SimpleNamespace(get_call=lambda call_id: {"id": call_id}, api=lambda *a, **k: None)
    original_api = kit.api

    client = FreyaClient(None, kit)
    client.get_call("c1")

    assert kit.api is original_api  # nothing was patched, so unit tests with fake kits never sleep


def test_the_kit_the_client_loads_itself_is_paced_on_every_http_call(monkeypatch: pytest.MonkeyPatch) -> None:
    limiter = SpyLimiter()
    requested: list[float | None] = []

    def fake_shared(interval: float | None = None) -> PoliteLimiter:
        requested.append(interval)
        return limiter

    kit = SimpleNamespace()

    def api(method: str, path: str, body: Any = None, **params: Any) -> Any:
        return {"workspaces": [{"id": "ws_9"}]}

    kit.api = api
    kit.workspace_id = lambda: kit.api("GET", "/api/v2/workspaces")["workspaces"][0]["id"]
    monkeypatch.setattr(client_mod, "load_kit", lambda: kit)
    monkeypatch.setattr(client_mod, "shared_limiter", fake_shared)
    monkeypatch.setattr(client_mod, "get_settings", lambda: SimpleNamespace(freya_min_interval_s=0.7))

    client = FreyaClient(None)

    assert client.workspace_id() == "ws_9"
    assert requested == [0.7]  # the interval comes from settings.freya_min_interval_s
    assert (
        limiter.waits == 1
    )  # the HTTP call inside workspace_id waited; the wrapper did not wait a second time
    assert client.workspace_id() == "ws_9"
    assert limiter.waits == 2


def test_pacing_can_be_switched_off(monkeypatch: pytest.MonkeyPatch) -> None:
    kit = SimpleNamespace(api=lambda *a, **k: None, workspace_id=lambda: "ws_1")
    original_api = kit.api
    monkeypatch.setattr(client_mod, "load_kit", lambda: kit)

    client = FreyaClient(None, limiter=False)

    assert client.workspace_id() == "ws_1"
    assert kit.api is original_api
