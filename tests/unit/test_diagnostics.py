"""Offline monitor checks: diagnostics remain useful without disclosing credentials."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from builder.diagnostics import (
    events_from_call,
    is_failed_attempt,
    needs_attention,
    read_trace,
    redact_data,
    trace_rows,
    trace_summary,
)


def test_redaction_masks_credentials_but_preserves_customer_ids_and_usage() -> None:
    value = {
        "Authorization": "Bearer sample-value",
        "api_key": "sample-key",
        "password": "sample-password",
        "refreshToken": "sample-refresh",
        "client_secret": "sample-secret",
        "nested": [{"x-team-key": "sample-team", "customer_id": "customer-1"}],
        "input_tokens": 17,
        "output_tokens": 9,
        "max_tokens": 200,
        "key_label": "openai#1(...abcd)",
    }

    clean = redact_data(value)

    assert all(
        clean[key] == "***"
        for key in ("Authorization", "api_key", "password", "refreshToken", "client_secret")
    )
    assert clean["nested"] == [{"x-team-key": "***", "customer_id": "customer-1"}]
    assert (clean["input_tokens"], clean["output_tokens"], clean["max_tokens"]) == (17, 9, 200)
    assert clean["key_label"] == "openai#1(...abcd)"
    assert value["password"] == "sample-password"  # never modify the caller's data


def test_unknown_custom_header_credentials_are_masked_inside_json_strings() -> None:
    original = json.dumps(
        {
            "event": "function_call_start",
            "args": {
                "headers": {"X-Opaque": "opaque-value", "Content-Type": "application/json"},
                "body": {"customer_id": "customer-1"},
            },
        }
    )

    clean = redact_data({"message": original})
    event = json.loads(clean["message"])

    assert event["args"]["headers"] == {"X-Opaque": "***", "Content-Type": "application/json"}
    assert event["args"]["body"]["customer_id"] == "customer-1"
    assert "opaque-value" not in json.dumps(clean)


def test_nested_json_strings_and_known_secret_substrings_are_masked() -> None:
    value = {
        "message": json.dumps(
            {"result": json.dumps({"password": "unknown-password"}), "error": "failed with configured-value"}
        ),
        "configured-value": "a configured-value suffix",
    }

    clean = redact_data(value, secrets=("", "configured-value"))

    assert "configured-value" not in json.dumps(clean)
    assert "unknown-password" not in json.dumps(clean)
    assert clean["***"] == "a *** suffix"
    assert json.loads(json.loads(clean["message"])["result"])["password"] == "***"


def test_plain_header_strings_and_generic_key_shapes_are_masked() -> None:
    clean = redact_data(
        {
            "headers": "X-Opaque: unknown-value",
            "error": "failed with sk-proj-abcdefghijklmnopqrstuvwxyz and Bearer other-value",
        }
    )

    assert clean["headers"] == "***"
    assert "abcdefghijklmnopqrstuvwxyz" not in clean["error"]
    assert "other-value" not in clean["error"]


def test_query_credentials_are_masked_and_customer_id_remains_visible() -> None:
    clean = redact_data("https://user:sample-password@bank.test/tool?api_key=opaque&customer_id=customer-1")

    assert "sample-password" not in clean
    assert "opaque" not in clean
    assert "customer_id=customer-1" in clean


@pytest.mark.parametrize("prefix", ["/request", "", "//bank.test/request"])
def test_relative_url_query_credentials_are_masked_without_configuration(prefix: str) -> None:
    clean = redact_data({"path": prefix + "?api_key=synthetic-opaque-value&customer_id=c1"})

    assert "synthetic-opaque-value" not in clean["path"]
    assert "customer_id=c1" in clean["path"]
    assert "api_key=" in clean["path"]
    assert clean["path"].startswith(prefix + "?")


@pytest.mark.parametrize("url", ["https://bank.test/request", "/request", ""])
def test_unknown_url_credentials_inside_error_prose_are_masked(url: str) -> None:
    error = f"GET {url}?api_key=synthetic-opaque-value&customer_id=c1 failed"

    clean = redact_data({"error": error})

    assert "synthetic-opaque-value" not in clean["error"]
    assert "customer_id=c1" in clean["error"]
    assert clean["error"].startswith(f"GET {url}?")
    assert clean["error"].endswith(" failed")


def test_embedded_url_redaction_handles_multiple_urls_and_preserves_safe_prose() -> None:
    safe = "GET https://bank.test/account?customer_id=c1&input_tokens=17 failed"
    assert redact_data(safe) == safe
    value = {
        "message": json.dumps(
            {
                "event": "error",
                "message": "GET https://bank.test/one?api_key=synthetic-first-value failed; "
                "POST /two?token=synthetic-second-value failed",
            }
        )
    }

    clean = redact_data(value)

    rendered = json.dumps(clean)
    assert "synthetic-first-value" not in rendered
    assert "synthetic-second-value" not in rendered
    assert json.loads(clean["message"])["message"].endswith(" failed")


def test_unreadable_embedded_json_never_returns_unknown_credentials() -> None:
    limit = sys.get_int_max_str_digits()
    if limit == 0:
        pytest.skip("Python integer-digit limit is disabled")
    document = '{"n":' + "9" * (limit + 1) + ',"api_key":"synthetic-opaque-value"}'

    clean = redact_data({"message": document})

    assert clean == {"message": "<unreadable JSON>"}


def test_redaction_is_bounded_for_recursive_and_nonfinite_values() -> None:
    value: dict = {"number": float("nan")}
    value["self"] = value

    clean = redact_data(value)

    assert clean["number"] is None
    assert "<nested too deep>" in json.dumps(clean, allow_nan=False)


def test_call_events_retain_tool_arguments_routes_results_and_snapshots() -> None:
    call = {
        "messages": [
            {
                "role": "system",
                "secondsFromStart": "0.2",
                "nodeId": "verification",
                "message": json.dumps(
                    {"event": "node_enter", "node": "verification", "from_node": "opening"}
                ),
            },
            {
                "role": "system",
                "secondsFromStart": 1.5,
                "message": json.dumps(
                    {
                        "event": "function_call_start",
                        "function": {"name": "verify_identity"},
                        "args": {
                            "headers": {"X-Opaque": "unknown-value"},
                            "body": {"customer_id": "customer-1"},
                        },
                    }
                ),
            },
            {
                "role": "system",
                "secondsFromStart": 1.7,
                "message": {
                    "event": "function_call_complete",
                    "function": "verify_identity",
                    "result": {"verified": False},
                },
            },
            {
                "role": "system",
                "message": json.dumps(
                    {
                        "event": "route_evaluated",
                        "from_node": "verification",
                        "target": "retry",
                        "matched": True,
                        "branches": [{"variable": "verified", "matched": True}],
                        "variable_table": {"verified": False},
                    }
                ),
            },
            {
                "role": "assistant",
                "secondsFromStart": 2,
                "metadata": {
                    "workflowSnapshot": {
                        "currentNode": "retry",
                        "variableTable": {"verified": {"value": False}},
                    }
                },
            },
            {"role": "user", "workflowSnapshot": {"currentNode": "retry", "nodeTurnCount": 1}},
            {"role": "system", "message": json.dumps({"event": "error", "message": "configured-value"})},
        ]
    }

    rows = events_from_call(call, secrets=("configured-value",))

    assert [row["event"] for row in rows] == [
        "node_enter",
        "function_call_start",
        "function_call_complete",
        "route_evaluated",
        "workflow_snapshot",
        "workflow_snapshot",
        "error",
    ]
    assert rows[0]["elapsed_s"] == 0.2
    assert rows[0]["node"] == "verification"
    assert rows[1]["function"] == "verify_identity"
    assert rows[1]["details"]["args"]["body"]["customer_id"] == "customer-1"
    assert rows[2]["details"]["result"] == {"verified": False}
    assert rows[3]["details"]["branches"] == [{"variable": "verified", "matched": True}]
    assert rows[4]["node"] == "retry"
    assert rows[4]["details"]["workflowSnapshot"]["variableTable"]["verified"]["value"] is False
    assert "configured-value" not in json.dumps(rows)
    assert "unknown-value" not in json.dumps(rows)


def test_call_envelopes_and_malformed_messages_do_not_hide_valid_events() -> None:
    call = {
        "data": {
            "call": {
                "messages": [
                    None,
                    "bad",
                    {"role": "system", "message": "not JSON"},
                    {"role": "system", "message": "[]"},
                    {"role": "system", "message": {"event": "unrecognised"}},
                    {"role": "system", "message": {"event": []}},
                    {"role": "assistant", "message": "regular transcript"},
                    {
                        "role": "system",
                        "content": json.dumps({"type": "node_enter", "data": {"node": "opening"}}),
                    },
                ]
            }
        }
    }

    rows = events_from_call(call)

    assert len(rows) == 1
    assert rows[0]["node"] == "opening"
    assert events_from_call({}) == []
    assert events_from_call({"messages": "wrong-shape"}) == []


def test_read_trace_accepts_final_complete_record_and_redacts_each_line(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    path.write_text(
        '{"kind":"start","message":"configured-value"}\n\n'
        '{"kind":"request","data":{"password":"sample-password"}}',
        encoding="utf-8",
    )

    result = read_trace(path, secrets=("configured-value",))

    assert len(result.records) == 2
    assert result.errors == []
    assert not result.partial_line and not result.truncated
    assert result.bytes_read == path.stat().st_size
    assert "configured-value" not in json.dumps(result.records)
    assert "sample-password" not in json.dumps(result.records)


def test_secret_iterator_applies_to_every_trace_line(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    path.write_text('{"message":"configured-value"}\n{"message":"configured-value"}\n', encoding="utf-8")

    result = read_trace(path, secrets=iter(["configured-value"]))

    assert result.records == [{"message": "***"}, {"message": "***"}]


@pytest.mark.parametrize("tail", ['{"kind":', '{"kind":"req', '{"kind": tru'])
def test_read_trace_tolerates_incomplete_final_write(tmp_path: Path, tail: str) -> None:
    path = tmp_path / "trace.jsonl"
    path.write_text('{"kind":"start"}\n' + tail, encoding="utf-8")

    result = read_trace(path)

    assert result.records == [{"kind": "start"}]
    assert result.partial_line
    assert result.errors == []


def test_read_trace_reports_complete_malformed_lines_without_echoing_content(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    path.write_text(
        '{"kind":"start"}\nopaque-secret\n{"password":"opaque-secret",}\n[]\n{"kind":"end"}\n',
        encoding="utf-8",
    )

    result = read_trace(path)

    assert result.records == [{"kind": "start"}, {"kind": "end"}]
    assert result.errors == [
        "Line 2: invalid JSON.",
        "Line 3: invalid JSON.",
        "Line 4: expected a JSON object.",
    ]
    assert not result.partial_line
    assert "opaque-secret" not in json.dumps(result.errors)


def test_read_trace_reports_malformed_final_complete_object(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    path.write_text('{"kind": invalid}', encoding="utf-8")

    result = read_trace(path)

    assert result.errors == ["Line 1: invalid JSON."]
    assert not result.partial_line


def test_read_trace_rejects_oversized_integer_and_recovers_next_record(tmp_path: Path) -> None:
    limit = sys.get_int_max_str_digits()
    if limit == 0:
        pytest.skip("Python integer-digit limit is disabled")
    path = tmp_path / "trace.jsonl"
    path.write_text(
        '{"n":' + "9" * (limit + 1) + '}\n{"kind":"request","data":{"ok":true}}\n', encoding="utf-8"
    )

    result = read_trace(path)

    assert result.records == [{"kind": "request", "data": {"ok": True}}]
    assert result.errors == ["Line 1: invalid JSON."]
    assert not result.partial_line


def test_read_trace_limit_and_utf8_truncation_are_visible(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    first = b'{"kind":"start"}\n'
    path.write_bytes(first + '{"message":"\u00e9"}\n'.encode())

    result = read_trace(path, max_bytes=len(first) + 13)

    assert result.records == [{"kind": "start"}]
    assert result.truncated and result.partial_line
    assert result.errors == []
    assert result.bytes_read == len(first) + 13


def test_read_trace_reports_invalid_utf8_and_handles_bom(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    path.write_bytes(b'\xef\xbb\xbf{"kind":"start"}\n\xff\n{"kind":"end"}\n')

    result = read_trace(path)

    assert result.records == [{"kind": "start"}, {"kind": "end"}]
    assert result.errors == ["Line 2: invalid UTF-8."]


def test_missing_trace_is_a_safe_error_and_invalid_limit_is_rejected(tmp_path: Path) -> None:
    result = read_trace(tmp_path / "opaque-secret.jsonl")

    assert result.records == []
    assert result.errors == ["Could not read trace (FileNotFoundError)."]
    assert "opaque-secret" not in json.dumps(result.errors)
    with pytest.raises(ValueError, match="positive integer"):
        read_trace(tmp_path / "missing", max_bytes=0)


def test_trace_rows_and_summary_count_attempts_without_double_counting_other_events() -> None:
    records = [
        {
            "kind": "request",
            "ts": "timestamp-1",
            "data": {
                "service": "freya",
                "scope": "wrapper",
                "operation": "get_call",
                "ok": False,
                "status": 503,
                "duration_ms": 120,
                "attempt": 1,
                "request_id": "request-1",
            },
        },
        {
            "kind": "request",
            "data": {
                "service": "freya",
                "operation": "get_call",
                "ok": True,
                "duration_ms": 80,
                "attempt": 2,
                "request_id": "request-1",
            },
        },
        {
            "kind": "model_call",
            "data": {
                "provider": "openai",
                "model": "sample-model",
                "purpose": "compile",
                "ok": True,
                "latency_s": 2,
                "input_tokens": 17,
                "output_tokens": 9,
            },
        },
        {
            "kind": "model_call",
            "data": {
                "provider": "openai",
                "ok": False,
                "latency_s": 0.5,
                "error": "failed configured-value",
                "input_tokens": None,
            },
        },
        {"kind": "freya", "data": {"action": "get_call", "ok": False}},
        {"kind": "llm", "data": {"error": "retrying"}},
    ]

    rows = trace_rows(records, secrets=("configured-value",))
    summary = trace_summary(records)

    assert len(rows) == 4
    assert rows[0]["action"] == "get_call" and rows[0]["latency_s"] == 0.12
    assert rows[0]["timestamp"] == "timestamp-1"
    assert rows[2]["service"] == "openai" and rows[2]["input_tokens"] == 17
    assert "configured-value" not in json.dumps(rows)
    assert summary == {
        "requests": 2,
        "request_failures": 1,
        "model_calls": 2,
        "model_failures": 1,
        "input_tokens": 17,
        "output_tokens": 9,
        "latency_s": 2.7,
    }


def test_summary_handles_unknown_status_and_old_freya_sink_records() -> None:
    records = [
        {"kind": "request", "data": {"status": None, "duration_ms": "bad"}},
        {"kind": "freya", "data": {"action": "request", "status": "429", "duration_s": 0.25}},
        {"kind": "model_call", "data": {"input_tokens": "invalid", "latency_s": float("inf")}},
        {"kind": "unrelated"},
        {"kind": []},
        None,
    ]

    summary = trace_summary(records)

    assert summary["requests"] == 2
    assert summary["request_failures"] == 1
    assert summary["model_calls"] == 1
    assert summary["latency_s"] == 0.25
    assert trace_summary([])["requests"] == 0


# --- Raw-text redaction: the document does not have to be valid JSON ---------------------------------------


@pytest.mark.parametrize(
    "document",
    [
        # Truncated mid-record: an unknown custom header that no name rule would flag.
        '{"event": "function_call_start", "args": {"headers": {"X-Opaque": "synthetic-leak", '
        '"Content-Type": "application/json"}, "body": {"customer_id": "c1"',
        # Cut off in the middle of the credential value itself.
        '{"headers": {"Content-Type": "application/json", "X-Opaque": "synthetic-le',
        # Missing the final brace only.
        '{"args": {"headers": {"X-Trace-Opaque": "synthetic-leak"}}, "customer_id": "c1"',
        # Prose before a truncated document.
        'Request failed: {"headers": {"X-Opaque": "synthetic-leak"',
        # Single-quoted pseudo JSON (invalid JSON).
        "{'headers': {'X-Opaque': 'synthetic-leak'}, 'customer_id': 'c1'}",
    ],
)
def test_unknown_header_credentials_are_masked_when_embedded_json_is_incomplete(document: str) -> None:
    clean = redact_data({"message": document})["message"]

    assert "synthetic-le" not in clean
    assert "***" in clean


def test_incomplete_json_keeps_safe_headers_and_customer_ids_readable() -> None:
    document = (
        '{"args": {"headers": {"X-Opaque": "synthetic-leak", "Content-Type": "text/html; charset=utf-8"}, '
        '"body": {"customer_id": "c1"'
    )

    clean = redact_data({"message": document})["message"]

    assert clean == (
        '{"args": {"headers": {"X-Opaque": "***", "Content-Type": "text/html; charset=utf-8"}, '
        '"body": {"customer_id": "c1"'
    )


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        (
            '{"event": "x", "x-custom-key": "synthetic-leak", "customer_id": "c1", "a": [1, 2',
            '{"event": "x", "x-custom-key": "***", "customer_id": "c1", "a": [1, 2',
        ),
        (
            '{"x-session-token": "synthetic-leak", "note": "kept"',
            '{"x-session-token": "***", "note": "kept"',
        ),
        (
            '{"Authorization": "Bearer synthetic-leak", "password": 12345, "client_secret": "synthetic-le',
            '{"Authorization": "***", "password": "***", "client_secret": "***',
        ),
        (
            '{"Cookie": "session=synthetic-leak; theme=dark", "customer_id": "c1"',
            '{"Cookie": "***", "customer_id": "c1"',
        ),
        (
            '{"credentials": {"user": "u", "pw": "synthetic-leak"}, "customer_id": "c1"',
            '{"credentials": "***", "customer_id": "c1"',
        ),
    ],
)
def test_credential_named_members_are_masked_in_incomplete_json(document: str, expected: str) -> None:
    clean = redact_data(document)

    assert clean == expected
    assert "synthetic-le" not in clean


def test_escaped_json_inside_an_incomplete_document_is_masked() -> None:
    document = '{"message": "{\\"api_key\\": \\"synthetic-leak\\", \\"customer_id\\": \\"c1\\"}", "broken'

    clean = redact_data(document)

    assert "synthetic-leak" not in clean
    assert 'customer_id\\": \\"c1' in clean


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "POST /x failed; Authorization: Bearer synthetic-leak trailing",
            "POST /x failed; Authorization: *** trailing",
        ),
        ("retry with X-Api-Key: synthetic-leak, then stop", "retry with X-Api-Key: ***, then stop"),
        ("sent token=synthetic-leak&customer_id=c1", "sent token=***&customer_id=c1"),
        ("password: synthetic-leak was rejected", "password: *** was rejected"),
    ],
)
def test_credential_pairs_in_plain_text_are_masked(text: str, expected: str) -> None:
    assert redact_data({"error": text})["error"] == expected


def test_usage_figures_and_ordinary_prose_are_not_mistaken_for_credentials() -> None:
    text = "total tokens: 17 and input_tokens=5; missing key: customer_id; customer_id: c1"

    assert redact_data({"error": text}) == {"error": text}


@pytest.mark.parametrize("secret", ["opaque-secret-1", "a/b+c=="])
def test_known_secret_values_are_masked_in_incomplete_json_and_plain_text(secret: str) -> None:
    document = '{"detail": "failed with ' + secret + ' inside", "customer_id": "c1"'

    clean = redact_data([document, f"plain {secret} text"], secrets=[secret])

    assert clean == ['{"detail": "failed with *** inside", "customer_id": "c1"', "plain *** text"]


def test_key_shaped_tokens_are_masked_in_incomplete_json() -> None:
    document = '{"detail": "used fak_abcdefghij1234 and sk-ant-abcdefghijklmnop", "x": "kept"'

    clean = redact_data(document)

    assert clean == '{"detail": "used *** and ***", "x": "kept"'


def test_valid_json_remains_valid_json_after_raw_masking() -> None:
    document = json.dumps(
        {
            "args": {"x-custom-key": "synthetic-leak", "password": 12345, "customer_id": "c1"},
            "headers": {"X-Opaque": "synthetic-leak", "Accept": "application/json"},
            "nested": json.dumps({"token": "synthetic-leak", "ok": True}),
        }
    )

    clean = json.loads(redact_data({"message": document})["message"])

    assert clean["args"] == {"x-custom-key": "***", "password": "***", "customer_id": "c1"}
    assert clean["headers"] == {"X-Opaque": "***", "Accept": "application/json"}
    assert json.loads(clean["nested"]) == {"token": "***", "ok": True}


def test_redaction_is_idempotent() -> None:
    document = (
        '{"headers": {"X-Opaque": "synthetic-leak"}, "x-custom-key": "synthetic-leak", "customer_id": "c1"'
    )

    once = redact_data({"message": document, "path": "/r?api_key=synthetic-leak&customer_id=c1"})

    assert redact_data(once) == once


# --- One failure predicate for the counters and the monitor's "errors and retries" filter ------------------


@pytest.mark.parametrize(
    ("row", "failed"),
    [
        ({"ok": False}, True),
        ({"ok": False, "status": None}, True),
        ({"status": 429}, True),  # legacy record: a status and no ok flag
        ({"status": "429"}, True),
        ({"status": 500}, True),
        ({"status": 400}, True),
        ({"ok": True, "status": 502}, True),
        ({"status": 399}, False),
        ({"status": 200}, False),
        ({"ok": True}, False),
        ({"ok": True, "status": 200}, False),
        ({"status": "not-a-number"}, False),
        ({"status": True}, False),
        ({"status": None}, False),
        ({}, False),
    ],
)
def test_failed_attempt_predicate(row: dict, failed: bool) -> None:
    assert is_failed_attempt(row) is failed


@pytest.mark.parametrize(
    ("row", "attention"),
    [
        ({"ok": True, "details": {"retry_scheduled": True}}, True),
        ({"ok": True, "details": {"retry_scheduled": "true"}}, True),
        ({"ok": True, "details": {"retry_scheduled": False}}, False),
        ({"ok": True, "details": {"retry_scheduled": "false"}}, False),
        ({"ok": True, "details": {}}, False),
        ({"ok": True, "details": None}, False),
        ({"status": 429, "details": {}}, True),
        ({"ok": False}, True),
        ({"ok": True, "status": 200, "details": {"retry_scheduled": None}}, False),
    ],
)
def test_needs_attention_predicate(row: dict, attention: bool) -> None:
    assert needs_attention(row) is attention


def test_counter_and_attention_filter_agree_on_status_only_failures() -> None:
    records = [
        {"kind": "request", "data": {"operation": "legacy-429", "status": 429}},
        {"kind": "request", "data": {"operation": "status-500", "status": "500"}},
        {"kind": "request", "data": {"operation": "ok-false", "ok": False}},
        {"kind": "request", "data": {"operation": "fine", "ok": True, "status": 200}},
        {"kind": "request", "data": {"operation": "unknown"}},
        {"kind": "request", "data": {"operation": "retry", "ok": True, "retry_scheduled": True}},
        {"kind": "model_call", "data": {"model": "legacy-model-429", "status": 429}},
        {"kind": "model_call", "data": {"model": "fine-model", "ok": True}},
    ]

    rows = trace_rows(records)
    summary = trace_summary(records)

    failed_requests = [row["action"] for row in rows if row["kind"] == "request" and is_failed_attempt(row)]
    assert failed_requests == ["legacy-429", "status-500", "ok-false"]
    assert summary["request_failures"] == 3
    assert summary["model_failures"] == 1
    assert [row["action"] or row["model"] for row in rows if needs_attention(row)] == [
        "legacy-429",
        "status-500",
        "ok-false",
        "retry",
        "legacy-model-429",
    ]
