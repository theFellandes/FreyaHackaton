"""Guardrails: deterministic checks around every LLM-touched artifact of the build.

The builder reads untrusted bank documents with a model and compiles the result into a phone agent that talks to
real customers. The guardrails sit at each hand-over so a manipulated, invented or leaking artifact is stopped
before it is used:

* :mod:`~builder.guardrails.input_guard` - ``check_inputs`` (brief documents and tool API), ``wrap_untrusted``;
* :mod:`~builder.guardrails.prompt_guard` - ``GUARD_PREAMBLE`` / ``guarded_system`` for every system prompt;
* :mod:`~builder.guardrails.output_guard` - ``check_ledger``, ``check_policy``, ``check_prose``, ``check_workflow``;
* :mod:`~builder.guardrails.models` - ``GuardReport`` / ``GuardViolation`` returned by every check.

Importing this package is cheap and cycle free: no module imports the LLM gateway, the understand stages or the
compiler at import time, so ``builder.llm.gateway`` may import ``builder.guardrails.prompt_guard`` directly.
See ``docs/GUARDRAILS.md`` for the flow and how to add a guard.
"""

from __future__ import annotations

from builder.guardrails.input_guard import check_inputs, escape_untrusted, wrap_bundle, wrap_untrusted
from builder.guardrails.models import GuardReport, GuardSeverity, GuardStage, GuardViolation
from builder.guardrails.output_guard import check_ledger, check_policy, check_prose, check_workflow
from builder.guardrails.prompt_guard import GUARD_MARKER, GUARD_PREAMBLE, guarded_system, is_guarded

__all__ = [
    "GUARD_MARKER",
    "GUARD_PREAMBLE",
    "GuardReport",
    "GuardSeverity",
    "GuardStage",
    "GuardViolation",
    "check_inputs",
    "check_ledger",
    "check_policy",
    "check_prose",
    "check_workflow",
    "escape_untrusted",
    "guarded_system",
    "is_guarded",
    "wrap_bundle",
    "wrap_untrusted",
]
