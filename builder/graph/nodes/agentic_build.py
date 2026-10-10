"""Node ``agentic_build``: the fallback track - a Claude Agent SDK session writes the workflow file.

Used when the user asked for ``--track agentic`` or when the compiled pipeline could not produce a deployable workflow and
the budget still covers a session (:data:`builder.budget.STAGE_ESTIMATES_S` ``["agentic_build"]``). The runner
(:func:`builder.agentic.runner.run_agentic_build`, replaceable through ``ctx.agentic_runner``) tries every Anthropic key and
then the Claude Code login, and writes ``out/<slot>.agentic.workflow.json``. The workflow it returns goes through the
same gates as a compiled one (``static_checks``, ``guard_workflow``, ``remote_validate``, ``deploy``, ``self_test``);
on a second attempt the findings of the checks are passed back to the session as feedback.
"""

from __future__ import annotations

from typing import Any

from langgraph.runtime import Runtime

from builder.agentic.runner import run_agentic_build
from builder.graph.context import BuilderContext
from builder.graph.nodes._common import failure, run_node, short
from builder.graph.state import BuilderState

NAME = "agentic_build"


async def _body(state: BuilderState, ctx: BuilderContext) -> dict[str, Any]:
    attempts = int(state.get("agentic_attempts", 0))
    inputs = state.get("inputs") or {}
    feedback = state.get("compile_feedback", "") if state.get("track") == "agentic" and attempts > 0 else ""
    runner = ctx.agentic_runner or run_agentic_build
    ctx.trace.decision(
        f"agentic build attempt {attempts + 1}"
        + (f" with feedback from the checks ({len(feedback)} chars)" if feedback else ""),
        budget_left_s=round(ctx.budget.remaining()),
    )
    result = await runner(
        bundle_paths=inputs.get("briefs", []),
        tool_api_path=inputs.get("tool_api", ""),
        slot=state["slot"],
        settings=ctx.settings,
        trace=ctx.trace,
        budget=ctx.budget,
        feedback=feedback,
    )
    base: dict[str, Any] = {"agentic_attempts": attempts + 1}
    workflow = result.get("workflow")
    if not result.get("ok") or not isinstance(workflow, dict) or not workflow:
        return failure(NAME, short(result.get("error") or "the session produced no workflow"), **base)
    ctx.trace.decision(
        f"agentic workflow written: {len(workflow.get('nodes', {}))} nodes "
        f"({result.get('turns', 0)} turns, credential {result.get('credential')})"
    )
    return {
        **base,
        "track": "agentic",
        "profile": "agentic",
        "compiled": {"workflow": workflow, "rule_map": {}, "profile": "agentic", "notes": []},
        "lint": {},
        "blocked": False,
        "remote_validation": {},
        "notes": [f"agentic track: workflow written by a Claude session ({result.get('turns', 0)} turns)"],
    }


async def run(state: BuilderState, runtime: Runtime[BuilderContext]) -> dict[str, Any]:
    """LangGraph entry point of the node."""
    return await run_node(NAME, state, runtime.context, _body)
