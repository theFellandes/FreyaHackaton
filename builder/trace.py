"""The run trace: a human-readable log plus a machine-readable JSONL twin of every builder event.

The hackathon requires one trace per brief, committed with the submission, showing: the start and end time, every
model call (model and a one-line purpose), what was created on Freya (workflow id, agent id, number of steps and
tools) and every self-test with its result. Organisers re-run the best builders and compare against the trace, so it
must be complete, readable and free of secrets.

:class:`TraceWriter` writes two files per run into ``traces/``::

    <slot>-<UTC %Y%m%dT%H%M%SZ>.log     one plain-text line per event, for people
    <slot>-<UTC %Y%m%dT%H%M%SZ>.jsonl   one JSON object per event, for tools

A text line looks like::

    2026-10-11T17:02:03.123Z | MODEL  | anthropic/claude-sonnet-5-5 | key=anthropic#1(...f3a2) | ok 12.3s in=12034 out=2981 | extract policy IR

and :meth:`TraceWriter.summary` appends the final block (start, end, duration, model calls per model, Freya objects,
tests and results, track, slot).

Guarantees:

* **Secrets never reach the files.** Every configured secret substring is replaced by ``***`` in messages and in all
  (nested) data, and well-known key shapes (``sk-ant-...``, ``sk-...``, ``fak_...``, ``Bearer ...``) are masked as a
  second line of defence. Pass :func:`builder.settings.secret_values` as ``secrets``.
* **No local folder layout.** Paths inside the repository are written relative to it (``./out/x.json``), so a
  committed trace does not carry the builder machine's user name (audit AUD-57). The START record names the git commit,
  the number of uncommitted files and the command-line arguments, so a re-run can be matched to the code.
* **Thread and asyncio safe.** Writes are serialised by a lock and flushed immediately, so a crash loses nothing.
* **Never crashes the build.** After construction, I/O errors and unserialisable data are logged and swallowed.

:class:`NullTrace` has the same API but keeps everything in memory (``.events`` and ``.lines``); use it in tests and
anywhere a real trace file is not wanted.
"""

from __future__ import annotations

import dataclasses
import functools
import json
import logging
import math
import platform
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, date, datetime
from datetime import time as dt_time
from enum import Enum
from pathlib import Path, PurePath
from typing import Any, TextIO

from pydantic import SecretBytes, SecretStr

from builder import __version__, paths

logger = logging.getLogger(__name__)

#: Replacement text for a redacted secret.
REDACTED = "***"
#: Secrets shorter than this are ignored by the redactor (an empty secret would otherwise match everywhere).
MIN_REDACT_LENGTH = 4
#: Longest purpose text kept on a MODEL line.
MAX_PURPOSE_CHARS = 160
#: Longest error text kept on a text line (the JSONL keeps up to MAX_JSON_TEXT_CHARS).
MAX_ERROR_CHARS = 300
#: Longest single string value kept in the JSONL file.
MAX_JSON_TEXT_CHARS = 20000
#: Longest rendering of one data value on a text line.
MAX_VALUE_CHARS = 120

#: Text-line labels of the built-in event kinds; other kinds are shown upper-cased.
KIND_LABELS: dict[str, str] = {
    "start": "START",
    "end": "END",
    "stage": "STAGE",
    "model_call": "MODEL",
    "freya": "FREYA",
    "test": "TEST",
    "test_start": "TEST",
    "decision": "DECIDE",
    "ambiguity": "AMBIG",
    "error": "ERROR",
    "summary": "FINAL",
}

#: Statuses that end a stage (a later stage() call with one of these reports the stage's duration).
_STAGE_END_STATUSES = frozenset({"end", "done", "ok", "failed", "error", "skipped", "timeout", "aborted"})

#: Freya facts that :meth:`TraceWriter.freya` remembers for the summary.
_FREYA_FACTS = (
    "workflow_id",
    "agent_id",
    "version",
    "node_count",
    "tool_count",
    "slot",
    "published",
    "exported",
)

#: Generic credential shapes masked even when they were not configured as secrets.
_KEY_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{10,}"), REDACTED),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}"), REDACTED),
    (re.compile(r"\bfak_[A-Za-z0-9_\-]{10,}"), REDACTED),
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._\-~+/=]{16,}"), r"\1 " + REDACTED),
)

_WS_RE = re.compile(r"\s+")


class _Redactor:
    """Replaces secret substrings (configured values and generic key shapes) with :data:`REDACTED`.

    With ``repo_root`` it also writes paths inside that folder relative to it (``C:\\...\\repo\\out\\x`` ->
    ``.\\out\\x``), in its native, POSIX and JSON-escaped spellings, case-insensitively (Windows drive letters).
    """

    def __init__(self, secrets: Iterable[str] = (), *, repo_root: Path | None = None) -> None:
        self._secrets: list[str] = []
        self.add(secrets)
        self._root_re: re.Pattern[str] | None = None
        if repo_root is not None:
            native = str(repo_root)
            spellings = {native, repo_root.as_posix(), native.replace("\\", "\\\\")}
            spellings = {s.rstrip("\\/") for s in spellings if len(s.rstrip("\\/")) > 3}
            if spellings:
                alternatives = "|".join(re.escape(s) for s in sorted(spellings, key=len, reverse=True))
                self._root_re = re.compile(f"(?:{alternatives})(?=[\\\\/]|$|[\"'\\s,;)\\]}}])", re.IGNORECASE)

    def add(self, secrets: Iterable[str]) -> None:
        """Register more secrets; longest first so that a secret containing another is replaced whole."""
        merged = list(self._secrets)
        for secret in secrets:
            if isinstance(secret, str) and len(secret.strip()) >= MIN_REDACT_LENGTH and secret not in merged:
                merged.append(secret)
        # Rebind instead of sorting in place, so a concurrent text() call always iterates a complete list.
        self._secrets = sorted(merged, key=len, reverse=True)

    def text(self, value: str, *, shorten_paths: bool = True) -> str:
        """Return ``value`` with every secret and key-shaped token masked (and repository paths shortened)."""
        for secret in self._secrets:
            if secret in value:
                value = value.replace(secret, REDACTED)
        for pattern, replacement in _KEY_PATTERNS:
            value = pattern.sub(replacement, value)
        if shorten_paths and self._root_re is not None:
            value = self._root_re.sub(".", value)
        return value


@functools.lru_cache(maxsize=1)
def _git_state() -> dict[str, Any]:
    """The checkout's commit and the number of uncommitted / untracked files (read-only git; ``None`` when unknown).

    ``--no-optional-locks`` keeps ``git status`` from refreshing the index, so nothing in ``.git`` is written.
    """
    state: dict[str, Any] = {"git_commit": None, "git_dirty": None}
    try:
        head = subprocess.run(
            ["git", "-C", str(paths.REPO_ROOT), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5
        )
        if head.returncode == 0 and head.stdout.strip():
            state["git_commit"] = head.stdout.strip()
        status = subprocess.run(
            ["git", "--no-optional-locks", "-C", str(paths.REPO_ROOT), "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if status.returncode == 0:
            state["git_dirty"] = sum(1 for line in status.stdout.splitlines() if line.strip())
    except Exception:  # no git, no checkout, a timeout: the trace never depends on it
        logger.debug("could not read the git state", exc_info=True)
    return state


def _one_line(text: object, limit: int | None = None) -> str:
    """Collapse all whitespace (newlines included) to single spaces and optionally cut to ``limit`` characters."""
    flat = _WS_RE.sub(" ", str(text)).strip()
    if limit is not None and len(flat) > limit:
        return flat[: max(limit - 3, 0)] + "..."
    return flat


def _enum_text(value: object) -> str:
    """Render enums by value (``ErrorKind.QUOTA`` -> ``quota``) and anything else with ``str``."""
    if isinstance(value, Enum):
        return str(value.value)
    return str(value)


def _iso(moment: datetime) -> str:
    """UTC ISO-8601 timestamp with milliseconds and a ``Z`` suffix."""
    moment = moment.astimezone(UTC) if moment.tzinfo else moment.replace(tzinfo=UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def _human_duration(seconds: float) -> str:
    """``1387.3`` -> ``"1387.3 s (23 min 07 s)"``."""
    whole = round(seconds)
    minutes, secs = divmod(whole, 60)
    return f"{seconds:.1f} s ({minutes} min {secs:02d} s)"


def _safe_filename_part(value: str) -> str:
    """Make ``value`` usable as part of a file name on every platform."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-.")
    return cleaned or "run"


class TraceWriter:
    """Writes the run trace (``.log`` + ``.jsonl``) for one builder run. See the module docstring.

    Args:
        run_id: Unique id of the run (shown in every JSON record and in the summary).
        slot: The build slot (workflow label); also the file-name prefix.
        directory: Where the trace files go (default: ``paths.TRACES_DIR``, read at call time); created if missing.
        secrets: Strings to redact everywhere (see :func:`builder.settings.secret_values`).
        clock: Wall-clock source returning an aware UTC ``datetime`` (injectable for tests).
        monotonic: Monotonic seconds source used for durations (injectable for tests).
        echo: Optional stream (for example ``sys.stderr``) that also receives every text line.

    Raises:
        OSError: If the trace directory or files cannot be created. Later I/O errors are logged, never raised.
    """

    def __init__(
        self,
        run_id: str,
        slot: str,
        directory: Path | None = None,
        secrets: Iterable[str] = (),
        *,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        echo: TextIO | None = None,
    ) -> None:
        self.run_id = run_id
        self.slot = slot
        self._clock: Callable[[], datetime] = clock or (lambda: datetime.now(UTC))
        self._monotonic = monotonic
        self._echo = echo
        self._redactor = _Redactor(secrets, repo_root=paths.REPO_ROOT)
        self._lock = threading.RLock()
        self._closed = False
        self._seq = 0
        self.started_at: datetime = self._clock()
        self._started_mono = self._monotonic()
        self._stage_starts: dict[str, float] = {}
        self._stages: list[dict[str, Any]] = []
        self._model_stats: dict[str, dict[str, int]] = {}
        self._tests: list[dict[str, Any]] = []
        self._pending_tests: dict[str, None] = {}  # started, not yet finished or skipped (insertion ordered)
        self._freya: dict[str, Any] = {}
        self._error_count = 0
        self.log_path: Path | None = None
        self.jsonl_path: Path | None = None
        self._open(paths.TRACES_DIR if directory is None else Path(directory))
        self._record_start()

    def _record_start(self) -> None:
        """Write the START line: run id, slot, start time, the software versions, the git commit and the arguments."""
        versions = f"builder {__version__}, python {platform.python_version()}"
        git = _git_state()
        commit = git.get("git_commit")
        dirty = git.get("git_dirty")
        where = ""
        if commit:
            where = f" | commit {str(commit)[:7]}" + ("+dirty" if dirty else "")
        self._emit(
            "start",
            f"run {self.run_id} started (slot {self.slot})",
            {
                "started_at": _iso(self.started_at),
                "builder_version": __version__,
                "python": platform.python_version(),
                "platform": platform.platform(terse=True),
                "git_commit": commit,
                "git_dirty": dirty,
                "argv": [str(arg) for arg in sys.argv[1:]],
            },
            text=f"run {self.run_id} started | slot={self.slot} | {versions}{where}",
        )

    # --- file handling (overridden by NullTrace) ----------------------------------------------------------------------
    def _open(self, directory: Path) -> None:
        """Create the directory and the two trace files; a numeric suffix avoids clobbering an existing trace."""
        directory.mkdir(parents=True, exist_ok=True)
        stem = (
            f"{_safe_filename_part(self.slot)}-{self.started_at.astimezone(UTC).strftime('%Y%m%dT%H%M%SZ')}"
        )
        candidate, counter = stem, 1
        while (directory / f"{candidate}.log").exists() or (directory / f"{candidate}.jsonl").exists():
            counter += 1
            candidate = f"{stem}-{counter}"
        self.log_path = directory / f"{candidate}.log"
        self.jsonl_path = directory / f"{candidate}.jsonl"
        self._log_file: TextIO | None = open(self.log_path, "x", encoding="utf-8", newline="\n")  # noqa: SIM115
        self._jsonl_file: TextIO | None = open(self.jsonl_path, "x", encoding="utf-8", newline="\n")  # noqa: SIM115

    def _write(self, line: str, record: dict[str, Any]) -> None:
        """Append one text line and one JSON record, flushing both. Called with the lock held."""
        payload = json.dumps(record, ensure_ascii=False, allow_nan=False)
        for handle, text in ((self._log_file, line), (self._jsonl_file, payload)):
            if handle is None:
                continue
            try:
                handle.write(text + "\n")
                handle.flush()
            except (OSError, ValueError) as exc:
                logger.error("trace write failed (%s): %s", type(exc).__name__, exc)

    def _close_files(self) -> None:
        """Close the trace files (called once, with the lock held)."""
        for handle in (self._log_file, self._jsonl_file):
            if handle is None:
                continue
            try:
                handle.close()
            except OSError as exc:
                logger.error("trace close failed: %s", exc)
        self._log_file = None
        self._jsonl_file = None

    # --- core ------------------------------------------------------------------------------------------------------------
    @property
    def closed(self) -> bool:
        """True after :meth:`close`; later events are dropped (with a log warning)."""
        return self._closed

    def add_secrets(self, *secrets: str) -> None:
        """Register more secrets to redact from now on (for keys discovered after the trace was opened)."""
        with self._lock:
            self._redactor.add(secrets)

    def redact(self, text: str) -> str:
        """Return ``text`` with every known secret masked (the same redaction the trace files get)."""
        return self._redactor.text(text)

    def redact_secrets(self, text: str) -> str:
        """Return ``text`` with every known secret masked, paths left as they are (for the result file)."""
        return self._redactor.text(text, shorten_paths=False)

    def event(self, kind: str, message: str, /, **data: Any) -> dict[str, Any]:
        """Record a generic event.

        Args:
            kind: Event kind (``stage``, ``model_call``, ... or any custom word such as ``input``).
            message: One human-readable sentence; newlines are flattened on the text line.
            **data: Structured details. ``None`` values are dropped; everything else is made JSON-safe.
                ``kind`` and ``message`` are positional-only, so ``data`` may itself contain those keys
                (the same holds for ``freya(action)``, ``decision(message)`` and ``error(message)``).

        Returns:
            The (redacted) JSON record that was written.
        """
        return self._emit(kind, message, data)

    def _emit(
        self, kind: str, message: str, data: Mapping[str, Any], text: str | None = None
    ) -> dict[str, Any]:
        """Build, redact and write one event. ``text`` replaces the default text-line body."""
        clean = {key: value for key, value in data.items() if value is not None}
        with self._lock:
            if self._closed:
                logger.warning("trace already closed; dropped %s event: %s", kind, _one_line(message, 80))
                return {}
            try:
                self._seq += 1
                record: dict[str, Any] = {
                    "ts": _iso(self._clock()),
                    "seq": self._seq,
                    "t_s": round(self._monotonic() - self._started_mono, 3),
                    "run_id": self.run_id,
                    "slot": self.slot,
                    "kind": kind,
                    "message": self._redactor.text(str(message)),
                    "data": self._jsonable(clean),
                }
                body = text if text is not None else self._default_body(message, clean)
                label = KIND_LABELS.get(kind, kind.upper())
                line = self._redactor.text(f"{record['ts']} | {label:<6} | {_one_line(body)}")
                self._write(line, record)
                self._echo_line(line)
                return record
            except Exception:  # the trace must never take the build down
                logger.exception("could not record %s trace event", kind)
                return {}

    def _default_body(self, message: str, data: Mapping[str, Any]) -> str:
        """``message | k=v k=v`` for scalar-ish data (nested values are rendered as short JSON)."""
        pairs = _format_pairs(data)
        return f"{message} | {pairs}" if pairs else str(message)

    def _echo_line(self, line: str) -> None:
        """Mirror a text line to the echo stream, surviving consoles that cannot encode every character."""
        if self._echo is None:
            return
        try:
            try:
                self._echo.write(line + "\n")
            except UnicodeEncodeError:
                encoding = getattr(self._echo, "encoding", None) or "ascii"
                self._echo.write(line.encode(encoding, errors="replace").decode(encoding) + "\n")
            self._echo.flush()
        except Exception as exc:  # echo is a convenience only
            logger.debug("trace echo failed: %s", exc)

    def _jsonable(self, value: Any, depth: int = 0) -> Any:
        """Convert ``value`` into JSON-safe data, redacting every string on the way."""
        if depth > 20:
            return "<nested too deep>"
        if value is None or isinstance(value, bool | int):
            return value
        if isinstance(value, float):
            return value if math.isfinite(value) else str(value)
        if isinstance(value, str):
            text = self._redactor.text(value)
            if len(text) > MAX_JSON_TEXT_CHARS:
                text = text[:MAX_JSON_TEXT_CHARS] + f"... [{len(text) - MAX_JSON_TEXT_CHARS} more chars]"
            return text
        if isinstance(value, Enum):
            return self._jsonable(value.value, depth + 1)
        if isinstance(value, PurePath):
            return self._redactor.text(value.as_posix())
        if isinstance(value, datetime | date | dt_time):
            return value.isoformat()
        if isinstance(value, SecretStr | SecretBytes):
            return REDACTED
        if isinstance(value, bytes | bytearray):
            return f"<{len(value)} bytes>"
        if isinstance(value, Mapping):
            return {
                self._redactor.text(str(key)): self._jsonable(item, depth + 1) for key, item in value.items()
            }
        if isinstance(value, set | frozenset):
            return [self._jsonable(item, depth + 1) for item in sorted(value, key=repr)]
        if isinstance(value, list | tuple):
            return [self._jsonable(item, depth + 1) for item in value]
        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            try:
                return self._jsonable(model_dump(mode="json"), depth + 1)
            except Exception:  # fall back to str() below
                logger.debug("model_dump failed for %s", type(value).__name__)
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            return self._jsonable(dataclasses.asdict(value), depth + 1)
        return self._redactor.text(str(value))

    # --- typed events ------------------------------------------------------------------------------------------------------
    def stage(self, name: str, status: str, **data: Any) -> dict[str, Any]:
        """Record a graph stage transition, e.g. ``stage("compile_workflow", "start")`` then ``(..., "end")``.

        When a stage ends (status ``end``/``done``/``ok``/``failed``/``error``/``skipped``/``timeout``/``aborted``)
        after a ``start``, its duration is added as ``duration_s`` and listed in the summary.
        """
        status_text = _enum_text(status)
        with self._lock:
            now = self._monotonic()
            if status_text == "start":
                self._stage_starts[name] = now
            elif (
                status_text in _STAGE_END_STATUSES and name in self._stage_starts and "duration_s" not in data
            ):
                data["duration_s"] = round(now - self._stage_starts.pop(name), 3)
            if status_text != "start":
                self._stages.append(
                    {"name": name, "status": status_text, "duration_s": data.get("duration_s")}
                )
        duration = data.get("duration_s")
        head = f"{name} | {status_text}" + (
            f" {float(duration):.1f}s" if isinstance(duration, int | float) else ""
        )
        extra = _format_pairs({k: v for k, v in data.items() if k != "duration_s"})
        return self._emit(
            "stage",
            f"{name} {status_text}",
            {"stage": name, "status": status_text, **data},
            text=f"{head} | {extra}" if extra else head,
        )

    def model_call(
        self,
        provider: str,
        model: str,
        key_label: str | None,
        purpose: str,
        ok: bool,
        latency_s: float,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        error_kind: object | None = None,
        error: str | None = None,
        attempt: int | None = None,
    ) -> dict[str, Any]:
        """Record one LLM attempt (successful or not); the gateway calls this for every attempt.

        The text line shows ``provider/model``, the masked key label, ``ok``/``FAIL <kind>`` with latency, token
        counts and the one-line purpose; failures add the (truncated, redacted) error text.
        """
        name = f"{provider}/{model}"
        kind_text = _enum_text(error_kind) if error_kind is not None else None
        with self._lock:
            stats = self._model_stats.setdefault(
                name, {"calls": 0, "ok": 0, "failed": 0, "input_tokens": 0, "output_tokens": 0}
            )
            stats["calls"] += 1
            stats["ok" if ok else "failed"] += 1
            stats["input_tokens"] += int(input_tokens or 0)
            stats["output_tokens"] += int(output_tokens or 0)
        purpose_line = _one_line(purpose, MAX_PURPOSE_CHARS)
        latency = float(latency_s or 0.0)
        outcome = f"ok {latency:.1f}s" if ok else f"FAIL {kind_text or 'error'} {latency:.1f}s"
        if input_tokens is not None:
            outcome += f" in={input_tokens}"
        if output_tokens is not None:
            outcome += f" out={output_tokens}"
        if attempt is not None:
            outcome += f" attempt={attempt}"
        text = f"{name} | key={key_label or '-'} | {outcome} | {purpose_line}"
        if not ok and error:
            text += f" | error: {_one_line(error, MAX_ERROR_CHARS)}"
        data = {
            "provider": provider,
            "model": model,
            "key_label": key_label,
            "purpose": purpose_line,
            "ok": ok,
            "latency_s": round(latency, 3),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "error_kind": kind_text,
            "error": error,
            "attempt": attempt,
        }
        return self._emit("model_call", purpose_line, data, text=text)

    def freya(self, action: str, /, **data: Any) -> dict[str, Any]:
        """Record a Freya platform action (validate, create, update, publish, export, chat, ...).

        Facts such as ``workflow_id``, ``agent_id``, ``version``, ``node_count`` and ``tool_count`` are remembered
        for the summary (the latest value wins).
        """
        with self._lock:
            for fact in _FREYA_FACTS:
                if data.get(fact) is not None:
                    self._freya[fact] = data[fact]
        return self._emit("freya", action, {"action": action, **data}, text=self._default_body(action, data))

    def test_started(self, name: str) -> None:
        """Remember that self-test ``name`` is running (written as a ``TEST`` start line).

        If the build is cut off before :meth:`test` or :meth:`test_skipped` is called for it, :meth:`merged_tests`
        lists it as an ``aborted`` (failed) test, so the summary of a cut-off build shows the run that never finished.
        """
        with self._lock:
            self._pending_tests[name] = None
        self._emit("test_start", f"START {name}", {"name": name}, text=f"START {name}")

    def test_skipped(self, name: str, reason: str) -> dict[str, Any]:
        """Record that self-test ``name`` was planned but did not run (not a pass, not a fail, not listed as a test)."""
        with self._lock:
            self._pending_tests.pop(name, None)
        return self._emit(
            "test",
            f"SKIPPED {name}: {reason}",
            {"name": name, "status": "skipped", "summary": reason},
            text=f"SKIPPED {name} | {reason}",
        )

    def test(self, name: str, passed: bool, summary: str, **data: Any) -> dict[str, Any]:
        """Record a self-test result (``PASS``/``FAIL`` + one-line summary); listed again in the final summary."""
        with self._lock:
            self._tests.append({"name": name, "passed": bool(passed), "summary": _one_line(summary, 300)})
            self._pending_tests.pop(name, None)
        verdict = "PASS" if passed else "FAIL"
        text = self._default_body(f"{verdict} {name} | {summary}", data)
        return self._emit(
            "test",
            f"{verdict} {name}: {summary}",
            {"name": name, "passed": bool(passed), "summary": summary, **data},
            text=text,
        )

    def merged_tests(
        self, given: Iterable[Mapping[str, Any]] = (), latest_only: Iterable[str] = ()
    ) -> list[dict[str, Any]]:
        """``given`` tests plus the ones this trace recorded that ``given`` lacks, plus unfinished ones as ``aborted``.

        ``given`` (the builder state's view, with repair rounds named ``smoke``, ``smoke#2`` ...) wins where both
        know a test; every recorded result is matched to at most one given entry of the same base name (the part
        before ``#``), so a repair round is not counted twice. Names in ``latest_only`` (checks the state keeps only
        the latest result of, such as the static checks that re-run after every compile repair) are never added from
        the trace when ``given`` has them. What the trace recorded and ``given`` does not hold
        - for example the tests of a build that the wall-clock guard cut off - is appended in the order it ran, and
        every test that was started but never finished is appended as a failed test whose summary starts with
        ``aborted``. The arguments are not modified.
        """
        merged = [dict(test) for test in given]
        unmatched = list(range(len(merged)))
        latest = set(latest_only)
        has_latest = {_base_test_name(test.get("name")) for test in merged} & latest
        with self._lock:
            recorded = [dict(test) for test in self._tests]
            pending = list(self._pending_tests)
        for record in recorded:
            if record["name"] in has_latest:
                continue  # the state's entry is the latest run of this check; earlier runs are history
            hit = next(
                (i for i in unmatched if _base_test_name(merged[i].get("name")) == record["name"]), None
            )
            if hit is None:
                merged.append(record)
            else:
                unmatched.remove(hit)
        for name in pending:  # a pending test has no result by definition, whatever earlier rounds of it said
            merged.append(
                {
                    "name": name,
                    "passed": False,
                    "summary": "aborted: started but did not finish (the build was cut off)",
                }
            )
        return merged

    def decision(self, message: str, /, **data: Any) -> dict[str, Any]:
        """Record a routing or design decision of the builder (why it chose a track, a repair, a fallback...)."""
        return self._emit("decision", message, data)

    def ambiguity(
        self, rule_ids: Iterable[str] | str, question: str, chosen: str, rationale: str
    ) -> dict[str, Any]:
        """Record how an ambiguous brief rule was interpreted, so reviewers can follow the choice."""
        ids = [rule_ids] if isinstance(rule_ids, str) else [str(rule_id) for rule_id in rule_ids]
        text = f"[{', '.join(ids)}] {question} -> chosen: {chosen} (because: {rationale})"
        data = {"rule_ids": ids, "question": question, "chosen": chosen, "rationale": rationale}
        return self._emit("ambiguity", question, data, text=text)

    def error(self, message: str, /, **data: Any) -> dict[str, Any]:
        """Record an error the builder handled (the run continues or ends gracefully)."""
        with self._lock:
            self._error_count += 1
        return self._emit("error", message, data)

    # --- summary and lifecycle ---------------------------------------------------------------------------------------------
    def model_stats(self) -> dict[str, dict[str, int]]:
        """Per ``provider/model``: calls, ok, failed, input_tokens, output_tokens (a copy)."""
        with self._lock:
            return {name: dict(stats) for name, stats in self._model_stats.items()}

    def summary(self, data: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Write the final summary block required by the hackathon and return the summary as a dict.

        The trace's own records (start time, model calls per model, Freya facts, tests, stage durations, error
        count) are combined with ``data`` from the caller; caller values win. Recognised caller keys: ``track``,
        ``status``, ``slot``, ``tests`` (list of ``{name, passed, summary}``), ``freya`` (dict) and the Freya facts
        ``workflow_id``, ``agent_id``, ``version``, ``node_count``, ``tool_count`` at the top level. Any other key
        is kept under ``extra`` and printed in the block.
        """
        given = dict(data or {})
        with self._lock:
            ended = self._clock()
            duration = self._monotonic() - self._started_mono
            freya = dict(self._freya)
            given_freya = given.pop("freya", None)
            if isinstance(given_freya, Mapping):
                freya.update(given_freya)
            for fact in _FREYA_FACTS:
                if given.get(fact) is not None:
                    freya[fact] = given.pop(fact)
            stats = {name: dict(values) for name, values in self._model_stats.items()}
            given_tests = given.pop("tests", None)
            tests = (
                list(given_tests)
                if isinstance(given_tests, list | tuple) and given_tests
                else list(self._tests)
            )
            result: dict[str, Any] = {
                "run_id": self.run_id,
                "slot": given.pop("slot", None) or self.slot,
                "track": given.pop("track", None),
                "status": given.pop("status", None),
                "started_at": _iso(self.started_at),
                "ended_at": _iso(ended),
                "duration_s": round(duration, 3),
                "model_calls": {
                    "total": sum(s["calls"] for s in stats.values()),
                    "ok": sum(s["ok"] for s in stats.values()),
                    "failed": sum(s["failed"] for s in stats.values()),
                    "by_model": stats,
                },
                "freya": freya,
                "tests": tests,
                "stages": list(self._stages),
                "errors": self._error_count,
                "extra": given,
            }
            try:
                block: list[str] | None = self._summary_block(result)
            except Exception:  # odd caller data must not cost the run its summary record
                logger.exception("could not render the trace summary block")
                block = None
            record = self._emit("summary", "build summary", result, text=f"run {self.run_id} finished")
            if record and block:  # record is empty when the trace is already closed
                self._write_block(block)
            return self._jsonable(result)

    def _write_block(self, block: list[str]) -> None:
        """Append the multi-line summary block to the text log only (the JSONL already has the record)."""
        text = self._redactor.text("\n".join(block))
        handle = getattr(self, "_log_file", None)
        if handle is not None:
            try:
                handle.write(text + "\n")
                handle.flush()
            except (OSError, ValueError) as exc:
                logger.error("trace summary write failed: %s", exc)
        for line in text.split("\n"):
            self._echo_line(line)

    def _summary_block(self, summary: Mapping[str, Any]) -> list[str]:
        """Render the human-readable summary block."""
        rule = "=" * 100
        calls = summary["model_calls"]
        freya = summary["freya"]
        tests = summary["tests"]
        lines = [
            rule,
            "BUILD SUMMARY",
            f"run id       : {summary['run_id']}",
            f"slot         : {summary['slot']}",
            f"track        : {summary['track'] or '-'}",
            f"status       : {summary['status'] or '-'}",
            f"started      : {summary['started_at']}",
            f"ended        : {summary['ended_at']}",
            f"duration     : {_human_duration(summary['duration_s'])}",
            f"model calls  : {calls['total']} ({calls['ok']} ok, {calls['failed']} failed)",
        ]
        for name, stats in sorted(calls["by_model"].items()):
            lines.append(
                f"  - {name}: calls={stats['calls']} ({stats['ok']} ok, {stats['failed']} failed), "
                f"tokens in={stats['input_tokens']} out={stats['output_tokens']}"
            )
        if freya:
            lines.append("Freya        :")
            labels = {
                "workflow_id": "workflow id",
                "agent_id": "agent id",
                "version": "published version",
                "node_count": "steps (nodes)",
                "tool_count": "tools",
            }
            if (
                freya.get("published") is False
            ):  # the newest version is stored but nothing confirms it is live
                labels["version"] = "newest version (NOT confirmed published)"
            elif freya.get("published") is None:
                labels["version"] = "version"
            for key, value in freya.items():
                lines.append(f"  - {labels.get(key, key)}: {value}")
        else:
            lines.append("Freya        : nothing created")
        passed = sum(1 for test in tests if _test_passed(test))
        lines.append(f"tests        : {len(tests)} ({passed} passed, {len(tests) - passed} failed)")
        for test in tests:
            name, verdict, text = _test_fields(test)
            lines.append(f"  - {verdict} {name}: {_one_line(text, 200)}")
        if summary["stages"]:
            lines.append("stages       :")
            for stage in summary["stages"]:
                duration = stage.get("duration_s")
                timing = f" {duration:.1f}s" if isinstance(duration, int | float) else ""
                lines.append(f"  - {stage['name']}: {stage['status']}{timing}")
        lines.append(f"errors       : {summary['errors']}")
        for key, value in summary["extra"].items():
            lines.append(f"{key:<13}: {_one_line(_compact(value), 300)}")
        lines.append(rule)
        return lines

    def close(self) -> None:
        """Write the END line and close the files. Idempotent; later events are dropped with a warning."""
        with self._lock:
            if self._closed:
                return
            elapsed = self._monotonic() - self._started_mono
            self._emit(
                "end", f"run {self.run_id} closed after {elapsed:.1f} s", {"duration_s": round(elapsed, 3)}
            )
            self._closed = True
            self._close_files()

    def __enter__(self) -> TraceWriter:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


class NullTrace(TraceWriter):
    """A :class:`TraceWriter` that writes no files and keeps everything in memory.

    ``events`` holds the JSON records and ``lines`` the text lines (summary block lines included), both redacted
    exactly as the files would be. Intended for tests and for code paths that run without a real trace.

    The constructor mirrors :class:`TraceWriter` (so it can stand in anywhere); ``directory`` is accepted and ignored,
    and every argument has a default.
    """

    def __init__(
        self,
        run_id: str = "test-run",
        slot: str = "test",
        directory: Path | None = None,
        secrets: Iterable[str] = (),
        *,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        echo: TextIO | None = None,
    ) -> None:
        self.events: list[dict[str, Any]] = []
        self.lines: list[str] = []
        super().__init__(run_id, slot, directory, secrets, clock=clock, monotonic=monotonic, echo=echo)

    def _open(self, directory: Path) -> None:
        """No files: nothing to open."""
        self._log_file = None
        self._jsonl_file = None

    def _write(self, line: str, record: dict[str, Any]) -> None:
        self.lines.append(line)
        self.events.append(json.loads(json.dumps(record, ensure_ascii=False, allow_nan=False)))

    def _write_block(self, block: list[str]) -> None:
        self.lines.extend(self._redactor.text("\n".join(block)).split("\n"))

    def of_kind(self, kind: str) -> list[dict[str, Any]]:
        """The recorded events of one kind (e.g. ``"model_call"``), in order."""
        return [event for event in self.events if event.get("kind") == kind]

    @property
    def text(self) -> str:
        """All text lines joined with newlines (what the ``.log`` file would contain)."""
        return "\n".join(self.lines)


def _format_pairs(data: Mapping[str, Any]) -> str:
    """Render data as ``key=value`` pairs for a text line (None values skipped, long values shortened)."""
    parts = []
    for key, value in data.items():
        if value is None:
            continue
        parts.append(f"{key}={_one_line(_compact(value), MAX_VALUE_CHARS)}")
    return " ".join(parts)


def _compact(value: Any) -> str:
    """Short, readable rendering of a value for the text log."""
    if isinstance(value, str):
        return value
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, PurePath):
        return value.as_posix()
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, bool | int):
        return str(value)
    if isinstance(value, list | tuple | set | frozenset) and all(
        isinstance(item, str | int | float | bool) for item in value
    ):
        items = sorted(value, key=repr) if isinstance(value, set | frozenset) else list(value)
        return "[" + ", ".join(str(item) for item in items) + "]"
    try:
        return json.dumps(value, ensure_ascii=False, default=str, sort_keys=False)
    except (TypeError, ValueError):
        return str(value)


def _test_fields(test: Any) -> tuple[str, str, str]:
    """(name, PASS/FAIL, summary) from a test entry given as a dict or an object."""
    getter = test.get if isinstance(test, Mapping) else lambda key, default=None: getattr(test, key, default)
    name = str(getter("name", "test"))
    verdict = "PASS" if _test_passed(test) else "FAIL"
    return name, verdict, str(getter("summary", "") or "")


def _base_test_name(name: Any) -> str:
    """``smoke#2`` -> ``smoke`` (repair rounds are numbered by the builder state, not by the trace)."""
    return str(name).split("#", 1)[0]


def _test_passed(test: Any) -> bool:
    if isinstance(test, Mapping):
        return bool(test.get("passed"))
    return bool(getattr(test, "passed", False))


__all__ = [
    "KIND_LABELS",
    "MIN_REDACT_LENGTH",
    "REDACTED",
    "NullTrace",
    "TraceWriter",
]
