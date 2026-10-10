"""LLM access for the builder: the key changer.

Every LLM call the builder makes goes through :class:`LLMGateway`, which rotates **keys within a provider**
(``ANTHROPIC_API_KEY``, ``ANTHROPIC_API_KEY_2`` ...) and **providers in a chain** (default
``anthropic -> openai -> claude_code``), classifies every failure (no credits, invalid key, rate limit,
overload, bad request, unparseable output ...) and applies a fixed recovery policy, with per-provider circuit
breakers, a persistent dead-key file and one trace line per attempt.

Modules:
    ``errors``     - ErrorKind, classify_error, recovery POLICY, package exceptions.
    ``keys``       - ApiKey, key discovery from the environment, KeyPool, HealthStore.
    ``providers``  - Provider protocol, Anthropic / OpenAI / Claude Code adapters, PROVIDER_REGISTRY.
    ``structured`` - JSON-schema preparation and robust JSON extraction / validation.
    ``ratelimit``  - client-side rate limiting: TokenBucket, RateLimiter (concurrency, rpm / tpm per key, pacing).
    ``gateway``    - LLMGateway (the fallback loop), CircuitBreaker, AttemptRecord, LLMCallResult.

Typical use (inside a graph node)::

    result = await ctx.llm.structured(purpose="extract policy IR", schema=PolicyIR, system=..., user=...,
                                      deadline=ctx.budget.deadline())
    ir = result.value

See ``docs/LLM_KEY_CHANGER.md`` for configuration and troubleshooting. Provider SDKs are imported lazily, so
importing this package never requires ``anthropic`` / ``openai`` / ``claude_agent_sdk``.
"""

from __future__ import annotations

from builder.llm.errors import (
    POLICY,
    Action,
    AllProvidersFailed,
    ErrorKind,
    ErrorPolicy,
    KeyEffect,
    LLMError,
    ParseError,
    ProviderError,
    classify_error,
    describe_error,
    retry_after_seconds,
    scrub_secrets,
)
from builder.llm.gateway import (
    AttemptRecord,
    CircuitBreaker,
    CircuitState,
    GatewayConfig,
    LLMCallResult,
    LLMGateway,
    TraceSink,
    build_gateway,
    parse_provider_order,
)
from builder.llm.keys import (
    CLAUDE_CODE,
    ApiKey,
    HealthStore,
    KeyPool,
    KeyStatus,
    claude_code_available,
    default_env,
    discover_all_keys,
    discover_keys,
    llm_secrets,
)
from builder.llm.providers import (
    PROVIDER_REGISTRY,
    AnthropicProvider,
    ClaudeCodeProvider,
    LLMRole,
    OpenAIProvider,
    Provider,
    StructuredResult,
    TextResult,
    create_provider,
    register_provider,
)
from builder.llm.ratelimit import (
    RateLimitConfig,
    RateLimiter,
    RateLimitTimeout,
    TokenBucket,
    estimate_tokens,
    parse_rate_overrides,
)
from builder.llm.structured import extract_json, parse_json_model, schema_for_llm

__all__ = [
    "CLAUDE_CODE",
    "POLICY",
    "PROVIDER_REGISTRY",
    "Action",
    "AllProvidersFailed",
    "AnthropicProvider",
    "ApiKey",
    "AttemptRecord",
    "CircuitBreaker",
    "CircuitState",
    "ClaudeCodeProvider",
    "ErrorKind",
    "ErrorPolicy",
    "GatewayConfig",
    "HealthStore",
    "KeyEffect",
    "KeyPool",
    "KeyStatus",
    "LLMCallResult",
    "LLMError",
    "LLMGateway",
    "LLMRole",
    "OpenAIProvider",
    "ParseError",
    "Provider",
    "ProviderError",
    "RateLimitConfig",
    "RateLimitTimeout",
    "RateLimiter",
    "StructuredResult",
    "TextResult",
    "TokenBucket",
    "TraceSink",
    "build_gateway",
    "classify_error",
    "claude_code_available",
    "create_provider",
    "default_env",
    "describe_error",
    "discover_all_keys",
    "discover_keys",
    "estimate_tokens",
    "extract_json",
    "llm_secrets",
    "parse_json_model",
    "parse_provider_order",
    "parse_rate_overrides",
    "register_provider",
    "retry_after_seconds",
    "schema_for_llm",
    "scrub_secrets",
]
