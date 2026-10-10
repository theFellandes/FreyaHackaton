"""Tests for builder.compiler.tools: tool-function builders driven by the kit's tool contracts."""

from __future__ import annotations

import json

import pytest

from builder.checks.lint import lint_workflow
from builder.compiler.pycode import assert_safe_code, json_literal, py_literal
from builder.compiler.tools import code_exec, end_terminal, engine_call, field_leaf, model_tool
from builder.ingest.tool_api import ToolApi


def _body(tool: dict) -> dict:
    return tool["function"]["parameters"]["properties"]["body"]


def test_model_tool_bakes_customer_id_and_leaves_other_fields_to_the_model(kit_tool_api: ToolApi) -> None:
    contract = kit_tool_api.tools["create_promise"]
    tool = model_tool(contract, description="Record ONE promise. CALL it after a clear yes.")
    function = tool["function"]
    assert function["name"] == "create_promise"
    assert function["handler"] == "__function__:api_call"
    props = function["parameters"]["properties"]
    assert props["url"]["default"] == contract.url
    assert props["method"]["default"] == "POST"
    assert props["headers"]["properties"]["Content-Type"]["default"] == "application/json"
    body = _body(tool)
    assert body["properties"]["customer_id"] == {"type": "string", "default": "{{customer_id}}"}
    for name in ("product_id", "amount", "date"):
        assert body["properties"][name]["default"] == "{{" + name + "}}"
    assert body["properties"]["amount"]["type"] == "number"
    assert "YYYY-MM-DD" in body["properties"]["date"]["description"]
    assert body["required"] == ["product_id", "amount", "date"]
    assert tool["config"] == {"timeoutMs": 12000, "maxRetries": 0}


def test_enum_and_notes_come_from_the_contract(kit_tool_api: ToolApi) -> None:
    contract = kit_tool_api.tools["add_note"]
    tool = model_tool(contract, description="CALL it.", field_notes={"reason": "Why."}, include_optional=["text"])
    props = _body(tool)["properties"]
    assert props["reason"]["enum"] == ["refused", "hardship", "dispute", "paid", "service_error"]
    assert props["reason"]["description"].endswith("Why.")
    assert "text" in props
    assert "text" not in _body(tool)["required"]
    without = model_tool(contract, description="CALL it.")
    assert "text" not in _body(without)["properties"]


def test_baked_values_are_not_model_fields(kit_tool_api: ToolApi) -> None:
    contract = kit_tool_api.tools["transfer_to_agent"]
    tool = model_tool(contract, baked={"queue": "support"}, description="CALL it.")
    body = _body(tool)
    assert body["properties"]["queue"] == {"type": "string", "default": "support"}
    assert body["required"] == []


def test_response_mapping_and_hooks_are_attached(kit_tool_api: ToolApi) -> None:
    contract = kit_tool_api.tools["schedule_callback"]
    tool = model_tool(contract, description="CALL it.", response_mapping=[("data.ok", "cb_ok")],
                      pre_process_code="x = 1\n", post_process_code="y = 2\n", max_retries=1, timeout_ms=5000)
    assert tool["response_mapping"] == [{"path": "data.ok", "variable": "cb_ok"}]
    assert tool["pre_process_code"] == "x = 1\n" and tool["post_process_code"] == "y = 2\n"
    assert tool["config"] == {"timeoutMs": 5000, "maxRetries": 1}


def test_hooks_with_double_braces_are_refused(kit_tool_api: ToolApi) -> None:
    with pytest.raises(ValueError, match="double brace"):
        model_tool(kit_tool_api.tools["add_note"], description="CALL it.", post_process_code="x = {'a': {'b': 1}}\n")


def test_engine_call_bakes_every_field(kit_tool_api: ToolApi) -> None:
    contract = kit_tool_api.tools["close_call"]
    action = engine_call(contract, baked={"customer_id": "{{customer_id}}", "outcome": "{{final_outcome}}",
                                          "reason": "{{final_reason}}"}, max_retries=2)
    body = _body(action)
    assert {k: v["default"] for k, v in body["properties"].items()} == {
        "customer_id": "{{customer_id}}", "outcome": "{{final_outcome}}", "reason": "{{final_reason}}"}
    assert body["required"] == []
    assert action["config"]["maxRetries"] == 2
    assert action["function"]["parameters"]["properties"]["url"]["default"] == contract.url


def test_engine_call_name_override(kit_tool_api: ToolApi) -> None:
    action = engine_call(kit_tool_api.tools["get_account"], baked={"customer_id": "{{customer_id}}"}, name="load")
    assert action["function"]["name"] == "load"


def test_code_exec_and_end_terminal() -> None:
    action = code_exec("vars['a'] = 1\n", timeout_ms=3000)
    props = action["function"]["parameters"]["properties"]
    assert action["function"]["handler"] == "__function__:code_exec"
    assert props["code"]["default"] == "vars['a'] = 1\n" and props["timeout_ms"]["default"] == 3000
    with pytest.raises(ValueError):
        code_exec("def broken(:\n")
    silent = end_terminal("call_closed")
    assert silent["function"]["handler"] == "__function__:end_call"
    assert silent["function"]["parameters"]["properties"] == {"message_mode": {"type": "string", "default": "none"}}
    spoken = end_terminal("bye", "custom", "Goodbye.")
    assert spoken["function"]["parameters"]["properties"]["spoken_message"]["default"] == "Goodbye."


def test_field_leaf_adds_date_format_note(kit_tool_api: ToolApi) -> None:
    spec = kit_tool_api.tools["schedule_callback"].field("date")
    leaf = field_leaf(spec)
    assert leaf["type"] == "string" and leaf["default"] == "{{date}}" and "YYYY-MM-DD" in leaf["description"]


def test_a_workflow_made_of_the_builders_passes_the_tool_checks(kit_tool_api: ToolApi) -> None:
    tools = [model_tool(c, description=f"CALL {name} when needed.") for name, c in kit_tool_api.tools.items()]
    workflow = {
        "initial_node": "talk",
        "nodes": {
            "talk": {"name": "talk", "node_type": "conversation", "tools": tools, "max_node_turns": 5,
                     "max_turn_transition": "bye", "transitions": [
                         {"target_node": "bye", "description": "x", "is_tool_call": True,
                          "condition": {"operator": "matches_intent", "description": "x"}}]},
            "bye": {"name": "bye", "node_type": "end_call", "on_enter": {"type": "silent"},
                    "disable_inactivity_monitor": True, "transitions": [], "terminal": end_terminal("bye")},
        },
    }
    report = lint_workflow(workflow, kit_tool_api, {"customer_id"})
    assert not [f for f in report.errors if f.rule in ("L011", "L012")], report.format()


def test_pycode_helpers_never_leave_braces_in_the_source() -> None:
    literal = py_literal('a {b} {{c}} "q" \\ ')
    assert "{" not in literal and "}" not in literal
    assert eval(literal) == 'a {b} {{c}} "q" \\ '
    expression = json_literal({"k": {"n": [1, {"m": "x}}"}]}})
    assert "{" not in expression and "}" not in expression
    assert eval(expression, {"json": json}) == {"k": {"n": [1, {"m": "x}}"}]}}
    assert assert_safe_code("x = 1\n") == "x = 1\n"
