"""AUD-18: the persuasion date kind must match the date rule its own text states.

``builder.understand.date_cues`` reads generic English date vocabulary; the safe fixes apply its correction and the
validator reports any contradiction. Test texts are generic (no brief wording is needed to state a date rule).
"""

from __future__ import annotations

import copy
import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest

from builder.ingest.tool_api import ToolApi
from builder.policy.ir import PolicyIR, RuleLedger
from builder.understand.date_cues import classify_proposal_date, corrected_proposal_date
from builder.understand.fixes import apply_safe_fixes
from builder.understand.validate import validate_policy

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "policy"
COND = "last_business_day_next_week_if_today_is_last"
WEEK = "last_business_day_this_week"
NEXT = "next_business_day_after_today"

SAMPLES: list[tuple[str, str | None, int | None]] = [
    ("Propose the last business day of this week. If today is that day, propose the last business day of next week.",
     COND, None),
    ("Propose the floor amount on the deadline.", "deadline", None),
    ("Propose the full past-due amount, or the floor if the customer already said the full amount is too much, on the "
     "last business day of this week; if today is that day, on the last business day of next week.", COND, None),
    ("Late band only: propose the floor amount on the deadline.", "deadline", None),
    ("Propose the full overdue amount, or the floor if the customer already said the full amount is too much, on the "
     "next business day after today.", NEXT, None),
    ("Propose the floor amount on the next business day after today.", NEXT, None),
    ("Propose the next business day after the call date for the full overdue amount.", NEXT, None),
    ("Propose the last business day of this week.", WEEK, None),
    ("Propose paying tomorrow, or the next business day.", NEXT, None),
    ("Propose the 3rd business day after today.", "nth_business_day_after_today", 3),
    ("Propose the floor in 5 calendar days.", "nth_calendar_day_after_today", 5),
    ("Propose the floor amount on the 2nd business day after the call date.", "nth_business_day_after_today", 2),
    ("Propose paying today.", "today", None),
    ("Propose the floor amount on the latest possible date.", "deadline", None),
    ("Propose the following working day.", NEXT, None),
    ("Propose the end of this week.", WEEK, None),
    ("Propose the 10th calendar day after today.", "nth_calendar_day_after_today", 10),
    ("Propose the second business day after today.", "nth_business_day_after_today", 2),
    ("Propose the first business day after today.", NEXT, None),
    # abstentions: no date rule, several, or a condition the plain kinds cannot express
    ("Ask the customer on which day they can pay the floor amount.", None, None),
    ("Propose the deadline, or the next business day if the deadline is a holiday.", None, None),
    ("Propose today, or the next business day if today is not a business day.", None, None),
    ("Propose the last business day of this week, or next week's if today is a weekend.", None, None),
    ("Propose the next business day, unless that is after the deadline.", None, None),
    ("Propose a date within the next 5 business days chosen by the customer.", None, None),
    ("Propose the last business day of next week.", None, None),
    ("", None, None),
]


@pytest.mark.parametrize(("text", "kind", "n"), SAMPLES)
def test_the_date_kind_is_read_from_generic_date_words(text: str, kind: str | None, n: int | None) -> None:
    found = classify_proposal_date(text)
    assert (found[0] if found else None) == kind
    if found is not None:
        assert found[1] == n


def test_a_custom_kind_and_the_conditional_week_kind_are_never_overridden() -> None:
    assert corrected_proposal_date("custom", None, "Propose the next business day after today.") is None
    assert corrected_proposal_date(COND, None, "Propose the last business day of this week.") is None
    assert corrected_proposal_date(WEEK, None, SAMPLES[0][0]) == (COND, None)


def test_a_customer_kind_is_replaced_only_when_the_text_proposes_a_date() -> None:
    assert corrected_proposal_date("customer", None, "Propose the floor on the next business day after today.") == (
        NEXT, None)
    assert corrected_proposal_date("customer", None, "Ask the customer for a date by the deadline.") is None
    assert corrected_proposal_date("customer", None, "Let the customer pick a day by the following business day.") is None


def test_a_matching_kind_and_n_are_kept() -> None:
    assert corrected_proposal_date("nth_business_day_after_today", 3, "Propose the 3rd business day after today.") is None
    assert corrected_proposal_date("nth_business_day_after_today", 2, "Propose the 3rd business day after today.") == (
        "nth_business_day_after_today", 3)


# --- the validator and the safe fixes -------------------------------------------------------------------------------


def _load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def ledger_a() -> RuleLedger:
    return RuleLedger.model_validate(_load("brief_a_ledger.json"))


def _kind_issues(data: dict[str, Any], ledger: RuleLedger, api: ToolApi) -> list[Any]:
    return [i for i in validate_policy(PolicyIR.model_validate(data), ledger, api) if i.code == "PERSUASION_DATE_KIND"]


def test_a_week_kind_that_drops_its_if_today_condition_is_an_error_and_is_fixed(ledger_a, kit_tool_api) -> None:
    """The live extractor wrote the plain week kind for a text with the "if today is that day" condition."""
    data = _load("brief_a_ir.json")
    data["persuasion"]["steps"][0]["date"] = WEEK
    issues = _kind_issues(data, ledger_a, kit_tool_api)
    assert len(issues) == 1 and issues[0].severity == "error" and COND in issues[0].message
    fixed, log = apply_safe_fixes(PolicyIR.model_validate(data), ledger_a, kit_tool_api)
    assert fixed.persuasion.steps[0].date == COND
    assert any("persuasion" in line and COND in line for line in log)
    assert _kind_issues(fixed.model_dump(mode="json"), ledger_a, kit_tool_api) == []


def test_a_next_business_day_text_with_a_week_kind_is_an_error_and_is_fixed(ledger_a, kit_tool_api) -> None:
    data = _load("brief_a_ir.json")
    data["persuasion"]["steps"][0]["text"] = "Propose the floor amount on the next business day after today."
    issues = _kind_issues(data, ledger_a, kit_tool_api)
    assert len(issues) == 1 and NEXT in issues[0].message
    fixed, _ = apply_safe_fixes(PolicyIR.model_validate(data), ledger_a, kit_tool_api)
    assert fixed.persuasion.steps[0].date == NEXT and fixed.persuasion.steps[0].n is None


def test_an_nth_kind_with_the_wrong_n_is_fixed(ledger_a, kit_tool_api) -> None:
    data = _load("brief_a_ir.json")
    step = data["persuasion"]["steps"][1]
    step.update(date="nth_business_day_after_today", n=2, text="Propose the floor in 3 business days.")
    assert len(_kind_issues(data, ledger_a, kit_tool_api)) == 1
    fixed, _ = apply_safe_fixes(PolicyIR.model_validate(data), ledger_a, kit_tool_api)
    assert (fixed.persuasion.steps[1].date, fixed.persuasion.steps[1].n) == ("nth_business_day_after_today", 3)


@pytest.mark.parametrize("name", ["brief_a", "brief_ab"])
def test_the_kit_fixtures_raise_no_date_kind_issue_and_are_not_changed(name: str, kit_tool_api: ToolApi) -> None:
    data = _load(f"{name}_ir.json")
    ledger = RuleLedger.model_validate(_load(f"{name}_ledger.json"))
    assert _kind_issues(data, ledger, kit_tool_api) == []
    fixed, log = apply_safe_fixes(PolicyIR.model_validate(data), ledger, kit_tool_api)
    assert [s.date for s in fixed.persuasion.steps] == [s["date"] for s in data["persuasion"]["steps"]]
    assert not any("persuasion" in line for line in log)


def test_after_the_fix_the_compiled_facts_propose_next_weeks_day_on_the_last_business_day(ledger_a, kit_tool_api) -> None:
    """End to end: on a Friday the conditional kind proposes the next week's last business day, not today."""
    from builder.checks.code_sandbox import run_snippet
    from builder.compiler import codegen

    data = copy.deepcopy(_load("brief_a_ir.json"))
    data["persuasion"]["steps"][0]["date"] = WEEK
    fixed, _ = apply_safe_fixes(PolicyIR.model_validate(data), ledger_a, kit_tool_api)
    friday = dt.date(2026, 10, 16)
    assert friday.weekday() == 4
    product = {"product_id": "p1", "type": "credit_card", "card_last4": "1234", "past_due": 100, "days_late": 5,
               "minimum_payment": 30}
    state = run_snippet(codegen.setup_snippet(fixed), {"now": f"{friday.isoformat()}T11:00"})
    facts = run_snippet(codegen.account_facts_snippet(fixed), {**state, "products": [product]})
    attempt = next(line for line in facts["account_facts"].splitlines() if "Attempt 1 - ask:" in line)
    assert "Friday 23 October" in attempt and "16 October" not in attempt
