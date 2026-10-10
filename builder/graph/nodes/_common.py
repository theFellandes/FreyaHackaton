"""Plumbing shared by the graph nodes: the run wrapper, state accessors, guard reporting and file writing.

Every node module has the same shape::

    async def _body(state, ctx) -> dict:      # the real work; returns a partial state update
        ...

    async def run(state, runtime):            # what LangGraph calls
        return await run_node(NAME, state, runtime.context, _body)

:func:`run_node` is where the cross-cutting rules live:

* **trace** - ``stage(name, "start")`` before and ``stage(name, "end" | "failed" | "skipped")`` after, with the time left;
* **observability** - the node runs inside an observer span, so every LLM generation is attached to its node;
* **never crash** - an unexpected exception is logged to the trace and turned into ``failed=<node>`` plus an entry in
  ``errors``; the routing functions then pick the fallback (agentic build or finalize). Only ``asyncio.CancelledError``
  (the wall-clock guard) is allowed through;
* **budget** - the seconds left are recorded in ``budget_remaining_s`` so routing can stay pure;
* **progress** - key facts (deploy info, files, the tests that finished, errors and notes) are mirrored into
  ``ctx.progress`` for the wall-clock guard, and ``ctx.active_node`` names the node that is running, so a build that
  is cut off still reports what it had and which node it interrupted.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from builder import paths
from builder.graph.context import BuilderContext
from builder.graph.state import BuilderState
from builder.guardrails.models import GuardReport
from builder.ingest.brief_loader import BriefBundle
from builder.ingest.tool_api import ToolApi
from builder.observability.base import observed_span
from builder.policy.ir import PolicyIR, RuleLedger
from builder.understand.validate import PolicyIssue

logger = logging.getLogger(__name__)

NodeBody = Callable[[BuilderState, BuilderContext], Awaitable[dict[str, Any]]]

#: Call variables every Freya call has (the platform passes ``customer_id`` and ``now``; the generated code reads both).
PLATFORM_CALL_VARIABLES: tuple[str, ...] = ("customer_id", "now")

#: State keys mirrored into ``ctx.progress`` after each node (the latest value wins).
PROGRESS_KEYS: tuple[str, ...] = (
    "status",
    "track",
    "deploy",
    "workflow_path",
    "exported",
    "blocked",
    "failed",
    "profile",
    "lint",
    "coverage",
    "remote_validation",
    "untested_redeploy",
)

#: State keys the graph accumulates (``Annotated[list, add]``): a node returns only its new items, so the progress
#: record appends them instead of overwriting.
ADDITIVE_PROGRESS_KEYS: tuple[str, ...] = ("self_tests", "errors", "notes")

#: Longest error text kept in ``errors`` (full details are in the trace).
MAX_ERROR_CHARS = 400


async def run_node(name: str, state: BuilderState, ctx: BuilderContext, body: NodeBody) -> dict[str, Any]:
    """Run one node body with tracing, observability, the never-crash policy and budget bookkeeping.

    Args:
        name: Node name (also the trace stage and the span name).
        state: The graph state the node was called with.
        ctx: The run context.
        body: ``async (state, ctx) -> partial update``. It may set ``failed`` / ``blocked`` / ``errors`` itself for
            problems it expects; anything it raises is converted here.

    Returns:
        The partial state update, always containing ``failed`` (empty string on success) and ``budget_remaining_s``.
    """
    ctx.trace.stage(name, "start", budget_left_s=round(ctx.budget.remaining(), 1))
    ctx.active_node = name  # stays set when the guard cancels the node: ``build.py`` reads it
    update: dict[str, Any]
    with observed_span(
        ctx.observer, name, {"slot": state.get("slot", ""), "track": state.get("track", "")}
    ) as span:
        try:
            update = dict(await body(state, ctx) or {})
        except asyncio.CancelledError:
            ctx.trace.stage(name, "aborted")
            raise
        except Exception as exc:  # never-crash policy: the routing picks the fallback
            logger.exception("graph node %s crashed", name)
            message = f"{name}: {type(exc).__name__}: {exc}"[:MAX_ERROR_CHARS]
            ctx.trace.error(
                f"node {name} crashed: {type(exc).__name__}", stage=name, error=str(exc)[:MAX_ERROR_CHARS]
            )
            update = {"failed": name, "errors": [message]}
        update.setdefault("failed", "")
        update["budget_remaining_s"] = round(ctx.budget.remaining(), 1)
        failed = bool(update["failed"])
        span.set_output(
            {"failed": update["failed"], "errors": update.get("errors", [])}
            if failed
            else {"keys": sorted(k for k in update if k != "budget_remaining_s")}
        )
    skipped = bool(update.pop("_skipped", False))
    ctx.trace.stage(
        name,
        "failed" if failed else ("skipped" if skipped else "end"),
        budget_left_s=update["budget_remaining_s"],
    )
    for key in PROGRESS_KEYS:
        if key in update:
            ctx.progress[key] = update[key]
    for key in ADDITIVE_PROGRESS_KEYS:
        if update.get(key):
            ctx.progress[key] = [*ctx.progress.get(key, []), *update[key]]
    ctx.active_node = ""
    return update


# ------------------------------------------------------------------------------------------------ state accessors


def get_bundle(state: BuilderState) -> BriefBundle:
    """The loaded briefs of the run (re-validated from the state dump)."""
    return BriefBundle.model_validate(state["bundle"])


def get_tool_api(state: BuilderState) -> ToolApi:
    """The parsed tool API of the run."""
    return ToolApi.model_validate(state["tool_api"])


def get_ledger(state: BuilderState) -> RuleLedger | None:
    """The rule ledger, or ``None`` before it exists (agentic track)."""
    data = state.get("ledger")
    return RuleLedger.model_validate(data) if data else None


def get_ir(state: BuilderState) -> PolicyIR | None:
    """The policy IR, or ``None`` before it exists (agentic track)."""
    data = state.get("ir")
    return PolicyIR.model_validate(data) if data else None


def get_policy_issues(state: BuilderState) -> list[PolicyIssue]:
    """The ``policy_issues`` of the state as models (entries that do not parse are skipped)."""
    issues: list[PolicyIssue] = []
    for raw in state.get("policy_issues") or []:
        try:
            issues.append(PolicyIssue.model_validate(raw))
        except ValueError:
            logger.warning("ignoring a malformed policy issue: %r", raw)
    return issues


def workflow_of(state: BuilderState) -> dict[str, Any] | None:
    """The workflow dict of the current ``compiled`` entry, or ``None``."""
    compiled = state.get("compiled") or {}
    workflow = compiled.get("workflow")
    return workflow if isinstance(workflow, dict) and workflow else None


def lint_call_variables(ir: PolicyIR | None) -> set[str]:
    """Variable names the lint may treat as call variables: the IR's declared ones plus the platform's."""
    names = set(PLATFORM_CALL_VARIABLES)
    if ir is not None:
        names.update(ir.call_variable_names())
    return names


# ------------------------------------------------------------------------------------------------ update helpers


def failure(name: str, message: str, **extra: Any) -> dict[str, Any]:
    """An update that marks ``name`` as failed with a short message (the details belong in the trace)."""
    return {"failed": name, "errors": [f"{name}: {message}"[:MAX_ERROR_CHARS]], **extra}


def short(text: object, limit: int = 240) -> str:
    """One-line, length-limited rendering of ``text`` for traces and error lists."""
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def report_guard(ctx: BuilderContext, report: GuardReport) -> dict[str, Any]:
    """Write a guard report to the trace (``GUARD | <stage> | ok|blocked | n violations``) and return its dump."""
    ctx.trace.event(
        "guard",
        report.trace_message(),
        blocked=report.blocked,
        violations=[short(v, 200) for v in [*report.blocking, *report.warnings][:20]],
    )
    return report.model_dump(mode="json")


def guard_issues(report: GuardReport) -> list[dict[str, Any]]:
    """The blocking violations of a guard report as ``PolicyIssue`` dumps (feedback for the policy repair)."""
    return [
        PolicyIssue(
            code=f"GUARD_{violation.guard.upper()}",
            message=violation.message,
            severity="error",
            path=violation.location,
        ).model_dump(mode="json")
        for violation in report.blocking
    ]


# ------------------------------------------------------------------------------------------------ files


def write_json(path: Path, data: Any) -> Path:
    """Write ``data`` as pretty UTF-8 JSON with LF line ends (parent directories are created)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    return path


def workflow_file(slot: str, suffix: str = ".workflow.json") -> Path:
    """``out/<slot><suffix>`` (``builder.paths.OUT_DIR`` is read at call time so tests can redirect it)."""
    return paths.OUT_DIR / f"{slot}{suffix}"


def write_workflow_file(slot: str, workflow: Mapping[str, Any], suffix: str = ".workflow.json") -> Path:
    """Write the workflow to ``out/<slot>.workflow.json`` (the file the kit's own tools also use)."""
    return write_json(workflow_file(slot, suffix), dict(workflow))


def dedupe(items: Iterable[str]) -> list[str]:
    """Items without duplicates, order kept."""
    return list(dict.fromkeys(items))


__all__ = [
    "ADDITIVE_PROGRESS_KEYS",
    "MAX_ERROR_CHARS",
    "PLATFORM_CALL_VARIABLES",
    "NodeBody",
    "dedupe",
    "failure",
    "get_bundle",
    "get_ir",
    "get_ledger",
    "get_policy_issues",
    "get_tool_api",
    "guard_issues",
    "lint_call_variables",
    "report_guard",
    "run_node",
    "short",
    "workflow_file",
    "workflow_of",
    "write_json",
    "write_workflow_file",
]
