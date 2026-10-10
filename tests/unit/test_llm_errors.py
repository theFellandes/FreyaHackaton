"""Tests for builder.llm.errors: error classification, retry-after parsing, recovery policy, scrubbing.

The classification table uses constructed exceptions only (no network): duck-typed fakes that mimic the SDKs'
attributes (status_code / message / body / code / type), real SDK exception classes instantiated locally with an
offline ``httpx2`` request/response, and generic Python exceptions.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from builder._compat import ensure_uuid_utils
from builder.llm.errors import (
    POLICY,
    Action,
    AllProvidersFailed,
    ErrorKind,
    KeyEffect,
    ParseError,
    ProviderError,
    classify_error,
    describe_error,
    retry_after_seconds,
    scrub_secrets,
)
from builder.llm.gateway import AttemptRecord

# ----------------------------------------------------------------------------------------------- fake SDK errors

ANTHROPIC_CREDIT_MSG = (
    "Your credit balance is too low to access the Anthropic API. Please go to Plans & Billing to upgrade or "
    "purchase credits."
)
OPENAI_QUOTA_MSG = (
    "You exceeded your current quota, please check your plan and billing details. For more information on this "
    "error, read the docs: https://platform.openai.com/docs/guides/error-codes/api-errors."
)
OPENAI_RATE_MSG = (
    "Rate limit reached for gpt-5.6-sol in organization org-abc on tokens per min (TPM): Limit 30000, Used 29000, "
    "Requested 2000. Please try again in 2s."
)
ANTHROPIC_RATE_MSG = (
    "This request would exceed the rate limit for your organization of 50,000 input tokens per minute. "
    "Please reduce the prompt length or the maximum tokens requested, or try again later."
)


class FakeAPIStatusError(Exception):
    """Duck-typed stand-in for anthropic/openai ``APIStatusError``."""

    def __init__(
        self,
        message: str,
        status_code: int,
        body: Any = None,
        headers: dict[str, str] | None = None,
        code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.body = body
        self.code = code
        self.response = SimpleNamespace(status_code=status_code, headers=headers or {})


class BadRequestError(FakeAPIStatusError):
    pass


class RateLimitError(FakeAPIStatusError):
    pass


class APIConnectionError(Exception):
    pass


class APITimeoutError(APIConnectionError):
    pass


class CLINotFoundError(Exception):
    pass


class FakeResultError(Exception):
    """Mimics claude_agent_sdk.ResultError (api_error_status + result text, no status_code)."""

    def __init__(self, message: str, api_error_status: int | None, result: str | None = None) -> None:
        super().__init__(message)
        self.api_error_status = api_error_status
        self.result = result


def anthropic_body(error_type: str, message: str) -> dict[str, Any]:
    return {"type": "error", "error": {"type": error_type, "message": message}}


def openai_body(message: str, error_type: str, code: str | None) -> dict[str, Any]:
    return {"message": message, "type": error_type, "param": None, "code": code}


def _validation_error() -> ValidationError:
    class M(BaseModel):
        x: int

    try:
        M.model_validate({"x": "not a number"})
    except ValidationError as exc:
        return exc
    raise AssertionError("expected a ValidationError")


def _json_error() -> json.JSONDecodeError:
    try:
        json.loads("{nope")
    except json.JSONDecodeError as exc:
        return exc
    raise AssertionError("expected a JSONDecodeError")


CLASSIFICATION_TABLE: list[tuple[str, BaseException, ErrorKind]] = [
    # --- quota / credits (must win over the HTTP status) ---
    (
        "anthropic credit balance (400 invalid_request_error)",
        BadRequestError(
            f"Error code: 400 - {anthropic_body('invalid_request_error', ANTHROPIC_CREDIT_MSG)}",
            400,
            anthropic_body("invalid_request_error", ANTHROPIC_CREDIT_MSG),
        ),
        ErrorKind.QUOTA,
    ),
    (
        "openai insufficient_quota (429)",
        RateLimitError(
            OPENAI_QUOTA_MSG,
            429,
            openai_body(OPENAI_QUOTA_MSG, "insufficient_quota", "insufficient_quota"),
            code="insufficient_quota",
        ),
        ErrorKind.QUOTA,
    ),
    ("http 402", FakeAPIStatusError("Payment Required", 402), ErrorKind.QUOTA),
    (
        "anthropic billing_error type",
        FakeAPIStatusError("organization disabled", 400, anthropic_body("billing_error", "disabled")),
        ErrorKind.QUOTA,
    ),
    (
        "anthropic spend limit reached",
        BadRequestError(
            "You have reached your specified API usage limits. You will regain access on 2026-11-01", 400
        ),
        ErrorKind.QUOTA,
    ),
    # --- auth ---
    (
        "anthropic 401 invalid x-api-key",
        FakeAPIStatusError(
            "invalid x-api-key", 401, anthropic_body("authentication_error", "invalid x-api-key")
        ),
        ErrorKind.AUTH,
    ),
    (
        "anthropic 403 permission_error",
        FakeAPIStatusError("no access", 403, anthropic_body("permission_error", "no access to model")),
        ErrorKind.AUTH,
    ),
    (
        "openai 401 incorrect key",
        FakeAPIStatusError(
            "Incorrect API key provided: sk-proj-****abcd.",
            401,
            openai_body("Incorrect API key provided", "invalid_request_error", "invalid_api_key"),
        ),
        ErrorKind.AUTH,
    ),
    # --- rate limit ---
    (
        "anthropic 429 rate_limit_error",
        RateLimitError(ANTHROPIC_RATE_MSG, 429, anthropic_body("rate_limit_error", ANTHROPIC_RATE_MSG)),
        ErrorKind.RATE_LIMIT,
    ),
    (
        "openai 429 rate_limit_exceeded",
        RateLimitError(OPENAI_RATE_MSG, 429, openai_body(OPENAI_RATE_MSG, "requests", "rate_limit_exceeded")),
        ErrorKind.RATE_LIMIT,
    ),
    # --- overloaded / server / timeout ---
    (
        "anthropic 529 overloaded",
        FakeAPIStatusError("Overloaded", 529, anthropic_body("overloaded_error", "Overloaded")),
        ErrorKind.OVERLOADED,
    ),
    ("503 service unavailable", FakeAPIStatusError("Service Unavailable", 503), ErrorKind.OVERLOADED),
    ("500 internal", FakeAPIStatusError("Internal server error", 500), ErrorKind.SERVER),
    ("502 bad gateway", FakeAPIStatusError("Bad Gateway", 502), ErrorKind.SERVER),
    ("504 gateway timeout", FakeAPIStatusError("Gateway Timeout", 504), ErrorKind.TIMEOUT),
    ("408 request timeout", FakeAPIStatusError("Request Timeout", 408), ErrorKind.TIMEOUT),
    # --- bad request / context ---
    (
        "404 model not found",
        FakeAPIStatusError("model: claude-x", 404, anthropic_body("not_found_error", "model: claude-x")),
        ErrorKind.BAD_REQUEST,
    ),
    ("422 unprocessable", FakeAPIStatusError("Unprocessable Entity", 422), ErrorKind.BAD_REQUEST),
    (
        "400 unsupported parameter",
        BadRequestError("Unsupported parameter: 'temperature' is not supported with this model.", 400),
        ErrorKind.BAD_REQUEST,
    ),
    (
        "anthropic prompt too long",
        BadRequestError("prompt is too long: 250123 tokens > 200000 maximum", 400),
        ErrorKind.CONTEXT,
    ),
    (
        "openai context_length_exceeded",
        BadRequestError(
            "This model's maximum context length is 128000 tokens.",
            400,
            openai_body("too long", "invalid_request_error", "context_length_exceeded"),
        ),
        ErrorKind.CONTEXT,
    ),
    ("413 request too large", FakeAPIStatusError("request_too_large", 413), ErrorKind.CONTEXT),
    # --- transport ---
    ("asyncio.TimeoutError", TimeoutError(), ErrorKind.TIMEOUT),
    ("builtin TimeoutError", TimeoutError("slow"), ErrorKind.TIMEOUT),
    ("ConnectionResetError", ConnectionResetError("reset by peer"), ErrorKind.CONNECTION),
    (
        "duck APITimeoutError subclass of APIConnectionError",
        APITimeoutError("Request timed out."),
        ErrorKind.TIMEOUT,
    ),
    ("duck APIConnectionError", APIConnectionError("Connection error."), ErrorKind.CONNECTION),
    # --- parse ---
    ("pydantic ValidationError", _validation_error(), ErrorKind.PARSE),
    ("json decode error", _json_error(), ErrorKind.PARSE),
    ("our ParseError", ParseError("no JSON"), ErrorKind.PARSE),
    # --- ProviderError ---
    ("ProviderError with kind", ProviderError("billing", kind=ErrorKind.QUOTA), ErrorKind.QUOTA),
    (
        "ProviderError with status only",
        ProviderError("upstream said no", status_code=529),
        ErrorKind.OVERLOADED,
    ),
    # --- Claude Code / Agent SDK ---
    (
        "ResultError-like 529",
        FakeResultError(
            "Claude Code returned an error result", 529, "API Error: Repeated 529 Overloaded errors"
        ),
        ErrorKind.OVERLOADED,
    ),
    ("CLI not found", CLINotFoundError("Claude Code not found"), ErrorKind.AUTH),
    ("login missing message", RuntimeError("Invalid API key · Please run /login"), ErrorKind.AUTH),
    ("not logged in message", RuntimeError("Not logged in · Please run /login"), ErrorKind.AUTH),
    ("subscription limit message", RuntimeError("Claude AI usage limit reached|1760000000"), ErrorKind.QUOTA),
    # --- message-only fallbacks ---
    ("status in message 429", RuntimeError("Error code: 429 - {'type': 'error'}"), ErrorKind.RATE_LIMIT),
    ("status in message 503", RuntimeError("HTTP 503 from upstream"), ErrorKind.OVERLOADED),
    ("overloaded message", RuntimeError("Anthropic is overloaded right now"), ErrorKind.OVERLOADED),
    ("credit message", RuntimeError("Credit balance is too low"), ErrorKind.QUOTA),
    # --- unknown ---
    ("generic RuntimeError", RuntimeError("boom"), ErrorKind.UNKNOWN),
    ("generic ValueError", ValueError("something odd"), ErrorKind.UNKNOWN),
]


@pytest.mark.parametrize(
    ("exc", "expected"),
    [(exc, kind) for _, exc, kind in CLASSIFICATION_TABLE],
    ids=[name for name, _, _ in CLASSIFICATION_TABLE],
)
def test_classification_table(exc: BaseException, expected: ErrorKind) -> None:
    assert classify_error(exc) is expected


def test_wrapped_exception_uses_typed_cause() -> None:
    inner = RateLimitError(ANTHROPIC_RATE_MSG, 429, anthropic_body("rate_limit_error", ANTHROPIC_RATE_MSG))
    try:
        try:
            raise inner
        except RateLimitError as exc:
            raise RuntimeError("model call failed") from exc
    except RuntimeError as outer:
        assert classify_error(outer) is ErrorKind.RATE_LIMIT


def test_typed_inner_error_beats_vague_outer_message() -> None:
    inner = BadRequestError(
        ANTHROPIC_CREDIT_MSG, 400, anthropic_body("invalid_request_error", ANTHROPIC_CREDIT_MSG)
    )
    outer = RuntimeError("request timed out?")
    outer.__cause__ = inner
    assert classify_error(outer) is ErrorKind.QUOTA


def test_exception_group_members_are_inspected() -> None:
    group = ExceptionGroup("task group failed", [FakeAPIStatusError("invalid x-api-key", 401)])
    assert classify_error(group) is ErrorKind.AUTH


def test_cyclic_exception_chain_terminates() -> None:
    a, b = RuntimeError("a"), RuntimeError("b")
    a.__context__, b.__context__ = b, a
    assert classify_error(a) is ErrorKind.UNKNOWN


def test_httpx_transport_errors() -> None:
    httpx = pytest.importorskip("httpx")
    request = httpx.Request("POST", "https://api.example.invalid/v1")
    assert classify_error(httpx.ConnectError("connect failed", request=request)) is ErrorKind.CONNECTION
    assert classify_error(httpx.ReadTimeout("read timed out", request=request)) is ErrorKind.TIMEOUT


# ------------------------------------------------------------------------------- real SDK exception classes


def _httpx2() -> Any:
    return pytest.importorskip("httpx2")


def _response(status: int, headers: dict[str, str] | None = None) -> Any:
    httpx2 = _httpx2()
    request = httpx2.Request("POST", "https://api.example.invalid/v1/messages")
    return httpx2.Response(status, request=request, headers=headers or {})


def test_real_anthropic_sdk_errors() -> None:
    anthropic = pytest.importorskip("anthropic")
    rate = anthropic.RateLimitError(
        "Error code: 429",
        response=_response(429),
        body=anthropic_body("rate_limit_error", ANTHROPIC_RATE_MSG),
    )
    credit = anthropic.BadRequestError(
        f"Error code: 400 - {ANTHROPIC_CREDIT_MSG}",
        response=_response(400),
        body=anthropic_body("invalid_request_error", ANTHROPIC_CREDIT_MSG),
    )
    auth = anthropic.AuthenticationError(
        "Error code: 401",
        response=_response(401),
        body=anthropic_body("authentication_error", "invalid x-api-key"),
    )
    overloaded = anthropic.OverloadedError(
        "Error code: 529", response=_response(529), body=anthropic_body("overloaded_error", "Overloaded")
    )
    server = anthropic.InternalServerError("Error code: 500", response=_response(500), body=None)
    request = _response(200).request
    assert classify_error(rate) is ErrorKind.RATE_LIMIT
    assert classify_error(credit) is ErrorKind.QUOTA
    assert classify_error(auth) is ErrorKind.AUTH
    assert classify_error(overloaded) is ErrorKind.OVERLOADED
    assert classify_error(server) is ErrorKind.SERVER
    assert classify_error(anthropic.APITimeoutError(request=request)) is ErrorKind.TIMEOUT
    assert classify_error(anthropic.APIConnectionError(request=request)) is ErrorKind.CONNECTION


def test_real_openai_sdk_errors() -> None:
    openai = pytest.importorskip("openai")
    quota = openai.RateLimitError(
        OPENAI_QUOTA_MSG,
        response=_response(429),
        body=openai_body(OPENAI_QUOTA_MSG, "insufficient_quota", "insufficient_quota"),
    )
    rate = openai.RateLimitError(
        OPENAI_RATE_MSG,
        response=_response(429),
        body=openai_body(OPENAI_RATE_MSG, "tokens", "rate_limit_exceeded"),
    )
    assert quota.code == "insufficient_quota"
    assert classify_error(quota) is ErrorKind.QUOTA
    assert classify_error(rate) is ErrorKind.RATE_LIMIT
    request = _response(200).request
    assert classify_error(openai.APITimeoutError(request=request)) is ErrorKind.TIMEOUT
    assert classify_error(openai.APIConnectionError(request=request)) is ErrorKind.CONNECTION
    not_found = openai.NotFoundError(
        "The model `gpt-x` does not exist",
        response=_response(404),
        body=openai_body("The model does not exist", "invalid_request_error", "model_not_found"),
    )
    assert classify_error(not_found) is ErrorKind.BAD_REQUEST


def test_langchain_wrapped_anthropic_errors() -> None:
    ensure_uuid_utils()  # a blocked native uuid_utils must not stop LangChain from importing
    chat_models = pytest.importorskip("langchain_anthropic.chat_models")
    rate = chat_models.AnthropicRateLimitError(
        message="rate limited", response=_response(429), body=anthropic_body("rate_limit_error", "slow down")
    )
    overflow = chat_models.AnthropicContextOverflowError(
        message="prompt is too long: 300000 tokens > 200000 maximum", response=_response(400), body=None
    )
    assert classify_error(rate) is ErrorKind.RATE_LIMIT
    assert classify_error(overflow) is ErrorKind.CONTEXT


# --------------------------------------------------------------------------------------------- retry-after


def test_retry_after_seconds_from_headers_and_attributes() -> None:
    assert retry_after_seconds(FakeAPIStatusError("x", 429, headers={"retry-after": "7"})) == 7.0
    assert retry_after_seconds(FakeAPIStatusError("x", 429, headers={"retry-after-ms": "1500"})) == 1.5
    assert retry_after_seconds(FakeAPIStatusError("x", 429, headers={"retry-after": "soon"})) is None
    assert retry_after_seconds(FakeAPIStatusError("x", 429)) is None
    assert retry_after_seconds(ProviderError("x", retry_after_s=3)) == 3.0
    assert retry_after_seconds(RuntimeError("no hint")) is None


def test_retry_after_http_date() -> None:
    when = datetime.now(UTC) + timedelta(seconds=90)
    seconds = retry_after_seconds(
        FakeAPIStatusError("x", 429, headers={"retry-after": format_datetime(when)})
    )
    assert seconds is not None
    assert 80 <= seconds <= 91


def test_retry_after_from_real_sdk_response_headers() -> None:
    anthropic = pytest.importorskip("anthropic")
    exc = anthropic.RateLimitError(
        "Error code: 429", response=_response(429, {"retry-after": "12"}), body=None
    )
    assert retry_after_seconds(exc) == 12.0


def test_retry_after_found_through_wrapper() -> None:
    outer = RuntimeError("wrapped")
    outer.__cause__ = FakeAPIStatusError("x", 429, headers={"retry-after": "4"})
    assert retry_after_seconds(outer) == 4.0


# ------------------------------------------------------------------------------------------------- policy


def test_policy_covers_every_kind() -> None:
    assert set(POLICY) == set(ErrorKind)


@pytest.mark.parametrize("kind", [ErrorKind.QUOTA, ErrorKind.AUTH])
def test_dead_key_kinds_rotate(kind: ErrorKind) -> None:
    assert POLICY[kind].action is Action.ROTATE_KEY
    assert POLICY[kind].key_effect is KeyEffect.DEAD
    assert not POLICY[kind].provider_failure


def test_rate_limit_cools_and_rotates() -> None:
    policy = POLICY[ErrorKind.RATE_LIMIT]
    assert (policy.action, policy.key_effect, policy.provider_failure) == (
        Action.ROTATE_KEY,
        KeyEffect.COOL,
        False,
    )


@pytest.mark.parametrize(
    "kind", [ErrorKind.OVERLOADED, ErrorKind.SERVER, ErrorKind.TIMEOUT, ErrorKind.CONNECTION]
)
def test_transient_kinds_retry_same_key_with_backoff(kind: ErrorKind) -> None:
    policy = POLICY[kind]
    assert policy.action is Action.RETRY_SAME
    assert policy.retry_limit is None  # -> settings.llm_retries_per_key
    assert policy.backoff
    assert policy.exhausted_action is Action.ROTATE_KEY
    assert policy.key_effect is KeyEffect.NONE
    assert policy.provider_failure


@pytest.mark.parametrize("kind", [ErrorKind.BAD_REQUEST, ErrorKind.CONTEXT])
def test_request_errors_go_to_next_provider(kind: ErrorKind) -> None:
    assert POLICY[kind].action is Action.NEXT_PROVIDER
    assert POLICY[kind].key_effect is KeyEffect.NONE


def test_parse_retries_once_then_next_provider() -> None:
    policy = POLICY[ErrorKind.PARSE]
    assert (policy.action, policy.retry_limit, policy.backoff, policy.exhausted_action) == (
        Action.RETRY_SAME,
        1,
        False,
        Action.NEXT_PROVIDER,
    )


def test_unknown_rotates_and_counts_for_circuit() -> None:
    policy = POLICY[ErrorKind.UNKNOWN]
    assert policy.action is Action.ROTATE_KEY
    assert policy.key_effect is KeyEffect.NONE
    assert policy.provider_failure


# ---------------------------------------------------------------------------------------- text helpers


def test_scrub_secrets_masks_known_and_key_shaped_values() -> None:
    secret = "my-very-secret-value-123"
    text = (
        f"key {secret} failed; also sk-ant-api03-abcdefghijklmnop and sk-proj-ABCDEFGHIJKLMNOPQRST "
        "and Bearer abcdefghijklmnopqrstuv"
    )
    scrubbed = scrub_secrets(text, [secret])
    assert secret not in scrubbed
    assert "sk-ant-api03-abcdefghijklmnop" not in scrubbed
    assert "sk-proj-ABCDEFGHIJKLMNOPQRST" not in scrubbed
    assert "abcdefghijklmnopqrstuv" not in scrubbed
    assert scrubbed.count("***") >= 4


def test_scrub_secrets_ignores_tiny_values() -> None:
    assert scrub_secrets("login works", ["login"]) == "login works"


def test_describe_error_is_one_line_capped_and_secret_free() -> None:
    secret = "sk-ant-api03-SECRETSECRETSECRET"
    exc = FakeAPIStatusError(f"bad key {secret}\nsecond line " + "x" * 500, 401)
    text = describe_error(exc, limit=120, secrets=[secret])
    assert "\n" not in text
    assert len(text) <= 120
    assert secret not in text
    assert text.startswith("FakeAPIStatusError: bad key ***")


def test_all_providers_failed_summary() -> None:
    attempts = [
        AttemptRecord("anthropic", "m", "anthropic#1(…1111)", False, ErrorKind.QUOTA, "no credits", 0.1, 1),
        AttemptRecord("openai", "m", "openai#1(…3333)", False, ErrorKind.AUTH, "bad key", 0.1, 2),
    ]
    exc = AllProvidersFailed("extract policy", attempts, ["claude_code: no keys configured"])
    assert isinstance(exc, RuntimeError)
    assert exc.attempts == attempts
    message = str(exc)
    assert "extract policy" in message
    assert "anthropic/anthropic#1(…1111): quota" in message
    assert "openai/openai#1(…3333): auth" in message
    assert "claude_code: no keys configured" in message


def test_all_providers_failed_without_attempts() -> None:
    assert "no attempt could be made" in str(AllProvidersFailed("x", []))
