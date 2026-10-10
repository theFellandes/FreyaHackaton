"""Lessons of the reference agent (v6-v8) that the compiler reproduces: the hard promise guard, one money contract,
hints on non-business days, complete identity answers, hardship precedence, talkative customers and turn caps.

Every expectation is a literal value, not a comparison between two generated artefacts.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest

from builder.checks.code_sandbox import extract_code_snippets, run_hook, run_snippet
from builder.checks.lint import lint_workflow
from builder.compiler import codegen, hooks
from builder.compiler.compile import compile_workflow
from builder.ingest.tool_api import ToolApi, load_tool_api
from builder.policy.calendar import BusinessCalendar
from builder.policy.ir import PolicyIR, RuleLedger

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "fixtures" / "policy"
TODAY = dt.date(2026, 10, 14)


def _ir(name: str) -> PolicyIR:
    return PolicyIR.model_validate(json.loads((FIXTURES / f"{name}_ir.json").read_text(encoding="utf-8")))


def _ledger(name: str) -> RuleLedger:
    return RuleLedger.model_validate(json.loads((FIXTURES / f"{name}_ledger.json").read_text(encoding="utf-8")))


IR_A, IR_AB = _ir("brief_a"), _ir("brief_ab")
LEDGER_A, LEDGER_AB = _ledger("brief_a"), _ledger("brief_ab")
CASES = {c["id"]: c for c in json.loads((REPO / "public-test-set.json").read_text(encoding="utf-8"))}
CAL = BusinessCalendar.from_ir(IR_A.calendar)


@pytest.fixture(scope="module")
def api() -> ToolApi:
    return load_tool_api(REPO / "tools" / "openapi.json")


def _facts(ir: PolicyIR, case_id: str | None = None) -> dict[str, Any]:
    state = run_snippet(codegen.setup_snippet(ir), {"now": "2026-10-14T11:00"})
    if case_id is not None:
        products = [{"product_id": f"p{i + 1}", **p} for i, p in enumerate(CASES[case_id]["products"])]
        state = run_snippet(codegen.account_facts_snippet(ir), {**state, "products": products})
    return state


# ---- money: one input contract, integer cents -------------------------------------------------------------------------

MONEY_VECTORS = [
    (1.005, 1.01),
    (0.125, 0.13),
    (2125.1, 2125.10),
    ("1,250.00", 1250.0),
    ("637,50", 637.5),
    ("420 dollars", 420.0),
    ("$ 99.999", 100.0),
    ("1.234,56", 1234.56),
    (420, 420.0),
    ("$1,250", 1250.0),
    ("-5,25", -5.25),
    (".5", 0.5),
    ("637,5", 637.5),
    ("0.005", 0.01),
]


@pytest.mark.parametrize(("raw", "expected"), MONEY_VECTORS)
def test_money_hook_vectors(raw: Any, expected: float) -> None:
    code = hooks.pre_hook_coerce({"amount": "money"})
    assert run_hook(code, config={"body": {"amount": raw}})["config"]["body"]["amount"] == expected


def test_money_hook_leaves_unreadable_amounts_alone() -> None:
    code = hooks.pre_hook_coerce({"amount": "money"})
    for raw in ("lots", None, True, "", "1-2", "5-", "1,234.567,89", "٣٤٠"):
        assert run_hook(code, config={"body": {"amount": raw}})["config"]["body"] == {"amount": raw}


@pytest.mark.parametrize(
    ("past_due", "floor", "due_text", "floor_text"),
    [
        (1.005, 0.125, "1.01", "0.13"),
        (2125.1, "637,50", "2125.10", "637.50"),
        ("1,250.00", "420 dollars", "1250.00", "420.00"),
        (99.999, 33.335, "100.00", "33.34"),
    ],
)
def test_account_facts_round_half_up_without_float_arithmetic(past_due: Any, floor: Any, due_text: str,
                                                              floor_text: str) -> None:
    products = [{"product_id": "p1", "type": "credit_card", "card_last4": "1111", "past_due": past_due,
                 "days_late": 3, "minimum_payment": floor}]
    state = run_snippet(codegen.account_facts_snippet(IR_A), {**_facts(IR_A), "products": products})
    assert state["p1_past_due"] == due_text and state["p1_floor"] == floor_text
    assert f"past-due amount of {due_text}" in state["debt_statement"]
    assert f'say exactly: "I can offer you the minimum payment of {floor_text} instead.' in state["account_facts"]


def test_check_promise_uses_the_same_amount_contract() -> None:
    state = _facts(IR_A, "PUB-04")  # full 1250.00
    code = codegen.check_promise_snippet(IR_A)
    verdicts = (("1,250.00", "ok"), ("1250.004", "ok"), ("1250.005", "above_full"), ("$ 900", "ok"),
                ("lots", "unreadable"))
    for amount, verdict in verdicts:
        out = run_snippet(code, {**state, "chk_product_id": "p1", "chk_amount": amount, "chk_date": "2026-10-20"})
        assert out["promise_amount_check"] == verdict, amount
    over = run_snippet(code, {**state, "chk_product_id": "p1", "chk_amount": "1250.01", "chk_date": "2026-10-20"})
    assert over["promise_suggestion"] == "1250.00"


# ---- the hard promise guard ---------------------------------------------------------------------------------------------

FACTS = {
    "product_ids": "p1,p2",
    "allowed_p1": "2026-10-14,2026-10-15,2026-10-16",
    "allowed_p2": "2026-10-14,2026-10-15",
    "p1_floor": "126.00",
    "p1_past_due": "420.00",
    "p2_floor": "",
    "p2_past_due": "300.00",
}


def _guard(body: dict[str, Any], *, accept_between: bool = True, facts: dict[str, Any] | None = None) -> dict[str, Any]:
    code = hooks.join_hooks(
        hooks.pre_hook_coerce({"product_id": "product_id", "amount": "money", "date": "date_iso"}),
        hooks.pre_hook_promise_guard("product_id", "amount", "date", accept_between=accept_between),
    )
    config = {"url": "https://bank.example/create_promise", "body": body}
    return run_hook(code, vars=dict(FACTS if facts is None else facts), config=config)


@pytest.mark.parametrize(
    "body",
    [
        {"product_id": "p1", "amount": 420, "date": "2026-10-14"},
        {"product_id": "1", "amount": "126.00", "date": "2026-10-16"},  # the floor itself, a bare product number
        {"product_id": "p1", "amount": 200.5, "date": "2026-10-15"},
        {"product_id": "p2", "amount": "300 dollars", "date": "2026-10-15"},  # no floor: only the full amount
    ],
)
def test_guard_lets_allowed_promises_through(body: dict[str, Any]) -> None:
    out = _guard(dict(body))
    assert out["vars"]["promise_guard"] == "ok"
    assert out["config"]["url"] == "https://bank.example/create_promise"


@pytest.mark.parametrize(
    ("body", "needle"),
    [
        ({"product_id": "p9", "amount": 100, "date": "2026-10-14"}, "unknown product_id p9"),
        ({"product_id": "p1", "amount": 420, "date": "2026-10-19"}, "not an allowed promise date for p1"),
        ({"product_id": "p1", "amount": 420, "date": "2026-10-17"}, "latest allowed: 2026-10-16"),
        ({"product_id": "p1", "amount": 125.99, "date": "2026-10-14"}, "amount 125.99"),
        ({"product_id": "p1", "amount": 420.01, "date": "2026-10-14"}, "amount 420.01"),
        ({"product_id": "p2", "amount": 100, "date": "2026-10-14"}, "amount 100.00"),
    ],
)
def test_guard_blocks_what_the_rules_forbid(body: dict[str, Any], needle: str) -> None:
    out = _guard(dict(body))
    assert out["vars"]["promise_guard"].startswith("blocked: ") and needle in out["vars"]["promise_guard"]
    assert out["config"]["url"] == ""


@pytest.mark.parametrize(("amount", "blocked"), [
    (126, False), ("126.00", False), (125.995, False),  # 125.995 is 126.00
    ("125.994", True), (125.99, True),
    (420, False), ("420.004", False), ("420.005", True),  # 420.005 is 420.01
    ("1,250", True), ("lots", False),  # an unreadable amount is not this hook's business: the service answers
])
def test_guard_compares_in_whole_cents_with_the_same_amount_contract(amount: Any, blocked: bool) -> None:
    out = _guard({"product_id": "p1", "amount": amount, "date": "2026-10-14"})
    assert (out["config"]["url"] == "") is blocked, amount


def test_guard_with_half_cent_facts() -> None:
    facts = {**FACTS, "p1_floor": "1.005", "p1_past_due": 1.015}  # facts are compared as cents too: 1.01 .. 1.02
    for amount, blocked in ((1.005, False), (1.004, True), (1.015, False), (1.02, False), ("1.0251", True)):
        out = _guard({"product_id": "p1", "amount": amount, "date": "2026-10-14"}, facts=facts)
        assert (out["config"]["url"] == "") is blocked, amount


def test_guard_without_partial_amounts_accepts_only_the_floor_or_the_full_amount() -> None:
    for amount, blocked in ((126, False), (420, False), (200, True)):
        out = _guard({"product_id": "p1", "amount": amount, "date": "2026-10-14"}, accept_between=False)
        assert (out["config"]["url"] == "") is blocked, amount


def test_guard_never_blocks_when_the_facts_are_missing() -> None:
    out = _guard({"product_id": "p1", "amount": 5, "date": "2030-01-01"}, facts={})
    assert out["vars"]["promise_guard"] == "ok" and out["config"]["url"] != ""
    broken = run_hook(hooks.pre_hook_promise_guard("product_id", "amount", "date"), config={"body": "text"})
    assert broken["config"] == {"body": "text"}


def test_post_hook_explains_a_blocked_call() -> None:
    code = hooks.post_hook([(hooks.flag_true("ok"), "RECORDED")], default="FAILED", error_message="SERVICE",
                           blocked_message="BLOCKED")
    assert run_hook(code, result={"status": "error", "message": "URL is required"})["result"]["what_to_do_next"] == "BLOCKED"
    assert run_hook(code, result={"status": "error", "message": "timeout"})["result"]["what_to_do_next"] == "SERVICE"
    recorded = run_hook(code, result={"status": "success", "data": {"ok": True}})["result"]
    assert recorded["data"]["what_to_do_next"] == "RECORDED"


def test_the_compiled_create_promise_tool_guards_and_explains(api: ToolApi) -> None:
    workflow = compile_workflow(IR_AB, api, LEDGER_AB).workflow
    tool = workflow["nodes"]["negotiate"]["tools"][0]
    state = _facts(IR_AB, "PUB-13")
    assert state["allowed_p2"] == ",".join(d.isoformat() for d in CAL.list_business_days(TODAY, dt.date(2026, 10, 23)))
    assert state["allowed_p1"].endswith("2026-11-02") and "2026-10-26" not in state["allowed_p1"]
    late = run_hook(tool["pre_process_code"], vars=state, config={"url": "u", "body": {
        "product_id": "p2", "amount": 900, "date": "2026-10-30"}})
    assert late["config"]["url"] == "" and "not an allowed promise date for p2" in late["vars"]["promise_guard"]
    fine = run_hook(tool["pre_process_code"], vars=state, config={"url": "u", "body": {
        "product_id": "p2", "amount": 900, "date": "2026-10-23"}})
    assert fine["config"]["url"] == "u" and fine["vars"]["promise_guard"] == "ok"
    blocked = run_hook(tool["post_process_code"], result={"status": "error", "message": "URL is required"})["result"]
    assert "not allowed for this product" in blocked["what_to_do_next"]
    assert "offer the deadline for the same amount" in blocked["what_to_do_next"]


# ---- dates --------------------------------------------------------------------------------------------------------------


def test_date_table_names_the_alternative_for_non_business_days() -> None:
    lines = {line.split(" = ")[0]: line for line in _facts(IR_A)["date_table"].splitlines()}
    assert lines["2026-10-17"].endswith(
        "weekend, not a business day -> if the customer names this day, offer Monday 19 October 2026 (2026-10-19) instead")
    assert lines["2026-10-26"].endswith(
        "BANK HOLIDAY, not a business day -> if the customer names this day, offer Tuesday 27 October 2026 (2026-10-27) instead")
    assert "->" not in lines["2026-10-16"]
    previous = IR_A.model_copy(deep=True)
    previous.promise_dates.non_business_day_shift = "previous_business_day"
    table = {line.split(" = ")[0]: line for line in _facts(previous)["date_table"].splitlines()}
    assert "offer Friday 23 October 2026 (2026-10-23) instead" in table["2026-10-24"]
    reject = IR_A.model_copy(deep=True)
    reject.promise_dates.non_business_day_shift = "reject"
    rejected = {line.split(" = ")[0]: line for line in _facts(reject)["date_table"].splitlines()}
    assert "cannot be recorded; ask for another day" in rejected["2026-10-17"]


# ---- decide -------------------------------------------------------------------------------------------------------------


def test_an_unverified_call_ended_by_the_turn_cap_is_failed_or_declined() -> None:
    code = codegen.decide_outcome_snippet(IR_A, left_variable="verify_identity_response.data.wrong_answers_left")
    failing = run_snippet(code, {"chosen_end": "auto", "verify_identity_response.data.wrong_answers_left": 0})
    assert (failing["final_outcome"], failing["final_reason"]) == ("not_verified", "failed")
    for left in (None, 2, "2"):
        variables: dict[str, Any] = {"chosen_end": "auto"}
        if left is not None:
            variables["verify_identity_response.data.wrong_answers_left"] = left
        declined = run_snippet(code, variables)
        assert (declined["final_outcome"], declined["final_reason"]) == ("not_verified", "declined")


# ---- prompts and graph --------------------------------------------------------------------------------------------------


def test_the_prompts_carry_the_lessons(api: ToolApi) -> None:
    workflow = compile_workflow(IR_A, api, LEDGER_A).workflow
    nodes = workflow["nodes"]
    assert 'reply only with a short acknowledgement such as "I understand, go on."' in workflow["system_prompt"]
    assert "act on it right away" in workflow["system_prompt"]
    identity = nodes["identity"]["task_message"]
    assert "Only a COMPLETE answer can be checked" in identity
    assert "date_of_birth: a date needs day, month and year; phone_last4: exactly 4 digits" in identity
    assert "ask only for the missing part and combine both parts" in identity
    assert "Never send a partial answer" in json.dumps(nodes["identity"]["tools"][0])
    negotiate = nodes["negotiate"]["task_message"]
    assert "(the DATE TABLE names it). This is NOT the after-deadline case." in negotiate
    assert "offer it for the SAME amount" in negotiate and "must not lower the amount" in negotiate
    assert "Make this offer only once." in negotiate
    assert "Make no other offer of any kind (no other transfer, plan or discount)" in negotiate
    assert ("it comes before the rules for flat_refusal, restructuring: never make a persuasion attempt and never "
            "transfer the customer to the restructuring team in this call") in negotiate
    assert "A request for a discount or a waiver is not a request for this" in negotiate
    third = nodes["third_party"]["task_message"]
    assert "right away - do not ask them to confirm first" in third


def test_turn_caps_are_generous_and_lead_to_a_computed_end(api: ToolApi) -> None:
    nodes = compile_workflow(IR_A, api, LEDGER_A).workflow["nodes"]
    caps = {name: (node["max_node_turns"], node["max_turn_transition"]) for name, node in nodes.items()
            if "max_node_turns" in node}
    assert caps == {
        "opening": (30, "end_unclear_party"),
        "third_party": (30, "end_callback_not_booked"),
        "identity": (60, "end_auto"),
        "negotiate": (60, "end_auto"),
    }
    decide = extract_code_snippets(compile_workflow(IR_A, api, LEDGER_A).workflow)["decide"]
    assert "verify_identity_response.data.wrong_answers_left" in decide


@pytest.mark.parametrize("profile", ["staged", "lean"])
def test_the_updated_workflows_still_lint_clean(profile: str, api: ToolApi) -> None:
    for ir, ledger in ((IR_A, LEDGER_A), (IR_AB, LEDGER_AB)):
        workflow = compile_workflow(ir, api, ledger, None, profile).workflow  # type: ignore[arg-type]
        report = lint_workflow(workflow, api, set(ir.call_variable_names()))
        assert report.ok, report.format()


# ---- reference v9: opening confirmation and escape exits ------------------------------------------------------------------


def test_the_opening_needs_an_explicit_complete_confirmation(api: ToolApi) -> None:
    opening = compile_workflow(IR_A, api, LEDGER_A).workflow["nodes"]["opening"]["task_message"]
    assert ('A reply that is cut off, or that contains "no" or says who is speaking ("this is her ...", '
            '"this is his ..."), is NOT a confirmation: let the person finish ("I understand, go on.") '
            "before you decide.") in opening
    assert ("- Confirmed (an explicit, complete confirmation only): call transition_to_identity (no text needed)."
            in opening)


@pytest.mark.parametrize("ir_ledger", [(IR_A, LEDGER_A), (IR_AB, LEDGER_AB)], ids=["A", "AB"])
def test_identity_can_leave_to_the_third_party_and_to_the_wrong_number_end(
    api: ToolApi, ir_ledger: tuple[PolicyIR, RuleLedger]
) -> None:
    node = compile_workflow(ir_ledger[0], api, ir_ledger[1]).workflow["nodes"]["identity"]
    exits = {t["target_node"] for t in node["transitions"] if t.get("is_tool_call")}
    assert {"third_party", "end_wrong_number", "end_identity_declined", "handoff_general"} <= exits
    wrong = next(t for t in node["transitions"] if t["target_node"] == "end_wrong_number")
    assert wrong["description"] == "The number does not belong to the customer; you said goodbye."
    task = node["task_message"]
    assert ("If the person says they are not the customer (for example a relative or a colleague), say nothing about "
            "the account, ask no further security question and call transition_to_third_party.") in task
    assert ("The number does not belong to the customer: Apologise for the mistake. Thank them, say goodbye and call "
            "transition_to_end_wrong_number.") in task
    # deceased is only reachable through the third-party step, as in the reference
    assert "end_deceased" not in exits


# ---- reference v10: the date table marks days after a deadline -------------------------------------------------------------


def _table(state: dict[str, Any]) -> dict[str, str]:
    return {line.split(" = ")[0]: line for line in state["date_table"].splitlines()}


def test_the_date_table_marks_every_day_after_the_deadline_for_one_product() -> None:
    table = _table(_facts(IR_A, "PUB-01"))  # early band: the deadline is Monday 2 November
    assert table["2026-11-02"].endswith("[in 3 weeks] business day")
    assert table["2026-11-03"].endswith("business day | AFTER THE DEADLINE - not allowed: offer the deadline instead")
    assert table["2026-11-07"].endswith(
        "-> if the customer names this day, offer Monday 9 November 2026 (2026-11-09) instead"
        " | AFTER THE DEADLINE - not allowed: offer the deadline instead")
    assert "deadline" not in table["2026-10-30"].lower()


def test_the_date_table_names_the_product_whose_deadline_has_passed() -> None:
    table = _table(_facts(IR_AB, "PUB-13"))  # p1 (early) may go to 2 November, p2 (late) only to 23 October
    assert "deadline" not in table["2026-10-23"].lower()
    assert table["2026-10-27"].endswith("business day | after the deadline of p2 - not allowed for p2")
    assert table["2026-11-02"].endswith("business day | after the deadline of p2 - not allowed for p2")
    assert table["2026-11-03"].endswith("business day | AFTER THE DEADLINE - not allowed: offer the deadline instead")


def test_the_date_table_only_says_not_allowed_when_the_policy_does_not_offer_the_deadline() -> None:
    ir = IR_A.model_copy(deep=True)
    ir.promise_dates.after_deadline = "reject"
    table = _table(_facts(ir, "PUB-01"))
    assert table["2026-11-03"].endswith("business day | AFTER THE DEADLINE - not allowed")


def test_the_date_table_is_not_marked_when_promises_may_go_past_the_deadline() -> None:
    ir = IR_A.model_copy(deep=True)
    ir.promise_dates.not_after_deadline = False
    assert "deadline" not in _facts(ir, "PUB-01")["date_table"].lower()


def test_account_facts_write_the_spoken_deadline_and_the_spoken_allowed_days() -> None:
    state = _facts(IR_AB, "PUB-13")
    assert (state["p1_deadline_spoken"], state["p2_deadline_spoken"]) == ("Monday 2 November", "Friday 23 October")
    assert state["allowed_spoken_p2"] == (
        "Wednesday 14 October,Thursday 15 October,Friday 16 October,Monday 19 October,Tuesday 20 October,"
        "Wednesday 21 October,Thursday 22 October,Friday 23 October")
    assert len(state["allowed_spoken_p1"].split(",")) == len(state["allowed_p1"].split(","))


# ---- reference v10: guard recovery -----------------------------------------------------------------------------------------

SAY_FACTS = {
    "product_ids": "p1,p2,p3",
    "allowed_p1": "2026-10-14,2026-10-15,2026-10-16",
    "allowed_spoken_p1": "Wednesday 14 October,Thursday 15 October,Friday 16 October",
    "p1_deadline_spoken": "Friday 16 October",
    "p1_name": "credit card ending 1111",
    "p1_floor": "126.00",
    "p1_past_due": "420.00",
    "allowed_p2": "2026-10-14,2026-10-15",
    "allowed_spoken_p2": "Wednesday 14 October,Thursday 15 October",
    "p2_deadline_spoken": "Thursday 15 October",
    "p2_name": "personal loan",
    "p2_floor": "",
    "p2_past_due": "300.00",
    # a gap in the middle: the weekend 17-18 October
    "allowed_p3": "2026-10-14,2026-10-15,2026-10-16,2026-10-19,2026-10-20",
    "allowed_spoken_p3": "Wednesday 14 October,Thursday 15 October,Friday 16 October,Monday 19 October,Tuesday 20 October",
    "p3_deadline_spoken": "Tuesday 20 October",
    "p3_name": "car loan",
    "p3_floor": "100.00",
    "p3_past_due": "200.00",
}


def _say(body: dict[str, Any], *, facts: dict[str, Any] | None = None, **options: Any) -> dict[str, Any]:
    code = hooks.join_hooks(
        hooks.pre_hook_coerce({"product_id": "product_id", "amount": "money", "date": "date_iso"}),
        hooks.pre_hook_promise_guard("product_id", "amount", "date", **options),
    )
    config = {"url": "https://bank.example/create_promise", "body": body}
    return run_hook(code, vars=dict(SAY_FACTS if facts is None else facts), config=config)


@pytest.mark.parametrize(
    ("body", "options", "expected"),
    [
        # after the deadline: the latest allowed day, the same amount
        ({"product_id": "p1", "amount": 200.5, "date": "2026-10-19"}, {"currency": "dollars"},
         "I am sorry, the latest date I can record for your credit card ending 1111 is Friday 16 October. "
         "Could you pay 200.50 dollars on that day?"),
        # an amount that is not allowed either: the full amount is offered
        ({"product_id": "p1", "amount": 5, "date": "2026-10-20"}, {"currency": "dollars"},
         "I am sorry, the latest date I can record for your credit card ending 1111 is Friday 16 October. "
         "Could you pay 420.00 dollars on that day?"),
        # no currency word in the brief: none is spoken
        ({"product_id": "p2", "amount": 300, "date": "2026-10-21"}, {},
         "I am sorry, the latest date I can record for your personal loan is Thursday 15 October. "
         "Could you pay 300.00 on that day?"),
        # a policy that does not offer the deadline asks for an earlier day
        ({"product_id": "p1", "amount": 200, "date": "2026-10-19"}, {"after_deadline": "reject"},
         "I am sorry, I cannot record a date after Friday 16 October for your credit card ending 1111. "
         "Could you tell me an earlier day?"),
        # a day inside the range that is not allowed: the next allowed day, spoken
        ({"product_id": "p3", "amount": 150, "date": "2026-10-17"}, {},
         "I am sorry, I cannot record that day for your car loan. The next day I can record is Monday 19 October. "
         "Would that work for you?"),
        # a day in the past: the first allowed day
        ({"product_id": "p3", "amount": 150, "date": "2026-10-13"}, {},
         "I am sorry, I cannot record that day for your car loan. The next day I can record is Wednesday 14 October. "
         "Would that work for you?"),
        # previous-business-day policy: the closest earlier allowed day, and the first one when nothing is earlier
        ({"product_id": "p3", "amount": 150, "date": "2026-10-17"}, {"shift": "previous_business_day"},
         "I am sorry, I cannot record that day for your car loan. The closest day I can record is Friday 16 October. "
         "Would that work for you?"),
        ({"product_id": "p3", "amount": 150, "date": "2026-10-13"}, {"shift": "previous_business_day"},
         "I am sorry, I cannot record that day for your car loan. The closest day I can record is Wednesday 14 October. "
         "Would that work for you?"),
        ({"product_id": "p3", "amount": 150, "date": "2026-10-17"}, {"shift": "reject"},
         "I am sorry, I cannot record that day for your car loan. Could you tell me another day?"),
        # an amount outside floor..full
        ({"product_id": "p1", "amount": 5, "date": "2026-10-15"}, {"currency": "dollars"},
         "I am sorry, for your credit card ending 1111 I can record an amount from 126.00 to 420.00 dollars. "
         "Which amount can you pay?"),
        ({"product_id": "p1", "amount": 200, "date": "2026-10-15"}, {"currency": "euros", "accept_between": False},
         "I am sorry, for your credit card ending 1111 I can record only 126.00 or 420.00 euros. "
         "Which one do you prefer?"),
        # a product without a floor: only the full amount
        ({"product_id": "p2", "amount": 100, "date": "2026-10-15"}, {"currency": "dollars"},
         "I am sorry, for your personal loan I can record only the full amount of 300.00 dollars. Can you pay that?"),
        # an unreadable day, an unknown product
        ({"product_id": "p1", "amount": 200, "date": "Friday"}, {},
         "I am sorry, I did not catch the day for your credit card ending 1111. Which day can you pay?"),
        ({"product_id": "p9", "amount": 200, "date": "2026-10-15"}, {},
         "I am sorry, I could not record that. Let me check the details again."),
    ],
)
def test_the_blocked_promise_prepares_the_spoken_correction(
    body: dict[str, Any], options: dict[str, Any], expected: str
) -> None:
    out = _say(dict(body), **options)
    assert out["config"]["url"] == "" and out["vars"]["promise_guard"].startswith("blocked: ")
    assert out["vars"]["promise_guard_say"] == expected


def test_the_correction_falls_back_to_iso_dates_and_a_neutral_name_when_the_spoken_facts_are_missing() -> None:
    facts = {k: v for k, v in SAY_FACTS.items() if "spoken" not in k and not k.endswith("_name")}
    late = _say({"product_id": "p1", "amount": 200, "date": "2026-10-19"}, facts=facts)
    assert late["vars"]["promise_guard_say"] == (
        "I am sorry, the latest date I can record for your this product is 2026-10-16. Could you pay 200.00 on that day?")
    gap = _say({"product_id": "p3", "amount": 150, "date": "2026-10-17"}, facts=facts)
    assert "The next day I can record is 2026-10-19." in gap["vars"]["promise_guard_say"]


def test_the_guard_counts_its_blocks_and_leaves_an_allowed_call_alone() -> None:
    first = _say({"product_id": "p1", "amount": 5, "date": "2026-10-15"})
    assert first["vars"]["promise_guard_blocks"] == "1"
    second = _say({"product_id": "p1", "amount": 5, "date": "2026-10-15"}, facts=first["vars"])
    assert second["vars"]["promise_guard_blocks"] == "2"
    fine = _say({"product_id": "p1", "amount": 200, "date": "2026-10-15"}, facts=second["vars"])
    assert fine["vars"]["promise_guard"] == "ok" and fine["config"]["url"] != ""
    assert fine["vars"]["promise_guard_blocks"] == "2"  # an allowed call changes nothing else
    clean = _say({"product_id": "p1", "amount": 200, "date": "2026-10-15"})
    assert "promise_guard_blocks" not in clean["vars"] and "promise_guard_say" not in clean["vars"]


def test_the_compiled_workflow_routes_a_blocked_promise_to_a_spoken_fix(api: ToolApi) -> None:
    nodes = compile_workflow(IR_AB, api, LEDGER_AB).workflow["nodes"]
    negotiate = nodes["negotiate"]
    first = negotiate["transitions"][0]
    assert first["target_node"] == "promise_fix"
    assert first["description"] == "The promise guard blocked a create_promise call"
    assert first["condition"] == {"logic": "or", "conditions": [
        {"variable": "promise_guard", "operator": "contains", "operand": "blocked"}]}
    assert negotiate["on_enter_first_entry_only"] is True  # coming back does not repeat the debt statement
    fix, say = nodes["promise_fix"], nodes["promise_fix_say"]
    assert fix["node_type"] == "code" and fix["passthrough_default_transition"] == "promise_fix_say"
    assert say["node_type"] == "conversation" and say["passthrough"] is True
    assert say["on_enter"] == {"type": "tts", "text": "{{promise_fix_text}}"}
    assert say["passthrough_default_transition"] == "negotiate"
    assert say["transitions"][0]["target_node"] == "negotiate"
    code = extract_code_snippets({"nodes": nodes})["promise_fix"]
    blocked = run_snippet(code, {"promise_guard": "blocked: the date", "promise_guard_say": "Say this."})
    assert (blocked["promise_guard"], blocked["promise_fix_text"]) == ("handled", "Say this.")
    neutral = run_snippet(code, {"promise_guard": "blocked: unknown"})
    assert neutral["promise_fix_text"] == "I am sorry, I could not record that. Let me check the details again."


def test_the_compiled_guard_speaks_the_correction_with_the_real_facts(api: ToolApi) -> None:
    workflow = compile_workflow(IR_AB, api, LEDGER_AB).workflow
    tool = workflow["nodes"]["negotiate"]["tools"][0]
    state = _facts(IR_AB, "PUB-13")
    expected = {
        ("p2", 900, "2026-10-30"): ("I am sorry, the latest date I can record for your personal loan is Friday 23 October. "
                                    "Could you pay 900.00 on that day?"),
        ("p1", 5, "2026-10-16"): ("I am sorry, for your credit card ending 6604 I can record an amount from 90.00 to "
                                  "300.00. Which amount can you pay?"),
        ("p1", 200, "2026-10-17"): ("I am sorry, I cannot record that day for your credit card ending 6604. The next day "
                                    "I can record is Monday 19 October. Would that work for you?"),
        ("p1", 200, "2026-10-26"): ("I am sorry, I cannot record that day for your credit card ending 6604. The next day "
                                    "I can record is Tuesday 27 October. Would that work for you?"),
    }
    for (pid, amount, date), say in expected.items():
        out = run_hook(tool["pre_process_code"], vars=state,
                       config={"url": "u", "body": {"product_id": pid, "amount": amount, "date": date}})
        assert out["config"]["url"] == "" and out["vars"]["promise_guard_say"] == say, (pid, date)
        assert out["vars"]["promise_guard_blocks"] == "1"


def test_every_promise_correction_is_brace_free() -> None:
    for options in ({}, {"currency": "dollars", "accept_between": False, "after_deadline": "reject", "shift": "reject"},
                    {"shift": "previous_business_day", "enabled_dates": False}):
        code = hooks.pre_hook_promise_guard("product_id", "amount", "date", **options)
        assert "{{" not in code and "}}" not in code


# ---- audit C-20: any phrasing of the recording notice becomes the active sentence ------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("This call is recorded.", "We record this call."),
        ("This call may be recorded for quality purposes.", "We record this call for quality purposes."),
        ("Calls are recorded.", "We record this call."),
        ("This conversation is recorded.", "We record this call."),
        ("Please note that this call will be recorded.", "Please note that we record this call."),
        ("As required by law, this call is recorded.", "As required by law, we record this call."),
        ("Hello. This call is being recorded and monitored for training.", "Hello. We record this call for training."),
        ("Hello, this is the bank. Your call is recorded. Am I speaking with Mr Reyes?",
         "Hello, this is the bank. We record this call. Am I speaking with Mr Reyes?"),
        ("We are recording this call.", "We record this call."),
        ("This call is recorded for training purposes", "We record this call for training purposes"),
        # unchanged: no call is recorded here
        ("We record this call.", "We record this call."),
        ("Your promise is recorded.", "Your promise is recorded."),
        ("The payment was recorded yesterday.", "The payment was recorded yesterday."),
        ("", ""),
    ],
)
def test_the_recording_notice_vectors(text: str, expected: str) -> None:
    from builder.compiler.context import soften_recording_notice

    assert soften_recording_notice(text) == expected


def test_a_differently_worded_recording_notice_never_reaches_the_opening_as_recorded(api: ToolApi) -> None:
    ir = IR_A.model_copy(deep=True)
    ir.phrases.opening_sentence = "Hello, this is the bank. Please note that this call may be recorded."
    opening = compile_workflow(ir, api, LEDGER_A).workflow["nodes"]["opening"]["on_enter"]["text"]
    assert opening == ("Hello, this is the bank. Please note that we record this call. "
                       "Am I speaking with {{title}} {{last_name}}?")
    assert "recorded" not in opening
