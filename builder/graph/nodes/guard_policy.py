"""Node ``guard_policy``: the output guard for the policy IR (runs after the validator accepted it).

:func:`builder.guardrails.check_policy` blocks values that appear nowhere in the briefs or the tool API (an invented date,
amount or number), IR enum values outside the tool API's enums, and injection markers. A block turns the violations
into policy issues and sends the run back to ``repair_policy`` while repairs are left; otherwise the run ends
``blocked`` - an unvetted policy is never compiled.

One exception (audit AUD-14): when the repairs are used up (or no longer affordable) and *every* blocking finding is a
grounding heuristic (:data:`SOFT_GUARDS`: a date, amount or number the guard could not find in the briefs), the
findings are kept as warnings and the run goes on, best effort. Those heuristics have false positives the repair
cannot clear (a holiday written as a name, a date spelling the guard cannot read; the only "fix" the model has is to
delete a true value), and a blocked build is a certain zero. The decision is in the trace, the notes and the errors.
Secrets, foreign hosts, tool-API enum violations, injection markers and any unknown code still end the run blocked.

This node is also the policy acceptance gate (CRITICAL-GAPS gap 3): every pass records ``policy_unresolved`` - the
validator's issues the run goes on with (errors left after the repairs, warnings) plus the guard's blocking findings -
so the closing summary can list them (``policy_issues``) instead of reporting a clean build. A later pass (after a
repair round) replaces the list.
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
from builder.graph.routing import can_repair_policy
from builder.graph.state import BuilderState
from builder.guardrails import check_policy

NAME = "guard_policy"
#: Blocking findings that may be deployed best effort once the repairs cannot clear them (see the module docstring).
SOFT_GUARDS = frozenset({"ungrounded_date", "ungrounded_amount", "ungrounded_number"})


async def _body(state: BuilderState, ctx: BuilderContext) -> dict[str, Any]:
    ir = get_ir(state)
    if ir is None:
        return failure(NAME, "there is no policy IR to check")
    report = check_policy(ir, get_bundle(state), get_tool_api(state))
    update: dict[str, Any] = {
        "guard_reports": [report_guard(ctx, report)],
        "blocked": report.blocked,
        # the validator's issues (state["policy_issues"] holds them here: validate_policy ran right before) + the guard's
        "policy_unresolved": [*(state.get("policy_issues") or []), *guard_issues(report)],
    }
    if report.blocked and not can_repair_policy(state) and all(v.guard in SOFT_GUARDS for v in report.blocking):
        kinds = ", ".join(sorted({v.guard for v in report.blocking}))
        message = (
            f"{NAME}: {len(report.blocking)} grounding finding(s) ({kinds}) left after "
            f"{int(state.get('policy_repairs') or 0)} policy repair(s); deploying best effort"
        )
        ctx.trace.decision(message, locations=[v.location for v in report.blocking][:10])
        update.update({"blocked": False, "notes": [message], "errors": [message]})
        return update
    if report.blocked:
        update["policy_issues"] = guard_issues(report)
        update["notes"] = [f"{NAME}: {len(report.blocking)} blocking violation(s)"]
    return update


async def run(state: BuilderState, runtime: Runtime[BuilderContext]) -> dict[str, Any]:
    """LangGraph entry point of the node."""
    return await run_node(NAME, state, runtime.context, _body)
