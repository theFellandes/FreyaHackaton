"""End-to-end, offline: structural variants of a hidden brief through the real compile pipeline and the compiled code.

The hidden briefs change numbers and limits, identity questions, the outcome table, the calendar and the tool contract
(field names and allowed values). Each variant here (see :mod:`tests.variants.variants`; invented values on a synthetic,
model-made policy, never Brief A or B) goes through the compiled track's pipeline - safe fixes, ``validate_policy``,
``compile_workflow`` (staged and lean), lint, the workflow guard, coverage - and then the compiled workflow is *run* by
:class:`tests.variants.engine.Engine`: setup, account facts, tool hooks, deterministic routes, ``decide`` and the
``close_call`` request. The tests assert behaviour: the request bodies, which flag is read, the dates and amounts the
guards allow for several ``now`` values, identity routing, the close outcome, and that no ``{placeholder}`` and no kit
brief value reaches the agent.

A test marked ``xfail(strict=True)`` pins a gap that is still open (its audit id is in the reason); it turns red the day
the gap is closed, so the mark must then be removed.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

import pytest

from builder.checks.code_sandbox import extract_code_snippets, run_hook, run_snippet
from builder.compiler.context import soften_recording_notice
from builder.policy.calendar import BusinessCalendar, deadline_for, latest_valid_promise_date
from tests.variants import variants as V
from tests.variants.conftest import VARIANTS, Built, run_pipeline
from tests.variants.engine import Engine

NOW = "2026-11-02T10:00"  # a Monday; Tuesday 2026-11-03 is a holiday of the synthetic bank
CALL = {"customer_id": "c-77", "first_name": "Ann", "last_name": "Lee", "title": "Ms",
        "company_name": "Kestrel Joinery Ltd"}
CARD = {"product_id": "p1", "type": "card", "card_last4": "1111", "past_due": 300.0, "days_overdue": 20,
        "min_due": 45.0}
VEHICLE = {"product_id": "p2", "type": "vehicle_finance", "past_due": 800.0, "days_overdue": 40,
           "monthly_instalment": 210.0}
SINGLE_BRACE = re.compile(r"(?<!\{)\{[A-Za-z_][A-Za-z0-9_]*\}(?!\})")
PROFILES = ("staged", "lean")

Responder = Callable[[str, dict[str, Any]], dict[str, Any] | None]


# ---- helpers --------------------------------------------------------------------------------------------------------------


def bank(built: Built, products: list[dict[str, Any]] | None = None, **answers: Any) -> Responder:
    """The bank's answers: every security answer right, the products, ``ok`` for the rest (``answers`` override a tool:
    a dict, or a function of the request body; ``None`` = HTTP 5xx)."""
    fields = [q.field for q in built.ir.identity.questions]

    def respond(tool: str, body: dict[str, Any]) -> dict[str, Any] | None:
        if tool in answers:
            answer = answers[tool]
            return answer(body) if callable(answer) else answer
        if tool == "get_account":
            return {"ok": True, "products": products if products is not None else [CARD, VEHICLE]}
        if tool == "verify_identity":
            return {"ok": True, "verified": True, "all_verified": body.get("field") == fields[-1],
                    "wrong_answers_left": 4}
        return {"ok": True}

    return respond


def call(built: Built, responder: Responder | None = None, now: str = NOW, clock: str = "now") -> Engine:
    engine = Engine(built.workflow, {**CALL, clock: now}, responder or bank(built))
    engine.go(built.workflow["initial_node"])
    return engine


def verified(built: Built, responder: Responder | None = None, now: str = NOW, answers: tuple[str, ...] = ()) -> Engine:
    """A staged call that reached ``negotiate``: the right person, every security answer right."""
    engine = call(built, responder, now)
    engine.move("identity")
    questions = [q.field for q in built.ir.identity.questions]
    for field, answer in zip(questions, answers or ("AB12CD",) * len(questions), strict=True):
        engine.call_tool("verify_identity", {"field": field, "answer": answer})
    assert engine.node == "negotiate", engine.visited[-6:]
    return engine


def facts(built: Built, products: list[dict[str, Any]], now: str = NOW, clock: str = "now") -> dict[str, Any]:
    """What ``setup`` and ``account_facts`` of the compiled workflow compute for ``products`` at ``now``."""
    snippets = extract_code_snippets(built.workflow)
    state = run_snippet(snippets["setup"], {**CALL, clock: now})
    return run_snippet(snippets["account_facts"], {**state, "products": [dict(p) for p in products]})


def promise_guard(built: Built, state: dict[str, Any], body: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    """Run the compiled ``create_promise`` pre-hook: (sent?, the request config and variables after it)."""
    tool = next(t for t in built.workflow["nodes"]["negotiate"]["tools"] if t["function"]["name"] == "create_promise")
    out = run_hook(tool["pre_process_code"], vars=dict(state), config={"url": "u", "body": dict(body)})
    return out["config"]["url"] == "u", out


def texts(workflow: dict[str, Any]) -> list[tuple[str, str]]:
    """Every text the dialogue model reads or the caller hears (no code): prompts, fixed lines, tool descriptions."""
    out = [("system_prompt", workflow["system_prompt"])]
    for name, node in workflow["nodes"].items():
        out.append((f"{name}.task_message", node.get("task_message") or ""))
        out.append((f"{name}.on_enter", (node.get("on_enter") or {}).get("text") or ""))
        for tool in node.get("tools") or []:
            function = tool["function"]
            out.append((f"{name}/{function['name']}", function.get("description") or ""))
            body = function["parameters"]["properties"].get("body") or {}
            for key, leaf in (body.get("properties") or {}).items():
                out.append((f"{name}/{function['name']}.{key}", leaf.get("description") or ""))
    return out


def tool_body(built: Built, node: str, tool: str) -> dict[str, Any]:
    found = next(t for t in built.workflow["nodes"][node]["tools"] if t["function"]["name"] == tool)
    return found["function"]["parameters"]["properties"]["body"]["properties"]


def engine_body(built: Built, node: str) -> dict[str, Any]:
    action = built.workflow["nodes"][node]["pre_actions"][0]
    return {k: v.get("default") for k, v in action["function"]["parameters"]["properties"]["body"]["properties"].items()}


# ---- 1. every variant passes every gate of the pipeline, on both profiles -------------------------------------------------


@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize("name", list(VARIANTS))
def test_every_variant_passes_the_pipeline_gates(pipeline: Callable[..., Built], name: str, profile: str) -> None:
    built = pipeline(name, profile)
    assert [str(i) for i in built.issues] == []  # not even a warning
    assert [str(f) for f in built.lint.errors] == []
    assert not built.guard.blocked, built.guard.blocking_messages()
    assert built.coverage.uncovered == [] and built.coverage.unknown == []
    if profile == "staged":
        assert built.coverage.dangling == {}
    else:  # the lean rule map also names the staged nodes; every such rule keeps a location that exists
        assert all(built.coverage.covered.get(rule) for rule in built.coverage.dangling)
    assert [n for n in built.notes if "callback_question" not in n and "date code reads" not in n] == []


@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize("name", list(VARIANTS))
def test_every_variant_compiles_to_the_same_bytes_twice(
    pipeline: Callable[..., Built], name: str, profile: str, tmp_path: Any,
) -> None:
    again = run_pipeline(VARIANTS[name](), profile, tmp_path)
    assert json.dumps(again.workflow, sort_keys=True) == json.dumps(pipeline(name, profile).workflow, sort_keys=True)


@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize("name", list(VARIANTS))
def test_no_variant_carries_a_kit_brief_value(pipeline: Callable[..., Built], name: str, profile: str) -> None:
    text = json.dumps(pipeline(name, profile).workflow, ensure_ascii=False)
    assert [literal for literal in V.KIT_LITERALS if literal in text] == []


@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize("name", list(VARIANTS))
def test_no_single_brace_placeholder_reaches_a_prompt_or_a_fixed_line(
    pipeline: Callable[..., Built], name: str, profile: str,
) -> None:
    hits = [(where, m.group(0)) for where, text in texts(pipeline(name, profile).workflow)
            for m in SINGLE_BRACE.finditer(text)]
    assert hits == []


def test_the_safe_fix_repairs_the_extracted_persuasion_dates(pipeline: Callable[..., Built]) -> None:
    built = pipeline("base")
    assert any(line.startswith("persuasion date kinds set") for line in built.fixes)
    assert [step.date for step in built.ir.persuasion.steps] == [
        "next_business_day_after_today", "deadline", "next_business_day_after_today"]


# ---- 2. the base: a whole call through the compiled workflow ---------------------------------------------------------------


def test_the_opening_is_the_brief_s_sentence_and_names_the_customer(pipeline: Callable[..., Built]) -> None:
    built = pipeline("base")
    engine = call(built)
    assert engine.node == "opening"
    assert engine.spoken == [f"{soften_recording_notice(built.ir.phrases.opening_sentence)} May I speak with Ann Lee?"]


def test_a_verified_two_product_call_states_both_debts_and_closes_agreed(pipeline: Callable[..., Built]) -> None:
    built = pipeline("base")
    engine = verified(built)
    assert [r.body for r in engine.requests[:2]] == [
        {"customer_id": "c-77", "field": "postcode", "answer": "AB12CD"},
        {"customer_id": "c-77", "field": "birth_year", "answer": "AB12CD"}]
    assert engine.sent("get_account") == [{"customer_id": "c-77"}]
    assert engine.spoken[-1] == (
        "Thank you. I am calling from Meridian's arrears desk about an overdue payment. Your card ending 1111 has a "
        "past-due amount of 300.00 and is 20 days late. Your vehicle finance has a past-due amount of 800.00 and is 40 "
        "days late. Can you settle this today?")
    sent, advice = engine.call_tool("create_promise", {"product_id": "p1", "amount": "300", "date": "2026-11-04"})
    assert sent and advice.startswith("The promise IS recorded.")
    assert engine.sent("create_promise") == [
        {"customer_id": "c-77", "product_id": "p1", "amount": 300.0, "date": "2026-11-04"}]
    sent, advice = engine.call_tool("add_note", {"product_id": "p2", "reason": "rejected"})
    assert sent and advice.startswith("The note is saved.")
    engine.move("end_promise_recorded")
    assert engine.node == "hangup"
    assert engine.sent("close_call") == [{"customer_id": "c-77", "outcome": "agreed", "reason": ""}]


def test_a_holiday_and_a_date_after_the_deadline_are_never_sent(pipeline: Callable[..., Built]) -> None:
    built = pipeline("base")
    state = facts(built, [CARD, VEHICLE])
    assert state["p1_band"] == "amber" and state["p1_deadline"] == "2026-11-11"  # 6th business day, the holiday skipped
    assert state["p2_band"] == "red" and state["p2_deadline"] == "2026-11-06"
    assert state["allowed_p2"] == "2026-11-02,2026-11-04,2026-11-05,2026-11-06"
    for date in ("2026-11-03", "2026-11-07", "2026-11-09"):
        sent, out = promise_guard(built, state, {"customer_id": "c", "product_id": "p2", "amount": 800, "date": date})
        assert not sent and out["vars"]["promise_guard"].startswith("blocked"), date
    sent, _ = promise_guard(built, state, {"customer_id": "c", "product_id": "p2", "amount": 800, "date": "2026-11-06"})
    assert sent


# ---- 3. a business customer ----------------------------------------------------------------------------------------------


def test_the_business_identity_questions_reach_the_tool_and_the_prompt(pipeline: Callable[..., Built]) -> None:
    built = pipeline("business")
    field = tool_body(built, "identity", "verify_identity")["field"]
    assert field["enum"] == ["company_number", "founded_year"]
    prompt = built.workflow["nodes"]["identity"]["task_message"]
    assert "1. What is the company's registration number?\n2. In which year was the company founded?" in prompt
    assert "field company_number: Send the eight digits only, without spaces." in prompt
    assert "company_number: exactly 8 digits; founded_year: all four digits of the year" in prompt


@pytest.mark.parametrize(("field", "said", "sent"), [
    ("company_number", "0 1 2 3 4 5 6 7", "01234567"),
    ("company_number", "0123 4567", "01234567"),
    ("company_number", "12345678, sorry, 12345679", "12345679"),
    ("founded_year", "19 88", "1988"),
    ("founded_year", "1988", "1988"),
    ("founded_year", "88", "88"),  # never padded: the bank answers with its two-digit hint
    ("founded_year", "nineteen eighty-eight", "nineteen eighty-eight"),  # words are sent as said, never cut
])
def test_the_business_identity_answers_are_normalised_by_their_question(
    pipeline: Callable[..., Built], field: str, said: str, sent: str,
) -> None:
    built = pipeline("business")
    engine = call(built)
    engine.move("identity")
    engine.call_tool("verify_identity", {"field": field, "answer": said}, route=False)
    assert engine.sent("verify_identity") == [{"customer_id": "c-77", "field": field, "answer": sent}]


def test_a_business_customer_who_passes_both_questions_reaches_the_debt(pipeline: Callable[..., Built]) -> None:
    built = pipeline("business")
    engine = verified(built, answers=("01234567", "1988"))
    assert engine.spoken[0].endswith("May I speak with Ann Lee of Kestrel Joinery Ltd?")
    assert engine.vars["account_state"] == "ok" and engine.vars["product_ids"] == "p1,p2"


def test_four_wrong_business_answers_close_unverified(pipeline: Callable[..., Built]) -> None:
    built = pipeline("business")
    left = iter((3, 2, 1, 0))
    wrong = bank(built, verify_identity=lambda body: {"ok": True, "verified": False, "all_verified": False,
                                                       "wrong_answers_left": next(left)})
    engine = call(built, wrong)
    engine.move("identity")
    for field in ("company_number", "founded_year", "company_number", "founded_year"):
        engine.call_tool("verify_identity", {"field": field, "answer": "1111"})
    assert engine.visited[-5:] == ["identity_failed", "end_identity_failed", "decide", "close", "hangup"]
    assert engine.sent("get_account") == []
    assert engine.sent("close_call") == [{"customer_id": "c-77", "outcome": "unverified", "reason": "exhausted"}]
    assert engine.spoken[-1] == "Please visit a Meridian branch or use online banking. Thank you, goodbye."


def test_the_receptionist_rule_reaches_the_third_party_step_with_call_variables(pipeline: Callable[..., Built]) -> None:
    prompt = pipeline("business").workflow["nodes"]["third_party"]["task_message"]
    assert ("- A receptionist or colleague may offer to take a message: thank them, give no detail and ask when "
            "{{first_name}} {{last_name}} can be reached.") in prompt
    assert 'ask exactly: "May I speak with {{first_name}} {{last_name}} of {{company_name}}?"' in prompt


def test_a_business_brief_without_a_title_variable_never_says_a_title(pipeline: Callable[..., Built]) -> None:
    text = json.dumps(pipeline("business").workflow)
    assert "{{title}}" not in text and "Mr " not in text and "Ms " not in text


@pytest.mark.xfail(strict=True, reason="AUD-12 (open): an undeclared {title} in the right-party question stays literal "
                                       "in the opening / third-party prompt text")
def test_an_undeclared_placeholder_in_the_right_party_question_is_never_left_literal(tmp_path: Any) -> None:
    variant = V.business(V.base())
    variant.ir["phrases"]["right_party_question"] = "May I speak with {title} {last_name}?"
    built = run_pipeline(variant, "staged", tmp_path)
    hits = [(where, m.group(0)) for where, text in texts(built.workflow) for m in SINGLE_BRACE.finditer(text)]
    assert hits == []


@pytest.mark.xfail(strict=True, reason="AUD-15 (open): a postcode is sent in capitals with single spaces, although the "
                                       "contract and the IR say 'capital letters and digits without spaces'")
@pytest.mark.parametrize("said", ["sw1a 1aa", "S W 1 A 1 A A"])
def test_a_postcode_is_sent_in_the_format_the_contract_states(pipeline: Callable[..., Built], said: str) -> None:
    built = pipeline("base")
    engine = call(built)
    engine.move("identity")
    engine.call_tool("verify_identity", {"field": "postcode", "answer": said}, route=False)
    assert engine.sent("verify_identity")[0]["answer"] == "SW1A1AA"


# ---- 4. a reworded tool contract -------------------------------------------------------------------------------------------

RENAMED_PRODUCTS = [
    {"id": "p1", "type": "card", "card_last4": "1111", "overdue_cents": 30000, "days_overdue": 20,
     "min_due_cents": 4500},
    {"id": "p2", "type": "vehicle_finance", "overdue_cents": 80000, "days_overdue": 40, "monthly_instalment": 210.0},
]


def renamed_call(built: Built, **answers: Any) -> Engine:
    return verified(built, bank(built, RENAMED_PRODUCTS, **answers))


def test_renamed_fields_are_sent_by_every_engine_call(pipeline: Callable[..., Built]) -> None:
    built = pipeline("renamed_contract")
    engine = renamed_call(built)
    assert engine.sent("verify_identity")[0] == {"customer_ref": "c-77", "field": "postcode", "answer": "AB12CD"}
    assert engine.sent("get_account") == [{"customer_ref": "c-77"}]
    assert engine_body(built, "handoff_front_desk") == {"customer_ref": "{{customer_id}}", "team": "front_desk"}
    assert engine_body(built, "close") == {"customer_ref": "{{customer_id}}", "disposition": "{{final_outcome}}",
                                           "disposition_reason": "{{final_reason}}"}


def test_renamed_account_items_in_cents_are_read_by_the_facts(pipeline: Callable[..., Built]) -> None:
    engine = renamed_call(pipeline("renamed_contract"))
    assert engine.vars["product_ids"] == "p1,p2"
    assert (engine.vars["p1_past_due"], engine.vars["p1_floor"]) == ("300.00", "45.00")
    assert (engine.vars["p2_past_due"], engine.vars["p2_floor"]) == ("800.00", "210.00")
    assert "Your card ending 1111 has a past-due amount of 300.00 and is 20 days late." in engine.spoken[-1]


def test_a_renamed_promise_contract_gets_cents_an_iso_day_and_the_fixed_channel(pipeline: Callable[..., Built]) -> None:
    built = pipeline("renamed_contract")
    engine = renamed_call(built, create_promise={"accepted": True})
    sent, advice = engine.call_tool("create_promise", {"account_ref": "p1", "amount_minor": "300", "pay_on": "2026-11-04"})
    assert sent and advice.startswith("The promise IS recorded.")
    assert engine.sent("create_promise") == [{"account_ref": "p1", "amount_minor": 30000, "pay_on": "2026-11-04",
                                              "customer_ref": "c-77", "channel": "voice"}]
    assert isinstance(engine.sent("create_promise")[0]["amount_minor"], int)
    assert engine.vars["promise_acked"] == "yes"
    engine.move("end_promise_recorded")
    assert engine.sent("close_call") == [{"customer_ref": "c-77", "disposition": "agreed", "disposition_reason": ""}]


def test_the_reworded_success_flag_is_the_one_that_is_read(pipeline: Callable[..., Built]) -> None:
    built = pipeline("renamed_contract")
    engine = renamed_call(built, create_promise={"accepted": False, "ok": True}, add_note={"saved": False, "ok": True})
    sent, advice = engine.call_tool("create_promise", {"account_ref": "p1", "amount_minor": 300, "pay_on": "2026-11-04"})
    assert sent and advice.startswith("The promise was NOT recorded.")
    assert engine.vars["promise_acked"] == "no" and not engine.vars.get("promised_products")
    sent, advice = engine.call_tool("add_note", {"product_id": "p2", "note_code": "rejected"})
    assert sent and engine.sent("add_note") == [{"product_id": "p2", "note_code": "rejected", "customer_ref": "c-77"}]
    assert not engine.vars.get("notes_log")
    engine.move("end_flat_refusal")
    assert engine.sent("close_call")[0]["disposition"] != "agreed"


def test_a_renamed_note_and_callback_contract_is_sent_under_its_own_names(pipeline: Callable[..., Built]) -> None:
    built = pipeline("renamed_contract")
    engine = renamed_call(built, add_note={"saved": True})
    engine.call_tool("add_note", {"product_id": "p1", "note_code": "contested"})
    engine.call_tool("add_note", {"product_id": "p2", "note_code": "rejected"})
    assert engine.vars["notes_log"] == "p1:contested;p2:rejected;"
    engine.move("end_flat_refusal")
    assert engine.sent("close_call") == [{"customer_ref": "c-77", "disposition": "declined",
                                          "disposition_reason": "contested"}]
    third = call(built)
    third.move("third_party")
    sent, _ = third.call_tool("schedule_callback", {"callback_date": "2026-11-04", "callback_time": "9am"})
    assert sent and third.sent("schedule_callback") == [
        {"callback_date": "2026-11-04", "callback_time": "09:00", "customer_ref": "c-77"}]
    assert third.visited[-5:] == ["callback_done", "end_callback_booked", "decide", "close", "hangup"]
    assert third.sent("close_call")[0]["disposition"] == "unreachable"


def test_the_lean_tools_offer_the_renamed_fields(pipeline: Callable[..., Built]) -> None:
    workflow = pipeline("renamed_contract", "lean").workflow
    fields = {(node, t["function"]["name"]): sorted(t["function"]["parameters"]["properties"]["body"]["properties"])
              for node, n in workflow["nodes"].items() for t in n.get("tools") or []}
    assert fields[("negotiate", "create_promise")] == ["account_ref", "amount_minor", "channel", "customer_ref", "pay_on"]
    assert fields[("negotiate", "add_note")] == ["customer_ref", "note_code", "product_id"]
    assert fields[("call", "schedule_callback")] == ["callback_date", "callback_time", "customer_ref"]
    assert fields[("call", "close_call")] == ["customer_ref", "disposition", "disposition_reason"]
    assert fields[("call", "transfer_to_agent")] == ["customer_ref", "team"]


@pytest.mark.xfail(strict=True, reason="AUD-48 (open): a date field the contract documents without a format (pay_on) is "
                                       "not rebuilt from a non-ISO date, so the guard blocks it as unreadable")
def test_a_non_iso_day_in_a_date_field_without_a_format_is_rebuilt(pipeline: Callable[..., Built]) -> None:
    built = pipeline("renamed_contract")
    engine = renamed_call(built, create_promise={"accepted": True})
    sent, _ = engine.call_tool("create_promise", {"account_ref": "p1", "amount_minor": 300, "pay_on": "4/11/2026"})
    assert sent and engine.sent("create_promise")[0]["pay_on"] == "2026-11-04"


def test_a_non_iso_day_in_a_documented_date_field_is_rebuilt(pipeline: Callable[..., Built]) -> None:
    engine = verified(pipeline("base"))
    sent, _ = engine.call_tool("create_promise", {"product_id": "p1", "amount": 300, "date": "4/11/2026"})
    assert sent and engine.sent("create_promise")[0]["date"] == "2026-11-04"


def test_a_string_status_rejection_is_never_acknowledged(pipeline: Callable[..., Built]) -> None:
    """AUD-04 (string flags): a string status ('recorded' / 'rejected') is read, never the HTTP envelope alone."""
    built = pipeline("string_status")
    engine = verified(built, bank(built, create_promise={"status": "rejected"}))
    _, advice = engine.call_tool("create_promise", {"product_id": "p1", "amount": 300, "date": "2026-11-04"})
    assert engine.vars["promise_acked"] == "no" and "NOT recorded" in advice


def test_a_string_status_success_is_acknowledged(pipeline: Callable[..., Built]) -> None:
    built = pipeline("string_status")
    engine = verified(built, bank(built, create_promise={"status": "recorded"}))
    _, advice = engine.call_tool("create_promise", {"product_id": "p1", "amount": 300, "date": "2026-11-04"})
    assert engine.vars["promise_acked"] == "yes" and "IS recorded" in advice


# ---- 5. a renamed clock ----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("name", "clock"), [("renamed_clock", "call_started_at"),
                                             ("clock_without_example", "current_time")])
@pytest.mark.parametrize("now", ["2026-11-02T10:00", "2026-11-06T16:45", "2026-11-27T09:00"])
def test_the_date_code_reads_the_renamed_clock(pipeline: Callable[..., Built], name: str, clock: str, now: str) -> None:
    built = pipeline(name)
    assert f"the date code reads the call variable {clock!r} as the current date and time" in built.notes
    assert "{{now}}" not in json.dumps(built.workflow)
    renamed = facts(built, [CARD, VEHICLE], now, clock)
    reference = facts(pipeline("base"), [CARD, VEHICLE], now)
    assert renamed["setup_state"] == "ok" and renamed["today_iso"] == now[:10]
    keys = [k for k in reference if k.startswith(("allowed_", "p1_", "p2_", "deadline_", "today", "next_business"))]
    assert {k: renamed[k] for k in keys} == {k: reference[k] for k in keys}


def test_the_renamed_clock_drives_the_whole_call(pipeline: Callable[..., Built]) -> None:
    built = pipeline("renamed_clock")
    engine = Engine(built.workflow, {**CALL, "call_started_at": "2026-11-06T16:45"}, bank(built))
    engine.go("setup")
    engine.move("identity")
    for field in ("postcode", "birth_year"):
        engine.call_tool("verify_identity", {"field": field, "answer": "AB12CD"})
    assert engine.vars["today_iso"] == "2026-11-06" and engine.vars["p2_deadline"] == "2026-11-11"
    sent, _ = engine.call_tool("create_promise", {"product_id": "p2", "amount": 800, "date": "2026-11-12"}, route=False)
    assert not sent  # after the red band's deadline counted from the call's clock


# ---- 6. a percentage floor ----------------------------------------------------------------------------------------------


def test_a_percentage_floor_is_computed_offered_and_enforced(pipeline: Callable[..., Built]) -> None:
    built = pipeline("percent_floor")
    card = {**CARD, "past_due": 301.10}
    card.pop("min_due")
    state = facts(built, [card, VEHICLE])
    assert state["p1_floor"] == "75.28"  # 25 % of 301.10 = 75.275, rounded up to the cent
    assert state["p2_floor"] == "210.00"  # the vehicle finance keeps its floor field
    assert ('"I can offer you the minimum payment of 75.28 instead. The rest stays overdue."'
            in state["account_facts"])
    for amount, allowed in (("75.28", True), ("75.27", False), ("301.10", True), ("301.11", False), ("150", True)):
        sent, out = promise_guard(built, state, {"customer_id": "c", "product_id": "p1", "amount": amount,
                                                 "date": "2026-11-04"})
        assert sent is allowed, (amount, out["vars"].get("promise_guard"))


# ---- 7. three products with per-product rules ---------------------------------------------------------------------------

THREE = [
    {**CARD, "days_overdue": 40},
    {"product_id": "p2", "type": "loan", "past_due": 500.0, "days_overdue": 40, "monthly_instalment": 120.0},
    {**VEHICLE, "product_id": "p3", "days_overdue": 5},
]


def test_each_product_gets_its_own_table_row_deadline_and_floor(pipeline: Callable[..., Built]) -> None:
    state = facts(pipeline("three_products"), THREE)
    assert state["product_count"] == "3" and state["product_ids"] == "p1,p2,p3"
    rows = {p: (state[f"{p}_band"], state[f"{p}_deadline"], state[f"{p}_floor"]) for p in ("p1", "p2", "p3")}
    assert rows == {"p1": ("card_late", "2026-11-05", "45.00"), "p2": ("red", "2026-11-06", "120.00"),
                    "p3": ("green", "2026-11-17", "210.00")}
    assert state["allowed_p1"] == "2026-11-02,2026-11-04,2026-11-05"
    assert "Persuasion attempts for p1: EXACTLY 2 (card_late band)" in state["account_facts"]
    assert "Persuasion attempts for p2: EXACTLY 3 (red band)" in state["account_facts"]
    assert "Persuasion attempts for p3: EXACTLY 1 (green band)" in state["account_facts"]


def test_the_promise_guard_applies_each_product_s_own_deadline_and_floor(pipeline: Callable[..., Built]) -> None:
    built = pipeline("three_products")
    state = facts(built, THREE)
    cases = [("p1", 300, "2026-11-06", False), ("p2", 500, "2026-11-06", True), ("p3", 800, "2026-11-17", True),
             ("p2", 119.99, "2026-11-04", False), ("p3", 210, "2026-11-04", True), ("p4", 100, "2026-11-04", False)]
    for product, amount, date, allowed in cases:
        sent, out = promise_guard(built, state, {"customer_id": "c", "product_id": product, "amount": amount,
                                                 "date": date})
        assert sent is allowed, (product, amount, date, out["vars"].get("promise_guard"))


def test_three_products_close_by_the_first_non_settled_note_or_agreed(pipeline: Callable[..., Built]) -> None:
    built = pipeline("three_products")
    engine = verified(built, bank(built, THREE))
    assert engine.spoken[-1].count(" has a past-due amount of ") == 3
    for product, reason in (("p1", "settled"), ("p2", "contested"), ("p3", "rejected")):
        engine.call_tool("add_note", {"product_id": product, "reason": reason})
    engine.move("end_flat_refusal")
    assert engine.sent("close_call")[0]["outcome"] == "declined"
    assert engine.sent("close_call")[0]["reason"] == "contested"
    paid = verified(built, bank(built, THREE))
    for product in ("p1", "p2", "p3"):
        paid.call_tool("add_note", {"product_id": product, "reason": "settled"})
    paid.move("end_flat_refusal")
    assert paid.sent("close_call")[0]["outcome"] == "settled"
    mixed = verified(built, bank(built, THREE))
    mixed.call_tool("create_promise", {"product_id": "p3", "amount": 210, "date": "2026-11-16"})
    mixed.call_tool("add_note", {"product_id": "p1", "reason": "rejected"})
    mixed.move("end_promise_recorded")
    assert mixed.sent("close_call") == [{"customer_id": "c-77", "outcome": "agreed", "reason": ""}]


# ---- 8. another calendar and other deadline rules ------------------------------------------------------------------------

# Sunday-to-Thursday week; holidays Tue 2031-03-04, Tue 2031-03-11, Tue 2031-04-01. Bands: green = end of the month,
# amber = 9 calendar days (moved back to a business day), red = 3rd business day. 2031-03-01 is a Saturday.
CALENDAR_CASES = {
    "2031-03-02T09:00": ("yes", "2031-03-03", "2031-03-31", "2031-03-10", "2031-03-06",
                         "2031-03-02,2031-03-03,2031-03-05,2031-03-06,2031-03-09,2031-03-10"),
    "2031-03-06T16:00": ("yes", "2031-03-09", "2031-03-31", "2031-03-13", "2031-03-12",
                         "2031-03-06,2031-03-09,2031-03-10,2031-03-12,2031-03-13"),
    "2031-03-28T10:00": ("no", "2031-03-30", "2031-03-31", "2031-04-06", "2031-04-02",
                         "2031-03-30,2031-03-31,2031-04-02,2031-04-03,2031-04-06"),
    "2031-03-30T10:00": ("yes", "2031-03-31", "2031-03-31", "2031-04-08", "2031-04-03",
                         "2031-03-30,2031-03-31,2031-04-02,2031-04-03,2031-04-06,2031-04-07,2031-04-08"),
}
CAL_PRODUCTS = [
    {"product_id": "g", "type": "card", "card_last4": "1111", "past_due": 300.0, "days_overdue": 5, "min_due": 45.0},
    {"product_id": "a", "type": "loan", "past_due": 500.0, "days_overdue": 20, "monthly_instalment": 120.0},
]


@pytest.mark.parametrize("now", list(CALENDAR_CASES))
def test_another_week_holidays_and_deadline_rules_give_the_right_dates(pipeline: Callable[..., Built], now: str) -> None:
    built = pipeline("other_calendar")
    business, next_day, green, amber, red, allowed_amber = CALENDAR_CASES[now]
    state = facts(built, CAL_PRODUCTS, now)
    assert (state["today_is_business_day"], state["next_business_day_iso"]) == (business, next_day)
    assert (state["g_deadline"], state["a_deadline"]) == (green, amber)
    assert state["allowed_a"] == allowed_amber
    red_state = facts(built, [{**CAL_PRODUCTS[1], "product_id": "r", "days_overdue": 45}], now)
    assert red_state["r_deadline"] == red


def test_the_compiled_dates_match_the_reference_calendar_on_every_day_of_two_months(
    pipeline: Callable[..., Built],
) -> None:
    import datetime as dt

    built = pipeline("other_calendar")
    cal = BusinessCalendar.from_ir(built.ir.calendar)
    for offset in range(61):
        today = dt.date(2031, 3, 1) + dt.timedelta(days=offset)
        state = facts(built, CAL_PRODUCTS, f"{today.isoformat()}T10:00")
        for pid, band in (("g", built.ir.bands[0]), ("a", built.ir.bands[1])):
            deadline = latest_valid_promise_date(deadline_for(band.deadline, today, cal), cal)
            allowed = [today + dt.timedelta(days=d) for d in range((deadline - today).days + 1)]
            expected = ",".join(d.isoformat() for d in allowed if cal.is_business_day(d))
            assert (state[f"{pid}_deadline"], state[f"allowed_{pid}"]) == (deadline.isoformat(), expected), today


def test_a_weekend_or_holiday_promise_is_moved_to_the_next_business_day(pipeline: Callable[..., Built]) -> None:
    built = pipeline("other_calendar")
    state = facts(built, CAL_PRODUCTS, "2031-03-02T09:00")
    for date, offered in (("2031-03-04", "2031-03-05"), ("2031-03-07", "2031-03-09"), ("2031-03-08", "2031-03-09")):
        sent, out = promise_guard(built, state, {"customer_id": "c", "product_id": "g", "amount": 300, "date": date})
        assert not sent and out["vars"]["promise_guard"].startswith("blocked"), date
        assert offered[-2:].lstrip("0") in out["vars"]["promise_guard_say"], (date, out["vars"]["promise_guard_say"])
    sent, _ = promise_guard(built, state, {"customer_id": "c", "product_id": "g", "amount": 300, "date": "2031-03-02"})
    assert sent  # a Sunday is a business day of this bank


# ---- 9. an outcome table with extra outcomes ----------------------------------------------------------------------------


@pytest.mark.parametrize("via", [(), ("third_party",)])
def test_a_deceased_customer_closes_with_the_brief_s_own_outcome(pipeline: Callable[..., Built], via: tuple[str, ...]) -> None:
    engine = call(pipeline("extra_outcomes"))
    for step in via:
        engine.move(step)
    engine.move("end_deceased")
    assert engine.sent("close_call") == [{"customer_id": "c-77", "outcome": "bereavement", "reason": "notified"}]


def test_a_callback_the_verified_customer_asks_for_is_booked_and_closes_call_back(pipeline: Callable[..., Built]) -> None:
    built = pipeline("extra_outcomes")
    engine = verified(built)
    assert "schedule_callback" in [t["function"]["name"] for t in built.workflow["nodes"]["negotiate"]["tools"]]
    sent, _ = engine.call_tool("schedule_callback", {"date": "2026-11-02", "time": "18:00"})
    assert not sent and engine.visited[-1] == "negotiate" and "callback_fix_negotiate" in engine.visited
    sent, advice = engine.call_tool("schedule_callback", {"date": "2026-11-04", "time": "10:30"})
    assert sent and advice.startswith("The callback is booked.")
    assert engine.visited[-4:] == ["end_callback_request", "decide", "close", "hangup"]
    assert engine.sent("close_call") == [{"customer_id": "c-77", "outcome": "call_back", "reason": "customer_request"}]


def test_a_callback_request_without_a_booking_falls_back_to_the_refusal_row(pipeline: Callable[..., Built]) -> None:
    engine = verified(pipeline("extra_outcomes"))
    engine.move("end_callback_request")
    assert engine.sent("close_call") == [{"customer_id": "c-77", "outcome": "declined", "reason": "rejected"}]


# ---- 10. another decline limit -------------------------------------------------------------------------------------------

DECLINE_LINES = {
    "decline_max_1": "- If the customer refuses to answer: thank them, say goodbye and ",
    "base": "The second refusal or such question in a row, with no answer in between: thank them, say goodbye and ",
    "decline_max_3": "The third refusal or such question in a row, with no answer in between: thank them, say goodbye and ",
}


@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize("name", list(DECLINE_LINES))
def test_the_decline_limit_of_the_brief_is_the_one_the_agent_follows(
    pipeline: Callable[..., Built], name: str, profile: str,
) -> None:
    built = pipeline(name, profile)
    prompt = built.workflow["nodes"]["identity" if profile == "staged" else "call"]["task_message"]
    assert DECLINE_LINES[name] in prompt
    for other, line in DECLINE_LINES.items():
        if other != name:
            assert line not in prompt


def test_a_declined_identity_check_closes_with_the_brief_s_outcome(pipeline: Callable[..., Built]) -> None:
    engine = call(pipeline("decline_max_1"))
    engine.move("identity")
    engine.move("end_identity_declined")
    assert engine.sent("close_call") == [{"customer_id": "c-77", "outcome": "unverified", "reason": "declined_checks"}]


# ---- 11. callback windows ------------------------------------------------------------------------------------------------


def callback_guard(built: Built, now: str, date: str, time: str) -> tuple[bool, str]:
    state = run_snippet(extract_code_snippets(built.workflow)["setup"], {**CALL, "now": now})
    tool = next(t for t in built.workflow["nodes"]["third_party"]["tools"]
                if t["function"]["name"] == "schedule_callback")
    out = run_hook(tool["pre_process_code"], vars=state, config={"url": "u", "body": {"date": date, "time": time}})
    return out["config"]["url"] == "u", out["config"]["body"]["time"]


@pytest.mark.parametrize(("name", "now", "date", "time", "allowed"), [
    # base: today or the next business day, 09:00-17:30 both included, later than now
    ("base", "2026-11-02T10:00", "2026-11-02", "10:00", False),
    ("base", "2026-11-02T10:00", "2026-11-02", "10:15", True),
    ("base", "2026-11-02T10:00", "2026-11-02", "17:30", True),
    ("base", "2026-11-02T10:00", "2026-11-02", "17:31", False),
    ("base", "2026-11-02T10:00", "2026-11-03", "11:00", False),  # the holiday is not the next business day
    ("base", "2026-11-02T10:00", "2026-11-04", "09:00", True),
    ("base", "2026-11-02T10:00", "2026-11-05", "11:00", False),
    # variant: only the next business day, 08:30-12:00 with the end excluded
    ("callback_window", "2026-11-02T10:00", "2026-11-02", "11:00", False),
    ("callback_window", "2026-11-02T10:00", "2026-11-04", "08:30", True),
    ("callback_window", "2026-11-02T10:00", "2026-11-04", "11:59", True),
    ("callback_window", "2026-11-02T10:00", "2026-11-04", "12:00", False),
    ("callback_window", "2026-11-02T10:00", "2026-11-04", "8am", False),
    ("callback_window", "2026-11-02T10:00", "2026-11-04", "11.30", True),
    ("callback_window", "2026-11-06T11:30", "2026-11-09", "10:00", True),  # Friday -> Monday
    ("callback_window", "2026-11-06T11:30", "2026-11-06", "11:45", False),
])
def test_the_callback_guard_allows_exactly_the_brief_s_window(
    pipeline: Callable[..., Built], name: str, now: str, date: str, time: str, allowed: bool,
) -> None:
    sent, _ = callback_guard(pipeline(name), now, date, time)
    assert sent is allowed


def test_a_relative_who_offers_no_allowed_time_twice_closes_without_a_callback(pipeline: Callable[..., Built]) -> None:
    engine = call(pipeline("callback_window"))
    engine.move("third_party")
    for time in ("13:00", "14:00"):
        sent, _ = engine.call_tool("schedule_callback", {"date": "2026-11-04", "time": time})
        assert not sent
    assert engine.sent("schedule_callback") == []
    assert engine.sent("close_call") == [{"customer_id": "c-77", "outcome": "unreachable", "reason": "no_rebook"}]


def test_a_relative_who_gives_an_allowed_time_books_the_callback(pipeline: Callable[..., Built]) -> None:
    engine = call(pipeline("callback_window"))
    engine.move("third_party")
    sent, _ = engine.call_tool("schedule_callback", {"date": "2026-11-04", "time": "13:00"})
    assert not sent and engine.node == "third_party"
    sent, _ = engine.call_tool("schedule_callback", {"date": "2026-11-04", "time": "09:15"})
    assert sent and engine.sent("schedule_callback") == [{"date": "2026-11-04", "time": "09:15",
                                                          "customer_id": "c-77"}]
    assert engine.sent("close_call") == [{"customer_id": "c-77", "outcome": "unreachable", "reason": "rebook"}]


# ---- 12. an amended rule -------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("profile", PROFILES)
def test_a_superseded_rule_is_replaced_by_its_amendment(pipeline: Callable[..., Built], profile: str) -> None:
    built = pipeline("amended_rule", profile)
    assert "D1-26a" in built.coverage.superseded and "M-1" in built.coverage.covered
    text = json.dumps(built.workflow)
    old = built.ledger.get("D1-26a")
    assert old is not None and old.statement not in text and old.quote not in text
    assert "If they say the full amount is too much: offer the floor two times at most" in text
    assert "too much: offer the floor once" not in text  # (an amount below the floor is still met once: rule D1-27b)


@pytest.mark.parametrize("profile", PROFILES)
def test_an_amendment_without_a_structured_field_still_reaches_the_agent(
    pipeline: Callable[..., Built], profile: str,
) -> None:
    built = pipeline("amended_rule", profile)
    assert any("M-2" in line for line in built.fixes)
    prompt = built.workflow["nodes"]["negotiate"]["task_message"]
    assert "[M-2] A note for a vehicle finance product also states the vehicle's registration" in prompt
