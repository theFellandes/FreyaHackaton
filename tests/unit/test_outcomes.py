"""Tests for builder.policy.outcomes: the reference close-outcome engine.

1. Every practice case of public-test-set.json: a CallSituation is built from the case (recorded promises, notes,
   callbacks and transfers from its ``expected`` block; who answered, verification and conversation situations from
   its ``script``, by hand) and compute_outcome must give the expected outcome and reason under the fixture IR of its
   brief.
2. Edge cases of Briefs A and B (transfer after a promise, notes rules, recording failures, refusals before and after
   verification, third parties).
3. Consistency between the fixture IRs' intent rules and their outcome tables.
4. The generic mechanics (precedence, promise_wins, transfer_after_promise_outcome, row qualifiers, section and
   final fallbacks, the notes rule variants) on modified IR copies, so they do not depend on the kit briefs.
5. The paid invariant: ONE table of literal (outcome, reason) expectations, run against the reference engine, the
   generated ``decide`` snippet and the hand-authored reference agent's ``decide`` code (they must not share a mistake).
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

from builder.checks.code_sandbox import run_snippet
from builder.compiler import codegen
from builder.policy.ir import Closing, OutcomeRef, OutcomeRow, PolicyIR
from builder.policy.outcomes import (
    CallSituation,
    NoteRecord,
    PromiseRecord,
    Situation,
    active_situations,
    compute_outcome,
    decide_outcome,
    transfer_situation,
)

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "fixtures" / "policy"


def _load_ir(name: str) -> PolicyIR:
    return PolicyIR.model_validate(json.loads((FIXTURES / name).read_text(encoding="utf-8")))


IR_A = _load_ir("brief_a_ir.json")
IR_AB = _load_ir("brief_ab_ir.json")
CASES: dict[str, dict[str, Any]] = {
    case["id"]: case for case in json.loads((REPO / "public-test-set.json").read_text(encoding="utf-8"))
}

# Facts read from each case's script that the expected ledger does not show.
SCRIPT_FACTS: dict[str, dict[str, Any]] = {
    "PUB-01": {"party": "confirmed", "verified": True},
    "PUB-02": {"party": "third_party", "verified": False},  # husband answers, callback tomorrow 11:00
    "PUB-03": {"party": "confirmed", "verified": False, "identity_failed": True},
    "PUB-04": {"party": "confirmed", "verified": True, "situations": {"proposes_date", "robot_question"}},
    "PUB-05": {"party": "confirmed", "verified": True, "situations": {"hardship"}},  # declines the transfer
    "PUB-06": {"party": "confirmed", "verified": True, "situations": {"cant_afford_full"}},
    "PUB-07": {"party": "confirmed", "verified": True, "situations": {"vague_period", "proposes_date"}},
    "PUB-08": {"party": "confirmed", "verified": True, "situations": {"already_paid"}},
    "PUB-09": {"party": "confirmed", "verified": True, "situations": {"restructuring"}},
    "PUB-10": {"party": "confirmed", "verified": True},  # first create_promise fails, the retry succeeds
    "PUB-11": {"party": "confirmed", "verified": True},
    "PUB-12": {"party": "confirmed", "verified": True, "situations": {"flat_refusal"}},
    "PUB-13": {"party": "confirmed", "verified": True, "situations": {"proposes_date"}},
}


def _situation_for(case: dict[str, Any]) -> CallSituation:
    expected = case["expected"]
    product_ids = [f"p{i + 1}" for i in range(len(case["products"]))]
    return CallSituation.from_records(
        promises=expected["promises"],
        notes=expected["notes"],
        transfers=expected["transfers"],
        callbacks=expected["callbacks"],
        product_order=product_ids,
        **SCRIPT_FACTS[case["id"]],
    )


def _ir_for(case: dict[str, Any]) -> PolicyIR:
    return IR_A if case["brief"] == "A" else IR_AB


def test_every_practice_case_has_script_facts() -> None:
    assert set(SCRIPT_FACTS) == set(CASES)
    assert len(CASES) == 13


@pytest.mark.parametrize("case_id", sorted(CASES))
def test_practice_case_outcome(case_id: str) -> None:
    case = CASES[case_id]
    got = compute_outcome(_ir_for(case), _situation_for(case))
    assert got.as_tuple() == (case["expected"]["outcome"], case["expected"]["reason"])


# Brief A cases still close the same way under the merged A+B policy, where the agent also notes every product
# without a promise (Brief B rule 8).
B_NOTES_FOR_A_CASES: dict[str, list[dict[str, str]]] = {
    "PUB-05": [{"product_id": "p1", "reason": "hardship"}],
    "PUB-08": [{"product_id": "p1", "reason": "paid"}],
}


@pytest.mark.parametrize("case_id", sorted(cid for cid, case in CASES.items() if case["brief"] == "A"))
def test_brief_a_cases_under_the_merged_policy(case_id: str) -> None:
    case = CASES[case_id]
    situation = _situation_for(case)
    situation.notes = [NoteRecord(n["product_id"], n["reason"]) for n in B_NOTES_FOR_A_CASES.get(case_id, [])]
    got = compute_outcome(IR_AB, situation)
    assert got.as_tuple() == (case["expected"]["outcome"], case["expected"]["reason"])


# --------------------------------------------------------------------------------------------------------------------
# Edge cases of the kit briefs
# --------------------------------------------------------------------------------------------------------------------

P1 = PromiseRecord("p1", 420.0, "2026-10-14")
P2 = PromiseRecord("p2", 640.0, "2026-10-16")


def _verified(**facts: Any) -> CallSituation:
    return CallSituation(party="confirmed", verified=True, **facts)


@pytest.mark.parametrize(("queue", "reason"), [("support", "support"), ("general", "human"),
                                               ("restructuring", "restructuring")])
def test_transfer_after_a_recorded_promise_is_transferred(queue: str, reason: str) -> None:
    for ir in (IR_A, IR_AB):
        decision = decide_outcome(ir, _verified(promises=[P1], transfer_queue=queue))
        assert decision.outcome.as_tuple() == ("transferred", reason)
        assert decision.source == "transfer_after_promise"


def test_brief_b_restructuring_after_recorded_promise() -> None:
    s = _verified(promises=[P1], transfer_queue="restructuring", situations={"restructuring"},
                  product_order=["p1", "p2", "p3"])
    assert compute_outcome(IR_AB, s).as_tuple() == ("transferred", "restructuring")


def test_brief_b_no_promise_first_non_paid_note_decides() -> None:
    s = _verified(notes=[NoteRecord("p1", "dispute"), NoteRecord("p2", "paid")], product_order=["p1", "p2"],
                  situations={"dispute", "already_paid"})
    decision = decide_outcome(IR_AB, s)
    assert decision.outcome.as_tuple() == ("no_promise", "dispute")
    assert decision.source == "notes" and decision.situation == Situation.PRODUCT_NOTES
    s.notes = [NoteRecord("p1", "paid"), NoteRecord("p2", "dispute")]
    assert compute_outcome(IR_AB, s).as_tuple() == ("no_promise", "dispute"), "paid notes are skipped"


def test_brief_b_notes_follow_get_account_order_not_recording_order() -> None:
    s = _verified(notes=[NoteRecord("p3", "refused"), NoteRecord("p2", "hardship"), NoteRecord("p1", "paid")],
                  product_order=["p1", "p2", "p3"])
    assert compute_outcome(IR_AB, s).as_tuple() == ("no_promise", "hardship")
    s.product_order = []
    assert compute_outcome(IR_AB, s).as_tuple() == ("no_promise", "refused"), "without an order: recorded order"


def test_brief_b_all_products_noted_paid_is_paid() -> None:
    s = _verified(notes=[NoteRecord("p1", "paid"), NoteRecord("p2", "paid")], product_order=["p1", "p2"])
    assert compute_outcome(IR_AB, s).as_tuple() == ("paid", None)


# --------------------------------------------------------------------------------------------------------------------
# The paid invariant: "paid" needs a paid note for EVERY known product (literal expectations, three implementations)
# --------------------------------------------------------------------------------------------------------------------

# (case id, notes in recorded order, known products in get_account order, expected (outcome, reason)). The customer was
# verified and, where no note decides, the call ended with a flat refusal (the generic fallback).
PAID_INVARIANT: list[tuple[str, list[tuple[str, str]], list[str], tuple[str, str | None]]] = [
    ("only the first of two is noted paid", [("p1", "paid")], ["p1", "p2"], ("no_promise", "refused")),
    ("only the second of two is noted paid", [("p2", "paid")], ["p1", "p2"], ("no_promise", "refused")),
    ("both noted paid", [("p1", "paid"), ("p2", "paid")], ["p1", "p2"], ("paid", None)),
    ("both noted paid, recorded in reverse", [("p2", "paid"), ("p1", "paid")], ["p1", "p2"], ("paid", None)),
    ("paid and a dispute", [("p1", "paid"), ("p2", "dispute")], ["p1", "p2"], ("no_promise", "dispute")),
    ("refused second, paid first", [("p2", "refused"), ("p1", "paid")], ["p1", "p2"], ("no_promise", "refused")),
    ("dispute on a product that is the only one noted", [("p2", "dispute")], ["p1", "p2"], ("no_promise", "dispute")),
    ("the first product in get_account order decides", [("p2", "refused"), ("p1", "hardship")], ["p1", "p2"],
     ("no_promise", "hardship")),
    ("a non-paid note beats the missing note", [("p1", "paid"), ("p3", "dispute")], ["p1", "p2", "p3"],
     ("no_promise", "dispute")),
    ("two of three noted paid", [("p1", "paid"), ("p2", "paid")], ["p1", "p2", "p3"], ("no_promise", "refused")),
    ("three of three noted paid", [("p3", "paid"), ("p1", "paid"), ("p2", "paid")], ["p1", "p2", "p3"],
     ("paid", None)),
    ("the latest note of a product counts", [("p1", "refused"), ("p1", "paid"), ("p2", "paid")], ["p1", "p2"],
     ("paid", None)),
    ("the latest note of a product counts (now refused)", [("p1", "paid"), ("p2", "paid"), ("p1", "refused")],
     ["p1", "p2"], ("no_promise", "refused")),
    ("no notes at all: the fallback", [], ["p1", "p2"], ("no_promise", "refused")),
]


def _reference_agent() -> Any:
    spec = importlib.util.spec_from_file_location("brief_a_ref", REPO / "evals" / "reference" / "brief_a_ref.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_reference_decide(notes: list[tuple[str, str]], order: list[str]) -> tuple[str, str | None]:
    variables: dict[str, Any] = {"product_ids": ",".join(order), "chosen_end": "refused"}
    if notes:
        variables["notes_log"] = "".join(f"{pid}:{reason};" for pid, reason in notes)
    state = run_snippet(_reference_agent().DECIDE_CODE, variables)
    return state["final_outcome"], state.get("final_reason")


@pytest.mark.parametrize(("label", "notes", "order", "expected"), PAID_INVARIANT, ids=[c[0] for c in PAID_INVARIANT])
def test_paid_invariant_in_the_reference_engine(label: str, notes: list[tuple[str, str]], order: list[str],
                                                expected: tuple[str, str | None]) -> None:
    s = _verified(notes=[NoteRecord(pid, reason) for pid, reason in notes], product_order=order,
                  situations={"flat_refusal"})
    assert compute_outcome(IR_AB, s).as_tuple() == expected, label


@pytest.mark.parametrize(("label", "notes", "order", "expected"), PAID_INVARIANT, ids=[c[0] for c in PAID_INVARIANT])
def test_paid_invariant_in_the_generated_decide_code(label: str, notes: list[tuple[str, str]], order: list[str],
                                                     expected: tuple[str, str | None]) -> None:
    variables: dict[str, Any] = {"identity_verified": "yes", "product_ids": ",".join(order),
                                 "chosen_end": "flat_refusal"}
    if notes:
        variables["notes_log"] = "".join(f"{pid}:{reason};" for pid, reason in notes)
    state = run_snippet(codegen.decide_outcome_snippet(IR_AB), variables)
    assert (state["final_outcome"], state.get("final_reason")) == expected, label


@pytest.mark.parametrize(("label", "notes", "order", "expected"), PAID_INVARIANT, ids=[c[0] for c in PAID_INVARIANT])
def test_paid_invariant_in_the_hand_written_reference_agent(label: str, notes: list[tuple[str, str]],
                                                            order: list[str], expected: tuple[str, str | None]) -> None:
    assert _run_reference_decide(notes, order) == expected, label


def test_paid_invariant_without_a_product_list_and_with_the_all_paid_row_missing() -> None:
    """Unknown product list: the noted products are the known ones. No all-paid outcome in the IR: never invented."""
    s = _verified(notes=[NoteRecord("p1", "paid")], situations={"flat_refusal"})
    assert compute_outcome(IR_AB, s).as_tuple() == ("paid", None)
    no_all_paid = IR_AB.model_copy(update={
        "multi_product": IR_AB.multi_product.model_copy(update={"all_paid_outcome": None})})
    full = _verified(notes=[NoteRecord("p1", "paid"), NoteRecord("p2", "paid")], product_order=["p1", "p2"],
                     situations={"flat_refusal"})
    assert compute_outcome(no_all_paid, full).as_tuple() == ("no_promise", "refused")


def test_brief_b_promise_wins_over_notes() -> None:
    s = _verified(promises=[P1], notes=[NoteRecord("p2", "dispute"), NoteRecord("p3", "refused")],
                  situations={"dispute"})
    assert compute_outcome(IR_AB, s).as_tuple() == ("promise", None)


def test_create_promise_failing_twice_with_nothing_recorded() -> None:
    assert compute_outcome(IR_A, _verified(service_error=True)).as_tuple() == ("no_promise", "service_error")
    noted = _verified(service_error=True, notes=[NoteRecord("p1", "service_error")])
    assert compute_outcome(IR_AB, noted).as_tuple() == ("no_promise", "service_error")


def test_brief_b_failed_product_does_not_cancel_recorded_promises() -> None:
    s = _verified(promises=[P1], notes=[NoteRecord("p2", "service_error")], service_error=True)
    assert compute_outcome(IR_AB, s).as_tuple() == ("promise", None)


def test_hardship_paths() -> None:
    assert compute_outcome(IR_A, _verified(situations={"hardship"})).as_tuple() == ("no_promise", "hardship")
    accepted = _verified(situations={"hardship"}, transfer_queue="support")
    assert compute_outcome(IR_A, accepted).as_tuple() == ("transferred", "support")
    promised = _verified(situations={"hardship"}, promises=[P1])  # names a date anyway (rule 32)
    assert compute_outcome(IR_A, promised).as_tuple() == ("promise", None)


def test_refusal_to_talk_depends_on_verification() -> None:
    after = _verified(situations={"refuse_to_talk"})
    assert compute_outcome(IR_A, after).as_tuple() == ("no_promise", "refused")
    before = CallSituation(party="confirmed", verified=False, situations={"refuse_to_talk"})
    assert compute_outcome(IR_A, before).as_tuple() == ("not_verified", "declined")


def test_human_request_before_and_after_verification() -> None:
    for verified in (False, True):
        s = CallSituation(party="confirmed", verified=verified, transfer_queue="general",
                          situations={"human_request"})
        assert compute_outcome(IR_A, s).as_tuple() == ("transferred", "human")


@pytest.mark.parametrize(("facts", "expected"), [
    ({"party": "third_party", "callback_booked": True}, ("not_reached", "callback")),
    ({"party": "third_party"}, ("not_reached", "no_callback")),
    ({"party": "third_party", "callback_failed": True}, ("not_reached", "no_callback")),
    ({"party": "wrong_number"}, ("not_reached", "wrong_number")),
    ({"party": "deceased"}, ("not_reached", "deceased")),
    ({"party": "unclear"}, ("not_reached", "unclear")),
    ({"party": "confirmed", "identity_failed": True}, ("not_verified", "failed")),
    ({"party": "confirmed", "identity_declined": True}, ("not_verified", "declined")),
])
def test_pre_verification_outcomes(facts: dict[str, Any], expected: tuple[str, str]) -> None:
    for ir in (IR_A, IR_AB):
        assert compute_outcome(ir, CallSituation(**facts)).as_tuple() == expected


def test_situations_that_need_verification_are_ignored_before_it() -> None:
    s = CallSituation(party="confirmed", verified=False, identity_failed=True, situations={"already_paid"})
    assert compute_outcome(IR_A, s).as_tuple() == ("not_verified", "failed")


def test_brief_a_ignores_notes() -> None:
    s = _verified(notes=[NoteRecord("p1", "paid")], situations={"flat_refusal"})
    assert Situation.PRODUCT_NOTES not in active_situations(IR_A, s)
    assert compute_outcome(IR_A, s).as_tuple() == ("no_promise", "refused")


def test_fallbacks_when_no_situation_applies() -> None:
    decision = decide_outcome(IR_A, _verified())
    assert decision.outcome.as_tuple() == ("no_promise", "refused") and decision.source == "fallback"
    on_line = decide_outcome(IR_A, CallSituation(party="confirmed", verified=False))
    assert on_line.outcome.as_tuple() == ("not_verified", "declined")


# --------------------------------------------------------------------------------------------------------------------
# Intent rules agree with the outcome tables
# --------------------------------------------------------------------------------------------------------------------


def _intent_cases(ir: PolicyIR) -> list[tuple[str, CallSituation, OutcomeRef]]:
    cases: list[tuple[str, CallSituation, OutcomeRef]] = []
    for intent in ir.intents:
        verified = intent.scope != "before_verification"
        notes = ([NoteRecord("p1", intent.note_reason)]
                 if ir.multi_product.enabled and intent.note_reason else [])
        if intent.outcome is not None:
            queue = intent.transfer_queue()
            s = CallSituation(party="confirmed", verified=verified, situations={intent.key}, transfer_queue=queue,
                              notes=[] if queue else list(notes), product_order=["p1"])
            cases.append((f"{intent.key}/{intent.scope}/accepted", s, intent.outcome))
        if intent.declined_outcome is not None:
            s = CallSituation(party="confirmed", verified=verified, situations={intent.key}, notes=list(notes),
                              product_order=["p1"])
            cases.append((f"{intent.key}/{intent.scope}/declined", s, intent.declined_outcome))
    return cases


@pytest.mark.parametrize("ir_name", ["A", "AB"])
def test_intent_outcomes_match_the_outcome_table(ir_name: str) -> None:
    ir = IR_A if ir_name == "A" else IR_AB
    cases = _intent_cases(ir)
    assert len(cases) >= 8
    for label, situation, expected in cases:
        assert compute_outcome(ir, situation) == expected, label


# --------------------------------------------------------------------------------------------------------------------
# Generic mechanics on modified IRs
# --------------------------------------------------------------------------------------------------------------------


def _with_closing(ir: PolicyIR, **changes: Any) -> PolicyIR:
    return ir.model_copy(update={"closing": ir.closing.model_copy(update=changes)})


def test_promise_wins_false_lets_precedence_decide() -> None:
    order = ["dispute", *[k for k in IR_A.closing.precedence if k != "dispute"]]
    ir = _with_closing(IR_A, promise_wins=False, precedence=order)
    s = _verified(promises=[P1], situations={"dispute"})
    assert compute_outcome(ir, s).as_tuple() == ("no_promise", "dispute")
    assert compute_outcome(IR_A, s).as_tuple() == ("promise", None)


def test_without_transfer_after_promise_rule_promise_wins() -> None:
    ir = _with_closing(IR_A, transfer_after_promise_outcome=None)
    assert compute_outcome(ir, _verified(promises=[P1], transfer_queue="support")).as_tuple() == ("promise", None)
    neither = _with_closing(IR_A, transfer_after_promise_outcome=None, promise_wins=False)
    s = _verified(promises=[P1], transfer_queue="support")
    assert compute_outcome(neither, s).as_tuple() == ("transferred", "support"), "precedence ranks transfers first"


def test_transfer_after_promise_with_fixed_reason() -> None:
    ir = _with_closing(IR_A, transfer_after_promise_outcome=OutcomeRef(outcome="transferred", reason="custom"))
    assert compute_outcome(ir, _verified(promises=[P1], transfer_queue="general")).as_tuple() == ("transferred",
                                                                                                   "custom")


def test_rows_keyed_by_the_transfer_intent() -> None:
    rows = [r.model_copy(update={"situation": "human_request"}) if r.situation == "transfer:general" else r
            for r in IR_A.closing.rows]
    precedence = ["human_request" if k == "transfer:general" else k for k in IR_A.closing.precedence]
    ir = _with_closing(IR_A, rows=rows, precedence=precedence)
    s = _verified(transfer_queue="general", transfer_reason="human_request")
    assert compute_outcome(ir, s).as_tuple() == ("transferred", "human")
    after_promise = _verified(promises=[P1], transfer_queue="general", transfer_reason="human_request")
    assert compute_outcome(ir, after_promise).as_tuple() == ("transferred", "human")


def test_structural_keys_fall_back_to_section_outcomes() -> None:
    structural = {str(k) for k in Situation} - {Situation.PROMISE_RECORDED}
    rows = [r for r in IR_A.closing.rows if r.situation not in structural]
    ir = _with_closing(IR_A, rows=rows, precedence=[r.situation for r in rows])
    expectations = [
        (CallSituation(party="third_party", callback_booked=True), ("not_reached", "callback")),
        (CallSituation(party="third_party"), ("not_reached", "no_callback")),
        (CallSituation(party="wrong_number"), ("not_reached", "wrong_number")),
        (CallSituation(party="deceased"), ("not_reached", "deceased")),
        (CallSituation(party="unclear"), ("not_reached", "unclear")),
        (CallSituation(identity_failed=True), ("not_verified", "failed")),
        (CallSituation(identity_declined=True), ("not_verified", "declined")),
        (_verified(service_error=True), ("no_promise", "service_error")),
    ]
    for situation, expected in expectations:
        decision = decide_outcome(ir, situation)
        assert decision.outcome.as_tuple() == expected
        assert decision.source == "section"


def test_notes_rule_variants() -> None:
    notes = [NoteRecord("p1", "refused")]
    by_row = IR_AB.model_copy(update={"multi_product": IR_AB.multi_product.model_copy(update={"notes_outcome": None})})
    assert compute_outcome(by_row, _verified(notes=notes)).as_tuple() == ("no_promise", "refused"), "row lookup"
    fixed_mp = IR_AB.multi_product.model_copy(update={"closing_reason_rule": "fixed"})
    fixed = IR_AB.model_copy(update={"multi_product": fixed_mp})
    fixed = _with_closing(fixed, rows=[*fixed.closing.rows, OutcomeRow(outcome="no_promise", reason="partial",
                                                                     situation="product_notes")])
    assert compute_outcome(fixed, _verified(notes=notes)).as_tuple() == ("no_promise", "partial")
    no_all_paid_mp = IR_AB.multi_product.model_copy(update={"all_paid_outcome": None})
    no_all_paid = IR_AB.model_copy(update={"multi_product": no_all_paid_mp})
    s = _verified(notes=[NoteRecord("p1", "paid")], situations={"already_paid"})
    assert compute_outcome(no_all_paid, s).as_tuple() == ("paid", None), "falls through to the situation row"


def test_requires_verified_rows() -> None:
    row_after = OutcomeRow(outcome="end", reason="after", situation="x", requires_verified=True)
    row_before = OutcomeRow(outcome="end", reason="before", situation="x", requires_verified=False)
    closing = Closing(rows=[row_after, row_before], precedence=["x"])
    assert closing.row_for("x", verified=True) == row_after
    assert closing.row_for("x", verified=False) == row_before
    assert closing.row_for("y", verified=True) is None
    assert closing.resolved_precedence() == ["x"]


def test_example_minimal_ir() -> None:
    ir = PolicyIR.example_minimal()
    assert compute_outcome(ir, _verified(promises=[P1])).as_tuple() == ("agreed", None)
    assert compute_outcome(ir, _verified(promises=[P1], transfer_queue="people")).as_tuple() == ("handed_over",
                                                                                                 "person_requested")
    assert compute_outcome(ir, _verified()).as_tuple() == ("not_agreed", "declined_offer")
    assert compute_outcome(ir, CallSituation(party="confirmed")).as_tuple() == ("not_identified", "would_not_answer")
    assert compute_outcome(ir, CallSituation(party="third_party")).as_tuple() == ("not_contacted", "callback_missing")


# --------------------------------------------------------------------------------------------------------------------
# CallSituation helpers and explanations
# --------------------------------------------------------------------------------------------------------------------


def test_from_records() -> None:
    s = CallSituation.from_records(
        promises=[{"product_id": "p1", "amount": 510.0, "date": "2026-10-15"}],
        notes=[{"product_id": "p2", "reason": "refused"}],
        transfers=[{"queue": "general"}, {"queue": "support"}],
        callbacks=[{"date": "2026-10-15", "time": "11:00"}],
        party="confirmed",
        verified=True,
        situations={"flat_refusal"},
    )
    assert s.promises == [PromiseRecord("p1", 510.0, "2026-10-15")]
    assert s.notes == [NoteRecord("p2", "refused")]
    assert s.transfer_queue == "support", "the last transfer counts"
    assert s.callback_booked and s.verified and s.situations == {"flat_refusal"}
    empty = CallSituation.from_records()
    assert empty.transfer_queue is None and not empty.callback_booked and empty.party == "confirmed"


def test_active_situations() -> None:
    s = _verified(promises=[P1, P2], transfer_queue="support", transfer_reason="hardship",
                  situations={"hardship", "robot_question"}, notes=[NoteRecord("p3", "refused")])
    assert active_situations(IR_AB, s) == ["promise_recorded", "product_notes", transfer_situation("support"),
                                           "hardship", "robot_question"]
    third = CallSituation(party="third_party", callback_booked=True)
    assert active_situations(IR_A, third) == ["callback_booked"]


def test_decision_explains_itself() -> None:
    decision = decide_outcome(IR_A, _verified(situations={"already_paid"}))
    assert decision.outcome.as_tuple() == ("paid", None)
    assert decision.situation == "already_paid" and decision.source == "precedence"
    assert "A-OUTCOMES" in decision.rule_ids and decision.active == ("already_paid",)
    promise = decide_outcome(IR_A, _verified(promises=[P1]))
    assert promise.source == "promise_wins" and promise.situation == "promise_recorded"
    assert isinstance(promise.situation, str) and type(promise.situation) is str
