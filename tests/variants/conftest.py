"""Fixtures of the structural-variant tests: every variant goes through the real offline pipeline once per profile.

The pipeline is the compiled track's: the safe fixes of the extraction stage, ``validate_policy``, ``compile_workflow``,
``lint_workflow``, the workflow guard and the coverage report. Results are cached per (variant, profile) for the session.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from builder.checks.coverage import CoverageReport, coverage_report
from builder.checks.lint import LintReport, lint_workflow
from builder.compiler.compile import compile_workflow
from builder.graph.nodes._common import lint_call_variables
from builder.guardrails import check_workflow
from builder.guardrails.models import GuardReport
from builder.ingest.tool_api import ToolApi
from builder.policy.ir import PolicyIR, RuleLedger
from builder.understand.fixes import apply_safe_fixes
from builder.understand.validate import PolicyIssue, validate_policy
from tests.variants import variants as V

#: Every variant of the module, by name.
VARIANTS: dict[str, Callable[[], V.Variant]] = {
    "base": V.base,
    "business": lambda: V.business(V.base()),
    "renamed_contract": lambda: V.renamed_contract(V.base()),
    "renamed_clock": lambda: V.renamed_clock(V.base()),
    "clock_without_example": lambda: V.renamed_clock(V.base(), "current_time", ""),
    "percent_floor": lambda: V.percent_floor(V.base()),
    "three_products": lambda: V.three_products(V.base()),
    "other_calendar": lambda: V.other_calendar(V.base()),
    "extra_outcomes": lambda: V.extra_outcomes(V.base()),
    "decline_max_1": lambda: V.decline_max(V.base(), 1),
    "decline_max_3": lambda: V.decline_max(V.base(), 3),
    "callback_window": lambda: V.callback_window(V.base()),
    "amended_rule": lambda: V.amended_rule(V.base()),
    "string_status": lambda: V.string_status_contract(V.base()),
}


@dataclass
class Built:
    """One variant after the pipeline."""

    variant: V.Variant
    ir: PolicyIR
    ledger: RuleLedger
    api: ToolApi
    fixes: list[str]
    issues: list[PolicyIssue]
    workflow: dict[str, Any]
    rule_map: dict[str, list[str]]
    notes: list[str]
    lint: LintReport
    guard: GuardReport
    coverage: CoverageReport
    profile: str
    extra: dict[str, Any] = field(default_factory=dict)


def run_pipeline(variant: V.Variant, profile: str, directory: Path) -> Built:
    api = variant.tool_api(directory)
    ledger = variant.rules()
    ir, fixes = apply_safe_fixes(variant.policy(), ledger, api)
    issues = validate_policy(ir, ledger, api)
    compiled = compile_workflow(ir, api, ledger, None, profile)  # type: ignore[arg-type]
    workflow = compiled.workflow
    return Built(
        variant=variant, ir=ir, ledger=ledger, api=api, fixes=fixes, issues=issues, workflow=workflow,
        rule_map=compiled.rule_map, notes=compiled.notes,
        lint=lint_workflow(workflow, api, lint_call_variables(ir)),
        guard=check_workflow(workflow, api, (), ir),
        coverage=coverage_report(ledger, compiled.rule_map, workflow),
        profile=profile,
    )


@pytest.fixture(scope="session")
def pipeline(tmp_path_factory: pytest.TempPathFactory) -> Callable[[str, str], Built]:
    """``pipeline(name, profile)`` -> the cached :class:`Built` of that variant."""
    cache: dict[tuple[str, str], Built] = {}
    directory = tmp_path_factory.mktemp("variant-apis")

    def get(name: str, profile: str = "staged") -> Built:
        key = (name, profile)
        if key not in cache:
            cache[key] = run_pipeline(VARIANTS[name](), profile, directory)
        return cache[key]

    return get
