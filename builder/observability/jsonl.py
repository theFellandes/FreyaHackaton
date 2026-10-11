"""The always-on local LLM log: ``traces/<slot>-<ts>.llm.jsonl``.

One JSON object per LLM attempt (successes *and* failures), with the **full** prompt (``system`` + ``user``, exactly
as sent to the provider) and the **full** reply (the text, or the JSON of a structured reply), the model, token
counts, latency, attempt number and error. This is the "check out what the LLM said" log; it needs no account and
no network. Secret values and key-shaped tokens are masked in every string before anything is written.

Which texts are kept is the ``content`` mode (``LLM_LOG_CONTENT``): ``"full"`` (default) writes the texts above;
``"metadata"`` writes ``null`` for ``system`` / ``user`` / ``output`` and keeps everything else, including the length
of each text (``*_chars``). It is independent of ``OBSERVABILITY_LOG_PROMPTS``, which only governs Langfuse.

Record shape (``type`` is always ``"generation"``)::

    {"type": "generation", "seq": 3, "ts": "...Z", "run_id": "...", "slot": "...", "span": "extract_policy",
     "purpose": "...", "provider": "openai", "model": "...", "key_label": "openai#1(…ab12)", "attempt": 1,
     "ok": true, "error_kind": null, "error": null, "latency_s": 4.21, "input_tokens": 1200, "output_tokens": 310,
     "content": "full", "system_chars": 800, "user_chars": 5000, "output_chars": 1200,
     "system": "...", "user": "...", "output": "..."}

Read it back with :func:`read_llm_log`. The file is opened lazily on the first record, appended to and flushed after
every line, so a crashed build keeps everything logged up to the crash. Without a path the records are only kept in
memory (``records``) - used when the trace itself writes no files.
"""

from __future__ import annotations

import json
import logging
import math
import threading
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, get_args

from builder.observability.base import Redactor, to_text

logger = logging.getLogger(__name__)

__all__ = ["LLM_LOG_SUFFIX", "JsonlObserver", "LogContent", "llm_log_path_for", "read_llm_log"]

#: Extension of the LLM log; it sits next to the run's ``.log`` / ``.jsonl`` trace and shares their stem.
LLM_LOG_SUFFIX = ".llm.jsonl"

#: What a record keeps of the prompt and the reply: their texts (``full``) or only their lengths (``metadata``).
LogContent = Literal["full", "metadata"]


def llm_log_path_for(trace: Any) -> Path | None:
    """Path of the LLM log that belongs to ``trace`` (same stem as its ``.log``), or ``None`` when it has no files."""
    log_path = getattr(trace, "log_path", None)
    if not log_path:
        return None
    log_path = Path(log_path)
    return log_path.parent / (log_path.stem + LLM_LOG_SUFFIX)


def read_llm_log(path: Path | str) -> list[dict[str, Any]]:
    """Parse an ``.llm.jsonl`` file (blank and unreadable lines are skipped)."""
    records: list[dict[str, Any]] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            logger.warning("skipping an unreadable line in %s", path)
    return records


class JsonlObserver:
    """Writes one redacted JSON line per LLM generation.

    Args:
        path: The file to append to (parents are created on first write). ``None`` keeps records in memory only.
        redact: Extra redaction function, usually ``TraceWriter.redact`` (it knows the configured secrets).
        secrets: Additional secret values to mask.
        clock: Aware-UTC ``datetime`` source (injectable for tests).
        content: ``"full"`` (default) keeps the prompt and reply texts; ``"metadata"`` keeps only their lengths.

    Raises:
        ValueError: ``content`` is neither ``"full"`` nor ``"metadata"`` (a typo must not silently log everything).
    """

    def __init__(
        self,
        path: Path | str | None = None,
        *,
        redact: Callable[[str], str] | None = None,
        secrets: Iterable[str] = (),
        clock: Callable[[], datetime] | None = None,
        content: LogContent = "full",
    ) -> None:
        if content not in get_args(LogContent):
            raise ValueError(f"content must be one of {', '.join(get_args(LogContent))}, got {content!r}")
        self.content: LogContent = content
        self.path: Path | None = Path(path) if path is not None else None
        self.records: list[dict[str, Any]] = []
        self._redactor = Redactor(redact, secrets)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = threading.Lock()
        self._seq = 0
        self._span_counter = 0
        self._spans: dict[str, str] = {}
        self._run_id = ""
        self._slot = ""

    # ------------------------------------------------------------------------------------------------ observer

    def add_secrets(self, *secrets: str) -> None:
        """Mask more secret values from now on."""
        self._redactor.add(*secrets)

    def start_run(self, run_id: str, slot: str, metadata: Mapping[str, Any] | None = None) -> None:
        self._run_id, self._slot = str(run_id), str(slot)

    def start_span(self, name: str, metadata: Mapping[str, Any] | None = None) -> str | None:
        with self._lock:
            self._span_counter += 1
            span_id = f"j{self._span_counter}"
            self._spans[span_id] = str(name)
        return span_id

    def end_span(self, span_id: str | None, output_summary: Any = None, error: str | None = None) -> None:
        return None

    def end_run(self, status: str, summary: Any = None) -> None:
        return None

    def flush(self) -> None:
        return None

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
        """Append one generation record (never raises)."""
        try:
            clean = self._redactor
            latency = float(latency_s or 0.0)
            if not math.isfinite(latency):
                latency = 0.0
            system_text, user_text, output_text = (
                clean(to_text(system)),
                clean(to_text(user)),
                clean(to_text(output)),
            )
            full = self.content == "full"
            with self._lock:
                self._seq += 1
                record: dict[str, Any] = {
                    "type": "generation",
                    "seq": self._seq,
                    "ts": self._clock().astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
                    "run_id": self._run_id,
                    "slot": self._slot,
                    "span": self._spans.get(span_id) if span_id else None,
                    "purpose": clean(str(purpose)),
                    "provider": provider,
                    "model": model,
                    "key_label": clean(str(key_label)),
                    "attempt": attempt,
                    "ok": bool(ok),
                    "error_kind": None
                    if error_kind is None
                    else str(getattr(error_kind, "value", error_kind)),
                    "error": None if error is None else clean(str(error)),
                    "latency_s": round(latency, 3),
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "content": self.content,
                    "system_chars": len(system_text),
                    "user_chars": len(user_text),
                    "output_chars": len(output_text),
                    "system": system_text if full else None,
                    "user": user_text if full else None,
                    "output": output_text if full else None,
                }
                self._write(record)
        except Exception:
            logger.warning("could not write the LLM log record", exc_info=True)

    # ------------------------------------------------------------------------------------------------ internals

    def _write(self, record: dict[str, Any]) -> None:
        """Keep the record in memory (no path) or append it to the file. Called with the lock held."""
        if self.path is None:
            self.records.append(record)
            return
        line = json.dumps(record, ensure_ascii=False, allow_nan=False)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(line + "\n")
