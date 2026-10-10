"""LLMGateway - the key changer every builder LLM call goes through.

The user's requirement: "if ANTHROPIC doesn't have the credits, then it should change to openai ... it should
try it then try other api key". The gateway implements that as a fallback chain with two levels of rotation:

* **keys within a provider** - every provider has a :class:`builder.llm.keys.KeyPool`; a key with no credits
  or an invalid key is marked DEAD (and remembered in ``data/key_health.json`` for the next run), a
  rate-limited key COOLS down, and the next key of the same provider is tried;
* **providers in a chain** - configured by ``LLM_PROVIDERS`` (default ``anthropic,openai,claude_code``); a
  provider is left when it has no usable key, when its circuit breaker is open (3 consecutive provider-level
  failures -> open for 120 s) or when the request itself is rejected (bad request / context too long /
  structured output unparseable twice).

Algorithm of one call (``text`` or ``structured``)::

    repeat (bounded):
        for provider in configured order (skipping providers finished for this call):
            while the provider's circuit allows and it has a usable key:
                for attempt on that key (1 + llm_retries_per_key for transient errors, 1 repair for PARSE):
                    run the provider with timeout = min(llm_timeout_s, time left before the deadline)
                    trace.model_call(...)                      # one MODEL line per attempt
                    success -> mark key ok, close circuit, return LLMCallResult
                    failure -> classify_error -> POLICY: retry same key (backoff 1, 2, 4 s + jitter) |
                               rotate key (dead / cooling / exhausted) | next provider
        if only cooling keys remain and the soonest cooldown <= llm_max_cooldown_wait_s: sleep, repeat
        else if keys were skipped only because a previous run marked them dead: revive them once, repeat
    raise AllProvidersFailed(attempts)

Waiting for a cooldown happens only when *no* provider can answer right now, so a rate-limited Anthropic key
never blocks a healthy OpenAI key. A ``deadline`` (``time.monotonic()`` value, normally the build budget's
deadline) caps each attempt's timeout and stops new attempts and waits once too little time is left.

Every attempt is written to the trace via ``trace.model_call`` (provider, model, masked key label, purpose,
latency, tokens, error kind) - never a secret. The trace object only needs ``model_call`` and ``event``
(:class:`TraceSink`); ``builder.trace.TraceWriter`` satisfies it.

Rate limiting (:mod:`builder.llm.ratelimit`): every attempt first waits for a slot of the
:class:`~builder.llm.ratelimit.RateLimiter` (global concurrency cap plus per-key requests / tokens per minute and
pacing); a wait longer than 0.5 s is traced as a ``RATE`` event, a waiting time that would pass the call's deadline
ends the call like any other deadline, and a 429 penalises the key's buckets for ``Retry-After`` (or the cooldown
setting).

Observability (:mod:`builder.observability`): when an ``observer`` is set, every attempt - success *and* failure -
is reported with the exact ``system`` / ``user`` text sent to the provider and the raw reply (or the parse error's
raw text). The observer redacts secrets itself and its failures are swallowed.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Generic, NamedTuple, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from builder.llm.errors import (
    POLICY,
    Action,
    AllProvidersFailed,
    ErrorKind,
    ErrorPolicy,
    KeyEffect,
    ParseError,
    classify_error,
    describe_error,
    retry_after_seconds,
)
from builder.llm.keys import (
    ApiKey,
    HealthStore,
    KeyPool,
    KeyStatus,
    default_env,
    discover_keys,
    llm_secrets,
)
from builder.llm.providers import PROVIDER_REGISTRY, LLMRole, Provider, create_provider
from builder.llm.ratelimit import RateLimiter, RateLimitTimeout, estimate_tokens
from builder.llm.structured import repair_instruction, summarize_validation_error
from builder.observability.base import LLMObserver, current_span_id

logger = logging.getLogger(__name__)

__all__ = [
    "BACKOFF_BASE_S",
    "CIRCUIT_FAILURE_THRESHOLD",
    "CIRCUIT_OPEN_S",
    "AttemptRecord",
    "CircuitBreaker",
    "CircuitState",
    "GatewayConfig",
    "LLMCallResult",
    "LLMGateway",
    "TraceSink",
    "build_gateway",
    "parse_provider_order",
]

T = TypeVar("T")
ModelT = TypeVar("ModelT", bound=BaseModel)

CIRCUIT_FAILURE_THRESHOLD = 3
"""Consecutive provider-level failures (overloaded / 5xx / timeout / connection / unknown) that open a circuit."""
CIRCUIT_OPEN_S = 120.0
"""How long an open circuit keeps a provider out of the chain before one trial call is allowed."""
BACKOFF_BASE_S = 1.0
"""Same-key retry backoff: 1, 2, 4 ... seconds."""
BACKOFF_JITTER = 0.25
"""Random extra delay, as a fraction of the backoff step (0-25 %)."""
MIN_ATTEMPT_TIMEOUT_S = 10.0
"""No attempt is started with less time than this before the deadline."""
TIMEOUT_GRACE_S = 5.0
"""Extra time our ``asyncio.wait_for`` allows on top of the SDK's own timeout."""
MAX_COOLDOWN_WAITS = 3
"""Maximum number of "everything is cooling, wait" pauses within one call."""
MAX_ATTEMPTS_PER_CALL = 40
"""Hard safety cap on attempts within one call."""
RATE_TRACE_MIN_WAIT_S = 0.5
"""Waits for the client-side rate limiter longer than this are written to the trace as ``RATE`` events."""
_DEFAULT_ORDER = ("anthropic", "openai", "claude_code")


# --------------------------------------------------------------------------------------------------------------
# Trace protocol
# --------------------------------------------------------------------------------------------------------------


class TraceSink(Protocol):
    """The part of ``builder.trace.TraceWriter`` the gateway uses."""

    def model_call(
        self,
        provider: str,
        model: str,
        key_label: str,
        purpose: str,
        ok: bool,
        latency_s: float,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        error_kind: str | None = None,
        error: str | None = None,
        attempt: int | None = None,
    ) -> None:
        """Record one model attempt (one MODEL line in the trace)."""
        ...

    def event(self, kind: str, message: str, **data: Any) -> None:
        """Record a free-form event (key rotation, cooldown wait, circuit open ...)."""
        ...


class _NullSink:
    """Trace sink that drops everything (used when ``trace=None``)."""

    def model_call(self, *args: Any, **kwargs: Any) -> None:
        return None

    def event(self, kind: str, message: str, **data: Any) -> None:
        return None


# --------------------------------------------------------------------------------------------------------------
# Value objects
# --------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AttemptRecord:
    """One attempt of one gateway call (also what ``AllProvidersFailed.attempts`` holds).

    Attributes:
        provider: Provider name.
        model: Model id used.
        key_label: Masked key label (``anthropic#1(…f3a2)``).
        ok: Whether the attempt succeeded.
        error_kind: Classified failure kind (``None`` on success).
        error: Short, secret-free error description (``None`` on success).
        latency_s: Wall time of the attempt.
        attempt: 1-based attempt number within the call.
        action: What the gateway did next (``retry_same`` / ``rotate_key`` / ``next_provider``), failures only.
    """

    provider: str
    model: str
    key_label: str
    ok: bool
    error_kind: ErrorKind | None
    error: str | None
    latency_s: float
    attempt: int = 0
    action: str | None = None


@dataclass(frozen=True)
class LLMCallResult(Generic[T]):
    """Successful gateway call: the value plus where it came from and what it cost."""

    value: T
    provider: str
    model: str
    key_label: str
    attempts: list[AttemptRecord]
    latency_s: float
    input_tokens: int | None = None
    output_tokens: int | None = None
    purpose: str = ""


@dataclass(frozen=True, slots=True)
class GatewayConfig:
    """The settings the gateway reads, with defaults (see ``builder.settings.Settings`` for the env names)."""

    providers: tuple[str, ...] = _DEFAULT_ORDER
    timeout_s: float = 300.0
    max_tokens: int = 16000
    retries_per_key: int = 2
    rate_limit_cooldown_s: float = 30.0
    max_cooldown_wait_s: float = 45.0
    key_health_file: Path | None = None
    dead_key_ttl_s: float = 21600.0

    @classmethod
    def from_settings(cls, settings: Any) -> GatewayConfig:
        """Read ``llm_*`` attributes from any settings object; missing ones keep the defaults.

        A missing ``llm_key_health_file`` attribute disables persistence (so ad-hoc settings objects in tests
        never write into the repository).
        """
        defaults = cls()

        def get(name: str, default: Any) -> Any:
            value = getattr(settings, name, None)
            return default if value is None else value

        health = getattr(settings, "llm_key_health_file", None)
        return cls(
            providers=parse_provider_order(get("llm_providers", ",".join(defaults.providers))),
            timeout_s=float(get("llm_timeout_s", defaults.timeout_s)),
            max_tokens=int(get("llm_max_tokens", defaults.max_tokens)),
            retries_per_key=max(0, int(get("llm_retries_per_key", defaults.retries_per_key))),
            rate_limit_cooldown_s=float(get("llm_rate_limit_cooldown_s", defaults.rate_limit_cooldown_s)),
            max_cooldown_wait_s=float(get("llm_max_cooldown_wait_s", defaults.max_cooldown_wait_s)),
            key_health_file=Path(health) if health else None,
            dead_key_ttl_s=float(get("llm_dead_key_ttl_s", defaults.dead_key_ttl_s)),
        )


def parse_provider_order(value: str | Iterable[str]) -> tuple[str, ...]:
    """Normalise ``LLM_PROVIDERS`` (``"anthropic, openai claude-code"`` -> ``("anthropic", "openai", "claude_code")``).

    Separators: comma, semicolon, whitespace. Names are lower-cased, ``-`` becomes ``_``, duplicates dropped.
    """
    items = value.replace(";", ",").replace(" ", ",").split(",") if isinstance(value, str) else list(value)
    order: list[str] = []
    for item in items:
        name = str(item).strip().lower().replace("-", "_")
        if name and name not in order:
            order.append(name)
    return tuple(order)


# --------------------------------------------------------------------------------------------------------------
# Circuit breaker
# --------------------------------------------------------------------------------------------------------------


class CircuitState(StrEnum):
    """Classic circuit-breaker states."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """Keeps a failing provider out of the chain for a while (per provider, shared by all calls).

    CLOSED -> (``threshold`` consecutive provider-level failures) -> OPEN -> (``open_s`` elapsed) -> HALF_OPEN:
    one trial is allowed; success closes the circuit, failure re-opens it.
    """

    def __init__(
        self,
        threshold: int = CIRCUIT_FAILURE_THRESHOLD,
        open_s: float = CIRCUIT_OPEN_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Create a closed breaker (``clock`` must be monotonic seconds)."""
        self.threshold = threshold
        self.open_s = open_s
        self._clock = clock
        self._state = CircuitState.CLOSED
        self._failures = 0
        self._opened_at = 0.0

    @property
    def state(self) -> CircuitState:
        """Current state (an OPEN circuit whose time is up reports HALF_OPEN)."""
        if self._state is CircuitState.OPEN and self._clock() - self._opened_at >= self.open_s:
            return CircuitState.HALF_OPEN
        return self._state

    @property
    def is_open(self) -> bool:
        """True while calls must not be sent to the provider."""
        return self.state is CircuitState.OPEN

    def remaining_open_s(self) -> float:
        """Seconds until an open circuit allows a trial call (0 when not open)."""
        if not self.is_open:
            return 0.0
        return max(0.0, self.open_s - (self._clock() - self._opened_at))

    def allow(self) -> bool:
        """Whether a call may be sent now (moves OPEN -> HALF_OPEN once the open period is over)."""
        state = self.state
        if state is CircuitState.HALF_OPEN:
            self._state = CircuitState.HALF_OPEN
        return state is not CircuitState.OPEN

    def record_success(self) -> None:
        """A call succeeded: close the circuit and reset the failure count."""
        self._state = CircuitState.CLOSED
        self._failures = 0

    def record_failure(self) -> None:
        """A provider-level failure: open the circuit at the threshold, or immediately when half-open."""
        self._failures += 1
        if self.state is CircuitState.HALF_OPEN or self._failures >= self.threshold:
            self._state = CircuitState.OPEN
            self._opened_at = self._clock()


# --------------------------------------------------------------------------------------------------------------
# Gateway
# --------------------------------------------------------------------------------------------------------------

Invoker = Callable[[Provider, ApiKey, str, float, str | None], Awaitable[tuple[Any, int | None, int | None]]]
"""``(provider, key, model, timeout_s, repair_note) -> (value, input_tokens, output_tokens)``."""


@dataclass
class _CallSpec:
    purpose: str
    role: LLMRole
    deadline: float | None
    invoke: Invoker
    repair_note: Callable[[BaseException], str]
    only_provider: str | None = None
    system: str = ""
    user: str = ""
    max_tokens: int = 0
    raw_output: str | None = None  # set by ``invoke`` with the provider's raw reply (observability)


@dataclass
class _Run:
    """Timing facts of the attempt that is running (filled in by :meth:`LLMGateway._run_attempt`)."""

    started: float
    waited_s: float = 0.0


@dataclass
class _CallState:
    started: float
    attempts: list[AttemptRecord] = field(default_factory=list)
    finished: set[str] = field(default_factory=set)
    excluded: dict[str, list[ApiKey]] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    waits: int = 0
    revived: bool = False
    stopped: bool = False

    def note(self, reason: str) -> None:
        if reason not in self.reasons:
            self.reasons.append(reason)


class _KeyOutcome(NamedTuple):
    action: Action | None
    result: LLMCallResult[Any] | None


class LLMGateway:
    """Multi-key, multi-provider LLM client with error classification, circuit breakers and tracing."""

    def __init__(
        self,
        settings: Any,
        trace: TraceSink | None,
        env: Mapping[str, str] | None = None,
        providers: Mapping[str, Provider] | None = None,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        rng: random.Random | None = None,
        limiter: RateLimiter | bool | None = None,
        observer: LLMObserver | None = None,
    ) -> None:
        """Discover keys, build pools / breakers / providers in the configured order.

        Args:
            settings: ``builder.settings.Settings`` (or any object with the ``llm_*`` attributes).
            trace: Where every attempt is recorded (``TraceWriter``); ``None`` drops events.
            env: Environment to read keys from; default ``.env`` + ``os.environ``.
            providers: Pre-built providers by name (tests / custom adapters). When given, only these are used,
                in the configured order (or their own order if none is configured).
            sleep: Async sleep used for backoff and cooldown waits (injectable for tests).
            clock: Epoch-seconds clock for key cooldowns and the health file.
            monotonic: Monotonic clock for latency, deadlines and circuit breakers.
            rng: Random source for backoff jitter.
            limiter: The client-side :class:`~builder.llm.ratelimit.RateLimiter` every attempt waits on. ``None``
                (default) builds one from the ``llm_max_concurrency`` / ``llm_rpm_per_key`` / ``llm_tpm_per_key`` /
                ``llm_min_interval_s`` / ``llm_rate_overrides`` settings - and none when ``settings`` defines none
                of them (ad-hoc settings objects stay unlimited). ``False`` switches rate limiting off.
            observer: Receives one ``log_generation`` per attempt (success and failure) with the exact prompt sent
                and the raw reply (:mod:`builder.observability`). Can also be set later via :attr:`observer`.
        """
        self._settings = settings
        self.config = GatewayConfig.from_settings(settings)
        self._trace: TraceSink = trace if trace is not None else _NullSink()
        self._env: dict[str, str] = dict(default_env(_env_file_of(settings)) if env is None else env)
        self._sleep = sleep
        self._clock = clock
        self._monotonic = monotonic
        self._rng = rng or random.Random()
        self._store = (
            HealthStore(self.config.key_health_file, self.config.dead_key_ttl_s, clock)
            if self.config.key_health_file
            else None
        )
        self._providers: dict[str, Provider] = self._build_providers(providers)
        self._order: tuple[str, ...] = tuple(self._providers)
        self._pools: dict[str, KeyPool] = {
            name: KeyPool(name, self._discover(name, provider), self._store, clock)
            for name, provider in self._providers.items()
        }
        self._breakers: dict[str, CircuitBreaker] = {
            name: CircuitBreaker(clock=monotonic) for name in self._order
        }
        self._secrets: list[str] = self._collect_secrets()
        self._limiter: RateLimiter | None = (
            None
            if limiter is False
            else limiter
            if isinstance(limiter, RateLimiter)
            else RateLimiter.from_settings(settings, monotonic, sleep)
        )
        self._observer: LLMObserver | None = None
        self.observer = observer
        logger.info(
            "LLM key changer ready: %s",
            ", ".join(
                f"{n}[{', '.join(k.label for k in self._pools[n].keys) or 'no keys'}]" for n in self._order
            )
            or "no providers",
        )

    # ---------------------------------------------------------------------------------------------- public API

    @property
    def order(self) -> tuple[str, ...]:
        """Provider names in fallback order."""
        return self._order

    @property
    def limiter(self) -> RateLimiter | None:
        """The client-side rate limiter (``None`` when rate limiting is off)."""
        return self._limiter

    @property
    def observer(self) -> LLMObserver | None:
        """Where every attempt is reported (``None`` = nobody)."""
        return self._observer

    @observer.setter
    def observer(self, observer: LLMObserver | None) -> None:
        self._observer = observer
        add_secrets = getattr(observer, "add_secrets", None)
        if callable(add_secrets):  # let the observer mask the keys this gateway discovered
            try:
                add_secrets(*self._secrets)
            except Exception:
                logger.warning("observer.add_secrets failed", exc_info=True)

    def key_pool(self, provider: str) -> KeyPool:
        """The :class:`KeyPool` of ``provider`` (diagnostics and tests)."""
        return self._pools[provider]

    def secret_values(self) -> list[str]:
        """Every credential the gateway knows (for trace redaction); never includes the ``login`` pseudo key."""
        return list(self._secrets)

    def describe(self) -> dict[str, Any]:
        """Secret-free description of the chain: order, models, key states and circuit states."""
        providers: dict[str, Any] = {}
        for name in self._order:
            provider = self._providers[name]
            providers[name] = {
                "models": {role.value: provider.model_for(role, self._settings) for role in LLMRole},
                "keys": self._pools[name].snapshot(),
                "circuit": self._breakers[name].state.value,
                "available": name in self.available_providers(),
            }
        return {
            "order": list(self._order),
            "providers": providers,
            "key_health_file": str(self.config.key_health_file) if self.config.key_health_file else None,
        }

    def available_providers(self) -> list[str]:
        """Providers that currently have a non-dead key and a closed (or half-open) circuit."""
        return [
            name
            for name in self._order
            if self._pools[name].has_live_keys() and not self._breakers[name].is_open
        ]

    async def text(
        self,
        *,
        purpose: str,
        system: str,
        user: str,
        role: LLMRole = LLMRole.STRONG,
        max_tokens: int | None = None,
        deadline: float | None = None,
    ) -> LLMCallResult[str]:
        """Plain-text completion through the key changer.

        Args:
            purpose: One-line purpose written to the trace for every attempt (hackathon trace requirement).
            system: System prompt (may be empty).
            user: User message.
            role: STRONG or FAST model tier.
            max_tokens: Output cap; default ``settings.llm_max_tokens``.
            deadline: ``time.monotonic()`` value after which no attempt is started.

        Raises:
            AllProvidersFailed: No provider / key produced an answer.
        """
        tokens = max_tokens or self.config.max_tokens

        async def invoke(
            provider: Provider, key: ApiKey, model: str, timeout_s: float, repair: str | None
        ) -> tuple[str, int | None, int | None]:
            result = await provider.complete_text(
                key, model, system, user + (repair or ""), max_tokens=tokens, timeout_s=timeout_s
            )
            spec.raw_output = result.text
            if not result.text.strip():
                raise ParseError("empty reply", raw_text=result.text)
            return result.text, result.input_tokens, result.output_tokens

        spec = _CallSpec(
            purpose,
            LLMRole(role),
            deadline,
            invoke,
            _text_repair_note,
            system=system,
            user=user,
            max_tokens=tokens,
        )
        return await self._execute(spec)

    async def structured(
        self,
        *,
        purpose: str,
        schema: type[ModelT],
        system: str,
        user: str,
        role: LLMRole = LLMRole.STRONG,
        max_tokens: int | None = None,
        deadline: float | None = None,
    ) -> LLMCallResult[ModelT]:
        """Structured completion: the result ``value`` is a validated instance of ``schema``.

        A reply that cannot be parsed is retried once on the same key with a repair message (the parse error is
        quoted to the model), then the next provider is tried.

        Raises:
            AllProvidersFailed: No provider / key produced a valid answer.
        """
        tokens = max_tokens or self.config.max_tokens

        async def invoke(
            provider: Provider, key: ApiKey, model: str, timeout_s: float, repair: str | None
        ) -> tuple[ModelT, int | None, int | None]:
            result = await provider.complete_structured(
                key, model, schema, system, user + (repair or ""), max_tokens=tokens, timeout_s=timeout_s
            )
            spec.raw_output = result.raw_text
            return _as_model(result.value, schema, result.raw_text), result.input_tokens, result.output_tokens

        spec = _CallSpec(
            purpose,
            LLMRole(role),
            deadline,
            invoke,
            _structured_repair_note,
            system=system,
            user=user,
            max_tokens=tokens,
        )
        return await self._execute(spec)

    async def probe(self) -> dict[str, dict[str, Any]]:
        """One tiny call per provider (diagnostics only; the build never calls it).

        Returns:
            ``{provider: {"ok", "model", "key", "latency_s", "attempts", "error_kind", "error"}}``.
        """
        report: dict[str, dict[str, Any]] = {}

        async def invoke(
            provider: Provider, key: ApiKey, model: str, timeout_s: float, repair: str | None
        ) -> tuple[str, int | None, int | None]:
            result = await provider.complete_text(
                key,
                model,
                "",
                "Reply with the single word OK." + (repair or ""),
                max_tokens=16,
                timeout_s=timeout_s,
            )
            return result.text, result.input_tokens, result.output_tokens

        for name in self._order:
            spec = _CallSpec(
                f"probe {name}",
                LLMRole.FAST,
                self._monotonic() + 120.0,
                invoke,
                _text_repair_note,
                only_provider=name,
                user="Reply with the single word OK.",
                max_tokens=16,
            )
            try:
                result = await self._execute(spec)
            except AllProvidersFailed as exc:
                last = exc.attempts[-1] if exc.attempts else None
                report[name] = {
                    "ok": False,
                    "attempts": len(exc.attempts),
                    "error_kind": last.error_kind.value if last and last.error_kind else None,
                    "error": str(exc)[:400],
                }
                continue
            report[name] = {
                "ok": True,
                "model": result.model,
                "key": result.key_label,
                "latency_s": round(result.latency_s, 3),
                "attempts": len(result.attempts),
            }
        return report

    # ---------------------------------------------------------------------------------------------- the loop

    async def _execute(self, spec: _CallSpec) -> LLMCallResult[Any]:
        state = _CallState(started=self._monotonic())
        while True:
            result = await self._round(spec, state)
            if result is not None:
                return result
            if state.stopped:
                break
            wait = self._cooldown_wait(spec, state)
            if wait is not None:
                state.waits += 1
                self._event(f"all usable keys are cooling; waiting {wait:.1f}s", purpose=spec.purpose)
                await self._sleep(wait)
                continue
            if not state.revived:
                state.revived = True
                if self._revive_persisted(spec):
                    continue
            break
        raise AllProvidersFailed(spec.purpose, state.attempts, state.reasons)

    async def _round(self, spec: _CallSpec, state: _CallState) -> LLMCallResult[Any] | None:
        for name in self._call_order(spec):
            if name in state.finished:
                continue
            result = await self._try_provider(name, spec, state)
            if result is not None:
                return result
            if state.stopped:
                return None
        return None

    async def _try_provider(self, name: str, spec: _CallSpec, state: _CallState) -> LLMCallResult[Any] | None:
        pool, breaker = self._pools[name], self._breakers[name]
        if len(pool) == 0:
            state.note(f"{name}: no keys configured")
            state.finished.add(name)
            return None
        excluded = state.excluded.setdefault(name, [])
        while True:
            if not breaker.allow():
                state.note(f"{name}: circuit open ({breaker.remaining_open_s():.0f}s left)")
                return None
            key = pool.next_key(exclude=excluded)
            if key is None:
                state.note(
                    f"{name}: {'no usable key left' if not pool.has_live_keys() else 'keys cooling or exhausted'}"
                )
                return None
            outcome = await self._try_key(name, key, spec, state)
            if outcome.result is not None:
                return outcome.result
            if outcome.action is None:  # deadline / attempt cap
                return None
            if outcome.action is Action.NEXT_PROVIDER:
                state.finished.add(name)
                return None
            if pool.status(key) is KeyStatus.ACTIVE:  # exhausted retries / unknown error: not again this call
                excluded.append(key)
            self._event(f"{name}: rotating away from {key.label}", purpose=spec.purpose)

    async def _try_key(self, name: str, key: ApiKey, spec: _CallSpec, state: _CallState) -> _KeyOutcome:
        provider, pool, breaker = self._providers[name], self._pools[name], self._breakers[name]
        model = provider.model_for(spec.role, self._settings)
        # Same-key retry budgets per policy: the transient kinds share one policy (and so one budget),
        # PARSE has its own, so an earlier overload never eats the repair retry.
        retries_used: dict[ErrorPolicy, int] = {}
        repair: str | None = None
        while True:
            timeout_s = self._attempt_timeout(spec.deadline)
            if timeout_s is None:
                state.note("deadline reached")
                state.stopped = True
                return _KeyOutcome(None, None)
            if len(state.attempts) >= MAX_ATTEMPTS_PER_CALL:
                state.note(f"attempt cap ({MAX_ATTEMPTS_PER_CALL}) reached")
                state.stopped = True
                return _KeyOutcome(None, None)
            attempt_no = len(state.attempts) + 1
            sent_user = spec.user + (repair or "")
            spec.raw_output = None
            run = _Run(started=self._monotonic())
            try:
                value, input_tokens, output_tokens = await self._run_attempt(
                    spec, name, provider, key, model, timeout_s, repair, run
                )
            except asyncio.CancelledError:
                raise
            except RateLimitTimeout as exc:  # the rate limiter would make us wait past the deadline
                state.note(f"deadline reached while waiting for the rate limit ({exc})")
                state.stopped = True
                return _KeyOutcome(None, None)
            except Exception as exc:
                latency = self._monotonic() - run.started
                kind = classify_error(exc)
                policy = POLICY[kind]
                error_text = describe_error(exc, secrets=self._secrets)
                pool.mark_failure(key, kind.value)
                if policy.provider_failure:
                    breaker.record_failure()
                action = self._next_action(policy, retries_used.get(policy, 0), breaker)
                self._record(
                    state,
                    spec.purpose,
                    AttemptRecord(
                        name, model, key.label, False, kind, error_text, latency, attempt_no, action.value
                    ),
                )
                self._observe(
                    spec,
                    name,
                    model,
                    key,
                    attempt_no,
                    sent_user,
                    ok=False,
                    latency=latency,
                    output=spec.raw_output or getattr(exc, "raw_text", None),
                    kind=kind,
                    error=error_text,
                )
                if kind is ErrorKind.RATE_LIMIT:
                    self._penalize(name, key, exc, spec.purpose)
                self._apply_key_effect(pool, key, kind, policy, exc, error_text)  # after the MODEL line
                logger.info("LLM %r: %s on %s -> %s", spec.purpose, kind.value, key.label, action.value)
                if action is not Action.RETRY_SAME:
                    return _KeyOutcome(action, None)
                retries_used[policy] = retries_used.get(policy, 0) + 1
                if kind is ErrorKind.PARSE:
                    repair = spec.repair_note(exc)
                if policy.backoff:
                    delay = self._backoff_delay(retries_used[policy])
                    if not self._deadline_allows(spec.deadline, delay):
                        state.note("deadline reached")
                        state.stopped = True
                        return _KeyOutcome(None, None)
                    await self._sleep(delay)
                continue
            latency = self._monotonic() - run.started
            pool.mark_success(key)
            breaker.record_success()
            record = AttemptRecord(name, model, key.label, True, None, None, latency, attempt_no)
            self._record(state, spec.purpose, record, input_tokens, output_tokens)
            self._observe(
                spec,
                name,
                model,
                key,
                attempt_no,
                sent_user,
                ok=True,
                latency=latency,
                output=spec.raw_output or value,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
            return _KeyOutcome(
                None,
                LLMCallResult(
                    value=value,
                    provider=name,
                    model=model,
                    key_label=key.label,
                    attempts=list(state.attempts),
                    latency_s=self._monotonic() - state.started,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    purpose=spec.purpose,
                ),
            )

    # ---------------------------------------------------------------------------------------------- one attempt

    async def _run_attempt(
        self,
        spec: _CallSpec,
        name: str,
        provider: Provider,
        key: ApiKey,
        model: str,
        timeout_s: float,
        repair: str | None,
        run: _Run,
    ) -> tuple[Any, int | None, int | None]:
        """Wait for the rate limiter (when there is one), then call the provider once.

        ``run.started`` is the time the provider call began (after any wait), so the latency written to the trace
        never includes the time spent queueing for the limiter.

        Raises:
            RateLimitTimeout: Waiting for the limiter would pass ``spec.deadline``.
        """
        limiter = self._limiter
        if limiter is None:
            run.started = self._monotonic()
            return await asyncio.wait_for(
                spec.invoke(provider, key, model, timeout_s, repair), timeout_s + TIMEOUT_GRACE_S
            )
        estimate = estimate_tokens(
            spec.system, spec.user + (repair or ""), spec.max_tokens or self.config.max_tokens
        )
        async with limiter.slot(name, key, estimate, spec.deadline) as waited:
            run.waited_s = waited
            if waited > RATE_TRACE_MIN_WAIT_S:
                self._rate_event(
                    f"waited {waited:.1f} s for {key.label}",
                    spec.purpose,
                    provider=name,
                    waited_s=round(waited, 2),
                )
            usable = self._attempt_timeout(spec.deadline)
            if usable is None:
                raise RateLimitTimeout("too little time left after waiting")
            timeout_s = min(timeout_s, usable)
            run.started = self._monotonic()
            result = await asyncio.wait_for(
                spec.invoke(provider, key, model, timeout_s, repair), timeout_s + TIMEOUT_GRACE_S
            )
        reported = [t for t in result[1:] if t is not None]
        limiter.adjust(name, key, estimate, sum(reported) if reported else None)
        return result  # type: ignore[no-any-return]

    def _penalize(self, name: str, key: ApiKey, exc: BaseException, purpose: str) -> None:
        """A real 429: stop the limiter from sending to this key for ``Retry-After`` (or the cooldown setting)."""
        if self._limiter is None:
            return
        hinted = retry_after_seconds(exc)
        seconds = hinted if hinted is not None else self.config.rate_limit_cooldown_s
        self._limiter.penalize(name, key, seconds)
        self._rate_event(
            f"{key.label} rate limited; no calls to it for {seconds:.0f} s", purpose, provider=name
        )

    def _observe(
        self,
        spec: _CallSpec,
        name: str,
        model: str,
        key: ApiKey,
        attempt_no: int,
        sent_user: str,
        *,
        ok: bool,
        latency: float,
        output: Any,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        kind: ErrorKind | None = None,
        error: str | None = None,
    ) -> None:
        """Report one attempt (success or failure) to the observer; the observer can never break a call."""
        observer = self._observer
        if observer is None:
            return
        try:
            observer.log_generation(
                purpose=spec.purpose,
                provider=name,
                model=model,
                key_label=key.label,
                system=spec.system,
                user=sent_user,
                output=output,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                latency_s=round(latency, 3),
                ok=ok,
                error_kind=kind.value if kind else None,
                error=error,
                attempt=attempt_no,
                span_id=current_span_id(),
            )
        except Exception:  # observability must never break a model call
            logger.warning("observer.log_generation failed", exc_info=True)

    def _rate_event(self, message: str, purpose: str, **data: Any) -> None:
        event = getattr(self._trace, "event", None)
        if event is None:
            return
        try:
            event("rate", message, purpose=purpose, **data)
        except Exception:
            logger.warning("trace.event failed", exc_info=True)

    # ---------------------------------------------------------------------------------------------- helpers

    def _next_action(self, policy: ErrorPolicy, retries_used: int, breaker: CircuitBreaker) -> Action:
        if policy.action is not Action.RETRY_SAME:
            return policy.action
        if breaker.is_open:
            return Action.NEXT_PROVIDER
        limit = self.config.retries_per_key if policy.retry_limit is None else policy.retry_limit
        return Action.RETRY_SAME if retries_used < limit else policy.exhausted_action

    def _apply_key_effect(
        self,
        pool: KeyPool,
        key: ApiKey,
        kind: ErrorKind,
        policy: ErrorPolicy,
        exc: BaseException,
        error_text: str,
    ) -> None:
        if policy.key_effect is KeyEffect.DEAD:
            pool.mark_dead(key, f"{kind.value}: {error_text}")
            self._event(f"{key.label} marked dead ({kind.value})", provider=pool.provider)
        elif policy.key_effect is KeyEffect.COOL:
            hinted = retry_after_seconds(exc)
            seconds = hinted if hinted is not None else self.config.rate_limit_cooldown_s
            pool.mark_cooling(key, seconds)
            self._event(f"{key.label} cooling for {seconds:.0f}s ({kind.value})", provider=pool.provider)

    def _cooldown_wait(self, spec: _CallSpec, state: _CallState) -> float | None:
        if state.waits >= MAX_COOLDOWN_WAITS:
            return None
        waits = [
            s
            for name in self._call_order(spec)
            if name not in state.finished
            and not self._breakers[name].is_open
            and (s := self._pools[name].soonest_cooldown_s(exclude=state.excluded.get(name, ()))) is not None
        ]
        if not waits:
            return None
        wait = min(waits) + 0.05
        if wait > self.config.max_cooldown_wait_s or not self._deadline_allows(spec.deadline, wait):
            return None
        return wait

    def _revive_persisted(self, spec: _CallSpec) -> bool:
        revived: list[ApiKey] = []
        for name in self._call_order(spec):
            revived += self._pools[name].revive_persisted()
        if revived:
            self._event(
                "last resort: retrying keys a previous run marked dead: "
                + ", ".join(k.label for k in revived),
                purpose=spec.purpose,
            )
        return bool(revived)

    def _attempt_timeout(self, deadline: float | None) -> float | None:
        if deadline is None:
            return self.config.timeout_s
        usable = deadline - self._monotonic() - TIMEOUT_GRACE_S
        if usable < MIN_ATTEMPT_TIMEOUT_S:
            return None
        return min(self.config.timeout_s, usable)

    def _deadline_allows(self, deadline: float | None, wait_s: float) -> bool:
        if deadline is None:
            return True
        return self._monotonic() + wait_s + MIN_ATTEMPT_TIMEOUT_S + TIMEOUT_GRACE_S <= deadline

    def _backoff_delay(self, retry_number: int) -> float:
        step = BACKOFF_BASE_S * (2 ** max(0, retry_number - 1))
        return step + self._rng.uniform(0.0, BACKOFF_JITTER * step)

    def _call_order(self, spec: _CallSpec) -> Sequence[str]:
        return (spec.only_provider,) if spec.only_provider else self._order

    def _record(
        self,
        state: _CallState,
        purpose: str,
        record: AttemptRecord,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
    ) -> None:
        state.attempts.append(record)
        try:
            self._trace.model_call(
                provider=record.provider,
                model=record.model,
                key_label=record.key_label,
                purpose=purpose,
                ok=record.ok,
                latency_s=round(record.latency_s, 3),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                error_kind=record.error_kind.value if record.error_kind else None,
                error=record.error,
                attempt=record.attempt,
            )
        except Exception:  # tracing must never break a model call
            logger.warning("trace.model_call failed", exc_info=True)

    def _event(self, message: str, **data: Any) -> None:
        event = getattr(self._trace, "event", None)
        if event is None:
            return
        try:
            event("llm", message, **data)
        except Exception:
            logger.warning("trace.event failed", exc_info=True)

    def _build_providers(self, providers: Mapping[str, Provider] | None) -> dict[str, Provider]:
        if providers is not None:
            names = [n for n in self.config.providers if n in providers] or list(providers)
            return {n: providers[n] for n in names}
        built: dict[str, Provider] = {}
        for name in self.config.providers:
            if name not in PROVIDER_REGISTRY:
                logger.warning(
                    "Unknown LLM provider %r in LLM_PROVIDERS (known: %s); skipped",
                    name,
                    ", ".join(sorted(PROVIDER_REGISTRY)),
                )
                continue
            built[name] = create_provider(name, self._env)
        return built

    def _discover(self, name: str, provider: Provider) -> list[ApiKey]:
        custom = getattr(provider, "discover_keys", None)
        if callable(custom):
            return list(custom(self._env))
        return discover_keys(name, self._env)

    def _collect_secrets(self) -> list[str]:
        values = llm_secrets(self._env)
        for pool in self._pools.values():
            values += [k.secret for k in pool.keys if not k.is_login]
        return list(dict.fromkeys(v for v in values if v))


def build_gateway(
    settings: Any,
    trace: TraceSink | None,
    observer: LLMObserver | None = None,
    limiter: RateLimiter | bool | None = None,
) -> LLMGateway:
    """Create the production gateway (keys from ``.env`` + environment, providers from the registry).

    Rate limiting comes from the ``llm_*`` rate settings; ``observer`` (see :mod:`builder.observability`) receives
    every attempt.
    """
    return LLMGateway(settings, trace, observer=observer, limiter=limiter)


# --------------------------------------------------------------------------------------------------------------
# Module helpers
# --------------------------------------------------------------------------------------------------------------


def _as_model(value: Any, schema: type[ModelT], raw_text: str) -> ModelT:
    if isinstance(value, schema):
        return value
    try:
        return schema.model_validate(value)
    except ValidationError as exc:
        raise ParseError(
            f"JSON does not match {schema.__name__}: {summarize_validation_error(exc)}", raw_text=raw_text
        ) from exc


def _structured_repair_note(exc: BaseException) -> str:
    return repair_instruction(str(getattr(exc, "message", "") or exc))


def _text_repair_note(exc: BaseException) -> str:
    del exc
    return "\n\nIMPORTANT - your previous reply was empty. Answer the request above in full."


def _env_file_of(settings: Any) -> Path | None:
    config = getattr(settings, "model_config", None)
    env_file = config.get("env_file") if isinstance(config, Mapping) else None
    if isinstance(env_file, str | Path):
        return Path(env_file)
    return None
