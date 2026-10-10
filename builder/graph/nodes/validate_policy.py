"""Node ``validate_policy``: check the policy IR against the tool API and the ledger (no model call).

:func:`builder.understand.validate.validate_policy` produces the issues the next routing step reads (``error`` issues
trigger a repair round). Two informational checks are added to the trace without influencing the route: fixed sentences
of the IR that cannot be found in the briefs (:func:`~builder.understand.grounding.check_phrase_grounding`) and the offline
invariant sampler (:func:`builder.robustness.run_robustness`).
"""

from __future__ import annotations

from typing import Any

from langgraph.runtime import Runtime

from builder.graph.context import BuilderContext
from builder.graph.nodes._common import failure, get_bundle, get_ir, get_ledger, get_tool_api, run_node, short
from builder.graph.state import BuilderState
from builder.robustness import run_robustness
from builder.understand import check_phrase_grounding, errors_of, format_issues, validate_policy

NAME = "validate_policy"


async def _body(state: BuilderState, ctx: BuilderContext) -> dict[str, Any]:
    ir, ledger = get_ir(state), get_ledger(state)
    if ir is None or ledger is None:
        return failure(NAME, "there is no policy IR to validate")
    api = get_tool_api(state)
    issues = validate_policy(ir, ledger, api)
    errors = errors_of(issues)
    ctx.trace.event(
        "validate",
        f"policy IR: {len(errors)} error(s), {len(issues) - len(errors)} warning(s)",
        codes=sorted({issue.code for issue in issues}),
    )
    if issues:
        ctx.trace.event("validate", "issues:\n" + format_issues(issues)[:3000])
    try:
        ungrounded = check_phrase_grounding(ir, get_bundle(state))
        if ungrounded:
            ctx.trace.event(
                "validate",
                f"{len(ungrounded)} fixed sentence(s) not found in the briefs",
                sentences=ungrounded[:10],
            )
    except Exception as exc:  # informational only
        ctx.trace.event("validate", f"phrase grounding skipped: {short(exc)}")
    try:
        robustness = run_robustness(ir, api)
        ctx.trace.event(
            "validate",
            f"offline invariants: {robustness['passed']} passed, {robustness['failed']} failed, "
            f"{robustness['unchecked']} unchecked",
            failing=[c["name"] for c in robustness["checks"] if c["status"] == "fail"],
        )
    except Exception as exc:  # informational only
        ctx.trace.event("validate", f"offline invariants skipped: {short(exc)}")
    return {"policy_issues": [issue.model_dump(mode="json") for issue in issues]}


async def run(state: BuilderState, runtime: Runtime[BuilderContext]) -> dict[str, Any]:
    """LangGraph entry point of the node."""
    return await run_node(NAME, state, runtime.context, _body)
