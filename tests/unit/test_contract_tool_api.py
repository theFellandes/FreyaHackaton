"""The tool API of a case folder: base-URL override, canonical tool roles, and an OpenAPI derived from a prose contract.

Everything is invented (``tests/unit/_case_folder_data.py``): a contract whose paths and field names are Turkish-like,
an LLM stand-in that answers from the test. Proven here:

* every compiled tool request goes to the mock's origin, keeping each operation's path and method (A2);
* operations are renamed to the canonical tool names the compiler needs (``x-freya-role`` first, else one small LLM
  call), while method, path and field names stay the contract's; missing required roles fail clearly (A5);
* the kit's practice API passes through untouched, with no LLM call (A5);
* an OpenAPI is derived from a prose contract with one LLM call (plus one repair), keeping the contract's names and never
  inventing an operation (A4);
* a contract with Turkish field names compiles (A6).
"""

from __future__ import annotations

import asyncio
import copy
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

from builder.compiler.compile import compile_workflow
from builder.ingest.brief_loader import load_brief
from builder.ingest.tool_api import ToolApi, load_tool_api, parse_tool_api, with_base_url
from builder.llm.gateway import LLMCallResult
from builder.policy.ir import PolicyIR, RuleLedger
from builder.trace import NullTrace
from builder.understand.contract import ContractError, DerivedOpenApi, check_derived_document, derive_tool_api
from builder.understand.tool_roles import REQUIRED_ROLES, ToolRoleError, ToolRoleMap, canonical_tool_api
from tests.unit._case_folder_data import CONTRACT_SERVER, PATHS, contract_markdown, turkish_api_document

REPO = Path(__file__).resolve().parents[2]
MOCK = "https://banka-mock.ornek.workers.dev"
URL_RE = re.compile(r"https?://[^\s\"'<>\\]+")


class FakeLLM:
    """Answers structured calls with queued values and records every call (purpose, schema, prompts)."""

    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.calls: list[dict[str, Any]] = []

    async def structured(self, *, purpose: str, schema: type, system: str, user: str, **kwargs: Any) -> Any:
        self.calls.append({"purpose": purpose, "schema": schema.__name__, "system": system, "user": user, **kwargs})
        answer = self.answers.pop(0)
        value = answer if isinstance(answer, schema) else schema.model_validate(answer)
        return LLMCallResult(value=value, provider="fake", model="fake-model", key_label="fake#0", attempts=[],
                             latency_s=0.0, purpose=purpose)


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def hosts_in(workflow: dict[str, Any]) -> set[str]:
    return {urlsplit(url).hostname or "" for url in URL_RE.findall(json.dumps(workflow, ensure_ascii=False))}


def real(name: str) -> tuple[PolicyIR, RuleLedger]:
    base = REPO / "tests" / "fixtures" / "real_ir"
    ir = PolicyIR.model_validate_json((base / f"{name}_ir.json").read_text(encoding="utf-8"))
    ledger = RuleLedger.model_validate_json((base / f"{name}_ledger.json").read_text(encoding="utf-8"))
    return ir, ledger


# --- A2: base URL override ----------------------------------------------------------------------------------------------


def test_a_bare_origin_replaces_only_the_origin(kit_tool_api: ToolApi) -> None:
    api = with_base_url(kit_tool_api, MOCK)
    for name, contract in api.tools.items():
        original = kit_tool_api.tools[name]
        assert contract.url == f"{MOCK}/v1/{name}"  # the contract's own prefix (/v1) and path are kept
        assert contract.method == original.method and contract.path == original.path
        assert contract.request_fields == original.request_fields
    assert api.base_url == f"{MOCK}/v1"
    assert kit_tool_api.tools["close_call"].url.startswith("https://freya-bank-mock")  # the original is untouched


def test_a_base_url_with_a_path_replaces_the_server_url(kit_tool_api: ToolApi) -> None:
    api = with_base_url(kit_tool_api, MOCK + "/api")
    assert api.tools["verify_identity"].url == f"{MOCK}/api/verify_identity"
    assert api.base_url == f"{MOCK}/api"


def test_a_contract_without_a_server_gets_the_base_url() -> None:
    document = turkish_api_document()
    document.pop("servers")
    api = with_base_url(parse_tool_api(document, every_operation=True), MOCK)
    assert {contract.url for contract in api.tools.values()} == {MOCK + path for path in document["paths"]}


def test_a_relative_server_prefix_is_kept_on_the_mock() -> None:
    document = turkish_api_document()
    document["servers"] = [{"url": "/api/v2"}]
    api = with_base_url(parse_tool_api(document, every_operation=True), MOCK)
    assert api.base_url == MOCK + "/api/v2"
    assert {contract.url for contract in api.tools.values()} == {MOCK + "/api/v2" + path for path in document["paths"]}


@pytest.mark.parametrize("profile", ["staged", "lean"])
@pytest.mark.parametrize("name", ["brief_a", "brief_ab"])
def test_the_compiled_agent_calls_only_the_mock(kit_tool_api: ToolApi, profile: str, name: str) -> None:
    ir, ledger = real(name)
    workflow = compile_workflow(ir, with_base_url(kit_tool_api, MOCK), ledger, None, profile).workflow  # type: ignore[arg-type]
    assert hosts_in(workflow) == {urlsplit(MOCK).hostname}


# --- A5: canonical roles ------------------------------------------------------------------------------------------------


def test_the_practice_api_passes_through_without_a_model_call(kit_tool_api: ToolApi, kit_openapi_path: Path) -> None:
    llm = FakeLLM()
    document = json.loads(kit_openapi_path.read_text(encoding="utf-8"))
    api = run(canonical_tool_api(kit_tool_api, document, llm=llm, trace=NullTrace()))
    assert api.model_dump() == load_tool_api(kit_openapi_path).model_dump()
    assert llm.calls == []


def test_x_freya_role_renames_operations_but_keeps_the_contract() -> None:
    document = turkish_api_document()
    trace = NullTrace()
    api = run(canonical_tool_api(parse_tool_api(document), document, llm=FakeLLM(), trace=trace))
    assert list(api.tools)[:7] == ["verify_identity", "get_account", "create_promise", "add_note",
                                   "schedule_callback", "transfer_to_agent", "close_call"]
    assert "kampanya_sorgula" in api.tools  # an operation of no canonical role keeps a safe name of its own
    verify = api.tools["verify_identity"]
    assert verify.url == CONTRACT_SERVER + PATHS["/verify_identity"] and verify.method == "POST"
    assert [spec.name for spec in verify.request_fields] == ["musteri_no", "soru", "cevap"]
    assert any("verify_identity" in json.dumps(event, ensure_ascii=False) for event in trace.events)


def test_without_hints_one_small_model_call_maps_the_roles() -> None:
    document = turkish_api_document(roles=False)
    names = {"kimlik-dogrula": "verify_identity", "hesap-ozeti": "get_account", "taahhut": "create_promise",
             "not": "add_note", "geri-ara": "schedule_callback", "aktar": "transfer_to_agent",
             "sonlandir": "close_call", "sorgula": "other"}
    api = parse_tool_api(document, every_operation=True)
    answer = {"roles": [{"operation": tool, "role": names[contract.path.rsplit("/", 1)[-1]]}
                        for tool, contract in api.tools.items()]}
    llm = FakeLLM(answer)
    trace = NullTrace()
    mapped = run(canonical_tool_api(api, document, llm=llm, trace=trace))
    assert set(REQUIRED_ROLES) <= set(mapped.tools)
    assert mapped.tools["close_call"].url.endswith("/arama/sonlandir")
    assert len(llm.calls) == 1 and llm.calls[0]["schema"] == "ToolRoleMap"
    assert "role" in llm.calls[0]["purpose"] and "/arama/sonlandir" in llm.calls[0]["user"]


def test_an_invalid_role_map_is_repaired_once_then_fails() -> None:
    document = turkish_api_document(roles=False)
    api = parse_tool_api(document, every_operation=True)
    tools = list(api.tools)
    twice = {"roles": [{"operation": tool, "role": "verify_identity"} for tool in tools[:2]]}
    llm = FakeLLM(twice, twice)
    with pytest.raises(ToolRoleError, match="verify_identity"):
        run(canonical_tool_api(api, document, llm=llm, trace=NullTrace()))
    assert len(llm.calls) == 2 and "verify_identity" in llm.calls[1]["user"]  # the repair quotes the problem


def test_missing_required_roles_fail_with_an_actionable_error() -> None:
    document = turkish_api_document()
    del document["paths"][PATHS["/close_call"]]
    with pytest.raises(ToolRoleError) as info:
        run(canonical_tool_api(parse_tool_api(document), document, llm=None, trace=NullTrace()))
    message = str(info.value)
    assert "close_call" in message and "x-freya-role" in message


def test_role_map_schema_lists_the_roles() -> None:
    schema = json.dumps(ToolRoleMap.model_json_schema())
    for role in (*REQUIRED_ROLES, "other"):
        assert role in schema


def test_every_operation_of_a_path_becomes_a_tool() -> None:
    document = turkish_api_document()
    item = document["paths"].pop(PATHS["/get_account"])
    get_op = copy.deepcopy(item["post"])
    get_op["operationId"] = "hesap_getir"
    get_op["x-freya-role"] = "other"
    document["paths"]["/hesap"] = {"post": item["post"], "get": get_op}
    api = parse_tool_api(document, every_operation=True)
    methods = sorted(contract.method for contract in api.tools.values() if contract.path == "/hesap")
    assert methods == ["GET", "POST"]
    assert all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) for name in api.tools)
    assert len(parse_tool_api(document).tools) == len(api.tools) - 1  # the default keeps one operation per path


# --- A4: an OpenAPI derived from a prose contract -----------------------------------------------------------------------


def contract_doc(tmp_path: Path, document: dict[str, Any]) -> Any:
    path = tmp_path / "servis-sozlesmesi.md"
    path.write_text(contract_markdown(document), encoding="utf-8")
    return load_brief(path)


def test_an_openapi_is_derived_from_the_contract(tmp_path: Path) -> None:
    document = turkish_api_document()
    llm = FakeLLM(DerivedOpenApi(document=document))
    trace = NullTrace()
    out = tmp_path / "slot.openapi.json"
    derived = run(derive_tool_api([contract_doc(tmp_path, document)], llm=llm, trace=trace, out_path=out))
    assert len(llm.calls) == 1 and llm.calls[0]["schema"] == "DerivedOpenApi"
    assert "musteri_no" in llm.calls[0]["user"] and "x-freya-role" in llm.calls[0]["system"]
    assert json.loads(out.read_text(encoding="utf-8")) == derived.document
    assert derived.path == out
    assert any(str(out.name) in json.dumps(event) for event in trace.events)
    api = derived.api
    assert {contract.path for contract in api.tools.values()} == set(document["paths"])


def test_translated_names_are_repaired_once(tmp_path: Path) -> None:
    document = turkish_api_document()
    translated = copy.deepcopy(document)
    schema = translated["paths"][PATHS["/create_promise"]]["post"]["requestBody"]["content"]["application/json"]
    schema["schema"]["properties"]["amount"] = schema["schema"]["properties"].pop("tutar")
    llm = FakeLLM(DerivedOpenApi(document=translated), DerivedOpenApi(document=document))
    derived = run(derive_tool_api([contract_doc(tmp_path, document)], llm=llm, trace=NullTrace(),
                                  out_path=tmp_path / "o.json"))
    assert len(llm.calls) == 2
    assert "amount" in llm.calls[1]["user"] and "repair" in llm.calls[1]["purpose"]
    assert derived.document == document


def test_an_invented_operation_is_never_accepted(tmp_path: Path) -> None:
    document = turkish_api_document()
    invented = copy.deepcopy(document)
    invented["paths"]["/odeme/taksitlendir"] = copy.deepcopy(invented["paths"][PATHS["/create_promise"]])
    invented["paths"]["/odeme/taksitlendir"]["post"]["x-freya-role"] = "other"
    llm = FakeLLM(DerivedOpenApi(document=invented), DerivedOpenApi(document=invented))
    with pytest.raises(ContractError, match="taksitlendir"):
        run(derive_tool_api([contract_doc(tmp_path, document)], llm=llm, trace=NullTrace(),
                            out_path=tmp_path / "o.json"))
    assert len(llm.calls) == 2


def test_the_derived_document_checks(tmp_path: Path) -> None:
    document = turkish_api_document()
    text = contract_markdown(document)
    assert check_derived_document(document, text) == []
    broken = copy.deepcopy(document)
    broken["paths"][PATHS["/close_call"]]["post"]["x-freya-role"] = "verify_identity"
    problems = check_derived_document(broken, text)
    assert any("verify_identity" in problem for problem in problems)
    assert any("close_call" in problem for problem in problems)
    assert check_derived_document({"openapi": "3.0.3", "paths": {}}, text)


# --- A6: Turkish field names --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("profile", ["staged", "lean"])
@pytest.mark.parametrize("name", ["brief_a", "brief_ab"])
def test_a_contract_with_turkish_names_compiles_and_calls_only_the_mock(profile: str, name: str) -> None:
    document = turkish_api_document()
    api = run(canonical_tool_api(parse_tool_api(document), document, llm=None, trace=NullTrace()))
    api = with_base_url(api, MOCK)
    ir, ledger = real(name)
    workflow = compile_workflow(ir, api, ledger, None, profile).workflow  # type: ignore[arg-type]
    assert hosts_in(workflow) == {urlsplit(MOCK).hostname}
    text = json.dumps(workflow, ensure_ascii=False)
    assert MOCK + "/v1/musteri/kimlik-dogrula" in text and "musteri_no" in text and "tutar" in text


def _obj(properties: dict[str, Any], required: list[str] = ()) -> dict[str, Any]:  # type: ignore[assignment]
    return {"type": "object", "properties": properties, "required": list(required)}


def _op(role: str, request: dict[str, Any], response: dict[str, Any], summary: str) -> dict[str, Any]:
    return {"post": {
        "operationId": summary.split()[0].lower(), "summary": summary, "x-freya-role": role,
        "requestBody": {"content": {"application/json": {"schema": request}}},
        "responses": {"200": {"description": "`basarili` is false when the action was not carried out.",
                              "content": {"application/json": {"schema": response}}}},
    }}


def fully_turkish_document() -> dict[str, Any]:
    """Every name Turkish (paths, request AND response fields, enum values), descriptions in English - the shape the
    derivation prompt asks the model for."""
    ok = {"type": "boolean", "description": "True when the operation succeeded."}
    customer = {"type": "string", "description": "Customer number."}
    return {
        "openapi": "3.0.3", "info": {"title": "t", "version": "1"}, "servers": [{"url": CONTRACT_SERVER}],
        "paths": {
            "/kimlik/dogrula": _op("verify_identity", _obj({
                "musteri_no": customer,
                "soru_tipi": {"type": "string", "enum": ["dogum_tarihi", "tckn_son4"],
                              "description": "Which security question is answered."},
                "cevap": {"type": "string", "description": "The caller's answer."},
            }, ["musteri_no", "soru_tipi", "cevap"]), _obj({
                "basarili": ok,
                "dogru_mu": {"type": "boolean", "description": "True when this security answer is correct."},
                "tamamlandi": {"type": "boolean", "description": "True when all identity questions are verified."},
                "kalan_hak": {"type": "integer", "description": "Number of wrong answers left."},
            }), "Dogrula checks one security answer."),
            "/borc/sorgula": _op("get_account", _obj({"musteri_no": customer}, ["musteri_no"]), _obj({
                "basarili": ok,
                "urunler": {"type": "array", "items": _obj({
                    "urun_no": {"type": "string", "description": "Product id."},
                    "urun_tipi": {"type": "string", "enum": ["kredi_karti", "ihtiyac_kredisi"],
                                  "description": "Product type."},
                    "gecikmis_tutar": {"type": "number", "description": "Past-due amount in TRY."},
                    "asgari_odeme": {"type": "number", "description": "Minimum payment in TRY."},
                })},
            }), "Sorgula reads the statement debt."),
            "/odeme/taahhut": _op("create_promise", _obj({
                "musteri_no": customer,
                "urun_no": {"type": "string", "description": "Product id."},
                "tutar": {"type": "number", "description": "Promised amount in TRY."},
                "odeme_tarihi": {"type": "string", "format": "date", "description": "Date as YYYY-MM-DD."},
            }, ["musteri_no", "urun_no", "tutar", "odeme_tarihi"]), _obj({"basarili": ok}),
                "Taahhut records a payment promise."),
            "/gorusme/kapat": _op("close_call", _obj({
                "musteri_no": customer,
                "sonuc_kodu": {"type": "string", "enum": ["soz_alindi", "red", "dogrulanamadi"],
                               "description": "How the call ended."},
                "aciklama": {"type": "string", "description": "Free-text reason / detail."},
            }, ["musteri_no", "sonuc_kodu"]), _obj({"basarili": ok}), "Kapat ends the call."),
        },
    }


def test_role_discovery_works_on_a_fully_turkish_contract() -> None:
    from builder.compiler import roles

    document = fully_turkish_document()
    assert check_derived_document(document, json.dumps(document, ensure_ascii=False)) == []
    api = with_base_url(run(canonical_tool_api(parse_tool_api(document), document, trace=NullTrace())), MOCK)
    assert set(REQUIRED_ROLES) == set(api.tools)
    binding = roles.customer_binding(api)
    assert binding.field == "musteri_no"
    shape = roles.verify_shape(api.tools["verify_identity"])
    assert (shape.ok, shape.verified, shape.all_verified, shape.left) == ("basarili", "dogru_mu", "tamamlandi",
                                                                           "kalan_hak")
    assert roles.verify_roles(api.tools["verify_identity"], "musteri_no") == roles.VerifyRoles("soru_tipi", "cevap")
    account = roles.account_roles(api, {"kredi_karti"}, "musteri_no")
    assert (account.list_field, account.id_key, account.type_key) == ("urunler", "urun_no", "urun_tipi")
    assert roles.promise_roles(api.tools["create_promise"], "musteri_no", "urun_no") == roles.PromiseRoles(
        "urun_no", "tutar", "odeme_tarihi")
    assert roles.close_roles(api.tools["close_call"], "musteri_no") == roles.CloseRoles("sonuc_kodu", "aciklama")
    for name in REQUIRED_ROLES:
        assert roles.success_flag(api.tools[name]) == "basarili"
