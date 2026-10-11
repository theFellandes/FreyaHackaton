# ruff: noqa: RUF001, RUF002  (Turkish test data)
"""A contract whose products carry a type field WITHOUT an enum (a free string, values only known from the data).

The type values then come from the brief and the documents, so the policy's per-type maps (``product_naming``,
``floor_field_by_type``, ``floor_rule_by_type``, typed bands) are keyed by the names the documents use. Before this fix
the validator treated such a contract like one whose products have no type at all and rejected every key but ``"*"``,
and the compiled agent looked a product's type up by its exact spelling only (``"Kredi Kartı"`` never found
``"kredi_karti"``). Invented data: ``tests/fixtures/invented_tr_build`` plus a ``kart_tipi`` field.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from builder.checks.code_sandbox import run_snippet
from builder.compiler.compile import compile_workflow
from builder.compiler.roles import account_roles
from builder.ingest.tool_api import ToolApi, parse_tool_api, with_base_url
from builder.policy.ir import ANY_PRODUCT, PolicyIR, RuleLedger, fold_product_type
from builder.understand.tool_roles import rename_to_roles, roles_from_document
from builder.understand.validate import tool_vocabulary, validate_policy

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "invented_tr_build"
MOCK = "https://ornek-mock.example.workers.dev"
TYPE_DESCRIPTION = "Card type as text, for example 'Kredi Kartı' or 'Ek Kart'."
PRODUCT = {"kart_no": "K-501", "son_dort": "4821", "ekstre_borcu": 12450.0, "asgari_tutar": 4980.0,
           "gecikme_gunu": 6, "son_odeme_tarihi": "2026-10-20", "kart_tipi": "Kredi Kartı"}
CALL = {"musteri_no": "M-1001", "ad": "Ayşe", "soyad": "Demir", "hitap": "Hanım", "simdi": "2026-10-26T10:30"}
TYPED_NAME = "sonu {son_dort} ile biten kredi kartı"
ANY_NAME = "sonu {son_dort} ile biten kart"


def _api(description: str = TYPE_DESCRIPTION) -> ToolApi:
    document = json.loads((FIXTURE / "openapi.json").read_text(encoding="utf-8"))
    document = copy.deepcopy(document)
    response = document["paths"]["/hesap-ozeti"]["post"]["responses"]["200"]["content"]["application/json"]
    items = response["schema"]["properties"]["kartlar"]["items"]["properties"]
    items["kart_tipi"] = {"type": "string", "description": description}  # a type field, no enum
    api = parse_tool_api(document, every_operation=True)
    return with_base_url(rename_to_roles(api, roles_from_document(api, document)), MOCK)


def _ledger() -> RuleLedger:
    return RuleLedger.model_validate_json((FIXTURE / "ledger.json").read_text(encoding="utf-8"))


def _ir(naming: dict[str, str] | None = None, floors: dict[str, str] | None = None) -> PolicyIR:
    ir = PolicyIR.model_validate_json((FIXTURE / "ir.json").read_text(encoding="utf-8"))
    naming = naming if naming is not None else {"kredi_karti": TYPED_NAME, ANY_PRODUCT: ANY_NAME}
    floors = floors if floors is not None else {"kredi_karti": "asgari_tutar"}
    disclosure = ir.disclosure.model_copy(update={"product_naming": naming})
    amounts = ir.amounts.model_copy(update={"floor_field_by_type": floors})
    return ir.model_copy(update={"disclosure": disclosure, "amounts": amounts})


def _code(workflow: dict[str, Any], node: str) -> str:
    return workflow["nodes"][node]["pre_actions"][0]["function"]["parameters"]["properties"]["code"]["default"]


def _facts(workflow: dict[str, Any], product: dict[str, Any]) -> dict[str, Any]:
    state = run_snippet(_code(workflow, "setup"), dict(CALL))
    state["products"] = [dict(product)]
    state["get_account_response"] = {"status": 200, "data": {"basarili": True, "kartlar": [dict(product)]}}
    return run_snippet(_code(workflow, "account_facts"), state)


# ---- the vocabulary and the validator ---------------------------------------------------------------------------


def test_a_type_field_without_an_enum_is_a_type_field() -> None:
    vocab = tool_vocabulary(_api())
    assert vocab.name_of("product_type", "type") == "kart_tipi"
    assert vocab.product_types is None  # the contract lists no values
    assert vocab.product_type_field == "kart_tipi"
    described = vocab.describe()
    assert "no type field" not in described
    assert "kart_tipi" in described


def test_type_keys_from_the_documents_are_no_validation_error() -> None:
    issues = validate_policy(_ir(), _ledger(), _api())
    errors = [issue for issue in issues if issue.code == "PRODUCT_TYPE" and issue.severity == "error"]
    assert errors == []


def test_a_key_the_contract_text_names_needs_no_warning() -> None:
    issues = validate_policy(_ir(), _ledger(), _api())
    assert not [issue for issue in issues if issue.code.startswith("PRODUCT_TYPE") and "kredi_karti" in issue.message]


def test_a_key_the_contract_text_does_not_name_is_only_a_warning() -> None:
    ir = _ir(naming={"bireysel_kart": TYPED_NAME}, floors={"bireysel_kart": "asgari_tutar"})
    found = [issue for issue in validate_policy(ir, _ledger(), _api()) if "bireysel_kart" in issue.message
             and issue.code.startswith("PRODUCT_TYPE")]
    assert found
    assert all(issue.severity == "warning" for issue in found)


def test_type_keys_are_accepted_with_a_warning_when_the_contract_names_no_values() -> None:
    ir = _ir()
    found = [issue for issue in validate_policy(ir, _ledger(), _api("The card's type.")) if issue.code.startswith(
        "PRODUCT_TYPE") and "kredi_karti" in issue.message]
    assert found and all(issue.severity == "warning" for issue in found)


def test_typed_bands_are_accepted() -> None:
    ir = _ir()
    bands = [band.model_copy(update={"product_types": ["kredi_karti"]}) for band in ir.bands]
    ir = ir.model_copy(update={"bands": bands})
    errors = [issue for issue in validate_policy(ir, _ledger(), _api())
              if issue.code == "PRODUCT_TYPE" and issue.severity == "error"]
    assert errors == []


def test_the_type_field_is_found_by_its_description() -> None:
    roles = account_roles(_api(), ("kredi_karti",), "musteri_no")
    assert roles.type_key == "kart_tipi"
    assert roles.type_open


# ---- spelling-insensitive lookup --------------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["Kredi Kartı", "kredi_karti", "KREDİ KARTI", "kredi-kartı", " Kredi  Kartı "])
def test_type_values_fold_to_one_key(value: str) -> None:
    assert fold_product_type(value) == "kredi_karti"


def test_the_policy_helpers_match_spelling_variants_and_fall_back_to_the_wildcard() -> None:
    ir = _ir()
    assert ir.amounts.floor_field("Kredi Kartı") == "asgari_tutar"
    assert ir.amounts.has_floor("KREDİ KARTI")
    assert ir.disclosure.product_name(PRODUCT, "kart_tipi") == "sonu 4821 ile biten kredi kartı"
    assert ir.disclosure.product_name({**PRODUCT, "kart_tipi": "Ek Kart"}, "kart_tipi") == "sonu 4821 ile biten kart"


@pytest.mark.parametrize("profile", ["staged", "lean"])
@pytest.mark.parametrize("value", ["kredi_karti", "Kredi Kartı", "KREDİ KARTI"])
def test_the_compiled_lookup_matches_spelling_variants(profile: str, value: str) -> None:
    compiled = compile_workflow(_ir(), _api(), _ledger(), profile=profile)
    facts = _facts(compiled.workflow, {**PRODUCT, "kart_tipi": value})["account_facts"]
    assert "sonu 4821 ile biten kredi kartı" in facts
    assert "Floor (asgari tutar): 4980.00" in facts


@pytest.mark.parametrize("profile", ["staged", "lean"])
def test_an_unknown_type_falls_back_to_the_wildcard(profile: str) -> None:
    naming = {"kredi_karti": TYPED_NAME, ANY_PRODUCT: ANY_NAME}
    floors = {"kredi_karti": "asgari_tutar", ANY_PRODUCT: "asgari_tutar"}
    compiled = compile_workflow(_ir(naming, floors), _api(), _ledger(), profile=profile)
    facts = _facts(compiled.workflow, {**PRODUCT, "kart_tipi": "Ek Kart"})["account_facts"]
    assert "sonu 4821 ile biten kart" in facts
    assert "ile biten kredi kartı" not in facts
    assert "Floor (asgari tutar): 4980.00" in facts
