"""The observer protocol, a fan-out composite, the "current span" context and the redaction helper.

An *observer* is told what a build does: when a run starts and ends, which graph node (span) is running and, for
every LLM attempt, the exact prompt that was sent and the raw reply that came back. Two implementations exist:

* :class:`builder.observability.jsonl.JsonlObserver` - always on, writes ``traces/<slot>-<ts>.llm.jsonl``;
* :class:`builder.observability.langfuse_observer.LangfuseObserver` - optional, sends the same facts to Langfuse.

Observability must never break a build, so :class:`CompositeObserver` (and every concrete observer) swallows its own
exceptions. Observers redact secrets themselves (:class:`Redactor`), because their output leaves the process
(Langfuse) or lands in files that get shared.

Graph nodes wrap their work in :func:`observed_span`, which also publishes the span id through a context variable
(:func:`current_span_id`); the gateway uses it to attach every generation to the node that asked for it.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel

from builder.trace import _Redactor  # the trace's own redactor: one definition of "secret-shaped"

logger = logging.getLogger(__name__)

__all__ = [
    "CompositeObserver",
    "LLMObserver",
    "NullObserver",
    "Redactor",
    "SpanHandle",
    "current_span_id",
    "observed_span",
    "to_text",
]

#: Span id of the graph node that is running in this task (``None`` outside a node).
_CURRENT_SPAN: ContextVar[str | None] = ContextVar("llm_observer_span", default=None)


@runtime_checkable
class LLMObserver(Protocol):
    """What the gateway, the graph and ``build.py`` call. All methods must be cheap and must not raise."""

    def start_run(self, run_id: str, slot: str, metadata: Mapping[str, Any] | None = None) -> None:
        """A build run starts (``metadata``: track, profile, brief sha ...)."""

    def start_span(self, name: str, metadata: Mapping[str, Any] | None = None) -> str | None:
        """A graph node starts; returns an id for :meth:`end_span` and :meth:`log_generation`."""

    def end_span(self, span_id: str | None, output_summary: Any = None, error: str | None = None) -> None:
        """The node ended (``error`` is a short description when it failed)."""

    def log_generation(
        self,
        *,
        purpose: str,
        provider: str,
        model: str,
        key_label: str,
        system: str,
        user: str,
        output: Any,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        latency_s: float = 0.0,
        ok: bool = True,
        error_kind: str | None = None,
        error: str | None = None,
        attempt: int | None = None,
        span_id: str | None = None,
    ) -> None:
        """One LLM attempt (success or failure): the exact ``system`` / ``user`` sent and the raw ``output``."""

    def end_run(self, status: str, summary: Any = None) -> None:
        """The run ended (``status``: ``deployed`` / ``blocked`` / ``failed`` ...)."""

    def flush(self) -> None:
        """Push everything buffered to its destination (called once at the end of a build)."""


class NullObserver:
    """Observer that does nothing (the default when none is configured)."""

    def start_run(self, run_id: str, slot: str, metadata: Mapping[str, Any] | None = None) -> None:
        return None

    def start_span(self, name: str, metadata: Mapping[str, Any] | None = None) -> str | None:
        return None

    def end_span(self, span_id: str | None, output_summary: Any = None, error: str | None = None) -> None:
        return None

    def log_generation(self, **kwargs: Any) -> None:
        return None

    def end_run(self, status: str, summary: Any = None) -> None:
        return None

    def flush(self) -> None:
        return None

    def add_secrets(self, *secrets: str) -> None:
        return None


# --------------------------------------------------------------------------------------------------------------
# Redaction and serialisation
# --------------------------------------------------------------------------------------------------------------


class Redactor:
    """Masks secrets in text and in nested JSON-like values.

    Combines the trace's redaction (``extra``: usually ``TraceWriter.redact``, which knows the configured keys),
    the generic key shapes (``sk-...``, ``fak_...``, bearer tokens) and any secrets registered with :meth:`add`.
    """

    def __init__(self, extra: Callable[[str], str] | None = None, secrets: Iterable[str] = ()) -> None:
        self._extra = extra
        self._inner = _Redactor(secrets)

    def add(self, *secrets: str) -> None:
        """Register more secret values (for example the keys the gateway discovered)."""
        self._inner.add(secrets)

    def __call__(self, text: str) -> str:
        if not text:
            return text
        if self._extra is not None:
            try:
                text = self._extra(text)
            except Exception:  # a broken trace redactor must not leak: fall through to our own patterns
                logger.warning("trace redaction failed; using the observer's own patterns", exc_info=True)
        return self._inner.text(text)

    def value(self, obj: Any, _depth: int = 0) -> Any:
        """Return ``obj`` made JSON-safe with every string redacted (mapping keys included)."""
        if obj is None or isinstance(obj, bool | int | float):
            return obj
        if isinstance(obj, str):
            return self(obj)
        if _depth > 8:
            return self(str(obj))
        if isinstance(obj, BaseModel):
            return self.value(obj.model_dump(mode="json"), _depth + 1)
        if isinstance(obj, Mapping):
            return {self(str(k)): self.value(v, _depth + 1) for k, v in obj.items()}
        if isinstance(obj, list | tuple | set | frozenset):
            return [self.value(v, _depth + 1) for v in obj]
        return self(str(obj))


def to_text(value: Any) -> str:
    """Render a model reply (text, parsed model, JSON-like value or ``None``) as the text that is logged."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bytes | bytearray):
        return bytes(value).decode("utf-8", errors="replace")
    if isinstance(value, BaseModel):
        return value.model_dump_json()
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


# --------------------------------------------------------------------------------------------------------------
# Fan-out
# --------------------------------------------------------------------------------------------------------------


class CompositeObserver:
    """Forwards every call to several observers; an exception in one never reaches the caller or the others.

    Span ids are the composite's own: each child gets (and later receives back) the id it created itself.
    """

    def __init__(self, observers: Sequence[LLMObserver] = ()) -> None:
        self._observers: list[LLMObserver] = list(observers)
        self._spans: dict[str, list[tuple[LLMObserver, str | None]]] = {}
        self._lock = threading.Lock()
        self._counter = 0

    @property
    def observers(self) -> tuple[LLMObserver, ...]:
        """The wrapped observers, in call order."""
        return tuple(self._observers)

    def add(self, observer: LLMObserver) -> None:
        """Append one more observer."""
        self._observers.append(observer)

    def _each(self, method: str, *args: Any, **kwargs: Any) -> None:
        for observer in list(self._observers):
            try:
                getattr(observer, method)(*args, **kwargs)
            except Exception:
                logger.warning("observer %s.%s failed", type(observer).__name__, method, exc_info=True)

    def start_run(self, run_id: str, slot: str, metadata: Mapping[str, Any] | None = None) -> None:
        self._each("start_run", run_id, slot, metadata)

    def start_span(self, name: str, metadata: Mapping[str, Any] | None = None) -> str | None:
        children: list[tuple[LLMObserver, str | None]] = []
        for observer in list(self._observers):
            try:
                children.append((observer, observer.start_span(name, metadata)))
            except Exception:
                logger.warning("observer %s.start_span failed", type(observer).__name__, exc_info=True)
        with self._lock:
            self._counter += 1
            span_id = f"span-{self._counter}"
            self._spans[span_id] = children
        return span_id

    def end_span(self, span_id: str | None, output_summary: Any = None, error: str | None = None) -> None:
        with self._lock:
            children = self._spans.pop(span_id, None) if span_id is not None else None
        for observer, child_id in children or ():
            try:
                observer.end_span(child_id, output_summary, error)
            except Exception:
                logger.warning("observer %s.end_span failed", type(observer).__name__, exc_info=True)

    def log_generation(self, *, span_id: str | None = None, **fields: Any) -> None:
        with self._lock:
            children = dict((id(o), cid) for o, cid in self._spans.get(span_id, ())) if span_id else {}
        for observer in list(self._observers):
            try:
                observer.log_generation(span_id=children.get(id(observer)), **fields)
            except Exception:
                logger.warning("observer %s.log_generation failed", type(observer).__name__, exc_info=True)

    def end_run(self, status: str, summary: Any = None) -> None:
        self._each("end_run", status, summary)

    def flush(self) -> None:
        self._each("flush")

    def add_secrets(self, *secrets: str) -> None:
        """Pass extra secrets to every observer that redacts (``add_secrets``)."""
        for observer in list(self._observers):
            add = getattr(observer, "add_secrets", None)
            if callable(add):
                try:
                    add(*secrets)
                except Exception:
                    logger.warning("observer %s.add_secrets failed", type(observer).__name__, exc_info=True)


# --------------------------------------------------------------------------------------------------------------
# Span helper for graph nodes
# --------------------------------------------------------------------------------------------------------------


def current_span_id() -> str | None:
    """The id of the span (graph node) running in this task, or ``None``."""
    return _CURRENT_SPAN.get()


@dataclass
class SpanHandle:
    """Yielded by :func:`observed_span`; call :meth:`set_output` to give the span a summary."""

    span_id: str | None = None
    output: Any = None

    def set_output(self, summary: Any) -> None:
        """Remember what the node produced (a short string or small mapping) for :meth:`end_span`."""
        self.output = summary


@contextmanager
def observed_span(
    observer: LLMObserver | None, name: str, metadata: Mapping[str, Any] | None = None
) -> Iterator[SpanHandle]:
    """Run a block as one span: start it, make it the current span, end it with the outcome.

    An exception inside the block ends the span with ``error`` set and is re-raised unchanged. A failing observer
    is ignored. With ``observer=None`` the block simply runs.

    Usage::

        with observed_span(ctx.observer, "extract_policy", {"attempt": 1}) as span:
            ...
            span.set_output("12 rules")
    """
    handle = SpanHandle()
    if observer is not None:
        try:
            handle.span_id = observer.start_span(name, metadata)
        except Exception:
            logger.warning("observer start_span(%r) failed", name, exc_info=True)
    token = _CURRENT_SPAN.set(handle.span_id) if handle.span_id is not None else None
    try:
        yield handle
    except BaseException as exc:
        _end_span(observer, handle, f"{type(exc).__name__}: {exc}"[:500])
        raise
    else:
        _end_span(observer, handle, None)
    finally:
        if token is not None:
            _CURRENT_SPAN.reset(token)


def _end_span(observer: LLMObserver | None, handle: SpanHandle, error: str | None) -> None:
    if observer is None or handle.span_id is None:
        return
    try:
        observer.end_span(handle.span_id, handle.output, error)
    except Exception:
        logger.warning("observer end_span failed", exc_info=True)
