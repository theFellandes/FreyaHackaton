"""Exercise the monitor and robustness pages with isolated files and mocked live reads."""

from __future__ import annotations

import json
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import Mock, call

import pytest

# Streamlit is the optional ``ui`` extra and ``builder.monitor_views`` imports it at module top: skip the whole module
# before importing anything that needs it, or collection aborts and no test of the run executes.
pytest.importorskip("streamlit")
testing = pytest.importorskip("streamlit.testing.v1")

import builder.monitor_views as monitor_views  # noqa: E402
import builder.robustness as robustness  # noqa: E402
from builder.freya.client import FreyaClient  # noqa: E402
from builder.policy.calendar import DateCheck  # noqa: E402
from builder.settings import get_settings  # noqa: E402

APP_PATH = Path(__file__).resolve().parents[2] / "streamlit_app.py"


def _widget(widgets: Any, label: str) -> Any:
    return next(widget for widget in widgets if widget.label == label)


def _metrics(app: Any) -> dict[str, str]:
    return {metric.label: metric.value for metric in app.metric}


def _json(element: Any) -> Any:
    return json.loads(element.value) if isinstance(element.value, str) else element.value


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch, tmp_dirs: Any) -> Any:
    # Neither saved workflow IDs nor the developer's real traces participate in these tests.
    monkeypatch.setattr(monitor_views, "saved_workflows", lambda path: {})
    monkeypatch.setattr(monitor_views, "TemporaryDirectory", partial(TemporaryDirectory, dir=tmp_dirs.root))
    # Rendering or changing local controls must never implicitly construct a platform client.
    monkeypatch.setattr(
        monitor_views, "FreyaClient", Mock(side_effect=AssertionError("unexpected live read"))
    )
    result = testing.AppTest.from_file(APP_PATH, default_timeout=20).run()
    assert not result.exception
    return result


def _enable_mocked_reads(monkeypatch: pytest.MonkeyPatch) -> tuple[Mock, Mock]:
    client = Mock(spec=FreyaClient)
    constructor = Mock(return_value=client)
    monkeypatch.setattr(monitor_views, "FreyaClient", constructor)
    trace = Mock()
    trace.closed = False
    trace.jsonl_path = None
    monkeypatch.setattr(monitor_views, "TraceWriter", Mock(return_value=trace))
    monkeypatch.setenv("FREYA_API_KEY", "configured-ui-value")
    get_settings.cache_clear()
    return client, constructor


def test_monitor_without_credentials_or_traces_requires_explicit_live_reads(app: Any) -> None:
    app.radio(key="navigation").set_value("Request monitor").run()

    assert not app.exception
    assert not app.error
    assert app.subheader[0].value == "Request monitor"
    assert any("No JSONL traces yet" in message.value for message in app.info)
    assert any("Configure FREYA_API_KEY" in message.value for message in app.info)
    assert _widget(app.button, "Read call events").disabled
    assert _widget(app.button, "Refresh workflow status").disabled
    assert _widget(app.button, "Refresh simulation status").disabled
    assert len(app.metric) == 0
    monitor_views.FreyaClient.assert_not_called()

    app.radio(key="monitor_source").set_value("Current conversation").run()
    assert not app.exception
    assert any("There is no trace for this source yet" in message.value for message in app.info)
    monitor_views.FreyaClient.assert_not_called()


def test_recorded_activity_counts_failures_filters_retries_and_masks_raw_details(
    app: Any, monkeypatch: pytest.MonkeyPatch, tmp_dirs: Any
) -> None:
    monkeypatch.setenv("FREYA_API_KEY", "configured-ui-value")
    get_settings.cache_clear()
    records = [
        {
            "kind": "request",
            "data": {
                "service": "freya",
                "operation": "chat",
                "ok": True,
                "duration_ms": 25,
                "attempt": 1,
                "headers": {
                    "X-Opaque": "unknown-header-value",
                    "x-team-key": "unknown-team-value",
                    "Content-Type": "application/json",
                },
            },
        },
        {
            "kind": "request",
            "data": {
                "service": "freya",
                "operation": "get_call",
                "ok": False,
                "status": 503,
                "duration_ms": 40,
                "attempt": 1,
                "retry_scheduled": True,
            },
        },
        {
            "kind": "request",
            "data": {
                "service": "freya",
                "operation": "get_call",
                "ok": True,
                "duration_ms": 10,
                "attempt": 2,
                "retry_scheduled": False,
            },
        },
        {
            "kind": "model_call",
            "data": {
                "provider": "openai",
                "model": "sample-model",
                "ok": True,
                "latency_s": 1.5,
                "input_tokens": 17,
                "output_tokens": 9,
            },
        },
        {
            "kind": "model_call",
            "data": {
                "provider": "openai",
                "ok": False,
                "error": "failed configured-ui-value",
                "latency_s": 0.2,
            },
        },
    ]
    (tmp_dirs.traces / "synthetic.jsonl").write_text(
        "\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8"
    )

    app.radio(key="navigation").set_value("Request monitor").run()

    assert not app.exception
    assert not app.error
    assert _metrics(app) == {
        "Request attempts": "3",
        "Failed attempts": "1",
        "Builder model attempts": "2",
        "Model failures": "1",
    }
    assert len(app.dataframe[0].value) == 5
    details = _json(app.json[0])
    assert details[0]["details"]["headers"] == {
        "X-Opaque": "***",
        "x-team-key": "***",
        "Content-Type": "application/json",
    }
    assert details[3]["input_tokens"] == 17
    rendered = json.dumps(details)
    assert all(
        secret not in rendered
        for secret in ("unknown-header-value", "unknown-team-value", "configured-ui-value")
    )
    monitor_views.FreyaClient.assert_not_called()

    app.checkbox(key="monitor_errors_only").check().run()

    assert not app.exception
    assert len(app.dataframe[0].value) == 2
    assert set(app.dataframe[0].value["kind"]) == {"request", "model_call"}
    assert app.dataframe[0].value["ok"].tolist() == [False, False]
    assert _metrics(app)["Request attempts"] == "3"  # filter changes the rows, not the total
    monitor_views.FreyaClient.assert_not_called()


def test_errors_filter_keeps_every_attempt_that_the_failed_counter_counts(
    app: Any, monkeypatch: pytest.MonkeyPatch, tmp_dirs: Any
) -> None:
    monkeypatch.setenv("FREYA_API_KEY", "configured-ui-value")
    get_settings.cache_clear()
    records = [
        # Legacy records: a failure status and no ok flag at all.
        {"kind": "request", "data": {"service": "freya", "operation": "legacy-429", "status": 429}},
        {"kind": "request", "data": {"service": "freya", "operation": "status-500", "status": 500}},
        # The opposite legacy shape: an ok flag and no status.
        {"kind": "request", "data": {"service": "freya", "operation": "ok-false", "ok": False}},
        {"kind": "request", "data": {"service": "freya", "operation": "fine", "ok": True, "status": 200}},
        {"kind": "request", "data": {"service": "freya", "operation": "unknown"}},
        {
            "kind": "request",
            "data": {"service": "freya", "operation": "retried", "ok": True, "retry_scheduled": True},
        },
        {"kind": "model_call", "data": {"provider": "openai", "purpose": "legacy-model-502", "status": 502}},
        {"kind": "model_call", "data": {"provider": "openai", "purpose": "fine-model", "ok": True}},
    ]
    (tmp_dirs.traces / "status-only.jsonl").write_text(
        "\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8"
    )

    app.radio(key="navigation").set_value("Request monitor").run()

    assert not app.exception
    assert _metrics(app) == {
        "Request attempts": "6",
        "Failed attempts": "3",
        "Builder model attempts": "2",
        "Model failures": "1",
    }
    assert len(app.dataframe[0].value) == 8

    app.checkbox(key="monitor_errors_only").check().run()

    assert not app.exception
    # The 3 failed requests and the 1 failed model attempt all remain, plus the request that scheduled a retry.
    assert app.dataframe[0].value["action"].tolist() == [
        "legacy-429",
        "status-500",
        "ok-false",
        "retried",
        "legacy-model-502",
    ]
    assert _metrics(app)["Failed attempts"] == "3"
    monitor_views.FreyaClient.assert_not_called()


def test_dashboard_warns_about_stale_draft_after_one_explicit_read(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, constructor = _enable_mocked_reads(monkeypatch)
    client.workflow_status.return_value = {
        "workflow_id": "workflow-test",
        "newest_published": False,
        "newest_version_id": "draft-v2",
        "newest_published_version_id": "published-v1",
    }
    app.radio(key="navigation").set_value("Request monitor").run()
    _widget(app.text_input, "Workflow ID to check").set_value("workflow-test").run()
    constructor.assert_not_called()
    client.workflow_status.assert_not_called()

    app.button(key="monitor_workflow_refresh").click().run()

    assert not app.exception
    assert not app.error
    constructor.assert_called_once()
    assert client.method_calls == [call.workflow_status("workflow-test")]
    assert any("newest workflow version is unpublished" in warning.value for warning in app.warning)
    assert app.session_state["monitor_workflow"]["newest_version_id"] == "draft-v2"

    app.radio(key="monitor_source").set_value("Dashboard inspection").run()
    assert client.method_calls == [call.workflow_status("workflow-test")]


def test_call_inspection_shows_tool_routes_snapshots_and_only_redacted_raw_call(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _constructor = _enable_mocked_reads(monkeypatch)
    client.get_call.return_value = {
        "messages": [
            {
                "role": "system",
                "secondsFromStart": 1,
                "nodeId": "verification",
                "message": json.dumps(
                    {
                        "event": "function_call_start",
                        "function": "verify_identity",
                        "args": {
                            "headers": {"X-Opaque": "unknown-call-header"},
                            "body": {"customer_id": "customer-1"},
                        },
                    }
                ),
            },
            {
                "role": "system",
                "message": json.dumps(
                    {"event": "route_evaluated", "target": "retry", "variable_table": {"verified": False}}
                ),
            },
            {"role": "assistant", "workflowSnapshot": {"currentNode": "retry", "nodeTurnCount": 1}},
            {"role": "system", "message": json.dumps({"event": "error", "message": "configured-ui-value"})},
        ]
    }
    app.radio(key="navigation").set_value("Request monitor").run()
    app.text_input(key="monitor_call_id").set_value("call-test").run()
    client.get_call.assert_not_called()

    app.button(key="monitor_read_call").click().run()

    assert not app.exception
    assert not app.error
    assert client.method_calls == [call.get_call("call-test")]
    assert _metrics(app)["Engine errors"] == "1"
    assert app.dataframe[0].value["event"].tolist() == [
        "function_call_start",
        "route_evaluated",
        "workflow_snapshot",
        "error",
    ]
    tool_rows, raw_call = [_json(element) for element in app.json]
    assert tool_rows[0]["details"]["args"]["body"]["customer_id"] == "customer-1"
    assert tool_rows[2]["details"]["workflowSnapshot"]["currentNode"] == "retry"
    rendered = json.dumps([tool_rows, raw_call])
    assert "unknown-call-header" not in rendered
    assert "configured-ui-value" not in rendered


def test_saved_practice_case_opens_monitor_with_call_id_without_fetching(app: Any, tmp_dirs: Any) -> None:
    row = {
        "id": "PUB-TEST",
        "pass": False,
        "family": "Verification",
        "call_id": "saved-call-test",
        "failed": ["synthetic wrong route"],
        "tools": ["verify_identity"],
    }
    report = {"passed": 0, "total": 1, "runs": 1, "rows": [row]}
    (tmp_dirs.results / "saved-report.json").write_text(json.dumps(report), encoding="utf-8")
    app.radio(key="navigation").set_value("Practice results").run()
    assert not app.exception
    assert not app.error
    assert _widget(app.button, "Inspect call requests")

    _widget(app.button, "Inspect call requests").click().run()

    assert not app.exception
    assert not app.error
    assert app.radio(key="navigation").value == "Request monitor"
    assert app.text_input(key="monitor_call_id").value == "saved-call-test"
    assert app.button(key="monitor_read_call").disabled
    monitor_views.FreyaClient.assert_not_called()


@pytest.mark.parametrize("source", ["Brief A", "Brief A + B"])
def test_bundled_robustness_reports_metrics_groups_and_limits_without_network(app: Any, source: str) -> None:
    app.radio(key="navigation").set_value("Robustness checks").run()
    app.selectbox(key="robustness_source").set_value(source).run()
    assert len(app.metric) == 0

    app.button(key="robustness_run").click().run()

    assert not app.exception
    assert not app.error
    report = app.session_state["robustness_report"]["report"]
    assert report["cases_tested"] > 1000
    assert report["failed"] == 0
    assert _metrics(app) == {
        "Invariant evaluations": str(report["cases_tested"]),
        "Passed groups": str(report["passed"]),
        "Failed groups": "0",
        "Unchecked groups": str(report["unchecked"]),
    }
    groups = set(app.dataframe[0].value["name"])
    assert {
        "calendar.steps_and_intervals",
        "promise_dates.boundaries_and_suggestions",
        "amounts.cent_boundaries",
        "callbacks.slot_boundaries",
        "outcomes.table_precedence",
    } <= groups
    assert sum(report[key] for key in ("passed", "failed", "unchecked")) == len(groups)
    assert _json(app.json[0]) == report
    assert any("hidden benchmark" in limitation for limitation in report["limitations"])
    monitor_views.FreyaClient.assert_not_called()


def test_robustness_failure_counterexamples_remain_visible_and_report_becomes_stale(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Simulate a date-check regression rather than substituting a prebuilt report.
    monkeypatch.setattr(robustness, "check_promise_date", lambda *args: DateCheck("ok"))
    app.radio(key="navigation").set_value("Robustness checks").run()
    app.selectbox(key="robustness_source").set_value("Brief A").run()
    app.button(key="robustness_run").click().run()

    assert not app.exception
    assert any("Some invariants failed" in error.value for error in app.error)
    assert int(_metrics(app)["Failed groups"]) >= 1
    report = _json(app.json[0])
    date_check = next(
        check for check in report["checks"] if check["name"] == "promise_dates.boundaries_and_suggestions"
    )
    assert date_check["status"] == "fail"
    assert date_check["counterexamples"]
    assert {"today", "deadline", "proposed"} <= date_check["counterexamples"][0]["inputs"].keys()
    assert any("Counterexamples" in expander.label for expander in app.expander)

    app.selectbox(key="robustness_source").set_value("Brief A + B").run()

    assert not app.exception
    assert any("selection changed" in warning.value for warning in app.warning)
    assert _json(app.json[0]) == report
    monitor_views.FreyaClient.assert_not_called()
