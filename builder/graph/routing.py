"""Routing of the builder graph: pure functions from the state to the name of the next node.

The functions look only at the state (no clock, no I/O, no settings): the caps come from ``state["limits"]`` (copied by
``load_inputs``) and the time left from ``state["budget_remaining_s"]`` (recorded by every node), so they are trivial to
unit-test. Conventions shared by all of them:

* ``state["failed"]`` is the node that could not do its work (empty when the last node was fine). A failure on the
  compiled track goes to :func:`fallback_target`: the agentic track when it is allowed, still has attempts and the budget
  covers a whole session, otherwise ``finalize``;
* ``state["blocked"]`` means a guard stopped the build - never routed to the agentic track, never deployed;
* a deployed agent is never replaced by a fallback: once ``state["deploy"]`` exists, failures finish the run.

The route names are the module-level constants (also used by :mod:`builder.graph.build_graph`).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from builder.budget import DEFAULT_RESERVE_S, estimate

LOAD_INPUTS = "load_inputs"
GUARD_INPUTS = "guard_inputs"
UNDERSTAND_BRIEF = "understand_brief"
EXTRACT_POLICY = "extract_policy"
VALIDATE_POLICY = "validate_policy"
REPAIR_POLICY = "repair_policy"
GUARD_POLICY = "guard_policy"
DRAFT_PROSE = "draft_prose"
COMPILE_WORKFLOW = "compile_workflow"
SERVICE_WORKFLOW = "service_workflow"
STATIC_CHECKS = "static_checks"
REPAIR_COMPILE = "repair_compile"
GUARD_WORKFLOW = "guard_workflow"
REMOTE_VALIDATE = "remote_validate"
DEPLOY = "deploy"
SELF_TEST = "self_test"
DECIDE = "decide"
AGENTIC_BUILD = "agentic_build"
FINALIZE = "finalize"

#: The agentic track may run at most this many times per build (a second run gets the checks' feedback).
MAX_AGENTIC_ATTEMPTS = 2

_DEFAULT_LIMITS: Mapping[str, Any] = {
    "max_policy_repairs": 2,
    "max_compile_repairs": 2,
    "max_selftest_repairs": 1,
    "allow_agentic": False,
    "dry_run": False,
}

State = Mapping[str, Any]


def _limit(state: State, key: str) -> Any:
    return (state.get("limits") or {}).get(key, _DEFAULT_LIMITS[key])


def affordable(state: State, stage: str, reserve_s: float = DEFAULT_RESERVE_S) -> bool:
    """True when the time recorded in the state still covers ``stage`` (pessimistic estimate) plus the reserve."""
    left = state.get("budget_remaining_s")
    return True if left is None else float(left) - reserve_s >= estimate(stage)


def has_policy_errors(state: State) -> bool:
    """True when ``policy_issues`` holds at least one error."""
    return any(
        (issue or {}).get("severity", "error") == "error" for issue in state.get("policy_issues") or []
    )


def is_deployed(state: State) -> bool:
    """True when an agent already exists on the platform for this run."""
    return bool((state.get("deploy") or {}).get("workflow_id"))


def fallback_target(state: State) -> str:
    """Where a failed pipeline goes: ``agentic_build`` when allowed, attempts remain and time suffices, else ``finalize``."""
    if state.get("service_mode") or is_deployed(state) or state.get("blocked") or not _limit(state, "allow_agentic"):
        return FINALIZE
    if int(state.get("agentic_attempts", 0)) >= MAX_AGENTIC_ATTEMPTS or not affordable(
        state, "agentic_build"
    ):
        return FINALIZE
    return AGENTIC_BUILD


def can_repair_policy(state: State) -> bool:
    """True while a policy repair round is left and affordable (``guard_policy`` asks it too)."""
    return int(state.get("policy_repairs", 0)) < _limit(state, "max_policy_repairs") and affordable(
        state, "repair"
    )


_can_repair_policy = can_repair_policy


def _can_repair_compile(state: State) -> bool:
    return state.get("track") != "agentic" and int(state.get("compile_repairs", 0)) < _limit(
        state, "max_compile_repairs"
    )


def _compile_repair_target(state: State) -> str:
    """Replan service inputs; retain the existing deterministic collection repairs."""
    return SERVICE_WORKFLOW if state.get("service_mode") else REPAIR_COMPILE


# ---------------------------------------------------------------------------------------------------- the routers


def after_load_inputs(state: State) -> str:
    """Unreadable inputs end the run; otherwise the inputs are guarded."""
    return FINALIZE if state.get("failed") else GUARD_INPUTS


def after_guard_inputs(state: State) -> str:
    """Blocked inputs end the run; the agentic track starts straight away; otherwise the brief is understood."""
    if state.get("blocked") or state.get("failed"):
        return FINALIZE
    if state.get("service_mode"):
        return SERVICE_WORKFLOW
    return AGENTIC_BUILD if state.get("track") == "agentic" else UNDERSTAND_BRIEF


def after_understand_brief(state: State) -> str:
    """A blocked ledger ends the run, a failed one falls back; otherwise the policy is extracted."""
    if state.get("blocked"):
        return FINALIZE
    return fallback_target(state) if state.get("failed") else EXTRACT_POLICY


def after_extract_policy(state: State) -> str:
    """A failed extraction falls back; otherwise the policy is validated."""
    return fallback_target(state) if state.get("failed") else VALIDATE_POLICY


def after_validate_policy(state: State) -> str:
    """No errors -> guard the policy; errors -> repair while rounds and time remain, else fall back or go on best-effort."""
    if state.get("failed"):
        return fallback_target(state)
    if not has_policy_errors(state):
        return GUARD_POLICY
    if _can_repair_policy(state):
        return REPAIR_POLICY
    target = fallback_target(state)
    return GUARD_POLICY if target == FINALIZE else target


def after_guard_policy(state: State) -> str:
    """A blocked policy goes back to repair while rounds remain, else the run ends blocked; otherwise prose is drafted."""
    if state.get("failed"):
        return fallback_target(state)
    if not state.get("blocked"):
        return DRAFT_PROSE
    return REPAIR_POLICY if _can_repair_policy(state) else FINALIZE


def after_draft_prose(state: State) -> str:
    """Compile (the prose node falls back to template-only prompts itself; only a missing IR fails it)."""
    return fallback_target(state) if state.get("failed") else COMPILE_WORKFLOW


def after_compile_workflow(state: State) -> str:
    """A compile error is repaired (another profile, no prose) while rounds remain; otherwise the checks run."""
    if state.get("failed"):
        return _compile_repair_target(state) if _can_repair_compile(state) else fallback_target(state)
    return STATIC_CHECKS


def after_static_checks(state: State) -> str:
    """Lint-clean -> guard the workflow; errors -> repair the compile while rounds remain, else fall back."""
    if state.get("failed"):
        return fallback_target(state)
    if (state.get("lint") or {}).get("ok"):
        return GUARD_WORKFLOW
    return _compile_repair_target(state) if _can_repair_compile(state) else fallback_target(state)


def after_repair_compile(state: State) -> str:
    """Compile again; with no strategy left (the node failed) fall back."""
    return fallback_target(state) if state.get("failed") else COMPILE_WORKFLOW


def after_guard_workflow(state: State) -> str:
    """Blocked -> repair the compile or end blocked; a dry run ends here; otherwise validate on the platform."""
    if state.get("failed"):
        return fallback_target(state)
    if state.get("blocked"):
        return _compile_repair_target(state) if _can_repair_compile(state) else FINALIZE
    if state.get("dry_run") or _limit(state, "dry_run"):
        return FINALIZE
    return REMOTE_VALIDATE


def _after_platform_rejection(state: State) -> str:
    """The platform rejected the content: repair the compile, else fall back (never replace a deployed agent)."""
    if is_deployed(state):
        return FINALIZE
    return _compile_repair_target(state) if _can_repair_compile(state) else fallback_target(state)


def after_remote_validate(state: State) -> str:
    """Accepted (or platform trouble that is not the workflow's fault) -> deploy; rejected -> repair or fall back."""
    if state.get("failed"):
        return fallback_target(state)
    verdict = state.get("remote_validation") or {}
    if verdict.get("ok") or verdict.get("kind") in ("infra", "skipped"):
        return DEPLOY
    return _after_platform_rejection(state)


def after_deploy(state: State) -> str:
    """Deployed -> self-test; a content rejection on save -> repair; any other failure (or a skipped re-deploy) ends the run."""
    if state.get("failed") or state.get("redeploy_skipped"):
        return FINALIZE
    verdict = state.get("remote_validation") or {}
    if verdict.get("kind") == "invalid" and not verdict.get("ok"):
        return _after_platform_rejection(state)
    return SELF_TEST


def after_self_test(state: State) -> str:
    """Decide on the results of the CURRENT round; with none (mode ``none``, or no time) there is nothing to decide.

    ``self_tests`` accumulates over the repair rounds, so older results must not make a round in which nothing ran look
    like a round with a verdict.
    """
    round_no = int(state.get("selftest_repairs", 0))
    current = [t for t in state.get("self_tests") or [] if int((t or {}).get("round", 0)) == round_no]
    return DECIDE if current and not state.get("failed") else FINALIZE


def after_decide(state: State) -> str:
    """Another policy round when the decision asked for one (and the caps still allow it), else finish."""
    if state.get("failed") or not state.get("repair_requested"):
        return FINALIZE
    if int(state.get("selftest_repairs", 0)) > int(_limit(state, "max_selftest_repairs")):
        return FINALIZE
    return SERVICE_WORKFLOW if state.get("service_mode") else REPAIR_POLICY


def after_agentic_build(state: State) -> str:
    """A written workflow goes through the same checks; a failed session may be retried (bounded), else the run ends."""
    return fallback_target(state) if state.get("failed") else STATIC_CHECKS


#: Source node -> (router, every node the router can return); used by ``build_graph`` and the topology test.
ROUTERS: dict[str, tuple[Any, tuple[str, ...]]] = {
    LOAD_INPUTS: (after_load_inputs, (GUARD_INPUTS, FINALIZE)),
    GUARD_INPUTS: (after_guard_inputs, (UNDERSTAND_BRIEF, SERVICE_WORKFLOW, AGENTIC_BUILD, FINALIZE)),
    UNDERSTAND_BRIEF: (after_understand_brief, (EXTRACT_POLICY, AGENTIC_BUILD, FINALIZE)),
    EXTRACT_POLICY: (after_extract_policy, (VALIDATE_POLICY, AGENTIC_BUILD, FINALIZE)),
    VALIDATE_POLICY: (after_validate_policy, (REPAIR_POLICY, GUARD_POLICY, AGENTIC_BUILD, FINALIZE)),
    GUARD_POLICY: (after_guard_policy, (DRAFT_PROSE, REPAIR_POLICY, AGENTIC_BUILD, FINALIZE)),
    DRAFT_PROSE: (after_draft_prose, (COMPILE_WORKFLOW, AGENTIC_BUILD, FINALIZE)),
    COMPILE_WORKFLOW: (after_compile_workflow, (STATIC_CHECKS, REPAIR_COMPILE, AGENTIC_BUILD, FINALIZE)),
    SERVICE_WORKFLOW: (after_compile_workflow, (STATIC_CHECKS, SERVICE_WORKFLOW, FINALIZE)),
    STATIC_CHECKS: (after_static_checks, (GUARD_WORKFLOW, REPAIR_COMPILE, SERVICE_WORKFLOW, AGENTIC_BUILD, FINALIZE)),
    REPAIR_COMPILE: (after_repair_compile, (COMPILE_WORKFLOW, AGENTIC_BUILD, FINALIZE)),
    GUARD_WORKFLOW: (after_guard_workflow, (REMOTE_VALIDATE, REPAIR_COMPILE, SERVICE_WORKFLOW, AGENTIC_BUILD, FINALIZE)),
    REMOTE_VALIDATE: (after_remote_validate, (DEPLOY, REPAIR_COMPILE, SERVICE_WORKFLOW, AGENTIC_BUILD, FINALIZE)),
    DEPLOY: (after_deploy, (SELF_TEST, REPAIR_COMPILE, SERVICE_WORKFLOW, AGENTIC_BUILD, FINALIZE)),
    SELF_TEST: (after_self_test, (DECIDE, FINALIZE)),
    DECIDE: (after_decide, (REPAIR_POLICY, SERVICE_WORKFLOW, FINALIZE)),
    AGENTIC_BUILD: (after_agentic_build, (STATIC_CHECKS, AGENTIC_BUILD, FINALIZE)),
}

#: Edges that do not depend on the state.
STATIC_EDGES: tuple[tuple[str, str], ...] = ((REPAIR_POLICY, VALIDATE_POLICY),)

__all__ = [
    "AGENTIC_BUILD",
    "COMPILE_WORKFLOW",
    "DECIDE",
    "DEPLOY",
    "DRAFT_PROSE",
    "EXTRACT_POLICY",
    "FINALIZE",
    "GUARD_INPUTS",
    "GUARD_POLICY",
    "GUARD_WORKFLOW",
    "LOAD_INPUTS",
    "MAX_AGENTIC_ATTEMPTS",
    "REMOTE_VALIDATE",
    "REPAIR_COMPILE",
    "REPAIR_POLICY",
    "ROUTERS",
    "SELF_TEST",
    "SERVICE_WORKFLOW",
    "STATIC_CHECKS",
    "STATIC_EDGES",
    "UNDERSTAND_BRIEF",
    "VALIDATE_POLICY",
    "affordable",
    "after_agentic_build",
    "after_compile_workflow",
    "after_decide",
    "after_deploy",
    "after_draft_prose",
    "after_extract_policy",
    "after_guard_inputs",
    "after_guard_policy",
    "after_guard_workflow",
    "after_load_inputs",
    "after_remote_validate",
    "after_repair_compile",
    "after_self_test",
    "after_static_checks",
    "after_understand_brief",
    "after_validate_policy",
    "fallback_target",
    "has_policy_errors",
    "is_deployed",
]
