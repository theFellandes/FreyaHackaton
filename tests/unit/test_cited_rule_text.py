"""AUD-10: a cited rule whose condition or exception the section's fields cannot hold keeps its words.

Citing a rule in a section's ``rule_ids`` does not carry its text to the agent. The extraction prompt now asks the model
to ALSO list such a rule in ``residual_rules``; this pins the compiler side: a rule that is both cited and residual is
printed verbatim in the stage prompt it is scoped to (both profiles), and coverage still maps it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from builder.checks.coverage import coverage_report
from builder.compiler.compile import compile_workflow
from builder.compiler.context import uncovered_rules
from builder.ingest.tool_api import ToolApi
from builder.policy.ir import PolicyIR, ResidualRule, RuleEntry, RuleLedger
from builder.understand.validate import validate_policy

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "policy"
EXCEPTION = "If the customer names an amount and a day themselves after declining, record that promise anyway."


def _inputs() -> tuple[PolicyIR, RuleLedger]:
    ir = PolicyIR.model_validate(json.loads((FIXTURES / "brief_a_ir.json").read_text(encoding="utf-8")))
    ledger = RuleLedger.model_validate(json.loads((FIXTURES / "brief_a_ledger.json").read_text(encoding="utf-8")))
    rule = RuleEntry(id="X-EXC", source="memo.md", quote=EXCEPTION, category="intent",
                     statement="Record a promise the customer names after declining.", scope=["negotiation"])
    ledger = ledger.model_copy(update={"rules": [*ledger.rules, rule]})
    ir = ir.model_copy(deep=True)
    ir.intents[0].rule_ids.append("X-EXC")  # cited by an intent whose fields cannot say it
    return ir, ledger


def test_a_cited_rule_alone_is_not_printed_but_a_cited_residual_rule_is() -> None:
    ir, ledger = _inputs()
    assert ("X-EXC", EXCEPTION) not in uncovered_rules(ir, ledger, ["negotiation"])
    ir.residual_rules.append(ResidualRule(rule_id="X-EXC", text=EXCEPTION, scope=["negotiation"]))
    assert ("X-EXC", EXCEPTION) in uncovered_rules(ir, ledger, ["negotiation"])


@pytest.mark.parametrize("profile", ["staged", "lean"])
def test_a_cited_residual_rule_reaches_the_negotiation_prompt(profile: str, kit_tool_api: ToolApi) -> None:
    ir, ledger = _inputs()
    ir.residual_rules.append(ResidualRule(rule_id="X-EXC", text=EXCEPTION, scope=["negotiation"]))
    assert not [i for i in validate_policy(ir, ledger, kit_tool_api) if i.severity == "error"]
    compiled = compile_workflow(ir, kit_tool_api, ledger, None, profile)  # type: ignore[arg-type]
    assert EXCEPTION in compiled.workflow["nodes"]["negotiate"]["task_message"]
    report = coverage_report(ledger, compiled.rule_map, compiled.workflow)
    assert "X-EXC" in report.covered


def test_the_extraction_prompt_asks_for_unexpressed_conditions_in_residual_rules() -> None:
    text = (Path(__file__).resolve().parents[2] / "builder" / "prompts" / "extract" / "policy_ir.v1.j2").read_text(
        encoding="utf-8")
    assert "Citing a rule does not carry its words to the agent" in text
