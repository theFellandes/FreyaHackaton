"""Exercise the actual compiled note hooks through failed writes, retries and closing decisions."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from builder.checks.code_sandbox import run_hook, run_snippet
from builder.compiler import codegen
from builder.compiler.compile import compile_workflow
from builder.ingest.tool_api import load_tool_api
from builder.policy.ir import PolicyIR, RuleLedger

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "fixtures" / "policy"
IR = PolicyIR.model_validate(json.loads((FIXTURES / "brief_ab_ir.json").read_text(encoding="utf-8")))
LEDGER = RuleLedger.model_validate(json.loads((FIXTURES / "brief_ab_ledger.json").read_text(encoding="utf-8")))


@pytest.fixture(scope="module", params=["staged", "lean"])
def note(request: pytest.FixtureRequest) -> dict[str, Any]:
    workflow = compile_workflow(IR, load_tool_api(REPO / "tools" / "openapi.json"), LEDGER,
                                profile=request.param).workflow
    return next(t for t in workflow["nodes"]["negotiate"]["tools"] if t["function"]["name"] == "add_note")


def note_call(note: dict[str, Any], state: dict[str, Any], product: str, reason: str,
              response: dict[str, Any]) -> dict[str, Any]:
    staged = run_hook(note["pre_process_code"], vars=state,
                      config={"url": "https://bank.example/add_note", "body": {
                          "product_id": product, "reason": reason}})
    assert staged["vars"].get("notes_log") == state.get("notes_log")
    return run_hook(note["post_process_code"], vars=staged["vars"], result=response)


@pytest.mark.parametrize("response", [
    {"status": "success", "data": {"ok": False}},
    {"status": "error", "data": {"ok": False}},
    {"status": "error", "data": {"ok": True}},
    {"status": "error", "message": "transport timeout"},
    {"status": "success", "data": {}},
])
def test_failed_notes_do_not_change_saved_facts_or_closing(note: dict[str, Any], response: dict[str, Any]) -> None:
    state = {"product_ids": "p1,p2", "identity_verified": "yes", "chosen_end": "flat_refusal"}
    saved = note_call(note, state, "P1", "Paid", {"status": "success", "data": {"ok": True}})["vars"]
    failed = note_call(note, saved, "P2", "Dispute", response)
    assert failed["vars"]["notes_log"] == "p1:paid;"
    message = failed["result"].get("what_to_do_next") or failed["result"].get("data", {}).get("what_to_do_next")
    assert "NOT saved" in message
    # The platform may count HTTP success even when the bank's acknowledgment is false; the latest args are overwritten.
    failed["vars"].update({"add_note_success_count": 2, "add_note_args.product_id": "p2",
                           "add_note_args.reason": "dispute"})
    decided = run_snippet(codegen.decide_outcome_snippet(IR), failed["vars"])
    assert (decided["final_outcome"], decided.get("final_reason")) == ("no_promise", "refused")


def test_failed_paid_note_cannot_complete_the_all_paid_outcome(note: dict[str, Any]) -> None:
    state = {"product_ids": "p1,p2", "identity_verified": "yes", "chosen_end": "flat_refusal"}
    saved = note_call(note, state, "p1", "paid", {"status": "success", "data": {"ok": True}})["vars"]
    failed = note_call(note, saved, "p2", "paid", {"status": "success", "data": {"ok": False}})["vars"]
    decided = run_snippet(codegen.decide_outcome_snippet(IR), failed)
    assert decided["final_outcome"] != "paid"
    retried = note_call(note, failed, "p2", "paid", {"status": "success", "data": {"ok": True}})["vars"]
    assert retried["notes_log"] == "p1:paid;p2:paid;"
    decided = run_snippet(codegen.decide_outcome_snippet(IR), retried)
    assert (decided["final_outcome"], decided.get("final_reason")) == ("paid", None)


def test_a_failed_correction_does_not_overwrite_an_acknowledged_reason(note: dict[str, Any]) -> None:
    state = {"product_ids": "p1,p2", "identity_verified": "yes", "chosen_end": "flat_refusal"}
    first = note_call(note, state, "p2", "dispute", {"status": "success", "data": {"ok": True}})["vars"]
    failed = note_call(note, first, "p2", "paid", {"status": "success", "data": {"ok": False}})["vars"]
    assert failed["notes_log"] == "p2:dispute;"
    decided = run_snippet(codegen.decide_outcome_snippet(IR), failed)
    assert (decided["final_outcome"], decided.get("final_reason")) == ("no_promise", "dispute")
    promised = run_snippet(codegen.decide_outcome_snippet(IR), {**failed, "create_promise_success_count": 1})
    assert (promised["final_outcome"], promised.get("final_reason")) == ("promise", None)


def test_the_same_note_result_cannot_be_counted_twice(note: dict[str, Any]) -> None:
    response = {"status": "success", "data": {"ok": True}}
    first = note_call(note, {"product_ids": "p1"}, "p1", "paid", response)
    repeated = run_hook(note["post_process_code"], vars=first["vars"], result=response)
    assert repeated["vars"]["notes_log"] == "p1:paid;"


def test_an_incomplete_request_clears_the_pending_note(note: dict[str, Any]) -> None:
    first = run_hook(note["pre_process_code"], vars={"product_ids": "p1"},
                     config={"body": {"product_id": "p1", "reason": "paid"}})
    incomplete = run_hook(note["pre_process_code"], vars=first["vars"], config={"body": {"product_id": "p1"}})
    result = run_hook(note["post_process_code"], vars=incomplete["vars"],
                      result={"status": "success", "data": {"ok": True}})
    assert "notes_log" not in result["vars"]


# ---- the hand-authored reference agent has the same mechanism (evals/reference/brief_a_ref.py) -------------------------------


@pytest.fixture(scope="module")
def ref() -> Any:
    import importlib.util

    spec = importlib.util.spec_from_file_location("brief_a_ref_notes", REPO / "evals" / "reference" / "brief_a_ref.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _ref_note(ref: Any, state: dict[str, Any], product: str, reason: str, response: dict[str, Any]) -> dict[str, Any]:
    staged = run_hook(ref.NOTE_PRE, vars=state, config={"url": "https://bank.example/add_note", "body": {
        "product_id": product, "reason": reason}})
    assert staged["vars"].get("notes_log") == state.get("notes_log")  # the request alone changes nothing
    return run_hook(ref.NOTE_POST, vars=staged["vars"], result=response)["vars"]


def test_the_reference_records_a_note_only_after_the_bank_acknowledged_it(ref: Any) -> None:
    ok = {"status": "success", "data": {"ok": True}}
    saved = _ref_note(ref, {"product_ids": "p1,p2"}, "P1", "Paid", ok)
    assert saved["notes_log"] == "p1:paid;"
    for response in ({"status": "success", "data": {"ok": False}}, {"status": "error", "data": {"ok": True}},
                     {"status": "error", "message": "timeout"}, {"status": "success", "data": {}}):
        failed = _ref_note(ref, saved, "p2", "dispute", response)
        assert failed["notes_log"] == "p1:paid;", response
    retried = _ref_note(ref, saved, "p2", "dispute", ok)
    assert retried["notes_log"] == "p1:paid;p2:dispute;"


def test_the_reference_note_result_cannot_be_counted_twice(ref: Any) -> None:
    ok = {"status": "success", "data": {"ok": True}}
    first = _ref_note(ref, {"product_ids": "p1"}, "p1", "paid", ok)
    again = run_hook(ref.NOTE_POST, vars=first, result=json.loads(json.dumps(ok)))["vars"]
    assert again["notes_log"] == "p1:paid;"
