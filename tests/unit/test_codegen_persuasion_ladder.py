"""A persuasion ladder shorter than the allowed attempts is never padded with its last step (audit F19).

The generated ``account_facts`` node used to repeat the final step to fill the allowance, so a customer heard the same
offer twice. Now the node compiles exactly the distinct prescribed steps of the product's band, the stated attempt count is
``min(allowed, prescribed steps)``, and the compiler reports the inconsistent policy in its notes.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from builder.checks.code_sandbox import run_snippet
from builder.compiler import codegen
from builder.compiler.compile import compile_workflow
from builder.ingest.tool_api import load_tool_api
from builder.policy.ir import PersuasionStep, PolicyIR, RuleLedger

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "fixtures" / "policy"
IR_A = PolicyIR.model_validate(json.loads((FIXTURES / "brief_a_ir.json").read_text(encoding="utf-8")))
LEDGER_A = RuleLedger.model_validate(json.loads((FIXTURES / "brief_a_ledger.json").read_text(encoding="utf-8")))
API = load_tool_api(REPO / "tools" / "openapi.json")
EARLY = {"product_id": "p1", "type": "credit_card", "card_last4": "1234", "past_due": 100, "days_late": 5,
         "minimum_payment": 30}
LATE = {**EARLY, "days_late": 40}
NOW = "2026-10-14T11:00"


def _ir(steps: list[PersuasionStep], attempts: dict[str, int]) -> PolicyIR:
    ir = IR_A.model_copy(deep=True)
    ir.persuasion.steps = steps
    for band in ir.bands:
        band.persuasion_attempts = attempts[band.name]
    return ir


def _facts(ir: PolicyIR, product: dict) -> str:
    state = run_snippet(codegen.setup_snippet(ir), {"now": NOW})
    return run_snippet(codegen.account_facts_snippet(ir), {**state, "products": [dict(product)]})["account_facts"]


def _asks(facts: str) -> list[str]:
    return [line.strip() for line in facts.splitlines() if re.match(r"\s+Attempt \d+ - ask:", line)]


ONE_STEP = [PersuasionStep(attempt=1, amount="full", date="today")]
TWO_STEPS = [PersuasionStep(attempt=1, amount="full", date="today"),
             PersuasionStep(attempt=2, amount="floor", date="deadline")]


def test_one_applicable_step_and_two_allowed_attempts_compile_exactly_one_attempt() -> None:
    facts = _facts(_ir(ONE_STEP, {"early": 2, "late": 2}), LATE)
    assert len(_asks(facts)) == 1
    assert "EXACTLY 1 " in facts and "EXACTLY 2" not in facts
    assert "After attempt 1 is refused: make NO further offer for p1." in facts
    assert "Attempt 2" not in facts


def test_no_two_successive_attempts_are_identical_offers() -> None:
    for attempts in (1, 2, 3, 4):
        ir = _ir(TWO_STEPS, {"early": attempts, "late": attempts})
        asks = [re.sub(r"^Attempt \d+ ", "", ask) for ask in _asks(_facts(ir, LATE))]
        assert len(asks) == min(attempts, 2)
        assert len(set(asks)) == len(asks)


def test_distinct_prescribed_steps_stay_intact_and_ordered() -> None:
    steps = [PersuasionStep(attempt=2, amount="floor", date="deadline"),
             PersuasionStep(attempt=1, amount="full", date="today")]
    asks = _asks(_facts(_ir(steps, {"early": 2, "late": 2}), LATE))
    assert len(asks) == 2
    assert asks[0].startswith("Attempt 1 - ask: \"Could you pay the 100.00") and "30.00" not in asks[0]
    assert asks[1].startswith("Attempt 2 - ask: \"Could you pay at least the minimum payment of 30.00")


def test_the_allowance_still_caps_a_longer_ladder() -> None:
    three = [*TWO_STEPS, PersuasionStep(attempt=3, amount="full", date="next_business_day_after_today")]
    facts = _facts(_ir(three, {"early": 1, "late": 2}), LATE)
    assert len(_asks(facts)) == 2 and "EXACTLY 2 " in facts


def test_band_specific_steps_follow_the_product_band() -> None:
    steps = [PersuasionStep(attempt=1, amount="full", date="today"),
             PersuasionStep(attempt=2, bands=["late"], amount="floor", date="deadline")]
    ir = _ir(steps, {"early": 2, "late": 2})
    assert len(_asks(_facts(ir, EARLY))) == 1  # the second step applies to the late band only; no padding
    assert len(_asks(_facts(ir, LATE))) == 2


def test_a_band_without_any_applicable_step_makes_no_offer_at_all() -> None:
    steps = [PersuasionStep(attempt=1, bands=["late"], amount="full", date="today")]
    facts = _facts(_ir(steps, {"early": 1, "late": 1}), EARLY)
    assert _asks(facts) == []
    assert "Persuasion attempts for p1: NONE" in facts
    assert "EXACTLY" not in facts.split("Persuasion attempts for p1")[1].splitlines()[0]
    assert "Attempt 1" not in facts
    # AUD-30: no attempt was ever made, so the facts must not claim the attempts are "used up"
    assert "already used up" not in facts and "no persuasion offer" in facts


@pytest.mark.parametrize("profile", ["staged", "lean"])
def test_the_compiler_reports_a_ladder_shorter_than_the_allowance(profile: str) -> None:
    ir = _ir(ONE_STEP, {"early": 1, "late": 2})
    notes = compile_workflow(ir, API, LEDGER_A, profile=profile).notes
    assert any("persuasion" in note and "late" in note and "2" in note and "1" in note for note in notes), notes
    assert not any("early" in note and "persuasion" in note for note in notes)


@pytest.mark.parametrize("profile", ["staged", "lean"])
def test_a_consistent_ladder_has_no_note(profile: str) -> None:
    notes = compile_workflow(_ir(TWO_STEPS, {"early": 1, "late": 2}), API, LEDGER_A, profile=profile).notes
    assert not [n for n in notes if "persuasion" in n.lower()]


def test_the_brief_a_fixture_keeps_its_two_distinct_late_band_attempts() -> None:
    facts = _facts(IR_A, LATE)
    asks = _asks(facts)
    assert len(asks) == 2 and asks[0] != asks[1]
    assert len(_asks(_facts(IR_A, EARLY))) == 1
