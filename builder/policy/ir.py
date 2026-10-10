"""Generic policy Intermediate Representation (IR) and the rule ledger it is traced to.

This module is the contract between *understanding* a brief and *compiling* a phone agent:

* ``understand/ledger.py`` reads every brief file and produces a :class:`RuleLedger`: one :class:`RuleEntry` per
  atomic rule, with a verbatim quote, the agent stages it applies to and any supersession by a later document
  (a memo or a second brief that replaces a rule).
* ``understand/extract.py`` turns the ledger into a :class:`PolicyIR`: the structured, computable part of the policy
  (identity questions, calendar, deadline bands, amount floors, persuasion ladder, intents, outcome table, ...).
  Every section carries ``rule_ids`` that point back into the ledger, so coverage can be audited and the compiler can
  place each rule's verbatim text in the node that enforces it. Whatever does not fit a structured field goes to
  ``residual_rules``.
* ``compiler/`` reads the IR only. Code-node snippets mirror the reference implementations in
  :mod:`builder.policy.calendar`, :mod:`builder.policy.money` and :mod:`builder.policy.outcomes`.

Design rules:

* **Generic.** Nothing here names a bank, a product, a holiday or an outcome of a particular brief. Hidden briefs
  change identity questions, limits, deadlines, calendars, outcome tables and tool enums; every such value is IR
  *data*. Outcome and reason names are free strings checked later against the tool API's ``close_call`` enum.
* **LLM friendly.** Models forbid unknown keys (``extra="forbid"``) and avoid numeric/length constraints in the JSON
  schema, so the schema works with structured-output APIs. Only cheap structural checks that keep the reference
  functions total (e.g. a deadline rule that names its ``n``) live in validators.
* **Pure.** No I/O. Fixtures for the kit briefs live in ``tests/fixtures/policy/``.

Placeholders in fixed sentences use single braces (``{title}``, ``{last_name}``, ``{account_suffix}``); the compiler maps
them to Freya ``{{var}}`` references.
"""

from __future__ import annotations

import datetime as _dt
import re
from collections.abc import Iterator, Mapping
from typing import Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [
    "RULE_CATEGORIES",
    "STAGE_SCOPES",
    "AllowedCallbackDays",
    "Ambiguity",
    "AmountRules",
    "Band",
    "Calendar",
    "CallVariable",
    "CallbackRule",
    "Closing",
    "Confirmation",
    "DeadlineKind",
    "DeadlineRule",
    "Disclosure",
    "HintRule",
    "Identity",
    "IdentityQuestion",
    "IntentRule",
    "IntentScope",
    "MultiProduct",
    "OutcomeRef",
    "OutcomeRow",
    "Persuasion",
    "PersuasionStep",
    "Phrases",
    "PolicyIR",
    "PromiseDateRules",
    "ProposalDateKind",
    "Recording",
    "ResidualRule",
    "RightParty",
    "RuleCategory",
    "RuleEntry",
    "RuleLedger",
    "StageScope",
    "ThirdParty",
    "ToolAction",
    "fill_template",
    "template_fields",
]

# --------------------------------------------------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------------------------------------------------

RuleCategory = Literal[
    "opening",
    "right_party",
    "third_party",
    "callback",
    "identity",
    "disclosure",
    "calendar",
    "deadline",
    "promise_date",
    "amount",
    "persuasion",
    "intent",
    "confirmation",
    "recording",
    "multi_product",
    "closing",
    "conduct",
    "other",
]
"""What a ledger rule is about. ``conduct`` = general behaviour (style, never-do lists); ``other`` = anything else."""

StageScope = Literal[
    "global",
    "opening",
    "third_party",
    "identity",
    "disclosure",
    "negotiation",
    "confirmation",
    "recording",
    "closing",
]
"""Agent stages a rule applies to. ``global`` means every stage (system prompt level)."""

RULE_CATEGORIES: tuple[str, ...] = get_args(RuleCategory)
STAGE_SCOPES: tuple[str, ...] = get_args(StageScope)
PHRASE_EXTRA_KEYS: tuple[str, ...] = (
    "disclosure_intro",
    "today_question_all",
    "floor_offer",
    "below_floor",
    "unknown_answer",
    "decline_explanation",
)
"""Keys of ``Phrases.extra`` that the compiler reads (the extraction prompt lists them)."""

IntentScope = Literal["any", "before_verification", "after_verification"]
AllowedCallbackDays = Literal["today_or_next_business_day", "next_business_day", "any_business_day", "custom"]
DeadlineKind = Literal["nth_business_day_after_today", "nth_calendar_day_after_today", "end_of_month", "fixed_date",
                       "custom"]
ProposalDateKind = Literal[
    "today",
    "last_business_day_this_week",
    "last_business_day_next_week_if_today_is_last",
    "deadline",
    "customer",
    "next_business_day_after_today",
    "nth_business_day_after_today",
    "nth_calendar_day_after_today",
    "custom",
]
"""The date a persuasion attempt proposes. ``customer`` = the customer names it; ``custom`` = a rule no other kind
expresses, applied from the step's ``text``. The ``nth_*`` kinds need the step's ``n``."""
PROPOSAL_KINDS_WITH_N: tuple[str, ...] = ("nth_business_day_after_today", "nth_calendar_day_after_today")
PROPOSAL_KINDS_FROM_TEXT: tuple[str, ...] = ("customer", "custom")

_HHMM = re.compile(r"^\s*(\d{1,2})[:.h](\d{2})\s*$")
# Single-brace ``{field}`` is the IR convention. Double braces ``{{var}}`` are Freya runtime variables and must be left
# alone, so a placeholder may not touch another brace on either side.
_PLACEHOLDER = re.compile(r"(?<!\{)\{([A-Za-z_][A-Za-z0-9_]*)\}(?!\})")


def template_fields(text: str) -> list[str]:
    """Return the single-brace placeholder names used in ``text``, in order of first appearance.

    Double-brace ``{{name}}`` tokens are Freya runtime variables, not IR placeholders, and are not reported.

    >>> template_fields("Am I speaking with {title} {last_name}?")
    ['title', 'last_name']
    >>> template_fields("keep {{runtime_var}} out")
    []
    """
    seen: list[str] = []
    for name in _PLACEHOLDER.findall(text or ""):
        if name not in seen:
            seen.append(name)
    return seen


def fill_template(text: str, values: Mapping[str, Any]) -> str:
    """Replace ``{name}`` placeholders with ``values[name]``; unknown placeholders are left untouched.

    Used for product naming templates (``"loan ending {account_suffix}"``) and fixed sentences in tests and in
    reference computations. Never raises on missing keys.
    """

    def _sub(match: re.Match[str]) -> str:
        key = match.group(1)
        return str(values[key]) if key in values and values[key] is not None else match.group(0)

    return _PLACEHOLDER.sub(_sub, text or "")


class _IRModel(BaseModel):
    """Base for every IR model: unknown keys are rejected so typos in fixtures and LLM output surface early."""

    model_config = ConfigDict(extra="forbid")


class _Traced(_IRModel):
    """A section of the IR that cites the ledger rules it was derived from."""

    rule_ids: list[str] = Field(default_factory=list, description="Ledger rule ids this section implements.")


# --------------------------------------------------------------------------------------------------------------------
# Rule ledger
# --------------------------------------------------------------------------------------------------------------------


class RuleEntry(_IRModel):
    """One atomic rule of a brief, with its verbatim quote.

    Numbered brief rules that hold several obligations are split into sub-ids (for example ``A-26a``, ``A-26b``).
    ``supersedes`` / ``superseded_by`` record an amending document replacing a rule; superseded rules stay in the
    ledger (for the audit trail) but must not be used by the IR. ``narrowed_by`` records an amendment that changes a
    rule only for some cases (for example "when the customer has several products"): the rule stays active, and the
    amending rule says where it no longer applies.
    """

    id: str = Field(description="Stable rule id, e.g. 'A-12' or 'A-26b'.")
    source: str = Field(description="File the quote comes from (name or repo-relative path).")
    quote: str = Field(description="Verbatim text of the rule as written in the source.")
    category: RuleCategory
    statement: str = Field(description="One imperative sentence: what the agent must or must not do.")
    scope: list[StageScope] = Field(description="Agent stages the rule applies to.")
    supersedes: list[str] = Field(default_factory=list, description="Ids of earlier rules this rule replaces.")
    superseded_by: str | None = Field(default=None, description="Id of the later rule that replaces this one.")
    narrowed_by: list[str] | None = Field(
        default=None,
        description="Ids of later rules that replace this rule only for some cases (it stays in force for the rest).",
    )

    @property
    def active(self) -> bool:
        """True when no later rule replaces this one."""
        return self.superseded_by is None


class Ambiguity(_IRModel):
    """An unclear point of the brief and the interpretation the builder chose (written to the trace)."""

    id: str
    rule_ids: list[str]
    question: str
    options: list[str]
    chosen: str
    rationale: str


class RuleLedger(_IRModel):
    """Every atomic rule of the input briefs, plus the ambiguity register and the supersession log."""

    rules: list[RuleEntry]
    ambiguities: list[Ambiguity] = Field(default_factory=list)
    supersessions: list[str] = Field(
        default_factory=list, description="Human-readable log of replacements and amendments between documents."
    )
    source_files: list[str] = Field(default_factory=list)

    def ids(self) -> list[str]:
        """All rule ids, in ledger order."""
        return [rule.id for rule in self.rules]

    def get(self, rule_id: str) -> RuleEntry | None:
        """The rule with ``rule_id`` (first one if duplicated), or ``None``."""
        return next((rule for rule in self.rules if rule.id == rule_id), None)

    def active_rules(self) -> list[RuleEntry]:
        """Rules not replaced by a later document, after reconciling both supersession directions."""
        superseded = self.superseded_ids()
        return [rule for rule in self.rules if rule.id not in superseded]

    def superseded_ids(self) -> set[str]:
        """Ids replaced by another rule, whether recorded as ``superseded_by`` or as another rule's ``supersedes``."""
        out = {rule.id for rule in self.rules if rule.superseded_by}
        for rule in self.rules:
            out.update(rule.supersedes)
        return out

    def for_scope(self, scope: str, *, include_global: bool = True) -> list[RuleEntry]:
        """Active rules that apply to an agent stage (and ``global`` rules unless ``include_global`` is False)."""
        wanted = {scope, "global"} if include_global else {scope}
        return [rule for rule in self.active_rules() if wanted.intersection(rule.scope)]

    def duplicate_ids(self) -> list[str]:
        """Ids that occur more than once (a ledger defect worth reporting)."""
        seen: set[str] = set()
        dupes: list[str] = []
        for rule_id in self.ids():
            if rule_id in seen and rule_id not in dupes:
                dupes.append(rule_id)
            seen.add(rule_id)
        return dupes

    def with_consistent_supersessions(self) -> RuleLedger:
        """Return a copy where ``supersedes`` and ``superseded_by`` agree in both directions.

        An LLM often fills only one side; this makes ``superseded_by`` authoritative for consumers that read it.
        """
        copies = [rule.model_copy(deep=True) for rule in self.rules]
        by_id: dict[str, RuleEntry] = {}
        for rule in copies:
            by_id.setdefault(rule.id, rule)
        for rule in copies:
            for old_id in rule.supersedes:
                old = by_id.get(old_id)
                if old is not None and old.superseded_by is None:
                    old.superseded_by = rule.id
        for rule in copies:
            newer = by_id.get(rule.superseded_by) if rule.superseded_by else None
            if newer is not None and rule.id not in newer.supersedes:
                newer.supersedes.append(rule.id)
        return self.model_copy(update={"rules": copies})


# --------------------------------------------------------------------------------------------------------------------
# Small shared models
# --------------------------------------------------------------------------------------------------------------------


class OutcomeRef(_IRModel):
    """An outcome (``close_call.outcome`` value) with an optional reason. Names come from the brief's table."""

    outcome: str
    reason: str | None = None

    def as_tuple(self) -> tuple[str, str | None]:
        """``(outcome, reason)``, the shape the kit scorer compares."""
        return (self.outcome, self.reason)


class CallVariable(_IRModel):
    """A variable every call starts with (for example the customer id or the current date and time)."""

    name: str
    meaning: str
    example: str = ""


class ToolAction(_IRModel):
    """A tool call an intent triggers, with fixed parameters (for example ``queue`` of ``transfer_to_agent``)."""

    tool: str
    params: dict[str, str] = Field(default_factory=dict)


# --------------------------------------------------------------------------------------------------------------------
# Sections
# --------------------------------------------------------------------------------------------------------------------


class Phrases(_Traced):
    """Fixed sentences the brief prescribes. Empty string = the brief prescribes none.

    ``extra`` holds any other prescribed sentence by a descriptive key (for example ``"unknown_answer"``). Keys the
    compiler reads (see :data:`PHRASE_EXTRA_KEYS`): ``disclosure_intro`` (the whole sentence that names the caller, said
    before the debt, when ``disclosure.team_phrase`` cannot express it), ``today_question_all`` (the first ask when the
    customer has several late products; ``today_question`` is then the single-product wording), ``floor_offer`` (what to
    say when the customer finds the full amount too much; ``{floor_label}`` and ``{floor_amount}`` are filled in) and
    ``below_floor`` (what to say to an amount under the floor).
    """

    opening_sentence: str
    right_party_question: str
    robot_answer: str = ""
    scam_answer: str = ""
    confirm_question: str = ""
    today_question: str = ""
    recorded_sentence: str = ""
    identity_failed_sentence: str = ""
    deceased_sentence: str = ""
    paid_sentence: str = ""
    goodbye_sentence: str = ""
    extra: dict[str, str] = Field(
        default_factory=dict,
        description="Other prescribed sentences by key. Keys the compiler uses: disclosure_intro, today_question_all, "
        "floor_offer (placeholders {floor_label} {floor_amount}), below_floor, unknown_answer, decline_explanation.",
    )


class RightParty(_Traced):
    """How the agent establishes that the intended customer is on the line."""

    confirm_examples: list[str]
    not_confirm_examples: list[str]
    max_asks: int = 2
    unclear_outcome: OutcomeRef | None = None


class CallbackRule(_Traced):
    """When a callback may be booked for a customer who is not on the line.

    ``window_start``/``window_end`` are ``HH:MM``. With ``inclusive`` both bounds are allowed; without it the end bound
    is excluded. ``must_be_after_now`` means a slot today must be strictly later than the current time.
    """

    allowed_days: AllowedCallbackDays
    window_start: str
    window_end: str
    inclusive: bool = True
    must_be_after_now: bool = True
    max_extra_asks: int = 1
    success: OutcomeRef
    failure: OutcomeRef
    custom_rule_text: str = ""

    @field_validator("window_start", "window_end")
    @classmethod
    def _normalise_hhmm(cls, value: str) -> str:
        match = _HHMM.match(value or "")
        if not match:
            raise ValueError(f"time must be HH:MM, got {value!r}")
        hours, minutes = int(match.group(1)), int(match.group(2))
        if hours > 24 or minutes > 59 or (hours == 24 and minutes):
            raise ValueError(f"time out of range: {value!r}")
        return f"{hours:02d}:{minutes:02d}"

    @model_validator(mode="after")
    def _window_ordered(self) -> CallbackRule:
        if self.window_start > self.window_end:
            raise ValueError(f"callback window starts after it ends: {self.window_start} > {self.window_end}")
        return self


class ThirdParty(_Traced):
    """Behaviour when someone other than the customer answers."""

    disclose_nothing: bool = True
    ask_when_reachable: bool = True
    callback: CallbackRule | None = None
    wrong_number: OutcomeRef | None = None
    deceased: OutcomeRef | None = None
    notes: list[str] = Field(default_factory=list)


class IdentityQuestion(_IRModel):
    """One security question. ``field`` is a value of the ``verify_identity`` request's ``field`` enum."""

    field: str
    ask: str
    answer_format: Literal["date_iso", "digits", "year", "text", "postcode"]
    digits: int | None = None
    normalisation: str = ""


class HintRule(_IRModel):
    """What to do when ``verify_identity`` returns a given ``hint`` value."""

    value: str
    instruction: str
    counts_as_wrong: bool = False


class Identity(_Traced):
    """Identity verification: ordered questions, failure limit, hints, failure and decline handling."""

    questions: list[IdentityQuestion]
    one_at_a_time: bool = True
    max_wrong_total: int
    hints: list[HintRule] = Field(default_factory=list)
    on_failed: OutcomeRef
    failed_instruction: str
    decline_explanation: str
    decline_max: int = 2
    on_declined: OutcomeRef

    def question(self, field: str) -> IdentityQuestion | None:
        """The question that sends ``field`` to ``verify_identity``, or ``None``."""
        return next((q for q in self.questions if q.field == field), None)


_CALLING_FROM = re.compile(
    r"^\s*(?:i\s*am|i['\u2019]m|we\s*are|we['\u2019]re)\s+calling\s+(?:you\s+)?(?:on\s+behalf\s+of|from)\s+", re.IGNORECASE
)
_TRAILING_PUNCTUATION = ".!?;:,"
_SENTENCE_OPENER = re.compile(
    r"^\s*(?:this\s+(?:is|was)|we\s+(?:are|have|would)|we['\u2019]re|i\s+(?:am|have|would)|i['\u2019](?:m|ve)|you\s+(?:are|have)|"
    r"you['\u2019]re|my\s+name|it\s+(?:is|was)|it['\u2019]s|hello\b|hi\b|good\s+(?:morning|afternoon|evening))",
    re.IGNORECASE,
)


def normalise_team_phrase(text: str) -> str:
    """The words that complete "I am calling from ___": a leading "I am calling from" and the final punctuation go.

    >>> normalise_team_phrase("I am calling from the collections team about a late payment.")
    'the collections team about a late payment'
    """
    cleaned = _CALLING_FROM.sub("", " ".join((text or "").split()), count=1)
    return cleaned.strip().rstrip(_TRAILING_PUNCTUATION).strip()


def looks_like_sentence(text: str) -> bool:
    """True when ``text`` reads as a whole sentence ("This is ...", "We are ...", two sentences) rather than a noun phrase."""
    flat = " ".join((text or "").split())
    return bool(_SENTENCE_OPENER.match(flat) or re.search(r"[.!?]\s+\S", flat))


def normalise_product_name(text: str) -> str:
    """A product's spoken name as a bare noun phrase: whitespace collapsed, final punctuation removed."""
    return " ".join((text or "").split()).rstrip(_TRAILING_PUNCTUATION).strip()


class Disclosure(_Traced):
    """How the debt is stated once identity is verified.

    The compiled agent says: "Thank you. I am calling from ``team_phrase``. Your ``<product name>`` has a past-due amount
    of ... and is ... days late. ``<today question>``" - so the fields hold *parts of that sentence*, never whole
    sentences:

    * ``team_phrase`` completes "I am calling from ___" (``the collections team``): no leading "I am calling from", no
      verb, no final full stop. A whole quoted sentence goes to ``phrases.extra["disclosure_intro"]`` instead.
    * ``product_naming`` maps a product ``type`` (``get_account`` enum value) to a bare noun phrase that may use product
      fields in braces (``loan ending {account_suffix}``): no verb, no punctuation, and no amount or days-late field
      (the statement adds those itself).
    * ``state_fields`` are the product fields the statement mentions (the past-due amount, the days late ...).
    * ``amount_field`` / ``days_field`` name the product fields that hold the past-due amount and the days late. They
      drive the band and the deadline of every product, so they are set explicitly whenever the tool API's field names
      are not self-evident; left empty they are read from ``state_fields`` (see :meth:`amount_and_days_fields`).
    """

    team_phrase: str = Field(
        description='The words that complete "I am calling from ___", for example "the collections team". No leading '
        '"I am calling from", no verb, no final full stop.'
    )
    product_naming: dict[str, str] = Field(
        description="Product type -> spoken name as a bare noun phrase (no verb, no punctuation, no amount or days "
        "field), e.g. 'loan ending {account_suffix}'."
    )
    state_fields: list[str]
    amount_field: str | None = Field(
        default=None,
        description="get_account product field that holds the past-due amount; omitted = read from state_fields.",
    )
    days_field: str | None = Field(
        default=None,
        description="get_account product field that holds the days late; omitted = read from state_fields.",
    )

    @field_validator("team_phrase")
    @classmethod
    def _team_phrase_completes_the_sentence(cls, value: str) -> str:
        return normalise_team_phrase(value)

    @field_validator("product_naming")
    @classmethod
    def _product_names_are_noun_phrases(cls, value: dict[str, str]) -> dict[str, str]:
        return {key: normalise_product_name(name) for key, name in value.items()}

    def amount_and_days_fields(self) -> tuple[str, str]:
        """``(amount field, days-late field)`` of the products: the explicit fields, else read from ``state_fields``.

        Without an explicit field the amount is the first state field whose name mentions neither "late" nor "day",
        and the days field is the first one that mentions "day". An empty string means "cannot tell": the validator
        reports it (``AMOUNT_FIELD_MISSING`` / ``DAYS_FIELD_MISSING``) and the safe fixes fill the fields in from the
        tool API, which is the authority on field names.
        """
        names = list(self.state_fields)
        amount = (self.amount_field or "").strip() or next((n for n in names if "late" not in n and "day" not in n), "")
        days = (self.days_field or "").strip() or next((n for n in names if "day" in n), "")
        return amount, days

    def product_name(self, product: Mapping[str, Any]) -> str:
        """Spoken name of a ``get_account`` product, falling back to its type with spaces."""
        product_type = str(product.get("type", ""))
        template = self.product_naming.get(product_type)
        if template is None:
            return product_type.replace("_", " ")
        return fill_template(template, product)


class Calendar(_Traced):
    """Business-day calendar. Weekdays use Python numbering (Monday = 0 ... Sunday = 6)."""

    business_weekdays: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4])
    holidays: list[_dt.date]
    date_input_order: Literal["DMY", "MDY", "YMD"] = "DMY"
    notes: list[str] = Field(default_factory=list)

    @field_validator("business_weekdays")
    @classmethod
    def _valid_weekdays(cls, value: list[int]) -> list[int]:
        cleaned = sorted(set(value))
        if not cleaned:
            raise ValueError("a calendar needs at least one business weekday")
        if any(day < 0 or day > 6 for day in cleaned):
            raise ValueError(f"business weekdays must be 0..6 (Monday=0), got {value}")
        return cleaned

    @field_validator("holidays")
    @classmethod
    def _sorted_holidays(cls, value: list[_dt.date]) -> list[_dt.date]:
        return sorted(set(value))


class DeadlineRule(_IRModel):
    """How the latest allowed promise date is computed from today's date.

    * ``nth_business_day_after_today``: the n-th business day strictly after today (today never counts).
    * ``nth_calendar_day_after_today``: today + n calendar days.
    * ``end_of_month``: the last calendar day of today's month.
    * ``fixed_date``: ``fixed``.
    * ``custom``: not computable; ``custom_rule_text`` holds the brief's wording for the prompt.
    """

    kind: DeadlineKind
    n: int | None = None
    fixed: _dt.date | None = None
    custom_rule_text: str = ""

    @model_validator(mode="after")
    def _parameters_present(self) -> DeadlineRule:
        counts_days = self.kind in ("nth_business_day_after_today", "nth_calendar_day_after_today")
        if counts_days and (self.n is None or self.n < 0):
            raise ValueError(f"deadline kind {self.kind} needs a non-negative n")
        if self.kind == "fixed_date" and self.fixed is None:
            raise ValueError("deadline kind fixed_date needs 'fixed'")
        return self


class Band(_Traced):
    """A lateness band: inclusive ``days_late`` range, its deadline rule and number of persuasion attempts."""

    name: str
    days_late_min: int
    days_late_max: int
    deadline: DeadlineRule
    persuasion_attempts: int

    @model_validator(mode="after")
    def _range_ordered(self) -> Band:
        if self.days_late_min > self.days_late_max:
            raise ValueError(f"band {self.name!r}: days_late_min > days_late_max")
        return self

    def contains(self, days_late: int) -> bool:
        """True when ``days_late`` falls inside this band (bounds included)."""
        return self.days_late_min <= days_late <= self.days_late_max


class PromiseDateRules(_Traced):
    """Which promise dates are valid and how invalid ones are handled (see ``calendar.check_promise_date``)."""

    allow_today: bool = True
    business_days_only: bool = True
    not_after_deadline: bool = True
    non_business_day_shift: Literal["next_business_day", "previous_business_day", "reject"] = "next_business_day"
    vague_period: Literal["ask_once_then_deadline", "ask_once_then_refusal", "reject"] = "ask_once_then_deadline"
    next_weekday_semantics: Literal["following_calendar_week", "next_occurrence"] = "following_calendar_week"
    after_deadline: Literal["offer_deadline", "reject"] = "offer_deadline"
    refuse_deadline_is_refusal: bool = True


class AmountRules(_Traced):
    """Which amounts the agent proposes and accepts.

    ``floor_field_by_type`` maps a product type to the ``get_account`` field holding its minimum acceptable amount.
    """

    ask_full_first: bool = True
    never_ask_open_amount: bool = True
    floor_field_by_type: dict[str, str]
    floor_offer_times: int = 1
    floor_note: str = ""
    accept_between_floor_and_full: bool = True
    below_floor: Literal["offer_floor_once", "reject"] = "offer_floor_once"

    def floor_field(self, product_type: str) -> str | None:
        """Name of the floor field for a product type, or ``None`` when the brief defines no floor for it."""
        return self.floor_field_by_type.get(product_type)


class PersuasionStep(_IRModel):
    """One persuasion attempt after a refusal.

    ``bands`` empty = all bands. ``amount``: ``full`` past-due amount, ``floor`` amount, or ``current`` (the amount
    under discussion: full, or the floor when the customer already said the full amount is too much).
    ``date`` kinds are resolved by ``calendar.resolve_proposal_date``:

    * ``today`` - today, or the next business day when today is not one;
    * ``last_business_day_this_week`` / ``last_business_day_next_week_if_today_is_last`` - see the calendar module;
    * ``deadline`` - the product's deadline;
    * ``next_business_day_after_today`` - the first business day strictly after today ("tomorrow, or the next business
      day");
    * ``nth_business_day_after_today`` / ``nth_calendar_day_after_today`` - the ``n``-th such day after today;
    * ``customer`` - the customer names the date (the step's ``text`` says within which limits);
    * ``custom`` - a date rule none of the kinds expresses; ``text`` holds it in the documents' words.

    ``text`` is the instruction in the documents' words. For ``customer`` and ``custom`` it is what the compiled agent
    follows, so it must carry the whole date rule.
    """

    attempt: int
    bands: list[str] = Field(default_factory=list)
    amount: Literal["full", "floor", "current"]
    date: ProposalDateKind
    n: int | None = Field(default=None, description="How many days, for the nth_* date kinds; omitted otherwise.")
    text: str = ""

    @model_validator(mode="after")
    def _parameters_present(self) -> PersuasionStep:
        if self.date in PROPOSAL_KINDS_WITH_N and (self.n is None or self.n < 0):
            raise ValueError(f"persuasion date kind {self.date} needs a non-negative n")
        return self

    def applies_to(self, band: str) -> bool:
        """True when this step is used for products in ``band``."""
        return not self.bands or band in self.bands


class Persuasion(_Traced):
    """The first ask and the persuasion ladder used after flat refusals."""

    first_ask: str
    non_answer_counts: bool = True
    steps: list[PersuasionStep]
    stop_outcome: OutcomeRef

    def steps_for(self, band: str, max_attempts: int | None = None) -> list[PersuasionStep]:
        """Steps that apply to ``band``, ordered by attempt, capped at the band's number of attempts."""
        steps = sorted((s for s in self.steps if s.applies_to(band)), key=lambda s: s.attempt)
        return steps if max_attempts is None else steps[:max_attempts]


class IntentRule(_Traced):
    """How the agent handles one customer intent (catalog key from ``intents.py`` or a brief-defined key).

    * ``response``: what to say/do first (brief wording, verbatim where the brief prescribes it).
    * ``offer``: an offer or question made before acting (e.g. "offer the support team", "ask once whether you can
      help first"). When set, ``actions`` and ``outcome`` apply only if the customer accepts or insists, and
      ``declined_outcome`` applies when they decline (``None`` = continue the call).
    * ``actions``/``outcome``: tool calls to make and the close outcome; ``outcome`` ``None`` = the call continues.
    * ``note_reason``: per-product note reason (multi-product briefs) recorded for this intent.
    * ``precedence``: higher wins when several intents are detected together; ``overrides`` lists intent keys this
      one explicitly beats.
    * ``min_occurrences``: how many times the intent must be expressed before acting (e.g. "says twice").
    """

    key: str
    description: str
    examples: list[str] = Field(default_factory=list)
    scope: IntentScope = "any"
    response: str = ""
    actions: list[ToolAction] = Field(default_factory=list)
    outcome: OutcomeRef | None = None
    note_reason: str | None = None
    precedence: int = 50
    overrides: list[str] = Field(default_factory=list)
    offer: str = ""
    declined_outcome: OutcomeRef | None = None
    min_occurrences: int = 1

    def applies(self, verified: bool) -> bool:
        """True when this rule is in force at the given verification state."""
        if self.scope == "any":
            return True
        return verified if self.scope == "after_verification" else not verified

    def transfer_queue(self, tool: str = "transfer_to_agent") -> str | None:
        """The queue of the first transfer action, or ``None`` when the intent does not transfer."""
        for action in self.actions:
            if action.tool == tool and "queue" in action.params:
                return action.params["queue"]
        return None


class Confirmation(_Traced):
    """Read-back and confirmation before recording a promise."""

    readback_fields: list[str] = Field(default_factory=lambda: ["product", "amount", "date"])
    question: str
    on_change: str = "recheck_and_readback"
    single_confirmation_for_all_products: bool = False


class Recording(_Traced):
    """Promise recording: limits, retries and the honesty sentences around it."""

    one_promise_per_product: bool = True
    retries_on_failure: int = 1
    failure_outcome: OutcomeRef
    failure_sentence: str = ""
    success_sentence: str = ""
    immutable_sentence: str = ""


class MultiProduct(_Traced):
    """Handling of customers late on several products, including per-product notes and the notes closing rule.

    ``closing_reason_rule``:

    * ``first_non_paid_note``: with no promise recorded, close with ``all_paid_outcome`` when every product is noted
      ``paid_note_reason``; otherwise close with ``notes_outcome`` (or the outcome-table row whose reason equals the
      note reason) and the reason of the first product, in ``get_account`` order, whose note is not the paid reason.
    * ``fixed``: notes do not pick the reason; the outcome-table row keyed ``product_notes`` applies.
    """

    enabled: bool = False
    max_products: int = 1
    state_all_before_asking: bool = True
    order: Literal["as_returned", "custom"] = "as_returned"
    ask_all_today_first: bool = True
    per_product_attempts: bool = True
    one_date_after_deadline: Literal["use_product_deadline", "reject"] = "use_product_deadline"
    note_tool: str = "add_note"
    note_reasons: list[str] = Field(default_factory=list)
    notes_on_transfer: bool = False
    closing_reason_rule: Literal["first_non_paid_note", "fixed"] = "first_non_paid_note"
    all_paid_outcome: OutcomeRef | None = None
    paid_note_reason: str | None = Field(
        default=None, description="The note reason that means 'already paid' (used by the all-paid rule)."
    )
    notes_outcome: str | None = Field(
        default=None, description="Outcome used with the first non-paid note's reason when notes decide the close."
    )


class OutcomeRow(_IRModel):
    """One row of the brief's outcome table, keyed by a situation (see ``outcomes.py`` for the generic keys).

    ``requires_verified`` restricts the row to calls where identity was (``True``) or was not (``False``) verified;
    ``None`` = any. It lets one situation (e.g. a refusal to talk) map to different rows before/after verification.
    """

    outcome: str
    reason: str | None = None
    situation: str
    when: str = ""
    rule_ids: list[str] = Field(default_factory=list)
    requires_verified: bool | None = None

    def ref(self) -> OutcomeRef:
        """The row as an :class:`OutcomeRef`."""
        return OutcomeRef(outcome=self.outcome, reason=self.reason)

    def matches(self, situation: str, verified: bool) -> bool:
        """True when this row applies to ``situation`` at the given verification state."""
        return self.situation == situation and self.requires_verified in (None, verified)


class Closing(_Traced):
    """The outcome table and how the final outcome is chosen (reference: ``outcomes.compute_outcome``).

    * ``precedence``: situation keys, strongest first.
    * ``promise_wins``: a recorded promise beats every other situation except a transfer handled by
      ``transfer_after_promise_outcome``.
    * ``transfer_after_promise_outcome``: outcome when a transfer follows a recorded promise; a ``None`` reason means
      "the transfer's own reason" (taken from the transfer's row). ``None`` = no special rule.
    """

    rows: list[OutcomeRow]
    precedence: list[str]
    promise_wins: bool = True
    transfer_after_promise_outcome: OutcomeRef | None = None
    goodbye_before_close: bool = True
    close_last_exactly_once: bool = True

    def rows_for(self, situation: str) -> list[OutcomeRow]:
        """All rows keyed by ``situation``, in table order."""
        return [row for row in self.rows if row.situation == situation]

    def row_for(self, situation: str, verified: bool) -> OutcomeRow | None:
        """The first row for ``situation`` that applies at the given verification state."""
        return next((row for row in self.rows if row.matches(situation, verified)), None)

    def resolved_precedence(self) -> list[str]:
        """``precedence`` followed by any row situation it omits (table order), without duplicates."""
        ordered: list[str] = []
        for key in [*self.precedence, *(row.situation for row in self.rows)]:
            if key not in ordered:
                ordered.append(key)
        return ordered

    def outcomes(self) -> list[str]:
        """Distinct outcome names used by the table, in table order."""
        out: list[str] = []
        for row in self.rows:
            if row.outcome not in out:
                out.append(row.outcome)
        return out


class ResidualRule(_IRModel):
    """A rule with no structured home in the IR; the compiler places its text in the scoped prompts."""

    rule_id: str
    text: str
    scope: list[StageScope] = Field(default_factory=lambda: ["global"])


# --------------------------------------------------------------------------------------------------------------------
# Root
# --------------------------------------------------------------------------------------------------------------------


class PolicyIR(_Traced):
    """The complete structured policy of one brief (or of a brief plus its amendments)."""

    bank_name: str
    language: str = "en"
    currency_word: str = Field(
        default="",
        description="Word spoken after amounts; empty when the brief names no currency (never invent one).",
    )
    call_variables: list[CallVariable]
    phrases: Phrases
    right_party: RightParty
    third_party: ThirdParty = Field(default_factory=ThirdParty)
    identity: Identity
    disclosure: Disclosure
    calendar: Calendar
    bands: list[Band]
    promise_dates: PromiseDateRules = Field(default_factory=PromiseDateRules)
    amounts: AmountRules
    persuasion: Persuasion
    intents: list[IntentRule] = Field(default_factory=list)
    confirmation: Confirmation
    recording: Recording
    multi_product: MultiProduct = Field(default_factory=MultiProduct)
    closing: Closing
    forbidden_before_verification: list[str] = Field(
        default_factory=list, description="Words and topics never said before identity is verified."
    )
    residual_rules: list[ResidualRule] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    # ---- lookups -------------------------------------------------------------------------------------------------

    def band_for(self, days_late: int) -> Band | None:
        """The band containing ``days_late``, or ``None`` when no band covers it."""
        return next((band for band in self.bands if band.contains(days_late)), None)

    def nearest_band(self, days_late: int) -> Band | None:
        """The band containing ``days_late``, else the band whose range is closest to it (``None`` without bands)."""
        exact = self.band_for(days_late)
        if exact is not None or not self.bands:
            return exact

        def distance(band: Band) -> int:
            return band.days_late_min - days_late if days_late < band.days_late_min else days_late - band.days_late_max

        return min(self.bands, key=distance)

    def intent(self, key: str, verified: bool | None = None) -> IntentRule | None:
        """The intent rule for ``key`` (optionally the one in force at a verification state)."""
        for rule in self.intents:
            if rule.key == key and (verified is None or rule.applies(verified)):
                return rule
        return None

    def call_variable_names(self) -> list[str]:
        """Names of the declared call variables."""
        return [variable.name for variable in self.call_variables]

    # ---- audit helpers -------------------------------------------------------------------------------------------

    def iter_models(self) -> Iterator[tuple[str, BaseModel]]:
        """Yield ``(path, model)`` for this IR and every nested model, depth first (paths like ``bands[0].deadline``)."""
        yield from _walk_models(self, "")

    def referenced_rule_ids(self) -> set[str]:
        """Every ledger id cited anywhere in the IR (``rule_ids`` lists and residual rules)."""
        found: set[str] = set()
        for _, model in self.iter_models():
            ids = getattr(model, "rule_ids", None)
            if isinstance(ids, list):
                found.update(ids)
            if isinstance(model, ResidualRule):
                found.add(model.rule_id)
        return found

    def outcome_refs(self) -> list[tuple[str, OutcomeRef]]:
        """Every outcome the IR can close with, as ``(path, OutcomeRef)``; outcome-table rows are included."""
        refs: list[tuple[str, OutcomeRef]] = []
        for path, model in self.iter_models():
            if isinstance(model, OutcomeRef):
                refs.append((path, model))
            elif isinstance(model, OutcomeRow):
                refs.append((path, model.ref()))
        return refs

    def to_json_dict(self) -> dict[str, Any]:
        """JSON-ready dict without ``None`` values (the form stored in builder state and fixtures)."""
        return self.model_dump(mode="json", exclude_none=True)

    # ---- example -------------------------------------------------------------------------------------------------

    @classmethod
    def example_minimal(cls) -> PolicyIR:
        """A small, valid, deliberately generic IR for tests (placeholder names, not any kit brief)."""
        not_agreed = OutcomeRef(outcome="not_agreed", reason="declined_offer")
        unverified = OutcomeRef(outcome="not_identified", reason="wrong_answers")
        unverified_declined = OutcomeRef(outcome="not_identified", reason="would_not_answer")
        unreached = OutcomeRef(outcome="not_contacted", reason="unclear")
        callback_ok = OutcomeRef(outcome="not_contacted", reason="callback_set")
        callback_none = OutcomeRef(outcome="not_contacted", reason="callback_missing")
        handover = OutcomeRef(outcome="handed_over", reason="person_requested")
        record_failed = OutcomeRef(outcome="not_agreed", reason="system_down")
        return cls(
            bank_name="Example Lender",
            call_variables=[
                CallVariable(name="customer_id", meaning="Opaque id passed to every tool call."),
                CallVariable(name="now", meaning="Current date and time.", example="2030-01-02T10:00"),
            ],
            phrases=Phrases(
                opening_sentence="Hello, this is the automated assistant of Example Lender.",
                right_party_question="Am I speaking with {first_name}?",
                confirm_question="Is that correct?",
                today_question="Could you settle this today?",
                goodbye_sentence="Thank you, goodbye.",
            ),
            right_party=RightParty(
                confirm_examples=["yes"], not_confirm_examples=["hello"], max_asks=2, unclear_outcome=unreached
            ),
            third_party=ThirdParty(
                callback=CallbackRule(
                    allowed_days="next_business_day",
                    window_start="09:00",
                    window_end="17:00",
                    success=callback_ok,
                    failure=callback_none,
                ),
            ),
            identity=Identity(
                questions=[IdentityQuestion(field="birth_year", ask="What is your year of birth?",
                                            answer_format="year", digits=4)],
                max_wrong_total=2,
                on_failed=unverified,
                failed_instruction="Do not discuss the account.",
                decline_explanation="The question protects your account.",
                on_declined=unverified_declined,
            ),
            disclosure=Disclosure(
                team_phrase="the accounts team",
                product_naming={"loan": "loan {account_suffix}"},
                state_fields=["past_due"],
            ),
            calendar=Calendar(holidays=[]),
            bands=[
                Band(name="only", days_late_min=1, days_late_max=90,
                     deadline=DeadlineRule(kind="nth_business_day_after_today", n=5), persuasion_attempts=1),
            ],
            amounts=AmountRules(floor_field_by_type={"loan": "minimum_due"}),
            persuasion=Persuasion(
                first_ask="Could you settle this today?",
                steps=[PersuasionStep(attempt=1, amount="current", date="deadline")],
                stop_outcome=not_agreed,
            ),
            intents=[
                IntentRule(
                    key="human_request",
                    description="Asks for a person.",
                    actions=[ToolAction(tool="transfer_to_agent", params={"queue": "people"})],
                    outcome=handover,
                ),
            ],
            confirmation=Confirmation(question="Is that correct?"),
            recording=Recording(failure_outcome=record_failed),
            closing=Closing(
                rows=[
                    OutcomeRow(outcome="handed_over", reason="person_requested", situation="transfer:people"),
                    OutcomeRow(outcome="agreed", situation="promise_recorded"),
                    OutcomeRow(outcome="not_agreed", reason="system_down", situation="recording_failed"),
                    OutcomeRow(outcome="not_agreed", reason="declined_offer", situation="flat_refusal"),
                    OutcomeRow(outcome="not_identified", reason="wrong_answers", situation="identity_failed"),
                    OutcomeRow(outcome="not_identified", reason="would_not_answer", situation="identity_declined"),
                    OutcomeRow(outcome="not_contacted", reason="callback_set", situation="callback_booked"),
                    OutcomeRow(outcome="not_contacted", reason="callback_missing", situation="callback_not_booked"),
                    OutcomeRow(outcome="not_contacted", reason="unclear", situation="unclear_party"),
                ],
                precedence=["transfer:people", "promise_recorded", "recording_failed", "flat_refusal",
                            "identity_failed", "identity_declined", "callback_booked", "callback_not_booked",
                            "unclear_party"],
                transfer_after_promise_outcome=OutcomeRef(outcome=handover.outcome),
            ),
        )


def _walk_models(obj: Any, path: str) -> Iterator[tuple[str, BaseModel]]:
    """Depth-first walk over nested pydantic models, lists and dicts, yielding ``(path, model)``."""
    if isinstance(obj, BaseModel):
        yield path, obj
        for name in type(obj).model_fields:
            child_path = f"{path}.{name}" if path else name
            yield from _walk_models(getattr(obj, name), child_path)
    elif isinstance(obj, list | tuple):
        for index, item in enumerate(obj):
            yield from _walk_models(item, f"{path}[{index}]")
    elif isinstance(obj, dict):
        for key, value in obj.items():
            yield from _walk_models(value, f"{path}.{key}")
