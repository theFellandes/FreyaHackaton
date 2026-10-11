"""Roles of the tool fields, derived from the bank's tool contracts - never from fixed names.

The hackathon keeps the seven tool names but lets a hidden brief change their fields and allowed values: the outcome
field of ``close_call`` may be called ``disposition``, the success flag of every response ``success``, the product key of
``get_account`` ``id``. A compiler that hard-codes ``outcome``, ``ok`` or ``product_id`` then produces an agent that
deploys cleanly and silently drops a field or misreads an answer. This module finds each *role* by what the contract says
(type, enum, ``required``, description, the way the tools refer to each other) and the compilers use the roles:

* :func:`success_flag` - the boolean that says a tool carried out its action; :func:`success_status` - without one,
  a string / enum status (``recorded`` / ``rejected``) that says it;
* :func:`customer_binding` - the request field that carries the customer id and the call variable that fills it;
* :func:`close_roles` - outcome and reason fields of ``close_call``;
* :func:`account_roles` - the product list and the id / type keys of the items ``get_account`` returns;
* :func:`promise_roles` - product, amount and date fields of ``create_promise``;
* :func:`note_roles`, :func:`queue_field`, :func:`verify_roles`, :func:`verify_shape` - the same for the other tools.

When a role the agent cannot work without is not found, the functions raise :class:`~builder.compiler.errors.CompileError`
(the build then repairs or falls back); they never fall back to an invented name.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass

from builder.compiler.errors import CompileError
from builder.ingest.tool_api import FieldSpec, ToolApi, ToolContract

__all__ = [
    "DEFAULT_MINOR_SCALE",
    "STATUS_FAILURE",
    "STATUS_SUCCESS",
    "AccountRoles",
    "CloseRoles",
    "CustomerBinding",
    "NoteRoles",
    "PromiseRoles",
    "SuccessStatus",
    "VerifyRoles",
    "VerifyShape",
    "account_roles",
    "close_roles",
    "customer_binding",
    "minor_scale",
    "minor_units",
    "note_roles",
    "promise_roles",
    "queue_field",
    "status_value",
    "success_flag",
    "success_status",
    "verify_roles",
    "verify_shape",
]

_SUCCESS_NAMES = ("ok", "success", "succeeded", "successful", "is_success", "is_ok")
_OUTCOME_HINTS = ("outcome", "result", "disposition", "status", "resolution", "state", "code")
_REASON_HINTS = ("reason", "detail", "details", "why", "cause", "explanation", "comment", "note", "text")
_ID_WORDS = ("id", "ref", "code", "number", "no", "key", "reference", "identifier")
_ID_HINTS = ("product", "item", "account", "loan", "contract", "line")
_TYPE_HINTS = ("type", "kind", "category", "class")
_AMOUNT_HINTS = ("amount", "sum", "payment", "value", "total")
_CUSTOMER_HINTS = ("customer", "caller", "client", "user", "subscriber", "member", "holder", "ref", "id")
_OVERALL_WORDS = ("all", "overall", "fully", "complete", "completed", "every", "both", "entire", "whole")
_VERDICT_WORDS = frozenset({"verified", "correct", "match", "matched", "valid", "validated", "right", "accepted",
                            "passed", "approved", "authenticated", "successful"})
_COUNTS_DOWN = ("left", "remaining", "remain", "available", "spare")
_COUNTS_UP = ("strikes", "strike", "wrong", "failed", "failures", "failure", "incorrect", "errors", "misses", "used")
_HINT_WORDS = ("hint", "suggestion", "advice", "tip", "clue", "guidance")
# "bare" success prose: success / failure words surrounded only by function words ("True if successful.")
_BARE_SUCCESS = frozenset(["success", "successful", "successfully", "succeeded", "succeeds", "succeed", "error",
                           "errors", "failure", "failures", "failed", "fails", "fail"])
_BARE_FUNCTION = frozenset(["is", "it", "the", "a", "an", "if", "when", "on", "upon", "whether", "true", "false",
                            "set", "to", "returns", "return", "indicates", "indicate", "means", "mean", "otherwise",
                            "and", "or", "was", "were", "be", "been", "has", "had", "did", "does", "not", "no",
                            "any", "there", "in", "of", "case", "flag", "field", "value", "this", "that", "response",
                            "result", "call", "request", "operation", "action", "only"])
# a name made only of these says "the action happened" (verdict words such as verified / accepted are excluded)
_ACTION_PARTICIPLES = frozenset(["recorded", "saved", "created", "booked", "scheduled", "transferred", "closed",
                                 "done", "completed", "processed", "stored", "added", "logged", "executed",
                                 "performed"])
_PARTICIPLE_FILLERS = frozenset({"is", "was", "has", "been", "action", "request", "call"})


def _tokens(name: str) -> list[str]:
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name)
    return [part for part in re.split(r"[^A-Za-z0-9]+", spaced.lower()) if part]


def _hits(name: str, words: Iterable[str]) -> bool:
    """True when a word is one of the name's tokens, or (for words of 5+ letters) occurs inside the name."""
    lowered = name.lower()
    tokens = set(_tokens(name))
    return any(word in tokens or (len(word) >= 5 and word in lowered) for word in words)


def _is_date(spec: FieldSpec) -> bool:
    lowered = spec.name.lower()
    return spec.format == "date" or lowered.endswith("date") or lowered == "day"


_MINOR_NAME = re.compile(r"(?:^|_)(?:cents?|minor|pence|pennies|kurus|paise|mills|millimes)(?:_|$)")
_MINOR_TEXT = re.compile(r"\b(?:cents?|minor units?|pence|pennies|kuru[s\u015f]|paise|smallest (?:currency )?unit"
                         r"|thousandths|mills?|millimes?|x-minor-units|implied decimals?)\b", re.I)


def minor_units(spec: FieldSpec) -> bool:
    """True when the contract keeps this amount in minor units (cents): a ``cents`` / ``minor`` / ``pence`` name token,
    or an integer field whose description says so. ``minimum_*`` and plain integer amounts stay in major units."""
    if spec.type not in ("integer", "number"):
        return False
    if _MINOR_NAME.search("_".join(_tokens(spec.name))):
        return True
    return spec.type == "integer" and bool(_MINOR_TEXT.search(spec.description or ""))


#: The usual minor-unit scale: 100 minor units (cents) make one major unit.
DEFAULT_MINOR_SCALE = 100
_SCALE_DIGITS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4}
_SCALE_EXPONENTS = (
    re.compile(r"x-minor-units\s*[:=]?\s*(\d+)", re.I),
    re.compile(r"\b(\d+|zero|one|two|three|four)\s+(?:implied\s+)?(?:decimal places?|decimals?|decimal digits?"
               r"|fractional digits|digits after the decimal point)\b", re.I),
    re.compile(r"\b(?:exponent|scale)\s*(?:of|is|=|:)?\s*(\d+)\b", re.I),
)
_SCALE_FACTORS = (
    re.compile(r"\b(?:multipl(?:y|ied)|divided?|divides)\b[^.;]{0,40}?\bby\s+(10+)\b", re.I),
    re.compile(r"\btimes\s+(10+)\b", re.I),
    re.compile(r"\b1\s*/\s*(10+)\b"),
    re.compile(r"\b1\s+[A-Za-z]+\s*=\s*(10+)\s+[A-Za-z]", re.I),  # 1 dinar = 1000 fils
)
_SCALE_WORDS = ((re.compile(r"\b(?:thousandths|mills?|millimes?)\b", re.I), 1000),
                (re.compile(r"\b(?:hundredths|cents?|pence|pennies|paise|kuru[s\u015f])\b", re.I), 100))
_SCALE_CUE = re.compile(r"\b(?:decimal places?|decimals?|decimal digits?|exponent|scale|multipl(?:y|ied)|divided? by"
                        r"|x-minor-units)\b", re.I)


def minor_scale(spec: FieldSpec) -> tuple[int, str | None]:
    """How many minor units make one major unit for a minor-unit amount (:func:`minor_units`), and a compile note.

    The contract may state another scale than cents explicitly: ``thousandths`` / ``mills``, ``3 decimal places``,
    ``multiply by 1000``, ``x-minor-units: 3`` (in the field's name or description). A unique stated scale gives
    ``10 ** n``; nothing stated gives 100 (cents). A scale the text hints at but that cannot be read (no number,
    two different values, an exponent above 4) keeps 100 and returns a note saying so.
    """
    text = f"{spec.name.replace('_', ' ')} {spec.description or ''}"
    found: set[int] = set()
    bad = False
    for pattern in _SCALE_EXPONENTS:
        for match in pattern.finditer(text):
            raw = match.group(1).lower()
            n = _SCALE_DIGITS.get(raw, int(raw) if raw.isdigit() else -1)
            if re.fullmatch(r"10+", raw):  # ``scale of 1000``: the factor itself, not an exponent
                n = len(raw) - 1
            if 0 <= n <= 4:
                found.add(10 ** n)
            else:
                bad = True
    for pattern in _SCALE_FACTORS:
        for match in pattern.finditer(text):
            n = len(match.group(1)) - 1
            if n <= 4:
                found.add(10 ** n)
            else:
                bad = True
    for pattern, factor in _SCALE_WORDS:
        if pattern.search(text):
            found.add(factor)
    if len(found) == 1 and not bad:
        return found.pop(), None
    if found or bad or _SCALE_CUE.search(text):
        stated = ", ".join(str(f) for f in sorted(found)) or "unreadable"
        return DEFAULT_MINOR_SCALE, (f"the minor-unit scale of {spec.name!r} could not be read from the contract "
                                     f"(stated: {stated}); amounts are converted as cents (x100)")
    return DEFAULT_MINOR_SCALE, None


def _plain_strings(contract: ToolContract, exclude: Iterable[str]) -> list[FieldSpec]:
    """Request fields of type string that are not dates, enums or one of ``exclude``."""
    skip = set(exclude)
    return [s for s in contract.request_fields if s.type == "string" and not s.enum and not _is_date(s)
            and s.name not in skip]


def _field_meanings(contract: ToolContract, spec: FieldSpec) -> list[str]:
    """Schema prose and response clauses that describe this field, without borrowing a neighbour's meaning."""
    meanings = [spec.description.strip()] if spec.description.strip() else []
    for clause in re.split(r"[.;\n]\s*", contract.response_description or ""):
        mentions = list(re.finditer(r"`([A-Za-z_][A-Za-z0-9_]*)`", clause))
        for index, mention in enumerate(mentions):
            if mention.group(1) != spec.name:
                continue
            start = 0 if index == 0 else mention.start()
            end = mentions[index + 1].start() if index + 1 < len(mentions) else len(clause)
            meanings.append(clause[start:end].strip())
    return meanings


def _one_boolean(contract: ToolContract, candidates: Iterable[FieldSpec], role: str) -> str | None:
    """A unique supported role, never the first of several equally plausible fields."""
    names = list(dict.fromkeys(spec.name for spec in candidates))
    if len(names) > 1:
        raise CompileError(f"{contract.name} has ambiguous {role} fields: {', '.join(names)}; "
                           "the contract must distinguish their meanings")
    return names[0] if names else None


def _require_positive_flag(contract: ToolContract, meaning: str, role: str) -> None:
    """The hooks and routes require true to mean a positive result; inverted flags need a different mechanism."""
    polarity = re.search(r"\b(true|false)\b\s+(?:only\s+)?(?:if|when|means?|indicates?|denotes?)\b(.*)",
                         meaning, re.I)
    on = None if polarity else re.search(r"\b(true|false)\b\s+(?:on|upon)\b(.*)", meaning, re.I)
    if on is not None:  # "false on error", "true upon success": error / failure words are the negative side here
        rest = re.sub(r"\b(?:no|without)\s+(?:errors?|failures?)\b", "", on.group(2), flags=re.I)
        negative = bool(re.search(r"\b(?:not|never|failed|fails|unsuccessful|incorrect|invalid|wrong|errors?|"
                                  r"failures?)\b", rest, re.I))
        if (on.group(1).lower() == "true") == negative:
            raise CompileError(f"{contract.name} describes an inverted {role} flag; "
                               "the compiler requires true to mean a positive result")
        return
    if polarity is None:
        polarity = re.search(r"\b(?:if|when)\s+(?:the\s+(?:flag|field|value)\s+is\s+)?"
                             r"(true|false)\b[:,]?\s*(.*)", meaning, re.I)
    negative_words = r"\b(?:not|never|failed|fails|unsuccessful|incorrect|invalid|wrong)\b"
    if polarity:
        negative = bool(re.search(negative_words, polarity.group(2), re.I))
        if (polarity.group(1).lower() == "true") == negative:
            raise CompileError(f"{contract.name} describes an inverted {role} flag; "
                               "the compiler requires true to mean a positive result")
    elif re.search(negative_words, meaning, re.I) or re.search(r"\bfalse\b", meaning, re.I):
        raise CompileError(f"{contract.name} has ambiguous {role} flag polarity; "
                           "the contract must say whether true means a positive result")


def _identity_role(contract: ToolContract, spec: FieldSpec) -> str | None:
    """An answer verdict or an identity-wide verdict; unrelated booleans are not verification evidence."""
    meanings = _field_meanings(contract, spec)
    roles: set[str] = set()
    for meaning in meanings:
        words = set(_tokens(meaning))
        if not words & (_VERDICT_WORDS | {"incorrect", "invalid", "wrong", "failed"}):
            continue
        scope = words & {"identity", "security", "verification", "answer", "answers", "question", "questions"}
        if not scope:
            continue
        _require_positive_flag(contract, meaning, "verification")
        if words & set(_OVERALL_WORDS):
            roles.add("overall")
        elif scope & {"answer", "answers", "question", "questions"}:
            roles.add("answer")
        else:
            roles.add("overall")
    if len(roles) > 1:
        raise CompileError(f"{contract.name}.{spec.name} describes both an answer and overall verification; "
                           "the contract must distinguish their meanings")
    if roles:
        return roles.pop()
    if meanings:
        return None  # explicit but unrelated prose cannot be overridden by a suggestive field name
    words = set(_tokens(spec.name))
    qualifiers = set(_OVERALL_WORDS)
    context = {"is", "has", "are", "identity", "security", "verification", "check", "checks", "answer", "answers",
               "question", "questions", "result"}
    if not words & _VERDICT_WORDS or words - (_VERDICT_WORDS | qualifiers | context):
        return None
    if words & qualifiers or ("identity" in words and not words & {"answer", "answers", "question", "questions"}):
        return "overall"
    return "answer"


def _bare_success(spec: FieldSpec | None, meaning: str) -> bool:
    """Prose made only of success / failure words and function words ("True if successful.", "`ok` is false on error.").

    A closed vocabulary: any other content word ("audit", "metadata", "stored", "duplicate") means the text describes
    something else, so an auxiliary boolean never qualifies.
    """
    if spec is None:
        return False
    found = set(_tokens(re.sub(r"`[^`]*`", " ", meaning))) - set(_tokens(spec.name))
    return bool(found & _BARE_SUCCESS) and not found - _BARE_FUNCTION - _BARE_SUCCESS


def _action_success_meaning(contract: ToolContract, meaning: str, spec: FieldSpec | None = None) -> bool:
    """Recognise an action acknowledgement, not success of an arbitrary audit or auxiliary task."""
    generic = re.search(r"\b(?:action|operation|request|call|tool)\b[^.;]{0,48}"
                        r"\b(?:success(?:ful(?:ly)?)?|succeed(?:s|ed)?|performed|carried out|processed|completed"
                        r"|executed|went through)\b", meaning, re.I)
    subject = re.escape(_tokens(contract.name)[-1])
    action = re.search(rf"\b{subject}\b\s+(?:(?:is|was|were|has been|could|can|cannot|can't|did)\s+)?(?:not\s+)?"
                       r"(?:be\s+)?(?:successfully\s+)?"
                       r"(?:recorded|saved|created|booked|transferred|closed|retrieved|updated)\b", meaning, re.I)
    if not (generic or action or _bare_success(spec, meaning)):
        return False
    _require_positive_flag(contract, meaning, "action-success")
    return True


# --------------------------------------------------------------------------------------------------------------------
# success flag
# --------------------------------------------------------------------------------------------------------------------


def success_flag(contract: ToolContract) -> str | None:
    """The boolean response field that says the tool carried out its action, or ``None`` when the contract has none.

    Prefer unique action-success semantics in the field's own description or a response clause tied to that field.
    Otherwise accept a unique conventional success name with no contradictory prose, and as a last resort a unique
    undocumented boolean whose name only says the action happened (``recorded``, ``is_saved``). Plain success prose
    ("True if successful.", "`ok` is false on error.") counts as action-success semantics; inverted prose ("true on
    error") fails. Ambiguous or unexplained booleans fail compilation. A genuinely flag-less contract uses the result envelope; identity answer verdicts are
    handled separately and are never treated as action acknowledgements.
    """
    booleans = [spec for spec in contract.response_fields if spec.type == "boolean"]
    explicit = [spec for spec in booleans
                if any(_action_success_meaning(contract, text, spec) for text in _field_meanings(contract, spec))]
    named = [spec for spec in booleans if spec.name.lower() in _SUCCESS_NAMES and not _field_meanings(contract, spec)]
    if explicit or named:
        return _one_boolean(contract, [*explicit, *named], "action-success")
    if not booleans or (contract.name == "verify_identity"
                        and any(_identity_role(contract, spec) for spec in booleans)):
        return None
    # last resort: one undocumented boolean whose name only says the action happened (``recorded``, ``is_saved``);
    # verdict words (verified, accepted) are deliberately not in this list
    participles = [spec for spec in booleans if not _field_meanings(contract, spec)
                   and set(_tokens(spec.name)) & _ACTION_PARTICIPLES
                   and not set(_tokens(spec.name)) - _ACTION_PARTICIPLES - _PARTICIPLE_FILLERS]
    if participles:
        return _one_boolean(contract, participles, "action-success")
    raise CompileError(f"{contract.name} has no boolean with supported action-success semantics "
                       f"(response fields: {', '.join(spec.name for spec in booleans)})")


#: Values of a string / enum status that say the action was (or was not) carried out (AUD-04, string flags). They are
#: compared after :func:`status_value` (lower case, spaces and dashes as ``_``).
STATUS_SUCCESS = frozenset({"ok", "success", "succeeded", "recorded", "saved", "created", "accepted", "done",
                            "completed", "scheduled", "transferred", "true"})
STATUS_FAILURE = frozenset({"error", "failed", "failure", "rejected", "declined", "denied", "invalid", "not_recorded",
                            "false"})
_STATUS_NAMES = frozenset({"status", "result", "outcome", "state", "ack", "acknowledgement", "acknowledgment"})
_QUOTED = re.compile(r"[`'\"]([A-Za-z][A-Za-z _-]{0,30})[`'\"]")


def status_value(value: str) -> str:
    """The spelling a status value is compared in: lower case, trimmed, spaces and dashes as ``_``."""
    return "_".join(re.split(r"[\s\-]+", str(value).strip().lower()))


@dataclass(frozen=True)
class SuccessStatus:
    """A string response field whose value says whether the action was carried out (``status: recorded|rejected``).

    ``success`` are the values (in :func:`status_value` spelling) that acknowledge the action; any other value - a
    failure word or one the contract does not explain - is not an acknowledgement.
    """

    field: str
    success: tuple[str, ...]


def _status_values(contract: ToolContract, spec: FieldSpec) -> set[str]:
    """The values the contract documents for a string field: its enum, else the quoted words of its prose."""
    if spec.enum:
        return {status_value(v) for v in spec.enum if str(v).strip()}
    found: set[str] = set()
    for meaning in _field_meanings(contract, spec):
        found |= {status_value(m.group(1)) for m in _QUOTED.finditer(meaning)}
    return found - {status_value(spec.name)}


def success_status(contract: ToolContract) -> SuccessStatus | None:
    """The string / enum status field that acknowledges the action, when the contract has no success boolean.

    Only a field whose documented values (its enum, or the values its prose quotes) include a success or failure word
    qualifies: a field named like a status (``status``, ``result``, ``outcome``, ``state``), or an enum made only of
    success / failure words. The acknowledgement values are the documented success words (the standard success words
    when the contract documents only failure values). Several equally plausible fields, or none, give ``None``: the
    hooks then keep reading the result envelope (HTTP status). Never raises.
    """
    candidates: list[tuple[FieldSpec, set[str]]] = []
    for spec in contract.response_fields:
        if spec.type != "string":
            continue
        values = _status_values(contract, spec)
        known = values & (STATUS_SUCCESS | STATUS_FAILURE)
        if not known:
            continue
        named = bool(set(_tokens(spec.name)) & _STATUS_NAMES)
        if named or (spec.enum and values <= STATUS_SUCCESS | STATUS_FAILURE and values & STATUS_SUCCESS
                     and values & STATUS_FAILURE):
            candidates.append((spec, values))
    if len(candidates) > 1:
        candidates = [c for c in candidates if set(_tokens(c[0].name)) & _STATUS_NAMES]
    if len(candidates) != 1:
        return None
    spec, values = candidates[0]
    success = values & STATUS_SUCCESS
    if not success and spec.enum:
        return None  # a closed set without any success value could never acknowledge: keep the envelope
    return SuccessStatus(field=spec.name, success=tuple(sorted(success or STATUS_SUCCESS)))


# --------------------------------------------------------------------------------------------------------------------
# customer id
# --------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class CustomerBinding:
    """The request field that identifies the customer in every tool, and the call variable that fills it."""

    field: str = "customer_id"
    variable: str = "customer_id"

    @property
    def ref(self) -> str:
        """The ``{{variable}}`` reference to bake into a request."""
        return "{{" + self.variable + "}}"


def customer_binding(api: ToolApi, call_variables: Iterable[str] = ()) -> CustomerBinding:
    """Find the customer-id request field of the tool API and the call variable that carries its value.

    ``customer_id`` when some tool has it. Otherwise the required plain string field that most tools share, preferring a
    name that equals a call variable or says customer / caller / client. The variable is the field's own name when that is
    a call variable, else ``customer_id`` when declared, else the call variable that looks like an identifier.
    """
    variables = list(call_variables)
    names = Counter(
        spec.name for contract in api.tools.values() for spec in contract.request_fields
        if spec.required and spec.type == "string" and not spec.enum and not _is_date(spec)
    )
    field = "customer_id"
    if not any(spec.name == "customer_id" for c in api.tools.values() for spec in c.request_fields) and names:
        shared = [name for name, count in names.items() if count >= max(2, (len(api.tools) * 3 + 4) // 5)]
        if shared:
            field = max(shared, key=lambda n: (n in variables, _hits(n, _CUSTOMER_HINTS), names[n]))
    if field in variables:
        variable = field
    elif "customer_id" in variables:
        variable = "customer_id"
    else:
        variable = next((v for v in variables if _hits(v, ("customer", "caller", "client"))), field)
    return CustomerBinding(field, variable)


# --------------------------------------------------------------------------------------------------------------------
# close_call
# --------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class CloseRoles:
    """``outcome`` is the field that names how the call ended; ``reason`` the optional detail field (``None`` = none)."""

    outcome: str
    reason: str | None


def close_roles(contract: ToolContract, customer_field: str = "customer_id") -> CloseRoles:
    """Outcome and reason fields of ``close_call``.

    The outcome is the required enum string field that is not the customer id (names such as ``outcome`` or
    ``disposition`` win a tie); the reason is another string field called or described as the reason / detail.

    Raises:
        CompileError: When no field can hold the outcome.
    """
    fields = [s for s in contract.request_fields if s.name != customer_field and s.type == "string"]
    candidates = ([s for s in fields if s.enum and s.required] or [s for s in fields if s.enum]
                  or [s for s in fields if s.required])
    hinted = [s for s in candidates if _hits(s.name, _OUTCOME_HINTS)]
    if not (hinted or candidates):
        raise CompileError(
            f"close_call has no field the compiler can use for the outcome (a required string field with the allowed "
            f"outcomes); its request fields are: {', '.join(s.name for s in contract.request_fields) or 'none'}")
    outcome = (hinted or candidates)[0]
    rest = [s for s in fields if s.name != outcome.name]
    named = [s for s in rest if "reason" in s.name.lower()]
    described = [s for s in rest if _hits(s.name, _REASON_HINTS) or re.search(r"reason|detail|why", s.description, re.I)]
    chosen = (named or sorted(described, key=lambda s: not s.enum))[:1]
    return CloseRoles(outcome.name, chosen[0].name if chosen else None)


# --------------------------------------------------------------------------------------------------------------------
# get_account / create_promise / add_note
# --------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class AccountRoles:
    """The product list of ``get_account`` and the keys of its items: ``id_key`` (the product id), ``type_key`` (its kind)."""

    list_field: str = "products"
    id_key: str = "product_id"
    type_key: str = "type"
    minor_fields: tuple[str, ...] = ()  # item amounts the contract keeps in minor units (cents)
    #: ``(field, minor units per major unit)`` for the minor fields whose contract states another scale than 100
    minor_scales: tuple[tuple[str, int], ...] = ()
    #: compile notes about a minor-unit scale that could not be read (100 is used)
    scale_notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class PromiseRoles:
    """Product, amount and date fields of ``create_promise`` (``None`` = not found)."""

    product: str | None
    amount: str | None
    date: str | None


@dataclass(frozen=True)
class NoteRoles:
    """Product and reason fields of the notes tool."""

    product: str | None
    reason: str | None


def _id_like(spec: FieldSpec, *, integer: bool = False) -> bool:
    """A plain string field whose name reads like an identifier (``id``, ``product_id``, ``loan_ref``, ``account_number``).

    ``integer`` also accepts an integer-typed field (a contract may number its products); callers try strings first.
    """
    if spec.type not in (("string", "integer") if integer else ("string",)) or spec.enum or _is_date(spec):
        return False
    tokens = _tokens(spec.name)
    return bool(tokens) and (tokens[-1] in _ID_WORDS or spec.name.lower().endswith(("id", "ref", "code", "number", "key")))


def _product_field(contract: ToolContract, customer_field: str, id_key: str | None, exclude: Iterable[str] = ()) -> str | None:
    """The request field that carries the product id: the item key's name, an id-like name, or the only plain string."""
    plain = _plain_strings(contract, [customer_field, *exclude])
    if id_key and any(s.name == id_key for s in plain):
        return id_key
    id_like = [s for s in plain if _id_like(s)]
    if id_like:
        return sorted(id_like, key=lambda s: not _hits(s.name, _ID_HINTS))[0].name
    skip = {customer_field, *exclude}
    numbered = [s for s in contract.request_fields if s.type == "integer" and s.name not in skip
                and _id_like(s, integer=True)]  # integer ids only when no string id exists
    if id_key and any(s.name == id_key for s in numbered):
        return id_key
    if numbered:
        return sorted(numbered, key=lambda s: not _hits(s.name, _ID_HINTS))[0].name
    required = [s for s in plain if s.required]
    if len(required) == 1:
        return required[0].name
    return None


def _array_field(contract: ToolContract) -> FieldSpec | None:
    arrays = [s for s in contract.response_fields if s.type == "array"]
    return next((s for s in arrays if s.items), arrays[0] if arrays else None)


def account_roles(api: ToolApi, product_types: Iterable[str] = (), customer_field: str = "customer_id") -> AccountRoles:
    """Product list, id key and type key of ``get_account`` items.

    The id key is the item field the promise tool refers to (same name), else the only id-like string field; the type
    key is the item field whose enum covers the policy's ``product_types``. A contract without a response schema falls
    back to the promise tool's product field and the usual ``type`` name.

    Raises:
        CompileError: When the items have fields but none can be the product id.
    """
    contract = api.require("get_account")
    array = _array_field(contract)
    list_field = array.name if array is not None else "products"
    items = list(array.items or []) if array is not None else []
    promise = api.tools.get("create_promise")
    promise_product = _product_field(promise, customer_field, None) if promise is not None else None
    types = set(product_types)

    id_key: str | None = None
    strings = [s for s in items if s.type == "string" and not s.enum]
    numbered = [s for s in items if s.type == "integer" and _id_like(s, integer=True)]  # only when no string id fits
    same = ([s for s in strings if s.name == promise_product]
            or [s for s in numbered if s.name == promise_product])
    like = [s for s in strings if _id_like(s)] or numbered
    if same:
        id_key = same[0].name
    elif like:
        id_key = sorted(like, key=lambda s: not _hits(s.name, _ID_HINTS))[0].name
    elif not items:
        id_key = promise_product or "product_id"
    else:
        raise CompileError(
            "get_account returns products without a field the compiler can use as the product id "
            f"(fields: {', '.join(s.name for s in items)}); the promise tool cannot be pointed at a product")

    type_key = ""
    enums = [s for s in items if s.type == "string" and s.enum]
    scored = sorted(((len(types & set(s.enum or [])), s) for s in enums), key=lambda pair: -pair[0])
    if scored and scored[0][0] > 0:
        type_key = scored[0][1].name
    else:
        named = [s for s in items if s.type == "string" and _hits(s.name, _TYPE_HINTS)]
        if named:
            type_key = named[0].name
        elif len(enums) == 1:
            type_key = enums[0].name
        elif not items:
            type_key = "type" if types else ""
        elif types:
            raise CompileError(
                "get_account returns products without a field that holds the product type "
                f"(fields: {', '.join(s.name for s in items)}); the policy names its products by type "
                f"({', '.join(sorted(types))})")
    minor = [s for s in items if minor_units(s)]
    scales = tuple((s.name, scale) for s in minor for scale, _ in [minor_scale(s)] if scale != DEFAULT_MINOR_SCALE)
    notes = tuple(note for s in minor for _, note in [minor_scale(s)] if note)
    return AccountRoles(list_field, id_key, type_key, tuple(s.name for s in minor), scales, notes)


def promise_roles(contract: ToolContract, customer_field: str = "customer_id", id_key: str | None = None) -> PromiseRoles:
    """Product, amount and date fields of ``create_promise``.

    The date is the ``date``-format field; the amount a number-typed field (``amount`` / ``sum`` ... first, an integer
    field is never the amount unless its name says so); the product the field named like the account's id key, an id-like
    string, or the only required plain string.
    """
    fields = [s for s in contract.request_fields if s.name != customer_field]
    dates = [s for s in fields if _is_date(s)]
    date = next((s for s in dates if s.format == "date"), dates[0] if dates else None)
    numbers = [s for s in fields if s.type == "number"]
    named = [s for s in fields if s.type != "boolean" and not s.enum and not _is_date(s) and _hits(s.name, _AMOUNT_HINTS)]
    amount = (sorted(numbers, key=lambda s: not _hits(s.name, _AMOUNT_HINTS)) or named or [None])[0]
    if date is None:  # no date format and no ``*date`` name: the one other string field named or described as a day
        taken = {amount.name if amount else "", id_key or ""}
        dayish = [s for s in fields if s.type == "string" and not s.enum and s.name not in taken and not _id_like(s)
                  and (set(_tokens(s.name)) & {"date", "day", "on", "when"}
                       or re.search(r"\b(?:date|day|yyyy-mm-dd)\b", s.description or "", re.I))]
        date = dayish[0] if len(dayish) == 1 else None
    product = _product_field(contract, customer_field, id_key, exclude=[s.name for s in (date, amount) if s])
    return PromiseRoles(product, amount.name if amount else None, date.name if date else None)


def note_roles(contract: ToolContract, customer_field: str = "customer_id", id_key: str | None = None) -> NoteRoles:
    """Product and reason fields of the notes tool: the product id field and the (required) enum string field."""
    enums = [s for s in contract.request_fields if s.type == "string" and s.enum and s.name != customer_field]
    reason = next((s for s in enums if _hits(s.name, _REASON_HINTS)), (sorted(enums, key=lambda s: not s.required) or [None])[0])
    product = _product_field(contract, customer_field, id_key, exclude=[reason.name] if reason else [])
    return NoteRoles(product, reason.name if reason else None)


def queue_field(contract: ToolContract, customer_field: str = "customer_id") -> str | None:
    """The enum string field of the transfer tool that names the team the call goes to (``None`` = no such field)."""
    enums = [s for s in contract.request_fields if s.type == "string" and s.enum and s.name != customer_field]
    if not enums:
        return None
    preferred = [s for s in enums if _hits(s.name, ("queue", "team", "department", "destination", "target"))]
    return sorted(preferred or enums, key=lambda s: not s.required)[0].name


# --------------------------------------------------------------------------------------------------------------------
# verify_identity
# --------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class VerifyRoles:
    """The request fields of the identity tool: ``selector`` names the question, ``answer`` carries the answer."""

    selector: str
    answer: str


def verify_roles(contract: ToolContract, customer_field: str = "customer_id") -> VerifyRoles:
    """Selector (the enum field that says which question is answered) and answer fields of ``verify_identity``."""
    fields = [s for s in contract.request_fields if s.name != customer_field]
    enums = [s for s in fields if s.type == "string" and s.enum]
    selector = (sorted(enums, key=lambda s: not s.required) or [None])[0]
    selector_name = selector.name if selector is not None else "field"
    plain = [s for s in fields if s.type == "string" and not s.enum and s.name != selector_name]
    preferred = [s for s in plain if _hits(s.name, ("answer", "value", "response", "reply"))]
    answer = (preferred or sorted(plain, key=lambda s: not s.required) or [None])[0]
    return VerifyRoles(selector_name, answer.name if answer is not None else "answer")


@dataclass(frozen=True)
class VerifyShape:
    """Names in the ``verify_identity`` response that the workflow routes on (``None`` = the contract has no such field).

    ``left`` counts the wrong answers still allowed down to zero; ``wrong`` counts the wrong answers made so far up to the
    policy's limit; at most one of them is set (a counter whose direction is unclear is left to the model).
    """

    all_verified: str | None
    verified: str | None
    left: str | None
    wrong: str | None
    hint: str | None
    ok: str | None

    @property
    def counter(self) -> str | None:
        """The counter field the engine can route on (``left`` or ``wrong``), ``None`` when the model has to count."""
        return self.left or self.wrong


def verify_shape(contract: ToolContract, hint_values: Iterable[str] = ()) -> VerifyShape:
    """Find the response fields of the identity tool: overall flag, per-answer flag, counter, hint, success flag.

    Raises:
        CompileError: When no boolean tells whether an answer (or every answer) was accepted: the agent could not tell a
            right answer from a wrong one.
    """
    ok = success_flag(contract)
    booleans = [s for s in contract.response_fields if s.type == "boolean" and s.name != ok]
    overall = _one_boolean(contract, (s for s in booleans if _identity_role(contract, s) == "overall"),
                           "overall verification")
    per = _one_boolean(contract, (s for s in booleans if _identity_role(contract, s) == "answer"),
                       "answer verification")
    if not (per or overall):
        raise CompileError(
            "verify_identity returns no boolean that says whether an answer was accepted "
            f"(response fields: {', '.join(s.name for s in contract.response_fields) or 'none'}); the agent could not "
            "tell a right answer from a wrong one")
    counters = [s for s in contract.response_fields if s.type in ("integer", "number")]
    left = next((s for s in counters if _hits(s.name, _COUNTS_DOWN)
                 or re.search(r"\bleft\b|remaining", s.description or "", re.I)), None)
    wrong = None if left else next((s for s in counters if _hits(s.name, _COUNTS_UP)), None)
    wanted = set(hint_values)
    hints = [s for s in contract.response_fields if s.type == "string" and s.enum]
    hint = next((s for s in hints if wanted & set(s.enum or [])), None) or next(
        (s for s in hints if _hits(s.name, _HINT_WORDS)), hints[0] if len(hints) == 1 else None)
    return VerifyShape(
        all_verified=overall,
        verified=per,
        left=left.name if left else None,
        wrong=wrong.name if wrong else None,
        hint=hint.name if hint else None,
        ok=ok,
    )
