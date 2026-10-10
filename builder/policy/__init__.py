"""Policy layer: the generic IR of a collections brief and pure reference computations over it.

Modules (all pure, no I/O):

* :mod:`builder.policy.ir` — :class:`PolicyIR` (structured policy) and :class:`RuleLedger` (atomic rules with
  verbatim quotes, ambiguities and supersessions). Produced by ``builder.understand``, consumed by
  ``builder.compiler``.
* :mod:`builder.policy.intents` — seed catalog of generic customer intents (keys double as situation keys).
* :mod:`builder.policy.calendar` — business-day arithmetic, deadlines, proposal dates, promise-date and callback
  checks.
* :mod:`builder.policy.money` — cent-exact amounts, formatting, the amount check.
* :mod:`builder.policy.outcomes` — :func:`compute_outcome`, the reference for the compiled ``decide_outcome`` node.

Nothing in this package is specific to one brief: holidays, limits, outcome names, queues and phrases are IR data.
"""

from builder.policy.calendar import (
    BusinessCalendar,
    CallbackCheck,
    DateCheck,
    check_callback,
    check_promise_date,
    deadline_for,
    parse_now,
    resolve_proposal_date,
    spoken_date,
)
from builder.policy.intents import INTENT_CATALOG, IntentSpec, canonical_intent_key
from builder.policy.ir import (
    Ambiguity,
    Band,
    OutcomeRef,
    OutcomeRow,
    PolicyIR,
    RuleEntry,
    RuleLedger,
)
from builder.policy.money import (
    amount_equal,
    check_promise_amount,
    fmt_amount,
    in_range,
    spoken_amount,
    to_money,
)
from builder.policy.outcomes import CallSituation, OutcomeDecision, Situation, compute_outcome, decide_outcome

__all__ = [
    "INTENT_CATALOG",
    "Ambiguity",
    "Band",
    "BusinessCalendar",
    "CallSituation",
    "CallbackCheck",
    "DateCheck",
    "IntentSpec",
    "OutcomeDecision",
    "OutcomeRef",
    "OutcomeRow",
    "PolicyIR",
    "RuleEntry",
    "RuleLedger",
    "Situation",
    "amount_equal",
    "canonical_intent_key",
    "check_callback",
    "check_promise_amount",
    "check_promise_date",
    "compute_outcome",
    "deadline_for",
    "decide_outcome",
    "fmt_amount",
    "in_range",
    "parse_now",
    "resolve_proposal_date",
    "spoken_amount",
    "spoken_date",
    "to_money",
]
