"""Node ``repair_policy``: fix the policy IR using the issues in ``state["policy_issues"]``.

The issues come from the validator, from the policy guard, or (after a deployment) from the self-tests - whichever sent the
run here. :func:`builder.understand.repair.repair_policy` first applies the model-free safe fixes and only asks the model
for what remains. A failing model call - or any other exception of the repair - keeps the old IR (the validator will
report the same issues again and the repair budget runs out). The counter ``policy_repairs`` bounds the loop, so it is
advanced on every path out of the node: a repair that crashed without advancing it would send the run
``validate_policy -> repair_policy`` until LangGraph's recursion limit.
"""

from __future__ import annotations

import logging
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

logger = logging.getLogger(__name__)

#: Issue-code prefixes written by the ``decide`` node from self-test findings.
SELF_TEST_CODE_PREFIXES = ("WALKTHROUGH_", "PRACTICE_")


async def _body(state: BuilderState, ctx: BuilderContext) -> dict[str, Any]:
    attempt = int(state.get("policy_repairs", 0)) + 1
    update: dict[str, Any] = {"policy_repairs": attempt}
    try:
        return await _repair(state, ctx, attempt, update)
    except AllProvidersFailed as exc:
        ctx.trace.error("policy repair failed; keeping the previous IR", error=short(exc))
        update["errors"] = [f"{NAME}: {short(exc)}"]
    except Exception as exc:  # a crash must still use up a repair round, or the loop never ends
        logger.exception("policy repair crashed")
        ctx.trace.error(
            f"policy repair crashed ({type(exc).__name__}); keeping the previous IR", error=short(exc)
        )
        update["errors"] = [f"{NAME}: {type(exc).__name__}: {short(exc)}"]
    return update


async def _repair(
    state: BuilderState, ctx: BuilderContext, attempt: int, update: dict[str, Any]
) -> dict[str, Any]:
    ir, ledger = get_ir(state), get_ledger(state)
    if ir is None or ledger is None:
        return failure(NAME, "there is no policy IR to repair", policy_repairs=attempt)
    issues = get_policy_issues(state)
    ctx.trace.decision(
        f"policy repair round {attempt}: {len(issues)} issue(s)", codes=sorted({i.code for i in issues})
    )
    # Findings of the self-tests are not validator errors: with the tool API the repair step would re-validate, see no
    # error and return without asking the model. Without it the issues given here go to the model as they are (the
    # validate_policy node checks the result right afterwards).
    from_self_test = any(issue.code.startswith(SELF_TEST_CODE_PREFIXES) for issue in issues)
    repaired = await repair_policy(
        ctx.llm,
        ir,
        issues,
        get_bundle(state),
        ledger,
        ctx.trace,
        tool_api=None if from_self_test else get_tool_api(state),
        # optional (a failure keeps the IR it had): it must leave compile, validation and deploy their time (AUD-53)
        deadline=ctx.budget.deadline_before("compile", "remote_validate", "deploy"),
        attempt=attempt,
    )
    update["ir"] = repaired.to_json_dict()
    return update


async def run(state: BuilderState, runtime: Runtime[BuilderContext]) -> dict[str, Any]:
    """LangGraph entry point of the node."""
    return await run_node(NAME, state, runtime.context, _body)
