"""Tests for the prompt layer: Jinja environment, prose cleaning, context helpers and situation naming."""

from __future__ import annotations

import json
from pathlib import Path

import jinja2
import pytest

from builder.checks.lint import lint_workflow
from builder.compiler import naming
from builder.compiler.compile import compile_workflow
from builder.compiler.context import (
    LeanExits,
    StagedExits,
    exit_bullet,
    ids_tag,
    ordinal,
    outcome_text,
    person_refs,
    quoted,
    soften_recording_notice,
    times_text,
    to_freya,
    uncovered_rules,
    words,
)
from builder.compiler.prompts import TEMPLATES, environment, render, tidy
from builder.compiler.prose import NodeProse, clean_text, default_prose
from builder.ingest.tool_api import ToolApi
from builder.policy.ir import (
    IntentRule,
    OutcomeRef,
    PolicyIR,
    ResidualRule,
    RuleEntry,
    RuleLedger,
    ToolAction,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "policy"


def _ir(name: str) -> PolicyIR:
    return PolicyIR.model_validate(json.loads((FIXTURES / f"{name}_ir.json").read_text(encoding="utf-8")))


def _ledger(name: str) -> RuleLedger:
    return RuleLedger.model_validate(json.loads((FIXTURES / f"{name}_ledger.json").read_text(encoding="utf-8")))


IR_A, IR_AB = _ir("brief_a"), _ir("brief_ab")
LEDGER_A, LEDGER_AB = _ledger("brief_a"), _ledger("brief_ab")


# ---- prompts -----------------------------------------------------------------------------------------------------------


def test_every_template_exists_and_compiles() -> None:
    for name in TEMPLATES:
        environment().get_template(f"{name}.v1.j2")


def test_freya_variables_pass_through_and_jinja_uses_angle_delimiters() -> None:
    text = render("system_prompt", bank_name="Acme", persona="digital assistant", language="English", forbidden="",
                  robot_answer="", scam_answer="", unknown_answer="", currency="", global_rules=[], extra="Hi {{title}}")
    assert "Acme's digital assistant" in text and text.endswith("Hi {{title}}")
    assert "<<" not in text and "<%" not in text


def test_a_missing_value_fails_loudly() -> None:
    with pytest.raises(jinja2.UndefinedError):
        render("system_prompt", bank_name="Acme")


def test_inline_block_tags_keep_their_line_break() -> None:
    text = render("opening", question="Q?", address="Mr X", confirm='"yes"', not_confirm='"hello"', max_asks=2,
                  ordinal="second", unclear_action="stop", confirmed_action="go on", third_party_action="other",
                  exits=[], rules=[], extra="")
    lines = text.splitlines()
    assert lines[1].startswith('- Only "yes" confirms that you are speaking with Mr X.')
    assert lines[2].startswith("- If it is not a confirmation, ask once more exactly")
    assert not any(". - " in line or '"- ' in line for line in lines)


def test_tidy_collapses_blank_runs() -> None:
    assert tidy("a  \n\n\n\nb \n") == "a\n\nb"


# ---- prose -------------------------------------------------------------------------------------------------------------


def test_clean_text_defuses_unknown_references_and_limits_length() -> None:
    cleaned = clean_text("Hi {{title}} {{ system.conversation }} {{bogus}} {{amount:json}} {{ broken", ["title", "amount"])
    assert cleaned.startswith("Hi {{title}} {{ system.conversation }} <bogus> {{amount:json}}")
    assert cleaned.endswith("broken") and "{{ broken" not in cleaned
    assert clean_text("a\x00b\x07c\n\n\n\nd") == "abc\n\nd"
    long = "word " * 1000
    assert len(clean_text(long, max_chars=100)) <= 100
    assert clean_text("") == ""


def test_node_prose_lookup_and_default() -> None:
    prose = NodeProse(system_prompt_extra="x", per_node={"negotiate": "y"})
    assert prose.for_node("negotiate") == "y" and prose.for_node("opening") == ""
    assert default_prose(IR_A, LEDGER_A) == NodeProse()


# ---- context helpers -----------------------------------------------------------------------------------------------------


def test_wording_helpers() -> None:
    assert (words(3), words(12), ordinal(2), ordinal(11), times_text(1), times_text(2), times_text(4)) == (
        "three", "12", "second", "11th", "once", "twice", "four times")
    assert quoted(["a", "b", "c"]) == '"a", "b" or "c"' and quoted(["a"]) == '"a"' and quoted([]) == ""
    assert quoted(["a", "b"], "and") == '"a" and "b"'
    assert ids_tag([]) == "" and ids_tag(["A-1", "A-1", "A-2"]) == "[A-1, A-2]"
    assert ids_tag([f"R{i}" for i in range(7)]) == "[R0, R1, R2, R3, +3]"
    assert outcome_text(OutcomeRef(outcome="x", reason="y")) == 'outcome "x" and reason "y"'
    assert outcome_text(OutcomeRef(outcome="x")) == 'outcome "x"'


def test_placeholder_mapping_only_touches_call_variables() -> None:
    assert to_freya("Am I speaking with {title} {last_name}? {other} {{x}}", ["title", "last_name"]) == (
        "Am I speaking with {{title}} {{last_name}}? {other} {{x}}")


def test_the_recording_notice_is_rephrased_but_other_text_is_not() -> None:
    assert soften_recording_notice("Hello. This call is recorded.") == "Hello. We record this call."
    assert soften_recording_notice("This call is being recorded for quality.") == "We record this call for quality."
    assert soften_recording_notice("Your promise is recorded.") == "Your promise is recorded."


def test_person_refs() -> None:
    assert person_refs(IR_A) == ("{{title}} {{last_name}}", "{{first_name}} {{last_name}}")
    generic = PolicyIR.example_minimal()
    assert person_refs(generic) == ("the customer", "the customer")


def test_uncovered_rules_are_verbatim_and_never_superseded() -> None:
    ledger = RuleLedger(rules=[
        RuleEntry(id="R1", source="b", quote="Cited rule.", category="other", statement="s", scope=["negotiation"]),
        RuleEntry(id="R2", source="b", quote="Loose  rule\nwith spaces.", category="other", statement="s",
                  scope=["negotiation"]),
        RuleEntry(id="R3", source="b", quote="Replaced.", category="other", statement="s", scope=["negotiation"],
                  superseded_by="R4"),
        RuleEntry(id="R4", source="b", quote="Other stage.", category="other", statement="s", scope=["identity"]),
        RuleEntry(id="R5", source="b", quote="Residual quote.", category="other", statement="s", scope=["global"]),
    ])
    ir = PolicyIR.example_minimal().model_copy(deep=True)
    ir.amounts.rule_ids = ["R1"]
    ir.residual_rules = [ResidualRule(rule_id="R5", text="Residual text.", scope=["global"])]
    assert uncovered_rules(ir, ledger, ["negotiation"]) == [("R2", "Loose rule with spaces.")]
    assert uncovered_rules(ir, ledger, ["global"]) == [("R5", "Residual text.")]
    assert uncovered_rules(ir, ledger, ["identity"]) == [("R4", "Other stage.")]
    clipped = uncovered_rules(ir, ledger, ["negotiation"], max_chars=5)
    assert clipped == [("R2", "Loose ...")]


def _intent(**kwargs: object) -> IntentRule:
    return IntentRule(key="k", description="Asks for a person.", **kwargs)  # type: ignore[arg-type]


def test_exit_bullets_for_staged_and_lean() -> None:
    ir = PolicyIR.example_minimal()
    human = ir.intents[0]
    staged = exit_bullet(ir, human, StagedExits())
    assert staged == "human_request".replace("human_request", "Asks for a person") + (
        ": Say you will transfer them, say goodbye and call transition_to_handoff_people.")
    lean = exit_bullet(ir, human, LeanExits())
    assert lean is not None and 'call transfer_to_agent with queue "people", then close_call with outcome "handed_over" and reason "person_requested"' in lean
    offer = _intent(offer="Ask once.", actions=[ToolAction(tool="transfer_to_agent", params={"queue": "q"})],
                    outcome=OutcomeRef(outcome="o"), declined_outcome=OutcomeRef(outcome="n", reason="r"),
                    rule_ids=["A-1"])
    bullet = exit_bullet(ir, offer, StagedExits())
    assert bullet is not None and "If they accept or insist" in bullet and "do not ask again" in bullet and bullet.endswith("[A-1]")
    ending = _intent(outcome=OutcomeRef(outcome="done"), min_occurrences=2, response="Be brief.")
    text = exit_bullet(ir, ending, StagedExits())
    assert text is not None and "twice" in text and "Be brief." in text and "transition_to_end_k" in text
    assert exit_bullet(ir, _intent(response="Just answer."), StagedExits()) is None


def test_note_bullets_in_multi_product_mode() -> None:
    bullets = {i.key: exit_bullet(IR_AB, i, StagedExits(), note_tool="add_note") for i in IR_AB.intents}
    assert bullets["already_paid"] is not None and 'add_note reason "paid"' in bullets["already_paid"]
    assert bullets["hardship"] is not None and "transition_to_handoff_support" in bullets["hardship"]
    assert 'reason "hardship"' in bullets["hardship"]


# ---- naming ------------------------------------------------------------------------------------------------------------


def test_node_names() -> None:
    assert naming.slug("Transfer:Support Team") == "transfer_support_team" and naming.slug("***") == "x"
    assert naming.end_node("flat_refusal") == "end_flat_refusal"
    assert naming.handoff_node("general") == "handoff_general"
    assert naming.transition_tool("end_x") == "transition_to_end_x"


def test_intent_exit_classification() -> None:
    kinds = {i.key: naming.intent_exit(IR_A, i) for i in IR_A.intents}
    assert kinds["already_paid"].kind == "end" and kinds["already_paid"].situation == "already_paid"
    assert kinds["restructuring"].kind == "handoff" and kinds["restructuring"].queue == "restructuring"
    hardship = kinds["hardship"]
    assert hardship.kind == "handoff" and hardship.queue == "support"
    assert hardship.declined is not None and hardship.declined.kind == "end" and hardship.declined.situation == "hardship"
    assert kinds["decline_verification"].situation == "identity_declined"
    assert kinds["wrong_number"].situation == "wrong_number"
    assert kinds["proposes_date"].kind == "info"
    multi = {i.key: naming.intent_exit(IR_AB, i) for i in IR_AB.intents}
    assert multi["already_paid"].kind == "note" and multi["already_paid"].reason == "paid"
    assert multi["hardship"].declined is not None and multi["hardship"].declined.kind == "note"


def test_stop_situation_queues_and_negotiation_situations() -> None:
    assert naming.stop_situation(IR_A) == "flat_refusal"
    assert naming.transfer_queues(IR_A) == ["general", "support", "restructuring"] or set(naming.transfer_queues(IR_A)) == {
        "general", "support", "restructuring"}
    assert set(naming.negotiation_situations(IR_A)) == {"refuse_to_talk", "hardship", "already_paid", "dispute", "flat_refusal"}
    ir = PolicyIR.example_minimal()
    assert naming.transfer_queues(ir) == ["people"] and naming.stop_situation(ir) == "flat_refusal"


def test_closing_rows_add_derived_rows_without_overriding_the_table() -> None:
    ir = PolicyIR.example_minimal().model_copy(deep=True)
    ir.closing.rows = [r for r in ir.closing.rows if r.situation != "unclear_party"]
    rows = naming.closing_rows(ir)
    derived = [r for r in rows if r.when.startswith("(derived")]
    assert [r.situation for r in derived] == ["unclear_party"]
    assert (derived[0].outcome, derived[0].reason) == ("not_contacted", "unclear")
    assert naming.closing_rows(IR_A)[: len(IR_A.closing.rows)] == IR_A.closing.rows


# ---- a policy with different names and a missing optional part ------------------------------------------------------------


def test_the_generic_example_policy_compiles(kit_tool_api: ToolApi) -> None:
    ir = PolicyIR.example_minimal()
    compiled = compile_workflow(ir, kit_tool_api, RuleLedger(rules=[]))
    assert any("people" in note for note in compiled.notes)  # the example queue is not in the kit's enum
    assert "handoff_people" in compiled.workflow["nodes"]
    report = lint_workflow(compiled.workflow, kit_tool_api, set(ir.call_variable_names()))
    assert [f.rule for f in report.errors] == ["L012"]  # exactly the queue that is not in the contract
    # the persona noun is the one of the policy's own opening sentence ("... the automated assistant of Example Lender")
    assert "Example Lender's automated assistant" in compiled.workflow["system_prompt"]


def test_a_policy_without_a_callback_rule(kit_tool_api: ToolApi) -> None:
    ir = IR_A.model_copy(deep=True)
    ir.third_party.callback = None
    compiled = compile_workflow(ir, kit_tool_api, LEDGER_A)
    third = compiled.workflow["nodes"]["third_party"]
    assert "tools" not in third and "callback_done" not in compiled.workflow["nodes"]
    assert "schedule_callback" not in json.dumps(third)
    assert lint_workflow(compiled.workflow, kit_tool_api, set(ir.call_variable_names())).ok


def test_extra_phrase_lookup_is_tolerant() -> None:
    from builder.compiler.context import extra_phrase

    extra = {"collections_intro": " Hello there. ", "below_floor": "Too low.", "empty": "  "}
    assert extra_phrase(extra, "disclosure_intro", "intro") == "Hello there."
    assert extra_phrase(extra, "below_floor") == "Too low."
    assert extra_phrase(extra, "empty") == "" and extra_phrase(extra, "missing") == ""
