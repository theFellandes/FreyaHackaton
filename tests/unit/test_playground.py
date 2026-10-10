"""Playground conversation and mock bank adapters, with no external requests."""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import Mock

import pytest

from builder.freya.client import ChatTurn, FreyaClient
from builder.playground import ChatSession, PracticeBank, json_object, public_error, saved_workflows
from builder.settings import Settings
from builder.trace import NullTrace


def test_chat_continues_same_workflow_and_sends_initial_configuration_once() -> None:
    client = Mock(spec=FreyaClient)
    client.chat.side_effect = [
        ChatTurn(response="Hello", call_id="call-1"),
        ChatTurn(response="Understood", call_id="call-1"),
    ]
    variables = {"customer_id": "customer-7", "balance": 125}
    session = ChatSession(client, "workflow-2", variables, version_id="version-4")

    session.send("Hi")
    session.send("I can pay tomorrow")

    assert client.chat.call_args_list[0].args == ("workflow-2", "Hi")
    assert client.chat.call_args_list[0].kwargs == {
        "customer_variables": variables,
        "call_id": None,
        "version_id": "version-4",
    }
    assert client.chat.call_args_list[1].args == ("workflow-2", "I can pay tomorrow")
    assert client.chat.call_args_list[1].kwargs == {
        "customer_variables": None,
        "call_id": "call-1",
        "version_id": None,
    }
    assert (session.workflow_id, session.variables, session.version_id) == (
        "workflow-2",
        {"customer_id": "customer-7", "balance": 125},
        "version-4",
    )
    assert session.messages == [
        {"role": "user", "content": "Hi"},
        {"role": "assistant", "content": "Hello"},
        {"role": "user", "content": "I can pay tomorrow"},
        {"role": "assistant", "content": "Understood"},
    ]
    assert session.call_id == "call-1"
    assert not session.ended


def test_agent_ending_call_prevents_more_messages_or_duplicate_end() -> None:
    client = Mock(spec=FreyaClient)
    client.chat.return_value = ChatTurn(response="Goodbye", call_id="call-1", raw={"endedByAgent": True})
    session = ChatSession(client, "workflow-2", {})

    session.send("Goodbye")
    with pytest.raises(ValueError, match="conversation has ended"):
        session.send("Another message")
    session.end()

    assert session.ended
    assert client.chat.call_count == 1
    client.end_chat.assert_not_called()
    assert len(session.messages) == 2


@pytest.mark.parametrize("message", ["", "   ", "\n\t"])
def test_blank_chat_message_makes_no_request(message: str) -> None:
    client = Mock(spec=FreyaClient)
    session = ChatSession(client, "workflow-2", {})

    with pytest.raises(ValueError, match="Enter a message"):
        session.send(message)

    client.chat.assert_not_called()
    assert session.messages == []
    assert session.call_id is None


@pytest.mark.parametrize("call_id", [None, "existing-call"])
def test_failed_chat_preserves_transcript_and_call_id(call_id: str | None) -> None:
    client = Mock(spec=FreyaClient)
    client.chat.side_effect = RuntimeError("chat unavailable")
    messages = [{"role": "assistant", "content": "Previous reply"}]
    session = ChatSession(client, "workflow-2", {}, call_id=call_id, messages=messages.copy())

    with pytest.raises(RuntimeError, match="chat unavailable"):
        session.send("New message")

    assert session.messages == messages
    assert session.call_id == call_id
    assert not session.ended
    assert client.chat.call_count == 1


def test_end_is_idempotent_for_started_chat() -> None:
    client = Mock(spec=FreyaClient)
    session = ChatSession(client, "workflow-2", {}, call_id="call-1")

    session.end()
    session.end()

    assert session.ended
    client.end_chat.assert_called_once_with("call-1")
    with pytest.raises(ValueError, match="conversation has ended"):
        session.send("Hello again")
    client.chat.assert_not_called()


def test_end_before_first_message_makes_no_request() -> None:
    client = Mock(spec=FreyaClient)
    session = ChatSession(client, "workflow-2", {})

    session.end()
    session.end()

    assert session.ended
    client.end_chat.assert_not_called()


def test_failed_end_keeps_chat_active_and_can_be_retried() -> None:
    client = Mock(spec=FreyaClient)
    client.end_chat.side_effect = [RuntimeError("end unavailable"), {"ended": True}]
    session = ChatSession(client, "workflow-2", {}, call_id="call-1")

    with pytest.raises(RuntimeError, match="end unavailable"):
        session.end()

    assert not session.ended
    assert session.call_id == "call-1"
    session.end()
    assert session.ended
    assert client.end_chat.call_count == 2


@pytest.fixture
def bank_settings() -> Settings:
    return Settings(_env_file=None, freya_bank_team_key="practice-team-secret")


def test_practice_session_posts_case_and_returns_customer_variables(
    bank_settings: Settings, null_trace: NullTrace, monkeypatch: pytest.MonkeyPatch
) -> None:
    variables = {"customer_id": "customer-7", "account_reference": "account-2"}
    response = io.BytesIO(json.dumps({"variables": variables}).encode())
    urlopen = Mock(return_value=response)
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    bank = PracticeBank("https://bank.example.invalid/", bank_settings, null_trace)

    assert bank.create_session("case-4") == variables

    urlopen.assert_called_once()
    request = urlopen.call_args.args[0]
    assert request.full_url == "https://bank.example.invalid/practice/sessions"
    assert request.method == "POST"
    assert request.get_header("X-team-key") == "practice-team-secret"
    assert json.loads(request.data) == {"case": "case-4"}
    assert urlopen.call_args.kwargs == {"timeout": 30}


def test_practice_ledger_get_encodes_customer_id_as_one_path_segment(
    bank_settings: Settings, null_trace: NullTrace, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = {"payments": [{"amount": 125}], "promises": []}
    urlopen = Mock(return_value=io.BytesIO(json.dumps(ledger).encode()))
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    bank = PracticeBank("https://bank.example.invalid", bank_settings, null_trace)

    assert bank.ledger("customer/7 ?#") == ledger

    urlopen.assert_called_once()
    request = urlopen.call_args.args[0]
    assert request.full_url == "https://bank.example.invalid/practice/ledger/customer%2F7%20%3F%23"
    assert request.method == "GET"
    assert request.data is None
    assert request.get_header("X-team-key") == "practice-team-secret"


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        ([], "non-object response"),
        ({"error": "unknown case"}, "unknown case"),
        ({}, "no customer variables/customer_id"),
        ({"variables": []}, "no customer variables/customer_id"),
        ({"variables": {"customer_id": ""}}, "no customer variables/customer_id"),
    ],
)
def test_invalid_practice_session_does_not_retry_post(
    payload: object,
    error: str,
    bank_settings: Settings,
    null_trace: NullTrace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    urlopen = Mock(return_value=io.BytesIO(json.dumps(payload).encode()))
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    bank = PracticeBank("https://bank.example.invalid", bank_settings, null_trace)

    with pytest.raises(ValueError, match=error):
        bank.create_session("case-4")

    urlopen.assert_called_once()


def test_practice_network_failure_is_reported_without_retrying(
    bank_settings: Settings, null_trace: NullTrace, monkeypatch: pytest.MonkeyPatch
) -> None:
    urlopen = Mock(side_effect=TimeoutError("bank unavailable"))
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    bank = PracticeBank("https://bank.example.invalid", bank_settings, null_trace)

    with pytest.raises(TimeoutError, match="bank unavailable"):
        bank.create_session("case-4")

    urlopen.assert_called_once()


def test_practice_request_telemetry_records_duration_status_and_no_credentials_or_body(
    bank_settings: Settings, null_trace: NullTrace, monkeypatch: pytest.MonkeyPatch
) -> None:
    variables = {"customer_id": "customer-7", "private_field": "not-for-request-telemetry"}
    response = io.BytesIO(json.dumps({"variables": variables}).encode())
    response.status = 201
    monkeypatch.setattr(urllib.request, "urlopen", Mock(return_value=response))
    timestamps = iter([4.0, 4.035])
    bank = PracticeBank(
        "https://bank.example.invalid", bank_settings, null_trace, monotonic=lambda: next(timestamps)
    )

    assert bank.create_session("private-case-id") == variables

    requests = null_trace.of_kind("request")
    assert len(requests) == 1
    data = requests[0]["data"]
    assert data["service"] == "practice_bank"
    assert data["scope"] == "http"
    assert data["operation"] == "create_session"
    assert data["method"] == "POST"
    assert data["path"] == "/practice/sessions"
    assert data["status"] == 201
    assert data["duration_ms"] == 35.0
    assert data["ok"] is True
    assert data["attempt"] == data["max_attempts"] == 1
    assert data["retry_scheduled"] is False
    assert "practice-team-secret" not in json.dumps(requests)
    assert "private-case-id" not in json.dumps(requests)
    assert "not-for-request-telemetry" not in json.dumps(requests)
    assert len(null_trace.of_kind("practice_bank")) == 1  # keep existing operation trace


def test_practice_http_error_telemetry_has_status_without_retries_or_headers(
    bank_settings: Settings, null_trace: NullTrace, monkeypatch: pytest.MonkeyPatch
) -> None:
    failure = urllib.error.HTTPError(
        "https://bank.example.invalid/practice/sessions",
        503,
        "unavailable",
        {},
        io.BytesIO(b"private-error-body"),
    )
    urlopen = Mock(side_effect=failure)
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    timestamps = iter([7.0, 7.125])
    bank = PracticeBank(
        "https://bank.example.invalid", bank_settings, null_trace, monotonic=lambda: next(timestamps)
    )

    with pytest.raises(urllib.error.HTTPError) as caught:
        bank.create_session("case-4")

    assert caught.value is failure
    urlopen.assert_called_once()
    requests = null_trace.of_kind("request")
    assert len(requests) == 1
    data = requests[0]["data"]
    assert data["ok"] is False
    assert data["status"] == 503
    assert data["error_type"] == "HTTPError"
    assert data["duration_ms"] == 125.0
    assert data["retryable"] is True
    assert data["retry_scheduled"] is False
    assert data["attempt"] == data["max_attempts"] == 1
    assert "private-error-body" not in json.dumps(requests)
    assert "headers" not in data


def test_practice_transport_error_telemetry_keeps_status_unknown(
    bank_settings: Settings, null_trace: NullTrace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(urllib.request, "urlopen", Mock(side_effect=TimeoutError("bank unavailable")))
    bank = PracticeBank("https://bank.example.invalid", bank_settings, null_trace)

    with pytest.raises(TimeoutError):
        bank.ledger("customer-7")

    data = null_trace.of_kind("request")[0]["data"]
    assert data["operation"] == "ledger"
    assert data["method"] == "GET"
    assert data["path"] == "/practice/ledger/customer-7"
    assert data["ok"] is False
    assert "status" not in data
    assert data["error_type"] == "TimeoutError"
    assert data["retry_scheduled"] is False


def test_practice_response_error_traces_redact_team_key_even_without_configured_trace_redactor(
    bank_settings: Settings, null_trace: NullTrace, monkeypatch: pytest.MonkeyPatch
) -> None:
    response = io.BytesIO(json.dumps({"error": "invalid practice-team-secret"}).encode())
    monkeypatch.setattr(urllib.request, "urlopen", Mock(return_value=response))
    bank = PracticeBank("https://bank.example.invalid", bank_settings, null_trace)

    with pytest.raises(ValueError, match="invalid"):
        bank.create_session("case-4")

    assert "practice-team-secret" not in json.dumps(null_trace.events)
    assert null_trace.of_kind("request")[0]["data"]["ok"] is False


def test_practice_trace_failure_does_not_break_successful_request(
    bank_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    response = io.BytesIO(json.dumps({"payments": []}).encode())
    monkeypatch.setattr(urllib.request, "urlopen", Mock(return_value=response))
    trace = Mock()
    trace.event.side_effect = RuntimeError("trace unavailable")
    bank = PracticeBank("https://bank.example.invalid", bank_settings, trace)

    assert bank.ledger("customer-7") == {"payments": []}


def test_practice_bank_requires_team_key(settings: Settings, null_trace: NullTrace) -> None:
    with pytest.raises(ValueError, match="FREYA_BANK_TEAM_KEY"):
        PracticeBank("https://bank.example.invalid", settings, null_trace)


@pytest.mark.parametrize("base_url", ["", "bank.example.invalid", "file:///tmp/bank"])
def test_practice_bank_requires_http_url(
    base_url: str, bank_settings: Settings, null_trace: NullTrace
) -> None:
    with pytest.raises(ValueError, match="HTTP server URL"):
        PracticeBank(base_url, bank_settings, null_trace)


@pytest.mark.parametrize("content", ["{broken", "[]", "null", '"workflow-id"'])
def test_saved_workflows_ignores_invalid_state(content: str, tmp_path: Path) -> None:
    path = tmp_path / "agents.json"
    path.write_text(content, encoding="utf-8")

    assert saved_workflows(path) == {}


def test_saved_workflows_missing_file_is_empty(tmp_path: Path) -> None:
    assert saved_workflows(tmp_path / "missing.json") == {}


def test_saved_workflows_keeps_valid_entries_and_skips_malformed_records(tmp_path: Path) -> None:
    path = tmp_path / "agents.json"
    path.write_text(
        json.dumps(
            {
                "brief-a": {"workflow_id": "workflow-1", "agent_id": "agent-1"},
                "brief-b": {"workflow_id": "workflow-2"},
                "missing": {"agent_id": "agent-3"},
                "empty": {"workflow_id": ""},
                "null": None,
                "list": [{"workflow_id": "workflow-3"}],
                "scalar": "workflow-4",
            }
        ),
        encoding="utf-8",
    )

    assert saved_workflows(path) == {"brief-a": "workflow-1", "brief-b": "workflow-2"}


def test_json_editor_accepts_object_with_nested_values() -> None:
    assert json_object('{"customer_id": "customer-7", "flags": [true, null]}') == {
        "customer_id": "customer-7",
        "flags": [True, None],
    }


@pytest.mark.parametrize("text", ["[]", "null", '"customer-7"', "123", "true"])
def test_json_editor_rejects_non_object(text: str) -> None:
    with pytest.raises(ValueError, match="JSON object"):
        json_object(text)


def test_json_editor_rejects_malformed_json() -> None:
    with pytest.raises(json.JSONDecodeError):
        json_object("{customer_id: customer-7}")


def test_public_error_redacts_configured_and_environment_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        _env_file=None,
        freya_api_key="fak_test-platform-secret",
        freya_bank_team_key="practice-team-secret",
    )
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-provider-secret")
    error = RuntimeError(
        "Request failed: fak_test-platform-secret / practice-team-secret / sk-test-provider-secret"
    )

    assert public_error(error, settings) == "Request failed: [REDACTED] / [REDACTED] / [REDACTED]"


def test_public_error_preserves_useful_message_without_secrets(settings: Settings) -> None:
    assert public_error(ValueError("Workflow is missing nodes"), settings) == "Workflow is missing nodes"
