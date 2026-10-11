"""The IRs the extraction model really produced for the accepted public builds compile cleanly (audit AUD-41).

``tests/fixtures/policy`` holds hand-written IRs; their shape differs from a model-made IR in hundreds of leaf paths. The
files in ``tests/fixtures/real_ir`` are copies of the accepted live outputs (``out/brief-a.{ir,ledger}.json`` and
``out/brief-a-brief-b.{ir,ledger}.json``, the runs that scored Brief A 8/10 and A+B 3/3). A compiler change that breaks on
a real model shape turns this test red even when every hand-written fixture still compiles.

The fixtures pin today's validator: a new, stricter validator rule that rejects them must decide between regenerating the
fixtures (a live extraction) and relaxing the rule.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from builder.checks.coverage import coverage_report
from builder.checks.lint import lint_workflow
from builder.compiler.compile import compile_workflow
from builder.graph.nodes._common import lint_call_variables
from builder.guardrails import check_workflow
from builder.ingest.tool_api import ToolApi, load_tool_api
from builder.policy.ir import PolicyIR, RuleLedger
from builder.understand.validate import errors_of, validate_policy

REPO = Path(__file__).resolve().parents[2]
REAL = REPO / "tests" / "fixtures" / "real_ir"
NAMES = ("brief_a", "brief_ab")


def _load(name: str) -> tuple[PolicyIR, RuleLedger]:
    ir = PolicyIR.model_validate(json.loads((REAL / f"{name}_ir.json").read_text(encoding="utf-8")))
    ledger = RuleLedger.model_validate(json.loads((REAL / f"{name}_ledger.json").read_text(encoding="utf-8")))
    return ir, ledger


@pytest.fixture(scope="module")
def api() -> ToolApi:
    return load_tool_api(REPO / "tools" / "openapi.json")


@pytest.mark.parametrize("name", NAMES)
def test_a_real_model_ir_passes_the_validator(api: ToolApi, name: str) -> None:
    ir, ledger = _load(name)
    assert [str(issue) for issue in errors_of(validate_policy(ir, ledger, api))] == []


@pytest.mark.parametrize("profile", ["staged", "lean"])
@pytest.mark.parametrize("name", NAMES)
def test_a_real_model_ir_compiles_lints_guards_and_maps_every_rule(api: ToolApi, name: str, profile: str) -> None:
    ir, ledger = _load(name)
    compiled = compile_workflow(ir, api, ledger, None, profile)  # type: ignore[arg-type]
    workflow = compiled.workflow
    assert [str(f) for f in lint_workflow(workflow, api, lint_call_variables(ir)).errors] == []
    guard = check_workflow(workflow, api, (), ir)
    assert not guard.blocked, guard.blocking_messages()
    coverage = coverage_report(ledger, compiled.rule_map, workflow)
    assert coverage.uncovered == [] and coverage.unknown == []
    if profile == "staged":
        assert coverage.dangling == {}
    assert all(coverage.covered.get(rule) for rule in coverage.dangling)
    again = compile_workflow(ir, api, ledger, None, profile)  # type: ignore[arg-type]
    assert json.dumps(again.workflow, sort_keys=True) == json.dumps(workflow, sort_keys=True)
