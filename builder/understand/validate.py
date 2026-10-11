"""Deterministic validation of an extracted :class:`~builder.policy.ir.PolicyIR` against the tool API and the ledger.

The LLM that extracts the IR is good at reading prose and bad at keeping closed vocabularies straight. This module
catches what a machine can check, so the repair loop spends model calls only on real problems:

* **Tool vocabulary** (read from the :class:`~builder.ingest.tool_api.ToolApi`, never hard-coded): identity question
  fields must be values of the ``verify_identity`` ``field`` enum; every outcome the IR can close with must be a
  ``close_call`` ``outcome`` value and every reason a ``reason`` value (when the API lists them) and present exactly
  where the API says a reason is required; note reasons must be ``add_note`` reasons; transfer queues must be
  ``transfer_to_agent`` queues; tool actions must name real tools with real parameters; product types and fields used
  by the disclosure and amount rules must exist in the ``get_account`` response.
* **Structure**: lateness bands must not overlap (gaps are warned about), band names must be unique and referenced
  correctly by the persuasion ladder, holidays must be plausible calendar dates, identity needs questions and a
  failure limit, the outcome table must exist and its precedence keys must be known.
* **Format of the free-text fields**: ``disclosure.team_phrase`` must complete "I am calling from ___" (no whole
  sentence, not empty), a product's spoken name must be a bare noun phrase that does not state the amount or the days
  late, a persuasion step whose text names a date rule must not use the date kind ``customer``, and the product
  fields that hold the past-due amount and the days late (they decide every band and deadline) must be real
  ``get_account`` fields that the compiler can identify.
* **Prescribed sentences**: when an active ledger rule prescribes something the agent says (the opening sentence, the
  answer to "are you a robot", the farewell ...), quoted or only described, the matching ``phrases`` field must not be
  empty (``PHRASE_MISSING``; the repair prompt then asks the model to quote or compose the sentence).
* **Ledger coverage**: every *active* ledger rule must be cited by some section's ``rule_ids`` or sit in
  ``residual_rules``; superseded rules must not be cited as the source of a value; cited ids must exist.

Each problem is a :class:`PolicyIssue` (``code``, ``message``, ``rule_ids``, ``severity``). ``error`` issues make the
compiled agent wrong or the IR unusable and trigger the repair loop; ``warning`` issues are quality notes that go to
the trace (and are auto-fixed where that is safe, see :func:`builder.understand.repair.apply_safe_fixes`).
"""

from __future__ import annotations

import itertools
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Literal, get_args

from pydantic import BaseModel, Field

from builder.compiler.roles import (
    account_roles,
    close_roles,
    customer_binding,
    note_roles,
    queue_field,
    verify_roles,
)
from builder.ingest.tool_api import FieldSpec, ToolApi
from builder.policy.intents import canonical_intent_key
from builder.policy.ir import (
    ANY_PRODUCT,
    Band,
    OutcomeRef,
    PolicyIR,
    ProposalDateKind,
    RuleEntry,
    RuleLedger,
    fold_product_type,
    looks_like_sentence,
    template_fields,
)
from builder.policy.outcomes import STRUCTURAL_ORDER, TRANSFER_PREFIX, Situation, is_transfer_situation
from builder.understand.date_cues import corrected_proposal_date

__all__ = [
    "EXTERNAL_ISSUE_PREFIXES",
    "PhraseRequirement",
    "PolicyIssue",
    "ToolVocabulary",
    "errors_of",
    "format_issues",
    "has_errors",
    "is_external_issue",
    "phrase_checklist",
    "phrase_requirements",
    "resolve_product_fields",
    "tool_vocabulary",
    "validate_policy",
]

Severity = Literal["error", "warning"]

EXTERNAL_ISSUE_PREFIXES: tuple[str, ...] = ("GUARD_", "WALKTHROUGH_", "PRACTICE_")
"""Issue-code prefixes written by other stages: the policy guard (``GUARD_<guard>``) and the self-tests
(``WALKTHROUGH_``, ``PRACTICE_``). :func:`validate_policy` never produces them, so re-validating cannot tell whether a
repair removed them - only a model (or a person) can act on them."""


class PolicyIssue(BaseModel):
    """One problem found in an IR.

    Attributes:
        code: Stable upper-case identifier (``IDENTITY_FIELD``, ``BANDS_OVERLAP``, ``RULE_UNCOVERED`` ...).
        message: What is wrong and what the allowed values or the fix would be - written for the repair prompt.
        rule_ids: Ledger rule ids the problem relates to.
        severity: ``error`` (repair needed) or ``warning`` (note only).
        path: Where in the IR (``identity.questions[1].field``); empty for global problems.
    """

    code: str
    message: str
    rule_ids: list[str] = Field(default_factory=list)
    severity: Severity = "error"
    path: str = ""

    def __str__(self) -> str:
        where = f" at {self.path}" if self.path else ""
        ids = f" [{', '.join(self.rule_ids)}]" if self.rule_ids else ""
        return f"{self.severity.upper()} {self.code}{where}: {self.message}{ids}"


def is_external_issue(issue: PolicyIssue) -> bool:
    """True for an issue that another stage found (policy guard, self-test) and that this validator cannot re-check."""
    return issue.code.startswith(EXTERNAL_ISSUE_PREFIXES)


def errors_of(issues: Iterable[PolicyIssue]) -> list[PolicyIssue]:
    """The ``error`` issues among ``issues``."""
    return [issue for issue in issues if issue.severity == "error"]


def has_errors(issues: Iterable[PolicyIssue]) -> bool:
    """True when at least one issue is an error."""
    return any(issue.severity == "error" for issue in issues)


def format_issues(issues: Iterable[PolicyIssue]) -> str:
    """One line per issue, errors first (used in repair prompts and traces)."""
    ordered = sorted(issues, key=lambda issue: (issue.severity != "error", issue.code))
    return "\n".join(f"- {issue}" for issue in ordered)


# --------------------------------------------------------------------------------------------------------------------
# Tool vocabulary
# --------------------------------------------------------------------------------------------------------------------


@dataclass
class ToolVocabulary:
    """The closed vocabularies of a tool API that the IR must stay inside. ``None`` = the API does not say."""

    tool_names: set[str] = field(default_factory=set)
    identity_fields: list[str] | None = None
    outcomes: list[str] | None = None
    reasons: list[str] | None = None
    reason_exempt_outcomes: set[str] | None = None
    note_reasons: list[str] | None = None
    queues: list[str] | None = None
    queue_field: str = "queue"
    product_types: list[str] | None = None
    #: the product type field when the products have one (with or without an enum); ``None`` = untyped products
    product_type_field: str | None = None
    #: type values the contract's text names for a type field without an enum (quoted in its description)
    product_type_hints: list[str] = field(default_factory=list)
    product_fields: set[str] = field(default_factory=set)
    request_fields: dict[str, dict[str, FieldSpec]] = field(default_factory=dict)
    field_names: dict[str, str] = field(default_factory=dict)
    """Role (``outcome``, ``reason``, ``selector``, ``note_reason``, ``product_type``) -> the API's field name."""

    def name_of(self, role: str, default: str) -> str:
        """The tool API's field name for ``role`` (``default`` when unknown)."""
        return self.field_names.get(role) or default

    def describe(self) -> str:
        """A short bullet list of the vocabularies, for prompts."""
        lines: list[str] = []

        def add(label: str, values: Iterable[str] | None) -> None:
            if values:
                lines.append(f"- {label}: {' | '.join(values)}")

        reason = self.name_of("reason", "reason")
        add(f"verify_identity {self.name_of('selector', 'field')} values", self.identity_fields)
        add(f"close_call {self.name_of('outcome', 'outcome')} values", self.outcomes)
        add(f"close_call {reason} values", self.reasons)
        if self.reason_exempt_outcomes is not None:
            exempt = sorted(self.reason_exempt_outcomes)
            lines.append(
                f"- close_call {reason} is required for every outcome except: " + (", ".join(exempt) or "(none)")
            )
        add(f"add_note {self.name_of('note_reason', 'reason')} values", self.note_reasons)
        add(f"transfer_to_agent {self.queue_field} values", self.queues)
        add(f"get_account product {self.name_of('product_type', 'type')} values", self.product_types)
        if self.product_types is None and self.product_type_field:
            named = f" (the contract names: {' | '.join(self.product_type_hints)})" if self.product_type_hints else ""
            lines.append(f"- get_account product {self.product_type_field} values: free text, not listed by the "
                         f"contract{named}; key product_naming, floor_field_by_type and floor_rule_by_type by the type "
                         f'names the documents use (matched ignoring case and accents), "{ANY_PRODUCT}" = every other '
                         f"product")
        elif self.product_types is None and self.product_fields:
            lines.append(f'- get_account products have no type field: key product_naming, floor_field_by_type and '
                         f'floor_rule_by_type by "{ANY_PRODUCT}" (= every product)')
        add("get_account product fields", sorted(self.product_fields))
        return "\n".join(lines)


#: a value quoted in a field description ('Crédito', "card", or in typographic quotes): an opening quote after a space
#: or bracket, a closing one before a space or punctuation (so an apostrophe inside a word opens nothing)
_QUOTED = re.compile(
    r"(?:^|(?<=[\s(\[,:;/]))['\"\u201c\u2018\u00ab]([^'\"\u201c\u201d\u2018\u2019\u00ab\u00bb\n]{1,60})"
    r"['\"\u201d\u2019\u00bb](?=$|[\s).,;:/!?\]])"
)


def _request_enum(api: ToolApi, tool: str, name: str) -> list[str] | None:
    contract = api.tools.get(tool)
    spec = contract.get_field(name, "request") if contract else None
    return list(spec.enum) if spec is not None and spec.enum else None


def _product_spec(api: ToolApi) -> list[FieldSpec]:
    contract = api.tools.get("get_account")
    if contract is None:
        return []
    for spec in contract.response_fields:
        if spec.items:
            return list(spec.items)
    return []


_EXCEPT = re.compile(r"\bexcept\b(?P<tail>[^.;]*)", re.IGNORECASE)


_DATE_RULE = re.compile(
    r"\b(?:next|following)\s+(?:business|working|calendar)\s+day\b|\bbusiness\s+days?\s+(?:after|from)\b|"
    r"\btomorrow\b|\b(?:in|within)\s+(?:\d+|one|two|three|four|five|six|seven|ten)\s+(?:business\s+|calendar\s+|"
    r"working\s+)?days?\b|\b(?:last|first)\s+business\s+day\b|\bend\s+of\s+(?:the\s+)?(?:week|month)\b|"
    r"\b(?:on|by)\s+(?:next\s+)?(?:monday|tuesday|wednesday|thursday|friday)\b",
    re.IGNORECASE,
)
_ALL_PRODUCTS = re.compile(
    r"\ball\s+(?:of\s+)?(?:them|these|those|the\s+(?:products?|accounts?|loans?|cards?|debts?)|your\s+\w+)\b",
    re.IGNORECASE,
)
# a percentage OF the past-due amount (a percentage of the balance or of anything else is not a floor rule here: the
# product's own floor field may already hold it)
_PERCENT = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:%|percent\b|per\s+cent\b)\s+of\s+(?:the\s+|their\s+|its\s+)?"
                      r"[`'\"]?(?:total\s+)?(?:past[-\s_]?due|overdue|arrears?|late)\b", re.IGNORECASE)
_FLOOR_WORDS = re.compile(r"\b(?:minimum|at\s+least|floor|lowest|no\s+less\s+than|not\s+less\s+than|smallest|partial)\b",
                          re.IGNORECASE)
_DAYS_NAME = re.compile(r"day|age|dpd|late|overdue|arrear|delinq", re.IGNORECASE)
_AMOUNT_NAME = re.compile(r"due|arrear|overdue|owed|outstanding|balance|amount|debt", re.IGNORECASE)


def _intro_key(extra: dict[str, str], key: str) -> str:
    """The sentence under ``key`` in ``phrases.extra``, or under any key that ends with ``_<key>`` (the compiler's lookup)."""
    for name, value in extra.items():
        if (name == key or name.endswith("_" + key)) and value.strip():
            return value.strip()
    return ""


def _intro_sentence(extra: dict[str, str]) -> str:
    """The whole sentence that names the caller (``disclosure_intro`` or any ``*_intro`` key), or ``""``."""
    return _intro_key(extra, "disclosure_intro") or _intro_key(extra, "intro")


def _pick(candidates: list[FieldSpec], preferred: list[str], hint: re.Pattern[str]) -> str:
    """The one field among ``candidates`` that is clearly meant: a single candidate, else a single preferred one."""
    names = [spec.name for spec in candidates]
    if len(names) == 1:
        return names[0]
    for pool in (
        [n for n in names if n in preferred],
        [n for n in names if hint.search(n)],
        [n for n in names if n in preferred and hint.search(n)],
    ):
        if len(pool) == 1:
            return pool[0]
    return ""


def resolve_product_fields(ir: PolicyIR, tool_api: ToolApi) -> tuple[str, str]:
    """``(amount field, days-late field)`` of the ``get_account`` products, from the tool API; ``""`` = cannot tell.

    The explicit ``disclosure.amount_field`` / ``days_field`` win when they name a real product field. Otherwise the
    days field is the integer product field with a days-like name, and the amount field the numeric one with an
    amount-like name that is neither a floor field of ``ir.amounts`` nor the days field; ``disclosure.state_fields``
    break ties. Names are matched by meaning only (``day``, ``age``, ``dpd``, ``due``, ``arrears`` ...), never by the
    spelling of a particular bank's API.
    """
    specs = _product_spec(tool_api)
    disclosure = ir.disclosure
    if not specs:
        return disclosure.amount_and_days_fields()
    names = {spec.name for spec in specs}
    state = list(disclosure.state_fields)
    days = (disclosure.days_field or "").strip()
    if days not in names:
        whole = [spec for spec in specs if spec.type == "integer"]
        days = _pick([s for s in whole if _DAYS_NAME.search(s.name)] or whole, state, _DAYS_NAME)
        if not days:
            days = _pick([s for s in specs if s.type in ("integer", "number") and "day" in s.name.lower()], state, _DAYS_NAME)
    floors = set(ir.amounts.floor_field_by_type.values())
    amount = (disclosure.amount_field or "").strip()
    if amount not in names:
        numeric = [s for s in specs if s.type == "number" and s.name not in floors and s.name != days]
        amount = _pick([s for s in numeric if _AMOUNT_NAME.search(s.name)] or numeric, state, _AMOUNT_NAME)
    return amount, days


def _role_fields(tool_api: ToolApi) -> dict[str, str]:
    """The field names that hold the closed vocabularies, found by role the way the compiler finds them (AUD-20).

    A hidden tool API may rename ``close_call.outcome`` (``disposition``), ``verify_identity.field`` (``question``) or
    the product ``type`` (``kind``); the compiler follows the rename through :mod:`builder.compiler.roles`, so the
    validator, the safe fixes, the extraction prompt and the output guard must read the same fields. A role that cannot
    be resolved keeps the kit name, so this never raises.
    """
    names = {"outcome": "outcome", "reason": "reason", "selector": "field", "note_reason": "reason",
             "product_type": "type"}
    try:
        cust = customer_binding(tool_api).field
    except Exception:  # a degenerate contract keeps the kit names
        cust = "customer_id"
    tools = tool_api.tools
    resolvers: list[tuple[str, Callable[[], str | None]]] = []
    if "close_call" in tools:
        resolvers += [("outcome", lambda: close_roles(tools["close_call"], cust).outcome),
                      ("reason", lambda: close_roles(tools["close_call"], cust).reason)]
    if "verify_identity" in tools:
        resolvers.append(("selector", lambda: verify_roles(tools["verify_identity"], cust).selector))
    if "add_note" in tools:
        resolvers.append(("note_reason", lambda: note_roles(tools["add_note"], cust).reason))
    if "get_account" in tools:
        resolvers.append(("product_type", lambda: account_roles(tool_api, (), cust).type_key))
    for key, resolve in resolvers:
        try:
            found = resolve()
        except Exception:  # CompileError or a malformed contract: keep the kit name
            continue
        if found:
            names[key] = found
    return names


def tool_vocabulary(tool_api: ToolApi) -> ToolVocabulary:
    """Read the closed vocabularies the validator needs out of ``tool_api`` (missing tools give ``None`` entries).

    Every vocabulary is read from the field that holds its role (:func:`_role_fields`), so a renamed field stays checked.
    """
    fields = _role_fields(tool_api)
    vocab = ToolVocabulary(tool_names=set(tool_api.tools))
    vocab.field_names = dict(fields)
    vocab.identity_fields = _request_enum(tool_api, "verify_identity", fields["selector"])
    vocab.outcomes = _request_enum(tool_api, "close_call", fields["outcome"])
    vocab.reasons = _request_enum(tool_api, "close_call", fields["reason"])
    vocab.note_reasons = _request_enum(tool_api, "add_note", fields["note_reason"])
    transfer = tool_api.tools.get("transfer_to_agent")
    vocab.queue_field = (queue_field(transfer) if transfer is not None else None) or "queue"  # the API's own name
    vocab.queues = _request_enum(tool_api, "transfer_to_agent", vocab.queue_field)
    close = tool_api.tools.get("close_call")
    reason_spec = close.get_field(fields["reason"], "request") if close else None
    if reason_spec is not None and vocab.outcomes:
        text = f"{reason_spec.description} {close.description if close else ''}"
        match = _EXCEPT.search(text)
        if match and re.search(r"\brequired\b", text, re.IGNORECASE):
            tail = match.group("tail").lower()
            vocab.reason_exempt_outcomes = {o for o in vocab.outcomes if re.search(rf"\b{re.escape(o.lower())}\b", tail)}
    products = _product_spec(tool_api)
    vocab.product_fields = {spec.name for spec in products}
    type_spec = next((spec for spec in products if spec.name == fields["product_type"]), None)
    vocab.product_types = list(type_spec.enum) if type_spec is not None and type_spec.enum else None
    if type_spec is not None:
        vocab.product_type_field = type_spec.name
        if not type_spec.enum:
            vocab.product_type_hints = list(dict.fromkeys(
                m.group(1).strip() for m in _QUOTED.finditer(type_spec.description or "") if m.group(1).strip()))
    for name, contract in tool_api.tools.items():
        vocab.request_fields[name] = {spec.name: spec for spec in contract.request_fields}
    return vocab


# --------------------------------------------------------------------------------------------------------------------
# Sentences the ledger prescribes
# --------------------------------------------------------------------------------------------------------------------
#
# A brief often DESCRIBES an utterance instead of quoting it ("say that you are an automated caller", "thank the
# customer and say goodbye"). A model that copies only quoted sentences leaves the matching ``phrases`` field empty and
# the compiled agent then never says it. The detectors below read the ledger statements (generic domain vocabulary, no
# brief wording) and name the ``phrases`` fields that must be filled. They are deliberately conservative: a detector
# fires only when a rule both names the situation and asks the agent to say something about it.

_I = re.IGNORECASE
_SPEECH = re.compile(
    r"(?<!\bnot )(?<!\bnever )(?<!n't )\b(?:say|tell|state|inform|explain|announce|mention|advise|instruct|reply|respond|"
    r"answer (?:that|with)|(?:refer|direct) (?:the )?(?:customer|caller|them))\b|"
    r"\b(?:agent|assistant|bot|it|you) (?:says|tells|states|informs|explains|answers|replies|responds|mentions|advises)\b",
    _I)

_OPENING_CONTENT = re.compile(
    r"\b(?:first sentence|first line|opening (?:sentence|line|statement)|introduc\w*|greet\w*|calling from|"
    r"(?:call|conversation) (?:is|are|will be) recorded|record(?:s|ed|ing)? (?:the|this) call|"
    r"(?:digital|virtual|automated|ai) (?:assistant|agent|caller))\b", _I)
_ASK_WHO = re.compile(
    r"(?:^|[.;,]\s*)(?:then |first |always )?ask\b[^.;]{0,80}?\b(?:speaking (?:with|to)|for the (?:person|customer)|"
    r"who is on the line|whether (?:you are|this is|it is)|if (?:you are|this is)|the right (?:person|party)|is this)\b",
    _I)
_ROBOT = re.compile(r"\b(?:robots?|bots?|chatbots?|machines?|artificial intelligence)\b|\bAI\b", _I)
_SCAM = re.compile(
    r"\b(?:scams?|scammers?|phishing|fraudulent call|genuine|legitimate|(?:call|this) is real|real call|"
    r"verify (?:the|this) call)\b", _I)
_FAILURE = re.compile(
    r"\b(?:wrong answers?|incorrect answers?|too many (?:wrong|incorrect)|not verified|cannot be verified|"
    r"(?:fails?|failed|failing|unable to|cannot|could not) (?:to )?(?:pass |complete )?(?:the )?"
    r"(?:identity|verif\w*|security)|"
    r"(?:identity|verification|security (?:questions?|check)|the check) (?:has |have )?(?:fail\w*|not pass\w*))\b", _I)
_NEGATED_WRONG = re.compile(r"\b(?:not|never|no)\b[^.;]{0,25}\bwrong\b", _I)
_PAID = re.compile(
    r"\b(?:already (?:paid|settled|made (?:the|a) payment)|(?:has|have|had) (?:already )?paid|paid (?:it )?already|"
    r"(?:payment|it) (?:was|has been) (?:made|sent))\b", _I)
_GOODBYE = re.compile(r"\b(?:good-?bye|farewell|say bye|thank (?:the )?(?:customer|caller|them))\b", _I)
_DECEASED = re.compile(r"\b(?:died|deceased|passed away|has passed|death of the customer)\b", _I)
_CONDOLENCE = re.compile(r"\b(?:condolences?|sympath\w*|sorry|regrets?|apolog\w*)\b", _I)


def _in_opening(rule: RuleEntry) -> bool:
    return rule.category == "opening" or "opening" in rule.scope


def _opening_sentence(rule: RuleEntry) -> bool:
    text = rule.statement
    return (_in_opening(rule) and _OPENING_CONTENT.search(text) is not None and _ROBOT.search(text) is None
            and _SCAM.search(text) is None)


def _right_party_question(rule: RuleEntry) -> bool:
    return (_in_opening(rule) or rule.category == "right_party") and _ASK_WHO.search(rule.statement) is not None


def _robot_answer(rule: RuleEntry) -> bool:
    return _ROBOT.search(rule.statement) is not None and _SPEECH.search(rule.statement) is not None


def _scam_answer(rule: RuleEntry) -> bool:
    return _SCAM.search(rule.statement) is not None and _SPEECH.search(rule.statement) is not None


def _identity_failed_sentence(rule: RuleEntry) -> bool:
    text = _NEGATED_WRONG.sub(" ", rule.statement)  # "that retry is not a wrong answer" is not a failure rule
    return _FAILURE.search(text) is not None and _SPEECH.search(text) is not None


def _paid_sentence(rule: RuleEntry) -> bool:
    return _PAID.search(rule.statement) is not None and _SPEECH.search(rule.statement) is not None


def _goodbye_sentence(rule: RuleEntry) -> bool:
    return _GOODBYE.search(rule.statement) is not None


def _deceased_sentence(rule: RuleEntry) -> bool:
    text = rule.statement
    return (_DECEASED.search(text) is not None and _CONDOLENCE.search(text) is not None
            and _SPEECH.search(text) is not None)


@dataclass(frozen=True, slots=True)
class PhraseRequirement:
    """A ``phrases`` field that the ledger requires to be filled.

    Attributes:
        field: Name of the :class:`~builder.policy.ir.Phrases` field.
        meaning: What the sentence is, for messages and prompts.
        rule_ids: The active ledger rules that prescribe it.
    """

    field: str
    meaning: str
    rule_ids: tuple[str, ...]


_PHRASE_DETECTORS: tuple[tuple[str, str, Callable[[RuleEntry], bool]], ...] = (
    ("opening_sentence", "the first sentence the agent says, before it asks who is on the line", _opening_sentence),
    ("right_party_question", "the question that asks who is on the line", _right_party_question),
    ("robot_answer", "what to say when the customer asks whether the agent is a robot or an AI", _robot_answer),
    ("scam_answer", "what to say when the customer asks whether the call is a scam", _scam_answer),
    ("identity_failed_sentence", "what to tell the customer when the identity check has failed",
     _identity_failed_sentence),
    ("deceased_sentence", "what to say when told the customer has died", _deceased_sentence),
    ("paid_sentence", "what to say to a customer who claims to have paid already", _paid_sentence),
    ("goodbye_sentence", "the thanks and farewell before the call is closed", _goodbye_sentence),
)


def phrase_requirements(ledger: RuleLedger) -> list[PhraseRequirement]:
    """The ``phrases`` fields that active ledger rules prescribe (quoted or described), in schema order."""
    active = ledger.active_rules()
    found: list[PhraseRequirement] = []
    for name, meaning, matches in _PHRASE_DETECTORS:
        ids = tuple(rule.id for rule in active if matches(rule))
        if ids:
            found.append(PhraseRequirement(field=name, meaning=meaning, rule_ids=ids))
    return found


def phrase_checklist(ledger: RuleLedger) -> str:
    """:func:`phrase_requirements` as prompt lines (``- phrases.robot_answer: ... [rules A-4]``); empty when none."""
    return "\n".join(
        f"- phrases.{req.field}: {req.meaning} [rules {', '.join(req.rule_ids)}]" for req in phrase_requirements(ledger)
    )


# --------------------------------------------------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------------------------------------------------


def validate_policy(ir: PolicyIR, ledger: RuleLedger, tool_api: ToolApi) -> list[PolicyIssue]:
    """Check ``ir`` against ``tool_api`` and ``ledger``; returns issues, errors first (empty list = valid).

    The checks are listed in the module docstring. The function never raises on a malformed IR section; a section it
    cannot interpret is reported as an issue.
    """
    checker = _Checker(ir, ledger, tool_api)
    checker.run()
    return sorted(checker.issues, key=lambda issue: (issue.severity != "error", issue.code, issue.path))


class _Checker:
    """Runs every check and collects the issues (one instance per validation)."""

    def __init__(self, ir: PolicyIR, ledger: RuleLedger, tool_api: ToolApi) -> None:
        self.ir = ir
        self.ledger = ledger
        self.api = tool_api
        self.vocab = tool_vocabulary(tool_api)
        self.issues: list[PolicyIssue] = []

    def add(self, code: str, message: str, *, severity: Severity = "error", path: str = "",
            rule_ids: Iterable[str] = ()) -> None:
        """Record an issue."""
        self.issues.append(
            PolicyIssue(code=code, message=message, severity=severity, path=path, rule_ids=list(dict.fromkeys(rule_ids)))
        )

    def run(self) -> None:
        """Run all checks."""
        self.identity()
        self.outcomes()
        self.notes_and_queues()
        self.actions()
        self.bands()
        self.calendar()
        self.persuasion_dates()
        self.disclosure_text()
        self.products()
        self.floor_rules()
        self.product_fields()
        self.today_question()
        self.placeholders()
        self.closing()
        self.misc()
        self.required_phrases()
        self.ledger_coverage()

    # ---- identity ----------------------------------------------------------------------------------------------------

    def identity(self) -> None:
        identity = self.ir.identity
        if not identity.questions:
            self.add("IDENTITY_NO_QUESTIONS", "identity.questions is empty; the brief's security questions are missing.",
                     path="identity.questions", rule_ids=identity.rule_ids)
        if identity.max_wrong_total < 1:
            self.add("IDENTITY_LIMIT", "identity.max_wrong_total must be at least 1.", path="identity.max_wrong_total",
                     rule_ids=identity.rule_ids)
        seen: set[str] = set()
        allowed = self.vocab.identity_fields
        for index, question in enumerate(identity.questions):
            path = f"identity.questions[{index}].field"
            if allowed is not None and question.field not in allowed:
                self.add("IDENTITY_FIELD",
                         f"identity question field {question.field!r} is not a verify_identity field; allowed: "
                         f"{', '.join(allowed)}.", path=path, rule_ids=identity.rule_ids)
            if question.field in seen:
                self.add("IDENTITY_FIELD_DUPLICATE", f"field {question.field!r} is asked twice.", severity="warning",
                         path=path, rule_ids=identity.rule_ids)
            seen.add(question.field)
            if question.answer_format == "digits" and not question.digits:
                self.add("IDENTITY_FORMAT", f"question {question.field!r} has answer_format digits but no digits count.",
                         severity="warning", path=f"identity.questions[{index}]", rule_ids=identity.rule_ids)

    # ---- outcomes ----------------------------------------------------------------------------------------------------

    def outcomes(self) -> None:
        table_pairs = {row.ref().as_tuple() for row in self.ir.closing.rows}
        after_promise = self.ir.closing.transfer_after_promise_outcome
        for path, ref in self.ir.outcome_refs():
            rule_ids = self._rule_ids_near(path)
            self._check_outcome_ref(path, ref, rule_ids)
            in_table_scope = not path.startswith("closing.rows")
            if (in_table_scope and self.ir.closing.rows and ref.as_tuple() not in table_pairs
                    and not (after_promise is not None and ref == after_promise)):
                self.add("OUTCOME_NOT_IN_TABLE",
                         f"{ref.outcome}/{ref.reason or '-'} is used here but is not a row of closing.rows.",
                         severity="warning", path=path, rule_ids=rule_ids)

    def _check_outcome_ref(self, path: str, ref: OutcomeRef, rule_ids: list[str]) -> None:
        vocab = self.vocab
        if vocab.outcomes is not None and ref.outcome not in vocab.outcomes:
            self.add("OUTCOME_UNKNOWN",
                     f"outcome {ref.outcome!r} is not a close_call outcome; allowed: {', '.join(vocab.outcomes)}.",
                     path=path, rule_ids=rule_ids)
            return
        if ref.reason is not None and vocab.reasons is not None and ref.reason not in vocab.reasons:
            self.add("REASON_UNKNOWN",
                     f"reason {ref.reason!r} is not a close_call reason; allowed: {', '.join(vocab.reasons)}.",
                     path=path, rule_ids=rule_ids)
        exempt = vocab.reason_exempt_outcomes
        if exempt is None:
            return
        if ref.outcome in exempt and ref.reason:
            self.add("REASON_UNEXPECTED", f"outcome {ref.outcome!r} takes no reason, but {ref.reason!r} is set.",
                     severity="warning", path=path, rule_ids=rule_ids)
        if ref.outcome not in exempt and not ref.reason and not self._reason_from_elsewhere(path):
            self.add("REASON_MISSING", f"outcome {ref.outcome!r} requires a reason (close_call).", path=path,
                     rule_ids=rule_ids)

    def _reason_from_elsewhere(self, path: str) -> bool:
        """Outcomes whose reason is decided at run time, not written in the IR.

        * ``transfer_after_promise_outcome`` has no reason: it is taken from the transfer's own row;
        * the ``product_notes`` row, when the notes pick the closing reason (``first_non_paid_note``): the reason is the
          one of the first product, in ``get_account`` order, whose note is not "paid".
        """
        if path.startswith("closing.transfer_after_promise_outcome"):
            return True
        match = re.match(r"closing\.rows\[(\d+)\]", path)
        if match is None or self.ir.multi_product.closing_reason_rule != "first_non_paid_note":
            return False
        index = int(match.group(1))
        rows = self.ir.closing.rows
        return index < len(rows) and rows[index].situation == Situation.PRODUCT_NOTES

    # ---- notes, queues, actions --------------------------------------------------------------------------------------

    def notes_and_queues(self) -> None:
        notes = self.vocab.note_reasons
        multi = self.ir.multi_product
        if notes is not None:
            for index, reason in enumerate(multi.note_reasons):
                if reason not in notes:
                    self.add("NOTE_REASON", f"note reason {reason!r} is not an add_note reason; allowed: {', '.join(notes)}.",
                             path=f"multi_product.note_reasons[{index}]", rule_ids=multi.rule_ids)
            for index, intent in enumerate(self.ir.intents):
                if intent.note_reason and intent.note_reason not in notes:
                    self.add("NOTE_REASON",
                             f"intent {intent.key!r}: note_reason {intent.note_reason!r} is not an add_note reason; "
                             f"allowed: {', '.join(notes)}.", path=f"intents[{index}].note_reason",
                             rule_ids=intent.rule_ids)
            for label, value in (("paid_note_reason", multi.paid_note_reason),):
                if value and value not in notes:
                    self.add("NOTE_REASON", f"{label} {value!r} is not an add_note reason; allowed: {', '.join(notes)}.",
                             path=f"multi_product.{label}", rule_ids=multi.rule_ids)
        if multi.enabled and not multi.note_reasons and self.vocab.note_reasons:
            self.add("MULTI_PRODUCT_NOTES", "multi_product is enabled but note_reasons is empty.", severity="warning",
                     path="multi_product.note_reasons", rule_ids=multi.rule_ids)
        queues = self.vocab.queues
        if queues is not None:
            for key in self._situation_keys():
                if is_transfer_situation(key) and key[len(TRANSFER_PREFIX):] not in queues:
                    self.add("QUEUE_UNKNOWN",
                             f"situation {key!r} names a queue that transfer_to_agent does not have; allowed: "
                             f"{', '.join(queues)}.", path="closing", rule_ids=self.ir.closing.rule_ids)

    def _situation_keys(self) -> list[str]:
        closing = self.ir.closing
        return list(dict.fromkeys([*closing.precedence, *(row.situation for row in closing.rows)]))

    def actions(self) -> None:
        for index, intent in enumerate(self.ir.intents):
            for position, action in enumerate(intent.actions):
                path = f"intents[{index}].actions[{position}]"
                if not self.vocab.tool_names:
                    continue  # no tool API to check against
                if action.tool not in self.vocab.tool_names:
                    self.add("TOOL_UNKNOWN",
                             f"intent {intent.key!r} uses tool {action.tool!r}, which is not in the tool API "
                             f"({', '.join(sorted(self.vocab.tool_names))}).", path=path, rule_ids=intent.rule_ids)
                    continue
                fields = self.vocab.request_fields.get(action.tool, {})
                for name, value in action.params.items():
                    spec = fields.get(name)
                    if spec is None:
                        self.add("ACTION_PARAM",
                                 f"tool {action.tool!r} has no request field {name!r} (fields: {', '.join(fields)}).",
                                 path=f"{path}.params.{name}", rule_ids=intent.rule_ids)
                    elif spec.enum and value not in spec.enum:
                        self.add("ACTION_PARAM",
                                 f"{action.tool}.{name}={value!r} is not allowed; allowed: {', '.join(spec.enum)}.",
                                 path=f"{path}.params.{name}", rule_ids=intent.rule_ids)

    # ---- bands -------------------------------------------------------------------------------------------------------

    def bands(self) -> None:
        bands = self.ir.bands
        if not bands:
            self.add("BANDS_EMPTY", "no lateness bands; the brief's days-late table is missing.", path="bands")
            return
        names = [band.name for band in bands]
        for name in dict.fromkeys(n for n in names if names.count(n) > 1):
            self.add("BAND_NAME_DUPLICATE", f"band name {name!r} is used more than once.", path="bands")
        # AUD-39: rows typed for some product types form their own table (they win over untyped rows for those types)
        untyped = [band for band in bands if not band.product_types]
        typed_for = {t: [b for b in bands if t in b.product_types] for b0 in bands for t in b0.product_types}
        tables: list[tuple[str, list[Band]]] = [("", untyped)] + [(f" for product type {t!r}", rows)
                                                                  for t, rows in typed_for.items()]
        for label, table in tables:
            ordered = sorted(table, key=lambda b: (b.days_late_min, b.days_late_max))
            for before, after in itertools.pairwise(ordered):
                if after.days_late_min <= before.days_late_max:
                    self.add("BANDS_OVERLAP",
                             f"bands {before.name!r} ({before.days_late_min}-{before.days_late_max}) and {after.name!r} "
                             f"({after.days_late_min}-{after.days_late_max}) overlap{label}.", path="bands",
                             rule_ids=[*before.rule_ids, *after.rule_ids])
            rows = sorted(table if not label else [*table, *untyped], key=lambda b: (b.days_late_min, b.days_late_max))
            reach: Band | None = None
            for after in rows:
                if reach is not None and after.days_late_min > reach.days_late_max + 1:
                    self.add("BANDS_GAP",
                             f"days late {reach.days_late_max + 1}-{after.days_late_min - 1} fall in no band{label} "
                             f"(between {reach.name!r} and {after.name!r}).", severity="warning", path="bands",
                             rule_ids=[*reach.rule_ids, *after.rule_ids])
                if reach is None or after.days_late_max > reach.days_late_max:
                    reach = after
        types = self.vocab.product_types
        for index, band in enumerate(bands):
            self.open_type_keys(f"band {band.name!r} product_types", band.product_types,
                                f"bands[{index}].product_types", band.rule_ids)
            for name in band.product_types:
                if types is not None and name not in types:
                    self.add("PRODUCT_TYPE", f"band {band.name!r} names product type {name!r}, which is not a "
                             f"get_account product type; allowed: {', '.join(types)}.",
                             path=f"bands[{index}].product_types", rule_ids=band.rule_ids)
        for index, band in enumerate(bands):
            if band.deadline.kind == "custom":
                self.add("DEADLINE_CUSTOM", f"band {band.name!r} has a deadline the compiler cannot compute "
                         f"({band.deadline.custom_rule_text!r}).", severity="warning", path=f"bands[{index}].deadline",
                         rule_ids=band.rule_ids)
            wanted = band.persuasion_attempts
            available = len(self.ir.persuasion.steps_for(band.name))
            if wanted > available:  # AUD-30: an error, so a repair completes the ladder or states the real count
                self.add("PERSUASION_STEPS_SHORT",
                         f"band {band.name!r} allows {wanted} persuasion attempts but only {available} persuasion "
                         f"steps apply to it (steps whose bands list is empty or names it), so the agent would make "
                         f"only {available}. If the documents describe the missing step(s), add them exactly as stated "
                         f"(attempt number, bands, amount, date, text). Otherwise set this band's persuasion_attempts "
                         f"to {available}: the agent never repeats an offer, so it can only make the distinct steps the "
                         f"documents describe. Never invent an offer.",
                         path=f"bands[{index}]", rule_ids=[*band.rule_ids, *self.ir.persuasion.rule_ids])
        known = set(names)
        for index, step in enumerate(self.ir.persuasion.steps):
            for name in step.bands:
                if name not in known:
                    self.add("BAND_NAME_UNKNOWN", f"persuasion step {step.attempt} names band {name!r}; bands are: "
                             f"{', '.join(names)}.", path=f"persuasion.steps[{index}].bands",
                             rule_ids=self.ir.persuasion.rule_ids)

    # ---- calendar ----------------------------------------------------------------------------------------------------

    def calendar(self) -> None:
        calendar = self.ir.calendar
        years = {holiday.year for holiday in calendar.holidays}
        for holiday in calendar.holidays:
            if not (2000 <= holiday.year <= 2100):
                self.add("HOLIDAY_RANGE", f"holiday {holiday.isoformat()} is not a plausible date.",
                         path="calendar.holidays", rule_ids=calendar.rule_ids)
            elif holiday.weekday() not in calendar.business_weekdays:
                self.add("HOLIDAY_WEEKEND", f"holiday {holiday.isoformat()} is not on a business weekday (no effect).",
                         severity="warning", path="calendar.holidays", rule_ids=calendar.rule_ids)
        if years and max(years) - min(years) > 3:
            self.add("HOLIDAY_SPREAD", f"holidays span the years {min(years)}-{max(years)}; check the year digits.",
                     severity="warning", path="calendar.holidays", rule_ids=calendar.rule_ids)

    # ---- products ----------------------------------------------------------------------------------------------------

    def products(self) -> None:
        vocab = self.vocab
        disclosure, amounts = self.ir.disclosure, self.ir.amounts
        types = vocab.product_types
        if types is None and vocab.product_type_field:  # a type field without an enum: the documents name the values
            for label, keys in (("product_naming", disclosure.product_naming), ("floor_field_by_type",
                                amounts.floor_field_by_type), ("floor_rule_by_type", amounts.floor_rule_by_type)):
                owner = disclosure if label == "product_naming" else amounts
                self.open_type_keys(f"{label} keys", list(keys),
                                    f"{'disclosure' if owner is disclosure else 'amounts'}.{label}", owner.rule_ids)
        elif types is None and vocab.product_fields:  # untyped products: only the wildcard can name them
            for label, keys in (("product_naming", disclosure.product_naming), ("floor_field_by_type",
                                amounts.floor_field_by_type), ("floor_rule_by_type", amounts.floor_rule_by_type)):
                wrong = [name for name in keys if name != ANY_PRODUCT]
                if wrong:
                    owner = "disclosure" if label == "product_naming" else "amounts"
                    self.add("PRODUCT_TYPE", f"{label} keys {', '.join(map(repr, wrong))}: the get_account products "
                             f"have no type field, so the only key is {ANY_PRODUCT!r} (every product).",
                             path=f"{owner}.{label}",
                             rule_ids=(disclosure if owner == "disclosure" else amounts).rule_ids)
        if types is not None:
            for name in disclosure.product_naming:
                if name not in types and name != ANY_PRODUCT:
                    self.add("PRODUCT_TYPE", f"product_naming key {name!r} is not a get_account product type; allowed: "
                             f"{', '.join(types)}.", path="disclosure.product_naming", rule_ids=disclosure.rule_ids)
            for name in amounts.floor_field_by_type:
                if name not in types and name != ANY_PRODUCT:
                    self.add("PRODUCT_TYPE", f"floor_field_by_type key {name!r} is not a get_account product type; "
                             f"allowed: {', '.join(types)}.", path="amounts.floor_field_by_type",
                             rule_ids=amounts.rule_ids)
            for name in amounts.floor_rule_by_type:
                if name not in types and name != ANY_PRODUCT:
                    self.add("PRODUCT_TYPE", f"floor_rule_by_type key {name!r} is not a get_account product type; "
                             f"allowed: {', '.join(types)}.", path="amounts.floor_rule_by_type",
                             rule_ids=amounts.rule_ids)
            for name in types:
                if name not in disclosure.product_naming and ANY_PRODUCT not in disclosure.product_naming:
                    self.add("PRODUCT_TYPE_UNCOVERED", f"product type {name!r} has no spoken name in disclosure.product_naming.",
                             severity="warning", path="disclosure.product_naming", rule_ids=disclosure.rule_ids)
                if not amounts.has_floor(name):
                    self.add("PRODUCT_TYPE_UNCOVERED", f"product type {name!r} has no floor field in amounts.floor_field_by_type.",
                             severity="warning", path="amounts.floor_field_by_type", rule_ids=amounts.rule_ids)
        fields = vocab.product_fields
        if not fields:
            return
        for name, field_name in amounts.floor_field_by_type.items():
            if field_name not in fields:
                self.add("PRODUCT_FIELD", f"floor field {field_name!r} (for {name!r}) is not a get_account product field; "
                         f"fields: {', '.join(sorted(fields))}.", path=f"amounts.floor_field_by_type.{name}",
                         rule_ids=amounts.rule_ids)
        for field_name in disclosure.state_fields:
            if field_name not in fields:
                self.add("PRODUCT_FIELD", f"state field {field_name!r} is not a get_account product field; fields: "
                         f"{', '.join(sorted(fields))}.", path="disclosure.state_fields", rule_ids=disclosure.rule_ids)
        for name, template in disclosure.product_naming.items():
            for placeholder in template_fields(template):
                if placeholder not in fields:
                    self.add("PRODUCT_FIELD", f"product_naming[{name!r}] uses {{{placeholder}}}, which is not a get_account "
                             f"product field; fields: {', '.join(sorted(fields))}.",
                             path=f"disclosure.product_naming.{name}", rule_ids=disclosure.rule_ids)


    def open_type_keys(self, label: str, keys: Iterable[str], path: str, rule_ids: Iterable[str]) -> None:
        """Product type keys for a type field WITHOUT an enum: never an error (the values come from the documents).

        A key the contract's text does not name (compared ignoring case, accents and separators) is a warning, and so
        is every key when the contract names no value at all; the compiled agent matches the keys to the data the same
        way and falls back to the "*" entry for a type without one of its own.
        """
        field_name = self.vocab.product_type_field
        if self.vocab.product_types is not None or not field_name:
            return
        typed = [key for key in keys if key != ANY_PRODUCT]
        if not typed:
            return
        hints = self.vocab.product_type_hints
        known = {fold_product_type(hint) for hint in hints}
        unlisted = [key for key in typed if fold_product_type(key) not in known]
        if hints and unlisted:
            self.add("PRODUCT_TYPE_UNLISTED", f"{label} {', '.join(map(repr, unlisted))}: the contract names the "
                     f"get_account {field_name} values {', '.join(map(repr, hints))} (matched ignoring case and "
                     f"accents); check the spelling against the documents.", severity="warning", path=path,
                     rule_ids=rule_ids)
        elif not hints:
            self.add("PRODUCT_TYPE_UNVERIFIED", f"{label} {', '.join(map(repr, typed))}: the get_account {field_name} "
                     f"field lists no values, so these type names from the documents cannot be checked (they are "
                     f"matched to the data ignoring case and accents; keep a {ANY_PRODUCT!r} entry for other types).",
                     severity="warning", path=path, rule_ids=rule_ids)

    # ---- floors stated as a computation (AUD-10) -----------------------------------------------------------------------

    def floor_rules(self) -> None:
        amounts = self.ir.amounts
        for name, rule in amounts.floor_rule_by_type.items():
            problem = rule.problem()
            if problem:
                self.add("FLOOR_RULE_INVALID", f"amounts.floor_rule_by_type[{name!r}]: {problem}.",
                         path=f"amounts.floor_rule_by_type.{name}", rule_ids=amounts.rule_ids)
        stated = {round(rule.percent_of_amount, 4) for rule in amounts.floor_rule_by_type.values()
                  if rule.percent_of_amount is not None}
        for rule_id in amounts.rule_ids:
            entry = self.ledger.get(rule_id)
            if entry is None or not entry.active:
                continue
            for sentence in re.split(r"(?<=[.;])\s+", entry.quote):
                if not _FLOOR_WORDS.search(sentence):
                    continue
                for match in _PERCENT.finditer(sentence):
                    value = round(float(match.group(1).replace(",", ".")), 4)
                    if value not in stated:
                        self.add("FLOOR_RULE_UNEXPRESSED",
                                 f"rule {rule_id} states a minimum amount as {match.group(0).strip()!r} of an amount, "
                                 f"but no amounts.floor_rule_by_type entry carries percent_of_amount {value:g}; the "
                                 f"agent would offer only the floor field (or nothing) and the promise guard would block "
                                 f"the amounts the rule allows. Add floor_rule_by_type for the product types it applies "
                                 f"to (percent_of_amount, fixed, combine, rounding as the documents state them).",
                                 path="amounts.floor_rule_by_type", rule_ids=[rule_id])

    # ---- persuasion dates ---------------------------------------------------------------------------------------------

    def persuasion_dates(self) -> None:
        kinds = " | ".join(k for k in get_args(ProposalDateKind) if k not in ("customer", "custom"))
        for index, step in enumerate(self.ir.persuasion.steps):
            path = f"persuasion.steps[{index}]"
            ids = self.ir.persuasion.rule_ids
            if step.date == "custom" and not step.text.strip():
                self.add("PERSUASION_DATE_TEXT",
                         f"persuasion step {step.attempt} has date 'custom' but no text; the compiled agent applies a "
                         f"custom date rule from the text, so write the rule there in the documents' words.",
                         path=path, rule_ids=ids)
            better = corrected_proposal_date(step.date, step.n, step.text)
            if better is not None:  # AUD-18: the kind contradicts the date rule its own text states
                wanted = better[0] + (f" with n={better[1]}" if better[1] is not None else "")
                self.add("PERSUASION_DATE_KIND",
                         f"persuasion step {step.attempt} has date {step.date!r}"
                         f"{f' (n={step.n})' if step.n is not None else ''}, but its text states another date rule "
                         f"({step.text!r}); the compiled agent would propose the wrong day on every call. Use "
                         f"{wanted}, or 'custom' when the text holds a rule no kind expresses.",
                         path=f"{path}.date", rule_ids=ids)
                continue
            found = _DATE_RULE.search(step.text) if step.date == "customer" else None
            if found is not None:
                self.add("PERSUASION_DATE_KIND",
                         f"persuasion step {step.attempt} has date 'customer' (the customer names the day), but its text "
                         f"states a date rule ({found.group(0)!r}). A 'customer' date is rendered as 'on the day the "
                         f"customer names', so the agent would not propose that date. Use the kind that matches the "
                         f"text: {kinds} (the nth_* kinds need n), or 'custom' with the rule in text.",
                         path=f"{path}.date", rule_ids=ids)

    # ---- disclosure wording -------------------------------------------------------------------------------------------

    def disclosure_text(self) -> None:
        disclosure = self.ir.disclosure
        has_intro = bool(_intro_sentence(self.ir.phrases.extra))
        if not disclosure.team_phrase and not has_intro:
            self.add("TEAM_PHRASE_MISSING",
                     'disclosure.team_phrase is empty, so the debt statement would read "I am calling from ." Write the '
                     'words that complete "I am calling from ___" (the team or department the documents name, otherwise '
                     "the institution's name), or quote the whole sentence in phrases.extra['disclosure_intro'].",
                     path="disclosure.team_phrase", rule_ids=disclosure.rule_ids)
        elif disclosure.team_phrase and not has_intro and looks_like_sentence(disclosure.team_phrase):
            # an opener ("This is ...") is certain; a full stop inside ("... Inc. Collections") only a suspicion
            certain = looks_like_sentence(disclosure.team_phrase, opener_only=True)
            self.add("TEAM_PHRASE_SENTENCE",
                     f'disclosure.team_phrase {disclosure.team_phrase!r} reads as a whole sentence, but the agent says '
                     f'"I am calling from " + team_phrase. Keep only the words that complete that sentence, or move '
                     f"the sentence to phrases.extra['disclosure_intro'].",
                     severity="error" if certain else "warning",
                     path="disclosure.team_phrase", rule_ids=disclosure.rule_ids)
        numeric = self._numeric_fields()
        for name, template in disclosure.product_naming.items():
            path = f"disclosure.product_naming.{name}"
            stated = [field_name for field_name in template_fields(template) if field_name in numeric]
            if stated:
                self.add("PRODUCT_NAMING_STATEMENT",
                         f"product_naming[{name!r}] uses {', '.join('{' + f + '}' for f in stated)}: the debt statement "
                         f"already says the amount and the days late, so the name must only identify the product "
                         f"(for example 'loan ending {{account_suffix}}'), with no amount, days or verb.",
                         path=path, rule_ids=disclosure.rule_ids)
            elif re.search(r"[.;!?]", template):
                self.add("PRODUCT_NAMING_STATEMENT",
                         f"product_naming[{name!r}] ({template!r}) is more than a name: it holds sentence punctuation. "
                         f"Use a bare noun phrase without a verb or a full stop.",
                         path=path, rule_ids=disclosure.rule_ids)

    def _numeric_fields(self) -> set[str]:
        """Product fields that carry amounts or day counts: the resolved amount and days fields plus numeric state fields."""
        disclosure = self.ir.disclosure
        numeric = {name for name in resolve_product_fields(self.ir, self.api) if name}
        specs = {spec.name: spec for spec in _product_spec(self.api)}
        for name in disclosure.state_fields:
            spec = specs.get(name)
            if spec is not None and spec.type in ("number", "integer"):
                numeric.add(name)
        return numeric

    # ---- the amount and days fields -----------------------------------------------------------------------------------

    def product_fields(self) -> None:
        specs = _product_spec(self.api)
        if not specs:
            return
        names = {spec.name for spec in specs}
        disclosure = self.ir.disclosure
        resolved = dict(zip(("amount", "days"), resolve_product_fields(self.ir, self.api), strict=True))
        effective = dict(zip(("amount", "days"), disclosure.amount_and_days_fields(), strict=True))
        explicit = {"amount": (disclosure.amount_field or "").strip(), "days": (disclosure.days_field or "").strip()}
        what = {"amount": "the past-due amount", "days": "the days late"}
        for label in ("amount", "days"):
            path = f"disclosure.{label}_field"
            code = f"{label.upper()}_FIELD"
            if explicit[label]:
                if explicit[label] not in names:
                    hint = f" ({resolved[label]!r} looks right)" if resolved[label] else ""
                    self.add(f"{code}_UNKNOWN",
                             f"{path} {explicit[label]!r} is not a get_account product field; fields: "
                             f"{', '.join(sorted(names))}{hint}.", path=path, rule_ids=disclosure.rule_ids)
            elif resolved[label]:
                if effective[label] != resolved[label]:
                    self.add(f"{code}_MISSING",
                             f"the product field holding {what[label]} cannot be read from disclosure.state_fields "
                             f"({effective[label] or 'nothing found'!r}); set {path} to {resolved[label]!r}. It decides "
                             f"every band and deadline.", path=path, rule_ids=disclosure.rule_ids)
            elif effective[label] not in names:
                self.add(f"{code}_MISSING",
                         f"cannot tell which product field holds {what[label]}; set {path} to one of: "
                         f"{', '.join(sorted(names))}.", path=path, rule_ids=disclosure.rule_ids)

    # ---- the first ask --------------------------------------------------------------------------------------------------

    def today_question(self) -> None:
        if not self.ir.multi_product.enabled or _intro_key(self.ir.phrases.extra, "today_question_all"):
            return
        asked = self.ir.phrases.today_question.strip() or self.ir.persuasion.first_ask.strip()
        if _ALL_PRODUCTS.search(asked):
            self.add("TODAY_QUESTION_ALL",
                     f"the first ask {asked!r} is about all products, but the agent asks the same question when the "
                     f"customer has only one. Put the single-product wording in phrases.today_question (and "
                     f"persuasion.first_ask) and this sentence in phrases.extra['today_question_all']. If the "
                     f"documents give no separate single-product wording, adapt this one minimally (for example "
                     f"'all of them' becomes 'this') and add nothing else.",
                     path="phrases.today_question", rule_ids=[*self.ir.phrases.rule_ids, *self.ir.persuasion.rule_ids])

    # ---- placeholders ------------------------------------------------------------------------------------------------

    def placeholders(self) -> None:
        names = set(self.ir.call_variable_names())
        if not names:
            return
        phrases = self.ir.phrases
        checked = {
            "phrases.opening_sentence": phrases.opening_sentence,
            "phrases.right_party_question": phrases.right_party_question,
            "phrases.robot_answer": phrases.robot_answer,
            "phrases.scam_answer": phrases.scam_answer,
            "phrases.identity_failed_sentence": phrases.identity_failed_sentence,
            "phrases.deceased_sentence": phrases.deceased_sentence,
        }
        for index, question in enumerate(self.ir.identity.questions):
            checked[f"identity.questions[{index}].ask"] = question.ask
        for path, text in checked.items():
            unknown = [p for p in template_fields(text) if p not in names]
            if unknown:
                self.add("PLACEHOLDER_UNKNOWN",
                         f"{path} uses {', '.join('{' + p + '}' for p in unknown)}, which is not a declared call variable "
                         f"({', '.join(sorted(names))}).", severity="warning", path=path, rule_ids=phrases.rule_ids)

    # ---- closing -----------------------------------------------------------------------------------------------------

    def closing(self) -> None:
        closing = self.ir.closing
        if not closing.rows:
            self.add("CLOSING_EMPTY", "closing.rows is empty; the brief's outcome table is missing.", path="closing.rows",
                     rule_ids=closing.rule_ids)
            return
        known = {row.situation for row in closing.rows} | set(STRUCTURAL_ORDER)
        known |= {intent.key for intent in self.ir.intents}
        for key in closing.precedence:
            if key not in known and not is_transfer_situation(key) and canonical_intent_key(key) not in known:
                self.add("PRECEDENCE_UNKNOWN",
                         f"closing.precedence names {key!r}, which is neither a table situation, a structural situation "
                         f"nor an intent key.", severity="warning", path="closing.precedence", rule_ids=closing.rule_ids)
        # AUD-46: the first matching row wins, so a later row for the same situation and verification state never applies
        for index, row in enumerate(closing.rows):
            states = (True, False) if row.requires_verified is None else (row.requires_verified,)
            earlier = closing.rows[:index]
            if all(any(other.matches(row.situation, state) for other in earlier) for state in states):
                self.add("ROW_SHADOWED",
                         f"closing.rows[{index}] ({row.situation!r} -> {row.outcome}/{row.reason}) never applies: an "
                         f"earlier row for the same situation and verification state always wins. The IR cannot pick "
                         f"between rows by anything else (for example the terms of a promise); keep one row per "
                         f"situation and verification state.", severity="warning", path=f"closing.rows[{index}]",
                         rule_ids=list(row.rule_ids) or list(closing.rule_ids))

    # ---- misc --------------------------------------------------------------------------------------------------------

    def misc(self) -> None:
        seen: dict[tuple[str, str], int] = {}
        for index, intent in enumerate(self.ir.intents):
            if intent.scope != "after_verification" and intent.note_reason and intent.outcome is None:
                self.add("INTENT_NOTE_BEFORE_VERIFICATION",
                         f"intent {intent.key!r} ({intent.scope}) only records the note {intent.note_reason!r}, but "
                         f"before the identity is verified the agent has no note tool and must close the call. Give it "
                         f"the close outcome the documents prescribe for this situation before verification (outcome / "
                         f"reason from the outcome table), and keep the note for the after_verification entry.",
                         path=f"intents[{index}]", rule_ids=intent.rule_ids)
            marker = (intent.key, intent.scope)
            if marker in seen:
                self.add("INTENT_DUPLICATE", f"intent {intent.key!r} ({intent.scope}) is defined twice "
                         f"(intents[{seen[marker]}] and intents[{index}]).", severity="warning", path=f"intents[{index}]",
                         rule_ids=intent.rule_ids)
            seen[marker] = index

    # ---- prescribed sentences ----------------------------------------------------------------------------------------

    def required_phrases(self) -> None:
        for requirement in phrase_requirements(self.ledger):
            if getattr(self.ir.phrases, requirement.field).strip():
                continue
            self.add("PHRASE_MISSING",
                     f"phrases.{requirement.field} is empty, but the rule ledger prescribes it ({requirement.meaning}). "
                     f"Fill it: copy the documents' wording when they quote the sentence; when they only describe it, "
                     f"compose one short natural sentence that contains exactly the elements the rule requires and "
                     f"nothing else, and add the rule ids to phrases.rule_ids.",
                     path=f"phrases.{requirement.field}", rule_ids=requirement.rule_ids)

    # ---- ledger coverage ---------------------------------------------------------------------------------------------

    def ledger_coverage(self) -> None:
        ledger = self.ledger
        if not ledger.rules:
            self.add("LEDGER_EMPTY", "the rule ledger is empty.")
            return
        for dupe in ledger.duplicate_ids():
            self.add("LEDGER_DUPLICATE_ID", f"ledger id {dupe!r} occurs more than once.", severity="warning",
                     rule_ids=[dupe])
        known = set(ledger.ids())
        superseded = ledger.superseded_ids()
        for rule in ledger.rules:
            for other in (*rule.supersedes, *([rule.superseded_by] if rule.superseded_by else [])):
                if other not in known:
                    self.add("SUPERSESSION_DANGLING", f"rule {rule.id} refers to unknown rule {other!r} in its supersession.",
                             severity="warning", rule_ids=[rule.id])
        cited = self.ir.referenced_rule_ids()
        stale = sorted(cited & superseded & known)
        if stale:
            newer = {old: self._replacement(old) for old in stale}
            detail = "; ".join(f"{old} (replaced by {new or '?'})" for old, new in newer.items())
            self.add("RULE_SUPERSEDED_USED",
                     f"the IR cites superseded rules: {detail}. Follow the amending rule and cite it instead.",
                     rule_ids=stale)
        unknown = sorted(cited - known)
        if unknown:
            self.add("RULE_ID_UNKNOWN", f"the IR cites ids that are not in the ledger: {', '.join(unknown)}.",
                     severity="warning", rule_ids=unknown)
        uncovered = [rule.id for rule in ledger.active_rules() if rule.id not in cited]
        if uncovered:
            self.add("RULE_UNCOVERED",
                     f"{len(uncovered)} active ledger rules are cited by no section and are not residual rules: "
                     f"{', '.join(uncovered)}.", severity="warning", rule_ids=uncovered)

    def _replacement(self, old_id: str) -> str:
        rule = self.ledger.get(old_id)
        if rule is not None and rule.superseded_by:
            return rule.superseded_by
        return next((r.id for r in self.ledger.rules if old_id in r.supersedes), "")

    def _rule_ids_near(self, path: str) -> list[str]:
        """Rule ids of the IR section that owns ``path`` (the first path segment's ``rule_ids``)."""
        head = re.split(r"[.\[]", path, maxsplit=1)[0]
        section = getattr(self.ir, head, None)
        ids = getattr(section, "rule_ids", None)
        if isinstance(ids, list) and ids:
            return list(ids)
        if isinstance(section, list) and section:
            match = re.match(r"\w+\[(\d+)\]", path)
            if match and int(match.group(1)) < len(section):
                return list(getattr(section[int(match.group(1))], "rule_ids", []))
        return []
