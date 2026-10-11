"""Structural variants of a hidden brief, built from one synthetic, model-made policy - never from Brief A or B.

The base is the IR and ledger the real extraction model produced for the synthetic pack ``evals/synthetic_briefs/v01_clean``
(an invented bank: other holidays, three lateness bands, postcode + year-of-birth identity, three product types, renamed
``get_account`` fields, another outcome table), copied unchanged to ``tests/fixtures/variants/``. That extraction still
had defects the current validator rejects; :func:`base` repairs them the way the documents say (each repair is
listed in its docstring), so every variant starts from a policy that passes ``validate_policy``.

Each variant function takes a :class:`Variant` and returns an edited deep copy. The edits are the structural changes the
audit found a hidden brief may bring: a business customer, a reworded tool contract, a renamed clock variable, a
percentage floor, three products with per-product rules, another calendar and deadline rule, extra outcomes, another
decline limit, another callback window and an amended rule. Values are invented here; none comes from a kit brief.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from builder.ingest.tool_api import ToolApi, load_tool_api
from builder.policy.ir import PolicyIR, RuleLedger

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "fixtures" / "variants"
SYNTHETIC_PACK = REPO / "evals" / "synthetic_briefs" / "v01_clean"

#: Values of the public kit briefs (A and B) and of the kit tool API that a variant replaced. None may appear in a
#: compiled variant: they would mean the builder carries a brief value instead of reading it from the IR / contract.
KIT_LITERALS = (
    "Freya Bank", "freya-bank-mock", "2026-10-26", "2026-11-11", "2026-11-26", "2026-12-25", "2027-01-01",
    "date_of_birth", "phone_last4", "day_month_swapped", "minimum_payment", "instalment_amount", "days_late\"",
    "credit_card", "personal_loan", "car_loan", "no_promise", "not_verified", "not_reached", "Daniel", "Reyes",
    "text message confirming", "credit card ending", "4821",
)


@dataclass
class Variant:
    """A policy (IR + ledger, as JSON data) and the tool contract (an OpenAPI document) it is compiled against."""

    name: str
    ir: dict[str, Any]
    ledger: dict[str, Any]
    openapi: dict[str, Any]

    def copy(self, name: str) -> Variant:
        return Variant(name, copy.deepcopy(self.ir), copy.deepcopy(self.ledger), copy.deepcopy(self.openapi))

    def policy(self) -> PolicyIR:
        return PolicyIR.model_validate(self.ir)

    def rules(self) -> RuleLedger:
        return RuleLedger.model_validate(self.ledger)

    def tool_api(self, directory: Path) -> ToolApi:
        """The contract parsed by the real loader (it is written to ``directory`` first)."""
        path = directory / f"{self.name}.openapi.json"
        path.write_text(json.dumps(self.openapi, indent=1), encoding="utf-8")
        return load_tool_api(path)


# ---- OpenAPI editing helpers --------------------------------------------------------------------------------------------


def _operation(spec: dict[str, Any], tool: str) -> dict[str, Any]:
    return spec["paths"][f"/{tool}"]["post"]


def request_schema(spec: dict[str, Any], tool: str) -> dict[str, Any]:
    return _operation(spec, tool)["requestBody"]["content"]["application/json"]["schema"]


def response_schema(spec: dict[str, Any], tool: str) -> dict[str, Any]:
    return _operation(spec, tool)["responses"]["200"]["content"]["application/json"]["schema"]


def item_schema(spec: dict[str, Any]) -> dict[str, Any]:
    return response_schema(spec, "get_account")["properties"]["products"]["items"]


def rename_property(schema: dict[str, Any], old: str, new: str, **changes: Any) -> None:
    """Rename one property (order and ``required`` kept) and update its schema with ``changes``."""
    schema["properties"] = {(new if k == old else k): v for k, v in schema["properties"].items()}
    schema["properties"][new].update(changes)
    if old in schema.get("required", []):
        schema["required"] = [new if r == old else r for r in schema["required"]]


def set_response_description(spec: dict[str, Any], tool: str, text: str) -> None:
    _operation(spec, tool)["responses"]["200"]["description"] = text


# ---- the base -----------------------------------------------------------------------------------------------------------


def _load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def base() -> Variant:
    """The synthetic model-made policy with the defects of that extraction repaired (1-3) or left to the pipeline (4).

    1. ``refuse_to_talk`` recorded a note in every scope but had no outcome: before verification there is no note tool,
       so it is split (as the documents' outcome table says) into a before-verification exit (``unverified`` /
       ``declined_checks``) and an after-verification exit with the note (``declined`` / ``rejected``).
    2. ``product_naming`` held whole debt statements; it now holds bare product names.
    3. Two outcome rows were shadowed (``ROW_SHADOWED``): the care-team transfer's row was filed under ``hardship`` (it
       is the ``transfer:care_team`` row now) and a second ``product_notes`` row repeated what the
       first-non-settled-note rule decides.
    4. The persuasion date kinds contradicted their own text; this one is left as extracted on purpose - the pipeline's
       safe fix (:func:`builder.understand.fixes.apply_safe_fixes`) must repair it.
    """
    ir = _load("synthetic_v01_ir.json")
    intents: list[dict[str, Any]] = []
    for intent in ir["intents"]:
        if intent["key"] == "refuse_to_talk":
            before = {k: v for k, v in intent.items() if k != "note_reason"}
            before.update(scope="before_verification", outcome={"outcome": "unverified", "reason": "declined_checks"})
            after = {**intent, "scope": "after_verification", "outcome": {"outcome": "declined", "reason": "rejected"}}
            intents += [before, after]
        else:
            intents.append(intent)
    ir["intents"] = intents
    for row in ir["closing"]["rows"]:
        if (row["situation"], row["outcome"]) == ("hardship", "escalated"):
            row["situation"] = "transfer:care_team"
    ir["closing"]["rows"] = [r for r in ir["closing"]["rows"] if (r["situation"], r["outcome"]) != ("product_notes",
                                                                                                    "declined")]
    ir["disclosure"]["product_naming"] = {
        "card": "card ending {card_last4}", "loan": "personal loan", "vehicle_finance": "vehicle finance"}
    openapi = json.loads((SYNTHETIC_PACK / "openapi.json").read_text(encoding="utf-8"))
    return Variant("base", ir, _load("synthetic_v01_ledger.json"), openapi)


def add_rule(variant: Variant, rule_id: str, quote: str, category: str, scope: list[str], **extra: Any) -> None:
    """Add a ledger rule (the quote is the documents' wording, the statement repeats it)."""
    variant.ledger["rules"].append({"id": rule_id, "source": "memo.txt", "quote": quote, "category": category,
                                    "statement": quote, "scope": scope, "supersedes": [], "superseded_by": None,
                                    **extra})


def rule(variant: Variant, rule_id: str) -> dict[str, Any]:
    return next(r for r in variant.ledger["rules"] if r["id"] == rule_id)


def _replace_citation(node: Any, old: str, new: str) -> None:
    """Cite ``new`` wherever the IR cites ``old`` (an amended rule)."""
    if isinstance(node, dict):
        if isinstance(node.get("rule_ids"), list) and old in node["rule_ids"]:
            node["rule_ids"] = list(dict.fromkeys(new if r == old else r for r in node["rule_ids"]))
        for value in node.values():
            _replace_citation(value, old, new)
    elif isinstance(node, list):
        for value in node:
            _replace_citation(value, old, new)


# ---- the variants -------------------------------------------------------------------------------------------------------


def business(v: Variant) -> Variant:
    """A business customer: a company name call variable, company number + founding year as the security questions, and a
    receptionist rule for the third-party branch."""
    out = v.copy("business")
    ir = out.ir
    ir["call_variables"] = [c for c in ir["call_variables"] if c["name"] != "title"]
    ir["call_variables"].insert(3, {"name": "company_name", "meaning": "The business the call is about.",
                                    "example": "Kestrel Joinery Ltd"})
    ir["phrases"]["right_party_question"] = "May I speak with {first_name} {last_name} of {company_name}?"
    ir["identity"]["questions"] = [
        {"field": "company_number", "ask": "What is the company's registration number?", "answer_format": "digits",
         "digits": 8, "normalisation": "Send the eight digits only, without spaces."},
        {"field": "founded_year", "ask": "In which year was the company founded?", "answer_format": "year",
         "digits": 4, "normalisation": "Send as four digits only."},
    ]
    ir["identity"]["hints"] = []
    ir["third_party"]["notes"].append(
        "A receptionist or colleague may offer to take a message: thank them, give no detail and ask when "
        "{first_name} {last_name} can be reached.")
    ir["right_party"]["confirm_examples"] = ["yes", "speaking", "this is {first_name}"]
    verify = request_schema(out.openapi, "verify_identity")
    verify["properties"]["field"]["enum"] = ["company_number", "founded_year"]
    verify["properties"]["answer"]["description"] = (
        "For company_number: eight digits. For founded_year: four digits. An unreadable value is rejected with ok=false "
        "and is not counted as a wrong answer.")
    hint = response_schema(out.openapi, "verify_identity")["properties"]
    hint.pop("hint")
    for r in out.ledger["rules"]:  # the security questions of the documents change with the policy
        if r["id"] in ("D1-11a", "D1-11c", "D1-11e"):
            r["quote"] = r["statement"] = {
                "D1-11a": "Ask two security questions, one at a time: first the company registration number, then "
                          "the year the company was founded.",
                "D1-11c": "Send the registration number as eight digits and the founding year as four digits.",
                "D1-11e": "A registration number is complete when it has eight digits.",
            }[r["id"]]
        if r["id"] == "D1-13a":
            r["quote"] = r["statement"] = "If the founding year is said with two digits, ask for all four digits."
    add_rule(out, "X-TP-1", "A receptionist may take a message but must learn nothing about the account.",
             "third_party", ["third_party"])
    ir["third_party"]["rule_ids"].append("X-TP-1")
    return out


def renamed_contract(v: Variant) -> Variant:
    """The seven tool names stay, every field the agent writes or reads is renamed or reworded.

    * every tool: ``customer_id`` -> ``customer_ref``;
    * ``create_promise``: ``product_id`` -> ``account_ref``, ``amount`` -> ``amount_minor`` (integer cents), ``date`` ->
      ``pay_on`` (a date without a format), a new required ``channel`` fixed to ``voice``, and the success flag ``ok`` ->
      ``accepted`` described in prose ("true when the promise was recorded");
    * ``add_note``: ``reason`` -> ``note_code``; success flag ``ok`` -> ``saved`` described as "true when the note was saved";
    * ``schedule_callback``: ``date`` / ``time`` -> ``callback_date`` / ``callback_time``;
    * ``transfer_to_agent``: ``queue`` -> ``team``;
    * ``close_call``: ``outcome`` / ``reason`` -> ``disposition`` / ``disposition_reason``;
    * ``get_account`` items: ``product_id`` -> ``id``, ``past_due`` -> ``overdue_cents`` and ``min_due`` ->
      ``min_due_cents`` (integer cents).
    """
    out = v.copy("renamed_contract")
    spec = out.openapi
    for tool in ("verify_identity", "get_account", "create_promise", "add_note", "schedule_callback",
                 "transfer_to_agent", "close_call"):
        rename_property(request_schema(spec, tool), "customer_id", "customer_ref")
    promise = request_schema(spec, "create_promise")
    rename_property(promise, "product_id", "account_ref", description="The product the promise is for.")
    rename_property(promise, "amount", "amount_minor", type="integer",
                    description="Promised amount in minor units (cents), e.g. 12050 for 120.50.")
    rename_property(promise, "date", "pay_on", description="The day the customer will pay.")
    promise["properties"]["pay_on"].pop("format", None)
    promise["properties"]["channel"] = {"type": "string", "enum": ["voice"]}
    promise["required"].append("channel")
    answer = response_schema(spec, "create_promise")
    rename_property(answer, "ok", "accepted", description="True when the promise was recorded.")
    set_response_description(spec, "create_promise", "The bank's answer.")
    note = request_schema(spec, "add_note")
    rename_property(note, "reason", "note_code")
    rename_property(response_schema(spec, "add_note"), "ok", "saved", description="True when the note was saved.")
    set_response_description(spec, "add_note", "The bank's answer.")
    callback = request_schema(spec, "schedule_callback")
    rename_property(callback, "date", "callback_date")
    rename_property(callback, "time", "callback_time")
    rename_property(request_schema(spec, "transfer_to_agent"), "queue", "team")
    close = request_schema(spec, "close_call")
    rename_property(close, "outcome", "disposition")
    rename_property(close, "reason", "disposition_reason")
    items = item_schema(spec)
    rename_property(items, "product_id", "id")
    rename_property(items, "past_due", "overdue_cents", type="integer", description="Overdue amount in cents.")
    rename_property(items, "min_due", "min_due_cents", type="integer", description="Minimum due in cents (cards only).")
    ir = out.ir
    ir["amounts"]["floor_field_by_type"]["card"] = "min_due_cents"
    ir["disclosure"]["state_fields"] = ["overdue_cents", "days_overdue"]
    return out


def string_status_contract(v: Variant) -> Variant:
    """``create_promise`` answers with a string status instead of a boolean: ``status`` is ``recorded`` or ``rejected``."""
    out = v.copy("string_status")
    answer = response_schema(out.openapi, "create_promise")
    answer["properties"].pop("ok")
    answer["properties"] = {"status": {"type": "string", "enum": ["recorded", "rejected"],
                                       "description": "`recorded` when the promise was recorded, else `rejected`."},
                            **answer["properties"]}
    set_response_description(out.openapi, "create_promise", "`status` says whether the promise was recorded.")
    return out


def renamed_clock(v: Variant, name: str = "call_started_at", example: str = "2026-11-04T14:30") -> Variant:
    """The call's current date and time arrives under another name than ``now``."""
    out = v.copy(f"clock_{name}")
    for variable in out.ir["call_variables"]:
        if variable["name"] == "now":
            variable.update(name=name, meaning="The date and time the call started. All date rules use it.",
                            example=example)
    return out


def percent_floor(v: Variant) -> Variant:
    """The card floor is 25 % of the overdue amount, rounded up to the cent (no floor field for cards)."""
    out = v.copy("percent_floor")
    amounts = out.ir["amounts"]
    amounts["floor_field_by_type"].pop("card")
    amounts["floor_rule_by_type"] = {"card": {"percent_of_amount": 25, "rounding": "up", "label": "minimum payment"}}
    add_rule(out, "X-FL-1", "For cards the floor is 25% of the overdue amount, rounded up to the cent.", "amount",
             ["negotiation"])
    amounts["rule_ids"].append("X-FL-1")
    return out


def three_products(v: Variant) -> Variant:
    """Up to three products, each with its own lateness table: cards use a shorter table than loans and vehicle finance."""
    out = v.copy("three_products")
    ir = out.ir
    ir["multi_product"]["max_products"] = 3
    loans = [dict(b, product_types=["loan", "vehicle_finance"]) for b in ir["bands"]]
    cards = [
        {"rule_ids": ["X-BAND-C"], "name": "card_early", "days_late_min": 1, "days_late_max": 30,
         "deadline": {"kind": "nth_business_day_after_today", "n": 8, "custom_rule_text": ""},
         "persuasion_attempts": 1, "product_types": ["card"]},
        {"rule_ids": ["X-BAND-C"], "name": "card_late", "days_late_min": 31, "days_late_max": 60,
         "deadline": {"kind": "nth_business_day_after_today", "n": 2, "custom_rule_text": ""},
         "persuasion_attempts": 2, "product_types": ["card"]},
    ]
    ir["bands"] = loans + cards
    for step in ir["persuasion"]["steps"]:
        if step["bands"]:
            step["bands"] = sorted({*step["bands"], *(["card_late"] if step["attempt"] == 2 else [])})
    add_rule(out, "X-BAND-C", "Cards 1 to 30 days overdue: deadline the 8th business day after today, one attempt. "
             "Cards 31 to 60 days overdue: deadline the 2nd business day after today, two attempts.", "deadline",
             ["negotiation"])
    return out


def other_calendar(v: Variant) -> Variant:
    """A Sunday-to-Thursday business week, other holidays, and deadlines in calendar days / at the end of the month."""
    out = v.copy("other_calendar")
    ir = out.ir
    ir["calendar"]["business_weekdays"] = [6, 0, 1, 2, 3]
    ir["calendar"]["holidays"] = ["2031-03-04", "2031-03-11", "2031-04-01"]
    ir["bands"][0]["deadline"] = {"kind": "end_of_month", "custom_rule_text": ""}
    ir["bands"][1]["deadline"] = {"kind": "nth_calendar_day_after_today", "n": 9, "custom_rule_text": ""}
    rule(out, "D1-BUSINESS")["quote"] = rule(out, "D1-BUSINESS")["statement"] = (
        "Business days are Sunday to Thursday, except holidays: 2031-03-04, 2031-03-11, 2031-04-01.")
    for rid, text in (("D1-18a", "1 to 10 | green | the last business day of the month | 1"),
                      ("D1-18b", "11 to 30 | amber | the 9th calendar day after today | 2")):
        rule(out, rid)["quote"] = rule(out, rid)["statement"] = text
    return out


def extra_outcomes(v: Variant) -> Variant:
    """The outcome table gains outcomes: a bereavement outcome, and a callback the customer asks for after verification."""
    out = v.copy("extra_outcomes")
    close = request_schema(out.openapi, "close_call")
    close["properties"]["outcome"]["enum"] += ["bereavement", "call_back"]
    ir = out.ir
    ir["third_party"]["deceased"] = {"outcome": "bereavement", "reason": "notified"}
    for row in ir["closing"]["rows"]:
        if row["situation"] == "deceased":
            row.update(outcome="bereavement", reason="notified")
    ir["intents"].append({
        "rule_ids": ["X-CB-1"], "key": "callback_request",
        "description": "The verified customer asks to be called back at another time to talk about the payment.",
        "examples": [], "scope": "after_verification", "response": "Book the callback inside the callback window.",
        "actions": [{"tool": "schedule_callback", "params": {}}], "outcome": {"outcome": "call_back",
                                                                            "reason": "customer_request"},
        "precedence": 50, "overrides": [], "offer": "", "min_occurrences": 1})
    ir["closing"]["rows"].append({"outcome": "call_back", "reason": "customer_request",
                                  "situation": "callback_request", "when": "The verified customer asked for a callback.",
                                  "rule_ids": ["X-CB-1"], "requires_verified": True})
    ir["closing"]["precedence"].insert(ir["closing"]["precedence"].index("dispute"), "callback_request")
    add_rule(out, "X-CB-1", "If the verified customer asks to be called back, book a callback and close as "
             "`call_back` / `customer_request`.", "closing", ["negotiation", "closing"])
    add_rule(out, "X-DEC-1", "If the customer has died, close as `bereavement` / `notified`.", "closing",
             ["third_party", "closing"])
    ir["closing"]["rule_ids"] += ["X-CB-1", "X-DEC-1"]
    return out


def decline_max(v: Variant, limit: int) -> Variant:
    """The customer may decline the security questions ``limit`` times before the call closes."""
    out = v.copy(f"decline_max_{limit}")
    out.ir["identity"]["decline_max"] = limit
    for intent in out.ir["intents"]:
        if intent["key"] == "decline_verification":
            intent["min_occurrences"] = limit
    return out


def callback_window(v: Variant) -> Variant:
    """Callbacks only on the next business day, 08:30 to 12:00 with both ends excluded."""
    out = v.copy("callback_window")
    callback = out.ir["third_party"]["callback"]
    callback.update(allowed_days="next_business_day", window_start="08:30", window_end="12:00", inclusive=False,
                    custom_rule_text="")
    rule(out, "D1-6a")["quote"] = rule(out, "D1-6a")["statement"] = (
        "A callback is for the next business day, after 08:30 and before 12:00, and later than `now`.")
    return out


def amended_rule(v: Variant) -> Variant:
    """A later memo replaces the floor-offer rule (offer the floor twice) and narrows the note rule."""
    out = v.copy("amended_rule")
    add_rule(out, "M-1", "From now on the floor may be offered twice before it counts as a refusal.", "amount",
             ["negotiation"], supersedes=["D1-26a"])
    rule(out, "D1-26a")["superseded_by"] = "M-1"
    out.ledger["supersessions"] = [*out.ledger.get("supersessions", []), "M-1 supersedes D1-26a"]
    add_rule(out, "M-2", "A note for a vehicle finance product also states the vehicle's registration if the customer "
             "gives it.", "multi_product", ["negotiation"])
    rule(out, "D1-52")["narrowed_by"] = ["M-2"]
    amounts = out.ir["amounts"]
    amounts["floor_offer_times"] = 2
    _replace_citation(out.ir, "D1-26a", "M-1")  # M-2 has no structured field: the pipeline keeps it as a residual rule
    return out
