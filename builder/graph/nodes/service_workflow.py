"""Generic service planner -> deterministic compiler, without collections PolicyIR."""

from __future__ import annotations

import json
from typing import Any

from langgraph.runtime import Runtime

from builder.compiler.profiles.service import SERVICE_PLANNER_SYSTEM, ServicePlan, compile_service_workflow
from builder.graph.context import BuilderContext
from builder.graph.nodes._common import failure, get_bundle, get_tool_api, run_node, short, workflow_file, write_json
from builder.graph.state import BuilderState
from builder.understand._common import call_structured

NAME = "service_workflow"


async def _body(state: BuilderState, ctx: BuilderContext) -> dict[str, Any]:
    attempt = int(state.get("service_attempts", 0)) + 1
    base: dict[str, Any] = {
        "service_attempts": attempt,
        "compile_repairs": max(0, attempt - 1),
        "profile": "service",
        "track": "compiled",
        "blocked": False,
        "lint": {},
        "remote_validation": {},
    }
    bundle, api = get_bundle(state), get_tool_api(state)
    feedback = str(state.get("compile_feedback") or "")
    user = ("ORIGINAL BANK DOCUMENTS (complete, unabridged):\n" + bundle.combined_text()
            + "\n\nACTUAL TOOL API CONTRACTS:\n" + json.dumps(api.model_dump(mode="json"), ensure_ascii=False)
            + ("\n\nREPAIR FEEDBACK: Correct the plan using the same original sources.\n" + feedback if feedback else ""))
    try:
        plan = await call_structured(
            ctx.llm,
            purpose="plan_service_workflow",
            schema=ServicePlan,
            system=SERVICE_PLANNER_SYSTEM,
            user=user,
            deadline=ctx.budget.deadline(),
        )
        compiled = compile_service_workflow(plan, bundle, api)
    except Exception as exc:
        ctx.trace.error("service planning/compile failed", attempt=attempt, error=short(exc, 1000))
        return failure(NAME, short(exc), **base, compiled={}, compile_feedback=short(exc, 1500))
    plan_data = plan.model_dump(mode="json")
    write_json(workflow_file(state["slot"], ".service-plan.json"), {
        "bundle_sha256": bundle.sha256,
        "tool_api_sha256": api.sha256,
        "attempt": attempt,
        "plan": plan_data,
    })
    ctx.trace.decision(
        "compiled source-grounded service workflow",
        attempt=attempt,
        stages=len(plan.stages),
        rules=len(plan.rules),
        call_variables=[var.name for var in plan.call_variables],
        bundle_sha256=bundle.sha256,
        tool_api_sha256=api.sha256,
    )
    return {**base, "compiled": compiled, "service_plan": plan_data, "compile_feedback": "",
            "coverage": {"summary": f"{len(plan.rules)} source-grounded service rules in system prompt"}}


async def run(state: BuilderState, runtime: Runtime[BuilderContext]) -> dict[str, Any]:
    return await run_node(NAME, state, runtime.context, _body)
