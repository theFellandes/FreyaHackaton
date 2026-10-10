"""Generated validation snippets preserve the calendar helper boundary behavior."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from builder.checks.code_sandbox import run_snippet
from builder.compiler.codegen import check_callback_snippet, check_promise_snippet
from builder.policy.calendar import BusinessCalendar, check_callback, check_promise_date
from builder.policy.ir import PolicyIR, PromiseDateRules


@pytest.mark.parametrize(
    ("today", "proposed", "deadline", "updates"),
    [
        (
            "2032-01-09",
            "2032-01-11",
            "2032-01-20",
            {"allow_today": False, "non_business_day_shift": "previous_business_day"},
        ),
        ("2032-01-09", "2032-01-11", "2032-01-10", {"allow_today": False}),
        ("2032-01-09", "2032-01-08", "2032-01-10", {"allow_today": False}),
        ("2032-01-10", "2032-01-10", "2032-01-20", {}),
        ("2032-01-10", "2032-01-11", "2032-01-20", {"allow_today": False}),
        ("2032-01-09", "2032-01-18", "2032-01-10", {"not_after_deadline": False}),
        ("2032-01-09", "2032-01-11", "2032-01-10", {}),
        ("2032-01-08", "2032-01-10", "2032-01-10", {}),
        ("2032-01-09", "2032-01-08", "2032-01-08", {}),
        ("2032-01-09", "2032-01-11", "2032-01-20", {"non_business_day_shift": "reject"}),
        ("2032-01-09", "2032-01-21", "2032-01-20", {"after_deadline": "reject"}),
        ("2032-01-09", "2032-01-11", "2032-01-20", {"business_days_only": False}),
        ("2032-01-09", "2032-01-11", "2032-01-10", {"business_days_only": False}),
    ],
)
def test_promise_date_status_and_suggestion_match_reference(
    today: str,
    proposed: str,
    deadline: str,
    updates: dict[str, Any],
) -> None:
    ir = PolicyIR.example_minimal()
    ir.promise_dates = PromiseDateRules(**updates)
    cal = BusinessCalendar.from_ir(ir.calendar)
    expected = check_promise_date(proposed, today, cal, ir.promise_dates, deadline)

    actual = run_snippet(
        check_promise_snippet(ir),
        {
            "now": f"{today}T11:00",
            "chk_product_id": "synthetic",
            "chk_date": proposed,
            "chk_amount": "100",
            "synthetic_past_due": "100",
            "synthetic_floor": "20",
            "synthetic_deadline": deadline,
        },
    )

    assert actual["promise_date_check"] == expected.status
    assert actual["promise_check"] == expected.status
    assert actual["promise_suggestion"] == (expected.suggestion.isoformat() if expected.suggestion else "")


def test_nonstandard_calendar_and_holidays_preserve_suggestion_parity() -> None:
    ir = PolicyIR.example_minimal()
    ir.calendar.business_weekdays = [1, 2, 3, 4, 5]
    ir.calendar.holidays = [date(2032, 1, 10)]
    ir.promise_dates = PromiseDateRules(allow_today=False, non_business_day_shift="previous_business_day")
    cal = BusinessCalendar.from_ir(ir.calendar)
    snippet = check_promise_snippet(ir)
    for proposed in ("2032-01-08", "2032-01-09", "2032-01-10", "2032-01-11", "2032-01-12", "2032-01-13"):
        expected = check_promise_date(proposed, "2032-01-09", cal, ir.promise_dates, "2032-01-12")
        actual = run_snippet(
            snippet,
            {
                "now": "2032-01-09T11:00",
                "chk_product_id": "synthetic",
                "chk_date": proposed,
                "chk_amount": "100",
                "synthetic_past_due": "100",
                "synthetic_deadline": "2032-01-12",
            },
        )
        assert actual["promise_date_check"] == expected.status
        assert actual["promise_suggestion"] == (
            expected.suggestion.isoformat() if expected.suggestion else ""
        )


@pytest.mark.parametrize(
    "time_text",
    ["25:00", "24:00", "11:60", "11", "11:", "-1:00", "11: 00", "11:00x", "11:00:invalid", "11:00:00:00"],
)
@pytest.mark.parametrize("after_now", [False, True])
def test_invalid_callback_time_is_unreadable_instead_of_accepted_or_crashing(
    time_text: str,
    after_now: bool,
) -> None:
    ir = PolicyIR.example_minimal()
    rule = ir.third_party.callback
    assert rule is not None
    rule.must_be_after_now = after_now
    cal = BusinessCalendar.from_ir(ir.calendar)
    with pytest.raises(ValueError):
        check_callback("2032-01-12", time_text, "2032-01-09T11:00", cal, rule)

    actual = run_snippet(
        check_callback_snippet(ir),
        {
            "now": "2032-01-09T11:00",
            "chk_cb_date": "2032-01-12",
            "chk_cb_time": time_text,
        },
    )

    assert actual["callback_check"] == "unreadable"
    assert actual["callback_suggestion"] == ""


@pytest.mark.parametrize("end", ["17:00", "24:00"])
@pytest.mark.parametrize("inclusive", [False, True])
def test_callback_normalization_and_window_boundaries_match_reference(end: str, inclusive: bool) -> None:
    ir = PolicyIR.example_minimal()
    rule = ir.third_party.callback
    assert rule is not None
    rule.allowed_days = "today_or_next_business_day"
    rule.window_end = end
    rule.inclusive = inclusive
    cal = BusinessCalendar.from_ir(ir.calendar)
    snippet = check_callback_snippet(ir)
    for day in ("2032-01-09", "2032-01-10", "2032-01-12"):
        for time_text in ("8:59", "9:00", "11:00", "11:001", "11H01", "11.01", "11:01:59", "17:00", "23:59"):
            expected = check_callback(day, time_text, "2032-01-09T11:00", cal, rule)
            actual = run_snippet(
                snippet,
                {
                    "now": "2032-01-09T11:00",
                    "chk_cb_date": day,
                    "chk_cb_time": time_text,
                },
            )
            assert actual["callback_check"] == expected.status, (day, time_text, end, inclusive)
