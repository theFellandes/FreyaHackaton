"""The compiled prompts, hooks and graph must not contradict each other (audit findings C-09, C-11, C-12, C-13, C-15,
C-19, C-22).

* the retry rule of the negotiation prompt and the post-hook of ``create_promise`` come from the policy's
  ``retries_on_failure`` and say the same thing;
* several-product prompts do not ask for a note that the transfer rule forbids, and the failed-promise rule agrees with the
  service-error note rule;
* no node has two transitions to the same target (the engine would merge them);
* a hardship (or any intent with ``overrides``) latches: the overridden exit is guarded in the graph, not only in the prompt;
* a phrase the brief does not prescribe never becomes an empty quote;
* the rule map only names mechanisms that exist.

Expectations are literal values, not comparisons between two generated artefacts.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from builder.checks.code_sandbox import extract_code_snippets, run_hook, run_snippet
from builder.checks.lint import lint_workflow
from builder.compiler.compile import compile_workflow
from builder.compiler.errors import CompileError
from builder.ingest.tool_api import ToolApi, load_tool_api
from builder.policy.ir import PolicyIR, RuleLedger

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "fixtures" / "policy"


def _ir(name: str) -> PolicyIR:
    return PolicyIR.model_validate(json.loads((FIXTURES / f"{name}_ir.json").read_text(encoding="utf-8")))


def _ledger(name: str) -> RuleLedger:
    return RuleLedger.model_validate(json.loads((FIXTURES / f"{name}_ledger.json").read_text(encoding="utf-8")))


IR_A, IR_AB = _ir("brief_a"), _ir("brief_ab")
LEDGER_A, LEDGER_AB = _ledger("brief_a"), _ledger("brief_ab")
BRIEFS = {"A": (IR_A, LEDGER_A), "AB": (IR_AB, LEDGER_AB)}


@pytest.fixture(scope="module")
def api() -> ToolApi:
    return load_tool_api(REPO / "tools" / "openapi.json")


def build(ir: PolicyIR, ledger: RuleLedger, api: ToolApi, profile: str = "staged") -> dict[str, Any]:
    return compile_workflow(ir, api, ledger, None, profile).workflow  # type: ignore[arg-type]


def promise_hook(workflow: dict[str, Any]) -> str:
    tool = next(t for t in workflow["nodes"]["negotiate"]["tools"] if t["function"]["name"] == "create_promise")
    return tool["post_process_code"]


def failure_message(workflow: dict[str, Any]) -> str:
    out = run_hook(promise_hook(workflow), result={"status": "success", "data": {"ok": False}})["result"]["data"]
    return out["what_to_do_next"]


def line_with(text: str, needle: str) -> str:
    return next(line.strip() for line in text.splitlines() if needle in line)


# ---- C-09: the retry rule comes from the policy ---------------------------------------------------------------------------

SENTENCE = "I'm sorry, I could not record your promise."
GIVE_UP = f'say "{SENTENCE}", thank them, say goodbye and call transition_to_end_recording_failed.'


@pytest.mark.parametrize(
    ("retries", "prompt", "hook"),
    [
        (0, f"If it fails, do not try again: {GIVE_UP}",
         f"Do not try again: {GIVE_UP}"),
        (1, f"If it fails, call it once more with the same values. If it fails again, {GIVE_UP}",
         "If this is the first failure for this product, call create_promise once more with exactly the same values "
         f"now. If it already failed once before, {GIVE_UP}"),
        (2, f"If it fails, call it again with the same values (at most twice more). If it fails a third time, {GIVE_UP}",
         "If this is failure number 1 to 2 for this product, call create_promise again with exactly the same values "
         f"now. If it already failed twice before, {GIVE_UP}"),
        (3, "If it fails, call it again with the same values (at most three times more). If it fails a fourth time, "
            f"{GIVE_UP}",
         "If this is failure number 1 to 3 for this product, call create_promise again with exactly the same values "
         f"now. If it already failed three times before, {GIVE_UP}"),
    ],
)
def test_the_retry_rule_follows_retries_on_failure_in_the_prompt_and_in_the_hook(
    retries: int, prompt: str, hook: str, api: ToolApi
) -> None:
    ir = IR_A.model_copy(deep=True)
    ir.recording.retries_on_failure = retries
    workflow = build(ir, LEDGER_A, api)
    rule = line_with(workflow["nodes"]["negotiate"]["task_message"], "If it fails")
    assert rule == prompt
    assert failure_message(workflow) == f"The promise was NOT recorded. Never say it is recorded. {hook}"


# ---- C-11 / C-12: several products ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("profile", ["staged", "lean"])
def test_several_product_notes_are_written_at_the_end_never_before_a_transfer(profile: str, api: ToolApi) -> None:
    task = build(IR_AB, LEDGER_AB, api, profile)["nodes"]["negotiate"]["task_message"]
    assert "On any transfer add no notes at all." in task
    for reason in ("paid", "dispute"):
        assert (f'Take no promise for this product; add_note reason "{reason}" (do not call add_note yet - write it only '
                "at the very end); continue with the others.") in task
    # a refusal to talk at all closes the call (AUD-11): every open product is noted at once, then the call ends
    refuse = next(line for line in task.splitlines() if line.startswith("- Says they do not want to talk"))
    assert "continue with the others" not in refuse
    assert ('Call add_note once for each product without a promise - with the reason already remembered for it, else '
            'reason "refused" - and wait for the results; then, in your next message, thank them, say goodbye and ') in refuse
    assert "Call add_note only at the very end" in task
    # every sentence that mentions the notes tool defers it: the transfer rule and the notes can no longer collide
    for line in task.splitlines():
        if "add_note" in line and "On any transfer" not in line:
            assert any(marker in line for marker in (
                "do not call add_note yet", "Call add_note only at the very end", "Close:", "B7.", "remember")), line


def test_notes_are_written_at_once_when_the_policy_wants_notes_on_a_transfer(api: ToolApi) -> None:
    ir = IR_AB.model_copy(deep=True)
    ir.multi_product.notes_on_transfer = True
    task = build(ir, LEDGER_AB, api)["nodes"]["negotiate"]["task_message"]
    assert 'Take no promise for this product; add_note reason "paid"; continue with the others.' in task
    assert "do not call add_note yet" not in task and "On any transfer add no notes" not in task


def test_the_notes_tool_description_agrees_with_the_deferral(api: ToolApi) -> None:
    tool = next(t for t in build(IR_AB, LEDGER_AB, api)["nodes"]["negotiate"]["tools"]
                if t["function"]["name"] == "add_note")
    description = tool["function"]["description"]
    assert "only at the very end" in description and "never before a transfer" in description


DEFERRED_NOTE = ('then remember the reason "service_error" for that product (do not call add_note yet) and continue '
                 "with the next product")
IMMEDIATE_NOTE = 'then call add_note with that product_id and reason "service_error" and continue with the next product'


@pytest.mark.parametrize("profile", ["staged", "lean"])
@pytest.mark.parametrize(("notes_on_transfer", "note_step"), [(False, DEFERRED_NOTE), (True, IMMEDIATE_NOTE)])
def test_a_failed_promise_in_a_several_product_call_is_noted_and_the_call_goes_on(
    profile: str, notes_on_transfer: bool, note_step: str, api: ToolApi
) -> None:
    ir = IR_AB.model_copy(deep=True)
    ir.multi_product.notes_on_transfer = notes_on_transfer
    workflow = build(ir, LEDGER_AB, api, profile)
    rule = line_with(workflow["nodes"]["negotiate"]["task_message"], "If it fails again")
    assert rule == ("If it fails, call it once more with the same values. If it fails again, say "
                    f'"{SENTENCE}", {note_step} (rule B7).')
    message = failure_message(workflow)
    assert message == ("The promise was NOT recorded. Never say it is recorded. If this is the first failure for this "
                       "product, call create_promise once more with exactly the same values now. If it already failed "
                       f'once before, say "{SENTENCE}", {note_step} (rule B7).')
    assert "transition_to_end_recording_failed" not in rule + message
    assert "B7. If create_promise fails twice for one product" in workflow["nodes"]["negotiate"]["task_message"]


def test_a_failed_promise_for_a_single_product_still_ends_the_call(api: ToolApi) -> None:
    workflow = build(IR_A, LEDGER_A, api)
    assert "transition_to_end_recording_failed" in failure_message(workflow)
    assert "B7." not in workflow["nodes"]["negotiate"]["task_message"]


# ---- C-13: one transition per target -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("brief", ["A", "AB"])
def test_no_node_has_two_transitions_to_the_same_target(brief: str, api: ToolApi) -> None:
    ir, ledger = BRIEFS[brief]
    workflow = build(ir, ledger, api)
    for name, node in workflow["nodes"].items():
        targets = [t["target_node"] for t in node.get("transitions", [])]
        assert len(targets) == len(set(targets)), (name, targets)
    third = workflow["nodes"]["third_party"]
    loop = next(t for t in third["transitions"] if t["condition"].get("operator") == "matches_intent"
                and not t.get("is_tool_call"))
    exit_tool = next(t for t in third["transitions"] if t.get("is_tool_call")
                     and t["target_node"] == "end_callback_not_booked")
    assert loop["target_node"] == "end_auto_third_party" != exit_tool["target_node"]
    snippet = extract_code_snippets(workflow)["end_auto_third_party"]
    assert run_snippet(snippet, {})["chosen_end"] == "callback_not_booked"
    assert workflow["nodes"]["end_auto_third_party"]["passthrough_default_transition"] == "decide"
    assert lint_workflow(workflow, api, set(ir.call_variable_names())).by_rule("L028") == []


# ---- C-15: a hardship latches --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("brief", ["A", "AB"])
def test_the_hardship_latch_is_extracted_next_to_the_call_situation(brief: str, api: ToolApi) -> None:
    ir, ledger = BRIEFS[brief]
    schema = {f["name"]: f for f in build(ir, ledger, api)["nodes"]["negotiate"]["extraction_schema"]}
    latch = schema["hardship_seen"]
    assert latch["type"] == "string" and latch["enum_values"] == ["yes", "no"] and latch["min_confidence"] == 0.6
    assert "at ANY point in this call" in latch["description"]
    assert "a later request for a discount or a waiver does not change it" in latch["description"]
    assert "restructuring" not in schema["call_situation"]["enum_values"]


@pytest.mark.parametrize("brief", ["A", "AB"])
def test_the_overridden_transfer_goes_through_a_guard(brief: str, api: ToolApi) -> None:
    ir, ledger = BRIEFS[brief]
    workflow = build(ir, ledger, api)
    negotiate = workflow["nodes"]["negotiate"]
    exits = {t["target_node"] for t in negotiate["transitions"] if t.get("is_tool_call")}
    assert "guard_restructuring" in exits and "handoff_restructuring" not in exits
    assert {"handoff_support", "handoff_general"} <= exits  # nothing else is guarded
    assert ("end_hardship" in exits) is (brief == "A")  # with several products a declined offer becomes a note
    guard = workflow["nodes"]["guard_restructuring"]
    assert guard["node_type"] == "code" and guard["passthrough"] is True
    assert guard["passthrough_default_transition"] == "handoff_restructuring"
    first, last = guard["transitions"][0], guard["transitions"][-1]
    assert first["target_node"] == "guard_restructuring_say"
    assert first["condition"] == {"logic": "or", "conditions": [
        {"variable": "restructuring_blocked", "operator": "equals", "operand": "yes"}]}
    assert last["target_node"] == "handoff_restructuring" and last["condition"] == {"operator": "always"}
    say = workflow["nodes"]["guard_restructuring_say"]
    assert say["on_enter"] == {"type": "tts", "text": "I am sorry, I cannot arrange that."}
    assert say["passthrough"] is True and say["passthrough_default_transition"] == "negotiate"
    report = lint_workflow(workflow, api, set(ir.call_variable_names()))
    assert report.ok and not [f for f in report.warnings if f.rule != "L018"], report.format()


@pytest.mark.parametrize(
    ("variables", "blocked"),
    [
        ({"hardship_seen": "yes"}, "yes"),
        ({"hardship_seen": "Yes", "call_situation": "none"}, "yes"),
        ({"call_situation": "hardship"}, "yes"),
        ({"hardship_seen": "no", "call_situation": "none"}, "no"),
        ({}, "no"),
    ],
)
def test_the_guard_blocks_the_transfer_only_after_a_hardship(variables: dict[str, str], blocked: str,
                                                              api: ToolApi) -> None:
    code = extract_code_snippets(build(IR_A, LEDGER_A, api))["guard_restructuring"]
    assert run_snippet(code, variables)["restructuring_blocked"] == blocked


def test_the_pub_05_sequence_cannot_end_in_the_restructuring_queue(api: ToolApi) -> None:
    """Hardship first, then 'I just need a discount': the extraction flips to ``restructuring`` in the old design."""
    workflow = build(IR_A, LEDGER_A, api)
    code = extract_code_snippets(workflow)["guard_restructuring"]
    after_second_message = {"hardship_seen": "yes", "call_situation": "none"}
    assert run_snippet(code, after_second_message)["restructuring_blocked"] == "yes"
    node = workflow["nodes"]["guard_restructuring"]
    assert [t["target_node"] for t in node["transitions"]] == ["guard_restructuring_say", "handoff_restructuring"]


def test_a_policy_without_overrides_has_no_guards(api: ToolApi) -> None:
    ir = IR_A.model_copy(deep=True)
    for intent in ir.intents:
        intent.overrides = []
    workflow = build(ir, LEDGER_A, api)
    assert not [n for n in workflow["nodes"] if n.startswith("guard_")]
    assert "hardship_seen" not in json.dumps(workflow["nodes"]["negotiate"]["extraction_schema"])
    assert any(t["target_node"] == "handoff_restructuring" for t in workflow["nodes"]["negotiate"]["transitions"])


def test_every_overridden_exit_of_a_custom_policy_is_guarded(api: ToolApi) -> None:
    """An intent that overrides an ``end`` exit (not a transfer) is guarded the same way."""
    ir = IR_A.model_copy(deep=True)
    for intent in ir.intents:
        if intent.key == "hardship":
            intent.overrides = ["flat_refusal", "dispute"]
    workflow = build(ir, LEDGER_A, api)
    exits = {t["target_node"] for t in workflow["nodes"]["negotiate"]["transitions"] if t.get("is_tool_call")}
    assert "guard_dispute" in exits and "end_dispute" not in exits and "guard_restructuring" not in exits
    assert workflow["nodes"]["guard_dispute"]["passthrough_default_transition"] == "end_dispute"


# ---- C-19: phrases the brief does not prescribe ------------------------------------------------------------------------------


def _blank_phrases(ir: PolicyIR) -> PolicyIR:
    ir = ir.model_copy(deep=True)
    ir.confirmation.question = ""
    ir.phrases.confirm_question = ""
    ir.phrases.today_question = ""
    ir.persuasion.first_ask = ""
    ir.identity.decline_explanation = ""
    ir.phrases.extra.pop("decline_explanation", None)
    ir.phrases.opening_sentence = ""
    ir.phrases.right_party_question = ""
    ir.right_party.confirm_examples = []
    return ir


LEDGER_WITHOUT_RECORDING_RULE = LEDGER_A.model_copy(update={"rules": [r for r in LEDGER_A.rules if r.id != "A-1a"]})


def test_a_missing_opening_sentence_is_an_error_when_the_brief_requires_a_recording_notice(api: ToolApi) -> None:
    with pytest.raises(CompileError, match=r"A-1a.*recording notice"):
        compile_workflow(_blank_phrases(IR_A), api, LEDGER_A)


def test_blank_phrases_never_become_empty_quotes(api: ToolApi) -> None:
    compiled = compile_workflow(_blank_phrases(IR_A), api, LEDGER_WITHOUT_RECORDING_RULE)
    workflow = compiled.workflow
    texts = [workflow["system_prompt"], *(n.get("task_message", "") for n in workflow["nodes"].values())]
    for text in texts:
        assert '""' not in text, [line for line in text.splitlines() if '""' in line]
    opening = workflow["nodes"]["opening"]["task_message"]
    assert "- Only \"yes\", \"that's me\" or \"speaking\" confirms that you are speaking with" in opening
    assert "Only  confirms" not in opening
    negotiate = workflow["nodes"]["negotiate"]["task_message"]
    assert 'You already stated the debt and asked: "Can you pay the full amount today?"' in negotiate
    assert 'ask exactly "Is that correct?"' in negotiate
    assert 'explain once that "The questions are needed before we can continue."' in workflow["nodes"]["identity"][
        "task_message"]
    assert workflow["nodes"]["opening"]["on_enter"]["text"] == (
        "Hello, this is Freya Bank's digital assistant. Am I speaking with {{title}} {{last_name}}?")
    assert any("opening sentence" in note for note in compiled.notes)
    state = run_snippet(extract_code_snippets(workflow)["setup"], {"now": "2026-10-14T11:00"})
    state = run_snippet(extract_code_snippets(workflow)["account_facts"], {**state, "products": [
        {"product_id": "p1", "type": "credit_card", "card_last4": "1", "past_due": 10, "days_late": 3,
         "minimum_payment": 2}]})
    assert state["debt_statement"].endswith("Can you pay the full amount today?")
    assert lint_workflow(workflow, api, set(IR_A.call_variable_names())).ok


def test_the_prescribed_phrases_are_untouched(api: ToolApi) -> None:
    workflow = build(IR_A, LEDGER_A, api)
    assert 'You already stated the debt and asked: "Can you make this payment today?"' in workflow["nodes"][
        "negotiate"]["task_message"]
    assert workflow["nodes"]["opening"]["on_enter"]["text"].startswith("Hello, I am a digital assistant calling from")


# ---- C-22: the rule map names mechanisms that exist -------------------------------------------------------------------------


def _missing_mechanisms(workflow: dict[str, Any], rule_map: dict[str, list[str]]) -> dict[tuple[str, str], list[str]]:
    nodes, codes = workflow["nodes"], extract_code_snippets(workflow)
    tools: dict[str, list[dict[str, Any]]] = {}
    for node in nodes.values():
        for tool in node.get("tools", []):
            tools.setdefault(tool["function"]["name"], []).append(tool)
    bad: dict[tuple[str, str], list[str]] = {}
    for rule_id, places in rule_map.items():
        for place in places:
            why = ""
            if place == "system_prompt":
                why = "" if workflow.get("system_prompt") else "no system prompt"
            elif place.startswith("node:"):
                name, _, field = place[5:].partition("/")
                if name not in nodes:
                    why = f"no node {name}"
                elif field and not nodes[name].get(field):
                    why = f"node {name} has no {field}"
            elif place.startswith("code:"):
                why = "" if place[5:] in codes else f"no code node {place[5:]}"
            elif place.startswith("tool:"):
                name, _, hook = place[5:].partition("/")
                if name not in tools:
                    why = f"no node lists tool {name}"
                elif hook and not any(t.get(hook) for t in tools[name]):
                    why = f"tool {name} has no {hook}"
            else:
                why = "unknown location kind"
            if why:
                bad.setdefault((place, why), []).append(rule_id)
    return bad


@pytest.mark.parametrize("profile", ["staged", "lean"])
@pytest.mark.parametrize("brief", ["A", "AB"])
def test_every_rule_map_location_exists_in_the_workflow(brief: str, profile: str, api: ToolApi) -> None:
    ir, ledger = BRIEFS[brief]
    compiled = compile_workflow(ir, api, ledger, None, profile)  # type: ignore[arg-type]
    assert _missing_mechanisms(compiled.workflow, compiled.rule_map) == {}


def test_a_single_product_brief_does_not_claim_the_notes_hook(api: ToolApi) -> None:
    compiled = compile_workflow(IR_A, api, LEDGER_A)
    assert not [r for r, places in compiled.rule_map.items() if "tool:add_note/pre_process_code" in places]
    multi = compile_workflow(IR_AB, api, LEDGER_AB)
    assert [r for r, places in multi.rule_map.items() if "tool:add_note/pre_process_code" in places]


def test_the_guard_nodes_are_in_the_rule_map(api: ToolApi) -> None:
    compiled = compile_workflow(IR_A, api, LEDGER_A)
    places = {p for ps in compiled.rule_map.values() for p in ps}
    assert "node:guard_restructuring" in places and "code:guard_restructuring" in places


# ---- the tools a prompt names are the tools the engine offers -----------------------------------------------------------------


@pytest.mark.parametrize("brief", ["A", "AB"])
def test_every_transition_tool_a_prompt_names_exists_in_that_node(brief: str, api: ToolApi) -> None:
    """The engine names the tool of a tool-call transition ``transition_to_<target>``: a prompt that names another one
    sends the model to a function that does not exist (the guard in front of an exit changes the name)."""
    import re

    ir, ledger = BRIEFS[brief]
    workflow = build(ir, ledger, api)
    for name, node in workflow["nodes"].items():
        offered = {f"transition_to_{t['target_node']}" for t in node.get("transitions", []) if t.get("is_tool_call")}
        named = set(re.findall(r"transition_to_[a-z0-9_]+", node.get("task_message", "")))
        assert named <= offered, (name, sorted(named - offered))
        for tool in node.get("tools", []):
            hook = tool.get("post_process_code", "")
            assert set(re.findall(r"transition_to_[a-z0-9_]+", hook)) <= offered | {"transition_to_load_account"}, name


def test_the_restructuring_bullet_names_the_guards_tool(api: ToolApi) -> None:
    task = build(IR_A, LEDGER_A, api)["nodes"]["negotiate"]["task_message"]
    bullet = line_with(task, "Asks for a new payment plan")
    assert "say goodbye and call transition_to_guard_restructuring." in bullet
    assert "transition_to_handoff_restructuring" not in task
    assert "call transition_to_handoff_support" in line_with(task, "Mentions illness")
