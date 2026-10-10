"""Plumbing shared by the modules of the understand stage.

The stage talks to two collaborators that tests replace with fakes:

* the LLM gateway (:class:`builder.llm.gateway.LLMGateway`): only its ``structured`` coroutine is used, through the
  :class:`LLMLike` protocol, so any object with that method works;
* the run trace (:class:`builder.trace.TraceWriter`): only ``event``, ``decision``, ``ambiguity`` and ``error`` are
  used, through :class:`TraceLike`; ``None`` is accepted everywhere and silences tracing.

Nothing in this module (or in the stage) knows a particular brief; every brief value reaches the stage as data.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel

from builder.llm.providers import LLMRole

__all__ = [
    "MAX_OUTPUT_TOKENS",
    "LLMLike",
    "TraceLike",
    "UnderstandError",
    "call_structured",
    "trace_ambiguity",
    "trace_decision",
    "trace_error",
    "trace_event",
]

logger = logging.getLogger(__name__)

ModelT = TypeVar("ModelT", bound=BaseModel)

MAX_OUTPUT_TOKENS = 16000
"""Output cap for the big structured answers (rule ledger parts, policy IR, repairs)."""


class UnderstandError(RuntimeError):
    """The understand stage cannot produce a usable result (for example the model returned an empty ledger)."""


class LLMLike(Protocol):
    """The slice of :class:`builder.llm.gateway.LLMGateway` the stage uses."""

    async def structured(
        self,
        *,
        purpose: str,
        schema: type[Any],
        system: str,
        user: str,
        role: LLMRole = LLMRole.STRONG,
        max_tokens: int | None = None,
        deadline: float | None = None,
    ) -> Any:
        """Return an object whose ``value`` attribute is a validated instance of ``schema``."""
        ...


class TraceLike(Protocol):
    """The slice of :class:`builder.trace.TraceWriter` the stage writes to."""

    def event(self, kind: str, message: str, /, **data: Any) -> Any:
        """Record a free-form event."""
        ...

    def decision(self, message: str, /, **data: Any) -> Any:
        """Record a design decision."""
        ...

    def ambiguity(self, rule_ids: Iterable[str] | str, question: str, chosen: str, rationale: str) -> Any:
        """Record how an ambiguous rule was interpreted."""
        ...

    def error(self, message: str, /, **data: Any) -> Any:
        """Record a handled error."""
        ...


async def call_structured(
    llm: LLMLike,
    *,
    purpose: str,
    schema: type[ModelT],
    system: str,
    user: str,
    role: LLMRole = LLMRole.STRONG,
    max_tokens: int | None = MAX_OUTPUT_TOKENS,
    deadline: float | None = None,
) -> ModelT:
    """Run one structured gateway call and return the validated model instance.

    The gateway already validates against ``schema``; a fake that returns a plain dict is validated here too, so the
    stage always works with typed objects.

    Raises:
        builder.llm.errors.AllProvidersFailed: No provider produced a valid answer (propagated unchanged).
    """
    result = await llm.structured(
        purpose=purpose,
        schema=schema,
        system=system,
        user=user,
        role=role,
        max_tokens=max_tokens,
        deadline=deadline,
    )
    value = getattr(result, "value", result)
    if isinstance(value, schema):
        return value
    return schema.model_validate(value)


def trace_event(trace: TraceLike | None, kind: str, message: str, **data: Any) -> None:
    """Write a trace event when a trace is present, always mirror it to the module logger."""
    logger.info("%s: %s", kind, message)
    if trace is not None:
        trace.event(kind, message, **data)


def trace_decision(trace: TraceLike | None, message: str, **data: Any) -> None:
    """Write a decision to the trace (and the logger)."""
    logger.info("decision: %s", message)
    if trace is not None:
        trace.decision(message, **data)


def trace_error(trace: TraceLike | None, message: str, **data: Any) -> None:
    """Write a handled error to the trace (and the logger)."""
    logger.warning("error: %s", message)
    if trace is not None:
        trace.error(message, **data)


def trace_ambiguity(
    trace: TraceLike | None, rule_ids: Iterable[str], question: str, chosen: str, rationale: str
) -> None:
    """Write an ambiguity and its chosen interpretation to the trace."""
    ids = list(rule_ids)
    logger.info("ambiguity %s: %s -> %s", ids, question, chosen)
    if trace is not None:
        trace.ambiguity(ids, question, chosen, rationale)
