"""Names and situation keys shared by the code generators and the agent profiles.

The compiled call ends in one of a few *situations* (``already_paid``, ``flat_refusal``, ``wrong_number``,
``transfer:support`` ...). The outcome table of the IR is keyed by exactly these strings (see
:mod:`builder.policy.outcomes`). The agent reaches an outcome by moving to an ``end_<situation>`` code node (which only
records the situation in ``vars['chosen_end']``) or to a ``handoff_<queue>`` node (which transfers the call); the
``decide`` node then applies the IR's precedence to the recorded facts.

Everything here derives names from IR data; nothing is hard-coded to a brief. Where the IR's outcome table has no row for
a situation the compiler needs, :func:`closing_rows` adds a *synthesised* row built from the intent that causes it, so
the compiled ``decide`` node and the prompts always agree.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

from builder.policy.intents import canonical_intent_key
from builder.policy.ir import IntentRule, OutcomeRef, OutcomeRow, PolicyIR
from builder.policy.outcomes import TRANSFER_PREFIX, Situation, transfer_situation

__all__ = [
    "CALLBACK_TOOL",
    "CALL_LEVEL_INTENTS",
    "IntentExit",
    "books_callback",
    "closing_rows",
    "end_node",
    "handoff_node",
    "intent_exit",
    "is_call_level",
    "negotiation_situations",
    "note_reason_of",
    "slug",
    "stop_situation",
    "transfer_queues",
    "transition_tool",
]

TRANSFER_TOOL = "transfer_to_agent"
CALLBACK_TOOL = "schedule_callback"

#: Catalog intents about the call as a whole, not about one product: in a several-product call they close the call
#: (after noting every product still open) instead of "note this product and continue with the others" (audit AUD-11).
CALL_LEVEL_INTENTS = frozenset({"refuse_to_talk"})


def slug(text: str) -> str:
    """Lower-case identifier made of ``a-z``, ``0-9`` and ``_`` (``transfer:support`` -> ``transfer_support``)."""
    cleaned = re.sub(r"[^a-z0-9]+", "_", str(text).lower()).strip("_")
    return cleaned or "x"


def end_node(situation: str) -> str:
    """Node name of the code node that records ``situation`` as the way the call ended."""
    return f"end_{slug(situation)}"


def handoff_node(queue: str) -> str:
    """Node name of the ``api_request`` node that transfers the call to ``queue``."""
    return f"handoff_{slug(queue)}"


def transition_tool(node: str) -> str:
    """Name of the tool the dialogue model sees for a tool-call transition into ``node``."""
    return f"transition_to_{node}"


@dataclass(frozen=True, slots=True)
class IntentExit:
    """What an intent does once it is acted on.

    ``kind``: ``end`` (move to ``end_<situation>``), ``handoff`` (transfer to ``queue``, then decide), ``note`` (the
    multi-product notes tool with ``reason``; the call continues) or ``info`` (guidance only, no exit).
    ``reason`` on an ``end`` exit: the note reason written for every product without a promise before the call ends (a
    call-level intent in a several-product call, or a note action in a single-product one).
    ``callback`` on an ``end`` exit: the intent books a callback first (its ``schedule_callback`` action); the exit is
    the "no time could be agreed" branch, a booked callback ends the call on the tool's acknowledgement (AUD-08).
    ``declined``: the ``end``/``note`` branch that applies when the customer declines the intent's ``offer``.
    """

    kind: str
    situation: str = ""
    queue: str = ""
    outcome: OutcomeRef | None = None
    reason: str = ""
    declined: IntentExit | None = None
    callback: bool = False


def transfer_queues(ir: PolicyIR) -> list[str]:
    """Queues of ``transfer_to_agent`` named anywhere in the IR (intents, outcome rows, precedence), first-seen order."""
    queues: list[str] = []

    def add(queue: str | None) -> None:
        if queue and queue not in queues:
            queues.append(queue)

    for intent in ir.intents:
        add(intent.transfer_queue(TRANSFER_TOOL))
    for row in ir.closing.rows:
        if row.situation.startswith(TRANSFER_PREFIX):
            add(row.situation[len(TRANSFER_PREFIX):])
    for key in ir.closing.precedence:
        if key.startswith(TRANSFER_PREFIX):
            add(key[len(TRANSFER_PREFIX):])
    return queues


def _matching_row(ir: PolicyIR, ref: OutcomeRef, *, verified: bool | None) -> OutcomeRow | None:
    """The first outcome row with the same outcome and reason (and a compatible verification qualifier)."""
    for row in ir.closing.rows:
        if row.outcome == ref.outcome and row.reason == ref.reason and (
            verified is None or row.requires_verified in (None, verified)
        ):
            return row
    return None


def _situation_for(ir: PolicyIR, intent: IntentRule, ref: OutcomeRef) -> str:
    """Situation key under which ``ref`` (an outcome of ``intent``) is looked up in the outcome table."""
    verified = {"before_verification": False, "after_verification": True}.get(intent.scope)
    if any(row.situation == intent.key and row.ref() == ref for row in ir.closing.rows):
        return intent.key
    row = _matching_row(ir, ref, verified=verified)
    if row is not None and not row.situation.startswith(TRANSFER_PREFIX):
        return row.situation
    return intent.key


def books_callback(intent: IntentRule) -> bool:
    """True when ``intent`` has a ``schedule_callback`` action (and does not transfer the call)."""
    return intent.transfer_queue(TRANSFER_TOOL) is None and any(a.tool == CALLBACK_TOOL for a in intent.actions)


def is_call_level(intent: IntentRule) -> bool:
    """True for an intent about the call as a whole (see :data:`CALL_LEVEL_INTENTS`)."""
    return canonical_intent_key(intent.key) in CALL_LEVEL_INTENTS


def note_reason_of(ir: PolicyIR, intent: IntentRule) -> str:
    """The note reason of ``intent``: its ``note_reason``, else the reason parameter of a notes-tool action (AUD-08).

    The extraction sometimes writes "add a note with reason X" as an action of the intent instead of ``note_reason``;
    both mean the same, and the action form was silently dropped. The reason parameter is the action's only parameter
    whose value is not a placeholder, preferring a key named like a reason.
    """
    if intent.note_reason:
        return intent.note_reason
    tool = ir.multi_product.note_tool or "add_note"
    for action in intent.actions:
        if action.tool != tool:
            continue
        values = [(k, v) for k, v in action.params.items() if v and "{" not in v]
        named = [v for k, v in values if "reason" in k.lower()]
        if named:
            return named[0]
        if len(values) == 1:
            return values[0][1]
    return ""


def _row_exit(ir: PolicyIR, intent: IntentRule, reason: str = "") -> IntentExit | None:
    """An ``end`` exit for an intent with no outcome of its own whose key names an outcome-table row (AUD-11).

    The table already says how the call closes in that situation, so the intent must lead there (the ``decide`` node
    then picks the verified or unverified row). The persuasion stop situation is excluded: it is reached through the
    persuasion rule, never as an intent exit. ``outcome`` is the row's outcome when every row of the key that fits the
    intent's scope agrees, else ``None`` (a lean prompt then lets the model pick the row).
    """
    if intent.key == stop_situation(ir) or intent.key.startswith(TRANSFER_PREFIX):
        return None
    verified = {"before_verification": False, "after_verification": True}.get(intent.scope)
    rows = [row for row in ir.closing.rows if row.situation == intent.key
            and (verified is None or row.requires_verified in (None, verified))]
    if not rows:
        return None
    refs = {(row.outcome, row.reason) for row in rows}
    return IntentExit("end", situation=intent.key, outcome=rows[0].ref() if len(refs) == 1 else None, reason=reason)


def intent_exit(ir: PolicyIR, intent: IntentRule) -> IntentExit:
    """Classify an intent by what it does at the end (see :class:`IntentExit`)."""
    multi = ir.multi_product.enabled
    queue = intent.transfer_queue(TRANSFER_TOOL)
    note = note_reason_of(ir, intent)
    declined: IntentExit | None = None
    if intent.declined_outcome is not None:
        if multi and note:
            declined = IntentExit("note", reason=note, outcome=intent.declined_outcome)
        else:
            declined = IntentExit(
                "end", situation=_situation_for(ir, intent, intent.declined_outcome), outcome=intent.declined_outcome
            )
    if queue:
        return IntentExit("handoff", situation=transfer_situation(queue), queue=queue, outcome=intent.outcome,
                          declined=declined)
    if multi and note:
        if is_call_level(intent):
            if intent.outcome is not None:
                return IntentExit("end", situation=_situation_for(ir, intent, intent.outcome), outcome=intent.outcome,
                                  reason=note)
            row_exit = _row_exit(ir, intent, note)
            if row_exit is not None:
                return row_exit
        return IntentExit("note", reason=note, outcome=intent.outcome)
    callback = books_callback(intent)
    if intent.outcome is not None:
        return IntentExit("end", situation=_situation_for(ir, intent, intent.outcome), outcome=intent.outcome,
                          reason=note, callback=callback)
    if declined is not None:
        return IntentExit("info", declined=declined)
    found = _row_exit(ir, intent, note)
    if found is not None:
        return replace(found, callback=callback)
    if callback:  # "call me back later" with no outcome of its own: the callback situations of the outcome table
        success = ir.third_party.callback.success if ir.third_party.callback else None
        return IntentExit("end", situation=str(Situation.CALLBACK_BOOKED), outcome=success, callback=True)
    return IntentExit("info")


def stop_situation(ir: PolicyIR) -> str:
    """Situation key of "persuasion attempts used up": the outcome row equal to ``persuasion.stop_outcome``."""
    if any(row.situation == "flat_refusal" for row in ir.closing.rows):
        return "flat_refusal"
    row = _matching_row(ir, ir.persuasion.stop_outcome, verified=True)
    return row.situation if row is not None and not row.situation.startswith(TRANSFER_PREFIX) else "flat_refusal"


def closing_rows(ir: PolicyIR) -> list[OutcomeRow]:
    """The IR's outcome rows plus synthesised ones for situations the agent can end in but the table does not list.

    Synthesised rows: the persuasion stop situation, every intent exit (``end`` and ``handoff``) and the structural
    situations with an outcome in their own IR section (callback, wrong number, deceased, unclear party, identity
    failed / declined, recording failed). They are appended, so table rows always win.
    """
    rows = list(ir.closing.rows)

    def have(situation: str) -> bool:
        return any(row.situation == situation for row in rows)

    def add(situation: str, ref: OutcomeRef | None) -> None:
        if ref is not None and not have(situation):
            rows.append(OutcomeRow(outcome=ref.outcome, reason=ref.reason, situation=situation,
                                   when="(derived from the policy sections)"))

    add(stop_situation(ir), ir.persuasion.stop_outcome)
    for intent in ir.intents:
        info = intent_exit(ir, intent)
        if info.kind in ("end", "handoff") and info.situation and info.outcome is not None:
            add(info.situation, info.outcome)
        if info.declined is not None and info.declined.kind == "end" and info.declined.outcome is not None:
            add(info.declined.situation, info.declined.outcome)
    callback = ir.third_party.callback
    add(Situation.CALLBACK_BOOKED, callback.success if callback else None)
    add(Situation.CALLBACK_NOT_BOOKED, callback.failure if callback else None)
    add(Situation.WRONG_NUMBER, ir.third_party.wrong_number)
    add(Situation.DECEASED, ir.third_party.deceased)
    add(Situation.UNCLEAR_PARTY, ir.right_party.unclear_outcome)
    add(Situation.IDENTITY_FAILED, ir.identity.on_failed)
    add(Situation.IDENTITY_DECLINED, ir.identity.on_declined)
    add(Situation.RECORDING_FAILED, ir.recording.failure_outcome)
    return rows


def negotiation_situations(ir: PolicyIR) -> list[str]:
    """Situation keys the customer can end the verified part of the call in (used by the ``call_situation`` fallback).

    These are the ``end`` exits of intents in force after verification, the persuasion stop situation and, with
    several products, nothing else (notes are logged by the notes tool, not by situations).
    """
    keys: list[str] = []
    for intent in ir.intents:
        if intent.scope == "before_verification":
            continue
        info = intent_exit(ir, intent)
        for candidate in (info, info.declined):
            if candidate is not None and candidate.kind == "end" and candidate.situation not in keys:
                keys.append(candidate.situation)
    stop = stop_situation(ir)
    if stop not in keys:
        keys.append(stop)
    return keys
