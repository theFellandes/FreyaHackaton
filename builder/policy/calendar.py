"""Business-day calendar arithmetic: the reference implementation of every date rule.

The phone agent never does date arithmetic itself: code nodes compute deadlines, proposal dates and allowed callback
days from the call variable ``now`` and give the model finished facts. Those code-node snippets
(``compiler/codegen.py``) must agree with the functions here, so both are tested against the same vectors.

Everything is parameterised by IR data (:class:`~builder.policy.ir.Calendar`, :class:`~builder.policy.ir.DeadlineRule`,
:class:`~builder.policy.ir.PromiseDateRules`, :class:`~builder.policy.ir.CallbackRule`); nothing here knows a
particular bank's holidays or limits.

Conventions:

* Weekdays use Python numbering: Monday = 0 ... Sunday = 6. Weeks run Monday to Sunday.
* "The n-th business day after today" counts business days strictly after today; today never counts, whether or not
  it is a business day.
* ``now`` values are read as wall-clock local time; a ``Z`` or ``+hh:mm`` suffix is dropped, not converted.
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from builder.policy.ir import Calendar, CallbackRule, DeadlineRule, PromiseDateRules, ProposalDateKind

__all__ = [
    "BusinessCalendar",
    "CalendarError",
    "CallbackCheck",
    "DateCheck",
    "DateLike",
    "UncomputableDeadline",
    "allowed_callback_days",
    "as_date",
    "check_callback",
    "check_promise_date",
    "deadline_for",
    "latest_valid_promise_date",
    "parse_hhmm",
    "parse_now",
    "resolve_proposal_date",
    "spoken_date",
    "weekday_number",
]

DateLike = _dt.date | _dt.datetime | str
"""Accepted date inputs: a ``date``, a ``datetime`` (its date part is used) or an ISO string (first 10 chars)."""

_MAX_SCAN_DAYS = 3660
_WEEKDAY_NAMES = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_MONTH_NAMES = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


class CalendarError(ValueError):
    """Raised for an unusable calendar (no business weekday) or a search that finds no business day."""


class UncomputableDeadline(ValueError):
    """Raised by :func:`deadline_for` for a ``custom`` deadline rule, which only the prompt can apply."""


def as_date(value: DateLike) -> _dt.date:
    """Normalise a date-like value to a ``datetime.date``.

    ``datetime`` values lose their time; strings are parsed from their first ten characters (``YYYY-MM-DD``).
    Raises ``ValueError`` for an unreadable string.
    """
    if isinstance(value, _dt.datetime):
        return value.date()
    if isinstance(value, _dt.date):
        return value
    text = str(value).strip()
    try:
        return _dt.date.fromisoformat(text[:10])
    except ValueError as exc:
        raise ValueError(f"not an ISO date: {value!r}") from exc


def weekday_number(weekday: int | str) -> int:
    """Return the Python weekday number (Monday = 0) for an int or an English name/abbreviation ("fri", "Friday")."""
    if isinstance(weekday, int) and not isinstance(weekday, bool):
        if 0 <= weekday <= 6:
            return weekday
        raise ValueError(f"weekday number must be 0..6, got {weekday}")
    name = str(weekday).strip().lower()
    for index, full in enumerate(_WEEKDAY_NAMES):
        if len(name) >= 2 and full.startswith(name):
            return index
    raise ValueError(f"unknown weekday: {weekday!r}")


class BusinessCalendar:
    """Business days = configured weekdays minus holidays.

    Construct directly or from IR data with :meth:`from_ir`. All methods accept :data:`DateLike` values and return
    ``datetime.date`` objects.
    """

    def __init__(
        self, business_weekdays: Iterable[int] = (0, 1, 2, 3, 4), holidays: Iterable[DateLike] = ()
    ) -> None:
        weekdays = frozenset(weekday_number(day) for day in business_weekdays)
        if not weekdays:
            raise CalendarError("a calendar needs at least one business weekday")
        self._weekdays = weekdays
        self._holidays = frozenset(as_date(day) for day in holidays)

    @classmethod
    def from_ir(cls, calendar: Calendar) -> BusinessCalendar:
        """Build from the IR's :class:`~builder.policy.ir.Calendar` section."""
        return cls(calendar.business_weekdays, calendar.holidays)

    def __repr__(self) -> str:
        return f"BusinessCalendar(business_weekdays={self.business_weekdays}, holidays={len(self._holidays)})"

    @property
    def business_weekdays(self) -> tuple[int, ...]:
        """Business weekday numbers, sorted."""
        return tuple(sorted(self._weekdays))

    @property
    def holidays(self) -> tuple[_dt.date, ...]:
        """Holiday dates, sorted."""
        return tuple(sorted(self._holidays))

    # ---- classification ------------------------------------------------------------------------------------------

    def is_holiday(self, day: DateLike) -> bool:
        """True when ``day`` is a listed holiday."""
        return as_date(day) in self._holidays

    def is_business_day(self, day: DateLike) -> bool:
        """True when ``day`` falls on a business weekday and is not a holiday."""
        value = as_date(day)
        return value.weekday() in self._weekdays and value not in self._holidays

    def day_kind(self, day: DateLike) -> Literal["business day", "holiday", "weekend"]:
        """Human label for a day: ``business day``, ``holiday`` or ``weekend`` (any non-business weekday)."""
        value = as_date(day)
        if value in self._holidays:
            return "holiday"
        return "business day" if value.weekday() in self._weekdays else "weekend"

    # ---- stepping ------------------------------------------------------------------------------------------------

    def _scan(self, start: _dt.date, step: int) -> _dt.date:
        current = start
        for _ in range(_MAX_SCAN_DAYS):
            if self.is_business_day(current):
                return current
            current += _dt.timedelta(days=step)
        raise CalendarError(f"no business day within {_MAX_SCAN_DAYS} days of {start.isoformat()}")

    def next_business_day(self, day: DateLike, include_self: bool = False) -> _dt.date:
        """The first business day after ``day`` (or ``day`` itself when ``include_self`` and it is one)."""
        value = as_date(day)
        return self._scan(value if include_self else value + _dt.timedelta(days=1), 1)

    def previous_business_day(self, day: DateLike, include_self: bool = False) -> _dt.date:
        """The last business day before ``day`` (or ``day`` itself when ``include_self`` and it is one)."""
        value = as_date(day)
        return self._scan(value if include_self else value - _dt.timedelta(days=1), -1)

    def add_business_days(self, day: DateLike, n: int) -> _dt.date:
        """The n-th business day strictly after ``day`` (n > 0), strictly before it (n < 0), or ``day`` (n = 0)."""
        current = as_date(day)
        if n == 0:
            return current
        step = 1 if n > 0 else -1
        for _ in range(abs(n)):
            current = self._scan(current + _dt.timedelta(days=step), step)
        return current

    # ---- weeks ---------------------------------------------------------------------------------------------------

    @staticmethod
    def week_start(day: DateLike) -> _dt.date:
        """Monday of the week containing ``day``."""
        value = as_date(day)
        return value - _dt.timedelta(days=value.weekday())

    def last_business_day_of_week(self, day: DateLike, week_offset: int = 0) -> _dt.date | None:
        """The last business day of the Monday-Sunday week containing ``day`` (shifted by ``week_offset`` weeks).

        Returns ``None`` when that week has no business day at all.
        """
        monday = self.week_start(day) + _dt.timedelta(weeks=week_offset)
        for offset in range(6, -1, -1):
            candidate = monday + _dt.timedelta(days=offset)
            if self.is_business_day(candidate):
                return candidate
        return None

    def weekday_date(
        self,
        day: DateLike,
        weekday: int | str,
        which: Literal["this", "next"],
        *,
        semantics: Literal["following_calendar_week", "next_occurrence"] = "following_calendar_week",
    ) -> _dt.date:
        """Resolve "this <weekday>" / "next <weekday>" said on ``day``.

        * ``this``: that weekday in the current Monday-Sunday week (it may lie in the past).
        * ``next`` with ``following_calendar_week``: that weekday in the following calendar week, so "next Friday"
          said on a Friday is seven days later.
        * ``next`` with ``next_occurrence``: the first such weekday strictly after ``day``.

        Business days are not considered; validate the result with :func:`check_promise_date`.
        """
        value = as_date(day)
        target = weekday_number(weekday)
        if which == "this":
            return self.week_start(value) + _dt.timedelta(days=target)
        if which != "next":
            raise ValueError(f"which must be 'this' or 'next', got {which!r}")
        if semantics == "next_occurrence":
            delta = (target - value.weekday()) % 7 or 7
            return value + _dt.timedelta(days=delta)
        return self.week_start(value) + _dt.timedelta(weeks=1, days=target)

    # ---- ranges --------------------------------------------------------------------------------------------------

    def business_days_between(self, start: DateLike, end: DateLike) -> int:
        """Number of business days ``d`` with ``start < d <= end`` (negative when ``end`` is before ``start``).

        Inverse of :meth:`add_business_days` for nonnegative ``n``; also for negative ``n`` when ``a`` is a
        business day. The signed count is antisymmetric, so an off-business-day start cannot be the inverse
        of backward stepping as well (the lower business-day endpoint is excluded).
        """
        a, b = as_date(start), as_date(end)
        if b == a:
            return 0
        sign = 1 if b > a else -1
        low, high = (a, b) if sign > 0 else (b, a)
        count = sum(1 for d in self._days(low + _dt.timedelta(days=1), high) if self.is_business_day(d))
        return sign * count

    def list_business_days(self, start: DateLike, end: DateLike) -> list[_dt.date]:
        """Business days from ``start`` to ``end``, both included (empty when ``end`` is before ``start``)."""
        return [d for d in self._days(as_date(start), as_date(end)) if self.is_business_day(d)]

    @staticmethod
    def _days(start: _dt.date, end: _dt.date) -> Iterable[_dt.date]:
        current = start
        while current <= end:
            yield current
            current += _dt.timedelta(days=1)


# --------------------------------------------------------------------------------------------------------------------
# now, deadlines and proposal dates
# --------------------------------------------------------------------------------------------------------------------


def parse_now(value: str | _dt.datetime | _dt.date) -> _dt.datetime:
    """Parse the ``now`` call variable into a naive datetime (wall-clock time as written).

    Accepts ``2030-01-02T09:30``, with seconds or fractions, a space instead of ``T``, a trailing ``Z`` or an
    ``+hh:mm`` offset (dropped, not converted), or a bare date (midnight). Raises ``ValueError`` otherwise.
    """
    if isinstance(value, _dt.datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, _dt.date):
        return _dt.datetime(value.year, value.month, value.day)
    text = str(value).strip()
    if text[-1:] in ("Z", "z"):
        text = text[:-1]
    try:
        parsed = _dt.datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"cannot read the current date and time from {value!r}") from exc
    return parsed.replace(tzinfo=None)


def parse_hhmm(value: str) -> _dt.time:
    """Parse ``HH:MM`` (also ``H:MM``, ``HH.MM``, ``HHhMM``, ``HH:MM:SS``) into a ``datetime.time``.

    Seconds are dropped; ``24:00`` is not accepted. Raises ``ValueError`` for anything else.
    """
    text = str(value).strip().lower().replace(".", ":").replace("h", ":")
    parts = text.split(":")
    try:
        if len(parts) not in (2, 3) or any(not part.isdigit() for part in parts):
            raise ValueError(text)
        return _dt.time(int(parts[0]), int(parts[1]))
    except ValueError as exc:
        raise ValueError(f"not a HH:MM time: {value!r}") from exc


def deadline_for(rule: DeadlineRule, today: DateLike, cal: BusinessCalendar) -> _dt.date:
    """Compute the deadline a :class:`~builder.policy.ir.DeadlineRule` gives for ``today``.

    Raises :class:`UncomputableDeadline` for ``custom`` rules. The result is the raw rule value; for calendar-day
    rules it may fall on a non-business day (see :func:`latest_valid_promise_date`).
    """
    base = as_date(today)
    if rule.kind == "nth_business_day_after_today":
        return cal.add_business_days(base, int(rule.n or 0))
    if rule.kind == "nth_calendar_day_after_today":
        return base + _dt.timedelta(days=int(rule.n or 0))
    if rule.kind == "end_of_month":
        first_of_next = (base.replace(day=28) + _dt.timedelta(days=4)).replace(day=1)
        return first_of_next - _dt.timedelta(days=1)
    if rule.kind == "fixed_date" and rule.fixed is not None:
        return rule.fixed
    raise UncomputableDeadline(f"deadline rule {rule.kind!r} cannot be computed: {rule.custom_rule_text!r}")


def latest_valid_promise_date(deadline: DateLike, cal: BusinessCalendar) -> _dt.date:
    """The last business day on or before ``deadline``."""
    return cal.previous_business_day(deadline, include_self=True)


def resolve_proposal_date(
    kind: ProposalDateKind,
    today: DateLike,
    cal: BusinessCalendar,
    deadline: DateLike | None = None,
    n: int | None = None,
) -> _dt.date | None:
    """Resolve a persuasion step's ``date`` kind to a concrete date, capped at ``deadline`` when one is given.

    * ``today``: today, or the next business day when today is not one.
    * ``last_business_day_this_week``: this week's last business day; next week's when this week has none left
      (weekend calls).
    * ``last_business_day_next_week_if_today_is_last``: as above, but when today *is* this week's last business day
      the last business day of next week is used.
    * ``next_business_day_after_today``: the first business day strictly after today (a business day today does not
      count - that is what separates it from ``today``).
    * ``nth_business_day_after_today`` / ``nth_calendar_day_after_today``: the ``n``-th such day after today (``n``
      is required). A calendar day that is not a business day moves to the next business day: a proposal must be a
      date the promise can be recorded on.
    * ``deadline``: the deadline (``None`` without one).
    * ``customer`` / ``custom``: ``None`` (the customer names the date, or only the step's text can apply the rule).
    """
    base = as_date(today)
    if kind in ("customer", "custom"):
        return None
    if kind == "deadline":
        return as_date(deadline) if deadline is not None else None
    if kind == "today":
        result = cal.next_business_day(base, include_self=True)
    elif kind == "next_business_day_after_today":
        result = cal.next_business_day(base)
    elif kind in ("nth_business_day_after_today", "nth_calendar_day_after_today"):
        if n is None:
            raise ValueError(f"proposal date kind {kind!r} needs n")
        if kind == "nth_business_day_after_today":
            result = cal.add_business_days(base, int(n))
        else:
            result = base + _dt.timedelta(days=int(n))
            if not cal.is_business_day(result):
                result = cal.next_business_day(result)
    else:
        this_week = cal.last_business_day_of_week(base)
        strictly_later = kind == "last_business_day_next_week_if_today_is_last"
        usable = this_week is not None and (this_week > base if strictly_later else this_week >= base)
        result = this_week if usable and this_week is not None else _next_weeks_last_business_day(base, cal)
    if deadline is not None:
        result = min(result, latest_valid_promise_date(deadline, cal))
    return result


def _next_weeks_last_business_day(base: _dt.date, cal: BusinessCalendar) -> _dt.date:
    for week_offset in range(1, 54):
        candidate = cal.last_business_day_of_week(base, week_offset)
        if candidate is not None:
            return candidate
    raise CalendarError(f"no business day in the year after {base.isoformat()}")


# --------------------------------------------------------------------------------------------------------------------
# Validation of proposed dates
# --------------------------------------------------------------------------------------------------------------------

DateStatus = Literal["ok", "before_today", "not_business_day", "after_deadline"]


@dataclass(frozen=True, slots=True)
class DateCheck:
    """Verdict on a proposed promise date and, when it is not ok, the date to offer instead (``None`` = none)."""

    status: DateStatus
    suggestion: _dt.date | None = None

    @property
    def ok(self) -> bool:
        """True when the proposed date can be recorded as it is."""
        return self.status == "ok"


def check_promise_date(
    proposed: DateLike,
    today: DateLike,
    cal: BusinessCalendar,
    rules: PromiseDateRules | None = None,
    deadline: DateLike | None = None,
) -> DateCheck:
    """Check a promise date against the IR's date rules.

    Order of checks: before today (or today when today is not allowed) → after the deadline → not a business day.
    A non-business day after the deadline therefore reports ``after_deadline``. Suggestions: the earliest valid date
    for ``before_today``; the latest valid date on or before the deadline for ``after_deadline`` (when the rules
    offer the deadline); the shifted business day for ``not_business_day``, capped at the deadline.
    """
    rules = rules or PromiseDateRules()
    day, base = as_date(proposed), as_date(today)
    limit = as_date(deadline) if deadline is not None and rules.not_after_deadline else None
    earliest = base if rules.allow_today else base + _dt.timedelta(days=1)
    if rules.business_days_only:
        earliest = cal.next_business_day(earliest, include_self=True)
    latest = (
        latest_valid_promise_date(limit, cal) if limit is not None and rules.business_days_only else limit
    )

    if day < base or (day == base and not rules.allow_today):
        return DateCheck("before_today", earliest if latest is None or earliest <= latest else None)
    if limit is not None and day > limit:
        offer = (
            latest
            if rules.after_deadline == "offer_deadline" and latest is not None and latest >= earliest
            else None
        )
        return DateCheck("after_deadline", offer)
    if rules.business_days_only and not cal.is_business_day(day):
        return DateCheck("not_business_day", _shift_suggestion(day, earliest, cal, rules, latest))
    return DateCheck("ok")


def _shift_suggestion(
    day: _dt.date, earliest: _dt.date, cal: BusinessCalendar, rules: PromiseDateRules, latest: _dt.date | None
) -> _dt.date | None:
    if rules.non_business_day_shift == "reject":
        return None
    if rules.non_business_day_shift == "previous_business_day":
        shifted = cal.previous_business_day(day)
        if shifted < earliest:
            shifted = cal.next_business_day(day)
    else:
        shifted = cal.next_business_day(day)
    if latest is not None and shifted > latest:
        shifted = latest
    return shifted if shifted >= earliest else None


# --------------------------------------------------------------------------------------------------------------------
# Callbacks
# --------------------------------------------------------------------------------------------------------------------

CallbackStatus = Literal["ok", "not_allowed_day", "outside_window", "not_after_now", "unchecked"]


@dataclass(frozen=True, slots=True)
class CallbackCheck:
    """Verdict on a proposed callback slot. ``unchecked`` = the rule is ``custom`` and only the prompt applies it."""

    status: CallbackStatus

    @property
    def ok(self) -> bool:
        """True when the slot can be booked."""
        return self.status == "ok"


def allowed_callback_days(
    now: str | _dt.datetime, cal: BusinessCalendar, rule: CallbackRule
) -> list[_dt.date]:
    """Days on which a callback may be booked (empty for ``any_business_day`` and ``custom``, which are open-ended).

    ``today_or_next_business_day`` allows today literally (even on a non-business day) and the next business day.
    """
    today = parse_now(now).date()
    if rule.allowed_days == "today_or_next_business_day":
        return [today, cal.next_business_day(today)]
    if rule.allowed_days == "next_business_day":
        return [cal.next_business_day(today)]
    return []


def check_callback(
    day: DateLike, time_hhmm: str, now: str | _dt.datetime, cal: BusinessCalendar, rule: CallbackRule
) -> CallbackCheck:
    """Check a proposed callback date and ``HH:MM`` time against the IR's callback rule at ``now``."""
    if rule.allowed_days == "custom":
        return CallbackCheck("unchecked")
    current = parse_now(now)
    slot_day, slot_time = as_date(day), parse_hhmm(time_hhmm)
    if rule.allowed_days == "any_business_day":
        day_ok = slot_day >= current.date() and cal.is_business_day(slot_day)
    else:
        day_ok = slot_day in allowed_callback_days(current, cal, rule)
    if not day_ok:
        return CallbackCheck("not_allowed_day")

    # 24:00 is a valid policy window boundary, but never a schedulable time of day.
    def boundary_minutes(value: str) -> int:
        if value == "24:00":
            return 24 * 60
        parsed = parse_hhmm(value)
        return parsed.hour * 60 + parsed.minute

    start, end = boundary_minutes(rule.window_start), boundary_minutes(rule.window_end)
    slot_minutes = slot_time.hour * 60 + slot_time.minute
    in_window = start <= slot_minutes <= end if rule.inclusive else start <= slot_minutes < end
    if not in_window:
        return CallbackCheck("outside_window")
    if rule.must_be_after_now and _dt.datetime.combine(slot_day, slot_time) <= current:
        return CallbackCheck("not_after_now")
    return CallbackCheck("ok")


# --------------------------------------------------------------------------------------------------------------------
# Speech
# --------------------------------------------------------------------------------------------------------------------


def spoken_date(day: DateLike, *, with_weekday: bool = True, with_year: bool = True) -> str:
    """English spoken form, e.g. ``Wednesday 2 January 2030`` (no ordinal suffixes, no leading zero)."""
    value = as_date(day)
    parts = [str(value.day), _MONTH_NAMES[value.month - 1]]
    if with_weekday:
        parts.insert(0, _WEEKDAY_NAMES[value.weekday()].capitalize())
    if with_year:
        parts.append(str(value.year))
    return " ".join(parts)
