"""Command-line entry point of the builder: brief(s) + tool API in, a deployed Freya phone agent out.

Usage (what the organisers run is ``python3 builder/build.py {brief_path} {tool_api_path}``)::

    python builder/build.py <brief>... <tool_api> [--slot S] [--dry-run] [--track auto|compiled|agentic]
        [--profile staged|lean] [--self-test auto|none|smoke|walkthrough|practice] [--budget-s N] [--fake-llm DIR]

It also runs as ``python -m builder.build`` and as the ``freya-builder`` console script. A directory argument is expanded
to its files; the tool API is the one file that is an OpenAPI / Swagger document, every other file is brief material.

What happens: the inputs are classified (:mod:`builder.inputs`), a trace is opened (``traces/<slot>-<time>.log`` and
``.jsonl``; secrets redacted), the LangGraph of :mod:`builder.graph` runs (understand the brief -> policy IR -> compile ->
checks -> deploy -> self-test, with repair loops and an agentic fallback), and the trace summary is written.

Policies
--------
* **Never crash**: every stage catches its own errors; whatever happens, the trace is finalised and one JSON summary
  line is printed as the *last line of stdout* (all progress goes to stderr)::

      {"slot", "track", "workflow_id", "agent_id", "version", "exported", "trace", "status", ...}

  That includes an invalid ``.env`` / environment value, an unwritable ``traces/`` folder, an unreadable input and
  an interpreter that is outside ``>=3.11,<3.14`` or lacks the dependencies: the last case is checked before any
  dependency is imported (:mod:`builder._startup`) and prints the exact install commands.

* **Exit code**: 0 when an agent was deployed (or a ``--dry-run`` produced a lint-clean, guard-clean workflow), 2 for
  anything else (including usage errors).
* **Wall-clock guard**: the graph is cut off a little after the time budget (default 28 minutes, the limit is 30); what
  was deployed by then is kept and reported. Platform calls run in abandonable daemon threads bound to a cancel token
  (:mod:`builder.freya.background`): the guard does not wait for a blocked request, no further request is started
  after it, a deploy that was already in flight gets a short grace period (:data:`INFLIGHT_GRACE_S`) so that what it
  created is reported (as unconfirmed), and the final JSON carries ``"cut_off": true``.
* **Dry run**: no Freya call at all; the workflow is written to ``out/<slot>.workflow.json``.
* ``--fake-llm DIR`` answers every model call from fixture files (``ledger.json`` + ``ir.json`` or ``<name>_ledger.json`` +
  ``<name>_ir.json``) so the whole pipeline can be exercised offline, without keys.
"""

from __future__ import annotations

import sys
from pathlib import Path

if not __package__:  # started as a script: make `import builder` work and keep builder/ itself off the path
    _here = Path(__file__).resolve().parent
    sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != _here]
    sys.path.insert(0, str(_here.parent))

try:
    from builder._startup import (  # standard library only
        check_python,
        incomplete_checkout_message,
        is_builder_module_missing,
        missing_dependency_message,
        start_failure,
    )
except ImportError as _startup_error:  # an incomplete checkout (audit AUD-03): still one JSON line and exit 2
    if __name__ != "__main__":
        raise
    import json as _json

    _message = f"the checkout is incomplete: {type(_startup_error).__name__}: {_startup_error}"
    print(f"error: {_message}", file=sys.stderr)
    print(_json.dumps({"slot": "inputs", "track": "", "workflow_id": None, "agent_id": None, "version": None,
                       "exported": None, "trace": "", "status": "failed", "errors": [_message]}), flush=True)
    raise SystemExit(2) from None

if __name__ == "__main__":
    # an unsupported interpreter is handed over to a supported one (the repository's .venv, $FREYA_BUILDER_PYTHON or
    # uv) when there is one; otherwise it ends here (message, JSON line, exit 2), before any dependency is imported
    check_python(rerun=[str(Path(__file__).resolve())])

import argparse
import asyncio
import json
import logging
import time
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

try:
    from pydantic import ValidationError

    from builder import paths
    from builder.budget import Budget
    from builder.freya.background import wait_inflight
    from builder.graph.build_graph import RECURSION_LIMIT, build_graph
    from builder.graph.context import BuilderContext
    from builder.graph.nodes.finalize import SUCCESS_STATUSES, finalize_run
    from builder.graph.state import initial_state
    from builder.inputs import InputBundle, InputError, classify_inputs
    from builder.settings import Settings, apply_kit_env, get_settings, secret_values
    from builder.trace import TraceWriter
    from builder.trace import (
        _Redactor as Redactor,  # the trace's own redactor: one definition of "secret-shaped"
    )
except ImportError as exc:  # a dependency is missing or broken: name the fix instead of a bare traceback
    if __name__ != "__main__":
        raise
    if is_builder_module_missing(exc):  # a file of the builder itself is missing: no interpreter can fix that
        start_failure(incomplete_checkout_message(exc), show_help=False)
    start_failure(missing_dependency_message(exc), rerun=[str(Path(__file__).resolve())])

logger = logging.getLogger("builder.build")

#: Seconds after the budget at which the wall-clock guard cancels the graph.
GUARD_GRACE_S = 45.0
#: Seconds the guard waits for a platform call that was in flight when it fired (so that a workflow it created can be
#: reported). The guard sits at budget + GUARD_GRACE_S = 28 min 45 s by default; this keeps the end below 30 minutes.
INFLIGHT_GRACE_S = 15.0
TRACKS = ("auto", "compiled", "agentic")
PROFILES = ("staged", "lean")
SELF_TESTS = ("auto", "none", "smoke", "walkthrough", "practice")
#: The error of a build that was asked to deploy but had no Freya key (it is built as a dry run and exits 2).
FORCED_DRY_RUN_ERROR = "FREYA_API_KEY is not set: nothing was deployed (built as a dry run; pass --dry-run to ask for one)"


def build_parser() -> argparse.ArgumentParser:
    """The command-line parser (see the module docstring for the arguments)."""
    parser = argparse.ArgumentParser(
        prog="build.py",
        description="Build a Freya phone agent from bank brief(s) and the tool API file.",
    )
    parser.add_argument(
        "inputs", nargs="+", help="brief file(s) or directories, and the tool API (OpenAPI/Swagger) file"
    )
    parser.add_argument("--slot", help="workflow label (default: derived from the brief file names)")
    parser.add_argument(
        "--dry-run", action="store_true", help="compile and check locally; make no Freya call"
    )
    parser.add_argument(
        "--track", choices=TRACKS, help="compiled (deterministic), agentic (Claude session) or auto"
    )
    parser.add_argument("--profile", choices=PROFILES, help="workflow shape produced by the compiler")
    parser.add_argument(
        "--self-test", dest="self_test", choices=SELF_TESTS, help="which self-test runs after deploy"
    )
    parser.add_argument(
        "--budget-s", dest="budget_s", type=float, help="wall-clock budget in seconds (default 1680)"
    )
    parser.add_argument(
        "--fake-llm", dest="fake_llm", metavar="DIR", help="answer model calls from fixture files in DIR"
    )
    return parser


def settings_from_args(args: argparse.Namespace, base: Settings | None = None) -> Settings:
    """The effective settings: environment / ``.env`` defaults with the command-line flags applied."""
    settings = base or get_settings()
    updates: dict[str, Any] = {}
    if args.dry_run:
        updates["dry_run"] = True
    if args.track:
        updates["builder_track"] = args.track
    if args.profile:
        updates["agent_profile"] = args.profile
    if args.self_test:
        updates["self_test"] = args.self_test
    if args.budget_s:
        if args.budget_s <= 0:
            raise InputError("--budget-s must be a positive number of seconds")
        updates["build_time_budget_s"] = float(args.budget_s)
    if args.fake_llm:
        updates["builder_track"] = "compiled"  # a fixture run must never start a real agentic session
    return settings.model_copy(update=updates) if updates else settings


def _configure_stdio() -> None:
    """Make stdout / stderr UTF-8 tolerant (Windows consoles) without ever failing."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError, OSError):
            continue


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


async def run_build(
    settings: Settings,
    inputs: InputBundle,
    *,
    fake_llm_dir: str | Path | None = None,
    llm: Any = None,
    freya: Any = None,
    trace: TraceWriter | None = None,
    budget: Budget | None = None,
    agentic_runner: Any = None,
    practice_runner: Any = None,
    echo: Any = None,
) -> dict[str, Any]:
    """Run one build and return its final summary dict (never raises; the trace is always finalised).

    Args:
        settings: Effective settings.
        inputs: The classified inputs.
        fake_llm_dir: Fixture directory for an offline run (``--fake-llm``).
        llm: A ready gateway (tests); overrides ``fake_llm_dir`` and the real key changer.
        freya: A ready Freya client (tests); a dry run uses none.
        trace: A ready trace (tests); by default one is created in ``traces/``.
        budget: A ready budget (tests).
        agentic_runner: Replacement for the Claude Agent SDK runner (tests).
        practice_runner: Replacement for the practice harness (tests).
        echo: Stream that also receives the trace lines (the CLI passes ``sys.stderr``).
    """
    run_id = uuid.uuid4().hex[:12]
    secrets = secret_values(settings)
    paths.ensure_dirs()
    own_trace = trace is None
    if trace is None:
        trace = TraceWriter(run_id, inputs.slot, secrets=secrets, echo=echo)
    run_id = trace.run_id
    observer: Any = None
    ctx: BuilderContext | None = None
    state: dict[str, Any] | None = None
    final: dict[str, Any] | None = None
    cut_off = False
    crash = ""
    forced_dry_run = False
    try:
        budget = budget or Budget(settings.build_time_budget_s)
        if not settings.dry_run and not settings.freya_api_key and freya is None:
            trace.error("FREYA_API_KEY is not set: nothing can be deployed, running as a dry run")
            settings = settings.model_copy(update={"dry_run": True})
            forced_dry_run = True
        trace.event("config", "effective configuration", **settings.public_summary())
        from builder.observability import build_observer

        observer = build_observer(settings, trace)
        observer.start_run(
            run_id, inputs.slot, {"track": settings.builder_track, "profile": settings.agent_profile}
        )
        if llm is None:
            if fake_llm_dir:
                from builder.testing import load_fixture_gateway

                llm = load_fixture_gateway(
                    fake_llm_dir, [p.name for p in inputs.brief_paths], trace, observer
                )
                trace.event("config", f"offline run: model calls are answered from {fake_llm_dir}")
            else:
                from builder.llm.gateway import build_gateway

                llm = build_gateway(settings, trace, observer=observer)
                trace.event("config", "LLM key changer", **_safe_describe(llm))
        if settings.dry_run:
            freya = None
        elif freya is None:
            apply_kit_env(settings)
            from builder.freya.client import FreyaClient

            freya = FreyaClient(trace)
        ctx = BuilderContext(
            settings=settings,
            llm=llm,
            freya=freya,
            trace=trace,
            budget=budget,
            observer=observer,
            secrets=tuple(secrets),
            agentic_runner=agentic_runner,
            practice_runner=practice_runner,
        )
        start = initial_state(
            run_id=run_id,
            slot=inputs.slot,
            started_at=_now_iso(),
            inputs=inputs.as_dict(),
            dry_run=bool(settings.dry_run),
        )
        if forced_dry_run:  # in the result file and the summary, not only in the trace (audit AUD-55)
            start["errors"] = [FORCED_DRY_RUN_ERROR]
        graph = build_graph()
        guard_s = settings.build_time_budget_s + GUARD_GRACE_S
        ctx.cancel.deadline = (
            time.monotonic() + guard_s
        )  # no platform request starts after the guard's instant
        try:
            state = await asyncio.wait_for(
                graph.ainvoke(start, context=ctx, config={"recursion_limit": RECURSION_LIMIT}),
                timeout=guard_s,
            )
        except TimeoutError:
            cut_off = True
            ctx.cancel.cancel("the wall-clock guard fired")
            trace.error("the wall-clock guard fired: the build was cut off; keeping what exists")
            # only a deploy in flight is worth waiting for (what it created is reported); a practice run is not
            if ctx.active_node == "deploy" and not await wait_inflight(INFLIGHT_GRACE_S):
                trace.error(
                    "a platform call is still running; it is abandoned (it starts no further request)"
                )
    except asyncio.CancelledError:
        trace.error("the build was cancelled")
        raise
    except Exception as exc:  # never crash: report and finalise
        logger.exception("the build failed outside the graph")
        crash = f"the build failed: {type(exc).__name__}: {exc}"[:400]
        trace.error(crash)
    finally:
        final = _finish(ctx, state, trace, inputs, observer, cut_off=cut_off, crash=crash)
        if forced_dry_run:
            final["dry_run_requested"] = False  # the CLI exits 2: a build asked to deploy did not
            errors = list(final.get("errors") or [])
            if FORCED_DRY_RUN_ERROR not in errors:
                final["errors"] = [FORCED_DRY_RUN_ERROR, *errors]
        if own_trace:
            trace.close()
    return final


def _safe_describe(llm: Any) -> dict[str, Any]:
    try:
        return {k: v for k, v in llm.describe().items() if isinstance(v, str | int | float | bool | list)}
    except Exception:
        return {}


def _adopt_deploy_snapshot(ctx: BuilderContext, known: dict[str, Any], slot: str) -> None:
    """After a cut-off: take what the Freya client says its (possibly unfinished) save created into ``known``.

    The deploy node only reports after it returns, so a save that was in flight when the guard fired is invisible to
    ``ctx.progress``. A workflow created by it is reported (``published`` stays ``False`` unless the client confirmed
    it); a finished deploy newer than the reported one replaces it - and the export of the older version, which no
    longer describes what is live, is dropped.
    """
    snapshot_of = getattr(ctx.freya, "deploy_snapshot", None)
    if not callable(snapshot_of):
        return
    try:
        snapshot = snapshot_of(slot)
    except Exception:  # a diagnostic aid must never cost the run its summary
        logger.debug("could not read the deploy snapshot", exc_info=True)
        return
    if not snapshot:
        return
    current = known.get("deploy") or {}
    complete = bool(snapshot.get("complete"))
    newer = complete and (
        snapshot.get("workflow_id") != current.get("workflow_id")
        or snapshot.get("version") != current.get("version")
    )
    if not current or newer:
        known["deploy"] = {
            **{key: value for key, value in snapshot.items() if key != "complete"},
            "track": known.get("track") or "compiled",
        }
        known["exported"] = None  # an export that exists belongs to an older version
        if not complete:
            known["errors"] = [
                *known.get("errors", []),
                "deploy: the build was cut off while the workflow was being saved; it exists on the platform "
                "but its newest version is not confirmed published",
            ]
    elif not complete and ctx.active_node == "deploy":
        known["errors"] = [
            *known.get("errors", []),
            "deploy: an update was in flight when the build was cut off; the platform may hold a version "
            "newer than the one reported",
        ]


def _finish(
    ctx: BuilderContext | None,
    state: dict[str, Any] | None,
    trace: TraceWriter,
    inputs: InputBundle,
    observer: Any,
    *,
    cut_off: bool = False,
    crash: str = "",
) -> dict[str, Any]:
    """Build the final summary from the graph's last state (or what the nodes recorded) and close the observer.

    ``crash`` is the message of an exception that ended the graph run (for example LangGraph's recursion limit): the
    summary lists it, so a ``failed`` build never has an empty ``errors`` list.
    """
    final: dict[str, Any] | None = (state or {}).get("final")
    try:
        if final is None and ctx is not None:
            known = {"slot": inputs.slot, "run_id": trace.run_id, **(ctx.progress or {}), **(state or {})}
            if crash:
                known["errors"] = [*(known.get("errors") or []), crash]
            note: str | None = None
            if cut_off:
                active = ctx.active_node
                note = "the build was cut off by the wall-clock guard" + (
                    f" while running {active}" if active else ""
                )
                _adopt_deploy_snapshot(ctx, known, inputs.slot)
            final = finalize_run(known, ctx, cut_off=note)
        elif final is None:
            summary = trace.summary({"status": "failed", "slot": inputs.slot, "track": ""})
            final = {
                "slot": inputs.slot,
                "track": "",
                "workflow_id": None,
                "agent_id": None,
                "version": None,
                "exported": None,
                "trace": str(trace.log_path or ""),
                "status": "failed",
                "errors": [f"build failed before the graph started (run {summary.get('run_id')})"],
            }
    except Exception:
        logger.exception("could not finalise the build")
        final = {
            "slot": inputs.slot,
            "status": "failed",
            "trace": str(trace.log_path or ""),
            "errors": ["finalise failed"],
        }
    if observer is not None:
        try:
            observer.end_run(
                str(final.get("status")), {k: final.get(k) for k in ("workflow_id", "agent_id", "version")}
            )
            observer.flush()
        except Exception:
            logger.debug("observer shutdown failed", exc_info=True)
    return final


def _fallback_settings() -> Settings:
    """The settings for a failure summary: the real ones, or the defaults when the environment is invalid."""
    try:
        return get_settings()
    except Exception:  # the very error being reported must not stop its own report
        return Settings.model_construct()


def _describe_error(exc: Exception) -> str:
    """One line for an error of the inputs or of the configuration (field names and reasons, never the values)."""
    if isinstance(exc, ValidationError):
        fields = "; ".join(
            f"{'.'.join(str(part) for part in err['loc']) or 'settings'}: {err['msg']}" for err in exc.errors()
        )
        return f"invalid configuration (environment / .env): {fields}"
    return str(exc)


def _failure_without_run(message: str, settings: Settings, slot: str = "inputs") -> dict[str, Any]:
    """The summary for inputs that cannot even be classified: a trace is still written (when the folder allows it)."""
    log = ""
    try:
        paths.ensure_dirs()
        trace = TraceWriter(uuid.uuid4().hex[:12], slot, secrets=secret_values(settings), echo=sys.stderr)
        try:
            trace.error(message)
            trace.summary({"status": "failed", "slot": slot, "track": ""})
            log = str(trace.log_path or "")
        finally:
            trace.close()
    except Exception as exc:  # an unwritable trace folder must not cost the summary line
        message = f"{message} (no trace written: {type(exc).__name__})"
    return {
        "slot": slot,
        "track": "",
        "workflow_id": None,
        "agent_id": None,
        "version": None,
        "exported": None,
        "trace": log,
        "status": "failed",
        "errors": [message],
    }


def _summary_line(final: dict[str, Any], settings: Settings | None) -> str:
    """The final JSON line, with every known secret and key-shaped token masked (error texts may quote a request)."""
    line = json.dumps(final, ensure_ascii=True, default=str)
    try:
        return Redactor(secret_values(settings or _fallback_settings())).text(line)
    except Exception:  # redaction must not cost the summary line; the key-shaped patterns still apply
        logger.debug("could not collect the secrets for the summary line", exc_info=True)
        return Redactor().text(line)


def _crash_summary(slot: str, message: str) -> dict[str, Any]:
    """The summary of a run that could not even start: the usual keys, no trace, the reason in ``errors``."""
    return {
        "slot": slot,
        "track": "",
        "workflow_id": None,
        "agent_id": None,
        "version": None,
        "exported": None,
        "trace": "",
        "status": "failed",
        "errors": [message[:400]],
    }


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point; returns the exit code (0 deployed or dry-run ok, 2 otherwise).

    Never raises (usage errors of ``argparse`` aside): an invalid ``.env`` value, an unreadable input, an unwritable
    ``traces/`` folder or a bug still end with the JSON summary line as the last line of stdout and exit code 2.
    """
    _configure_stdio()
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    log_redactor = Redactor()
    log_handler = install_log_redaction(log_redactor)
    try:
        return _main(args, log_redactor)
    finally:
        if log_handler is not None:
            logging.getLogger().removeHandler(log_handler)


class _RedactingFormatter(logging.Formatter):
    """The plain ``%(message)s`` format of Python's last-resort handler, with secrets masked (tracebacks included)."""

    def __init__(self, redactor: Redactor) -> None:
        super().__init__("%(message)s")
        self._redactor = redactor

    def format(self, record: logging.LogRecord) -> str:
        return self._redactor.text(super().format(record))


def install_log_redaction(redactor: Redactor, root: logging.Logger | None = None) -> logging.Handler | None:
    """Route warnings and errors of Python logging to stderr through ``redactor`` (audit A11).

    Without a handler, Python prints them unmasked through its last-resort handler; an error text can quote a request
    that holds a key. Nothing is installed when logging is already configured (a host application, pytest).
    """
    target = root or logging.getLogger()
    if target.handlers:
        return None
    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(logging.WARNING)
    handler.setFormatter(_RedactingFormatter(redactor))
    target.addHandler(handler)
    return handler


def _main(args: argparse.Namespace, log_redactor: Redactor) -> int:
    settings: Settings | None = None
    slot = _safe_explicit_slot(args.slot)
    try:
        try:
            settings = settings_from_args(args)
            try:
                log_redactor.add(secret_values(settings))
            except Exception:  # the key-shaped patterns still apply
                logger.debug("could not collect the secrets for log redaction", exc_info=True)
            inputs = classify_inputs(args.inputs, args.slot, settings=settings)
        except (ValueError, OSError) as exc:  # InputError, an invalid environment / .env value, an unreadable file
            message = _describe_error(exc)
            print(log_redactor.text(f"error: {message}"), file=sys.stderr)
            final = _failure_without_run(message, settings or _fallback_settings(), slot)
        else:
            slot = inputs.slot
            try:
                final = asyncio.run(run_build(settings, inputs, fake_llm_dir=args.fake_llm, echo=sys.stderr))
            except KeyboardInterrupt:
                final = {"slot": inputs.slot, "status": "failed", "errors": ["interrupted"]}
            except Exception as exc:  # run_build reports its own errors; this is an error before its trace exists
                logger.exception("the build could not start")
                final = _crash_summary(
                    inputs.slot, f"the build could not start: {type(exc).__name__}: {exc}"
                )
    except Exception as exc:  # last resort: the summary line is printed whatever went wrong
        logger.exception("unexpected error before the build")
        print(log_redactor.text(f"error: {type(exc).__name__}: {exc}"), file=sys.stderr)
        final = _crash_summary(slot, f"unexpected error: {type(exc).__name__}: {exc}")
    print(_summary_line(final, settings), flush=True)
    return exit_code(final)


def exit_code(final: dict[str, Any]) -> int:
    """0 for a deployed agent or a dry run that was asked for; 2 for anything else (a forced dry run included)."""
    if final.get("status") not in SUCCESS_STATUSES:
        return 2
    return 2 if final.get("status") == "dry_run" and final.get("dry_run_requested") is False else 0


def _safe_explicit_slot(value: str | None) -> str:
    """The ``--slot`` value for a failure that happens before the inputs are classified, when it is a safe label."""
    if not value or not value.strip():
        return "inputs"
    try:
        from builder.inputs import _check_explicit_slot

        return _check_explicit_slot(value.strip(), "--slot")
    except (InputError, ImportError):
        return "inputs"


if __name__ == "__main__":
    sys.exit(main())
