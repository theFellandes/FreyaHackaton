# ruff: noqa: RUF001  (Turkish test data)
"""A prescribed sentence with placeholders for runtime values is said with the values, never dropped or emptied.

A brief may prescribe the promise confirmation as "Your payment promise of {amount} on {date} is recorded." (in any
language, with placeholder names in any language: ``{tutar}``, ``{tarih}``, ``[tutar]``, ``<tarih>``). The values are not
call variables: they are the amount and the date the customer agreed and the bank recorded. The compiler used to remove
the placeholders from the spoken line ("Ödeme sözünüz TL olarak tarihine kaydedildi."), so the confirmation lost the
amount and the date. Now the line is said through the model with an explicit instruction to put the recorded values in
place of each placeholder, and the prompt that tells the model to say the sentence carries the same instruction.

The data is invented (``tests/fixtures/invented_tr_build`` and the English practice fixtures); nothing is brief-specific.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from builder.compiler.compile import compile_workflow
from builder.compiler.phrasing import fill_values_note, runtime_placeholders
from builder.ingest.tool_api import ToolApi, load_tool_api, parse_tool_api, with_base_url
from builder.policy.ir import ANY_PRODUCT, PolicyIR, RuleLedger
from builder.understand.tool_roles import rename_to_roles, roles_from_document

REPO = Path(__file__).resolve().parents[2]
TR = REPO / "tests" / "fixtures" / "invented_tr_build"
EN = REPO / "tests" / "fixtures" / "policy"
MOCK = "https://ornek-mock.example.workers.dev"
#: any single-brace, square-bracket or angle-bracket placeholder (rule-id tags such as ``[D1-20a]`` have digits)
PLACEHOLDER = re.compile(r"(?<!\{)\{[^\W\d][\w]*\}(?!\})|\[[^\W\d][^\W\d ]*(?: [^\W\d]+)*\]|<[^\W\d][\w ]*>")

TR_CURLY = "Ödeme sözünüz {tutar} TL olarak {tarih} tarihine kaydedildi."
TR_BRACKET = "Ödeme sözünüz [tutar] TL olarak <tarih> tarihine kaydedildi."
EN_CURLY = "Your payment promise of {amount} on {date} is recorded."


def _tr_api() -> ToolApi:
    document = json.loads((TR / "openapi.json").read_text(encoding="utf-8"))
    api = parse_tool_api(document, every_operation=True)
    return with_base_url(rename_to_roles(api, roles_from_document(api, document)), MOCK)


def _tr(sentence: str) -> tuple[PolicyIR, RuleLedger]:
    ir = PolicyIR.model_validate_json((TR / "ir.json").read_text(encoding="utf-8"))
    disclosure = ir.disclosure.model_copy(update={"product_naming": {ANY_PRODUCT: "sonu {son_dort} ile biten kredi kartı"}})
    amounts = ir.amounts.model_copy(update={"floor_field_by_type": {ANY_PRODUCT: "asgari_tutar"}})
    phrases = ir.phrases.model_copy(update={"recorded_sentence": sentence})
    ir = ir.model_copy(update={"disclosure": disclosure, "amounts": amounts, "phrases": phrases})
    return ir, RuleLedger.model_validate_json((TR / "ledger.json").read_text(encoding="utf-8"))


def _en(sentence: str) -> tuple[PolicyIR, RuleLedger]:
    ir = PolicyIR.model_validate_json((EN / "brief_a_ir.json").read_text(encoding="utf-8"))
    ir.recording.success_sentence = sentence
    return ir, RuleLedger.model_validate_json((EN / "brief_a_ledger.json").read_text(encoding="utf-8"))


def _spoken(workflow: dict[str, Any]) -> list[str]:
    """Every line the engine speaks verbatim (``tts`` entries)."""
    out = []
    for node in workflow["nodes"].values():
        enter = node.get("on_enter") or {}
        if enter.get("type") == "tts":
            out.append(str(enter.get("text") or ""))
    return out


def _names(sentence: str) -> list[str]:
    return [m.group(0) for m in PLACEHOLDER.finditer(sentence)]


CASES = [
    pytest.param(_tr, TR_CURLY, id="tr-curly"),
    pytest.param(_tr, TR_BRACKET, id="tr-bracket"),
    pytest.param(_tr, EN_CURLY, id="tr-ir-english-names"),
    pytest.param(_en, EN_CURLY, id="en-curly"),
]


def _compile(build: Any, sentence: str, profile: str) -> Any:
    ir, ledger = build(sentence)
    api = _tr_api() if build is _tr else load_tool_api(REPO / "tools" / "openapi.json")
    return compile_workflow(ir, api, ledger, profile=profile)


# ---- the helpers ------------------------------------------------------------------------------------------------------


def test_runtime_placeholders_find_every_form_and_skip_call_variables_and_rule_tags() -> None:
    assert runtime_placeholders(TR_CURLY) == ["{tutar}", "{tarih}"]
    assert runtime_placeholders(TR_BRACKET) == ["[tutar]", "<tarih>"]
    assert runtime_placeholders("Söz {ödeme_tarihi} için {miktar}.") == ["{ödeme_tarihi}", "{miktar}"]
    assert runtime_placeholders("Hello {first_name}, {{ad}} [D1-20a] <3", ["first_name"]) == []
    assert runtime_placeholders("Your payment of {amount} on {date} and {amount}.") == ["{amount}", "{date}"]
    assert runtime_placeholders("No placeholders here.") == []


def test_the_fill_note_names_each_placeholder_and_forbids_saying_it() -> None:
    note = fill_values_note(["{tutar}", "{tarih}"])
    assert "{tutar}" in note and "{tarih}" in note
    assert "recorded" in note and "never" in note.lower()
    assert fill_values_note([]) == ""


# ---- the compiled workflow -------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("build", "sentence"), CASES)
def test_the_promise_confirmation_is_said_with_its_values(build: Any, sentence: str) -> None:
    workflow = _compile(build, sentence, "staged").workflow
    enter = workflow["nodes"]["promise_done"]["on_enter"]
    assert enter["type"] == "llm"  # a tts line cannot fill a value from the conversation
    prompt = enter["prompt"]
    assert sentence in prompt  # the sentence is kept whole, placeholders included, as the template the model fills
    for name in _names(sentence):
        assert prompt.count(name) >= 2  # in the sentence and in the instruction that says what to put there
    assert "recorded" in prompt and "never" in prompt.lower()
    assert "entry line" in workflow["nodes"]["promise_done"]["task_message"]


@pytest.mark.parametrize("profile", ["staged", "lean"])
@pytest.mark.parametrize(("build", "sentence"), CASES)
def test_the_negotiation_prompt_says_how_to_fill_the_sentence(build: Any, sentence: str, profile: str) -> None:
    workflow = _compile(build, sentence, profile).workflow
    task = workflow["nodes"]["negotiate"]["task_message"]
    assert f'say exactly: "{sentence}"' in task
    after = task.split(f'say exactly: "{sentence}"', 1)[1]
    note = after.split("\n", 1)[0]
    for name in _names(sentence):
        assert name in note


@pytest.mark.parametrize("profile", ["staged", "lean"])
@pytest.mark.parametrize(("build", "sentence"), CASES)
def test_no_placeholder_is_spoken_and_nothing_is_dropped(build: Any, sentence: str, profile: str) -> None:
    compiled = _compile(build, sentence, profile)
    for line in _spoken(compiled.workflow):
        assert not PLACEHOLDER.search(line), line
    assert not [n for n in compiled.notes if "left out" in n and "promise_done" in n]  # the old removal note


def test_a_sentence_without_placeholders_is_still_a_fixed_tts_line() -> None:
    workflow = _compile(_en, "Thank you, your promise is noted.", "staged").workflow
    enter = workflow["nodes"]["promise_done"]["on_enter"]
    assert enter["type"] == "tts" and "Thank you, your promise is noted." in enter["text"]


@pytest.mark.parametrize("profile", ["staged", "lean"])
def test_a_confirmation_question_with_placeholders_says_how_to_fill_them(profile: str) -> None:
    ir, ledger = _tr("")
    ir = ir.model_copy(update={"confirmation": ir.confirmation.model_copy(
        update={"question": "{tutar} TL tutarındaki ödemeyi {tarih} tarihinde yapacağınızı onaylıyor musunuz?"})})
    task = compile_workflow(ir, _tr_api(), ledger, profile=profile).workflow["nodes"]["negotiate"]["task_message"]
    rule = next(line for line in task.split("\n") if "onaylıyor musunuz?" in line and "ask exactly" in line)
    assert "{tutar} and {tarih} stand for values of this call" in rule


def test_a_placed_fixed_sentence_with_placeholders_carries_the_fill_note() -> None:
    from builder.compiler.phrasing import unspoken_phrases

    ir, ledger = _tr("")
    ir.phrases.extra["promise_summary"] = "Toplam {tutar} TL, son tarih {tarih}."
    found = [p for p in unspoken_phrases(ir, ledger, [], ["ad"]) if p.source == "phrases.extra.promise_summary"]
    assert found and "{tutar} and {tarih} stand for values of this call" in found[0].line
    assert '"Toplam {tutar} TL, son tarih {tarih}."' in found[0].line
