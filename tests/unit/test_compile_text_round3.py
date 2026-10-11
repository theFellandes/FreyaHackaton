"""Round-3 text fixes: prescribed sentences, spoken placeholders, joint confirmation, amended rules, offer wording,
the promise-guard amount correction, concessions, the floor sentence, pre-verification leaks and the form of address.

Every test builds its variant from the generic fixtures in the test itself; nothing here is brief-specific in the compiler.
Audit ids: AUD-09, AUD-12, AUD-32, AUD-36, AUD-38, AUD-47, AUD-49, AUD-51, AUD-52 and the PUB-03 cut-off identity answer.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from builder.checks.code_sandbox import extract_code_snippets, run_hook, run_snippet
from builder.compiler import codegen, hooks
from builder.compiler.compile import compile_workflow
from builder.compiler.context import person_refs, uncovered_rules
from builder.compiler.phrasing import speakable, unspoken_phrases
from builder.compiler.prompts import render
from builder.ingest.tool_api import load_tool_api
from builder.policy.ir import (
    IntentRule,
    OutcomeRef,
    PolicyIR,
    ResidualRule,
    RuleEntry,
    RuleLedger,
    ToolAction,
)

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "fixtures" / "policy"
API = load_tool_api(REPO / "tools" / "openapi.json")
SINGLE = re.compile(r"(?<!\{)\{[A-Za-z_][A-Za-z0-9_]*\}(?!\})")


def _load(name: str) -> tuple[PolicyIR, RuleLedger]:
    ir = PolicyIR.model_validate(json.loads((FIXTURES / f"{name}_ir.json").read_text(encoding="utf-8")))
    ledger = RuleLedger.model_validate(json.loads((FIXTURES / f"{name}_ledger.json").read_text(encoding="utf-8")))
    return ir, ledger


IR_A, LEDGER_A = _load("brief_a")
IR_AB, LEDGER_AB = _load("brief_ab")


def _copy(ir: PolicyIR, ledger: RuleLedger) -> tuple[PolicyIR, RuleLedger]:
    return ir.model_copy(deep=True), ledger.model_copy(deep=True)


def _task(workflow: dict[str, Any], node: str) -> str:
    return workflow["system_prompt"] if node == "system_prompt" else workflow["nodes"][node]["task_message"]


def _run_account(workflow: dict[str, Any], products: list[dict[str, Any]], **variables: str) -> dict[str, Any]:
    snippets = extract_code_snippets(workflow)
    setup = next(code for key, code in snippets.items() if "setup" in key)
    facts = next(code for key, code in snippets.items() if "account_facts" in key)
    state = run_snippet(setup, {"now": "2026-10-14T11:00", "customer_id": "c1", **variables})
    return run_snippet(facts, {**state, "products": products})


CARD = {"product_id": "p1", "type": "credit_card", "past_due_amount": 420.0, "days_past_due": 6,
        "minimum_payment": 126.0, "card_last4": "4821"}


# ---- AUD-09: prescribed sentences reach the agent ---------------------------------------------------------------------

SENTENCE = "I will now pass you to a colleague."
WHERE = {
    ("staged", "negotiation"): "negotiate", ("staged", "global"): "system_prompt",
    ("staged", "third_party"): "third_party", ("staged", "identity"): "identity",
    ("lean", "negotiation"): "negotiate", ("lean", "global"): "system_prompt",
    ("lean", "third_party"): "call", ("lean", "identity"): "call",
}


@pytest.mark.parametrize(("profile", "scope"), sorted(WHERE))
def test_a_sentence_under_an_unknown_extra_key_is_placed_in_its_step(profile: str, scope: str) -> None:
    ir, ledger = _copy(IR_AB, LEDGER_AB)
    rule = RuleEntry(id="X-PH", source="memo.md", quote=f'Before transferring, say exactly: "{SENTENCE}"',
                     category="other", statement=f'Say "{SENTENCE}" before a transfer.', scope=[scope])
    ledger.rules.append(rule)
    ir.phrases.rule_ids.append("X-PH")
    ir.phrases.extra["transfer_sentence"] = SENTENCE
    compiled = compile_workflow(ir, API, ledger, profile=profile)
    node = WHERE[(profile, scope)]
    assert SENTENCE in _task(compiled.workflow, node)
    place = "system_prompt" if node == "system_prompt" else f"node:{node}/task_message"
    assert place in compiled.rule_map["X-PH"]
    assert any("said by no node" in note for note in compiled.notes)


@pytest.mark.parametrize("profile", ["staged", "lean"])
def test_an_unlinked_extra_sentence_is_still_placed(profile: str) -> None:
    ir, ledger = _copy(IR_A, LEDGER_A)
    ir.phrases.extra["legal_notice"] = "Any information you give is used to manage your file."
    compiled = compile_workflow(ir, API, ledger, profile=profile)
    assert 'Fixed sentence (legal notice): when this applies, say exactly "Any information you give is used to manage ' \
           'your file."' in compiled.workflow["system_prompt"]


@pytest.mark.parametrize("profile", ["staged", "lean"])
def test_the_paid_sentence_reaches_the_already_paid_bullet(profile: str) -> None:
    ir, ledger = _copy(IR_A, LEDGER_A)
    ir.phrases.paid_sentence = "A payment shows on the account after two working days."
    paid = next(i for i in ir.intents if i.key == "already_paid")
    paid.response = "Take no promise."
    negotiate = compile_workflow(ir, API, ledger, profile=profile).workflow["nodes"]["negotiate"]["task_message"]
    (line,) = [line for line in negotiate.splitlines() if line.startswith("- " + paid.description.rstrip("."))]
    assert 'Say "A payment shows on the account after two working days." Take no promise.' in line


def test_the_deceased_sentence_reaches_a_declared_deceased_intent() -> None:
    ir, ledger = _copy(IR_A, LEDGER_A)
    ir.phrases.deceased_sentence = "Please accept our condolences."
    ref = ir.third_party.deceased or OutcomeRef(outcome="not_reached", reason="deceased")
    ir.intents = [i for i in ir.intents if i.key != "deceased"]
    ir.intents.append(IntentRule(key="deceased", description="The person says the customer has died",
                                 response="Ask nothing else.", outcome=ref, scope="before_verification"))
    workflow = compile_workflow(ir, API, ledger).workflow
    for node in ("opening", "third_party"):
        assert 'Say "Please accept our condolences." Ask nothing else.' in workflow["nodes"][node]["task_message"]


@pytest.mark.parametrize("name", ["brief_a", "brief_ab"])
def test_no_prescribed_sentence_of_the_fixtures_is_missing(name: str) -> None:
    ir, ledger = _load(name)
    compiled = compile_workflow(ir, API, ledger)
    texts = [compiled.workflow["system_prompt"], *[json.dumps(n) for n in compiled.workflow["nodes"].values()]]
    assert unspoken_phrases(ir, ledger, texts) == []
    assert not [note for note in compiled.notes if "said by no node" in note]


def test_a_drifted_extra_key_is_read_as_the_key_it_names() -> None:
    ir, ledger = _copy(IR_A, LEDGER_A)
    ir.phrases.extra.pop("below_floor")
    ir.phrases.extra["below_floor_sentence"] = "That amount is below what I can accept."
    negotiate = compile_workflow(ir, API, ledger).workflow["nodes"]["negotiate"]["task_message"]
    assert 'Below the floor: say "That amount is below what I can accept." and offer the floor once' in negotiate


# ---- AUD-12: placeholders are never spoken ----------------------------------------------------------------------------

def _with_first_name(ir: PolicyIR) -> PolicyIR:
    ir = ir.model_copy(deep=True)
    ir.phrases.today_question = "Can you make this payment today, {first_name}?"
    ir.phrases.extra["disclosure_intro"] = "I am calling about your account, {first_name}."
    ir.phrases.robot_answer = "I am a digital assistant, {first_name}."
    ir.recording.success_sentence = "Thank you, {first_name}, it is noted."
    ir.identity.decline_explanation = "These questions protect you, {first_name}."
    ir.confirmation.question = "Do you confirm, {first_name}?"
    paid = next(i for i in ir.intents if i.key == "already_paid")
    paid.response = "Tell {first_name} that payments take a day to show."
    return ir


@pytest.mark.parametrize("profile", ["staged", "lean"])
def test_a_declared_variable_in_any_sentence_becomes_a_reference(profile: str) -> None:
    ir = _with_first_name(IR_A)
    workflow = compile_workflow(ir, API, LEDGER_A, profile=profile).workflow
    texts = [workflow["system_prompt"]]
    for node in workflow["nodes"].values():
        texts.append(node.get("task_message") or "")
        enter = node.get("on_enter") or {}
        texts.append(enter.get("text") or enter.get("prompt") or "")
    assert not [t for t in texts if "{first_name}" in SINGLE.findall(t)]
    assert any("{{first_name}}" in t for t in texts)


@pytest.mark.parametrize("profile", ["staged", "lean"])
def test_the_spoken_debt_statement_fills_call_variables(profile: str) -> None:
    workflow = compile_workflow(_with_first_name(IR_A), API, LEDGER_A, profile=profile).workflow
    state = _run_account(workflow, [dict(CARD)], first_name="Priya", last_name="Nair", title="Ms")
    statement = state["debt_statement"]
    assert "Priya" in statement and not SINGLE.search(statement) and "  " not in statement


def test_an_undeclared_name_in_a_spoken_line_is_left_out_with_a_note() -> None:
    ir = IR_A.model_copy(deep=True)
    ir.phrases.right_party_question = "Am I speaking with {salutation} {last_name}?"
    compiled = compile_workflow(ir, API, LEDGER_A)
    opening = compiled.workflow["nodes"]["opening"]["on_enter"]["text"]
    assert not SINGLE.search(opening) and opening.endswith("Am I speaking with {{last_name}}?")
    assert any("salutation" in note for note in compiled.notes)
    assert speakable("Hi {x}, {{y}} .", ["y"], spoken=True) == ("Hi, {{y}}.", ["{x}"])
    assert speakable("Say {floor_amount}.", [], spoken=False) == ("Say {floor_amount}.", [])


@pytest.mark.parametrize("missing", ["absent", "none", "blank"])
def test_a_product_without_its_naming_field_is_named_by_its_type(missing: str) -> None:
    product = dict(CARD)
    if missing == "absent":
        product.pop("card_last4")
    else:
        product["card_last4"] = None if missing == "none" else ""
    ir = IR_A.model_copy(deep=True)
    ir.disclosure.product_naming["credit_card"] = "credit card ending {card_last4}"
    workflow = compile_workflow(ir, API, LEDGER_A).workflow
    statement = _run_account(workflow, [product])["debt_statement"]
    assert "Your credit card has" in statement or "Your credit card " in statement
    assert "{" not in statement and "  " not in statement and "ending" not in statement


# ---- AUD-32: one joint read-back and one confirmation ----------------------------------------------------------------

def test_the_joint_confirmation_defers_every_per_product_read_back() -> None:
    assert IR_AB.confirmation.single_confirmation_for_all_products
    workflow = compile_workflow(IR_AB, API, LEDGER_AB).workflow
    negotiate = workflow["nodes"]["negotiate"]["task_message"]
    assert "8. Read back only as in B4: once, for all products together" in negotiate
    assert "read back all agreed promises together in ONE message" in negotiate
    assert "one call at a time, without asking again in between" in negotiate
    assert "continue with the next product." not in negotiate
    tool = workflow["nodes"]["negotiate"]["tools"][0]
    assert "without asking again" in tool["post_process_code"]
    assert "Then continue with the next product" not in tool["post_process_code"]
    assert "joint read-back" in tool["function"]["description"]


def test_a_single_product_brief_keeps_the_per_promise_read_back() -> None:
    workflow = compile_workflow(IR_A, API, LEDGER_A).workflow
    negotiate = workflow["nodes"]["negotiate"]["task_message"]
    assert '8. Read back the product, the amount and the date and ask exactly "Do you confirm?".' in negotiate
    assert "Then continue with the next product" in workflow["nodes"]["negotiate"]["tools"][0]["post_process_code"]


def test_several_products_with_separate_confirmations_keep_the_per_product_wording() -> None:
    ir = IR_AB.model_copy(deep=True)
    ir.confirmation.single_confirmation_for_all_products = False
    negotiate = compile_workflow(ir, API, LEDGER_AB).workflow["nodes"]["negotiate"]["task_message"]
    assert 'B4. Read each promise back and ask "Do you confirm?" before each create_promise call.' in negotiate
    assert "8. Read back the product, the amount and the date" in negotiate
    assert "11. After a promise is recorded and you said the sentence above: continue with the next product. Only" \
           in negotiate


# ---- AUD-36: a narrowed rule is printed with its amendment -----------------------------------------------------------

def _narrowed() -> tuple[PolicyIR, RuleLedger]:
    ir, ledger = _copy(IR_AB, LEDGER_AB)
    old = ledger.get("A-NONOTE")
    assert old is not None
    old.superseded_by = None
    old.narrowed_by = ["B-8b"]
    for rule in ledger.rules:
        rule.supersedes = [x for x in rule.supersedes if x != "A-NONOTE"]
    return ir, ledger


def test_a_narrowed_rule_carries_its_amendment() -> None:
    ir, ledger = _narrowed()
    rules = dict(uncovered_rules(ir, ledger, ["global"]))
    text = rules["A-NONOTE"]
    assert text.startswith("Brief A never uses `add_note`. Amended by [B-8b] For each product without a promise")
    assert text.endswith("Where they differ, the amendment applies.")
    plain = dict(uncovered_rules(IR_AB, LEDGER_AB, ["global"]))
    assert rules["A-33"] == plain["A-33"]


@pytest.mark.parametrize("profile", ["staged", "lean"])
def test_the_system_prompt_never_states_the_narrowed_rule_alone(profile: str) -> None:
    ir, ledger = _narrowed()
    system = compile_workflow(ir, API, ledger, profile=profile).workflow["system_prompt"]
    lines = [line for line in system.splitlines() if "[A-NONOTE]" in line]
    assert lines and all("B-8b" in line for line in lines)


def test_an_unknown_or_superseded_amendment_is_ignored() -> None:
    ir, ledger = _narrowed()
    old = ledger.get("A-NONOTE")
    assert old is not None
    old.narrowed_by = ["NOPE-1", "A-NONOTE"]
    assert dict(uncovered_rules(ir, ledger, ["global"]))["A-NONOTE"] == "Brief A never uses `add_note`."


# ---- AUD-38: the amount correction proposes an amount ----------------------------------------------------------------

OPEN_QUESTION = re.compile(r"\b(which|what)\s+amount\b|\bhow\s+much\b", re.IGNORECASE)


def _guard(**kwargs: Any) -> str:
    return hooks.join_hooks(hooks.pre_hook_coerce({"amount": "money"}),
                            hooks.pre_hook_promise_guard("product_id", "amount", "date", enabled_dates=False,
                                                         currency="dollars", **kwargs))


@pytest.mark.parametrize(("amount", "proposed"), [(5, "10.00"), (80, "50.00")])
def test_the_amount_correction_never_asks_how_much(amount: int, proposed: str) -> None:
    facts = {"product_ids": "p1", "p1_floor": "10.00", "p1_past_due": "50.00", "p1_name": "card"}
    out = run_hook(_guard(), vars=dict(facts), config={"url": "u", "body": {"product_id": "p1", "amount": amount,
                                                                            "date": "2026-10-15"}})
    say = out["vars"]["promise_guard_say"]
    assert out["config"]["url"] == "" and not OPEN_QUESTION.search(say)
    assert say.endswith(f"Could you pay {proposed} dollars?")


def test_a_brief_that_rejects_below_the_floor_gets_no_question() -> None:
    facts = {"product_ids": "p1", "p1_floor": "10.00", "p1_past_due": "50.00", "p1_name": "card"}
    out = run_hook(_guard(below_floor="reject"), vars=dict(facts),
                   config={"url": "u", "body": {"product_id": "p1", "amount": 5, "date": "2026-10-15"}})
    assert out["vars"]["promise_guard_say"] == "I am sorry, I cannot record an amount below 10.00 dollars for your card."


# ---- AUD-47: the restructuring line follows the brief's concession rule ----------------------------------------------

def _negotiate(ir: PolicyIR, ledger: RuleLedger) -> str:
    return compile_workflow(ir, API, ledger).workflow["nodes"]["negotiate"]["task_message"]


def _restructuring_line(text: str) -> str:
    (line,) = [line for line in text.splitlines() if "new payment plan or lower instalments" in line]
    return line


def _without_concession_rule() -> tuple[PolicyIR, RuleLedger]:
    ir, ledger = _copy(IR_A, LEDGER_A)
    ir.residual_rules = [r for r in ir.residual_rules if r.rule_id != "A-33"]
    ledger.rules = [r for r in ledger.rules if not re.search(r"discount|waiv", f"{r.quote} {r.statement}", re.I)]
    return ir, ledger


def test_a_brief_that_forbids_concessions_keeps_the_refusal() -> None:
    line = _restructuring_line(_negotiate(IR_A, LEDGER_A))
    assert "A request for a discount or a waiver is not a request for this: say you cannot offer one." in line


def test_a_brief_with_a_waiver_queue_does_not_refuse_waivers() -> None:
    ir, ledger = _without_concession_rule()
    ir.intents.append(IntentRule(
        key="fee_waiver", description="The customer asks for a late-fee waiver", scope="after_verification",
        response="Say the fees team can review a one-time waiver.",
        actions=[ToolAction(tool="transfer_to_agent", params={"queue": "fees"})],
        outcome=OutcomeRef(outcome="transferred", reason="fees"), rule_ids=["A-41a"]))
    text = _negotiate(ir, ledger)
    assert "cannot offer one" not in text
    assert "The customer asks for a late-fee waiver: Say the fees team can review a one-time waiver." in text
    assert "discount or a waiver" not in _restructuring_line(text)


def test_a_brief_silent_on_concessions_only_keeps_the_routing_hint() -> None:
    ir, ledger = _without_concession_rule()
    line = _restructuring_line(_negotiate(ir, ledger))
    assert "cannot offer one" not in line
    assert "A request for a discount or a waiver is not by itself a request for this." in line


def test_a_restructuring_intent_that_covers_discounts_gets_no_extra_line() -> None:
    ir, ledger = _copy(IR_A, LEDGER_A)
    intent = next(i for i in ir.intents if i.key == "restructuring")
    intent.description = "Asks for a new payment plan or lower instalments, or a discount"
    text = _negotiate(ir, ledger)
    (line,) = [line for line in text.splitlines() if line.startswith("- " + intent.description)]
    assert "discount or a waiver" not in line


# ---- AUD-49: the floor sentence ---------------------------------------------------------------------------------------

def _floor_line(ir: PolicyIR, minimum: float = 30.0) -> str:
    product = {**CARD, "minimum_payment": minimum}
    facts = run_snippet(codegen.account_facts_snippet(ir),
                        {**run_snippet(codegen.setup_snippet(ir), {"now": "2026-10-14T11:00"}), "products": [product]})
    (line,) = [line for line in facts["account_facts"].splitlines() if "Floor offer" in line]
    return line.split("say exactly: ", 1)[1]


def test_a_floor_sentence_that_already_holds_the_note_says_it_once() -> None:
    ir = IR_A.model_copy(deep=True)
    ir.phrases.extra["floor_offer"] = "I can offer {floor_label} of {floor_amount}; the rest stays overdue."
    ir.amounts.floor_note = "The rest stays overdue."
    line = _floor_line(ir)
    assert len(re.findall("the rest stays overdue", line, re.IGNORECASE)) == 1
    assert "offer the minimum payment of 30.00" in line


def test_a_floor_sentence_without_the_note_gets_it_once() -> None:
    ir = IR_A.model_copy(deep=True)
    ir.phrases.extra["floor_offer"] = "{floor_label} is {floor_amount}."
    ir.amounts.floor_note = "The rest stays overdue."
    line = _floor_line(ir)
    assert line == '"The minimum payment is 30.00. The rest stays overdue."'


def test_a_floor_label_after_a_determiner_gets_no_article() -> None:
    ir = IR_A.model_copy(deep=True)
    ir.phrases.extra["floor_offer"] = "Your {floor_label} is {floor_amount}."
    ir.amounts.floor_note = ""
    assert _floor_line(ir) == '"Your minimum payment is 30.00."'


# ---- AUD-51: nothing about the account before verification -----------------------------------------------------------

@pytest.mark.parametrize("profile", ["staged", "lean"])
@pytest.mark.parametrize("name", ["brief_a", "brief_ab"])
def test_the_opening_gives_a_relative_nothing_about_the_account(profile: str, name: str) -> None:
    ir, ledger = _load(name)
    workflow = compile_workflow(ir, API, ledger, profile=profile).workflow
    node = "opening" if profile == "staged" else "call"
    (line,) = [line for line in _task(workflow, node).splitlines() if line.startswith("- Someone else answered (a relative")]
    assert "do not mention any account" in line
    reply = re.search(r'say only "([^"]+)"', line)
    assert reply is not None and reply.group(1) == "I can only discuss this with {{title}} {{last_name}}."
    assert not any(term in reply.group(1).lower() for term in ir.forbidden_before_verification)
    assert not re.search(r"\d", reply.group(1))


@pytest.mark.parametrize("profile", ["staged", "lean"])
def test_a_global_rule_that_names_a_forbidden_word_moves_to_the_negotiation(profile: str) -> None:
    ir, ledger = _copy(IR_A, LEDGER_A)
    job = "Find the customer, state the debt and obtain a promise where possible."
    keep = "Never say debt before the identity is verified."
    ledger.rules += [RuleEntry(id="X-JOB", source="b.md", quote=job, category="other", statement=job, scope=["global"]),
                     RuleEntry(id="X-KEEP", source="b.md", quote=keep, category="other", statement=keep,
                               scope=["global"])]
    compiled = compile_workflow(ir, API, ledger, profile=profile)
    system = compiled.workflow["system_prompt"]
    assert "[X-JOB]" not in system and job in compiled.workflow["nodes"]["negotiate"]["task_message"]
    assert "node:negotiate/task_message" in compiled.rule_map["X-JOB"]
    assert "system_prompt" not in compiled.rule_map["X-JOB"]
    assert f"[X-KEEP] {keep}" in system and "[A-33]" in system


def test_a_deferred_residual_rule_is_mapped_to_the_negotiation() -> None:
    ir, ledger = _copy(IR_A, LEDGER_A)
    ir.residual_rules.append(ResidualRule(rule_id="X-RES", text="Obtain a payment promise where possible.",
                                          scope=["global"]))
    ledger.rules.append(RuleEntry(id="X-RES", source="b.md", quote="Obtain a payment promise where possible.",
                                  category="other", statement="Obtain a promise.", scope=["global"]))
    compiled = compile_workflow(ir, API, ledger)
    assert compiled.rule_map["X-RES"] == ["node:negotiate/task_message"]


# ---- AUD-52: the form of address names a person -----------------------------------------------------------------------

def _refs(question: str, variables: list[str]) -> tuple[str, str]:
    ir = IR_A.model_copy(deep=True)
    ir.phrases.right_party_question = question
    ir.call_variables = [v.model_copy(update={"name": name}) for v, name in
                         zip([ir.call_variables[0]] * len(variables), variables, strict=True)]
    return person_refs(ir)


def test_the_public_form_of_address_is_unchanged() -> None:
    assert person_refs(IR_A) == ("{{title}} {{last_name}}", "{{first_name}} {{last_name}}")


@pytest.mark.parametrize(("question", "variables", "expected"), [
    ("Am I speaking with {contact_name}, a director of {company_name}?", ["contact_name", "company_name"],
     ("{{contact_name}}", "{{contact_name}}")),
    ("Am I speaking with a director of {company_name}?", ["company_name"], ("the customer", "the customer")),
    ("Am I speaking with {title} {last_name} about the account ending {account_last4}?",
     ["title", "last_name", "account_last4"], ("{{title}} {{last_name}}", "{{title}} {{last_name}}")),
    ("", ["customer_name"], ("{{customer_name}}", "{{customer_name}}")),
    ("Spreche ich mit {nome}?", ["nome"], ("{{nome}}", "{{nome}}")),
])
def test_only_name_like_placeholders_form_the_address(question: str, variables: list[str],
                                                      expected: tuple[str, str]) -> None:
    assert _refs(question, variables) == expected


# ---- PUB-03: a complete identity answer inside a cut-off message is checked at once ----------------------------------

@pytest.mark.parametrize("profile", ["staged", "lean"])
def test_a_cut_off_message_with_a_complete_answer_is_checked(profile: str) -> None:
    workflow = compile_workflow(IR_A, API, LEDGER_A, profile=profile).workflow
    node = "identity" if profile == "staged" else "call"
    (line,) = [line for line in _task(workflow, node).splitlines() if "is checked FIRST" in line]
    assert "cut off or trails off after the answer" in line and 'never reply to it only with "I understand, go on."' \
        in line
    prompt = workflow["system_prompt"]
    # PUB-03 live (11 Oct): with the acknowledgement rule first, the model answered a cut-off complete date with
    # "I understand, go on."; the act-on-it rule must come first and the acknowledgement only as the fallback
    assert "act on it right away (call the tool the step needs), also when the message is cut off" in prompt
    assert prompt.index("act on it right away") < prompt.index('"I understand, go on."')


def test_the_identity_template_keeps_the_rule_in_both_decline_modes() -> None:
    for decline_max in (1, 2):
        text = render("identity", n_questions="one", one_at_a_time=True,
                      questions=[{"ask": "Q?", "field": "f", "how": "as said"}], verify_tool="verify", max_wrong="Two",
                      complete="f: the whole answer", partial="part", spoken="", hints=[], explanation="E.",
                      decline_max=decline_max, decline_nth="second", counting="c", declined_action="stop", exits=[],
                      rules=[], extra="")
        assert "cut off or trails off after the answer" in text
