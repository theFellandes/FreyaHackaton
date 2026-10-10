"""Node ``draft_prose``: let a (cheap) model draft the short task messages of the conversational nodes.

:func:`builder.understand.prose.draft_prose` writes one concise text per node from the IR and the ledger. The result
passes the prose guard (:func:`builder.guardrails.check_prose`: no invented amounts or dates, no unknown tools); on a
block, a model failure, or when the budget no longer allows the stage, the template-only fallback is used
(:func:`builder.compiler.prose.default_prose`) - the compiler's templates already carry every IR rule, so the prose
only polishes. This node does not fail because of the model.
"""

from __future__ import annotations

from typing import Any

from langgraph.runtime import Runtime

from builder.compiler.prose import default_prose
from builder.graph.context import BuilderContext
from builder.graph.nodes._common import (
    failure,
    get_ir,
    get_ledger,
    get_tool_api,
    report_guard,
    run_node,
    short,
)
from builder.graph.state import BuilderState
from builder.guardrails import check_prose
from builder.llm.errors import AllProvidersFailed
from builder.understand import draft_prose

NAME = "draft_prose"


async def _body(state: BuilderState, ctx: BuilderContext) -> dict[str, Any]:
    ir, ledger = get_ir(state), get_ledger(state)
    if ir is None or ledger is None:
        return failure(NAME, "there is no policy IR to draft prose for")
    fallback = default_prose(ir, ledger).model_dump(mode="json")
    if not ctx.budget.allows_stage("prose"):
        ctx.trace.decision("not enough time for the prose draft: using the template-only prompts")
        return {"prose": fallback, "notes": [f"{NAME}: skipped (budget)"]}
    profile = state.get("profile", ctx.settings.agent_profile)
    try:
        prose = await draft_prose(ctx.llm, ir, ledger, profile, ctx.trace, deadline=ctx.budget.deadline())
    except AllProvidersFailed as exc:
        ctx.trace.error("prose draft failed; using the template-only prompts", error=short(exc))
        return {"prose": fallback, "notes": [f"{NAME}: model failed, template-only prompts used"]}
    report = check_prose(prose, ir, tool_api=get_tool_api(state), ledger=ledger)
    update: dict[str, Any] = {"guard_reports": [report_guard(ctx, report)]}
    if report.blocked:
        update["prose"] = fallback
        update["notes"] = [f"{NAME}: prose blocked by the guard, template-only prompts used"]
    else:
        update["prose"] = prose.model_dump(mode="json")
    return update


async def run(state: BuilderState, runtime: Runtime[BuilderContext]) -> dict[str, Any]:
    """LangGraph entry point of the node."""
    return await run_node(NAME, state, runtime.context, _body)
