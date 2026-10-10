"""Code-node snippet generators: everything that needs arithmetic, dates or counting is computed in Python, not by the model.

The validated agent design (``evals/reference``) moves all exact facts out of the dialogue model and into small code
nodes whose results reach the prompts as ``{{variable}}`` text:

* :func:`setup_snippet` - runs first. From ``vars['now']`` it writes today's facts, a **date table** for the next weeks
  (weekday names, this-week / next-week labels, business-day / holiday flags), the first persuasion proposal date and
  the **callback facts** (which day and time a callback may be booked).
* :func:`account_facts_snippet` - runs after ``get_account``. For every product it writes the spoken name, past-due
  amount, days late, band, deadline, floor, the **exact floor-offer sentence**, the **scripted persuasion attempts**,
  the allowed promise dates and finally the ``debt_statement`` the agent says on entry to the negotiation.
* :func:`check_callback_snippet` / :func:`check_promise_snippet` - mirror :func:`builder.policy.calendar.check_callback`
  and the promise date/amount checks, for use in code nodes or hooks.
* :func:`decide_outcome_snippet` - mirrors :func:`builder.policy.outcomes.compute_outcome`: from the recorded facts
  (tool success counters, the notes log, the situation chosen by the ``end_*`` node) it writes ``final_outcome`` and
  ``final_reason``. The unit tests run it against the reference engine on the same vectors.

Every snippet obeys the platform's code rules (``docs/tools-and-code.md``): it reads and writes ``vars``, never raises
(a ``try`` / ``except`` writes safe fallbacks for the variables later prompts read), writes strings for routing flags, imports
only ``json`` / ``re`` / ``datetime`` and contains no ``{{`` or ``}}``. IR data is embedded with
:func:`builder.compiler.pycode.json_literal` so the source has no braces from data.
"""

from __future__ import annotations

import textwrap
from collections.abc import Mapping, Sequence
from typing import Any

from builder.compiler import naming
from builder.compiler.context import extra_phrase, today_question
from builder.compiler.hooks import PROMISE_FIX_DEFAULT
from builder.compiler.pycode import MONEY_FUNCS, assert_safe_code, json_literal, py_literal, strip_code
from builder.compiler.roles import AccountRoles
from builder.policy.ir import DeadlineRule, PolicyIR
from builder.policy.outcomes import STRUCTURAL_ORDER, Situation

__all__ = [
    "ALLOWED_HORIZON_DAYS",
    "DATE_TABLE_MAX_DAYS",
    "DATE_TABLE_MIN_DAYS",
    "VARS_WRITTEN",
    "account_facts_snippet",
    "check_callback_snippet",
    "check_promise_snippet",
    "decide_config",
    "decide_outcome_snippet",
    "guard_snippet",
    "promise_fix_snippet",
    "set_choice_snippet",
    "setup_snippet",
]

#: Variables each code node writes (documented in docs/COMPILER.md; used by the lint rule that resolves ``{{refs}}``).
VARS_WRITTEN: dict[str, tuple[str, ...]] = {
    "setup": (
        "setup_state", "today_iso", "today_spoken", "weekday_name", "now_time", "today_is_business_day",
        "next_business_day_iso", "next_business_day_spoken", "date_table", "attempt1_date", "attempt1_spoken",
        "callback_facts",
    ),
    "account_facts": (
        "account_state", "account_facts", "debt_statement", "product_ids", "product_count", "identity_verified",
    ),
    "decide": ("final_outcome", "final_reason", "decide_state", "decide_situation"),
    "promise_fix": ("promise_guard", "promise_fix_text"),
}

#: The date table lists at least this many days, and as many as a deadline needs but never more than the maximum (a long
#: deadline must not turn the negotiation prompt into a calendar; the allowed dates are summarised in ``account_facts``).
DATE_TABLE_MIN_DAYS = 50
DATE_TABLE_MAX_DAYS = 70
#: How far ahead the promise guard knows every allowed date. A deadline beyond it (or no deadline) leaves the list open:
#: the guard then accepts later ISO dates (up to the deadline, when there is one).
ALLOWED_HORIZON_DAYS = 366
#: Up to this many allowed dates are listed one by one in the prompt; longer lists are summarised as a range.
ALLOWED_LIST_LIMIT = 30
#: ``allowed_spoken_<pid>`` holds the first dates only (the guard speaks the next allowed day, which is a near one).
ALLOWED_SPOKEN_LIMIT = 90

_WEEKDAYS = "['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']"
_MONTHS = (
    "['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', "
    "'November', 'December']"
)

# --------------------------------------------------------------------------------------------------------------------
# Shared calendar functions (source text, indented by the wrapper)
# --------------------------------------------------------------------------------------------------------------------

_CALENDAR_FUNCS = f"""
WD = {_WEEKDAYS}
MO = {_MONTHS}
hol = set(CFG['holidays'])
bw = set(CFG['weekdays'])
ONE = datetime.timedelta(days=1)
def isbd(d):
    return d.weekday() in bw and d.isoformat() not in hol
def spoken(d):
    return WD[d.weekday()] + ' ' + str(d.day) + ' ' + MO[d.month - 1] + ' ' + str(d.year)
def short(d):
    return WD[d.weekday()] + ' ' + str(d.day) + ' ' + MO[d.month - 1]
def next_bd(d):
    x = d + ONE
    for _ in range(400):
        if isbd(x):
            break
        x = x + ONE
    return x
def prev_bd(d):
    x = d
    for _ in range(400):
        if isbd(x):
            break
        x = x - ONE
    return x
def nth_bd_after(d, n):
    x = d
    for _ in range(int(n)):
        x = next_bd(x)
    return x
def last_bd_of_week(d, offset):
    monday = d - datetime.timedelta(days=d.weekday()) + datetime.timedelta(days=7 * offset)
    x = monday + datetime.timedelta(days=6)
    while x >= monday and not isbd(x):
        x = x - ONE
    return x if x >= monday else None
def next_weeks_last(d):
    for off in range(1, 54):
        x = last_bd_of_week(d, off)
        if x is not None:
            return x
    return d
def dl_of(rule):
    k = rule.get('kind')
    n = int(rule.get('n') or 0)
    if k == 'nth_business_day_after_today':
        return nth_bd_after(today, n)
    if k == 'nth_calendar_day_after_today':
        return today + datetime.timedelta(days=n)
    if k == 'end_of_month':
        first = (today.replace(day=28) + datetime.timedelta(days=4)).replace(day=1)
        return first - ONE
    if k == 'fixed_date':
        return datetime.date.fromisoformat(str(rule.get('fixed')))
    return None
"""

_PARSE_NOW = """
raw = str(vars.get('now') or '').strip().replace('Z', '')
state = 'ok'
try:
    now = datetime.datetime.fromisoformat(raw[:19])
except Exception:
    try:
        now = datetime.datetime.fromisoformat(raw[:10] + 'T09:00')
    except Exception:
        now = datetime.datetime.now()
        state = 'now_missing'
today = now.date()
"""


def _indent(body: str, spaces: int = 4) -> str:
    return textwrap.indent(body.strip("\n"), " " * spaces)


def _wrap(imports: Sequence[str], body: str, fallback: str) -> str:
    """Assemble ``imports`` + ``try: body except Exception as e: fallback`` and check the result."""
    head = "\n".join(f"import {name}" for name in imports)
    code = f"{head}\ntry:\n{_indent(body)}\nexcept Exception as e:\n{_indent(fallback)}\n"
    return assert_safe_code(strip_code(code), "code snippet")


def _calendar_cfg(ir: PolicyIR) -> dict[str, Any]:
    return {
        "holidays": [day.isoformat() for day in ir.calendar.holidays],
        "weekdays": list(ir.calendar.business_weekdays),
    }


def _rule_cfg(rule: DeadlineRule) -> dict[str, Any]:
    return {
        "kind": rule.kind,
        "n": rule.n,
        "fixed": rule.fixed.isoformat() if rule.fixed else None,
        "text": rule.custom_rule_text,
    }


def _band_cfg(ir: PolicyIR) -> list[dict[str, Any]]:
    return [
        {
            "name": band.name,
            "slug": naming.slug(band.name),
            "min": band.days_late_min,
            "max": band.days_late_max,
            "attempts": band.persuasion_attempts,
            "deadline": _rule_cfg(band.deadline),
        }
        for band in ir.bands
    ]


def set_choice_snippet(situation: str) -> str:
    """The body of an ``end_<situation>`` code node: it records how the call ended for the ``decide`` node."""
    return assert_safe_code(f"vars['chosen_end'] = {py_literal(situation)}\n", "end node")


_GUARD_BODY = """
LATCHES = __LATCHES__
blocked = 'no'
sit = str(vars.get('call_situation') or '').strip().lower()
for pair in LATCHES:
    if str(vars.get(pair[0]) or '').strip().lower() == 'yes':
        blocked = 'yes'
    elif sit != '' and sit in [str(x).lower() for x in pair[1:]]:
        blocked = 'yes'
vars[__VAR__] = blocked
"""

_GUARD_FALLBACK = """
vars[__VAR__] = 'no'
"""


def guard_snippet(variable: str, latches: Sequence[tuple[str, Sequence[str]]]) -> str:
    """The body of a ``guard_<intent>`` code node: is the exit of an overridden intent blocked?

    ``latches`` lists ``(latch variable, situations)`` for every intent whose precedence blocks the exit: the extraction
    ``<intent>_seen`` (``yes`` once the intent was expressed at any point of the call) and the ``call_situation`` values
    that mean the same. The node writes ``yes`` or ``no`` to ``variable``; a failing snippet writes ``no`` (the exit is then
    taken, as before the guard existed).
    """
    data = [[seen, *situations] for seen, situations in latches]
    body = _GUARD_BODY.replace("__LATCHES__", json_literal(data)).replace("__VAR__", py_literal(variable))
    return _wrap(["json"], body, _GUARD_FALLBACK.replace("__VAR__", py_literal(variable)))


_PROMISE_FIX_BODY = """
vars['promise_guard'] = 'handled'
vars['promise_fix_text'] = str(vars.get('promise_guard_say') or __SAY__)
"""

_PROMISE_FIX_FALLBACK = """
vars['promise_guard'] = 'handled'
vars['promise_fix_text'] = vars.get('promise_fix_text') or __SAY__
"""


def promise_fix_snippet() -> str:
    """The body of the ``promise_fix`` code node (guard recovery).

    The ``create_promise`` pre-hook writes ``promise_guard`` (``blocked: ...``) and ``promise_guard_say`` (the spoken
    correction) when it blocks a call. ``negotiate``'s first transition fires on ``promise_guard`` containing ``blocked``
    and leads here: the node marks the block ``handled`` (so the transition fires once per block) and copies the
    correction to ``promise_fix_text``, which the ``promise_fix_say`` node speaks before the conversation resumes.
    """
    say = py_literal(PROMISE_FIX_DEFAULT)
    return _wrap([], _PROMISE_FIX_BODY.replace("__SAY__", say), _PROMISE_FIX_FALLBACK.replace("__SAY__", say))


# --------------------------------------------------------------------------------------------------------------------
# setup
# --------------------------------------------------------------------------------------------------------------------

_SETUP_BODY = """
CFG = __CFG__
__PARSE_NOW__
__CALENDAR__
this_monday = today - datetime.timedelta(days=today.weekday())
last_needed = today + datetime.timedelta(days=CFG['table_min_days'])
for b in CFG['bands']:
    dl = dl_of(b['deadline'])
    vars['deadline_' + b['slug']] = dl.isoformat() if dl is not None else ''
    if dl is not None and dl + datetime.timedelta(days=3) > last_needed:
        last_needed = dl + datetime.timedelta(days=3)
if last_needed > today + datetime.timedelta(days=CFG['table_max_days']):
    last_needed = today + datetime.timedelta(days=CFG['table_max_days'])
lines = []
for i in range((last_needed - today).days + 1):
    d = today + datetime.timedelta(days=i)
    wk = (d - this_monday).days // 7
    week = 'this week' if wk == 0 else ('next week' if wk == 1 else 'in ' + str(wk) + ' weeks')
    if isbd(d):
        kind = 'business day'
    else:
        kind = 'BANK HOLIDAY, not a business day' if d.isoformat() in hol else 'weekend, not a business day'
        alt = None
        if CFG['shift'] == 'next_business_day':
            alt = next_bd(d)
        elif CFG['shift'] == 'previous_business_day':
            alt = prev_bd(d)
            if alt < today:
                alt = next_bd(d)
        if alt is not None:
            kind = kind + ' -> if the customer names this day, offer ' + spoken(alt) + ' (' + alt.isoformat() + ') instead'
        else:
            kind = kind + ' -> this day cannot be recorded; ask for another day'
    rel = ' (today)' if i == 0 else (' (tomorrow)' if i == 1 else '')
    lines.append(d.isoformat() + ' = ' + spoken(d) + rel + ' [' + week + '] ' + kind)
nb = next_bd(today)
hhmm = '%02d:%02d' % (now.hour, now.minute)
vars['today_iso'] = today.isoformat()
vars['today_spoken'] = spoken(today)
vars['weekday_name'] = WD[today.weekday()]
vars['now_time'] = hhmm
vars['today_is_business_day'] = 'yes' if isbd(today) else 'no'
vars['next_business_day_iso'] = nb.isoformat()
vars['next_business_day_spoken'] = spoken(nb)
vars['date_table'] = '\\n'.join(lines)
this_week = last_bd_of_week(today, 0)
kind1 = CFG.get('attempt1_kind')
a1 = None
if kind1 == 'today':
    a1 = today if isbd(today) else nb
elif kind1 in ('last_business_day_this_week', 'last_business_day_next_week_if_today_is_last'):
    strictly = kind1 == 'last_business_day_next_week_if_today_is_last'
    usable = this_week is not None and (this_week > today if strictly else this_week >= today)
    a1 = this_week if usable else next_weeks_last(today)
if a1 is not None:
    vars['attempt1_date'] = a1.isoformat()
    vars['attempt1_spoken'] = spoken(a1)
cb = CFG.get('callback')
facts = 'Now is ' + spoken(today) + ', ' + hhmm + '.'
if cb is not None:
    mode = cb['allowed_days']
    lo = cb['start']
    hi = cb['end']
    span = 'from ' + lo + ' to ' + hi + (', both included' if cb['inclusive'] else ', the end time excluded')
    parts = []
    if mode == 'custom':
        facts = facts + ' Callback rule: ' + cb['text']
    else:
        days = []
        if mode == 'today_or_next_business_day':
            days = [today, nb]
        elif mode == 'next_business_day':
            days = [nb]
        else:
            x = today
            for _ in range(14):
                if isbd(x):
                    days.append(x)
                x = x + ONE
        for d in days:
            if d == today:
                if cb['after_now'] and hhmm >= hi:
                    continue
                later = ', but only a time later than ' + hhmm if cb['after_now'] else ''
                parts.append('today ' + spoken(today) + ' (' + today.isoformat() + ') ' + span + later)
            else:
                label = 'the next business day ' if d == nb else ''
                parts.append(label + spoken(d) + ' (' + d.isoformat() + ') ' + span)
        facts = facts + ' A callback can be booked only for: ' + '; or '.join(parts) + '. Nothing else is allowed.'
        if not parts:
            facts = 'Now is ' + spoken(today) + ', ' + hhmm + '. No callback time is available today; the person may name another allowed day.'
    vars['callback_facts'] = facts
else:
    vars['callback_facts'] = facts
vars['setup_state'] = state
"""

_SETUP_FALLBACK = """
vars['setup_state'] = 'error: ' + str(e)[:200]
vars['date_table'] = vars.get('date_table') or ''
vars['callback_facts'] = vars.get('callback_facts') or ''
vars['today_spoken'] = vars.get('today_spoken') or ''
vars['today_iso'] = vars.get('today_iso') or ''
"""


def setup_snippet(ir: PolicyIR) -> str:
    """Code of the ``setup`` node: today's facts, the date table, the first proposal date and the callback facts.

    Reads ``vars['now']`` (a missing or unreadable value falls back to the real clock and sets
    ``setup_state='now_missing'``). Writes the variables listed in :data:`VARS_WRITTEN` ``['setup']`` and
    ``deadline_<band>`` (ISO) for every band whose deadline rule is computable.
    """
    callback = ir.third_party.callback
    first_step = min(ir.persuasion.steps, key=lambda s: s.attempt) if ir.persuasion.steps else None
    attempt1_kind = first_step.date if first_step and first_step.date in (
        "today", "last_business_day_this_week", "last_business_day_next_week_if_today_is_last"
    ) else None
    cfg: dict[str, Any] = {
        **_calendar_cfg(ir),
        "bands": _band_cfg(ir),
        "table_min_days": DATE_TABLE_MIN_DAYS,
        "table_max_days": DATE_TABLE_MAX_DAYS,
        "shift": ir.promise_dates.non_business_day_shift,
        "attempt1_kind": attempt1_kind,
        "callback": None
        if callback is None
        else {
            "allowed_days": callback.allowed_days,
            "start": callback.window_start,
            "end": callback.window_end,
            "inclusive": callback.inclusive,
            "after_now": callback.must_be_after_now,
            "text": callback.custom_rule_text,
        },
    }
    body = (
        _SETUP_BODY.replace("__CFG__", json_literal(cfg))
        .replace("__PARSE_NOW__", _PARSE_NOW.strip("\n"))
        .replace("__CALENDAR__", _CALENDAR_FUNCS.strip("\n"))
    )
    return _wrap(["datetime", "json"], body, _SETUP_FALLBACK)


# --------------------------------------------------------------------------------------------------------------------
# account facts
# --------------------------------------------------------------------------------------------------------------------

_ACCOUNT_BODY = """
CFG = __CFG__
__PARSE_NOW__
__CALENDAR__
__MONEY__
def amt(c):
    return (fmt_cents(c) if c is not None else 'an unknown amount') + (' ' + CFG['currency'] if CFG['currency'] and c is not None else '')
def fill(tpl, p):
    out = tpl
    for k in list(p.keys()):
        if p.get(k) is not None:
            out = out.replace('{' + str(k) + '}', str(p.get(k)))
    return out
def load_products():
    p = vars.get('products')
    if isinstance(p, str):
        try:
            p = json.loads(p)
        except Exception:
            p = None
    if isinstance(p, list) and p:
        return p
    found = dict()
    marker = '.' + CFG['list_field'] + '.'
    for k in list(vars.keys()):
        ks = str(k)
        if marker in ks:
            tail = ks.split(marker, 1)[1]
            parts = tail.split('.', 1)
            if len(parts) == 2 and parts[0].isdigit():
                found.setdefault(int(parts[0]), dict())[parts[1]] = vars.get(k)
    return [found[i] for i in sorted(found)]
def band_of(late):
    best = None
    gap = None
    for b in CFG['bands']:
        if b['min'] <= late <= b['max']:
            return b
        g = b['min'] - late if late < b['min'] else late - b['max']
        if gap is None or g < gap:
            best = b
            gap = g
    return best
this_week = last_bd_of_week(today, 0)
def proposal(kind, latest):
    if kind == 'customer':
        return None
    if kind == 'deadline':
        return latest
    if kind == 'today':
        r = today if isbd(today) else next_bd(today)
    else:
        strictly = kind == 'last_business_day_next_week_if_today_is_last'
        usable = this_week is not None and (this_week > today if strictly else this_week >= today)
        r = this_week if usable else next_weeks_last(today)
    if latest is not None and r > latest:
        r = latest
    return r
products = load_products()
out = []
ids = []
stmts = []
deadlines = dict()
for i, p in enumerate(products):
    if not isinstance(p, dict):
        continue
    pid = str(p.get(CFG['id_key']) or '').strip()
    if not pid:
        raise ValueError('a product has no ' + CFG['id_key'] + ': its id cannot be invented')
    ids.append(pid)
    ptype = str(p.get(CFG['type_key']) or '') if CFG['type_key'] else ''
    name = fill(CFG['naming'].get(ptype) or ptype.replace('_', ' '), p) or ('product ' + pid)
    ffield = CFG['floors'].get(ptype)
    floor = to_cents(p.get(ffield)) if ffield else None
    flabel = ffield.replace('_', ' ') if ffield else 'floor'
    due = to_cents(p.get(CFG['amount_field']))
    try:
        late = int(float(p.get(CFG['days_field']) or 0))
    except Exception:
        late = 0
    band = band_of(late)
    bname = band['name'] if band else ''
    attempts = band['attempts'] if band else 1
    dl = dl_of(band['deadline']) if band else None
    latest = None
    if dl is not None:
        latest = prev_bd(dl) if CFG['business_only'] else dl
    s = []
    s.append(pid + ': ' + name + '. Past-due amount ' + amt(due) + ', ' + str(late) + ' days late (' + bname + ' band).')
    if floor is not None:
        s.append('   Floor (' + flabel + '): ' + amt(floor) + '.')
    else:
        s.append('   Floor: none for this product - only the full past-due amount can be offered.')
    if latest is not None:
        extra = '' if dl == latest else ' (the deadline rule gives ' + dl.isoformat() + ', which is not a business day)'
        s.append('   Deadline (latest promise date): ' + latest.isoformat() + ' = ' + short(latest) + '.' + extra)
    else:
        s.append('   Deadline: apply this rule: ' + (band['deadline']['text'] if band else '') + ' (no date could be computed).')
    offer = ''
    if floor is not None:
        offer = fill(CFG['floor_offer'].replace('{floor_label}', flabel).replace('{floor_amount}', amt(floor)), p)
        offer = offer.replace('{name}', name)
        if CFG['floor_note'] and CFG['floor_note'] not in offer:
            offer = offer + ' ' + CFG['floor_note']
        s.append('   Floor offer - when the customer says the full amount is too much, say exactly: "' + offer + '"')
    steps = [x for x in CFG['steps'] if (not x['bands']) or bname in x['bands']]
    steps = steps[:attempts]
    while steps and len(steps) < attempts:
        steps.append(steps[-1])
    s.append('   Persuasion attempts for ' + pid + ': EXACTLY ' + str(attempts) + ' (' + bname + ' band). Make them one at a time, only after a refusal or a non-answer:')
    for n, st in enumerate(steps):
        d = proposal(st['date'], latest)
        if st['date'] == 'deadline' and latest is not None:
            when = ' by ' + short(latest)
        elif d is not None:
            when = ' on ' + short(d)
        else:
            when = ' on the day the customer names'
        if st['amount'] == 'floor' and floor is not None:
            q = 'Could you pay at least the ' + flabel + ' of ' + amt(floor) + when + '?'
            if CFG['floor_note']:
                q = q + ' ' + CFG['floor_note']
            s.append('     Attempt ' + str(n + 1) + ' - ask: "' + q + '"')
        else:
            q = 'Could you pay the ' + amt(due) + ' for your ' + name + when + '?'
            tail = ''
            if st['amount'] == 'current' and floor is not None:
                tail = ' (if they already said the full amount is too much, ask for the ' + flabel + ' ' + amt(floor) + ' instead)'
            s.append('     Attempt ' + str(n + 1) + ' - ask: "' + q + '"' + tail + '.')
    s.append('     After attempt ' + str(attempts) + ' is refused: make NO further offer for ' + pid + '.')
    allowed = []
    allowed_iso = []
    allowed_say = []
    d = today if CFG['allow_today'] else today + ONE
    capped = latest is not None and CFG['not_after_deadline']
    horizon = today + datetime.timedelta(days=CFG['horizon'])
    end = horizon
    until = 'open'
    if capped:
        if latest <= horizon:
            end = latest
            until = ''
        else:
            until = latest.isoformat()
    for _ in range(CFG['horizon'] + 2):
        if d > end:
            break
        if (not CFG['business_only']) or isbd(d):
            allowed.append(d.isoformat() + ' ' + WD[d.weekday()])
            allowed_iso.append(d.isoformat())
            allowed_say.append(short(d))
        d = d + ONE
    if not allowed_iso:
        s.append('   Allowed promise dates: none - no day is left up to the deadline. Record NO promise for ' + pid + ': say that you cannot record a payment date for it.')
    elif len(allowed) <= CFG['list_limit']:
        s.append('   Allowed promise dates: ' + ', '.join(allowed) + '.')
    else:
        first = datetime.date.fromisoformat(allowed_iso[0])
        last_day = latest if capped else datetime.date.fromisoformat(allowed_iso[-1])
        text = 'every ' + ('business day' if CFG['business_only'] else 'day') + ' from ' + first.isoformat() + ' (' + WD[first.weekday()] + ')'
        text = text + (' to ' + last_day.isoformat() + ' (' + WD[last_day.weekday()] + ')' if capped else ' on')
        if CFG['business_only']:
            text = text + '; weekends and bank holidays are not allowed'
            inside = [h for h in sorted(hol) if first.isoformat() <= h <= (last_day.isoformat() if capped else allowed_iso[-1]) and datetime.date.fromisoformat(h).weekday() in bw]
            if inside:
                text = text + ' (bank holidays in this range: ' + ', '.join(inside) + ')'
        s.append('   Allowed promise dates: ' + text + '.')
    out.append('\\n'.join(s))
    parts = []
    for f in CFG['state_fields']:
        if f == CFG['amount_field']:
            parts.append('has a past-due amount of ' + amt(due))
        elif f == CFG['days_field']:
            parts.append('is ' + str(late) + (' day late' if late == 1 else ' days late'))
        elif p.get(f) is not None:
            parts.append('has ' + f.replace('_', ' ') + ' ' + str(p.get(f)))
    stmts.append('Your ' + name + ' ' + ' and '.join(parts) + '.' if parts else 'Your ' + name + '.')
    vars[pid + '_deadline'] = latest.isoformat() if latest is not None else ''
    vars[pid + '_deadline_spoken'] = short(latest) if latest is not None else ''
    vars[pid + '_floor'] = fmt_cents(floor) if floor is not None else ''
    vars[pid + '_past_due'] = fmt_cents(due) if due is not None else ''
    vars['allowed_' + pid] = ','.join(allowed_iso)
    vars['allowed_spoken_' + pid] = ','.join(allowed_say[:CFG['spoken_limit']])
    vars['allowed_count_' + pid] = str(len(allowed_iso))
    if until:
        vars['until_' + pid] = until
    vars[pid + '_band'] = bname
    vars[pid + '_name'] = name
    if latest is not None and CFG['not_after_deadline']:
        deadlines[pid] = latest
marked = []
for line in str(vars.get('date_table') or '').split('\\n'):
    try:
        d0 = datetime.date.fromisoformat(line[:10])
    except Exception:
        marked.append(line)
        continue
    late_for = [k for k in ids if k in deadlines and d0 > deadlines[k]]
    if late_for and len(late_for) == len(ids):
        line = line + ' | AFTER THE DEADLINE - not allowed' + (': offer the deadline instead' if CFG['offer_deadline'] else '')
    elif late_for:
        line = line + ' | after the deadline of ' + ', '.join(late_for) + ' - not allowed for ' + ', '.join(late_for)
    marked.append(line)
vars['date_table'] = '\\n'.join(marked)
question =CFG['today_question_all'] if len(stmts) > 1 and CFG['today_question_all'] else CFG['today_question']
vars['debt_statement'] = ('Thank you. ' + CFG['intro'] + ' ' + ' '.join(stmts) + ' ' + question).strip()
vars['product_ids'] = ','.join(ids)
vars['product_count'] = str(len(ids))
vars['account_facts'] = 'LATE PRODUCTS (discuss them in this order):\\n' + '\\n'.join(out)
vars['account_state'] = 'ok' if ids else 'empty'
vars['identity_verified'] = 'yes'
"""

_ACCOUNT_FALLBACK = """
vars['account_state'] = 'error: ' + str(e)[:200]
vars['account_facts'] = vars.get('account_facts') or 'The account facts could not be computed. Say you do not have that information.'
vars['debt_statement'] = vars.get('debt_statement') or __FALLBACK_STATEMENT__
vars['product_ids'] = vars.get('product_ids') or ''
vars['product_count'] = vars.get('product_count') or '0'
vars['identity_verified'] = 'yes'
"""


def _statement_intro(ir: PolicyIR) -> str:
    intro = extra_phrase(ir.phrases.extra, "disclosure_intro", "intro")
    return intro or f"I am calling from {ir.disclosure.team_phrase}."


def _floor_offer_template(ir: PolicyIR) -> str:
    template = extra_phrase(ir.phrases.extra, "floor_offer")
    return template or "I can offer you the {floor_label} of {floor_amount} instead."


def _amount_and_days_fields(ir: PolicyIR) -> tuple[str, str]:
    fields = list(ir.disclosure.state_fields)
    amount = next((f for f in fields if "late" not in f and "day" not in f), "past_due")
    days = next((f for f in fields if "day" in f), "days_late")
    return amount, days


def account_facts_snippet(ir: PolicyIR, roles: AccountRoles | None = None) -> str:
    """Code of the ``account_facts`` node: per-product facts, scripts and the debt statement from ``vars['products']``.

    Reads the products written by the ``load_account`` node's response mapping (a list, a JSON string or flattened
    ``....<list field>.<i>.<field>`` keys) and ``vars['now']``. ``roles`` names the keys of an item that hold the product
    id and the product type (found in the ``get_account`` contract, see :mod:`builder.compiler.roles`; default: the kit's
    ``product_id`` / ``type``). A product without an id makes the node fail with ``account_state = error: ...`` - an id is
    never invented. Writes ``account_facts``, ``debt_statement``,
    ``product_ids`` (csv), ``product_count`` and, per product id ``pN``: ``pN_deadline`` (latest valid promise date),
    ``pN_deadline_spoken`` (the same day as weekday + day + month), ``pN_floor``, ``pN_past_due`` (two decimals),
    ``pN_band``, ``pN_name``, ``allowed_pN`` (ISO csv of EVERY allowed day up to the deadline - or up to a horizon of a
    year, see ``until_pN``), ``allowed_count_pN`` (how many; ``0`` = no day is left, the guard then blocks every date),
    ``until_pN`` (only for a list cut at the horizon: ``open`` = later days are allowed, or the ISO deadline up to which
    later days are allowed) and ``allowed_spoken_pN`` (the first days spoken, same order; the promise guard names the next
    allowed day from it). The prompt lists up to 30 allowed days one by one and summarises longer lists as a range.

    It also re-renders ``date_table`` (written by ``setup``): every row after a product's deadline gets
    ``| after the deadline of pN - not allowed for pN`` or, when the day is after every product's deadline,
    ``| AFTER THE DEADLINE - not allowed`` (plus ``: offer the deadline instead`` when the policy offers the deadline).
    Without that mark a model called a day after the deadline "a business day" and read it back.
    """
    amount_field, days_field = _amount_and_days_fields(ir)
    roles = roles or AccountRoles()
    cfg: dict[str, Any] = {
        **_calendar_cfg(ir),
        "bands": _band_cfg(ir),
        "list_field": roles.list_field,
        "id_key": roles.id_key,
        "type_key": roles.type_key,
        "horizon": ALLOWED_HORIZON_DAYS,
        "list_limit": ALLOWED_LIST_LIMIT,
        "spoken_limit": ALLOWED_SPOKEN_LIMIT,
        "naming": dict(ir.disclosure.product_naming),
        "floors": dict(ir.amounts.floor_field_by_type),
        "floor_note": ir.amounts.floor_note.strip(),
        "floor_offer": _floor_offer_template(ir),
        "currency": ir.currency_word.strip(),
        "state_fields": list(ir.disclosure.state_fields),
        "amount_field": amount_field,
        "days_field": days_field,
        "allow_today": ir.promise_dates.allow_today,
        "business_only": ir.promise_dates.business_days_only,
        "not_after_deadline": ir.promise_dates.not_after_deadline,
        "offer_deadline": ir.promise_dates.after_deadline == "offer_deadline",
        "steps": [
            {"attempt": s.attempt, "bands": list(s.bands), "amount": s.amount, "date": s.date}
            for s in sorted(ir.persuasion.steps, key=lambda step: step.attempt)
        ],
        "intro": _statement_intro(ir),
        "today_question": today_question(ir),
        "today_question_all": extra_phrase(ir.phrases.extra, "today_question_all"),
    }
    fallback_statement = py_literal(
        " ".join(["Thank you.", _statement_intro(ir), cfg["today_question"]]).strip()
    )
    body = (
        _ACCOUNT_BODY.replace("__CFG__", json_literal(cfg))
        .replace("__PARSE_NOW__", _PARSE_NOW.strip("\n"))
        .replace("__CALENDAR__", _CALENDAR_FUNCS.strip("\n"))
        .replace("__MONEY__", MONEY_FUNCS.rstrip("\n"))
    )
    fallback = _ACCOUNT_FALLBACK.replace("__FALLBACK_STATEMENT__", fallback_statement)
    return _wrap(["datetime", "json"], body, fallback)


# --------------------------------------------------------------------------------------------------------------------
# callback and promise checks
# --------------------------------------------------------------------------------------------------------------------

_CHECK_CALLBACK_BODY = """
CFG = __CFG__
__PARSE_NOW__
__CALENDAR__
cb = CFG.get('callback')
vars['callback_check'] = 'unchecked'
vars['callback_suggestion'] = ''
if cb is not None and cb['allowed_days'] != 'custom':
    raw_d = str(vars.get('chk_cb_date') or '').strip()[:10]
    raw_t = str(vars.get('chk_cb_time') or '').strip().lower().replace('.', ':').replace('h', ':')
    parts = raw_t.split(':')
    try:
        day = datetime.date.fromisoformat(raw_d)
        if len(parts) not in (2, 3) or any(not part.isdigit() for part in parts):
            raise ValueError('not a HH:MM time')
        hh = int(parts[0])
        mm = int(parts[1])
        slot_time = datetime.time(hh, mm)
        slot_minutes = hh * 60 + mm
    except Exception:
        day = None
    if day is None:
        vars['callback_check'] = 'unreadable'
    else:
        mode = cb['allowed_days']
        if mode == 'today_or_next_business_day':
            day_ok = day in (today, next_bd(today))
        elif mode == 'next_business_day':
            day_ok = day == next_bd(today)
        else:
            day_ok = day >= today and isbd(day)
        lo = cb['start']
        hi = cb['end']
        start = int(lo[:2]) * 60 + int(lo[3:])
        end = int(hi[:2]) * 60 + int(hi[3:])
        in_window = start <= slot_minutes <= end if cb['inclusive'] else start <= slot_minutes < end
        if not day_ok:
            vars['callback_check'] = 'not_allowed_day'
            vars['callback_suggestion'] = next_bd(today).isoformat() + ' ' + lo
        elif not in_window:
            vars['callback_check'] = 'outside_window'
            vars['callback_suggestion'] = day.isoformat() + ' ' + lo
        elif cb['after_now'] and datetime.datetime.combine(day, slot_time) <= now:
            vars['callback_check'] = 'not_after_now'
            vars['callback_suggestion'] = next_bd(today).isoformat() + ' ' + lo
        else:
            vars['callback_check'] = 'ok'
"""

_CHECK_CALLBACK_FALLBACK = """
vars['callback_check'] = 'error'
vars['callback_suggestion'] = ''
"""


def check_callback_snippet(ir: PolicyIR) -> str:
    """Code that checks a proposed callback slot (mirror of :func:`builder.policy.calendar.check_callback`).

    Input ``vars``: ``now``, ``chk_cb_date`` (ISO) and ``chk_cb_time`` (``HH:MM``). Output: ``callback_check`` - one of
    ``ok``, ``not_allowed_day``, ``outside_window``, ``not_after_now``, ``unreadable``, ``unchecked`` (custom rule or no
    callback rule), ``error`` - and ``callback_suggestion`` (``YYYY-MM-DD HH:MM`` to offer instead, or empty).
    """
    callback = ir.third_party.callback
    cfg: dict[str, Any] = {
        **_calendar_cfg(ir),
        "bands": [],
        "callback": None
        if callback is None
        else {
            "allowed_days": callback.allowed_days,
            "start": callback.window_start,
            "end": callback.window_end,
            "inclusive": callback.inclusive,
            "after_now": callback.must_be_after_now,
        },
    }
    body = (
        _CHECK_CALLBACK_BODY.replace("__CFG__", json_literal(cfg))
        .replace("__PARSE_NOW__", _PARSE_NOW.strip("\n"))
        .replace("__CALENDAR__", _CALENDAR_FUNCS.strip("\n"))
    )
    return _wrap(["datetime", "json"], body, _CHECK_CALLBACK_FALLBACK)


_CHECK_PROMISE_BODY = """
CFG = __CFG__
__PARSE_NOW__
__CALENDAR__
__MONEY__
pid = str(vars.get('chk_product_id') or '').strip()
for k in [x for x in str(vars.get('product_ids') or '').split(',') if x]:
    if k.lower() == pid.lower():
        pid = k
vars['promise_date_check'] = 'ok'
vars['promise_amount_check'] = 'ok'
vars['promise_check'] = 'ok'
vars['promise_suggestion'] = ''
latest_s = str(vars.get(pid + '_deadline') or '')
if not pid or (pid + '_past_due') not in vars:
    vars['promise_check'] = 'unknown_product'
else:
    try:
        day = datetime.date.fromisoformat(str(vars.get('chk_date') or '').strip()[:10])
    except Exception:
        day = None
    latest = None
    if latest_s:
        latest = datetime.date.fromisoformat(latest_s)
    limit = latest if CFG['not_after_deadline'] else None
    last_allowed = prev_bd(limit) if limit is not None and CFG['business_only'] else limit
    earliest = today if CFG['allow_today'] else today + ONE
    if CFG['business_only'] and not isbd(earliest):
        earliest = next_bd(earliest)
    if day is None:
        vars['promise_date_check'] = 'unreadable'
    elif day < today or (day == today and not CFG['allow_today']):
        vars['promise_date_check'] = 'before_today'
        vars['promise_suggestion'] = earliest.isoformat() if last_allowed is None or earliest <= last_allowed else ''
    elif limit is not None and day > limit:
        vars['promise_date_check'] = 'after_deadline'
        vars['promise_suggestion'] = last_allowed.isoformat() if CFG['after_deadline'] == 'offer_deadline' and last_allowed is not None and last_allowed >= earliest else ''
    elif CFG['business_only'] and not isbd(day):
        vars['promise_date_check'] = 'not_business_day'
        if CFG['shift'] == 'previous_business_day':
            sug = prev_bd(day)
            if sug < earliest:
                sug = next_bd(day)
        elif CFG['shift'] == 'next_business_day':
            sug = next_bd(day)
        else:
            sug = None
        if sug is not None and last_allowed is not None and sug > last_allowed:
            sug = last_allowed
        vars['promise_suggestion'] = sug.isoformat() if sug is not None and sug >= earliest else ''
    try:
        cents = to_cents(vars.get('chk_amount'))
        full = to_cents(vars.get(pid + '_past_due'))
        fl = to_cents(vars.get(pid + '_floor'))
        if cents is None or full is None:
            raise ValueError('unreadable amount')
        floor_c = min(fl, full) if fl is not None else None
        low = floor_c if floor_c is not None else full
        if cents > full:
            vars['promise_amount_check'] = 'above_full'
            vars['promise_suggestion'] = vars['promise_suggestion'] or fmt_cents(full)
        elif cents <= 0 or cents < low:
            vars['promise_amount_check'] = 'below_floor'
        elif cents not in (full, floor_c) and not CFG['accept_between']:
            vars['promise_amount_check'] = 'not_allowed'
    except Exception:
        vars['promise_amount_check'] = 'unreadable'
    if vars['promise_date_check'] != 'ok':
        vars['promise_check'] = vars['promise_date_check']
    elif vars['promise_amount_check'] != 'ok':
        vars['promise_check'] = vars['promise_amount_check']
"""

_CHECK_PROMISE_FALLBACK = """
vars['promise_check'] = 'error'
vars['promise_suggestion'] = ''
"""


def check_promise_snippet(ir: PolicyIR) -> str:
    """Code that checks a proposed promise (date and amount) against the IR's rules and the account facts.

    Input ``vars``: ``now``, ``chk_product_id``, ``chk_amount``, ``chk_date`` (ISO) and the per-product variables the
    ``account_facts`` node wrote (``pN_deadline``, ``pN_past_due``, ``pN_floor``). Output: ``promise_check`` - ``ok``,
    ``before_today``, ``after_deadline``, ``not_business_day``, ``above_full``, ``below_floor``, ``not_allowed``,
    ``unreadable``, ``unknown_product`` or ``error`` (the date verdict wins over the amount verdict) - the two partial
    verdicts ``promise_date_check`` / ``promise_amount_check``, and ``promise_suggestion`` (a date or amount to offer).
    """
    cfg: dict[str, Any] = {
        **_calendar_cfg(ir),
        "bands": [],
        "allow_today": ir.promise_dates.allow_today,
        "business_only": ir.promise_dates.business_days_only,
        "not_after_deadline": ir.promise_dates.not_after_deadline,
        "after_deadline": ir.promise_dates.after_deadline,
        "shift": ir.promise_dates.non_business_day_shift,
        "accept_between": ir.amounts.accept_between_floor_and_full,
    }
    body = (
        _CHECK_PROMISE_BODY.replace("__CFG__", json_literal(cfg))
        .replace("__PARSE_NOW__", _PARSE_NOW.strip("\n"))
        .replace("__CALENDAR__", _CALENDAR_FUNCS.strip("\n"))
        .replace("__MONEY__", MONEY_FUNCS.rstrip("\n"))
    )
    return _wrap(["datetime", "json"], body, _CHECK_PROMISE_FALLBACK)


# --------------------------------------------------------------------------------------------------------------------
# decide
# --------------------------------------------------------------------------------------------------------------------

_DECIDE_BODY = """
CFG = __CFG__
def cnt(k):
    try:
        return int(float(vars.get(k) or 0))
    except Exception:
        return 0
chosen = str(vars.get('chosen_end') or '')
verified = str(vars.get('identity_verified') or '') == 'yes'
promised = cnt(CFG['promise_tool'] + '_success_count') > 0
if chosen == 'auto' and not verified:
    raw_left = vars.get(CFG['left_var']) if CFG.get('left_var') else None
    left = '' if raw_left is None else str(raw_left)
    ended = left in ('0', '0.0')
    raw_wrong = vars.get(CFG['wrong_var']) if CFG.get('wrong_var') else None
    if raw_wrong is not None and CFG.get('max_wrong'):
        try:
            ended = ended or int(float(raw_wrong)) >= int(CFG['max_wrong'])
        except Exception:
            pass
    chosen = 'identity_failed' if ended else 'identity_declined'
elif chosen == 'auto':
    sit = str(vars.get('call_situation') or '')
    chosen = sit if sit in CFG['auto_keys'] else ''
order_ids = [x for x in str(vars.get('product_ids') or '').split(',') if x]
idmap = dict()
for x in order_ids:
    idmap[x.lower()] = x
rmap = dict()
for x in CFG['reasons']:
    rmap[x.lower()] = x
latest_note = dict()
for item in str(vars.get('notes_log') or '').split(';'):
    if ':' in item:
        k, v = item.split(':', 1)
        if k.strip() and v.strip():
            latest_note[idmap.get(k.strip().lower(), k.strip())] = rmap.get(v.strip().lower(), v.strip())
if not latest_note and CFG['note_tool'] and cnt(CFG['note_tool'] + '_success_count') > 0:
    r = str(vars.get(CFG['note_tool'] + '_args.' + CFG['note_reason_field']) or '').strip()
    if r:
        k = str(vars.get(CFG['note_tool'] + '_args.' + CFG['note_product_field']) or '?').strip()
        latest_note[idmap.get(k.lower(), k)] = rmap.get(r.lower(), r)
notes = list(latest_note.items())
queue = ''
for pair in CFG['transfers']:
    if cnt(pair[0] + '_success_count') + cnt(pair[0] + '_failure_count') > 0:
        queue = pair[1]
active = []
if promised:
    active.append('promise_recorded')
if notes and CFG['mp_enabled']:
    active.append('product_notes')
service_failures = max(0, cnt(CFG['promise_tool'] + '_failure_count') - cnt('promise_guard_blocks'))
service_error = chosen == 'recording_failed' or (chosen == 'promise_recorded' and service_failures > 0)
if service_error and not promised:
    active.append('recording_failed')
if chosen == 'identity_failed':
    active.append('identity_failed')
if chosen == 'identity_declined':
    active.append('identity_declined')
if chosen in ('wrong_number', 'deceased', 'unclear_party'):
    active.append(chosen)
booked = cnt(CFG['callback_tool'] + '_success_count') > 0
if booked or chosen in ('callback_booked', 'callback_not_booked'):
    active.append('callback_booked' if booked else 'callback_not_booked')
if queue:
    active.append('transfer:' + queue)
if chosen and chosen not in active and chosen not in CFG['structural']:
    active.append(chosen)
rows = CFG['rows']
def row_for(key):
    for r in rows:
        if r[0] == key and (r[1] == '' or r[1] == verified):
            return r
    return None
def notes_decision():
    if not notes or CFG['mp_rule'] != 'first_non_paid_note':
        return None
    rank = dict()
    for i, x in enumerate(order_ids):
        rank[x] = i
    ranked = [(rank.get(n[0], len(rank)), i, n[1]) for i, n in enumerate(notes)]
    ranked.sort()
    reasons = [x[2] for x in ranked]
    paid = CFG.get('mp_paid')
    unpaid = [r for r in reasons if r != paid]
    if not unpaid:
        if paid is not None and all(x in latest_note for x in order_ids):
            return CFG.get('mp_all_paid')
        return None
    first = unpaid[0]
    oc = CFG.get('mp_notes_outcome') or CFG['reason_outcome'].get(first)
    if not oc:
        return None
    return [oc, first]
def resolve(key):
    if key == 'product_notes':
        got = notes_decision()
        if got is not None:
            return got
    r = row_for(key)
    if r is not None:
        return [r[2], r[3]]
    return CFG['sections'].get(key)
transfer_key = None
for k in CFG['order']:
    if k.startswith('transfer:') and k in active:
        transfer_key = k
        break
if transfer_key is None:
    for k in active:
        if k.startswith('transfer:'):
            transfer_key = k
            break
res = None
decided = ''
tap = CFG.get('tap')
if promised and transfer_key and tap is not None:
    reason = tap[1]
    if not reason:
        r = row_for(transfer_key)
        if r is None and chosen:
            r = row_for(chosen)
        if r is not None:
            reason = r[3]
    res = [tap[0], reason]
    decided = transfer_key
elif promised and CFG['promise_wins']:
    res = resolve('promise_recorded')
    decided = 'promise_recorded'
if res is None:
    for k in CFG['order']:
        if k in active:
            res = resolve(k)
            if res is not None:
                decided = k
                break
if res is None:
    reached = cnt(CFG['verify_tool'] + '_success_count') + cnt(CFG['verify_tool'] + '_failure_count') > 0
    if verified:
        res = CFG['fb_verified']
        decided = 'fallback_verified'
    elif reached or chosen in ('identity_failed', 'identity_declined') or CFG.get('fb_unreached') is None:
        res = CFG['fb_declined']
        decided = 'fallback_unverified'
    else:
        res = CFG['fb_unreached']
        decided = 'fallback_unreached'
vars['final_outcome'] = res[0]
if res[1]:
    vars['final_reason'] = res[1]
vars['decide_situation'] = decided
vars['decide_state'] = 'ok'
"""

_DECIDE_FALLBACK = """
vars['final_outcome'] = vars.get('final_outcome') or __DEFAULT_OUTCOME__
__DEFAULT_REASON__
vars['decide_state'] = 'error: ' + str(e)[:200]
"""


def decide_config(
    ir: PolicyIR,
    *,
    transfer_nodes: Mapping[str, str] | None = None,
    auto_keys: Sequence[str] | None = None,
    tool_names: Mapping[str, str] | None = None,
    left_variable: str = "",
    note_fields: tuple[str, str] = ("product_id", "reason"),
    wrong_variable: str = "",
    max_wrong: int = 0,
) -> dict[str, Any]:
    """The data embedded in the ``decide`` snippet (exposed for tests and the docs).

    Args:
        ir: The policy.
        transfer_nodes: ``handoff node name -> queue``; default one ``handoff_<queue>`` node per queue in the IR.
        auto_keys: Situations a ``call_situation`` extraction may name; default :func:`naming.negotiation_situations`.
        tool_names: Overrides for ``promise``, ``note``, ``callback``, ``verify`` tool names (defaults: the kit's names).
        left_variable: Response variable that counts the wrong answers still allowed (``0`` = the check failed).
        note_fields: ``(product field, reason field)`` of the notes tool, for the fallback that reads its arguments.
        wrong_variable / max_wrong: A response counter that counts wrong answers UP; the check failed at ``max_wrong``.
    """
    names = {"promise": "create_promise", "note": ir.multi_product.note_tool or "add_note",
             "callback": "schedule_callback", "verify": "verify_identity", **dict(tool_names or {})}
    transfers = dict(transfer_nodes) if transfer_nodes is not None else {
        naming.handoff_node(q): q for q in naming.transfer_queues(ir)
    }
    rows = naming.closing_rows(ir)
    precedence = ir.closing.resolved_precedence()
    stop = naming.stop_situation(ir)
    order = list(precedence)
    if stop not in order:
        order.append(stop)
    order.extend(key for key in STRUCTURAL_ORDER if key not in order)
    callback = ir.third_party.callback
    sections: dict[str, list[Any]] = {}
    for key, ref in (
        (Situation.CALLBACK_BOOKED, callback.success if callback else None),
        (Situation.CALLBACK_NOT_BOOKED, callback.failure if callback else None),
        (Situation.WRONG_NUMBER, ir.third_party.wrong_number),
        (Situation.DECEASED, ir.third_party.deceased),
        (Situation.UNCLEAR_PARTY, ir.right_party.unclear_outcome),
        (Situation.IDENTITY_FAILED, ir.identity.on_failed),
        (Situation.IDENTITY_DECLINED, ir.identity.on_declined),
        (Situation.RECORDING_FAILED, ir.recording.failure_outcome),
    ):
        if ref is not None:
            sections[str(key)] = [ref.outcome, ref.reason]
    mp = ir.multi_product
    reason_outcome: dict[str, str] = {}
    for row in ir.closing.rows:
        if row.reason and row.reason not in reason_outcome:
            reason_outcome[row.reason] = row.outcome
    tap = ir.closing.transfer_after_promise_outcome
    unreached = ir.right_party.unclear_outcome
    reasons = {row.reason for row in ir.closing.rows if row.reason} | set(mp.note_reasons)
    reasons |= {ref.reason for ref in sections_refs(ir) if ref.reason}
    if mp.paid_note_reason:
        reasons.add(mp.paid_note_reason)
    return {
        "reasons": sorted(reasons),
        "note_product_field": note_fields[0],
        "note_reason_field": note_fields[1],
        "wrong_var": wrong_variable,
        "max_wrong": int(max_wrong),
        "order": [str(key) for key in order],
        "rows": [[r.situation, r.requires_verified, r.outcome, r.reason] for r in rows],
        "sections": sections,
        "structural": [str(key) for key in STRUCTURAL_ORDER],
        "left_var": left_variable,
        "promise_tool": names["promise"],
        "note_tool": names["note"] if mp.enabled else "",
        "callback_tool": names["callback"],
        "verify_tool": names["verify"],
        "transfers": [[node, queue] for node, queue in transfers.items()],
        "auto_keys": list(auto_keys) if auto_keys is not None else naming.negotiation_situations(ir),
        "mp_enabled": bool(mp.enabled),
        "mp_rule": mp.closing_reason_rule,
        "mp_paid": mp.paid_note_reason,
        "mp_all_paid": [mp.all_paid_outcome.outcome, mp.all_paid_outcome.reason] if mp.all_paid_outcome else None,
        "mp_notes_outcome": mp.notes_outcome,
        "reason_outcome": reason_outcome,
        "tap": [tap.outcome, tap.reason] if tap is not None else None,
        "promise_wins": bool(ir.closing.promise_wins),
        "fb_verified": [ir.persuasion.stop_outcome.outcome, ir.persuasion.stop_outcome.reason],
        "fb_declined": [ir.identity.on_declined.outcome, ir.identity.on_declined.reason],
        "fb_unreached": [unreached.outcome, unreached.reason] if unreached is not None else None,
    }


def sections_refs(ir: PolicyIR) -> list[Any]:
    """The outcomes of the structural IR sections (callback, wrong number, identity ... recording failure)."""
    callback = ir.third_party.callback
    refs = [callback.success, callback.failure] if callback else []
    refs += [ir.third_party.wrong_number, ir.third_party.deceased, ir.right_party.unclear_outcome,
             ir.identity.on_failed, ir.identity.on_declined, ir.recording.failure_outcome]
    return [ref for ref in refs if ref is not None]


def decide_outcome_snippet(
    ir: PolicyIR,
    *,
    transfer_nodes: Mapping[str, str] | None = None,
    auto_keys: Sequence[str] | None = None,
    tool_names: Mapping[str, str] | None = None,
    left_variable: str = "",
    note_fields: tuple[str, str] = ("product_id", "reason"),
    wrong_variable: str = "",
    max_wrong: int = 0,
) -> str:
    """Code of the ``decide`` node: a mirror of :func:`builder.policy.outcomes.compute_outcome` working on call variables.

    Inputs (``vars``): ``chosen_end`` (the situation key an ``end_<situation>`` node recorded, or ``auto``),
    ``call_situation`` (extraction fallback for ``auto``), ``identity_verified`` (``yes`` after ``account_facts``),
    ``product_ids`` and ``notes_log`` (``pid:reason;`` entries), and the engine's counters
    ``<tool>_success_count`` / ``<tool>_failure_count`` and ``<handoff node>_success_count``. Calls the promise guard
    blocked are counted by the engine as failures; ``promise_guard_blocks`` (written by the pre-hook) is subtracted, so
    only real service failures can make the call a recording failure.
    Output: ``final_outcome``; ``final_reason`` only when the outcome has a reason (an unset variable is dropped
    from the ``close_call`` request - verified on the platform); ``decide_situation`` (the deciding key, for traces).
    """
    cfg = decide_config(
        ir, transfer_nodes=transfer_nodes, auto_keys=auto_keys, tool_names=tool_names, left_variable=left_variable,
        note_fields=note_fields, wrong_variable=wrong_variable, max_wrong=max_wrong,
    )
    fallback_ref = ir.persuasion.stop_outcome
    reason_line = (
        f"vars['final_reason'] = vars.get('final_reason') or {py_literal(fallback_ref.reason)}"
        if fallback_ref.reason
        else "pass"
    )
    fallback = _DECIDE_FALLBACK.replace("__DEFAULT_OUTCOME__", py_literal(fallback_ref.outcome)).replace(
        "__DEFAULT_REASON__", reason_line
    )
    return _wrap(["json"], _DECIDE_BODY.replace("__CFG__", json_literal(cfg)), fallback)

