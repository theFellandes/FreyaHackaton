# ruff: noqa: RUF001  (Turkish test data: the dotless i is intended)
"""The derived OpenAPI must keep the contract's FULL paths, its HTTP methods and its server prefix.

All contracts here are invented for the tests (Turkish-like wording, made-up bank and endpoints).
"""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from typing import Any

import pytest

from builder.ingest.brief_loader import load_brief
from builder.llm.gateway import LLMCallResult
from builder.trace import NullTrace
from builder.understand.contract import (
    ContractError,
    DerivedOpenApi,
    check_derived_document,
    derive_tool_api,
    review_derived_document,
)
from builder.understand.tool_roles import REQUIRED_ROLES
from tests.unit._case_folder_data import PATHS, contract_markdown, turkish_api_document

VERIFY = PATHS["/verify_identity"]  # /musteri/kimlik-dogrula
PROMISE = PATHS["/create_promise"]  # /odeme/taahhut


class FakeLLM:
    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.calls: list[dict[str, Any]] = []

    async def structured(self, *, purpose: str, schema: type, system: str, user: str, **kwargs: Any) -> Any:
        self.calls.append({"purpose": purpose, "system": system, "user": user})
        value = self.answers.pop(0)
        return LLMCallResult(value=value, provider="fake", model="fake-model", key_label="fake#0", attempts=[],
                             latency_s=0.0, purpose=purpose)


def path_problems(document: dict[str, Any], text: str) -> list[str]:
    """The problems of ``document`` except a missing required role (the small documents here have few operations)."""
    return [problem for problem in check_derived_document(document, text)
            if not problem.startswith("no operation has the role")]


def _rename_path(document: dict[str, Any], old: str, new: str) -> dict[str, Any]:
    changed = copy.deepcopy(document)
    changed["paths"] = {(new if path == old else path): item for path, item in changed["paths"].items()}
    return changed


def _with_method(document: dict[str, Any], path: str, method: str) -> dict[str, Any]:
    changed = copy.deepcopy(document)
    item = changed["paths"][path]
    item[method] = item.pop("post")
    return changed


# --- methods -------------------------------------------------------------------------------------------------------


def test_a_wrong_method_is_rejected_with_the_contract_method() -> None:
    document = turkish_api_document()
    text = contract_markdown(document)
    problems = check_derived_document(_with_method(document, VERIFY, "get"), text)
    assert any(f"expected 'POST {VERIFY}'" in problem for problem in problems), problems


def test_a_wrong_method_is_repaired_once(tmp_path: Path) -> None:
    document = turkish_api_document()
    contract = tmp_path / "servis-sozlesmesi.md"
    contract.write_text(contract_markdown(document), encoding="utf-8")
    llm = FakeLLM(DerivedOpenApi(document=_with_method(document, PROMISE, "put")), DerivedOpenApi(document=document))
    derived = asyncio.run(derive_tool_api([load_brief(contract)], llm=llm, trace=NullTrace(),
                                          out_path=tmp_path / "o.json"))
    assert len(llm.calls) == 2 and "repair" in llm.calls[1]["purpose"]
    assert f"expected 'POST {PROMISE}'" in llm.calls[1]["user"]
    assert derived.document == document


def test_a_method_stated_on_a_neighbouring_line_counts() -> None:
    document = _minimal({"/kayit/not-ekle": "post"})
    text = "Not ekleme\n\nYöntem: POST\n\nYol: /kayit/not-ekle\n\nİstek: musteri_no, metin\n"
    assert path_problems(document, text) == []
    problems = path_problems(_minimal({"/kayit/not-ekle": "get"}), text)
    assert any("expected 'POST /kayit/not-ekle'" in problem for problem in problems), problems


def test_methods_in_a_table_row_belong_to_their_own_path() -> None:
    text = ("Yöntem | Yol | Açıklama\n"
            "GET | /hesap/ozet | Borcu döner.\n"
            "POST | /hesap/not | Not yazar.\n"
            "İstek alanları: musteri_no; metin.\n")
    assert path_problems(_minimal({"/hesap/ozet": "get", "/hesap/not": "post"}), text) == []
    problems = path_problems(_minimal({"/hesap/ozet": "post", "/hesap/not": "post"}), text)
    assert any("expected 'GET /hesap/ozet'" in problem for problem in problems), problems


def test_a_method_written_before_another_path_is_not_taken() -> None:
    text = ("Uç noktalar:\n"
            "GET | /hesap/ozet | borcu döner\n"
            "- /odeme/soz : sözü kaydeder; kaydetmeden önce GET /hesap/ozet çağrılmalı.\n")
    review = review_derived_document(_minimal({"/hesap/ozet": "get", "/odeme/soz": "post"}), text)
    assert [problem for problem in review.problems if not problem.startswith("no operation has the role")] == []
    assert any("/odeme/soz" in note for note in review.notes), review.notes


def test_a_contract_without_methods_is_accepted_with_a_note(tmp_path: Path) -> None:
    text = ("Servisler:\n- /kimlik/dogrula : güvenlik cevabını kontrol eder.\n- /hesap/ozet : borcu döner.\n"
            "- /odeme/soz : ödeme sözünü kaydeder.\n- /gorusme/kapat : görüşmeyi bitirir.\n")
    document = _minimal({"/kimlik/dogrula": "post", "/hesap/ozet": "get", "/odeme/soz": "post",
                         "/gorusme/kapat": "post"})
    review = review_derived_document(document, text)
    assert review.problems == []
    assert any("/hesap/ozet" in note and "method" in note for note in review.notes), review.notes
    contract = tmp_path / "servis.md"
    contract.write_text(text, encoding="utf-8")
    trace = NullTrace()
    asyncio.run(derive_tool_api([load_brief(contract)], llm=FakeLLM(DerivedOpenApi(document=document)), trace=trace))
    assert "no HTTP method" in json.dumps(trace.events, ensure_ascii=False, default=str)


def test_a_single_method_stated_for_every_request_is_expected() -> None:
    text = "Bütün istekler POST isteğidir.\n\nUç noktalar:\n\n\n\n\n- /kayit/not-ekle: not yazar (musteri_no, metin).\n"
    assert path_problems(_minimal({"/kayit/not-ekle": "post"}), text) == []
    problems = path_problems(_minimal({"/kayit/not-ekle": "get"}), text)
    assert any("expected 'POST /kayit/not-ekle'" in problem for problem in problems), problems


# --- full paths ----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("wrong", [
    "/kimlik-dogrula/musteri",  # segments swapped
    "/musteri/taahhut",  # segments of two endpoints recombined
    "/musteri/odeme/taahhut",  # a segment of another endpoint inserted
    "/kimlik-dogrula",  # a segment dropped
])
def test_a_recombined_path_is_rejected(wrong: str) -> None:
    document = turkish_api_document()
    text = contract_markdown(document)
    old = VERIFY if "kimlik" in wrong else PROMISE
    problems = check_derived_document(_rename_path(document, old, wrong), text)
    assert any(wrong in problem and "contract" in problem for problem in problems), problems


def test_the_correct_document_is_accepted() -> None:
    document = turkish_api_document()
    review = review_derived_document(document, contract_markdown(document))
    assert review.problems == [] and review.notes == []


def test_turkish_paths_with_accents_hyphens_and_placeholders() -> None:
    text = ("Uç noktalar\n"
            "POST | /Ödeme/Söz‑Ver | ödeme sözünü kaydeder (musteri_no, tutar)\n"  # non-breaking hyphen, other case
            "GET | /müşteri/:müşteri_no/borç-özeti | borcu döner\n"
            "DELETE | /arama/<arama_no> | aramayı siler\n")
    good = _minimal({"/ödeme/söz-ver": "post", "/müşteri/{müşteri_no}/borç-özeti": "get", "/arama/{arama_no}": "delete"})
    assert path_problems(good, text) == []
    deaccented = _minimal({"/odeme/soz-ver": "post", "/müşteri/{müşteri_no}/borç-özeti": "get",
                           "/arama/{arama_no}": "delete"})
    problems = path_problems(deaccented, text)
    assert any("/odeme/soz-ver" in problem for problem in problems), problems
    wrong_method = _minimal({"/ödeme/söz-ver": "post", "/müşteri/{müşteri_no}/borç-özeti": "post",
                             "/arama/{arama_no}": "delete"})
    problems = path_problems(wrong_method, text)
    assert any("expected 'GET /müşteri/{müşteri_no}/borç-özeti'" in problem for problem in problems), problems


# --- server prefix -------------------------------------------------------------------------------------------------


CONTRACT_WITH_PREFIX = ("Sunucu adresi: https://servis.ornek.example/api. Bütün istekler POST isteğidir.\n"
                        "POST | /kayit/not-ekle | not yazar (musteri_no, metin)\n")


def test_the_server_prefix_is_kept() -> None:
    document = _minimal({"/kayit/not-ekle": "post"}, server="https://servis.ornek.example/api")
    assert path_problems(document, CONTRACT_WITH_PREFIX) == []
    dropped = _minimal({"/kayit/not-ekle": "post"}, server="https://servis.ornek.example")
    problems = path_problems(dropped, CONTRACT_WITH_PREFIX)
    assert any("/api" in problem and "servers" in problem for problem in problems), problems


def test_the_server_prefix_is_never_repeated_in_a_path() -> None:
    doubled = _minimal({"/api/kayit/not-ekle": "post"}, server="https://servis.ornek.example/api")
    text = CONTRACT_WITH_PREFIX + "Tam adres: https://servis.ornek.example/api/kayit/not-ekle\n"
    problems = path_problems(doubled, text)
    assert any("/api/kayit/not-ekle" in problem and "prefix" in problem for problem in problems), problems
    # the same path with the prefix in the path and none in the server is fine
    assert path_problems(_minimal({"/api/kayit/not-ekle": "post"}, server="https://servis.ornek.example"),
                                  text) == []


def test_a_path_found_only_below_an_unstated_prefix_is_rejected() -> None:
    text = "Adres: https://servis.ornek.example/v2/kayit/not-ekle (POST; musteri_no, metin)\n"
    relative = _minimal({"/kayit/not-ekle": "post"}, server="/")
    problems = path_problems(relative, text)
    assert any("/v2/kayit/not-ekle" in problem for problem in problems), problems
    assert path_problems(_minimal({"/kayit/not-ekle": "post"}, server="https://servis.ornek.example/v2"),
                                  text) == []


def test_an_invented_contract_with_a_contract_failure_still_raises(tmp_path: Path) -> None:
    contract = tmp_path / "servis.md"
    contract.write_text(CONTRACT_WITH_PREFIX, encoding="utf-8")
    wrong = _minimal({"/kayit/not-ekle": "get"}, server="https://servis.ornek.example/api")
    llm = FakeLLM(DerivedOpenApi(document=wrong), DerivedOpenApi(document=wrong))
    with pytest.raises(ContractError, match="expected 'POST /kayit/not-ekle'"):
        asyncio.run(derive_tool_api([load_brief(contract)], llm=llm, trace=NullTrace()))


def _minimal(paths: dict[str, str], *, server: str = "/") -> dict[str, Any]:
    """An OpenAPI document with one operation per path; the first operations get the required roles in order."""
    document: dict[str, Any] = {"openapi": "3.0.3", "info": {"title": "t", "version": "1"}, "servers": [{"url": server}],
                                "paths": {}}
    for path, method in paths.items():
        params = [{"name": name.strip("{}"), "in": "path", "required": True, "schema": {"type": "string"}}
                  for name in path.split("/") if name.startswith("{")]
        document["paths"][path] = {method: {
            "operationId": "op" + str(len(document["paths"])),
            "x-freya-role": REQUIRED_ROLES[len(document["paths"])] if len(document["paths"]) < len(REQUIRED_ROLES)
            else "other",
            **({"parameters": params} if params else {}),
            "responses": {"200": {"description": "ok"}},
        }}
    return document
