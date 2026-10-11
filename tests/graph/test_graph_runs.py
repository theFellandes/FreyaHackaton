"""The graph end to end with a fixture gateway and a fake Freya: the happy path, the repair loops and every fallback.

Each test drives the real graph (``build_graph().ainvoke``) and looks at the final state, the fake platform's call log and the
trace. The LLM is a ``FixtureGateway`` (answers from the Brief A fixtures), Freya a ``FakeFreya``; no network, no model.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from builder.build import run_build
from builder.compiler.prose import NodeProse
from builder.graph.nodes.finalize import finalize_run
from builder.guardrails import GuardReport
from builder.policy.ir import PolicyIR
from builder.settings import Settings
from builder.testing import FakeFreya, FixtureGateway
from builder.trace import NullTrace
from tests.graph.conftest import load_ir, stages_of


def broken_ir() -> PolicyIR:
    """A Brief A IR whose stop outcome is not in the tool API (a validator error the model has to repair)."""
    data = load_ir("brief_a").to_json_dict()
    data["persuasion"]["stop_outcome"] = {"outcome": "declined", "reason": "refused"}
    return PolicyIR.model_validate(data)


def actions(freya: FakeFreya) -> list[str]:
    return [name for name, _ in freya.calls]


# ------------------------------------------------------------------------------------------------ the happy path


async def test_full_build_deploys_exports_and_self_tests(
    make_ctx: Any,
    make_gateway: Any,
    make_freya: Any,
    practice_ok: Any,
    invoke: Any,
    tmp_dirs: SimpleNamespace,
    null_trace: NullTrace,
) -> None:
    gateway, freya = make_gateway(), make_freya()
    ctx = make_ctx(gateway, freya, practice_runner=practice_ok)
    state = await invoke(ctx)

    final = state["final"]
    assert final["status"] == "deployed" and final["track"] == "compiled"
    assert final["workflow_id"].startswith("wf_fake") and final["agent_id"].startswith("ag_fake")
    assert final["version"] == 1
    exported = json.loads(Path(final["exported"]).read_text(encoding="utf-8"))
    assert exported["workflow_id"] == final["workflow_id"] and "nodes" in exported["workflow"]
    assert Path(state["workflow_path"]) == tmp_dirs.out / "brief-a.workflow.json"
    assert actions(freya)[:4] == ["validate", "save", "get_workflow", "export"]
    assert actions(freya).count("chat") == 2 and actions(freya)[-1] == "end_chat"

    assert [t["name"] for t in state["self_tests"]] == ["smoke", "walkthrough", "practice"]
    assert all(t["passed"] for t in state["self_tests"]), [t["summary"] for t in state["self_tests"]]
    assert final["tests"] == {"passed": 5, "total": 5}  # static checks, platform validation, three self-tests
    assert stages_of(null_trace) == [
        "load_inputs", "guard_inputs", "understand_brief", "extract_policy", "validate_policy", "guard_policy",
        "draft_prose", "compile_workflow", "static_checks", "guard_workflow", "remote_validate", "deploy",
        "self_test", "decide", "finalize",
    ]  # fmt: skip
    summary = null_trace.of_kind("summary")
    assert len(summary) == 1
    assert summary[0]["data"]["freya"]["workflow_id"] == final["workflow_id"]
    assert summary[0]["data"]["status"] == "deployed"
    assert [e["data"]["name"] for e in null_trace.of_kind("test")][-3:] == [
        "smoke",
        "walkthrough",
        "practice",
    ]


async def test_the_deployed_workflow_is_sent_with_editor_positions_for_every_node(
    make_ctx: Any,
    make_gateway: Any,
    make_freya: Any,
    practice_ok: Any,
    invoke: Any,
    tmp_dirs: SimpleNamespace,
) -> None:
    """A compiled workflow without positions would be unreadable in the editor (all nodes on one spot)."""
    freya = make_freya()
    state = await invoke(make_ctx(make_gateway(), freya, practice_runner=practice_ok))
    deployed = state["compiled"]["workflow"]
    expected = set(deployed["nodes"]) | set(deployed.get("global_nodes") or {})
    positions = freya.node_positions[state["slot"]]
    assert set(positions) == expected and len(expected) > 5
    assert len({(round(p["x"]), round(p["y"])) for p in positions.values()}) == len(positions)  # no two on one spot
    assert freya.published_versions[state["slot"]] == state["final"]["version"]


async def test_the_optional_prose_draft_stops_in_time_for_the_deploy(
    make_ctx: Any,
    make_gateway: Any,
    make_freya: Any,
    practice_ok: Any,
    invoke: Any,
    tmp_dirs: SimpleNamespace,
) -> None:
    """The mandatory calls (rule passages, IR extraction) get the whole budget; the optional ones (prose draft, ledger
    completeness / re-quote / reconcile) must leave the time the later stages need (P-04, AUD-53)."""
    from builder.budget import estimate

    gateway = make_gateway()
    ctx = make_ctx(gateway, make_freya(), practice_runner=practice_ok)
    await invoke(ctx)

    end = ctx.budget.deadline()
    prose = next(c for c in gateway.calls if c.schema == "ProseDraft")
    tail = estimate("compile") + estimate("remote_validate") + estimate("deploy")
    assert prose.deadline is not None and prose.deadline <= end - tail
    optional_prefixes = ("rule ledger: completeness", "rule ledger: re-quote", "rule ledger: link amendments")
    optional = [c for c in gateway.calls if c.purpose.startswith(optional_prefixes)]
    assert optional and all(
        c.deadline is not None and c.deadline <= end - tail - estimate("extract") for c in optional
    )  # an optional ledger call can never leave extraction without time
    mandatory = [
        c for c in gateway.calls if c.schema != "ProseDraft" and c not in optional and c.deadline is not None
    ]
    assert mandatory and all(c.deadline == end for c in mandatory)  # mandatory stages keep the full budget


async def test_self_test_none_skips_the_tests_and_still_deploys(
    make_ctx: Any, make_gateway: Any, make_freya: Any, invoke: Any
) -> None:
    freya = make_freya()
    state = await invoke(make_ctx(make_gateway(), freya, self_test="none"))
    assert state["final"]["status"] == "deployed"
    assert state["self_tests"] == [] and "chat" not in actions(freya)


async def test_unseen_briefs_skip_the_practice_cases(
    make_ctx: Any, make_gateway: Any, make_freya: Any, invoke: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("builder.selftest.practice.kit_brief_letter", lambda bundle: None)
    called: list[Any] = []
    state = await invoke(
        make_ctx(make_gateway(), make_freya(), self_test="practice", practice_runner=called.append)
    )
    (practice,) = state["self_tests"]
    assert practice["details"]["skipped"] and "do not apply" in practice["summary"]
    assert called == [] and state["final"]["tests"]["total"] == 2  # skipped tests are not reported as tests


# ------------------------------------------------------------------------------------------------ compile repairs


async def test_platform_rejection_drops_the_prose_first(
    make_ctx: Any, make_gateway: Any, make_freya: Any, invoke: Any
) -> None:
    prose = NodeProse(per_node={"opening": "- Keep the opening short and friendly."})
    freya = make_freya(reject_validations=[["steps.3: unknown field"]])
    state = await invoke(make_ctx(make_gateway(prose=prose), freya))
    assert state["final"]["status"] == "deployed"
    assert state["compile_repair_log"] == ["drop_prose"] and state["compile_repairs"] == 1
    assert state["prose"] == {}
    assert actions(freya).count("validate") == 2 and actions(freya).count("save") == 1


async def test_without_prose_the_first_repair_switches_the_profile(
    make_ctx: Any, make_gateway: Any, make_freya: Any, invoke: Any
) -> None:
    freya = make_freya(reject_validations=[["bad one"]])
    state = await invoke(make_ctx(make_gateway(), freya))  # no prose: dropping it would change nothing
    assert state["final"]["status"] == "deployed"
    assert state["profile"] == "lean" and state["compile_repair_log"] == ["switch_profile"]
    assert state["compile_repairs"] == 1
    assert len(freya.saved["brief-a"]["workflow"]["nodes"]) < 15


async def test_two_rejections_use_both_strategies(
    make_ctx: Any, make_gateway: Any, make_freya: Any, invoke: Any
) -> None:
    prose = NodeProse(per_node={"opening": "- Keep the opening short and friendly."})
    freya = make_freya(reject_validations=[["bad one"], ["bad two"]])
    state = await invoke(make_ctx(make_gateway(prose=prose), freya))
    assert state["final"]["status"] == "deployed"
    assert state["compile_repair_log"] == ["drop_prose", "switch_profile"] and state["compile_repairs"] == 2
    assert state["profile"] == "lean"


async def test_a_platform_that_keeps_rejecting_ends_failed_without_a_deployment(
    make_ctx: Any, make_gateway: Any, make_freya: Any, invoke: Any
) -> None:
    freya = make_freya(reject_validations=[["no"]] * 6)
    state = await invoke(make_ctx(make_gateway(), freya, builder_track="compiled"))
    assert state["final"]["status"] == "failed"
    assert "deploy" not in state and freya.saved == {}
    assert "save" not in actions(freya)


async def test_a_rejection_on_save_is_repaired_like_a_failed_validation(
    make_ctx: Any, make_gateway: Any, make_freya: Any, invoke: Any
) -> None:
    freya = make_freya(reject_saves=[["bad on write"]])
    state = await invoke(make_ctx(make_gateway(), freya))
    assert state["final"]["status"] == "deployed"
    assert state["compile_repairs"] == 1 and actions(freya).count("save") == 2


# ------------------------------------------------------------------------------------------------ policy repairs


async def test_validator_errors_are_repaired_by_the_model(
    make_ctx: Any, make_gateway: Any, invoke: Any, null_trace: NullTrace
) -> None:
    answers = iter([broken_ir(), load_ir("brief_a")])
    gateway = make_gateway(responders={"PolicyIR": lambda call: next(answers)})
    state = await invoke(make_ctx(gateway))
    assert state["final"]["status"] == "dry_run"
    assert state["policy_repairs"] == 1
    assert [c.purpose for c in gateway.calls if c.schema == "PolicyIR"][1].startswith("repair policy IR")
    assert any(e["kind"] == "validate" and "1 error" in e["message"] for e in null_trace.events)


async def test_a_policy_repair_leaves_time_for_compile_validation_and_deploy(
    make_ctx: Any, make_gateway: Any, invoke: Any
) -> None:
    """AUD-53: the repair is optional (a failure keeps the old IR); its call must not run to the budget's end."""
    from builder.budget import estimate

    answers = iter([broken_ir(), load_ir("brief_a")])
    gateway = make_gateway(responders={"PolicyIR": lambda call: next(answers)})
    ctx = make_ctx(gateway)
    await invoke(ctx)
    repair = next(c for c in gateway.calls if c.purpose.startswith("repair policy IR"))
    tail = estimate("compile") + estimate("remote_validate") + estimate("deploy")
    assert repair.deadline is not None and repair.deadline <= ctx.budget.deadline() - tail


async def test_unrepairable_policy_falls_back_to_the_agentic_track(
    make_ctx: Any, make_gateway: Any, make_freya: Any, invoke: Any, compiled_a_workflow: dict[str, Any]
) -> None:
    gateway = make_gateway(responders={"PolicyIR": lambda call: broken_ir()})
    calls: list[dict[str, Any]] = []

    async def runner(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"ok": True, "workflow": compiled_a_workflow, "turns": 12, "credential": "anthropic#1(...)"}

    freya = make_freya()
    state = await invoke(make_ctx(gateway, freya, agentic_runner=runner, builder_track="auto"))
    assert state["policy_repairs"] == 2  # the cap was used up first
    assert state["final"]["status"] == "deployed" and state["final"]["track"] == "agentic"
    assert len(calls) == 1 and calls[0]["feedback"] == "" and calls[0]["slot"] == "brief-a"
    assert state["agentic_attempts"] == 1


async def test_a_crashing_policy_repair_uses_up_its_rounds_instead_of_looping(
    make_ctx: Any, make_gateway: Any, invoke: Any, null_trace: NullTrace
) -> None:
    """An exception other than AllProvidersFailed in the repair used to leave the counter at 0: 48 validate/repair
    rounds until LangGraph's recursion limit, and ``failed`` with an empty error list (audit REPO-10)."""
    purposes: list[str] = []

    def respond(call: Any) -> PolicyIR:
        purposes.append(call.purpose)
        if len(purposes) == 1:
            return broken_ir()
        raise RuntimeError("the repair crashed")

    gateway = make_gateway(responders={"PolicyIR": respond})
    state = await invoke(make_ctx(gateway))
    repairs = [purpose for purpose in purposes if purpose.startswith("repair policy IR")]
    assert 1 <= len(repairs) <= 2  # max_policy_repairs, not one per recursion step
    assert state["policy_repairs"] == 2
    assert any("RuntimeError" in e for e in state.get("errors", []))
    assert state["final"]["errors"], "a failed or best-effort build must say what went wrong"


async def test_blocked_policy_that_cannot_be_repaired_ends_blocked(
    make_ctx: Any, make_gateway: Any, invoke: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    blocked = GuardReport(stage="policy")
    blocked.block(
        "invented_value", "the IR names a date that is not in the briefs", "bands[0].deadline.fixed"
    )
    monkeypatch.setattr("builder.graph.nodes.guard_policy.check_policy", lambda *a, **k: blocked)
    gateway = make_gateway()
    state = await invoke(make_ctx(gateway))
    assert state["final"]["status"] == "blocked" and state["policy_repairs"] == 2
    assert not [c for c in gateway.calls if c.schema == "ProseDraft"]  # nothing was compiled


@pytest.mark.parametrize("guard", ["ungrounded_date", "ungrounded_amount", "ungrounded_number"])
async def test_grounding_findings_left_after_the_repairs_deploy_best_effort(
    make_ctx: Any, make_gateway: Any, invoke: Any, monkeypatch: pytest.MonkeyPatch, null_trace: NullTrace, guard: str
) -> None:
    """AUD-14: a grounding heuristic the repairs cannot clear (a holiday written as a name, a date spelling the guard
    cannot read) used to end the build blocked with no agent - a certain 0. After the repair rounds it is kept as a
    warning and the run goes on, with the decision in the trace and the summary."""
    soft = GuardReport(stage="policy")
    soft.block(guard, "the IR states a value the guard cannot find in the briefs", "calendar.holidays[1]")
    monkeypatch.setattr("builder.graph.nodes.guard_policy.check_policy", lambda *a, **k: soft)
    gateway = make_gateway()
    state = await invoke(make_ctx(gateway))
    assert state["policy_repairs"] == 2  # the repairs still ran first
    assert state["final"]["status"] == "dry_run"
    assert any("best effort" in note for note in state["notes"])
    assert any("best effort" in e["message"] for e in null_trace.of_kind("decision"))


async def test_a_hard_finding_left_after_the_repairs_still_ends_blocked(
    make_ctx: Any, make_gateway: Any, invoke: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    mixed = GuardReport(stage="policy")
    mixed.block("ungrounded_date", "a date the guard cannot find", "calendar.holidays[1]")
    mixed.block("injection", "text addressed to a model", "phrases.extra.x")
    monkeypatch.setattr("builder.graph.nodes.guard_policy.check_policy", lambda *a, **k: mixed)
    state = await invoke(make_ctx(make_gateway()))
    assert state["final"]["status"] == "blocked" and state["policy_repairs"] == 2


# ------------------------------------------------------------------------------------------------ guards and crashes


async def test_blocked_inputs_end_the_run_before_any_model_call(
    make_ctx: Any, make_gateway: Any, invoke: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    blocked = GuardReport(stage="inputs")
    blocked.block("size", "the input is far too large")
    monkeypatch.setattr("builder.graph.nodes.guard_inputs.check_inputs", lambda *a, **k: blocked)
    gateway = make_gateway()
    state = await invoke(make_ctx(gateway))
    assert state["final"]["status"] == "blocked" and gateway.calls == []


async def test_a_blocked_workflow_is_never_deployed(
    make_ctx: Any, make_gateway: Any, make_freya: Any, invoke: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    blocked = GuardReport(stage="workflow")
    blocked.block("foreign_url", "a tool call goes to a host that is not the bank's", "nodes.x")
    monkeypatch.setattr("builder.graph.nodes.guard_workflow.check_workflow", lambda *a, **k: blocked)
    freya = make_freya()
    state = await invoke(make_ctx(make_gateway(), freya))
    assert state["final"]["status"] == "blocked"
    assert freya.calls == []  # not even a dry-run validation
    assert state["compile_repairs"] >= 1  # the compile was retried with the other strategies first


async def test_a_failed_ledger_ends_failed_and_the_summary_is_still_written(
    make_ctx: Any, make_gateway: Any, invoke: Any, null_trace: NullTrace
) -> None:
    state = await invoke(make_ctx(make_gateway(fail_schemas={"LedgerPart"})))
    assert state["final"]["status"] == "failed"
    assert any("understand_brief" in e for e in state["errors"])
    assert len(null_trace.of_kind("summary")) == 1


async def test_an_unexpected_exception_in_a_node_is_contained(
    make_ctx: Any, make_gateway: Any, invoke: Any, null_trace: NullTrace, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr("builder.graph.nodes.validate_policy.validate_policy", boom)
    state = await invoke(make_ctx(make_gateway()))
    assert state["final"]["status"] == "failed"
    assert state["errors"] == ["validate_policy: RuntimeError: boom"]
    assert any(e["kind"] == "error" and "validate_policy crashed" in e["message"] for e in null_trace.events)
    failed_stage = next(e for e in null_trace.of_kind("stage") if e["data"]["status"] == "failed")
    assert failed_stage["data"]["stage"] == "validate_policy"


# ------------------------------------------------------------------------------------------------ self-test repair loop


async def test_a_failing_walkthrough_triggers_one_policy_repair_round_and_a_redeploy(
    make_ctx: Any, make_gateway: Any, make_freya: Any, invoke: Any, null_trace: NullTrace
) -> None:
    rounds = iter(
        [
            [
                {
                    "rule_id": "A-1b",
                    "verdict": "missing",
                    "where": "opening",
                    "explanation": "no opening sentence",
                }
            ],
            [],
        ]
    )
    reports: list[Any] = []

    def walkthrough(call: Any) -> Any:
        from builder.selftest.walkthrough import WalkthroughReport

        report = WalkthroughReport.model_validate({"issues": next(rounds), "checked": 100})
        reports.append(report)
        return report

    gateway = make_gateway(responders={"WalkthroughReport": walkthrough})
    freya = make_freya()
    state = await invoke(make_ctx(gateway, freya, self_test="walkthrough"))
    assert [t["passed"] for t in state["self_tests"]] == [False, True]
    assert [t["round"] for t in state["self_tests"]] == [0, 1]
    assert state["selftest_repairs"] == 1
    assert freya.versions[state["final"]["workflow_id"]] == 2  # deployed twice, same workflow
    repair_calls = [c for c in gateway.calls if c.purpose.startswith("repair policy IR")]
    assert (
        len(repair_calls) == 1
        and "WALKTHROUGH_MISSING" in repair_calls[0].user
        and "A-1b" in repair_calls[0].user
    )
    assert state["final"]["status"] == "deployed"
    assert (
        state["final"]["tests"]["passed"] == state["final"]["tests"]["total"] - 1
    )  # the first round's failure is kept
    assert any("requesting policy repair round 1" in e["message"] for e in null_trace.of_kind("decision"))


async def test_the_default_self_test_never_redeploys_a_tested_version(
    make_ctx: Any, make_gateway: Any, make_freya: Any, invoke: Any, practice_ok: Any
) -> None:
    """AUD-01: with the default settings (what the organisers' bare command runs) a walkthrough finding cannot replace
    the tested version 1 - the walkthrough and its repair round are explicit opt-ins."""
    from builder.selftest.walkthrough import WalkthroughReport

    defaults = Settings.model_fields
    gateway = make_gateway(
        responders={
            "WalkthroughReport": lambda call: WalkthroughReport.model_validate(
                {
                    "issues": [{"rule_id": "A-1b", "verdict": "missing", "where": "opening", "explanation": "x"}],
                    "checked": 1000,
                }
            )
        }
    )
    freya = make_freya()
    ctx = make_ctx(
        gateway,
        freya,
        practice_runner=practice_ok,
        builder_track=defaults["builder_track"].default,
        self_test=defaults["self_test"].default,
    )
    state = await invoke(ctx)
    assert state["final"]["status"] == "deployed"
    assert freya.versions[state["final"]["workflow_id"]] == 1
    assert int(state.get("selftest_repairs") or 0) == 0
    assert [t["name"] for t in state["self_tests"]] == ["smoke"]
    assert not [c for c in gateway.calls if c.purpose.startswith("repair policy IR")]


async def test_no_repair_round_when_the_budget_does_not_cover_it(
    make_ctx: Any, make_gateway: Any, make_freya: Any, invoke: Any
) -> None:
    issue = [{"rule_id": "A-1b", "verdict": "contradicted", "where": "opening", "explanation": "wrong"}]
    gateway = make_gateway(responders={"WalkthroughReport": lambda c: {"issues": issue, "checked": 1}})
    freya = make_freya()
    state = await invoke(make_ctx(gateway, freya, self_test="walkthrough", budget_s=700))
    assert state["final"]["status"] == "deployed"  # what is deployed stays deployed
    assert state["selftest_repairs"] == 0 and freya.versions[state["final"]["workflow_id"]] == 1
    assert [t["passed"] for t in state["self_tests"]] == [False]


# ------------------------------------------------------------------------------------------------ agentic track


async def test_requested_agentic_track_skips_the_compiler_and_uses_the_same_gates(
    make_ctx: Any, make_gateway: Any, make_freya: Any, invoke: Any, compiled_a_workflow: dict[str, Any]
) -> None:
    async def runner(**kwargs: Any) -> dict[str, Any]:
        return {"ok": True, "workflow": compiled_a_workflow, "turns": 30, "credential": "anthropic#1(...)"}

    gateway, freya = make_gateway(), make_freya()
    state = await invoke(make_ctx(gateway, freya, builder_track="agentic", agentic_runner=runner))
    assert state["final"]["status"] == "deployed" and state["final"]["track"] == "agentic"
    assert gateway.calls == [] or all(c.schema == "WalkthroughReport" for c in gateway.calls)
    assert actions(freya)[:2] == ["validate", "save"]
    assert "ledger" not in state and state["lint"]["ok"]


async def test_agentic_second_attempt_receives_the_findings_of_the_checks(
    make_ctx: Any, make_gateway: Any, make_freya: Any, invoke: Any, compiled_a_workflow: dict[str, Any]
) -> None:
    flawed = {**compiled_a_workflow, "agent_config": {"model": "x"}}  # lint rule L019: leave agent_config out
    feedbacks: list[str] = []

    async def runner(**kwargs: Any) -> dict[str, Any]:
        feedbacks.append(kwargs["feedback"])
        return {"ok": True, "workflow": flawed if len(feedbacks) == 1 else compiled_a_workflow, "turns": 5}

    state = await invoke(
        make_ctx(make_gateway(), make_freya(), builder_track="agentic", agentic_runner=runner)
    )
    assert state["final"]["status"] == "deployed" and state["agentic_attempts"] == 2
    assert feedbacks[0] == "" and "L019" in feedbacks[1]


async def test_agentic_unavailable_falls_back_to_the_compiled_track_when_requested(
    make_ctx: Any, make_gateway: Any, invoke: Any, null_trace: NullTrace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("builder.graph.nodes.load_inputs.agentic_available", lambda settings: False)
    state = await invoke(make_ctx(make_gateway(), builder_track="agentic"))
    assert state["final"]["track"] == "compiled" and state["final"]["status"] == "dry_run"
    assert any("not available" in note for note in state["notes"])


async def test_failed_agentic_session_ends_failed(
    make_ctx: Any, make_gateway: Any, make_freya: Any, invoke: Any
) -> None:
    async def runner(**kwargs: Any) -> dict[str, Any]:
        return {"ok": False, "workflow": None, "error": "no usable credential"}

    freya = make_freya()
    state = await invoke(make_ctx(make_gateway(), freya, builder_track="agentic", agentic_runner=runner))
    assert state["final"]["status"] == "failed" and freya.calls == []
    assert any("no usable credential" in e for e in state["errors"])


# ------------------------------------------------------------------------------------------------ run_build wrapper


async def test_run_build_without_a_freya_key_downgrades_to_a_dry_run(
    make_settings: Any, inputs: Any, make_gateway: Any, null_trace: NullTrace, tmp_dirs: SimpleNamespace
) -> None:
    settings: Settings = make_settings(dry_run=False)
    final = await run_build(settings, inputs, llm=make_gateway(), trace=null_trace)
    assert final["status"] == "dry_run"
    assert any("FREYA_API_KEY is not set" in e["message"] for e in null_trace.of_kind("error"))


async def test_the_wall_clock_guard_cuts_the_graph_off_and_still_reports(
    make_settings: Any,
    inputs: Any,
    make_gateway: Any,
    null_trace: NullTrace,
    tmp_dirs: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SlowGateway(FixtureGateway):
        async def structured(self, **kwargs: Any) -> Any:
            await asyncio.sleep(30)

    monkeypatch.setattr("builder.build.GUARD_GRACE_S", 0.0)
    slow = SlowGateway(load_ir_ledger(), load_ir("brief_a"), trace=null_trace)
    final = await asyncio.wait_for(
        run_build(make_settings(dry_run=True, build_time_budget_s=0.5), inputs, llm=slow, trace=null_trace),
        timeout=10,
    )
    assert final["status"] == "failed"
    assert any("wall-clock guard" in e["message"] for e in null_trace.of_kind("error"))
    assert len(null_trace.of_kind("summary")) == 1


def load_ir_ledger() -> Any:
    from tests.graph.conftest import load_ledger

    return load_ledger("brief_a")


def test_finalize_after_a_cut_off_keeps_the_deployed_agent(
    make_settings: Any, null_trace: NullTrace, tmp_dirs: SimpleNamespace
) -> None:
    from builder.budget import Budget
    from builder.graph.context import BuilderContext

    ctx = BuilderContext(settings=make_settings(), llm=None, freya=None, trace=null_trace, budget=Budget(60))
    progress = {
        "slot": "s",
        "track": "compiled",
        "deploy": {
            "workflow_id": "wf_1",
            "agent_id": "ag_1",
            "version": 3,
            "node_count": 31,
            "tool_count": 5,
        },
        "exported": str(tmp_dirs.out / "s.exported.json"),
    }
    final = finalize_run(progress, ctx)
    assert final["status"] == "deployed" and final["workflow_id"] == "wf_1" and final["version"] == 3
    assert finalize_run(progress, ctx)["status"] == "deployed"
    assert len(null_trace.of_kind("summary")) == 1  # written exactly once


async def test_auto_track_without_proof_of_a_login_skips_the_agentic_fallback_and_says_why(
    make_ctx: Any,
    make_gateway: Any,
    make_freya: Any,
    invoke: Any,
    null_trace: NullTrace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A credentials file exists but the login answers "Not logged in": no doomed agentic session may start."""
    config_dir = tmp_path / "claude_cfg"
    config_dir.mkdir()
    (config_dir / ".credentials.json").write_text("{}", encoding="utf-8")
    env = {"CLAUDE_CONFIG_DIR": str(config_dir)}
    monkeypatch.setattr("builder.agentic.runner.default_env", lambda *a, **k: dict(env))
    monkeypatch.setattr("builder.agentic.runner.sdk_installed", lambda: True)
    monkeypatch.setattr("builder.llm.keys._sdk_installed", lambda: True)
    gateway = make_gateway(responders={"PolicyIR": lambda call: broken_ir()})
    freya = make_freya()
    state = await invoke(
        make_ctx(gateway, freya, builder_track="auto")
    )  # no agentic_runner: the real check runs
    assert state["limits"]["allow_agentic"] is False
    assert state["final"]["status"] != "deployed" and freya.calls == []
    assert state.get("agentic_attempts", 0) == 0
    skipped = [e for e in null_trace.of_kind("decision") if e["message"].startswith("agentic track skipped")]
    assert len(skipped) == 1 and "no usable credential" in skipped[0]["message"]


async def test_auto_track_with_a_verified_login_keeps_the_agentic_fallback_on(
    make_ctx: Any,
    make_gateway: Any,
    make_freya: Any,
    practice_ok: Any,
    invoke: Any,
    null_trace: NullTrace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "tok-" + "x" * 30
    monkeypatch.setattr(
        "builder.agentic.runner.default_env", lambda *a, **k: {"CLAUDE_CODE_OAUTH_TOKEN": token}
    )
    monkeypatch.setattr("builder.agentic.runner.sdk_installed", lambda: True)
    monkeypatch.setattr("builder.llm.keys._sdk_installed", lambda: True)
    state = await invoke(
        make_ctx(make_gateway(), make_freya(), practice_runner=practice_ok, builder_track="auto")
    )
    assert state["limits"]["allow_agentic"] is True and state["final"]["status"] == "deployed"
    assert not [e for e in null_trace.of_kind("decision") if e["message"].startswith("agentic track skipped")]


# ------------------------------------------------------------------------------------------------ A11 redaction sinks


async def test_a_secret_inside_an_error_text_never_reaches_the_result_file(
    make_ctx: Any, make_gateway: Any, make_freya: Any, invoke: Any, tmp_dirs: SimpleNamespace
) -> None:
    from builder.freya.client import FreyaError

    secret = "sk-test-secret-value-0123456789"  # one of make_ctx's secrets
    freya = make_freya()

    def leaky_save(*args: Any, **kwargs: Any) -> Any:
        raise FreyaError(f"save: HTTP 401 Unauthorized for key {secret} (synthetic)", action="save", status=401)

    freya.save = leaky_save
    state = await invoke(make_ctx(make_gateway(), freya))
    assert state["final"]["status"] == "failed"
    written = list(tmp_dirs.results.glob("*.build.json"))
    assert written, "the result file is written"
    text = written[0].read_text(encoding="utf-8")
    assert secret not in text and "***" in text
