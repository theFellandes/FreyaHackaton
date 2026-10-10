"""The wall-clock guard of ``build.py`` against blocking platform calls and self-tests that are cut off.

Regression tests for two defects of the pipeline audit:

* **blocked worker thread (P-03)** - the guard cancels the awaiting coroutine, but a platform call that is blocked in a
  worker thread used to keep ``asyncio.run`` (and so the final JSON line and the process exit) waiting, and could
  create the workflow *after* the build had already reported ``failed``. Platform calls now run in abandonable daemon
  threads, nothing new is started once the guard fired, and the summary reports what the in-flight call created;
* **lost self-tests (P-09)** - after a cut-off the closing summary used to drop the tests that had already run.

The graph is replaced by a short script of the real nodes; the platform and the harness are in-memory stand-ins.
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import builder.build as build
from builder.budget import Budget
from builder.freya.client import DeployInfo, FreyaClient
from builder.graph.nodes import deploy, self_test
from builder.graph.nodes._common import run_node
from builder.ingest.brief_loader import load_briefs
from builder.inputs import InputBundle
from builder.testing import FakeFreya
from builder.trace import NullTrace
from tests.unit.test_freya_client import EVENT_LLM, FakePlatform, make_kit

WF = {"initial_node": "end", "nodes": {"end": {"type": "end_call"}}}


class ScriptedGraph:
    """Runs the given ``(name, body)`` nodes in order, like the real graph would, and merges their updates."""

    def __init__(self, nodes: list[tuple[str, Any]], extra_state: dict[str, Any] | None = None) -> None:
        self.nodes = nodes
        self.extra_state = extra_state or {}

    async def ainvoke(self, start: dict[str, Any], *, context: Any, config: Any) -> dict[str, Any]:
        state: dict[str, Any] = {
            **start,
            "track": "compiled",
            "compiled": {"workflow": WF},
            **self.extra_state,
        }
        for name, body in self.nodes:
            update = await run_node(name, state, context, body)
            for key in ("self_tests", "errors", "notes"):
                state[key] = [*state.get(key, []), *update.pop(key, [])]
            state.update(update)
        return state


def inputs_for(tmp_path: Path, slot: str) -> InputBundle:
    return InputBundle([], tmp_path / "unused-tool-api.json", slot)


@pytest.fixture
def quick_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """No grace after the budget, and only a moment of patience with a call that is still in flight."""
    monkeypatch.setattr(build, "GUARD_GRACE_S", 0.0)
    monkeypatch.setattr(build, "INFLIGHT_GRACE_S", 0.05, raising=False)


# ------------------------------------------------------------------------------------------------ P-03


def test_the_guard_does_not_wait_for_a_blocked_platform_call(
    monkeypatch: pytest.MonkeyPatch,
    tmp_dirs: SimpleNamespace,
    tmp_path: Path,
    make_settings: Any,
    quick_guard: None,
) -> None:
    """A save that is stuck in a worker thread must not hold up the final summary or the process exit."""
    release = threading.Event()

    class BlockedFreya:
        def save(self, slot: str, workflow: dict[str, Any], state_file: Any = None) -> DeployInfo:
            release.wait(6.0)  # a slow urllib request
            return DeployInfo(
                workflow_id="wf_late", agent_id="ag_late", version=1, created=True, node_count=1, tool_count=0
            )

    monkeypatch.setattr(build, "build_graph", lambda: ScriptedGraph([("deploy", deploy._body)]))
    settings = make_settings(builder_track="compiled", self_test="none", build_time_budget_s=0.5)
    started = time.monotonic()
    try:
        final = asyncio.run(
            build.run_build(
                settings,
                inputs_for(tmp_path, "blocked"),
                llm=object(),
                freya=BlockedFreya(),
                trace=NullTrace(),
            )
        )
        elapsed = time.monotonic() - started
        assert elapsed < 3.0, f"the guard waited {elapsed:.1f} s for a worker thread"
        assert final["status"] == "failed" and final["workflow_id"] is None
        assert final["cut_off"] is True
    finally:
        release.set()


async def test_a_deploy_that_finishes_inside_the_grace_period_is_reported_not_orphaned(
    monkeypatch: pytest.MonkeyPatch,
    tmp_dirs: SimpleNamespace,
    tmp_path: Path,
    make_settings: Any,
    quick_guard: None,
) -> None:
    """The create call that was in flight when the guard fired is reported (unconfirmed), and no call follows it."""
    monkeypatch.setattr(build, "INFLIGHT_GRACE_S", 5.0)
    platform = FakePlatform(default_llm=EVENT_LLM)
    kit = make_kit(platform)
    real_save = kit.save_workflow

    def slow_save(slot: str, workflow: dict[str, Any], state_file: str = ".freya-agents.json") -> Any:
        time.sleep(0.5)  # the guard (0.2 s) fires while the workflow is being created
        return real_save(slot, workflow, state_file)

    kit.save_workflow = slow_save
    trace = NullTrace()
    client = FreyaClient(
        trace, kit=kit, state_file=tmp_path / "state.json", limiter=False, sleep=lambda s: None
    )
    monkeypatch.setattr(build, "build_graph", lambda: ScriptedGraph([("deploy", deploy._body)]))
    settings = make_settings(builder_track="compiled", self_test="none", build_time_budget_s=0.2)
    final = await build.run_build(
        settings, inputs_for(tmp_path, "grace"), llm=object(), freya=client, trace=trace
    )

    assert final["workflow_id"] == "wf_1" and final["agent_id"] == "ag_1"
    assert final["status"] == "deployed_unpublished" and final["cut_off"] is True
    assert (
        platform.count("POST", r"/api/v2/versions/workflow/[^/]+/publish") == 0
    )  # nothing followed the guard
    assert platform.count("GET", r"/api/v2/versions/workflow/[^/]+") == 0


# ------------------------------------------------------------------------------------------------ P-09


def test_a_cut_off_build_still_reports_the_tests_that_already_ran(
    monkeypatch: pytest.MonkeyPatch,
    tmp_dirs: SimpleNamespace,
    tmp_path: Path,
    make_settings: Any,
    brief_a_path: Path,
    quick_guard: None,
) -> None:
    """Static checks, platform validation and the smoke chat passed; the practice run is cut off by the guard."""
    release = threading.Event()
    trace = NullTrace()

    async def static_body(state: Any, ctx: Any) -> dict[str, Any]:
        ctx.trace.test("static_checks", True, "0 errors, 0 warnings")
        return {"lint": {"ok": True, "summary": "0 errors, 0 warnings"}}

    async def remote_body(state: Any, ctx: Any) -> dict[str, Any]:
        ctx.trace.test("remote_validate", True, "the platform accepted the workflow (dry run)")
        return {"remote_validation": {"ok": True, "kind": "ok", "problems": []}}

    def hanging_practice(agent_id: str, letter: str | None, **kwargs: Any) -> Any:
        release.wait(6.0)  # the practice harness is still running when the guard fires
        raise RuntimeError("released")

    graph = ScriptedGraph(
        [
            ("static_checks", static_body),
            ("remote_validate", remote_body),
            ("deploy", deploy._body),
            ("self_test", self_test._body),
        ],
        extra_state={"bundle": load_briefs([brief_a_path]).model_dump(mode="json")},
    )
    monkeypatch.setattr(build, "build_graph", lambda: graph)
    settings = make_settings(builder_track="compiled", self_test="auto", build_time_budget_s=1.5)
    try:
        final = asyncio.run(
            build.run_build(
                settings,
                inputs_for(tmp_path, "cutoff"),
                llm=object(),
                freya=FakeFreya(trace),
                trace=trace,
                budget=Budget(1680),
                practice_runner=hanging_practice,
            )
        )
    finally:
        release.set()

    assert final["status"] == "deployed" and final["cut_off"] is True
    assert final["tests"] == {"passed": 3, "total": 4}  # + the practice run that never finished
    block = "\n".join(trace.lines).split("BUILD SUMMARY")[-1]
    assert "  - PASS static_checks" in block and "  - PASS remote_validate" in block
    assert "  - PASS smoke" in block
    assert "  - FAIL practice: aborted" in block
    assert "tests        : 4 (3 passed, 1 failed)" in block
    assert "cut_off" in block  # the summary says the build was cut off by the guard


async def test_a_cut_off_redeploy_keeps_the_published_version_and_warns(
    monkeypatch: pytest.MonkeyPatch,
    tmp_dirs: SimpleNamespace,
    tmp_path: Path,
    make_settings: Any,
    quick_guard: None,
) -> None:
    """Version one is deployed and exported; the update is in flight when the guard fires and cannot be confirmed."""
    monkeypatch.setattr(build, "INFLIGHT_GRACE_S", 5.0)
    platform = FakePlatform(default_llm=EVENT_LLM)
    kit = make_kit(platform)
    real_save = kit.save_workflow
    saves = {"n": 0}

    def second_save_is_slow(
        slot: str, workflow: dict[str, Any], state_file: str = ".freya-agents.json"
    ) -> Any:
        saves["n"] += 1
        if saves["n"] == 2:
            time.sleep(1.0)
        return real_save(slot, workflow, state_file)

    kit.save_workflow = second_save_is_slow
    trace = NullTrace()
    client = FreyaClient(
        trace, kit=kit, state_file=tmp_path / "state.json", limiter=False, sleep=lambda s: None
    )
    graph = ScriptedGraph([("deploy", deploy._body), ("deploy", deploy._body)])
    monkeypatch.setattr(build, "build_graph", lambda: graph)
    settings = make_settings(builder_track="compiled", self_test="none", build_time_budget_s=0.6)
    final = await build.run_build(
        settings, inputs_for(tmp_path, "redeploy"), llm=object(), freya=client, trace=trace
    )

    assert final["status"] == "deployed" and final["version"] == 1 and final["cut_off"] is True
    assert final["export_ok"] is True  # the export of version one still describes what the record says
    assert any("update was in flight" in error for error in final["errors"])
