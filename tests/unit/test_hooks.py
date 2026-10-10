"""Tests for builder.compiler.hooks: the generated pre/post hooks are executed in the sandbox."""

from __future__ import annotations

from typing import Any

import pytest

from builder.checks.code_sandbox import run_hook
from builder.compiler import hooks


def _post(code: str, data: Any, status: str = "success") -> dict[str, Any]:
    result: dict[str, Any] = {"status": status, "header": {"X": "1"}, "message": "m"}
    if data is not None:
        result["data"] = data
    return run_hook(code, result=result)["result"]


POST = hooks.post_hook(
    [
        (hooks.flag_true("all_verified"), "ALL"),
        (hooks.all_of(hooks.flag_false("ok"), hooks.is_empty("hint")), "UNREADABLE"),
        (hooks.is_value("hint", "swapped"), "SWAPPED"),
        (hooks.flag_true("verified"), "CORRECT"),
        (hooks.is_zero("left"), "ENDED"),
    ],
    default="WRONG",
    error_message="SERVICE",
)


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ({"ok": True, "all_verified": True}, "ALL"),
        ({"ok": True, "all_verified": "True"}, "ALL"),
        ({"ok": False}, "UNREADABLE"),
        ({"ok": False, "hint": "swapped"}, "SWAPPED"),
        ({"ok": True, "verified": True, "left": 3}, "CORRECT"),
        ({"ok": True, "verified": False, "left": 0}, "ENDED"),
        ({"ok": True, "verified": False, "left": 2}, "WRONG"),
        ({"ok": True, "verified": False, "left": 0.0}, "ENDED"),
    ],
)
def test_post_hook_rules_in_order(data: dict[str, Any], expected: str) -> None:
    out = _post(POST, dict(data))
    assert out["data"]["what_to_do_next"] == expected
    assert "header" not in out


def test_post_hook_without_data_reports_a_service_error() -> None:
    assert _post(POST, None, status="error")["what_to_do_next"] == "SERVICE"
    assert "what_to_do_next" not in _post(POST, None, status="success")


def test_post_hook_never_raises_on_odd_results() -> None:
    for result in ({}, {"data": "text"}, {"data": None}, {"data": {"left": object()}}):
        run_hook(POST, result=result)  # must not raise


def test_post_hook_without_default_writes_nothing_when_no_rule_matches() -> None:
    code = hooks.post_hook([(hooks.flag_true("ok"), "fine")])
    assert "what_to_do_next" not in _post(code, {"ok": False})["data"]


def test_post_hook_messages_with_braces_stay_brace_free() -> None:
    code = hooks.post_hook([("True", "say {x} and {{y}}")])
    assert "{{" not in code and "}}" not in code
    assert _post(code, {})["data"]["what_to_do_next"] == "say {x} and {{y}}"


@pytest.mark.parametrize(
    ("kind", "raw", "expected"),
    [
        ("money", "420", 420.0),
        ("money", "$1,250.50", 1250.5),
        ("money", "637,50 dollars", 637.5),
        ("money", 99.999, 100.0),
        ("date_iso", "2026-10-16T00:00:00", "2026-10-16"),
        ("digits", "4-4 7 1", "4471"),
        ("time_hhmm", "9.5", "09:5"),
        ("time_hhmm", "11:00:00", "11:00"),
        ("token", " Refused ", "refused"),
        ("text", "  hi ", "hi"),
    ],
)
def test_pre_hook_coerce_kinds(kind: str, raw: Any, expected: Any) -> None:
    code = hooks.pre_hook_coerce({"f": kind})  # type: ignore[dict-item]
    assert run_hook(code, config={"body": {"f": raw}})["config"]["body"]["f"] == expected


def test_pre_hook_coerce_leaves_missing_and_unreadable_values_alone() -> None:
    code = hooks.pre_hook_coerce({"amount": "money", "date": "date_iso"})
    body = run_hook(code, config={"body": {"amount": "lots"}})["config"]["body"]
    assert body == {"amount": "lots"}  # nothing is written back empty
    assert run_hook(code, config={})["config"] == {}
    assert run_hook(code, config={"body": "not a dict"})["config"] == {"body": "not a dict"}


def test_pre_hook_product_id_completes_a_bare_number() -> None:
    code = hooks.pre_hook_coerce({"product_id": "product_id"})
    state = {"product_ids": "p1,p2,p3"}
    for raw, expected in (("2", "p2"), (" P3 ", "p3"), ("p1", "p1"), ("9", "9")):
        assert run_hook(code, vars=state, config={"body": {"product_id": raw}})["config"]["body"]["product_id"] == expected


def test_notes_log_hook_appends_one_entry_per_call() -> None:
    code = hooks.pre_hook_notes_log()
    first = run_hook(code, vars={"product_ids": "p1,p2"}, config={"body": {"product_id": "1", "reason": "Refused"}})
    assert first["vars"]["notes_log"] == "p1:refused;"
    second = run_hook(code, vars=first["vars"], config={"body": {"product_id": "p2", "reason": "paid"}})
    assert second["vars"]["notes_log"] == "p1:refused;p2:paid;"
    unchanged = run_hook(code, vars={"notes_log": "x:y;"}, config={"body": {"product_id": "p1"}})
    assert unchanged["vars"]["notes_log"] == "x:y;"


@pytest.mark.parametrize(
    ("order", "field", "raw", "expected"),
    [
        ("DMY", "dob", "14/03/1988", "1988-03-14"),
        ("DMY", "dob", "14 03 1988", "1988-03-14"),
        ("DMY", "dob", "1988-03-14", "1988-03-14"),
        ("DMY", "dob", "14 March 1988", "14 March 1988"),
        ("MDY", "dob", "03/14/1988", "1988-03-14"),
        ("DMY", "pin", "4-4 7 1", "4471"),
        ("DMY", "year", "nineteen 88", "88"),
        ("DMY", "other", "keep this", "keep this"),
    ],
)
def test_identity_answer_hook(order: str, field: str, raw: str, expected: str) -> None:
    code = hooks.pre_hook_identity_answers(
        "field", "answer", {"dob": "date_iso", "pin": "digits", "year": "year"}, order)  # type: ignore[arg-type]
    body = run_hook(code, config={"body": {"field": field, "answer": raw}})["config"]["body"]
    assert body["answer"] == expected


def test_join_hooks_skips_empty_and_stays_valid() -> None:
    joined = hooks.join_hooks(hooks.pre_hook_coerce({"a": "text"}), "", hooks.pre_hook_notes_log())
    assert joined.count("try:") >= 2 and "{{" not in joined
    assert hooks.join_hooks("", "  ") == ""
    out = run_hook(joined, vars={}, config={"body": {"a": " x ", "product_id": "p1", "reason": "paid"}})
    assert out["config"]["body"]["a"] == "x" and out["vars"]["notes_log"] == "p1:paid;"


def test_promise_guard_without_date_checks_blocks_amounts_only() -> None:
    code = hooks.join_hooks(
        hooks.pre_hook_coerce({"amount": "money"}),
        hooks.pre_hook_promise_guard("product_id", "amount", "date", enabled_dates=False, currency="dollars"),
    )
    facts = {"product_ids": "p1", "allowed_p1": "2026-10-14", "p1_floor": "10.00", "p1_past_due": "50.00",
             "p1_name": "card"}
    late = run_hook(code, vars=dict(facts), config={"url": "u", "body": {"product_id": "p1", "amount": 20, "date": "2030-01-01"}})
    assert late["config"]["url"] == "u" and late["vars"]["promise_guard"] == "ok"
    low = run_hook(code, vars=dict(facts), config={"url": "u", "body": {"product_id": "p1", "amount": 5, "date": "2026-10-14"}})
    assert low["config"]["url"] == ""
    assert low["vars"]["promise_guard_say"] == (
        "I am sorry, for your card I can record an amount from 10.00 to 50.00 dollars. Which amount can you pay?")


def test_the_guard_default_sentence_is_exported() -> None:
    assert hooks.PROMISE_FIX_DEFAULT == "I am sorry, I could not record that. Let me check the details again."


# ---- audit C-02: a response without a success flag is judged by the envelope ---------------------------------------------------


def test_envelope_ok_rule_reads_the_status_of_the_result_not_a_field() -> None:
    code = hooks.post_hook([(hooks.envelope_ok(), "DONE")], default="FAILED", error_message="SERVICE")
    assert _post(code, {"anything": 1})["data"]["what_to_do_next"] == "DONE"
    assert _post(code, {"anything": 1}, status="error")["data"]["what_to_do_next"] == "FAILED"
    assert _post(code, None, status="error")["what_to_do_next"] == "SERVICE"


def test_is_count_matches_integers_and_floats() -> None:
    code = hooks.post_hook([(hooks.is_count("strikes", 3), "ENDED")], default="GO")
    for value, expected in ((3, "ENDED"), (3.0, "ENDED"), ("3", "ENDED"), (2, "GO"), (None, "GO")):
        assert _post(code, {"strikes": value})["data"]["what_to_do_next"] == expected


# ---- audit C-08: enum values and ids keep the spelling of the data ------------------------------------------------------------


def test_enum_tokens_are_written_back_in_the_contracts_spelling() -> None:
    code = hooks.pre_hook_coerce({"reason": "token"}, enums={"reason": ["Refused", "Service_error", "paid"]})
    for raw, expected in (("refused", "Refused"), (" REFUSED ", "Refused"), ("service error", "Service_error"),
                          ("service-error", "Service_error"), ("Paid", "paid"), ("maybe", "maybe")):
        body = run_hook(code, config={"body": {"reason": raw}})["config"]["body"]
        assert body["reason"] == expected, raw
    assert run_hook(code, config={"body": {}})["config"]["body"] == {}


def test_a_token_without_an_enum_is_still_lower_cased() -> None:
    code = hooks.pre_hook_coerce({"f": "token"}, enums={"other": ["X"]})
    assert run_hook(code, config={"body": {"f": " Refused "}})["config"]["body"]["f"] == "refused"


def test_product_ids_are_matched_ignoring_case_spaces_and_dashes() -> None:
    code = hooks.pre_hook_coerce({"product_id": "product_id"})
    state = {"product_ids": "CC-4821,LN-77"}
    for raw, expected in (("CC-4821", "CC-4821"), ("cc-4821", "CC-4821"), (" cc 4821 ", "CC-4821"), ("ln77", "LN-77"),
                          ("4821", "CC-4821"), ("ZZ-9", "ZZ-9"), ("zz 9", "zz9")):
        body = run_hook(code, vars=state, config={"body": {"product_id": raw}})["config"]["body"]
        assert body["product_id"] == expected, raw
    no_ids = run_hook(code, config={"body": {"product_id": " Cc-1 "}})["config"]["body"]["product_id"]
    assert no_ids == "Cc-1"


def test_the_notes_log_uses_the_canonical_spellings() -> None:
    code = hooks.pre_hook_notes_log("product_id", "reason", reasons=["Refused", "Paid"])
    out = run_hook(code, vars={"product_ids": "CC-4821"}, config={"body": {"product_id": "cc-4821", "reason": "paid"}})
    assert out["vars"]["notes_log"] == "CC-4821:Paid;"


# ---- audit C-06 / C-07: the guard and the lists of allowed dates ----------------------------------------------------------------

GUARD_FACTS = {"product_ids": "p1", "allowed_p1": "2026-10-14,2026-10-15", "p1_floor": "10.00", "p1_past_due": "50.00",
               "p1_name": "card"}


def _guard_run(facts: dict[str, Any], date: str) -> dict[str, Any]:
    code = hooks.join_hooks(
        hooks.pre_hook_coerce({"product_id": "product_id", "amount": "money", "date": "date_iso"}),
        hooks.pre_hook_promise_guard("product_id", "amount", "date"),
    )
    return run_hook(code, vars=dict(facts), config={"url": "u", "body": {
        "product_id": "p1", "amount": 20, "date": date}})


def test_the_guard_blocks_when_no_date_is_left() -> None:
    facts = {**GUARD_FACTS, "allowed_p1": "", "allowed_count_p1": "0"}
    out = _guard_run(facts, "2026-10-15")
    assert out["config"]["url"] == "" and out["vars"]["promise_guard"] == "blocked: no promise date is left for p1"
    assert out["vars"]["promise_guard_say"] == "I am sorry, I cannot record a payment date for your card in this call."
    # without the count (the facts were never written) nothing is blocked on dates
    assert _guard_run({**GUARD_FACTS, "allowed_p1": ""}, "2026-10-15")["config"]["url"] == "u"


def test_the_guard_accepts_dates_beyond_a_truncated_list_only_up_to_its_bound() -> None:
    assert _guard_run(GUARD_FACTS, "2026-10-20")["config"]["url"] == ""
    open_list = {**GUARD_FACTS, "until_p1": "open"}
    assert _guard_run(open_list, "2030-01-02")["config"]["url"] == "u"
    assert _guard_run(open_list, "Friday")["config"]["url"] == ""  # not a date: never accepted by the tail
    bounded = {**GUARD_FACTS, "until_p1": "2026-12-31"}
    assert _guard_run(bounded, "2026-12-31")["config"]["url"] == "u"
    assert _guard_run(bounded, "2027-01-04")["config"]["url"] == ""


def test_the_guard_finds_the_product_whatever_its_case() -> None:
    facts = {"product_ids": "CC-4821", "allowed_CC-4821": "2026-10-15", "CC-4821_floor": "10.00",
             "CC-4821_past_due": "50.00"}
    code = hooks.pre_hook_promise_guard("product_id", "amount", "date")
    out = run_hook(code, vars=facts, config={"url": "u", "body": {"product_id": "cc-4821", "amount": 20,
                                                                 "date": "2026-10-15"}})
    assert out["config"]["url"] == "u" and out["vars"]["promise_guard"] == "ok"
