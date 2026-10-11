# ruff: noqa: RUF001  (Turkish test data)
"""A contract whose products carry no type field, and a notes tool that takes free text.

Both shapes come from a bank's own service contract (here an invented Turkish one, ``tests/fixtures/invented_tr_build``:
the OpenAPI document, rule ledger and policy IR the builder derived from ``tests/fixtures/invented_tr_case``):

* ``get_account`` returns ``kartlar`` items without a product type, so the per-type maps of the policy
  (``product_naming``, ``floor_field_by_type``, ``floor_rule_by_type``) are keyed by ``"*"`` = every product. Without
  that the floor (a minimum payment) and the product name were silently lost: the agent offered only the full amount
  and called the card "product K-501".
* ``add_note`` takes ``{musteri_no, metin}``: no product id, no reason enum, one free-text field. An intent that says
  "write a note" must still produce exactly one note, with the text in that field (it was dropped with a compile note).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from builder.checks.code_sandbox import run_snippet
from builder.compiler.compile import compile_workflow
from builder.compiler.roles import note_roles
from builder.compiler.toolset import product_types
from builder.ingest.tool_api import ToolApi, parse_tool_api, with_base_url
from builder.policy.ir import ANY_PRODUCT, PolicyIR, RuleLedger
from builder.understand.tool_roles import rename_to_roles, roles_from_document
from builder.understand.validate import tool_vocabulary, validate_policy

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "invented_tr_build"
MOCK = "https://ornek-mock.example.workers.dev"
PRODUCT = {"kart_no": "K-501", "son_dort": "4821", "ekstre_borcu": 12450.0, "asgari_tutar": 4980.0,
           "gecikme_gunu": 6, "son_odeme_tarihi": "2026-10-20"}
CALL = {"musteri_no": "M-1001", "ad": "Ayşe", "soyad": "Demir", "hitap": "Hanım", "simdi": "2026-10-26T10:30"}


def _api() -> ToolApi:
    document = json.loads((FIXTURE / "openapi.json").read_text(encoding="utf-8"))
    api = parse_tool_api(document, every_operation=True)
    return with_base_url(rename_to_roles(api, roles_from_document(api, document)), MOCK)


def _ledger() -> RuleLedger:
    return RuleLedger.model_validate_json((FIXTURE / "ledger.json").read_text(encoding="utf-8"))


def _ir(**amounts: Any) -> PolicyIR:
    ir = PolicyIR.model_validate_json((FIXTURE / "ir.json").read_text(encoding="utf-8"))
    disclosure = ir.disclosure.model_copy(update={"product_naming": {ANY_PRODUCT: "sonu {son_dort} ile biten kredi kartı"}})
    floors = {"floor_field_by_type": {ANY_PRODUCT: "asgari_tutar"}, **amounts}
    return ir.model_copy(update={"disclosure": disclosure, "amounts": ir.amounts.model_copy(update=floors)})


def _code(workflow: dict[str, Any], node: str) -> str:
    return workflow["nodes"][node]["pre_actions"][0]["function"]["parameters"]["properties"]["code"]["default"]


def _facts(workflow: dict[str, Any]) -> dict[str, Any]:
    state = run_snippet(_code(workflow, "setup"), dict(CALL))
    state["products"] = [dict(PRODUCT)]
    state["get_account_response"] = {"status": 200, "data": {"basarili": True, "kartlar": [dict(PRODUCT)]}}
    return run_snippet(_code(workflow, "account_facts"), state)


# ---- untyped product lists ------------------------------------------------------------------------------------------


def test_the_vocabulary_tells_the_extraction_to_key_untyped_products_by_the_wildcard() -> None:
    described = tool_vocabulary(_api()).describe()
    assert f'"{ANY_PRODUCT}"' in described
    assert "no type field" in described


def test_the_wildcard_is_a_valid_key_and_covers_every_product() -> None:
    issues = validate_policy(_ir(), _ledger(), _api())
    codes = {issue.code for issue in issues}
    assert "PRODUCT_TYPE" not in codes
    assert "PRODUCT_FIELD" not in codes


def test_a_type_key_on_an_untyped_product_list_is_reported() -> None:
    ir = _ir(floor_field_by_type={"credit_card": "asgari_tutar"})
    found = [issue for issue in validate_policy(ir, _ledger(), _api()) if issue.code == "PRODUCT_TYPE"]
    assert found and ANY_PRODUCT in found[0].message


def test_the_wildcard_is_not_a_product_type_the_compiler_looks_for() -> None:
    assert ANY_PRODUCT not in product_types(_ir())


@pytest.mark.parametrize("profile", ["staged", "lean"])
def test_the_floor_and_the_name_apply_to_every_untyped_product(profile: str) -> None:
    compiled = compile_workflow(_ir(), _api(), _ledger(), profile=profile)
    state = _facts(compiled.workflow)
    facts = state["account_facts"]
    assert "Floor (asgari tutar): 4980.00" in facts
    assert "sonu 4821 ile biten kredi kartı" in facts
    assert "product K-501" not in facts
    assert "sonu 4821 ile biten kredi kartı" in state["debt_statement"]


def test_the_policy_helpers_fall_back_to_the_wildcard() -> None:
    ir = _ir()
    assert ir.amounts.floor_field("") == "asgari_tutar"
    assert ir.amounts.has_floor("credit_card")
    assert ir.disclosure.product_name(PRODUCT) == "sonu 4821 ile biten kredi kartı"


# ---- a free-text notes tool -----------------------------------------------------------------------------------------


def test_a_free_text_notes_tool_has_a_text_field_and_no_product() -> None:
    roles = note_roles(_api().tools["add_note"], "musteri_no", "kart_no")
    assert roles.text == "metin"
    assert roles.product is None
    assert roles.reason is None


def test_the_kit_notes_tool_keeps_its_roles() -> None:
    from builder.ingest.tool_api import load_tool_api

    kit = load_tool_api(Path(__file__).resolve().parents[2] / "tools" / "openapi.json")
    roles = note_roles(kit.tools["add_note"], "customer_id", "product_id")
    assert (roles.product, roles.reason, roles.text) == ("product_id", "reason", None)


def _note_tool(workflow: dict[str, Any], node: str = "negotiate") -> dict[str, Any]:
    tools = [t for t in workflow["nodes"][node].get("tools", []) if t["function"]["name"] == "add_note"]
    assert len(tools) == 1, f"{node} must carry the notes tool"
    return tools[0]["function"]


def _bullet(task: str, situation: str) -> str:
    lines = [line for line in task.splitlines() if line.lstrip("- ").startswith(situation)]
    assert lines, f"no bullet for {situation!r}"
    return lines[0]


def _baked_note_texts(workflow: dict[str, Any]) -> list[str]:
    """Every ``metin`` value the workflow fixes itself (an engine-called notes request), instead of the model."""
    found = []
    for node in workflow["nodes"].values():
        for action in node.get("pre_actions", []):
            body = action.get("function", {}).get("parameters", {}).get("properties", {}).get("body", {})
            leaf = body.get("properties", {}).get("metin")
            if leaf is not None and leaf.get("default", "{{metin}}") != "{{metin}}":
                found.append(leaf["default"])
    return found


@pytest.mark.parametrize("profile", ["staged", "lean"])
def test_an_intent_note_is_written_by_the_model_with_the_customers_own_information(profile: str) -> None:
    """The note is what the customer said (the date they paid, that they dispute), never the brief's instruction.

    The notes tool takes free text, so the words must come from the model at call time: a note baked into the workflow
    can hold neither the date nor the amount the customer gives in the call (it was the intent's description, or an
    instruction such as "ask the payment date").
    """
    compiled = compile_workflow(_ir(), _api(), _ledger(), profile=profile)
    assert not [note for note in compiled.notes if "add_note" in note and "no node or tool" in note]
    workflow = compiled.workflow
    function = _note_tool(workflow)
    assert function["parameters"]["properties"]["url"]["default"] == f"{MOCK}/api/not-ekle"
    body = function["parameters"]["properties"]["body"]
    assert set(body["properties"]) == {"musteri_no", "metin"}
    assert body["properties"]["musteri_no"]["default"] == "{{musteri_no}}"
    text = body["properties"]["metin"]
    assert text.get("default") == "{{metin}}", "the note's words are the model's argument, not a fixed value"
    assert "metin" in body.get("required", [])
    described = text["description"]
    assert "Turkish" in described
    for word in ("date", "amount", "customer", "instruction"):
        assert word in described, word
    # nothing fixes the note's words: no engine-called note with the intent's description or the brief's instruction
    assert _baked_note_texts(workflow) == []
    assert not [name for name in workflow["nodes"] if name.startswith("note_")]
    # the negotiation tells the model to write the note with the customer's own facts before it says goodbye
    task = workflow["nodes"]["negotiate"]["task_message"]
    for situation in ("Müşteri zaten ödediğini söyler", "Müşteri borcu kabul etmez"):
        bullet = _bullet(task, situation)
        assert "add_note" in bullet and "metin" in bullet
        assert "what the customer" in bullet
        assert bullet.index("add_note") < bullet.index("goodbye")
    if profile == "staged":  # the exit itself says the note comes first
        exits = {t["target_node"]: t["description"] for t in workflow["nodes"]["negotiate"]["transitions"]}
        for target in ("end_already_paid", "end_dispute"):
            assert "note is saved" in exits[target]


def test_the_note_follows_a_format_the_brief_prescribes() -> None:
    ir = _ir()
    fixed = "Ödendi - ödeme tarihi: <tarih>"
    intents = [i.model_copy(update={"actions": [a.model_copy(update={"params": {"metin": fixed}})
                                                for a in i.actions]}) if i.key == "already_paid" else i
               for i in ir.intents]
    compiled = compile_workflow(ir.model_copy(update={"intents": intents}), _api(), _ledger(), profile="staged")
    bullet = _bullet(compiled.workflow["nodes"]["negotiate"]["task_message"], "Müşteri zaten ödediğini söyler")
    assert fixed in bullet and "real values" in bullet
    assert _baked_note_texts(compiled.workflow) == []
