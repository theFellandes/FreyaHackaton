"""The routing functions are pure: each case below is a state in, a node name out.

Covered: the repair caps, the budget gates (the time left is a number in the state), the agentic fallback and its limits,
guard blocks, dry runs, and the rule that a deployed agent is never replaced by a fallback.
"""

from __future__ import annotations

from typing import Any

import pytest

from builder.graph import routing as r

LIMITS: dict[str, Any] = {
    "max_policy_repairs": 2,
    "max_compile_repairs": 2,
    "max_selftest_repairs": 1,
    "allow_agentic": True,
    "dry_run": False,
}
PLENTY = 1500.0  # seconds left: covers the agentic estimate (1100) plus the reserve
LITTLE = 200.0


def state(**kwargs: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"limits": dict(LIMITS), "budget_remaining_s": PLENTY, "track": "compiled"}
    base.update(kwargs)
    return base


def error_issue() -> dict[str, Any]:
    return {"code": "X", "message": "m", "severity": "error"}


def warning_issue() -> dict[str, Any]:
    return {"code": "X", "message": "m", "severity": "warning"}


def test_inputs_and_guard_inputs() -> None:
    assert r.after_load_inputs(state()) == "guard_inputs"
    assert r.after_load_inputs(state(failed="load_inputs")) == "finalize"
    assert r.after_guard_inputs(state()) == "understand_brief"
    assert r.after_guard_inputs(state(track="agentic")) == "agentic_build"
    assert r.after_guard_inputs(state(blocked=True)) == "finalize"
    assert r.after_guard_inputs(state(failed="guard_inputs")) == "finalize"


@pytest.mark.parametrize("router", [r.after_understand_brief, r.after_extract_policy])
def test_a_failed_understanding_step_falls_back_to_the_agentic_track(router: Any) -> None:
    assert router(state(failed="x")) == "agentic_build"
    assert router(state(failed="x", limits={**LIMITS, "allow_agentic": False})) == "finalize"
    assert router(state(failed="x", budget_remaining_s=LITTLE)) == "finalize"  # a session no longer fits
    assert router(state(failed="x", agentic_attempts=2)) == "finalize"


def test_understanding_goes_on_when_nothing_failed_and_a_blocked_ledger_stops() -> None:
    assert r.after_understand_brief(state()) == "extract_policy"
    assert r.after_understand_brief(state(blocked=True, failed="understand_brief")) == "finalize"
    assert r.after_extract_policy(state()) == "validate_policy"


def test_validate_policy_repairs_errors_within_the_cap() -> None:
    assert r.after_validate_policy(state(policy_issues=[])) == "guard_policy"
    assert r.after_validate_policy(state(policy_issues=[warning_issue()])) == "guard_policy"
    assert r.after_validate_policy(state(policy_issues=[error_issue()], policy_repairs=0)) == "repair_policy"
    assert r.after_validate_policy(state(policy_issues=[error_issue()], policy_repairs=1)) == "repair_policy"


def test_validate_policy_after_the_cap_falls_back_or_goes_on_best_effort() -> None:
    capped = state(policy_issues=[error_issue()], policy_repairs=2)
    assert r.after_validate_policy(capped) == "agentic_build"
    no_agentic = state(
        policy_issues=[error_issue()], policy_repairs=2, limits={**LIMITS, "allow_agentic": False}
    )
    assert (
        r.after_validate_policy(no_agentic) == "guard_policy"
    )  # best effort: the guards still stand in the way
    no_time = state(policy_issues=[error_issue()], policy_repairs=0, budget_remaining_s=LITTLE)
    assert r.after_validate_policy(no_time) == "guard_policy"  # neither a repair round nor a session fits


def test_guard_policy_block_repairs_or_ends_blocked() -> None:
    assert r.after_guard_policy(state(blocked=False)) == "draft_prose"
    assert r.after_guard_policy(state(blocked=True, policy_repairs=0)) == "repair_policy"
    assert r.after_guard_policy(state(blocked=True, policy_repairs=2)) == "finalize"


def test_compile_failure_is_repaired_then_falls_back() -> None:
    assert r.after_draft_prose(state()) == "compile_workflow"
    assert r.after_compile_workflow(state()) == "static_checks"
    assert r.after_compile_workflow(state(failed="compile_workflow", compile_repairs=0)) == "repair_compile"
    assert r.after_compile_workflow(state(failed="compile_workflow", compile_repairs=2)) == "agentic_build"


def test_static_checks_routes() -> None:
    assert r.after_static_checks(state(lint={"ok": True})) == "guard_workflow"
    assert r.after_static_checks(state(lint={"ok": False}, compile_repairs=0)) == "repair_compile"
    assert r.after_static_checks(state(lint={"ok": False}, compile_repairs=2)) == "agentic_build"
    exhausted = state(lint={"ok": False}, compile_repairs=2, limits={**LIMITS, "allow_agentic": False})
    assert r.after_static_checks(exhausted) == "finalize"


def test_agentic_workflows_skip_the_compile_repair() -> None:
    failing = state(lint={"ok": False}, track="agentic", agentic_attempts=1, compile_repairs=0)
    assert (
        r.after_static_checks(failing) == "agentic_build"
    )  # one more attempt, with the findings as feedback
    assert r.after_static_checks({**failing, "agentic_attempts": 2}) == "finalize"
    assert r.after_static_checks({**failing, "budget_remaining_s": LITTLE}) == "finalize"
    assert r.after_static_checks(state(lint={"ok": True}, track="agentic")) == "guard_workflow"


def test_repair_compile_without_a_strategy_falls_back() -> None:
    assert r.after_repair_compile(state()) == "compile_workflow"
    assert r.after_repair_compile(state(failed="repair_compile")) == "agentic_build"


def test_guard_workflow_routes() -> None:
    assert r.after_guard_workflow(state()) == "remote_validate"
    assert r.after_guard_workflow(state(dry_run=True)) == "finalize"
    assert r.after_guard_workflow(state(limits={**LIMITS, "dry_run": True})) == "finalize"
    assert r.after_guard_workflow(state(blocked=True, compile_repairs=0)) == "repair_compile"
    assert (
        r.after_guard_workflow(state(blocked=True, compile_repairs=2)) == "finalize"
    )  # never to the agentic track
    assert r.after_guard_workflow(state(blocked=True, track="agentic")) == "finalize"


def test_remote_validate_routes() -> None:
    assert r.after_remote_validate(state(remote_validation={"ok": True, "kind": "ok"})) == "deploy"
    assert r.after_remote_validate(state(remote_validation={"ok": False, "kind": "infra"})) == "deploy"
    invalid = {"ok": False, "kind": "invalid"}
    assert r.after_remote_validate(state(remote_validation=invalid, compile_repairs=1)) == "repair_compile"
    assert r.after_remote_validate(state(remote_validation=invalid, compile_repairs=2)) == "agentic_build"
    deployed = state(remote_validation=invalid, compile_repairs=2, deploy={"workflow_id": "wf"})
    assert r.after_remote_validate(deployed) == "finalize"


def test_deploy_routes() -> None:
    ok = {"ok": True, "kind": "deployed"}
    assert r.after_deploy(state(remote_validation=ok, deploy={"workflow_id": "wf"})) == "self_test"
    assert r.after_deploy(state(failed="deploy")) == "finalize"
    rejected = {"ok": False, "kind": "invalid"}
    assert r.after_deploy(state(remote_validation=rejected, compile_repairs=0)) == "repair_compile"
    assert r.after_deploy(state(remote_validation=rejected, deploy={"workflow_id": "wf"})) == "finalize"
    # a repair round that was not deployed (no time to test it) must not be self-tested again: the old version is live
    skipped = state(remote_validation=ok, deploy={"workflow_id": "wf"}, redeploy_skipped=True)
    assert r.after_deploy(skipped) == "finalize"


def test_self_test_and_decide_routes() -> None:
    assert r.after_self_test(state(self_tests=[{"name": "smoke", "passed": True}])) == "decide"
    assert r.after_self_test(state(self_tests=[])) == "finalize"
    assert r.after_self_test(state(self_tests=[{"name": "smoke"}], failed="self_test")) == "finalize"
    # only the results of the CURRENT round count: round 1 ran nothing, so the round-0 failure is no verdict on it
    older = [{"name": "practice", "passed": False, "round": 0}]
    assert r.after_self_test(state(self_tests=older, selftest_repairs=1)) == "finalize"
    assert (
        r.after_self_test(
            state(self_tests=[*older, {"name": "smoke", "passed": True, "round": 1}], selftest_repairs=1)
        )
        == "decide"
    )
    assert r.after_decide(state(repair_requested=True, selftest_repairs=1)) == "repair_policy"
    assert r.after_decide(state(repair_requested=False)) == "finalize"
    assert r.after_decide(state(repair_requested=True, selftest_repairs=2)) == "finalize"  # above the cap


def test_agentic_build_routes() -> None:
    assert r.after_agentic_build(state(agentic_attempts=1)) == "static_checks"
    assert r.after_agentic_build(state(failed="agentic_build", agentic_attempts=1)) == "agentic_build"
    assert r.after_agentic_build(state(failed="agentic_build", agentic_attempts=2)) == "finalize"


def test_grounded_service_policy_repairs_have_their_own_bounded_budget() -> None:
    pending = state(service_mode=True, failed="service_workflow", compile_repairs=2,
                    service_policy_repair_pending=True, service_policy_repairs=0)
    assert r.after_compile_workflow(pending) == "service_workflow"
    assert r.after_compile_workflow({**pending, "service_policy_repairs": 1}) == "service_workflow"
    assert r.after_compile_workflow({**pending, "service_policy_repairs": 2}) == "finalize"
    assert r.after_compile_workflow({**pending, "service_policy_repair_pending": False}) == "finalize"
    assert r.after_compile_workflow({**pending, "budget_remaining_s": 300}) == "finalize"
    assert r.after_compile_workflow({**pending, "deploy": {"workflow_id": "wf"}}) == "finalize"


def test_fallback_never_replaces_a_deployed_agent_or_a_blocked_build() -> None:
    assert r.fallback_target(state(deploy={"workflow_id": "wf"})) == "finalize"
    assert r.fallback_target(state(blocked=True)) == "finalize"
    assert r.fallback_target(state()) == "agentic_build"


def test_budget_helpers() -> None:
    assert r.affordable({}, "agentic_build")  # unknown time: do not block
    assert r.affordable({"budget_remaining_s": 1300}, "agentic_build")
    assert not r.affordable({"budget_remaining_s": 1100}, "agentic_build")  # estimate 1100 + reserve 120
    assert r.has_policy_errors(
        {"policy_issues": [{"code": "A", "message": "m"}]}
    )  # severity defaults to error
    assert not r.has_policy_errors({"policy_issues": [warning_issue()]})
    assert r.is_deployed({"deploy": {"workflow_id": "wf"}}) and not r.is_deployed({"deploy": {}})


def test_limits_default_when_the_state_has_none() -> None:
    bare = {"policy_issues": [error_issue()], "policy_repairs": 1}
    assert r.after_validate_policy(bare) == "repair_policy"
    assert r.after_validate_policy({**bare, "policy_repairs": 2}) == "guard_policy"
