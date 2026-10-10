"""Node ``compile_workflow``: policy IR + tool API + prose -> the Freya workflow (deterministic, no model call).

Wraps :func:`builder.compiler.compile.compile_workflow` with the profile in ``state["profile"]`` (``staged`` by default;
``repair_compile`` may switch it) and the prose from ``draft_prose`` (empty = template-only prompts). The rule coverage
(:func:`builder.checks.coverage.coverage_report`: which ledger rule is enforced where) is written to the trace and
kept in ``state["coverage"]``. A :class:`~builder.compiler.errors.CompileError` (for example a tool the policy needs that
the tool API lacks) marks the node as failed.
"""

from __future__ import annotations

from typing import Any

from langgraph.runtime import Runtime

from builder.checks.coverage import coverage_report
from builder.compiler import compile_workflow
from builder.compiler.errors import CompileError
from builder.compiler.prose import NodeProse
from builder.graph.context import BuilderContext
from builder.graph.nodes._common import failure, get_ir, get_ledger, get_tool_api, run_node, short
from builder.graph.state import BuilderState

NAME = "compile_workflow"


async def _body(state: BuilderState, ctx: BuilderContext) -> dict[str, Any]:
    ir, ledger = get_ir(state), get_ledger(state)
    if ir is None or ledger is None:
        return failure(NAME, "there is no policy IR to compile")
    prose_data = state.get("prose") or {}
    prose = NodeProse.model_validate(prose_data) if prose_data else None
    profile = state.get("profile") or ctx.settings.agent_profile
    try:
        compiled = compile_workflow(ir, get_tool_api(state), ledger, prose, profile)  # type: ignore[arg-type]
    except CompileError as exc:
        ctx.trace.error("compile failed", profile=profile, error=short(exc))
        return failure(NAME, short(exc), compile_feedback=f"compile error ({profile}): {short(exc, 500)}")
    coverage = coverage_report(ledger, compiled.rule_map)
    ctx.trace.decision(
        f"compiled {compiled.profile} workflow: {compiled.node_count} nodes; rule coverage: {coverage.summary()}",
        notes=compiled.notes[:10],
    )
    return {
        "compiled": compiled.model_dump(mode="json"),
        "coverage": {
            "covered": len(coverage.covered),
            "uncovered": list(coverage.uncovered),
            "superseded": len(coverage.superseded),
            "ratio": round(coverage.ratio, 3),
            "summary": coverage.summary(),
        },
        "track": "compiled",
        "profile": compiled.profile,
        "notes": [f"compiler: {note}" for note in compiled.notes[:10]],
    }


async def run(state: BuilderState, runtime: Runtime[BuilderContext]) -> dict[str, Any]:
    """LangGraph entry point of the node."""
    return await run_node(NAME, state, runtime.context, _body)
