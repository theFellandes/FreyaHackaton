"""Optional Langfuse observer: one trace per build run, one span per graph node, one generation per LLM attempt.

Enabled by ``LANGFUSE_PUBLIC_KEY`` + ``LANGFUSE_SECRET_KEY`` (``LANGFUSE_HOST``, default
``https://cloud.langfuse.com``; point it at a self-hosted instance, see ``docker-compose.langfuse.yml``). It needs the
optional extra ``obs`` (``langfuse>=3``); without the package the factory simply leaves it out.

What reaches Langfuse (everything is passed through the same secret redaction as the local log first):

* trace ``build:<slot>`` - root span with the run metadata, tags ``slot:..``, ``track:..``, ``profile:..`` and the
  run id as session id;
* one span per graph node (child of the root);
* one *generation* per LLM attempt (child of the node's span): input = the messages sent, output = the raw reply,
  model, ``usage_details`` (input / output tokens), metadata (provider, masked key label, attempt, latency) and, for
  a failed attempt, level ``ERROR`` plus the error text as status message.

With ``log_prompts=False`` (``OBSERVABILITY_LOG_PROMPTS=false``) the prompt and reply texts are replaced by their
lengths: Langfuse then only sees metadata (the local ``.llm.jsonl`` log keeps the texts).

SDK compatibility: the observer uses ``start_observation(as_type=...)`` (Langfuse Python SDK v3.x / v4) and falls back
to ``start_span`` / ``start_generation`` on older v3 releases. Trace attributes are set with ``propagate_attributes``
when the SDK has it. Every method swallows its exceptions (a Langfuse outage must never stop a build).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable, Mapping
from contextlib import AbstractContextManager, nullcontext
from typing import Any

from builder.observability.base import Redactor, to_text

logger = logging.getLogger(__name__)

__all__ = ["DEFAULT_HOST", "LangfuseObserver"]

DEFAULT_HOST = "https://cloud.langfuse.com"
_MAX_LOGGED_FAILURES = 3
_FAILED_STATUSES = {"failed": "ERROR", "error": "ERROR", "blocked": "WARNING"}
_UNSET: Any = object()


def _ascii(value: Any, limit: int = 200) -> str:
    """Trace attributes must be US-ASCII and at most 200 characters."""
    return str(value).encode("ascii", "replace").decode("ascii")[:limit]


class LangfuseObserver:
    """Sends the build's LLM activity to Langfuse (cloud or self-hosted).

    Args:
        public_key: ``pk-lf-...``.
        secret_key: ``sk-lf-...`` (also masked in everything this observer sends).
        host: Base URL of the Langfuse server.
        log_prompts: ``False`` sends lengths instead of prompt / reply texts.
        redact: Extra redaction function, usually ``TraceWriter.redact``.
        secrets: Additional secret values to mask.
        client: A ready Langfuse client (tests pass a fake). Created from the keys when omitted.
        propagate: ``propagate_attributes``-like callable; ``None`` disables trace attribute propagation. Default:
            the SDK's own when ``client`` is created here, none when a ``client`` is injected.

    Raises:
        ImportError: ``client`` was not given and the ``langfuse`` package is not installed.
    """

    def __init__(
        self,
        public_key: str,
        secret_key: str,
        host: str = DEFAULT_HOST,
        *,
        log_prompts: bool = True,
        redact: Callable[[str], str] | None = None,
        secrets: Iterable[str] = (),
        client: Any = None,
        propagate: Any = _UNSET,
    ) -> None:
        self.host = host or DEFAULT_HOST
        self.log_prompts = log_prompts
        self._redactor = Redactor(redact, [*secrets, secret_key, public_key])
        self._lock = threading.RLock()
        self._failures = 0
        self._counter = 0
        self._spans: dict[str, tuple[str, Any]] = {}
        self._root: Any = None
        self._trace_attrs: dict[str, Any] = {}
        if client is None:
            from langfuse import Langfuse  # raises ImportError when the optional extra is missing

            client = Langfuse(public_key=public_key, secret_key=secret_key, host=self.host)
            if propagate is _UNSET:
                try:
                    from langfuse import propagate_attributes

                    propagate = propagate_attributes
                except ImportError:  # older SDK: trace attributes go on the root observation instead
                    propagate = None
        elif propagate is _UNSET:
            propagate = None
        self._client = client
        self._propagate = propagate

    # ------------------------------------------------------------------------------------------------ helpers

    def add_secrets(self, *secrets: str) -> None:
        """Mask more secret values from now on."""
        self._redactor.add(*secrets)

    def _failed(self, operation: str, exc: BaseException) -> None:
        self._failures += 1
        if self._failures <= _MAX_LOGGED_FAILURES:
            logger.warning("langfuse %s failed (%s: %s)", operation, type(exc).__name__, exc)
        else:
            logger.debug("langfuse %s failed (%s)", operation, type(exc).__name__, exc_info=True)

    def _propagated(self) -> AbstractContextManager[Any]:
        if self._propagate is None or not self._trace_attrs:
            return nullcontext()
        try:
            return self._propagate(**self._trace_attrs)  # type: ignore[no-any-return]
        except Exception as exc:
            self._failed("propagate_attributes", exc)
            return nullcontext()

    @staticmethod
    def _start(parent: Any, *, name: str, as_type: str, **fields: Any) -> Any:
        """Create an observation under ``parent`` (a client or an observation) with whichever API the SDK has."""
        start = getattr(parent, "start_observation", None)
        if start is not None:
            return start(name=name, as_type=as_type, **fields)
        if as_type == "generation":
            return parent.start_generation(name=name, **fields)
        return parent.start_span(name=name, **fields)

    def _safe(self, value: Any) -> Any:
        return self._redactor.value(value)

    # ------------------------------------------------------------------------------------------------ run / spans

    def start_run(self, run_id: str, slot: str, metadata: Mapping[str, Any] | None = None) -> None:
        try:
            meta = {str(k): v for k, v in (metadata or {}).items() if v is not None}
            tags = [_ascii(f"slot:{slot}")]
            for label in ("track", "profile"):
                if meta.get(label) is not None:
                    tags.append(_ascii(f"{label}:{meta[label]}"))
            flat = {
                _ascii(k): _ascii(self._safe(v) if not isinstance(v, str) else self._redactor(v))
                for k, v in meta.items()
            }
            flat.setdefault("slot", _ascii(slot))
            name = f"build:{slot}"
            with self._lock:
                self._trace_attrs = {
                    "trace_name": _ascii(name),
                    "session_id": _ascii(run_id),
                    "tags": tags,
                    "metadata": flat,
                }
                with self._propagated():
                    root = self._start(
                        self._client,
                        name=name,
                        as_type="span",
                        input={"run_id": run_id, "slot": slot, **self._safe(meta)},
                        metadata=self._safe({"run_id": run_id, "slot": slot, "tags": tags, **meta}),
                    )
                self._root = root
                update_trace = getattr(root, "update_trace", None)  # SDK v3 without propagate_attributes
                if self._propagate is None and callable(update_trace):
                    update_trace(name=name, session_id=run_id, tags=tags, metadata=self._safe(meta))
        except Exception as exc:
            self._failed("start_run", exc)

    def start_span(self, name: str, metadata: Mapping[str, Any] | None = None) -> str | None:
        try:
            with self._lock:
                self._counter += 1
                span_id = f"lf{self._counter}"
                parent = self._root if self._root is not None else self._client
                with self._propagated():
                    span = self._start(
                        parent, name=str(name), as_type="span", metadata=self._safe(dict(metadata or {}))
                    )
                self._spans[span_id] = (str(name), span)
            return span_id
        except Exception as exc:
            self._failed("start_span", exc)
            return None

    def end_span(self, span_id: str | None, output_summary: Any = None, error: str | None = None) -> None:
        try:
            with self._lock:
                entry = self._spans.pop(span_id, None) if span_id else None
            if entry is None:
                return
            _, span = entry
            fields: dict[str, Any] = {}
            if output_summary is not None:
                fields["output"] = self._safe(output_summary)
            if error:
                fields["level"] = "ERROR"
                fields["status_message"] = self._redactor(str(error))[:500]
            if fields:
                span.update(**fields)
            span.end()
        except Exception as exc:
            self._failed("end_span", exc)

    def end_run(self, status: str, summary: Any = None) -> None:
        try:
            with self._lock:
                leaked = list(self._spans.items())
                self._spans.clear()
                root, self._root = self._root, None
            for _, (_, span) in leaked:
                try:
                    span.update(
                        level="WARNING", status_message="the run ended while this node was still open"
                    )
                    span.end()
                except Exception as exc:
                    self._failed("end_span", exc)
            if root is None:
                return
            fields: dict[str, Any] = {"output": self._safe({"status": status, "summary": summary})}
            level = _FAILED_STATUSES.get(str(status).lower())
            if level:
                fields["level"] = level
                fields["status_message"] = _ascii(f"run {status}")
            root.update(**fields)
            root.end()
        except Exception as exc:
            self._failed("end_run", exc)

    # ------------------------------------------------------------------------------------------------ generations

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
        """Record one LLM attempt as a Langfuse generation (never raises)."""
        try:
            clean = self._redactor
            system_text, user_text, output_text = (
                clean(to_text(system)),
                clean(to_text(user)),
                clean(to_text(output)),
            )
            if self.log_prompts:
                messages = ([{"role": "system", "content": system_text}] if system_text else []) + [
                    {"role": "user", "content": user_text}
                ]
                shown_output: Any = output_text or None
            else:
                messages = [
                    {
                        "role": "omitted",
                        "content": f"prompt hidden (system {len(system_text)} chars, user {len(user_text)} chars)",
                    }
                ]
                shown_output = f"reply hidden ({len(output_text)} chars)" if output_text else None
            usage = {
                key: int(value)
                for key, value in (("input", input_tokens), ("output", output_tokens))
                if value is not None
            }
            kind = None if error_kind is None else str(getattr(error_kind, "value", error_kind))
            metadata = {
                "provider": provider,
                "key": clean(str(key_label)),
                "attempt": attempt,
                "latency_s": round(float(latency_s or 0.0), 3),
                "ok": bool(ok),
                "error_kind": kind,
                "prompt_logged": self.log_prompts,
            }
            fields: dict[str, Any] = {
                "model": model,
                "input": messages,
                "output": shown_output,
                "metadata": {k: v for k, v in metadata.items() if v is not None},
            }
            if usage:
                fields["usage_details"] = usage
            if not ok:
                fields["level"] = "ERROR"
                fields["status_message"] = clean(f"{kind or 'error'}: {error or ''}".strip())[:500]
            with self._lock:
                entry = self._spans.get(span_id) if span_id else None
                parent = entry[1] if entry else (self._root if self._root is not None else self._client)
                with self._propagated():
                    generation = self._start(
                        parent, name=clean(str(purpose))[:200] or "llm", as_type="generation", **fields
                    )
            generation.end()
        except Exception as exc:
            self._failed("log_generation", exc)

    def flush(self) -> None:
        try:
            self._client.flush()
        except Exception as exc:
            self._failed("flush", exc)
