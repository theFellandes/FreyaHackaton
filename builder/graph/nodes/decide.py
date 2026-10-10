"""Node ``decide``: read the self-test results and decide whether another build round is worth its time.

Findings that can be fixed in the policy become policy issues for ``repair_policy``:

* a walkthrough finding that a rule is ``missing`` or ``contradicted`` (the rule id and the stage are known);
* a failed practice case (its failed checks are the feedback).

A failed smoke test alone is reported but does not start a repair (nothing in the policy explains it). A repair round
is requested only on the compiled track, within ``max_selftest_repairs`` and when the budget still covers a whole
round (repair, prose, compile, validate, deploy and a smoke test) plus the usual reserve. Otherwise the build ends with
what is deployed, and the failing tests stay in the trace. The agent that is already published is never removed.

Only the results of the *current* round count. After a repair round the workflow was re-deployed; if no self-test ran
on that new version the verdict says so (and ``untested_redeploy`` is set) instead of reporting "all self-tests
passed" because the round before had nothing left to fail. Tests that were skipped did not run and are not evidence
either.
"""

from __future__ import annotations

from typing import Any

from langgraph.runtime import Runtime

from builder.budget import STAGE_ESTIMATES_S
from builder.graph.context import BuilderContext
from builder.graph.nodes._common import run_node, short
from builder.graph.state import BuilderState
from builder.understand.validate import PolicyIssue

NAME = "decide"

#: Stages one more round would run again.
ROUND_STAGES: tuple[str, ...] = ("repair", "prose", "compile", "remote_validate", "deploy", "smoke")
#: Seconds that must remain after a repair round.
RESERVE_S = 120.0


def round_cost_s() -> float:
    """Pessimistic duration of one repair round in seconds."""
    return sum(STAGE_ESTIMATES_S[stage] for stage in ROUND_STAGES)


def issues_from_tests(tests: list[dict[str, Any]]) -> list[PolicyIssue]:
    """Policy issues derived from failed walkthrough findings and failed practice cases."""
    issues: list[PolicyIssue] = []
    for test in tests:
        if test.get("passed"):
            continue
        details = test.get("details") or {}
        if test.get("name") == "walkthrough":
            for item in details.get("issues") or []:
                if item.get("verdict") in ("missing", "contradicted"):
                    issues.append(
                        PolicyIssue(
                            code=f"WALKTHROUGH_{str(item['verdict']).upper()}",
                            message=f"{item.get('explanation', '')} (stage: {item.get('where', '?')})".strip(),
                            rule_ids=[str(item.get("rule_id", ""))],
                            severity="error",
                        )
                    )
        elif test.get("name") == "practice":
            for case in details.get("failed_cases") or []:
                reasons = "; ".join(case.get("failed") or []) or "see the practice results"
                issues.append(
                    PolicyIssue(
                        code="PRACTICE_FAILED",
                        message=f"practice case {case.get('id')} ({case.get('family', '')}) failed: {reasons}",
                        severity="error",
                    )
                )
    return issues


async def _body(state: BuilderState, ctx: BuilderContext) -> dict[str, Any]:
    round_no = int(state.get("selftest_repairs", 0))
    limits = state.get("limits") or {}
    tests = [t for t in state.get("self_tests") or [] if t.get("round", 0) == round_no]
    ran = [t for t in tests if not (t.get("details") or {}).get("skipped")]
    failing = [t for t in ran if not t.get("passed")]
    issues = issues_from_tests(failing)
    can_repair = (
        bool(issues)
        and state.get("track") != "agentic"
        and not state.get("dry_run")
        and round_no < int(limits.get("max_selftest_repairs", 0))
        and ctx.budget.allows(round_cost_s(), RESERVE_S)
    )
    untested_redeploy = round_no > 0 and not ran
    if failing:
        verdict = "failing: " + ", ".join(f"{t['name']} ({short(t.get('summary', ''), 80)})" for t in failing)
    elif ran:
        verdict = "all self-tests passed"
    elif round_no > 0:
        verdict = f"no self-test ran for the redeployed version (round {round_no}); it is untested"
    else:
        verdict = "no self-test ran (all were skipped)"
    ctx.trace.decision(
        f"self-test verdict round {round_no}: {verdict}; "
        + (
            f"requesting policy repair round {round_no + 1} for {len(issues)} issue(s)"
            if can_repair
            else "finishing"
        ),
        issues=len(issues),
        budget_left_s=round(ctx.budget.remaining()),
        round_cost_s=round(round_cost_s()),
    )
    if not can_repair:
        update: dict[str, Any] = {"repair_requested": False, "notes": [f"decide: {verdict}"]}
        if untested_redeploy:
            update["untested_redeploy"] = True
        return update
    return {
        "repair_requested": True,
        "selftest_repairs": round_no + 1,
        "policy_issues": [issue.model_dump(mode="json") for issue in issues],
        "policy_repairs": 0,
        "compile_repairs": 0,
        "compile_repair_log": [],
        "remote_validation": {},
        "lint": {},
        "blocked": False,
        "notes": [f"decide: {verdict}; repairing the policy (round {round_no + 1})"],
    }


async def run(state: BuilderState, runtime: Runtime[BuilderContext]) -> dict[str, Any]:
    """LangGraph entry point of the node."""
    return await run_node(NAME, state, runtime.context, _body)
