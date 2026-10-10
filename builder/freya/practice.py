"""Run the organisers' practice harness (``tools/run_cases.py``) against a deployed agent and read its results.

The kit's ``run_cases.py`` is the official way to score an agent on the public practice cases: it creates (or
re-uses) one persona per case, starts a text simulation, reads the mock bank's ledger for every case and combines
it with the judge model's verdict. This module does not re-implement any of that. It:

* decides whether practice applies at all (:func:`kit_brief_letter`): the public cases belong to the kit's own
  briefs, so they only say something about an agent built from those exact briefs. A brief counts as the kit's when
  its content matches a ``briefs/brief-<letter>.md`` file of the kit (sha256 of the raw file or of its normalised
  text). The kit's briefs are cumulative (a later letter extends the earlier ones), so the highest matching letter
  selects the case set;
* builds the command line, runs it as a subprocess with the builder's settings exported as environment
  variables (``FREYA_API_KEY``, ``FREYA_API_URL``, ``FREYA_BANK_TEAM_KEY``, ``FREYA_WORKSPACE_ID``) and UTF-8 mode
  forced (the harness uses ``open()`` without an encoding, which is not UTF-8 by default on Windows);
* parses the JSON the harness writes with ``--out`` into a :class:`PracticeResult`.

The ``--out`` file written by ``run_cases.py`` has this shape (see its ``main()``)::

    {"agent_id", "simulation_id", "iteration_id", "seconds", "passed", "total", "runs",
     "final": [{"id", "family", "passes", "runs", "pass"}],
     "rows": [{"id", "level", "family", "pass", "failed", "call_id", "tools", "customer_id", ...}],
     "all_rows": [...]}

A practice run takes minutes (one simulation per repetition), so the graph's self-test node calls
:func:`run_practice` only when the time budget allows. The subprocess runner is injectable for tests.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from builder import paths
from builder.settings import apply_kit_env, get_settings

__all__ = [
    "RUN_CASES_SCRIPT",
    "PracticeError",
    "PracticeResult",
    "build_practice_command",
    "kit_brief_letter",
    "kit_brief_letters",
    "parse_practice_results",
    "practice_env",
    "run_practice",
]

logger = logging.getLogger(__name__)

#: The kit's harness script, under ``builder.paths.KIT_TOOLS_DIR``.
RUN_CASES_SCRIPT = "run_cases.py"

_KIT_BRIEF_FILE = re.compile(r"^brief-(?P<letter>[A-Za-z0-9]+)\.md$")
_REQUIRED_ENV = ("FREYA_API_KEY", "FREYA_BANK_TEAM_KEY")
_TAIL_LINES = 40

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


class PracticeTrace(Protocol):
    """The part of :class:`builder.trace.TraceWriter` used here."""

    def event(self, kind: str, message: str, **data: Any) -> None:
        """Record one event."""


class PracticeError(RuntimeError):
    """The practice harness could not run or produced no usable results.

    Attributes:
        returncode: The harness's exit code when it ran to completion, else ``None``.
        log_tail: The last lines of its standard output (one ``PASS``/``FAIL`` line per case).
        stderr_tail: The last lines of its standard error (tracebacks).
    """

    def __init__(
        self, message: str, *, returncode: int | None = None, log_tail: str = "", stderr_tail: str = ""
    ):
        super().__init__(message)
        self.returncode = returncode
        self.log_tail = log_tail
        self.stderr_tail = stderr_tail


class PracticeResult(BaseModel):
    """Parsed ``--out`` file of ``tools/run_cases.py``.

    ``passed``/``total`` count customers (cases); with ``runs`` > 1 a case passes only when every repetition passed.
    ``rows`` are the per-case rows of the last repetition (ledger failures, tool sequence, call id); ``final`` holds
    the per-case verdict across repetitions.
    """

    model_config = ConfigDict(frozen=True)

    passed: int
    total: int
    runs: int
    rows: list[dict[str, Any]] = Field(default_factory=list)
    simulation_id: str | None = None
    raw_path: Path
    final: list[dict[str, Any]] = Field(default_factory=list)
    iteration_id: str | None = None
    seconds: int | None = None
    agent_id: str | None = None
    letter: str | None = None
    log_tail: str = ""

    @property
    def failed_ids(self) -> list[str]:
        """Ids of the cases that did not pass (from ``final``, else from ``rows``)."""
        source = self.final or self.rows
        return [str(item.get("id")) for item in source if not item.get("pass")]

    @property
    def pass_rate(self) -> float:
        """``passed / total`` (0.0 when no case ran)."""
        return self.passed / self.total if self.total else 0.0


# ---------------------------------------------------------------------- which kit brief is this?


def kit_brief_letters(kit_briefs_dir: Path | None = None) -> dict[str, frozenset[str]]:
    """Return ``{letter: sha256 fingerprints}`` for every ``brief-<letter>.md`` in the kit's ``briefs/`` directory.

    Each file gets three fingerprints, so a copy matches however the brief loader hashed it: the raw bytes, the
    normalised text (BOM removed, CRLF/CR -> LF) and that text stripped of surrounding whitespace.
    """
    directory = kit_briefs_dir or (paths.REPO_ROOT / "briefs")
    letters: dict[str, frozenset[str]] = {}
    if not directory.is_dir():
        return letters
    for path in sorted(directory.iterdir()):
        match = _KIT_BRIEF_FILE.match(path.name)
        if not match or not path.is_file():
            continue
        raw = path.read_bytes()
        letters[match["letter"].upper()] = frozenset(
            {_sha256(raw), *_text_fingerprints(raw.decode("utf-8", "replace"))}
        )
    return letters


def kit_brief_letter(bundle: Any, *, kit_briefs_dir: Path | None = None) -> str | None:
    """Return the kit brief letter whose public practice cases apply to ``bundle``, or ``None``.

    Args:
        bundle: A :class:`builder.ingest.brief_loader.BriefBundle` or its ``model_dump()`` dict -- anything with
            ``documents`` whose items have ``text`` and/or ``sha256``.
        kit_briefs_dir: The kit's ``briefs/`` directory (default ``REPO_ROOT/briefs``).

    Returns:
        The upper-case letter of the highest matching kit file ``brief-<letter>.md`` (so with two kit briefs, the
        later one wins whether or not the earlier one is also an input), or ``None`` when no input is a kit brief
        (an unseen brief: the practice cases would test the wrong rules).
    """
    kit = kit_brief_letters(kit_briefs_dir)
    if not kit:
        return None
    matched: set[str] = set()
    for document in _documents(bundle):
        fingerprints = _document_fingerprints(document)
        matched.update(letter for letter, known in kit.items() if fingerprints & known)
    if not matched:
        return None
    return max(matched, key=lambda letter: (len(letter), letter))


def _documents(bundle: Any) -> list[Any]:
    documents = bundle.get("documents") if isinstance(bundle, Mapping) else getattr(bundle, "documents", None)
    return list(documents or [])


def _document_fingerprints(document: Any) -> set[str]:
    def field(name: str) -> Any:
        return document.get(name) if isinstance(document, Mapping) else getattr(document, name, None)

    fingerprints: set[str] = set()
    digest = field("sha256")
    if isinstance(digest, str) and digest:
        fingerprints.add(digest.lower())
    text = field("text")
    if isinstance(text, str) and text:
        fingerprints.update(_text_fingerprints(text))
    return fingerprints


def _text_fingerprints(text: str) -> set[str]:
    normalised = text.removeprefix("﻿").replace("\r\n", "\n").replace("\r", "\n")
    return {_sha256(normalised.encode("utf-8")), _sha256(normalised.strip().encode("utf-8"))}


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------- running the harness


def practice_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return the environment for the harness: ``base`` (default ``os.environ``) plus the builder's Freya settings.

    ``os.environ`` itself is not modified. UTF-8 mode is forced so that the harness reads the case file and prints
    case names correctly on Windows.
    """
    env = dict(os.environ if base is None else base)
    apply_kit_env(get_settings(), environ=env)
    return _with_utf8(env)


def build_practice_command(
    agent_id: str,
    letter: str | None,
    *,
    out: Path,
    reps: int = 1,
    style: str | None = None,
    only: str | Sequence[str] | None = None,
    python: str | None = None,
) -> list[str]:
    """Build the ``run_cases.py`` command line.

    Produces ``<python> tools/run_cases.py <agent_id> --reps N --out <out> [--brief L] [--style S] [--only a,b]``.
    ``letter=None`` runs every case in the case file.
    """
    if not agent_id or agent_id.startswith("-"):
        raise ValueError(f"invalid agent id {agent_id!r}")
    if reps < 1:
        raise ValueError("reps must be at least 1")
    command = [
        python or sys.executable,
        str(paths.KIT_TOOLS_DIR / RUN_CASES_SCRIPT),
        agent_id,
        "--reps",
        str(reps),
        "--out",
        str(out),
    ]
    if letter:
        command += ["--brief", letter.upper()]
    if style:
        command += ["--style", style]
    only_csv = _only_csv(only)
    if only_csv:
        command += ["--only", only_csv]
    return command


def parse_practice_results(path: Path, *, letter: str | None = None, log_tail: str = "") -> PracticeResult:
    """Parse the JSON that ``run_cases.py --out`` wrote.

    Raises:
        PracticeError: The file is missing, is not JSON, or lacks ``passed``/``total``.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PracticeError(f"cannot read the practice results {path}: {exc}", log_tail=log_tail) from exc
    if not isinstance(data, dict):
        raise PracticeError(f"the practice results {path} are not a JSON object", log_tail=log_tail)
    try:
        passed, total = int(data["passed"]), int(data["total"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PracticeError(
            f"the practice results {path} lack valid passed/total counts", log_tail=log_tail
        ) from exc
    return PracticeResult(
        passed=passed,
        total=total,
        runs=_int_or(data.get("runs"), 1) or 1,
        rows=[row for row in data.get("rows") or [] if isinstance(row, dict)],
        simulation_id=_str_or_none(data.get("simulation_id")),
        raw_path=path,
        final=[item for item in data.get("final") or [] if isinstance(item, dict)],
        iteration_id=_str_or_none(data.get("iteration_id")),
        seconds=_int_or(data.get("seconds"), None),
        agent_id=_str_or_none(data.get("agent_id")),
        letter=letter.upper() if letter else None,
        log_tail=log_tail,
    )


def run_practice(
    agent_id: str,
    letter: str | None,
    reps: int = 1,
    style: str | None = None,
    only: str | Sequence[str] | None = None,
    out: Path | str | None = None,
    timeout_s: float = 1500,
    *,
    runner: Runner = subprocess.run,
    env: Mapping[str, str] | None = None,
    trace: PracticeTrace | None = None,
    python: str | None = None,
) -> PracticeResult:
    """Run ``tools/run_cases.py`` for ``agent_id`` and return its parsed results.

    Args:
        agent_id: The deployed agent (``DeployInfo.agent_id``).
        letter: The kit brief letter from :func:`kit_brief_letter` (selects the cases); ``None`` runs all cases.
        reps: Repetitions; a case passes only when every repetition passes.
        style: Optional customer speaking style understood by the harness (``--style``).
        only: Case ids to run (a list or a comma-separated string).
        out: Where the harness writes its JSON (default ``results/practice-<letter>-<agent>-<time>.json``). A stale
            file at that path is removed first so an old result is never mistaken for a new one.
        timeout_s: Wall-clock limit for the whole subprocess; it is killed when exceeded.
        runner: ``subprocess.run`` or a test double with the same signature.
        env: The complete environment for the subprocess (UTF-8 mode is added); default :func:`practice_env`.
        trace: Optional trace receiving ``event("practice", ...)`` at start and end.
        python: Interpreter to run the harness with (default ``sys.executable``).

    Raises:
        PracticeError: Missing credentials, timeout, non-zero exit, or no usable results file.
    """
    if timeout_s <= 0:
        raise ValueError("timeout_s must be positive")
    target = Path(out) if out is not None else _default_out(agent_id, letter)
    target = target if target.is_absolute() else paths.REPO_ROOT / target
    command = build_practice_command(
        agent_id, letter, out=target, reps=reps, style=style, only=only, python=python
    )
    run_env = _with_utf8(dict(env)) if env is not None else practice_env()
    missing = [name for name in _REQUIRED_ENV if not run_env.get(name)]
    if missing:
        raise PracticeError(
            f"the practice harness needs {', '.join(missing)}; set it in .env or the environment"
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    target.unlink(missing_ok=True)
    _event(
        trace,
        f"running {RUN_CASES_SCRIPT} for agent {agent_id}",
        agent_id=agent_id,
        letter=letter,
        reps=reps,
        style=style,
        only=_only_csv(only),
        out=str(target),
        timeout_s=timeout_s,
    )
    started = time.monotonic()
    try:
        completed = runner(
            command,
            cwd=str(paths.REPO_ROOT),
            env=run_env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        tail = _tail(exc.stdout)
        _event(trace, f"{RUN_CASES_SCRIPT} timed out after {timeout_s:.0f} s", ok=False, log_tail=tail)
        raise PracticeError(
            f"{RUN_CASES_SCRIPT} did not finish within {timeout_s:.0f} s", log_tail=tail
        ) from exc
    except OSError as exc:
        raise PracticeError(f"cannot start {RUN_CASES_SCRIPT}: {exc}") from exc

    elapsed = time.monotonic() - started
    log_tail, stderr_tail = _tail(completed.stdout), _tail(completed.stderr)
    if completed.returncode != 0:
        _event(
            trace,
            f"{RUN_CASES_SCRIPT} exited with {completed.returncode}",
            ok=False,
            returncode=completed.returncode,
            stderr_tail=stderr_tail,
        )
        raise PracticeError(
            f"{RUN_CASES_SCRIPT} exited with code {completed.returncode}: {_last_line(stderr_tail or log_tail)}",
            returncode=completed.returncode,
            log_tail=log_tail,
            stderr_tail=stderr_tail,
        )
    if not target.is_file():
        raise PracticeError(
            f"{RUN_CASES_SCRIPT} finished but wrote no results to {target}",
            returncode=0,
            log_tail=log_tail,
            stderr_tail=stderr_tail,
        )
    result = parse_practice_results(target, letter=letter, log_tail=log_tail)
    _event(
        trace,
        f"practice {result.passed}/{result.total} passed",
        ok=True,
        passed=result.passed,
        total=result.total,
        runs=result.runs,
        failed=result.failed_ids,
        simulation_id=result.simulation_id,
        seconds=round(elapsed),
        raw_path=str(target),
    )
    return result


# ---------------------------------------------------------------------- helpers


def _default_out(agent_id: str, letter: str | None) -> Path:
    agent = re.sub(r"[^A-Za-z0-9_-]", "", agent_id)[:8] or "agent"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return paths.RESULTS_DIR / f"practice-{(letter or 'all').lower()}-{agent}-{stamp}.json"


def _only_csv(only: str | Sequence[str] | None) -> str:
    if not only:
        return ""
    items = only.split(",") if isinstance(only, str) else list(only)
    return ",".join(item.strip() for item in items if item and item.strip())


def _with_utf8(env: dict[str, str]) -> dict[str, str]:
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _tail(output: str | bytes | None, lines: int = _TAIL_LINES) -> str:
    if output is None:
        return ""
    text = output.decode("utf-8", "replace") if isinstance(output, bytes) else output
    return "\n".join(text.strip().splitlines()[-lines:])


def _last_line(text: str) -> str:
    stripped = text.strip()
    return stripped.splitlines()[-1] if stripped else "no output"


def _int_or(value: Any, default: int | None) -> int | None:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _str_or_none(value: Any) -> str | None:
    return str(value) if value not in (None, "") else None


def _event(trace: PracticeTrace | None, message: str, **data: Any) -> None:
    logger.info("practice: %s", message)
    if trace is None:
        return
    try:
        trace.event("practice", message, **data)
    except Exception:  # pragma: no cover - tracing is a side channel and must not break the run
        logger.exception("trace.event(practice) failed")
