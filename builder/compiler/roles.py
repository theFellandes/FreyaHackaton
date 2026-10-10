"""Roles of the tool fields, derived from the bank's tool contracts - never from fixed names.

The hackathon keeps the seven tool names but lets a hidden brief change their fields and allowed values: the outcome
field of ``close_call`` may be called ``disposition``, the success flag of every response ``success``, the product key of
``get_account`` ``id``. A compiler that hard-codes ``outcome``, ``ok`` or ``product_id`` then produces an agent that
deploys cleanly and silently drops a field or misreads an answer. This module finds each *role* by what the contract says
(type, enum, ``required``, description, the way the tools refer to each other) and the compilers use the roles:

* :func:`success_flag` - the boolean that says a tool carried out its action;
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
    "AccountRoles",
    "CloseRoles",
    "CustomerBinding",
    "NoteRoles",
    "PromiseRoles",
    "VerifyRoles",
    "VerifyShape",
    "account_roles",
    "close_roles",
    "customer_binding",
    "note_roles",
    "promise_roles",
    "queue_field",
    "success_flag",
    "verify_roles",
    "verify_shape",
]

_SUCCESS_NAMES = ("ok", "success", "succeeded", "successful", "is_success", "is_ok", "done", "completed")
_OUTCOME_HINTS = ("outcome", "result", "disposition", "status", "resolution", "state", "code")
_REASON_HINTS = ("reason", "detail", "details", "why", "cause", "explanation", "comment", "note", "text")
_ID_WORDS = ("id", "ref", "code", "number", "no", "key", "reference", "identifier")
_ID_HINTS = ("product", "item", "account", "loan", "contract", "line")
_TYPE_HINTS = ("type", "kind", "category", "class")
_AMOUNT_HINTS = ("amount", "sum", "payment", "value", "total")
_CUSTOMER_HINTS = ("customer", "caller", "client", "user", "subscriber", "member", "holder", "ref", "id")
_OVERALL_WORDS = ("all", "overall", "fully", "complete", "completed", "every", "both", "entire", "whole")
_PER_ANSWER_WORDS = ("verified", "correct", "match", "matched", "valid", "right", "accepted", "answer")
_COUNTS_DOWN = ("left", "remaining", "remain", "available", "spare")
_COUNTS_UP = ("strikes", "strike", "wrong", "failed", "failures", "failure", "incorrect", "errors", "misses", "used")
_HINT_WORDS = ("hint", "suggestion", "advice", "tip", "clue", "guidance")


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


def _plain_strings(contract: ToolContract, exclude: Iterable[str]) -> list[FieldSpec]:
    """Request fields of type string that are not dates, enums or one of ``exclude``."""
    skip = set(exclude)
    return [s for s in contract.request_fields if s.type == "string" and not s.enum and not _is_date(s)
            and s.name not in skip]


# --------------------------------------------------------------------------------------------------------------------
# success flag
# --------------------------------------------------------------------------------------------------------------------


def success_flag(contract: ToolContract) -> str | None:
    """The boolean response field that says the tool carried out its action, or ``None`` when the contract has none.

    Order: the field the response description names in backticks (``` `ok` is false when the action was not carried
    out ```), a boolean with a conventional name (``ok``, ``success`` ...), a boolean whose own description speaks of
    success. ``None`` makes the hooks judge the call by the status of the result envelope instead.
    """
    booleans = [spec for spec in contract.response_fields if spec.type == "boolean"]
    names = {spec.name for spec in booleans}
    for match in re.finditer(r"`([A-Za-z_][A-Za-z0-9_]*)`", contract.response_description or ""):
        if match.group(1) in names:
            return match.group(1)
    for wanted in _SUCCESS_NAMES:
        for spec in booleans:
            if spec.name.lower() == wanted:
                return spec.name
    for spec in booleans:
        if re.search(r"success|succeeded|carried out|performed", spec.description or "", re.IGNORECASE):
            return spec.name
    return None


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


def _id_like(spec: FieldSpec) -> bool:
    """A plain string field whose name reads like an identifier (``id``, ``product_id``, ``loan_ref``, ``account_number``)."""
    if spec.type != "string" or spec.enum or _is_date(spec):
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
    same = [s for s in strings if s.name == promise_product]
    like = [s for s in strings if _id_like(s)]
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
    return AccountRoles(list_field, id_key, type_key)


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
    overall = [s for s in booleans if _hits(s.name, _OVERALL_WORDS) or re.search(r"\ball\b|every|both|overall", s.description or "", re.I)]
    rest = [s for s in booleans if s not in overall]
    per = [s for s in rest if _hits(s.name, _PER_ANSWER_WORDS)]
    loose = [s for s in rest if s not in per]
    if loose:
        if overall and not per:
            per = loose[:1]
        elif per and not overall:
            overall = loose[-1:]
        elif not per and not overall:
            if len(loose) >= 2:
                per, overall = loose[:1], loose[-1:]
            else:
                per = loose
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
        all_verified=overall[0].name if overall else None,
        verified=per[0].name if per else None,
        left=left.name if left else None,
        wrong=wrong.name if wrong else None,
        hint=hint.name if hint else None,
        ok=ok,
    )
