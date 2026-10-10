"""Node ``guard_policy``: the output guard for the policy IR (runs after the validator accepted it).

:func:`builder.guardrails.check_policy` blocks values that appear nowhere in the briefs or the tool API (an invented date,
amount or number), IR enum values outside the tool API's enums, and injection markers. A block turns the violations
into policy issues and sends the run back to ``repair_policy`` while repairs are left; otherwise the run ends
``blocked`` - an unvetted policy is never compiled.
"""

from __future__ import annotations

from typing import Any

from langgraph.runtime import Runtime

from builder.graph.context import BuilderContext
from builder.graph.nodes._common import (
    failure,
    get_bundle,
    get_ir,
    get_tool_api,
    guard_issues,
    report_guard,
    run_node,
)
from builder.graph.state import BuilderState
from builder.guardrails import check_policy

NAME = "guard_policy"


async def _body(state: BuilderState, ctx: BuilderContext) -> dict[str, Any]:
    ir = get_ir(state)
    if ir is None:
        return failure(NAME, "there is no policy IR to check")
    report = check_policy(ir, get_bundle(state), get_tool_api(state))
    update: dict[str, Any] = {"guard_reports": [report_guard(ctx, report)], "blocked": report.blocked}
    if report.blocked:
        update["policy_issues"] = guard_issues(report)
        update["notes"] = [f"{NAME}: {len(report.blocking)} blocking violation(s)"]
    return update


async def run(state: BuilderState, runtime: Runtime[BuilderContext]) -> dict[str, Any]:
    """LangGraph entry point of the node."""
    return await run_node(NAME, state, runtime.context, _body)
