"""Node ``understand_brief``: read every brief file in full and build the rule ledger.

Calls :func:`builder.understand.ledger.build_rule_ledger` (one atomic, quoted rule per entry; amendments and
ambiguities recorded) and then the ledger guard (:func:`builder.guardrails.check_ledger`: no rules, duplicate ids, a
quote found nowhere in the briefs). A model failure (every provider failed) or an empty ledger marks the node as failed so
the routing can fall back; a blocked ledger ends the run as ``blocked``.
"""

from __future__ import annotations

from typing import Any

from langgraph.runtime import Runtime

from builder.graph.context import BuilderContext
from builder.graph.nodes._common import failure, get_bundle, get_tool_api, report_guard, run_node, short
from builder.graph.state import BuilderState
from builder.guardrails import check_ledger
from builder.llm.errors import AllProvidersFailed
from builder.understand import build_rule_ledger
from builder.understand._common import UnderstandError

NAME = "understand_brief"
#: Stages that must still fit after an optional ledger call ends (audit AUD-53).
OPTIONAL_CALLS_LEAVE_TIME_FOR: tuple[str, ...] = ("extract", "compile", "remote_validate", "deploy")


async def _body(state: BuilderState, ctx: BuilderContext) -> dict[str, Any]:
    bundle = get_bundle(state)
    try:
        ledger = await build_rule_ledger(
            ctx.llm,
            bundle,
            get_tool_api(state),
            ctx.trace,
            deadline=ctx.budget.deadline(),  # the rule passages: no ledger, no agent
            # the optional calls (re-quote, completeness, amendment links) must leave extraction and deploy their time
            optional_deadline=ctx.budget.deadline_before(*OPTIONAL_CALLS_LEAVE_TIME_FOR),
        )
    except (AllProvidersFailed, UnderstandError) as exc:
        ctx.trace.error("could not build the rule ledger", error=short(exc))
        return failure(NAME, short(exc))
    report = check_ledger(ledger, bundle)
    update: dict[str, Any] = {
        "ledger": ledger.model_dump(mode="json"),
        "guard_reports": [report_guard(ctx, report)],
        "blocked": report.blocked,
    }
    ctx.trace.decision(
        f"rule ledger: {len(ledger.rules)} rules ({len(ledger.active_rules())} active), "
        f"{len(ledger.ambiguities)} ambiguities, {len(ledger.supersessions)} supersessions"
    )
    if report.blocked:
        update.update(
            failed=NAME, status="blocked", errors=[f"{NAME}: " + "; ".join(report.blocking_messages())[:400]]
        )
    return update


async def run(state: BuilderState, runtime: Runtime[BuilderContext]) -> dict[str, Any]:
    """LangGraph entry point of the node."""
    return await run_node(NAME, state, runtime.context, _body)
