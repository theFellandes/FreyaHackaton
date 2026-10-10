"""Node ``guard_workflow``: the last gate before anything reaches the platform.

:func:`builder.guardrails.check_workflow` blocks a workflow whose tool call goes to a host that is not the bank tool API's
(the phone agent may call only the bank), that contains a secret or a key-like token anywhere, or whose fixed spoken
lines before identity verification contain a word the IR forbids. A blocked workflow is never deployed: the run goes
to ``repair_compile`` while repairs are left, else it ends ``blocked``. It also runs in dry runs.
"""

from __future__ import annotations

from typing import Any

from langgraph.runtime import Runtime

from builder.graph.context import BuilderContext
from builder.graph.nodes._common import failure, get_ir, get_tool_api, report_guard, run_node, workflow_of
from builder.graph.state import BuilderState
from builder.guardrails import check_workflow

NAME = "guard_workflow"


async def _body(state: BuilderState, ctx: BuilderContext) -> dict[str, Any]:
    workflow = workflow_of(state)
    if workflow is None:
        return failure(NAME, "there is no workflow to check")
    ir = None if state.get("track") == "agentic" else get_ir(state)
    report = check_workflow(workflow, get_tool_api(state), ctx.secrets, ir)
    update: dict[str, Any] = {"guard_reports": [report_guard(ctx, report)], "blocked": report.blocked}
    if report.blocked:
        update["compile_feedback"] = "\n".join(report.blocking_messages()[:25])
        update["notes"] = [f"{NAME}: {len(report.blocking)} blocking violation(s)"]
    return update


async def run(state: BuilderState, runtime: Runtime[BuilderContext]) -> dict[str, Any]:
    """LangGraph entry point of the node."""
    return await run_node(NAME, state, runtime.context, _body)
