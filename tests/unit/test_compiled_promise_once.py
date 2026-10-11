"""One immutable acknowledged promise per product (audit C-17 / F17), exercised through the compiled ``create_promise`` hooks.

The pre-hook stages the product of a request that passed the guard; the post-hook commits it to ``promised_products`` only
when the bank acknowledged THIS request. A later request for an acknowledged product - identical, with other terms, with
another spelling of the id, or after node re-entry - is blocked before it reaches the bank and the engine speaks the
explanation (the same ``promise_fix`` route as every other block). Another product stays eligible, and failed or unknown
outcomes never count as acknowledged, so the supported retry stays possible.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

from builder.checks.code_sandbox import run_hook, run_snippet
from builder.compiler import codegen, hooks
from builder.compiler.compile import compile_workflow
from builder.ingest.tool_api import load_tool_api
from builder.policy.ir import PolicyIR, RuleLedger

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "fixtures" / "policy"
IR = PolicyIR.model_validate(json.loads((FIXTURES / "brief_ab_ir.json").read_text(encoding="utf-8")))
LEDGER = RuleLedger.model_validate(json.loads((FIXTURES / "brief_ab_ledger.json").read_text(encoding="utf-8")))
API = load_tool_api(REPO / "tools" / "openapi.json")
PRODUCTS = [
    {"product_id": "p1", "type": "credit_card", "card_last4": "1234", "past_due": 100, "days_late": 5,
     "minimum_payment": 30},
    {"product_id": "p2", "type": "personal_loan", "past_due": 200, "days_late": 5, "instalment_amount": 50},
]
OK = {"status": "success", "data": {"ok": True}}
URL = "https://bank.example/create_promise"


def _facts(ir: PolicyIR = IR) -> dict[str, Any]:
    state = run_snippet(codegen.setup_snippet(ir), {"now": "2026-10-14T11:00"})
    return run_snippet(codegen.account_facts_snippet(ir), {**state, "products": [dict(p) for p in PRODUCTS]})


def _tool(ir: PolicyIR, profile: str) -> dict[str, Any]:
    workflow = compile_workflow(ir, API, LEDGER, profile=profile).workflow
    return next(t for t in workflow["nodes"]["negotiate"]["tools"] if t["function"]["name"] == "create_promise")


def _call(tool: dict[str, Any], state: dict[str, Any], product: str, response: dict[str, Any] | None = OK,
          *, amount: Any = 100, date: str = "2026-10-16") -> dict[str, Any]:
    """One create_promise call: pre-hook, then (only when the request was sent) the post-hook with ``response``."""
    pre = run_hook(tool["pre_process_code"], vars=dict(state), config={
        "url": URL, "body": {"customer_id": "c", "product_id": product, "amount": amount, "date": date}})
    sent = pre["config"]["url"] == URL
    out: dict[str, Any] = {"sent": sent, "vars": pre["vars"], "result": None}
    if sent and response is not None:
        post = run_hook(tool["post_process_code"], vars=pre["vars"], result=json.loads(json.dumps(response)))
        out.update(vars=post["vars"], result=post["result"])
    return out


@pytest.fixture(scope="module", params=["staged", "lean"])
def tool(request: pytest.FixtureRequest) -> dict[str, Any]:
    return _tool(IR, request.param)


@pytest.fixture(scope="module")
def facts() -> dict[str, Any]:
    return _facts()


def test_an_acknowledged_promise_blocks_every_later_request_for_the_same_product(
    tool: dict[str, Any], facts: dict[str, Any],
) -> None:
    first = _call(tool, facts, "p1")
    assert first["sent"] and first["vars"]["promised_products"] == "p1"
    for amount, date in ((100, "2026-10-16"), (100, "2026-10-19"), (60, "2026-10-16")):  # same terms and changed terms
        again = _call(tool, first["vars"], "p1", amount=amount, date=date)
        assert not again["sent"], (amount, date)
        assert again["vars"]["promise_guard"].startswith("blocked:")
        assert "already recorded" in again["vars"]["promise_guard"]
        assert again["vars"]["promise_guard_blocks"] == "1"
        say = again["vars"]["promise_guard_say"]
        assert "credit card ending 1234" in say and "already recorded" in say
        assert "cannot be changed" in say
        assert IR.recording.immutable_sentence in say  # the brief's own sentence is part of the explanation
        assert again["vars"]["promised_products"] == "p1"


def test_another_product_stays_independently_eligible(tool: dict[str, Any], facts: dict[str, Any]) -> None:
    first = _call(tool, facts, "p1")
    second = _call(tool, first["vars"], "p2", amount=200)
    assert second["sent"] and second["vars"]["promise_guard"] == "ok"
    assert second["vars"]["promised_products"] == "p1,p2"
    assert not _call(tool, second["vars"], "p2", amount=200)["sent"]


@pytest.mark.parametrize("response", [
    {"status": "success", "data": {"ok": False}},
    {"status": "error", "data": {"ok": False}},
    {"status": "error", "data": {"ok": True}},
    {"status": "error", "message": "transport timeout"},
    {"status": "success", "data": {}},
    None,  # no response at all: the outcome is unknown
])
def test_failed_or_unknown_outcomes_are_not_acknowledged_and_the_retry_stays_possible(
    tool: dict[str, Any], facts: dict[str, Any], response: dict[str, Any] | None,
) -> None:
    failed = _call(tool, facts, "p1", response)
    assert failed["sent"] and not failed["vars"].get("promised_products")
    retry = _call(tool, failed["vars"], "p1")
    assert retry["sent"] and retry["vars"]["promise_guard"] == "ok"
    assert retry["vars"]["promised_products"] == "p1"


def test_the_spelling_of_the_product_id_does_not_get_around_the_block(tool: dict[str, Any], facts: dict[str, Any]) -> None:
    first = _call(tool, facts, "P1")
    assert first["vars"]["promised_products"] == "p1"
    assert not _call(tool, first["vars"], "P1")["sent"]
    assert not _call(tool, first["vars"], " p1 ")["sent"]
    assert not _call(tool, first["vars"], "1")["sent"]


def test_the_same_acknowledgment_cannot_be_committed_twice_or_for_a_stale_request(
    tool: dict[str, Any], facts: dict[str, Any],
) -> None:
    first = _call(tool, facts, "p1")
    repeated = run_hook(tool["post_process_code"], vars=first["vars"], result=json.loads(json.dumps(OK)))
    assert repeated["vars"]["promised_products"] == "p1"
    # a block leaves nothing staged: a stray success result afterwards must not acknowledge the blocked product
    blocked = _call(tool, {**facts}, "p9", None)
    assert not blocked["sent"]
    stray = run_hook(tool["post_process_code"], vars=blocked["vars"], result=json.loads(json.dumps(OK)))
    assert not stray["vars"].get("promised_products")


def test_a_block_by_another_rule_stages_nothing(tool: dict[str, Any], facts: dict[str, Any]) -> None:
    early = _call(tool, facts, "p1", date="2026-10-03")  # in the past: the date rule blocks it
    assert not early["sent"] and early["vars"]["promise_guard"].startswith("blocked:")
    assert not early["vars"].get("promise_pending_product")
    assert _call(tool, early["vars"], "p1")["sent"]


@pytest.mark.parametrize("profile", ["staged", "lean"])
def test_the_policy_may_allow_a_second_promise(profile: str, facts: dict[str, Any]) -> None:
    ir = IR.model_copy(deep=True)
    ir.recording.one_promise_per_product = False
    open_tool = _tool(ir, profile)
    first = _call(open_tool, facts, "p1")
    assert first["sent"]
    assert _call(open_tool, first["vars"], "p1", amount=60)["sent"]


@pytest.mark.parametrize("profile", ["staged", "lean"])
def test_without_an_immutable_sentence_the_guard_still_explains(profile: str, facts: dict[str, Any]) -> None:
    ir = IR.model_copy(deep=True)
    ir.recording.immutable_sentence = ""
    plain = _tool(ir, profile)
    first = _call(plain, facts, "p1")
    again = _call(plain, first["vars"], "p1")
    assert not again["sent"] and "already recorded" in again["vars"]["promise_guard_say"]


def test_the_hooks_themselves_have_no_double_braces() -> None:
    code = hooks.post_hook_promise_log(hooks.flag_true("ok"))
    assert "{{" not in code and "}}" not in code
    run_hook(code, vars={}, result={})  # never raises on an odd result


# ---- the hand-authored reference agent has the same mechanism (evals/reference/brief_a_ref.py) -------------------------------


@pytest.fixture(scope="module")
def ref() -> Any:
    spec = importlib.util.spec_from_file_location("brief_a_ref_once", REPO / "evals" / "reference" / "brief_a_ref.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _ref_call(ref: Any, state: dict[str, Any], product: str, response: dict[str, Any] | None = OK) -> dict[str, Any]:
    pre = run_hook(ref.PROMISE_PRE, vars=dict(state), config={
        "url": URL, "body": {"customer_id": "c", "product_id": product, "amount": 100, "date": "2026-10-16"}})
    sent = pre["config"]["url"] == URL
    out: dict[str, Any] = {"sent": sent, "vars": pre["vars"]}
    if sent and response is not None:
        out["vars"] = run_hook(ref.PROMISE_POST, vars=pre["vars"], result=json.loads(json.dumps(response)))["vars"]
    return out


def test_the_reference_blocks_a_second_promise_for_an_acknowledged_product(ref: Any, facts: dict[str, Any]) -> None:
    state = {**facts, "product_ids": "p1,p2"}
    first = _ref_call(ref, state, "p1")
    assert first["sent"] and first["vars"]["promised_products"] == "p1"
    again = _ref_call(ref, first["vars"], "p1")
    assert not again["sent"] and "already recorded" in again["vars"]["promise_guard_say"]
    assert _ref_call(ref, first["vars"], "p2")["sent"]


def test_the_reference_does_not_acknowledge_failed_promises(ref: Any, facts: dict[str, Any]) -> None:
    state = {**facts, "product_ids": "p1,p2"}
    for response in ({"status": "success", "data": {"ok": False}}, {"status": "error", "message": "timeout"}, None):
        failed = _ref_call(ref, state, "p1", response)
        assert failed["sent"] and not failed["vars"].get("promised_products")
        assert _ref_call(ref, failed["vars"], "p1")["sent"]
