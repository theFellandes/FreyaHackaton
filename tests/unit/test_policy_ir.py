"""Tests for builder.policy.ir and the kit-brief fixtures (tests/fixtures/policy).

The fixtures are the expected rule ledger and PolicyIR for the kit's Brief A and Brief A+B. These tests check that they
validate, are canonical (complete, no stale keys), quote the briefs verbatim, cover every active rule, use only values
the kit tool API allows, and that the IR models enforce their structural rules.
"""

from __future__ import annotations

import itertools
import json
import re
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from builder.policy.intents import INTENT_CATALOG
from builder.policy.ir import (
    Band,
    Calendar,
    CallbackRule,
    DeadlineRule,
    Disclosure,
    OutcomeRef,
    PolicyIR,
    RuleEntry,
    RuleLedger,
    fill_template,
    template_fields,
)
from builder.policy.outcomes import STRUCTURAL_ORDER, is_transfer_situation

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "fixtures" / "policy"
BRIEFS = {"brief-a.md": REPO / "briefs" / "brief-a.md", "brief-b.md": REPO / "briefs" / "brief-b.md"}

IR_FILES = ["brief_a_ir.json", "brief_ab_ir.json"]
LEDGER_FILES = ["brief_a_ledger.json", "brief_ab_ledger.json"]
PAIRS = [("brief_a_ir.json", "brief_a_ledger.json"), ("brief_ab_ir.json", "brief_ab_ledger.json")]


def _json(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _ir(name: str) -> PolicyIR:
    return PolicyIR.model_validate(_json(name))


def _ledger(name: str) -> RuleLedger:
    return RuleLedger.model_validate(_json(name))


@pytest.fixture(scope="module")
def tool_api() -> dict[str, Any]:
    """The kit's tool API (read-only kit file)."""
    return json.loads((REPO / "tools" / "openapi.json").read_text(encoding="utf-8"))


def _request_enum(api: dict[str, Any], tool: str, field: str) -> set[str]:
    schema = api["paths"][f"/{tool}"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    return set(schema["properties"][field]["enum"])


def _response_schema(api: dict[str, Any], tool: str) -> dict[str, Any]:
    return api["paths"][f"/{tool}"]["post"]["responses"]["200"]["content"]["application/json"]["schema"]


# --------------------------------------------------------------------------------------------------------------------
# Fixtures validate and are canonical
# --------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", IR_FILES)
def test_ir_fixture_validates_and_is_canonical(name: str) -> None:
    raw = _json(name)
    ir = PolicyIR.model_validate(raw)
    assert ir.to_json_dict() == raw, "fixture must be a complete, canonical dump (regenerate it after model changes)"


@pytest.mark.parametrize("name", LEDGER_FILES)
def test_ledger_fixture_validates_and_is_canonical(name: str) -> None:
    raw = _json(name)
    ledger = RuleLedger.model_validate(raw)
    assert ledger.model_dump(mode="json", exclude_none=True) == raw
    assert ledger.duplicate_ids() == []


@pytest.mark.parametrize("name", LEDGER_FILES)
def test_every_quote_is_verbatim_in_its_brief(name: str) -> None:
    ledger = _ledger(name)
    texts = {source: path.read_text(encoding="utf-8") for source, path in BRIEFS.items()}
    assert set(ledger.source_files) <= set(texts)
    for rule in ledger.rules:
        assert rule.source in ledger.source_files, rule.id
        assert rule.quote in texts[rule.source], f"{rule.id}: quote not found verbatim in {rule.source}"


@pytest.mark.parametrize(("name", "source", "prefix"), [
    ("brief_a_ledger.json", "brief-a.md", "A"),
    ("brief_ab_ledger.json", "brief-a.md", "A"),
    ("brief_ab_ledger.json", "brief-b.md", "B"),
])
def test_every_numbered_rule_has_ledger_entries(name: str, source: str, prefix: str) -> None:
    text = BRIEFS[source].read_text(encoding="utf-8")
    numbers = {int(n) for n in re.findall(r"^(\d+)\. ", text, flags=re.MULTILINE)}
    assert numbers, "the brief has numbered rules"
    ids = set(_ledger(name).ids())
    for number in sorted(numbers):
        pattern = re.compile(rf"^{prefix}-{number}[a-z]?$")
        assert any(pattern.match(rule_id) for rule_id in ids), f"rule {number} of {source} has no ledger entry"


def test_brief_a_special_entries_present() -> None:
    ids = set(_ledger("brief_a_ledger.json").ids())
    assert {"A-DATES", "A-BANDS", "A-OUTCOMES", "A-OUT-T", "A-NONOTE", "A-47"} <= ids


def test_brief_ab_ledger_holds_all_a_rules_and_b_rules() -> None:
    a_ids = _ledger("brief_a_ledger.json").ids()
    ab = _ledger("brief_ab_ledger.json")
    assert ab.ids()[: len(a_ids)] == a_ids
    assert {"B-0", "B-5", "B-11", "B-CLOSING-a", "B-CLOSING-d"} <= set(ab.ids())


# --------------------------------------------------------------------------------------------------------------------
# Supersession and coverage
# --------------------------------------------------------------------------------------------------------------------


def test_brief_a_ledger_has_no_supersession() -> None:
    ledger = _ledger("brief_a_ledger.json")
    assert ledger.superseded_ids() == set()
    assert len(ledger.active_rules()) == len(ledger.rules)


def test_brief_ab_supersessions_are_the_expected_replacements() -> None:
    ledger = _ledger("brief_ab_ledger.json")
    assert ledger.superseded_ids() == {"A-JOB-a", "A-NONOTE", "A-39b", "A-40b", "A-37c"}
    expected_by = {"A-JOB-a": "B-0", "A-NONOTE": "B-8b", "A-39b": "B-9", "A-40b": "B-9", "A-37c": "B-11"}
    for old, new in expected_by.items():
        rule = ledger.get(old)
        assert rule is not None and rule.superseded_by == new
        newer = ledger.get(new)
        assert newer is not None and old in newer.supersedes
    assert ledger.with_consistent_supersessions() == ledger, "both supersession directions already agree"
    assert ledger.supersessions, "the supersession log is filled"


@pytest.mark.parametrize(("ir_name", "ledger_name"), PAIRS)
def test_ir_covers_every_active_rule_and_nothing_else(ir_name: str, ledger_name: str) -> None:
    ir, ledger = _ir(ir_name), _ledger(ledger_name)
    referenced = ir.referenced_rule_ids()
    active = {rule.id for rule in ledger.active_rules()}
    assert referenced - set(ledger.ids()) == set(), "IR cites unknown rule ids"
    assert active - referenced == set(), "active ledger rules not referenced by the IR"
    assert referenced & ledger.superseded_ids() == set(), "IR cites superseded rules"


@pytest.mark.parametrize("name", LEDGER_FILES)
def test_ambiguities_cite_known_rules(name: str) -> None:
    ledger = _ledger(name)
    ids = set(ledger.ids())
    assert ledger.ambiguities
    for ambiguity in ledger.ambiguities:
        assert set(ambiguity.rule_ids) <= ids, ambiguity.id
        assert ambiguity.chosen and ambiguity.rationale and len(ambiguity.options) >= 2


def test_ab_ambiguities_include_the_brief_b_conflicts() -> None:
    ids = {a.id for a in _ledger("brief_ab_ledger.json").ambiguities}
    assert {"AMB-06", "AMB-17", "AMB-18", "AMB-23"} <= ids


# --------------------------------------------------------------------------------------------------------------------
# Fixture values against the kit tool API and the brief tables
# --------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", IR_FILES)
def test_ir_values_are_allowed_by_the_tool_api(name: str, tool_api: dict[str, Any]) -> None:
    ir = _ir(name)
    outcomes = _request_enum(tool_api, "close_call", "outcome")
    for path, ref in ir.outcome_refs():
        assert ref.outcome in outcomes, path
    assert {q.field for q in ir.identity.questions} <= _request_enum(tool_api, "verify_identity", "field")
    hint_enum = set(_response_schema(tool_api, "verify_identity")["properties"]["hint"]["enum"])
    assert {h.value for h in ir.identity.hints} <= hint_enum
    queues = _request_enum(tool_api, "transfer_to_agent", "queue")
    for intent in ir.intents:
        if intent.transfer_queue() is not None:
            assert intent.transfer_queue() in queues, intent.key
    for row in ir.closing.rows:
        if is_transfer_situation(row.situation):
            assert row.situation.split(":", 1)[1] in queues
    assert set(ir.multi_product.note_reasons) <= _request_enum(tool_api, "add_note", "reason")
    product = _response_schema(tool_api, "get_account")["properties"]["products"]["items"]
    product_types, product_fields = set(product["properties"]["type"]["enum"]), set(product["properties"])
    assert set(ir.amounts.floor_field_by_type) == product_types
    assert set(ir.disclosure.product_naming) == product_types
    assert set(ir.amounts.floor_field_by_type.values()) <= product_fields
    assert set(ir.disclosure.state_fields) <= product_fields
    for template in ir.disclosure.product_naming.values():
        assert set(template_fields(template)) <= product_fields


@pytest.mark.parametrize("name", IR_FILES)
def test_outcome_rows_are_exactly_the_brief_a_table(name: str) -> None:
    pairs = {(row.outcome, row.reason) for row in _ir(name).closing.rows}
    assert pairs == {
        ("promise", None), ("paid", None),
        ("transferred", "restructuring"), ("transferred", "human"), ("transferred", "support"),
        ("no_promise", "refused"), ("no_promise", "hardship"), ("no_promise", "dispute"),
        ("no_promise", "service_error"),
        ("not_verified", "failed"), ("not_verified", "declined"),
        ("not_reached", "callback"), ("not_reached", "no_callback"), ("not_reached", "wrong_number"),
        ("not_reached", "deceased"), ("not_reached", "unclear"),
    }


@pytest.mark.parametrize("name", IR_FILES)
def test_precedence_keys_resolve_and_rows_are_reachable(name: str) -> None:
    closing = _ir(name).closing
    row_keys = {row.situation for row in closing.rows}
    for key in closing.precedence:
        assert key in row_keys or key in STRUCTURAL_ORDER, f"precedence key {key!r} has no row"
    assert row_keys <= set(closing.precedence), "every row situation is ranked explicitly"
    assert len(closing.precedence) == len(set(closing.precedence))


@pytest.mark.parametrize("name", IR_FILES)
def test_bands_are_contiguous_and_cover_1_to_45(name: str) -> None:
    bands = sorted(_ir(name).bands, key=lambda b: b.days_late_min)
    assert bands[0].days_late_min == 1 and bands[-1].days_late_max == 45
    for lower, upper in itertools.pairwise(bands):
        assert upper.days_late_min == lower.days_late_max + 1


@pytest.mark.parametrize("name", IR_FILES)
def test_phrase_placeholders_are_call_variables(name: str) -> None:
    ir = _ir(name)
    variables = set(ir.call_variable_names())
    texts = [v for k, v in ir.phrases.model_dump().items() if isinstance(v, str)] + list(ir.phrases.extra.values())
    for text in texts:
        assert set(template_fields(text)) <= variables, text


def test_brief_a_ir_key_values() -> None:
    ir = _ir("brief_a_ir.json")
    assert [q.field for q in ir.identity.questions] == ["date_of_birth", "phone_last4"]
    assert ir.identity.max_wrong_total == 3
    assert [(b.name, b.deadline.n, b.persuasion_attempts) for b in ir.bands] == [("early", 12, 1), ("late", 7, 2)]
    assert [d.isoformat() for d in ir.calendar.holidays] == [
        "2026-10-26", "2026-11-11", "2026-11-26", "2026-12-25", "2027-01-01"]
    assert ir.phrases.right_party_question == "Am I speaking with {title} {last_name}?"
    assert ir.third_party.callback is not None
    assert (ir.third_party.callback.window_start, ir.third_party.callback.window_end) == ("10:00", "19:00")
    assert not ir.multi_product.enabled and ir.multi_product.note_reasons == []
    assert ir.currency_word == "", "the brief names no currency"
    assert {"collections", "debt", "payment"} <= set(ir.forbidden_before_verification)


def test_brief_ab_ir_differs_from_a_only_where_b_says_so() -> None:
    a, ab = _ir("brief_a_ir.json"), _ir("brief_ab_ir.json")
    assert ab.multi_product.enabled and ab.multi_product.max_products == 3
    assert ab.multi_product.note_reasons == ["refused", "hardship", "dispute", "paid", "service_error"]
    assert ab.multi_product.all_paid_outcome == OutcomeRef(outcome="paid")
    assert ab.confirmation.single_confirmation_for_all_products and not a.confirmation.single_confirmation_for_all_products
    for section in ("identity", "calendar", "amounts", "third_party", "right_party"):
        a_dump = getattr(a, section).model_dump(exclude={"rule_ids"})
        ab_dump = getattr(ab, section).model_dump(exclude={"rule_ids"})
        assert a_dump == ab_dump, f"Brief B does not change {section}"
    assert [b.model_dump(exclude={"rule_ids"}) for b in a.bands] == [b.model_dump(exclude={"rule_ids"}) for b in ab.bands]


@pytest.mark.parametrize("name", IR_FILES)
def test_ir_intent_keys_are_catalog_keys(name: str) -> None:
    keys = {intent.key for intent in _ir(name).intents}
    assert keys <= set(INTENT_CATALOG)


# --------------------------------------------------------------------------------------------------------------------
# PolicyIR helpers
# --------------------------------------------------------------------------------------------------------------------


def test_band_lookup() -> None:
    ir = _ir("brief_a_ir.json")
    assert ir.band_for(1).name == "early"  # type: ignore[union-attr]
    assert ir.band_for(7).name == "early"  # type: ignore[union-attr]
    assert ir.band_for(8).name == "late"  # type: ignore[union-attr]
    assert ir.band_for(45).name == "late"  # type: ignore[union-attr]
    assert ir.band_for(0) is None and ir.band_for(46) is None
    assert ir.nearest_band(0).name == "early"  # type: ignore[union-attr]
    assert ir.nearest_band(90).name == "late"  # type: ignore[union-attr]


def test_intent_lookup_by_verification_state() -> None:
    ir = _ir("brief_a_ir.json")
    before, after = ir.intent("refuse_to_talk", verified=False), ir.intent("refuse_to_talk", verified=True)
    assert before is not None and before.outcome == OutcomeRef(outcome="not_verified", reason="declined")
    assert after is not None and after.outcome == OutcomeRef(outcome="no_promise", reason="refused")
    hardship = ir.intent("hardship")
    assert hardship is not None and hardship.transfer_queue() == "support"
    assert ir.intent("does_not_exist") is None


def test_persuasion_steps_per_band() -> None:
    ir = _ir("brief_a_ir.json")
    assert [s.attempt for s in ir.persuasion.steps_for("early", 1)] == [1]
    assert [s.attempt for s in ir.persuasion.steps_for("late", 2)] == [1, 2]
    assert [s.attempt for s in ir.persuasion.steps_for("early")] == [1], "attempt 2 is late band only"


def test_outcome_refs_include_rows_and_section_refs() -> None:
    paths = dict(_ir("brief_a_ir.json").outcome_refs())
    assert paths["identity.on_failed"] == OutcomeRef(outcome="not_verified", reason="failed")
    assert paths["closing.rows[0]"] == OutcomeRef(outcome="transferred", reason="restructuring")
    assert "third_party.callback.success" in paths


def test_product_naming() -> None:
    disclosure = _ir("brief_a_ir.json").disclosure
    assert disclosure.product_name({"type": "credit_card", "card_last4": "4821"}) == "credit card ending 4821"
    assert disclosure.product_name({"type": "car_loan"}) == "car loan"
    assert disclosure.product_name({"type": "home_loan"}) == "home loan", "unknown types fall back to their name"


def test_template_helpers() -> None:
    assert template_fields("Hi {title} {last_name}, {title}") == ["title", "last_name"]
    assert template_fields("no placeholders, {{double}} braces stay out") == []
    assert fill_template("{a} and {b}", {"a": 1}) == "1 and {b}"
    assert fill_template("{a}", {"a": None}) == "{a}"
    assert fill_template("{{a}} is a runtime variable, {a} is not", {"a": 1}) == "{{a}} is a runtime variable, 1 is not"


def test_example_minimal_is_valid_and_generic() -> None:
    ir = PolicyIR.example_minimal()
    assert PolicyIR.model_validate(ir.to_json_dict()) == ir
    assert "Freya" not in json.dumps(ir.to_json_dict())
    assert ir.band_for(30) is not None


def test_json_schema_is_closed_for_structured_output() -> None:
    schema = PolicyIR.model_json_schema()
    assert schema.get("additionalProperties") is False
    assert all(definition.get("additionalProperties") is False
               for definition in schema["$defs"].values() if definition.get("type") == "object")
    assert "RuleEntry" in RuleLedger.model_json_schema()["$defs"]


# --------------------------------------------------------------------------------------------------------------------
# Model validation rules
# --------------------------------------------------------------------------------------------------------------------


def _ref() -> OutcomeRef:
    return OutcomeRef(outcome="x", reason="y")


def test_callback_rule_normalises_and_checks_times() -> None:
    rule = CallbackRule(allowed_days="next_business_day", window_start="9:00", window_end="17.30",
                        success=_ref(), failure=_ref())
    assert (rule.window_start, rule.window_end) == ("09:00", "17:30")
    with pytest.raises(ValidationError):
        CallbackRule(allowed_days="next_business_day", window_start="nine", window_end="17:00",
                     success=_ref(), failure=_ref())
    with pytest.raises(ValidationError):
        CallbackRule(allowed_days="next_business_day", window_start="18:00", window_end="10:00",
                     success=_ref(), failure=_ref())
    with pytest.raises(ValidationError):
        CallbackRule(allowed_days="next_business_day", window_start="10:00", window_end="25:00",
                     success=_ref(), failure=_ref())


def test_deadline_rule_requires_its_parameters() -> None:
    with pytest.raises(ValidationError):
        DeadlineRule(kind="nth_business_day_after_today")
    with pytest.raises(ValidationError):
        DeadlineRule(kind="nth_calendar_day_after_today", n=-1)
    with pytest.raises(ValidationError):
        DeadlineRule(kind="fixed_date")
    assert DeadlineRule(kind="custom", custom_rule_text="two weeks after the statement").n is None


def test_band_range_must_be_ordered() -> None:
    with pytest.raises(ValidationError):
        Band(name="bad", days_late_min=10, days_late_max=5,
             deadline=DeadlineRule(kind="end_of_month"), persuasion_attempts=1)


def test_calendar_normalises_weekdays_and_holidays() -> None:
    cal = Calendar(business_weekdays=[4, 0, 0, 2], holidays=["2030-05-01", "2030-01-01", "2030-05-01"])
    assert cal.business_weekdays == [0, 2, 4]
    assert [d.isoformat() for d in cal.holidays] == ["2030-01-01", "2030-05-01"]
    with pytest.raises(ValidationError):
        Calendar(business_weekdays=[7], holidays=[])
    with pytest.raises(ValidationError):
        Calendar(business_weekdays=[], holidays=[])
    with pytest.raises(ValidationError):
        Calendar(holidays=["not a date"])


def test_unknown_keys_and_bad_vocabulary_are_rejected() -> None:
    with pytest.raises(ValidationError):
        Disclosure(team_phrase="t", product_naming={}, state_fields=[], colour="blue")  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        RuleEntry(id="X-1", source="s", quote="q", category="weather", statement="s", scope=["global"])  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        RuleEntry(id="X-1", source="s", quote="q", category="other", statement="s", scope=["lobby"])  # type: ignore[list-item]


# --------------------------------------------------------------------------------------------------------------------
# RuleLedger helpers
# --------------------------------------------------------------------------------------------------------------------


def _rule(rule_id: str, scope: list[str] | None = None, **extra: Any) -> RuleEntry:
    return RuleEntry(id=rule_id, source="memo.txt", quote=rule_id, category="other", statement=rule_id,
                     scope=scope or ["global"], **extra)


def test_with_consistent_supersessions_fills_both_directions() -> None:
    ledger = RuleLedger(rules=[
        _rule("R-1"), _rule("R-2"), _rule("M-1", supersedes=["R-1"]), _rule("R-3", superseded_by="M-2"),
        _rule("M-2"),
    ])
    fixed = ledger.with_consistent_supersessions()
    assert fixed.get("R-1").superseded_by == "M-1"  # type: ignore[union-attr]
    assert fixed.get("M-2").supersedes == ["R-3"]  # type: ignore[union-attr]
    assert ledger.get("R-1").superseded_by is None, "the original is not mutated"  # type: ignore[union-attr]
    assert {r.id for r in fixed.active_rules()} == {"R-2", "M-1", "M-2"}
    assert ledger.superseded_ids() == fixed.superseded_ids() == {"R-1", "R-3"}


def test_for_scope_and_duplicates() -> None:
    ledger = RuleLedger(rules=[
        _rule("G-1"), _rule("I-1", ["identity"]), _rule("N-1", ["negotiation"]), _rule("I-1", ["identity"]),
        _rule("I-2", ["identity"], superseded_by="I-3"),
    ])
    assert [r.id for r in ledger.for_scope("identity")] == ["G-1", "I-1", "I-1"]
    assert [r.id for r in ledger.for_scope("identity", include_global=False)] == ["I-1", "I-1"]
    assert ledger.duplicate_ids() == ["I-1"]
    assert ledger.get("missing") is None


def test_ab_ledger_scopes_route_rules_to_stages() -> None:
    ledger = _ledger("brief_ab_ledger.json")
    identity_ids = {r.id for r in ledger.for_scope("identity", include_global=False)}
    assert {"A-11a", "A-12b", "A-13", "A-15c"} <= identity_ids
    assert "A-17a" not in identity_ids, "debt wording never reaches the identity stage"
    negotiation_ids = {r.id for r in ledger.for_scope("negotiation")}
    assert {"B-4a", "B-5", "A-30d"} <= negotiation_ids
    assert "A-39b" not in negotiation_ids, "superseded rules are not routed"


# --- U-01 / U-02 / U-03 / U-06: the free-text fields have a format contract ------------------------------------------------


def test_persuasion_step_date_kinds_cover_a_next_business_day_ladder() -> None:
    from builder.policy.ir import PersuasionStep

    step = PersuasionStep(attempt=1, amount="current", date="next_business_day_after_today", text="Next business day.")
    assert step.n is None
    nth = PersuasionStep(attempt=2, amount="floor", date="nth_business_day_after_today", n=3)
    assert nth.n == 3
    with pytest.raises(ValidationError, match="needs a non-negative n"):
        PersuasionStep(attempt=2, amount="floor", date="nth_calendar_day_after_today")
    with pytest.raises(ValidationError):
        PersuasionStep(attempt=1, amount="full", date="whenever")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("written", "spoken"),
    [
        ("the collections team", "the collections team"),
        ("I am calling from the collections team about a late payment.", "the collections team about a late payment"),
        ("I'm calling from Meridian's arrears desk.", "Meridian's arrears desk"),
        ("  We are calling on behalf of   the bank!  ", "the bank"),
        ("i am calling from the arrears team", "the arrears team"),
        ("", ""),
    ],
)
def test_team_phrase_is_the_words_that_complete_the_sentence(written: str, spoken: str) -> None:
    """U-02: codegen says "I am calling from {team_phrase}."; a whole sentence there doubled the lead-in."""
    disclosure = Disclosure(team_phrase=written, product_naming={}, state_fields=[])
    assert disclosure.team_phrase == spoken


def test_product_names_are_bare_noun_phrases() -> None:
    disclosure = Disclosure(
        team_phrase="the team",
        product_naming={"card": "card ending {card_last4}.", "loan": "  personal   loan ; "},
        state_fields=[],
    )
    assert disclosure.product_naming == {"card": "card ending {card_last4}", "loan": "personal loan"}
    assert disclosure.product_name({"type": "card", "card_last4": "1234"}) == "card ending 1234"


def test_the_schema_the_model_sees_explains_team_phrase_and_product_naming() -> None:
    from builder.llm.structured import schema_for_llm

    text = json.dumps(schema_for_llm(PolicyIR))
    assert "I am calling from" in text and "bare noun phrase" in text
    assert "disclosure_intro" in text and "today_question_all" in text and "floor_offer" in text


def test_amount_and_days_fields_come_from_the_explicit_fields_first() -> None:
    plain = Disclosure(team_phrase="x", product_naming={}, state_fields=["past_due", "days_late"])
    assert plain.amount_and_days_fields() == ("past_due", "days_late")
    renamed = Disclosure(team_phrase="x", product_naming={}, state_fields=["past_due", "dpd"], days_field="dpd")
    assert renamed.amount_and_days_fields() == ("past_due", "dpd")
    listed_type_first = Disclosure(
        team_phrase="x", product_naming={}, state_fields=["type", "past_due"], amount_field="past_due",
        days_field="days_overdue",
    )
    assert listed_type_first.amount_and_days_fields() == ("past_due", "days_overdue")
    # nothing to go on: "" - the validator reports it, there is no brief-specific default in the policy package
    assert Disclosure(team_phrase="x", product_naming={}, state_fields=["dpd"]).amount_and_days_fields() == ("dpd", "")


def test_a_rule_can_be_narrowed_without_being_superseded() -> None:
    ledger = _ledger("brief_a_ledger.json")
    rules = [ledger.rules[0].model_copy(update={"narrowed_by": ["B-1"]}), *ledger.rules[1:]]
    narrowed = ledger.model_copy(update={"rules": rules})
    assert ledger.rules[0].id in {r.id for r in narrowed.active_rules()}
    assert ledger.rules[0].id not in narrowed.superseded_ids()
