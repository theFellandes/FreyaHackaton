"""Tests for builder.llm.gateway (the key changer loop) and the provider adapters.

Every provider here is a fake: scripted ``FakeProvider`` objects for the gateway scenarios, and fake
``ChatAnthropic`` / ``ChatOpenAI`` / ``claude_agent_sdk`` stand-ins for the adapters. No network, no LLM calls.
Time is simulated: ``FakeClock`` drives the wall clock (key cooldowns), the monotonic clock (deadlines, circuit
breakers) and ``sleep`` (backoff and cooldown waits), so the tests run instantly and can assert exact waits.
"""

from __future__ import annotations

import json
import random
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest
from pydantic import BaseModel

from builder.llm import gateway as gateway_module
from builder.llm import providers as providers_module
from builder.llm.errors import AllProvidersFailed, ErrorKind, ParseError, ProviderError, classify_error
from builder.llm.gateway import (
    CircuitBreaker,
    CircuitState,
    GatewayConfig,
    LLMGateway,
    build_gateway,
    parse_provider_order,
)
from builder.llm.keys import ApiKey, HealthStore, KeyStatus
from builder.llm.providers import (
    PROVIDER_REGISTRY,
    AnthropicProvider,
    ClaudeCodeProvider,
    LLMRole,
    OpenAIProvider,
    StructuredResult,
    TextResult,
    create_provider,
    register_provider,
)
from builder.llm.ratelimit import RateLimitConfig, RateLimiter
from builder.observability import JsonlObserver, observed_span, read_llm_log
from builder.settings import Settings

REPO_ROOT = Path(__file__).resolve().parents[2]

A1 = "sk-ant-api03-AAAAAAAAAAAAAAAA1111"
A2 = "sk-ant-api03-BBBBBBBBBBBBBBBB2222"
O1 = "sk-proj-CCCCCCCCCCCCCCCCCCCC3333"
ENV = {"ANTHROPIC_API_KEY": A1, "ANTHROPIC_API_KEY_2": A2, "OPENAI_API_KEY": O1}
SECRETS = (A1, A2, O1)


# ------------------------------------------------------------------------------------------------- fakes


class FakeClock:
    """Simulated wall + monotonic clocks and an async sleep that advances both."""

    def __init__(self) -> None:
        self.wall = 1_760_000_000.0
        self.mono = 5_000.0
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.wall

    def monotonic(self) -> float:
        return self.mono

    def advance(self, seconds: float) -> None:
        self.wall += seconds
        self.mono += seconds

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.advance(seconds)


class FakeStatusError(Exception):
    def __init__(self, message: str, status_code: int, headers: dict[str, str] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.response = SimpleNamespace(status_code=status_code, headers=headers or {})


def quota() -> Exception:
    return FakeStatusError("Your credit balance is too low to access the Anthropic API.", 400)


def auth(secret: str = "") -> Exception:
    return FakeStatusError(f"Incorrect API key provided: {secret}", 401)


def rate_limited(retry_after: float | None = None) -> Exception:
    headers = {"retry-after": str(retry_after)} if retry_after is not None else {}
    return FakeStatusError("rate_limit_error: slow down", 429, headers)


def overloaded() -> Exception:
    return FakeStatusError("Overloaded", 529)


def server_error() -> Exception:
    return FakeStatusError("Internal server error", 500)


def not_found() -> Exception:
    return FakeStatusError("model: does-not-exist", 404)


@dataclass
class Call:
    provider: str
    secret: str
    model: str
    system: str
    user: str
    max_tokens: int
    timeout_s: float
    structured: bool


@dataclass
class FakeProvider:
    """Scripted provider: ``outcomes[secret]`` is consumed one item per call (exception -> raised).

    When a key's script is empty the default outcome is used: ``"ok from <name>"`` for text and
    ``{"answer": "ok from <name>"}`` for structured calls. ``latency_s`` advances the fake clock per call.
    """

    name: str
    outcomes: dict[str, list[Any]] = field(default_factory=dict)
    clock: FakeClock | None = None
    latency_s: float = 0.0
    calls: list[Call] = field(default_factory=list)

    def model_for(self, role: LLMRole, settings: Any) -> str:
        return f"{self.name}-{LLMRole(role).value}"

    def _next(self, key: ApiKey, default: Any) -> Any:
        if self.clock is not None and self.latency_s:
            self.clock.advance(self.latency_s)
        script = self.outcomes.get(key.secret, [])
        outcome = script.pop(0) if script else default
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def complete_text(
        self, key: ApiKey, model: str, system: str, user: str, *, max_tokens: int, timeout_s: float
    ) -> TextResult:
        self.calls.append(Call(self.name, key.secret, model, system, user, max_tokens, timeout_s, False))
        return TextResult(str(self._next(key, f"ok from {self.name}")), 11, 7)

    async def complete_structured(
        self,
        key: ApiKey,
        model: str,
        schema: type[BaseModel],
        system: str,
        user: str,
        *,
        max_tokens: int,
        timeout_s: float,
    ) -> StructuredResult:
        self.calls.append(Call(self.name, key.secret, model, system, user, max_tokens, timeout_s, True))
        value = self._next(key, {"answer": f"ok from {self.name}"})
        return StructuredResult(value=value, raw_text=json.dumps(value), input_tokens=21, output_tokens=9)


class Answer(BaseModel):
    answer: str


class RecordingTrace:
    """Minimal TraceSink that keeps every call."""

    def __init__(self) -> None:
        self.model_calls: list[dict[str, Any]] = []
        self.events: list[tuple[str, str, dict[str, Any]]] = []

    def model_call(self, **kwargs: Any) -> None:
        self.model_calls.append(kwargs)

    def event(self, kind: str, message: str, **data: Any) -> None:
        self.events.append((kind, message, data))


def make_settings(tmp_path: Path, **overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "llm_providers": "anthropic,openai",
        "llm_timeout_s": 300.0,
        "llm_max_tokens": 1000,
        "llm_retries_per_key": 2,
        "llm_rate_limit_cooldown_s": 30.0,
        "llm_max_cooldown_wait_s": 45.0,
        "llm_key_health_file": tmp_path / "key_health.json",
        "llm_dead_key_ttl_s": 21600.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def make_gateway(
    tmp_path: Path,
    providers: dict[str, Any],
    clock: FakeClock,
    *,
    env: dict[str, str] | None = None,
    trace: Any = None,
    **settings: Any,
) -> LLMGateway:
    return LLMGateway(
        make_settings(tmp_path, **settings),
        trace if trace is not None else RecordingTrace(),
        env=ENV if env is None else env,
        providers=providers,
        sleep=clock.sleep,
        clock=clock.time,
        monotonic=clock.monotonic,
        rng=random.Random(7),
    )


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def secrets_of(calls: list[Call]) -> list[str]:
    return [c.secret for c in calls]


# ------------------------------------------------------------------------------------- key rotation


async def test_quota_on_first_key_rotates_to_second_key(tmp_path: Path, clock: FakeClock) -> None:
    anthropic = FakeProvider("anthropic", {A1: [quota()]})
    openai = FakeProvider("openai")
    trace = RecordingTrace()
    gw = make_gateway(tmp_path, {"anthropic": anthropic, "openai": openai}, clock, trace=trace)

    result = await gw.text(purpose="draft prose", system="sys", user="hello")

    assert result.value == "ok from anthropic"
    assert result.provider == "anthropic"
    assert result.key_label == "anthropic#2(…2222)"
    assert secrets_of(anthropic.calls) == [A1, A2]
    assert openai.calls == []
    assert [a.error_kind for a in result.attempts] == [ErrorKind.QUOTA, None]
    assert [a.ok for a in result.attempts] == [False, True]
    assert result.attempts[0].action == "rotate_key"
    assert (result.input_tokens, result.output_tokens) == (11, 7)
    assert gw.key_pool("anthropic").status(ApiKey("anthropic", 1, A1)) is KeyStatus.DEAD
    assert clock.sleeps == []
    health = (tmp_path / "key_health.json").read_text(encoding="utf-8")
    assert ApiKey("anthropic", 1, A1).fingerprint in health
    assert all(secret not in health for secret in SECRETS)


async def test_key_marked_dead_is_skipped_in_the_next_run(tmp_path: Path, clock: FakeClock) -> None:
    first = make_gateway(tmp_path, {"anthropic": FakeProvider("anthropic", {A1: [quota()]})}, clock)
    await first.text(purpose="p", system="", user="u")

    anthropic = FakeProvider("anthropic")
    second = make_gateway(tmp_path, {"anthropic": anthropic}, clock)
    result = await second.text(purpose="p", system="", user="u")

    assert secrets_of(anthropic.calls) == [A2]
    assert len(result.attempts) == 1


async def test_all_anthropic_keys_dead_falls_back_to_openai(tmp_path: Path, clock: FakeClock) -> None:
    anthropic = FakeProvider("anthropic", {A1: [quota()], A2: [auth(A2)]})
    openai = FakeProvider("openai")
    gw = make_gateway(tmp_path, {"anthropic": anthropic, "openai": openai}, clock)

    result = await gw.text(purpose="extract", system="", user="u")

    assert result.provider == "openai"
    assert result.model == "openai-strong"
    assert [a.provider for a in result.attempts] == ["anthropic", "anthropic", "openai"]
    assert [a.error_kind for a in result.attempts] == [ErrorKind.QUOTA, ErrorKind.AUTH, None]
    assert gw.available_providers() == ["openai"]

    # the dead keys are not retried by the next call
    await gw.text(purpose="again", system="", user="u")
    assert len(anthropic.calls) == 2


async def test_rate_limited_key_rotates_without_waiting(tmp_path: Path, clock: FakeClock) -> None:
    anthropic = FakeProvider("anthropic", {A1: [rate_limited(5)]})
    gw = make_gateway(tmp_path, {"anthropic": anthropic}, clock)

    result = await gw.text(purpose="p", system="", user="u")

    assert result.key_label == "anthropic#2(…2222)"
    assert clock.sleeps == []
    assert gw.key_pool("anthropic").status(ApiKey("anthropic", 1, A1)) is KeyStatus.COOLING


async def test_rate_limit_cooldown_then_reuse_same_key(tmp_path: Path, clock: FakeClock) -> None:
    anthropic = FakeProvider("anthropic", {A1: [rate_limited(5)]})
    gw = make_gateway(
        tmp_path, {"anthropic": anthropic}, clock, env={"ANTHROPIC_API_KEY": A1}, llm_providers="anthropic"
    )

    result = await gw.text(purpose="p", system="", user="u")

    assert result.value == "ok from anthropic"
    assert secrets_of(anthropic.calls) == [A1, A1]
    assert clock.sleeps == [pytest.approx(5.05)]
    assert [a.error_kind for a in result.attempts] == [ErrorKind.RATE_LIMIT, None]


async def test_rate_limit_without_header_uses_configured_cooldown(tmp_path: Path, clock: FakeClock) -> None:
    anthropic = FakeProvider("anthropic", {A1: [rate_limited()]})
    gw = make_gateway(
        tmp_path,
        {"anthropic": anthropic},
        clock,
        env={"ANTHROPIC_API_KEY": A1},
        llm_providers="anthropic",
        llm_rate_limit_cooldown_s=12.0,
    )
    await gw.text(purpose="p", system="", user="u")
    assert clock.sleeps == [pytest.approx(12.05)]


async def test_other_provider_preferred_over_waiting_for_cooldown(tmp_path: Path, clock: FakeClock) -> None:
    anthropic = FakeProvider("anthropic", {A1: [rate_limited(5)]})
    openai = FakeProvider("openai")
    gw = make_gateway(
        tmp_path,
        {"anthropic": anthropic, "openai": openai},
        clock,
        env={"ANTHROPIC_API_KEY": A1, "OPENAI_API_KEY": O1},
    )

    result = await gw.text(purpose="p", system="", user="u")

    assert result.provider == "openai"
    assert clock.sleeps == []


async def test_cooldown_longer_than_max_wait_fails_fast(tmp_path: Path, clock: FakeClock) -> None:
    anthropic = FakeProvider("anthropic", {A1: [rate_limited(120)]})
    gw = make_gateway(
        tmp_path, {"anthropic": anthropic}, clock, env={"ANTHROPIC_API_KEY": A1}, llm_providers="anthropic"
    )

    with pytest.raises(AllProvidersFailed) as info:
        await gw.text(purpose="p", system="", user="u")

    assert clock.sleeps == []
    assert len(info.value.attempts) == 1
    assert any("cooling" in r for r in info.value.reasons)


# -------------------------------------------------------------------------------- transient errors


async def test_transient_errors_retried_on_same_key_with_backoff(tmp_path: Path, clock: FakeClock) -> None:
    anthropic = FakeProvider("anthropic", {A1: [overloaded(), server_error()]})
    gw = make_gateway(tmp_path, {"anthropic": anthropic}, clock)

    result = await gw.text(purpose="p", system="", user="u")

    assert secrets_of(anthropic.calls) == [A1, A1, A1]
    assert result.key_label == "anthropic#1(…1111)"
    assert [a.error_kind for a in result.attempts] == [ErrorKind.OVERLOADED, ErrorKind.SERVER, None]
    assert [a.action for a in result.attempts[:2]] == ["retry_same", "retry_same"]
    assert len(clock.sleeps) == 2
    assert 1.0 <= clock.sleeps[0] <= 1.25
    assert 2.0 <= clock.sleeps[1] <= 2.5
    assert gw.describe()["providers"]["anthropic"]["circuit"] == "closed"


async def test_transient_retries_exhausted_then_next_key(tmp_path: Path, clock: FakeClock) -> None:
    anthropic = FakeProvider("anthropic", {A1: [overloaded(), overloaded()]})
    gw = make_gateway(tmp_path, {"anthropic": anthropic}, clock, llm_retries_per_key=1)

    result = await gw.text(purpose="p", system="", user="u")

    assert secrets_of(anthropic.calls) == [A1, A1, A2]
    assert result.attempts[1].action == "rotate_key"
    assert gw.key_pool("anthropic").status(ApiKey("anthropic", 1, A1)) is KeyStatus.ACTIVE  # not dead


async def test_circuit_opens_after_three_provider_failures(tmp_path: Path, clock: FakeClock) -> None:
    anthropic = FakeProvider("anthropic", {A1: [overloaded(), overloaded(), overloaded()]})
    openai = FakeProvider("openai")
    gw = make_gateway(tmp_path, {"anthropic": anthropic, "openai": openai}, clock)

    first = await gw.text(purpose="p1", system="", user="u")
    assert first.provider == "openai"
    assert secrets_of(anthropic.calls) == [A1, A1, A1]  # key 2 not tried: the provider is down
    assert first.attempts[2].action == "next_provider"
    assert gw.describe()["providers"]["anthropic"]["circuit"] == "open"
    assert gw.available_providers() == ["openai"]

    second = await gw.text(purpose="p2", system="", user="u")
    assert second.provider == "openai"
    assert len(anthropic.calls) == 3  # skipped while open

    clock.advance(121)
    third = await gw.text(purpose="p3", system="", user="u")
    assert third.provider == "anthropic"  # half-open trial succeeded
    assert gw.describe()["providers"]["anthropic"]["circuit"] == "closed"


async def test_unknown_errors_rotate_keys_then_provider(tmp_path: Path, clock: FakeClock) -> None:
    anthropic = FakeProvider("anthropic", {A1: [RuntimeError("boom")], A2: [RuntimeError("bang")]})
    openai = FakeProvider("openai")
    gw = make_gateway(tmp_path, {"anthropic": anthropic, "openai": openai}, clock)

    result = await gw.text(purpose="p", system="", user="u")

    assert result.provider == "openai"
    assert secrets_of(anthropic.calls) == [A1, A2]
    assert [a.error_kind for a in result.attempts[:2]] == [ErrorKind.UNKNOWN, ErrorKind.UNKNOWN]
    assert clock.sleeps == []


# ------------------------------------------------------------------------------ request-level errors


async def test_bad_request_moves_to_next_provider_without_touching_keys(
    tmp_path: Path, clock: FakeClock
) -> None:
    anthropic = FakeProvider("anthropic", {A1: [not_found()]})
    openai = FakeProvider("openai")
    gw = make_gateway(tmp_path, {"anthropic": anthropic, "openai": openai}, clock)

    result = await gw.text(purpose="p", system="", user="u")

    assert result.provider == "openai"
    assert secrets_of(anthropic.calls) == [A1]
    assert result.attempts[0].error_kind is ErrorKind.BAD_REQUEST
    assert result.attempts[0].action == "next_provider"
    assert gw.key_pool("anthropic").status(ApiKey("anthropic", 1, A1)) is KeyStatus.ACTIVE
    assert not (tmp_path / "key_health.json").exists()


async def test_context_overflow_moves_to_next_provider(tmp_path: Path, clock: FakeClock) -> None:
    anthropic = FakeProvider("anthropic", {A1: [FakeStatusError("prompt is too long: 300000 tokens", 400)]})
    openai = FakeProvider("openai")
    gw = make_gateway(tmp_path, {"anthropic": anthropic, "openai": openai}, clock)

    result = await gw.text(purpose="p", system="", user="u")

    assert result.provider == "openai"
    assert result.attempts[0].error_kind is ErrorKind.CONTEXT


async def test_parse_error_retried_once_with_repair_message(tmp_path: Path, clock: FakeClock) -> None:
    anthropic = FakeProvider("anthropic", {A1: [ParseError("answer: Field required"), {"answer": "fixed"}]})
    gw = make_gateway(tmp_path, {"anthropic": anthropic}, clock)

    result = await gw.structured(purpose="extract IR", schema=Answer, system="sys", user="question")

    assert isinstance(result.value, Answer)
    assert result.value.answer == "fixed"
    assert secrets_of(anthropic.calls) == [A1, A1]
    first, second = anthropic.calls
    assert first.user == "question"
    assert second.user.startswith("question")
    assert "could not be used because" in second.user
    assert "answer: Field required" in second.user
    assert clock.sleeps == []  # no backoff for PARSE


async def test_parse_error_twice_moves_to_next_provider(tmp_path: Path, clock: FakeClock) -> None:
    anthropic = FakeProvider("anthropic", {A1: [ParseError("bad"), ParseError("still bad")]})
    openai = FakeProvider("openai")
    gw = make_gateway(tmp_path, {"anthropic": anthropic, "openai": openai}, clock)

    result = await gw.structured(purpose="p", schema=Answer, system="", user="u")

    assert result.provider == "openai"
    assert result.value == Answer(answer="ok from openai")
    assert secrets_of(anthropic.calls) == [A1, A1]  # same key twice, key 2 not tried
    assert [a.action for a in result.attempts[:2]] == ["retry_same", "next_provider"]


async def test_transient_retry_does_not_consume_the_parse_repair(tmp_path: Path, clock: FakeClock) -> None:
    anthropic = FakeProvider("anthropic", {A1: [overloaded(), ParseError("answer: Field required")]})
    openai = FakeProvider("openai")
    gw = make_gateway(tmp_path, {"anthropic": anthropic, "openai": openai}, clock)

    result = await gw.structured(purpose="p", schema=Answer, system="", user="u")

    assert result.provider == "anthropic"
    assert secrets_of(anthropic.calls) == [A1, A1, A1]
    assert [a.action for a in result.attempts[:2]] == ["retry_same", "retry_same"]
    assert "answer: Field required" in anthropic.calls[2].user
    assert openai.calls == []


async def test_gateway_validates_structured_values(tmp_path: Path, clock: FakeClock) -> None:
    anthropic = FakeProvider("anthropic", {A1: [{"wrong": "shape"}, {"answer": "good"}]})
    gw = make_gateway(tmp_path, {"anthropic": anthropic}, clock)

    result = await gw.structured(purpose="p", schema=Answer, system="", user="u")

    assert result.value == Answer(answer="good")
    assert result.attempts[0].error_kind is ErrorKind.PARSE
    assert "answer" in anthropic.calls[1].user  # the repair note names the missing field


async def test_empty_text_reply_is_retried_then_next_provider(tmp_path: Path, clock: FakeClock) -> None:
    anthropic = FakeProvider("anthropic", {A1: ["", "  "]})
    openai = FakeProvider("openai")
    gw = make_gateway(tmp_path, {"anthropic": anthropic, "openai": openai}, clock)

    result = await gw.text(purpose="p", system="", user="u")

    assert result.provider == "openai"
    assert "previous reply was empty" in anthropic.calls[1].user


# ---------------------------------------------------------------------------------------- deadlines


async def test_deadline_caps_attempt_timeout(tmp_path: Path, clock: FakeClock) -> None:
    anthropic = FakeProvider("anthropic")
    gw = make_gateway(tmp_path, {"anthropic": anthropic}, clock)

    await gw.text(purpose="p", system="", user="u", deadline=clock.monotonic() + 100)
    await gw.text(purpose="p", system="", user="u")

    assert anthropic.calls[0].timeout_s == pytest.approx(95.0)  # 100 s left minus the 5 s grace
    assert anthropic.calls[1].timeout_s == pytest.approx(300.0)


async def test_deadline_stops_new_attempts(tmp_path: Path, clock: FakeClock) -> None:
    anthropic = FakeProvider("anthropic", {A1: [overloaded()] * 5}, clock=clock, latency_s=20.0)
    openai = FakeProvider("openai")
    gw = make_gateway(tmp_path, {"anthropic": anthropic, "openai": openai}, clock)

    with pytest.raises(AllProvidersFailed) as info:
        await gw.text(purpose="p", system="", user="u", deadline=clock.monotonic() + 45)

    assert len(anthropic.calls) == 2
    assert anthropic.calls[0].timeout_s == pytest.approx(40.0)
    assert anthropic.calls[1].timeout_s < 20.0
    assert openai.calls == []  # stopped instead of starting new attempts elsewhere
    assert "deadline reached" in info.value.reasons


async def test_expired_deadline_makes_no_attempt(tmp_path: Path, clock: FakeClock) -> None:
    anthropic = FakeProvider("anthropic")
    gw = make_gateway(tmp_path, {"anthropic": anthropic}, clock)

    with pytest.raises(AllProvidersFailed) as info:
        await gw.text(purpose="p", system="", user="u", deadline=clock.monotonic() + 3)

    assert anthropic.calls == []
    assert info.value.attempts == []


# ------------------------------------------------------------------------------------- exhaustion


async def test_everything_dead_raises_with_all_attempts(tmp_path: Path, clock: FakeClock) -> None:
    anthropic = FakeProvider("anthropic", {A1: [quota()], A2: [quota()]})
    openai = FakeProvider("openai", {O1: [auth(O1)]})
    gw = make_gateway(tmp_path, {"anthropic": anthropic, "openai": openai}, clock)

    with pytest.raises(AllProvidersFailed) as info:
        await gw.text(purpose="extract policy", system="", user="u")

    assert [a.key_label for a in info.value.attempts] == [
        "anthropic#1(…1111)",
        "anthropic#2(…2222)",
        "openai#1(…3333)",
    ]
    message = str(info.value)
    assert "extract policy" in message
    assert all(secret not in message for secret in SECRETS)
    assert all(secret not in (a.error or "") for a in info.value.attempts for secret in SECRETS)


async def test_no_keys_at_all(tmp_path: Path, clock: FakeClock) -> None:
    gw = make_gateway(
        tmp_path, {"anthropic": FakeProvider("anthropic"), "openai": FakeProvider("openai")}, clock, env={}
    )

    with pytest.raises(AllProvidersFailed) as info:
        await gw.text(purpose="p", system="", user="u")

    assert info.value.attempts == []
    assert "anthropic: no keys configured" in info.value.reasons
    assert gw.available_providers() == []


async def test_keys_dead_from_previous_run_are_revived_as_last_resort(
    tmp_path: Path, clock: FakeClock
) -> None:
    store = HealthStore(tmp_path / "key_health.json", ttl_s=21600, clock=clock.time)
    for secret, index in ((A1, 1), (A2, 2)):
        key = ApiKey("anthropic", index, secret)
        store.mark_dead("anthropic", key.fingerprint, "quota: old", label=key.label)
    anthropic = FakeProvider("anthropic")
    trace = RecordingTrace()
    gw = make_gateway(tmp_path, {"anthropic": anthropic}, clock, trace=trace, llm_providers="anthropic")

    result = await gw.text(purpose="p", system="", user="u")

    assert result.key_label == "anthropic#1(…1111)"
    assert store.lookup("anthropic", ApiKey("anthropic", 1, A1).fingerprint) is None  # healthy again
    assert store.lookup("anthropic", ApiKey("anthropic", 2, A2).fingerprint) is not None
    assert any("last resort" in message for _, message, _ in trace.events)


async def test_claude_code_not_logged_in_is_auth_dead_and_persisted(tmp_path: Path, clock: FakeClock) -> None:
    """No Anthropic key; OpenAI out of credit; the CLI says 'Not logged in' -> fast failure, login marked dead."""

    class LoginProvider(FakeProvider):
        def discover_keys(self, env: Any) -> list[ApiKey]:
            return [LOGIN]

    not_logged_in = ProviderError("Claude Code login missing: Not logged in", kind=ErrorKind.AUTH)
    openai = FakeProvider("openai", {O1: [quota()]})
    login = LoginProvider("claude_code", {"login": [not_logged_in]})
    env = {"OPENAI_API_KEY": O1}
    gw = make_gateway(
        tmp_path,
        {"anthropic": FakeProvider("anthropic"), "openai": openai, "claude_code": login},
        clock,
        env=env,
        llm_providers="anthropic,openai,claude_code",
    )

    with pytest.raises(AllProvidersFailed) as info:
        await gw.text(purpose="p", system="", user="u")

    assert [(a.provider, a.error_kind) for a in info.value.attempts] == [
        ("openai", ErrorKind.QUOTA),
        ("claude_code", ErrorKind.AUTH),
    ]
    assert clock.sleeps == [], "AUTH must not be retried or waited on"
    assert gw.key_pool("claude_code").status(LOGIN) is KeyStatus.DEAD
    health = (tmp_path / "key_health.json").read_text(encoding="utf-8")
    assert LOGIN.fingerprint in health
    assert O1 not in health

    # The next run starts with the login already dead (it is only revived as a last resort).
    second = make_gateway(
        tmp_path,
        {"claude_code": LoginProvider("claude_code")},
        clock,
        env=env,
        llm_providers="claude_code",
    )
    assert second.key_pool("claude_code").status(LOGIN) is KeyStatus.DEAD
    assert second.available_providers() == []


# ------------------------------------------------------------------------------------------- tracing


async def test_trace_has_one_model_call_per_attempt_and_no_secrets(tmp_path: Path, clock: FakeClock) -> None:
    anthropic = FakeProvider("anthropic", {A1: [auth(A1)], A2: [overloaded()]})
    openai = FakeProvider("openai")
    trace = RecordingTrace()
    gw = make_gateway(tmp_path, {"anthropic": anthropic, "openai": openai}, clock, trace=trace)

    result = await gw.text(purpose="understand brief", system="", user="u")

    assert len(trace.model_calls) == len(result.attempts) == 3
    assert [c["attempt"] for c in trace.model_calls] == [1, 2, 3]
    assert all(c["purpose"] == "understand brief" for c in trace.model_calls)
    assert [c["ok"] for c in trace.model_calls] == [False, False, True]
    assert trace.model_calls[0]["error_kind"] == "auth"
    assert trace.model_calls[0]["key_label"] == "anthropic#1(…1111)"
    assert trace.model_calls[2]["input_tokens"] == 11
    dumped = json.dumps([trace.model_calls, [(k, m, d) for k, m, d in trace.events]], default=str)
    assert all(secret not in dumped for secret in SECRETS)


async def test_real_null_trace_lines_have_no_secrets(tmp_path: Path, clock: FakeClock) -> None:
    trace_module = pytest.importorskip("builder.trace")
    null_trace = trace_module.NullTrace()  # no secrets registered: the gateway itself must not leak them
    anthropic = FakeProvider("anthropic", {A1: [auth(A1)]})
    gw = make_gateway(tmp_path, {"anthropic": anthropic}, clock, trace=null_trace)

    result = await gw.text(purpose="extract policy IR", system="", user="u")

    model_events = null_trace.of_kind("model_call")
    assert len(model_events) == len(result.attempts) == 2
    text = null_trace.text
    assert "anthropic#1(…1111)" in text and "extract policy IR" in text
    assert all(secret not in text for secret in SECRETS)
    assert all(secret not in json.dumps(null_trace.events) for secret in SECRETS)


# ---------------------------------------------------------------------------------- configuration


def test_parse_provider_order() -> None:
    assert parse_provider_order("anthropic, OpenAI;claude-code anthropic") == (
        "anthropic",
        "openai",
        "claude_code",
    )
    assert parse_provider_order(["openai", "openai", ""]) == ("openai",)


def test_gateway_config_from_partial_settings_disables_health_file() -> None:
    config = GatewayConfig.from_settings(SimpleNamespace(llm_providers="openai", llm_retries_per_key=-1))
    assert config.providers == ("openai",)
    assert config.retries_per_key == 0
    assert config.key_health_file is None
    assert config.timeout_s == 300.0


def test_gateway_config_from_real_settings(tmp_path: Path) -> None:
    settings_module = pytest.importorskip("builder.settings")
    settings = settings_module.Settings(
        _env_file=None, llm_providers="openai,anthropic", llm_key_health_file=tmp_path / "kh.json"
    )
    config = GatewayConfig.from_settings(settings)
    assert config.providers == ("openai", "anthropic")
    assert config.key_health_file == tmp_path / "kh.json"
    assert config.max_tokens == settings.llm_max_tokens


def test_describe_lists_models_and_masked_keys(tmp_path: Path, clock: FakeClock) -> None:
    gw = make_gateway(
        tmp_path, {"anthropic": FakeProvider("anthropic"), "openai": FakeProvider("openai")}, clock
    )
    description = gw.describe()
    assert description["order"] == ["anthropic", "openai"]
    anthropic = description["providers"]["anthropic"]
    assert anthropic["models"] == {"strong": "anthropic-strong", "fast": "anthropic-fast"}
    assert [k["label"] for k in anthropic["keys"]] == ["anthropic#1(…1111)", "anthropic#2(…2222)"]
    assert all(secret not in json.dumps(description, default=str) for secret in SECRETS)
    assert sorted(gw.secret_values()) == sorted(SECRETS)


def test_registry_providers_are_built_in_configured_order(tmp_path: Path) -> None:
    gw = LLMGateway(make_settings(tmp_path, llm_providers="openai,bogus,anthropic"), None, env=ENV)
    assert gw.order == ("openai", "anthropic")
    assert isinstance(gw._providers["openai"], OpenAIProvider)


async def test_custom_registered_provider_with_own_keys(
    tmp_path: Path, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    class LocalProvider(FakeProvider):
        def discover_keys(self, env: Any) -> list[ApiKey]:
            return [ApiKey("local", 0, "no-key-needed")]

    monkeypatch.setitem(PROVIDER_REGISTRY, "local", lambda env: LocalProvider("local"))
    gw = LLMGateway(make_settings(tmp_path, llm_providers="local"), None, env={}, sleep=clock.sleep)
    result = await gw.text(purpose="p", system="", user="u")
    assert result.provider == "local"


def test_register_provider_validates_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(providers_module, "PROVIDER_REGISTRY", dict(PROVIDER_REGISTRY))
    register_provider("  MyVendor ", lambda env: FakeProvider("myvendor"))
    assert "myvendor" in providers_module.PROVIDER_REGISTRY
    with pytest.raises(ValueError):
        register_provider("  ", lambda env: FakeProvider("x"))
    with pytest.raises(KeyError):
        create_provider("nope", {})


async def test_probe_reports_each_provider(tmp_path: Path, clock: FakeClock) -> None:
    anthropic = FakeProvider("anthropic", {A1: [quota()], A2: [quota()]})
    openai = FakeProvider("openai")
    gw = make_gateway(tmp_path, {"anthropic": anthropic, "openai": openai}, clock)

    report = await gw.probe()

    assert report["anthropic"]["ok"] is False
    assert report["anthropic"]["error_kind"] == "quota"
    assert report["openai"] == {
        "ok": True,
        "model": "openai-fast",
        "key": "openai#1(…3333)",
        "latency_s": 0.0,
        "attempts": 1,
    }
    assert openai.calls[0].max_tokens == 16


# ---------------------------------------------------------------------------------- circuit breaker


def test_circuit_breaker_state_machine(clock: FakeClock) -> None:
    breaker = CircuitBreaker(threshold=2, open_s=10, clock=clock.monotonic)
    breaker.record_failure()
    assert breaker.state is CircuitState.CLOSED and breaker.allow()
    breaker.record_failure()
    assert breaker.is_open and not breaker.allow()
    assert breaker.remaining_open_s() == pytest.approx(10)
    clock.advance(10)
    assert breaker.state is CircuitState.HALF_OPEN and breaker.allow()
    breaker.record_failure()  # failed trial re-opens immediately
    assert breaker.is_open
    clock.advance(10)
    assert breaker.allow()
    breaker.record_success()
    assert breaker.state is CircuitState.CLOSED


# ================================================================================ provider adapters


class FakeRunnable:
    def __init__(self, output: dict[str, Any]) -> None:
        self.output = output
        self.inputs: list[Any] = []

    async def ainvoke(self, messages: Any) -> dict[str, Any]:
        self.inputs.append(messages)
        return self.output


class FakeChat:
    """Stands in for ChatAnthropic / ChatOpenAI; records constructor kwargs and calls."""

    instances: ClassVar[list[FakeChat]] = []
    reply: ClassVar[Any] = None
    structured_output: ClassVar[dict[str, Any]] = {}

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.invocations: list[Any] = []
        self.structured_calls: list[tuple[Any, dict[str, Any]]] = []
        FakeChat.instances.append(self)

    async def ainvoke(self, messages: Any) -> Any:
        self.invocations.append(messages)
        return FakeChat.reply

    def with_structured_output(self, schema: Any, **kwargs: Any) -> FakeRunnable:
        self.structured_calls.append((schema, kwargs))
        return FakeRunnable(FakeChat.structured_output)


@pytest.fixture
def fake_chat(monkeypatch: pytest.MonkeyPatch) -> type[FakeChat]:
    messages = pytest.importorskip("langchain_core.messages")
    FakeChat.instances = []
    FakeChat.reply = messages.AIMessage(
        content="hello there", usage_metadata={"input_tokens": 5, "output_tokens": 3, "total_tokens": 8}
    )
    FakeChat.structured_output = {}
    monkeypatch.setattr(providers_module, "_chat_anthropic_class", lambda: FakeChat)
    monkeypatch.setattr(providers_module, "_chat_openai_class", lambda: FakeChat)
    return FakeChat


def _ai(text: str, **metadata: Any) -> Any:
    messages = pytest.importorskip("langchain_core.messages")
    return messages.AIMessage(
        content=text,
        usage_metadata={"input_tokens": 9, "output_tokens": 4, "total_tokens": 13},
        response_metadata=metadata,
    )


KEY_A = ApiKey("anthropic", 1, A1)
KEY_O = ApiKey("openai", 1, O1)


async def test_anthropic_text_builds_chat_without_sampling_params(fake_chat: type[FakeChat]) -> None:
    result = await AnthropicProvider().complete_text(
        KEY_A, "claude-x", "be brief", "hi", max_tokens=123, timeout_s=45.0
    )
    assert result == TextResult("hello there", 5, 3)
    (chat,) = fake_chat.instances
    assert chat.kwargs == {
        "model": "claude-x",
        "api_key": A1,
        "max_tokens": 123,
        "timeout": 45.0,
        "max_retries": 0,
    }
    system, human = chat.invocations[0]
    assert (system.type, system.content) == ("system", "be brief")
    assert (human.type, human.content) == ("human", "hi")


async def test_empty_system_prompt_is_omitted(fake_chat: type[FakeChat]) -> None:
    await AnthropicProvider().complete_text(KEY_A, "m", "  ", "hi", max_tokens=10, timeout_s=5)
    assert [m.type for m in fake_chat.instances[0].invocations[0]] == ["human"]


async def test_anthropic_structured_uses_native_json_schema(fake_chat: type[FakeChat]) -> None:
    fake_chat.structured_output = {
        "raw": _ai('{"answer": "42"}'),
        "parsed": {"answer": "42"},
        "parsing_error": None,
    }
    result = await AnthropicProvider().complete_structured(
        KEY_A, "claude-x", Answer, "sys", "q", max_tokens=100, timeout_s=10
    )
    assert result.value == Answer(answer="42")
    assert (result.input_tokens, result.output_tokens) == (9, 4)
    schema, kwargs = fake_chat.instances[0].structured_calls[0]
    assert kwargs == {"include_raw": True, "method": "json_schema"}
    assert (
        isinstance(schema, dict) and schema["title"] == "Answer" and schema["additionalProperties"] is False
    )


async def test_structured_falls_back_to_raw_text_parsing(fake_chat: type[FakeChat]) -> None:
    fake_chat.structured_output = {
        "raw": _ai('Here it is:\n```json\n{"answer": "from raw"}\n```'),
        "parsed": None,
        "parsing_error": ValueError("parser choked"),
    }
    result = await AnthropicProvider().complete_structured(
        KEY_A, "m", Answer, "", "q", max_tokens=10, timeout_s=5
    )
    assert result.value == Answer(answer="from raw")


async def test_structured_invalid_parsed_value_falls_back_then_raises(fake_chat: type[FakeChat]) -> None:
    fake_chat.structured_output = {
        "raw": _ai("not json at all", stop_reason="max_tokens"),
        "parsed": {"wrong": 1},
        "parsing_error": None,
    }
    with pytest.raises(ParseError) as info:
        await AnthropicProvider().complete_structured(KEY_A, "m", Answer, "", "q", max_tokens=10, timeout_s=5)
    assert "truncated at max_tokens" in info.value.message
    assert "answer" in info.value.message
    assert info.value.raw_text == "not json at all"
    assert classify_error(info.value) is ErrorKind.PARSE


async def test_anthropic_free_form_schema_uses_prompted_json(fake_chat: type[FakeChat]) -> None:
    class Labels(BaseModel):
        labels: dict[str, str]

    fake_chat.reply = _ai('{"labels": {"a": "b"}}')
    result = await AnthropicProvider().complete_structured(
        KEY_A, "m", Labels, "sys", "q", max_tokens=10, timeout_s=5
    )
    assert result.value == Labels(labels={"a": "b"})
    chat = fake_chat.instances[0]
    assert chat.structured_calls == []  # native constrained decoding cannot express open maps
    assert "Return ONLY JSON matching this JSON schema" in chat.invocations[0][0].content


async def test_openai_provider_kwargs(fake_chat: type[FakeChat]) -> None:
    await OpenAIProvider().complete_text(KEY_O, "gpt-x", "", "hi", max_tokens=99, timeout_s=30)
    assert fake_chat.instances[0].kwargs == {"model": "gpt-x", "api_key": O1, "timeout": 30, "max_retries": 0}
    fake_chat.structured_output = {
        "raw": _ai('{"answer": "o"}'),
        "parsed": {"answer": "o"},
        "parsing_error": None,
    }
    result = await OpenAIProvider().complete_structured(
        KEY_O, "gpt-x", Answer, "", "q", max_tokens=9, timeout_s=3
    )
    assert result.value == Answer(answer="o")
    _, kwargs = fake_chat.instances[1].structured_calls[0]
    assert kwargs == {"include_raw": True, "method": "json_schema", "strict": False}


def test_model_for_reads_settings_with_defaults() -> None:
    settings = SimpleNamespace(anthropic_strong_model="claude-custom", anthropic_fast_model="  ")
    provider = AnthropicProvider()
    assert provider.model_for(LLMRole.STRONG, settings) == "claude-custom"
    assert (
        provider.model_for(LLMRole.FAST, settings)
        == providers_module.DEFAULT_MODELS["anthropic"][LLMRole.FAST]
    )
    assert OpenAIProvider().model_for(LLMRole.STRONG, object()) == "gpt-5.6-terra"


# -------------------------------------------------------------------------------------- Claude Code


@dataclass
class FakeTextBlock:
    text: str


@dataclass
class FakeAssistantMessage:
    content: list[Any]
    model: str = "claude-x"
    error: str | None = None


@dataclass
class FakeResultMessage:
    subtype: str = "success"
    is_error: bool = False
    usage: dict[str, Any] | None = None
    result: str | None = None
    api_error_status: int | None = None
    errors: list[str] | None = None


class FakeOptions:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


def fake_sdk(messages: list[Any], captured: list[Any]) -> SimpleNamespace:
    async def query(*, prompt: str, options: FakeOptions) -> Any:
        captured.append((prompt, options))
        for message in messages:
            yield message

    return SimpleNamespace(
        query=query,
        ClaudeAgentOptions=FakeOptions,
        AssistantMessage=FakeAssistantMessage,
        ResultMessage=FakeResultMessage,
        TextBlock=FakeTextBlock,
    )


LOGIN = ApiKey("claude_code", 0, "login")


async def test_claude_code_text_one_turn_no_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[Any] = []
    messages = [
        FakeAssistantMessage([FakeTextBlock("Hello "), FakeTextBlock("world")]),
        FakeResultMessage(usage={"input_tokens": 10, "cache_read_input_tokens": 5, "output_tokens": 7}),
    ]
    monkeypatch.setattr(providers_module, "_import_claude_sdk", lambda: fake_sdk(messages, captured))
    provider = ClaudeCodeProvider({"CLAUDE_CODE_OAUTH_TOKEN": "oauth-tok-123456"})

    result = await provider.complete_text(
        LOGIN, "claude-x", "sys", "user prompt", max_tokens=500, timeout_s=60
    )

    assert result == TextResult("Hello world", 15, 7)
    prompt, options = captured[0]
    assert prompt == "user prompt"
    assert options.kwargs["model"] == "claude-x"
    assert options.kwargs["system_prompt"] == "sys"
    assert options.kwargs["tools"] == [] and options.kwargs["allowed_tools"] == []
    assert options.kwargs["max_turns"] == 1
    assert options.kwargs["setting_sources"] == []
    assert options.kwargs["permission_mode"] == "default"
    assert options.kwargs["env"] == {
        "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "500",
        "ANTHROPIC_API_KEY": "",
        "CLAUDE_CODE_OAUTH_TOKEN": "oauth-tok-123456",
    }


async def test_claude_code_with_real_key_passes_it_to_the_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[Any] = []
    messages = [FakeAssistantMessage([FakeTextBlock("ok")]), FakeResultMessage()]
    monkeypatch.setattr(providers_module, "_import_claude_sdk", lambda: fake_sdk(messages, captured))
    await ClaudeCodeProvider().complete_text(
        ApiKey("claude_code", 1, A1), "m", "", "p", max_tokens=10, timeout_s=5
    )
    assert captured[0][1].kwargs["env"]["ANTHROPIC_API_KEY"] == A1
    assert captured[0][1].kwargs["system_prompt"] is None


async def test_claude_code_billing_error_is_quota(monkeypatch: pytest.MonkeyPatch) -> None:
    messages = [
        FakeAssistantMessage([FakeTextBlock("Credit balance is too low")], error="billing_error"),
        FakeResultMessage(is_error=True, subtype="success", result="Credit balance is too low"),
    ]
    monkeypatch.setattr(providers_module, "_import_claude_sdk", lambda: fake_sdk(messages, []))
    with pytest.raises(ProviderError) as info:
        await ClaudeCodeProvider().complete_text(LOGIN, "m", "", "p", max_tokens=10, timeout_s=5)
    assert info.value.kind is ErrorKind.QUOTA
    assert classify_error(info.value) is ErrorKind.QUOTA


async def test_claude_code_not_logged_in_text_is_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bundled CLI answers 'Not logged in' as ordinary assistant text; it must become an AUTH failure."""
    messages = [
        FakeAssistantMessage([FakeTextBlock("Not logged in · Please run /login")]),
        FakeResultMessage(),
    ]
    monkeypatch.setattr(providers_module, "_import_claude_sdk", lambda: fake_sdk(messages, []))
    with pytest.raises(ProviderError) as info:
        await ClaudeCodeProvider().complete_text(LOGIN, "m", "", "p", max_tokens=10, timeout_s=5)
    assert info.value.kind is ErrorKind.AUTH
    assert classify_error(info.value) is ErrorKind.AUTH


async def test_claude_code_not_logged_in_result_error_is_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    messages = [FakeResultMessage(is_error=True, result="Not logged in · Please run /login")]
    monkeypatch.setattr(providers_module, "_import_claude_sdk", lambda: fake_sdk(messages, []))
    with pytest.raises(ProviderError) as info:
        await ClaudeCodeProvider().complete_text(LOGIN, "m", "", "p", max_tokens=10, timeout_s=5)
    assert classify_error(info.value) is ErrorKind.AUTH


async def test_claude_code_api_error_status_is_classified(monkeypatch: pytest.MonkeyPatch) -> None:
    messages = [FakeResultMessage(is_error=True, api_error_status=529, result="API Error: overloaded")]
    monkeypatch.setattr(providers_module, "_import_claude_sdk", lambda: fake_sdk(messages, []))
    with pytest.raises(ProviderError) as info:
        await ClaudeCodeProvider().complete_text(LOGIN, "m", "", "p", max_tokens=10, timeout_s=5)
    assert info.value.kind is None
    assert classify_error(info.value) is ErrorKind.OVERLOADED


async def test_claude_code_structured_is_prompted_json(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[Any] = []
    messages = [FakeAssistantMessage([FakeTextBlock('```json\n{"answer": "cc"}\n```')]), FakeResultMessage()]
    monkeypatch.setattr(providers_module, "_import_claude_sdk", lambda: fake_sdk(messages, captured))
    result = await ClaudeCodeProvider().complete_structured(
        LOGIN, "m", Answer, "sys", "q", max_tokens=10, timeout_s=5
    )
    assert result.value == Answer(answer="cc")
    system_prompt = captured[0][1].kwargs["system_prompt"]
    assert system_prompt.startswith("sys\n\nReturn ONLY JSON matching this JSON schema")


# ---------------------------------------------------------------------------------------- packaging


def test_importing_builder_llm_does_not_import_provider_sdks() -> None:
    code = (
        "import sys, builder.llm; "
        "print(','.join(m for m in ('anthropic', 'openai', 'langchain_anthropic', 'langchain_openai', "
        "'claude_agent_sdk', 'langchain_core') if m in sys.modules))"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True, text=True, timeout=120, check=True
    )
    assert completed.stdout.strip() == ""


def test_registry_has_the_three_builtin_providers() -> None:
    assert {"anthropic", "openai", "claude_code"} <= set(PROVIDER_REGISTRY)
    assert isinstance(create_provider("claude_code", {}), ClaudeCodeProvider)
    assert gateway_module.CIRCUIT_FAILURE_THRESHOLD == 3
    assert gateway_module.CIRCUIT_OPEN_S == 120.0


# ------------------------------------------------------------------------- rate limiting integration


class SpyLimiter(RateLimiter):
    """A real limiter (fake clock) that also records every slot request and penalty."""

    def __init__(self, clock: FakeClock, **config: Any) -> None:
        defaults: dict[str, Any] = {
            "max_concurrency": 0,
            "rpm_per_key": 0,
            "tpm_per_key": 0,
            "min_interval_s": 0.0,
        }
        defaults.update(config)
        super().__init__(RateLimitConfig(**defaults), clock=clock.monotonic, sleep=clock.sleep)
        self.slots: list[tuple[str, str, int, float | None]] = []
        self.penalties: list[tuple[str, str, float]] = []

    def slot(self, provider: str, key: Any, est_tokens: int = 0, deadline: float | None = None) -> Any:
        self.slots.append((provider, key.secret, est_tokens, deadline))
        return super().slot(provider, key, est_tokens, deadline)

    def penalize(self, provider: str, key: Any, seconds: float) -> None:
        self.penalties.append((provider, key.secret, seconds))
        super().penalize(provider, key, seconds)


def make_limited_gateway(
    tmp_path: Path,
    providers: dict[str, Any],
    clock: FakeClock,
    *,
    limiter: Any,
    observer: Any = None,
    env: dict[str, str] | None = None,
    trace: Any = None,
    **settings: Any,
) -> LLMGateway:
    return LLMGateway(
        make_settings(tmp_path, **settings),
        trace if trace is not None else RecordingTrace(),
        env=ENV if env is None else env,
        providers=providers,
        sleep=clock.sleep,
        clock=clock.time,
        monotonic=clock.monotonic,
        rng=random.Random(7),
        limiter=limiter,
        observer=observer,
    )


async def test_every_attempt_takes_a_limiter_slot_with_the_estimated_tokens(
    tmp_path: Path, clock: FakeClock
) -> None:
    anthropic = FakeProvider("anthropic", {A1: [quota()]})
    limiter = SpyLimiter(clock)
    gw = make_limited_gateway(tmp_path, {"anthropic": anthropic}, clock, limiter=limiter)
    deadline = clock.monotonic() + 500

    await gw.text(purpose="p", system="S" * 40, user="U" * 80, deadline=deadline)

    # key 1 has no credits -> rotate to key 2: two attempts, two slots. (40 + 80) // 4 + max_tokens (1000) = 1030.
    assert limiter.slots == [("anthropic", A1, 1030, deadline), ("anthropic", A2, 1030, deadline)]
    assert limiter.in_flight == 0


async def test_a_repair_retry_takes_a_new_slot_that_counts_the_repair_text(
    tmp_path: Path, clock: FakeClock
) -> None:
    anthropic = FakeProvider("anthropic", {A1: [ParseError("answer: Field required"), {"answer": "fixed"}]})
    limiter = SpyLimiter(clock)
    gw = make_limited_gateway(tmp_path, {"anthropic": anthropic}, clock, limiter=limiter)

    await gw.structured(purpose="p", schema=Answer, system="sys", user="question")

    first, second = anthropic.calls
    assert [slot[2] for slot in limiter.slots] == [
        (len("sys") + len(first.user)) // 4 + 1000,
        (len("sys") + len(second.user)) // 4 + 1000,
    ]
    assert limiter.slots[1][2] > limiter.slots[0][2]


async def test_limiter_waits_are_traced_as_rate_events_and_not_counted_as_latency(
    tmp_path: Path, clock: FakeClock
) -> None:
    anthropic = FakeProvider("anthropic", clock=clock, latency_s=0.3)
    trace = RecordingTrace()
    limiter = SpyLimiter(clock, min_interval_s=2.0)  # one call start per two seconds on a key
    gw = make_limited_gateway(tmp_path, {"anthropic": anthropic}, clock, limiter=limiter, trace=trace)

    await gw.text(purpose="p", system="", user="u")
    assert [e for e in trace.events if e[0] == "rate"] == []  # nothing to wait for the first time

    clock.advance(0.5)  # 0.8 s since the first start (0.3 of them inside the call) -> 1.2 s still to wait
    await gw.text(purpose="p", system="", user="u")

    rate_events = [e for e in trace.events if e[0] == "rate"]
    assert len(rate_events) == 1
    _, message, data = rate_events[0]
    assert message == "waited 1.2 s for anthropic#1(…1111)"
    assert data == {"purpose": "p", "provider": "anthropic", "waited_s": pytest.approx(1.2)}
    assert [c["latency_s"] for c in trace.model_calls] == [
        0.3,
        0.3,
    ]  # the 1.2 s wait is not part of the latency


async def test_short_waits_are_not_traced(tmp_path: Path, clock: FakeClock) -> None:
    trace = RecordingTrace()
    limiter = SpyLimiter(clock, min_interval_s=0.4)
    gw = make_limited_gateway(
        tmp_path, {"anthropic": FakeProvider("anthropic")}, clock, limiter=limiter, trace=trace
    )

    await gw.text(purpose="p", system="", user="u")
    await gw.text(purpose="p", system="", user="u")

    assert clock.sleeps == [pytest.approx(0.4)]  # it did wait ...
    assert [
        e for e in trace.events if e[0] == "rate"
    ] == []  # ... but 0.4 s is below the 0.5 s trace threshold


async def test_a_429_penalizes_the_key_for_the_retry_after_time(tmp_path: Path, clock: FakeClock) -> None:
    anthropic = FakeProvider("anthropic", {A1: [rate_limited(7)]})
    trace = RecordingTrace()
    limiter = SpyLimiter(clock, rpm_per_key=60)
    gw = make_limited_gateway(tmp_path, {"anthropic": anthropic}, clock, limiter=limiter, trace=trace)

    result = await gw.text(purpose="p", system="", user="u")

    assert result.key_label == "anthropic#2(…2222)"
    assert limiter.penalties == [("anthropic", A1, 7.0)]
    assert limiter.available("anthropic", ApiKey("anthropic", 1, A1))["requests"] == 0.0
    assert limiter.available("anthropic", ApiKey("anthropic", 2, A2))["requests"] == pytest.approx(
        59.0, abs=0.1
    )
    expected = (
        "rate",
        "anthropic#1(…1111) rate limited; no calls to it for 7 s",
        {"purpose": "p", "provider": "anthropic"},
    )
    assert expected in trace.events


async def test_a_429_without_retry_after_penalizes_for_the_cooldown_setting(
    tmp_path: Path, clock: FakeClock
) -> None:
    anthropic = FakeProvider("anthropic", {A1: [rate_limited()]})
    limiter = SpyLimiter(clock)
    gw = make_limited_gateway(
        tmp_path, {"anthropic": anthropic}, clock, limiter=limiter, llm_rate_limit_cooldown_s=12.0
    )

    await gw.text(purpose="p", system="", user="u")

    assert limiter.penalties == [("anthropic", A1, 12.0)]


async def test_other_errors_do_not_penalize(tmp_path: Path, clock: FakeClock) -> None:
    anthropic = FakeProvider("anthropic", {A1: [quota()], A2: [overloaded()]})
    limiter = SpyLimiter(clock)
    gw = make_limited_gateway(tmp_path, {"anthropic": anthropic}, clock, limiter=limiter)

    await gw.text(purpose="p", system="", user="u")

    assert limiter.penalties == []


async def test_a_penalized_key_that_cools_down_in_time_is_not_delayed_twice(
    tmp_path: Path, clock: FakeClock
) -> None:
    anthropic = FakeProvider("anthropic", {A1: [rate_limited(5)]})
    limiter = SpyLimiter(clock, rpm_per_key=60)
    gw = make_limited_gateway(
        tmp_path,
        {"anthropic": anthropic},
        clock,
        limiter=limiter,
        env={"ANTHROPIC_API_KEY": A1},
        llm_providers="anthropic",
    )

    result = await gw.text(purpose="p", system="", user="u")

    assert result.value == "ok from anthropic"
    assert clock.sleeps == [pytest.approx(5.05)]  # the gateway's cooldown wait already covers the 5 s penalty


async def test_waiting_for_the_limiter_past_the_deadline_ends_the_call_without_calling_the_provider(
    tmp_path: Path, clock: FakeClock
) -> None:
    anthropic = FakeProvider("anthropic")
    limiter = SpyLimiter(clock, min_interval_s=100.0)
    gw = make_limited_gateway(tmp_path, {"anthropic": anthropic}, clock, limiter=limiter)
    await gw.text(purpose="p", system="", user="u")  # takes the only call start of the next 100 s

    with pytest.raises(AllProvidersFailed) as info:
        await gw.text(purpose="p", system="", user="u", deadline=clock.monotonic() + 30)

    assert len(anthropic.calls) == 1
    assert info.value.attempts == []
    assert any(r.startswith("deadline reached while waiting for the rate limit") for r in info.value.reasons)
    assert clock.sleeps == []  # it refused to sleep instead of sleeping and failing anyway


async def test_without_a_deadline_the_limiter_just_waits(tmp_path: Path, clock: FakeClock) -> None:
    limiter = SpyLimiter(clock, rpm_per_key=1)
    gw = make_limited_gateway(
        tmp_path,
        {"anthropic": FakeProvider("anthropic")},
        clock,
        limiter=limiter,
        env={"ANTHROPIC_API_KEY": A1},
    )

    await gw.text(purpose="p", system="", user="u")
    await gw.text(purpose="p", system="", user="u")

    assert clock.sleeps == [60.0]  # one request per minute on that key


async def test_the_reported_token_usage_replaces_the_estimate(tmp_path: Path, clock: FakeClock) -> None:
    limiter = SpyLimiter(clock, tpm_per_key=6000)
    gw = make_limited_gateway(
        tmp_path,
        {"anthropic": FakeProvider("anthropic")},
        clock,
        limiter=limiter,
        env={"ANTHROPIC_API_KEY": A1},
    )

    await gw.text(
        purpose="p", system="", user="u"
    )  # estimate 1000 + 0; the fake provider reports 11 + 7 tokens

    assert limiter.available("anthropic", ApiKey("anthropic", 1, A1))["tokens"] == pytest.approx(5982.0)


def test_the_gateway_builds_its_limiter_from_the_settings(tmp_path: Path) -> None:
    provider = {"anthropic": FakeProvider("anthropic")}
    configured = LLMGateway(
        make_settings(
            tmp_path,
            llm_max_concurrency=3,
            llm_rpm_per_key=7,
            llm_tpm_per_key=9000,
            llm_min_interval_s=0.1,
            llm_rate_overrides="openai=1:2",
        ),
        None,
        env=ENV,
        providers=provider,
    )
    plain = LLMGateway(make_settings(tmp_path), None, env=ENV, providers=provider)
    switched_off = LLMGateway(
        make_settings(tmp_path, llm_rpm_per_key=7), None, env=ENV, providers=provider, limiter=False
    )

    assert configured.limiter is not None
    assert configured.limiter.config == RateLimitConfig(3, 7, 9000, 0.1, {"openai": (1, 2)})
    assert plain.limiter is None  # ad-hoc settings without any rate attribute: unlimited, as before
    assert switched_off.limiter is None


def test_the_real_settings_give_the_documented_defaults(tmp_path: Path) -> None:
    real = Settings(_env_file=None, llm_key_health_file=tmp_path / "health.json")

    gw = LLMGateway(real, None, env=ENV, providers={"anthropic": FakeProvider("anthropic")})

    assert gw.limiter is not None
    assert gw.limiter.config == RateLimitConfig(2, 30, 150_000, 0.5, {})


# ------------------------------------------------------------------------- observability integration


class FakeObserver:
    """Records ``log_generation`` calls; spans are named after their node."""

    def __init__(self) -> None:
        self.generations: list[dict[str, Any]] = []
        self.secrets: list[str] = []
        self.ended: list[tuple[Any, ...]] = []

    def start_run(self, *args: Any, **kwargs: Any) -> None:
        return None

    def start_span(self, name: str, metadata: Any = None) -> str:
        return f"span-{name}"

    def end_span(self, span_id: Any, output_summary: Any = None, error: Any = None) -> None:
        self.ended.append((span_id, output_summary, error))

    def log_generation(self, **fields: Any) -> None:
        self.generations.append(fields)

    def end_run(self, *args: Any, **kwargs: Any) -> None:
        return None

    def flush(self) -> None:
        return None

    def add_secrets(self, *secrets: str) -> None:
        self.secrets.extend(secrets)


async def test_one_generation_is_logged_per_attempt_including_failures(
    tmp_path: Path, clock: FakeClock
) -> None:
    anthropic = FakeProvider("anthropic", {A1: [auth(A1)], A2: [overloaded()]})
    observer = FakeObserver()
    gw = make_limited_gateway(tmp_path, {"anthropic": anthropic}, clock, limiter=False, observer=observer)

    result = await gw.text(purpose="understand brief", system="be brief", user="what is 2+2?")

    assert len(result.attempts) == len(observer.generations) == 3
    assert [(g["attempt"], g["ok"], g["error_kind"], g["key_label"]) for g in observer.generations] == [
        (1, False, "auth", "anthropic#1(…1111)"),
        (2, False, "overloaded", "anthropic#2(…2222)"),
        (3, True, None, "anthropic#2(…2222)"),
    ]
    for generation, call in zip(observer.generations, anthropic.calls, strict=True):
        assert generation["system"] == call.system == "be brief"
        assert generation["user"] == call.user == "what is 2+2?"
        assert (generation["purpose"], generation["provider"], generation["model"]) == (
            "understand brief",
            "anthropic",
            "anthropic-strong",
        )
    success = observer.generations[2]
    assert success["output"] == "ok from anthropic"
    assert (success["input_tokens"], success["output_tokens"]) == (11, 7)
    assert observer.generations[0]["output"] is None
    assert "Incorrect API key" in observer.generations[0]["error"]
    assert A1 not in json.dumps(observer.generations, default=str)  # the error text arrives already scrubbed


async def test_structured_attempts_log_the_raw_reply_and_the_repair_prompt_that_was_sent(
    tmp_path: Path, clock: FakeClock
) -> None:
    anthropic = FakeProvider("anthropic", {A1: [{"wrong": 1}, {"answer": "fixed"}]})
    observer = FakeObserver()
    gw = make_limited_gateway(tmp_path, {"anthropic": anthropic}, clock, limiter=False, observer=observer)

    result = await gw.structured(purpose="extract IR", schema=Answer, system="sys", user="question")

    assert result.value == Answer(answer="fixed")
    bad, good = observer.generations
    assert (bad["ok"], bad["error_kind"], bad["output"], bad["user"]) == (
        False,
        "parse",
        '{"wrong": 1}',
        "question",
    )
    assert good["ok"] is True and good["output"] == '{"answer": "fixed"}'
    assert good["user"] == anthropic.calls[1].user  # exactly what the provider got, repair note included
    assert good["user"].startswith("question") and "could not be used because" in good["user"]


async def test_observer_failures_never_reach_the_caller(tmp_path: Path, clock: FakeClock) -> None:
    class Broken(FakeObserver):
        def log_generation(self, **fields: Any) -> None:
            raise RuntimeError("langfuse exploded")

        def add_secrets(self, *secrets: str) -> None:
            raise RuntimeError("no secrets for you")

    anthropic = FakeProvider("anthropic", {A1: [quota()]})
    gw = make_limited_gateway(tmp_path, {"anthropic": anthropic}, clock, limiter=False, observer=Broken())

    result = await gw.text(purpose="p", system="", user="u")

    assert result.value == "ok from anthropic" and len(result.attempts) == 2


async def test_generations_carry_the_span_of_the_graph_node_that_asked(
    tmp_path: Path, clock: FakeClock
) -> None:
    observer = FakeObserver()
    gw = make_limited_gateway(
        tmp_path, {"anthropic": FakeProvider("anthropic")}, clock, limiter=False, observer=observer
    )

    with observed_span(observer, "extract_policy"):
        await gw.text(purpose="inside", system="", user="u")
    await gw.text(purpose="outside", system="", user="u")

    assert [(g["purpose"], g["span_id"]) for g in observer.generations] == [
        ("inside", "span-extract_policy"),
        ("outside", None),
    ]


def test_the_observer_is_told_the_keys_the_gateway_knows(tmp_path: Path, clock: FakeClock) -> None:
    observer = FakeObserver()
    gw = make_limited_gateway(
        tmp_path, {"anthropic": FakeProvider("anthropic")}, clock, limiter=False, observer=observer
    )
    assert set(observer.secrets) >= {A1, A2, O1}

    later = FakeObserver()
    gw.observer = later  # an observer set after construction is told as well
    assert gw.observer is later and set(later.secrets) >= {A1, A2, O1}


def test_build_gateway_passes_the_observer_and_limiter_on(tmp_path: Path) -> None:
    observer = FakeObserver()

    gw = build_gateway(
        make_settings(tmp_path, llm_providers="anthropic"), None, observer=observer, limiter=False
    )

    assert gw.observer is observer and gw.limiter is None


def test_the_gateway_discovers_keys_from_the_dotenv_that_paths_points_to(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REPO-16: key discovery follows ``paths.ENV_FILE``, so the hermetic fixture also covers the gateway."""
    from builder import paths

    fake = "sk-ant-fake-from-test-dotenv-0001"
    env_file = tmp_path / "test.env"
    env_file.write_text(f"ANTHROPIC_API_KEY={fake}" + chr(10), encoding="utf-8")
    monkeypatch.setattr(paths, "ENV_FILE", env_file)

    gw = build_gateway(make_settings(tmp_path, llm_providers="anthropic"), None, limiter=False)

    assert any(key.secret == fake for key in gw.key_pool("anthropic").keys)
    assert len(gw.key_pool("anthropic").keys) == 1  # and nothing from the developer's real .env


async def test_the_jsonl_log_has_the_full_prompt_and_reply_and_no_secrets(
    tmp_path: Path, clock: FakeClock
) -> None:
    key1, key2 = (
        "plain-secret-key-0001",
        "plain-secret-key-0002",
    )  # not key-shaped: only the gateway knows them
    anthropic = FakeProvider("anthropic", {key1: [auth(key1)], key2: [f"the key was {key2}, as asked"]})
    path = tmp_path / "traces" / "slot.llm.jsonl"
    gw = make_limited_gateway(
        tmp_path,
        {"anthropic": anthropic},
        clock,
        limiter=False,
        observer=JsonlObserver(path),
        env={"ANTHROPIC_API_KEY": key1, "ANTHROPIC_API_KEY_2": key2},
        llm_providers="anthropic",
    )
    system = "You extract rules. " * 500  # 9 500 characters
    user = (
        "Brief: " + "Pay the full amount by Friday. " * 1000 + f"My key is {key1}"
    )  # about 31 000 characters

    await gw.text(purpose="understand brief", system=system, user=user)

    text = path.read_text(encoding="utf-8")
    assert key1 not in text and key2 not in text
    failed, ok = read_llm_log(path)
    assert (failed["ok"], failed["attempt"], failed["error_kind"]) == (False, 1, "auth")
    assert (ok["ok"], ok["attempt"]) == (True, 2)
    assert ok["system"] == system  # complete, not truncated
    assert ok["user"] == user.replace(key1, "***")
    assert len(ok["user"]) > 30_000
    assert ok["output"] == "the key was ***, as asked"
    assert failed["output"] == "" and key1 not in failed["error"]
