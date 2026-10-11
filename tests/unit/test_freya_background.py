"""Tests for builder.freya.background: abandonable daemon threads for blocking platform calls, and cancellation.

The wall-clock guard cancels the coroutine that awaits a blocking platform call, but cannot stop the thread that runs
it. These tests pin down what the builder does about that instead: the thread is a daemon the process does not wait
for, a cancel token stops every *further* request of that thread (kit requests and client operations alike), and the
client can still say what a cut-off save had created.
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from builder.freya.background import (
    CancelToken,
    PlatformCancelled,
    bound_token,
    check_cancelled,
    current_token,
    inflight_count,
    run_blocking,
    wait_inflight,
)
from builder.freya.client import FreyaClient
from builder.freya.ratelimit import PoliteLimiter, throttle_kit
from builder.trace import NullTrace
from tests.unit.test_freya_client import EVENT_LLM, FakePlatform, make_kit

WF = {"initial_node": "end", "nodes": {"end": {"type": "end_call"}}}


def make_client(platform: FakePlatform, tmp_path: Path) -> FreyaClient:
    return FreyaClient(
        NullTrace(),
        kit=make_kit(platform),
        state_file=tmp_path / "s.json",
        limiter=False,
        sleep=lambda s: None,
    )


# ------------------------------------------------------------------------------------------------ the token


def test_a_token_stops_when_cancelled_or_past_its_deadline() -> None:
    now = {"t": 100.0}
    token = CancelToken(deadline=105.0, clock=lambda: now["t"])
    token.check()  # nothing wrong yet
    now["t"] = 105.5
    assert token.expired and not token.cancelled
    with pytest.raises(PlatformCancelled, match="deadline"):
        token.check()
    other = CancelToken()
    other.cancel("the guard fired")
    assert other.cancelled and other.reason == "the guard fired"
    with pytest.raises(PlatformCancelled, match="the guard fired"):
        other.check()


def test_check_cancelled_only_looks_at_the_bound_token() -> None:
    check_cancelled()  # no token bound: nothing to check
    token = CancelToken()
    with bound_token(token):
        assert current_token() is token
        check_cancelled()
        token.cancel("stop")
        with pytest.raises(PlatformCancelled):
            check_cancelled()
    assert current_token() is None
    check_cancelled()


# ------------------------------------------------------------------------------------------------ run_blocking


async def test_run_blocking_returns_results_and_raises_errors() -> None:
    assert await run_blocking(lambda a, b=0: a + b, 2, b=3) == 5
    with pytest.raises(ZeroDivisionError):
        await run_blocking(lambda: 1 / 0)


async def test_the_token_is_visible_inside_the_worker_thread() -> None:
    token = CancelToken()
    assert await run_blocking(current_token, token=token) is token


async def test_a_cancelled_waiter_does_not_wait_for_the_thread_and_cancels_the_token() -> None:
    release = threading.Event()
    finished = threading.Event()
    token = CancelToken()

    def blocked() -> str:
        release.wait(5.0)
        finished.set()
        return "late"

    task = asyncio.ensure_future(run_blocking(blocked, token=token))
    await asyncio.sleep(0.05)
    assert inflight_count() >= 1
    started = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert time.monotonic() - started < 1.0  # no join on the thread
    assert token.cancelled and "cancel" in token.reason
    assert not finished.is_set()  # the thread is still blocked, and it is a daemon
    release.set()
    assert await wait_inflight(2.0) is True
    assert finished.is_set()


async def test_wait_inflight_gives_up_after_its_timeout() -> None:
    release = threading.Event()
    task = asyncio.ensure_future(run_blocking(release.wait, 5.0))
    await asyncio.sleep(0.02)
    assert await wait_inflight(0.05) is False
    release.set()
    await task
    assert await wait_inflight(1.0) is True


# ------------------------------------------------------------------------------------------------ nothing new after the cancel


async def test_the_paced_kit_refuses_requests_once_the_token_is_cancelled() -> None:
    reached: list[str] = []
    kit = SimpleNamespace(api=lambda method, path, body=None, **params: reached.append(path) or {})
    assert throttle_kit(kit, PoliteLimiter(0.0))
    token = CancelToken()
    await run_blocking(kit.api, "GET", "/first", token=token)
    token.cancel("the guard fired")
    with pytest.raises(PlatformCancelled):
        await run_blocking(kit.api, "GET", "/second", token=token)
    assert reached == ["/first"]


async def test_the_client_starts_no_operation_after_the_cancel(tmp_path: Path) -> None:
    platform = FakePlatform(default_llm=EVENT_LLM)
    client = make_client(platform, tmp_path)
    token = CancelToken()
    token.cancel("the guard fired")
    with pytest.raises(PlatformCancelled):
        await run_blocking(client.workspace_id, token=token)
    assert platform.calls == []


def test_the_snapshot_of_a_complete_save_is_its_deploy_info(tmp_path: Path) -> None:
    client = make_client(FakePlatform(default_llm=EVENT_LLM), tmp_path)
    assert client.deploy_snapshot("s") is None  # nothing was saved yet
    info = client.save("s", WF)
    snapshot: Any = client.deploy_snapshot("s")
    assert snapshot["complete"] is True and snapshot["published"] is True
    assert snapshot["workflow_id"] == info.workflow_id and snapshot["agent_id"] == info.agent_id


def test_the_snapshot_reports_a_workflow_created_before_the_save_was_cut_off(tmp_path: Path) -> None:
    platform = FakePlatform(default_llm=EVENT_LLM)
    token = CancelToken()
    real_api = platform.api

    def cancelling_api(method: str, path: str, body: Any = None, **params: Any) -> Any:
        result = real_api(method, path, body, **params)
        if (method, path) == ("POST", "/api/v2/workflow") and not params.get("dryRun"):
            token.cancel("the guard fired right after the create")
        return result

    platform.api = cancelling_api  # type: ignore[method-assign]
    client = make_client(platform, tmp_path)
    with bound_token(token), pytest.raises(PlatformCancelled):
        client.save("s", WF)
    snapshot: Any = client.deploy_snapshot("s")
    assert snapshot["workflow_id"] == "wf_1" and snapshot["agent_id"] == "ag_1"
    assert snapshot["complete"] is False and snapshot["published"] is False and snapshot["created"] is True
    assert platform.count("GET", r"/api/v2/versions/workflow/[^/]+") == 0  # no request followed the cancel


# ------------------------------------------------------------------------------------------------ deadlines (P-04)


def test_a_token_reports_the_time_left_before_its_deadline() -> None:
    now = {"t": 100.0}
    token = CancelToken(deadline=130.0, clock=lambda: now["t"])
    assert token.remaining() == 30.0
    now["t"] = 129.5
    assert token.remaining() == 0.5
    now["t"] = 140.0
    assert token.remaining() == 0.0  # never negative
    assert CancelToken().remaining() is None  # no deadline: unlimited


def test_time_left_follows_the_nearest_of_the_token_and_an_explicit_deadline() -> None:
    from builder.freya.background import bound_deadline, time_left

    assert time_left() is None  # nothing bound
    now = {"t": 1000.0}
    token = CancelToken(deadline=1100.0, clock=lambda: now["t"])
    with bound_token(token):
        assert time_left() == 100.0
        with bound_deadline(1050.0, clock=lambda: now["t"]):
            assert time_left() == 50.0  # the explicit deadline is nearer
        with bound_deadline(1500.0, clock=lambda: now["t"]):
            assert time_left() == 100.0  # the token's deadline is nearer
    with bound_deadline(1010.0, clock=lambda: now["t"]):
        assert time_left() == 10.0
    assert time_left() is None  # everything is unbound again


def test_a_cancelled_token_leaves_no_time() -> None:
    from builder.freya.background import time_left

    token = CancelToken(deadline=time.monotonic() + 500)
    token.cancel("the guard fired")
    with bound_token(token):
        assert time_left() == 0.0


def test_a_nested_deadline_never_extends_an_outer_one() -> None:
    from builder.freya.background import bound_deadline, time_left

    now = {"t": 0.0}

    def clock() -> float:
        return now["t"]

    with bound_deadline(10.0, clock=clock), bound_deadline(500.0, clock=clock):
        assert time_left() == 10.0
