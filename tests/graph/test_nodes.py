"""Unit tests of the node helpers and of the wrapper every node runs in (``run_node``): decisions, status, containment."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from builder.graph.context import BuilderContext
from builder.graph.nodes import _common
from builder.graph.nodes.decide import ROUND_STAGES, issues_from_tests, round_cost_s
from builder.graph.nodes.finalize import collect_tests, decide_status
from builder.graph.nodes.load_inputs import limits_from_settings
from builder.graph.nodes.repair_compile import next_strategy
from builder.guardrails import GuardReport
from builder.trace import NullTrace

# ------------------------------------------------------------------------------------------------ repair_compile


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ({"prose": {"per_node": {"opening": "x"}}}, "drop_prose"),
        ({"prose": {"system_prompt_extra": "x"}}, "drop_prose"),
        ({"prose": {}}, "switch_profile"),
        ({"prose": {"system_prompt_extra": "", "per_node": {}}}, "switch_profile"),
        ({"prose": {"per_node": {"a": "x"}}, "compile_repair_log": ["drop_prose"]}, "switch_profile"),
        ({"prose": {"per_node": {"a": "x"}}, "compile_repair_log": ["switch_profile"]}, "drop_prose"),
        ({"prose": {"per_node": {"a": "x"}}, "compile_repair_log": ["drop_prose", "switch_profile"]}, None),
        ({"prose": {}, "compile_repair_log": ["switch_profile"]}, None),
    ],
)
def test_next_compile_repair_strategy(state: dict[str, Any], expected: str | None) -> None:
    assert next_strategy(state) == expected  # type: ignore[arg-type]


# ------------------------------------------------------------------------------------------------ decide


def test_decide_turns_failed_findings_into_policy_issues() -> None:
    tests = [
        {"name": "smoke", "passed": False, "summary": "reply 2 is empty", "details": {}},
        {
            "name": "walkthrough",
            "passed": False,
            "details": {
                "issues": [
                    {
                        "rule_id": "R-1",
                        "verdict": "missing",
                        "where": "opening",
                        "explanation": "no greeting",
                    },
                    {
                        "rule_id": "R-2",
                        "verdict": "contradicted",
                        "where": "identity",
                        "explanation": "wrong count",
                    },
                    {"rule_id": "R-3", "verdict": "unclear", "where": "x", "explanation": "vague"},
                ]
            },
        },
        {
            "name": "practice",
            "passed": False,
            "details": {
                "failed_cases": [{"id": "C-9", "family": "Hardship", "failed": ["offered twice", "no note"]}]
            },
        },
        {"name": "other", "passed": True, "details": {"issues": [{"rule_id": "Z", "verdict": "missing"}]}},
    ]
    issues = issues_from_tests(tests)
    assert [(i.code, i.rule_ids) for i in issues[:2]] == [
        ("WALKTHROUGH_MISSING", ["R-1"]),
        ("WALKTHROUGH_CONTRADICTED", ["R-2"]),
    ]
    assert "stage: opening" in issues[0].message
    assert (
        issues[2].code == "PRACTICE_FAILED"
        and "C-9" in issues[2].message
        and "offered twice; no note" in issues[2].message
    )
    assert len(issues) == 3 and all(
        i.severity == "error" for i in issues
    )  # smoke and unclear findings start no repair


def test_a_repair_round_costs_what_its_stages_cost() -> None:
    assert round_cost_s() == pytest.approx(360 + 240 + 20 + 30 + 90 + 90)
    assert "deploy" in ROUND_STAGES


# ------------------------------------------------------------------------------------------------ finalize


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ({"deploy": {"workflow_id": "wf"}}, "deployed"),
        (
            {"deploy": {"workflow_id": "wf"}, "blocked": True},
            "deployed",
        ),  # the published agent is what counts
        ({"blocked": True}, "blocked"),
        ({"status": "blocked"}, "blocked"),
        ({"dry_run": True, "lint": {"ok": True}, "compiled": {"workflow": {"nodes": {"a": {}}}}}, "dry_run"),
        (
            {
                "limits": {"dry_run": True},
                "lint": {"ok": True},
                "compiled": {"workflow": {"nodes": {"a": {}}}},
            },
            "dry_run",
        ),
        ({"dry_run": True, "lint": {"ok": False}, "compiled": {"workflow": {"nodes": {"a": {}}}}}, "failed"),
        ({"dry_run": True, "lint": {"ok": True}}, "failed"),
        (
            {"lint": {"ok": True}, "compiled": {"workflow": {"nodes": {"a": {}}}}},
            "failed",
        ),  # not deployed, not a dry run
        ({}, "failed"),
    ],
)
def test_final_status(state: dict[str, Any], expected: str) -> None:
    assert decide_status(state) == expected


def test_tests_for_the_summary() -> None:
    state = {
        "lint": {"ok": True, "summary": "0 errors, 1 warnings"},
        "remote_validation": {"ok": True, "kind": "deployed"},
        "self_tests": [
            {"name": "smoke", "passed": False, "summary": "empty", "round": 0, "details": {}},
            {"name": "smoke", "passed": True, "summary": "ok", "round": 1, "details": {}},
            {
                "name": "practice",
                "passed": True,
                "summary": "skipped",
                "round": 1,
                "details": {"skipped": True},
            },
        ],
    }
    tests = collect_tests(state)
    assert [(t["name"], t["passed"]) for t in tests] == [
        ("static_checks", True), ("remote_validate", True), ("smoke", False), ("smoke#2", True),
    ]  # fmt: skip
    assert (
        collect_tests({"remote_validation": {"ok": False, "kind": "infra"}}) == []
    )  # infra trouble is not a test


# ------------------------------------------------------------------------------------------------ limits and helpers


def test_limits_copy_the_settings(make_settings: Any) -> None:
    from builder.budget import Budget

    ctx = BuilderContext(
        settings=make_settings(max_policy_repairs=3, self_test="smoke", dry_run=True),
        llm=None, freya=None, trace=NullTrace(), budget=Budget(900),
    )  # fmt: skip
    limits = limits_from_settings(ctx, allow_agentic=True)
    assert limits["max_policy_repairs"] == 3 and limits["self_test"] == "smoke" and limits["dry_run"] is True
    assert limits["allow_agentic"] is True and limits["budget_total_s"] == 900


def test_guard_issues_become_policy_issues() -> None:
    report = GuardReport(stage="policy")
    report.block("invented_value", "date not in the briefs", "bands[0].deadline.fixed")
    report.warn("approximate", "fuzzy")
    (issue,) = _common.guard_issues(report)
    assert issue["code"] == "GUARD_INVENTED_VALUE" and issue["path"] == "bands[0].deadline.fixed"
    assert issue["severity"] == "error"


def test_workflow_files_go_to_the_out_directory(tmp_dirs: SimpleNamespace) -> None:
    path = _common.write_workflow_file("slot-x", {"nodes": {}})
    assert path == tmp_dirs.out / "slot-x.workflow.json" and path.read_text(encoding="utf-8").endswith("\n")
    assert _common.workflow_file("slot-x", ".exported.json") == tmp_dirs.out / "slot-x.exported.json"
    assert _common.dedupe(["a", "b", "a"]) == ["a", "b"]
    assert _common.short("x " * 200, 20).endswith("…") and len(_common.short("x " * 200, 20)) == 20


def test_lint_call_variables_always_include_the_platform_ones(ir_a: Any) -> None:
    assert _common.lint_call_variables(None) == {"customer_id", "now"}
    assert set(ir_a.call_variable_names()) <= _common.lint_call_variables(ir_a)


# ------------------------------------------------------------------------------------------------ run_node


@pytest.fixture
def ctx(make_settings: Any, null_trace: NullTrace) -> BuilderContext:
    from builder.budget import Budget

    return BuilderContext(
        settings=make_settings(), llm=None, freya=None, trace=null_trace, budget=Budget(1000)
    )


async def test_run_node_wraps_success_with_trace_budget_and_progress(
    ctx: BuilderContext, null_trace: NullTrace
) -> None:
    async def body(state: Any, context: BuilderContext) -> dict[str, Any]:
        return {"deploy": {"workflow_id": "wf"}, "notes": ["x"]}

    update = await _common.run_node("demo", {"slot": "s"}, ctx, body)  # type: ignore[arg-type]
    assert update["failed"] == "" and update["notes"] == ["x"] and 990 < update["budget_remaining_s"] <= 1000
    assert ctx.progress["deploy"] == {"workflow_id": "wf"}
    statuses = [(e["data"]["stage"], e["data"]["status"]) for e in null_trace.of_kind("stage")]
    assert statuses == [("demo", "start"), ("demo", "end")]


async def test_run_node_turns_an_exception_into_a_failed_update(
    ctx: BuilderContext, null_trace: NullTrace
) -> None:
    async def body(state: Any, context: BuilderContext) -> dict[str, Any]:
        raise ValueError("no good")

    update = await _common.run_node("demo", {}, ctx, body)  # type: ignore[arg-type]
    assert update["failed"] == "demo" and update["errors"] == ["demo: ValueError: no good"]
    assert [e["data"]["status"] for e in null_trace.of_kind("stage")] == ["start", "failed"]
    assert null_trace.of_kind("error")[0]["message"] == "node demo crashed: ValueError"


async def test_run_node_marks_skipped_nodes(ctx: BuilderContext, null_trace: NullTrace) -> None:
    async def body(state: Any, context: BuilderContext) -> dict[str, Any]:
        return {"_skipped": True}

    update = await _common.run_node("demo", {}, ctx, body)  # type: ignore[arg-type]
    assert "_skipped" not in update
    assert [e["data"]["status"] for e in null_trace.of_kind("stage")] == ["start", "skipped"]


async def test_run_node_lets_cancellation_through(ctx: BuilderContext, null_trace: NullTrace) -> None:
    async def body(state: Any, context: BuilderContext) -> dict[str, Any]:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await _common.run_node("demo", {}, ctx, body)  # type: ignore[arg-type]
    assert [e["data"]["status"] for e in null_trace.of_kind("stage")] == ["start", "aborted"]


async def test_load_inputs_fails_cleanly_on_unreadable_inputs(ctx: BuilderContext, tmp_path: Path) -> None:
    from builder.graph.nodes import load_inputs

    update = await load_inputs._body(
        {"inputs": {"briefs": [str(tmp_path / "missing.md")], "tool_api": "x.json"}}, ctx
    )  # type: ignore[arg-type]
    assert update["failed"] == "load_inputs" and update["status"] == "failed"
    assert update["errors"][0].startswith("load_inputs: ")


# ------------------------------------------------------------------------------------------------ decide: honest verdicts


async def test_decide_does_not_claim_a_pass_for_a_round_in_which_no_self_test_ran(
    ctx: BuilderContext, null_trace: NullTrace
) -> None:
    """Round 1 re-deployed the policy repair but had no time left for any test; round 0 had a failing practice run."""
    from builder.graph.nodes import decide

    state = {
        "slot": "s",
        "track": "compiled",
        "selftest_repairs": 1,
        "limits": {"max_selftest_repairs": 1},
        "self_tests": [
            {
                "name": "practice",
                "passed": False,
                "round": 0,
                "summary": "brief A: 7/10",
                "details": {"failed_cases": [{"id": "PUB-04", "family": "x", "failed": ["ledger"]}]},
            }
        ],
    }
    update = await decide._body(state, ctx)  # type: ignore[arg-type]
    assert "all self-tests passed" not in " ".join(update["notes"])
    assert "no self-test ran" in update["notes"][0]
    assert update["untested_redeploy"] is True and update["repair_requested"] is False
    (decision,) = null_trace.of_kind("decision")
    assert "all self-tests passed" not in decision["message"] and "no self-test ran" in decision["message"]


async def test_decide_counts_only_the_tests_that_ran(ctx: BuilderContext, null_trace: NullTrace) -> None:
    from builder.graph.nodes import decide

    skipped_only = {
        "selftest_repairs": 0,
        "limits": {},
        "self_tests": [{"name": "practice", "passed": True, "round": 0, "details": {"skipped": True}}],
    }
    update = await decide._body(skipped_only, ctx)  # type: ignore[arg-type]
    assert "all self-tests passed" not in update["notes"][0] and "no self-test ran" in update["notes"][0]
    assert "untested_redeploy" not in update  # round 0 is the first deployment: nothing was re-deployed
    passed = {
        "selftest_repairs": 0,
        "limits": {},
        "self_tests": [
            {"name": "smoke", "passed": True, "round": 0, "details": {}},
            {"name": "practice", "passed": True, "round": 0, "details": {"skipped": True}},
        ],
    }
    update = await decide._body(passed, ctx)  # type: ignore[arg-type]
    assert update["notes"] == ["decide: all self-tests passed"]


async def test_the_self_test_node_flags_a_redeploy_that_could_not_be_tested(
    make_settings: Any, null_trace: NullTrace
) -> None:
    from builder.budget import Budget
    from builder.graph.nodes import self_test

    ctx = BuilderContext(
        settings=make_settings(self_test="auto"), llm=None, freya=None, trace=null_trace, budget=Budget(100)
    )
    state = {"slot": "s", "selftest_repairs": 1, "deploy": {"workflow_id": "wf", "agent_id": "ag"}}
    update = await self_test._body(state, ctx)  # type: ignore[arg-type]
    assert update["self_tests"] == [] and update["untested_redeploy"] is True
    first = await self_test._body({**state, "selftest_repairs": 0}, ctx)  # type: ignore[arg-type]
    assert "untested_redeploy" not in first  # the first round has no earlier tested version to compare with
