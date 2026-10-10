"""Tests for the staged profile: the generated workflow mirrors the validated reference agent.

* the workflow compiled from the Brief A / Brief A+B fixture IRs lints clean, has no ``null`` and no ``{{`` in code;
* its node types and transition structure equal the hand-authored reference (``evals/reference/brief_a_ref.py``);
* every generated code snippet is executed in the sandbox with practice-like variables (now 2026-10-14T11:00):
  deadlines, floors, allowed dates, the debt statement and the ``decide`` outcome of all 13 public cases;
* nothing in the compiler sources is specific to a brief.
"""

from __future__ import annotations

import importlib.util
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from builder.checks.code_sandbox import extract_code_snippets, run_hook, run_snippet
from builder.checks.lint import lint_workflow
from builder.compiler.compile import CompiledWorkflow, compile_workflow
from builder.compiler.errors import CompileError
from builder.compiler.prose import NodeProse
from builder.compiler.workflow_model import Workflow
from builder.ingest.tool_api import ToolApi, load_tool_api
from builder.policy.calendar import BusinessCalendar, deadline_for
from builder.policy.ir import PolicyIR, RuleLedger

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "fixtures" / "policy"
NOW = "2026-10-14T11:00"
TODAY = __import__("datetime").date(2026, 10, 14)


def _ir(name: str) -> PolicyIR:
    return PolicyIR.model_validate(json.loads((FIXTURES / f"{name}_ir.json").read_text(encoding="utf-8")))


def _ledger(name: str) -> RuleLedger:
    return RuleLedger.model_validate(json.loads((FIXTURES / f"{name}_ledger.json").read_text(encoding="utf-8")))


IR_A, IR_AB = _ir("brief_a"), _ir("brief_ab")
LEDGER_A, LEDGER_AB = _ledger("brief_a"), _ledger("brief_ab")
CASES: dict[str, dict[str, Any]] = {
    c["id"]: c for c in json.loads((REPO / "public-test-set.json").read_text(encoding="utf-8"))
}
BRIEFS = {"A": (IR_A, LEDGER_A), "B": (IR_AB, LEDGER_AB)}


@pytest.fixture(scope="module")
def api() -> ToolApi:
    return load_tool_api(REPO / "tools" / "openapi.json")


@pytest.fixture(scope="module")
def compiled_a(api: ToolApi) -> CompiledWorkflow:
    return compile_workflow(IR_A, api, LEDGER_A, None, "staged")


@pytest.fixture(scope="module")
def compiled_ab(api: ToolApi) -> CompiledWorkflow:
    return compile_workflow(IR_AB, api, LEDGER_AB, None, "staged")


def _reference(multi: bool) -> dict[str, Any]:
    spec = importlib.util.spec_from_file_location("brief_a_ref", REPO / "evals" / "reference" / "brief_a_ref.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return json.loads(json.dumps(module.build(multi=multi)))


def _role(name: str) -> str:
    if name.startswith("end_") and name != "end_call":
        return "end"
    return "handoff" if name.startswith("handoff_") else name


def _kind(transition: dict[str, Any]) -> str:
    condition = transition.get("condition") or {}
    if transition.get("is_tool_call"):
        return "tool"
    if condition.get("operator") == "always":
        return "always"
    return "intent" if condition.get("operator") == "matches_intent" else "var"


def _skeleton(workflow: dict[str, Any]) -> set[tuple[str, str, str]]:
    edges: set[tuple[str, str, str]] = set()
    for name, node in workflow["nodes"].items():
        for transition in node.get("transitions", []):
            edges.add((_role(name), _kind(transition), _role(transition["target_node"])))
        if node.get("passthrough_default_transition"):
            edges.add((_role(name), "pass", _role(node["passthrough_default_transition"])))
        if node.get("max_turn_transition"):
            edges.add((_role(name), "max", _role(node["max_turn_transition"])))
    return edges


# --------------------------------------------------------------------------------------------------------------------
# Shape and static checks
# --------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("brief", ["A", "B"])
def test_the_compiled_workflow_lints_clean(brief: str, api: ToolApi) -> None:
    ir, ledger = BRIEFS[brief]
    compiled = compile_workflow(ir, api, ledger)
    report = lint_workflow(compiled.workflow, api, set(ir.call_variable_names()))
    assert report.ok, report.format()
    assert not [f for f in report.warnings if f.rule != "L018"], report.format()
    assert compiled.profile == "staged" and compiled.node_count == len(compiled.workflow["nodes"])
    assert compiled.notes == []


@pytest.mark.parametrize("brief", ["A", "B"])
def test_no_null_and_no_double_braces_in_code(brief: str, api: ToolApi) -> None:
    ir, ledger = BRIEFS[brief]
    workflow = compile_workflow(ir, api, ledger).workflow
    assert "null" not in json.dumps(workflow)
    assert Workflow.from_json(workflow).to_json() == workflow
    snippets = extract_code_snippets(workflow)
    assert len(snippets) > 20
    for key, code in snippets.items():
        assert "{{" not in code and "}}" not in code, key
        compile(code, key, "exec")
    assert "agent_config" not in workflow


@pytest.mark.parametrize(("brief", "name"), [("A", "ref-a"), ("B", "ref-ab")])
def test_structure_matches_the_validated_reference(brief: str, name: str, api: ToolApi) -> None:
    ir, ledger = BRIEFS[brief]
    mine = compile_workflow(ir, api, ledger).workflow
    reference = _reference(multi=brief == "B")
    assert _skeleton(reference) <= _skeleton(mine)
    assert _skeleton(mine) <= _skeleton(reference)
    types = Counter(node["node_type"] for node in mine["nodes"].values())
    expected = Counter(node["node_type"] for node in reference["nodes"].values())
    for node_type in ("conversation", "api_request", "end_call"):
        assert types[node_type] == expected[node_type], node_type
    assert mine["initial_node"] == reference["initial_node"] == "setup"
    shared = {"setup", "opening", "third_party", "identity", "callback_done", "promise_done", "identity_failed",
              "load_account", "account_facts", "negotiate", "decide", "close"}
    assert shared <= set(mine["nodes"]) and shared <= set(reference["nodes"])
    for node in shared:
        assert mine["nodes"][node]["node_type"] == reference["nodes"][node]["node_type"], node


def test_node_set_of_brief_a(compiled_a: CompiledWorkflow) -> None:
    nodes = compiled_a.workflow["nodes"]
    assert {"end_already_paid", "end_dispute", "end_hardship", "end_flat_refusal", "end_recording_failed",
            "end_promise_recorded", "end_callback_booked", "end_callback_not_booked", "end_wrong_number",
            "end_deceased", "end_unclear_party", "end_identity_declined", "end_identity_failed", "end_refuse_to_talk",
            "end_auto", "handoff_support", "handoff_restructuring", "handoff_general", "hangup", "promise_fix",
            "promise_fix_say"} <= set(nodes)
    assert all(nodes[n]["node_type"] == "code" for n in nodes if n.startswith("end_") and n != "end_call")
    assert nodes["hangup"]["node_type"] == "end_call"


# --------------------------------------------------------------------------------------------------------------------
# Mechanisms
# --------------------------------------------------------------------------------------------------------------------


def _tool_names(node: dict[str, Any]) -> list[str]:
    return [t["function"]["name"] for t in node.get("tools", [])]


def test_setup_and_opening(compiled_a: CompiledWorkflow) -> None:
    nodes = compiled_a.workflow["nodes"]
    setup = nodes["setup"]
    assert setup["node_type"] == "code" and setup["passthrough"] and setup["passthrough_default_transition"] == "opening"
    opening = nodes["opening"]
    assert opening["on_enter"] == {
        "type": "tts",
        "text": "Hello, I am a digital assistant calling from Freya Bank. We record this call. "
                "Am I speaking with {{title}} {{last_name}}?",
    }
    assert opening["on_enter_first_entry_only"] is True
    assert "recorded" not in opening["on_enter"]["text"]
    assert opening["max_turn_transition"] == "end_unclear_party"
    assert all(t.get("is_tool_call") for t in opening["transitions"])
    targets = {t["target_node"] for t in opening["transitions"]}
    assert {"identity", "third_party", "end_unclear_party", "end_wrong_number", "end_deceased", "handoff_general",
            "end_refuse_to_talk"} <= targets
    assert "tools" not in opening or not opening["tools"]
    assert "transition_to_identity" in opening["task_message"] and "{{title}} {{last_name}}" in opening["task_message"]


def test_third_party_books_callbacks_with_a_deterministic_farewell(compiled_a: CompiledWorkflow) -> None:
    nodes = compiled_a.workflow["nodes"]
    third = nodes["third_party"]
    assert _tool_names(third) == ["schedule_callback"]
    tool = third["tools"][0]
    assert "what_to_do_next" in tool["post_process_code"] and tool["pre_process_code"]
    first = third["transitions"][0]
    assert first["target_node"] == "callback_done"
    assert first["condition"]["conditions"] == [
        {"variable": "schedule_callback_success_count", "operator": "greater_than", "operand": 0}]
    assert any(t["condition"].get("operator") == "matches_intent" and not t.get("is_tool_call")
               for t in third["transitions"])
    assert "{{callback_facts}}" in third["task_message"]
    done = nodes["callback_done"]
    assert done["on_enter"] == {"type": "tts", "text": "The callback is booked. Thank you for your time. Goodbye."}
    assert done["passthrough"] and done["passthrough_default_transition"] == "end_callback_booked"


def test_identity_routes_deterministically(compiled_a: CompiledWorkflow) -> None:
    nodes = compiled_a.workflow["nodes"]
    identity = nodes["identity"]
    assert _tool_names(identity) == ["verify_identity"]
    verified, failed = identity["transitions"][0], identity["transitions"][1]
    assert verified["target_node"] == "load_account"
    assert verified["condition"]["conditions"] == [
        {"variable": "verify_identity_response.data.all_verified", "operator": "equals", "operand": "true"},
        {"variable": "verify_identity_response.data.all_verified", "operator": "equals", "operand": "True"}]
    assert failed["target_node"] == "identity_failed"
    assert failed["condition"]["conditions"] == [
        {"variable": "verify_identity_response.data.wrong_answers_left", "operator": "equals", "operand": "0"}]
    assert identity["on_enter"]["type"] == "llm" and "date of birth" in identity["on_enter"]["prompt"]
    assert "FIRST" in identity["task_message"] and "what_to_do_next" in identity["task_message"]
    failure = nodes["identity_failed"]
    assert failure["on_enter"]["text"] == (
        "I'm sorry, I could not verify your identity. Please use the mobile app or visit a branch. "
        "Thank you for your time. Goodbye.")
    assert failure["passthrough_default_transition"] == "end_identity_failed"


def test_verify_identity_post_hook_follows_the_brief(compiled_a: CompiledWorkflow) -> None:
    hook = compiled_a.workflow["nodes"]["identity"]["tools"][0]["post_process_code"]

    def say(data: dict[str, Any]) -> str:
        return run_hook(hook, result={"status": "success", "header": {}, "data": data})["result"]["data"]["what_to_do_next"]

    assert "verified" in say({"ok": True, "verified": True, "all_verified": True})
    assert "next security question" in say({"ok": True, "verified": True, "all_verified": False, "wrong_answers_left": 3})
    assert "could not be read" in say({"ok": False})
    assert "month as a word" in say({"ok": False, "hint": "day_month_swapped"})
    assert "does not count as a wrong answer" in say({"ok": False, "hint": "day_month_swapped"})
    assert say({"ok": True, "verified": False, "wrong_answers_left": 2}).startswith("This answer is wrong")
    ended = say({"ok": True, "verified": False, "wrong_answers_left": 0})
    assert ended.startswith("Three wrong answers") and "Do not state the debt" in ended
    pre = compiled_a.workflow["nodes"]["identity"]["tools"][0]["pre_process_code"]
    body = run_hook(pre, config={"body": {"field": "date_of_birth", "answer": "14/03/1988"}})["config"]["body"]
    assert body["answer"] == "1988-03-14"


def test_load_account_and_account_facts(compiled_a: CompiledWorkflow) -> None:
    nodes = compiled_a.workflow["nodes"]
    load = nodes["load_account"]
    assert load["node_type"] == "api_request" and load["passthrough_default_transition"] == "account_facts"
    action = load["pre_actions"][0]
    assert action["config"]["maxRetries"] == 2 and action["function"]["name"] == "get_account"
    assert load["response_mapping"] == [{"path": "data.products", "variable": "products"}]
    assert "response_mapping" not in action
    assert nodes["account_facts"]["node_type"] == "code"
    assert nodes["account_facts"]["passthrough_default_transition"] == "negotiate"


def test_negotiate_node(compiled_a: CompiledWorkflow, compiled_ab: CompiledWorkflow) -> None:
    node = compiled_a.workflow["nodes"]["negotiate"]
    assert node["on_enter"] == {"type": "tts", "text": "{{debt_statement}}"} and node["on_enter_first_entry_only"]
    assert _tool_names(node) == ["create_promise"]
    guard = node["transitions"][0]  # the guard recovery comes first: a blocked call is fixed before anything else
    assert guard["target_node"] == "promise_fix"
    assert guard["condition"]["conditions"] == [{"variable": "promise_guard", "operator": "contains", "operand": "blocked"}]
    first = node["transitions"][1]
    assert first["target_node"] == "promise_done"
    assert first["condition"]["conditions"][0]["logic"] == "and"
    assert {"variable": "product_count", "operator": "equals", "operand": "1"} in first["condition"]["conditions"][0]["conditions"]
    second = node["transitions"][2]
    assert second["target_node"] == "end_auto" and second["condition"]["operator"] == "matches_intent"
    assert node["max_turn_transition"] == "end_auto"
    schema = node["extraction_schema"][0]
    assert schema["name"] == "call_situation" and schema["min_confidence"] == 0.6
    assert schema["enum_values"][0] == "none"
    assert set(schema["enum_values"][1:]) == {"hardship", "already_paid", "dispute", "refuse_to_talk", "flat_refusal"}
    assert _tool_names(compiled_ab.workflow["nodes"]["negotiate"]) == ["create_promise", "add_note"]
    note = compiled_ab.workflow["nodes"]["negotiate"]["tools"][1]
    assert "notes_log" in note["pre_process_code"]
    assert note["function"]["parameters"]["properties"]["body"]["properties"]["reason"]["enum"] == [
        "refused", "hardship", "dispute", "paid", "service_error"]
    assert "add_note" not in json.dumps(compiled_a.workflow)


def test_create_promise_hooks(compiled_a: CompiledWorkflow) -> None:
    tool = compiled_a.workflow["nodes"]["negotiate"]["tools"][0]
    body = run_hook(tool["pre_process_code"], vars={"product_ids": "p1"},
                    config={"body": {"product_id": "1", "amount": "420.00", "date": "2026-10-14T00:00"}})["config"]["body"]
    assert body == {"product_id": "p1", "amount": 420.0, "date": "2026-10-14"}
    ok = run_hook(tool["post_process_code"], result={"status": "success", "data": {"ok": True}})["result"]["data"]
    assert "IS recorded" in ok["what_to_do_next"] and "You will receive a text message confirming this promise." in ok["what_to_do_next"]
    failed = run_hook(tool["post_process_code"], result={"status": "success", "data": {"ok": False}})["result"]["data"]
    assert "NOT recorded" in failed["what_to_do_next"] and "once more" in failed["what_to_do_next"]
    assert "transition_to_end_recording_failed" in failed["what_to_do_next"]
    broken = run_hook(tool["post_process_code"], result={"status": "error"})["result"]
    assert "NOT recorded" in broken["what_to_do_next"]


def test_the_close_chain(compiled_a: CompiledWorkflow) -> None:
    nodes = compiled_a.workflow["nodes"]
    assert nodes["decide"]["passthrough_default_transition"] == "close"
    close = nodes["close"]
    action = close["pre_actions"][0]
    body = action["function"]["parameters"]["properties"]["body"]["properties"]
    assert {k: v["default"] for k, v in body.items()} == {
        "customer_id": "{{customer_id}}", "outcome": "{{final_outcome}}", "reason": "{{final_reason}}"}
    assert close["passthrough_default_transition"] == "hangup"
    hangup = nodes["hangup"]
    assert hangup["node_type"] == "end_call" and hangup["on_enter"] == {"type": "silent"} and hangup["transitions"] == []
    props = hangup["terminal"]["function"]["parameters"]["properties"]
    assert props == {"message_mode": {"type": "string", "default": "none"}}


def test_handoff_nodes_bake_the_queue(compiled_a: CompiledWorkflow) -> None:
    nodes = compiled_a.workflow["nodes"]
    for node_name, queue in (("handoff_support", "support"), ("handoff_restructuring", "restructuring"),
                             ("handoff_general", "general")):
        body = nodes[node_name]["pre_actions"][0]["function"]["parameters"]["properties"]["body"]["properties"]
        assert body["queue"]["default"] == queue and body["customer_id"]["default"] == "{{customer_id}}"
        assert nodes[node_name]["passthrough_default_transition"] == "decide"


def test_tool_urls_methods_fields_and_enums_match_the_tool_api(compiled_ab: CompiledWorkflow, api: ToolApi) -> None:
    seen = 0
    for node in compiled_ab.workflow["nodes"].values():
        for tool in [*node.get("tools", []), *node.get("pre_actions", [])]:
            function = tool["function"]
            if function["handler"] != "__function__:api_call":
                continue
            seen += 1
            contract = api.tools[function["name"]]
            props = function["parameters"]["properties"]
            assert props["url"]["default"] == contract.url and props["method"]["default"] == contract.method
            for field, leaf in props["body"]["properties"].items():
                spec = contract.field(field, "request")
                assert set(leaf.get("enum", spec.enum or [])) <= set(spec.enum or leaf.get("enum", []))
                assert "const" not in leaf and "default" in leaf
            assert set(contract.required_fields) <= set(props["body"]["properties"])
    assert seen == 9  # verify, callback, promise, note, get_account, close and the three transfers


# --------------------------------------------------------------------------------------------------------------------
# Prompts
# --------------------------------------------------------------------------------------------------------------------


def test_prompts_carry_the_brief_values_from_the_ir(compiled_a: CompiledWorkflow, compiled_ab: CompiledWorkflow) -> None:
    system = compiled_a.workflow["system_prompt"]
    assert "Freya Bank's digital assistant" in system and "I am Freya Bank's digital assistant." in system
    assert "You can verify this call by phoning Freya Bank's customer service number." in system
    assert '"collections"' in system and '"car loan"' in system and "[A-33]" in system
    negotiate = compiled_a.workflow["nodes"]["negotiate"]["task_message"]
    for needle in ("{{account_facts}}", "{{date_table}}", '"Can you make this payment today?"', '"Do you confirm?"',
                   "You will receive a text message confirming this promise.", "That amount cannot be recorded.",
                   "You can change it in the mobile app.", "transition_to_end_flat_refusal",
                   "transition_to_handoff_support", "transition_to_guard_restructuring",
                   "transition_to_end_already_paid", "transition_to_end_dispute", "transition_to_end_hardship",
                   "[A-32a"):
        assert needle in negotiate, needle
    # the restructuring exit is guarded (hardship precedence): the engine names the tool after the node the transition
    # leads to, so the prompt must not send the model to a tool that does not exist
    assert "transition_to_handoff_restructuring" not in negotiate
    assert "SEVERAL PRODUCTS" not in negotiate and "add_note" not in negotiate
    multi = compiled_ab.workflow["nodes"]["negotiate"]["task_message"]
    assert "SEVERAL PRODUCTS" in multi and 'asked: "Can you pay all of them today?"' in multi
    assert 'reason "paid"' in multi and "add_note" in multi
    assert len(negotiate) < 6000


def test_no_prompt_leaves_an_unrendered_marker(compiled_a: CompiledWorkflow, compiled_ab: CompiledWorkflow) -> None:
    for compiled in (compiled_a, compiled_ab):
        texts = [compiled.workflow["system_prompt"]]
        texts += [n.get("task_message", "") for n in compiled.workflow["nodes"].values()]
        for text in texts:
            assert "<<" not in text and "<%" not in text
            assert not re.search(r"[.?!\"]- ", text), text[:200]


def test_identity_prompt_lists_the_questions_in_order(compiled_a: CompiledWorkflow) -> None:
    task = compiled_a.workflow["nodes"]["identity"]["task_message"]
    assert task.index("Could you please tell me your date of birth?") < task.index("last four digits")
    assert "field date_of_birth:" in task and "field phone_last4: Exactly four digits." in task
    assert "Three wrong answers in total" in task and "These questions protect your account." in task


def test_prose_is_appended_and_cleaned(api: ToolApi) -> None:
    prose = NodeProse(system_prompt_extra="Be kind. {{bogus}} {{title}}",
                      per_node={"negotiate": "Mention the weather. {{nobody}}", "opening": "Greet warmly."})
    workflow = compile_workflow(IR_A, api, LEDGER_A, prose).workflow
    assert workflow["system_prompt"].endswith("Be kind. <bogus> {{title}}")
    assert workflow["nodes"]["negotiate"]["task_message"].endswith("Mention the weather. <nobody>")
    assert workflow["nodes"]["opening"]["task_message"].endswith("Greet warmly.")
    assert lint_workflow(workflow, api, set(IR_A.call_variable_names())).ok


# --------------------------------------------------------------------------------------------------------------------
# Execute every generated snippet against the practice cases
# --------------------------------------------------------------------------------------------------------------------


def _run(workflow: dict[str, Any], node: str, state: dict[str, Any]) -> dict[str, Any]:
    return run_snippet(extract_code_snippets(workflow)[node], state)


# (end node to run, extra variables) per case - what the engine would have recorded on the way
FLOWS: dict[str, tuple[str | None, dict[str, Any]]] = {
    "PUB-01": ("end_promise_recorded", {"create_promise_success_count": 1}),
    "PUB-02": ("end_callback_booked", {"schedule_callback_success_count": 1}),
    "PUB-03": ("end_identity_failed", {"verify_identity_success_count": 3}),
    "PUB-04": ("end_promise_recorded", {"create_promise_success_count": 1}),
    "PUB-05": ("end_hardship", {}),
    "PUB-06": ("end_promise_recorded", {"create_promise_success_count": 1}),
    "PUB-07": ("end_promise_recorded", {"create_promise_success_count": 1}),
    "PUB-08": ("end_already_paid", {}),
    "PUB-09": (None, {"handoff_restructuring_success_count": 1}),
    "PUB-10": ("end_promise_recorded", {"create_promise_success_count": 1, "create_promise_failure_count": 1}),
    "PUB-11": ("end_promise_recorded", {"create_promise_success_count": 3}),
    "PUB-12": ("end_promise_recorded", {"create_promise_success_count": 1, "notes_log": "p2:refused;p3:refused;"}),
    "PUB-13": ("end_promise_recorded", {"create_promise_success_count": 2}),
}
UNVERIFIED = {"PUB-02", "PUB-03"}


@pytest.mark.parametrize("case_id", sorted(CASES))
def test_practice_case_through_the_generated_code_nodes(case_id: str, api: ToolApi) -> None:
    case = CASES[case_id]
    ir, ledger = BRIEFS[case["brief"]]
    workflow = compile_workflow(ir, api, ledger).workflow
    calendar = BusinessCalendar.from_ir(ir.calendar)

    state = _run(workflow, "setup", {**case["variables"], "customer_id": "c_x"})
    assert state["setup_state"] == "ok"
    assert state["deadline_early"] == "2026-11-02" and state["deadline_late"] == "2026-10-23"

    if case_id not in UNVERIFIED:
        products = [{"product_id": f"p{i + 1}", **p} for i, p in enumerate(case["products"])]
        state = _run(workflow, "account_facts", {**state, "products": products})
        assert state["account_state"] == "ok" and state["product_count"] == str(len(products))
        for index, product in enumerate(case["products"]):
            pid = f"p{index + 1}"
            band = ir.band_for(product["days_late"])
            assert band is not None
            assert state[f"{pid}_deadline"] == deadline_for(band.deadline, TODAY, calendar).isoformat()
            floor_field = ir.amounts.floor_field(product["type"])
            assert state[f"{pid}_floor"] == f"{product[floor_field]:.2f}"
            assert f"{product['past_due']:.2f}" in state["debt_statement"]
            assert f"{product['days_late']} days late" in state["debt_statement"]
            assert state["account_facts"].count(f"{pid}: ") >= 1
        assert state["debt_statement"].startswith("Thank you. I am calling from the collections team")
        assert state["debt_statement"].endswith(
            "Can you make this payment today?" if len(products) == 1 else "Can you pay all of them today?")

    end_node, counters = FLOWS[case_id]
    state = {**state, **counters}
    if case_id not in UNVERIFIED:
        state["identity_verified"] = "yes"
    if end_node is not None:
        state = _run(workflow, end_node, state)
    state = _run(workflow, "decide", state)
    expected = case["expected"]
    assert state["final_outcome"] == expected["outcome"], case_id
    assert state.get("final_reason") == expected["reason"], case_id
    if case["brief"] == "B" and expected["notes"]:
        assert state["notes_log"] == "p2:refused;p3:refused;"


def _run_plain(code: str, state: dict[str, Any]) -> dict[str, Any]:
    """Run the HAND-WRITTEN reference's code with ordinary builtins (it calls ``strftime``, which the test sandbox blocks)."""
    working = json.loads(json.dumps(state))
    exec(compile(code, "<reference>", "exec"), {"vars": working})
    return working


def _sentences(statement: str) -> list[str]:
    """The ``Your ...`` product sentences of a debt statement, currency words removed."""
    parts = re.split(r"(?<=[.?])\s+", statement.replace(" dollars", ""))
    return [part.strip() for part in parts if part.startswith("Your ")]


@pytest.mark.parametrize("case_id", ["PUB-01", "PUB-04", "PUB-06", "PUB-11", "PUB-13"])
def test_code_nodes_write_what_the_reference_code_nodes_write(case_id: str, api: ToolApi) -> None:
    """Same code-node variables with the same values as the hand-written reference (the compiler only adds variables)."""
    case = CASES[case_id]
    ir, ledger = BRIEFS[case["brief"]]
    mine = extract_code_snippets(compile_workflow(ir, api, ledger).workflow)
    theirs = extract_code_snippets(_reference(multi=case["brief"] == "B"))
    start = {**case["variables"], "customer_id": "c_x"}

    ref_setup, my_setup = _run_plain(theirs["setup"], start), run_snippet(mine["setup"], start)
    assert set(ref_setup) <= set(my_setup)
    for name in ("today_iso", "today_spoken", "now_time", "attempt1_date", "attempt1_spoken", "deadline_early",
                 "deadline_late", "setup_state"):
        assert my_setup[name] == ref_setup[name], name
    ref_table, my_table = ref_setup["date_table"].splitlines(), my_setup["date_table"].splitlines()
    assert my_table[:len(ref_table)] == ref_table, "the date table, including the non-business-day hints"
    for needle in ("2026-10-14", "2026-10-15", "10:00", "19:00", "later than 11:00"):
        assert needle in ref_setup["callback_facts"] and needle in my_setup["callback_facts"], needle

    products = [{"product_id": f"p{i + 1}", **p} for i, p in enumerate(case["products"])]
    ref_facts = _run_plain(theirs["account_facts"], {**ref_setup, "products": products})
    my_facts = run_snippet(mine["account_facts"], {**my_setup, "products": products})
    assert set(ref_facts) <= set(my_facts)
    for name in ref_facts:
        if name not in ("account_facts", "debt_statement") and name not in ref_setup:
            assert my_facts[name] == ref_facts[name], name
    assert _sentences(my_facts["debt_statement"]) == _sentences(ref_facts["debt_statement"])
    ref_rows = ref_facts["date_table"].splitlines()
    assert my_facts["date_table"].splitlines()[:len(ref_rows)] == ref_rows, "the date table incl. the marks after a deadline"
    assert any("deadline" in row for row in ref_rows), "the fixture must exercise the deadline marks"
    my_blocks = re.split(r"\n(?=p\d+: )", my_facts["account_facts"])
    ref_blocks = re.split(r"\n(?=p\d+: )", ref_facts["account_facts"])
    assert len(my_blocks) == len(ref_blocks) == len(products) + 1  # the first part is the heading line
    for mine_block, theirs_block in zip(my_blocks[1:], ref_blocks[1:], strict=True):
        for marker in ("Attempt 1 - ask:", "Attempt 2 - ask:", "After attempt", "Floor offer - when the customer says",
                       "Allowed promise dates:", "EXACTLY"):
            assert (marker in mine_block) == (marker in theirs_block), (case_id, marker)
        assert re.findall(r"EXACTLY \d", mine_block) == re.findall(r"EXACTLY \d", theirs_block)
        assert mine_block.split("Allowed promise dates:")[1] == theirs_block.split("Allowed promise dates:")[1]


def test_multi_product_deadlines_differ_per_product(api: ToolApi) -> None:
    case = CASES["PUB-13"]  # one date for both products, but the deadlines differ
    workflow = compile_workflow(IR_AB, api, LEDGER_AB).workflow
    state = _run(workflow, "setup", {**case["variables"], "customer_id": "c_x"})
    products = [{"product_id": f"p{i + 1}", **p} for i, p in enumerate(case["products"])]
    state = _run(workflow, "account_facts", {**state, "products": products})
    assert (state["p1_deadline"], state["p2_deadline"]) == ("2026-11-02", "2026-10-23")
    assert state["p1_band"] == "early" and state["p2_band"] == "late"
    assert "EXACTLY 1 (early band)" in state["account_facts"] and "EXACTLY 2 (late band)" in state["account_facts"]


# --------------------------------------------------------------------------------------------------------------------
# Generality
# --------------------------------------------------------------------------------------------------------------------


def test_a_different_policy_gives_a_different_agent(api: ToolApi) -> None:
    ir = IR_A.model_copy(deep=True)
    ir.bank_name = "Northwind Credit"
    ir.currency_word = "euros"
    ir.calendar.holidays = [TODAY.replace(day=15), TODAY.replace(day=19)]
    ir.bands[0].deadline = ir.bands[0].deadline.model_copy(update={"n": 3})
    ir.identity.questions = list(reversed(ir.identity.questions))
    ir.identity.max_wrong_total = 2
    ir.phrases.today_question = "Would you settle this today?"
    workflow = compile_workflow(ir, api, LEDGER_A).workflow
    assert "Northwind Credit's digital assistant" in workflow["system_prompt"]
    assert "euros" in workflow["system_prompt"]
    assert "last four digits" in workflow["nodes"]["identity"]["on_enter"]["prompt"]
    assert "Two wrong answers in total" in workflow["nodes"]["identity"]["task_message"]
    state = _run(workflow, "setup", {"now": NOW})
    assert state["deadline_early"] == "2026-10-21"  # 15 and 19 October are holidays: 16th, 20th, 21st are the 1st-3rd
    assert "BANK HOLIDAY" in state["date_table"]


def test_a_missing_tool_is_a_compile_error(api: ToolApi) -> None:
    broken = api.model_copy(update={"tools": {k: v for k, v in api.tools.items() if k != "get_account"}})
    with pytest.raises(CompileError, match="get_account"):
        compile_workflow(IR_A, broken, LEDGER_A)
    no_notes = api.model_copy(update={"tools": {k: v for k, v in api.tools.items() if k != "add_note"}})
    with pytest.raises(CompileError, match="add_note"):
        compile_workflow(IR_AB, no_notes, LEDGER_AB)
    no_transfer = api.model_copy(update={"tools": {k: v for k, v in api.tools.items() if k != "transfer_to_agent"}})
    with pytest.raises(CompileError, match="transfer_to_agent"):
        compile_workflow(IR_A, no_transfer, LEDGER_A)


def test_unknown_profile_and_missing_identity_questions(api: ToolApi) -> None:
    with pytest.raises(CompileError, match="unknown profile"):
        compile_workflow(IR_A, api, LEDGER_A, None, "turbo")  # type: ignore[arg-type]
    ir = IR_A.model_copy(deep=True)
    ir.identity.questions = []
    with pytest.raises(CompileError, match="identity question"):
        compile_workflow(ir, api, LEDGER_A)


def test_the_compiler_sources_hold_no_brief_specific_values() -> None:
    forbidden = [r"2026-\d\d-\d\d", r"Freya Bank", r"credit card", r"personal loan", r"car loan", r"minimum_payment",
                 r"instalment_amount", r"\bno_promise\b", r"\bnot_verified\b", r"\bnot_reached\b", r"date_of_birth",
                 r"phone_last4", r"Daniel", r"Reyes", r"collections team", r"mobile app", r"text message"]
    roots = [REPO / "builder" / "compiler", REPO / "builder" / "checks", REPO / "builder" / "prompts" / "agent"]
    offenders: list[str] = []
    for root in roots:
        for path in root.rglob("*"):
            if path.suffix not in (".py", ".j2"):
                continue
            text = path.read_text(encoding="utf-8")
            for pattern in forbidden:
                for match in re.finditer(pattern, text):
                    offenders.append(f"{path.relative_to(REPO)}: {match.group(0)}")
    assert offenders == []


def test_missing_call_variables_are_reported_as_notes(api: ToolApi) -> None:
    ir = IR_A.model_copy(deep=True)
    ir.call_variables = [v for v in ir.call_variables if v.name not in ("now", "customer_id")]
    compiled = compile_workflow(ir, api, LEDGER_A)
    assert any("'now'" in note for note in compiled.notes) and any("'customer_id'" in note for note in compiled.notes)
    state = _run(compiled.workflow, "setup", {})
    assert state["setup_state"] == "now_missing" and state["date_table"]
