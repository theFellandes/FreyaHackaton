"""Seed catalog of customer intents in collection calls.

The catalog only *names* things to look for, with neutral detection descriptions and generic examples. An intent
becomes behaviour only when a brief defines handling for it: then the IR carries an
:class:`~builder.policy.ir.IntentRule` with the brief's response, actions, outcome and rule ids. A catalog intent the
brief does not handle produces no behaviour. A brief may also define new keys; they are used as they are.

Keys double as *situation keys* for the outcome table (see :mod:`builder.policy.outcomes`), so a row keyed
``already_paid`` applies when the call ended on that intent.

Prior art: the objection taxonomy of an earlier collections project (R5: forgot, can't afford, dispute, already paid,
wrong person, will pay later) is mapped onto these keys through :data:`INTENT_ALIASES`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

__all__ = [
    "INTENT_ALIASES",
    "INTENT_CATALOG",
    "IntentSpec",
    "canonical_intent_key",
    "catalog_keys",
    "describe_catalog",
    "get_intent_spec",
    "is_catalog_intent",
]

TypicalScope = Literal["any", "before_verification", "after_verification"]


@dataclass(frozen=True, slots=True)
class IntentSpec:
    """A catalog intent: key, neutral detection description, generic example utterances and usual stage."""

    key: str
    description: str
    examples: tuple[str, ...]
    typical_scope: TypicalScope


_SPECS: tuple[IntentSpec, ...] = (
    # --- who is on the line ---------------------------------------------------------------------------------------
    IntentSpec("right_party_confirmed", "The person confirms they are the customer the agent asked for.",
               ("yes, speaking", "that's me"), "before_verification"),
    IntentSpec("third_party", "Someone else answered: a relative, colleague or other person, not the customer.",
               ("she's not home right now", "I'm his brother"), "before_verification"),
    IntentSpec("wrong_number", "The number does not belong to the customer at all.",
               ("nobody by that name lives here", "you have the wrong number"), "before_verification"),
    IntentSpec("deceased", "The person says the customer has died.",
               ("he passed away last year",), "before_verification"),
    IntentSpec("unclear_party", "Greeting or question that neither confirms nor denies being the customer.",
               ("hello?", "who's calling?"), "before_verification"),
    # --- questions about the call ---------------------------------------------------------------------------------
    IntentSpec("robot_question", "Asks whether the caller is a machine, a bot or an AI.",
               ("am I talking to a machine?",), "any"),
    IntentSpec("scam_question", "Doubts that the call is genuine or asks how to check it.",
               ("how do I know this call is real?",), "any"),
    IntentSpec("unknown_question", "Asks something the agent cannot answer from its tools and facts.",
               ("what's the interest rate on that?",), "any"),
    # --- unwillingness ----------------------------------------------------------------------------------------------
    IntentSpec("refuse_to_talk", "Says they do not want to talk or wants the call to end.",
               ("I'm not discussing this", "stop calling me"), "any"),
    IntentSpec("decline_verification", "Will not answer a security question, or asks why instead of answering.",
               ("I'm not giving you that", "why do you need that?"), "before_verification"),
    # --- situations that change the path ----------------------------------------------------------------------------
    IntentSpec("hardship", "Mentions a serious personal difficulty such as illness, bereavement or losing income.",
               ("I was made redundant", "I've been in hospital"), "after_verification"),
    IntentSpec("already_paid", "Says the amount due has already been paid.",
               ("I paid it yesterday", "the money went out this morning"), "after_verification"),
    IntentSpec("dispute", "Denies owing the debt or having the product, or says the account was closed.",
               ("that's not my account", "I closed that years ago"), "after_verification"),
    IntentSpec("restructuring", "Asks for a new repayment arrangement or smaller instalments.",
               ("can we change the monthly amount?",), "after_verification"),
    IntentSpec("human_request", "Asks to speak to a person instead of the assistant.",
               ("put me through to a real person",), "any"),
    # --- negotiation ------------------------------------------------------------------------------------------------
    IntentSpec("cant_afford_full", "Says the full amount is too much right now.",
               ("I can't do the whole thing", "that's too much at the moment"), "after_verification"),
    IntentSpec("proposes_date", "Names a specific day to pay.", ("I can pay on the 3rd", "next Tuesday"),
               "after_verification"),
    IntentSpec("proposes_amount", "Names a specific amount they can pay.", ("I could manage 200",),
               "after_verification"),
    IntentSpec("vague_period", "Gives a period or an event instead of a day.",
               ("sometime next month", "once I get paid"), "after_verification"),
    IntentSpec("flat_refusal", "Says no to paying without naming a date or an amount.",
               ("no, I can't", "not paying that"), "after_verification"),
    IntentSpec("forgot", "Says they forgot about the payment.", ("oh, I completely forgot",), "after_verification"),
    IntentSpec("will_pay_later", "Promises to pay at some unspecified later time.",
               ("I'll sort it out later",), "after_verification"),
    IntentSpec("change_promise", "Wants to change or cancel a promise already recorded in this call.",
               ("can I move that to Friday instead?",), "after_verification"),
    IntentSpec("off_topic", "Talks about something unrelated to the call.", ("the traffic was awful today",), "any"),
)

INTENT_CATALOG: dict[str, IntentSpec] = {spec.key: spec for spec in _SPECS}
"""Catalog intents by key."""

INTENT_ALIASES: dict[str, str] = {
    "cant_afford": "cant_afford_full",
    "too_much": "cant_afford_full",
    "wrong_person": "third_party",
    "not_the_customer": "third_party",
    "paid": "already_paid",
    "already_paid_claim": "already_paid",
    "not_my_debt": "dispute",
    "payment_plan": "restructuring",
    "lower_instalments": "restructuring",
    "agent_request": "human_request",
    "speak_to_human": "human_request",
    "refusal": "flat_refusal",
    "refused": "flat_refusal",
    "pay_later": "will_pay_later",
    "is_robot": "robot_question",
    "is_scam": "scam_question",
    "declined_verification": "decline_verification",
    "hardship_declined": "hardship",
}
"""Other names for catalog intents (the R5 taxonomy and common LLM spellings) → catalog key."""

_NON_KEY = re.compile(r"[^a-z0-9]+")


def canonical_intent_key(name: str) -> str:
    """Normalise an intent name to snake_case and map known aliases to catalog keys.

    Unknown names are returned normalised (brief-defined intents keep their own key).

    >>> canonical_intent_key("Can't afford")
    'cant_afford_full'
    """
    key = _NON_KEY.sub("_", name.strip().lower().replace("'", "")).strip("_")
    return INTENT_ALIASES.get(key, key)


def get_intent_spec(key: str) -> IntentSpec | None:
    """The catalog entry for ``key`` (aliases accepted), or ``None`` for a brief-defined intent."""
    return INTENT_CATALOG.get(canonical_intent_key(key))


def is_catalog_intent(key: str) -> bool:
    """True when ``key`` (or its alias) is a catalog intent."""
    return get_intent_spec(key) is not None


def catalog_keys() -> list[str]:
    """Catalog keys in catalog order."""
    return list(INTENT_CATALOG)


def describe_catalog(keys: list[str] | None = None) -> str:
    """One line per intent, ``key (scope): description``, for prompts that ask a model to classify utterances."""
    selected = [INTENT_CATALOG[k] for k in (keys or catalog_keys()) if k in INTENT_CATALOG]
    return "\n".join(f"{spec.key} ({spec.typical_scope}): {spec.description}" for spec in selected)
