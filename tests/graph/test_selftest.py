"""The self-tests: planning by mode and time, the smoke chat, the walkthrough audit and the practice cases.

All collaborators are fakes (``FakeFreya``, ``FixtureGateway``, a practice runner stub); the checks themselves are
exercised with replies that are right and replies that leak a forbidden word, skip the opening or are empty.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from builder.budget import Budget
from builder.ingest.brief_loader import load_briefs
from builder.llm.errors import AllProvidersFailed
from builder.llm.providers import LLMRole
from builder.policy.ir import PolicyIR, RuleLedger
from builder.selftest import plan_self_tests
from builder.selftest.practice import run_practice_selftest
from builder.selftest.smoke import (
    call_variables_for_test,
    check_replies,
    coverage_of,
    forbidden_found,
    next_weekday_at_eleven,
    render_phrase,
    run_smoke,
)
from builder.selftest.walkthrough import rules_text, run_walkthrough, workflow_text
from builder.testing import FakeFreya, FixtureGateway
from builder.trace import NullTrace
from tests.graph.conftest import load_ir, load_ledger, opening_reply

# ------------------------------------------------------------------------------------------------ planning


@pytest.mark.parametrize(
    ("mode", "left_s", "has_ledger", "has_agent", "expected"),
    [
        ("none", 1600, True, True, []),
        ("auto", 1600, True, True, ["smoke", "walkthrough", "practice"]),
        ("auto", 500, True, True, ["smoke", "walkthrough"]),  # practice (780 s + reserve) no longer fits
        ("auto", 250, True, True, ["smoke"]),
        ("auto", 100, True, True, []),
        ("auto", 1600, False, True, ["smoke", "practice"]),  # no ledger on the agentic track: no walkthrough
        ("auto", 1600, True, False, ["walkthrough"]),  # nothing deployed: only the offline audit can run
        ("smoke", 10, True, True, ["smoke"]),  # an explicit mode is the operator's decision
        ("practice", 10, True, False, []),
        ("walkthrough", 10, False, True, []),
        ("bogus", 1600, True, True, []),
    ],
)
def test_plan_self_tests(
    mode: str, left_s: float, has_ledger: bool, has_agent: bool, expected: list[str], fake_clock: Any
) -> None:
    budget = Budget(1680, clock=fake_clock)
    fake_clock.advance(1680 - left_s)
    assert plan_self_tests(mode, budget, has_ledger=has_ledger, has_agent=has_agent) == expected


# ------------------------------------------------------------------------------------------------ smoke


def test_the_call_date_is_the_next_weekday_at_eleven() -> None:
    assert next_weekday_at_eleven(date(2026, 10, 14)) == "2026-10-15T11:00"  # Wednesday
    assert next_weekday_at_eleven(date(2026, 10, 9)) == "2026-10-12T11:00"  # Friday -> Monday
    assert next_weekday_at_eleven(date(2026, 10, 10)) == "2026-10-12T11:00"  # Saturday -> Monday


def test_smoke_variables_follow_the_declared_call_variables(ir_a: PolicyIR) -> None:
    variables = call_variables_for_test(ir_a, "2026-10-12T11:00")
    assert variables["customer_id"] == "c_selftest" and variables["now"] == "2026-10-12T11:00"
    assert set(variables) == set(ir_a.call_variable_names())
    assert all(variables[name] for name in variables)
    generic = call_variables_for_test(None, "2026-10-12T11:00")
    assert {"customer_id", "first_name", "last_name", "title", "now"} <= set(generic)


def test_phrase_rendering_and_fuzzy_matching() -> None:
    assert render_phrase(
        "Am I speaking with {title} {last_name}?", {"title": "Mr", "last_name": "Morgan"}
    ) == ("Am I speaking with Mr Morgan?")
    assert render_phrase("Hi {{first_name}} {unknown}", {"first_name": "Alex"}) == "Hi Alex {unknown}"
    assert (
        coverage_of("Hello, I am a digital assistant.", "hello i am a digital assistant calling you") == 1.0
    )
    assert coverage_of("alpha beta gamma delta", "alpha beta") == 0.5
    assert coverage_of("", "anything") == 1.0
    assert forbidden_found("This is about your Debt, not collectionsX", ["debt", "collections"]) == ["debt"]


def test_reply_checks(ir_a: PolicyIR) -> None:
    variables = call_variables_for_test(ir_a, "2026-10-12T11:00")
    good = opening_reply(ir_a)
    assert check_replies(ir_a, variables, [good, "I can only speak to the customer."]) == []
    assert check_replies(ir_a, variables, [good, ""]) == ["reply 2 is empty"]
    leaked = check_replies(ir_a, variables, [good, "I am calling from collections about your debt."])
    assert len(leaked) == 1 and "collections" in leaked[0] and "debt" in leaked[0]
    no_opening = check_replies(ir_a, variables, ["Yes?", "Hmm."])
    assert len(no_opening) == 2 and all("the opening lacks" in problem for problem in no_opening)
    assert (
        check_replies(None, variables, ["anything", "else"]) == []
    )  # no IR (agentic): only emptiness is checked


async def test_smoke_chat_passes_and_ends_the_call(ir_a: PolicyIR, null_trace: NullTrace) -> None:
    freya = FakeFreya(null_trace, opening=opening_reply(ir_a))
    freya.save("s", {"nodes": {}})
    result = await run_smoke(
        freya, freya.saved["s"]["workflow_id"], ir_a, null_trace, datetime(2026, 10, 14, 9, 0)
    )
    assert result["passed"] and result["name"] == "smoke"
    assert [a for a, _ in freya.calls[-3:]] == ["chat", "chat", "end_chat"]
    first, second = freya.chat_variables
    assert first["customer_id"] == "c_selftest" and first["now"] == "2026-10-15T11:00" and second is None
    assert len(result["details"]["turns"]) == 2


async def test_smoke_chat_fails_on_a_leak(ir_a: PolicyIR, null_trace: NullTrace) -> None:
    freya = FakeFreya(null_trace, opening=opening_reply(ir_a), replies=["This is about your debt."])
    freya.save("s", {"nodes": {}})
    result = await run_smoke(freya, freya.saved["s"]["workflow_id"], ir_a, null_trace)
    assert not result["passed"] and "forbidden before verification" in result["summary"]


async def test_smoke_chat_reports_platform_errors_instead_of_raising(
    ir_a: PolicyIR, null_trace: NullTrace
) -> None:
    freya = FakeFreya(null_trace)
    result = await run_smoke(freya, "wf_unknown", ir_a, null_trace)  # the fake platform answers 404
    assert not result["passed"] and "FreyaError" in result["summary"]
    assert "end_chat" not in [a for a, _ in freya.calls]  # no conversation was started


# ------------------------------------------------------------------------------------------------ walkthrough


def test_rules_text_lists_only_active_rules() -> None:
    ledger = load_ledger("brief_ab")
    text = rules_text(ledger)
    lines = text.splitlines()
    assert len(lines) == len(ledger.active_rules()) < len(ledger.rules)
    superseded = next(r.id for r in ledger.rules if r.superseded_by)
    assert not any(line.startswith(f"{superseded} |") for line in lines)
    assert all(line.count(" | ") >= 3 for line in lines)


def test_workflow_text_condenses_the_workflow(compiled_a_workflow: dict[str, Any]) -> None:
    text = workflow_text(compiled_a_workflow)
    assert text.startswith("SYSTEM PROMPT:") and "## node negotiate (conversation)" in text
    assert "TOOL create_promise:" in text and "TASK MESSAGE:" in text
    assert (
        "code step writes:" in text and "\\x7b" not in text
    )  # code is summarised, its data tables are not copied
    assert len(text) <= 48_000
    assert len(workflow_text(compiled_a_workflow, max_chars=2000)) <= 2000


async def test_walkthrough_reports_only_known_rules_and_fails_on_hard_findings(
    compiled_a_workflow: dict[str, Any], ledger_a: RuleLedger, null_trace: NullTrace
) -> None:
    issues = [
        {"rule_id": ledger_a.rules[0].id, "verdict": "missing", "where": "opening", "explanation": "absent"},
        {"rule_id": ledger_a.rules[1].id, "verdict": "unclear", "where": "identity", "explanation": "vague"},
        {"rule_id": "NOT-A-RULE", "verdict": "missing", "where": "x", "explanation": "invented"},
    ]
    gateway = FixtureGateway(ledger_a, load_ir("brief_a"), walkthrough_issues=issues, trace=null_trace)
    result = await run_walkthrough(
        gateway, ledger_a, compiled_a_workflow, {"A-1b": ["node:opening/task_message"]}, ["customer_id"], null_trace,
        deadline=5.0,
    )  # fmt: skip
    assert not result["passed"]
    assert [i["rule_id"] for i in result["details"]["issues"]] == [ledger_a.rules[0].id, ledger_a.rules[1].id]
    (call,) = gateway.calls
    assert call.role == LLMRole.FAST.value and call.deadline == 5.0
    assert "A-1b -> node:opening/task_message" in call.user and "customer_id" in call.user
    assert "SYSTEM PROMPT:" in call.user


async def test_walkthrough_with_only_unclear_findings_passes(
    compiled_a_workflow: dict[str, Any], ledger_a: RuleLedger, null_trace: NullTrace
) -> None:
    issues = [{"rule_id": ledger_a.rules[0].id, "verdict": "unclear", "where": "w", "explanation": "e"}]
    gateway = FixtureGateway(ledger_a, load_ir("brief_a"), walkthrough_issues=issues)
    result = await run_walkthrough(gateway, ledger_a, compiled_a_workflow, {}, [], null_trace)
    assert result["passed"] and "1 unclear" in result["summary"]


async def test_walkthrough_is_skipped_when_the_auditor_is_unavailable(
    compiled_a_workflow: dict[str, Any], ledger_a: RuleLedger, null_trace: NullTrace
) -> None:
    class Down:
        async def structured(self, **kwargs: Any) -> Any:
            raise AllProvidersFailed("walkthrough", [])

    result = await run_walkthrough(Down(), ledger_a, compiled_a_workflow, {}, [], null_trace)
    assert result["passed"] and result["details"]["skipped"]


# ------------------------------------------------------------------------------------------------ practice


def practice_result(passed: int, total: int, rows: list[dict[str, Any]]) -> Any:
    failed_ids = [r["id"] for r in rows]
    return SimpleNamespace(
        passed=passed, total=total, failed_ids=failed_ids, rows=rows, raw_path=Path("r.json")
    )


async def test_practice_runs_for_the_kit_brief_and_reports_failed_cases(
    brief_a_path: Path, null_trace: NullTrace
) -> None:
    seen: dict[str, Any] = {}

    def runner(agent_id: str, letter: str | None, **kwargs: Any) -> Any:
        seen.update(agent_id=agent_id, letter=letter, **kwargs)
        rows = [{"id": "PUB-05", "family": "Hardship", "pass": False, "failed": ["support offered twice"]}]
        return practice_result(12, 13, rows)

    result = await run_practice_selftest(
        FakeFreya(), "ag_1", load_briefs([brief_a_path]), Budget(1680), null_trace, runner
    )
    assert not result["passed"] and "12/13" in result["summary"] and "PUB-05" in result["summary"]
    assert result["details"]["failed_cases"] == [
        {"id": "PUB-05", "family": "Hardship", "failed": ["support offered twice"]}
    ]
    assert seen["agent_id"] == "ag_1" and seen["letter"] == "A" and 60 <= seen["timeout_s"] <= 1500


async def test_practice_is_skipped_for_other_briefs_agents_and_budgets(
    brief_a_path: Path, tmp_path: Path, null_trace: NullTrace
) -> None:
    other = tmp_path / "other-brief.md"
    other.write_text("# Some other bank\n\nOffer nothing. Call nobody.\n", encoding="utf-8")

    def never(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("must not run")

    unseen = await run_practice_selftest(
        FakeFreya(), "ag", load_briefs([other]), Budget(1680), null_trace, never
    )
    assert unseen["details"]["skipped"] and unseen["passed"]
    bundle = load_briefs([brief_a_path])
    no_agent = await run_practice_selftest(FakeFreya(), "", bundle, Budget(1680), null_trace, never)
    assert no_agent["details"]["skipped"]
    no_time = await run_practice_selftest(FakeFreya(), "ag", bundle, Budget(600), null_trace, never)
    assert no_time["details"]["skipped"] and "not enough time" in no_time["summary"]


async def test_practice_failure_is_a_failed_test_not_a_crash(
    brief_a_path: Path, null_trace: NullTrace
) -> None:
    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("harness exited with status 1")

    result = await run_practice_selftest(
        FakeFreya(), "ag", load_briefs([brief_a_path]), Budget(1680), null_trace, broken
    )
    assert not result["passed"] and "harness exited" in result["summary"]


# ------------------------------------------------------------------------------------------------ honest self-test records


def _report_gateway(ledger: RuleLedger, **report: Any) -> FixtureGateway:
    """A gateway whose audit answers with the given ``issues`` / ``checked`` (whatever the real model might say)."""
    from builder.selftest.walkthrough import WalkthroughReport

    return FixtureGateway(
        ledger,
        load_ir("brief_a"),
        responders={"WalkthroughReport": lambda call: WalkthroughReport.model_validate(report)},
    )


async def test_an_audit_that_examined_nothing_is_not_a_pass(
    compiled_a_workflow: dict[str, Any], ledger_a: RuleLedger, null_trace: NullTrace
) -> None:
    gateway = _report_gateway(ledger_a, issues=[], checked=0)
    result = await run_walkthrough(gateway, ledger_a, compiled_a_workflow, {}, [], null_trace)
    submitted = len(ledger_a.active_rules())
    assert not result["passed"] and result["details"]["inconclusive"] is True
    assert (
        "rules audited" not in result["summary"]
    )  # the old claim: "N rules audited" for N rules merely submitted
    assert f"{submitted} rules submitted" in result["summary"] and "0 examined" in result["summary"]
    assert result["details"]["issues"] == [] and not result["details"]["skipped"]


async def test_an_audit_that_examined_few_rules_is_inconclusive_even_when_it_found_something(
    compiled_a_workflow: dict[str, Any], ledger_a: RuleLedger, null_trace: NullTrace
) -> None:
    issue = {"rule_id": ledger_a.rules[0].id, "verdict": "unclear", "where": "w", "explanation": "e"}
    gateway = _report_gateway(ledger_a, issues=[issue], checked=3)
    result = await run_walkthrough(gateway, ledger_a, compiled_a_workflow, {}, [], null_trace)
    assert not result["passed"] and result["details"]["inconclusive"] is True
    assert [i["rule_id"] for i in result["details"]["issues"]] == [
        ledger_a.rules[0].id
    ]  # the finding is kept


async def test_a_complete_audit_reports_what_was_submitted_and_examined(
    compiled_a_workflow: dict[str, Any], ledger_a: RuleLedger, null_trace: NullTrace
) -> None:
    count = len(ledger_a.active_rules())
    gateway = _report_gateway(ledger_a, issues=[], checked=count)
    result = await run_walkthrough(gateway, ledger_a, compiled_a_workflow, {}, [], null_trace)
    assert result["passed"] and not result["details"].get("inconclusive")
    assert (
        f"{count} rules submitted, model reports {count} examined, 0 missing/contradicted"
        in result["summary"]
    )


async def test_findings_for_unknown_rule_ids_are_traced_not_dropped_silently(
    compiled_a_workflow: dict[str, Any], ledger_a: RuleLedger, null_trace: NullTrace
) -> None:
    count = len(ledger_a.active_rules())
    invented = {"rule_id": "R999", "verdict": "missing", "where": "x", "explanation": "invented"}
    gateway = _report_gateway(ledger_a, issues=[invented], checked=count)
    result = await run_walkthrough(gateway, ledger_a, compiled_a_workflow, {}, [], null_trace)
    assert result["details"]["unknown_rule_ids"] == ["R999"] and result["details"]["issues"] == []
    assert any("R999" in event["message"] for event in null_trace.events)


def test_the_rule_map_is_presented_as_an_unverified_claim() -> None:
    from builder.selftest.walkthrough import rule_map_text

    text = rule_map_text({"A-1": ["node:opening/task_message"], "A-2": []})
    assert "A-1 -> node:opening/task_message" in text and "A-2" not in text
    assert "unverified" in text.lower()


async def test_smoke_without_a_policy_says_that_only_reachability_was_checked(null_trace: NullTrace) -> None:
    freya = FakeFreya(null_trace, opening="blah", replies=("blah",))
    freya.save("s", {"nodes": {}})
    result = await run_smoke(
        freya, freya.saved["s"]["workflow_id"], None, null_trace, datetime(2026, 10, 14, 9, 0)
    )
    assert result["passed"] is True
    assert "opening and secrecy checks passed" not in result["summary"]
    assert "not checked" in result["summary"] and "no policy" in result["summary"]
    assert result["details"]["checks"] == ["replies non-empty"]


async def test_smoke_summary_lists_the_checks_that_really_ran(ir_a: PolicyIR, null_trace: NullTrace) -> None:
    freya = FakeFreya(null_trace, opening=opening_reply(ir_a))
    freya.save("s", {"nodes": {}})
    result = await run_smoke(
        freya, freya.saved["s"]["workflow_id"], ir_a, null_trace, datetime(2026, 10, 14, 9, 0)
    )
    assert result["passed"]
    forbidden = len([w for w in ir_a.forbidden_before_verification if w.strip()])
    assert f"forbidden words checked: {forbidden}" in result["summary"]
    assert "opening elements checked: 2" in result["summary"]


async def test_smoke_summary_admits_an_empty_policy(null_trace: NullTrace) -> None:
    ir = load_ir("brief_a").model_copy(deep=True)
    ir.forbidden_before_verification = []
    ir.phrases.opening_sentence = ""
    ir.phrases.right_party_question = ""
    freya = FakeFreya(null_trace, opening="hello", replies=("hello again",))
    freya.save("s", {"nodes": {}})
    result = await run_smoke(
        freya, freya.saved["s"]["workflow_id"], ir, null_trace, datetime(2026, 10, 14, 9, 0)
    )
    assert result["passed"] and "opening elements checked: 0" in result["summary"]
    assert "forbidden words checked: 0" in result["summary"]
