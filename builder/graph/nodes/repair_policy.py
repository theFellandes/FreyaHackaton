"""Node ``repair_policy``: fix the policy IR using the issues in ``state["policy_issues"]``.

The issues come from the validator, from the policy guard, or (after a deployment) from the self-tests - whichever sent the
run here. :func:`builder.understand.repair.repair_policy` first applies the model-free safe fixes and only asks the model
for what remains. A failing model call keeps the old IR (the validator will report the same issues again and the
repair budget runs out). The counter ``policy_repairs`` bounds the loop.
"""

from __future__ import annotations

from typing import Any

from langgraph.runtime import Runtime

from builder.graph.context import BuilderContext
from builder.graph.nodes._common import (
    failure,
    get_bundle,
    get_ir,
    get_ledger,
    get_policy_issues,
    get_tool_api,
    run_node,
    short,
)
from builder.graph.state import BuilderState
from builder.llm.errors import AllProvidersFailed
from builder.understand import repair_policy

NAME = "repair_policy"

#: Issue-code prefixes written by the ``decide`` node from self-test findings.
SELF_TEST_CODE_PREFIXES = ("WALKTHROUGH_", "PRACTICE_")


async def _body(state: BuilderState, ctx: BuilderContext) -> dict[str, Any]:
    ir, ledger = get_ir(state), get_ledger(state)
    if ir is None or ledger is None:
        return failure(NAME, "there is no policy IR to repair")
    attempt = int(state.get("policy_repairs", 0)) + 1
    issues = get_policy_issues(state)
    ctx.trace.decision(
        f"policy repair round {attempt}: {len(issues)} issue(s)", codes=sorted({i.code for i in issues})
    )
    update: dict[str, Any] = {"policy_repairs": attempt}
    # Findings of the self-tests are not validator errors: with the tool API the repair step would re-validate, see no
    # error and return without asking the model. Without it the issues given here go to the model as they are (the
    # validate_policy node checks the result right afterwards).
    from_self_test = any(issue.code.startswith(SELF_TEST_CODE_PREFIXES) for issue in issues)
    try:
        repaired = await repair_policy(
            ctx.llm,
            ir,
            issues,
            get_bundle(state),
            ledger,
            ctx.trace,
            tool_api=None if from_self_test else get_tool_api(state),
            deadline=ctx.budget.deadline(),
            attempt=attempt,
        )
    except AllProvidersFailed as exc:
        ctx.trace.error("policy repair failed; keeping the previous IR", error=short(exc))
        update["errors"] = [f"{NAME}: {short(exc)}"]
        return update
    update["ir"] = repaired.to_json_dict()
    return update


async def run(state: BuilderState, runtime: Runtime[BuilderContext]) -> dict[str, Any]:
    """LangGraph entry point of the node."""
    return await run_node(NAME, state, runtime.context, _body)
