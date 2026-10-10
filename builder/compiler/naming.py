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
from dataclasses import dataclass

from builder.policy.ir import IntentRule, OutcomeRef, OutcomeRow, PolicyIR
from builder.policy.outcomes import TRANSFER_PREFIX, Situation, transfer_situation

__all__ = [
    "IntentExit",
    "closing_rows",
    "end_node",
    "handoff_node",
    "intent_exit",
    "negotiation_situations",
    "slug",
    "stop_situation",
    "transfer_queues",
    "transition_tool",
]

TRANSFER_TOOL = "transfer_to_agent"


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
    ``declined``: the ``end``/``note`` branch that applies when the customer declines the intent's ``offer``.
    """

    kind: str
    situation: str = ""
    queue: str = ""
    outcome: OutcomeRef | None = None
    reason: str = ""
    declined: IntentExit | None = None


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


def intent_exit(ir: PolicyIR, intent: IntentRule) -> IntentExit:
    """Classify an intent by what it does at the end (see :class:`IntentExit`)."""
    multi = ir.multi_product.enabled
    queue = intent.transfer_queue(TRANSFER_TOOL)
    declined: IntentExit | None = None
    if intent.declined_outcome is not None:
        if multi and intent.note_reason:
            declined = IntentExit("note", reason=intent.note_reason, outcome=intent.declined_outcome)
        else:
            declined = IntentExit(
                "end", situation=_situation_for(ir, intent, intent.declined_outcome), outcome=intent.declined_outcome
            )
    if queue:
        return IntentExit("handoff", situation=transfer_situation(queue), queue=queue, outcome=intent.outcome,
                          declined=declined)
    if multi and intent.note_reason:
        return IntentExit("note", reason=intent.note_reason, outcome=intent.outcome)
    if intent.outcome is not None:
        return IntentExit("end", situation=_situation_for(ir, intent, intent.outcome), outcome=intent.outcome)
    if declined is not None:
        return IntentExit("info", declined=declined)
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
