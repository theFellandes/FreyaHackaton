# ruff: noqa: RUF001  (Turkish test sentences: the dotless i is meant)
"""A policy in another language than English is spoken in that language, from the first word to the last.

The compiler's own fixed lines (the debt statement, guard corrections, the callback and promise confirmations, the
rescue goodbyes ...) are built from English templates. In an English call they stay fixed ``tts`` lines (byte-identical
to before); in a call in another language every ``tts`` entry becomes an ``llm`` entry whose prompt asks the model to
say the line in the call's language, keeping every number, amount, date and name, with the original ``{{vars}}`` intact.

The IR used here is the real Brief A IR with invented Turkish sentences; nothing comes from a released case.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from builder.checks.lint import lint_workflow
from builder.compiler.compile import compile_workflow
from builder.compiler.language import is_english, spoken_language
from builder.graph.nodes._common import lint_call_variables
from builder.guardrails import check_workflow
from builder.ingest.tool_api import ToolApi, load_tool_api
from builder.policy.ir import PolicyIR, RuleLedger
from builder.selftest.smoke import USER_TURNS, coverage_of, run_smoke, user_turns
from builder.testing import FakeFreya
from builder.trace import NullTrace

REPO = Path(__file__).resolve().parents[2]
REAL = REPO / "tests" / "fixtures" / "real_ir"
PROFILES = ("staged", "lean")

TURKISH_PHRASES = {
    "opening_sentence": "Merhaba, ben Freya Bank'ın dijital asistanıyım, bu görüşmeyi kaydediyoruz.",
    "right_party_question": "{title} {last_name} ile mi görüşüyorum?",
    "robot_answer": "Ben Freya Bank'ın dijital asistanıyım.",
    "scam_answer": "Bu aramayı Freya Bank müşteri hizmetlerini arayarak doğrulayabilirsiniz.",
    "confirm_question": "Onaylıyor musunuz?",
    "today_question": "Bu ödemeyi bugün yapabilir misiniz?",
    "recorded_sentence": "Bu sözü onaylayan bir kısa mesaj alacaksınız.",
    "identity_failed_sentence": "Lütfen mobil uygulamayı veya bir şubeyi kullanın.",
    "deceased_sentence": "Başınız sağ olsun.",
    "paid_sentence": "Ödemelerin görünmesi bir iş günü sürebilir.",
    "goodbye_sentence": "Teşekkür ederim, iyi günler.",
}
TURKISH_EXTRA = {
    "disclosure_intro": "Gecikmiş bir ödeme için tahsilat ekibinden arıyorum.",
    "floor_offer": "Size {floor_amount} tutarında {floor_label} önerebilirim. Kalanı gecikmede kalır.",
    "below_floor": "Bunu kaydedemiyorum. Size {floor_amount} tutarında {floor_label} önerebilirim.",
    "unknown_answer": "Bu bilgiye sahip değilim.",
    "decline_explanation": "Bu sorular hesabınızı korur.",
}


def _load(name: str = "brief_a") -> tuple[PolicyIR, RuleLedger]:
    ir = PolicyIR.model_validate(json.loads((REAL / f"{name}_ir.json").read_text(encoding="utf-8")))
    ledger = RuleLedger.model_validate(json.loads((REAL / f"{name}_ledger.json").read_text(encoding="utf-8")))
    return ir, ledger


def turkish(ir: PolicyIR, language: str = "tr") -> PolicyIR:
    phrases = ir.phrases.model_copy(update={**TURKISH_PHRASES, "extra": {**ir.phrases.extra, **TURKISH_EXTRA}})
    return ir.model_copy(update={"language": language, "phrases": phrases})


@pytest.fixture(scope="module")
def api() -> ToolApi:
    return load_tool_api(REPO / "tools" / "openapi.json")


def _entries(workflow: dict[str, Any]) -> dict[str, dict[str, Any]]:
    nodes = {**workflow.get("nodes", {}), **(workflow.get("global_nodes") or {})}
    return {name: node["on_enter"] for name, node in nodes.items() if isinstance(node.get("on_enter"), dict)}


def _codes(workflow: dict[str, Any]) -> list[str]:
    found: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key in ("code", "pre_process_code", "post_process_code") and isinstance(item, str):
                    found.append(item)
                else:
                    walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(workflow)
    return found


# ---- B1: the IR's language --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("written", "code"), [("tr", "tr"), ("TR", "tr"), ("tr-TR", "tr"), ("tr_TR", "tr"),
                                               ("Turkish", "tr"), ("Türkçe", "tr"), ("German", "de"), ("en", "en"),
                                               ("", "en"), ("English", "en")])
def test_the_ir_language_is_an_iso_code(written: str, code: str) -> None:
    ir, _ = _load()
    data = json.loads(ir.model_dump_json())
    data["language"] = written
    assert PolicyIR.model_validate(data).language == code


def test_the_spoken_language_falls_back_to_the_fixed_sentences_when_the_ir_says_english() -> None:
    ir, _ = _load()
    assert spoken_language(ir) == "en"
    assert spoken_language(turkish(ir, "en")) == "tr"  # the extraction left the default, the sentences are Turkish
    assert spoken_language(turkish(ir, "tr")) == "tr"
    assert is_english("en") and not is_english("tr")


# ---- B2 / B3: compiled speech ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("profile", PROFILES)
def test_a_turkish_policy_has_no_fixed_english_line(api: ToolApi, profile: str) -> None:
    ir, ledger = _load()
    english = compile_workflow(ir, api, ledger, None, profile).workflow  # type: ignore[arg-type]
    compiled = compile_workflow(turkish(ir), api, ledger, None, profile)  # type: ignore[arg-type]
    workflow = compiled.workflow
    before, after = _entries(english), _entries(workflow)
    assert [name for name, entry in after.items() if entry.get("type") == "tts"] == []
    spoken = [name for name, entry in before.items() if entry.get("type") == "tts"]
    assert spoken, "the English compile speaks fixed lines"
    for name in spoken:
        entry = after[name]
        assert entry["type"] == "llm"
        prompt = entry["prompt"]
        assert "in Turkish" in prompt
        assert "every number, amount, date, name and product" in prompt
        for ref in re.findall(r"\{\{[A-Za-z_][A-Za-z0-9_]*\}\}", before[name]["text"]):
            assert ref in prompt, (name, ref)
    opening = "opening" if profile == "staged" else "call"
    assert TURKISH_PHRASES["opening_sentence"] in after[opening]["prompt"]
    assert "{{title}} {{last_name}} ile mi görüşüyorum?" in after[opening]["prompt"]
    assert "{{debt_statement}}" in after["negotiate"]["prompt"]
    if profile == "staged":
        assert "{{promise_fix_text}}" in after["promise_fix_say"]["prompt"]
        assert "Teşekkür ederim, iyi günler." in after["promise_done"]["prompt"]


@pytest.mark.parametrize("profile", PROFILES)
def test_the_turkish_system_prompt_speaks_turkish_dates_and_amounts(api: ToolApi, profile: str) -> None:
    ir, ledger = _load()
    prompt = compile_workflow(turkish(ir), api, ledger, None, profile).workflow["system_prompt"]  # type: ignore[arg-type]
    assert "Speak Turkish." in prompt
    assert "Speak tr." not in prompt
    assert re.search(r"Say amounts with two decimals.*dates as weekday plus day and month.*in Turkish", prompt)
    assert "say it in Turkish with exactly that meaning" in prompt
    english = compile_workflow(ir, api, ledger, None, profile).workflow["system_prompt"]  # type: ignore[arg-type]
    assert "Speak English." in english
    assert "with exactly that meaning" not in english


@pytest.mark.parametrize("profile", PROFILES)
def test_compiler_made_sentences_in_the_facts_are_meanings_but_brief_sentences_stay_verbatim(
    api: ToolApi, profile: str
) -> None:
    ir, ledger = _load()
    workflow = compile_workflow(turkish(ir), api, ledger, None, profile).workflow  # type: ignore[arg-type]
    facts = workflow["nodes"]["account_facts"]["pre_actions"][0]
    code = json.dumps(facts, ensure_ascii=False)
    assert "ask in Turkish, with exactly this meaning:" in code
    assert "say exactly: \\\"' + offer" in code  # the floor offer is the brief's own (Turkish) sentence
    # without a floor-offer sentence of its own the compiler's English template is a meaning to convey
    bare = turkish(ir)
    bare = bare.model_copy(update={"phrases": bare.phrases.model_copy(update={
        "extra": {k: v for k, v in bare.phrases.extra.items() if k != "floor_offer"}})})
    code = json.dumps(compile_workflow(bare, api, ledger, None, profile).workflow["nodes"]["account_facts"],  # type: ignore[arg-type]
                      ensure_ascii=False)
    assert "say in Turkish, with exactly this meaning: \\\"' + offer" in code
    english = json.dumps(compile_workflow(ir, api, ledger, None, profile).workflow, ensure_ascii=False)  # type: ignore[arg-type]
    assert "with exactly this meaning" not in english


@pytest.mark.parametrize("profile", PROFILES)
def test_a_turkish_compile_lints_guards_and_keeps_code_free_of_placeholders(api: ToolApi, profile: str) -> None:
    ir, ledger = _load()
    tr = turkish(ir)
    workflow = compile_workflow(tr, api, ledger, None, profile).workflow  # type: ignore[arg-type]
    assert [str(f) for f in lint_workflow(workflow, api, lint_call_variables(tr)).errors] == []
    guard = check_workflow(workflow, api, (), tr)
    assert not guard.blocked, guard.blocking_messages()
    codes = _codes(workflow)
    assert codes
    assert [c for c in codes if "{{" in c] == []


@pytest.mark.parametrize("profile", PROFILES)
def test_an_english_policy_keeps_its_fixed_lines(api: ToolApi, profile: str) -> None:
    ir, ledger = _load()
    workflow = compile_workflow(ir, api, ledger, None, profile).workflow  # type: ignore[arg-type]
    entries = _entries(workflow)
    opening = "opening" if profile == "staged" else "call"
    assert entries[opening]["type"] == "tts"
    assert entries["negotiate"] == {"type": "tts", "text": "{{debt_statement}}"}
    assert "in English" not in json.dumps(workflow)


# ---- B4: smoke turns ----------------------------------------------------------------------------------------------------


def test_the_smoke_caller_speaks_the_policy_language() -> None:
    ir, _ = _load()
    assert user_turns(ir) == USER_TURNS
    assert user_turns(None) == USER_TURNS
    assert user_turns(turkish(ir)) == ("Alo?", "Pardon, kiminle görüşüyorum?")
    for code in ("de", "fr", "es"):
        turns = user_turns(ir.model_copy(update={"language": code}))
        assert len(turns) == 2 and turns != USER_TURNS
    assert user_turns(ir.model_copy(update={"language": "ja"})) == USER_TURNS  # no table: English


def test_opening_coverage_counts_words_with_non_ascii_letters() -> None:
    assert coverage_of("ışığı", "merhaba") == 0.0
    assert coverage_of("Görüşmemizi kaydediyoruz", "Merhaba, görüşmemizi kaydediyoruz.") == 1.0
    assert coverage_of("İyi günler", "iyi günler") == 1.0
    assert coverage_of("I'm speaking with Mr Reyes", "Am I speaking with Mr Reyes?") == pytest.approx(0.8)


async def test_the_smoke_conversation_uses_the_turkish_turns() -> None:
    ir, _ = _load()
    tr = turkish(ir)
    trace = NullTrace()
    opening = "Merhaba, ben Freya Bank'ın dijital asistanıyım, bu görüşmeyi kaydediyoruz. Mr Morgan ile mi görüşüyorum?"
    freya = FakeFreya(trace, opening=opening)
    freya.save("s", {"nodes": {}})
    result = await run_smoke(freya, str(freya.saved["s"]["workflow_id"]), tr, trace, datetime(2026, 10, 14, 9, 0),
                             retry_pause_s=0)
    assert [turn["user"] for turn in result["details"]["turns"]][:1] == ["Alo?"]
    assert result["passed"], result["summary"]
