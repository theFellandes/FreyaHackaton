"""Bounded, offline checks of supplied policy data and deterministic helpers.

This is an invariant sampler, not the hidden benchmark or a conversation judge.
Every outcome, reason, queue, identity field and holiday comes from the supplied
IR/API. Synthetic money and dates only exercise arithmetic boundaries. No files,
models, mock-bank sessions, or network endpoints are used.
"""

from __future__ import annotations

import calendar
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from itertools import combinations
from typing import Any

from builder.ingest.tool_api import ToolApi
from builder.policy.calendar import (
    BusinessCalendar,
    check_callback,
    check_promise_date,
    deadline_for,
)
from builder.policy.ir import DeadlineRule, OutcomeRef, PolicyIR
from builder.policy.money import check_promise_amount, fmt_amount, to_money
from builder.policy.outcomes import (
    STRUCTURAL_ORDER,
    CallSituation,
    NoteRecord,
    PromiseRecord,
    compute_outcome,
)

_MAX_ANCHORS = 24
_MAX_BANDS = 16
_MAX_OUTCOME_KEYS = 16
_MAX_COUNTEREXAMPLES = 8
_MAX_DEADLINE_DAYS = 366
_CENT = Decimal("0.01")


@dataclass
class _Check:
    name: str
    cases_tested: int = 0
    failures: int = 0
    counterexamples: list[dict[str, Any]] = field(default_factory=list)
    unchecked: list[str] = field(default_factory=list)


class _Suite:
    def __init__(self) -> None:
        self.checks: dict[str, _Check] = {}

    def test(self, name: str, inputs: dict[str, Any], operation: Callable[[], str | None]) -> None:
        check = self.checks.setdefault(name, _Check(name))
        check.cases_tested += 1
        try:
            error = operation()
        except Exception as exc:  # A robustness run reports a helper crash instead of crashing itself.
            error = f"{type(exc).__name__}: {exc}"
        if error is not None:
            check.failures += 1
            if len(check.counterexamples) < _MAX_COUNTEREXAMPLES:
                check.counterexamples.append({"inputs": inputs, "error": error})

    def skip(self, name: str, reason: str) -> None:
        self.checks.setdefault(name, _Check(name)).unchecked.append(reason)

    def result(self, anchors: list[date]) -> dict[str, Any]:
        checks = []
        for check in self.checks.values():
            status = "fail" if check.failures else "unchecked" if check.unchecked else "pass"
            checks.append(
                {
                    "name": check.name,
                    "status": status,
                    "cases_tested": check.cases_tested,
                    "failures": check.failures,
                    "counterexamples": check.counterexamples,
                    "unchecked": check.unchecked,
                }
            )
        return {
            "kind": "offline invariant sampling",
            "passed": sum(check["status"] == "pass" for check in checks),
            "failed": sum(check["status"] == "fail" for check in checks),
            "unchecked": sum(check["status"] == "unchecked" for check in checks),
            "cases_tested": sum(check["cases_tested"] for check in checks),
            "calendar_anchors": [anchor.isoformat() for anchor in anchors],
            "checks": checks,
            "limitations": [
                "Cases tested counts invariant evaluations, not customers or hidden benchmark cases.",
                "No workflow execution, transcript policy/honesty judging, model behavior, or tool side effects.",
                "Custom deadline/callback prose and renamed semantic API fields need additional interpretation.",
                "Sampling is bounded; a passing report does not establish exhaustive coverage.",
            ],
        }


def _near(day: date) -> list[date]:
    result = []
    for offset in (-1, 0, 1):
        with suppress(OverflowError):
            result.append(day + timedelta(days=offset))
    return result


def _anchors(ir: PolicyIR) -> list[date]:
    # A synthetic Monday-Sunday week, leap-day boundary, and year boundary.
    days = [date(2032, 1, 5) + timedelta(days=i) for i in range(7)]
    days.extend([date(2032, 2, 28), date(2032, 2, 29), date(2032, 12, 31), date(2033, 1, 1)])
    for holiday in [*ir.calendar.holidays[:3], *ir.calendar.holidays[-1:]]:
        days.extend(_near(holiday))
    for band in ir.bands[:_MAX_BANDS]:
        if band.deadline.fixed is not None:
            days.extend(_near(band.deadline.fixed))
    return list(dict.fromkeys(days))[:_MAX_ANCHORS]


def _business_days(cal: BusinessCalendar, start: date, end: date) -> int:
    low, high = sorted([start, end])
    count = 0
    while low < high:
        low += timedelta(days=1)
        count += cal.is_business_day(low)
    return count if end >= start else -count


def _step(cal: BusinessCalendar, start: date, count: int) -> date:
    day = start
    direction = 1 if count > 0 else -1
    remaining = abs(count)
    for _ in range(4096):
        if not remaining:
            return day
        day += timedelta(days=direction)
        if cal.is_business_day(day):
            remaining -= 1
    raise ValueError("No usable business-day result within the sampler's 4096-day search bound")


def _calendar_checks(suite: _Suite, cal: BusinessCalendar, anchors: list[date]) -> None:
    for start in anchors:
        for count in (-3, -1, 0, 1, 3):

            def check(start: date = start, count: int = count) -> str | None:
                expected = _step(cal, start, count)
                actual = cal.add_business_days(start, count)
                if actual != expected:
                    return f"Business-day stepping returned {actual}; expected {expected}"
                interval = cal.business_days_between(start, actual)
                expected_interval = _business_days(cal, start, actual)
                if interval != expected_interval:
                    return f"Signed interval returned {interval}; expected {expected_interval}"
                if (count >= 0 or cal.is_business_day(start)) and interval != count:
                    return f"Applicable step/interval inverse returned {interval}; expected {count}"
                return None

            suite.test("calendar.steps_and_intervals", {"start": str(start), "business_days": count}, check)


def _date_status(ir: PolicyIR, cal: BusinessCalendar, proposed: date, today: date, deadline: date) -> str:
    rules = ir.promise_dates
    if proposed < today or (proposed == today and not rules.allow_today):
        return "before_today"
    if rules.not_after_deadline and proposed > deadline:
        return "after_deadline"
    if rules.business_days_only and not cal.is_business_day(proposed):
        return "not_business_day"
    return "ok"


def _promise_dates(suite: _Suite, ir: PolicyIR, cal: BusinessCalendar, today: date, deadline: date) -> None:
    for proposed in dict.fromkeys([*_near(today), *_near(deadline), _step(cal, today, 1)]):

        def check(proposed: date = proposed) -> str | None:
            actual = check_promise_date(proposed, today, cal, ir.promise_dates, deadline)
            expected = _date_status(ir, cal, proposed, today, deadline)
            if actual.status != expected:
                return f"Date status {actual.status}; expected {expected}"
            if actual.suggestion is not None:
                suggestion_status = _date_status(ir, cal, actual.suggestion, today, deadline)
                if suggestion_status != "ok":
                    return f"Suggested {actual.suggestion} is itself invalid: {suggestion_status}"
            return None

        suite.test(
            "promise_dates.boundaries_and_suggestions",
            {
                "today": str(today),
                "deadline": str(deadline),
                "proposed": str(proposed),
            },
            check,
        )


def _deadline_checks(suite: _Suite, ir: PolicyIR, cal: BusinessCalendar, anchors: list[date]) -> None:
    if len(ir.bands) > _MAX_BANDS:
        suite.skip("sampling.band_limit", f"Only the first {_MAX_BANDS} lateness bands were sampled")
    if not ir.bands:
        suite.skip("deadlines.rules", "The supplied policy has no lateness bands")
    for band in ir.bands[:_MAX_BANDS]:
        rule = band.deadline
        if rule.kind == "custom":
            suite.skip("deadlines.custom", f"Band {band.name}: custom prose cannot be computed offline")
            continue
        if rule.n is not None and rule.n > _MAX_DEADLINE_DAYS:
            suite.skip("deadlines.search_bound", f"Band {band.name}: n exceeds {_MAX_DEADLINE_DAYS}")
            continue
        for today in anchors:

            def check(today: date = today, rule: DeadlineRule = rule) -> str | None:
                actual = deadline_for(rule, today, cal)
                if rule.kind == "nth_business_day_after_today":
                    expected = _step(cal, today, int(rule.n or 0))
                elif rule.kind == "nth_calendar_day_after_today":
                    expected = today + timedelta(days=int(rule.n or 0))
                elif rule.kind == "end_of_month":
                    expected = today.replace(day=calendar.monthrange(today.year, today.month)[1])
                else:
                    expected = rule.fixed
                if actual != expected:
                    return f"Deadline {actual}; expected {expected}"
                _promise_dates(suite, ir, cal, today, actual)
                return None

            suite.test("deadlines.rules", {"band": band.name, "today": str(today), "rule": rule.kind}, check)


def _amount_checks(suite: _Suite, ir: PolicyIR) -> None:
    for total in (Decimal("0.01"), Decimal("1.00"), Decimal("123.46")):
        for floor in (None, Decimal("0.00"), total / 2, total, total + _CENT):
            lowest = min(floor.quantize(_CENT, rounding=ROUND_HALF_UP), total) if floor is not None else total
            values = {-_CENT, Decimal(0), _CENT, lowest - _CENT, lowest, lowest + _CENT, total, total + _CENT}
            for amount in sorted(values):

                def check(
                    amount: Decimal = amount,
                    total: Decimal = total,
                    lowest: Decimal = lowest,
                    floor: Decimal | None = floor,
                ) -> str | None:
                    if amount > total:
                        expected = "above_full"
                    elif amount <= 0 or amount < lowest:
                        expected = "below_floor"
                    elif amount in (lowest, total) or ir.amounts.accept_between_floor_and_full:
                        expected = "ok"
                    else:
                        expected = "not_allowed"
                    actual = check_promise_amount(amount, total, floor, ir.amounts)
                    return None if actual == expected else f"Amount status {actual}; expected {expected}"

                suite.test(
                    "amounts.cent_boundaries",
                    {
                        "full": str(total),
                        "floor": str(floor) if floor is not None else None,
                        "amount": str(amount),
                    },
                    check,
                )
    for value in ("1234.56", "$1,234.56", "1.234,56", "1234,56"):
        suite.test(
            "amounts.localized_parsing",
            {"value": value},
            lambda value=value: (
                None if fmt_amount(to_money(value)) == "1234.56" else "Locale parsing changed value"
            ),
        )
    for value in (True, "not an amount", "", float("nan"), float("inf")):

        def reject(value: Any = value) -> str | None:
            try:
                to_money(value)
            except ValueError:
                return None
            return "An invalid amount was accepted"

        suite.test("amounts.invalid_inputs", {"value": repr(value)}, reject)


def _callback_checks(suite: _Suite, ir: PolicyIR, cal: BusinessCalendar, anchors: list[date]) -> None:
    rule = ir.third_party.callback
    if rule is None or rule.allowed_days == "custom":
        suite.skip("callbacks.slot_boundaries", "No computable callback rule is supplied")
        return
    start = int(rule.window_start[:2]) * 60 + int(rule.window_start[3:])
    end = int(rule.window_end[:2]) * 60 + int(rule.window_end[3:])
    minutes = sorted(
        {0, 12 * 60, 1439, start - 1, start, start + 1, end - 1, end, end + 1} & set(range(1440))
    )
    for today in anchors:
        now = datetime.combine(today, time(12))
        try:
            next_business = _step(cal, today, 1)
            days = list(dict.fromkeys([*_near(today), next_business, *_near(next_business)]))
        except (ValueError, OverflowError) as exc:
            suite.skip("callbacks.search_bound", f"Anchor {today}: {exc}")
            continue
        for day in days:
            for minute in minutes:

                def check(
                    day: date = day,
                    minute: int = minute,
                    now: datetime = now,
                    next_business: date = next_business,
                ) -> str | None:
                    if rule.allowed_days == "any_business_day":
                        day_ok = day >= now.date() and cal.is_business_day(day)
                    elif rule.allowed_days == "next_business_day":
                        day_ok = day == next_business
                    else:
                        day_ok = day in (now.date(), next_business)
                    in_window = start <= minute <= end if rule.inclusive else start <= minute < end
                    slot = datetime.combine(day, time(minute // 60, minute % 60))
                    expected = (
                        "not_allowed_day"
                        if not day_ok
                        else "outside_window"
                        if not in_window
                        else ("not_after_now" if rule.must_be_after_now and slot <= now else "ok")
                    )
                    actual = check_callback(day, f"{minute // 60:02}:{minute % 60:02}", now, cal, rule).status
                    return None if actual == expected else f"Callback status {actual}; expected {expected}"

                suite.test(
                    "callbacks.slot_boundaries",
                    {
                        "now": now.isoformat(),
                        "day": str(day),
                        "time": f"{minute // 60:02}:{minute % 60:02}",
                    },
                    check,
                )


def _outcome_checks(suite: _Suite, ir: PolicyIR) -> None:
    keys = [key for key in ir.closing.resolved_precedence() if key not in STRUCTURAL_ORDER]
    if len(keys) > _MAX_OUTCOME_KEYS:
        suite.skip("sampling.outcome_limit", f"Only the first {_MAX_OUTCOME_KEYS} table keys were sampled")
    keys = keys[:_MAX_OUTCOME_KEYS]
    for verified in (False, True):
        for pair in [*(tuple([key]) for key in keys), *combinations(keys, 2)]:
            expected = next(
                (
                    row.ref()
                    for key in ir.closing.resolved_precedence()
                    if key in pair
                    for row in ir.closing.rows
                    if row.matches(key, verified)
                ),
                None,
            )
            if expected is None:
                continue
            situation = CallSituation(verified=verified, situations=set(pair))
            suite.test(
                "outcomes.table_precedence",
                {"verified": verified, "situations": list(pair)},
                lambda situation=situation, expected=expected: _compare_outcome(ir, situation, expected),
            )
        fallback = ir.persuasion.stop_outcome if verified else ir.identity.on_declined
        suite.test(
            "outcomes.empty_call_fallback",
            {"verified": verified},
            lambda verified=verified, fallback=fallback: _compare_outcome(
                ir, CallSituation(verified=verified), fallback
            ),
        )
    promise = PromiseRecord("synthetic-product", 1.0, "2032-01-06")
    promised_row = ir.closing.row_for("promise_recorded", True)
    if ir.closing.promise_wins and promised_row is not None:
        for key in ["", *(intent.key for intent in ir.intents if not intent.key.startswith("transfer:"))]:
            situation = CallSituation(
                verified=True, promises=[promise], service_error=True, situations={key} if key else set()
            )
            suite.test(
                "outcomes.promise_dominance",
                {"additional_situation": key},
                lambda situation=situation: _compare_outcome(ir, situation, promised_row.ref()),
            )
    else:
        suite.skip(
            "outcomes.promise_dominance", "Promise dominance is disabled or has no applicable table row"
        )
    transfer_outcome = ir.closing.transfer_after_promise_outcome
    queues = [key.split(":", 1)[1] for key in keys if key.startswith("transfer:")]
    if transfer_outcome is not None and queues:
        for queue in queues:
            transfer_row = ir.closing.row_for(f"transfer:{queue}", True)
            reason = transfer_outcome.reason
            if reason is None and transfer_row is not None:
                reason = transfer_row.reason
            expected = OutcomeRef(outcome=transfer_outcome.outcome, reason=reason)
            situation = CallSituation(verified=True, promises=[promise], transfer_queue=queue)
            suite.test(
                "outcomes.transfer_after_promise",
                {"queue": queue},
                lambda situation=situation, expected=expected: _compare_outcome(ir, situation, expected),
            )
    else:
        suite.skip(
            "outcomes.transfer_after_promise",
            "No configured transfer-after-promise rule and queue combination",
        )
    if ir.multi_product.enabled:
        reasons = ir.multi_product.note_reasons[:8]
        for first in reasons:
            for second in reasons:
                records = [NoteRecord("synthetic-first", first), NoteRecord("synthetic-second", second)]
                order = ["synthetic-first", "synthetic-second"]

                def check(records: list[NoteRecord] = records, order: list[str] = order) -> str | None:
                    forward = compute_outcome(
                        ir, CallSituation(verified=True, notes=records, product_order=order)
                    )
                    reverse = compute_outcome(
                        ir, CallSituation(verified=True, notes=records[::-1], product_order=order)
                    )
                    return (
                        None
                        if forward == reverse
                        else "Note recording order overrode configured product order"
                    )

                suite.test("outcomes.note_order_invariance", {"product_reasons": [first, second]}, check)
    else:
        suite.skip("outcomes.note_order_invariance", "Multi-product policy is disabled")


def _compare_outcome(ir: PolicyIR, situation: CallSituation, expected: OutcomeRef) -> str | None:
    actual = compute_outcome(ir, situation)
    return None if actual == expected else f"Outcome {actual.model_dump()}; expected {expected.model_dump()}"


def _enum_check(suite: _Suite, api: ToolApi, tool: str, name: str, values: list[str]) -> None:
    check_name = f"contracts.{tool}.{name}"
    contract = api.tools.get(tool)
    if contract is None:
        suite.test(check_name, {"tool": tool}, lambda: "Required tool is absent from the supplied API")
        return
    spec = contract.get_field(name, "request")
    if spec is None or not spec.enum:
        suite.skip(
            check_name, "No named request enum available; field mapping or unconstrained values are unchecked"
        )
        return
    for value in dict.fromkeys(values):
        suite.test(
            check_name,
            {"value": value},
            lambda value=value: None if value in spec.enum else f"Value is outside the API enum {spec.enum}",
        )


def _api_checks(suite: _Suite, ir: PolicyIR, api: ToolApi | None) -> None:
    if api is None:
        suite.skip("contracts.api", "No tool API was supplied")
        return
    refs = [ref for _, ref in ir.outcome_refs()]
    _enum_check(suite, api, "close_call", "outcome", [ref.outcome for ref in refs])
    _enum_check(suite, api, "close_call", "reason", [ref.reason for ref in refs if ref.reason is not None])
    _enum_check(
        suite, api, "verify_identity", "field", [question.field for question in ir.identity.questions]
    )
    queues = [
        row.situation.split(":", 1)[1] for row in ir.closing.rows if row.situation.startswith("transfer:")
    ]
    if queues:
        _enum_check(suite, api, "transfer_to_agent", "queue", queues)
    if ir.multi_product.enabled and ir.multi_product.note_reasons:
        _enum_check(suite, api, ir.multi_product.note_tool, "reason", ir.multi_product.note_reasons)
    for intent in ir.intents:
        for action in intent.actions:
            contract = api.tools.get(action.tool)
            suite.test(
                "contracts.intent_tools",
                {"intent": intent.key, "tool": action.tool},
                lambda contract=contract: (
                    None if contract is not None else "Intent references an absent tool"
                ),
            )
            if contract is None:
                continue
            for name, value in action.params.items():
                if "{" in value or "}" in value:
                    suite.skip(
                        "contracts.dynamic_parameters",
                        f"{action.tool}.{name}: template requires runtime values",
                    )
                    continue
                _enum_check(suite, api, action.tool, name, [value])


def run_robustness(ir: PolicyIR, api: ToolApi | None = None) -> dict[str, Any]:
    """Return JSON-ready pass/fail/unchecked checks and bounded counterexamples.

    Reads supplied data only. Samples up to 24 date anchors, 16 lateness bands,
    and 16 outcome-table situation keys. Deadline counts over 366 and custom
    prose are marked unchecked. Each failing check retains at most eight
    counterexamples while its failure count includes all evaluated variants.
    """
    suite = _Suite()
    cal = BusinessCalendar.from_ir(ir.calendar)
    anchors = _anchors(ir)
    _calendar_checks(suite, cal, anchors)
    _deadline_checks(suite, ir, cal, anchors)
    _amount_checks(suite, ir)
    _callback_checks(suite, ir, cal, anchors)
    _outcome_checks(suite, ir)
    _api_checks(suite, ir, api)
    return suite.result(anchors)
