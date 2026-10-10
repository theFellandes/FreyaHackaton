"""Error taxonomy and recovery policy for the LLM key changer.

Every builder LLM call goes through :class:`builder.llm.gateway.LLMGateway`. When a call fails, the gateway
has to decide *what kind* of failure it was and *what to do next*:

* a key with no credits (Anthropic "credit balance is too low", OpenAI ``insufficient_quota``) or an invalid
  key is useless for the rest of the run -> mark it dead and rotate to the next key;
* a rate-limited key will work again in a few seconds -> cool it down and rotate;
* an overloaded / flaky provider is retried on the same key with exponential backoff;
* a request the provider rejects (unknown model, context too long) will not succeed with another key of the
  same provider -> move on to the next provider in the chain.

This module holds that knowledge in one place:

``ErrorKind``
    The closed set of failure categories.
``classify_error(exc)``
    Maps *any* exception (Anthropic / OpenAI SDK errors, their LangChain-wrapped subclasses, httpx transport
    errors, ``asyncio.TimeoutError``, Claude Agent SDK errors, or a generic exception that merely carries a
    ``status_code`` / ``message`` / ``body``) to an ``ErrorKind``. It never imports a provider SDK: real SDK
    classes are only consulted when the SDK is already loaded (an exception of a class can only exist if its
    module was imported), and everything else is duck-typed (status codes, error ``type`` / ``code`` fields,
    class names along the MRO, and message patterns).
``Action`` / ``ErrorPolicy`` / ``POLICY``
    What the gateway does for each kind (retry same key, rotate key, next provider) and the side effect on
    the key (dead / cooling / nothing).
``ProviderError`` / ``ParseError`` / ``AllProvidersFailed``
    The exceptions the ``builder.llm`` package raises itself.

Helpers ``retry_after_seconds`` (reads ``retry-after`` / ``retry-after-ms`` headers), ``describe_error`` (one
short line for traces) and ``scrub_secrets`` (removes API keys from any text) complete the module.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from builder.llm.gateway import AttemptRecord

__all__ = [
    "POLICY",
    "Action",
    "AllProvidersFailed",
    "ErrorKind",
    "ErrorPolicy",
    "KeyEffect",
    "LLMError",
    "ParseError",
    "ProviderError",
    "classify_error",
    "describe_error",
    "retry_after_seconds",
    "scrub_secrets",
]


class ErrorKind(StrEnum):
    """Category of an LLM call failure; drives the recovery :data:`POLICY`."""

    QUOTA = "quota"
    """No credits / billing problem / ``insufficient_quota`` / HTTP 402. The key is useless until topped up."""
    AUTH = "auth"
    """HTTP 401/403, invalid or revoked key, missing permission, Claude Code not logged in."""
    RATE_LIMIT = "rate_limit"
    """HTTP 429 that is *not* a quota problem. The key works again after a cooldown."""
    OVERLOADED = "overloaded"
    """HTTP 529 / 503 or an "overloaded" message. Provider-wide and usually short-lived."""
    TIMEOUT = "timeout"
    """The request (or our own deadline-capped ``wait_for``) timed out."""
    CONNECTION = "connection"
    """Network / transport failure before an HTTP status was received."""
    SERVER = "server"
    """Any other HTTP 5xx."""
    BAD_REQUEST = "bad_request"
    """HTTP 400/404/409/422: unknown model, unsupported parameter, malformed request."""
    CONTEXT = "context"
    """The prompt does not fit the model's context window (or HTTP 413)."""
    PARSE = "parse"
    """The model answered but the structured output could not be parsed / validated."""
    UNKNOWN = "unknown"
    """Anything we could not classify."""


class Action(StrEnum):
    """What the gateway does after a failed attempt."""

    RETRY_SAME = "retry_same"
    """Retry with the same provider and the same key (after a backoff)."""
    ROTATE_KEY = "rotate_key"
    """Try the next usable key of the same provider."""
    NEXT_PROVIDER = "next_provider"
    """Give up on this provider for the current call and move to the next one in the chain."""


class KeyEffect(StrEnum):
    """Side effect a failure has on the key that was used."""

    NONE = "none"
    """Only the failure counter is incremented."""
    COOL = "cool"
    """The key is put in COOLING state (``retry-after`` header, else the configured cooldown)."""
    DEAD = "dead"
    """The key is DEAD for the rest of the run and persisted to the key-health file."""


@dataclass(frozen=True, slots=True)
class ErrorPolicy:
    """Recovery rule for one :class:`ErrorKind`.

    Attributes:
        action: First reaction to the failure.
        key_effect: What happens to the key that failed.
        retry_limit: For ``RETRY_SAME``: how many *extra* attempts on the same key are allowed. ``None`` means
            "use ``settings.llm_retries_per_key``". Ignored for the other actions.
        backoff: Sleep ``1, 2, 4 ... s`` (+ jitter) before each same-key retry.
        exhausted_action: What to do once ``retry_limit`` same-key retries are used up.
        provider_failure: Whether the failure counts towards the provider's circuit breaker (provider-wide
            problems do; key- or request-specific problems do not).
    """

    action: Action
    key_effect: KeyEffect = KeyEffect.NONE
    retry_limit: int | None = 0
    backoff: bool = False
    exhausted_action: Action = Action.ROTATE_KEY
    provider_failure: bool = False


_TRANSIENT = ErrorPolicy(
    action=Action.RETRY_SAME,
    retry_limit=None,
    backoff=True,
    exhausted_action=Action.ROTATE_KEY,
    provider_failure=True,
)

POLICY: dict[ErrorKind, ErrorPolicy] = {
    ErrorKind.QUOTA: ErrorPolicy(action=Action.ROTATE_KEY, key_effect=KeyEffect.DEAD),
    ErrorKind.AUTH: ErrorPolicy(action=Action.ROTATE_KEY, key_effect=KeyEffect.DEAD),
    ErrorKind.RATE_LIMIT: ErrorPolicy(action=Action.ROTATE_KEY, key_effect=KeyEffect.COOL),
    ErrorKind.OVERLOADED: _TRANSIENT,
    ErrorKind.SERVER: _TRANSIENT,
    ErrorKind.TIMEOUT: _TRANSIENT,
    ErrorKind.CONNECTION: _TRANSIENT,
    ErrorKind.BAD_REQUEST: ErrorPolicy(action=Action.NEXT_PROVIDER),
    ErrorKind.CONTEXT: ErrorPolicy(action=Action.NEXT_PROVIDER),
    ErrorKind.PARSE: ErrorPolicy(
        action=Action.RETRY_SAME, retry_limit=1, backoff=False, exhausted_action=Action.NEXT_PROVIDER
    ),
    ErrorKind.UNKNOWN: ErrorPolicy(action=Action.ROTATE_KEY, provider_failure=True),
}
"""ErrorKind -> recovery rule. See ``docs/LLM_KEY_CHANGER.md`` for the rationale of every row."""


# --------------------------------------------------------------------------------------------------------------
# Exceptions raised by builder.llm itself
# --------------------------------------------------------------------------------------------------------------


class LLMError(Exception):
    """Base class of the exceptions raised by the ``builder.llm`` package."""


class ProviderError(LLMError):
    """A provider adapter's own failure, optionally pre-classified.

    Adapters raise it when the SDK reports a failure without raising (e.g. the Claude Agent SDK yields an
    ``AssistantMessage`` with ``error="billing_error"``). ``classify_error`` trusts ``kind`` when it is set and
    otherwise falls back to ``status_code`` and the message.
    """

    def __init__(
        self,
        message: str,
        *,
        kind: ErrorKind | None = None,
        status_code: int | None = None,
        retry_after_s: float | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.kind = kind
        self.status_code = status_code
        self.retry_after_s = retry_after_s


class ParseError(LLMError, ValueError):
    """Structured output could not be extracted from the model reply or did not validate.

    Attributes:
        raw_text: The model reply that failed to parse (kept for the repair prompt; never logged in full).
    """

    def __init__(self, message: str, *, raw_text: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.raw_text = raw_text


class AllProvidersFailed(LLMError, RuntimeError):
    """Every provider / key in the chain failed (or none was usable) for one gateway call.

    Attributes:
        purpose: The one-line purpose of the call (as written to the trace).
        attempts: Every attempt that was made, in order (``AttemptRecord`` objects).
        reasons: Human-readable notes on providers that were skipped without an attempt.
    """

    def __init__(
        self,
        purpose: str,
        attempts: Sequence[AttemptRecord],
        reasons: Sequence[str] = (),
    ) -> None:
        self.purpose = purpose
        self.attempts = list(attempts)
        self.reasons = list(reasons)
        super().__init__(self._summary())

    def _summary(self) -> str:
        parts = [f"All LLM providers failed for {self.purpose!r}"]
        if self.attempts:
            tail = self.attempts[-8:]
            shown = "; ".join(f"{a.provider}/{a.key_label}: {a.error_kind or 'error'}" for a in tail)
            more = f" (+{len(self.attempts) - len(tail)} earlier)" if len(self.attempts) > len(tail) else ""
            parts.append(f"attempts{more}: {shown}")
        else:
            parts.append("no attempt could be made")
        if self.reasons:
            parts.append("skipped: " + "; ".join(self.reasons))
        return " | ".join(parts)


# --------------------------------------------------------------------------------------------------------------
# Classification tables
# --------------------------------------------------------------------------------------------------------------

_QUOTA_PATTERNS: tuple[str, ...] = (
    "insufficient_quota",
    "exceeded your current quota",
    "credit balance is too low",
    "credit balance too low",
    "purchase credits",
    "billing_error",
    "billing details",
    "plans & billing",
    "payment required",
    "api usage limits",
    "usage limit reached",
    "hit your limit",
    "out of credits",
    "no credits",
)
"""Message fragments that unambiguously mean "this key has no money", whatever the HTTP status."""

_CONTEXT_PATTERNS: tuple[str, ...] = (
    "prompt is too long",
    "context_length_exceeded",
    "maximum context length",
    "context window",
    "input is too long",
    "too many input tokens",
    "request too large",
)

_STATUS_KINDS: dict[int, ErrorKind] = {
    400: ErrorKind.BAD_REQUEST,
    401: ErrorKind.AUTH,
    402: ErrorKind.QUOTA,
    403: ErrorKind.AUTH,
    404: ErrorKind.BAD_REQUEST,
    408: ErrorKind.TIMEOUT,
    409: ErrorKind.BAD_REQUEST,
    413: ErrorKind.CONTEXT,
    422: ErrorKind.BAD_REQUEST,
    429: ErrorKind.RATE_LIMIT,
    503: ErrorKind.OVERLOADED,
    504: ErrorKind.TIMEOUT,
    529: ErrorKind.OVERLOADED,
}

_TYPE_KINDS: dict[str, ErrorKind] = {
    # Anthropic error.type values
    "billing_error": ErrorKind.QUOTA,
    "authentication_error": ErrorKind.AUTH,
    "permission_error": ErrorKind.AUTH,
    "rate_limit_error": ErrorKind.RATE_LIMIT,
    "overloaded_error": ErrorKind.OVERLOADED,
    "timeout_error": ErrorKind.TIMEOUT,
    "api_error": ErrorKind.SERVER,
    "not_found_error": ErrorKind.BAD_REQUEST,
    "invalid_request_error": ErrorKind.BAD_REQUEST,
    # OpenAI error.code / error.type values
    "insufficient_quota": ErrorKind.QUOTA,
    "invalid_api_key": ErrorKind.AUTH,
    "rate_limit_exceeded": ErrorKind.RATE_LIMIT,
    "server_error": ErrorKind.SERVER,
    "model_not_found": ErrorKind.BAD_REQUEST,
    "context_length_exceeded": ErrorKind.CONTEXT,
    # Claude Agent SDK AssistantMessage.error values
    "authentication_failed": ErrorKind.AUTH,
    "rate_limit": ErrorKind.RATE_LIMIT,
    "invalid_request": ErrorKind.BAD_REQUEST,
}

# Class names (anywhere in the MRO) -> kind. Order matters: timeouts subclass connection errors in both SDKs.
_CLASS_NAME_KINDS: tuple[tuple[str, ErrorKind], ...] = (
    ("ContextOverflowError", ErrorKind.CONTEXT),
    ("APITimeoutError", ErrorKind.TIMEOUT),
    ("ModelTimeoutError", ErrorKind.TIMEOUT),
    ("TimeoutException", ErrorKind.TIMEOUT),  # httpx / httpx2
    ("ReadTimeout", ErrorKind.TIMEOUT),
    ("ConnectTimeout", ErrorKind.TIMEOUT),
    ("WriteTimeout", ErrorKind.TIMEOUT),
    ("PoolTimeout", ErrorKind.TIMEOUT),
    ("TimeoutError", ErrorKind.TIMEOUT),
    ("CLINotFoundError", ErrorKind.AUTH),  # Claude Code CLI missing: the "credential" is unusable
    ("AuthenticationError", ErrorKind.AUTH),
    ("PermissionDeniedError", ErrorKind.AUTH),
    ("ModelAuthenticationError", ErrorKind.AUTH),
    ("ModelPermissionDeniedError", ErrorKind.AUTH),
    ("RateLimitError", ErrorKind.RATE_LIMIT),
    ("ModelRateLimitError", ErrorKind.RATE_LIMIT),
    ("OverloadedError", ErrorKind.OVERLOADED),
    ("ServiceUnavailableError", ErrorKind.OVERLOADED),
    ("DeadlineExceededError", ErrorKind.TIMEOUT),
    ("InternalServerError", ErrorKind.SERVER),
    ("ModelAPIError", ErrorKind.SERVER),
    ("NotFoundError", ErrorKind.BAD_REQUEST),
    ("BadRequestError", ErrorKind.BAD_REQUEST),
    ("UnprocessableEntityError", ErrorKind.BAD_REQUEST),
    ("ModelInvalidRequestError", ErrorKind.BAD_REQUEST),
    ("ModelNotFoundError", ErrorKind.BAD_REQUEST),
    ("APIConnectionError", ErrorKind.CONNECTION),
    ("ModelConnectionError", ErrorKind.CONNECTION),
    ("CLIConnectionError", ErrorKind.CONNECTION),
    ("ConnectError", ErrorKind.CONNECTION),
    ("RemoteProtocolError", ErrorKind.CONNECTION),
    ("NetworkError", ErrorKind.CONNECTION),
    ("TransportError", ErrorKind.CONNECTION),
    ("ConnectionError", ErrorKind.CONNECTION),
    ("OutputParserException", ErrorKind.PARSE),
    ("ValidationError", ErrorKind.PARSE),
    ("JSONDecodeError", ErrorKind.PARSE),
    ("CLIJSONDecodeError", ErrorKind.PARSE),
)

# Message patterns used only when nothing structured (status, type, class) matched.
_MESSAGE_KINDS: tuple[tuple[ErrorKind, tuple[str, ...]], ...] = (
    (
        ErrorKind.AUTH,
        (
            "invalid api key",
            "invalid x-api-key",
            "incorrect api key",
            "invalid_api_key",
            "authentication",
            "unauthorized",
            "permission denied",
            "forbidden",
            "not logged in",
            "please run /login",
            "oauth token",
            "invalid bearer token",
        ),
    ),
    (ErrorKind.RATE_LIMIT, ("rate limit", "rate_limit", "too many requests")),
    (ErrorKind.OVERLOADED, ("overloaded", "service unavailable", "over capacity")),
    (ErrorKind.TIMEOUT, ("timed out", "timeout", "deadline exceeded")),
    (
        ErrorKind.CONNECTION,
        (
            "connection error",
            "connection reset",
            "connection refused",
            "connection aborted",
            "network is unreachable",
            "name resolution",
            "getaddrinfo",
        ),
    ),
    (ErrorKind.SERVER, ("internal server error", "bad gateway", "api_error", "server error")),
    (
        ErrorKind.BAD_REQUEST,
        (
            "invalid_request",
            "model not found",
            "does not exist",
            "unsupported parameter",
            "unsupported value",
            "not supported",
            "bad request",
        ),
    ),
    (ErrorKind.PARSE, ("could not parse", "invalid json", "failed to parse", "validation error")),
)

_STATUS_IN_MESSAGE = re.compile(
    r"\b(?:error code|status code|status|http)\s*[:=]?\s*([45]\d\d)\b", re.IGNORECASE
)
_MAX_CHAIN = 8


# --------------------------------------------------------------------------------------------------------------
# Public helpers
# --------------------------------------------------------------------------------------------------------------


def classify_error(exc: BaseException) -> ErrorKind:
    """Map any exception raised during an LLM call to an :class:`ErrorKind`.

    The exception chain (``__cause__`` / ``__context__`` and exception groups) is inspected in two passes:
    first only *structured* signals (pre-classified :class:`ProviderError`, HTTP status, provider error
    ``type`` / ``code``, quota / context-overflow markers, exception classes), then message patterns. The first
    match wins, so a typed inner SDK error beats a vague wrapper message.

    Args:
        exc: The exception to classify.

    Returns:
        The best matching kind; ``ErrorKind.UNKNOWN`` when nothing matched.
    """
    chain = list(_exception_chain(exc))
    for item in chain:
        kind = _classify_structured(item)
        if kind is not None:
            return kind
    for item in chain:
        kind = _classify_by_message(item)
        if kind is not None:
            return kind
    return ErrorKind.UNKNOWN


def retry_after_seconds(exc: BaseException) -> float | None:
    """Return the server-suggested wait before retrying, if the error carries one.

    Looks (along the exception chain) at ``ProviderError.retry_after_s``, a ``retry_after`` attribute, and the
    HTTP response headers ``retry-after-ms`` / ``retry-after`` (seconds or an HTTP date).

    Args:
        exc: The failure.

    Returns:
        Seconds to wait (>= 0), or ``None`` when the error gives no hint.
    """
    for item in _exception_chain(exc):
        for attr in ("retry_after_s", "retry_after"):
            value = getattr(item, attr, None)
            if isinstance(value, int | float) and not isinstance(value, bool) and value >= 0:
                return float(value)
        headers = getattr(getattr(item, "response", None), "headers", None)
        seconds = _retry_after_from_headers(headers)
        if seconds is not None:
            return seconds
    return None


def describe_error(exc: BaseException, *, limit: int = 300, secrets: Iterable[str] = ()) -> str:
    """One-line, length-capped, secret-free description of an exception for traces and health files.

    Args:
        exc: The exception.
        limit: Maximum length of the returned string.
        secrets: Known secret values to mask (on top of the generic key patterns).

    Returns:
        ``"<ClassName>: <message>"`` with whitespace collapsed and secrets replaced by ``***``.
    """
    message = str(getattr(exc, "message", "") or exc) or exc.__class__.__name__
    text = f"{exc.__class__.__name__}: {message}"
    text = " ".join(scrub_secrets(text, secrets).split())
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"


_GENERIC_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"sk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_\-]{16,}"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{12,}"),
    re.compile(r"(?i)(x-api-key['\"]?\s*[:=]\s*['\"]?)[A-Za-z0-9._\-]{12,}"),
)


def scrub_secrets(text: str, secrets: Iterable[str] = ()) -> str:
    """Replace known secrets and anything that looks like an API key / bearer token with ``***``.

    Args:
        text: Any text (error message, reason, log line).
        secrets: Exact secret values to mask; values shorter than 6 characters are ignored so that common
            words are never masked by accident.

    Returns:
        The scrubbed text.
    """
    for secret in sorted({s for s in secrets if s and len(s) >= 6}, key=len, reverse=True):
        text = text.replace(secret, "***")
    for pattern in _GENERIC_SECRET_PATTERNS:
        text = pattern.sub(lambda m: (m.group(1) if m.groups() else "") + "***", text)
    return text


# --------------------------------------------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------------------------------------------


def _exception_chain(exc: BaseException) -> Iterator[BaseException]:
    """Yield ``exc``, its causes/contexts and exception-group members (cycle-safe, bounded)."""
    seen: set[int] = set()
    queue: list[BaseException] = [exc]
    while queue and len(seen) < _MAX_CHAIN:
        item = queue.pop(0)
        if id(item) in seen:
            continue
        seen.add(id(item))
        yield item
        if isinstance(item, BaseExceptionGroup):
            queue.extend(item.exceptions)
        nxt = item.__cause__ or item.__context__
        if nxt is not None:
            queue.append(nxt)


def _classify_structured(exc: BaseException) -> ErrorKind | None:
    if isinstance(exc, ProviderError) and exc.kind is not None:
        return exc.kind
    if isinstance(exc, ParseError):
        return ErrorKind.PARSE
    text = _error_text(exc)
    if _contains_any(text, _QUOTA_PATTERNS):
        return ErrorKind.QUOTA
    if _contains_any(text, _CONTEXT_PATTERNS):
        return ErrorKind.CONTEXT
    status = _status_code(exc)
    if status is not None:
        return _kind_for_status(status)
    for error_type in _error_types(exc):
        if error_type in _TYPE_KINDS:
            return _TYPE_KINDS[error_type]
    sdk_kind = _loaded_sdk_kind(exc)
    if sdk_kind is not None:
        return sdk_kind
    return _class_name_kind(exc)


def _classify_by_message(exc: BaseException) -> ErrorKind | None:
    text = _error_text(exc)
    match = _STATUS_IN_MESSAGE.search(text)
    if match:
        return _kind_for_status(int(match.group(1)))
    for kind, patterns in _MESSAGE_KINDS:
        if _contains_any(text, patterns):
            return kind
    return None


def _kind_for_status(status: int) -> ErrorKind:
    if status in _STATUS_KINDS:
        return _STATUS_KINDS[status]
    if status >= 500:
        return ErrorKind.SERVER
    if 400 <= status < 500:
        return ErrorKind.BAD_REQUEST
    return ErrorKind.UNKNOWN


def _status_code(exc: BaseException) -> int | None:
    for attr in ("status_code", "http_status", "api_error_status", "status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int) and not isinstance(value, bool) and 100 <= value <= 599:
            return value
    response = getattr(exc, "response", None)
    value = getattr(response, "status_code", None)
    if isinstance(value, int) and not isinstance(value, bool) and 100 <= value <= 599:
        return value
    return None


def _error_types(exc: BaseException) -> list[str]:
    """Collect provider error ``type`` / ``code`` strings from attributes and the response body."""
    found: list[str] = []
    for attr in ("type", "code", "error"):
        value = getattr(exc, attr, None)
        if isinstance(value, str):
            found.append(value.lower())
    body = getattr(exc, "body", None)
    for mapping in _body_mappings(body):
        for field in ("type", "code"):
            value = mapping.get(field)
            if isinstance(value, str):
                found.append(value.lower())
    # Anthropic bodies have type="error" at the top level; that is not informative.
    return [t for t in found if t != "error"]


def _body_mappings(body: object) -> list[dict[str, Any]]:
    if not isinstance(body, dict):
        return []
    mappings = [body]
    inner = body.get("error")
    if isinstance(inner, dict):
        mappings.insert(0, inner)
    return mappings


def _error_text(exc: BaseException) -> str:
    """Lower-cased concatenation of everything textual an exception carries."""
    parts: list[str] = []
    message = getattr(exc, "message", None)
    if isinstance(message, str):
        parts.append(message)
    try:
        parts.append(str(exc))
    except Exception:  # pragma: no cover - pathological __str__
        parts.append(exc.__class__.__name__)
    body = getattr(exc, "body", None)
    if body is not None:
        try:
            parts.append(json.dumps(body, default=str)[:2000] if not isinstance(body, str) else body[:2000])
        except (TypeError, ValueError):  # pragma: no cover - defensive
            parts.append(str(body)[:2000])
    for attr in ("code", "type", "error", "result", "stderr"):
        value = getattr(exc, attr, None)
        if isinstance(value, str):
            parts.append(value)
    errors = getattr(exc, "errors", None)
    if isinstance(errors, list):
        parts.extend(str(e) for e in errors[:5])
    return " ".join(parts).lower()


def _contains_any(text: str, patterns: Iterable[str]) -> bool:
    return any(p in text for p in patterns)


def _class_names(exc: BaseException) -> list[str]:
    return [cls.__name__ for cls in type(exc).__mro__]


def _class_name_kind(exc: BaseException) -> ErrorKind | None:
    names = _class_names(exc)
    for name, kind in _CLASS_NAME_KINDS:
        if name in names:
            return kind
    if isinstance(exc, TimeoutError):  # asyncio.TimeoutError is TimeoutError on 3.11+
        return ErrorKind.TIMEOUT
    if isinstance(exc, ConnectionError):
        return ErrorKind.CONNECTION
    return None


def _loaded_sdk_kind(exc: BaseException) -> ErrorKind | None:
    """isinstance checks against the real SDK classes, only for SDKs that are already imported.

    Status-carrying SDK errors are handled earlier by their ``status_code``; this covers the status-less ones
    (timeouts and connection errors), where ``APITimeoutError`` must win over its base ``APIConnectionError``.
    """
    for module_name in ("anthropic", "openai"):
        module = sys.modules.get(module_name)
        if module is None:
            continue
        timeout_cls = getattr(module, "APITimeoutError", None)
        connection_cls = getattr(module, "APIConnectionError", None)
        if isinstance(timeout_cls, type) and isinstance(exc, timeout_cls):
            return ErrorKind.TIMEOUT
        if isinstance(connection_cls, type) and isinstance(exc, connection_cls):
            return ErrorKind.CONNECTION
    return None


def _retry_after_from_headers(headers: object) -> float | None:
    getter = getattr(headers, "get", None)
    if getter is None:
        return None
    try:
        retry_ms = getter("retry-after-ms")
        if retry_ms is not None:
            return max(0.0, float(retry_ms) / 1000.0)
        retry = getter("retry-after")
    except (TypeError, ValueError):
        return None
    if retry is None:
        return None
    try:
        return max(0.0, float(retry))
    except (TypeError, ValueError):
        pass
    try:
        when = parsedate_to_datetime(str(retry))
    except (TypeError, ValueError, IndexError):
        return None
    if when is None:  # pragma: no cover - older Pythons
        return None
    from datetime import UTC, datetime

    return max(0.0, (when - datetime.now(UTC)).total_seconds())
