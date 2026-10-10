"""Node ``finalize``: write the closing files, the trace summary and the one-line result of the build.

:func:`finalize_run` does the work and is also called by ``build.py`` when the graph itself cannot finish (the wall-clock
guard fired or something outside the nodes failed), so the trace summary is written exactly once on every path
(``ctx.finalized``). It

* decides the final ``status``: ``deployed`` (an agent exists on the platform, its newest version is published and
  its platform export was written), ``deployed_unpublished`` (it exists but the newest version could not be confirmed
  as published - exit code 2), ``deployed_unexported`` (it is live but no verified platform export of it exists -
  exit code 2, the exported file is part of the submission), ``dry_run`` (a lint-clean, guard-clean workflow was written locally), ``blocked`` (a guard stopped the build) or
  ``failed``;
* writes ``out/<slot>.workflow.json`` (the definition that was sent) when the workflow passed the checks - otherwise
  the rejected definition goes to ``out/<slot>.rejected.workflow.json`` for diagnosis - plus ``out/<slot>.ledger.json``
  and ``out/<slot>.ir.json`` so the understanding step can be inspected;
* writes the hackathon trace summary (:meth:`builder.trace.TraceWriter.summary`: start, end, model calls, Freya
  objects, tests and results, track, slot). The tests are those of the state merged with the ones the trace recorded
  itself (plus tests that started but never finished, marked ``aborted``), so a build cut off by the wall-clock guard
  still lists every test that ran;
* writes ``results/<slot>-<run id>.build.json`` with the complete outcome (errors, notes, tests).

The returned ``final`` dict is what ``build.py`` prints as the last line: ``slot``, ``track``, ``workflow_id``,
``agent_id``, ``version``, ``exported``, ``trace``, ``status`` (plus ``workflow_file``, ``tests`` and ``errors``).
``exported`` is the platform export of the deployed version, never the locally sent definition: when the deployed
workflow has no usable export it is ``None``, ``export_ok`` is ``False`` and ``errors`` says so. Only a dry run (no
platform at all) reports its local workflow file there. ``cut_off`` is ``True`` when the wall-clock guard ended the
build, ``untested_redeploy`` when a repair round re-deployed a version that no self-test ran on.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from langgraph.runtime import Runtime

from builder import paths
from builder.graph.context import BuilderContext
from builder.graph.nodes._common import run_node, workflow_file, workflow_of, write_json, write_workflow_file
from builder.graph.state import BuilderState

logger = logging.getLogger(__name__)

NAME = "finalize"

#: Checks the state keeps only the latest result of (they re-run after every repair); the trace's earlier runs of
#: them are not added to the summary again.
LATEST_ONLY_TESTS: tuple[str, ...] = ("static_checks", "remote_validate")

#: Statuses with which ``build.py`` exits 0.
SUCCESS_STATUSES = frozenset({"deployed", "dry_run"})


def decide_status(state: Mapping[str, Any]) -> str:
    """The final status of a run (see the module docstring)."""
    deploy = state.get("deploy") or {}
    if deploy.get("workflow_id"):
        # ``published`` is False only when the platform did not confirm it; a record without the key is hand-built
        if deploy.get("published") is False:
            return "deployed_unpublished"
        # the deploy node sets ``exported`` to None when the export failed; a state without the key is unknown
        if "exported" in state and not state.get("exported"):
            return "deployed_unexported"
        return "deployed"
    if state.get("blocked") or state.get("status") == "blocked":
        return "blocked"
    lint = state.get("lint") or {}
    if (
        (state.get("dry_run") or (state.get("limits") or {}).get("dry_run"))
        and lint.get("ok")
        and workflow_of(dict(state))
    ):
        return "dry_run"
    return "failed"


def collect_tests(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The tests of the run for the trace summary: static checks, platform validation and the self-tests."""
    tests: list[dict[str, Any]] = []
    lint = state.get("lint") or {}
    if lint:
        tests.append(
            {"name": "static_checks", "passed": bool(lint.get("ok")), "summary": lint.get("summary", "")}
        )
    remote = state.get("remote_validation") or {}
    if remote.get("kind") in ("ok", "deployed", "invalid"):
        tests.append(
            {
                "name": "remote_validate",
                "passed": bool(remote.get("ok")),
                "summary": "; ".join(remote.get("problems") or []) or "the platform accepted the workflow",
            }
        )
    for test in state.get("self_tests") or []:
        if (test.get("details") or {}).get("skipped"):
            continue
        suffix = f"#{test['round'] + 1}" if test.get("round") else ""
        tests.append(
            {
                "name": f"{test['name']}{suffix}",
                "passed": bool(test["passed"]),
                "summary": test.get("summary", ""),
            }
        )
    return tests


def finalize_run(
    state: Mapping[str, Any], ctx: BuilderContext, *, cut_off: str | None = None
) -> dict[str, Any]:
    """Write the closing files and the trace summary (once) and return the ``final`` summary dict.

    Args:
        state: The last known state (the graph's, or a partial one built from ``ctx.progress``).
        ctx: The run context; ``ctx.finalized`` makes the call idempotent.
        cut_off: Set by ``build.py`` when the wall-clock guard ended the build: a sentence saying so (and which node
            was interrupted). It shows in the final JSON (``cut_off: true``), the trace summary and ``errors``.
    """
    status = decide_status(state)
    slot = str(state.get("slot") or ctx.trace.slot)
    deploy = state.get("deploy") or {}
    workflow = workflow_of(dict(state))
    local: str | None = state.get("workflow_path") or None
    if workflow is not None and not local:
        try:
            passed_checks = bool((state.get("lint") or {}).get("ok")) and not state.get("blocked")
            suffix = ".workflow.json" if passed_checks else ".rejected.workflow.json"
            local = str(write_workflow_file(slot, workflow, suffix))
        except OSError as exc:
            logger.warning("could not write the workflow file: %s", exc)
    for key, suffix in (("ledger", ".ledger.json"), ("ir", ".ir.json")):
        if state.get(key):
            try:
                write_json(workflow_file(slot, suffix), state[key])
            except OSError as exc:
                logger.warning("could not write %s: %s", suffix, exc)
    tests = collect_tests(state)
    # the trace knows tests the state does not (a cut-off build) and those that were started but never finished
    merge = getattr(ctx.trace, "merged_tests", None)
    if callable(merge):
        tests = merge(tests, latest_only=LATEST_ONLY_TESTS)
    track = state.get("track") or ctx.progress.get("track") or ""
    errors = list(state.get("errors") or [])
    exported: str | None
    if deploy.get("workflow_id"):
        exported = state.get("exported") or None  # the platform export of the deployed version, or nothing
        if not exported and not any("export" in str(error) for error in errors):
            errors.append("export: the deployed workflow has no platform export file")
    elif status == "dry_run":
        exported = local  # there is no platform in a dry run: the local file is all there is
    else:
        exported = state.get("exported") or None
    export_ok: bool | None = bool(exported) if deploy.get("workflow_id") else None
    if cut_off:
        errors.append(cut_off)
    final: dict[str, Any] = {
        "slot": slot,
        "track": track,
        "workflow_id": deploy.get("workflow_id"),
        "agent_id": deploy.get("agent_id"),
        "version": deploy.get("version"),
        "exported": exported,
        "export_ok": export_ok,
        "trace": str(ctx.trace.log_path) if getattr(ctx.trace, "log_path", None) else "",
        "status": status,
        "workflow_file": local,
        "tests": {"passed": sum(1 for t in tests if t["passed"]), "total": len(tests)},
        "errors": errors[:10],
    }
    if cut_off:
        final["cut_off"] = True
    if state.get("untested_redeploy"):
        final["untested_redeploy"] = True
    if ctx.finalized:
        return final
    ctx.finalized = True
    try:
        summary: dict[str, Any] = {
            "track": track or None,
            "status": status,
            "slot": slot,
            "workflow_id": deploy.get("workflow_id"),
            "agent_id": deploy.get("agent_id"),
            "version": deploy.get("version"),
            "node_count": deploy.get("node_count"),
            "tool_count": deploy.get("tool_count"),
            "exported": final["exported"],
            "tests": tests,
            "profile": state.get("profile"),
            "coverage": (state.get("coverage") or {}).get("summary"),
            "notes": list(state.get("notes") or [])[:20],
            "errors_list": errors[:10],
        }
        # shown only when they say something (the block prints every extra key)
        for key, value in (
            ("export_ok", export_ok),
            ("cut_off", cut_off),
            ("untested_redeploy", True if state.get("untested_redeploy") else None),
        ):
            if value is not None:
                summary[key] = value
        ctx.trace.summary(summary)
    except Exception:  # the summary must never take the run down
        logger.exception("could not write the trace summary")
    try:
        write_json(
            paths.RESULTS_DIR / f"{slot}-{state.get('run_id', ctx.trace.run_id)}.build.json",
            {**final, "notes": list(state.get("notes") or []), "tests_detail": state.get("self_tests") or []},
        )
    except OSError as exc:
        logger.warning("could not write the build result file: %s", exc)
    return final


async def _body(state: BuilderState, ctx: BuilderContext) -> dict[str, Any]:
    final = finalize_run(state, ctx)
    return {"final": final, "status": final["status"]}


async def run(state: BuilderState, runtime: Runtime[BuilderContext]) -> dict[str, Any]:
    """LangGraph entry point of the node."""
    return await run_node(NAME, state, runtime.context, _body)
