"""Provider adapters for the key changer: one small class per LLM vendor behind a common protocol.

The gateway never talks to an SDK directly. It asks a :class:`Provider` to run one attempt with one key:

* ``model_for(role, settings)`` - which model to use for a STRONG (extraction, drafting) or FAST (audits,
  probes) call, read from settings such as ``anthropic_strong_model``;
* ``complete_text(key, model, system, user, *, max_tokens, timeout_s)`` -> :class:`TextResult`;
* ``complete_structured(key, model, schema, system, user, *, max_tokens, timeout_s)`` ->
  :class:`StructuredResult` whose ``value`` is a validated instance of the Pydantic ``schema``.

Adapters do **no** retries of their own (``max_retries=0``): retrying, key rotation and provider fallback are
the gateway's job, so every attempt is visible in the trace. SDKs are imported lazily inside the methods, so
``import builder.llm`` works without any provider SDK installed.

Implementations
    ``AnthropicProvider``  - ``langchain_anthropic.ChatAnthropic``; native structured outputs
    (``method="json_schema"``, ``include_raw=True``) with a fallback to parsing the raw text. Temperature /
    top_p are never sent (recent Claude models reject them).
    ``OpenAIProvider``     - ``langchain_openai.ChatOpenAI``; ``json_schema`` with ``strict=False`` and the
    same fallback. ``max_tokens`` is not forwarded because reasoning models count hidden reasoning tokens
    against it and would return empty answers.
    ``ClaudeCodeProvider`` - ``claude_agent_sdk.query`` (the local Claude Code login, no API key): one turn, no
    tools, no project settings; structured output is prompt-instructed JSON parsed with
    :func:`builder.llm.structured.parse_json_model`.

``PROVIDER_REGISTRY`` maps a provider name to a factory ``(env) -> Provider`` (Mitrailleuse-style adapter
registry). Add a vendor with :func:`register_provider`; see ``docs/LLM_KEY_CHANGER.md``.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel, ValidationError

from builder.llm.errors import ErrorKind, ParseError, ProviderError
from builder.llm.keys import CLAUDE_CODE, ApiKey
from builder.llm.structured import (
    has_free_form_objects,
    json_instruction,
    parse_json_model,
    schema_for_llm,
    summarize_validation_error,
)

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_MODELS",
    "PROVIDER_REGISTRY",
    "AnthropicProvider",
    "ClaudeCodeProvider",
    "LLMRole",
    "OpenAIProvider",
    "Provider",
    "ProviderFactory",
    "StructuredResult",
    "TextResult",
    "create_provider",
    "register_provider",
]

ModelT = TypeVar("ModelT", bound=BaseModel)


class LLMRole(StrEnum):
    """Which tier of model a call needs."""

    STRONG = "strong"
    """Best model: rule ledger, policy extraction, prose drafting, repairs."""
    FAST = "fast"
    """Cheaper / faster model: audits, probes, small classification calls."""


DEFAULT_MODELS: dict[str, dict[LLMRole, str]] = {
    "anthropic": {LLMRole.STRONG: "claude-sonnet-5-5", LLMRole.FAST: "claude-haiku-5-5"},
    "openai": {LLMRole.STRONG: "gpt-5.6-terra", LLMRole.FAST: "gpt-5.6-luna"},
    CLAUDE_CODE: {LLMRole.STRONG: "claude-sonnet-5-5", LLMRole.FAST: "claude-haiku-5-5"},
}
"""Fallback model names when the settings object does not define ``<provider>_<role>_model``."""


@dataclass(frozen=True, slots=True)
class TextResult:
    """Outcome of one successful text completion."""

    text: str
    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class StructuredResult:
    """Outcome of one successful structured completion (``value`` is a validated model instance)."""

    value: Any
    raw_text: str = ""
    input_tokens: int | None = None
    output_tokens: int | None = None


@runtime_checkable
class Provider(Protocol):
    """What the gateway needs from an LLM vendor adapter (one attempt, one key, no internal retries)."""

    name: str

    def model_for(self, role: LLMRole, settings: Any) -> str:
        """Model id for ``role`` (normally ``settings.<name>_<role>_model``)."""
        ...

    async def complete_text(
        self, key: ApiKey, model: str, system: str, user: str, *, max_tokens: int, timeout_s: float
    ) -> TextResult:
        """Return the model's plain-text answer; raise the SDK's exception (or ``ProviderError``) on failure."""
        ...

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
        """Return a validated ``schema`` instance; raise ``ParseError`` when the reply cannot be parsed."""
        ...


ProviderFactory = Callable[[Mapping[str, str]], Provider]
"""Builds a provider from the environment mapping the gateway uses (most adapters ignore it)."""


# --------------------------------------------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------------------------------------------


class _BaseProvider:
    """Model lookup shared by the built-in adapters."""

    name: str = ""

    def model_for(self, role: LLMRole, settings: Any) -> str:
        """Read ``settings.<name>_<role>_model``; fall back to :data:`DEFAULT_MODELS`."""
        role = LLMRole(role)
        value = getattr(settings, f"{self.name}_{role.value}_model", None)
        if isinstance(value, str) and value.strip():
            return value.strip()
        return DEFAULT_MODELS.get(self.name, {}).get(role, "")


def _messages(system: str, user: str) -> list[Any]:
    from langchain_core.messages import HumanMessage, SystemMessage

    messages: list[Any] = []
    if system.strip():
        messages.append(SystemMessage(content=system))
    messages.append(HumanMessage(content=user))
    return messages


def _message_text(message: Any) -> str:
    """Plain text of a LangChain message (``.text`` is a str-like property in langchain-core 1.x)."""
    text = getattr(message, "text", None)
    if isinstance(text, str):  # TextAccessor is a str subclass (also callable, deprecated)
        return str(text)
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    parts = []
    for block in content or []:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
    return "".join(parts)


def _usage(message: Any) -> tuple[int | None, int | None]:
    usage = getattr(message, "usage_metadata", None) or {}
    return usage.get("input_tokens"), usage.get("output_tokens")


async def _langchain_structured(
    chat: Any,
    schema: type[ModelT],
    system: str,
    user: str,
    *,
    structured_kwargs: dict[str, Any],
) -> StructuredResult:
    """Native structured output with ``include_raw=True`` and a raw-text fallback.

    LangChain returns ``{"raw": AIMessage, "parsed": dict | None, "parsing_error": Exception | None}``. We pass
    the cleaned dict schema (not the class), so ``parsed`` is a dict that we validate ourselves; if parsing or
    validation fails we try :func:`parse_json_model` on the raw text before raising ``ParseError``.
    """
    llm_schema = schema_for_llm(schema)
    runnable = chat.with_structured_output(llm_schema, include_raw=True, **structured_kwargs)
    output = await runnable.ainvoke(_messages(system, user))
    raw = output.get("raw") if isinstance(output, dict) else None
    raw_text = _message_text(raw) if raw is not None else ""
    input_tokens, output_tokens = _usage(raw)
    parsed = output.get("parsed") if isinstance(output, dict) else None
    problem = output.get("parsing_error") if isinstance(output, dict) else None
    if parsed is not None:
        try:
            value = parsed if isinstance(parsed, schema) else schema.model_validate(parsed)
            return StructuredResult(value, raw_text, input_tokens, output_tokens)
        except ValidationError as exc:
            problem = ParseError(f"JSON does not match {schema.__name__}: {summarize_validation_error(exc)}")
    try:
        value = parse_json_model(raw_text, schema)
    except ParseError as exc:
        reason = f"{exc.message}" + (f" (structured-output parser: {problem})" if problem else "")
        if _stopped_at_max_tokens(raw):
            reason = "reply truncated at max_tokens; " + reason
        raise ParseError(reason[:1000], raw_text=raw_text) from exc
    return StructuredResult(value, raw_text, input_tokens, output_tokens)


def _stopped_at_max_tokens(message: Any) -> bool:
    metadata = getattr(message, "response_metadata", None) or {}
    reason = metadata.get("stop_reason") or metadata.get("finish_reason")
    return reason in {"max_tokens", "length"}


# --------------------------------------------------------------------------------------------------------------
# Anthropic
# --------------------------------------------------------------------------------------------------------------


def _chat_anthropic_class() -> Any:
    from langchain_anthropic import ChatAnthropic

    return ChatAnthropic


class AnthropicProvider(_BaseProvider):
    """Anthropic Messages API through ``langchain_anthropic.ChatAnthropic`` (API keys ``ANTHROPIC_API_KEY*``)."""

    name = "anthropic"

    def __init__(self, env: Mapping[str, str] | None = None) -> None:
        """Create the adapter (``env`` is accepted for factory symmetry and unused)."""
        del env

    def _chat(self, key: ApiKey, model: str, max_tokens: int, timeout_s: float) -> Any:
        # Never pass temperature / top_p: recent Claude models reject sampling parameters.
        return _chat_anthropic_class()(
            model=model, api_key=key.secret, max_tokens=max_tokens, timeout=timeout_s, max_retries=0
        )

    async def complete_text(
        self, key: ApiKey, model: str, system: str, user: str, *, max_tokens: int, timeout_s: float
    ) -> TextResult:
        """One Messages API call; returns the text and token usage."""
        message = await self._chat(key, model, max_tokens, timeout_s).ainvoke(_messages(system, user))
        input_tokens, output_tokens = _usage(message)
        return TextResult(_message_text(message), input_tokens, output_tokens)

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
        """Native ``json_schema`` structured output, or prompt-instructed JSON for schemas with free-form maps."""
        llm_schema = schema_for_llm(schema)
        if has_free_form_objects(llm_schema):
            # Anthropic's constrained decoding closes every object; dict-typed fields would come back empty.
            logger.debug("schema %s has free-form maps; using prompt-instructed JSON", schema.__name__)
            prompt_system = f"{system}\n\n{json_instruction(llm_schema)}".strip()
            result = await self.complete_text(
                key, model, prompt_system, user, max_tokens=max_tokens, timeout_s=timeout_s
            )
            value = parse_json_model(result.text, schema)
            return StructuredResult(value, result.text, result.input_tokens, result.output_tokens)
        chat = self._chat(key, model, max_tokens, timeout_s)
        return await _langchain_structured(
            chat, schema, system, user, structured_kwargs={"method": "json_schema"}
        )


# --------------------------------------------------------------------------------------------------------------
# OpenAI
# --------------------------------------------------------------------------------------------------------------


def _chat_openai_class() -> Any:
    from langchain_openai import ChatOpenAI

    return ChatOpenAI


class OpenAIProvider(_BaseProvider):
    """OpenAI through ``langchain_openai.ChatOpenAI`` (API keys ``OPENAI_API_KEY*``)."""

    name = "openai"

    def __init__(self, env: Mapping[str, str] | None = None) -> None:
        """Create the adapter (``env`` is accepted for factory symmetry and unused)."""
        del env

    def _chat(self, key: ApiKey, model: str, timeout_s: float) -> Any:
        # max_tokens deliberately not forwarded: reasoning tokens count against max_completion_tokens.
        return _chat_openai_class()(model=model, api_key=key.secret, timeout=timeout_s, max_retries=0)

    async def complete_text(
        self, key: ApiKey, model: str, system: str, user: str, *, max_tokens: int, timeout_s: float
    ) -> TextResult:
        """One chat completion; returns the text and token usage."""
        del max_tokens
        message = await self._chat(key, model, timeout_s).ainvoke(_messages(system, user))
        input_tokens, output_tokens = _usage(message)
        return TextResult(_message_text(message), input_tokens, output_tokens)

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
        """``json_schema`` response format (non-strict) with a raw-text parsing fallback."""
        del max_tokens
        chat = self._chat(key, model, timeout_s)
        return await _langchain_structured(
            chat, schema, system, user, structured_kwargs={"method": "json_schema", "strict": False}
        )


# --------------------------------------------------------------------------------------------------------------
# Claude Code (Claude Agent SDK, local login)
# --------------------------------------------------------------------------------------------------------------

_SDK_ERROR_KINDS: dict[str, ErrorKind] = {
    "authentication_failed": ErrorKind.AUTH,
    "billing_error": ErrorKind.QUOTA,
    "rate_limit": ErrorKind.RATE_LIMIT,
    "invalid_request": ErrorKind.BAD_REQUEST,
    "server_error": ErrorKind.SERVER,
}


_NOT_LOGGED_IN_RE = re.compile(r"^\s*(?:not logged in|please run /login|invalid api key\W{0,8}please run /login)", re.I)
"""Reply text of the Claude Code CLI when no login exists ("Not logged in - Please run /login")."""


def _import_claude_sdk() -> Any:
    import claude_agent_sdk

    return claude_agent_sdk


class ClaudeCodeProvider(_BaseProvider):
    """Claude through the Claude Agent SDK and the local Claude Code login (no API key needed).

    Each attempt runs ``claude_agent_sdk.query`` with one turn, no tools and no project/user settings, so the
    CLI behaves like a plain completion endpoint. With the ``login`` pseudo key the subprocess gets
    ``ANTHROPIC_API_KEY=""`` so that a key in the parent environment cannot shadow the subscription login; with
    a real key (if a caller adds Anthropic keys to this pool) that key is passed instead.
    """

    name = CLAUDE_CODE

    def __init__(self, env: Mapping[str, str] | None = None) -> None:
        """Remember ``CLAUDE_CODE_OAUTH_TOKEN`` from ``env`` so it reaches the CLI even if only in ``.env``."""
        token = (env or {}).get("CLAUDE_CODE_OAUTH_TOKEN", "").strip()
        self._oauth_token = token or None

    def _sdk_env(self, key: ApiKey, max_tokens: int) -> dict[str, str]:
        sdk_env = {"CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(max_tokens)}
        if key.is_login:
            sdk_env["ANTHROPIC_API_KEY"] = ""
            if self._oauth_token:
                sdk_env["CLAUDE_CODE_OAUTH_TOKEN"] = self._oauth_token
        else:
            sdk_env["ANTHROPIC_API_KEY"] = key.secret
        return sdk_env

    async def complete_text(
        self, key: ApiKey, model: str, system: str, user: str, *, max_tokens: int, timeout_s: float
    ) -> TextResult:
        """Run one Claude Code turn and return the assistant text (usage from the ``ResultMessage``)."""
        del timeout_s  # enforced by the gateway's wait_for (the SDK has no per-request timeout)
        sdk = _import_claude_sdk()
        options = sdk.ClaudeAgentOptions(
            model=model,
            system_prompt=system or None,
            tools=[],
            allowed_tools=[],
            max_turns=1,
            setting_sources=[],
            permission_mode="default",
            env=self._sdk_env(key, max_tokens),
        )
        texts: list[str] = []
        assistant_error: str | None = None
        result: Any = None
        async for message in sdk.query(prompt=user, options=options):
            if isinstance(message, sdk.AssistantMessage):
                assistant_error = assistant_error or getattr(message, "error", None)
                texts.extend(b.text for b in message.content if isinstance(b, sdk.TextBlock))
            elif isinstance(message, sdk.ResultMessage):
                result = message
        text = "".join(texts).strip() or (getattr(result, "result", None) or "").strip()
        if assistant_error or (result is not None and getattr(result, "is_error", False)):
            raise _claude_code_error(assistant_error, result, text)
        if _NOT_LOGGED_IN_RE.match(text):
            # The bundled CLI answers an unauthenticated call with this sentence as ordinary assistant text; it
            # must never be returned as a model answer. AUTH marks the login dead so the chain moves on at once.
            raise ProviderError(f"Claude Code login missing: {text[:200]}", kind=ErrorKind.AUTH)
        input_tokens, output_tokens = _result_usage(result)
        return TextResult(text, input_tokens, output_tokens)

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
        """Prompt-instructed JSON ("Return ONLY JSON matching this JSON schema") parsed with the model."""
        prompt_system = f"{system}\n\n{json_instruction(schema_for_llm(schema))}".strip()
        result = await self.complete_text(
            key, model, prompt_system, user, max_tokens=max_tokens, timeout_s=timeout_s
        )
        value = parse_json_model(result.text, schema)
        return StructuredResult(value, result.text, result.input_tokens, result.output_tokens)


def _claude_code_error(assistant_error: str | None, result: Any, text: str) -> ProviderError:
    status = getattr(result, "api_error_status", None)
    details = [f"Claude Code error={assistant_error or getattr(result, 'subtype', 'error')}"]
    if text:
        details.append(text[:300])
    for err in (getattr(result, "errors", None) or [])[:3]:
        details.append(str(err)[:200])
    kind = _SDK_ERROR_KINDS.get(assistant_error or "")
    return ProviderError(
        ": ".join(details), kind=kind, status_code=status if isinstance(status, int) else None
    )


def _result_usage(result: Any) -> tuple[int | None, int | None]:
    usage = getattr(result, "usage", None)
    if not isinstance(usage, dict):
        return None, None
    input_tokens = sum(
        int(usage.get(k) or 0)
        for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    )
    output = usage.get("output_tokens")
    return (input_tokens or None), (int(output) if output is not None else None)


# --------------------------------------------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------------------------------------------

PROVIDER_REGISTRY: dict[str, ProviderFactory] = {
    "anthropic": AnthropicProvider,
    "openai": OpenAIProvider,
    CLAUDE_CODE: ClaudeCodeProvider,
}
"""Provider name -> factory ``(env) -> Provider``. Names are what ``LLM_PROVIDERS`` lists."""


def register_provider(name: str, factory: ProviderFactory) -> None:
    """Add (or replace) a provider adapter under ``name`` (lower-case, as used in ``LLM_PROVIDERS``)."""
    key = name.strip().lower()
    if not key:
        raise ValueError("provider name must not be empty")
    PROVIDER_REGISTRY[key] = factory


def create_provider(name: str, env: Mapping[str, str]) -> Provider:
    """Instantiate the registered provider ``name``.

    Raises:
        KeyError: ``name`` is not registered.
    """
    return PROVIDER_REGISTRY[name](env)
