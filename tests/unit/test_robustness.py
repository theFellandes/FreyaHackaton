"""Offline robustness sampler checks useful boundaries and reports injected faults."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from builder import robustness
from builder.ingest.tool_api import FieldSpec, ToolApi, ToolContract, load_tool_api
from builder.policy.calendar import DateCheck, check_promise_date
from builder.policy.ir import Calendar, DeadlineRule, OutcomeRef, PolicyIR, PromiseDateRules
from builder.robustness import run_robustness


def _checks(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {check["name"]: check for check in report["checks"]}


def _api_for(ir: PolicyIR) -> ToolApi:
    refs = [ref for _, ref in ir.outcome_refs()]
    fields = {
        "close_call": {
            "outcome": sorted({ref.outcome for ref in refs}),
            "reason": sorted({ref.reason for ref in refs if ref.reason is not None}),
        },
        "verify_identity": {"field": [question.field for question in ir.identity.questions]},
        "transfer_to_agent": {"queue": ["people"]},
    }
    return ToolApi(
        tools={
            name: ToolContract(
                name=name,
                url=f"https://synthetic.invalid/{name}",
                request_fields=[FieldSpec(name=field, enum=values) for field, values in request.items()],
            )
            for name, request in fields.items()
        },
        sha256="synthetic-api",
    )


def test_generic_policy_has_hundreds_of_checks_and_json_ready_summary() -> None:
    ir = PolicyIR.example_minimal()
    before = ir.model_dump_json()

    report = run_robustness(ir, _api_for(ir))

    assert report["failed"] == 0
    assert report["passed"] >= 8
    assert report["cases_tested"] > 300
    assert report["cases_tested"] == sum(check["cases_tested"] for check in report["checks"])
    assert sum(report[key] for key in ("passed", "failed", "unchecked")) == len(report["checks"])
    assert json.loads(json.dumps(report)) == report
    assert ir.model_dump_json() == before
    assert "hidden benchmark" in " ".join(report["limitations"])


@pytest.mark.parametrize("fixture", ["brief_a_ir.json", "brief_ab_ir.json"])
def test_audited_policies_and_real_api_have_no_invariant_failures(repo_root: Path, fixture: str) -> None:
    ir = PolicyIR.model_validate_json((repo_root / "tests" / "fixtures" / "policy" / fixture).read_text())
    report = run_robustness(ir, load_tool_api(repo_root / "tools" / "openapi.json"))

    assert report["failed"] == 0, [check for check in report["checks"] if check["status"] == "fail"]
    assert report["cases_tested"] > 1000
    for holiday in ir.calendar.holidays[:3]:
        assert holiday.isoformat() in report["calendar_anchors"]


def test_custom_prose_and_absent_api_are_explicitly_unchecked() -> None:
    ir = PolicyIR.example_minimal()
    ir.bands[0].deadline = DeadlineRule(kind="custom", custom_rule_text="A date supplied by another process")
    ir.third_party.callback.allowed_days = "custom"

    report = run_robustness(ir)
    checks = _checks(report)

    assert report["failed"] == 0
    assert checks["deadlines.custom"]["status"] == "unchecked"
    assert checks["callbacks.slot_boundaries"]["cases_tested"] == 0
    assert checks["contracts.api"]["status"] == "unchecked"
    assert "promise_dates.boundaries_and_suggestions" not in checks


def test_large_deadline_counts_are_bounded_and_unchecked() -> None:
    ir = PolicyIR.example_minimal()
    ir.bands[0].deadline = DeadlineRule(kind="nth_business_day_after_today", n=10_000_000)

    report = run_robustness(ir)

    assert report["failed"] == 0
    assert _checks(report)["deadlines.search_bound"]["status"] == "unchecked"


def test_nonstandard_business_week_and_leap_month_rule_work() -> None:
    ir = PolicyIR.example_minimal()
    ir.calendar = Calendar(business_weekdays=[1, 2, 3, 4, 5], holidays=[date(2032, 2, 29)])
    ir.bands[0].deadline = DeadlineRule(kind="end_of_month")
    ir.promise_dates = PromiseDateRules(allow_today=False, non_business_day_shift="previous_business_day")

    report = run_robustness(ir)

    assert report["failed"] == 0, [check for check in report["checks"] if check["status"] == "fail"]
    assert "2032-02-29" in report["calendar_anchors"]


def test_midnight_callback_endpoint_is_supported() -> None:
    ir = PolicyIR.example_minimal()
    ir.third_party.callback.window_end = "24:00"

    report = run_robustness(ir)

    assert _checks(report)["callbacks.slot_boundaries"]["status"] == "pass"


def test_misclassified_dates_produce_named_bounded_counterexamples(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(robustness, "check_promise_date", lambda *args: DateCheck("ok"))

    report = run_robustness(PolicyIR.example_minimal())
    check = _checks(report)["promise_dates.boundaries_and_suggestions"]

    assert check["status"] == "fail"
    assert check["failures"] > len(check["counterexamples"])
    assert len(check["counterexamples"]) == 8
    assert {"today", "deadline", "proposed"} <= check["counterexamples"][0]["inputs"].keys()
    assert "expected" in check["counterexamples"][0]["error"]


def test_invalid_suggested_date_is_caught_independently(monkeypatch: pytest.MonkeyPatch) -> None:
    def invalid_suggestion(*args: Any) -> DateCheck:
        original = check_promise_date(*args)
        return DateCheck(original.status, date(2000, 1, 1)) if not original.ok else original

    monkeypatch.setattr(robustness, "check_promise_date", invalid_suggestion)
    check = _checks(run_robustness(PolicyIR.example_minimal()))["promise_dates.boundaries_and_suggestions"]

    assert check["status"] == "fail"
    assert all("itself invalid" in case["error"] for case in check["counterexamples"])


def test_helper_exceptions_are_counterexamples_and_other_checks_continue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken_amount(*args: Any) -> str:
        raise OverflowError("synthetic arithmetic fault")

    monkeypatch.setattr(robustness, "check_promise_amount", broken_amount)
    checks = _checks(run_robustness(PolicyIR.example_minimal()))

    assert checks["amounts.cent_boundaries"]["status"] == "fail"
    assert "OverflowError" in checks["amounts.cent_boundaries"]["counterexamples"][0]["error"]
    assert checks["callbacks.slot_boundaries"]["status"] == "pass"


def test_outcome_and_identity_values_are_checked_against_supplied_enums() -> None:
    ir = PolicyIR.example_minimal()
    api = _api_for(ir)
    api.tools["close_call"].field("outcome").enum = ["another-outcome"]
    api.tools["verify_identity"].field("field").enum = ["another-security-field"]

    checks = _checks(run_robustness(ir, api))

    assert checks["contracts.close_call.outcome"]["status"] == "fail"
    assert checks["contracts.verify_identity.field"]["status"] == "fail"


def test_renamed_api_field_mapping_is_unchecked_not_invented() -> None:
    ir = PolicyIR.example_minimal()
    api = _api_for(ir)
    api.tools["verify_identity"].request_fields[0].name = "security_question"

    report = run_robustness(ir, api)

    assert report["failed"] == 0
    assert _checks(report)["contracts.verify_identity.field"]["status"] == "unchecked"


def test_an_intent_referencing_an_absent_tool_is_a_counterexample() -> None:
    ir = PolicyIR.example_minimal()
    ir.intents[0].actions[0].tool = "absent-tool"

    check = _checks(run_robustness(ir, _api_for(ir)))["contracts.intent_tools"]

    assert check["status"] == "fail"
    assert check["counterexamples"][0]["inputs"]["tool"] == "absent-tool"


def test_outcome_names_can_change_without_changing_sampler_code() -> None:
    ir = PolicyIR.example_minimal()
    for _, model in ir.iter_models():
        if isinstance(model, OutcomeRef):
            model.outcome = f"renamed_{model.outcome}"
    for row in ir.closing.rows:
        row.outcome = f"renamed_{row.outcome}"

    report = run_robustness(ir, _api_for(ir))

    assert report["failed"] == 0
    assert _checks(report)["contracts.close_call.outcome"]["status"] == "pass"
