"""Node ``self_test``: run the self-tests of the deployed agent that the mode and the remaining time allow.

``settings.self_test`` is ``auto`` (smoke chat, then the walkthrough audit, then the practice cases when the inputs are the
kit's briefs - each only if its pessimistic time estimate still fits), ``none``, or the name of one test. The tests
(:mod:`builder.selftest`) never raise; their results are appended to ``state["self_tests"]`` (``name``, ``passed``,
``summary``, ``details``, ``round``) and written to the trace as ``TEST`` lines, which is the self-test record the
hackathon asks for. Each test is announced to the trace before it starts (``trace.test_started``), so a build that
the wall-clock guard cuts off during a test still lists that test, marked ``aborted``.

A repair round re-deploys the workflow; if no test could run on the new version (not enough time left) the node says
so (``untested_redeploy``) instead of leaving the earlier round's results to stand in for it.
"""

from __future__ import annotations

from typing import Any

from langgraph.runtime import Runtime

from builder.graph.context import BuilderContext
from builder.graph.nodes._common import get_bundle, get_ir, get_ledger, run_node, workflow_of
from builder.graph.state import BuilderState
from builder.selftest import plan_self_tests
from builder.selftest.practice import run_practice_selftest
from builder.selftest.smoke import run_smoke
from builder.selftest.walkthrough import run_walkthrough

NAME = "self_test"


async def _run_one(name: str, state: BuilderState, ctx: BuilderContext) -> dict[str, Any]:
    deploy = state.get("deploy") or {}
    ir, ledger = (get_ir(state) if state.get("track") != "agentic" else None), get_ledger(state)
    if name == "smoke":
        return await run_smoke(
            ctx.freya, deploy["workflow_id"], ir, ctx.trace, ctx.clock(), cancel=ctx.cancel
        )
    if name == "walkthrough":
        workflow = workflow_of(state) or {}
        rule_map = (state.get("compiled") or {}).get("rule_map") or {}
        variables = get_ir(state).call_variable_names() if state.get("ir") else []
        assert ledger is not None  # plan_self_tests only schedules the walkthrough when a ledger exists
        return await run_walkthrough(
            ctx.llm, ledger, workflow, rule_map, variables, ctx.trace, deadline=ctx.budget.deadline()
        )
    return await run_practice_selftest(
        ctx.freya,
        str(deploy.get("agent_id") or ""),
        get_bundle(state),
        ctx.budget,
        ctx.trace,
        ctx.practice_runner,
        cancel=ctx.cancel,
    )


async def _body(state: BuilderState, ctx: BuilderContext) -> dict[str, Any]:
    mode = str((state.get("limits") or {}).get("self_test") or ctx.settings.self_test)
    round_no = int(state.get("selftest_repairs", 0))
    deploy = state.get("deploy") or {}
    results: list[dict[str, Any]] = []
    done: set[str] = set()
    while True:
        remaining = [
            n
            for n in plan_self_tests(
                mode,
                ctx.budget,
                has_ledger=state.get("ledger") is not None,
                has_agent=bool(deploy.get("workflow_id")),
            )
            if n not in done
        ]
        if not remaining:
            break
        name = remaining[0]
        done.add(name)
        ctx.trace.test_started(name)
        try:
            result = await _run_one(name, state, ctx)
        except Exception as exc:  # a test must never break the build
            result = {
                "name": name,
                "passed": False,
                "summary": f"the test crashed: {type(exc).__name__}: {exc}"[:300],
                "details": {"crashed": True},
            }
        result["round"] = round_no
        skipped = bool(result.get("details", {}).get("skipped"))
        if skipped:
            ctx.trace.test_skipped(name, result["summary"])
        else:
            ctx.trace.test(name, bool(result["passed"]), result["summary"])
        results.append(result)
    if not results:
        ctx.trace.decision(f"no self-test ran (mode {mode}, {ctx.budget.remaining():.0f} s left)")
        update: dict[str, Any] = {"self_tests": [], "_skipped": True}
        if round_no > 0:  # a repair round re-deployed the workflow and nothing tested the new version
            ctx.trace.decision(f"the re-deployed version (round {round_no}) was not self-tested")
            update["untested_redeploy"] = True
        return update
    return {"self_tests": results}


async def run(state: BuilderState, runtime: Runtime[BuilderContext]) -> dict[str, Any]:
    """LangGraph entry point of the node."""
    return await run_node(NAME, state, runtime.context, _body)
