"""Node ``guard_inputs``: check the briefs and the tool API before any model reads them.

Runs :func:`builder.guardrails.input_guard.check_inputs` (size limits, control characters, prompt-injection heuristics,
tool-API sanity). Findings are *warnings*: brief text stays data and is never obeyed. A ``block`` (for example an input
of several megabytes) ends the run with ``status = "blocked"``.
"""

from __future__ import annotations

from typing import Any

from langgraph.runtime import Runtime

from builder.graph.context import BuilderContext
from builder.graph.nodes._common import get_bundle, get_tool_api, report_guard, run_node
from builder.graph.state import BuilderState
from builder.guardrails import check_inputs

NAME = "guard_inputs"


async def _body(state: BuilderState, ctx: BuilderContext) -> dict[str, Any]:
    report = check_inputs(get_bundle(state), get_tool_api(state))
    dump = report_guard(ctx, report)
    update: dict[str, Any] = {"guard_reports": [dump], "blocked": report.blocked}
    if report.blocked:
        update.update(
            failed=NAME,
            status="blocked",
            errors=[f"{NAME}: " + "; ".join(report.blocking_messages())[:400]],
        )
    return update


async def run(state: BuilderState, runtime: Runtime[BuilderContext]) -> dict[str, Any]:
    """LangGraph entry point of the node."""
    return await run_node(NAME, state, runtime.context, _body)
