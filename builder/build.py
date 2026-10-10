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

import argparse
import asyncio
import json
import logging
import time
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

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

logger = logging.getLogger("builder.build")

#: Seconds after the budget at which the wall-clock guard cancels the graph.
GUARD_GRACE_S = 45.0
#: Seconds the guard waits for a platform call that was in flight when it fired (so that a workflow it created can be
#: reported). The guard sits at budget + GUARD_GRACE_S = 28 min 45 s by default; this keeps the end below 30 minutes.
INFLIGHT_GRACE_S = 15.0
TRACKS = ("auto", "compiled", "agentic")
PROFILES = ("staged", "lean")
SELF_TESTS = ("auto", "none", "smoke", "walkthrough", "practice")


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
    try:
        budget = budget or Budget(settings.build_time_budget_s)
        if not settings.dry_run and not settings.freya_api_key and freya is None:
            trace.error("FREYA_API_KEY is not set: nothing can be deployed, running as a dry run")
            settings = settings.model_copy(update={"dry_run": True})
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
        trace.error(f"the build failed: {type(exc).__name__}: {exc}"[:400])
    finally:
        final = _finish(ctx, state, trace, inputs, observer, cut_off=cut_off)
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
) -> dict[str, Any]:
    """Build the final summary from the graph's last state (or what the nodes recorded) and close the observer."""
    final: dict[str, Any] | None = (state or {}).get("final")
    try:
        if final is None and ctx is not None:
            known = {"slot": inputs.slot, "run_id": trace.run_id, **(ctx.progress or {}), **(state or {})}
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


def _failure_without_run(message: str, settings: Settings, slot: str = "inputs") -> dict[str, Any]:
    """The summary for inputs that cannot even be classified: a trace is still written."""
    paths.ensure_dirs()
    trace = TraceWriter(uuid.uuid4().hex[:12], slot, secrets=secret_values(settings), echo=sys.stderr)
    try:
        trace.error(message)
        trace.summary({"status": "failed", "slot": slot, "track": ""})
        log = str(trace.log_path or "")
    finally:
        trace.close()
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


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point; returns the exit code (0 deployed or dry-run ok, 2 otherwise)."""
    _configure_stdio()
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        settings = settings_from_args(args)
        inputs = classify_inputs(args.inputs, args.slot, settings=settings)
    except InputError as exc:
        print(f"error: {exc}", file=sys.stderr)
        final = _failure_without_run(str(exc), get_settings())
    else:
        try:
            final = asyncio.run(run_build(settings, inputs, fake_llm_dir=args.fake_llm, echo=sys.stderr))
        except KeyboardInterrupt:
            final = {"slot": inputs.slot, "status": "failed", "errors": ["interrupted"]}
    print(json.dumps(final, ensure_ascii=True, default=str), flush=True)
    return 0 if final.get("status") in SUCCESS_STATUSES else 2


if __name__ == "__main__":
    sys.exit(main())
