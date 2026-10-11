"""Start-up checks that must work before any dependency is imported. This module imports only the standard library.

The organisers run ``python3 builder/build.py {brief_path} {tool_api_path}``. On a host whose ``python3`` is outside the
supported range (``>=3.11,<3.14``, see ``pyproject.toml``) or has none of the dependencies installed, the run used to end
in a bare ``ImportError`` traceback: exit code 1, no JSON summary line, no hint what to do. ``builder/build.py`` calls
the functions below *before* it imports anything else, so that even then it ends the way every other failure does:

* a short message on stderr that names the interpreter and the exact commands that fix it,
* the one-line JSON summary (``status: failed``) as the last line of stdout,
* exit code 2.

Before giving up, an unsupported interpreter (or one without the dependencies) hands the run over to one that can run
it (audit AUD-02: on a developer machine ``python3`` may be a newer Python than the repository's ``.venv``): the
interpreter named by ``$FREYA_BUILDER_PYTHON``, else the repository's own ``.venv`` when its Python is supported, else
``uv run``. The same command line runs there; its JSON line stays the last line of stdout and its exit code is the run's.
``$FREYA_BUILDER_REEXEC`` (set for the child) stops a second hand-over, so it can never loop. Only when no candidate
exists does the run end with the message above.

Nothing here may import a third-party package (a test checks it): it has to run on an interpreter that has none.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import NoReturn

__all__ = [
    "MAX_PYTHON_EXCLUSIVE",
    "MIN_PYTHON",
    "PYTHON_OVERRIDE_VARIABLE",
    "REEXEC_GUARD_VARIABLE",
    "check_python",
    "fallback_interpreter",
    "incomplete_checkout_message",
    "install_help",
    "is_builder_module_missing",
    "missing_dependency_message",
    "python_support_problem",
    "rerun_with",
    "script_slot",
    "start_failure",
    "start_failure_summary",
]

#: Oldest supported interpreter (inclusive) and first unsupported one (``requires-python = ">=3.11,<3.14"``).
MIN_PYTHON = (3, 11)
MAX_PYTHON_EXCLUSIVE = (3, 14)

#: The repository root (``builder/`` lives directly below it).
REPO_ROOT = Path(__file__).resolve().parent.parent
#: Set in the environment of a re-started builder: it never hands the run over a second time.
REEXEC_GUARD_VARIABLE = "FREYA_BUILDER_REEXEC"
#: An interpreter to hand an unsupported run over to (tried before the repository's ``.venv`` and ``uv``).
PYTHON_OVERRIDE_VARIABLE = "FREYA_BUILDER_PYTHON"

#: ``build.py`` options that take a value (so the value is not mistaken for a brief file when guessing the slot).
_VALUE_OPTIONS = frozenset({"--slot", "--track", "--profile", "--self-test", "--budget-s", "--fake-llm"})

_FIX_ONE_LINE = (
    "fix: uv sync --extra agentic --extra ingest, then uv run python builder/build.py <brief> <tool_api>; "
    "or <python 3.11-3.13> -m pip install -r requirements.txt; "
    "or docker compose run --rm builder <brief> <tool_api>"
)


def python_support_problem(version_info: Sequence[int] | None = None) -> str | None:
    """Why the interpreter cannot run the builder, or ``None`` when its version is supported.

    Args:
        version_info: ``(major, minor, micro)``; the running interpreter when omitted.
    """
    version = tuple(int(part) for part in (version_info if version_info is not None else sys.version_info[:3]))
    if MIN_PYTHON <= version[:2] < MAX_PYTHON_EXCLUSIVE:
        return None
    shown = ".".join(str(part) for part in version)
    return (
        f"Python {shown} ({sys.executable}) is not supported: the builder needs Python 3.11, 3.12 or 3.13 "
        "(its dependencies are not published for other versions)."
    )


def install_help(python: str | None = None, *, current_interpreter: bool = True) -> str:
    """The commands that give the builder an interpreter and dependencies it can run with (multi-line text).

    Args:
        python: The interpreter to name in the ``pip`` line (default: the running one).
        current_interpreter: Whether to offer installing into the running interpreter. Not when its *version* is the
            problem: pip cannot fix a Python that is too new.
    """
    interpreter = python or sys.executable or "python"
    requirements = Path(__file__).resolve().parent.parent / "requirements.txt"
    lines = [
        "Fix it with any one of these (run them in the repository folder):",
        "  1. uv - it also installs a suitable Python by itself:",
        "       uv sync --extra agentic --extra ingest",
        "       uv run python builder/build.py <brief_path> <tool_api_path>",
        "  2. a virtual environment on Python 3.11, 3.12 or 3.13:",
        "       py -3.13 -m venv .venv          (Windows)    python3.13 -m venv .venv          (Linux / macOS)",
        "       .venv\\Scripts\\python.exe -m pip install -r requirements.txt    (Windows)",
        "       .venv/bin/python -m pip install -r requirements.txt            (Linux / macOS)",
        "       then start the builder with that interpreter instead of python3",
    ]
    if current_interpreter:
        lines.append(f"     or, for the interpreter that just ran: {interpreter} -m pip install -r requirements.txt")
    if not requirements.exists():
        lines.append("     (requirements.txt is missing from this checkout; `uv export` recreates it, see README)")
    lines += [
        "  3. Docker, with no local Python at all:",
        "       docker compose run --rm builder <brief_path> <tool_api_path>",
    ]
    return "\n".join(lines)


def missing_dependency_message(exc: BaseException) -> str:
    """One paragraph for an import error at start-up: what failed, with which interpreter, and the fix."""
    return (
        f"a required Python package is missing or broken: {type(exc).__name__}: {exc} "
        f"(interpreter {sys.executable}, Python {'.'.join(str(p) for p in sys.version_info[:3])})."
    )


def is_builder_module_missing(exc: BaseException) -> bool:
    """True when an import error names one of the builder's own modules (an incomplete checkout, not a package)."""
    name = getattr(exc, "name", None) or ""
    return isinstance(exc, ImportError) and (name == "builder" or name.startswith("builder."))


def incomplete_checkout_message(exc: BaseException) -> str:
    """One paragraph for a builder module that is missing from the checkout (another interpreter cannot fix that)."""
    name = getattr(exc, "name", None) or str(exc)
    return (
        f"the checkout is incomplete: the builder module {name} is missing ({type(exc).__name__}: {exc}). "
        "Commit every file under builder/ (git status lists the untracked ones) and check out that commit again."
    )


def _venv_version(venv: Path) -> tuple[int, int] | None:
    """The ``(major, minor)`` Python version of a virtual environment, read from its ``pyvenv.cfg`` (no subprocess)."""
    try:
        text = (venv / "pyvenv.cfg").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = re.search(r"^\s*(?:version_info|version)\s*=\s*(\d+)\.(\d+)", text, re.MULTILINE)
    return (int(match.group(1)), int(match.group(2))) if match else None


def fallback_interpreter(
    environ: Mapping[str, str] | None = None, root: Path | None = None
) -> list[str] | None:
    """The command prefix of an interpreter that can run the builder instead of this one, or ``None``.

    In order: ``$FREYA_BUILDER_PYTHON``; the repository's ``.venv`` when its Python is in the supported range (and it
    is not the interpreter already running); ``uv run`` (which installs a suitable Python and the locked
    dependencies by itself). ``None`` inside a run that was already handed over (``$FREYA_BUILDER_REEXEC``).
    """
    env = os.environ if environ is None else environ
    if env.get(REEXEC_GUARD_VARIABLE):
        return None
    override = (env.get(PYTHON_OVERRIDE_VARIABLE) or "").strip()
    if override:
        return [override]
    base = REPO_ROOT if root is None else root
    venv = base / ".venv"
    version = _venv_version(venv)
    if version is not None and MIN_PYTHON <= version < MAX_PYTHON_EXCLUSIVE:
        for candidate in (venv / "Scripts" / "python.exe", venv / "bin" / "python"):
            if candidate.is_file():
                try:
                    same = Path(sys.executable).resolve() == candidate.resolve()
                except OSError:
                    same = False
                if not same:
                    return [str(candidate)]
    uv = shutil.which("uv")
    if uv and (base / "pyproject.toml").is_file():
        return [uv, "run", "--locked", "--extra", "agentic", "--extra", "ingest", "--project", str(base), "python"]
    return None


def rerun_with(command: Sequence[str], target: Sequence[str], argv: Sequence[str]) -> NoReturn:
    """Run ``command + target + argv`` (same working directory, stdin/stdout/stderr) and exit with its exit code.

    ``target`` is the script path, or ``["-m", "module"]``. The child gets ``$FREYA_BUILDER_REEXEC`` so that it never
    hands over again. A Ctrl+C reaches both processes on a console; the parent then waits for the child, so the child's
    trace and JSON line are still written. A child that cannot even be started raises ``OSError`` (the caller then
    reports the original problem).
    """
    full = [*command, *target, *argv]
    print(f"note: this interpreter cannot run the builder; re-running with: {' '.join(command)}", file=sys.stderr)
    sys.stderr.flush()
    sys.stdout.flush()
    child = subprocess.Popen(full, env={**os.environ, REEXEC_GUARD_VARIABLE: "1"})
    while True:
        try:
            code = child.wait()
            break
        except KeyboardInterrupt:
            continue
    raise SystemExit(code)


def _hand_over(target: Sequence[str] | None, argv: Sequence[str]) -> None:
    """Re-run with a fallback interpreter when there is one (does not return then); otherwise return."""
    if not target:
        return
    command = fallback_interpreter()
    if command is None:
        return
    try:
        rerun_with(command, target, argv)
    except OSError as exc:  # the candidate cannot be started: report the original problem
        print(f"note: could not start {command[0]}: {exc}", file=sys.stderr)


def script_slot(argv: Sequence[str]) -> str:
    """Best guess of the build slot from the raw command line (the first brief file's stem, or ``--slot``)."""
    tokens = list(argv)
    for index, token in enumerate(tokens):
        if token == "--slot" and index + 1 < len(tokens):
            return tokens[index + 1] or "inputs"
        if token.startswith("--slot="):
            return token.split("=", 1)[1] or "inputs"
    skip = False
    for token in tokens:
        if skip:
            skip = False
            continue
        if token in _VALUE_OPTIONS:
            skip = True
            continue
        if token.startswith("-"):
            continue
        return Path(token).stem or "inputs"
    return "inputs"


def start_failure_summary(message: str, argv: Sequence[str] = ()) -> dict[str, object]:
    """The final JSON line of a run that could not start: the usual keys, ``status: failed``, the message and the fix."""
    return {
        "slot": script_slot(argv),
        "track": "",
        "workflow_id": None,
        "agent_id": None,
        "version": None,
        "exported": None,
        "trace": "",
        "status": "failed",
        "errors": [message, _FIX_ONE_LINE],
    }


def start_failure(
    message: str,
    argv: Sequence[str] | None = None,
    *,
    current_interpreter: bool = True,
    rerun: Sequence[str] | None = None,
    summary: Mapping[str, object] | None = None,
    show_help: bool = True,
) -> NoReturn:
    """Report a start-up failure the way every other failure is reported, then exit with code 2.

    Args:
        message: What went wrong.
        argv: The command-line arguments (default ``sys.argv[1:]``).
        current_interpreter: Whether installing into the running interpreter can fix it.
        rerun: The script path (or ``["-m", module]``) to re-run with a fallback interpreter first
            (:func:`fallback_interpreter`); ``None`` reports at once.
        summary: The JSON object to print instead of the build summary (another command's own failure shape).
        show_help: Whether to print the install commands (not for an incomplete checkout).
    """
    arguments = list(sys.argv[1:] if argv is None else argv)
    _hand_over(rerun, arguments)
    print(f"error: {message}", file=sys.stderr)
    if show_help:
        print(install_help(current_interpreter=current_interpreter), file=sys.stderr)
    sys.stderr.flush()
    shown = dict(summary) if summary is not None else start_failure_summary(message, arguments)
    if summary is None and not show_help:
        shown["errors"] = [message]
    print(json.dumps(shown, ensure_ascii=True), flush=True)
    sys.stdout.flush()
    raise SystemExit(2)


def check_python(
    argv: Sequence[str] | None = None,
    *,
    rerun: Sequence[str] | None = None,
    summary: Mapping[str, object] | None = None,
) -> None:
    """Exit with a clear message when the interpreter is outside the supported range (no-op otherwise).

    With ``rerun`` (the script path, or ``["-m", module]``) the run is first handed over to a supported interpreter
    when one exists (:func:`fallback_interpreter`); it then never returns here.
    """
    problem = python_support_problem()
    if problem is not None:
        start_failure(problem, argv, current_interpreter=False, rerun=rerun, summary=summary)
