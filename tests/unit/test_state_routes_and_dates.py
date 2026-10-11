"""Deterministic state, routes and date facts (audit round 3), run through the compiled workflow.

* AUD-07: a promise / callback the bank answered with HTTP 200 and ``ok: false`` is counted by the engine as a success;
  the routes to the fixed confirmations and the closing decision read the acknowledgement written by the post-hook.
* AUD-13: a clock call variable with another name than ``now`` is read by every date rule (never the real clock).
* AUD-17 / F21: the date table never offers a day the guard blocks; a policy without a business-day rule gets no shift
  clauses; the facts list the non-business days up to the deadline with the day to offer instead.
* AUD-23: without an overall "all verified" flag the hooks count the verified questions; the graph routes on that.
* AUD-24: an account that could not be loaded closes as a recording failure instead of negotiating without facts.
* AUD-29: a callback outside the allowed slots never reaches the bank (parity with ``policy.calendar.check_callback``).
* AUD-50: a ``now`` with an offset is read as wall-clock time; a count-up wrong-answer counter may skip the limit.
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Any

import pytest

from builder.checks.code_sandbox import extract_code_snippets, run_hook, run_snippet
from builder.checks.lint import lint_workflow
from builder.compiler import codegen
from builder.compiler.compile import compile_workflow
from builder.ingest.tool_api import FieldSpec, ToolApi, load_tool_api
from builder.policy.calendar import BusinessCalendar, check_callback
from builder.policy.ir import PolicyIR, RuleLedger
from builder.selftest.smoke import call_variables_for_test

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "fixtures" / "policy"
IR_A = PolicyIR.model_validate(json.loads((FIXTURES / "brief_a_ir.json").read_text(encoding="utf-8")))
LEDGER_A = RuleLedger.model_validate(json.loads((FIXTURES / "brief_a_ledger.json").read_text(encoding="utf-8")))
IR_AB = PolicyIR.model_validate(json.loads((FIXTURES / "brief_ab_ir.json").read_text(encoding="utf-8")))
LEDGER_AB = RuleLedger.model_validate(json.loads((FIXTURES / "brief_ab_ledger.json").read_text(encoding="utf-8")))
NOW = "2026-10-14T11:00"
CARD = {"product_id": "p1", "type": "credit_card", "card_last4": "4821", "past_due": 420.0, "days_late": 6,
        "minimum_payment": 126.0}
OK = {"status": "success", "data": {"ok": "true"}}
REFUSED = {"status": "success", "data": {"ok": "false", "error": "not carried out"}}


def _api() -> ToolApi:
    return load_tool_api(REPO / "tools" / "openapi.json")


def _compile(ir: PolicyIR = IR_A, ledger: RuleLedger = LEDGER_A, profile: str = "staged",
             api: ToolApi | None = None) -> dict[str, Any]:
    return compile_workflow(ir, api or _api(), ledger, profile=profile).workflow


def _snippet(workflow: dict[str, Any], node: str) -> str:
    return extract_code_snippets(workflow)[node]


def _tool(workflow: dict[str, Any], node: str, name: str) -> dict[str, Any]:
    return next(t for t in workflow["nodes"][node]["tools"] if t["function"]["name"] == name)


def _holds(condition: dict[str, Any], state: dict[str, Any]) -> bool:
    """The engine's condition semantics as verified: ``equals`` compares text, ``greater_than`` numbers, unset = False."""
    if "conditions" in condition:
        results = [_holds(c, state) for c in condition["conditions"]]
        return all(results) if condition.get("logic") == "and" else any(results)
    value = state.get(condition["variable"])
    if value is None:
        return False
    if condition["operator"] == "equals":
        return str(value) == str(condition["operand"])
    if condition["operator"] == "contains":
        return str(condition["operand"]) in str(value)
    if condition["operator"] == "greater_than":
        try:
            return float(value) > float(condition["operand"])
        except ValueError:
            return False
    raise AssertionError(condition)


def _route(workflow: dict[str, Any], node: str, state: dict[str, Any]) -> str | None:
    """The first variable transition of ``node`` that holds (tool-call and intent exits are the model's)."""
    for transition in workflow["nodes"][node]["transitions"]:
        condition = transition["condition"]
        if transition.get("is_tool_call") or condition.get("operator") in ("matches_intent", "always"):
            continue
        if _holds(condition, state):
            return str(transition["target_node"])
    return None


def _facts(workflow: dict[str, Any], products: list[dict[str, Any]], now: str = NOW, **extra: Any) -> dict[str, Any]:
    state = run_snippet(_snippet(workflow, "setup"), {"now": now, **extra})
    return run_snippet(_snippet(workflow, "account_facts"), {**state, "products": [dict(p) for p in products]})


def _call(tool: dict[str, Any], state: dict[str, Any], body: dict[str, Any],
          response: dict[str, Any] | None) -> tuple[dict[str, Any], bool]:
    """One call as the engine runs it: pre-hook, request, post-hook on a 2xx, then the engine's counters."""
    name = tool["function"]["name"]
    pre = run_hook(tool["pre_process_code"], vars=dict(state), config={"url": "https://bank.example", "body": body})
    state, sent = pre["vars"], pre["config"]["url"] != ""
    if sent and response is not None:
        state = run_hook(tool["post_process_code"], vars=state, result=json.loads(json.dumps(response)))["vars"]
    key = f"{name}_success_count" if sent and response is not None else f"{name}_failure_count"
    state[key] = int(state.get(key) or 0) + 1
    return state, sent


def _decide(workflow: dict[str, Any], state: dict[str, Any], end: str) -> tuple[str, str | None]:
    state = run_snippet(_snippet(workflow, end), {**state, "identity_verified": "yes"})
    out = run_snippet(_snippet(workflow, "decide"), state)
    return out["final_outcome"], out.get("final_reason")


# ---- AUD-07 -------------------------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def wf_a() -> dict[str, Any]:
    return _compile()


def test_a_refused_promise_never_routes_to_the_recorded_confirmation(wf_a: dict[str, Any]) -> None:
    tool = _tool(wf_a, "negotiate", "create_promise")
    state = _facts(wf_a, [CARD])
    body = {"customer_id": "c", "product_id": "p1", "amount": 420, "date": "2026-10-16"}
    refused, sent = _call(tool, state, body, REFUSED)
    assert sent and refused["create_promise_success_count"] == 1  # the engine counts the HTTP 200
    assert _route(wf_a, "negotiate", refused) is None  # ... but promise_done ("Your promise is recorded") never fires
    for chosen in ("end_promise_recorded", "end_recording_failed"):
        assert _decide(wf_a, refused, chosen) == ("no_promise", "service_error"), chosen
    recorded, _ = _call(tool, refused, body, OK)  # the supported retry is acknowledged
    assert _route(wf_a, "negotiate", recorded) == "promise_done"
    assert _decide(wf_a, recorded, "end_promise_recorded") == ("promise", None)


def test_a_500_then_an_acknowledged_retry_still_routes_to_the_confirmation(wf_a: dict[str, Any]) -> None:
    tool = _tool(wf_a, "negotiate", "create_promise")
    body = {"customer_id": "c", "product_id": "p1", "amount": 420, "date": "2026-10-16"}
    failed, _ = _call(tool, _facts(wf_a, [CARD]), body, None)
    assert _route(wf_a, "negotiate", failed) is None
    recorded, _ = _call(tool, failed, body, OK)
    assert _route(wf_a, "negotiate", recorded) == "promise_done"
    assert _decide(wf_a, recorded, "end_promise_recorded") == ("promise", None)


def test_a_refused_callback_never_routes_to_the_booked_confirmation(wf_a: dict[str, Any]) -> None:
    tool = _tool(wf_a, "third_party", "schedule_callback")
    state = run_snippet(_snippet(wf_a, "setup"), {"now": NOW})
    body = {"customer_id": "c", "date": "2026-10-15", "time": "11:00"}
    refused, sent = _call(tool, state, body, REFUSED)
    assert sent and _route(wf_a, "third_party", refused) is None
    for chosen in ("end_callback_booked", "end_callback_not_booked"):
        assert _decide(wf_a, {**refused, "identity_verified": ""}, chosen) == ("not_reached", "no_callback"), chosen
    booked, _ = _call(tool, refused, body, OK)
    assert _route(wf_a, "third_party", booked) == "callback_done"
    assert _decide(wf_a, {**booked, "identity_verified": ""}, "end_callback_booked") == ("not_reached", "callback")


# ---- one effective floor (Sol gap 5) ------------------------------------------------------------------------------------


@pytest.mark.parametrize("profile", ["staged", "lean"])
def test_a_floor_above_the_past_due_amount_is_capped_everywhere(profile: str) -> None:
    """A floor field larger than the debt is shown, offered, stored and guarded as the past-due amount."""
    workflow = _compile(profile=profile)
    small = {**CARD, "past_due": 60.01, "minimum_payment": 500.0}
    facts = _facts(workflow, [small])
    assert facts["p1_floor"] == "60.01" and facts["p1_past_due"] == "60.01"
    text = facts["account_facts"]
    assert "500" not in text, text
    assert "Floor (minimum payment): 60.01" in text
    node = next(n for n in ("negotiate", "negotiate_p1") if n in workflow["nodes"])
    tool = _tool(workflow, node, "create_promise")
    body = {"customer_id": "c", "product_id": "p1", "amount": 60.01, "date": "2026-10-16"}
    _, sent = _call(tool, facts, body, OK)
    assert sent
    _, sent = _call(tool, facts, {**body, "amount": 500}, OK)
    assert not sent
    # a floor under the debt is untouched
    normal = _facts(workflow, [CARD])
    assert normal["p1_floor"] == "126.00" and "126.00" in normal["account_facts"]


# ---- AUD-13 -------------------------------------------------------------------------------------------------------------


def _renamed_clock(name: str, example: str = "2026-10-16T18:30") -> PolicyIR:
    ir = IR_A.model_copy(deep=True)
    for variable in ir.call_variables:
        if variable.name == "now":
            variable.name = name
            variable.example = example
            variable.meaning = "The current date and time of the call."
    return ir


@pytest.mark.parametrize("name", ["call_time", "current_datetime"])
@pytest.mark.parametrize("profile", ["staged", "lean"])
def test_a_renamed_clock_drives_every_date_rule(name: str, profile: str) -> None:
    ir = _renamed_clock(name)
    assert codegen.clock_variable(ir) == name
    workflow = _compile(ir, profile=profile)
    reference = _facts(_compile(profile=profile), [CARD], now="2026-10-16T18:30")
    state = run_snippet(_snippet(workflow, "setup"), {name: "2026-10-16T18:30"})
    assert (state["setup_state"], state["today_iso"], state["now_time"]) == ("ok", "2026-10-16", "18:30")
    facts = run_snippet(_snippet(workflow, "account_facts"), {**state, "products": [dict(CARD)]})
    for key in ("p1_deadline", "allowed_p1", "date_table", "account_facts"):
        assert facts[key] == reference[key], key
    smoke = call_variables_for_test(ir, "2026-10-12T11:00")
    assert smoke[name] == "2026-10-12T11:00" and "now" not in smoke


def test_the_kit_clock_is_still_read_as_now() -> None:
    assert codegen.clock_variable(IR_A) == "now"
    code = codegen.setup_snippet(IR_A)
    assert "vars.get('now')" in code and "or vars.get('now'))" not in code
    assert call_variables_for_test(IR_A, "2026-10-12T11:00")["now"] == "2026-10-12T11:00"


def test_a_birth_date_is_never_taken_for_the_clock() -> None:
    ir = _renamed_clock("date_of_birth", "1988-03-14")
    ir.call_variables[-1].meaning = "The customer's date of birth."
    assert codegen.clock_variable(ir) == "now"


# ---- AUD-17 / F21 -------------------------------------------------------------------------------------------------------


def _guard_allows(tool: dict[str, Any], facts: dict[str, Any], pid: str, day: str) -> bool:
    body = {"customer_id": "c", "product_id": pid, "amount": float(facts[f"{pid}_past_due"]), "date": day}
    return run_hook(tool["pre_process_code"], vars=dict(facts), config={"url": "u", "body": body})["config"]["url"] == "u"


@pytest.mark.parametrize("variant", ["kit", "previous_day", "holiday", "ab"])
def test_every_offer_in_the_date_table_is_a_day_the_guard_accepts(variant: str) -> None:
    ir, ledger = (IR_AB, LEDGER_AB) if variant == "ab" else (IR_A.model_copy(deep=True), LEDGER_A)
    if variant == "previous_day":
        ir.promise_dates.non_business_day_shift = "previous_business_day"
    if variant == "holiday":
        ir.calendar.holidays = [*ir.calendar.holidays, datetime.date(2026, 10, 21)]
    workflow = _compile(ir, ledger)
    tool = _tool(workflow, "negotiate", "create_promise")
    products = [dict(CARD)] if variant != "ab" else [
        {"product_id": "p1", "type": "personal_loan", "past_due": 300, "days_late": 5, "instalment_amount": 50},
        {"product_id": "p2", "type": "personal_loan", "past_due": 200, "days_late": 40, "instalment_amount": 50}]
    for offset in range(0, 21, 3):
        now = (datetime.date(2026, 10, 12) + datetime.timedelta(days=offset)).isoformat() + "T11:00"
        facts = _facts(workflow, products, now=now)
        for row in facts["date_table"].splitlines():
            if " -> " not in row or "(" not in row.split(" -> ", 1)[1]:
                continue
            day, clause = row[:10], row.split(" -> ", 1)[1]
            target = clause.rsplit("(", 1)[1][:10]
            pids = [p["product_id"] for p in products]
            if clause.startswith("for "):
                pids = [p.strip() for p in clause[4:].split(":", 1)[0].split(",")]
            for pid in pids:
                assert not _guard_allows(tool, facts, pid, day), (now, row, pid)
                assert _guard_allows(tool, facts, pid, target), (now, row, pid)


def test_a_policy_without_a_business_day_rule_shifts_no_weekend() -> None:
    ir = IR_A.model_copy(deep=True)
    ir.promise_dates.business_days_only = False
    workflow = _compile(ir)
    facts = _facts(workflow, [CARD])
    saturday = next(r for r in facts["date_table"].splitlines() if r.startswith("2026-10-17"))
    assert " -> " not in saturday and "not a business day" not in saturday
    assert "A weekend day or bank holiday that is not after the deadline" not in workflow["nodes"]["negotiate"][
        "task_message"]
    assert "A weekend day or bank holiday that is not after the deadline" in _compile()["nodes"]["negotiate"][
        "task_message"]
    assert _guard_allows(_tool(workflow, "negotiate", "create_promise"), facts, "p1", "2026-10-17")


def test_a_row_after_one_products_deadline_offers_its_day_to_the_other_product_only() -> None:
    workflow = _compile(IR_AB, LEDGER_AB)
    products = [
        {"product_id": "p1", "type": "personal_loan", "past_due": 300, "days_late": 5, "instalment_amount": 50},
        {"product_id": "p2", "type": "personal_loan", "past_due": 200, "days_late": 40, "instalment_amount": 50}]
    facts = _facts(workflow, products)
    row = next(r for r in facts["date_table"].splitlines() if r.startswith("2026-10-24"))
    assert "-> for p1: if the customer names this day, offer Tuesday 27 October 2026 (2026-10-27) instead" in row
    assert row.endswith("| after the deadline of p2 - not allowed for p2")


def test_the_facts_name_the_non_business_days_before_the_deadline_with_the_day_to_offer() -> None:
    facts = _facts(_compile(), [CARD])
    line = next(x for x in facts["account_facts"].splitlines() if x.startswith("   Not business days up to the deadline"))
    assert "Monday 26 October (bank holiday) -> offer Tuesday 27 October (2026-10-27)" in line
    assert "Saturday 17 October and Sunday 18 October -> offer Monday 19 October (2026-10-19)" in line
    assert "November" not in line.split("->")[-1] or "Monday 2 November (2026-11-02)" in line  # never past the deadline


# ---- AUD-23 -------------------------------------------------------------------------------------------------------------


def _without_overall_flag(name: str | None) -> ToolApi:
    api = _api()
    verify = api.tools["verify_identity"]
    verify.response_fields = [s for s in verify.response_fields if s.name != "all_verified"]
    if name:  # a renamed overall flag the roles do not recognise
        verify.response_fields.append(FieldSpec(name=name, type="boolean",
                                                description="True once every security question has been answered correctly."))
    return api


@pytest.mark.parametrize("renamed", [None, "identity_confirmed"])
@pytest.mark.parametrize("profile", ["staged", "lean"])
def test_without_an_overall_flag_the_hooks_count_the_verified_questions(renamed: str | None, profile: str) -> None:
    api = _without_overall_flag(renamed)
    workflow = _compile(IR_AB, LEDGER_AB, profile, api)
    node = "identity" if profile == "staged" else "call"
    tool = _tool(workflow, node, "verify_identity")
    assert not any(t["target_node"] == "load_account" and t.get("is_tool_call")
                   for t in workflow["nodes"][node]["transitions"])

    def answer(state: dict[str, Any], field: str, verified: bool) -> dict[str, Any]:
        pre = run_hook(tool["pre_process_code"], vars=state, config={"url": "u", "body": {
            "customer_id": "c", "field": field, "answer": "x"}})
        return run_hook(tool["post_process_code"], vars=pre["vars"], result={"status": "success", "data": {
            "ok": True, "verified": verified, "wrong_answers_left": 3}})

    fields = [q.field for q in IR_AB.identity.questions]
    first = answer({}, fields[0], True)
    assert _route(workflow, node, first["vars"]) is None
    assert first["result"]["data"]["what_to_do_next"].startswith("This answer is correct")
    again = answer(first["vars"], fields[0], True)  # the same question twice is still one question
    assert _route(workflow, node, again["vars"]) is None
    wrong = answer(again["vars"], fields[1], False)
    assert _route(workflow, node, wrong["vars"]) is None
    done = answer(wrong["vars"], fields[1], True)
    assert _route(workflow, node, done["vars"]) == "load_account"
    assert "All security answers are verified" in done["result"]["data"]["what_to_do_next"]
    # parallel: both requests staged before either answer arrives
    pre1 = run_hook(tool["pre_process_code"], vars={}, config={"url": "u", "body": {"field": fields[0], "answer": "x"}})
    pre2 = run_hook(tool["pre_process_code"], vars=pre1["vars"], config={"url": "u", "body": {"field": fields[1],
                                                                                            "answer": "x"}})
    state = pre2["vars"]
    for _ in fields:
        state = run_hook(tool["post_process_code"], vars=state, result={"status": "success", "data": {
            "ok": True, "verified": True}})["vars"]
    assert _route(workflow, node, state) == "load_account"
    assert lint_workflow(workflow, api, set(IR_AB.call_variable_names())).ok


# ---- AUD-24 -------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("products", [None, [], [{"type": "credit_card", "past_due": 10, "days_late": 3}]])
def test_an_account_that_could_not_be_loaded_closes_as_a_recording_failure(products: Any) -> None:
    for ir, ledger in ((IR_A, LEDGER_A), (IR_AB, LEDGER_AB)):
        workflow = _compile(ir, ledger)
        state = run_snippet(_snippet(workflow, "setup"), {"now": NOW})
        facts = run_snippet(_snippet(workflow, "account_facts"), {**state, **({"products": products}
                                                                                if products is not None else {})})
        assert _route(workflow, "account_facts", facts) == "account_unavailable"
        node = workflow["nodes"]["account_unavailable"]
        assert node["passthrough_default_transition"] == "end_recording_failed"
        expected = ir.recording.failure_outcome
        assert _decide(workflow, facts, "end_recording_failed") == (expected.outcome, expected.reason)
    loaded = _facts(_compile(), [CARD])
    assert _route(_compile(), "account_facts", loaded) is None  # the always route: negotiate


# ---- AUD-29 -------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("now", ["2026-10-14T11:00", "2026-10-14T19:30", "2026-10-16T09:00"])
def test_the_callback_guard_blocks_exactly_what_the_callback_rule_forbids(wf_a: dict[str, Any], now: str) -> None:
    rule = IR_A.third_party.callback
    assert rule is not None
    cal = BusinessCalendar.from_ir(IR_A.calendar)
    tool = _tool(wf_a, "third_party", "schedule_callback")
    state = run_snippet(_snippet(wf_a, "setup"), {"now": now})
    today = datetime.date.fromisoformat(now[:10])
    for offset in range(0, 5):
        day = (today + datetime.timedelta(days=offset)).isoformat()
        for said in ("09:59", "10:00", "11:00", "11:01", "18:59", "19:00", "19:01", "7 pm", "7:30 pm", "11am"):
            pre = run_hook(tool["pre_process_code"], vars=dict(state), config={"url": "u", "body": {
                "customer_id": "c", "date": day, "time": said}})
            sent_time = pre["config"]["body"]["time"]
            expected = check_callback(day, sent_time, now, cal, rule).status == "ok"
            assert (pre["config"]["url"] == "u") is expected, (now, day, said, sent_time)
            if not expected:
                assert pre["vars"]["callback_guard"].startswith("blocked: ")
                assert "I am sorry, I cannot book a callback at that time." in pre["vars"]["callback_guard_say"]


def test_a_blocked_callback_is_corrected_then_given_up_after_the_allowed_asks(wf_a: dict[str, Any]) -> None:
    tool = _tool(wf_a, "third_party", "schedule_callback")
    state = run_snippet(_snippet(wf_a, "setup"), {"now": NOW})
    outside = {"customer_id": "c", "date": "2026-10-15", "time": "20:00"}
    first, sent = _call(tool, state, outside, OK)
    assert not sent and _route(wf_a, "third_party", first) == "callback_fix_third_party"
    fixed = run_snippet(_snippet(wf_a, "callback_fix_third_party"), first)
    assert _route(wf_a, "callback_fix_third_party", fixed) is None  # one more ask: the correction is spoken
    assert wf_a["nodes"]["callback_fix_say_third_party"]["passthrough_default_transition"] == "third_party"
    assert "10:00" in fixed["callback_fix_text"] and _route(wf_a, "third_party", fixed) is None
    second, _ = _call(tool, fixed, outside, OK)
    stop = run_snippet(_snippet(wf_a, "callback_fix_third_party"), second)
    assert _route(wf_a, "callback_fix_third_party", stop) == "callback_stop_third_party"
    assert wf_a["nodes"]["callback_stop_third_party"]["passthrough_default_transition"] == "end_callback_not_booked"
    assert _decide(wf_a, {**stop, "identity_verified": ""}, "end_callback_not_booked") == ("not_reached", "no_callback")


@pytest.mark.parametrize("said", ["11:99", "12:60", "10:75", "24:00", "25:30", "99:00"])
def test_the_callback_guard_blocks_an_impossible_clock_time(said: str) -> None:
    """Sol gap 4: an HH:MM-shaped time that is not a time of day never reaches the bank, even inside the window."""
    from builder.compiler.hooks import pre_hook_callback_guard

    code = pre_hook_callback_guard("date", "time")
    slots = "2030-01-07|10:00|0|19:00|0|Monday"
    pre = run_hook(code, vars={"callback_slots": slots}, config={"url": "u", "body": {"date": "2030-01-07",
                                                                                       "time": said}})
    assert pre["config"]["url"] == "", said
    assert pre["vars"]["callback_guard"].startswith("blocked: ")
    assert "I am sorry, I cannot book a callback at that time." in pre["vars"]["callback_guard_say"]
    for fine in ("10:00", "11:59", "18:59"):
        ok = run_hook(code, vars={"callback_slots": slots}, config={"url": "u", "body": {"date": "2030-01-07",
                                                                                         "time": fine}})
        assert ok["config"]["url"] == "u" and ok["vars"]["callback_guard"] == "ok", fine
    # documented: without computed slots the request passes as before
    free = run_hook(code, vars={}, config={"url": "u", "body": {"date": "2030-01-07", "time": said}})
    assert free["config"]["url"] == "u"


def test_the_compiled_callback_guard_blocks_an_impossible_clock_time(wf_a: dict[str, Any]) -> None:
    tool = _tool(wf_a, "third_party", "schedule_callback")
    state = run_snippet(_snippet(wf_a, "setup"), {"now": NOW})
    for said in ("11:99", "12:60"):
        pre = run_hook(tool["pre_process_code"], vars=dict(state), config={"url": "u", "body": {
            "customer_id": "c", "date": "2026-10-15", "time": said}})
        assert pre["config"]["url"] == "", said
        assert pre["vars"]["callback_guard"].startswith("blocked: ")


def test_a_custom_callback_rule_is_not_guarded() -> None:
    ir = IR_A.model_copy(deep=True)
    assert ir.third_party.callback is not None
    ir.third_party.callback.allowed_days = "custom"
    ir.third_party.callback.custom_rule_text = "Any time the person names."
    workflow = _compile(ir)
    tool = _tool(workflow, "third_party", "schedule_callback")
    state = run_snippet(_snippet(workflow, "setup"), {"now": NOW})
    pre = run_hook(tool["pre_process_code"], vars=state, config={"url": "u", "body": {"date": "2026-10-20",
                                                                                        "time": "21:00"}})
    assert pre["config"]["url"] == "u"


# ---- AUD-50 -------------------------------------------------------------------------------------------------------------


def test_a_now_with_an_offset_is_read_as_wall_clock_time() -> None:
    plain = run_snippet(codegen.setup_snippet(IR_A), {"now": NOW})
    for now in ("2026-10-14T11:00+02:00", "2026-10-14T11:00Z", "2026-10-14 11:00:00"):
        state = run_snippet(codegen.setup_snippet(IR_A), {"now": now})
        assert state["setup_state"] == "ok", now
        for key in ("today_iso", "now_time", "callback_facts", "callback_slots", "date_table"):
            assert state[key] == plain[key], (now, key)


def test_the_promise_guard_reads_an_unpadded_date(wf_a: dict[str, Any]) -> None:
    tool = _tool(wf_a, "negotiate", "create_promise")
    facts = _facts(wf_a, [CARD])
    for said in ("2026-10-6", "2026-10-16", "16/10/2026", "2026/10/16"):
        pre = run_hook(tool["pre_process_code"], vars=dict(facts), config={"url": "u", "body": {
            "customer_id": "c", "product_id": "p1", "amount": 420, "date": said}})
        expected = said != "2026-10-6"  # 6 October is before today: still blocked, as a date before today
        assert (pre["config"]["url"] == "u") is expected, said
        assert pre["config"]["body"]["date"] in ("2026-10-16", "2026-10-06")
