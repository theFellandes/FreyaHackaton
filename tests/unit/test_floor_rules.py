"""AUD-10: a floor the documents state as a computation (a percentage of the past-due amount, a fixed amount, or the
larger of those and the product's floor field) reaches the account facts and the hard promise guard.

Before, the IR could hold a floor only as a product field: a "25 % of the past-due amount" rule was reported as covered,
the facts said "only the full past-due amount", and the guard blocked every legal partial amount. Generic test data only.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from builder.checks.code_sandbox import run_hook, run_snippet
from builder.compiler import codegen
from builder.compiler.compile import compile_workflow
from builder.ingest.tool_api import load_tool_api
from builder.policy.ir import PolicyIR, RuleEntry, RuleLedger
from builder.understand.validate import validate_policy

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "fixtures" / "policy"
API = load_tool_api(REPO / "tools" / "openapi.json")
URL = "https://bank.example/create_promise"


def _data(name: str = "brief_ab") -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}_ir.json").read_text(encoding="utf-8"))


def _ledger(name: str = "brief_ab") -> RuleLedger:
    return RuleLedger.model_validate(json.loads((FIXTURES / f"{name}_ledger.json").read_text(encoding="utf-8")))


def _ir_with_rule(rule: dict[str, Any], *, keep_field: bool = False) -> PolicyIR:
    data = _data()
    if not keep_field:
        data["amounts"]["floor_field_by_type"].pop("credit_card", None)
    data["amounts"]["floor_rule_by_type"] = {"credit_card": rule}
    return PolicyIR.model_validate(data)


def _facts(ir: PolicyIR, product: dict[str, Any]) -> dict[str, Any]:
    state = run_snippet(codegen.setup_snippet(ir), {"now": "2026-10-14T11:00"})
    return run_snippet(codegen.account_facts_snippet(ir), {**state, "products": [dict(product)]})


CARD = {"product_id": "p1", "type": "credit_card", "card_last4": "1234", "past_due": 400, "days_late": 5,
        "minimum_payment": 30}


def _guard(ir: PolicyIR, facts: dict[str, Any], amount: Any, profile: str = "staged") -> dict[str, Any]:
    workflow = compile_workflow(ir, API, _ledger(), profile=profile).workflow
    tool = next(t for t in workflow["nodes"]["negotiate"]["tools"] if t["function"]["name"] == "create_promise")
    pre = run_hook(tool["pre_process_code"], vars=dict(facts), config={
        "url": URL, "body": {"customer_id": "c", "product_id": "p1", "amount": amount, "date": "2026-10-16"}})
    return pre


def test_a_percent_floor_reaches_the_facts_and_the_promise_guard() -> None:
    ir = _ir_with_rule({"percent_of_amount": 25})
    facts = _facts(ir, CARD)
    assert facts["p1_floor"] == "100.00"
    assert "Floor (minimum amount): 100.00" in facts["account_facts"]
    assert "only the full past-due amount" not in facts["account_facts"]
    ok = _guard(ir, facts, 160)
    assert ok["config"]["url"] == URL and ok["vars"]["promise_guard"] == "ok"
    low = _guard(ir, facts, 99.99)
    assert low["config"]["url"] != URL and low["vars"]["promise_guard"].startswith("blocked:")


@pytest.mark.parametrize(("rule", "floor"), [
    ({"percent_of_amount": 12.5}, "50.00"),
    ({"percent_of_amount": 10, "fixed": 50}, "50.00"),           # the larger of 40.00 and 50.00
    ({"percent_of_amount": 10, "fixed": 50, "combine": "min"}, "40.00"),
    ({"fixed": 25, "label": "minimum payment"}, "25.00"),
    ({"percent_of_amount": 33.333, "rounding": "down"}, "133.33"),
    ({"percent_of_amount": 33.333, "rounding": "up"}, "133.34"),
])
def test_floor_rule_arithmetic(rule: dict[str, Any], floor: str) -> None:
    assert _facts(_ir_with_rule(rule), CARD)["p1_floor"] == floor


def test_a_floor_rule_combines_with_the_floor_field() -> None:
    """"The minimum payment or 10 % of the past-due amount, whichever is higher": field 30.00 vs 40.00."""
    ir = _ir_with_rule({"percent_of_amount": 10}, keep_field=True)
    facts = _facts(ir, CARD)
    assert facts["p1_floor"] == "40.00"
    assert "Floor (minimum payment): 40.00" in facts["account_facts"]


def test_without_floor_rules_the_generated_code_is_unchanged() -> None:
    ir = PolicyIR.model_validate(_data())
    code = codegen.account_facts_snippet(ir)
    assert "floor_rules" not in code and "frule" not in code and "__FLOOR_RULE__" not in code


def test_invalid_and_missing_floor_rules_are_validation_errors() -> None:
    bad = _data()
    bad["amounts"]["floor_rule_by_type"] = {"credit_card": {"label": "x"}, "boat": {"percent_of_amount": 10}}
    issues = validate_policy(PolicyIR.model_validate(bad), _ledger(), API)
    found = {(i.code, i.severity) for i in issues}
    assert ("FLOOR_RULE_INVALID", "error") in found and ("PRODUCT_TYPE", "error") in found


def test_a_cited_percentage_minimum_without_a_floor_rule_is_an_error() -> None:
    data = _data()
    ledger = _ledger()
    rule = RuleEntry(id="X-FLOOR", source="memo.md", quote="For card products the minimum acceptable amount is 25% of "
                     "the past-due amount.", category="amount", statement="Accept at least 25% of the past-due amount.",
                     scope=["negotiation"])
    ledger = ledger.model_copy(update={"rules": [*ledger.rules, rule]})
    data["amounts"]["rule_ids"].append("X-FLOOR")
    issues = [i for i in validate_policy(PolicyIR.model_validate(data), ledger, API) if i.code == "FLOOR_RULE_UNEXPRESSED"]
    assert len(issues) == 1 and issues[0].severity == "error" and issues[0].rule_ids == ["X-FLOOR"]
    fixed = copy.deepcopy(data)
    fixed["amounts"]["floor_rule_by_type"] = {"credit_card": {"percent_of_amount": 25}}
    assert not [i for i in validate_policy(PolicyIR.model_validate(fixed), ledger, API)
                if i.code == "FLOOR_RULE_UNEXPRESSED"]


@pytest.mark.parametrize("quote", [
    "For a credit card the floor is 25% of past_due, rounded down to the cent.",
    "The lowest amount we accept is 25 percent of `past_due`.",
    "Accept no less than 25% of the pastdue amount.",
])
def test_a_percentage_floor_written_with_the_field_name_is_an_error_too(quote: str) -> None:
    """Briefs name the account field (`past_due`) as often as the words; the tripwire must read both spellings."""
    data = _data()
    rule = RuleEntry(id="X-FLOOR", source="memo.md", quote=quote, category="amount", statement=quote,
                     scope=["negotiation"])
    ledger = _ledger().model_copy(update={"rules": [*_ledger().rules, rule]})
    data["amounts"]["rule_ids"].append("X-FLOOR")
    issues = [i for i in validate_policy(PolicyIR.model_validate(data), ledger, API) if i.code == "FLOOR_RULE_UNEXPRESSED"]
    assert len(issues) == 1 and issues[0].rule_ids == ["X-FLOOR"]


@pytest.mark.parametrize("name", ["brief_a", "brief_ab"])
def test_the_kit_fixtures_raise_no_floor_rule_issue(name: str) -> None:
    issues = validate_policy(PolicyIR.model_validate(_data(name)), _ledger(name), API)
    assert not [i for i in issues if i.code.startswith("FLOOR_RULE")]


def test_a_percentage_of_something_other_than_the_past_due_amount_is_not_a_floor_rule() -> None:
    data = _data()
    rule = RuleEntry(id="X-PCT", source="memo.md", quote="The minimum payment is 5% of the card balance; a fee of 2% "
                     "applies to late payments.", category="amount", statement="s", scope=["negotiation"])
    ledger = _ledger().model_copy(update={"rules": [*_ledger().rules, rule]})
    data["amounts"]["rule_ids"].append("X-PCT")
    assert not [i for i in validate_policy(PolicyIR.model_validate(data), ledger, API) if i.code.startswith("FLOOR_RULE")]
