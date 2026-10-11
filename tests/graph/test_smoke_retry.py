"""The smoke self-test retries the whole conversation once after a transient platform error.

Freya staging sometimes answers ``POST /api/v2/chat/message`` with HTTP 504 after about a minute; a smoke test that fails
for that reason says nothing about the agent. :func:`builder.selftest.smoke.run_smoke` therefore repeats the short
conversation once when the failure is a transient platform error (HTTP 502/503/504, a timeout, a dropped connection)
and the build's budget still covers another smoke run, and traces both attempts. A real reply problem (a wrong opening,
a forbidden word) or a non-transient error (404, an unexpected response) is never retried.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from builder.budget import Budget
from builder.freya.background import CancelToken
from builder.freya.client import ChatTurn, FreyaError
from builder.policy.ir import PolicyIR
from builder.selftest.smoke import is_transient_platform_error, run_smoke
from builder.testing import FakeFreya
from builder.trace import NullTrace
from tests.graph.conftest import opening_reply

NOW = datetime(2026, 10, 14, 9, 0)


def http_504() -> FreyaError:
    return FreyaError(
        "chat: POST /api/v2/chat/message -> 504: Gateway Timeout",
        action="chat",
        status=504,
        body="Gateway Timeout",
        method="POST",
        path="/api/v2/chat/message",
    )


class FlakyFreya(FakeFreya):
    """A fake platform whose chat calls fail with the queued exceptions (``None`` = answer normally), in order."""

    def __init__(self, trace: NullTrace, failures: list[BaseException | None], **kwargs: Any) -> None:
        super().__init__(trace, **kwargs)
        self.failures = list(failures)
        self.chat_attempts = 0

    def chat(
        self,
        workflow_id: str,
        message: str,
        customer_variables: Mapping[str, Any] | None = None,
        call_id: str | None = None,
        *,
        version_id: str | None = None,
    ) -> ChatTurn:
        self.chat_attempts += 1
        failure = self.failures.pop(0) if self.failures else None
        if failure is not None:
            self._note("chat_failed", workflow_id, message)
            raise failure
        return super().chat(workflow_id, message, customer_variables, call_id, version_id=version_id)


def deployed(freya: FakeFreya) -> str:
    freya.save("s", {"nodes": {}})
    return str(freya.saved["s"]["workflow_id"])


def test_transient_platform_errors_are_classified() -> None:
    assert is_transient_platform_error(http_504())
    for status in (502, 503):
        assert is_transient_platform_error(FreyaError("x", action="chat", status=status))
    assert is_transient_platform_error(FreyaError("chat: network error", action="chat", kind="transport"))
    assert is_transient_platform_error(TimeoutError("timed out"))
    assert is_transient_platform_error(ConnectionResetError("reset"))
    assert not is_transient_platform_error(FreyaError("x", action="chat", status=404))
    assert not is_transient_platform_error(FreyaError("x", action="chat", status=500))
    assert not is_transient_platform_error(FreyaError("x", action="chat", status=400))
    assert not is_transient_platform_error(FreyaError("bad shape", action="chat", kind="response"))
    assert not is_transient_platform_error(ValueError("no"))


async def test_a_504_on_the_first_turn_is_retried_once_and_traced(
    ir_a: PolicyIR, null_trace: NullTrace
) -> None:
    freya = FlakyFreya(null_trace, [http_504()], opening=opening_reply(ir_a))
    result = await run_smoke(freya, deployed(freya), ir_a, null_trace, NOW, retry_pause_s=0)
    assert result["passed"], result["summary"]
    assert freya.chat_attempts == 3  # the failed turn + the two turns of the second attempt
    assert "attempt 2" in result["summary"] and "504" in result["summary"]
    attempts = result["details"]["attempts"]
    assert [a["attempt"] for a in attempts] == [1, 2]
    assert attempts[0]["transient"] is True and attempts[1].get("error") is None
    text = "\n".join(null_trace.lines)
    assert "smoke attempt 1 failed: HTTP 504 (platform); retrying" in text


async def test_a_504_mid_conversation_ends_the_old_chat_and_restarts(
    ir_a: PolicyIR, null_trace: NullTrace
) -> None:
    freya = FlakyFreya(null_trace, [None, http_504()], opening=opening_reply(ir_a))
    result = await run_smoke(freya, deployed(freya), ir_a, null_trace, NOW, retry_pause_s=0)
    assert result["passed"], result["summary"]
    actions = [a for a, _ in freya.calls]
    assert actions.count("end_chat") == 2  # the broken conversation and the good one are both ended
    assert len(result["details"]["turns"]) == 2  # the turns are those of the attempt that counted
    # the second attempt starts a fresh conversation with the call variables again
    assert freya.chat_variables[-2] is not None and freya.chat_variables[-1] is None


async def test_two_transient_failures_fail_honestly_after_one_retry(
    ir_a: PolicyIR, null_trace: NullTrace
) -> None:
    freya = FlakyFreya(null_trace, [http_504(), http_504(), http_504()], opening=opening_reply(ir_a))
    result = await run_smoke(freya, deployed(freya), ir_a, null_trace, NOW, retry_pause_s=0)
    assert not result["passed"]
    assert freya.chat_attempts == 2  # one retry, never more
    assert "attempt 1" in result["summary"] and "attempt 2" in result["summary"]
    assert "platform" in result["summary"]
    assert result["details"]["platform_error"] is True
    assert len(result["details"]["attempts"]) == 2


async def test_a_forbidden_word_is_never_retried(ir_a: PolicyIR, null_trace: NullTrace) -> None:
    freya = FlakyFreya(null_trace, [], opening=opening_reply(ir_a), replies=["This is about your debt."])
    result = await run_smoke(freya, deployed(freya), ir_a, null_trace, NOW, retry_pause_s=0)
    assert not result["passed"] and "forbidden before verification" in result["summary"]
    assert freya.chat_attempts == 2  # one conversation only
    assert "retrying" not in "\n".join(null_trace.lines)


async def test_a_wrong_opening_is_never_retried(ir_a: PolicyIR, null_trace: NullTrace) -> None:
    freya = FlakyFreya(null_trace, [], opening="Yes?", replies=["Hmm."])
    result = await run_smoke(freya, deployed(freya), ir_a, null_trace, NOW, retry_pause_s=0)
    assert not result["passed"] and "the opening lacks" in result["summary"]
    assert freya.chat_attempts == 2


async def test_a_non_transient_platform_error_is_not_retried(ir_a: PolicyIR, null_trace: NullTrace) -> None:
    freya = FlakyFreya(null_trace, [FreyaError("chat: -> 404: not found", action="chat", status=404)])
    result = await run_smoke(freya, deployed(freya), ir_a, null_trace, NOW, retry_pause_s=0)
    assert not result["passed"] and freya.chat_attempts == 1
    assert "retrying" not in "\n".join(null_trace.lines)


async def test_a_timeout_is_retried(ir_a: PolicyIR, null_trace: NullTrace) -> None:
    timeout = FreyaError("chat: network error: TimeoutError('timed out')", action="chat", kind="transport")
    freya = FlakyFreya(null_trace, [timeout], opening=opening_reply(ir_a))
    result = await run_smoke(freya, deployed(freya), ir_a, null_trace, NOW, retry_pause_s=0)
    assert result["passed"] and freya.chat_attempts == 3


async def test_no_retry_when_the_budget_has_no_room(
    ir_a: PolicyIR, null_trace: NullTrace, fake_clock: Any
) -> None:
    budget = Budget(1680, clock=fake_clock)
    fake_clock.advance(1680 - 150)  # 150 s left: not enough for another smoke run plus the reserve
    freya = FlakyFreya(null_trace, [http_504()], opening=opening_reply(ir_a))
    result = await run_smoke(freya, deployed(freya), ir_a, null_trace, NOW, budget=budget, retry_pause_s=0)
    assert not result["passed"] and freya.chat_attempts == 1
    text = "\n".join(null_trace.lines)
    assert "smoke attempt 1 failed: HTTP 504 (platform); not retried" in text
    assert "504" in result["summary"] and result["details"]["platform_error"] is True


async def test_a_retry_runs_when_the_budget_has_room(
    ir_a: PolicyIR, null_trace: NullTrace, fake_clock: Any
) -> None:
    budget = Budget(1680, clock=fake_clock)
    fake_clock.advance(1000)
    freya = FlakyFreya(null_trace, [http_504()], opening=opening_reply(ir_a))
    result = await run_smoke(freya, deployed(freya), ir_a, null_trace, NOW, budget=budget, retry_pause_s=0)
    assert result["passed"] and freya.chat_attempts == 3


async def test_no_retry_past_the_build_deadline(
    ir_a: PolicyIR, null_trace: NullTrace, fake_clock: Any
) -> None:
    token = CancelToken(deadline=fake_clock() + 30, clock=fake_clock)  # 30 s before the hard deadline
    freya = FlakyFreya(null_trace, [http_504()], opening=opening_reply(ir_a))
    result = await run_smoke(freya, deployed(freya), ir_a, null_trace, NOW, cancel=token, retry_pause_s=0)
    assert not result["passed"] and freya.chat_attempts == 1


async def test_no_retry_once_the_build_is_cancelled(ir_a: PolicyIR, null_trace: NullTrace) -> None:
    token = CancelToken()
    freya = FlakyFreya(null_trace, [http_504()], opening=opening_reply(ir_a))

    original = freya.chat

    def chat_then_cancel(*args: Any, **kwargs: Any) -> ChatTurn:
        try:
            return original(*args, **kwargs)
        finally:
            token.cancel("wall-clock guard")

    freya.chat = chat_then_cancel  # type: ignore[method-assign]
    result = await run_smoke(freya, deployed(freya), ir_a, null_trace, NOW, cancel=token, retry_pause_s=0)
    assert not result["passed"] and freya.chat_attempts == 1


async def test_the_self_test_node_retries_a_504_within_the_build(
    make_ctx: Any,
    make_gateway: Any,
    invoke: Any,
    ir_a: PolicyIR,
    null_trace: NullTrace,
    monkeypatch: Any,
) -> None:
    """Whole graph: a 504 on the first smoke turn is repeated (the build's budget passed down) and the smoke passes."""
    import builder.selftest.smoke as smoke

    monkeypatch.setattr(smoke, "SMOKE_RETRY_PAUSE_S", 0.0)
    freya = FlakyFreya(null_trace, [http_504()], opening=opening_reply(ir_a))
    state = await invoke(make_ctx(make_gateway(), freya, self_test="smoke"))
    (smoke_result,) = [t for t in state["self_tests"] if t["name"] == "smoke"]
    assert smoke_result["passed"], smoke_result["summary"]
    assert [a["attempt"] for a in smoke_result["details"]["attempts"]] == [1, 2]
    assert "smoke attempt 1 failed: HTTP 504 (platform); retrying" in "\n".join(null_trace.lines)
    assert state["final"]["status"] == "deployed"


# ------------------------------------------------------------------- remote validation answered 504 (P3)


class GatewayTimeoutOnValidate(FakeFreya):
    """A fake platform whose dry-run validation keeps answering 504 (after the client's own retries)."""

    def validate(self, name: str, workflow: dict[str, Any]) -> dict[str, Any]:
        self._note("validate", name)
        raise FreyaError(
            "validate: POST /api/v2/workflow -> 504: Gateway Timeout",
            action="validate",
            status=504,
            body="Gateway Timeout",
            method="POST",
            path="/api/v2/workflow",
        )


async def test_a_504_on_remote_validation_still_deploys_and_says_so(
    make_ctx: Any, make_gateway: Any, invoke: Any, ir_a: PolicyIR, null_trace: NullTrace
) -> None:
    freya = GatewayTimeoutOnValidate(null_trace, opening=opening_reply(ir_a))
    state = await invoke(make_ctx(make_gateway(), freya, self_test="smoke"))
    actions = [a for a, _ in freya.calls]
    assert "validate" in actions and "save" in actions  # the deploy went ahead
    assert state["final"]["status"] == "deployed"
    assert state["remote_validation"]["kind"] == "deployed"  # the save itself was accepted
    notes = " | ".join(state.get("notes") or [])
    assert "remote validation skipped: platform 504" in notes
    assert "remote validation skipped: platform 504" in "\n".join(null_trace.lines)


async def test_a_reply_problem_before_a_504_is_reported_and_not_retried(
    ir_a: PolicyIR, null_trace: NullTrace
) -> None:
    """A wrong opening already answered on turn 1 is a real reply problem: a 504 on turn 2 must not hide it behind a
    fresh conversation that might happen to pass."""
    freya = FlakyFreya(null_trace, [None, http_504()], opening="Yes?")
    result = await run_smoke(freya, deployed(freya), ir_a, null_trace, NOW, retry_pause_s=0)
    assert not result["passed"]
    assert "the opening lacks" in result["summary"]
    assert freya.chat_attempts == 2  # the good turn + the failed turn, no second conversation
    assert len(result["details"]["attempts"]) == 1
    assert "retrying" not in "\n".join(null_trace.lines)
