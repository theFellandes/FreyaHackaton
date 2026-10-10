"""Node ``extract_policy``: turn the rule ledger into the structured policy IR.

Calls :func:`builder.understand.extract.extract_policy` (the model fills the IR from the ledger and the tool contracts;
mechanical defects are fixed on the way out). The IR is stored as ``state["ir"]``; the validation happens in the next
node, not here.
"""

from __future__ import annotations

from typing import Any

from langgraph.runtime import Runtime

from builder.graph.context import BuilderContext
from builder.graph.nodes._common import failure, get_bundle, get_ledger, get_tool_api, run_node, short
from builder.graph.state import BuilderState
from builder.llm.errors import AllProvidersFailed
from builder.understand import extract_policy

NAME = "extract_policy"


async def _body(state: BuilderState, ctx: BuilderContext) -> dict[str, Any]:
    ledger = get_ledger(state)
    if ledger is None:
        return failure(NAME, "there is no rule ledger to extract a policy from")
    try:
        ir = await extract_policy(
            ctx.llm, get_bundle(state), get_tool_api(state), ledger, ctx.trace, deadline=ctx.budget.deadline()
        )
    except AllProvidersFailed as exc:
        ctx.trace.error("could not extract the policy IR", error=short(exc))
        return failure(NAME, short(exc))
    return {"ir": ir.to_json_dict(), "policy_repairs": 0}


async def run(state: BuilderState, runtime: Runtime[BuilderContext]) -> dict[str, Any]:
    """LangGraph entry point of the node."""
    return await run_node(NAME, state, runtime.context, _body)
