"""The allowed promise dates the ``account_facts`` node writes and the promise guard that enforces them (audit C-06, C-07).

* an empty list of allowed dates (the deadline is earlier than the first day a promise can be recorded) must block, not
  let every date through;
* long deadlines are not cut at 120 days, a policy without a deadline is not cut at 45 days, and the prompt does not grow
  with the length of the deadline;
* a differential test compares the generated dates with the reference engine ``builder.policy.calendar.check_promise_date``
  over many calendars, deadline kinds and date rules.
"""

from __future__ import annotations

import datetime as dt
import itertools
import json
from pathlib import Path
from typing import Any

import pytest

from builder.checks.code_sandbox import run_hook, run_snippet
from builder.compiler import codegen
from builder.compiler.compile import compile_workflow
from builder.ingest.tool_api import ToolApi, load_tool_api
from builder.policy.calendar import BusinessCalendar, check_promise_date, deadline_for
from builder.policy.ir import DeadlineRule, PolicyIR, RuleLedger

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "fixtures" / "policy"
PRODUCT = {"product_id": "p1", "type": "credit_card", "card_last4": "1234", "past_due": 100, "days_late": 5,
           "minimum_payment": 30}


def _ir(name: str) -> PolicyIR:
    return PolicyIR.model_validate(json.loads((FIXTURES / f"{name}_ir.json").read_text(encoding="utf-8")))


def _ledger(name: str) -> RuleLedger:
    return RuleLedger.model_validate(json.loads((FIXTURES / f"{name}_ledger.json").read_text(encoding="utf-8")))


IR_A, LEDGER_A = _ir("brief_a"), _ledger("brief_a")


@pytest.fixture(scope="module")
def api() -> ToolApi:
    return load_tool_api(REPO / "tools" / "openapi.json")


def _with(deadline: DeadlineRule | None = None, **rules: Any) -> PolicyIR:
    ir = IR_A.model_copy(deep=True)
    if deadline is not None:
        for band in ir.bands:
            band.deadline = deadline
    ir.promise_dates = ir.promise_dates.model_copy(update=rules)
    return ir


class Agent:
    """The code-node snippets of one policy, run against one clock; the compiled promise guard is built on demand."""

    def __init__(self, ir: PolicyIR, api: ToolApi, now: str) -> None:
        self.ir, self.api, self.now = ir, api, now
        state = run_snippet(codegen.setup_snippet(ir), {"now": now})
        self.state = run_snippet(codegen.account_facts_snippet(ir), {**state, "products": [dict(PRODUCT)]})
        self._pre_hook = ""

    @property
    def pre_hook(self) -> str:
        if not self._pre_hook:
            workflow = compile_workflow(self.ir, self.api, LEDGER_A).workflow
            tool = next(t for t in workflow["nodes"]["negotiate"]["tools"] if t["function"]["name"] == "create_promise")
            self._pre_hook = tool["pre_process_code"]
        return self._pre_hook

    @property
    def allowed(self) -> list[str]:
        return [d for d in self.state.get("allowed_p1", "").split(",") if d]

    def guard(self, date: str, amount: Any = 100) -> dict[str, Any]:
        return run_hook(self.pre_hook, vars=self.state, config={
            "url": "https://bank.example/create_promise",
            "body": {"customer_id": "c", "product_id": "p1", "amount": amount, "date": date}})

    def blocked(self, date: str) -> bool:
        return self.guard(date)["config"]["url"] == ""


# ---- C-06: nothing allowed means nothing is recorded -----------------------------------------------------------------------


def test_an_empty_list_of_allowed_dates_blocks_the_promise(api: ToolApi) -> None:
    """End of month on a Saturday: the deadline rolls back to Friday 30 October, which is before today."""
    agent = Agent(_with(DeadlineRule(kind="end_of_month")), api, "2026-10-31T11:00")
    assert agent.state["p1_deadline"] == "2026-10-30" and agent.allowed == []
    assert agent.state["allowed_count_p1"] == "0"
    facts = next(line for line in agent.state["account_facts"].splitlines() if "Allowed promise dates" in line)
    assert facts == ("   Allowed promise dates: none - no day is left up to the deadline. Record NO promise for p1: "
                     "say that you cannot record a payment date for it.")
    out = agent.guard("2026-11-02")
    assert out["config"]["url"] == ""
    assert out["vars"]["promise_guard"] == "blocked: no promise date is left for p1"
    assert out["vars"]["promise_guard_say"] == (
        "I am sorry, I cannot record a payment date for your credit card ending 1234 in this call.")
    assert out["vars"]["promise_guard_blocks"] == "1"
    assert agent.blocked("2026-10-30") and agent.blocked("2026-10-31")


def test_without_the_allowed_date_facts_the_guard_still_does_not_block_on_dates(api: ToolApi) -> None:
    agent = Agent(_with(), api, "2026-10-14T11:00")
    facts = {k: v for k, v in agent.state.items() if not k.startswith(("allowed_", "until_"))}
    out = run_hook(agent.pre_hook, vars=facts, config={"url": "u", "body": {
        "product_id": "p1", "amount": 100, "date": "2030-01-01"}})
    assert out["config"]["url"] == "u" and out["vars"]["promise_guard"] == "ok"


# ---- C-07: the lists are not capped ----------------------------------------------------------------------------------------


def test_a_long_deadline_is_enumerated_to_the_end_and_the_prompt_stays_short(api: ToolApi) -> None:
    ir = _with(DeadlineRule(kind="nth_calendar_day_after_today", n=180))
    agent = Agent(ir, api, "2026-10-14T11:00")
    assert agent.state["p1_deadline"] == "2027-04-12" and agent.allowed[-1] == "2027-04-12"
    assert len(agent.allowed) > 120 and "until_p1" not in agent.state  # the list is exhaustive
    assert not agent.blocked("2027-03-15") and agent.blocked("2027-04-13") and agent.blocked("2027-03-13")  # Saturday
    block = agent.state["account_facts"]
    allowed_line = next(line for line in block.splitlines() if "Allowed promise dates" in line)
    assert allowed_line.startswith("   Allowed promise dates: every business day from 2026-10-14 (Wednesday) to "
                                   "2027-04-12 (Monday)")
    assert "2026-10-26" in allowed_line  # the bank holiday inside the range is named
    assert len(block) < 3500
    assert len(agent.state["date_table"].splitlines()) <= 75


def test_a_policy_without_a_deadline_accepts_every_business_day_not_just_the_next_45(api: ToolApi) -> None:
    agent = Agent(_with(not_after_deadline=False), api, "2026-10-14T11:00")
    assert not agent.blocked("2026-12-30") and not agent.blocked("2027-06-02")
    assert agent.blocked("2026-12-26")  # a Saturday inside the listed range
    assert agent.state["until_p1"] == "open"
    assert not agent.blocked("2029-03-14")  # far beyond the listed horizon: any ISO date is accepted
    assert "every business day from 2026-10-14 (Wednesday)" in agent.state["account_facts"]


def test_the_listing_stays_verbatim_for_a_short_range(api: ToolApi) -> None:
    agent = Agent(_with(), api, "2026-10-14T11:00")
    line = next(line for line in agent.state["account_facts"].splitlines() if "Allowed promise dates" in line)
    assert line.startswith("   Allowed promise dates: 2026-10-14 Wednesday, 2026-10-15 Thursday, 2026-10-16 Friday")
    assert line.endswith("2026-11-02 Monday.")


# ---- the differential test against the reference engine --------------------------------------------------------------------

KINDS = [
    DeadlineRule(kind="nth_business_day_after_today", n=12),
    DeadlineRule(kind="nth_business_day_after_today", n=1),
    DeadlineRule(kind="nth_calendar_day_after_today", n=1),
    DeadlineRule(kind="nth_calendar_day_after_today", n=30),
    DeadlineRule(kind="nth_calendar_day_after_today", n=180),
    DeadlineRule(kind="end_of_month"),
    DeadlineRule(kind="fixed_date", fixed=dt.date(2026, 11, 20)),
    DeadlineRule(kind="fixed_date", fixed=dt.date(2026, 10, 1)),  # already in the past
]
NOWS = ["2026-10-14T11:00", "2026-10-30T11:00", "2026-10-31T11:00", "2026-12-31T09:00", "2026-10-24T09:00",
        "2026-10-25T10:00"]


def _accepts(agent: Agent, day: dt.date) -> bool:
    """The rule the guard applies to a date, from the facts: the list, a count of zero, the open tail."""
    iso = day.isoformat()
    if agent.state.get("allowed_count_p1") == "0":
        return False
    allowed = agent.allowed
    if iso in allowed:
        return True
    tail = agent.state.get("until_p1", "")
    return bool(tail) and iso > allowed[-1] and (tail == "open" or iso <= tail)


@pytest.mark.parametrize("deadline", KINDS, ids=lambda d: f"{d.kind}-{d.n or d.fixed}")
def test_the_allowed_dates_are_the_reference_engines_dates(deadline: DeadlineRule, api: ToolApi) -> None:
    mismatches: list[str] = []
    for now, allow_today, business_only, not_after in itertools.product(NOWS, [True, False], [True, False], [True, False]):
        rules = {"allow_today": allow_today, "business_days_only": business_only, "not_after_deadline": not_after}
        ir = _with(deadline, **rules)
        agent = Agent(ir, api, now)
        today = dt.date.fromisoformat(now[:10])
        cal = BusinessCalendar.from_ir(ir.calendar)
        raw = deadline_for(deadline, today, cal)
        for offset in range(-2, 200):
            day = today + dt.timedelta(days=offset)
            reference = check_promise_date(day, today, cal, ir.promise_dates, raw)
            if reference.ok != _accepts(agent, day):
                mismatches.append(f"{now} {rules} {day} reference={reference.status} allowed={len(agent.allowed)}")
    assert mismatches == []


@pytest.mark.parametrize(("deadline", "now"), [
    (DeadlineRule(kind="end_of_month"), "2026-10-31T11:00"),
    (DeadlineRule(kind="nth_calendar_day_after_today", n=180), "2026-10-14T11:00"),
    (DeadlineRule(kind="fixed_date", fixed=dt.date(2026, 10, 1)), "2026-10-14T11:00"),
    (DeadlineRule(kind="nth_business_day_after_today", n=12), "2026-10-14T11:00"),
], ids=["empty", "long", "past", "kit"])
def test_the_promise_guard_agrees_with_the_reference_engine_day_by_day(deadline: DeadlineRule, now: str,
                                                                        api: ToolApi) -> None:
    ir = _with(deadline)
    agent = Agent(ir, api, now)
    today = dt.date.fromisoformat(now[:10])
    cal = BusinessCalendar.from_ir(ir.calendar)
    raw = deadline_for(deadline, today, cal)
    wrong = []
    for offset in range(-1, 70):
        day = today + dt.timedelta(days=offset)
        reference = check_promise_date(day, today, cal, ir.promise_dates, raw)
        if reference.ok == agent.blocked(day.isoformat()):
            wrong.append(f"{day}: reference={reference.status} blocked={agent.blocked(day.isoformat())}")
    assert wrong == []
