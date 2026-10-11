"""Every closing situation a stage can reach has an exit, and every intent action is placed (audit AUD-08, AUD-11).

* AUD-11: a call-level intent (the customer does not want to talk at all) closes a several-product call instead of
  "note this product and continue with the others"; an intent with no outcome whose key names an outcome-table row ends
  on that row; a pre-verification intent the ledger scopes to the third-party step gets a third-party exit.
* AUD-08: a note action of an intent (``add_note`` with a reason) is honoured as the intent's note reason, and in a
  single-product brief it is written by the engine before the end node; a callback action of an intent puts the callback
  tool on the stages the intent acts in and routes on its acknowledgement.

All IR variants are built from the generic fixtures in the test; nothing here is specific to one brief.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from builder.checks.code_sandbox import extract_code_snippets, run_snippet
from builder.checks.lint import lint_workflow
from builder.compiler import naming
from builder.compiler.compile import compile_workflow
from builder.ingest.tool_api import ToolApi, load_tool_api
from builder.policy.ir import IntentRule, OutcomeRef, OutcomeRow, PolicyIR, RuleEntry, RuleLedger, ToolAction

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "fixtures" / "policy"


def _ir(name: str) -> PolicyIR:
    return PolicyIR.model_validate(json.loads((FIXTURES / f"{name}_ir.json").read_text(encoding="utf-8")))


def _ledger(name: str) -> RuleLedger:
    return RuleLedger.model_validate(json.loads((FIXTURES / f"{name}_ledger.json").read_text(encoding="utf-8")))


IR_A, IR_AB = _ir("brief_a"), _ir("brief_ab")
LEDGER_A, LEDGER_AB = _ledger("brief_a"), _ledger("brief_ab")


@pytest.fixture(scope="module")
def api() -> ToolApi:
    return load_tool_api(REPO / "tools" / "openapi.json")


def build(ir: PolicyIR, ledger: RuleLedger, api: ToolApi, profile: str = "staged") -> dict[str, Any]:
    return compile_workflow(ir, api, ledger, None, profile).workflow  # type: ignore[arg-type]


def targets(workflow: dict[str, Any], node: str) -> list[str]:
    return [t["target_node"] for t in workflow["nodes"][node].get("transitions", [])]


def bullet(task: str, start: str) -> str:
    return next(line for line in task.splitlines() if line.startswith(f"- {start}"))


def lean_section(task: str) -> str:
    """The third-party section of the lean ``call`` node's task."""
    return task.split("IF SOMEONE ELSE ANSWERED (not the customer)")[1].split("IDENTITY (only")[0]


def tool_names(node: dict[str, Any]) -> list[str]:
    return [t["function"]["name"] for t in node.get("tools", [])]


def decide(workflow: dict[str, Any], **state: Any) -> tuple[str, str | None, str]:
    snippets = extract_code_snippets(workflow)
    chosen = state.pop("end", None)
    variables: dict[str, Any] = {"product_ids": "p1,p2", **state}
    if chosen is not None:
        variables = run_snippet(snippets[chosen], variables)
    out = run_snippet(snippets["decide"], variables)
    return out["final_outcome"], out.get("final_reason"), out["decide_situation"]


def with_intents(ir: PolicyIR, *intents: IntentRule, drop: tuple[str, ...] = ()) -> PolicyIR:
    copy = ir.model_copy(deep=True)
    copy.intents = [i for i in copy.intents if i.key not in drop] + list(intents)
    return copy


def with_rules(ledger: RuleLedger, *rules: RuleEntry) -> RuleLedger:
    copy = ledger.model_copy(deep=True)
    copy.rules = [*copy.rules, *rules]
    return copy



# ---- AUD-11 (a): a call-level intent closes a several-product call ---------------------------------------------------


@pytest.mark.parametrize("profile", ["staged", "lean"])
def test_multi_product_refuse_to_talk_notes_every_open_product_and_ends(profile: str, api: ToolApi) -> None:
    workflow = build(IR_AB, LEDGER_AB, api, profile)
    task = workflow["nodes"]["negotiate"]["task_message"]
    refuse = bullet(task, "Says they do not want to talk")
    assert "continue with the others" not in refuse
    assert "any product not handled yet" in refuse
    assert 'reason "refused"' in refuse
    if profile == "staged":
        assert "transition_to_end_refuse_to_talk" in refuse
        assert "end_refuse_to_talk" in targets(workflow, "negotiate")
    else:
        assert "close_call" in refuse
    # per-product intents keep "note and continue" (a dispute or a payment claim on one product does not end the call)
    for start in ("Says they already paid", "Disputes"):
        line = next((x for x in task.splitlines() if x.startswith(f"- {start}")), None)
        if line is not None:
            assert "continue with the others" in line, line


def test_multi_product_refuse_to_talk_outcome_comes_from_the_notes(api: ToolApi) -> None:
    workflow = build(IR_AB, LEDGER_AB, api)
    end = "end_refuse_to_talk"
    assert decide(workflow, end=end, identity_verified="yes", notes_log="p1:refused;p2:refused;")[:2] == (
        "no_promise", "refused")
    assert decide(workflow, end=end, identity_verified="yes", notes_log="p2:refused;",
                  create_promise_success_count=1)[0] == "promise"
    assert decide(workflow, end=end, identity_verified="yes", notes_log="p1:dispute;p2:refused;")[:2] == (
        "no_promise", "dispute")


def test_the_multi_product_refusal_exit_lints_clean(api: ToolApi) -> None:
    workflow = build(IR_AB, LEDGER_AB, api)
    report = lint_workflow(workflow, api, set(IR_AB.call_variable_names()))
    assert report.ok, report.format()


# ---- AUD-11 (b): an intent with no outcome ends on the outcome row keyed by it ---------------------------------------


def _merged_refusal_ir() -> PolicyIR:
    """Brief-A-like IR where the refusal intent was extracted once, for any stage, with no outcome (seen in live runs)."""
    old = [i for i in IR_A.intents if i.key == "refuse_to_talk"]
    merged = IntentRule(key="refuse_to_talk", description=old[0].description, scope="any", min_occurrences=2,
                        rule_ids=[r for i in old for r in i.rule_ids])
    return with_intents(IR_A, merged, drop=("refuse_to_talk",))


@pytest.mark.parametrize("profile", ["staged", "lean"])
def test_intent_without_outcome_ends_on_its_outcome_row(profile: str, api: ToolApi) -> None:
    ir = _merged_refusal_ir()
    assert naming.intent_exit(ir, ir.intent("refuse_to_talk")).kind == "end"  # type: ignore[arg-type]
    workflow = build(ir, LEDGER_A, api, profile)
    if profile == "staged":
        for node in ("opening", "identity", "negotiate"):
            assert "end_refuse_to_talk" in targets(workflow, node), node
            assert "transition_to_end_refuse_to_talk" in workflow["nodes"][node]["task_message"], node
        assert decide(workflow, end="end_refuse_to_talk")[:2] == ("not_verified", "declined")
        assert decide(workflow, end="end_refuse_to_talk", identity_verified="yes")[:2] == ("no_promise", "refused")
    else:
        assert "close_call" in bullet(workflow["nodes"]["call"]["task_message"], "Says they do not want to talk")


def test_the_stop_situation_stays_on_the_persuasion_ladder(api: ToolApi) -> None:
    """flat_refusal has a row of its own, but it is reached through the persuasion rule, never as an intent exit."""
    ir = IR_A.model_copy(deep=True)
    ir.closing.rows.append(OutcomeRow(outcome="no_promise", reason="refused", situation="flat_refusal",
                                      requires_verified=True))
    flat = ir.intent("flat_refusal", verified=True)
    assert flat is not None
    assert naming.intent_exit(ir, flat).kind == "info"


# ---- AUD-11 (c): a pre-verification intent scoped to the third-party step gets a third-party exit ---------------------


def _third_party_variant(scope: list[str]) -> tuple[PolicyIR, RuleLedger]:
    intent = IntentRule(key="contact_left", description="The person says the customer no longer works or lives there",
                        scope="before_verification", response="Thank them for letting you know.",
                        outcome=OutcomeRef(outcome="not_reached", reason="unclear"), rule_ids=["X-1"])
    rule = RuleEntry(id="X-1", source="variant.md", quote="If the customer has left, close the call.",
                     category="third_party", statement="Close when the customer left.", scope=scope)  # type: ignore[arg-type]
    return with_intents(IR_A, intent), with_rules(LEDGER_A, rule)


def test_third_party_scoped_intent_gets_a_third_party_exit(api: ToolApi) -> None:
    ir, ledger = _third_party_variant(["opening", "third_party"])
    workflow = build(ir, ledger, api)
    situation = naming.intent_exit(ir, ir.intents[-1]).situation
    assert naming.end_node(situation) in targets(workflow, "third_party")
    assert "no longer works or lives there" in workflow["nodes"]["third_party"]["task_message"]
    lean = build(ir, ledger, api, "lean")
    third = lean_section(lean["nodes"]["call"]["task_message"])
    assert "no longer works or lives there" in third


def test_an_intent_not_scoped_to_the_third_party_step_stays_out_of_it(api: ToolApi) -> None:
    ir, ledger = _third_party_variant(["opening"])
    workflow = build(ir, ledger, api)
    assert "no longer works or lives there" not in workflow["nodes"]["third_party"]["task_message"]


# ---- AUD-08: note actions ---------------------------------------------------------------------------------------------


def _note_action_ir(base: PolicyIR, *, outcome: OutcomeRef | None) -> PolicyIR:
    """The dispute intent written with an ``add_note`` action instead of ``note_reason`` (an extraction variant)."""
    old = base.intent("dispute", verified=True)
    assert old is not None
    new = old.model_copy(update={"note_reason": None, "outcome": outcome,
                                 "actions": [ToolAction(tool="add_note", params={"reason": "dispute"})]})
    return with_intents(base, new, drop=("dispute",))


def test_a_single_product_note_action_is_written_by_the_engine_before_the_end(api: ToolApi) -> None:
    ir = _note_action_ir(IR_A, outcome=OutcomeRef(outcome="no_promise", reason="dispute"))
    workflow = build(ir, LEDGER_A, api)
    nodes = workflow["nodes"]
    assert "note_dispute" in targets(workflow, "negotiate")
    assert "end_dispute" not in targets(workflow, "negotiate")
    note = nodes["note_dispute"]
    assert note["node_type"] == "api_request" and note["passthrough_default_transition"] == "end_dispute"
    body = note["pre_actions"][0]["function"]["parameters"]["properties"]["body"]["properties"]
    assert body["reason"]["default"] == "dispute" and body["product_id"]["default"] == "{{product_ids}}"
    assert "/add_note" in json.dumps(note["pre_actions"][0])
    assert decide(workflow, end="end_dispute", identity_verified="yes", product_ids="p1")[:2] == ("no_promise", "dispute")
    report = lint_workflow(workflow, api, set(ir.call_variable_names()))
    assert report.ok, report.format()


def test_the_note_action_is_not_written_before_verification(api: ToolApi) -> None:
    """An intent in force in every stage writes its note only after verification (no product is known before)."""
    ir = _note_action_ir(IR_A, outcome=OutcomeRef(outcome="no_promise", reason="dispute"))
    for intent in ir.intents:
        if intent.key == "dispute":
            intent.scope = "any"
    workflow = build(ir, LEDGER_A, api)
    assert "end_dispute" in targets(workflow, "opening") and "note_dispute" not in targets(workflow, "opening")
    assert "note_dispute" in targets(workflow, "negotiate")


def test_a_multi_product_note_action_is_a_note_reason(api: ToolApi) -> None:
    ir = _note_action_ir(IR_AB, outcome=None)
    dispute = ir.intent("dispute", verified=True)
    assert dispute is not None
    info = naming.intent_exit(ir, dispute)
    assert (info.kind, info.reason) == ("note", "dispute")
    task = build(ir, LEDGER_AB, api)["nodes"]["negotiate"]["task_message"]
    assert 'add_note reason "dispute"' in task


def test_the_public_intents_compile_without_note_nodes(api: ToolApi) -> None:
    for ir, ledger in ((IR_A, LEDGER_A), (IR_AB, LEDGER_AB)):
        assert not [n for n in build(ir, ledger, api)["nodes"] if n.startswith(("note_", "callback_ok_"))]


# ---- AUD-08: callback actions -----------------------------------------------------------------------------------------


def _callback_intent(scope: str, outcome: OutcomeRef | None) -> IntentRule:
    return IntentRule(key="callback_request", description="Asks to be called back at another time", scope=scope,  # type: ignore[arg-type]
                      response="Offer to call back.", outcome=outcome, rule_ids=["X-2"],
                      actions=[ToolAction(tool="schedule_callback")])


def test_an_after_verification_callback_intent_books_and_closes_on_its_own_row(api: ToolApi) -> None:
    ir = with_intents(IR_AB, _callback_intent("after_verification", OutcomeRef(outcome="no_promise",
                                                                                reason="callback_requested")))
    workflow = build(ir, LEDGER_AB, api)
    negotiate = workflow["nodes"]["negotiate"]
    assert "schedule_callback" in tool_names(negotiate)
    route = next(t for t in negotiate["transitions"] if t["target_node"] == "callback_ok_negotiate")
    # AUD-07: only a booking the bank acknowledged routes to the confirmation
    assert route["condition"]["conditions"] == [{"variable": "callback_acked", "operator": "equals", "operand": "yes"}]
    ok = workflow["nodes"]["callback_ok_negotiate"]
    assert ok["passthrough_default_transition"] == "end_callback_request"
    line = bullet(negotiate["task_message"], "Asks to be called back")
    assert "schedule_callback" in line and "transition_to_end_callback_request" in line
    # booked: the intent's own row wins over the third-party callback row
    assert decide(workflow, end="end_callback_request", identity_verified="yes",
                  schedule_callback_success_count=1)[:2] == ("no_promise", "callback_requested")
    # no time agreed: not a booked callback (verified: the persuasion stop outcome)
    assert decide(workflow, end="end_callback_request", identity_verified="yes")[:2] == ("no_promise", "refused")
    # the third-party callback is unchanged
    assert decide(workflow, end="end_callback_booked", schedule_callback_success_count=1)[:2] == (
        "not_reached", "callback")
    report = lint_workflow(workflow, api, set(ir.call_variable_names()))
    assert report.ok, report.format()


def test_a_before_verification_callback_intent_books_in_the_opening(api: ToolApi) -> None:
    ir = with_intents(IR_A, _callback_intent("before_verification", None))
    workflow = build(ir, LEDGER_A, api)
    opening = workflow["nodes"]["opening"]
    assert tool_names(opening) == ["schedule_callback"]
    # AUD-29: the guard recovery of a blocked slot comes first, then the acknowledged booking
    assert [t["target_node"] for t in opening["transitions"][:2]] == ["callback_fix_opening", "callback_ok_opening"]
    assert workflow["nodes"]["callback_fix_say_opening"]["passthrough_default_transition"] == "opening"
    assert workflow["nodes"]["callback_ok_opening"]["passthrough_default_transition"] == "end_callback_booked"
    assert "schedule_callback" in tool_names(workflow["nodes"]["identity"])
    assert "schedule_callback" not in tool_names(workflow["nodes"]["negotiate"])
    assert decide(workflow, end="end_callback_booked", schedule_callback_success_count=1)[:2] == (
        "not_reached", "callback")
    assert decide(workflow, end="end_callback_booked")[:2] == ("not_reached", "no_callback")
    report = lint_workflow(workflow, api, set(ir.call_variable_names()))
    assert report.ok, report.format()


def test_a_callback_intent_needs_no_third_party_callback_rule(api: ToolApi) -> None:
    ir = with_intents(IR_A, _callback_intent("after_verification", OutcomeRef(outcome="no_promise", reason="refused")))
    ir.third_party.callback = None
    workflow = build(ir, LEDGER_A, api)
    assert "schedule_callback" in tool_names(workflow["nodes"]["negotiate"])
    assert "schedule_callback" not in tool_names(workflow["nodes"]["third_party"])


def test_an_action_no_mechanism_places_is_reported(api: ToolApi) -> None:
    intent = IntentRule(key="asks_statement", description="Asks for a statement", scope="after_verification",
                        actions=[ToolAction(tool="send_statement")], rule_ids=["X-3"])
    compiled = compile_workflow(with_intents(IR_A, intent), api, LEDGER_A)
    assert any("'send_statement'" in note for note in compiled.notes), compiled.notes
    assert compile_workflow(IR_A, api, LEDGER_A).notes == []


# ---- AUD-35: the identity decline limit follows the policy; an answer resets it ---------------------------------------


def _decline_line(workflow: dict[str, Any], profile: str) -> str:
    task = workflow["nodes"]["identity" if profile == "staged" else "call"]["task_message"]
    return next(line for line in task.splitlines()
                if line.startswith(("- If the customer does not want to answer", "- If the customer refuses to answer")))


@pytest.mark.parametrize("profile", ["staged", "lean"])
def test_the_identity_decline_limit_follows_decline_max(profile: str, api: ToolApi) -> None:
    lines = {}
    for limit in (1, 2, 3):
        ir = IR_A.model_copy(deep=True)
        ir.identity.decline_max = limit
        lines[limit] = _decline_line(build(ir, LEDGER_A, api, profile), profile)
    assert "The second refusal or such question in a row" in lines[2]
    assert "The third refusal or such question in a row" in lines[3] and "second" not in lines[3]
    assert "ask the same question again. The" not in lines[1] and "in a row" not in lines[1]
    assert len(set(lines.values())) == 3


@pytest.mark.parametrize("profile", ["staged", "lean"])
def test_an_answer_resets_the_identity_decline_count(profile: str, api: ToolApi) -> None:
    line = _decline_line(build(IR_A, LEDGER_A, api, profile), profile)
    assert "with no answer in between" in line and "starts this count again" in line
    assert 'a short question back that the customer follows with an answer' in line
    assert "These questions protect your account." in line


# ---- AUD-45: a third party who fetches the customer hands back to the identity step ------------------------------------


@pytest.mark.parametrize(("ir", "ledger"), [(IR_A, LEDGER_A), (IR_AB, LEDGER_AB)])
def test_third_party_can_hand_back_to_identity_when_the_customer_comes_to_the_phone(
    ir: PolicyIR, ledger: RuleLedger, api: ToolApi
) -> None:
    from builder.guardrails.output_guard import check_workflow

    workflow = build(ir, ledger, api)
    third = workflow["nodes"]["third_party"]
    handover = [t for t in third["transitions"] if t["target_node"] == "identity"]
    assert len(handover) == 1 and handover[0]["is_tool_call"]
    # the guard recovery of a blocked slot comes first (AUD-29), then the booking route
    assert [t["target_node"] for t in third["transitions"][:2]] == ["callback_fix_third_party", "callback_done"]
    question = workflow["nodes"]["opening"]["on_enter"]["text"].rsplit(". ", 1)[-1]
    task = third["task_message"]
    assert "transition_to_identity" in task and question in task
    assert "not the customer coming to the phone" in task
    assert "may not" in task or "Say nothing beyond your opening sentence" in task  # the relative still learns nothing
    assert not check_workflow(workflow, api, [], ir).blocked


def test_the_lean_call_node_covers_the_handover(api: ToolApi) -> None:
    task = build(IR_A, LEDGER_A, api, "lean")["nodes"]["call"]["task_message"]
    third = lean_section(task)
    assert "go on with the security questions (see IDENTITY below)" in third


# ---- AUD-34: the several-product switches of the IR reach the negotiation prompt ---------------------------------------

B1_DEFAULT = 'B1. You already stated every late product, one sentence each, and asked: "Can you pay all of them today?"'
B2_DEFAULT = ("B2. If not all today, go through the products one by one in the listed order. Do not ask about today again "
              "for each product. Start each product at attempt 1. Persuasion attempts are counted per product, with each "
              "product's own deadline. Even if the customer already said they will not pay a product, still make every "
              "scripted attempt for that product, one at a time, before you note it as refused.")


def _b_lines(ir: PolicyIR, ledger: RuleLedger, api: ToolApi, profile: str) -> tuple[str, str]:
    task = build(ir, ledger, api, profile)["nodes"]["negotiate"]["task_message"]
    lines = task.splitlines()
    return (next(x for x in lines if x.startswith("B1.")), next(x for x in lines if x.startswith("B2.")))


@pytest.mark.parametrize("profile", ["staged", "lean"])
def test_default_multi_product_switches_keep_the_validated_wording(profile: str, api: ToolApi) -> None:
    assert _b_lines(IR_AB, LEDGER_AB, api, profile) == (B1_DEFAULT, B2_DEFAULT)


@pytest.mark.parametrize("profile", ["staged", "lean"])
def test_attempts_counted_for_the_whole_call(profile: str, api: ToolApi) -> None:
    ir = IR_AB.model_copy(deep=True)
    ir.multi_product.per_product_attempts = False
    _, b2 = _b_lines(ir, LEDGER_AB, api, profile)
    assert "counted per product" not in b2 and "Start each product at attempt 1" not in b2
    assert "counted for the whole call, across all products" in b2


@pytest.mark.parametrize("profile", ["staged", "lean"])
def test_a_custom_product_order_keeps_the_brief_rule(profile: str, api: ToolApi) -> None:
    ir = IR_AB.model_copy(deep=True)
    ir.multi_product.order = "custom"
    rule = RuleEntry(id="X-9", source="variant.md", quote="Start with the product that is most days late.",
                     category="multi_product", statement="Start with the most overdue product.",
                     scope=["negotiation"])  # type: ignore[arg-type]
    ir.multi_product.rule_ids = ["X-9"]
    _, b2 = _b_lines(ir, with_rules(LEDGER_AB, rule), api, profile)
    assert '"Start with the product that is most days late." [X-9]' in b2
    assert "in the listed order" not in b2


# ---- AUD-28: a finishing move sent together with bank tools is recovered ------------------------------------------------


def _holds(condition: dict[str, Any], state: dict[str, Any]) -> bool:
    """A small evaluator of the variable conditions the compiler writes (or / and / equals / contains / greater_than)."""
    if "logic" in condition:
        parts = [_holds(c, state) for c in condition["conditions"]]
        return any(parts) if condition["logic"] == "or" else all(parts)
    value = state.get(condition["variable"])
    if condition["operator"] == "equals":
        return str(value) == str(condition["operand"])
    if condition["operator"] == "contains":
        return value is not None and str(condition["operand"]) in str(value)
    if condition["operator"] == "greater_than":
        try:
            return float(value) > float(condition["operand"])  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return False
    raise AssertionError(condition)


def _first_variable_route(node: dict[str, Any], state: dict[str, Any]) -> str | None:
    for transition in node["transitions"]:
        condition = transition.get("condition") or {}
        if transition.get("is_tool_call") or condition.get("operator") in ("matches_intent", "always"):
            continue
        if _holds(condition, state):
            return transition["target_node"]
    return None


@pytest.mark.parametrize(("ir", "ledger"), [(IR_A, LEDGER_A), (IR_AB, LEDGER_AB)])
def test_every_tool_exit_of_a_node_with_bank_tools_has_a_rescue_route(
    ir: PolicyIR, ledger: RuleLedger, api: ToolApi
) -> None:
    workflow = build(ir, ledger, api)
    nodes = workflow["nodes"]
    snippets = extract_code_snippets(workflow)
    for stage in ("identity", "third_party", "negotiate"):
        node = nodes[stage]
        exits = [t["target_node"] for t in node["transitions"] if t.get("is_tool_call")]
        rescue = next(t for t in node["transitions"] if t["target_node"] == f"rescue_{stage}")
        kinds = [("intent" if (t.get("condition") or {}).get("operator") == "matches_intent" else
                  "tool" if t.get("is_tool_call") else "var") for t in node["transitions"]]
        at = node["transitions"].index(rescue)
        assert set(kinds[:at]) <= {"var"} and kinds[at + 1] in ("intent", "tool"), (stage, kinds)
        for target in exits:
            key = f"transition_to_{target}_response"
            assert {"variable": key, "operator": "contains", "operand": "Transitioning"} in rescue["condition"]["conditions"]
            out = run_snippet(snippets[f"rescue_{stage}"], {key: f"Transitioning to {target}"})
            assert out["rescue_target"] == target and out[key] == "handled"
            hop = _first_variable_route(nodes[f"rescue_{stage}"], out)
            if out["rescue_ending"] == "yes":
                assert hop == f"rescue_{stage}_bye"
                assert nodes[hop]["passthrough_default_transition"] == f"rescue_{stage}_go"
                hop = _first_variable_route(nodes[f"rescue_{stage}_go"], out)
            assert hop == target, (stage, target)
        targets = [t["target_node"] for t in node["transitions"]]
        assert len(targets) == len(set(targets))
    report = lint_workflow(workflow, api, set(ir.call_variable_names()))
    assert report.ok, report.format()


def test_the_dropped_multi_product_close_is_recovered(api: ToolApi) -> None:
    """The variable table of a recorded several-product call: two notes and the finishing move sent in one message."""
    workflow = build(IR_AB, LEDGER_AB, api)
    state = {"create_promise_success_count": 1, "product_count": "3", "promise_guard": "",
             "notes_log": "p2:refused;p3:refused;",
             "transition_to_end_promise_recorded_response": "Transitioning to end_promise_recorded"}
    assert _first_variable_route(workflow["nodes"]["negotiate"], state) == "rescue_negotiate"
    # an ordinary turn (nothing dropped) takes no route
    assert _first_variable_route(workflow["nodes"]["negotiate"], {**state,
                                 "transition_to_end_promise_recorded_response": "handled"}) is None
