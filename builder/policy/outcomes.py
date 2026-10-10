"""Reference outcome engine: which ``close_call`` outcome and reason a finished call gets.

The compiler's ``decide_outcome`` code-node snippet is a mirror of :func:`compute_outcome`; both are tested against
the same vectors (the practice cases' expected outcomes and edge cases). The algorithm is generic: every outcome name,
reason, transfer queue and note reason comes from the IR (:class:`~builder.policy.ir.Closing`,
:class:`~builder.policy.ir.MultiProduct` and the sections' ``OutcomeRef`` fields), never from this module.

How a call is summarised
------------------------
A :class:`CallSituation` holds the facts the agent can observe at the end of a call: who answered, whether identity
was verified / failed / declined, recorded promises, per-product notes, the transfer queue, callback state, whether
recording failed beyond its retries, and the *situation keys* detected in the conversation (intent keys such as
``already_paid`` or ``flat_refusal``).

Situation keys
--------------
:func:`active_situations` turns the facts into keys. Structural keys (:class:`Situation`) are generic vocabulary;
transfers become ``transfer:<queue>`` (queue value from the tool API); every key in ``CallSituation.situations`` is
used as it is. Outcome-table rows are keyed by these strings.

Decision order (:func:`decide_outcome`)
---------------------------------------
1. A recorded promise plus a transfer, with ``closing.transfer_after_promise_outcome`` set → that outcome; a missing
   reason is taken from the transfer's own row.
2. A recorded promise with ``closing.promise_wins`` → the ``promise_recorded`` row.
3. Otherwise walk ``closing.resolved_precedence()`` (then the structural keys it omits) and return the first active
   key that resolves: ``product_notes`` through the multi-product notes rule, any other key through its row (rows may
   be limited to verified or unverified calls) or, for structural keys without a row, the IR section's own
   ``OutcomeRef`` (e.g. ``identity.on_failed``).
4. Fallback when nothing resolves: ``persuasion.stop_outcome`` for a verified call, ``identity.on_declined`` when the
   customer was on the line but not verified, else ``right_party.unclear_outcome``.

The paid invariant (multi-product notes)
----------------------------------------
With no promise and the ``first_non_paid_note`` rule, the call closes as "all paid" only when **every known product**
(``CallSituation.product_order``, the ``get_account`` order) has a note with the paid reason: a product without a note
is not paid. When every existing note is paid but some product has none, the notes rule does not decide and the walk
continues with the other active keys (the situation the customer was in, else the fallback). When a note is not paid,
the first such note in product order decides, whatever the other products have. Several notes for one product: the
latest one counts. Without a ``product_order`` the noted products are the known products.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal, NamedTuple

from builder.policy.ir import OutcomeRef, PolicyIR

__all__ = [
    "STRUCTURAL_ORDER",
    "TRANSFER_PREFIX",
    "CallSituation",
    "NoteRecord",
    "OutcomeDecision",
    "Party",
    "PromiseRecord",
    "Situation",
    "active_situations",
    "compute_outcome",
    "decide_outcome",
    "is_transfer_situation",
    "transfer_situation",
]

Party = Literal["confirmed", "third_party", "wrong_number", "deceased", "unclear"]
TRANSFER_PREFIX = "transfer:"


class Situation(StrEnum):
    """Generic structural situation keys derived from call facts (outcome-table rows are keyed by these)."""

    PROMISE_RECORDED = "promise_recorded"
    PRODUCT_NOTES = "product_notes"
    RECORDING_FAILED = "recording_failed"
    IDENTITY_FAILED = "identity_failed"
    IDENTITY_DECLINED = "identity_declined"
    WRONG_NUMBER = "wrong_number"
    DECEASED = "deceased"
    UNCLEAR_PARTY = "unclear_party"
    CALLBACK_BOOKED = "callback_booked"
    CALLBACK_NOT_BOOKED = "callback_not_booked"


STRUCTURAL_ORDER: tuple[str, ...] = (
    Situation.PROMISE_RECORDED,
    Situation.PRODUCT_NOTES,
    Situation.RECORDING_FAILED,
    Situation.IDENTITY_FAILED,
    Situation.IDENTITY_DECLINED,
    Situation.WRONG_NUMBER,
    Situation.DECEASED,
    Situation.CALLBACK_BOOKED,
    Situation.CALLBACK_NOT_BOOKED,
    Situation.UNCLEAR_PARTY,
)
"""Order in which structural keys are tried when the IR's precedence list omits them."""


def transfer_situation(queue: str) -> str:
    """Situation key of a transfer to ``queue`` (``transfer:<queue>``)."""
    return f"{TRANSFER_PREFIX}{queue}"


def is_transfer_situation(key: str) -> bool:
    """True for ``transfer:<queue>`` keys."""
    return key.startswith(TRANSFER_PREFIX)


class PromiseRecord(NamedTuple):
    """A promise recorded by ``create_promise`` (``ok: true``)."""

    product_id: str
    amount: float
    date: str


class NoteRecord(NamedTuple):
    """A per-product note recorded by the notes tool."""

    product_id: str
    reason: str


@dataclass(slots=True)
class CallSituation:
    """Facts observable at the end of a call; the input of :func:`compute_outcome`.

    * ``party``: who answered (``confirmed`` = the customer).
    * ``verified`` / ``identity_failed`` / ``identity_declined``: identity check state.
    * ``promises`` / ``notes``: recorded tool calls; ``product_order`` = product ids in ``get_account`` order
      (used by the first-non-paid-note rule; notes are taken in recorded order when it is empty).
    * ``transfer_queue``: queue of a successful transfer; ``transfer_reason``: optional intent key that caused it
      (added to the active situations, so rows may key on the intent instead of the queue).
    * ``situations``: intent/situation keys detected in the conversation.
    * ``callback_booked`` / ``callback_failed``: callback state for a third party.
    * ``service_error``: recording failed beyond its retries.
    """

    party: Party = "confirmed"
    verified: bool = False
    identity_failed: bool = False
    identity_declined: bool = False
    promises: list[PromiseRecord] = field(default_factory=list)
    notes: list[NoteRecord] = field(default_factory=list)
    transfer_queue: str | None = None
    transfer_reason: str | None = None
    situations: set[str] = field(default_factory=set)
    callback_booked: bool = False
    callback_failed: bool = False
    service_error: bool = False
    product_order: list[str] = field(default_factory=list)

    @classmethod
    def from_records(
        cls,
        *,
        promises: Iterable[Mapping[str, Any]] = (),
        notes: Iterable[Mapping[str, Any]] = (),
        transfers: Iterable[Mapping[str, Any]] = (),
        callbacks: Iterable[Mapping[str, Any]] = (),
        **facts: Any,
    ) -> CallSituation:
        """Build from ledger-shaped records (lists of dicts as the bank ledger reports them) plus other facts.

        ``promises``: ``{product_id, amount, date}``; ``notes``: ``{product_id, reason}``; ``transfers``:
        ``{queue}`` (the last one counts); ``callbacks``: any non-empty list means a callback was booked.
        Keyword ``facts`` set the remaining fields (``party``, ``verified``, ``situations``, ...).
        """
        transfer_list = [t for t in transfers if t.get("queue")]
        situation = cls(
            promises=[PromiseRecord(str(p["product_id"]), float(p["amount"]), str(p["date"])) for p in promises],
            notes=[NoteRecord(str(n.get("product_id", "")), str(n["reason"])) for n in notes],
            transfer_queue=str(transfer_list[-1]["queue"]) if transfer_list else None,
            **facts,
        )
        if any(True for _ in callbacks):
            situation.callback_booked = True
        situation.situations = set(situation.situations)
        return situation


DecisionSource = Literal["transfer_after_promise", "promise_wins", "precedence", "notes", "section", "fallback"]


@dataclass(frozen=True, slots=True)
class OutcomeDecision:
    """The chosen outcome with an explanation: the situation key that decided it and why (for traces/debugging)."""

    outcome: OutcomeRef
    situation: str
    source: DecisionSource
    rule_ids: tuple[str, ...] = ()
    active: tuple[str, ...] = ()


# --------------------------------------------------------------------------------------------------------------------
# Situations
# --------------------------------------------------------------------------------------------------------------------


def active_situations(ir: PolicyIR, s: CallSituation) -> list[str]:
    """Situation keys that hold for the call, structural keys first (in :data:`STRUCTURAL_ORDER`), then the rest."""
    structural: list[str] = []
    if s.promises:
        structural.append(Situation.PROMISE_RECORDED)
    if s.notes and ir.multi_product.enabled:
        structural.append(Situation.PRODUCT_NOTES)
    if s.service_error and not s.promises:
        structural.append(Situation.RECORDING_FAILED)
    if s.identity_failed:
        structural.append(Situation.IDENTITY_FAILED)
    if s.identity_declined:
        structural.append(Situation.IDENTITY_DECLINED)
    structural.extend(_party_situations(s))
    others: list[str] = []
    if s.transfer_queue:
        others.append(transfer_situation(s.transfer_queue))
    for key in [s.transfer_reason or "", *sorted(s.situations)]:
        if key and key not in others and key not in structural:
            others.append(key)
    return [str(key) for key in structural] + others


def _party_situations(s: CallSituation) -> list[str]:
    if s.party == "wrong_number":
        return [Situation.WRONG_NUMBER]
    if s.party == "deceased":
        return [Situation.DECEASED]
    if s.party == "unclear":
        return [Situation.UNCLEAR_PARTY]
    if s.party == "third_party":
        return [Situation.CALLBACK_BOOKED if s.callback_booked else Situation.CALLBACK_NOT_BOOKED]
    return []


# --------------------------------------------------------------------------------------------------------------------
# Decision
# --------------------------------------------------------------------------------------------------------------------


def compute_outcome(ir: PolicyIR, s: CallSituation) -> OutcomeRef:
    """The ``close_call`` outcome and reason for a finished call (see the module docstring for the order)."""
    return decide_outcome(ir, s).outcome


def decide_outcome(ir: PolicyIR, s: CallSituation) -> OutcomeDecision:
    """Like :func:`compute_outcome`, with the deciding situation key, the rule ids behind it and the active keys."""
    active = active_situations(ir, s)
    active_set = set(active)
    closing = ir.closing
    order = _search_order(closing.resolved_precedence())
    promised = Situation.PROMISE_RECORDED in active_set
    transfer_key = next((key for key in order if is_transfer_situation(key) and key in active_set), None)
    if transfer_key is None:
        transfer_key = next((key for key in active if is_transfer_situation(key)), None)

    if promised and transfer_key and closing.transfer_after_promise_outcome is not None:
        decision = _transfer_after_promise(ir, s, transfer_key, closing.transfer_after_promise_outcome)
        return _with_active(decision, active)
    if promised and closing.promise_wins:
        decision = _resolve(ir, s, Situation.PROMISE_RECORDED)
        if decision is not None:
            return _with_active(_relabel(decision, "promise_wins"), active)

    for key in order:
        if key not in active_set:
            continue
        decision = _resolve(ir, s, key)
        if decision is not None:
            return _with_active(decision, active)
    return _with_active(_fallback(ir, s), active)


def _search_order(precedence: Sequence[str]) -> list[str]:
    order = list(precedence)
    order.extend(key for key in STRUCTURAL_ORDER if key not in order)
    return order


def _with_active(decision: OutcomeDecision, active: Sequence[str]) -> OutcomeDecision:
    return OutcomeDecision(decision.outcome, str(decision.situation), decision.source, decision.rule_ids,
                           tuple(active))


def _relabel(decision: OutcomeDecision, source: DecisionSource) -> OutcomeDecision:
    return OutcomeDecision(decision.outcome, decision.situation, source, decision.rule_ids, decision.active)


def _transfer_after_promise(
    ir: PolicyIR, s: CallSituation, transfer_key: str, configured: OutcomeRef
) -> OutcomeDecision:
    reason = configured.reason
    rule_ids = list(ir.closing.rule_ids)
    if reason is None:
        row = ir.closing.row_for(transfer_key, s.verified)
        if row is None and s.transfer_reason:
            row = ir.closing.row_for(s.transfer_reason, s.verified)
        if row is not None:
            reason = row.reason
            rule_ids.extend(row.rule_ids)
    return OutcomeDecision(OutcomeRef(outcome=configured.outcome, reason=reason), transfer_key,
                           "transfer_after_promise", tuple(dict.fromkeys(rule_ids)))


def _resolve(ir: PolicyIR, s: CallSituation, key: str) -> OutcomeDecision | None:
    if key == Situation.PRODUCT_NOTES:
        notes_decision = _notes_outcome(ir, s)
        if notes_decision is not None:
            return notes_decision
    row = ir.closing.row_for(key, s.verified)
    if row is not None:
        return OutcomeDecision(row.ref(), key, "precedence", tuple(row.rule_ids))
    section_ref = _section_outcome(ir, key)
    if section_ref is not None:
        return OutcomeDecision(section_ref, key, "section")
    return None


def _section_outcome(ir: PolicyIR, key: str) -> OutcomeRef | None:
    """The outcome a structural key gets from its own IR section when the outcome table has no row for it."""
    callback = ir.third_party.callback
    lookup: dict[str, OutcomeRef | None] = {
        Situation.CALLBACK_BOOKED: callback.success if callback else None,
        Situation.CALLBACK_NOT_BOOKED: callback.failure if callback else None,
        Situation.WRONG_NUMBER: ir.third_party.wrong_number,
        Situation.DECEASED: ir.third_party.deceased,
        Situation.UNCLEAR_PARTY: ir.right_party.unclear_outcome,
        Situation.IDENTITY_FAILED: ir.identity.on_failed,
        Situation.IDENTITY_DECLINED: ir.identity.on_declined,
        Situation.RECORDING_FAILED: ir.recording.failure_outcome,
    }
    return lookup.get(key)


def _notes_outcome(ir: PolicyIR, s: CallSituation) -> OutcomeDecision | None:
    """Apply the multi-product notes closing rule; ``None`` when it does not decide (rule ``fixed``, a product without a note)."""
    mp = ir.multi_product
    if not s.notes or mp.closing_reason_rule != "first_non_paid_note":
        return None
    rule_ids = tuple(mp.rule_ids)
    paid = mp.paid_note_reason
    noted = _notes_in_product_order(s)
    unpaid = [note.reason for note in noted if note.reason != paid]
    if not unpaid:
        known = s.product_order or [note.product_id for note in noted]
        covered = {note.product_id for note in noted}
        if paid is None or mp.all_paid_outcome is None or any(product_id not in covered for product_id in known):
            return None  # paid needs a paid note for EVERY known product; a product with no note is not paid
        return OutcomeDecision(mp.all_paid_outcome, Situation.PRODUCT_NOTES, "notes", rule_ids)
    first = unpaid[0]
    outcome = mp.notes_outcome or _outcome_for_reason(ir, first)
    if outcome is None:
        return None
    return OutcomeDecision(OutcomeRef(outcome=outcome, reason=first), Situation.PRODUCT_NOTES, "notes", rule_ids)


def _notes_in_product_order(s: CallSituation) -> list[NoteRecord]:
    """One note per product (the latest one counts), in ``get_account`` order; unknown products last, in first-noted order."""
    latest: dict[str, NoteRecord] = {}
    for note in s.notes:
        latest[note.product_id] = note  # a dict keeps the first position of a key: same order as the generated code
    notes = list(latest.values())
    if not s.product_order:
        return notes
    rank = {product_id: index for index, product_id in enumerate(s.product_order)}
    return sorted(notes, key=lambda note: rank.get(note.product_id, len(rank)))


def _outcome_for_reason(ir: PolicyIR, reason: str) -> str | None:
    return next((row.outcome for row in ir.closing.rows if row.reason == reason), None)


def _fallback(ir: PolicyIR, s: CallSituation) -> OutcomeDecision:
    if s.verified:
        return OutcomeDecision(ir.persuasion.stop_outcome, "fallback_verified", "fallback",
                               tuple(ir.persuasion.rule_ids))
    if s.party == "confirmed" or ir.right_party.unclear_outcome is None:
        return OutcomeDecision(ir.identity.on_declined, "fallback_unverified", "fallback", tuple(ir.identity.rule_ids))
    return OutcomeDecision(ir.right_party.unclear_outcome, "fallback_unreached", "fallback",
                           tuple(ir.right_party.rule_ids))
