"""A handoff offer has separate accept / decline / clarification branches (audit F18).

The generated bullet used to say "If they decline (or answer anything other than yes)", so "What would support help with?"
or "give me a moment" ended the call as a refusal. Now a question, a request for time, small talk or cut-off speech keeps the
offer pending, is answered, and is neither a refusal nor a second offer; only a clear refusal (or an answer that is still
not a yes after one question) takes the decline branch.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from builder.compiler import context
from builder.compiler.compile import compile_workflow
from builder.compiler.context import LeanExits, StagedExits, exit_bullet
from builder.ingest.tool_api import load_tool_api
from builder.policy.ir import PolicyIR, RuleLedger

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "fixtures" / "policy"
API = load_tool_api(REPO / "tools" / "openapi.json")


def _load(name: str) -> tuple[PolicyIR, RuleLedger]:
    ir = PolicyIR.model_validate(json.loads((FIXTURES / f"{name}_ir.json").read_text(encoding="utf-8")))
    ledger = RuleLedger.model_validate(json.loads((FIXTURES / f"{name}_ledger.json").read_text(encoding="utf-8")))
    return ir, ledger


IR_A, LEDGER_A = _load("brief_a")
IR_AB, LEDGER_AB = _load("brief_ab")


def _support_bullet(name: str, profile: str) -> str:
    ir, ledger = (IR_A, LEDGER_A) if name == "A" else (IR_AB, LEDGER_AB)
    text = compile_workflow(ir, API, ledger, profile=profile).workflow["nodes"]["negotiate"]["task_message"]
    (bullet,) = [line for line in text.splitlines() if "customer support team" in line and line.startswith("- ")]
    return bullet


@pytest.mark.parametrize("profile", ["staged", "lean"])
@pytest.mark.parametrize("brief", ["A", "AB"])
def test_anything_other_than_yes_is_no_longer_a_decline(brief: str, profile: str) -> None:
    bullet = _support_bullet(brief, profile)
    assert "other than yes" not in bullet and "anything other" not in bullet


@pytest.mark.parametrize("profile", ["staged", "lean"])
@pytest.mark.parametrize("brief", ["A", "AB"])
def test_the_offer_has_separate_accept_decline_and_clarification_branches(brief: str, profile: str) -> None:
    bullet = _support_bullet(brief, profile)
    accept = bullet.index("If they accept")
    decline = bullet.index("If they clearly decline")
    clarify = bullet.index("If they ask a question about the offer")
    assert len({accept, decline, clarify}) == 3
    clarification = bullet[clarify:]
    for needle in ("ask for time", "small talk", "cut off", "keep the offer open", "answer",
                   "not a refusal", "not a second offer"):
        assert needle in clarification, needle
    # a clarification neither declines, nor writes a decline note, nor says goodbye
    assert "do not transfer, add a note or end the call because of it" in clarification
    # "alternative affirmative wording" is an acceptance
    assert "any clear agreement" in bullet[accept:decline]


@pytest.mark.parametrize("profile", ["staged", "lean"])
def test_the_decline_branch_keeps_the_prescribed_exit_for_a_single_product(profile: str) -> None:
    bullet = _support_bullet("A", profile)
    decline = bullet[bullet.index("If they clearly decline"):bullet.index("If they ask a question about the offer")]
    expected = ("call transition_to_end_hardship" if profile == "staged"
                else 'call close_call with outcome "no_promise" and reason "hardship"')
    assert "thank them, say goodbye and " + expected in decline
    assert "do not ask again" in decline


@pytest.mark.parametrize("profile", ["staged", "lean"])
def test_the_decline_branch_keeps_the_deferred_note_for_several_products(profile: str) -> None:
    bullet = _support_bullet("AB", profile)
    decline = bullet[bullet.index("If they clearly decline"):bullet.index("If they ask a question about the offer")]
    assert 'add_note reason "hardship" for the product (do not call add_note yet - write it only at the very end)' in decline


def test_a_question_is_answered_and_the_decision_is_asked_for_once() -> None:
    bullet = _support_bullet("A", "staged")
    clarify = bullet[bullet.index("If they ask a question about the offer"):]
    # AUD-37: the yes / no is not asked again (a judge counts that as a second offer); the agent waits for the answer
    assert "ask once more" not in clarify
    assert "keep the offer open and wait; do not ask it again" in clarify
    assert "still neither a clear yes nor a clear no" in clarify and "take it as a decline" in clarify
    # the offer itself is made once and its wording appears once
    assert bullet.count("Make this offer only once.") == 1
    assert bullet.count("Offer to connect them with the customer support team.") == 1


def test_an_offer_without_a_decline_outcome_keeps_its_plain_wording() -> None:
    human = next(i for i in IR_A.intents if i.key == "human_request")
    bullet = exit_bullet(IR_A, human, StagedExits(), note_tool="add_note") or ""
    assert "If they clearly decline" not in bullet and "If they ask a question about the offer" not in bullet
    # AUD-37: accepting "may I help first?" is not a reason to transfer; only still wanting the transfer is
    assert "accept or insist" not in bullet
    assert "If they insist: say you will transfer them" in bullet
    assert "If they let you help, go on; do not offer it again." in bullet


def test_an_offer_equal_to_the_fixed_response_is_said_once() -> None:
    # live Brief A IR (11 Oct): response and offer were both "Ask once whether you can help first.", and the bullet
    # said it twice
    human = next(i for i in IR_A.intents if i.key == "human_request").model_copy(deep=True)
    human.response = human.offer = "Ask once whether you can help first."
    bullet = exit_bullet(IR_A, human, StagedExits(), note_tool="add_note") or ""
    assert bullet.count("Ask once whether you can help first.") == 1
    assert "If they insist: say you will transfer them" in bullet


def test_the_declined_sentence_helper_describes_only_the_decline_action() -> None:
    support = next(i for i in IR_A.intents if i.key == "hardship")
    from builder.compiler import naming

    declined = naming.intent_exit(IR_A, support).declined
    assert declined is not None
    text = context._declined_sentence(LeanExits(), declined, "add_note")
    assert text.startswith("If they clearly decline") and "other than yes" not in text


def test_the_reference_agent_has_the_same_branches() -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location("brief_a_ref_offer", REPO / "evals" / "reference" / "brief_a_ref.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    task = module.NEGOTIATE_TASK
    assert "other than yes" not in task
    assert task.index("If they accept (yes, or any clear agreement)") < task.index("If they clearly decline") < task.index(
        "If they ask a question about the offer")
    assert "do not transfer or end the call because of it" in task
    assert "still neither a clear yes nor a clear no, take it as a decline" in task
    # C-14: its identity step checks a complete answer at once, too
    identity = module.IDENTITY_TASK
    assert "Only a COMPLETE answer can be checked" not in identity
    assert "A COMPLETE answer is checked at once" in identity and "never hold it back" in identity
    assert "is checked FIRST" in identity and "only then answer the complaint or the question" in identity
