"""Tests for builder.checks.coverage: which ledger rules does the compiled workflow enforce?"""

from __future__ import annotations

import json
from pathlib import Path

from builder.checks.coverage import coverage_report
from builder.compiler.compile import compile_workflow
from builder.ingest.tool_api import ToolApi
from builder.policy.ir import PolicyIR, RuleEntry, RuleLedger

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "policy"


def _rule(rule_id: str, **kwargs: object) -> RuleEntry:
    return RuleEntry(id=rule_id, source="b.md", quote="q", category="other", statement="s", scope=["global"],
                     **kwargs)  # type: ignore[arg-type]


def test_covered_uncovered_and_superseded() -> None:
    ledger = RuleLedger(rules=[_rule("R1"), _rule("R2"), _rule("R3", superseded_by="R4"), _rule("R4")])
    report = coverage_report(ledger, {"R1": ["node:a"], "R4": ["code:b"], "R3": ["node:old"], "X9": ["node:z"]})
    assert list(report.covered) == ["R1", "R4"]
    assert report.uncovered == ["R2"]
    assert report.superseded == ["R3"]
    assert report.enforced_superseded == ["R3"]
    assert report.unknown == ["X9"]
    assert not report.ok
    assert report.ratio == 2 / 3
    assert "R2" in report.summary()


def test_empty_places_do_not_count_and_an_empty_ledger_is_fully_covered() -> None:
    ledger = RuleLedger(rules=[_rule("R1")])
    assert coverage_report(ledger, {"R1": []}).uncovered == ["R1"]
    empty = coverage_report(RuleLedger(rules=[]), {})
    assert empty.ok and empty.ratio == 1.0


def test_duplicate_places_are_collapsed() -> None:
    report = coverage_report(RuleLedger(rules=[_rule("R1")]), {"R1": ["a", "a", "b"]})
    assert report.covered["R1"] == ["a", "b"]


def test_every_active_rule_of_the_kit_briefs_is_enforced_somewhere(kit_tool_api: ToolApi) -> None:
    for name in ("brief_a", "brief_ab"):
        ir = PolicyIR.model_validate(json.loads((FIXTURES / f"{name}_ir.json").read_text(encoding="utf-8")))
        ledger = RuleLedger.model_validate(json.loads((FIXTURES / f"{name}_ledger.json").read_text(encoding="utf-8")))
        for profile in ("staged", "lean"):
            compiled = compile_workflow(ir, kit_tool_api, ledger, None, profile)  # type: ignore[arg-type]
            report = coverage_report(ledger, compiled.rule_map)
            assert report.ok, (name, profile, report.summary())
            assert report.unknown == []
            assert report.superseded == sorted(ledger.superseded_ids(), key=ledger.ids().index)
