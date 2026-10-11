"""Tests for builder.compiler.codegen: the generated code-node snippets are executed in the sandbox.

* the snippets follow the platform's code rules (compile, no double braces, no imports beyond json/re/datetime);
* ``setup`` / ``account_facts`` give the dates, deadlines, floors and sentences the policy modules compute, for the
  practice cases' products (now = 2026-10-14T11:00);
* ``check_callback`` / ``check_promise`` agree with ``builder.policy.calendar`` and ``builder.policy.money``;
* ``decide`` gives every practice case's expected outcome and agrees with the reference engine
  ``builder.policy.outcomes.compute_outcome`` on a grid of call situations (both fixture IRs).
"""

from __future__ import annotations

import datetime as dt
import itertools
import json
import re
from pathlib import Path
from typing import Any

import pytest

from builder.checks.code_sandbox import SnippetError, run_snippet
from builder.compiler import codegen
from builder.policy.calendar import (
    BusinessCalendar,
    check_callback,
    check_promise_date,
    deadline_for,
    resolve_proposal_date,
    spoken_date,
)
from builder.policy.ir import PolicyIR
from builder.policy.money import check_promise_amount
from builder.policy.outcomes import CallSituation, NoteRecord, PromiseRecord, compute_outcome

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "fixtures" / "policy"
NOW = "2026-10-14T11:00"
TODAY = dt.date(2026, 10, 14)


def _load_ir(name: str) -> PolicyIR:
    return PolicyIR.model_validate(json.loads((FIXTURES / name).read_text(encoding="utf-8")))


IR_A = _load_ir("brief_a_ir.json")
IR_AB = _load_ir("brief_ab_ir.json")
CASES: dict[str, dict[str, Any]] = {
    c["id"]: c for c in json.loads((REPO / "public-test-set.json").read_text(encoding="utf-8"))
}
CAL = BusinessCalendar.from_ir(IR_A.calendar)


def _ir_for(case: dict[str, Any]) -> PolicyIR:
    return IR_A if case["brief"] == "A" else IR_AB


def _products(case: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"product_id": f"p{i + 1}", **product} for i, product in enumerate(case["products"])]


def _block(facts: str, pid: str) -> str:
    """The part of the account facts text that belongs to product ``pid``."""
    blocks = re.split(r"\n(?=p\d+: )", facts)
    return next(block for block in blocks if block.startswith(f"{pid}: "))


def _facts(ir: PolicyIR, case: dict[str, Any] | None = None, now: str = NOW) -> dict[str, Any]:
    """Variables after setup and account_facts for a practice case (or after setup only)."""
    state = run_snippet(codegen.setup_snippet(ir), {"now": now})
    if case is not None:
        state = run_snippet(codegen.account_facts_snippet(ir), {**state, "products": _products(case)})
    return state


# --------------------------------------------------------------------------------------------------------------------
# Code rules
# --------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("ir", [IR_A, IR_AB], ids=["A", "AB"])
def test_every_snippet_follows_the_code_rules(ir: PolicyIR) -> None:
    snippets = {
        "setup": codegen.setup_snippet(ir),
        "account": codegen.account_facts_snippet(ir),
        "cb": codegen.check_callback_snippet(ir),
        "promise": codegen.check_promise_snippet(ir),
        "decide": codegen.decide_outcome_snippet(ir),
        "choice": codegen.set_choice_snippet("flat_refusal"),
        "fix": codegen.promise_fix_snippet(),
    }
    for name, code in snippets.items():
        assert "{{" not in code and "}}" not in code, name
        compile(code, name, "exec")
        imports = {line.split()[1] for line in code.splitlines() if line.startswith("import ")}
        assert imports <= {"json", "re", "datetime"}, (name, imports)
        assert code.endswith("\n")


def test_ir_data_with_braces_never_reaches_the_source_as_braces() -> None:
    ir = IR_A.model_copy(deep=True)
    ir.disclosure.product_naming["credit_card"] = "card {card_last4} {{odd}}"
    ir.amounts.floor_note = "Rest {stays} overdue }}"
    for code in (codegen.account_facts_snippet(ir), codegen.decide_outcome_snippet(ir)):
        assert "{{" not in code and "}}" not in code
    state = run_snippet(codegen.account_facts_snippet(ir), {**_facts(ir), "products": _products(CASES["PUB-01"])})
    assert "card 4821 {{odd}}" in state["account_facts"]
    assert "Rest {stays} overdue }}" in state["account_facts"]


# --------------------------------------------------------------------------------------------------------------------
# setup
# --------------------------------------------------------------------------------------------------------------------


def test_setup_today_facts_and_deadlines() -> None:
    state = _facts(IR_A)
    assert state["setup_state"] == "ok"
    assert state["today_iso"] == "2026-10-14"
    assert state["today_spoken"] == "Wednesday 14 October 2026"
    assert state["weekday_name"] == "Wednesday"
    assert state["now_time"] == "11:00"
    assert state["today_is_business_day"] == "yes"
    assert state["next_business_day_iso"] == "2026-10-15"
    assert state["deadline_early"] == "2026-11-02"  # 12th business day, the 26 Oct holiday skipped
    assert state["deadline_late"] == "2026-10-23"  # 7th business day
    assert state["attempt1_date"] == "2026-10-16"  # last business day of this week
    assert state["attempt1_spoken"] == "Friday 16 October 2026"


def test_setup_date_table_labels_weeks_weekends_and_holidays() -> None:
    lines = _facts(IR_A)["date_table"].splitlines()
    by_date = {line.split(" = ")[0]: line for line in lines}
    assert by_date["2026-10-14"].endswith("[this week] business day") and "(today)" in by_date["2026-10-14"]
    assert "(tomorrow)" in by_date["2026-10-15"]
    assert "[this week] weekend, not a business day" in by_date["2026-10-17"]
    assert "[next week] business day" in by_date["2026-10-19"]
    assert "[in 2 weeks] BANK HOLIDAY, not a business day" in by_date["2026-10-26"]
    assert len(lines) >= 50
    assert "2026-11-02" in by_date  # the longest deadline is inside the table


def test_setup_callback_facts_follow_the_rule_and_the_clock() -> None:
    facts = _facts(IR_A, now="2026-10-14T11:00")["callback_facts"]
    assert "today Wednesday 14 October 2026 (2026-10-14) from 10:00 to 19:00" in facts
    assert "later than 11:00" in facts
    assert "Thursday 15 October 2026 (2026-10-15)" in facts
    late = _facts(IR_A, now="2026-10-14T19:00")["callback_facts"]
    assert "today" not in late and "Thursday 15 October 2026" in late
    friday_evening = _facts(IR_A, now="2026-10-16T20:00")["callback_facts"]
    assert "Monday 19 October 2026" in friday_evening and "today" not in friday_evening


def test_setup_weekend_call_proposes_the_following_week() -> None:
    state = _facts(IR_A, now="2026-10-17T12:00")  # Saturday
    assert state["today_is_business_day"] == "no"
    assert state["attempt1_date"] == "2026-10-23"
    assert state["deadline_early"] == str(deadline_for(IR_A.bands[0].deadline, dt.date(2026, 10, 17), CAL))


@pytest.mark.parametrize("now", ["", "garbage", "2026-10-14", "2026-10-14T11:00:30Z", "2026-10-14 11:00"])
def test_setup_survives_odd_now_values(now: str) -> None:
    state = run_snippet(codegen.setup_snippet(IR_A), {"now": now})
    assert state["date_table"]
    assert state["setup_state"] in ("ok", "now_missing")


def test_setup_without_now_uses_the_clock_and_says_so() -> None:
    state = run_snippet(codegen.setup_snippet(IR_A), {})
    assert state["setup_state"] == "now_missing"
    assert state["date_table"]


# --------------------------------------------------------------------------------------------------------------------
# account facts
# --------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("case_id", sorted(CASES))
def test_account_facts_match_the_policy_modules(case_id: str) -> None:
    case = CASES[case_id]
    ir = _ir_for(case)
    state = _facts(ir, case)
    assert state["account_state"] == "ok"
    assert state["identity_verified"] == "yes"
    assert state["product_ids"] == ",".join(f"p{i + 1}" for i in range(len(case["products"])))
    assert state["product_count"] == str(len(case["products"]))
    cal = BusinessCalendar.from_ir(ir.calendar)
    for index, product in enumerate(case["products"]):
        pid = f"p{index + 1}"
        band = ir.band_for(product["days_late"])
        assert band is not None
        deadline = deadline_for(band.deadline, TODAY, cal)
        floor_field = ir.amounts.floor_field(product["type"])
        assert state[f"{pid}_deadline"] == deadline.isoformat()
        assert state[f"{pid}_floor"] == f"{product[floor_field]:.2f}"
        assert state[f"{pid}_past_due"] == f"{product['past_due']:.2f}"
        assert state[f"{pid}_band"] == band.name
        block = _block(state["account_facts"], pid)
        for day in cal.list_business_days(TODAY, deadline):
            assert day.isoformat() in block
        assert f"EXACTLY {band.persuasion_attempts} ({band.name} band)" in block
        assert block.count(" - ask: ") == band.persuasion_attempts
        offer = (
            f"I can offer you the {floor_field.replace('_', ' ')} of {product[floor_field]:.2f} instead. "
            f"{ir.amounts.floor_note}"
        )
        assert f'say exactly: "{offer}"' in block


def test_account_facts_allowed_dates_stop_at_the_deadline() -> None:
    state = _facts(IR_A, CASES["PUB-04"])  # late band: 7th business day = 2026-10-23
    block = state["account_facts"]
    assert "2026-10-23 Friday" in block
    assert "2026-10-24" not in block and "2026-10-27" not in block


def test_account_facts_attempt_scripts_use_the_ir_steps() -> None:
    late = _facts(IR_A, CASES["PUB-04"])["account_facts"]  # car loan, 14 days late -> 2 attempts
    friday = spoken_date(resolve_proposal_date("last_business_day_next_week_if_today_is_last", TODAY, CAL),
                         with_year=False)
    assert f'Attempt 1 - ask: "Could you pay the 1250.00 for your car loan on {friday}?"' in late
    assert 'Attempt 2 - ask: "Could you pay at least the instalment amount of' in late
    assert "by Friday 23 October?" in late
    assert "After attempt 2 is refused: make NO further offer for p1." in late
    early = _facts(IR_A, CASES["PUB-01"])["account_facts"]
    assert "Attempt 2" not in early and "After attempt 1 is refused" in early


def test_debt_statement_single_and_multiple_products() -> None:
    single = _facts(IR_A, CASES["PUB-01"])["debt_statement"]
    assert single == (
        "Thank you. I am calling from the collections team about a late payment. "
        "Your credit card ending 4821 has a past-due amount of 420.00 and is 6 days late. "
        "Can you make this payment today?"
    )
    multi = _facts(IR_AB, CASES["PUB-11"])["debt_statement"]
    assert multi.count("Your ") == 3
    assert "Your credit card ending" in multi and "Your personal loan has" in multi and "Your car loan has" in multi
    assert multi.endswith("Can you pay all of them today?")


def test_currency_word_is_spoken_after_amounts_only_when_the_brief_has_one() -> None:
    ir = IR_A.model_copy(update={"currency_word": "dollars"})
    state = _facts(ir, CASES["PUB-01"])
    assert "420.00 dollars" in state["debt_statement"]
    assert "126.00 dollars instead" in state["account_facts"]
    assert " dollars" not in _facts(IR_A, CASES["PUB-01"])["debt_statement"]


def test_products_can_arrive_as_json_text_or_flattened_keys() -> None:
    case = CASES["PUB-12"]
    base = _facts(IR_AB)
    expected = run_snippet(codegen.account_facts_snippet(IR_AB), {**base, "products": _products(case)})
    as_text = run_snippet(codegen.account_facts_snippet(IR_AB), {**base, "products": json.dumps(_products(case))})
    flat: dict[str, Any] = dict(base)
    for index, product in enumerate(_products(case)):
        for key, value in product.items():
            flat[f"load_account_response.data.products.{index}.{key}"] = value
    from_flat = run_snippet(codegen.account_facts_snippet(IR_AB), flat)
    for state in (as_text, from_flat):
        assert state["account_facts"] == expected["account_facts"]
        assert state["debt_statement"] == expected["debt_statement"]


@pytest.mark.parametrize("products", [None, [], "not json", [1, 2], [{"type": "mortgage", "days_late": "x"}]])
def test_account_facts_never_raises_and_always_sets_the_spoken_line(products: Any) -> None:
    state = run_snippet(codegen.account_facts_snippet(IR_A), {**_facts(IR_A), "products": products})
    assert state["debt_statement"].startswith("Thank you.")
    assert state["account_state"] in ("empty", "ok") or state["account_state"].startswith("error")
    assert state["account_facts"] and "product_count" in state


def test_account_facts_with_an_unreadable_now_and_no_products_still_speaks() -> None:
    state = run_snippet(codegen.account_facts_snippet(IR_A), {"now": "garbage"})
    assert state["account_state"] == "empty"
    assert state["debt_statement"]


def test_unknown_product_type_and_missing_floor_are_handled() -> None:
    products = [{"product_id": "p1", "type": "mortgage", "past_due": 100, "days_late": 3}]
    state = run_snippet(codegen.account_facts_snippet(IR_A), {**_facts(IR_A), "products": products})
    assert state["account_state"] == "ok"
    assert "mortgage" in state["account_facts"]
    assert "Floor: none for this product" in state["account_facts"]
    assert state["p1_floor"] == ""
    assert "Floor offer" not in state["account_facts"]


def test_custom_deadline_rule_prints_the_rule_text() -> None:
    ir = IR_A.model_copy(deep=True)
    ir.bands[0].deadline = ir.bands[0].deadline.model_copy(
        update={"kind": "custom", "n": None, "custom_rule_text": "the Friday after the salary date"}
    )
    state = _facts(ir, CASES["PUB-01"])
    assert "apply this rule: the Friday after the salary date" in state["account_facts"]
    assert state["p1_deadline"] == ""


@pytest.mark.parametrize(
    "rule",
    [
        {"kind": "nth_calendar_day_after_today", "n": 10},
        {"kind": "end_of_month"},
        {"kind": "fixed_date", "fixed": dt.date(2026, 10, 30)},
    ],
)
def test_other_deadline_kinds_agree_with_the_policy_module(rule: dict[str, Any]) -> None:
    ir = IR_A.model_copy(deep=True)
    ir.bands[0].deadline = ir.bands[0].deadline.model_copy(update=rule)
    state = _facts(ir, CASES["PUB-01"])
    raw = deadline_for(ir.bands[0].deadline, TODAY, CAL)
    assert state["p1_deadline"] == CAL.previous_business_day(raw, include_self=True).isoformat()


# --------------------------------------------------------------------------------------------------------------------
# callback and promise checks
# --------------------------------------------------------------------------------------------------------------------

CALLBACK_GRID = list(
    itertools.product(
        ["2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16", "2026-10-17", "2026-10-19"],
        ["09:59", "10:00", "11:00", "11:01", "18:59", "19:00", "19:01", "10.30", "9:15"],
    )
)


@pytest.mark.parametrize("now", ["2026-10-14T11:00", "2026-10-14T19:30", "2026-10-16T09:00"])
def test_check_callback_agrees_with_the_policy_module(now: str) -> None:
    rule = IR_A.third_party.callback
    assert rule is not None
    code = codegen.check_callback_snippet(IR_A)
    for day, time in CALLBACK_GRID:
        got = run_snippet(code, {"now": now, "chk_cb_date": day, "chk_cb_time": time})["callback_check"]
        hh, mm = time.replace(".", ":").split(":")
        expected = check_callback(day, f"{int(hh):02d}:{mm}", now, CAL, rule).status
        assert got == expected, (now, day, time)


def test_check_callback_unreadable_and_custom() -> None:
    code = codegen.check_callback_snippet(IR_A)
    out = run_snippet(code, {"now": NOW, "chk_cb_date": "tomorrow", "chk_cb_time": "11:00"})
    assert out["callback_check"] == "unreadable"
    ir = IR_A.model_copy(deep=True)
    assert ir.third_party.callback is not None
    ir.third_party.callback.allowed_days = "custom"
    out = run_snippet(codegen.check_callback_snippet(ir),
                      {"now": NOW, "chk_cb_date": "2026-10-15", "chk_cb_time": "11:00"})
    assert out["callback_check"] == "unchecked"


DATE_GRID = ["2026-10-13", "2026-10-14", "2026-10-17", "2026-10-18", "2026-10-23", "2026-10-24", "2026-10-26",
             "2026-10-27", "2026-11-02", "2026-11-03"]


@pytest.mark.parametrize("date", DATE_GRID)
def test_check_promise_date_agrees_with_the_policy_module(date: str) -> None:
    state = _facts(IR_A, CASES["PUB-04"])  # late band: deadline 2026-10-23
    got = run_snippet(codegen.check_promise_snippet(IR_A),
                      {**state, "chk_product_id": "p1", "chk_amount": "1250", "chk_date": date})
    expected = check_promise_date(date, TODAY, CAL, IR_A.promise_dates, deadline=state["p1_deadline"])
    assert got["promise_date_check"] == expected.status, date
    assert got["promise_suggestion"] == (expected.suggestion.isoformat() if expected.suggestion else ""), date


AMOUNT_GRID: list[Any] = [
    1250.0, 1250.01, 1000, 417.0, 416.99, 400, 399.99, 399.995, "399.994", 300, 0, -5, "1,250.00", "$900", "1250.004",
    "1250.005", "1,250.005", "400,00", "637,50", "420 dollars", "lots", "",
]


@pytest.mark.parametrize("amount", AMOUNT_GRID, ids=repr)
def test_check_promise_amount_agrees_with_the_policy_module(amount: Any) -> None:
    """The expectation comes from the Decimal helper on the RAW input: no float / comma stripping is shared with the code."""
    state = _facts(IR_A, CASES["PUB-04"])  # full 1250.00, floor 400.00 (the instalment amount)
    product = CASES["PUB-04"]["products"][0]
    try:
        expected = check_promise_amount(amount, product["past_due"], product["instalment_amount"], IR_A.amounts)
    except ValueError:
        expected = "unreadable"
    for given in (amount, str(amount)):
        got = run_snippet(codegen.check_promise_snippet(IR_A),
                          {**state, "chk_product_id": "p1", "chk_amount": given, "chk_date": "2026-10-20"})
        assert got["promise_amount_check"] == expected, (amount, given)


@pytest.mark.parametrize(("amount", "verdict"), [
    ("1250.00", "ok"), ("1250.004", "ok"), ("1250.005", "above_full"), ("1250.01", "above_full"),
    ("400", "ok"), ("399.995", "ok"), ("399.994", "below_floor"), ("399.99", "below_floor"), ("637,50", "ok"),
    ("400,00", "ok"), ("1,250.00", "ok"), ("$ 900", "ok"), ("420 dollars", "ok"), ("0", "below_floor"),
    ("-5", "below_floor"), ("lots", "unreadable"),
])
def test_check_promise_amount_literal_verdicts(amount: str, verdict: str) -> None:
    state = _facts(IR_A, CASES["PUB-04"])  # full 1250.00, floor 400.00
    got = run_snippet(codegen.check_promise_snippet(IR_A),
                      {**state, "chk_product_id": "p1", "chk_amount": amount, "chk_date": "2026-10-20"})
    assert got["promise_amount_check"] == verdict


def test_check_promise_half_cent_boundary_matches_the_helper() -> None:
    """Facts that are themselves half-cent values (a full amount 1.005 is 1.01) are compared in whole cents."""
    base = {"now": NOW, "p1_deadline": "2026-10-23", "chk_product_id": "p1", "chk_date": "2026-10-20"}
    code = codegen.check_promise_snippet(IR_A)
    amounts = [0.994, 0.995, 1.004, 1.005, "1.0049", "1.0051", 1.01, 1.0149, 1.015, "1,005", "0.9949999"]
    fulls = [1.01, 1.005, "1.014", 1.015]
    floors: list[Any] = ["", 1.005, 0.995, "0.994", 1.02]
    checked = 0
    for amount, full, floor in itertools.product(amounts, fulls, floors):
        got = run_snippet(code, {**base, "chk_amount": amount, "p1_past_due": full, "p1_floor": floor})
        expected = check_promise_amount(amount, full, None if floor == "" else floor, IR_A.amounts)
        assert got["promise_amount_check"] == expected, (amount, full, floor)
        checked += 1
    assert checked == len(amounts) * len(fulls) * len(floors)
    literal = run_snippet(code, {**base, "chk_amount": 1.005, "p1_past_due": 1.01, "p1_floor": 1.01})
    assert literal["promise_amount_check"] == "ok"  # the audit case: helper ok, the old float code said below_floor


def test_check_promise_unknown_product_and_unreadable_values() -> None:
    state = _facts(IR_A, CASES["PUB-01"])
    code = codegen.check_promise_snippet(IR_A)
    unknown = run_snippet(code, {**state, "chk_product_id": "p9", "chk_amount": "1", "chk_date": "2026-10-14"})
    assert unknown["promise_check"] == "unknown_product"
    out = run_snippet(code, {**state, "chk_product_id": "p1", "chk_amount": "abc", "chk_date": "soon"})
    assert out["promise_check"] == "unreadable"


def test_check_promise_date_verdict_wins_over_amount() -> None:
    state = _facts(IR_A, CASES["PUB-01"])
    out = run_snippet(codegen.check_promise_snippet(IR_A),
                      {**state, "chk_product_id": "p1", "chk_amount": "10", "chk_date": "2026-10-17"})
    assert out["promise_check"] == "not_business_day" and out["promise_amount_check"] == "below_floor"
    assert out["promise_suggestion"] == "2026-10-19"


# --------------------------------------------------------------------------------------------------------------------
# decide: practice cases
# --------------------------------------------------------------------------------------------------------------------

OK = {"identity_verified": "yes"}
PROMISE = {"chosen_end": "promise_recorded", "create_promise_success_count": 1}
CASE_VARS: dict[str, dict[str, Any]] = {
    "PUB-01": {**OK, **PROMISE},
    "PUB-02": {"chosen_end": "callback_booked", "schedule_callback_success_count": 1},
    "PUB-03": {"chosen_end": "identity_failed", "verify_identity_failure_count": 1},
    "PUB-04": {**OK, **PROMISE},
    "PUB-05": {**OK, "chosen_end": "hardship"},
    "PUB-06": {**OK, **PROMISE},
    "PUB-07": {**OK, **PROMISE},
    "PUB-08": {**OK, "chosen_end": "already_paid"},
    "PUB-09": {**OK, "handoff_restructuring_success_count": 1},
    "PUB-10": {**OK, **PROMISE, "create_promise_failure_count": 1},
    "PUB-11": {**OK, **PROMISE, "create_promise_success_count": 3, "product_ids": "p1,p2,p3"},
    "PUB-12": {**OK, **PROMISE, "product_ids": "p1,p2,p3", "notes_log": "p2:refused;p3:refused;"},
    "PUB-13": {**OK, **PROMISE, "create_promise_success_count": 2, "product_ids": "p1,p2"},
}


@pytest.mark.parametrize("case_id", sorted(CASES))
def test_decide_gives_the_expected_outcome_of_every_practice_case(case_id: str) -> None:
    case = CASES[case_id]
    code = codegen.decide_outcome_snippet(_ir_for(case))
    state = run_snippet(code, CASE_VARS[case_id])
    assert state["decide_state"] == "ok"
    assert state["final_outcome"] == case["expected"]["outcome"]
    assert state.get("final_reason") == case["expected"]["reason"]


def test_decide_omits_the_reason_when_the_outcome_has_none() -> None:
    state = run_snippet(codegen.decide_outcome_snippet(IR_A), CASE_VARS["PUB-08"])
    assert state["final_outcome"] == "paid" and "final_reason" not in state


@pytest.mark.parametrize(
    ("variables", "expected"),
    [
        ({**OK, "chosen_end": "auto"}, ("no_promise", "refused")),
        ({**OK}, ("no_promise", "refused")),
        ({"chosen_end": "callback_booked"}, ("not_reached", "no_callback")),  # chosen, but nothing was booked
        ({**OK, "chosen_end": "promise_recorded"}, ("no_promise", "refused")),  # claimed, nothing recorded
        ({"verify_identity_failure_count": 2}, ("not_verified", "declined")),  # customer reached, not verified
    ],
)
def test_decide_fallbacks(variables: dict[str, Any], expected: tuple[str, str]) -> None:
    state = run_snippet(codegen.decide_outcome_snippet(IR_A), variables)
    assert (state["final_outcome"], state.get("final_reason")) == expected


def test_decide_auto_uses_the_extracted_call_situation() -> None:
    code = codegen.decide_outcome_snippet(IR_A)
    for situation, expected in (("already_paid", ("paid", None)), ("dispute", ("no_promise", "dispute")),
                                ("hardship", ("no_promise", "hardship")), ("none", ("no_promise", "refused"))):
        state = run_snippet(code, {**OK, "chosen_end": "auto", "call_situation": situation})
        assert (state["final_outcome"], state.get("final_reason")) == expected


def test_decide_promise_that_failed_twice_is_a_recording_failure() -> None:
    state = run_snippet(codegen.decide_outcome_snippet(IR_A),
                        {**OK, "chosen_end": "promise_recorded", "create_promise_failure_count": 2})
    assert (state["final_outcome"], state["final_reason"]) == ("no_promise", "service_error")


@pytest.mark.parametrize(
    ("failures", "blocks", "expected"),
    [
        (1, 1, ("no_promise", "refused")),  # the only failure was a blocked call: not a service failure
        (2, 2, ("no_promise", "refused")),
        (1, 3, ("no_promise", "refused")),  # more blocks than failures never goes negative
        (2, 1, ("no_promise", "service_error")),  # one real failure besides the block
        (1, 0, ("no_promise", "service_error")),
        (3, "1", ("no_promise", "service_error")),  # the pre-hook writes the counter as text
    ],
)
def test_decide_does_not_count_guard_blocked_calls_as_service_failures(
    failures: int, blocks: Any, expected: tuple[str, str]
) -> None:
    state = run_snippet(codegen.decide_outcome_snippet(IR_A), {
        **OK, "chosen_end": "promise_recorded", "create_promise_failure_count": failures, "promise_guard_blocks": blocks})
    assert (state["final_outcome"], state["final_reason"]) == expected


def test_decide_keeps_a_recorded_promise_whatever_the_guard_blocked() -> None:
    state = run_snippet(codegen.decide_outcome_snippet(IR_A), {
        **OK, **PROMISE, "chosen_end": "promise_recorded", "create_promise_failure_count": 2, "promise_guard_blocks": 2})
    assert (state["final_outcome"], state.get("final_reason")) == ("promise", None)


def test_decide_never_raises_on_garbage() -> None:
    state = run_snippet(codegen.decide_outcome_snippet(IR_A),
                        {"chosen_end": 7, "create_promise_success_count": "x", "notes_log": 5, "product_ids": None})
    assert state["final_outcome"]
    assert state["decide_state"] == "ok" or state["decide_state"].startswith("error")


def test_decide_does_not_treat_latest_arguments_as_an_acknowledged_note() -> None:
    code = codegen.decide_outcome_snippet(IR_AB)
    state = run_snippet(code, {**OK, "chosen_end": "flat_refusal", "add_note_success_count": 1,
                               "add_note_args.reason": "dispute", "add_note_args.product_id": "p1",
                               "product_ids": "p1"})
    assert (state["final_outcome"], state["final_reason"]) == ("no_promise", "refused")


# --------------------------------------------------------------------------------------------------------------------
# decide vs the reference engine
# --------------------------------------------------------------------------------------------------------------------

QUEUES = [None, "support", "general", "restructuring"]
SITUATIONS = [None, "already_paid", "dispute", "hardship", "flat_refusal", "refuse_to_talk"]
# Notes for every product of the call (p1..p3): the reference engine and the generated code agree on these. Notes that
# cover only some products are tested with literal expectations below (a product with no note is not paid).
NOTE_SETS = [[], [("p1", "paid"), ("p2", "paid"), ("p3", "paid")],
             [("p1", "refused"), ("p2", "paid"), ("p3", "paid")],
             [("p2", "dispute"), ("p1", "paid"), ("p3", "refused")],
             [("p3", "hardship"), ("p2", "refused"), ("p1", "refused")]]


def _vars_for(s: CallSituation, chosen: str, queue: str | None, promises: int) -> dict[str, Any]:
    out: dict[str, Any] = {"product_ids": "p1,p2,p3"}
    if s.verified:
        out["identity_verified"] = "yes"
    elif s.party == "confirmed":
        out["verify_identity_failure_count"] = 1
    if chosen:
        out["chosen_end"] = chosen
    if promises:
        out["create_promise_success_count"] = promises
    if s.notes:
        out["notes_log"] = "".join(f"{n.product_id}:{n.reason};" for n in s.notes)
    if queue:
        out[f"handoff_{queue}_success_count"] = 1
    if s.callback_booked:
        out["schedule_callback_success_count"] = 1
    return out


def _grid(multi: bool) -> list[tuple[CallSituation, str, str | None, int]]:
    combos = []
    notes_options = NOTE_SETS if multi else [[]]
    for party, verified, failed, declined, service, situation, queue, promises, notes, booked in itertools.product(
        ["confirmed", "third_party", "wrong_number", "deceased", "unclear"],
        [False, True],
        [False, True],
        [False, True],
        [False, True],
        SITUATIONS,
        QUEUES,
        [0, 1],
        notes_options,
        [False, True],
    ):
        if party != "confirmed" and (verified or failed or declined or service or situation or queue or promises
                                     or notes):
            continue
        if party != "third_party" and booked:
            continue
        if party == "confirmed" and (failed or declined) and verified:
            continue
        chosen_candidates = [
            {"wrong_number": "wrong_number", "deceased": "deceased", "unclear": "unclear_party"}.get(party, ""),
            "identity_failed" if failed else "",
            "identity_declined" if declined else "",
            "recording_failed" if service else "",
            situation or "",
        ]
        chosen_candidates = [c for c in chosen_candidates if c]
        if party == "third_party":
            chosen_candidates.append("callback_booked" if booked else "callback_not_booked")
        if len(chosen_candidates) > 1:
            continue
        chosen = chosen_candidates[0] if chosen_candidates else ""
        s = CallSituation(
            party=party,  # type: ignore[arg-type]
            verified=verified,
            identity_failed=failed,
            identity_declined=declined,
            service_error=service,
            transfer_queue=queue,
            situations={situation} if situation else set(),
            callback_booked=booked,
            promises=[PromiseRecord(f"p{i + 1}", 100.0, "2026-10-15") for i in range(promises)],
            notes=[NoteRecord(pid, reason) for pid, reason in notes],
            product_order=["p1", "p2", "p3"],
        )
        combos.append((s, chosen, queue, promises))
    return combos


@pytest.mark.parametrize(("ir", "multi"), [(IR_A, False), (IR_AB, True)], ids=["A", "AB"])
def test_decide_agrees_with_the_reference_engine(ir: PolicyIR, multi: bool) -> None:
    code = codegen.decide_outcome_snippet(ir)
    combos = _grid(multi)
    assert len(combos) > 100
    step = max(1, len(combos) // 700)
    checked = 0
    for s, chosen, queue, promises in combos[::step]:
        expected = compute_outcome(ir, s)
        state = run_snippet(code, _vars_for(s, chosen, queue, promises))
        got = (state["final_outcome"], state.get("final_reason"))
        assert got == expected.as_tuple(), (s, chosen, queue, promises)
        checked += 1
    assert checked > 100


def test_decide_snippet_for_the_generic_example_ir() -> None:
    """Outcome AND reason, as literals, for every row of a policy that is not Brief A or B."""
    ir = PolicyIR.example_minimal()
    code = codegen.decide_outcome_snippet(ir)
    verified = {
        "unclear_party": ("not_contacted", "unclear"),
        "callback_not_booked": ("not_contacted", "callback_missing"),
        "callback_booked": ("not_contacted", "callback_missing"),  # chosen, but nothing was booked
        "identity_failed": ("not_identified", "wrong_answers"),
        "identity_declined": ("not_identified", "would_not_answer"),
        "flat_refusal": ("not_agreed", "declined_offer"),
        "wrong_number": ("not_agreed", "declined_offer"),  # no row and no section outcome: the verified fallback
        "promise_recorded": ("not_agreed", "declined_offer"),  # claimed, nothing recorded: the verified fallback
        "auto": ("not_agreed", "declined_offer"),
        "": ("not_agreed", "declined_offer"),
    }
    for chosen, outcome in verified.items():
        state = run_snippet(code, {"chosen_end": chosen, "identity_verified": "yes"})
        assert state["decide_state"] == "ok", chosen
        assert (state["final_outcome"], state.get("final_reason")) == outcome, chosen
    unverified = {
        "wrong_number": ("not_contacted", "unclear"),  # no wrong-number row: the unreached fallback
        "deceased": ("not_contacted", "unclear"),
        "unclear_party": ("not_contacted", "unclear"),
        "callback_not_booked": ("not_contacted", "callback_missing"),
        "identity_failed": ("not_identified", "wrong_answers"),
    }
    for chosen, outcome in unverified.items():
        state = run_snippet(code, {"chosen_end": chosen})
        assert state["decide_state"] == "ok", chosen
        assert (state["final_outcome"], state.get("final_reason")) == outcome, chosen
    promised = run_snippet(code, {"chosen_end": "promise_recorded", "create_promise_success_count": 1})
    assert (promised["final_outcome"], promised.get("final_reason")) == ("agreed", None)
    transferred = run_snippet(code, {"create_promise_success_count": 1, "handoff_people_success_count": 1})
    assert (transferred["final_outcome"], transferred.get("final_reason")) == ("handed_over", "person_requested")
    booked = run_snippet(code, {"chosen_end": "callback_booked", "schedule_callback_success_count": 1})
    assert (booked["final_outcome"], booked.get("final_reason")) == ("not_contacted", "callback_set")


NOTES_VARS = {"identity_verified": "yes", "product_ids": "p1,p2,p3", "chosen_end": "flat_refusal"}


@pytest.mark.parametrize(
    ("notes_log", "expected"),
    [
        ("p1:paid;p2:paid;p3:paid;", ("paid", None)),
        ("p3:paid;p1:paid;p2:paid;", ("paid", None)),
        ("p1:paid;p2:paid;", ("no_promise", "refused")),  # p3 has no note: not paid, fall back to the chosen situation
        ("p1:paid;", ("no_promise", "refused")),
        ("p1:paid;p2:refused;", ("no_promise", "refused")),
        ("p1:paid;p3:dispute;", ("no_promise", "dispute")),  # a non-paid note decides even when p2 has none
        ("p3:dispute;p1:paid;p2:refused;", ("no_promise", "refused")),  # first non-paid in product order is p2
        ("p2:hardship;p3:refused;p1:paid;", ("no_promise", "hardship")),
    ],
)
def test_paid_needs_a_paid_note_for_every_product(notes_log: str, expected: tuple[str, str | None]) -> None:
    state = run_snippet(codegen.decide_outcome_snippet(IR_AB), {**NOTES_VARS, "notes_log": notes_log})
    assert (state["final_outcome"], state.get("final_reason")) == expected


def test_paid_without_a_product_list_uses_the_noted_products() -> None:
    state = run_snippet(codegen.decide_outcome_snippet(IR_AB),
                        {"identity_verified": "yes", "notes_log": "p1:paid;", "chosen_end": "flat_refusal"})
    assert (state["final_outcome"], state.get("final_reason")) == ("paid", None)


def test_a_call_ended_by_the_turn_cap_before_verification() -> None:
    code = codegen.decide_outcome_snippet(IR_A, left_variable="verify_identity_response.data.wrong_answers_left")
    failing = run_snippet(code, {"chosen_end": "auto", "verify_identity_response.data.wrong_answers_left": 0})
    assert (failing["final_outcome"], failing["final_reason"]) == ("not_verified", "failed")
    for left in (None, 2, "2"):
        variables: dict[str, Any] = {"chosen_end": "auto"}
        if left is not None:
            variables["verify_identity_response.data.wrong_answers_left"] = left
        declined = run_snippet(code, variables)
        assert (declined["final_outcome"], declined["final_reason"]) == ("not_verified", "declined")


def test_the_sandbox_reports_errors_as_snippet_errors() -> None:
    with pytest.raises(SnippetError):
        run_snippet("raise ValueError('boom')", {})
