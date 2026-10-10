"""Grounding and coverage of the rule ledger (``builder.understand.grounding``)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from builder.ingest.brief_loader import BriefBundle, BriefDocument, load_briefs
from builder.policy.ir import PolicyIR, RuleEntry, RuleLedger
from builder.understand.grounding import (
    check_grounding,
    check_phrase_grounding,
    ground_ledger,
    normalize_text,
    normalize_with_offsets,
    snap_quotes,
    uncovered_passages,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "policy"


def make_bundle(**files: str) -> BriefBundle:
    """A bundle from ``name=text`` pairs (the dots in file names are written as ``__``)."""
    docs = [
        BriefDocument(path=Path(name.replace("__", ".")), name=name.replace("__", "."), kind="markdown", text=text,
                      sha256="0" * 64)
        for name, text in files.items()
    ]
    return BriefBundle.from_documents(docs)


def rule(rule_id: str, quote: str, source: str = "a.md", **extra: object) -> RuleEntry:
    return RuleEntry(id=rule_id, source=source, quote=quote, category="other", statement=f"do {rule_id}",
                     scope=["global"], **extra)  # type: ignore[arg-type]


SAMPLE = """# Title

1. Say "hello" to the customer, then ask: *Who is this?*
2. A callback is for **today** or the next business day -- from 10:00 to 19:00, both included.

| Outcome | Reason |
|---|---|
| `no_promise` | `refused` |
"""


# --- normalisation ----------------------------------------------------------------------------------------------------


def test_normalisation_ignores_case_quotes_dashes_markup_and_whitespace() -> None:
    lq, rq, apostrophe, em = (chr(code) for code in (0x201C, 0x201D, 0x2019, 0x2014))
    text = f"Say {lq}Hello{rq} {em} it{apostrophe}s  **fine**,\n`ok`\t| yes"
    assert normalize_text(text) == "say \"hello\" - it's fine, ok yes"


def test_normalisation_offsets_map_back_to_the_original() -> None:
    raw = "Intro.\n\n**Bold  rule** here.\n"
    norm = normalize_with_offsets(raw)
    start = norm.text.index("bold rule")
    original_start, original_end = norm.original_span(start, start + len("bold rule"))
    assert raw[original_start:original_end].replace("*", "").split() == ["Bold", "rule"]
    assert len(norm.offsets) == len(norm.text)


# --- grounding --------------------------------------------------------------------------------------------------------


def test_exact_quotes_survive_markup_and_whitespace_differences() -> None:
    bundle = make_bundle(a__md=SAMPLE)
    ledger = RuleLedger(
        rules=[
            rule("R1", 'Say "hello" to the customer, then ask: Who is this?'),
            rule("R2", "A callback is for today or the next business day " + chr(0x2013) + " from 10:00 to 19:00,\nboth included."),
            rule("R3", "no_promise | refused"),
        ]
    )
    assert check_grounding(ledger, bundle) == []
    assert {c.status for c in ground_ledger(ledger, bundle).checks} == {"exact"}


def test_invented_and_empty_quotes_are_reported() -> None:
    bundle = make_bundle(a__md=SAMPLE)
    ledger = RuleLedger(rules=[rule("R1", "Offer a ten percent discount to every customer."), rule("R2", "  ")])
    problems = check_grounding(ledger, bundle)
    assert len(problems) == 2
    assert problems[0].startswith("R1:") and "not found" in problems[0]
    assert problems[1] == "R2: empty quote"


def test_fuzzy_quote_is_accepted_and_snapped_to_the_source_text() -> None:
    bundle = make_bundle(a__md=SAMPLE)
    near = "A callback is for today or the next business day, from 10:00 to 19:00, both included"  # " -- " lost
    ledger = RuleLedger(rules=[rule("R1", near)])
    report = ground_ledger(ledger, bundle)
    assert [c.status for c in report.checks] == ["fuzzy"]
    assert report.checks[0].ratio >= 0.9
    assert check_grounding(ledger, bundle) == []
    snapped, changes = snap_quotes(ledger, bundle)
    assert changes and "snapped" in changes[0]
    assert "-- from 10:00" in snapped.rules[0].quote.replace("**", "")
    assert [c.status for c in ground_ledger(snapped, bundle).checks] == ["exact"]


def test_quote_from_another_file_is_grounded_and_its_source_corrected() -> None:
    bundle = make_bundle(a__md="Alpha rule: always greet the customer politely.", b__md="Beta rule: never interrupt.")
    ledger = RuleLedger(rules=[rule("R1", "Beta rule: never interrupt.", source="a.md")])
    report = ground_ledger(ledger, bundle)
    assert report.checks[0].status == "moved" and report.checks[0].found_in == "b.md"
    assert report.ok
    snapped, changes = snap_quotes(ledger, bundle)
    assert snapped.rules[0].source == "b.md" and "source corrected" in changes[0]


def test_short_quotes_need_an_exact_hit() -> None:
    bundle = make_bundle(a__md="The code is 4821.")
    assert [c.status for c in ground_ledger(RuleLedger(rules=[rule("R1", "code 4812")]), bundle).checks] == ["missing"]


def test_kit_fixture_ledgers_are_fully_grounded_in_the_kit_briefs(brief_a_path: Path, brief_b_path: Path) -> None:
    for name, files in (("brief_a_ledger", [brief_a_path]), ("brief_ab_ledger", [brief_a_path, brief_b_path])):
        ledger = RuleLedger.model_validate(json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8")))
        bundle = load_briefs(files)
        assert check_grounding(ledger, bundle) == [], name
        assert {c.status for c in ground_ledger(ledger, bundle).checks} == {"exact"}


# --- coverage ---------------------------------------------------------------------------------------------------------


def test_uncovered_passages_find_the_line_no_rule_quotes() -> None:
    text = (
        "# Rules\n\n"
        "1. Always greet the customer politely before anything else.\n"
        "2. Never promise a discount to any customer on this call.\n"
        "3. Confirm the callback time twice before ending the conversation.\n"
    )
    bundle = make_bundle(a__md=text)
    ledger = RuleLedger(
        rules=[rule("R1", "Always greet the customer politely before anything else."),
               rule("R2", "Never promise a discount to any customer on this call.")]
    )
    found = uncovered_passages(ledger, bundle)
    assert [p.line for p in found] == [5]
    assert "callback time" in found[0].text and found[0].source == "a.md"


def test_headings_separators_and_short_lines_are_not_worklist_items() -> None:
    text = "# Heading with many words in it\n\n|---|---|\nShort line.\n"
    assert uncovered_passages(RuleLedger(rules=[rule("R1", "unrelated quote about nothing at all")]),
                              make_bundle(a__md=text)) == []


def test_kit_ledger_leaves_only_harmless_passages_uncovered(brief_a_path: Path) -> None:
    ledger = RuleLedger.model_validate(
        json.loads((FIXTURES / "brief_a_ledger.json").read_text(encoding="utf-8"))
    )
    texts = [p.text for p in uncovered_passages(ledger, load_briefs([brief_a_path]))]
    assert all("fictional" in t or "density" in t or "|" in t for t in texts), texts


# --- fixed sentences of the IR ----------------------------------------------------------------------------------------


@pytest.fixture
def example_ir() -> PolicyIR:
    return PolicyIR.example_minimal()


def test_phrase_grounding_accepts_templates_and_flags_invented_sentences(example_ir: PolicyIR) -> None:
    text = (
        "Hello, this is the automated assistant of Example Lender.\n"
        "Am I speaking with Ms Reyes? Is that correct?\n"
        "Could you settle this today? What is your year of birth? The question protects your account.\n"
        "Thank you, goodbye.\n"
    )
    ir = example_ir.model_copy(deep=True)
    problems = check_phrase_grounding(ir, make_bundle(a__md=text))
    assert not any("opening_sentence" in p for p in problems)
    ir.phrases.robot_answer = "Please hold while I fetch a supervisor right away."
    problems = check_phrase_grounding(ir, make_bundle(a__md=text))
    assert any("robot_answer" in p for p in problems)
