"""Tests for builder.checks.lint: every rule has a failing and a passing example.

A small valid workflow (``good``) is mutated one rule at a time. The validated reference agents and the generated
workflows are linted in their own test files.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from builder.checks.lint import RULES, Finding, LintReport, lint_workflow
from builder.compiler.nodes import (
    always_transition,
    api_request_node,
    bool_true,
    code_node,
    end_call_node,
    or_group,
    tool_transition,
    transition,
)
from builder.compiler.tools import engine_call, model_tool
from builder.ingest.tool_api import ToolApi

CALL_VARS = {"customer_id", "now", "first_name"}
REPO = Path(__file__).resolve().parents[2]


def good(api: ToolApi) -> dict[str, Any]:
    """A minimal workflow that passes every rule."""
    verify = model_tool(api.tools["verify_identity"],
                        description="Check ONE answer. CALL it every time the caller answers.")
    return {
        "initial_node": "setup",
        "system_prompt": "You are an assistant.",
        "nodes": {
            "setup": code_node("setup", "vars['greeting'] = 'hello'\n", "talk"),
            "talk": {
                "name": "talk",
                "node_type": "conversation",
                "on_enter": {"type": "tts", "text": "{{greeting}} {{first_name}}"},
                "task_message": "Talk. Result so far: {{verify_identity_response.data.ok}} {{mood}}",
                "tools": [verify],
                "extraction_schema": [{"name": "mood", "type": "string", "min_confidence": 0.6, "description": "m"}],
                "max_node_turns": 10,
                "max_turn_transition": "close",
                "transitions": [
                    transition("close", "verified", or_group(*bool_true("verify_identity_response.data.all_verified"))),
                    tool_transition("close_early", "The caller is done; you said goodbye."),
                ],
            },
            "close": api_request_node(
                "close",
                engine_call(api.tools["close_call"], baked={"customer_id": "{{customer_id}}", "outcome": "promise"}),
                "bye"),
            "close_early": api_request_node(
                "close_early",
                engine_call(api.tools["close_call"],
                            baked={"customer_id": "{{customer_id}}", "outcome": "not_reached", "reason": "unclear"}),
                "bye"),
            "bye": end_call_node("bye", "call_closed"),
        },
    }


def rules_of(report: LintReport) -> set[str]:
    return {f.rule for f in report.findings}


def lint(wf: dict[str, Any], api: ToolApi) -> LintReport:
    return lint_workflow(wf, api, CALL_VARS)


@pytest.fixture
def wf(kit_tool_api: ToolApi) -> dict[str, Any]:
    return good(kit_tool_api)


def test_the_baseline_is_clean(wf: dict[str, Any], kit_tool_api: ToolApi) -> None:
    report = lint(wf, kit_tool_api)
    assert report.ok and not report.findings, report.format()
    assert report.summary() == "0 errors, 0 warnings"
    assert set(RULES) >= {f"L{n:03d}" for n in range(1, 20)}


def test_findings_format_and_report_helpers(wf: dict[str, Any], kit_tool_api: ToolApi) -> None:
    wf["agent_config"] = {}
    wf["nodes"]["talk"]["task_message"] = "x" * 6100
    report = lint(wf, kit_tool_api)
    assert not report.ok
    assert [f.rule for f in report.errors] == ["L019"] and [f.rule for f in report.warnings] == ["L018"]
    assert report.by_rule("L019")[0].path == "agent_config"
    assert str(report.by_rule("L019")[0]).startswith("L019 error agent_config:")
    assert report.format().splitlines()[0].startswith("L019 error")
    assert report.format(limit=1).endswith("... 1 more")
    assert isinstance(report.findings[0], Finding)


def test_l001_null(wf: dict[str, Any], kit_tool_api: ToolApi) -> None:
    wf["nodes"]["talk"]["role_message"] = None
    report = lint(wf, kit_tool_api)
    assert report.by_rule("L001")[0].path == "nodes.talk.role_message"


def test_l002_tools_only_on_conversation_nodes(wf: dict[str, Any], kit_tool_api: ToolApi) -> None:
    wf["nodes"]["close"]["tools"] = copy.deepcopy(wf["nodes"]["talk"]["tools"])
    assert "L002" in rules_of(lint(wf, kit_tool_api))
    wf["nodes"]["close"]["tools"] = []
    wf["tools"] = [{"type": "function", "function": {"name": "x"}}]
    report = lint(wf, kit_tool_api)
    assert any(f.rule == "L002" and f.severity == "warning" for f in report.findings)


def test_l003_pre_actions_only_on_action_nodes(wf: dict[str, Any], kit_tool_api: ToolApi) -> None:
    wf["nodes"]["talk"]["pre_actions"] = copy.deepcopy(wf["nodes"]["close"]["pre_actions"])
    assert "L003" in rules_of(lint(wf, kit_tool_api))


def test_l004_passthrough_rules(wf: dict[str, Any], kit_tool_api: ToolApi) -> None:
    wf["nodes"]["setup"]["transitions"] = [transition("talk", "var", or_group({"variable": "a", "operator": "is_set"}))]
    assert "L004" in rules_of(lint(wf, kit_tool_api))
    wf = good(kit_tool_api)
    del wf["nodes"]["close"]["passthrough_default_transition"]
    assert "L004" in rules_of(lint(wf, kit_tool_api))
    wf = good(kit_tool_api)
    wf["nodes"]["close"]["passthrough"] = False
    assert any(f.rule == "L004" and "not a passthrough" in f.message for f in lint(wf, kit_tool_api).findings)


def test_l005_unreachable_and_l006_dead_end(wf: dict[str, Any], kit_tool_api: ToolApi) -> None:
    wf["nodes"]["island"] = {"name": "island", "node_type": "conversation", "transitions": [
        always_transition("island")]}
    report = lint(wf, kit_tool_api)
    assert report.by_rule("L005")[0].path == "nodes.island"
    assert report.by_rule("L006")[0].path == "nodes.island"
    del wf["nodes"]["bye"]
    wf["nodes"]["close"]["transitions"] = [always_transition("talk")]
    wf["nodes"]["close"]["passthrough_default_transition"] = "talk"
    assert any("no end_call" in f.message for f in lint(wf, kit_tool_api).by_rule("L006"))


def test_l005_global_nodes_are_roots(wf: dict[str, Any], kit_tool_api: ToolApi) -> None:
    wf["global_nodes"] = {"interrupt": {"name": "interrupt", "node_type": "conversation", "condition": {"operator": "always"},
                                        "passthrough": True, "passthrough_default_transition": "__previous__",
                                        "transitions": [always_transition("__previous__")]}}
    report = lint(wf, kit_tool_api)
    assert not report.by_rule("L005") and not report.by_rule("L007")


def test_l007_targets(wf: dict[str, Any], kit_tool_api: ToolApi) -> None:
    wf["nodes"]["talk"]["transitions"][0]["target_node"] = "nowhere"
    wf["nodes"]["talk"]["max_turn_transition"] = "__previous__"
    wf["initial_node"] = "missing"
    paths = {f.path for f in lint(wf, kit_tool_api).by_rule("L007")}
    assert {"nodes.talk.transitions[0].target_node", "nodes.talk.max_turn_transition", "initial_node"} <= paths


def test_l008_code(wf: dict[str, Any], kit_tool_api: ToolApi) -> None:
    def set_code(code: str) -> None:
        wf["nodes"]["setup"]["pre_actions"][0]["function"]["parameters"]["properties"]["code"]["default"] = code

    set_code("vars['greeting'] = {'a': {'b': 1}}\n")
    assert any("'{{'" in f.message or "}}" in f.message for f in lint(wf, kit_tool_api).by_rule("L008"))
    set_code("def broken(:\n")
    assert any("does not compile" in f.message for f in lint(wf, kit_tool_api).by_rule("L008"))
    set_code("import os\nvars['greeting'] = 'x'\n")
    assert any(f.severity == "warning" and "'os'" in f.message for f in lint(wf, kit_tool_api).by_rule("L008"))
    set_code("a = 1\nb = 2\nc = 3\nvars['greeting'] = str(a + b + c)\n")
    assert any("no top-level try" in f.message for f in lint(wf, kit_tool_api).by_rule("L008"))
    set_code("vars['greeting'] = 'x'\n")
    assert not lint(wf, kit_tool_api).by_rule("L008")


def test_l008_hooks_are_checked_too(wf: dict[str, Any], kit_tool_api: ToolApi) -> None:
    wf["nodes"]["talk"]["tools"][0]["post_process_code"] = "x = {{1}}\n"
    assert any("post_process_code" in f.path for f in lint(wf, kit_tool_api).by_rule("L008"))


def test_l009_response_mapping(wf: dict[str, Any], kit_tool_api: ToolApi) -> None:
    wf["nodes"]["talk"]["tools"][0]["response_mapping"] = [{"path": "ok", "variable": "good_name"},
                                                           {"path": "data.ok", "variable": "bad name"}]
    msgs = [f.message for f in lint(wf, kit_tool_api).by_rule("L009")]
    assert any("must start with 'data.'" in m for m in msgs) and any("not an identifier" in m for m in msgs)
    wf = good(kit_tool_api)
    wf["nodes"]["close"]["pre_actions"][0]["response_mapping"] = [{"path": "data.ok", "variable": "z"}]
    assert any("ignored" in f.message for f in lint(wf, kit_tool_api).by_rule("L009"))
    wf = good(kit_tool_api)
    wf["nodes"]["close"]["response_mapping"] = [{"path": "data.ok", "variable": "z"}]
    assert not lint(wf, kit_tool_api).by_rule("L009")
    wf["nodes"]["talk"]["response_mapping"] = [{"path": "data.ok", "variable": "y"}]
    assert any(f.severity == "warning" for f in lint(wf, kit_tool_api).by_rule("L009"))


def test_l010_boolean_equals(wf: dict[str, Any], kit_tool_api: ToolApi) -> None:
    conds = wf["nodes"]["talk"]["transitions"][0]["condition"]["conditions"]
    conds.pop()  # only "true" left
    assert "L010" in rules_of(lint(wf, kit_tool_api))
    wf["nodes"]["talk"]["transitions"][0]["condition"] = {"variable": "x", "operator": "equals", "operand": "true"}
    assert "L010" in rules_of(lint(wf, kit_tool_api))
    wf["nodes"]["talk"]["transitions"][0]["condition"] = or_group(
        {"variable": "x", "operator": "equals", "operand": "false"}, {"variable": "x", "operator": "equals", "operand": "False"})
    assert "L010" not in rules_of(lint(wf, kit_tool_api))


def test_l011_api_call_shape(wf: dict[str, Any], kit_tool_api: ToolApi) -> None:
    props = wf["nodes"]["talk"]["tools"][0]["function"]["parameters"]["properties"]
    props["answer"] = {"type": "string"}
    props["body"]["properties"]["answer"]["const"] = "x"
    del props["body"]["properties"]["field"]["default"]
    props["body"]["required"].append("ghost")
    messages = " | ".join(f.message for f in lint(wf, kit_tool_api).by_rule("L011"))
    assert "next to url/method/body" in messages and "'const'" in messages and "ghost" in messages
    props = wf["nodes"]["close"]["pre_actions"][0]["function"]["parameters"]["properties"]
    del props["body"]["properties"]["outcome"]["default"]
    props["body"]["default"] = {"a": 1}
    messages = " | ".join(f.message for f in lint(wf, kit_tool_api).by_rule("L011"))
    assert "body 'default' template object" in messages and "body field without 'default'" in messages


def test_l012_tool_api(wf: dict[str, Any], kit_tool_api: ToolApi) -> None:
    function = wf["nodes"]["talk"]["tools"][0]["function"]
    function["parameters"]["properties"]["url"]["default"] = "https://elsewhere.example/verify"
    function["parameters"]["properties"]["body"]["properties"]["field"]["enum"] = ["date_of_birth", "shoe_size"]
    function["parameters"]["properties"]["body"]["properties"]["bogus"] = {"type": "string", "default": "{{bogus}}"}
    del function["parameters"]["properties"]["body"]["properties"]["answer"]
    messages = " | ".join(f.message for f in lint(wf, kit_tool_api).by_rule("L012"))
    for needle in ("differs from the contract", "shoe_size", "not a request field", "required field 'answer'"):
        assert needle in messages
    wf = good(kit_tool_api)
    wf["nodes"]["talk"]["tools"][0]["function"]["name"] = "launch_rocket"
    assert any("not in the tool API" in f.message for f in lint(wf, kit_tool_api).by_rule("L012"))
    wf = good(kit_tool_api)
    wf["nodes"]["close"]["pre_actions"][0]["function"]["parameters"]["properties"]["body"]["properties"]["outcome"][
        "default"] = "vanished"
    assert any("baked value 'vanished'" in f.message for f in lint(wf, kit_tool_api).by_rule("L012"))


def test_l013_close_call(wf: dict[str, Any], kit_tool_api: ToolApi) -> None:
    wf["nodes"]["talk"]["transitions"].append(tool_transition("bye", "skip the close"))
    report = lint(wf, kit_tool_api)
    assert any("without close_call" in f.message for f in report.by_rule("L013"))
    wf = good(kit_tool_api)
    del wf["nodes"]["bye"]
    wf["nodes"]["again"] = code_node("again", "vars['a'] = 1\n", "close2")
    wf["nodes"]["close2"] = copy.deepcopy(wf["nodes"]["close"])
    wf["nodes"]["close2"]["name"] = "close2"
    wf["nodes"]["close2"]["passthrough_default_transition"] = "end"
    wf["nodes"]["close2"]["transitions"] = [always_transition("end")]
    wf["nodes"]["end"] = end_call_node("end", "x")
    wf["nodes"]["close"]["passthrough_default_transition"] = "again"
    wf["nodes"]["close"]["transitions"] = [always_transition("again")]
    assert any("twice" in f.message for f in lint(wf, kit_tool_api).by_rule("L013"))


def test_l013_model_called_close_and_lifecycle_tool(kit_tool_api: ToolApi) -> None:
    close = model_tool(kit_tool_api.tools["close_call"], description="CALL it last.")
    wf = {
        "initial_node": "talk",
        "nodes": {
            "talk": {"name": "talk", "node_type": "conversation", "tools": [close], "max_node_turns": 3,
                     "max_turn_transition": "bye",
                     "transitions": [transition("bye", "closed", or_group({"variable": "close_call_success_count",
                                                                              "operator": "greater_than", "operand": 0}))]},
            "bye": end_call_node("bye", "x"),
        },
    }
    assert lint(wf, kit_tool_api).ok
    wf["nodes"]["talk"]["tools"] = []
    assert "L013" in rules_of(lint(wf, kit_tool_api))
    wf["tools"] = [{"type": "function", "config": {"alwaysRunsAt": ["end_of_call"]},
                    "function": dict(close["function"])}]
    assert "L013" not in rules_of(lint(wf, kit_tool_api))


def test_l014_variables(wf: dict[str, Any], kit_tool_api: ToolApi) -> None:
    wf["nodes"]["talk"]["task_message"] = "Hi {{nobody}} {{greeting}} {{system.conversation}} {{customer_id:json}} {{talk.current_turns}}"
    wf["nodes"]["talk"]["transitions"].append(transition("close", "c", or_group(
        {"variable": "ghost", "operator": "is_set"}, {"variable": "verify_identity_success_count", "operator": "greater_than", "operand": 0})))
    wf["nodes"]["close"]["pre_actions"][0]["function"]["parameters"]["properties"]["body"]["properties"]["outcome"][
        "default"] = "{{not_written}}"
    messages = [f.message for f in lint(wf, kit_tool_api).by_rule("L014")]
    assert any("nobody" in m for m in messages)
    assert any("ghost" in m for m in messages)
    assert any("not_written" in m for m in messages)
    assert not any(token in m for m in messages for token in ("greeting", "system.", "customer_id", "current_turns", "success_count"))


def test_l014_dynamic_names_written_by_code(wf: dict[str, Any], kit_tool_api: ToolApi) -> None:
    code = "for pid in ['a']:\n    vars[pid + '_deadline'] = '1'\nvars['plain'] = '2'\nvars['greeting'] = 'x'\n"
    wf["nodes"]["setup"]["pre_actions"][0]["function"]["parameters"]["properties"]["code"]["default"] = code
    wf["nodes"]["talk"]["task_message"] = "{{p1_deadline}} {{plain}}"
    assert not lint(wf, kit_tool_api).by_rule("L014")


def test_l015_min_confidence(wf: dict[str, Any], kit_tool_api: ToolApi) -> None:
    del wf["nodes"]["talk"]["extraction_schema"][0]["min_confidence"]
    assert "L015" in rules_of(lint(wf, kit_tool_api))


def test_l016_end_call_shape(wf: dict[str, Any], kit_tool_api: ToolApi) -> None:
    bye = wf["nodes"]["bye"]
    bye["on_enter"] = {"type": "tts", "text": "bye"}
    bye["transitions"] = [always_transition("talk")]
    bye["disable_inactivity_monitor"] = False
    assert len(lint(wf, kit_tool_api).by_rule("L016")) == 3


def test_l017_max_turns(wf: dict[str, Any], kit_tool_api: ToolApi) -> None:
    del wf["nodes"]["talk"]["max_turn_transition"]
    assert "L017" in rules_of(lint(wf, kit_tool_api))


def test_l018_length_is_a_warning(wf: dict[str, Any], kit_tool_api: ToolApi) -> None:
    wf["system_prompt"] = "y" * 6001
    report = lint(wf, kit_tool_api)
    assert report.ok and report.by_rule("L018")[0].severity == "warning"


def test_l019_agent_config(wf: dict[str, Any], kit_tool_api: ToolApi) -> None:
    wf["agent_config"] = {"model": "x"}
    assert "L019" in rules_of(lint(wf, kit_tool_api))


def test_l020_tool_descriptions(wf: dict[str, Any], kit_tool_api: ToolApi) -> None:
    wf["nodes"]["talk"]["tools"][0]["function"]["description"] = "Looks up an answer"
    assert any(f.rule == "L020" and f.severity == "warning" for f in lint(wf, kit_tool_api).findings)


def test_l021_condition_shape_and_order(wf: dict[str, Any], kit_tool_api: ToolApi) -> None:
    wf["nodes"]["talk"]["transitions"].insert(0, {"target_node": "close", "description": "i", "condition": {
        "operator": "matches_intent", "description": "wants out"}})
    report = lint(wf, kit_tool_api)
    assert any("after a matches_intent" in f.message for f in report.by_rule("L021"))
    wf["nodes"]["talk"]["transitions"][1]["condition"]["logic"] = "and"
    wf["nodes"]["talk"]["transitions"].append({"target_node": "close", "description": "b", "condition": {
        "variable": "n", "operator": "is_set"}})
    messages = [f for f in lint(wf, kit_tool_api).by_rule("L021")]
    assert any(f.severity == "error" and "logic 'or'" in f.message for f in messages)
    assert any(f.severity == "warning" and "bare variable" in f.message for f in messages)


def test_l022_ir_placeholder_in_spoken_text(wf: dict[str, Any], kit_tool_api: ToolApi) -> None:
    wf["nodes"]["talk"]["on_enter"]["text"] = "Am I speaking with {title} {{first_name}}?"
    assert any(f.rule == "L022" and "{title}" in f.message for f in lint(wf, kit_tool_api).findings)


def test_l023_tool_call_on_non_conversation_node(wf: dict[str, Any], kit_tool_api: ToolApi) -> None:
    wf["nodes"]["setup"]["transitions"][0]["is_tool_call"] = True
    assert "L023" in rules_of(lint(wf, kit_tool_api))


def test_l024_name_and_l025_transition_shape(wf: dict[str, Any], kit_tool_api: ToolApi) -> None:
    wf["nodes"]["talk"]["name"] = "other"
    wf["nodes"]["talk"]["transitions"].append({"description": "no target"})
    wf["nodes"]["talk"]["transitions"].append({"target_node": "close", "condition": {"operator": "matches_intent"}})
    report = lint(wf, kit_tool_api)
    assert report.by_rule("L024") and len(report.by_rule("L025")) == 2


def test_l026_duplicate_tools_and_l027_empty_tts(wf: dict[str, Any], kit_tool_api: ToolApi) -> None:
    wf["nodes"]["talk"]["tools"].append(copy.deepcopy(wf["nodes"]["talk"]["tools"][0]))
    wf["nodes"]["talk"]["on_enter"]["text"] = "  "
    report = lint(wf, kit_tool_api)
    assert report.by_rule("L026") and report.by_rule("L027")


def test_the_validated_reference_agents_have_no_lint_errors(kit_tool_api: ToolApi) -> None:
    """The hand-written reference scored 9/10; its JSON must lint clean (warnings are fine)."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("brief_a_ref", REPO / "evals" / "reference" / "brief_a_ref.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    call_vars = {"customer_id", "first_name", "last_name", "title", "now"}
    for multi in (False, True):
        report = lint_workflow(json.loads(json.dumps(module.build(multi=multi))), kit_tool_api, call_vars)
        assert report.ok, report.format()


def test_the_kit_base_agent_shows_the_typical_errors(kit_tool_api: ToolApi) -> None:
    base = json.loads((REPO / "examples" / "base-agent.json").read_text(encoding="utf-8"))
    report = lint_workflow(base, kit_tool_api, {"customer_id"})
    assert {"L001", "L006", "L017"} <= rules_of(report)
    assert "L013" not in rules_of(report)  # its lifecycle close_call tool satisfies the rule


# ---- L028: one transition per target ---------------------------------------------------------------------------------------


def test_l028_transitions_of_one_node_have_distinct_targets(wf: dict[str, Any], kit_tool_api: ToolApi) -> None:
    assert "L028" not in rules_of(lint(wf, kit_tool_api))
    wf["nodes"]["talk"]["transitions"].append(tool_transition("close", "The caller wants to leave."))
    finding = lint(wf, kit_tool_api).by_rule("L028")[0]
    assert finding.severity == "error" and finding.path == "nodes.talk.transitions"
    assert "'close'" in finding.message and "merge" in finding.message


def test_l028_a_deterministic_and_a_tool_exit_to_one_node_is_the_typical_case(wf: dict[str, Any],
                                                                              kit_tool_api: ToolApi) -> None:
    wf["nodes"]["talk"]["transitions"][1]["target_node"] = "close"
    report = lint(wf, kit_tool_api)
    assert [f.rule for f in report.errors if f.rule == "L028"] == ["L028"]


# ---- L029: hooks, conditions and prompts read fields the contract returns --------------------------------------------------------


def test_l029_post_hooks_read_only_fields_the_contract_returns(wf: dict[str, Any], kit_tool_api: ToolApi) -> None:
    from builder.compiler import hooks

    tool = wf["nodes"]["talk"]["tools"][0]
    tool["post_process_code"] = hooks.post_hook([(hooks.flag_true("verified"), "x")], default="y")
    assert "L029" not in rules_of(lint(wf, kit_tool_api))  # what_to_do_next is written, never read
    tool["post_process_code"] = hooks.post_hook([(hooks.flag_true("success"), "x")], default="y")
    finding = lint(wf, kit_tool_api).by_rule("L029")[0]
    assert finding.severity == "error" and "'success'" in finding.message and "verify_identity" in finding.message
    assert finding.path == "code[talk/verify_identity.post_process_code]"


def test_l029_conditions_and_prompts_name_real_response_fields(wf: dict[str, Any], kit_tool_api: ToolApi) -> None:
    wf["nodes"]["talk"]["transitions"][0] = transition(
        "close", "verified", or_group(*bool_true("verify_identity_response.data.passed")))
    report = lint(wf, kit_tool_api)
    assert [f.path for f in report.by_rule("L029")] == ["nodes.talk.transitions[0].condition.conditions[0]",
                                                        "nodes.talk.transitions[0].condition.conditions[1]"]
    wf["nodes"]["talk"]["transitions"][0] = transition(
        "close", "verified", or_group(*bool_true("verify_identity_response.data.all_verified")))
    wf["nodes"]["talk"]["task_message"] = "Result: {{verify_identity_response.data.nope}} {{mood}}"
    report = lint(wf, kit_tool_api)
    assert report.by_rule("L029")[0].path == "nodes.talk.task_message"
    wf["nodes"]["talk"]["task_message"] = "Result: {{get_account_response.data.products.0.past_due}} {{mood}}"
    assert "L029" not in rules_of(lint(wf, kit_tool_api))
