"""Node ``load_inputs``: read the brief files and the tool API, and decide which track the run starts on.

Reads ``state["inputs"]`` (``{"briefs": [...], "tool_api": "..."}`` from :func:`builder.inputs.classify_inputs`), converts
the briefs to text (:func:`builder.ingest.brief_loader.load_briefs`) and parses the tool API
(:func:`builder.ingest.tool_api.load_tool_api`). It also copies the caps the routing needs into ``state["limits"]``
(so :mod:`builder.graph.routing` stays a set of pure functions) and chooses the starting track:

* ``builder_track = "agentic"`` starts on the agentic track when the Claude Agent SDK and a credential are available
  (otherwise the run continues on the compiled track and says so in ``notes``);
* ``"compiled"`` and ``"auto"`` start compiled; ``"auto"`` may fall back to the agentic track later.

Unreadable inputs end the run (``failed``): there is nothing to build from.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from langgraph.runtime import Runtime

from builder.agentic.runner import agentic_available, agentic_unavailable_reason
from builder.graph.context import BuilderContext
from builder.graph.nodes._common import failure, run_node, short
from builder.graph.state import BuilderState
from builder.ingest.brief_loader import load_briefs
from builder.ingest.tool_api import load_tool_api
from builder.inputs import InputError

NAME = "load_inputs"


def limits_from_settings(ctx: BuilderContext, *, allow_agentic: bool) -> dict[str, Any]:
    """The caps copied into the state for the pure routing functions."""
    s = ctx.settings
    return {
        "max_policy_repairs": int(s.max_policy_repairs),
        "max_compile_repairs": int(s.max_compile_repairs),
        "max_selftest_repairs": int(s.max_selftest_repairs),
        "allow_agentic": allow_agentic,
        "dry_run": bool(s.dry_run),
        "self_test": str(s.self_test),
        "builder_track": str(s.builder_track),
        "budget_total_s": float(ctx.budget.total_s),
    }


async def _body(state: BuilderState, ctx: BuilderContext) -> dict[str, Any]:
    inputs = state.get("inputs") or {}
    try:
        briefs = [Path(p) for p in inputs["briefs"]]
        bundle = load_briefs(briefs)
        tool_api = load_tool_api(inputs["tool_api"])
    except (InputError, OSError, KeyError, ValueError) as exc:
        return failure(NAME, short(exc), status="failed")

    settings = ctx.settings
    wants_agentic = settings.builder_track == "agentic" or (
        settings.builder_track == "auto" and not settings.dry_run
    )
    can_agentic = wants_agentic and (ctx.agentic_runner is not None or agentic_available(settings))
    notes: list[str] = []
    if wants_agentic and not can_agentic:
        # A doomed session would burn minutes before it fails: say in the trace why the agentic track is skipped.
        ctx.trace.decision(
            f"agentic track skipped: {agentic_unavailable_reason(settings) or 'not available'}",
            requested=settings.builder_track,
        )
    if settings.builder_track == "agentic" and not can_agentic:
        notes.append(
            "track 'agentic' requested but the Claude Agent SDK or a credential is not available: using compiled"
        )
    track = "agentic" if settings.builder_track == "agentic" and can_agentic else "compiled"
    ctx.trace.decision(
        f"track {track} (requested {settings.builder_track}; agentic fallback {'on' if can_agentic else 'off'})",
        profile=settings.agent_profile,
        dry_run=settings.dry_run,
    )
    ctx.trace.event(
        "input",
        f"{len(bundle.documents)} brief file(s), tool API with {len(tool_api.tools)} tool(s)",
        briefs=[doc.name for doc in bundle.documents],
        chars=sum(len(doc.text) for doc in bundle.documents),
        brief_sha=bundle.sha256[:12],
        tools=sorted(tool_api.tools),
    )
    return {
        "bundle": bundle.model_dump(mode="json"),
        "tool_api": tool_api.model_dump(mode="json"),
        "track": track,
        "profile": settings.agent_profile,
        "dry_run": bool(settings.dry_run),
        "limits": limits_from_settings(ctx, allow_agentic=can_agentic),
        "notes": notes,
    }


async def run(state: BuilderState, runtime: Runtime[BuilderContext]) -> dict[str, Any]:
    """LangGraph entry point of the node."""
    return await run_node(NAME, state, runtime.context, _body)
