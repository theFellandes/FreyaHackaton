"""Parallel tool calls, acknowledgements and the notes guard, run through the compiled hooks in the engine's order.

Verified in the call records (PUB-11/12/13): when the model sends several calls of one tool in one message, the engine
runs EVERY pre-hook before ANY post-hook, on one shared ``vars``; a post-hook does not run on an HTTP 5xx; a request the
pre-hook blocked (empty URL) sends nothing and runs no post-hook; ``<tool>_success_count`` counts any HTTP 2xx, an
``ok: false`` refusal included.

* AUD-06: the acknowledgement staging of ``create_promise`` / ``add_note`` keeps every acknowledged call of a parallel
  batch (``promised_products``, ``notes_log``), commits nothing on a guess, and recovers after the model retries a failed
  member.
* AUD-26: a second note for a product, a note for a promised product and (lean, no notes on a transfer) a note after a
  transfer are never sent.
* AUD-07: ``promise_acks`` counts acknowledged promises only.
"""

from __future__ import annotations

import itertools
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
API = load_tool_api(REPO / "tools" / "openapi.json")
OK = {"status": "success", "data": {"ok": "true"}}  # booleans arrive as lowercase strings
REFUSED = {"status": "success", "data": {"ok": "false", "error": "not carried out"}}
FIVE_XX = None  # HTTP 500: no post-hook, the failure counter goes up
PRODUCTS = [
    {"product_id": f"p{i}", "type": "personal_loan", "past_due": 100 * i, "days_late": 5, "instalment_amount": 50}
    for i in (1, 2, 3)
]
URL = "https://bank.example/tool"


def _tools(ir: PolicyIR, profile: str) -> dict[str, dict[str, Any]]:
    workflow = compile_workflow(ir, API, LEDGER, profile=profile).workflow
    return {t["function"]["name"]: t for t in workflow["nodes"]["negotiate"]["tools"]}


@pytest.fixture(scope="module", params=["staged", "lean"])
def tools(request: pytest.FixtureRequest) -> dict[str, dict[str, Any]]:
    return _tools(IR, request.param)


@pytest.fixture(scope="module")
def facts() -> dict[str, Any]:
    state = run_snippet(codegen.setup_snippet(IR), {"now": "2026-10-14T11:00"})
    return run_snippet(codegen.account_facts_snippet(IR), {**state, "products": [dict(p) for p in PRODUCTS]})


def batch(tool: dict[str, Any], state: dict[str, Any], calls: list[tuple[dict[str, Any], dict[str, Any] | None]],
          *, counters: bool = True) -> tuple[dict[str, Any], list[bool]]:
    """One model message with ``calls`` of ``tool``: every pre-hook, then the post-hooks of the 2xx answers in order.

    ``counters`` = False hides the engine counters from the hooks (their visibility in hook ``vars`` is not verified).
    """
    name = tool["function"]["name"]
    state = dict(state)
    sent: list[bool] = []
    for body, _response in calls:
        pre = run_hook(tool["pre_process_code"], vars=state, config={"url": URL, "body": dict(body)})
        state = pre["vars"]
        sent.append(pre["config"]["url"] == URL)
    for (_body, response), went in zip(calls, sent, strict=True):
        if went and response is not None:
            state = run_hook(tool["post_process_code"], vars=state, result=json.loads(json.dumps(response)))["vars"]
    if counters:
        for (_body, response), went in zip(calls, sent, strict=True):
            key = f"{name}_success_count" if went and response is not None else f"{name}_failure_count"
            state[key] = int(state.get(key) or 0) + 1
    return state, sent


def note(pid: str, reason: str) -> dict[str, Any]:
    return {"customer_id": "c", "product_id": pid, "reason": reason}


def promise(pid: str, amount: float = 100, date: str = "2026-10-16") -> dict[str, Any]:
    return {"customer_id": "c", "product_id": pid, "amount": amount, "date": date}


def decide(state: dict[str, Any]) -> tuple[str, str | None]:
    out = run_snippet(codegen.decide_outcome_snippet(IR), {**state, "identity_verified": "yes",
                                                          "chosen_end": "flat_refusal"})
    return out["final_outcome"], out.get("final_reason")


# ---- AUD-06: notes ------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("counters", [True, False])
def test_parallel_notes_are_all_logged_and_decide_the_first_reason(
    tools: dict[str, Any], facts: dict[str, Any], counters: bool,
) -> None:
    state, sent = batch(tools["add_note"], facts, [(note("p1", "refused"), OK), (note("p2", "dispute"), OK)],
                        counters=counters)
    assert sent == [True, True]
    assert state["notes_log"] == "p1:refused;p2:dispute;"
    assert decide(state) == ("no_promise", "refused")


def test_parallel_paid_notes_on_every_product_close_as_paid(tools: dict[str, Any], facts: dict[str, Any]) -> None:
    state, _ = batch(tools["add_note"], facts, [(note(p, "paid"), OK) for p in ("p1", "p2", "p3")])
    assert decide(state) == ("paid", None)


@pytest.mark.parametrize("size", [2, 3])
def test_every_reason_mix_closes_the_same_in_parallel_and_in_sequence(
    tools: dict[str, Any], facts: dict[str, Any], size: int,
) -> None:
    pids = [f"p{i}" for i in range(1, size + 1)]
    for reasons in itertools.product(("refused", "hardship", "dispute", "paid"), repeat=size):
        calls = [(note(pid, reason), OK) for pid, reason in zip(pids, reasons, strict=True)]
        parallel, _ = batch(tools["add_note"], facts, calls)
        sequential = dict(facts)
        for call in calls:
            sequential, _ = batch(tools["add_note"], sequential, [call])
        assert parallel["notes_log"] == sequential["notes_log"], reasons
        assert decide(parallel) == decide(sequential), reasons


def test_a_refusal_inside_a_note_batch_commits_nothing_until_the_retry(
    tools: dict[str, Any], facts: dict[str, Any],
) -> None:
    state, _ = batch(tools["add_note"], facts, [(note("p1", "hardship"), REFUSED), (note("p2", "refused"), OK)])
    assert not state.get("notes_log")  # which one was refused is unknown: nothing is committed on a guess
    state, sent = batch(tools["add_note"], state, [(note("p1", "hardship"), OK)])  # the model retries the refused one
    assert sent == [True]
    assert state["notes_log"] == "p1:hardship;p2:refused;"
    assert decide(state) == ("no_promise", "hardship")


@pytest.mark.parametrize("counters", [True, False])
def test_a_5xx_inside_a_note_batch_recovers_on_the_retry(
    tools: dict[str, Any], facts: dict[str, Any], counters: bool,
) -> None:
    state, _ = batch(tools["add_note"], facts, [(note("p1", "dispute"), FIVE_XX), (note("p2", "refused"), OK)],
                     counters=counters)
    assert not state.get("notes_log")
    state, _ = batch(tools["add_note"], state, [(note("p1", "dispute"), OK)], counters=counters)
    assert state["notes_log"] == "p1:dispute;p2:refused;"


def test_a_note_after_a_batch_that_failed_entirely_is_still_logged(tools: dict[str, Any], facts: dict[str, Any]) -> None:
    state, _ = batch(tools["add_note"], facts, [(note("p1", "dispute"), FIVE_XX), (note("p2", "dispute"), FIVE_XX)])
    state, _ = batch(tools["add_note"], state, [(note("p3", "refused"), OK)])
    assert state["notes_log"] == "p3:refused;"
    state, _ = batch(tools["add_note"], state, [(note("p1", "dispute"), OK), (note("p2", "dispute"), OK)])
    assert state["notes_log"] == "p3:refused;p1:dispute;p2:dispute;"


def test_a_retry_after_a_single_5xx_is_logged_even_without_visible_counters(
    tools: dict[str, Any], facts: dict[str, Any],
) -> None:
    state, _ = batch(tools["add_note"], facts, [(note("p2", "refused"), FIVE_XX)], counters=False)
    state, sent = batch(tools["add_note"], state, [(note("p2", "refused"), OK)], counters=False)
    assert sent == [True] and state["notes_log"] == "p2:refused;"


# ---- AUD-06 / AUD-07: promises ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("counters", [True, False])
def test_parallel_promises_are_all_acknowledged_and_each_repeat_is_blocked(
    tools: dict[str, Any], facts: dict[str, Any], counters: bool,
) -> None:
    calls = [(promise(p), OK) for p in ("p1", "p2", "p3")]
    state, sent = batch(tools["create_promise"], facts, calls, counters=counters)
    assert sent == [True, True, True]
    assert state["promised_products"].split(",") == ["p1", "p2", "p3"]
    assert state["promise_acks"] == "3"
    for pid in ("p1", "p2", "p3"):
        _, again = batch(tools["create_promise"], state, [(promise(pid, date="2026-10-19"), OK)])
        assert again == [False], pid


def test_a_5xx_inside_a_promise_batch_never_marks_the_failed_product(
    tools: dict[str, Any], facts: dict[str, Any],
) -> None:
    state, _ = batch(tools["create_promise"], facts, [(promise("p1"), FIVE_XX), (promise("p2"), OK),
                                                      (promise("p3"), OK)])
    assert "p1" not in str(state.get("promised_products") or "").split(",")
    assert state["promise_acks"] == "2"
    state, sent = batch(tools["create_promise"], state, [(promise("p1"), OK)])  # the supported retry is sent
    assert sent == [True]
    assert sorted(state["promised_products"].split(",")) == ["p1", "p2", "p3"]


def test_a_refused_promise_is_not_acknowledged(tools: dict[str, Any], facts: dict[str, Any]) -> None:
    state, sent = batch(tools["create_promise"], facts, [(promise("p1"), REFUSED)])
    assert sent == [True]
    assert state["create_promise_success_count"] == 1  # the engine counts the HTTP 200 ...
    assert state.get("promise_acks", "0") == "0" and not state.get("promised_products")  # ... the bank did not record
    state, sent = batch(tools["create_promise"], state, [(promise("p1"), OK)])
    assert sent == [True] and state["promise_acks"] == "1" and state["promised_products"] == "p1"


# ---- AUD-26: the notes guard --------------------------------------------------------------------------------------------


def test_a_second_note_for_a_product_is_never_sent(tools: dict[str, Any], facts: dict[str, Any]) -> None:
    state, _ = batch(tools["add_note"], facts, [(note("p2", "dispute"), OK)])
    for reason in ("dispute", "refused"):
        after, sent = batch(tools["add_note"], state, [(note("P2", reason), OK)])
        assert sent == [False], reason
        assert after["note_guard"].startswith("blocked:") and after["notes_log"] == "p2:dispute;"
    other, sent = batch(tools["add_note"], state, [(note("p3", "refused"), OK)])
    assert sent == [True] and other["notes_log"] == "p2:dispute;p3:refused;"


def test_a_failed_note_can_still_be_retried(tools: dict[str, Any], facts: dict[str, Any]) -> None:
    for failure in (REFUSED, FIVE_XX):
        state, _ = batch(tools["add_note"], facts, [(note("p2", "dispute"), failure)])
        state, sent = batch(tools["add_note"], state, [(note("p2", "dispute"), OK)])
        assert sent == [True] and state["notes_log"] == "p2:dispute;", failure


def test_a_promised_product_gets_no_note(tools: dict[str, Any], facts: dict[str, Any]) -> None:
    state, _ = batch(tools["create_promise"], facts, [(promise("p1"), OK)])
    _, sent = batch(tools["add_note"], state, [(note("p1", "refused"), OK)])
    assert sent == [False]
    after, sent = batch(tools["add_note"], state, [(note("p2", "refused"), OK)])
    assert sent == [True] and after["notes_log"] == "p2:refused;"


def test_no_note_is_sent_after_a_transfer_in_the_lean_profile(facts: dict[str, Any]) -> None:
    lean = _tools(IR, "lean")
    moved = {**facts, "transfer_to_agent_success_count": 1}
    _, sent = batch(lean["add_note"], moved, [(note("p1", "refused"), OK)])
    assert sent == [False]
    allowed = IR.model_copy(deep=True)
    allowed.multi_product.notes_on_transfer = True
    _, sent = batch(_tools(allowed, "lean")["add_note"], moved, [(note("p1", "refused"), OK)])
    assert sent == [True]
