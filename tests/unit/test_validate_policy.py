"""Validation of an extracted policy IR against the tool API and the rule ledger (``builder.understand.validate``)
and the deterministic fixes (``builder.understand.fixes``)."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from builder.ingest.tool_api import ToolApi
from builder.policy.ir import PolicyIR, RuleEntry, RuleLedger
from builder.understand.fixes import apply_safe_fixes
from builder.understand.validate import (
    PolicyIssue,
    errors_of,
    format_issues,
    has_errors,
    phrase_checklist,
    phrase_requirements,
    tool_vocabulary,
    validate_policy,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "policy"


def load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def ir_a() -> dict[str, Any]:
    return load("brief_a_ir.json")


@pytest.fixture
def ledger_a() -> RuleLedger:
    return RuleLedger.model_validate(load("brief_a_ledger.json"))


def codes(issues: list[PolicyIssue], severity: str | None = None) -> list[str]:
    return [i.code for i in issues if severity in (None, i.severity)]


def check(data: dict[str, Any], ledger: RuleLedger, api: ToolApi) -> list[PolicyIssue]:
    return validate_policy(PolicyIR.model_validate(data), ledger, api)


# --- the kit fixtures are valid -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["brief_a", "brief_ab"])
def test_kit_fixture_irs_have_no_issues(name: str, kit_tool_api: ToolApi) -> None:
    ir = PolicyIR.model_validate(load(f"{name}_ir.json"))
    ledger = RuleLedger.model_validate(load(f"{name}_ledger.json"))
    assert validate_policy(ir, ledger, kit_tool_api) == []


def test_vocabulary_is_read_from_the_tool_api(kit_tool_api: ToolApi) -> None:
    vocab = tool_vocabulary(kit_tool_api)
    assert vocab.identity_fields == ["date_of_birth", "phone_last4"]
    assert vocab.outcomes and "not_reached" in vocab.outcomes
    assert vocab.reason_exempt_outcomes == {"promise", "paid"}
    assert vocab.queues == ["restructuring", "general", "support"]
    assert vocab.note_reasons and "service_error" in vocab.note_reasons
    assert vocab.product_types == ["credit_card", "personal_loan", "car_loan"]
    assert {"past_due", "days_late", "minimum_payment", "instalment_amount", "card_last4"} <= vocab.product_fields
    assert "close_call outcome values" in vocab.describe()


def test_a_tool_api_without_the_tools_skips_vocabulary_checks(ir_a: dict[str, Any], ledger_a: RuleLedger,
                                                              kit_tool_api: ToolApi) -> None:
    kit_tool_api.tools.clear()
    ir_a["identity"]["questions"][0]["field"] = "anything"
    assert codes(check(ir_a, ledger_a, kit_tool_api), "error") == []


# --- vocabulary errors --------------------------------------------------------------------------------------------------


def test_identity_field_must_be_a_verify_identity_value(ir_a, ledger_a, kit_tool_api) -> None:
    ir_a["identity"]["questions"][1]["field"] = "mothers_maiden_name"
    issues = check(ir_a, ledger_a, kit_tool_api)
    bad = [i for i in issues if i.code == "IDENTITY_FIELD"]
    assert len(bad) == 1 and "date_of_birth" in bad[0].message and bad[0].path == "identity.questions[1].field"
    assert bad[0].rule_ids and has_errors(issues)


def test_outcome_and_reason_must_be_close_call_values(ir_a, ledger_a, kit_tool_api) -> None:
    ir_a["persuasion"]["stop_outcome"] = {"outcome": "declined", "reason": "refused"}
    ir_a["recording"]["failure_outcome"] = {"outcome": "no_promise", "reason": "technical"}
    # the API has no reason enum, so only the outcome is rejected; the reason is checked against the table
    issues = check(ir_a, ledger_a, kit_tool_api)
    assert "OUTCOME_UNKNOWN" in codes(issues, "error")
    assert any(i.code == "OUTCOME_NOT_IN_TABLE" and i.path == "recording.failure_outcome" for i in issues)


def test_reason_is_required_except_for_the_outcomes_the_api_names(ir_a, ledger_a, kit_tool_api) -> None:
    ir_a["third_party"]["wrong_number"] = {"outcome": "not_reached"}
    ir_a["intents"][0]["outcome"] = {"outcome": "paid", "reason": "because"}
    issues = check(ir_a, ledger_a, kit_tool_api)
    assert any(i.code == "REASON_MISSING" and i.path == "third_party.wrong_number" for i in issues)
    unexpected = [i for i in issues if i.code == "REASON_UNEXPECTED"]
    assert unexpected and unexpected[0].severity == "warning"


def test_transfer_after_promise_outcome_may_omit_the_reason(ir_a, ledger_a, kit_tool_api) -> None:
    assert ir_a["closing"]["transfer_after_promise_outcome"].get("reason") is None
    assert "REASON_MISSING" not in codes(check(ir_a, ledger_a, kit_tool_api))


def test_note_reason_queue_and_action_parameters(kit_tool_api: ToolApi) -> None:
    data = load("brief_ab_ir.json")
    ledger = RuleLedger.model_validate(load("brief_ab_ledger.json"))
    data["multi_product"]["note_reasons"].append("lost_interest")
    data["intents"][0]["note_reason"] = "bored"
    for intent in data["intents"]:
        for action in intent["actions"]:
            if action["tool"] == "transfer_to_agent":
                action["params"]["queue"] = "vip"
                break
    data["intents"][1]["actions"] = [{"tool": "launch_rocket", "params": {}}]
    data["closing"]["rows"][0]["situation"] = "transfer:vip"
    found = codes(check(data, ledger, kit_tool_api), "error")
    assert {"NOTE_REASON", "QUEUE_UNKNOWN", "ACTION_PARAM", "TOOL_UNKNOWN"} <= set(found)


def test_product_types_and_fields_must_exist(ir_a, ledger_a, kit_tool_api) -> None:
    ir_a["disclosure"]["product_naming"]["boat_loan"] = "boat {hull_number}"
    ir_a["amounts"]["floor_field_by_type"]["car_loan"] = "monthly_due"
    ir_a["disclosure"]["state_fields"].append("interest")
    issues = check(ir_a, ledger_a, kit_tool_api)
    assert codes(issues, "error").count("PRODUCT_FIELD") == 3  # floor field, state field, template placeholder
    assert "PRODUCT_TYPE" in codes(issues, "error")


def test_unknown_placeholder_in_an_opening_phrase_is_a_warning(ir_a, ledger_a, kit_tool_api) -> None:
    ir_a["phrases"]["right_party_question"] = "Am I speaking with {salutation} {last_name}?"
    issues = check(ir_a, ledger_a, kit_tool_api)
    assert [i.severity for i in issues if i.code == "PLACEHOLDER_UNKNOWN"] == ["warning"]


# --- structure ----------------------------------------------------------------------------------------------------------


def test_bands_overlap_is_an_error_and_a_gap_a_warning(ir_a, ledger_a, kit_tool_api) -> None:
    overlapping = copy.deepcopy(ir_a)
    overlapping["bands"][1]["days_late_min"] = 5
    assert "BANDS_OVERLAP" in codes(check(overlapping, ledger_a, kit_tool_api), "error")
    gap = copy.deepcopy(ir_a)
    gap["bands"][1]["days_late_min"] = 12
    issues = check(gap, ledger_a, kit_tool_api)
    assert "BANDS_GAP" in codes(issues, "warning") and "BANDS_OVERLAP" not in codes(issues)


def test_band_names_must_be_unique_and_known_to_the_persuasion_ladder(ir_a, ledger_a, kit_tool_api) -> None:
    ir_a["bands"][1]["name"] = ir_a["bands"][0]["name"]
    ir_a["persuasion"]["steps"][1]["bands"] = ["platinum"]
    found = codes(check(ir_a, ledger_a, kit_tool_api), "error")
    assert "BAND_NAME_DUPLICATE" in found and "BAND_NAME_UNKNOWN" in found


def test_no_bands_and_too_few_persuasion_steps(ir_a, ledger_a, kit_tool_api) -> None:
    short = copy.deepcopy(ir_a)
    short["bands"][0]["persuasion_attempts"] = 5
    assert "PERSUASION_STEPS_SHORT" in codes(check(short, ledger_a, kit_tool_api), "warning")
    empty = copy.deepcopy(ir_a)
    empty["bands"] = []
    assert "BANDS_EMPTY" in codes(check(empty, ledger_a, kit_tool_api), "error")


def test_implausible_holidays_are_flagged(ir_a, ledger_a, kit_tool_api) -> None:
    ir_a["calendar"]["holidays"].append("0026-12-25")
    ir_a["calendar"]["holidays"].append("2026-11-14")  # a Saturday: no effect
    issues = check(ir_a, ledger_a, kit_tool_api)
    assert "HOLIDAY_RANGE" in codes(issues, "error")
    assert "HOLIDAY_WEEKEND" in codes(issues, "warning")
    assert "HOLIDAY_SPREAD" in codes(issues, "warning")  # year 26 versus 2027


def test_identity_needs_questions_and_a_limit(ir_a, ledger_a, kit_tool_api) -> None:
    ir_a["identity"]["questions"] = []
    ir_a["identity"]["max_wrong_total"] = 0
    assert {"IDENTITY_NO_QUESTIONS", "IDENTITY_LIMIT"} <= set(codes(check(ir_a, ledger_a, kit_tool_api), "error"))


def test_outcome_table_is_required(ir_a, ledger_a, kit_tool_api) -> None:
    ir_a["closing"]["rows"] = []
    assert "CLOSING_EMPTY" in codes(check(ir_a, ledger_a, kit_tool_api), "error")


def test_unknown_precedence_key_is_a_warning(ir_a, ledger_a, kit_tool_api) -> None:
    ir_a["closing"]["precedence"].append("moon_phase")
    assert "PRECEDENCE_UNKNOWN" in codes(check(ir_a, ledger_a, kit_tool_api), "warning")


def test_duplicate_intents_are_a_warning(ir_a, ledger_a, kit_tool_api) -> None:
    ir_a["intents"].append(copy.deepcopy(ir_a["intents"][0]))
    assert "INTENT_DUPLICATE" in codes(check(ir_a, ledger_a, kit_tool_api), "warning")


# --- ledger coverage ----------------------------------------------------------------------------------------------------


def test_uncovered_active_rules_are_listed_in_one_warning(ir_a, ledger_a, kit_tool_api) -> None:
    ledger_a.rules.append(RuleEntry(id="A-NEW", source="brief-a.md", quote="Greet kindly.", category="conduct",
                                    statement="Greet kindly.", scope=["global"]))
    ledger_a.rules.append(RuleEntry(id="A-NEW2", source="brief-a.md", quote="Be brief.", category="conduct",
                                    statement="Be brief.", scope=["global"]))
    issues = check(ir_a, ledger_a, kit_tool_api)
    uncovered = [i for i in issues if i.code == "RULE_UNCOVERED"]
    assert len(uncovered) == 1 and uncovered[0].severity == "warning"
    assert uncovered[0].rule_ids == ["A-NEW", "A-NEW2"]


def test_a_residual_rule_covers_a_ledger_rule(ir_a, ledger_a, kit_tool_api) -> None:
    ledger_a.rules.append(RuleEntry(id="A-NEW", source="brief-a.md", quote="Greet kindly.", category="conduct",
                                    statement="Greet kindly.", scope=["global"]))
    ir_a["residual_rules"].append({"rule_id": "A-NEW", "text": "Greet kindly.", "scope": ["global"]})
    assert "RULE_UNCOVERED" not in codes(check(ir_a, ledger_a, kit_tool_api))


def test_superseded_rules_must_not_be_cited(kit_tool_api: ToolApi) -> None:
    data = load("brief_ab_ir.json")
    ledger = RuleLedger.model_validate(load("brief_ab_ledger.json"))
    old = next(r for r in ledger.rules if r.superseded_by)
    data["multi_product"]["rule_ids"].append(old.id)
    issues = check(data, ledger, kit_tool_api)
    bad = [i for i in issues if i.code == "RULE_SUPERSEDED_USED"]
    assert len(bad) == 1 and bad[0].severity == "error" and old.id in bad[0].rule_ids
    assert old.superseded_by in bad[0].message


def test_unknown_rule_ids_and_dangling_supersessions(ir_a, ledger_a, kit_tool_api) -> None:
    ir_a["calendar"]["rule_ids"].append("A-999")
    ledger_a.rules[0] = ledger_a.rules[0].model_copy(update={"supersedes": ["A-404"]})
    issues = check(ir_a, ledger_a, kit_tool_api)
    assert "RULE_ID_UNKNOWN" in codes(issues, "warning")
    assert "SUPERSESSION_DANGLING" in codes(issues, "warning")


def test_an_empty_ledger_is_an_error(kit_tool_api: ToolApi) -> None:
    assert codes(validate_policy(PolicyIR.example_minimal(), RuleLedger(rules=[]), kit_tool_api), "error").count(
        "LEDGER_EMPTY") == 1


def test_issue_helpers() -> None:
    issues = [
        PolicyIssue(code="B", message="warn", severity="warning"),
        PolicyIssue(code="A", message="boom", severity="error", rule_ids=["X-1"], path="identity"),
    ]
    assert errors_of(issues) == [issues[1]]
    text = format_issues(issues)
    assert text.splitlines()[0].startswith("- ERROR A at identity: boom [X-1]")


# --- safe fixes ---------------------------------------------------------------------------------------------------------


def test_safe_fixes_respell_vocabulary_and_catalog_keys(ir_a, ledger_a, kit_tool_api) -> None:
    ir_a["identity"]["questions"][0]["field"] = "Date_Of_Birth"
    ir_a["third_party"]["wrong_number"] = {"outcome": "Not_Reached", "reason": "wrong_number"}
    ir_a["intents"][0]["key"] = "Is_Robot"
    ir_a["closing"]["rows"][0]["situation"] = "Transfer:Restructuring"
    ir = PolicyIR.model_validate(ir_a)
    fixed, log = apply_safe_fixes(ir, ledger_a, kit_tool_api)
    assert fixed.identity.questions[0].field == "date_of_birth"
    assert fixed.third_party.wrong_number is not None and fixed.third_party.wrong_number.outcome == "not_reached"
    assert fixed.intents[0].key == "robot_question"
    assert fixed.closing.rows[0].situation == "transfer:restructuring"
    assert any("vocabulary" in line for line in log) and any("intent keys" in line for line in log)
    assert ir.identity.questions[0].field == "Date_Of_Birth"  # the input is not modified


def test_safe_fixes_clean_rule_ids_and_cover_every_active_rule(ir_a, ledger_a, kit_tool_api) -> None:
    ir_a["calendar"]["rule_ids"] += ["A-999", ir_a["calendar"]["rule_ids"][0]]
    removed = ir_a["recording"]["rule_ids"]
    ir_a["recording"]["rule_ids"] = []
    assert removed
    ir = PolicyIR.model_validate(ir_a)
    assert "RULE_UNCOVERED" in codes(validate_policy(ir, ledger_a, kit_tool_api), "warning") or any(
        rid not in ir.referenced_rule_ids() for rid in removed
    )
    fixed, log = apply_safe_fixes(ir, ledger_a, kit_tool_api)
    assert "A-999" not in fixed.calendar.rule_ids
    assert len(fixed.calendar.rule_ids) == len(set(fixed.calendar.rule_ids))
    assert set(removed) <= fixed.referenced_rule_ids()
    assert "RULE_UNCOVERED" not in codes(validate_policy(fixed, ledger_a, kit_tool_api))
    assert any("not in the ledger" in line for line in log)


def test_safe_fixes_keep_a_valid_ir_unchanged(ir_a, ledger_a, kit_tool_api) -> None:
    ir = PolicyIR.model_validate(ir_a)
    fixed, log = apply_safe_fixes(ir, ledger_a, kit_tool_api)
    assert log == []
    assert fixed.to_json_dict() == ir.to_json_dict()


# --- sentences the ledger prescribes (quoted or only described) -----------------------------------------------------------

DESCRIBED_FIELDS = ["opening_sentence", "robot_answer", "scam_answer", "identity_failed_sentence", "paid_sentence",
                    "goodbye_sentence"]


def described_rule(rule_id: str, category: str, statement: str, scope: tuple[str, ...] = ("global",)) -> RuleEntry:
    return RuleEntry(id=rule_id, source="brief.md", quote=statement, category=category, statement=statement,
                     scope=list(scope))


def missing_phrase_fields(issues: list[PolicyIssue]) -> list[str]:
    return [i.path.removeprefix("phrases.") for i in issues if i.code == "PHRASE_MISSING"]


def test_empty_described_phrases_are_errors_that_name_the_prescribing_rules(ir_a, ledger_a, kit_tool_api) -> None:
    for name in DESCRIBED_FIELDS:
        ir_a["phrases"][name] = ""
    issues = check(ir_a, ledger_a, kit_tool_api)
    missing = [i for i in issues if i.code == "PHRASE_MISSING"]
    assert sorted(missing_phrase_fields(issues)) == sorted(DESCRIBED_FIELDS)
    assert all(i.severity == "error" and i.rule_ids for i in missing) and has_errors(issues)
    by_field = {i.path.removeprefix("phrases."): i for i in missing}
    assert by_field["opening_sentence"].rule_ids == ["A-1a"]
    assert by_field["goodbye_sentence"].rule_ids == ["A-47"]
    assert by_field["paid_sentence"].rule_ids == ["A-39a"]
    assert "compose" in by_field["robot_answer"].message and "nothing else" in by_field["robot_answer"].message


@pytest.mark.parametrize("name", [*DESCRIBED_FIELDS, "right_party_question", "deceased_sentence"])
def test_each_prescribed_phrase_is_checked_on_its_own(name, ir_a, ledger_a, kit_tool_api) -> None:
    ir_a["phrases"][name] = ""
    assert missing_phrase_fields(check(ir_a, ledger_a, kit_tool_api)) == [name]
    ir_a["phrases"][name] = "   "  # blanks count as empty
    assert missing_phrase_fields(check(ir_a, ledger_a, kit_tool_api)) == [name]


def test_filled_phrases_raise_no_issue(ir_a, ledger_a, kit_tool_api) -> None:
    for name in DESCRIBED_FIELDS:
        ir_a["phrases"][name] = "A composed sentence."
    assert "PHRASE_MISSING" not in codes(check(ir_a, ledger_a, kit_tool_api))


def test_an_empty_phrase_is_fine_when_no_active_rule_prescribes_it(ir_a, ledger_a, kit_tool_api) -> None:
    for name in DESCRIBED_FIELDS:
        ir_a["phrases"][name] = ""
    unrelated = RuleLedger(rules=[
        described_rule("X-1", "conduct", "Be polite."),
        described_rule("X-2", "calendar", "Treat Monday to Friday as business days."),
        # a prescribing rule that a later one replaced is not active
        described_rule("X-3", "closing", "Thank the customer and say goodbye.").model_copy(update={"superseded_by": "X-1"}),
    ])
    assert "PHRASE_MISSING" not in codes(validate_policy(PolicyIR.model_validate(ir_a), unrelated, kit_tool_api))


def test_described_rules_in_any_category_are_recognised() -> None:
    ledger = RuleLedger(rules=[
        described_rule("D-1", "opening", "In the first sentence, say that you are a digital assistant calling from the "
                       "bank and that the call is recorded.", ("opening",)),
        described_rule("D-2", "opening", "If the customer asks whether you are a robot or an AI, say that you are the "
                       "bank's digital assistant and continue.", ("opening",)),
        described_rule("D-3", "opening", "When the customer asks whether this is a scam, say they can verify the call by "
                       "phoning customer service."),
        described_rule("D-4", "identity", "Tell the customer to use the mobile app or a branch after three wrong answers."),
        described_rule("D-5", "calendar", "For an already paid claim, say that payments can take one business day to "
                       "appear."),
        described_rule("D-6", "closing", "Thank the customer and say goodbye before closing."),
        described_rule("D-7", "right_party", "Ask whether you are speaking with the customer.", ("opening",)),
    ])
    found = {req.field: req.rule_ids for req in phrase_requirements(ledger)}
    assert found == {
        "opening_sentence": ("D-1",),
        "right_party_question": ("D-7",),
        "robot_answer": ("D-2",),
        "scam_answer": ("D-3",),
        "identity_failed_sentence": ("D-4",),
        "paid_sentence": ("D-5",),
        "goodbye_sentence": ("D-6",),
    }
    checklist = phrase_checklist(ledger)
    assert "- phrases.robot_answer:" in checklist and "[rules D-2]" in checklist


def test_rules_that_only_mention_a_topic_do_not_prescribe_a_sentence() -> None:
    ledger = RuleLedger(rules=[
        described_rule("N-1", "intent", "Do not take a promise when the customer says they already paid."),
        described_rule("N-2", "identity", "On a swapped day and month ask the customer to say the month as a word; that "
                       "retry is not a wrong answer."),
        described_rule("N-3", "recording", "After a second failure to record the promise say it could not be recorded."),
        described_rule("N-4", "identity", "Do not state the debt after three wrong answers."),
        described_rule("N-5", "opening", "Answer a question from the customer first."),
        described_rule("N-6", "third_party", "Say nothing beyond the opening sentence to someone else."),
        described_rule("N-7", "callback", "Ask when the customer can be reached."),
    ])
    assert phrase_requirements(ledger) == []
    assert phrase_checklist(ledger) == ""


def test_the_kit_ledgers_prescribe_phrases_their_fixtures_fill(kit_tool_api: ToolApi) -> None:
    for name in ("brief_a", "brief_ab"):
        ledger = RuleLedger.model_validate(load(f"{name}_ledger.json"))
        ir = PolicyIR.model_validate(load(f"{name}_ir.json"))
        prescribed = {req.field for req in phrase_requirements(ledger)}
        assert set(DESCRIBED_FIELDS) <= prescribed
        assert all(getattr(ir.phrases, field).strip() for field in prescribed)


# --- U-01: a persuasion ladder date the compiler cannot render ----------------------------------------------------------------


def test_a_customer_date_whose_text_names_a_date_rule_is_an_error(ir_a, ledger_a, kit_tool_api) -> None:
    """U-01: date "customer" renders as "on the day the customer names"; text that says "next business day" is a lie."""
    ir_a["persuasion"]["steps"][0]["date"] = "customer"
    ir_a["persuasion"]["steps"][0]["text"] = "Propose the full amount on the next business day after today."
    issues = check(ir_a, ledger_a, kit_tool_api)
    bad = [i for i in issues if i.code == "PERSUASION_DATE_KIND"]
    assert len(bad) == 1 and bad[0].severity == "error" and bad[0].path == "persuasion.steps[0].date"
    assert "next_business_day_after_today" in bad[0].message


def test_a_customer_date_with_a_neutral_text_is_fine(ir_a, ledger_a, kit_tool_api) -> None:
    ir_a["persuasion"]["steps"][1]["date"] = "customer"
    ir_a["persuasion"]["steps"][1]["text"] = "Ask the customer on which day they can pay the floor amount."
    assert "PERSUASION_DATE_KIND" not in codes(check(ir_a, ledger_a, kit_tool_api))


def test_a_custom_date_without_text_is_an_error(ir_a, ledger_a, kit_tool_api) -> None:
    ir_a["persuasion"]["steps"][1]["date"] = "custom"
    ir_a["persuasion"]["steps"][1]["text"] = ""
    issues = check(ir_a, ledger_a, kit_tool_api)
    assert any(i.code == "PERSUASION_DATE_TEXT" and i.path == "persuasion.steps[1]" for i in issues)


# --- U-02: team_phrase / product_naming ---------------------------------------------------------------------------------------


def test_an_empty_team_phrase_without_a_full_intro_sentence_is_an_error(ir_a, ledger_a, kit_tool_api) -> None:
    ir_a["phrases"]["extra"].pop("collections_intro")  # the fixture quotes the whole sentence under this key
    ir_a["disclosure"]["team_phrase"] = ""
    ir_a["bank_name"] = ""
    issues = check(ir_a, ledger_a, kit_tool_api)
    assert any(i.code == "TEAM_PHRASE_MISSING" and i.severity == "error" for i in issues)
    ir_a["phrases"]["extra"] = {"disclosure_intro": "This is the collections desk."}
    assert "TEAM_PHRASE_MISSING" not in codes(check(ir_a, ledger_a, kit_tool_api))


def test_a_team_phrase_that_is_a_whole_sentence_is_an_error(ir_a, ledger_a, kit_tool_api) -> None:
    ir_a["phrases"]["extra"].pop("collections_intro")
    ir_a["disclosure"]["team_phrase"] = "This is the collections team of the bank"
    issues = check(ir_a, ledger_a, kit_tool_api)
    assert any(i.code == "TEAM_PHRASE_SENTENCE" and i.severity == "error" for i in issues)


def test_product_naming_must_not_state_the_amount_or_the_days(ir_a, ledger_a, kit_tool_api) -> None:
    """U-02: "card ending 1234, with an overdue amount of 450.5 and 35 days overdue" became the product's name."""
    ir_a["disclosure"]["product_naming"]["credit_card"] = (
        "credit card ending {card_last4}, with an overdue amount of {past_due} and {days_late} days overdue"
    )
    issues = check(ir_a, ledger_a, kit_tool_api)
    bad = [i for i in issues if i.code == "PRODUCT_NAMING_STATEMENT"]
    assert len(bad) == 1 and bad[0].severity == "error"
    assert bad[0].path == "disclosure.product_naming.credit_card" and "past_due" in bad[0].message


def test_product_naming_with_inner_sentence_punctuation_is_an_error(ir_a, ledger_a, kit_tool_api) -> None:
    ir_a["disclosure"]["product_naming"]["car_loan"] = "car loan. It is overdue"
    issues = check(ir_a, ledger_a, kit_tool_api)
    assert any(i.code == "PRODUCT_NAMING_STATEMENT" and "car_loan" in i.path for i in issues)


# --- U-03: the amount and days fields ------------------------------------------------------------------------------------------


def _rename_days_field(api: ToolApi, old: str, new: str) -> None:
    products = next(spec for spec in api.tools["get_account"].response_fields if spec.items)
    field_spec = next(spec for spec in products.items if spec.name == old)
    field_spec.name = new


def test_a_days_field_that_the_tool_api_calls_something_else_is_found_and_checked(ir_a, ledger_a, kit_tool_api) -> None:
    """U-03: the API names the days field ``dpd``; guessing "days_late" silently put every customer in the green band."""
    _rename_days_field(kit_tool_api, "days_late", "dpd")
    ir_a["disclosure"]["state_fields"] = ["past_due", "dpd"]  # nothing in "dpd" says days to a name-based guess
    issues = check(ir_a, ledger_a, kit_tool_api)
    missing = [i for i in issues if i.code == "DAYS_FIELD_MISSING"]
    assert len(missing) == 1 and missing[0].severity == "error" and "'dpd'" in missing[0].message
    fixed, log = apply_safe_fixes(PolicyIR.model_validate(ir_a), ledger_a, kit_tool_api)
    assert fixed.disclosure.days_field == "dpd" and any("days_field" in line and "dpd" in line for line in log)
    assert "DAYS_FIELD_MISSING" not in codes(validate_policy(fixed, ledger_a, kit_tool_api))
    ir_a["disclosure"]["days_field"] = "days_overdue"  # an explicit field that does not exist
    issues = check(ir_a, ledger_a, kit_tool_api)
    assert any(i.code == "DAYS_FIELD_UNKNOWN" and i.severity == "error" and "dpd" in i.message for i in issues)


def test_state_fields_without_a_days_field_are_resolved_from_the_tool_api(ir_a, ledger_a, kit_tool_api) -> None:
    ir_a["disclosure"]["state_fields"] = ["type", "past_due"]  # the model forgot the days field and listed the type
    issues = check(ir_a, ledger_a, kit_tool_api)
    assert {"AMOUNT_FIELD_MISSING", "DAYS_FIELD_MISSING"} <= set(codes(issues, "error"))
    fixed, log = apply_safe_fixes(PolicyIR.model_validate(ir_a), ledger_a, kit_tool_api)
    assert fixed.disclosure.amount_and_days_fields() == ("past_due", "days_late")
    assert any("days_field" in line for line in log) and any("amount_field" in line for line in log)
    assert not {"AMOUNT_FIELD_MISSING", "DAYS_FIELD_MISSING"} & set(codes(validate_policy(fixed, ledger_a, kit_tool_api)))


def test_a_state_field_list_that_starts_with_the_type_does_not_make_the_type_the_amount(
    ir_a, ledger_a, kit_tool_api
) -> None:
    ir_a["disclosure"]["state_fields"] = ["type", "past_due", "days_late"]
    fixed, log = apply_safe_fixes(PolicyIR.model_validate(ir_a), ledger_a, kit_tool_api)
    assert fixed.disclosure.amount_and_days_fields() == ("past_due", "days_late")
    assert fixed.disclosure.amount_field == "past_due" and any("amount_field" in line for line in log)


def test_fields_the_state_fields_already_make_clear_are_left_alone(ir_a, ledger_a, kit_tool_api) -> None:
    fixed, log = apply_safe_fixes(PolicyIR.model_validate(ir_a), ledger_a, kit_tool_api)
    assert (fixed.disclosure.amount_field, fixed.disclosure.days_field) == (None, None)
    assert not any("amount_field" in line or "days_field" in line for line in log)


def test_an_unusable_amount_field_is_an_error(ir_a, ledger_a, kit_tool_api) -> None:
    ir_a["disclosure"]["amount_field"] = "balance"
    issues = check(ir_a, ledger_a, kit_tool_api)
    assert any(i.code == "AMOUNT_FIELD_UNKNOWN" and i.severity == "error" for i in issues)


# --- U-06: the first ask for several products ------------------------------------------------------------------------------------


def test_a_first_ask_about_all_products_needs_its_own_single_product_wording() -> None:
    from builder.ingest.tool_api import load_tool_api

    api = load_tool_api(Path(__file__).resolve().parents[2] / "tools" / "openapi.json")
    data = load("brief_ab_ir.json")
    ledger = RuleLedger.model_validate(load("brief_ab_ledger.json"))
    data["phrases"]["today_question"] = "Can you pay all of them today?"
    data["persuasion"]["first_ask"] = "Can you pay all of them today?"
    data["phrases"]["extra"].pop("today_question_all", None)
    issues = check(data, ledger, api)
    assert any(i.code == "TODAY_QUESTION_ALL" and i.severity == "error" for i in issues)
    data["phrases"]["today_question"] = "Can you make this payment today?"
    data["phrases"]["extra"]["today_question_all"] = "Can you pay all of them today?"
    assert "TODAY_QUESTION_ALL" not in codes(check(data, ledger, api))


# --- U-14: a note is not an exit before the identity is verified ---------------------------------------------------------------


def test_a_pre_verification_intent_with_a_note_reason_needs_an_outcome(ir_a, ledger_a, kit_tool_api) -> None:
    """U-14: the opening and identity nodes have no add_note tool; "add_note ...; continue" there is a dead end."""
    refusal = next(i for i in ir_a["intents"] if i["key"] == "refuse_to_talk" and i["scope"] == "before_verification")
    refusal["note_reason"] = "service_error"
    refusal.pop("outcome", None)
    issues = check(ir_a, ledger_a, kit_tool_api)
    bad = [i for i in issues if i.code == "INTENT_NOTE_BEFORE_VERIFICATION"]
    assert len(bad) == 1 and bad[0].severity == "error" and "refuse_to_talk" in bad[0].message
    refusal["outcome"] = {"outcome": "not_verified", "reason": "declined"}
    assert "INTENT_NOTE_BEFORE_VERIFICATION" not in codes(check(ir_a, ledger_a, kit_tool_api))


# --- U-15: the notes-driven closing row has no fixed reason -------------------------------------------------------------------


def test_the_product_notes_row_needs_no_reason_when_the_notes_pick_it() -> None:
    from builder.ingest.tool_api import load_tool_api

    api = load_tool_api(Path(__file__).resolve().parents[2] / "tools" / "openapi.json")
    data = load("brief_ab_ir.json")
    ledger = RuleLedger.model_validate(load("brief_ab_ledger.json"))
    assert data["multi_product"]["closing_reason_rule"] == "first_non_paid_note"
    data["closing"]["rows"].append({"outcome": "no_promise", "situation": "product_notes", "when": "notes decide"})
    assert "REASON_MISSING" not in codes(check(data, ledger, api))
    data["multi_product"]["closing_reason_rule"] = "fixed"  # the row's own reason is then the only source
    assert "REASON_MISSING" in codes(check(data, ledger, api))


# --- safe fixes for the format of the disclosure wording and for noted intents -----------------------------------------------


def _statement(ir: PolicyIR, product: dict[str, Any]) -> str:
    """The debt statement the compiled agent says for one product (account_facts code node, now = a Wednesday)."""
    from builder.checks.code_sandbox import run_snippet
    from builder.compiler import codegen

    state = run_snippet(codegen.setup_snippet(ir), {"now": "2026-10-14T11:00"})
    state = run_snippet(codegen.account_facts_snippet(ir), {**state, "products": [{"product_id": "p1", **product}]})
    return state["debt_statement"]


def test_a_model_written_lead_in_no_longer_doubles_in_the_debt_statement(ir_a) -> None:
    """U-02, end to end: team_phrase 'I am calling from ... about a late payment.' used to give 'from I am calling from'."""
    ir_a["phrases"]["extra"].pop("collections_intro")
    ir_a["disclosure"]["team_phrase"] = "I am calling from the collections team about a late payment."
    statement = _statement(
        PolicyIR.model_validate(ir_a),
        {"type": "credit_card", "card_last4": "4821", "past_due": 637.5, "days_late": 12, "minimum_payment": 60},
    )
    assert statement.startswith("Thank you. I am calling from the collections team about a late payment. Your credit card")
    assert statement.count("I am calling from") == 1 and ".." not in statement


def test_a_product_name_that_ends_in_a_full_stop_no_longer_breaks_the_statement(ir_a) -> None:
    ir_a["disclosure"]["product_naming"]["credit_card"] = "credit card ending {card_last4}."
    statement = _statement(
        PolicyIR.model_validate(ir_a),
        {"type": "credit_card", "card_last4": "4821", "past_due": 637.5, "days_late": 12, "minimum_payment": 60},
    )
    assert "Your credit card ending 4821 has a past-due amount of 637.50 and is 12 days late." in statement


def test_a_whole_sentence_team_phrase_moves_to_the_intro_sentence(ir_a, ledger_a, kit_tool_api) -> None:
    ir_a["phrases"]["extra"].pop("collections_intro")
    ir_a["disclosure"]["team_phrase"] = "This is the collections desk of Freya Bank"
    fixed, log = apply_safe_fixes(PolicyIR.model_validate(ir_a), ledger_a, kit_tool_api)
    assert fixed.disclosure.team_phrase == ""
    assert fixed.phrases.extra["disclosure_intro"] == "This is the collections desk of Freya Bank."
    assert any("disclosure_intro" in line for line in log)
    assert not {"TEAM_PHRASE_MISSING", "TEAM_PHRASE_SENTENCE"} & set(codes(validate_policy(fixed, ledger_a, kit_tool_api)))
    statement = _statement(fixed, {"type": "personal_loan", "past_due": 100, "days_late": 3, "instalment_amount": 20})
    assert statement.startswith("Thank you. This is the collections desk of Freya Bank. Your personal loan")


def test_an_empty_team_phrase_falls_back_to_the_institution_name(ir_a, ledger_a, kit_tool_api) -> None:
    ir_a["phrases"]["extra"].pop("collections_intro")
    ir_a["disclosure"]["team_phrase"] = ""
    fixed, log = apply_safe_fixes(PolicyIR.model_validate(ir_a), ledger_a, kit_tool_api)
    assert fixed.disclosure.team_phrase == ir_a["bank_name"] and any("institution" in line for line in log)


def test_an_any_scope_intent_that_records_a_note_is_split_at_the_verification_boundary(
    ir_a, ledger_a, kit_tool_api
) -> None:
    """U-14: the opening and identity nodes have no add_note tool; the note belongs to the verified half only."""
    ir_a["intents"] = [i for i in ir_a["intents"] if i["key"] != "human_request"]
    ir_a["intents"].append(
        {
            "key": "refuse_to_talk_again",
            "description": "Does not want to talk.",
            "scope": "any",
            "note_reason": "service_error",
            "outcome": {"outcome": "not_verified", "reason": "declined"},
        }
    )
    fixed, log = apply_safe_fixes(PolicyIR.model_validate(ir_a), ledger_a, kit_tool_api)
    halves = [i for i in fixed.intents if i.key == "refuse_to_talk_again"]
    assert [(i.scope, i.note_reason) for i in halves] == [("before_verification", None), ("after_verification", "service_error")]
    assert all(i.outcome is not None and i.outcome.outcome == "not_verified" for i in halves)
    assert any("split" in line for line in log)
    assert fixed.intent("refuse_to_talk_again", verified=False) is halves[0]


def test_a_before_verification_note_is_dropped_when_the_intent_has_an_outcome(ir_a, ledger_a, kit_tool_api) -> None:
    refusal = next(i for i in ir_a["intents"] if i["key"] == "refuse_to_talk" and i["scope"] == "before_verification")
    refusal["note_reason"] = "service_error"
    fixed, log = apply_safe_fixes(PolicyIR.model_validate(ir_a), ledger_a, kit_tool_api)
    kept = fixed.intent("refuse_to_talk", verified=False)
    assert kept is not None and kept.note_reason is None and kept.outcome is not None
    assert any("refuse_to_talk" in line and "note" in line for line in log)
