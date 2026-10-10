"""Tests for the lean profile: fewer nodes, the model calls close_call itself."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from builder.checks.code_sandbox import extract_code_snippets, run_hook, run_snippet
from builder.checks.lint import lint_workflow
from builder.compiler.compile import compile_workflow
from builder.compiler.errors import CompileError
from builder.ingest.tool_api import ToolApi, load_tool_api
from builder.policy.ir import PolicyIR, RuleLedger

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "fixtures" / "policy"


def _ir(name: str) -> PolicyIR:
    return PolicyIR.model_validate(json.loads((FIXTURES / f"{name}_ir.json").read_text(encoding="utf-8")))


def _ledger(name: str) -> RuleLedger:
    return RuleLedger.model_validate(json.loads((FIXTURES / f"{name}_ledger.json").read_text(encoding="utf-8")))


IR_A, IR_AB = _ir("brief_a"), _ir("brief_ab")
LEDGER_A, LEDGER_AB = _ledger("brief_a"), _ledger("brief_ab")
CASES = {c["id"]: c for c in json.loads((REPO / "public-test-set.json").read_text(encoding="utf-8"))}


@pytest.fixture(scope="module")
def api() -> ToolApi:
    return load_tool_api(REPO / "tools" / "openapi.json")


@pytest.fixture(scope="module")
def lean_a(api: ToolApi) -> dict[str, Any]:
    return compile_workflow(IR_A, api, LEDGER_A, None, "lean").workflow


@pytest.fixture(scope="module")
def lean_ab(api: ToolApi) -> dict[str, Any]:
    return compile_workflow(IR_AB, api, LEDGER_AB, None, "lean").workflow


def _tools(node: dict[str, Any]) -> list[str]:
    return [t["function"]["name"] for t in node.get("tools", [])]


@pytest.mark.parametrize(("ir", "ledger"), [(IR_A, LEDGER_A), (IR_AB, LEDGER_AB)], ids=["A", "AB"])
def test_lean_lints_clean_and_is_null_free(ir: PolicyIR, ledger: RuleLedger, api: ToolApi) -> None:
    compiled = compile_workflow(ir, api, ledger, None, "lean")
    report = lint_workflow(compiled.workflow, api, set(ir.call_variable_names()))
    assert report.ok, report.format()
    assert compiled.profile == "lean" and "null" not in json.dumps(compiled.workflow)
    for key, code in extract_code_snippets(compiled.workflow).items():
        assert "{{" not in code and "}}" not in code, key


def test_lean_has_six_nodes(lean_a: dict[str, Any]) -> None:
    assert list(lean_a["nodes"]) == ["setup", "call", "load_account", "account_facts", "negotiate", "hangup"]
    assert lean_a["initial_node"] == "setup"
    types = {name: node["node_type"] for name, node in lean_a["nodes"].items()}
    assert types == {"setup": "code", "call": "conversation", "load_account": "api_request", "account_facts": "code",
                     "negotiate": "conversation", "hangup": "end_call"}


def test_the_model_calls_close_call_and_the_engine_routes_on_it(lean_a: dict[str, Any]) -> None:
    call, negotiate = lean_a["nodes"]["call"], lean_a["nodes"]["negotiate"]
    assert _tools(call) == ["verify_identity", "schedule_callback", "transfer_to_agent", "close_call"]
    assert _tools(negotiate) == ["create_promise", "transfer_to_agent", "close_call"]
    verified, closed = call["transitions"]
    assert verified["target_node"] == "load_account" and len(verified["condition"]["conditions"]) == 2
    closing = {"variable": "close_call_success_count", "operator": "greater_than", "operand": 0}
    assert closed["target_node"] == "hangup" and closed["condition"]["conditions"] == [closing]
    assert negotiate["transitions"][0]["condition"]["conditions"] == [closing]
    assert call["max_turn_transition"] == negotiate["max_turn_transition"] == "hangup"
    assert negotiate["on_enter"] == {"type": "tts", "text": "{{debt_statement}}"}
    assert call["on_enter"]["text"].startswith("Hello, I am a digital assistant calling from Freya Bank. We record this call.")


def test_multi_product_lean_gets_the_notes_tool(lean_ab: dict[str, Any]) -> None:
    assert _tools(lean_ab["nodes"]["negotiate"]) == ["create_promise", "add_note", "transfer_to_agent", "close_call"]


def test_the_closing_table_is_in_the_prompts(lean_a: dict[str, Any]) -> None:
    for node in ("call", "negotiate"):
        task = lean_a["nodes"][node]["task_message"]
        assert "CLOSING THE CALL" in task
        assert 'outcome "promise"' in task and 'outcome "paid"' in task
        assert 'outcome "not_verified" and reason "failed"' in task and 'outcome "not_reached" and reason "callback"' in task
        assert 'If a transfer follows a recorded promise, close with outcome "transferred"' in task
        assert "call close_call with" in task
    call = lean_a["nodes"]["call"]["task_message"]
    assert 'call transfer_to_agent with queue "general", then close_call with outcome "transferred" and reason "human"' in call
    assert "IF SOMEONE ELSE ANSWERED" in call and "IDENTITY" in call
    negotiate = lean_a["nodes"]["negotiate"]["task_message"]
    assert 'queue "support", then close_call with outcome "transferred" and reason "support"' in negotiate
    assert 'call close_call with outcome "no_promise" and reason "refused"' in negotiate
    assert "transition_to_" not in call + negotiate


def test_multi_product_closing_rule_is_explained(lean_ab: dict[str, Any]) -> None:
    task = lean_ab["nodes"]["negotiate"]["task_message"]
    assert 'first product (in the listed order) whose note is not "paid"' in task
    assert 'every note is "paid", use outcome "paid"' in task


def test_identity_hook_tells_the_model_to_close_after_the_last_wrong_answer(lean_a: dict[str, Any]) -> None:
    hook = lean_a["nodes"]["call"]["tools"][0]["post_process_code"]
    result = {"status": "success", "data": {"ok": True, "verified": False, "wrong_answers_left": 0}}
    text = run_hook(hook, result=result)["result"]["data"]["what_to_do_next"]
    assert 'call close_call with outcome "not_verified" and reason "failed"' in text
    assert "I'm sorry, I could not verify your identity." in text


def test_close_call_tool_is_built_from_the_contract(lean_a: dict[str, Any], api: ToolApi) -> None:
    tool = next(t for t in lean_a["nodes"]["negotiate"]["tools"] if t["function"]["name"] == "close_call")
    body = tool["function"]["parameters"]["properties"]["body"]
    assert body["properties"]["outcome"]["enum"] == api.tools["close_call"].enum("outcome")
    assert set(body["properties"]) == {"customer_id", "outcome", "reason"}
    assert body["required"] == ["outcome"]
    assert tool["config"]["maxRetries"] == 1 and "LAST tool call" in tool["function"]["description"]


@pytest.mark.parametrize("case_id", ["PUB-01", "PUB-06", "PUB-13"])
def test_lean_code_nodes_compute_the_same_facts(case_id: str, api: ToolApi) -> None:
    case = CASES[case_id]
    ir, ledger = (IR_A, LEDGER_A) if case["brief"] == "A" else (IR_AB, LEDGER_AB)
    staged = compile_workflow(ir, api, ledger, None, "staged").workflow
    lean = compile_workflow(ir, api, ledger, None, "lean").workflow
    products = [{"product_id": f"p{i + 1}", **p} for i, p in enumerate(case["products"])]
    results = []
    for workflow in (staged, lean):
        snippets = extract_code_snippets(workflow)
        state = run_snippet(snippets["setup"], dict(case["variables"]))
        results.append(run_snippet(snippets["account_facts"], {**state, "products": products}))
    assert results[0]["account_facts"] == results[1]["account_facts"]
    assert results[0]["debt_statement"] == results[1]["debt_statement"]


def test_lean_errors_match_staged(api: ToolApi) -> None:
    broken = api.model_copy(update={"tools": {k: v for k, v in api.tools.items() if k != "close_call"}})
    with pytest.raises(CompileError, match="close_call"):
        compile_workflow(IR_A, broken, LEDGER_A, None, "lean")
    ir = IR_A.model_copy(deep=True)
    ir.identity.questions = []
    with pytest.raises(CompileError, match="identity question"):
        compile_workflow(ir, api, LEDGER_A, None, "lean")
