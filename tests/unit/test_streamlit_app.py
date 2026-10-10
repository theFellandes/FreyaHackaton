"""Exercise the optional Streamlit UI locally and with a mocked live conversation."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

import builder.freya.client as client_module
import builder.trace as trace_module
from builder import paths
from builder.freya.client import ChatTurn, FreyaClient
from builder.settings import get_settings

testing = pytest.importorskip("streamlit.testing.v1")
APP_PATH = Path(__file__).resolve().parents[2] / "streamlit_app.py"


def widget_with_label(widgets: Any, label: str) -> Any:
    return next(widget for widget in widgets if widget.label == label)


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    # Saved runs are optional and must not make UI tests depend on local evaluation artifacts.
    monkeypatch.setattr(paths, "RESULTS_DIR", tmp_path / "results")
    result = testing.AppTest.from_file(APP_PATH, default_timeout=15).run()
    assert not result.exception
    return result


def test_every_local_screen_renders_without_errors(app: Any) -> None:
    expected = {
        "Policy lab": "Policy lab",
        "Inspect inputs": "Inspect project inputs",
        "Practice results": "Inspect practice results",
        "Chat with agent": "Chat with a deployed agent",
    }
    for page, subheader in expected.items():
        app.radio(key="navigation").set_value(page).run()
        assert not app.exception
        assert not app.error
        assert app.subheader[0].value == subheader

    assert widget_with_label(app.button, "Prepare conversation").disabled
    assert any("Set FREYA_API_KEY" in message.value for message in app.info)


def test_policy_amount_floor_and_weekend_date_suggestion(app: Any) -> None:
    widget_with_label(app.text_input, "Proposed payment amount").set_value("125")
    widget_with_label(app.date_input, "Proposed payment date").set_value(date(2026, 10, 17)).run()

    assert not app.exception
    assert not app.error
    assert any("Amount: below_floor" in message.value for message in app.warning)
    assert any("Date: not_business_day" in message.value for message in app.warning)
    assert any("2026-10-19" in message.value for message in app.markdown)


def test_invalid_policy_editor_shows_friendly_error(app: Any) -> None:
    widget_with_label(app.text_area, "Policy document").set_value("{broken JSON").run()

    assert not app.exception
    assert len(app.error) == 1
    assert app.error[0].value.startswith("Invalid policy:")


@pytest.mark.parametrize("collection", ["promises", "notes", "transfers", "product_order"])
def test_invalid_call_record_collection_shows_friendly_error(app: Any, collection: str) -> None:
    widget_with_label(app.text_area, "Recorded tools (JSON)").set_value(
        json.dumps({collection: {"unexpected": "object"}})
    ).run()

    assert not app.exception
    assert len(app.error) == 1
    assert app.error[0].value.startswith(f"Invalid call records: {collection} must be a list")


@pytest.mark.parametrize("label", ["Policy document", "Recorded tools (JSON)"])
def test_editor_changes_survive_navigating_away_and_back(app: Any, label: str) -> None:
    original = json.loads(widget_with_label(app.text_area, label).value)
    if label == "Policy document":
        original["bank_name"] = "Edited Practice Bank"
    else:
        original["product_order"] = ["edited-product"]
    edited = json.dumps(original, indent=2)
    widget_with_label(app.text_area, label).set_value(edited).run()
    assert not app.exception
    assert not app.error

    app.radio(key="navigation").set_value("Inspect inputs").run()
    app.radio(key="navigation").set_value("Policy lab").run()

    assert not app.exception
    assert not app.error
    assert widget_with_label(app.text_area, label).value == edited


def test_mocked_custom_conversation_sends_continues_and_ends(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = Mock(spec=FreyaClient)
    client.chat.side_effect = [
        ChatTurn(response="How can I help?", call_id="test-call"),
        ChatTurn(response="I understand.", call_id="test-call"),
    ]
    constructor = Mock(return_value=client)
    monkeypatch.setattr(client_module, "FreyaClient", constructor)
    trace = Mock()
    trace.jsonl_path = None
    monkeypatch.setattr(trace_module, "TraceWriter", Mock(return_value=trace))
    monkeypatch.setenv("FREYA_API_KEY", "fak_test-ui-key")
    get_settings.cache_clear()

    app.radio(key="navigation").set_value("Chat with agent").run()
    app.selectbox(key="chat_slot").set_value("Enter workflow ID").run()
    app.radio(key="chat_source").set_value("Custom variables").run()
    widget_with_label(app.text_input, "Workflow ID").set_value("test-workflow")
    widget_with_label(app.text_input, "Version ID (optional)").set_value("test-version")
    variables = {"customer_id": "test-customer", "now": "2026-10-14T11:00"}
    widget_with_label(app.text_area, "Call variables (JSON)").set_value(json.dumps(variables))
    widget_with_label(app.button, "Prepare conversation").click().run()
    assert not app.exception
    assert not app.error
    constructor.assert_called_once_with(trace)
    client.chat.assert_not_called()

    app.chat_input[0].set_value("Hello").run()
    assert not app.exception
    assert not app.error
    client.chat.assert_called_once_with(
        "test-workflow", "Hello", customer_variables=variables, call_id=None, version_id="test-version"
    )
    assert [message.children[0].value for message in app.chat_message] == ["Hello", "How can I help?"]

    app.radio(key="navigation").set_value("Policy lab").run()
    app.radio(key="navigation").set_value("Chat with agent").run()
    assert not app.exception
    assert len(app.chat_message) == 2
    assert client.chat.call_count == 1
    app.chat_input[0].set_value("I can pay tomorrow").run()
    client.chat.assert_called_with(
        "test-workflow", "I can pay tomorrow", customer_variables=None, call_id="test-call", version_id=None
    )

    widget_with_label(app.button, "End conversation").click().run()

    assert not app.exception
    assert not app.error
    client.end_chat.assert_called_once_with("test-call")
    # The trace stays open for explicit call/ledger inspection after the call ends.
    trace.close.assert_not_called()
    assert app.chat_input[0].disabled
    assert widget_with_label(app.button, "End conversation").disabled
    assert any("Conversation ended" in message.value for message in app.success)
