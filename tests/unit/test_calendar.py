"""Tests for builder.policy.calendar: business-day arithmetic, deadlines, proposal dates and date/callback checks.

Calendar data comes from the Brief A fixture (business days Monday to Friday; holidays 2026-10-26, 2026-11-11,
2026-11-26, 2026-12-25, 2027-01-01). Every expected date below was counted by hand on the 2026/2027 calendar
(2026-10-14 is a Wednesday) and cross-checked with docs/research/13_brief_rule_mechanism_map.md section 7.

Reference strip from Wed 2026-10-14 (business days after today):
15 Thu=1, 16 Fri=2, 19 Mon=3, 20=4, 21=5, 22=6, 23 Fri=7, [26 Mon holiday], 27 Tue=8, 28=9, 29=10, 30 Fri=11, 2 Nov Mon=12.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from builder.policy.calendar import (
    BusinessCalendar,
    CalendarError,
    UncomputableDeadline,
    allowed_callback_days,
    as_date,
    check_callback,
    check_promise_date,
    deadline_for,
    latest_valid_promise_date,
    parse_hhmm,
    parse_now,
    resolve_proposal_date,
    spoken_date,
    weekday_number,
)
from builder.policy.ir import CallbackRule, DeadlineRule, OutcomeRef, PolicyIR, PromiseDateRules

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "policy"
D = dt.date.fromisoformat

EARLY = DeadlineRule(kind="nth_business_day_after_today", n=12)
LATE = DeadlineRule(kind="nth_business_day_after_today", n=7)


@pytest.fixture(scope="module")
def ir() -> PolicyIR:
    return PolicyIR.model_validate(json.loads((FIXTURES / "brief_a_ir.json").read_text(encoding="utf-8")))


@pytest.fixture(scope="module")
def cal(ir: PolicyIR) -> BusinessCalendar:
    return BusinessCalendar.from_ir(ir.calendar)


@pytest.fixture(scope="module")
def callback_rule(ir: PolicyIR) -> CallbackRule:
    assert ir.third_party.callback is not None
    return ir.third_party.callback


# --------------------------------------------------------------------------------------------------------------------
# Classification and stepping
# --------------------------------------------------------------------------------------------------------------------


def test_calendar_from_ir(cal: BusinessCalendar) -> None:
    assert cal.business_weekdays == (0, 1, 2, 3, 4)
    assert [d.isoformat() for d in cal.holidays] == [
        "2026-10-26",
        "2026-11-11",
        "2026-11-26",
        "2026-12-25",
        "2027-01-01",
    ]


@pytest.mark.parametrize(
    ("day", "business", "kind"),
    [
        ("2026-10-14", True, "business day"),
        ("2026-10-17", False, "weekend"),
        ("2026-10-18", False, "weekend"),
        ("2026-10-26", False, "holiday"),
        ("2026-10-27", True, "business day"),
        ("2026-11-11", False, "holiday"),
        ("2026-12-25", False, "holiday"),
        ("2027-01-01", False, "holiday"),
        ("2027-01-04", True, "business day"),
    ],
)
def test_is_business_day(cal: BusinessCalendar, day: str, business: bool, kind: str) -> None:
    assert cal.is_business_day(day) is business
    assert cal.day_kind(day) == kind


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        ("2026-10-26", "2026-10-27"),  # holiday Monday -> Tuesday (PUB-07)
        ("2026-10-23", "2026-10-27"),  # Friday before the holiday Monday
        ("2026-10-24", "2026-10-27"),
        ("2026-11-10", "2026-11-12"),  # skips Wed 11 Nov
        ("2026-11-25", "2026-11-27"),  # skips Thu 26 Nov
        ("2026-12-24", "2026-12-28"),  # skips Fri 25 Dec and the weekend
        ("2026-12-31", "2027-01-04"),  # skips Fri 1 Jan and the weekend
    ],
)
def test_next_business_day(cal: BusinessCalendar, day: str, expected: str) -> None:
    assert cal.next_business_day(day) == D(expected)


def test_next_and_previous_business_day_include_self(cal: BusinessCalendar) -> None:
    assert cal.next_business_day("2026-10-14", include_self=True) == D("2026-10-14")
    assert cal.next_business_day("2026-10-14") == D("2026-10-15")
    assert cal.next_business_day("2026-10-26", include_self=True) == D("2026-10-27")
    assert cal.previous_business_day("2026-10-27") == D("2026-10-23")
    assert cal.previous_business_day("2027-01-04") == D("2026-12-31")
    assert cal.previous_business_day("2026-12-28") == D("2026-12-24")
    assert cal.previous_business_day("2026-10-25", include_self=True) == D("2026-10-23")
    assert cal.previous_business_day("2026-10-23", include_self=True) == D("2026-10-23")


def test_add_business_days_strictly_after_today(cal: BusinessCalendar) -> None:
    today = "2026-10-14"
    expected = [
        "2026-10-15",
        "2026-10-16",
        "2026-10-19",
        "2026-10-20",
        "2026-10-21",
        "2026-10-22",
        "2026-10-23",
        "2026-10-27",
        "2026-10-28",
        "2026-10-29",
        "2026-10-30",
        "2026-11-02",
    ]
    assert [cal.add_business_days(today, n).isoformat() for n in range(1, 13)] == expected
    assert cal.add_business_days(today, 0) == D(today)
    assert cal.add_business_days("2026-10-27", -1) == D("2026-10-23")
    assert cal.add_business_days("2026-10-17", 1) == D("2026-10-19"), "a weekend 'today' never counts"


def test_business_days_between_is_the_inverse_of_add(cal: BusinessCalendar) -> None:
    for start in ("2026-10-14", "2026-10-17", "2026-12-23", "2026-11-09"):
        for n in range(0, 25):
            end = cal.add_business_days(start, n)
            assert cal.business_days_between(start, end) == n
            assert cal.business_days_between(end, start) == -n
    assert cal.business_days_between("2026-10-14", "2026-11-02") == 12
    assert cal.business_days_between("2026-12-23", "2027-01-05") == 7


def test_list_business_days(cal: BusinessCalendar) -> None:
    assert [d.isoformat() for d in cal.list_business_days("2026-12-24", "2027-01-04")] == [
        "2026-12-24",
        "2026-12-28",
        "2026-12-29",
        "2026-12-30",
        "2026-12-31",
        "2027-01-04",
    ]
    days = cal.list_business_days("2026-10-14", "2026-11-02")
    assert len(days) == 13 and D("2026-10-26") not in days
    assert cal.list_business_days("2026-10-20", "2026-10-19") == []


def test_signed_business_day_count_from_a_non_business_day() -> None:
    calendar = BusinessCalendar()
    sunday, friday = D("2032-01-11"), D("2032-01-09")
    assert calendar.add_business_days(sunday, -1) == friday
    # Signed interval counts exclude the lower bound; backward stepping has a different endpoint.
    assert calendar.business_days_between(sunday, friday) == 0
    assert calendar.business_days_between(friday, sunday) == 0


@pytest.mark.parametrize("deadline", ["2032-01-20", "2032-01-10"])
def test_promise_date_suggestions_never_offer_forbidden_today(deadline: str) -> None:
    calendar = BusinessCalendar()
    rules = PromiseDateRules(allow_today=False, non_business_day_shift="previous_business_day")
    result = check_promise_date("2032-01-11", "2032-01-09", calendar, rules, deadline)
    if deadline == "2032-01-10":
        assert result.suggestion is None, "The policy has no valid day before the deadline."
    else:
        assert result.suggestion == D("2032-01-12")
        assert check_promise_date(result.suggestion, "2032-01-09", calendar, rules, deadline).ok


@pytest.mark.parametrize("inclusive", [False, True])
def test_callback_midnight_window_endpoint_is_supported(inclusive: bool) -> None:
    rule = CallbackRule(
        allowed_days="next_business_day",
        window_start="10:00",
        window_end="24:00",
        inclusive=inclusive,
        success=OutcomeRef(outcome="booked"),
        failure=OutcomeRef(outcome="unbooked"),
    )
    calendar = BusinessCalendar()
    assert check_callback("2032-01-12", "23:59", "2032-01-09T10:00", calendar, rule).ok
    assert (
        check_callback("2032-01-12", "09:59", "2032-01-09T10:00", calendar, rule).status == "outside_window"
    )
    with pytest.raises(ValueError):
        check_callback("2032-01-12", "24:00", "2032-01-09T10:00", calendar, rule)


def test_last_business_day_of_week(cal: BusinessCalendar) -> None:
    assert cal.last_business_day_of_week("2026-10-14") == D("2026-10-16")
    assert cal.last_business_day_of_week("2026-10-18") == D("2026-10-16"), (
        "Sunday belongs to the Monday-Sunday week"
    )
    assert cal.last_business_day_of_week("2026-10-14", week_offset=1) == D("2026-10-23")
    assert cal.last_business_day_of_week("2026-11-23") == D("2026-11-27")
    assert cal.last_business_day_of_week("2026-12-23") == D("2026-12-24"), "Fri 25 Dec is a holiday"
    assert cal.last_business_day_of_week("2026-12-23", week_offset=1) == D("2026-12-31"), (
        "Fri 1 Jan is a holiday"
    )
    closed_week = BusinessCalendar(holidays=[D("2030-01-07") + dt.timedelta(days=i) for i in range(5)])
    assert closed_week.last_business_day_of_week("2030-01-09") is None


@pytest.mark.parametrize(
    ("today", "weekday", "which", "expected"),
    [
        ("2026-10-14", "friday", "this", "2026-10-16"),
        ("2026-10-14", "friday", "next", "2026-10-23"),
        ("2026-10-14", "monday", "next", "2026-10-19"),
        ("2026-10-14", "monday", "this", "2026-10-12"),  # in the past: the caller rejects it
        ("2026-10-14", "saturday", "this", "2026-10-17"),
        ("2026-10-14", "sunday", "next", "2026-10-25"),
        ("2026-10-14", "wednesday", "this", "2026-10-14"),
        ("2026-10-16", "friday", "this", "2026-10-16"),
        ("2026-10-16", "friday", "next", "2026-10-23"),  # "next Friday" said on a Friday (Brief A rule 22)
        ("2026-10-22", "monday", "next", "2026-10-26"),  # a holiday: the caller offers Tue 27
        ("2026-12-23", "friday", "this", "2026-12-25"),
        ("2026-12-23", "friday", "next", "2027-01-01"),
        ("2026-10-18", "friday", "this", "2026-10-16"),  # Sunday: this week's Friday is past
        ("2026-10-18", "friday", "next", "2026-10-23"),
    ],
)
def test_weekday_phrases_following_calendar_week(
    cal: BusinessCalendar, today: str, weekday: str, which: str, expected: str
) -> None:
    assert cal.weekday_date(today, weekday, which) == D(expected)  # type: ignore[arg-type]


def test_weekday_phrases_next_occurrence(cal: BusinessCalendar) -> None:
    assert cal.weekday_date("2026-10-14", "thursday", "next", semantics="next_occurrence") == D("2026-10-15")
    assert cal.weekday_date("2026-10-14", "thursday", "next") == D("2026-10-22")
    assert cal.weekday_date("2026-10-16", 4, "next", semantics="next_occurrence") == D("2026-10-23")
    with pytest.raises(ValueError):
        cal.weekday_date("2026-10-14", "friday", "last")  # type: ignore[arg-type]


def test_weekday_number() -> None:
    assert weekday_number("Friday") == 4
    assert weekday_number("fri") == 4
    assert weekday_number("TU") == 1
    assert weekday_number(6) == 6
    for bad in ("t", "s", "fridays", "", 7, -1):
        with pytest.raises(ValueError):
            weekday_number(bad)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        weekday_number(True)  # type: ignore[arg-type]


def test_calendar_rejects_empty_weekdays_and_endless_scans() -> None:
    with pytest.raises(CalendarError):
        BusinessCalendar(business_weekdays=[])
    only_mondays = BusinessCalendar(business_weekdays=["monday"], holidays=["2030-01-07"])
    assert only_mondays.next_business_day("2030-01-02") == D("2030-01-14")


# --------------------------------------------------------------------------------------------------------------------
# now parsing
# --------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-10-14T11:00", dt.datetime(2026, 10, 14, 11, 0)),
        ("2026-10-14T11:00:00", dt.datetime(2026, 10, 14, 11, 0)),
        ("2026-10-14T11:00:30.250", dt.datetime(2026, 10, 14, 11, 0, 30, 250000)),
        ("2026-10-14 11:00", dt.datetime(2026, 10, 14, 11, 0)),
        ("2026-10-14T11:00Z", dt.datetime(2026, 10, 14, 11, 0)),
        ("2026-10-14T11:00:00+02:00", dt.datetime(2026, 10, 14, 11, 0)),
        (" 2026-10-14T11:00 ", dt.datetime(2026, 10, 14, 11, 0)),
        ("2026-10-14", dt.datetime(2026, 10, 14, 0, 0)),
    ],
)
def test_parse_now(raw: str, expected: dt.datetime) -> None:
    parsed = parse_now(raw)
    assert parsed == expected and parsed.tzinfo is None


def test_parse_now_other_inputs() -> None:
    aware = dt.datetime(2026, 10, 14, 11, 0, tzinfo=dt.UTC)
    assert parse_now(aware) == dt.datetime(2026, 10, 14, 11, 0)
    assert parse_now(dt.date(2026, 10, 14)) == dt.datetime(2026, 10, 14)
    for bad in ("", "yesterday", "14/10/2026 11:00", "2026-13-01T10:00"):
        with pytest.raises(ValueError):
            parse_now(bad)


def test_parse_hhmm_and_as_date() -> None:
    assert parse_hhmm("11:00") == dt.time(11, 0)
    assert parse_hhmm("9:05") == dt.time(9, 5)
    assert parse_hhmm("19.00") == dt.time(19, 0)
    assert parse_hhmm("10h30") == dt.time(10, 30)
    assert parse_hhmm("10:30:59") == dt.time(10, 30)
    for bad in ("24:00", "11", "eleven", "11:60", "1:2:3:4"):
        with pytest.raises(ValueError):
            parse_hhmm(bad)
    assert as_date(dt.datetime(2026, 10, 14, 23, 59)) == D("2026-10-14")
    assert as_date("2026-10-14T11:00") == D("2026-10-14")
    with pytest.raises(ValueError):
        as_date("14 October")


# --------------------------------------------------------------------------------------------------------------------
# Deadlines and proposal dates per 'now'
# --------------------------------------------------------------------------------------------------------------------

# now date | late deadline (7th) | early deadline (12th) | attempt-1 date | next business day
NOW_VECTORS = [
    ("2026-10-14", "2026-10-23", "2026-11-02", "2026-10-16", "2026-10-15"),  # practice-set Wednesday
    ("2026-10-15", "2026-10-27", "2026-11-03", "2026-10-16", "2026-10-16"),
    ("2026-10-16", "2026-10-28", "2026-11-04", "2026-10-23", "2026-10-19"),  # Friday: today is the last bd
    ("2026-10-17", "2026-10-28", "2026-11-04", "2026-10-23", "2026-10-19"),  # Saturday
    ("2026-10-18", "2026-10-28", "2026-11-04", "2026-10-23", "2026-10-19"),  # Sunday
    ("2026-10-19", "2026-10-29", "2026-11-05", "2026-10-23", "2026-10-20"),  # Monday, holiday next week
    ("2026-10-22", "2026-11-03", "2026-11-10", "2026-10-23", "2026-10-23"),
    (
        "2026-10-23",
        "2026-11-04",
        "2026-11-12",
        "2026-10-30",
        "2026-10-27",
    ),  # Friday before the holiday Monday
    ("2026-10-26", "2026-11-04", "2026-11-12", "2026-10-30", "2026-10-27"),  # 'now' on a holiday
    ("2026-10-30", "2026-11-10", "2026-11-18", "2026-11-06", "2026-11-02"),  # month end, Friday
    (
        "2026-11-09",
        "2026-11-19",
        "2026-11-27",
        "2026-11-13",
        "2026-11-10",
    ),  # Monday before a Wednesday holiday
    ("2026-11-10", "2026-11-20", "2026-11-30", "2026-11-13", "2026-11-12"),
    ("2026-11-23", "2026-12-03", "2026-12-10", "2026-11-27", "2026-11-24"),
    ("2026-11-25", "2026-12-07", "2026-12-14", "2026-11-27", "2026-11-27"),
    ("2026-12-23", "2027-01-05", "2027-01-12", "2026-12-24", "2026-12-24"),  # year end
    ("2026-12-24", "2027-01-06", "2027-01-13", "2026-12-31", "2026-12-28"),
    ("2026-12-31", "2027-01-12", "2027-01-19", "2027-01-08", "2027-01-04"),
]


@pytest.mark.parametrize(("now", "late", "early", "attempt1", "next_bd"), NOW_VECTORS)
def test_deadlines_and_attempt_dates(
    cal: BusinessCalendar, now: str, late: str, early: str, attempt1: str, next_bd: str
) -> None:
    today = parse_now(f"{now}T11:00").date()
    assert deadline_for(LATE, today, cal) == D(late)
    assert deadline_for(EARLY, today, cal) == D(early)
    assert resolve_proposal_date("last_business_day_next_week_if_today_is_last", today, cal, D(late)) == D(
        attempt1
    )
    assert cal.next_business_day(today) == D(next_bd)


def test_practice_set_dates(ir: PolicyIR, cal: BusinessCalendar) -> None:
    today = parse_now("2026-10-14T11:00").date()
    early, late = ir.band_for(5), ir.band_for(14)
    assert early is not None and late is not None
    assert deadline_for(early.deadline, today, cal) == D("2026-11-02"), (
        "26 Oct is a holiday, so it is skipped"
    )
    assert deadline_for(late.deadline, today, cal) == D("2026-10-23")
    assert cal.last_business_day_of_week(today) == D("2026-10-16")


def test_other_deadline_kinds(cal: BusinessCalendar) -> None:
    assert deadline_for(DeadlineRule(kind="nth_calendar_day_after_today", n=10), "2026-10-14", cal) == D(
        "2026-10-24"
    )
    assert deadline_for(DeadlineRule(kind="end_of_month"), "2026-10-14", cal) == D("2026-10-31")
    assert deadline_for(DeadlineRule(kind="end_of_month"), "2026-12-23", cal) == D("2026-12-31")
    assert deadline_for(DeadlineRule(kind="end_of_month"), "2028-02-10", cal) == D("2028-02-29")
    assert deadline_for(DeadlineRule(kind="fixed_date", fixed=D("2026-11-30")), "2026-10-14", cal) == D(
        "2026-11-30"
    )
    with pytest.raises(UncomputableDeadline):
        deadline_for(
            DeadlineRule(kind="custom", custom_rule_text="when the statement is issued"), "2026-10-14", cal
        )
    assert latest_valid_promise_date("2026-10-25", cal) == D("2026-10-23")


def test_resolve_proposal_date_kinds(cal: BusinessCalendar) -> None:
    deadline = D("2026-10-23")
    assert resolve_proposal_date("today", "2026-10-14", cal, deadline) == D("2026-10-14")
    assert resolve_proposal_date("today", "2026-10-17", cal, deadline) == D("2026-10-19"), "weekend → next bd"
    assert resolve_proposal_date("deadline", "2026-10-14", cal, deadline) == deadline
    assert resolve_proposal_date("deadline", "2026-10-14", cal) is None
    assert resolve_proposal_date("customer", "2026-10-14", cal, deadline) is None
    # plain "this week" kind keeps today when today is the last business day; the if-today-is-last kind moves on
    assert resolve_proposal_date("last_business_day_this_week", "2026-10-16", cal) == D("2026-10-16")
    assert resolve_proposal_date("last_business_day_next_week_if_today_is_last", "2026-10-16", cal) == D(
        "2026-10-23"
    )
    assert resolve_proposal_date("last_business_day_this_week", "2026-10-17", cal) == D("2026-10-23")


def test_proposal_dates_are_capped_at_the_deadline(cal: BusinessCalendar) -> None:
    short = deadline_for(DeadlineRule(kind="nth_business_day_after_today", n=1), "2026-10-14", cal)
    assert short == D("2026-10-15")
    assert (
        resolve_proposal_date("last_business_day_next_week_if_today_is_last", "2026-10-14", cal, short)
        == short
    )
    weekend_deadline = D("2026-10-25")
    assert resolve_proposal_date(
        "last_business_day_next_week_if_today_is_last", "2026-10-16", cal, weekend_deadline
    ) == D("2026-10-23")


# --------------------------------------------------------------------------------------------------------------------
# Promise date checks
# --------------------------------------------------------------------------------------------------------------------

TODAY = "2026-10-14"
EARLY_DL, LATE_DL = D("2026-11-02"), D("2026-10-23")


@pytest.mark.parametrize(
    ("proposed", "deadline", "status", "suggestion"),
    [
        ("2026-10-14", LATE_DL, "ok", None),  # PUB-01: today
        ("2026-10-15", LATE_DL, "ok", None),  # PUB-12: tomorrow
        ("2026-10-16", LATE_DL, "ok", None),  # PUB-06/10/11: this Friday
        ("2026-10-23", LATE_DL, "ok", None),  # the deadline itself
        ("2026-11-06", LATE_DL, "after_deadline", "2026-10-23"),  # PUB-04: salary day after the deadline
        ("2026-10-26", EARLY_DL, "not_business_day", "2026-10-27"),  # PUB-07: holiday → next business day
        ("2026-10-30", EARLY_DL, "ok", None),  # PUB-13 credit card
        ("2026-10-30", LATE_DL, "after_deadline", "2026-10-23"),  # PUB-13 loan → own deadline
        ("2026-10-17", LATE_DL, "not_business_day", "2026-10-19"),  # Saturday
        ("2026-10-25", EARLY_DL, "not_business_day", "2026-10-27"),  # Sunday, Monday is a holiday
        ("2026-10-25", LATE_DL, "after_deadline", "2026-10-23"),  # weekend after the deadline → rule 23
        ("2026-10-13", LATE_DL, "before_today", "2026-10-14"),
        ("2026-10-12", LATE_DL, "before_today", "2026-10-14"),  # "this Monday" said on Wednesday
    ],
)
def test_check_promise_date_practice_vectors(
    cal: BusinessCalendar, proposed: str, deadline: dt.date, status: str, suggestion: str | None
) -> None:
    check = check_promise_date(proposed, TODAY, cal, PromiseDateRules(), deadline)
    assert check.status == status
    assert check.suggestion == (D(suggestion) if suggestion else None)
    assert check.ok is (status == "ok")


def test_check_promise_date_rule_variants(cal: BusinessCalendar) -> None:
    no_today = PromiseDateRules(allow_today=False)
    assert check_promise_date(TODAY, TODAY, cal, no_today, LATE_DL).status == "before_today"
    assert check_promise_date(TODAY, TODAY, cal, no_today, LATE_DL).suggestion == D("2026-10-15")
    backwards = PromiseDateRules(non_business_day_shift="previous_business_day")
    assert check_promise_date("2026-10-17", TODAY, cal, backwards, LATE_DL).suggestion == D("2026-10-16")
    reject = PromiseDateRules(non_business_day_shift="reject", after_deadline="reject")
    assert check_promise_date("2026-10-17", TODAY, cal, reject, LATE_DL).suggestion is None
    assert check_promise_date("2026-11-06", TODAY, cal, reject, LATE_DL).suggestion is None
    any_day = PromiseDateRules(business_days_only=False)
    assert check_promise_date("2026-10-17", TODAY, cal, any_day, LATE_DL).ok
    no_deadline = PromiseDateRules(not_after_deadline=False)
    assert check_promise_date("2026-12-01", TODAY, cal, no_deadline, LATE_DL).ok
    assert check_promise_date("2026-12-01", TODAY, cal).ok, (
        "without a deadline any later business day is fine"
    )


def test_check_promise_date_year_end(cal: BusinessCalendar) -> None:
    today = "2026-12-23"
    early = deadline_for(EARLY, today, cal)
    assert check_promise_date("2026-12-25", today, cal, None, early).suggestion == D("2026-12-28")
    assert check_promise_date("2027-01-01", today, cal, None, early).suggestion == D("2027-01-04")
    assert (
        check_promise_date("2027-01-01", today, cal, None, deadline_for(LATE, today, cal)).status
        == "not_business_day"
    )


# --------------------------------------------------------------------------------------------------------------------
# Callbacks
# --------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("now", "day", "time", "status"),
    [
        ("2026-10-14T11:00", "2026-10-15", "11:00", "ok"),  # PUB-02: tomorrow morning around eleven
        ("2026-10-14T11:00", "2026-10-14", "15:00", "ok"),
        ("2026-10-14T11:00", "2026-10-14", "10:30", "not_after_now"),
        ("2026-10-14T11:00", "2026-10-14", "11:00", "not_after_now"),
        ("2026-10-14T11:00", "2026-10-15", "09:00", "outside_window"),
        ("2026-10-14T11:00", "2026-10-15", "10:00", "ok"),  # start bound included
        ("2026-10-14T11:00", "2026-10-15", "19:00", "ok"),  # end bound included
        ("2026-10-14T11:00", "2026-10-15", "19:01", "outside_window"),
        (
            "2026-10-14T11:00",
            "2026-10-16",
            "12:00",
            "not_allowed_day",
        ),  # not today and not the next business day
        ("2026-10-14T19:30", "2026-10-14", "19:00", "not_after_now"),
        ("2026-10-14T19:30", "2026-10-15", "10:00", "ok"),  # only the next business day remains
        ("2026-10-23T18:00", "2026-10-26", "11:00", "not_allowed_day"),  # Mon 26 is a holiday
        ("2026-10-23T18:00", "2026-10-27", "11:00", "ok"),
        ("2026-10-23T18:00", "2026-10-23", "18:30", "ok"),
        ("2026-10-17T12:00", "2026-10-17", "15:00", "ok"),  # 'today' read literally on a Saturday
        ("2026-10-17T12:00", "2026-10-19", "15:00", "ok"),
    ],
)
def test_check_callback(
    cal: BusinessCalendar, callback_rule: CallbackRule, now: str, day: str, time: str, status: str
) -> None:
    assert check_callback(day, time, now, cal, callback_rule).status == status


def test_callback_rule_variants(cal: BusinessCalendar) -> None:
    ref = OutcomeRef(outcome="x", reason="y")
    exclusive = CallbackRule(
        allowed_days="next_business_day",
        window_start="10:00",
        window_end="19:00",
        inclusive=False,
        success=ref,
        failure=ref,
    )
    assert (
        check_callback("2026-10-15", "19:00", "2026-10-14T11:00", cal, exclusive).status == "outside_window"
    )
    assert (
        check_callback("2026-10-14", "15:00", "2026-10-14T11:00", cal, exclusive).status == "not_allowed_day"
    )
    any_day = exclusive.model_copy(update={"allowed_days": "any_business_day"})
    assert check_callback("2026-10-21", "12:00", "2026-10-14T11:00", cal, any_day).ok
    assert check_callback("2026-10-26", "12:00", "2026-10-14T11:00", cal, any_day).status == "not_allowed_day"
    custom = exclusive.model_copy(update={"allowed_days": "custom"})
    assert check_callback("2026-10-21", "12:00", "2026-10-14T11:00", cal, custom).status == "unchecked"
    assert allowed_callback_days("2026-10-23T18:00", cal, exclusive) == [D("2026-10-27")]


def test_allowed_callback_days(cal: BusinessCalendar, callback_rule: CallbackRule) -> None:
    assert allowed_callback_days("2026-10-14T11:00", cal, callback_rule) == [D("2026-10-14"), D("2026-10-15")]
    assert allowed_callback_days("2026-12-24T11:00", cal, callback_rule) == [D("2026-12-24"), D("2026-12-28")]


# --------------------------------------------------------------------------------------------------------------------
# Speech
# --------------------------------------------------------------------------------------------------------------------


def test_spoken_date() -> None:
    assert spoken_date("2026-10-16") == "Friday 16 October 2026"
    assert spoken_date(D("2026-11-02"), with_year=False) == "Monday 2 November"
    assert spoken_date("2027-01-04", with_weekday=False) == "4 January 2027"


# --- U-01: proposal dates beyond the "last business day of the week" family ------------------------------------------------


def test_next_business_day_after_today_is_strictly_later_than_today(cal: BusinessCalendar) -> None:
    # Wed 2026-11-04 -> Thu 2026-11-05 (the date a brief means by "propose the next business day after today")
    assert resolve_proposal_date("next_business_day_after_today", "2026-11-04", cal) == D("2026-11-05")
    # Fri -> Mon; Sat -> Mon; the day before a holiday skips the holiday (26 Oct 2026 is one)
    assert resolve_proposal_date("next_business_day_after_today", "2026-10-16", cal) == D("2026-10-19")
    assert resolve_proposal_date("next_business_day_after_today", "2026-10-17", cal) == D("2026-10-19")
    assert resolve_proposal_date("next_business_day_after_today", "2026-10-23", cal) == D("2026-10-27")
    # "today" would be wrong here: it answers 2026-11-04 on a business day
    assert resolve_proposal_date("today", "2026-11-04", cal) == D("2026-11-04")


def test_nth_day_kinds_count_from_today_and_need_n(cal: BusinessCalendar) -> None:
    assert resolve_proposal_date("nth_business_day_after_today", "2026-10-14", cal, n=3) == D("2026-10-19")
    assert resolve_proposal_date("nth_business_day_after_today", "2026-10-14", cal, n=8) == D("2026-10-27")
    assert resolve_proposal_date("nth_calendar_day_after_today", "2026-10-14", cal, n=5) == D("2026-10-19")
    with pytest.raises(ValueError, match="needs n"):
        resolve_proposal_date("nth_business_day_after_today", "2026-10-14", cal)


def test_new_kinds_are_capped_at_the_deadline_and_custom_is_left_to_the_prompt(cal: BusinessCalendar) -> None:
    deadline = D("2026-10-16")
    assert resolve_proposal_date("nth_business_day_after_today", "2026-10-14", cal, deadline, n=5) == deadline
    assert resolve_proposal_date("next_business_day_after_today", "2026-10-14", cal, deadline) == D("2026-10-15")
    assert resolve_proposal_date("custom", "2026-10-14", cal, deadline) is None
