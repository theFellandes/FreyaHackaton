"""More tool-contract variants a hidden brief may bring (audit round 3: AUD-04, AUD-16, AUD-21, AUD-22, AUD-25, AUD-48).

The seven tool names stay, but "the fields and the allowed values can change". Each test edits a copy of the kit's tool API
(generic names only) and compiles the Brief A / A+B fixture policies against it. The compiled agent must send the
right fields, read the right flags and lint clean - or fail loudly, never silently misread an answer.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from builder.checks.code_sandbox import run_hook
from builder.checks.lint import lint_workflow
from builder.compiler import roles
from builder.compiler.compile import compile_workflow
from builder.compiler.errors import CompileError
from builder.ingest.tool_api import FieldSpec, ToolApi, load_tool_api
from builder.policy.ir import PolicyIR, RuleLedger

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "fixtures" / "policy"


def _ir(name: str) -> PolicyIR:
    return PolicyIR.model_validate(json.loads((FIXTURES / f"{name}_ir.json").read_text(encoding="utf-8")))


def _ledger(name: str) -> RuleLedger:
    return RuleLedger.model_validate(json.loads((FIXTURES / f"{name}_ledger.json").read_text(encoding="utf-8")))


IR_A, IR_AB = _ir("brief_a"), _ir("brief_ab")
LEDGER_A, LEDGER_AB = _ledger("brief_a"), _ledger("brief_ab")
PROMISE_FACTS = {
    "product_ids": "p1", "allowed_p1": "2026-10-14,2026-10-15", "p1_floor": "10.00", "p1_past_due": "50.00",
}


@pytest.fixture(scope="module")
def kit() -> ToolApi:
    return load_tool_api(REPO / "tools" / "openapi.json")


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


def post(code: str, data: dict[str, Any], status: str = "success") -> str:
    out = run_hook(code, result={"status": status, "header": {}, "data": data})["result"]["data"]
    return out["what_to_do_next"]


# ---- AUD-04: ordinary rewordings of the success flag still compile and are read ----------------------------------------------

_ALL_TOOLS = ("verify_identity", "get_account", "create_promise", "add_note", "schedule_callback", "transfer_to_agent",
              "close_call")


def _describe_all(response: str | None = None, field: str | None = None, rename: str | None = None):
    def edit(api: ToolApi) -> None:
        for name in _ALL_TOOLS:
            contract = api.tools[name]
            flag = contract.field("ok", "response")
            if rename:
                flag.name = rename
            if response is not None:
                contract.response_description = response.replace("`ok`", f"`{flag.name}`")
            if field is not None:
                contract.response_description = ""
                flag.description = field
    return edit


REWORDINGS = [
    pytest.param(_describe_all(response="`ok` is false on error."), "ok", id="false-on-error"),
    pytest.param(_describe_all(response="`ok` is false when the request could not be processed."), "ok",
                 id="could-not-be-processed"),
    pytest.param(_describe_all(response="Returns `ok`: false if the call could not be completed."), "ok",
                 id="call-could-not-be-completed"),
    pytest.param(_describe_all(response="`ok` is true on success."), "ok", id="true-on-success"),
    pytest.param(_describe_all(response="`ok` is true if there were no errors."), "ok", id="true-if-no-errors"),
    pytest.param(_describe_all(response="`ok` is false if the operation failed."), "ok", id="false-if-operation-failed"),
    pytest.param(_describe_all(field="True if successful."), "ok", id="prop-true-if-successful"),
    pytest.param(_describe_all(field="Indicates success."), "ok", id="prop-indicates-success"),
    pytest.param(_describe_all(response="`ok` is true when the request completed.", rename="success"), "success",
                 id="success-request-completed"),
    pytest.param(_describe_all(response="", rename="recorded"), "recorded", id="renamed-recorded-no-prose"),
    pytest.param(_describe_all(response="", rename="is_saved"), "is_saved", id="renamed-is-saved-no-prose"),
]


@pytest.mark.parametrize(("edit", "flag"), REWORDINGS)
def test_a_reworded_success_flag_is_found_on_every_tool(kit: ToolApi, edit, flag: str) -> None:
    api = variant(kit, edit)
    for name in _ALL_TOOLS:
        if name == "verify_identity" and flag in ("recorded", "is_saved"):
            continue  # an identity response is judged by its verdicts; a renamed opaque flag is not needed there
        assert roles.success_flag(api.tools[name]) == flag, name


@pytest.mark.parametrize("profile", ["staged", "lean"])
@pytest.mark.parametrize(("edit", "flag"), REWORDINGS)
def test_a_reworded_success_flag_compiles_and_is_read_by_the_hooks(kit: ToolApi, edit, flag: str, profile: str) -> None:
    api = variant(kit, edit)
    workflow = compile_ab(api, profile)
    promise = tool_of(workflow, "negotiate", "create_promise")["post_process_code"]
    assert "IS recorded" in post(promise, {flag: True, "promise_id": "pr1"})
    assert "NOT recorded" in post(promise, {flag: False})
    note = tool_of(workflow, "negotiate", "add_note")["post_process_code"]
    assert "note is saved" in post(note, {flag: True}) and "NOT saved" in post(note, {flag: False})
    assert lint_errors(workflow, api) == []


def test_a_reworded_identity_success_flag_keeps_the_unreadable_answer_rule(kit: ToolApi) -> None:
    api = variant(kit, _describe_all(response="`ok` is false on error."))
    assert roles.verify_shape(api.tools["verify_identity"]).ok == "ok"
    hook = tool_of(compile_ab(api), "identity", "verify_identity")["post_process_code"]
    assert "could not be read" in post(hook, {"ok": False})


@pytest.mark.parametrize("description", ["`ok` is true on error.", "`ok` is false on success."])
def test_an_inverted_on_error_wording_is_still_an_inverted_flag(kit: ToolApi, description: str) -> None:
    api = variant(kit, _describe_all(response=description))
    with pytest.raises(CompileError, match="inverted action-success"):
        compile_ab(api)


@pytest.mark.parametrize("decoy", [
    FieldSpec(name="audit_logged", type="boolean"),
    FieldSpec(name="is_duplicate", type="boolean", description="True when a promise already existed."),
])
def test_a_decoy_boolean_never_replaces_the_reworded_flag(kit: ToolApi, decoy: FieldSpec) -> None:
    def edit(api: ToolApi) -> None:
        _describe_all(response="`ok` is false on error.")(api)
        api.tools["create_promise"].response_fields.insert(0, decoy.model_copy())
    api = variant(kit, edit)
    assert roles.success_flag(api.tools["create_promise"]) == "ok"


def test_an_undocumented_audit_boolean_alone_is_still_not_an_acknowledgement(kit: ToolApi) -> None:
    def edit(api: ToolApi) -> None:
        promise = api.tools["create_promise"]
        promise.response_fields = [FieldSpec(name="audit_logged", type="boolean")]
        promise.response_description = ""
    with pytest.raises(CompileError, match="action-success semantics"):
        compile_ab(variant(kit, edit))


def test_a_reworded_api_file_compiles_to_the_kit_workflow(kit: ToolApi, tmp_path: Path) -> None:
    """End to end on the deterministic fixture policy: same workflow as with the kit wording."""
    reworded = json.loads((REPO / "tools" / "openapi.json").read_text(encoding="utf-8"))
    for item in reworded["paths"].values():
        for operation in item.values():
            if isinstance(operation, dict) and "200" in operation.get("responses", {}):
                operation["responses"]["200"]["description"] = (
                    "`ok` is false when the request could not be processed.")
    path = tmp_path / "openapi.json"
    path.write_text(json.dumps(reworded), encoding="utf-8")
    api = load_tool_api(path)
    for profile in ("staged", "lean"):
        assert compile_ab(api, profile) == compile_ab(kit, profile)


# ---- AUD-16: a renamed transfer queue field keeps every handoff ---------------------------------------------------------------


def _rename_request(tool: str, old: str, new: str) -> Callable[[ToolApi], None]:
    def edit(api: ToolApi) -> None:
        for spec in api.tools[tool].request_fields:
            if spec.name == old:
                spec.name = new
    return edit


def _rekey(ir: PolicyIR, old: str, new: str) -> PolicyIR:
    data = json.loads(ir.model_dump_json())
    for intent in data["intents"]:
        for action in intent.get("actions", []):
            if old in (action.get("params") or {}):
                action["params"][new] = action["params"].pop(old)
    return PolicyIR.model_validate(data)


def _body(workflow: dict[str, Any], node: str) -> dict[str, Any]:
    action = workflow["nodes"][node]["pre_actions"][0]
    return {k: v["default"] for k, v in action["function"]["parameters"]["properties"]["body"]["properties"].items()}


@pytest.mark.parametrize("new", ["team", "destination", "group"])
def test_an_ir_keyed_by_the_renamed_queue_field_keeps_every_handoff(kit: ToolApi, new: str) -> None:
    from builder.understand.validate import errors_of, validate_policy

    api = variant(kit, _rename_request("transfer_to_agent", "queue", new))
    ir = _rekey(IR_AB, "queue", new)
    assert errors_of(validate_policy(ir, LEDGER_AB, api)) == []
    workflow = compile_workflow(ir, api, LEDGER_AB, None, "staged").workflow
    assert sorted(n for n in workflow["nodes"] if n.startswith("handoff_")) == [
        "handoff_general", "handoff_restructuring", "handoff_support"]
    assert _body(workflow, "handoff_restructuring") == {"customer_id": "{{customer_id}}", new: "restructuring"}
    assert lint_errors(workflow, api, ir) == []
    lean = compile_workflow(ir, api, LEDGER_AB, None, "lean").workflow
    assert f'call transfer_to_agent with {new} "restructuring"' in lean["nodes"]["negotiate"]["task_message"]


@pytest.mark.parametrize("new", ["team", "group"])
def test_an_ir_keyed_queue_is_rekeyed_to_the_api_field_instead_of_failing_validation(kit: ToolApi, new: str) -> None:
    from builder.understand.fixes import apply_safe_fixes
    from builder.understand.validate import errors_of, validate_policy

    api = variant(kit, _rename_request("transfer_to_agent", "queue", new))
    fixed, log = apply_safe_fixes(IR_AB, LEDGER_AB, api)
    assert [e for e in errors_of(validate_policy(fixed, LEDGER_AB, api)) if e.code == "ACTION_PARAM"] == []
    transfers = [a.params for i in fixed.intents for a in i.actions if a.tool == "transfer_to_agent"]
    assert transfers and all(set(p) == {new} for p in transfers)
    assert any(new in line for line in log)


def test_a_misspelled_queue_under_the_renamed_field_is_respelled(kit: ToolApi) -> None:
    from builder.understand.fixes import apply_safe_fixes

    api = variant(kit, _rename_request("transfer_to_agent", "queue", "team"))
    data = json.loads(_rekey(IR_AB, "queue", "team").model_dump_json())
    for intent in data["intents"]:
        for action in intent.get("actions", []):
            if action.get("tool") == "transfer_to_agent":
                action["params"]["team"] = action["params"]["team"].upper()
    fixed, _log = apply_safe_fixes(PolicyIR.model_validate(data), LEDGER_AB, api)
    assert fixed.intent("restructuring").transfer_queue() == "restructuring"


def test_the_queue_vocabulary_follows_the_renamed_field(kit: ToolApi) -> None:
    from builder.understand.validate import errors_of, tool_vocabulary, validate_policy

    api = variant(kit, _rename_request("transfer_to_agent", "queue", "team"))
    vocab = tool_vocabulary(api)
    assert vocab.queues == tool_vocabulary(kit).queues and vocab.queues
    assert "transfer_to_agent team values" in vocab.describe()
    assert "transfer_to_agent queue values" in tool_vocabulary(kit).describe()
    data = json.loads(_rekey(IR_AB, "queue", "team").model_dump_json())
    data["closing"]["precedence"] = ["transfer:nowhere", *data["closing"]["precedence"]]
    issues = errors_of(validate_policy(PolicyIR.model_validate(data), LEDGER_AB, api))
    assert any(e.code == "QUEUE_UNKNOWN" for e in issues)


def test_the_kit_ir_still_reads_its_queue() -> None:
    assert IR_AB.intent("restructuring").transfer_queue() == "restructuring"


# ---- AUD-25: a failed transfer is retried by the engine (the 500 says nothing was recorded) --------------------------------


def _retries(tool: dict[str, Any]) -> int:
    return int(tool["config"]["maxRetries"])


@pytest.mark.parametrize("ir, ledger", [(IR_A, LEDGER_A), (IR_AB, LEDGER_AB)])
def test_every_handoff_call_is_retried_once_and_writes_are_not(kit: ToolApi, ir: PolicyIR, ledger: RuleLedger) -> None:
    workflow = compile_workflow(ir, kit, ledger).workflow
    handoffs = [name for name in workflow["nodes"] if name.startswith("handoff_")]
    assert handoffs
    for name in handoffs:
        assert _retries(workflow["nodes"][name]["pre_actions"][0]) == 1, name
    assert _retries(workflow["nodes"]["load_account"]["pre_actions"][0]) == 2
    assert _retries(workflow["nodes"]["close"]["pre_actions"][0]) == 2
    assert _retries(tool_of(workflow, "negotiate", "create_promise")) == 0
    if ir.multi_product.enabled:
        assert _retries(tool_of(workflow, "negotiate", "add_note")) == 0
    assert _retries(tool_of(workflow, "third_party", "schedule_callback")) == 0


def test_the_lean_transfer_tool_is_retried_once(kit: ToolApi) -> None:
    workflow = compile_ab(kit, "lean")
    assert _retries(tool_of(workflow, "negotiate", "transfer_to_agent")) == 1


def test_decide_keeps_the_transfer_outcome_after_a_failed_then_retried_transfer() -> None:
    from builder.checks.code_sandbox import run_snippet
    from builder.compiler import codegen

    code = codegen.decide_outcome_snippet(IR_A)
    ok = {"identity_verified": "yes"}
    retried = run_snippet(code, {**ok, "handoff_support_failure_count": 1, "handoff_support_success_count": 1})
    assert (retried["final_outcome"], retried.get("final_reason")) == ("transferred", "support")
    # deliberately pinned: a transfer that finally failed still closes as transferred (closing as refused or as a
    # promise would be worse; the IR has no "transfer failed" outcome)
    failed = run_snippet(code, {**ok, "handoff_support_failure_count": 1})
    assert (failed["final_outcome"], failed.get("final_reason")) == ("transferred", "support")


# ---- AUD-22: a required request field whose value the contract fixes is sent, never dropped -------------------------------


def _kit_doc() -> dict[str, Any]:
    return json.loads((REPO / "tools" / "openapi.json").read_text(encoding="utf-8"))


def _with_required(tool: str, name: str, schema: dict[str, Any]) -> ToolApi:
    from builder.ingest.tool_api import parse_tool_api

    doc = _kit_doc()
    body = doc["paths"][f"/{tool}"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    if "$ref" in body:
        body = doc["components"]["schemas"][body["$ref"].rsplit("/", 1)[-1]]
    body["properties"][name] = schema
    body.setdefault("required", []).append(name)
    return parse_tool_api(doc)


@pytest.mark.parametrize(("schema", "fixed"), [
    ({"type": "string", "const": "EUR"}, "EUR"),
    ({"type": "string", "default": "phone"}, "phone"),
    ({"type": "string", "enum": ["phone"]}, "phone"),
    ({"type": "string", "enum": ["a", "b"]}, None),
    ({"type": "string"}, None),
])
def test_the_parser_keeps_the_value_a_contract_fixes(schema: dict[str, Any], fixed: str | None) -> None:
    api = _with_required("create_promise", "currency", schema)
    spec = api.tools["create_promise"].field("currency", "request")
    assert spec.fixed == fixed
    if "const" in schema:
        assert spec.enum == [schema["const"]]


def test_the_kit_contract_fixes_no_request_value(kit: ToolApi) -> None:
    assert [(t.name, s.name) for t in kit.tools.values() for s in t.request_fields if s.fixed is not None] == []


ENGINE_FIELDS = [
    pytest.param("get_account", "channel", {"type": "string", "enum": ["phone"]}, "phone", ["load_account"],
                 id="account-enum-of-one"),
    pytest.param("get_account", "channel", {"type": "string", "const": "phone"}, "phone", ["load_account"],
                 id="account-const"),
    pytest.param("get_account", "channel", {"type": "string", "default": "phone"}, "phone", ["load_account"],
                 id="account-default"),
    pytest.param("transfer_to_agent", "priority", {"type": "string", "enum": ["normal"]}, "normal",
                 ["handoff_general", "handoff_restructuring", "handoff_support"], id="transfer-enum-of-one"),
    pytest.param("transfer_to_agent", "priority", {"type": "string", "enum": ["normal", "urgent"]}, "normal",
                 ["handoff_general", "handoff_restructuring", "handoff_support"], id="transfer-enum-guess"),
    pytest.param("close_call", "channel", {"type": "string", "enum": ["phone"]}, "phone", ["close"],
                 id="close-enum-of-one"),
    pytest.param("get_account", "api_version", {"type": "integer", "const": 2}, 2, ["load_account"],
                 id="account-integer-const"),
]


@pytest.mark.parametrize(("tool", "name", "schema", "value", "nodes"), ENGINE_FIELDS)
def test_an_engine_call_sends_a_fixed_required_field_and_keeps_the_staged_profile(
    tool: str, name: str, schema: dict[str, Any], value: Any, nodes: list[str],
) -> None:
    api = _with_required(tool, name, schema)
    compiled = compile_workflow(IR_AB, api, LEDGER_AB, None, "staged")
    for node in nodes:
        assert _body(compiled.workflow, node)[name] == value, node
    assert lint_errors(compiled.workflow, api) == []
    if tool == "get_account":
        lean = compile_ab(api, "lean")
        assert _body(lean, "load_account")[name] == value
        assert lint_errors(lean, api) == []
    if "guess" in schema.get("description", "") or len(schema.get("enum") or []) > 1:
        assert any(name in note for note in compiled.notes)


def test_a_model_tool_never_asks_the_model_for_a_fixed_value() -> None:
    api = _with_required("create_promise", "currency", {"type": "string", "const": "EUR"})
    workflow = compile_ab(api)
    body = tool_of(workflow, "negotiate", "create_promise")["function"]["parameters"]["properties"]["body"]
    assert body["properties"]["currency"]["default"] == "EUR"
    assert "currency" not in body["required"]
    assert lint_errors(workflow, api) == []


def test_a_free_text_close_field_without_a_value_is_still_a_compile_error() -> None:
    api = _with_required("close_call", "agent_ref", {"type": "string"})
    with pytest.raises(CompileError, match="agent_ref"):
        compile_ab(api)


# ---- AUD-21: amounts the contract keeps in minor units (cents) ------------------------------------------------------------


def _minor_amount(name: str, description: str = "", type_: str = "integer") -> Callable[[ToolApi], None]:
    def edit(api: ToolApi) -> None:
        spec = api.tools["create_promise"].field("amount", "request")
        spec.name, spec.type, spec.description = name, type_, description
    return edit


def _guard(tool: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    pre = tool["pre_process_code"]
    return run_hook(pre, vars=dict(PROMISE_FACTS), config={"url": "u", "body": {"product_id": "p1",
                                                                                 "date": "2026-10-15", **body}})


@pytest.mark.parametrize(("name", "description"), [
    ("amount", "Amount in minor units (cents), e.g. 12050 for 120.50."),
    ("amount_cents", ""),
    ("amount_minor", ""),
])
def test_a_minor_unit_amount_is_sent_as_integer_cents(kit: ToolApi, name: str, description: str) -> None:
    api = variant(kit, _minor_amount(name, description))
    workflow = compile_ab(api)
    tool = tool_of(workflow, "negotiate", "create_promise")
    for said in ("20", "20.00", 20):
        out = _guard(tool, {name: said})
        assert out["config"]["url"] == "u" and out["config"]["body"][name] == 2000, said
        assert isinstance(out["config"]["body"][name], int)
    low = _guard(tool, {name: "5"})
    assert low["config"]["url"] == "" and "the amount 5.00" in low["vars"]["promise_guard"]
    leaf = tool["function"]["parameters"]["properties"]["body"]["properties"][name]
    assert leaf["type"] == "number" and "cents" not in leaf["description"].lower()
    assert "minor" not in leaf["description"].lower()
    assert lint_errors(workflow, api) == []


def test_an_integer_amount_without_a_minor_hint_stays_in_major_units(kit: ToolApi) -> None:
    api = variant(kit, _minor_amount("amount", "", "integer"))
    tool = tool_of(compile_ab(api), "negotiate", "create_promise")
    out = _guard(tool, {"amount": "20"})
    assert out["config"]["url"] == "u" and out["config"]["body"]["amount"] == 20.0


@pytest.mark.parametrize("name", ["minimum_payment", "minimum_amount"])
def test_a_minimum_field_is_not_a_minor_unit_field(name: str) -> None:
    assert not roles.minor_units(FieldSpec(name=name, type="integer", description="The minimum payment."))


def test_account_amounts_in_cents_are_read_as_cents(kit: ToolApi) -> None:
    from builder.checks.code_sandbox import extract_code_snippets, run_snippet

    def edit(api: ToolApi) -> None:
        products = next(s for s in api.tools["get_account"].response_fields if s.name == "products")
        for item in products.items or []:
            if item.name in ("past_due", "minimum_payment"):
                item.type, item.description = "integer", "Amount in cents."
    api = variant(kit, edit)
    workflow = compile_workflow(IR_A, api, LEDGER_A).workflow
    snippets = extract_code_snippets(workflow)
    state = run_snippet(snippets["setup"], {"now": "2026-10-14T11:00"})
    state = run_snippet(snippets["account_facts"], {**state, "products": [
        {"product_id": "AC-9", "type": "credit_card", "card_last4": "7740", "past_due": 36500, "days_late": 8,
         "minimum_payment": 10950}]})
    assert state["AC-9_past_due"] == "365.00" and state["AC-9_floor"] == "109.50"
    assert lint_errors(workflow, api, IR_A) == []


# ---- F03 / N-2: an exact product id always wins; a normalised match only when it is unique ----------------------------------

TWIN_FACTS = {
    "product_ids": "Ab-1,aB-1", "allowed_Ab-1": "2026-10-15", "allowed_aB-1": "2026-10-15",
    "Ab-1_floor": "10.00", "Ab-1_past_due": "50.00", "aB-1_floor": "100.00", "aB-1_past_due": "500.00",
}


@pytest.mark.parametrize(("sent", "expected"), [("aB-1", "aB-1"), ("Ab-1", "Ab-1"), ("AB-1", "AB-1"), ("ab 1", "ab1")])
def test_ids_that_differ_only_by_case_are_never_merged(kit: ToolApi, sent: str, expected: str) -> None:
    tool = tool_of(compile_ab(kit), "negotiate", "create_promise")
    out = run_hook(tool["pre_process_code"], vars=dict(TWIN_FACTS), config={"url": "u", "body": {
        "product_id": sent, "amount": 200, "date": "2026-10-15"}})
    assert out["config"]["body"]["product_id"] == expected
    if expected == "aB-1":  # its own facts (floor 100 .. 500), not the twin's (10 .. 50)
        assert out["vars"]["promise_guard"] == "ok"
    elif expected == "Ab-1":
        assert "the amount 200.00 is not allowed for Ab-1" in out["vars"]["promise_guard"]
    else:  # ambiguous: kept as said, the guard does not pick a twin
        assert out["config"]["url"] == "" and "unknown product_id" in out["vars"]["promise_guard"]


def test_a_unique_normalised_id_and_a_bare_number_still_complete(kit: ToolApi) -> None:
    tool = tool_of(compile_ab(kit), "negotiate", "create_promise")
    facts = {**PROMISE_FACTS, "product_ids": "CC-4821,LN-77", "allowed_CC-4821": "2026-10-15",
             "CC-4821_floor": "10.00", "CC-4821_past_due": "50.00"}
    for sent in ("cc 4821", "4821", "CC-4821"):
        out = run_hook(tool["pre_process_code"], vars=dict(facts), config={"url": "u", "body": {
            "product_id": sent, "amount": 20, "date": "2026-10-15"}})
        assert out["config"]["body"]["product_id"] == "CC-4821", sent


def test_the_staged_promise_check_and_decide_keep_exact_twin_ids(kit: ToolApi) -> None:
    from builder.checks.code_sandbox import extract_code_snippets, run_snippet
    from builder.compiler import codegen

    snippets = extract_code_snippets(compile_ab(kit))
    out = run_snippet(codegen.check_promise_snippet(IR_AB),{**TWIN_FACTS, "now": "2026-10-14T11:00", "chk_product_id": "Ab-1", "chk_amount": "200",
                              "chk_date": "2026-10-15"})
    assert out["promise_amount_check"] != "ok"  # 200 is above the past-due amount of Ab-1 (50), fine for aB-1
    base = {"identity_verified": "yes", "chosen_end": "flat_refusal", "product_ids": "Ab-1,aB-1",
            "add_note_success_count": 2}
    done = run_snippet(snippets["decide"], {**base, "notes_log": "Ab-1:paid;aB-1:paid;"})
    assert done["final_outcome"] == "paid"


# ---- AUD-48: integer product ids, a promise date without a date format, constant headers ----------------------------------


def _integer_product_ids(api: ToolApi) -> None:
    products = next(s for s in api.tools["get_account"].response_fields if s.name == "products")
    for item in products.items or []:
        if item.name == "product_id":
            item.type = "integer"
    for tool in ("create_promise", "add_note"):
        api.tools[tool].field("product_id", "request").type = "integer"


def test_integer_product_ids_compile_and_are_sent_as_integers(kit: ToolApi) -> None:
    api = variant(kit, _integer_product_ids)
    assert roles.account_roles(api).id_key == "product_id"
    assert roles.promise_roles(api.tools["create_promise"]).product == "product_id"
    workflow = compile_ab(api)
    tool = tool_of(workflow, "negotiate", "create_promise")
    facts = {**PROMISE_FACTS, "product_ids": "4821", "allowed_4821": "2026-10-15", "4821_floor": "10.00",
             "4821_past_due": "50.00"}
    for sent in (4821, "4821"):
        out = run_hook(tool["pre_process_code"], vars=dict(facts), config={"url": "u", "body": {
            "product_id": sent, "amount": 20, "date": "2026-10-15"}})
        assert out["vars"]["promise_guard"] == "ok" and out["config"]["body"]["product_id"] == 4821, sent
    note = tool_of(workflow, "negotiate", "add_note")
    out = run_hook(note["pre_process_code"], vars={"product_ids": "4821"}, config={"url": "u", "body": {
        "product_id": "4821", "reason": "paid"}})
    assert out["config"]["body"]["product_id"] == 4821
    assert lint_errors(workflow, api) == []


def test_a_string_id_is_preferred_over_an_integer_field(kit: ToolApi) -> None:
    def edit(api: ToolApi) -> None:
        products = next(s for s in api.tools["get_account"].response_fields if s.name == "products")
        products.items = [FieldSpec(name="line_no", type="integer"), *(products.items or [])]
    api = variant(kit, edit)
    assert roles.account_roles(api).id_key == "product_id"
    tool = tool_of(compile_ab(api), "negotiate", "create_promise")
    out = run_hook(tool["pre_process_code"], vars=dict(PROMISE_FACTS), config={"url": "u", "body": {
        "product_id": "p1", "amount": 20, "date": "2026-10-15"}})
    assert out["config"]["body"]["product_id"] == "p1"


@pytest.mark.parametrize(("name", "description"), [
    ("pay_on", "Promise date."), ("payment_day", "Day the customer will pay, YYYY-MM-DD."), ("promised_on", ""),
])
def test_a_promise_date_without_a_date_format_is_found_by_its_name_or_text(
    kit: ToolApi, name: str, description: str,
) -> None:
    def edit(api: ToolApi) -> None:
        spec = api.tools["create_promise"].field("date", "request")
        spec.name, spec.format, spec.description = name, None, description
    api = variant(kit, edit)
    assert roles.promise_roles(api.tools["create_promise"]).date == name
    tool = tool_of(compile_ab(api), "negotiate", "create_promise")
    blocked = run_hook(tool["pre_process_code"], vars=dict(PROMISE_FACTS), config={"url": "u", "body": {
        "product_id": "p1", "amount": 20, name: "2026-10-20"}})
    assert "not an allowed promise date" in blocked["vars"]["promise_guard"]


def test_a_constant_header_is_sent_and_an_open_credential_is_reported() -> None:
    from builder.ingest.tool_api import parse_tool_api

    doc = _kit_doc()
    for item in doc["paths"].values():
        item["post"].setdefault("parameters", []).append(
            {"name": "X-Client", "in": "header", "required": True, "schema": {"type": "string", "enum": ["voice-agent"]}})
    doc["paths"]["/close_call"]["post"]["parameters"].append(
        {"name": "X-Api-Key", "in": "header", "required": True, "schema": {"type": "string"}})
    api = parse_tool_api(doc)
    assert api.tools["close_call"].headers == {"X-Client": "voice-agent"}
    assert all(spec.name not in ("X-Client", "X-Api-Key") for spec in api.tools["close_call"].request_fields)
    compiled = compile_workflow(IR_AB, api, LEDGER_AB, None, "staged")
    headers = compiled.workflow["nodes"]["close"]["pre_actions"][0]["function"]["parameters"]["properties"]["headers"]
    assert headers["properties"]["X-Client"]["default"] == "voice-agent"
    assert headers["properties"]["Content-Type"]["default"] == "application/json"
    promise = tool_of(compiled.workflow, "negotiate", "create_promise")["function"]["parameters"]["properties"]
    assert promise["headers"]["properties"]["X-Client"]["default"] == "voice-agent"
    assert any("X-Api-Key" in note for note in compiled.notes)
    assert lint_errors(compiled.workflow, api) == []


# ---- AUD-27: a 5xx never runs the post-hook, so the prompt itself says what an error result means -------------------------


@pytest.mark.parametrize("profile", ["staged", "lean"])
def test_the_system_prompt_says_how_to_handle_a_tool_error(kit: ToolApi, profile: str) -> None:
    prompt = compile_ab(kit, profile)["system_prompt"]
    assert "If a tool result is an error instead of a result" in prompt
    assert "not a wrong or refused answer" in prompt
    assert "call the same tool once more with exactly the same values" in prompt
    assert "follow them instead" in prompt  # the promise tool's own retry rule wins


# ---- AUD-04 (string flags): a string / enum status acknowledges only on a success value -------------------------------------

_WRITE_TOOLS = ("create_promise", "add_note", "schedule_callback", "transfer_to_agent", "close_call")


def _string_status(field: str, enum: list[str] | None, description: str = "",
                   tools: tuple[str, ...] = _WRITE_TOOLS) -> Callable[[ToolApi], None]:
    """Every write tool answers with a string status instead of the boolean ``ok``."""
    def edit(api: ToolApi) -> None:
        for name in tools:
            contract = api.tools[name]
            contract.response_fields = [s for s in contract.response_fields if s.name != "ok"]
            contract.response_fields.insert(0, FieldSpec(name=field, type="string", enum=enum,
                                                         description=description))
            contract.response_description = ""
    return edit


STRING_STATUSES = [
    pytest.param("status", ["recorded", "rejected"], "", "recorded", "rejected", id="enum-recorded-rejected"),
    pytest.param("result", None, 'Either "ok" or "error".', "OK", "error", id="described-ok-error"),
    pytest.param("outcome", ["SUCCESS", "FAILED", "PENDING"], "", "success", "pending", id="enum-unknown-pending"),
    pytest.param("status", None, "`saved` when it was stored, `not_recorded` otherwise.", "saved", "not recorded",
                 id="described-saved"),
]


@pytest.mark.parametrize(("field", "enum", "description", "good", "bad"), STRING_STATUSES)
def test_a_string_status_is_the_success_signal(kit: ToolApi, field: str, enum: list[str] | None, description: str,
                                               good: str, bad: str) -> None:
    api = variant(kit, _string_status(field, enum, description))
    for name in _WRITE_TOOLS:
        assert roles.success_flag(api.tools[name]) is None, name  # no boolean: booleans keep precedence
        status = roles.success_status(api.tools[name])
        assert status is not None and status.field == field, name


@pytest.mark.parametrize("profile", ["staged", "lean"])
@pytest.mark.parametrize(("field", "enum", "description", "good", "bad"), STRING_STATUSES)
def test_a_string_status_is_read_by_every_post_hook(kit: ToolApi, field: str, enum: list[str] | None,
                                                    description: str, good: str, bad: str, profile: str) -> None:
    api = variant(kit, _string_status(field, enum, description))
    workflow = compile_ab(api, profile)
    promise = tool_of(workflow, "negotiate", "create_promise")["post_process_code"]
    assert "IS recorded" in post(promise, {field: good, "promise_id": "pr1"})
    assert "NOT recorded" in post(promise, {field: bad})
    assert "NOT recorded" in post(promise, {field: "something_else"})  # an unknown value is never acknowledged
    assert "NOT recorded" in post(promise, {})
    note = tool_of(workflow, "negotiate", "add_note")["post_process_code"]
    assert "note is saved" in post(note, {field: good}) and "NOT saved" in post(note, {field: bad})
    # the acknowledgement counters the routes and the closing decision read
    acked = run_hook(promise, vars={}, result={"status": "success", "header": {}, "data": {field: good}})["vars"]
    refused = run_hook(promise, vars={}, result={"status": "success", "header": {}, "data": {field: bad}})["vars"]
    assert acked.get("promise_acked") == "yes" and refused.get("promise_acked") != "yes"
    if profile == "lean":
        close = tool_of(workflow, "closing", "close_call") if "closing" in workflow["nodes"] else next(
            t for n in workflow["nodes"].values() for t in n.get("tools", []) if t["function"]["name"] == "close_call")
        assert "call is closed" in post(close["post_process_code"], {field: good})
        assert "Closing failed" in post(close["post_process_code"], {field: bad})
    assert lint_errors(workflow, api) == []


def test_a_string_status_callback_is_acknowledged_only_on_success(kit: ToolApi) -> None:
    api = variant(kit, _string_status("status", ["scheduled", "declined"]))
    workflow = compile_ab(api)
    hook = next(t for n in workflow["nodes"].values() for t in n.get("tools", [])
                if t["function"]["name"] == "schedule_callback")["post_process_code"]
    ok = run_hook(hook, vars={}, result={"status": "success", "header": {}, "data": {"status": "Scheduled"}})["vars"]
    no = run_hook(hook, vars={}, result={"status": "success", "header": {}, "data": {"status": "declined"}})["vars"]
    assert ok.get("callback_acked") == "yes" and no.get("callback_acked") != "yes"


def test_a_boolean_flag_keeps_precedence_over_a_string_status(kit: ToolApi) -> None:
    def edit(api: ToolApi) -> None:
        promise = api.tools["create_promise"]
        promise.response_fields.append(FieldSpec(name="status", type="string", enum=["recorded", "rejected"]))
    api = variant(kit, edit)
    assert roles.success_flag(api.tools["create_promise"]) == "ok"
    hook = tool_of(compile_ab(api), "negotiate", "create_promise")["post_process_code"]
    assert "IS recorded" in post(hook, {"ok": True, "status": "rejected"})


@pytest.mark.parametrize("spec", [
    FieldSpec(name="status", type="string"),  # no documented values
    FieldSpec(name="state", type="string", enum=["pending", "queued"]),  # no success or failure value
    FieldSpec(name="channel", type="string", enum=["voice", "sms"]),
    FieldSpec(name="error", type="string"),  # the kit's own error text
])
def test_an_undocumented_string_keeps_the_envelope_fallback(kit: ToolApi, spec: FieldSpec) -> None:
    def edit(api: ToolApi) -> None:
        promise = api.tools["create_promise"]
        promise.response_fields = [spec.model_copy()]
        promise.response_description = ""
    api = variant(kit, edit)
    assert roles.success_status(api.tools["create_promise"]) is None
    hook = tool_of(compile_ab(api), "negotiate", "create_promise")["post_process_code"]
    assert "IS recorded" in post(hook, {spec.name: "whatever"})  # HTTP 2xx, as before


# ---- Sol gap 6: a minor-unit scale the contract states (thousandths, 3 decimals) ------------------------------------------


@pytest.mark.parametrize(("name", "description", "scale", "noted"), [
    ("amount_cents", "", 100, False),
    ("amount", "Amount in minor units (cents), e.g. 12050 for 120.50.", 100, False),
    ("amount", "Amount in thousandths of the currency unit.", 1000, False),
    ("amount", "Amount in mills.", 1000, False),
    ("amount_minor", "Minor units with 3 decimal places.", 1000, False),
    ("amount_minor", "Multiply the major amount by 1000.", 1000, False),
    ("amount_minor", "x-minor-units: 3", 1000, False),
    ("amount_minor", "Minor units, one decimal place.", 10, False),
    ("amount_minor", "Minor units, 1 decimals.", 10, False),
    ("amount_minor", "Minor units; see the currency exponent.", 100, True),
    ("amount_minor", "Minor units: cents, but multiply by 1000.", 100, True),
])
def test_the_minor_unit_scale_is_read_from_the_contract(name: str, description: str, scale: int, noted: bool) -> None:
    spec = FieldSpec(name=name, type="integer", description=description)
    assert roles.minor_units(spec)
    got, note = roles.minor_scale(spec)
    assert got == scale and bool(note) is noted, (got, note)


@pytest.mark.parametrize(("description", "scale"), [
    ("Amount in minor units, scale of 1000.", 1000),
    ("Amount in minor units (scale: 1000).", 1000),
    ("Amount in minor units, scale of 100.", 100),
    ("Amount in minor units; 1 dinar = 1000 fils.", 1000),
    ("Amount in minor units; 1 euro = 100 cents.", 100),
    ("Amount in minor units (ISO 4217 exponent 3).", 1000),
])
def test_a_scale_stated_as_a_power_of_ten_is_read_as_that_factor(description: str, scale: int) -> None:
    """``scale of 1000`` is the factor itself, not an unreadable exponent: converting it as cents would send a tenth of
    the promised amount; ``1 dinar = 1000 fils`` states the factor too."""
    spec = FieldSpec(name="amount_minor", type="integer", description=description)
    assert roles.minor_scale(spec) == (scale, None)


def test_a_major_unit_field_with_three_decimals_is_not_minor_units() -> None:
    assert not roles.minor_units(FieldSpec(name="amount", type="number", description="multipleOf 0.001; 3 decimal places."))


def test_a_promise_amount_in_thousandths_is_sent_and_guarded_with_that_scale(kit: ToolApi) -> None:
    api = variant(kit, _minor_amount("amount", "Amount in thousandths of the currency unit (mills)."))
    workflow = compile_ab(api)
    tool = tool_of(workflow, "negotiate", "create_promise")
    for said in ("20", "20.00", 20, "20.5"):
        out = _guard(tool, {"amount": said})
        expected = 20500 if said == "20.5" else 20000
        assert out["config"]["url"] == "u" and out["config"]["body"]["amount"] == expected, said
        assert isinstance(out["config"]["body"]["amount"], int)
    low = _guard(tool, {"amount": "5"})
    assert low["config"]["url"] == "" and "the amount 5.00" in low["vars"]["promise_guard"]
    high = _guard(tool, {"amount": "50.01"})
    assert high["config"]["url"] == ""
    assert lint_errors(workflow, api) == []


def test_account_amounts_in_thousandths_are_read_with_that_scale(kit: ToolApi) -> None:
    from builder.checks.code_sandbox import extract_code_snippets, run_snippet

    def edit(api: ToolApi) -> None:
        products = next(s for s in api.tools["get_account"].response_fields if s.name == "products")
        for item in products.items or []:
            if item.name in ("past_due", "minimum_payment"):
                item.type, item.description = "integer", "Amount in thousandths (3 decimal places)."
    api = variant(kit, edit)
    compiled = compile_workflow(IR_A, api, LEDGER_A)
    snippets = extract_code_snippets(compiled.workflow)
    state = run_snippet(snippets["setup"], {"now": "2026-10-14T11:00"})
    state = run_snippet(snippets["account_facts"], {**state, "products": [
        {"product_id": "AC-9", "type": "credit_card", "card_last4": "7740", "past_due": 365000, "days_late": 8,
         "minimum_payment": 109500}]})
    assert state["AC-9_past_due"] == "365.00" and state["AC-9_floor"] == "109.50"
    assert not any("minor-unit scale" in note for note in compiled.notes)
    assert lint_errors(compiled.workflow, api, IR_A) == []


def test_an_unreadable_minor_unit_scale_keeps_cents_and_is_noted(kit: ToolApi) -> None:
    def edit(api: ToolApi) -> None:
        api.tools["create_promise"].field("amount", "request").name = "amount_minor"
        spec = api.tools["create_promise"].field("amount_minor", "request")
        spec.type, spec.description = "integer", "Minor units; see the currency exponent."
    api = variant(kit, edit)
    compiled = compile_workflow(IR_AB, api, LEDGER_AB, None, "staged")
    tool = tool_of(compiled.workflow, "negotiate", "create_promise")
    assert _guard(tool, {"amount_minor": "20"})["config"]["body"]["amount_minor"] == 2000
    assert any("minor-unit scale of 'amount_minor'" in note for note in compiled.notes)
