"""Persuasion-ladder dates and the amount / days fields in the generated ``account_facts`` node (audit U-01, U-03).

* every persuasion date kind of the IR is rendered with the date ``builder.policy.calendar.resolve_proposal_date``
  gives - a kind the node did not know used to fall into the "last business day of the week" branch;
* ``customer`` / ``custom`` steps carry the documents' own date rule instead of a made-up date;
* the past-due amount and the days late are read from the IR's explicit ``amount_field`` / ``days_field``, not guessed
  from field names of one particular API.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path

import pytest

from builder.checks.code_sandbox import run_snippet
from builder.compiler import codegen
from builder.policy.calendar import BusinessCalendar, deadline_for, resolve_proposal_date
from builder.policy.ir import DeadlineRule, PersuasionStep, PolicyIR

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "fixtures" / "policy"
IR_A = PolicyIR.model_validate(json.loads((FIXTURES / "brief_a_ir.json").read_text(encoding="utf-8")))
PRODUCT = {"product_id": "p1", "type": "credit_card", "card_last4": "1234", "past_due": 100, "days_late": 5,
           "minimum_payment": 30}

NOWS = ["2026-10-14T11:00", "2026-10-23T10:00", "2026-10-24T10:00", "2026-12-23T10:00"]
KINDS: list[tuple[str, int | None]] = [
    ("today", None),
    ("last_business_day_this_week", None),
    ("last_business_day_next_week_if_today_is_last", None),
    ("next_business_day_after_today", None),
    ("nth_business_day_after_today", 3),
    ("nth_calendar_day_after_today", 2),
    ("nth_calendar_day_after_today", 5),
]


def _spoken(day: dt.date) -> str:
    return f"{day.strftime('%A')} {day.day} {day.strftime('%B')}"


def _ir_with_steps(steps: list[PersuasionStep], deadline: DeadlineRule | None = None) -> PolicyIR:
    ir = IR_A.model_copy(deep=True)
    ir.persuasion.steps = steps
    for band in ir.bands:
        band.persuasion_attempts = len(steps)
        if deadline is not None:
            band.deadline = deadline
    return ir


def _facts(ir: PolicyIR, now: str, product: dict | None = None) -> dict[str, str]:
    state = run_snippet(codegen.setup_snippet(ir), {"now": now})
    return run_snippet(codegen.account_facts_snippet(ir), {**state, "products": [dict(product or PRODUCT)]})


def _attempt_lines(facts: dict[str, str]) -> list[str]:
    return [line for line in facts["account_facts"].split("\n") if re.match(r"\s+Attempt \d+ - ask:", line)]


@pytest.mark.parametrize("now", NOWS)
@pytest.mark.parametrize(("kind", "n"), KINDS)
def test_every_date_kind_matches_the_reference_calendar(kind: str, n: int | None, now: str) -> None:
    ir = _ir_with_steps([PersuasionStep(attempt=1, amount="full", date=kind, n=n)])  # type: ignore[arg-type]
    cal = BusinessCalendar.from_ir(ir.calendar)
    today = dt.date.fromisoformat(now[:10])
    facts = _facts(ir, now)
    deadline = deadline_for(ir.bands[0].deadline, today, cal)
    expected = resolve_proposal_date(kind, today, cal, deadline, n)  # type: ignore[arg-type]
    assert expected is not None
    (line,) = _attempt_lines(facts)
    assert f" on {_spoken(expected)}?" in line


def test_date_is_capped_at_the_products_deadline() -> None:
    deadline = DeadlineRule(kind="nth_business_day_after_today", n=2)
    ir = _ir_with_steps([PersuasionStep(attempt=1, amount="full", date="nth_business_day_after_today", n=9)], deadline)
    cal = BusinessCalendar.from_ir(ir.calendar)
    now = "2026-10-14T11:00"
    facts = _facts(ir, now)
    cap = cal.previous_business_day(deadline_for(deadline, dt.date(2026, 10, 14), cal), include_self=True)
    (line,) = _attempt_lines(facts)
    assert f" on {_spoken(cap)}?" in line


def test_first_attempt_date_variable_follows_the_new_kinds() -> None:
    ir = _ir_with_steps([PersuasionStep(attempt=1, amount="full", date="nth_business_day_after_today", n=3)])
    cal = BusinessCalendar.from_ir(ir.calendar)
    state = run_snippet(codegen.setup_snippet(ir), {"now": "2026-10-22T11:00"})
    assert state["attempt1_date"] == cal.add_business_days(dt.date(2026, 10, 22), 3).isoformat()


def test_customer_and_custom_steps_carry_the_documents_rule() -> None:
    steps = [
        PersuasionStep(attempt=1, amount="full", date="customer", text="Any day the customer names within the deadline."),
        PersuasionStep(attempt=2, amount="floor", date="custom", text="Ask for the first Friday of next month."),
    ]
    ir = _ir_with_steps(steps)
    lines = _attempt_lines(_facts(ir, "2026-10-14T11:00"))
    assert len(lines) == 2
    assert "on the day the customer names" in lines[0]
    assert "Date rule for this attempt: Any day the customer names within the deadline." in lines[0]
    assert "on the day the customer names" not in lines[1]
    assert "Date rule for this attempt: Ask for the first Friday of next month." in lines[1]
    # the rule is an instruction, not a sentence to read out: it stays outside the quoted question
    assert lines[1].index('"', lines[1].index('"') + 1) < lines[1].index("Date rule")


def test_date_rule_text_is_not_added_to_a_computed_date() -> None:
    steps = [PersuasionStep(attempt=1, amount="full", date="today", text="Propose today if it is a business day.")]
    (line,) = _attempt_lines(_facts(_ir_with_steps(steps), "2026-10-14T11:00"))
    assert "Date rule" not in line


def _renamed_fields_ir() -> PolicyIR:
    ir = IR_A.model_copy(deep=True)
    ir.disclosure.state_fields = ["balance_due", "dpd"]
    ir.disclosure.amount_field = "balance_due"
    ir.disclosure.days_field = "dpd"
    return ir


def test_amount_and_days_fields_come_from_the_ir() -> None:
    ir = _renamed_fields_ir()
    product = {"product_id": "p1", "type": "credit_card", "card_last4": "1234", "balance_due": 250, "dpd": 20}
    facts = _facts(ir, "2026-10-14T11:00", product)
    assert facts["p1_past_due"] == "250.00"
    assert facts["p1_band"] == "late"
    assert "20 days late" in facts["debt_statement"]
    assert "250" in facts["debt_statement"]


def test_explicit_fields_win_over_the_kit_names() -> None:
    """A product that also has ``past_due`` / ``days_late`` fields of a different meaning must not be read."""
    ir = _renamed_fields_ir()
    product = {"product_id": "p1", "type": "credit_card", "card_last4": "1234", "balance_due": 250, "dpd": 3,
               "past_due": 999, "days_late": 60}
    facts = _facts(ir, "2026-10-14T11:00", product)
    assert facts["p1_past_due"] == "250.00"
    assert facts["p1_band"] == "early"


def test_fields_are_read_from_state_fields_when_not_explicit() -> None:
    ir = IR_A.model_copy(deep=True)
    ir.disclosure.state_fields = ["amount_overdue", "overdue_days"]
    ir.disclosure.amount_field = None
    ir.disclosure.days_field = None
    product = {"product_id": "p1", "type": "credit_card", "card_last4": "1234", "amount_overdue": 80, "overdue_days": 12}
    facts = _facts(ir, "2026-10-14T11:00", product)
    assert facts["p1_past_due"] == "80.00"
    assert facts["p1_band"] == "late"
