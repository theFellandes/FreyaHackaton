"""The hidden briefs keep the seven tool names but may rename fields, flags and allowed values.

Every test renames something in a copy of the kit's tool API and compiles the Brief A / Brief A+B fixture policies against
it. The compiled agent must follow the rename (the right fields are sent, the right flags are read), lint clean, or
fail loudly with a ``CompileError`` - it must never compile into an agent that silently drops a field or misreads an
answer (audit findings C-01 .. C-05 and C-08).

Expectations are literal values (field names, hook messages), not comparisons between two generated artefacts.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from builder.checks.code_sandbox import extract_code_snippets, run_hook, run_snippet
from builder.checks.lint import lint_workflow
from builder.compiler.compile import compile_workflow
from builder.compiler.errors import CompileError
from builder.ingest.tool_api import FieldSpec, ToolApi, load_tool_api
from builder.policy.ir import PolicyIR, RuleLedger

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "fixtures" / "policy"
NOW = "2026-10-14T11:00"


def _ir(name: str) -> PolicyIR:
    return PolicyIR.model_validate(json.loads((FIXTURES / f"{name}_ir.json").read_text(encoding="utf-8")))


def _ledger(name: str) -> RuleLedger:
    return RuleLedger.model_validate(json.loads((FIXTURES / f"{name}_ledger.json").read_text(encoding="utf-8")))


IR_A, IR_AB = _ir("brief_a"), _ir("brief_ab")
LEDGER_A, LEDGER_AB = _ledger("brief_a"), _ledger("brief_ab")


@pytest.fixture(scope="module")
def kit() -> ToolApi:
    return load_tool_api(REPO / "tools" / "openapi.json")


# ---- helpers ------------------------------------------------------------------------------------------------------------


def rename_request(api: ToolApi, tool: str, old: str, new: str) -> None:
    for spec in api.tools[tool].request_fields:
        if spec.name == old:
            spec.name = new


def rename_response(api: ToolApi, tool: str, old: str, new: str) -> None:
    for spec in api.tools[tool].response_fields:
        if spec.name == old:
            spec.name = new
    api.tools[tool].response_description = api.tools[tool].response_description.replace(f"`{old}`", f"`{new}`")


def variant(api: ToolApi, *edits: Callable[[ToolApi], None]) -> ToolApi:
    copy = api.model_copy(deep=True)
    for edit in edits:
        edit(copy)
    return copy


def compile_ab(api: ToolApi, profile: str = "staged") -> dict[str, Any]:
    return compile_workflow(IR_AB, api, LEDGER_AB, None, profile).workflow  # type: ignore[arg-type]


def lint_errors(workflow: dict[str, Any], api: ToolApi, ir: PolicyIR = IR_AB) -> list[str]:
    return [str(f) for f in lint_workflow(workflow, api, set(ir.call_variable_names())).errors]


def tool_of(workflow: dict[str, Any], node: str, name: str) -> dict[str, Any]:
    return next(t for t in workflow["nodes"][node]["tools"] if t["function"]["name"] == name)


def body_defaults(workflow: dict[str, Any], node: str) -> dict[str, str]:
    action = workflow["nodes"][node]["pre_actions"][0]
    return {k: v["default"] for k, v in action["function"]["parameters"]["properties"]["body"]["properties"].items()}


def post(code: str, data: dict[str, Any], status: str = "success") -> str:
    out = run_hook(code, result={"status": status, "header": {}, "data": data})["result"]["data"]
    return out["what_to_do_next"]


# ---- C-01: the close_call fields are found by role ------------------------------------------------------------------------


def test_a_renamed_close_reason_field_is_still_sent(kit: ToolApi) -> None:
    api = variant(kit, lambda a: rename_request(a, "close_call", "reason", "reason_code"))
    workflow = compile_ab(api)
    assert body_defaults(workflow, "close") == {
        "customer_id": "{{customer_id}}", "outcome": "{{final_outcome}}", "reason_code": "{{final_reason}}"}
    assert lint_errors(workflow, api) == []


def test_a_renamed_close_outcome_field_is_still_sent(kit: ToolApi) -> None:
    api = variant(kit, lambda a: rename_request(a, "close_call", "outcome", "disposition"))
    compiled = compile_workflow(IR_AB, api, LEDGER_AB)
    assert body_defaults(compiled.workflow, "close") == {
        "customer_id": "{{customer_id}}", "disposition": "{{final_outcome}}", "reason": "{{final_reason}}"}
    assert lint_errors(compiled.workflow, api) == []
    assert compiled.notes == []


def test_the_lean_close_tool_offers_the_renamed_reason_field(kit: ToolApi) -> None:
    api = variant(kit, lambda a: rename_request(a, "close_call", "reason", "reason_code"))
    workflow = compile_ab(api, "lean")
    close = tool_of(workflow, "negotiate", "close_call")
    assert set(close["function"]["parameters"]["properties"]["body"]["properties"]) == {
        "customer_id", "outcome", "reason_code"}
    assert lint_errors(workflow, api) == []


def test_a_close_call_without_a_usable_outcome_field_is_a_compile_error(kit: ToolApi) -> None:
    def strip(api: ToolApi) -> None:
        api.tools["close_call"].request_fields = [s for s in api.tools["close_call"].request_fields
                                                  if s.name == "customer_id"]
    with pytest.raises(CompileError, match="close_call"):
        compile_ab(variant(kit, strip))


def test_a_close_field_the_compiler_cannot_fill_is_a_compile_error(kit: ToolApi) -> None:
    def add(api: ToolApi) -> None:
        api.tools["close_call"].request_fields.append(FieldSpec(name="agent_badge", type="string", required=True))
    with pytest.raises(CompileError, match="agent_badge"):
        compile_ab(variant(kit, add))


# ---- C-02: the success flag is found in the response contract --------------------------------------------------------------


def test_a_renamed_success_flag_is_read_by_the_create_promise_hook(kit: ToolApi) -> None:
    api = variant(kit, lambda a: rename_response(a, "create_promise", "ok", "success"))
    workflow = compile_ab(api)
    hook = tool_of(workflow, "negotiate", "create_promise")["post_process_code"]
    assert "IS recorded" in post(hook, {"success": True, "promise_id": "pr1"})
    assert "NOT recorded" in post(hook, {"success": False})
    assert lint_errors(workflow, api) == []


@pytest.mark.parametrize("profile", ["staged", "lean"])
def test_a_renamed_success_flag_is_read_by_every_other_hook(kit: ToolApi, profile: str) -> None:
    def edit(api: ToolApi) -> None:
        for tool in ("add_note", "schedule_callback", "transfer_to_agent", "close_call"):
            rename_response(api, tool, "ok", "success")
    api = variant(kit, edit)
    workflow = compile_ab(api, profile)
    note = tool_of(workflow, "negotiate", "add_note")["post_process_code"]
    assert "note is saved" in post(note, {"success": True}) and "NOT saved" in post(note, {"success": False})
    node = "third_party" if profile == "staged" else "call"
    callback = tool_of(workflow, node, "schedule_callback")["post_process_code"]
    assert "callback is booked" in post(callback, {"success": True})
    assert "NOT booked" in post(callback, {"success": False})
    if profile == "lean":
        close = tool_of(workflow, "negotiate", "close_call")["post_process_code"]
        assert "call is closed" in post(close, {"success": True})
        transfer = tool_of(workflow, "negotiate", "transfer_to_agent")["post_process_code"]
        assert "transfer is done" in post(transfer, {"success": True})
    assert lint_errors(workflow, api) == []


def test_a_renamed_success_flag_keeps_the_unreadable_answer_rule_of_the_identity_hook(kit: ToolApi) -> None:
    api = variant(kit, lambda a: rename_response(a, "verify_identity", "ok", "success"))
    hook = tool_of(compile_ab(api), "identity", "verify_identity")["post_process_code"]
    assert "could not be read" in post(hook, {"success": False})
    assert "does not count as a wrong answer" in post(hook, {"success": False})


def test_a_contract_without_a_success_flag_falls_back_to_the_envelope_status(kit: ToolApi) -> None:
    def edit(api: ToolApi) -> None:
        api.tools["create_promise"].response_fields = [s for s in api.tools["create_promise"].response_fields
                                                       if s.type != "boolean"]
        api.tools["create_promise"].response_description = ""
    api = variant(kit, edit)
    hook = tool_of(compile_ab(api), "negotiate", "create_promise")["post_process_code"]
    assert "IS recorded" in post(hook, {"promise_id": "pr1"}, status="success")
    assert "NOT recorded" in post(hook, {"promise_id": "pr1"}, status="error")


@pytest.mark.parametrize("profile", ["staged", "lean"])
@pytest.mark.parametrize("separator", [". ", "; ", " and "])
def test_an_audit_boolean_never_acknowledges_the_promise(kit: ToolApi, profile: str, separator: str) -> None:
    def edit(api: ToolApi) -> None:
        promise = api.tools["create_promise"]
        promise.response_fields.insert(0, FieldSpec(name="audit_logged", type="boolean",
                                                   description="Whether an audit event was saved."))
        promise.response_description = ("`audit_logged` reports audit storage" + separator
                                        + "`ok` is false when the action was not carried out.")
    api = variant(kit, edit)
    workflow = compile_ab(api, profile)
    hook = tool_of(workflow, "negotiate", "create_promise")["post_process_code"]
    assert "NOT recorded" in post(hook, {"audit_logged": True, "ok": False})
    assert "IS recorded" in post(hook, {"audit_logged": False, "ok": True})
    assert lint_errors(workflow, api) == []


def test_an_opaque_success_name_uses_its_documented_action_meaning(kit: ToolApi) -> None:
    def edit(api: ToolApi) -> None:
        rename_response(api, "create_promise", "ok", "receipt")
        promise = api.tools["create_promise"]
        promise.response_description = ""
        promise.field("receipt", "response").description = "True when the promise was recorded."
        promise.response_fields.insert(0, FieldSpec(name="metadata_ready", type="boolean",
                                                   description="Whether audit metadata was successfully stored."))
    api = variant(kit, edit)
    workflow = compile_ab(api)
    hook = tool_of(workflow, "negotiate", "create_promise")["post_process_code"]
    assert "NOT recorded" in post(hook, {"metadata_ready": True, "receipt": False})
    assert "IS recorded" in post(hook, {"metadata_ready": False, "receipt": True})


def test_two_documented_action_acknowledgements_are_not_selected_by_order(kit: ToolApi) -> None:
    def edit(api: ToolApi) -> None:
        api.tools["create_promise"].response_fields.append(
            FieldSpec(name="receipt", type="boolean", description="True when the operation succeeded."))
    with pytest.raises(CompileError, match="ambiguous action-success"):
        compile_ab(variant(kit, edit))


def test_documented_success_does_not_silently_override_another_supported_flag(kit: ToolApi) -> None:
    def edit(api: ToolApi) -> None:
        promise = api.tools["create_promise"]
        promise.response_description = ""
        promise.response_fields.append(FieldSpec(
            name="receipt", type="boolean", description="True when the operation succeeded."))
    with pytest.raises(CompileError, match="ambiguous action-success"):
        compile_ab(variant(kit, edit))


def test_an_unexplained_boolean_does_not_fall_back_to_http_success(kit: ToolApi) -> None:
    def edit(api: ToolApi) -> None:
        promise = api.tools["create_promise"]
        promise.response_fields = [FieldSpec(name="receipt", type="boolean")]
        promise.response_description = ""
    with pytest.raises(CompileError, match="action-success semantics"):
        compile_ab(variant(kit, edit))


@pytest.mark.parametrize("description", [
    "True when the action was not performed.",
    "When true, the action did not succeed.",
    "True means the promise was not recorded.",
    "False when the action was performed.",
])
def test_an_inverted_action_flag_is_not_used_as_a_positive_acknowledgement(
    kit: ToolApi, description: str,
) -> None:
    def edit(api: ToolApi) -> None:
        api.tools["create_promise"].field("ok", "response").description = description
    with pytest.raises(CompileError, match="inverted action-success"):
        compile_ab(variant(kit, edit))


# ---- C-03: the get_account item keys and the customer / queue fields -------------------------------------------------------


def _facts(workflow: dict[str, Any], products: list[dict[str, Any]]) -> dict[str, Any]:
    snippets = extract_code_snippets(workflow)
    state = run_snippet(snippets["setup"], {"now": NOW})
    return run_snippet(snippets["account_facts"], {**state, "products": products})


def test_renamed_account_item_keys_are_followed(kit: ToolApi) -> None:
    def edit(api: ToolApi) -> None:
        products = next(s for s in api.tools["get_account"].response_fields if s.name == "products")
        for item in products.items or []:
            if item.name == "product_id":
                item.name = "id"
            elif item.name == "type":
                item.name = "product_type"
    api = variant(kit, edit)
    workflow = compile_workflow(IR_A, api, LEDGER_A).workflow
    state = _facts(workflow, [{"id": "AC-9", "product_type": "credit_card", "card_last4": "7740", "past_due": 365.0,
                               "days_late": 8, "minimum_payment": 109.5}])
    assert state["product_ids"] == "AC-9"
    assert state["AC-9_name"] == "credit card ending 7740" and state["AC-9_floor"] == "109.50"
    assert "p1" not in state["account_facts"]
    assert lint_errors(workflow, api, IR_A) == []


def test_a_product_without_an_id_is_never_given_an_invented_one(kit: ToolApi) -> None:
    workflow = compile_workflow(IR_A, kit, LEDGER_A).workflow
    state = _facts(workflow, [{"type": "credit_card", "card_last4": "7740", "past_due": 365.0, "days_late": 8,
                               "minimum_payment": 109.5}])
    assert state["account_state"].startswith("error") and state["product_ids"] == ""
    assert not [key for key in state if key.startswith(("p1", "allowed_p1"))]


def test_an_account_without_any_id_field_is_a_compile_error(kit: ToolApi) -> None:
    def edit(api: ToolApi) -> None:
        products = next(s for s in api.tools["get_account"].response_fields if s.name == "products")
        products.items = [i for i in products.items or [] if i.name != "product_id"]
        for spec in api.tools["create_promise"].request_fields:
            if spec.name == "product_id":
                spec.name = "reference_text"
    with pytest.raises(CompileError, match="get_account"):
        compile_workflow(IR_A, variant(kit, edit), LEDGER_A)


def test_a_renamed_customer_field_is_baked_on_every_tool(kit: ToolApi) -> None:
    def edit(api: ToolApi) -> None:
        for name in api.tools:
            rename_request(api, name, "customer_id", "caller_ref")
    api = variant(kit, edit)
    for profile in ("staged", "lean"):
        compiled = compile_workflow(IR_AB, api, LEDGER_AB, None, profile)  # type: ignore[arg-type]
        assert lint_errors(compiled.workflow, api) == [], profile
        assert compiled.notes == [], profile
    workflow = compile_ab(api)
    assert body_defaults(workflow, "close")["caller_ref"] == "{{customer_id}}"
    assert body_defaults(workflow, "load_account") == {"caller_ref": "{{customer_id}}"}
    assert tool_of(workflow, "negotiate", "create_promise")["function"]["parameters"]["properties"]["body"][
        "properties"]["caller_ref"]["default"] == "{{customer_id}}"


def test_a_renamed_queue_field_is_baked_in_the_handoff_nodes(kit: ToolApi) -> None:
    api = variant(kit, lambda a: rename_request(a, "transfer_to_agent", "queue", "team"))
    workflow = compile_ab(api)
    assert body_defaults(workflow, "handoff_restructuring") == {"customer_id": "{{customer_id}}",
                                                                "team": "restructuring"}
    assert lint_errors(workflow, api) == []
    lean = compile_ab(api, "lean")
    assert "team" in tool_of(lean, "negotiate", "transfer_to_agent")["function"]["parameters"]["properties"][
        "body"]["properties"]


# ---- C-04: the create_promise roles ---------------------------------------------------------------------------------------

PROMISE_FACTS = {
    "product_ids": "p1", "allowed_p1": "2026-10-14,2026-10-15", "p1_floor": "10.00", "p1_past_due": "50.00",
}


@pytest.mark.parametrize("new", ["product_code", "loan_id", "account_number", "item"])
def test_a_renamed_product_field_keeps_the_promise_guard(kit: ToolApi, new: str) -> None:
    api = variant(kit, lambda a: rename_request(a, "create_promise", "product_id", new))
    workflow = compile_ab(api)
    pre = tool_of(workflow, "negotiate", "create_promise")["pre_process_code"]
    assert "promise_guard" in pre
    blocked = run_hook(pre, vars=dict(PROMISE_FACTS), config={
        "url": "u", "body": {new: "p1", "amount": 20, "date": "2026-10-20"}})
    assert blocked["config"]["url"] == "" and "not an allowed promise date" in blocked["vars"]["promise_guard"]
    fine = run_hook(pre, vars=dict(PROMISE_FACTS), config={
        "url": "u", "body": {new: "1", "amount": 20, "date": "2026-10-15"}})
    assert fine["config"]["url"] == "u" and fine["config"]["body"][new] == "p1"
    assert lint_errors(workflow, api) == []


def test_an_extra_integer_field_is_neither_the_amount_nor_coerced_as_money(kit: ToolApi) -> None:
    def edit(api: ToolApi) -> None:
        api.tools["create_promise"].request_fields.insert(2, FieldSpec(name="instalments", type="integer"))
    api = variant(kit, edit)
    pre = tool_of(compile_ab(api), "negotiate", "create_promise")["pre_process_code"]
    out = run_hook(pre, vars=dict(PROMISE_FACTS), config={
        "url": "u", "body": {"product_id": "p1", "instalments": 3, "amount": "20", "date": "2026-10-15"}})
    assert out["config"]["body"]["instalments"] == 3 and out["config"]["body"]["amount"] == 20.0
    low = run_hook(pre, vars=dict(PROMISE_FACTS), config={
        "url": "u", "body": {"product_id": "p1", "instalments": 3, "amount": "5", "date": "2026-10-15"}})
    assert low["config"]["url"] == "" and "the amount 5.00" in low["vars"]["promise_guard"]


def test_a_promise_without_a_date_field_is_a_compile_error(kit: ToolApi) -> None:
    def edit(api: ToolApi) -> None:
        api.tools["create_promise"].request_fields = [s for s in api.tools["create_promise"].request_fields
                                                      if s.name != "date"]
    with pytest.raises(CompileError, match="create_promise"):
        compile_ab(variant(kit, edit))


# ---- C-05: the verify_identity response shape -----------------------------------------------------------------------------


def _identity_node(workflow: dict[str, Any]) -> dict[str, Any]:
    return workflow["nodes"]["identity"]


def _routes(workflow: dict[str, Any]) -> dict[str, list[str]]:
    routes: dict[str, list[str]] = {}
    for transition in _identity_node(workflow)["transitions"]:
        cond = transition["condition"]
        if transition.get("is_tool_call") or cond.get("operator") in ("matches_intent", "always"):
            continue
        routes[transition["target_node"]] = [c["variable"] + "=" + str(c["operand"]) for c in cond["conditions"]]
    return routes


def test_a_renamed_overall_flag_still_routes_deterministically(kit: ToolApi) -> None:
    def edit(api: ToolApi) -> None:
        rename_response(api, "verify_identity", "all_verified", "passed")
        api.tools["verify_identity"].field("passed", "response").description = (
            "True when all security answers are verified.")
    api = variant(kit, edit)
    workflow = compile_ab(api)
    assert _routes(workflow)["load_account"] == ["verify_identity_response.data.passed=true",
                                                 "verify_identity_response.data.passed=True"]
    hook = tool_of(workflow, "identity", "verify_identity")["post_process_code"]
    assert "verified" in post(hook, {"ok": True, "verified": True, "passed": True})
    assert "next security question" in post(hook, {"ok": True, "verified": True, "passed": False,
                                                   "wrong_answers_left": 3})
    assert lint_errors(workflow, api) == []


def test_a_renamed_per_answer_flag_never_turns_a_correct_answer_into_a_wrong_one(kit: ToolApi) -> None:
    api = variant(kit, lambda a: rename_response(a, "verify_identity", "verified", "is_correct"))
    workflow = compile_ab(api)
    hook = tool_of(workflow, "identity", "verify_identity")["post_process_code"]
    right = post(hook, {"ok": True, "is_correct": True, "all_verified": False, "wrong_answers_left": 3})
    assert "next security question" in right and "wrong" not in right
    assert post(hook, {"ok": True, "is_correct": False, "all_verified": False,
                       "wrong_answers_left": 2}).startswith("This answer is wrong")
    assert lint_errors(workflow, api) == []


def test_a_renamed_remaining_counter_still_ends_the_check(kit: ToolApi) -> None:
    api = variant(kit, lambda a: rename_response(a, "verify_identity", "wrong_answers_left", "tries_left"))
    workflow = compile_ab(api)
    assert _routes(workflow)["identity_failed"] == ["verify_identity_response.data.tries_left=0"]
    hook = tool_of(workflow, "identity", "verify_identity")["post_process_code"]
    assert "identity check has ended" in post(hook, {"ok": True, "verified": False, "tries_left": 0})
    decide = extract_code_snippets(workflow)["decide"]
    assert "verify_identity_response.data.tries_left" in decide


def test_a_counter_that_counts_up_ends_the_check_at_the_policy_limit(kit: ToolApi) -> None:
    api = variant(kit, lambda a: rename_response(a, "verify_identity", "wrong_answers_left", "strikes"))
    workflow = compile_ab(api)
    # AUD-50: "at the limit or past it" (two answers checked in one message can skip the exact limit)
    failed = next(t for t in _identity_node(workflow)["transitions"] if t["target_node"] == "identity_failed")
    assert failed["condition"]["conditions"] == [
        {"variable": "verify_identity_response.data.strikes", "operator": "greater_than", "operand": 2}]
    hook = tool_of(workflow, "identity", "verify_identity")["post_process_code"]
    assert post(hook, {"ok": True, "verified": False, "strikes": 1}).startswith("This answer is wrong")
    assert "identity check has ended" in post(hook, {"ok": True, "verified": False, "strikes": 3})
    assert "identity check has ended" in post(hook, {"ok": True, "verified": False, "strikes": 4})
    decide = extract_code_snippets(workflow)["decide"]
    failing = run_snippet(decide, {"chosen_end": "auto", "verify_identity_response.data.strikes": 3})
    assert (failing["final_outcome"], failing["final_reason"]) == ("not_verified", "failed")
    assert lint_errors(workflow, api) == []


def test_an_ambiguous_counter_is_left_to_the_model_instead_of_being_guessed(kit: ToolApi) -> None:
    api = variant(kit, lambda a: rename_response(a, "verify_identity", "wrong_answers_left", "counter"))
    workflow = compile_ab(api)
    assert "identity_failed" not in _routes(workflow)
    assert any(t["target_node"] == "end_identity_failed" and t.get("is_tool_call")
               for t in _identity_node(workflow)["transitions"])
    assert lint_errors(workflow, api) == []


def test_a_renamed_hint_field_is_read_by_the_hint_rules(kit: ToolApi) -> None:
    api = variant(kit, lambda a: rename_response(a, "verify_identity", "hint", "suggestion"))
    workflow = compile_ab(api)
    hook = tool_of(workflow, "identity", "verify_identity")["post_process_code"]
    text = post(hook, {"ok": False, "suggestion": "day_month_swapped"})
    assert "month as a word" in text and "does not count as a wrong answer" in text
    assert lint_errors(workflow, api) == []


def test_a_contract_without_hint_and_with_other_names_compiles_without_phantom_fields(kit: ToolApi) -> None:
    def edit(api: ToolApi) -> None:
        api.tools["verify_identity"].response_fields = [
            s for s in api.tools["verify_identity"].response_fields if s.name != "hint"]
    api = variant(kit, edit)
    assert IR_A.identity.hints  # the policy still names a hint, the contract no longer returns one
    workflow = compile_workflow(IR_A, api, LEDGER_A).workflow
    hook = tool_of(workflow, "identity", "verify_identity")["post_process_code"]
    assert "'hint'" not in hook
    assert "could not be read" in post(hook, {"ok": False})
    assert lint_errors(workflow, api, IR_A) == []


def test_a_verify_response_without_any_verified_flag_is_a_compile_error(kit: ToolApi) -> None:
    def edit(api: ToolApi) -> None:
        api.tools["verify_identity"].response_fields = [
            s for s in api.tools["verify_identity"].response_fields if s.type != "boolean" or s.name == "ok"]
    with pytest.raises(CompileError, match="verify_identity"):
        compile_ab(variant(kit, edit))


@pytest.mark.parametrize("auxiliary", ["is_mobile", "all_cached", "all_devices_verified"])
def test_unrelated_booleans_never_route_a_per_answer_contract_to_account(kit: ToolApi, auxiliary: str) -> None:
    def edit(api: ToolApi) -> None:
        verify = api.tools["verify_identity"]
        verify.response_fields = [s for s in verify.response_fields if s.name != "all_verified"]
        verify.response_fields.append(FieldSpec(name=auxiliary, type="boolean"))
    api = variant(kit, edit)
    workflow = compile_ab(api)
    # AUD-23: the only way to the account is the counted-questions variable, never the auxiliary boolean nor the model
    assert _routes(workflow)["load_account"] == ["identity_all_verified=yes"]
    assert not any(t["target_node"] == "load_account" and t.get("is_tool_call")
                   for t in _identity_node(workflow)["transitions"])
    hook = tool_of(workflow, "identity", "verify_identity")["post_process_code"]
    assert auxiliary not in hook
    wrong = post(hook, {"ok": True, "verified": False, auxiliary: True, "wrong_answers_left": 2})
    assert wrong.startswith("This answer is wrong") and "All security answers" not in wrong
    right = post(hook, {"ok": True, "verified": True, auxiliary: False, "wrong_answers_left": 3})
    assert right.startswith("This answer is correct") and "All security answers" not in right
    assert lint_errors(workflow, api) == []


def test_an_overall_only_contract_does_not_treat_metadata_as_an_answer_verdict(kit: ToolApi) -> None:
    def edit(api: ToolApi) -> None:
        verify = api.tools["verify_identity"]
        verify.response_fields = [s for s in verify.response_fields if s.name != "verified"]
        verify.response_fields.append(FieldSpec(name="is_mobile", type="boolean",
                                                description="The caller uses a mobile device."))
    api = variant(kit, edit)
    workflow = compile_ab(api)
    assert _routes(workflow)["load_account"] == ["verify_identity_response.data.all_verified=true",
                                                 "verify_identity_response.data.all_verified=True"]
    hook = tool_of(workflow, "identity", "verify_identity")["post_process_code"]
    text = post(hook, {"ok": True, "all_verified": False, "is_mobile": True, "wrong_answers_left": 2})
    assert "does not say whether this answer was right" in text and "This answer is correct" not in text


def test_opaque_identity_names_follow_separate_documented_scopes(kit: ToolApi) -> None:
    def edit(api: ToolApi) -> None:
        rename_response(api, "verify_identity", "verified", "answer_result")
        rename_response(api, "verify_identity", "all_verified", "check_result")
        verify = api.tools["verify_identity"]
        verify.response_description += (" `answer_result` is true when this answer is correct."
                                        " `check_result` is true when all security answers are verified.")
    api = variant(kit, edit)
    workflow = compile_ab(api)
    assert _routes(workflow)["load_account"] == ["verify_identity_response.data.check_result=true",
                                                 "verify_identity_response.data.check_result=True"]
    hook = tool_of(workflow, "identity", "verify_identity")["post_process_code"]
    assert post(hook, {"ok": True, "answer_result": False, "check_result": False}).startswith("This answer is wrong")
    assert "next security question" in post(hook, {"ok": True, "answer_result": True, "check_result": False})
    assert "All security answers are verified" in post(hook, {"ok": True, "answer_result": True, "check_result": True})
    assert lint_errors(workflow, api) == []


def test_metadata_alone_cannot_supply_an_identity_verdict(kit: ToolApi) -> None:
    def edit(api: ToolApi) -> None:
        verify = api.tools["verify_identity"]
        verify.response_fields = [s for s in verify.response_fields if s.name not in ("verified", "all_verified")]
        verify.response_fields.append(FieldSpec(name="is_mobile", type="boolean"))
    with pytest.raises(CompileError, match="verify_identity"):
        compile_ab(variant(kit, edit))


def test_ambiguous_overall_identity_verdicts_fail_compilation(kit: ToolApi) -> None:
    def edit(api: ToolApi) -> None:
        api.tools["verify_identity"].response_fields.append(FieldSpec(
            name="check_result", type="boolean", description="True when the entire identity is verified."))
    with pytest.raises(CompileError, match="ambiguous overall verification"):
        compile_ab(variant(kit, edit))


@pytest.mark.parametrize("description", [
    "False when all security answers are verified.",
    "When true, the entire identity is not verified.",
    "True means all answers are incorrect.",
])
def test_an_inverted_identity_verdict_is_not_used_for_account_access(kit: ToolApi, description: str) -> None:
    def edit(api: ToolApi) -> None:
        api.tools["verify_identity"].field("all_verified", "response").description = description
    with pytest.raises(CompileError, match="inverted verification"):
        compile_ab(variant(kit, edit))


# ---- C-08: ids and enum values keep the spelling of the data ---------------------------------------------------------------

UPPER_PRODUCTS = [
    {"product_id": "CC-4821", "type": "credit_card", "card_last4": "4821", "past_due": 200, "days_late": 5,
     "minimum_payment": 40},
    {"product_id": "LN-77", "type": "personal_loan", "past_due": 500, "days_late": 12, "instalment_amount": 120},
]


def test_upper_case_product_ids_pass_the_guard_and_keep_their_spelling(kit: ToolApi) -> None:
    workflow = compile_ab(kit)
    state = _facts(workflow, UPPER_PRODUCTS)
    assert state["product_ids"] == "CC-4821,LN-77"
    pre = tool_of(workflow, "negotiate", "create_promise")["pre_process_code"]
    for sent in ("CC-4821", "cc-4821", " cc 4821 "):
        out = run_hook(pre, vars=state, config={"url": "u", "body": {
            "product_id": sent, "amount": 200, "date": "2026-10-15"}})
        assert out["config"]["url"] == "u" and out["config"]["body"]["product_id"] == "CC-4821", sent
        assert out["vars"]["promise_guard"] == "ok", sent
    unknown = run_hook(pre, vars=state, config={"url": "u", "body": {
        "product_id": "ZZ-1", "amount": 200, "date": "2026-10-15"}})
    assert unknown["config"]["url"] == "" and "unknown product_id ZZ-1; use one of CC-4821,LN-77" in unknown[
        "vars"]["promise_guard"]


def test_notes_are_logged_under_the_canonical_id_and_the_paid_rule_sees_them(kit: ToolApi) -> None:
    workflow = compile_ab(kit)
    state = _facts(workflow, UPPER_PRODUCTS)
    note = tool_of(workflow, "negotiate", "add_note")
    for sent in ("cc-4821", "LN-77"):
        state = run_hook(note["pre_process_code"], vars=state, config={
            "url": "u", "body": {"product_id": sent, "reason": "paid"}})["vars"]
        state = run_hook(note["post_process_code"], vars=state, result={"status": "success", "data": {"ok": True}})["vars"]
    assert state["notes_log"] == "CC-4821:paid;LN-77:paid;"
    state.update({"identity_verified": "yes", "chosen_end": "flat_refusal", "add_note_success_count": 2})
    done = run_snippet(extract_code_snippets(workflow)["decide"], state)
    assert (done["final_outcome"], done.get("final_reason")) == ("paid", None)


def test_the_decision_matches_notes_and_ids_whatever_their_case(kit: ToolApi) -> None:
    decide = extract_code_snippets(compile_ab(kit))["decide"]
    base = {"identity_verified": "yes", "chosen_end": "flat_refusal", "product_ids": "CC-4821,LN-77",
            "add_note_success_count": 2}
    out = run_snippet(decide, {**base, "notes_log": "cc-4821:Paid;ln-77:PAID;"})
    assert out["final_outcome"] == "paid"
    out = run_snippet(decide, {**base, "notes_log": "cc-4821:Paid;LN-77:Dispute;"})
    assert (out["final_outcome"], out["final_reason"]) == ("no_promise", "dispute")


def test_an_enum_value_is_sent_in_the_spelling_of_the_contract(kit: ToolApi) -> None:
    def edit(api: ToolApi) -> None:
        for spec in api.tools["add_note"].request_fields:
            if spec.name == "reason":
                spec.enum = ["Refused", "Hardship", "Dispute", "Paid", "Service_error"]
    api = variant(kit, edit)
    note = tool_of(compile_ab(api), "negotiate", "add_note")
    for sent, canonical in (("hardship", "Hardship"), ("Hardship", "Hardship"), (" service error ", "Service_error"),
                            ("something else", "something else")):
        out = run_hook(note["pre_process_code"], vars={"product_ids": "p1"}, config={"url": "u", "body": {
            "product_id": "p1", "reason": sent}})
        assert out["config"]["body"]["reason"] == canonical, sent
    saved = run_hook(note["post_process_code"], vars=out["vars"], result={"status": "success", "data": {"ok": True}})
    assert saved["vars"]["notes_log"] == "p1:something else;"


def test_a_value_that_is_not_in_the_enum_keeps_its_own_spelling_for_the_service_to_judge(kit: ToolApi) -> None:
    note = tool_of(compile_ab(kit), "negotiate", "add_note")["pre_process_code"]
    out = run_hook(note, vars={"product_ids": "p1,p2"}, config={"url": "u", "body": {
        "product_id": "P9", "reason": "Maybe"}})
    assert out["config"]["body"] == {"product_id": "P9", "reason": "Maybe"}


def test_the_lean_prompt_uses_the_field_names_of_the_contract(kit: ToolApi) -> None:
    def edit(api: ToolApi) -> None:
        rename_request(api, "close_call", "outcome", "disposition")
        rename_request(api, "close_call", "reason", "reason_code")
        rename_request(api, "transfer_to_agent", "queue", "team")
    api = variant(kit, edit)
    workflow = compile_ab(api, "lean")
    task = workflow["nodes"]["negotiate"]["task_message"]
    assert 'call close_call with disposition "promise"' in task
    assert 'call close_call with disposition "no_promise" and reason_code "refused"' in task
    assert ('call transfer_to_agent with team "support", then close_call with disposition "transferred" and '
            'reason_code "support"') in task
    assert "outcome " not in task.replace("the outcome", "")  # no stale field name
    assert lint_errors(workflow, api) == []


# ---- F04 / F17: the acknowledged records follow the explicit success role, never "any true boolean" -----------------------------


def _audit_variant(kit: ToolApi, tool: str, flag: str) -> ToolApi:
    def edit(api: ToolApi) -> None:
        contract = api.tools[tool]
        if flag != "ok":
            rename_response(api, tool, "ok", flag)
        contract.response_fields.insert(0, FieldSpec(name="audit_logged", type="boolean",
                                                    description="Whether an audit event was saved."))
        contract.response_description = (f"`audit_logged` reports audit storage. `{flag}` is false when the action was not "
                                         "carried out.")
    return variant(kit, edit)


@pytest.mark.parametrize("profile", ["staged", "lean"])
@pytest.mark.parametrize("flag", ["ok", "success"])
def test_the_acknowledged_promise_follows_the_success_role_not_an_audit_boolean(
    kit: ToolApi, profile: str, flag: str,
) -> None:
    api = _audit_variant(kit, "create_promise", flag)
    tool = tool_of(compile_ab(api, profile), "negotiate", "create_promise")
    state = {"product_ids": "p1,p2"}
    for data, acknowledged in (({"audit_logged": True, flag: False}, False), ({"audit_logged": False, flag: True}, True),
                               ({"audit_logged": True}, False), ({}, False)):
        pre = run_hook(tool["pre_process_code"], vars=dict(state), config={"url": "u", "body": {
            "customer_id": "c", "product_id": "p1", "amount": 1, "date": "2026-10-14"}})
        pre["vars"]["promise_pending_product"] = "p1"  # whatever the guard decided: the post-hook alone commits
        out = run_hook(tool["post_process_code"], vars=pre["vars"], result={"status": "success", "data": dict(data)})
        assert bool(out["vars"].get("promised_products")) is acknowledged, data


@pytest.mark.parametrize("profile", ["staged", "lean"])
@pytest.mark.parametrize("flag", ["ok", "success"])
def test_the_acknowledged_note_follows_the_success_role_not_an_audit_boolean(
    kit: ToolApi, profile: str, flag: str,
) -> None:
    api = _audit_variant(kit, "add_note", flag)
    tool = tool_of(compile_ab(api, profile), "negotiate", "add_note")
    for data, acknowledged in (({"audit_logged": True, flag: False}, False), ({"audit_logged": False, flag: True}, True),
                               ({"audit_logged": True}, False)):
        pre = run_hook(tool["pre_process_code"], vars={"product_ids": "p1"}, config={"url": "u", "body": {
            "customer_id": "c", "product_id": "p1", "reason": "paid"}})
        out = run_hook(tool["post_process_code"], vars=pre["vars"], result={"status": "success", "data": dict(data)})
        assert bool(out["vars"].get("notes_log")) is acknowledged, data
