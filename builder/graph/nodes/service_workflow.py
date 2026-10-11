"""Generic service planner -> deterministic compiler, without collections PolicyIR."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from langgraph.runtime import Runtime

from builder.compiler.profiles.service import (
    SERVICE_PLANNER_SYSTEM,
    ServicePlan,
    compile_service_workflow,
    validate_service_plan,
)
from builder.graph.context import BuilderContext
from builder.graph.nodes._common import (
    failure,
    get_bundle,
    get_tool_api,
    run_node,
    short,
    workflow_file,
    write_json,
)
from builder.graph.nodes.service_review import (
    SERVICE_POLICY_REVIEW_SYSTEM,
    ServicePolicyReview,
    require_complete_service_policy,
)
from builder.graph.state import BuilderState
from builder.guardrails import check_inputs
from builder.ingest.brief_loader import BriefBundle, load_brief
from builder.understand._common import call_structured

NAME = "service_workflow"


def source_bundle(state: BuilderState) -> BriefBundle:
    """Include original contract documents as well as policy, retaining exact text and hashes.

    Collection ingestion keeps contracts separate for extraction. Generic service
    policy can also depend on their envelopes, errors and response semantics, so
    the planner must read those originals rather than only a derived API summary.
    """
    bundle = get_bundle(state)
    documents = list(bundle.documents)
    seen = {doc.path.resolve() for doc in documents}
    inputs = state.get("inputs") or {}
    for raw_path in [*(inputs.get("contracts") or []), *(inputs.get("reference") or [])]:
        path = Path(raw_path).resolve()
        if path in seen:
            continue
        documents.append(load_brief(path).model_copy(update={"label": "original service contract reference"}))
        seen.add(path)
    return BriefBundle.from_documents(documents)


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
    try:
        bundle, api = source_bundle(state), get_tool_api(state)
        report = check_inputs(bundle, api)
        if report.blocked:
            return failure(NAME, "; ".join(report.blocking_messages()), **{**base, "blocked": True},
                           compiled={}, compile_feedback="original service sources failed input guard")
        ctx.trace.event("input", "service planner reads complete original policy and contract documents",
                        documents=bundle.names, bundle_sha256=bundle.sha256,
                        chars=sum(len(doc.text) for doc in bundle.documents))
        feedback = str(state.get("compile_feedback") or "")
        source_context = ("ORIGINAL BANK DOCUMENTS (complete, unabridged):\n" + bundle.combined_text()
                          + "\n\nACTUAL TOOL API CONTRACTS:\n"
                          + json.dumps(api.model_dump(mode="json"), ensure_ascii=False))
        user = (source_context
                + ("\n\nREPAIR FEEDBACK: Correct the plan using the same original sources.\n" + feedback if feedback else ""))
        plan = await call_structured(
            ctx.llm,
            purpose="plan_service_workflow",
            schema=ServicePlan,
            system=SERVICE_PLANNER_SYSTEM,
            user=user,
            deadline=ctx.budget.deadline(),
        )
        # Reject inexpensive deterministic errors before spending a policy-review call.
        validate_service_plan(plan, bundle, api)
        review = await call_structured(
            ctx.llm,
            purpose="review_service_policy",
            schema=ServicePolicyReview,
            system=SERVICE_POLICY_REVIEW_SYSTEM,
            user=source_context + "\n\nCANDIDATE SERVICE PLAN:\n" + plan.model_dump_json(),
            max_tokens=8192,
            deadline=ctx.budget.deadline(),
        )
        ctx.trace.decision("source-policy review of service plan", attempt=attempt, issues=len(review.issues),
                           bundle_sha256=bundle.sha256, tool_api_sha256=api.sha256)
        require_complete_service_policy(review, bundle)
        compiled = compile_service_workflow(plan, bundle, api)
    except Exception as exc:
        ctx.trace.error("service planning/compile failed", attempt=attempt, error=short(exc, 1000))
        return failure(NAME, short(exc), **base, compiled={}, compile_feedback=short(exc, 12000))
    plan_data = plan.model_dump(mode="json")
    write_json(workflow_file(state["slot"], ".service-plan.json"), {
        "bundle_sha256": bundle.sha256,
        "tool_api_sha256": api.sha256,
        "attempt": attempt,
        "plan": plan_data,
        "policy_review": review.model_dump(mode="json"),
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
    return {**base, "compiled": compiled, "service_plan": plan_data, "bundle": bundle.model_dump(mode="json"),
            "compile_feedback": "",
            "coverage": {"summary": f"{len(plan.rules)} source-grounded service rules in system prompt"}}


async def run(state: BuilderState, runtime: Runtime[BuilderContext]) -> dict[str, Any]:
    return await run_node(NAME, state, runtime.context, _body)
