"""AUD-39: lateness bands that depend on the product type.

A document may give one days-late table per product type ("cards: 10th business day; loans: 5th"). Before, the IR had
no way to say so: the validator rejected two rows with the same range (``BANDS_OVERLAP``) and one product type silently
got the other's deadline and attempts. Rows now carry optional ``product_types``; an untyped row applies to every type
and a typed row wins for its types. Generic test data only.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from builder.checks.code_sandbox import run_snippet
from builder.compiler import codegen
from builder.ingest.tool_api import ToolApi
from builder.policy.calendar import BusinessCalendar, deadline_for
from builder.policy.ir import PolicyIR, RuleLedger
from builder.understand.validate import validate_policy

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "policy"
NOW = "2026-10-14T11:00"


def _data() -> dict[str, Any]:
    return json.loads((FIXTURES / "brief_a_ir.json").read_text(encoding="utf-8"))


def _ledger() -> RuleLedger:
    return RuleLedger.model_validate(json.loads((FIXTURES / "brief_a_ledger.json").read_text(encoding="utf-8")))


def _scoped() -> dict[str, Any]:
    """The fixture's early/late table for every type plus card-only rows with other deadlines."""
    data = _data()
    early, late = copy.deepcopy(data["bands"][0]), copy.deepcopy(data["bands"][1])
    early.update(name="card_early", product_types=["credit_card"], deadline={"kind": "nth_business_day_after_today", "n": 3})
    late.update(name="card_late", product_types=["credit_card"], deadline={"kind": "nth_business_day_after_today", "n": 2})
    data["bands"] += [early, late]
    return data


def test_a_typed_row_wins_for_its_type_and_untyped_rows_keep_today_s_behaviour() -> None:
    ir = PolicyIR.model_validate(_scoped())
    assert ir.band_for(5, "credit_card").name == "card_early"
    assert ir.band_for(5, "personal_loan").name == "early"
    assert ir.band_for(5).name == "early"
    assert ir.nearest_band(90, "credit_card").name == "card_late"
    assert ir.nearest_band(90, "personal_loan").name == "late"
    assert ir.nearest_band(90).name == "late"


def test_same_ranges_for_disjoint_types_are_valid_but_overlaps_within_a_type_are_not(kit_tool_api: ToolApi) -> None:
    issues = validate_policy(PolicyIR.model_validate(_scoped()), _ledger(), kit_tool_api)
    assert not [i for i in issues if i.code in ("BANDS_OVERLAP", "BANDS_GAP", "PRODUCT_TYPE")]
    clash = _scoped()
    clash["bands"][3]["days_late_min"] = 5  # card_late now overlaps card_early
    codes = [(i.code, i.severity) for i in validate_policy(PolicyIR.model_validate(clash), _ledger(), kit_tool_api)]
    assert ("BANDS_OVERLAP", "error") in codes
    untyped = _data()
    untyped["bands"].append(copy.deepcopy(untyped["bands"][0]) | {"name": "again"})
    assert "BANDS_OVERLAP" in [i.code for i in validate_policy(PolicyIR.model_validate(untyped), _ledger(), kit_tool_api)]


def test_an_unknown_product_type_on_a_band_is_an_error(kit_tool_api: ToolApi) -> None:
    data = _scoped()
    data["bands"][2]["product_types"] = ["boat"]
    issues = validate_policy(PolicyIR.model_validate(data), _ledger(), kit_tool_api)
    assert any(i.code == "PRODUCT_TYPE" and i.path == "bands[2].product_types" and i.severity == "error" for i in issues)


def test_a_gap_in_a_type_s_table_is_a_warning(kit_tool_api: ToolApi) -> None:
    data = _data()
    data["bands"][0]["product_types"] = ["credit_card"]  # cards: 1-7 typed; every type: 8-45 -> no gap for cards
    data["bands"].append(data["bands"][0] | {"name": "card_far", "days_late_min": 60, "days_late_max": 90})
    issues = validate_policy(PolicyIR.model_validate(data), _ledger(), kit_tool_api)
    gaps = [i for i in issues if i.code == "BANDS_GAP"]
    assert gaps and all(i.severity == "warning" for i in gaps)
    assert any("46-59" in i.message and "credit_card" in i.message for i in gaps)


@pytest.mark.parametrize("ptype", ["credit_card", "personal_loan"])
def test_the_compiled_facts_use_the_row_of_the_product_s_type(ptype: str) -> None:
    import datetime as dt

    ir = PolicyIR.model_validate(_scoped())
    product = {"product_id": "p1", "type": ptype, "card_last4": "1234", "past_due": 100, "days_late": 5,
               "minimum_payment": 30, "instalment_amount": 30}
    state = run_snippet(codegen.setup_snippet(ir), {"now": NOW})
    facts = run_snippet(codegen.account_facts_snippet(ir), {**state, "products": [product]})
    band = ir.band_for(5, ptype)
    cal = BusinessCalendar.from_ir(ir.calendar)
    expected = deadline_for(band.deadline, dt.date.fromisoformat(NOW[:10]), cal)
    assert facts["p1_deadline"] == expected.isoformat()
    assert f"({band.name} band)" in facts["account_facts"]


def test_untyped_bands_compile_without_a_types_key() -> None:
    code = codegen.account_facts_snippet(PolicyIR.model_validate(_data()))
    assert "types" not in codegen._band_cfg(PolicyIR.model_validate(_data()))[0]
    assert "band_of(late, ptype)" in code
