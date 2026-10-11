# ruff: noqa: RUF001  (Turkish test data: the dotless i is intended)
"""Case-folder inputs: ``build.py {case_folder} {mock_url}`` (invented, Turkish-like data built in ``tmp_path``).

A bank may hand over a folder of its own documents instead of a brief and an OpenAPI file: a brief, a README, an analysis,
announcements, questions and answers, a service contract, test customers for the mock and example conversations, in
Markdown, Word, PDF and Excel, with names in any language. :func:`builder.inputs.classify_inputs` must sort them into
policy material, the contract, test customers (kept out of the policy) and examples (policy material, labelled), skip
what cannot be read, and take an ``http(s)`` argument as the base URL of the tools.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from builder.ingest.brief_loader import load_brief, load_briefs
from builder.ingest.case_folder import classify_document, fold
from builder.inputs import InputError, classify_inputs, normalise_base_url
from builder.settings import Settings
from tests.unit._case_folder_data import (
    DIALOGUE,
    make_docx,
    make_xlsx,
    turkish_api_document,
    write_case_folder,
)

MOCK = "https://banka-mock.ornek.workers.dev"


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    return write_case_folder(tmp_path)


def names(paths: list[Path]) -> list[str]:
    return [path.name for path in paths]


# --- the official command line ------------------------------------------------------------------------------------------


def test_a_case_folder_and_a_mock_url_are_classified(folder: Path, settings: Settings) -> None:
    bundle = classify_inputs([str(folder), MOCK + "/"], settings=settings)
    assert bundle.tool_base_url == MOCK
    assert bundle.tool_api_path is None  # no OpenAPI: the tool API is derived from the contract
    assert names(bundle.contract_paths) == ["servis-sozlesmesi.md"]
    assert names(bundle.customer_paths) == ["test-musterileri.xlsx"]
    assert names(bundle.example_paths) == ["konusma-1.md", "konusma-2.txt"]
    assert names(bundle.skipped_paths) == ["akis-semasi.png"]
    briefs = names(bundle.brief_paths)
    assert briefs[:2] == ["BRIEF.md", "README.md"]  # the brief first, then the README
    assert briefs[-2:] == ["konusma-1.md", "konusma-2.txt"]  # examples last: lowest priority
    assert set(briefs) == {"BRIEF.md", "README.md", "analiz.docx", "duyuru-01.md", "soru-cevap.txt",
                           "konusma-1.md", "konusma-2.txt"}
    assert "test-musterileri.xlsx" not in briefs and "servis-sozlesmesi.md" not in briefs
    assert not any(name.startswith(("~$", ".", "._")) for name in briefs)
    assert bundle.case_folder == folder.resolve()
    assert bundle.slot == "banka-vaka-klasoru"  # the folder's name, accents folded
    state = bundle.as_dict()
    assert state["tool_base_url"] == MOCK and state["tool_api"] == ""
    assert state["contracts"] == [path.as_posix() for path in bundle.contract_paths]
    assert state["customers"] and state["examples"] and state["skipped"]


def test_the_url_may_come_first_and_keeps_its_path_prefix(folder: Path, settings: Settings) -> None:
    bundle = classify_inputs([MOCK + "/api/v2/", str(folder)], settings=settings)
    assert bundle.tool_base_url == MOCK + "/api/v2"


def test_a_folder_with_its_own_openapi_uses_it(tmp_path: Path, settings: Settings) -> None:
    folder = write_case_folder(tmp_path, openapi=turkish_api_document())
    bundle = classify_inputs([folder], settings=settings)
    assert bundle.tool_api_path is not None and bundle.tool_api_path.name == "servis.openapi.json"
    assert bundle.contract_paths == []  # the machine-readable contract wins; the prose one is reference only
    assert bundle.tool_base_url is None  # no URL given: the contract's own server is kept
    assert "servis-sozlesmesi.md" not in names(bundle.brief_paths)


def test_the_practice_inputs_are_unchanged(brief_a_path: Path, kit_openapi_path: Path, settings: Settings) -> None:
    bundle = classify_inputs([brief_a_path, kit_openapi_path], settings=settings)
    assert bundle.as_dict() == {
        "briefs": [brief_a_path.resolve().as_posix()],
        "tool_api": kit_openapi_path.resolve().as_posix(),
        "slot": "brief-a",
        "inputs_sha": bundle.inputs_sha,
    }
    assert bundle.tool_base_url is None and bundle.case_folder is None


def test_the_practice_inputs_accept_a_mock_url(brief_a_path: Path, kit_openapi_path: Path, settings: Settings) -> None:
    plain = classify_inputs([brief_a_path, kit_openapi_path], settings=settings)
    bundle = classify_inputs([brief_a_path, kit_openapi_path, MOCK], settings=settings)
    assert bundle.tool_base_url == MOCK and bundle.slot == "brief-a"
    assert bundle.inputs_sha == plain.inputs_sha  # the same documents against another mock: the same workflow


def test_a_folder_without_a_contract_still_reports_the_missing_tool_api(tmp_path: Path, settings: Settings) -> None:
    folder = tmp_path / "case"
    folder.mkdir()
    (folder / "BRIEF.md").write_text("# Brief\nCall the customer.\n", encoding="utf-8")
    with pytest.raises(InputError, match="No tool API file found"):
        classify_inputs([folder, MOCK], settings=settings)


# --- the base URL -------------------------------------------------------------------------------------------------------


def test_the_flag_and_the_environment_also_give_the_base_url(folder: Path, make_settings) -> None:  # type: ignore[no-untyped-def]
    assert classify_inputs([folder], settings=make_settings(), tool_base_url=MOCK).tool_base_url == MOCK
    from_env = make_settings(tool_base_url=MOCK + "/")
    assert from_env.tool_base_url == MOCK
    assert classify_inputs([folder], settings=from_env).tool_base_url == MOCK
    other = "https://baska.workers.dev"
    assert classify_inputs([folder, other], settings=from_env).tool_base_url == other  # the command line wins


def test_two_different_urls_are_an_error(folder: Path, settings: Settings) -> None:
    with pytest.raises(InputError, match="base URL"):
        classify_inputs([folder, MOCK, "https://baska.workers.dev"], settings=settings)
    with pytest.raises(InputError, match="base URL"):
        classify_inputs([folder, MOCK], settings=settings, tool_base_url="https://baska.workers.dev")
    assert classify_inputs([folder, MOCK, MOCK + "/"], settings=settings, tool_base_url=MOCK).tool_base_url == MOCK


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://mock.example.dev", "https://mock.example.dev"),
        ("  http://127.0.0.1:8787/ ", "http://127.0.0.1:8787"),
        ("https://mock.example.dev/v1/", "https://mock.example.dev/v1"),
    ],
)
def test_base_urls_are_normalised(raw: str, expected: str) -> None:
    assert normalise_base_url(raw) == expected


@pytest.mark.parametrize("raw", ["https://", "https:///path", "https://mock.example.dev/?x=1", "https://a b.dev",
                                 "https://mock.example.dev/openapi.json", "https://mock.example.dev/spec.yaml"])
def test_bad_base_urls_are_rejected(raw: str) -> None:
    with pytest.raises(InputError, match="base URL"):
        normalise_base_url(raw)


def test_the_settings_reject_a_bad_tool_base_url(make_settings) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError, match="tool_base_url"):
        make_settings(tool_base_url="not a url")
    assert make_settings(tool_base_url="").tool_base_url is None
    assert make_settings(tool_base_url=MOCK).public_summary()["tool_base_url"] == MOCK


# --- classification ----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "role"),
    [
        ("BRIEF.md", "policy"),
        ("README.md", "policy"),
        ("Brief - Ödeme.docx", "policy"),
        ("analiz.pdf", "policy"),
        ("İş Analizi.docx", "policy"),
        ("Duyurular.md", "policy"),
        ("SSS.md", "policy"),
        ("soru-cevap.docx", "policy"),
        ("Questions and Answers.pdf", "policy"),
        ("akış-tanımı.md", "policy"),
        ("Müşteri İletişim Politikası.md", "policy"),
        ("Servis Sözleşmesi.docx", "contract"),
        ("SÖZLEŞME.pdf", "contract"),
        ("service_contract.md", "contract"),
        ("api-spec.yaml", "contract"),
        ("Test Müşterileri.xlsx", "customers"),
        ("musteriler.csv", "customers"),
        ("test_customers.json", "customers"),
        ("örnek-konuşma-3.md", "examples"),
        ("Örnek Konuşmalar.docx", "examples"),
        ("sample_conversations.txt", "examples"),
        ("transcript-2.md", "examples"),
        ("example-scenarios.md", "examples"),
        ("sample-analysis-conversation.md", "examples"),
        ("analysis-session-transcript.txt", "policy"),
        ("ivr-prompt-sheet.md", "policy"),
        ("akış-şeması.png", "skipped"),
        ("flow.JPG", "skipped"),
        ("diagram.svg", "skipped"),
    ],
)
def test_names_in_several_languages_are_classified(tmp_path: Path, name: str, role: str) -> None:
    path = tmp_path / name
    path.write_bytes(b"x")
    assert classify_document(path).role == role, classify_document(path).reason


def test_content_cues_classify_documents_with_neutral_names(tmp_path: Path) -> None:
    contract = tmp_path / "ek-1.md"
    contract.write_text("## POST /hesap/getir\nİstek: `musteri_no`\n\n## POST /odeme/taahhut\nYanıt: `basarili`\n",
                        encoding="utf-8")
    dialogue = tmp_path / "ek-2.md"
    dialogue.write_text(DIALOGUE, encoding="utf-8")
    customers = tmp_path / "ek-3.csv"
    customers.write_text("musteri_no,ad,dogum_tarihi\nM-1,Bir Kişi,1980-01-02\nM-2,İki Kişi,1981-02-03\n",
                         encoding="utf-8")
    rules = tmp_path / "ek-4.md"
    rules.write_text("# Kurallar\nMüşteriye borç tutarını yalnızca kimlik doğrulandıktan sonra söyleyin.\n",
                     encoding="utf-8")
    word = tmp_path / "ek-5.docx"
    word.write_bytes(make_docx(["Liste"], [["customer_id", "name", "date_of_birth"], ["C1", "A B", "1980-01-02"],
                                           ["C2", "C D", "1970-05-06"]]))
    assert [classify_document(path).role for path in (contract, dialogue, customers, rules, word)] == [
        "contract", "examples", "customers", "policy", "customers"]


def test_source_folders_and_explicit_examples_win_over_incidental_content(
    tmp_path: Path, settings: Settings,
) -> None:
    folder = tmp_path / "invented-case"
    (folder / "sources" / "meetings").mkdir(parents=True)
    (folder / "sources" / "documents").mkdir()
    (folder / "sources" / "emails").mkdir()
    (folder / "test-environment").mkdir()
    (folder / "BRIEF.md").write_text("Use the approved inventory process.", encoding="utf-8")
    (folder / "README.md").write_text("The call has sessionRef and now variables.", encoding="utf-8")
    (folder / "sources" / "documents" / "service-contract.md").write_text(
        "POST /inventory/list\nPOST /inventory/reserve\n", encoding="utf-8")
    prompt = folder / "sources" / "documents" / "phone-menu-prompt-sheet.md"
    prompt.write_text("Prompt | customer_id | name\nP1 | C1 | Greeting\nP2 | C2 | Farewell\n", encoding="utf-8")
    meeting = folder / "sources" / "meetings" / "interview-transcript.txt"
    meeting.write_text(DIALOGUE, encoding="utf-8")
    mail = folder / "sources" / "emails" / "conversation-recap.txt"
    mail.write_text(DIALOGUE, encoding="utf-8")
    example = folder / "example-scenarios.md"
    example.write_text("Example calls use POST /inventory/list then POST /inventory/reserve.\n" + DIALOGUE,
                       encoding="utf-8")
    customers = folder / "test-environment" / "test-customers.json"
    customers.write_text('[{"customer_id":"C1","name":"Ada"},{"customer_id":"C2","name":"Ben"}]',
                         encoding="utf-8")
    bundle = classify_inputs([folder, MOCK], settings=settings)
    assert bundle.customer_paths == [customers.resolve()]
    assert bundle.example_paths == [example.resolve()]
    assert names(bundle.contract_paths) == ["service-contract.md"]
    assert prompt.resolve() in bundle.brief_paths and meeting.resolve() in bundle.brief_paths
    assert mail.resolve() in bundle.brief_paths and bundle.brief_paths[-1] == example.resolve()
    loaded = load_briefs(bundle.brief_paths, examples=bundle.example_paths)
    labels = {doc.name: doc.label for doc in loaded.documents}
    assert labels[meeting.name] == labels[prompt.name] == labels[mail.name] == ""
    assert labels[example.name].startswith("example conversation")


def test_fold_removes_accents_and_case() -> None:
    assert fold("SÖZLEŞME Müşteri İŞLEM ığ") == "sozlesme musteri islem ig"


# --- reading the documents ---------------------------------------------------------------------------------------------


def test_word_and_excel_are_read_without_office_packages(folder: Path) -> None:
    import importlib.util

    assert importlib.util.find_spec("docx") is None and importlib.util.find_spec("openpyxl") is None
    analysis = load_brief(folder / "analiz.docx")
    assert analysis.text.splitlines() == [
        "İş analizi: arama akışı", "Kimlik doğrulama üç denemeyle sınırlıdır.",
        "Adım | Açıklama", "1 | Kimlik doğrulama", "2 | Borç bilgisi"]
    customers = load_brief(folder / "test-musterileri.xlsx")
    assert customers.text.splitlines()[1] == "M-1001 | Deneme Kişi | 1985-03-12 | 1234"


def test_examples_are_labelled_in_the_policy_corpus(folder: Path, settings: Settings) -> None:
    bundle = classify_inputs([folder], settings=settings)
    loaded = load_briefs(bundle.brief_paths, examples=bundle.example_paths)
    labels = {doc.name: doc.label for doc in loaded.documents}
    assert labels["konusma-1.md"].startswith("example conversation") and labels["BRIEF.md"] == ""
    text = loaded.combined_text()
    assert "=== FILE: BRIEF.md (markdown) ===" in text  # unlabelled documents keep the old header
    assert "=== FILE: konusma-1.md (markdown; example conversation" in text
    assert "M-1001" not in text  # the test customers never reach the policy stages


def test_an_excel_brief_and_yaml_files_are_brief_material(tmp_path: Path, settings: Settings) -> None:
    folder = tmp_path / "c"
    folder.mkdir()
    (folder / "brief.xlsx").write_bytes(make_xlsx([["kural", "değer"], ["deneme", "3"]]))
    (folder / "notlar.yml").write_text("kurallar:\n  - hafta sonu arama yok\n", encoding="utf-8")
    (folder / "openapi.yaml").write_text(
        "openapi: 3.0.3\ninfo: {title: t, version: '1'}\npaths:\n  /close_call:\n    post: {summary: x}\n",
        encoding="utf-8")
    bundle = classify_inputs([folder], settings=settings)
    assert names(bundle.brief_paths) == ["brief.xlsx", "notlar.yml"]
    assert bundle.tool_api_path is not None and bundle.tool_api_path.name == "openapi.yaml"
