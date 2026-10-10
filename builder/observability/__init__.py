"""LLM observability: see exactly what the builder sent to the models and what came back.

* :mod:`builder.observability.base` - the :class:`LLMObserver` protocol, :class:`CompositeObserver`,
  :func:`observed_span` (graph nodes) and :func:`current_span_id` (the gateway).
* :mod:`builder.observability.jsonl` - the always-on local log ``traces/<slot>-<ts>.llm.jsonl``.
* :mod:`builder.observability.langfuse_observer` - optional Langfuse (cloud or self-hosted) traces.
* :mod:`builder.observability.factory` - :func:`build_observer`, called once per build.

Importing this package never imports the ``langfuse`` SDK and makes no network call. See ``docs/OBSERVABILITY.md``.
"""

from __future__ import annotations

from builder.observability.base import (
    CompositeObserver,
    LLMObserver,
    NullObserver,
    Redactor,
    SpanHandle,
    current_span_id,
    observed_span,
    to_text,
)
from builder.observability.factory import build_observer
from builder.observability.jsonl import JsonlObserver, llm_log_path_for, read_llm_log

__all__ = [
    "CompositeObserver",
    "JsonlObserver",
    "LLMObserver",
    "NullObserver",
    "Redactor",
    "SpanHandle",
    "build_observer",
    "current_span_id",
    "llm_log_path_for",
    "observed_span",
    "read_llm_log",
    "to_text",
]
