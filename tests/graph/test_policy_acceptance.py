"""Unresolved policy issues stay visible (CRITICAL-GAPS gap 3).

A build that goes on best effort after the policy repairs are used up - validator errors or warnings still in the final
IR, or the grounding-only exception of the policy guard - must not look like a clean pass: the final result lists every
unresolved issue (``policy_issues``), the notes say so in one line, the trace has a ``WARN`` line per issue and a count
in the FINAL block, and the test summary carries a failed ``policy_validation`` entry. The build itself still goes on
(no agent is a certain zero).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from builder.graph.nodes.finalize import collect_tests, finalize_run
from builder.guardrails import GuardReport
from builder.policy.ir import PolicyIR
from builder.trace import NullTrace
from tests.graph.conftest import load_ir

SECRET = "sk-test-secret-value-0123456789"


def broken_ir() -> PolicyIR:
    """A Brief A IR with a validator error (the stop outcome is not in the tool API) the model never repairs."""
    data = load_ir("brief_a").to_json_dict()
    data["persuasion"]["stop_outcome"] = {"outcome": "declined", "reason": "refused"}
    return PolicyIR.model_validate(data)


def warn_lines(trace: NullTrace) -> list[dict[str, Any]]:
    return trace.of_kind("warn")


async def test_unrepaired_validator_errors_are_reported_not_hidden(
    make_ctx: Any, make_gateway: Any, invoke: Any, null_trace: NullTrace, tmp_dirs: SimpleNamespace
) -> None:
    gateway = make_gateway(responders={"PolicyIR": lambda call: broken_ir()})
    state = await invoke(make_ctx(gateway))
    final = state["final"]
    assert state["policy_repairs"] == 2  # the repairs ran and could not clear it
    issues = final["policy_issues"]
    assert issues, "the unresolved validator error must be in the final result"
    assert any(issue["severity"] == "error" for issue in issues)
    assert all({"code", "message", "severity", "rule_ids"} <= set(issue) for issue in issues)
    # the test summary must not read as a clean pass
    names = {t["name"]: t for t in collect_tests(state)}
    assert names["policy_validation"]["passed"] is False
    assert final["tests"]["passed"] < final["tests"]["total"]
    # one WARN line per issue and a count in the FINAL block
    assert len(warn_lines(null_trace)) == len(issues)
    (summary,) = null_trace.of_kind("summary")
    assert "unresolved" in str(summary["data"]["extra"]["policy_issues"])
    assert any(line.startswith("policy_issues") for line in null_trace.lines)
    written = json.loads((tmp_dirs.results / "brief-a-test-run.build.json").read_text(encoding="utf-8"))
    assert written["policy_issues"] == issues
    assert any("unresolved" in note and "policy" in note for note in written["notes"])


@pytest.mark.parametrize("guard", ["ungrounded_date", "ungrounded_amount"])
async def test_best_effort_grounding_findings_are_listed_as_policy_issues(
    make_ctx: Any, make_gateway: Any, invoke: Any, monkeypatch: pytest.MonkeyPatch, null_trace: NullTrace, guard: str
) -> None:
    soft = GuardReport(stage="policy")
    soft.block(guard, "the IR states a value the guard cannot find in the briefs", "calendar.holidays[1]")
    monkeypatch.setattr("builder.graph.nodes.guard_policy.check_policy", lambda *a, **k: soft)
    state = await invoke(make_ctx(make_gateway()))
    final = state["final"]
    assert final["status"] == "dry_run"  # still built: no agent is a certain zero
    codes = [issue["code"] for issue in final["policy_issues"]]
    assert f"GUARD_{guard.upper()}" in codes
    assert any(t["name"] == "policy_validation" and not t["passed"] for t in collect_tests(state))
    assert any(e["data"].get("code") == f"GUARD_{guard.upper()}" for e in warn_lines(null_trace))


async def test_a_clean_policy_adds_no_policy_issue(make_ctx: Any, make_gateway: Any, invoke: Any) -> None:
    state = await invoke(make_ctx(make_gateway()))
    errors = [i for i in state["final"]["policy_issues"] if i["severity"] == "error"]
    assert errors == []
    assert all(t["name"] != "policy_validation" for t in collect_tests(state))


def test_policy_issues_in_the_result_file_are_redacted(make_settings: Any, tmp_dirs: SimpleNamespace) -> None:
    from builder.budget import Budget
    from builder.graph.context import BuilderContext

    trace = NullTrace(run_id="test-run", slot="s", secrets=(SECRET,))
    ctx = BuilderContext(
        settings=make_settings(), llm=None, freya=None, trace=trace, budget=Budget(100), secrets=(SECRET,)
    )
    state = {
        "slot": "s",
        "dry_run": True,
        "lint": {"ok": True, "summary": "0 errors, 0 warnings"},
        "compiled": {"workflow": {"nodes": {"a": {}}}},
        "policy_unresolved": [
            {"code": "X_ERR", "message": f"quotes {SECRET}", "severity": "error", "rule_ids": ["R1"], "path": ""}
        ],
    }
    final = finalize_run(state, ctx)
    assert final["status"] == "dry_run" and final["policy_issues"][0]["code"] == "X_ERR"
    text = (tmp_dirs.results / "s-test-run.build.json").read_text(encoding="utf-8")
    assert SECRET not in text and "X_ERR" in text
    assert all(SECRET not in line for line in trace.lines)


def test_the_agentic_track_reports_no_policy_issues_of_an_unused_ir() -> None:
    state = {
        "track": "agentic",
        "policy_unresolved": [{"code": "X", "message": "m", "severity": "error", "rule_ids": [], "path": ""}],
    }
    assert all(t["name"] != "policy_validation" for t in collect_tests(state))
