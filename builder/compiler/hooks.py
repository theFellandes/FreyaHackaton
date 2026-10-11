"""Pre- and post-hooks for model-called tools: small, safe Python snippets that run around the HTTP call.

Why hooks exist (``docs/tools-and-code.md``): after a tool result the model must speak or call the next tool, but bare
data such as ``{"ok": true, "verified": false}`` often leads to silence or to ignoring a negative result. A **post-hook**
adds one plain instruction line, ``what_to_do_next``, to ``result["data"]``; the model then has something to obey. A
**pre-hook** cleans the model's arguments (amounts as numbers, ISO dates, digit-only answers) in ``config["body"]`` and
may write call variables for later steps.

Rules every generated hook follows (verified in a working builder, or documented):

* the whole body is inside ``try`` / ``except Exception: pass`` - a hook never raises;
* types are checked with ``isinstance`` before use; imports use only the documented sandbox modules, no ``{{`` / ``}}``;
* post-hooks drop the response headers (``result.pop('header', None)``) so they do not fill the model's context;
* a missing or empty argument is left untouched (never written back empty: an empty string would be *sent* empty,
  while a missing one is dropped from the request).

The condition helpers (:func:`flag_true`, :func:`is_value` ...) build the Python expressions that
:func:`post_hook` evaluates against ``d`` (the response's ``data`` dict) in rule order.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Literal

from builder.compiler.pycode import MONEY_BODY, assert_safe_code, py_literal, strip_code

__all__ = [
    "CALLBACK_ACKS",
    "IDENTITY_ALL_VERIFIED",
    "PROMISE_ACKS",
    "PROMISE_FIX_DEFAULT",
    "CoerceKind",
    "acked_flag",
    "all_of",
    "envelope_ok",
    "flag_false",
    "flag_true",
    "is_at_least",
    "is_count",
    "is_empty",
    "is_value",
    "is_zero",
    "join_hooks",
    "post_hook",
    "post_hook_ack",
    "post_hook_identity_progress",
    "post_hook_notes_log",
    "post_hook_promise_log",
    "pre_hook_callback_guard",
    "pre_hook_coerce",
    "pre_hook_identity_answers",
    "pre_hook_integer_fields",
    "pre_hook_notes_log",
    "pre_hook_promise_guard",
]

CoerceKind = Literal["money", "money_minor", "date_iso", "digits", "time_hhmm", "text", "token", "product_id"]

#: What the guard says when it blocked a call for a reason it has no precise correction for (also the ``promise_fix`` fallback).
PROMISE_FIX_DEFAULT = "I am sorry, I could not record that. Let me check the details again."


# --------------------------------------------------------------------------------------------------------------------
# Condition expressions for post-hook rules (evaluated against the response's ``data`` dict ``d``)
# --------------------------------------------------------------------------------------------------------------------


def flag_true(field: str) -> str:
    """Expression: ``d[field]`` is true however the platform spelled it (``true``, ``True``, ``TRUE``)."""
    return f"str(d.get({field!r})).lower() == 'true'"


def flag_false(field: str) -> str:
    """Expression: ``d[field]`` is false (``false``, ``False``)."""
    return f"str(d.get({field!r})).lower() == 'false'"


def status_in(field: str, values: Sequence[str]) -> str:
    """Expression: the string status ``d[field]`` is one of ``values`` (given in
    :func:`builder.compiler.roles.status_value` spelling: compared in lower case, spaces and dashes as ``_``)."""
    wanted = tuple(sorted({str(v) for v in values}))
    return (f"'_'.join(str(d.get({field!r}) or '').strip().lower().replace('-', ' ').split()) in {wanted!r}")


def is_value(field: str, value: str) -> str:
    """Expression: ``str(d[field])`` equals ``value``."""
    return f"str(d.get({field!r}) or '') == {value!r}"


def is_empty(field: str) -> str:
    """Expression: ``d[field]`` is missing or empty."""
    return f"not str(d.get({field!r}) or '').strip()"


def is_zero(field: str) -> str:
    """Expression: ``d[field]`` is the number zero (``0`` or ``0.0``)."""
    return is_count(field, 0)


def is_count(field: str, n: int) -> str:
    """Expression: ``d[field]`` is the whole number ``n`` (``3``, ``3.0`` or ``'3'``)."""
    return f"str(d.get({field!r})) in ({str(n)!r}, {str(n) + '.0'!r})"


def is_at_least(field: str, n: int) -> str:
    """Expression: ``d[field]`` is a whole number of at least ``n`` (a count-up counter that may skip the exact limit)."""
    return f"str(d.get({field!r})).replace('.0', '').isdigit() and int(str(d.get({field!r})).replace('.0', '')) >= {int(n)!r}"


def envelope_ok() -> str:
    """Expression: the platform reported the call as a success (``result['status']``), whatever the data holds.

    Used for a tool whose contract has no success flag of its own (see :func:`builder.compiler.roles.success_flag`).
    """
    return "str(result.get('status')) == 'success'"


def all_of(*expressions: str) -> str:
    """Expression: every given expression holds."""
    return " and ".join(f"({expression})" for expression in expressions)


# --------------------------------------------------------------------------------------------------------------------
# Post-hook
# --------------------------------------------------------------------------------------------------------------------


def post_hook(
    rules: Sequence[tuple[str, str]], default: str = "", error_message: str = "", blocked_message: str = ""
) -> str:
    """A post-process hook that writes ``what_to_do_next`` into ``result["data"]``.

    Args:
        rules: ``(condition expression, message)`` pairs, tested in order against ``d``; the first true one wins.
            Build the expressions with :func:`flag_true`, :func:`is_value`, :func:`all_of` ... or write them by hand.
        default: Message used when no rule matches (empty = write nothing).
        error_message: Message written to ``result`` when the response has no ``data`` dict and the call did not
            succeed (service error / timeout). Empty = write nothing.
        blocked_message: Message written when the pre-hook blocked the request (it emptied ``config['url']``, so the
            platform reports a missing URL). Empty = no special case.

    Returns:
        Python source, safe by construction (see the module docstring).
    """
    lines = [
        "try:",
        "    if isinstance(result, dict):",
        "        result.pop('header', None)",
        "        d = result.get('data')",
        "        if isinstance(d, dict):",
        f"            msg = {py_literal(default)}",
    ]
    for index, (condition, message) in enumerate(rules):
        lines.append(f"            {'if' if index == 0 else 'elif'} {condition}:")
        lines.append(f"                msg = {py_literal(message)}")
    lines += [
        "            if msg:",
        "                d['what_to_do_next'] = msg",
    ]
    if blocked_message:
        lines += [
            "        elif 'url' in str(result.get('message') or result.get('error') or '').lower():",
            f"            result['what_to_do_next'] = {py_literal(blocked_message)}",
        ]
    if error_message:
        lines += [
            "        elif result.get('status') != 'success':",
            f"            result['what_to_do_next'] = {py_literal(error_message)}",
        ]
    lines += ["except Exception:", "    pass"]
    return assert_safe_code(strip_code("\n".join(lines)), "post_hook")


# --------------------------------------------------------------------------------------------------------------------
# Pre-hooks
# --------------------------------------------------------------------------------------------------------------------


def _coerce_block(
    field: str, kind: CoerceKind, enum: Sequence[str] = (), date_order: str = "DMY", scale: int = 100,
) -> list[str]:
    """Statements (indented 8 spaces, inside ``if isinstance(b, dict):``) that normalise ``b[field]``.

    ``scale`` is how many minor units make one major unit for ``money_minor`` (100 = cents)."""
    key = repr(field)
    if kind == "money":
        return [
            f"        x = b.get({key})",
            *("        " + line for line in MONEY_BODY.splitlines()),
            "        if c is not None:",
            f"            b[{key}] = c / 100.0",
        ]
    if kind == "money_minor":  # the model says major units; the contract keeps integer minor units (cents)
        return [
            f"        x = b.get({key})",
            *("        " + line for line in MONEY_BODY.splitlines()),
            "        if c is not None:",
            # another stated scale (thousandths ...): cents times scale / 100, rounded half up to a whole minor unit
            f"            b[{key}] = c" if int(scale) == 100 else
            f"            b[{key}] = (c * {int(scale)!r} + 50) // 100",
        ]
    if kind == "date_iso":
        # AUD-50: an ISO prefix is kept as before; a date said with other separators or without zero padding
        # (2026-11-2, 02/11/2026) is rebuilt as YYYY-MM-DD in the brief's day order; anything else is left untouched
        day, month = {"DMY": (0, 1), "MDY": (1, 0), "YMD": (1, 0)}.get(date_order, (0, 1))
        return [
            f"        v = str(b.get({key}) or '').strip()",
            "        w = v[:10]",
            "        if len(w) == 10 and w[4] == '-' and w[7] == '-' and (w[:4] + w[5:7] + w[8:]).isdigit():",
            f"            b[{key}] = w",
            "        elif v:",
            "            parts = ''.join(c if c.isdigit() else ' ' for c in v).split()",
            "            if len(parts) == 3 and len(parts[0]) == 4 and len(parts[1]) <= 2 and len(parts[2]) <= 2:",
            f"                b[{key}] = parts[0] + '-' + parts[1].zfill(2) + '-' + parts[2].zfill(2)",
            "            elif len(parts) == 3 and len(parts[2]) == 4 and len(parts[0]) <= 2 and len(parts[1]) <= 2 and not "
            "[c for c in v if c.isalpha()]:",
            f"                b[{key}] = parts[2] + '-' + parts[{month}].zfill(2) + '-' + parts[{day}].zfill(2)",
        ]
    if kind == "digits":
        return [
            f"        v = ''.join(c for c in str(b.get({key}) or '') if c.isdigit())",
            "        if v:",
            f"            b[{key}] = v",
        ]
    if kind == "time_hhmm":
        # AUD-29: am/pm, bare hours, 'h' and '.' separators and HHMM become HH:MM; a time that cannot be read for
        # certain (eleven, 25:00, 13pm, 9:5) is left as said, so the callback guard or the service judges it
        return [
            f"        v = ''.join(str(b.get({key}) or '').strip().lower().split())",
            "        mer = ''",
            "        for m in ('a.m.', 'p.m.', 'a.m', 'p.m', 'am', 'pm'):",
            "            if v.endswith(m) and len(v) > len(m):",
            "                mer = m[0]",
            "                v = v[:-len(m)]",
            "                break",
            "        v = v.replace('.', ':').replace('h', ':').rstrip(':')",
            "        parts = v.split(':')",
            "        hh = -1",
            "        mm = -1",
            "        if len(parts) == 1 and parts[0].isdigit() and len(parts[0]) <= 2:",
            "            hh = int(parts[0])",
            "            mm = 0",
            "        elif len(parts) == 1 and parts[0].isdigit() and len(parts[0]) in (3, 4):",
            "            hh = int(parts[0][:-2])",
            "            mm = int(parts[0][-2:])",
            "        elif len(parts) in (2, 3) and parts[0].isdigit() and len(parts[0]) <= 2 and parts[1].isdigit() and "
            "len(parts[1]) == 2 and (len(parts) == 2 or parts[2].isdigit()):",
            "            hh = int(parts[0])",
            "            mm = int(parts[1])",
            "        if mer and 1 <= hh <= 12:",
            "            hh = hh % 12 + (12 if mer == 'p' else 0)",
            "        elif mer:",
            "            hh = -1",
            "        if 0 <= hh <= 23 and 0 <= mm <= 59:",
            f"            b[{key}] = '%02d:%02d' % (hh, mm)",
        ]
    if kind == "token" and enum:
        # the contract's own spelling wins: the model's value is matched ignoring case, spaces, dashes and underscores
        canon = ", ".join(py_literal(str(value)) for value in enum)
        return [
            f"        v = str(b.get({key}) or '').strip()",
            "        if v:",
            f"            canon = [{canon}]",
            "            hit = ''",
            "            for x in canon:",
            "                if x == v:",
            "                    hit = x",
            "                    break",
            "            if not hit:",
            "                low = ''.join(c for c in v.lower() if c.isalnum())",
            "                for x in canon:",
            "                    if ''.join(c for c in x.lower() if c.isalnum()) == low:",
            "                        hit = x",
            "                        break",
            f"            b[{key}] = hit or v",
        ]
    if kind == "token":
        return [
            f"        v = str(b.get({key}) or '').strip().lower()",
            "        if v:",
            f"            b[{key}] = v",
        ]
    if kind == "product_id":
        # the id of the data wins (``CC-1234``): an exact id first; else the one id equal ignoring case, spaces and
        # dashes, else the one id a bare number completes to; ids that only differ by case are never merged (F03)
        return [
            f"        v = str(b.get({key}) or '').strip().replace(' ', '')",
            "        if v:",
            "            ids = [x for x in str(vars.get('product_ids') or '').split(',') if x]",
            "            low = ''.join(c for c in v.lower() if c.isalnum())",
            "            hit = v if v in ids else ''",
            "            if not hit:",
            "                same = [x for x in ids if ''.join(c for c in x.lower() if c.isalnum()) == low]",
            "                if len(same) == 1:",
            "                    hit = same[0]",
            "                elif not same:",
            "                    same = [x for x in ids if ''.join(c for c in x.lower() if c.isalnum())"
            ".lstrip('abcdefghijklmnopqrstuvwxyz') == low]",
            "                    hit = same[0] if len(same) == 1 else ''",
            f"            b[{key}] = hit or v",
        ]
    return [
        f"        v = str(b.get({key}) or '').strip()",
        "        if v:",
        f"            b[{key}] = v",
    ]


def pre_hook_coerce(
    fields: Mapping[str, CoerceKind], enums: Mapping[str, Sequence[str]] | None = None, date_order: str = "DMY",
    minor_scales: Mapping[str, int] | None = None,
) -> str:
    """A pre-process hook that normalises request body fields.

    ``fields`` maps a body field to its kind: ``money`` (number with two decimals), ``money_minor`` (the model's
    major-unit amount sent as integer cents, for a contract that keeps minor units), ``date_iso`` (``YYYY-MM-DD``),
    ``digits`` (digits only), ``time_hhmm``, ``text`` (stripped), ``token`` or ``product_id``.

    * ``token`` with allowed values in ``enums`` is written back in the contract's own spelling (``Hardship`` stays
      ``Hardship``; ``hardship`` becomes it; a value outside the enum keeps its own spelling for the service to judge);
      without allowed values it is stripped and lower-cased.
    * ``product_id`` is matched against ``vars['product_ids']`` ignoring case, spaces and dashes and written back as the
      id of the data (``CC-1234``); a bare number is completed to the id format listed there.
    * ``money_minor`` uses the field's scale in ``minor_scales`` (minor units per major unit; default 100 = cents).
    """
    allowed = enums or {}
    scales = minor_scales or {}
    lines = ["try:", "    b = config.get('body')", "    if isinstance(b, dict):"]
    for field, kind in fields.items():
        lines += _coerce_block(field, kind, allowed.get(field) or (), date_order, scales.get(field, 100))
    if not fields:
        lines.append("        pass")
    lines += ["except Exception:", "    pass"]
    return assert_safe_code(strip_code("\n".join(lines)), "pre_hook_coerce")


def pre_hook_integer_fields(fields: Sequence[str]) -> str:
    """A pre-process hook that sends integer-typed ids as JSON integers (run last: the other hooks compare text).

    No fields gives ``""`` (no hook), so contracts with string ids keep their code unchanged.
    """
    if not fields:
        return ""
    lines = ["try:", "    b = config.get('body')", "    if isinstance(b, dict):"]
    for field in fields:
        lines += [
            f"        v = str(b.get({field!r}) or '').strip()",
            "        if v.isdigit():",
            f"            b[{field!r}] = int(v)",
        ]
    lines += ["except Exception:", "    pass"]
    return assert_safe_code(strip_code("\n".join(lines)), "pre_hook_integer_fields")


# --------------------------------------------------------------------------------------------------------------------
# Acknowledgement staging for parallel tool calls (AUD-06)
# --------------------------------------------------------------------------------------------------------------------
#
# When the model sends several calls of one tool in one message, the engine runs EVERY pre-hook before ANY post-hook (call
# records PUB-11/12/13), and a post-hook does not run on an HTTP 5xx. A single "pending" slot therefore loses all but one
# acknowledgement, and a FIFO queue shifts after a 5xx. The staging below is order-free instead: a *batch* is the calls of
# one tool whose pre-hooks ran back to back; its entries are committed together once the batch has as many
# acknowledgements as entries and no refusal. A batch that ended unfinished (a 5xx or an ``ok: false`` among its calls) is
# kept as ``<slot>_held`` with the number of calls that were not acknowledged; when a later batch is fully acknowledged and
# re-sends at least that many of its keys (the model retries what failed), the rest of the held batch is committed too.
# Nothing is committed on a guess: an unfinished batch that is never retried stays uncommitted (fail safe).


def _batch_open(slot: str, pending: str, sep: str, tool: str, blocks: str) -> list[str]:
    """Lines (indented 4, inside ``try:``) that start a new batch for ``slot`` when the previous one is over.

    A batch is over once one of its post-hooks ran (``<slot>_open`` = ``0``) or once the engine counted more finished calls
    of ``tool`` than calls the guard blocked (``blocks``): a batch whose requests all failed with a 5xx runs no post-hook.
    """
    acks, held, missing = repr(slot + "_acks"), repr(slot + "_held"), repr(slot + "_held_missing")
    return [
        "    bk = 0",
        "    try:",
        f"        bk = int(float(vars.get({tool + '_success_count'!r}) or 0)) + int(float(vars.get("
        f"{tool + '_failure_count'!r}) or 0)) - int(float(vars.get({blocks!r}) or 0))",
        "    except Exception:",
        "        bk = 0",
        f"    if str(vars.get({slot + '_open'!r}) or '') != '1' or bk > int(float(vars.get({slot + '_key'!r}) or 0)):",
        f"        old = [k for k in str(vars.get({pending!r}) or '').split({sep!r}) if k]",
        f"        got = int(float(vars.get({acks}) or 0))",
        "        if old and got < len(old):",
        f"            vars[{held}] = {sep!r}.join(old)",
        f"            vars[{missing}] = str(len(old) - got)",
        f"        vars[{pending!r}] = ''",
        f"        vars[{acks}] = '0'",
        f"        vars[{slot + '_nacks'!r}] = '0'",
        f"        vars[{slot + '_key'!r}] = str(bk)",
        f"    vars[{slot + '_open'!r}] = '1'",
    ]


def _batch_stage(pending: str, sep: str, entry: str, indent: str) -> list[str]:
    """Lines that add ``entry`` (an expression; ``'?'`` = an incomplete request) to the open batch, once."""
    return [
        f"{indent}e = {entry}",
        f"{indent}staged = [k for k in str(vars.get({pending!r}) or '').split({sep!r}) if k]",
        f"{indent}if e == '?' or e not in staged:",
        f"{indent}    staged.append(e)",
        f"{indent}vars[{pending!r}] = {sep!r}.join(staged)",
    ]


def _batch_settle(
    slot: str, pending: str, sep: str, success_condition: str, key: str, commit: Sequence[str], ack_counter: str = "",
) -> str:
    """A post-hook that counts this result for the open batch and commits the batch once every call is acknowledged.

    ``key`` is the expression of an entry's key (``k``: the entry itself, or its product id); ``commit`` are the lines
    (indented 16) that commit the list ``add``. ``ack_counter`` (AUD-07) counts every acknowledged result: routing and the
    closing decision read it instead of the engine's ``<tool>_success_count``, which counts any HTTP 2xx, ``ok: false``
    included.
    """
    acks, nacks = repr(slot + "_acks"), repr(slot + "_nacks")
    held, missing = repr(slot + "_held"), repr(slot + "_held_missing")
    lines = [
        "try:",
        "    if isinstance(result, dict):",
        "        d = result.get('data')",
        "        if not isinstance(d, dict):",
        "            d = dict()",
        f"        vars[{slot + '_open'!r}] = '0'",
        f"        staged = [k for k in str(vars.get({pending!r}) or '').split({sep!r}) if k]",
        f"        if str(result.get('status') or '').lower() == 'success' and ({success_condition}):",
        *([f"            vars[{ack_counter!r}] = str(int(float(vars.get({ack_counter!r}) or 0)) + 1)",
           f"            vars[{acked_flag(ack_counter)!r}] = 'yes'"]
          if ack_counter else []),
        f"            got = int(float(vars.get({acks}) or 0)) + 1",
        f"            vars[{acks}] = str(got)",
        f"            if staged and got >= len(staged) and int(float(vars.get({nacks}) or 0)) == 0:",
        f"                keys = [{key} for k in staged]",
        f"                kept = [k for k in str(vars.get({held}) or '').split({sep!r}) if k]",
        "                add = list(staged)",
        f"                if kept and len([k for k in kept if {key} in keys]) >= int(float(vars.get({missing}) or 1)):",
        f"                    add = add + [k for k in kept if {key} not in keys]",
        f"                    vars[{held}] = ''",
        f"                    vars[{missing}] = '0'",
        *commit,
        f"                vars[{pending!r}] = ''",
        f"                vars[{acks}] = '0'",
        "        else:",
        f"            vars[{nacks}] = str(int(float(vars.get({nacks}) or 0)) + 1)",
        "except Exception:",
        "    pass",
    ]
    return strip_code("\n".join(lines))


def post_hook_ack(success_condition: str, counter: str) -> str:
    """A post-hook that counts an acknowledged result in ``vars[counter]`` (AUD-07).

    The engine's ``<tool>_success_count`` counts any HTTP 2xx, an ``ok: false`` refusal included (call records), so routes
    and the closing decision that mean "the bank did it" read this counter instead. ``success_condition`` is the contract's
    success flag (or the envelope); the counter is written only when the transport succeeded AND the flag holds.
    """
    lines = [
        "try:",
        "    if isinstance(result, dict):",
        "        d = result.get('data')",
        "        if not isinstance(d, dict):",
        "            d = dict()",
        f"        if str(result.get('status') or '').lower() == 'success' and ({success_condition}):",
        f"            vars[{counter!r}] = str(int(float(vars.get({counter!r}) or 0)) + 1)",
        f"            vars[{acked_flag(counter)!r}] = 'yes'",
        "except Exception:",
        "    pass",
    ]
    return assert_safe_code(strip_code("\n".join(lines)), "post_hook_ack")


def pre_hook_callback_guard(date_field: str, time_field: str, max_extra_asks: int = 1) -> str:
    """A pre-process hook that blocks a callback outside the allowed slots (AUD-29); run it after the coerce hook.

    ``setup`` writes ``vars['callback_slots']``: ``;``-separated ``ISO date|from|from excluded (1/0)|to|to included
    (1/0)|spoken day`` for every day a callback may be booked (today's lower bound is the current time when the slot must
    be later than now). A request whose day is not listed, whose time is not a real ``HH:MM`` clock time (hour 0-23, minute
    0-59; ``11:99`` is blocked) or is outside the day's window sends
    nothing (``config['url'] = ''``) and writes ``callback_guard`` (``blocked: ...``), ``callback_guard_blocks`` and the
    spoken correction ``callback_guard_say``; after more than ``max_extra_asks`` blocked requests it also writes
    ``callback_guard_stop = yes`` (the brief asks again only that often, then books nothing). Without slots (a custom callback rule, or ``setup`` did not run) the guard
    lets the request through, as before.
    """
    lines = [
        "try:",
        "    slots = [s.split('|') for s in str(vars.get('callback_slots') or '').split(';') if s.count('|') >= 4]",
        "    b = config.get('body')",
        "    if slots and isinstance(b, dict):",
        f"        dt = str(b.get({date_field!r}) or '').strip()[:10]",
        f"        tm = str(b.get({time_field!r}) or '').strip()",
        "        why = ''",
        "        days = [s for s in slots if s[0] == dt]",
        "        shaped = len(tm) == 5 and tm[2] == ':' and tm[:2].isdigit() and tm[3:].isdigit()",
        "        if not (shaped and int(tm[:2]) <= 23 and int(tm[3:]) <= 59):",
        "            why = 'the time ' + tm + ' is not a time of day'",
        "        elif not days:",
        "            why = 'a callback cannot be booked on ' + dt",
        "        else:",
        "            s = days[0]",
        "            low_ok = tm > s[1] if s[2] == '1' else tm >= s[1]",
        "            high_ok = tm <= s[3] if s[4] == '1' else tm < s[3]",
        "            if not (low_ok and high_ok):",
        "                why = 'the time ' + tm + ' on ' + dt + ' is outside the allowed callback times'",
        "        if why:",
        "            vars['callback_guard'] = 'blocked: ' + why",
        "            vars['callback_guard_blocks'] = str(int(float(vars.get('callback_guard_blocks') or 0)) + 1)",
        f"            if int(vars['callback_guard_blocks']) > {int(max_extra_asks)!r}:",
        "                vars['callback_guard_stop'] = 'yes'",
        "            said = []",
        "            for s in slots:",
        "                day = s[5] if len(s) > 5 and s[5] else s[0]",
        "                said.append(day + (' after ' if s[2] == '1' else ' from ') + s[1] + ' to ' + s[3])",
        "            vars['callback_guard_say'] = 'I am sorry, I cannot book a callback at that time. I can book one ' + "
        "' or '.join(said) + '. Which time would suit?'",
        "            config['url'] = ''",
        "        else:",
        "            vars['callback_guard'] = 'ok'",
        "except Exception:",
        "    pass",
    ]
    return assert_safe_code(strip_code("\n".join(lines)), "pre_hook_callback_guard")


#: Acknowledged ``create_promise`` / ``schedule_callback`` results (AUD-07); ``setup`` starts them at ``0``.
PROMISE_ACKS = "promise_acks"
CALLBACK_ACKS = "callback_acks"


def acked_flag(counter: str) -> str:
    """The string flag (``yes`` once anything was acknowledged) next to an acknowledgement counter: routes compare it with
    ``equals`` (verified to compare as text), never a number a hook wrote as text with ``greater_than``."""
    return counter.removesuffix("_acks") + "_acked"


def pre_hook_promise_guard(
    product_field: str,
    amount_field: str,
    date_field: str,
    *,
    accept_between: bool = True,
    enabled_dates: bool = True,
    currency: str = "",
    after_deadline: str = "offer_deadline",
    shift: str = "next_business_day",
    allow_today: bool = True,
    business_days_only: bool = False,
    business_weekdays: Sequence[int] = (0, 1, 2, 3, 4),
    holidays: Sequence[str] = (),
    not_after_deadline: bool = True,
    once_per_product: bool = True,
    recorded_say: str = "",
    amount_minor: bool = False,
    minor_scale: int = 100,
    below_floor: str = "offer_floor_once",
    tool_name: str = "create_promise",
    clock: str = "now",
) -> str:
    """A pre-process hook that blocks a promise the rules do not allow (the HARD PROMISE GUARD) and prepares the spoken fix.

    Run it after :func:`pre_hook_coerce` (it reads the cleaned body). It checks the request against what ``account_facts``
    wrote - ``product_ids`` (csv), ``allowed_<pid>`` (csv of allowed ISO dates), ``allowed_spoken_<pid>`` (the same dates
    spoken, same order), ``<pid>_floor``, ``<pid>_past_due``, ``<pid>_name`` and ``<pid>_deadline_spoken`` - and blocks when
    the product id is unknown, the date is not an allowed date, or the amount is outside the floor..full range (or is
    neither the floor nor the full amount when ``accept_between`` is false). Every date must be a real calendar date.
    With ``now`` / ``today_iso`` present, the date is checked against the full calendar and the per-product deadline;
    the finite allowed-date list does not determine whether a later day is valid. Missing facts do not impose a deadline.
    ``allowed_count_<pid>`` = ``0`` without an open tail (written by ``account_facts``) means that no day is left before
    the deadline, and every date is then blocked. A list cut at the
    horizon (``until_<pid>`` = ``open``, or an ISO date up to which later days are allowed) describes later dates without
    bypassing the calendar check.
    The product id is matched ignoring case (the data's spelling is used for every lookup).

    **One acknowledged promise per product** (``once_per_product``, audit F17 / C-17): ``vars['promised_products']`` (csv,
    written by :func:`post_hook_promise_log` only after the bank acknowledged a request) lists the products whose promise is
    recorded. A request for one of them is blocked whatever its terms (identical, changed, another spelling of the id), with
    a spoken explanation (``recorded_say`` = the brief's sentence about a recorded promise, appended). A request that passes
    the guard is staged in the open batch ``vars['promise_pending_product']`` (csv; see the AUD-06 staging above: parallel
    calls are staged together and committed once each is acknowledged; a blocked request stages nothing); a failed or
    unknown outcome never reaches ``promised_products`` and the supported retry stays possible. Another product is not
    affected. A request without loaded account facts (``product_ids`` empty: ``get_account`` failed or returned no product)
    and an amount that is not a number while the full amount is known are blocked too (AUD-24): nothing reaches the bank
    on a guess.

    Platform behaviour (experiments E-8 and E-9, both VERIFIED): setting ``config['url'] = ''`` sends no request; the platform
    answers ``function_call_error: Missing required field: url``, sets the tool's response status to ``error`` and increments
    ``<tool>_failure_count``; **the post-hook does not run on this path**, so the model only sees that generic error. A write
    to ``vars`` made here persists and is visible to routing. Blocking therefore writes everything the workflow needs:

    * ``vars['promise_guard']`` - ``ok`` or ``blocked: <reason>`` (a routing condition ``contains blocked`` leads to the
      ``promise_fix`` node);
    * ``vars['promise_guard_blocks']`` - how many calls were blocked (``decide`` subtracts them from the tool's failure
      counter, because a blocked call is not a service failure);
    * ``vars['promise_guard_say']`` - the spoken correction: the latest allowed date (after the deadline), the next /
      closest allowed date (a day that is not allowed), or the allowed amount range, with the product's spoken name.

    Args:
        currency: The brief's currency word, spoken after amounts ("" = none).
        after_deadline: ``offer_deadline`` offers the latest allowed date for the same amount; anything else asks for an
            earlier day.
        shift: ``next_business_day`` / ``previous_business_day`` pick which allowed date is offered for a day that is not
            allowed; ``reject`` only asks for another day.
        amount_minor: The contract keeps the amount in integer minor units (``money_minor``): the coerced integer in
            the body is already cents.
        minor_scale: With ``amount_minor``: how many minor units make one major unit (100 = cents; 1000 for an
            amount in thousandths), so the guard compares the body's integer in cents.
        below_floor: The policy for an amount under the floor (``offer_floor_once`` proposes the floor; anything else
            only says it cannot be recorded). The spoken correction never asks the customer how much they can pay
            (AUD-38): it proposes the floor or the full amount, the closed choice of the two, or nothing.
    """
    pf, af, df = repr(product_field), repr(amount_field), repr(date_field)
    cur = py_literal(f" {currency.strip()}") if currency.strip() else "''"
    said = py_literal(recorded_say.strip())
    lines = [
        "try:",
        "    import datetime",
        *_batch_open("promise_batch", "promise_pending_product", ",", tool_name, "promise_guard_blocks"),
        "    b = config.get('body')",
        "    if isinstance(b, dict):",
        f"        pid = str(b.get({pf}) or '').strip()",
        f"        dt = str(b.get({df}) or '').strip()[:10]",
        f"        x = b.get({af})",
        *("        " + line for line in MONEY_BODY.splitlines()),
        # with ``amount_minor`` the coerce hook already wrote integer minor units (cents)
        *(["        if isinstance(x, int) and not isinstance(x, bool):",
           "            c = x" if int(minor_scale) == 100 else f"            c = (x * 100 + {int(minor_scale) // 2!r}) // {int(minor_scale)!r}"]
          if amount_minor else []),
        "        amount = c",
        "        known = [k for k in str(vars.get('product_ids') or '').split(',') if k]",
        "        if known and pid not in known:",
        "            same = [k for k in known if k.lower() == pid.lower()]",
        "            if len(same) == 1:",
        "                pid = same[0]",
        "        facts = []",
        "        for x in (vars.get(pid + '_past_due'), vars.get(pid + '_floor')):",
        *("            " + line for line in MONEY_BODY.splitlines()),
        "            facts.append(c)",
        "        full = facts[0]",
        "        low = facts[1] if facts[1] is not None else full",
        "        if full is not None and low is not None and low > full:",
        "            low = full",
        "        reason = ''",
        "        why = ''",
        "        if known and pid not in known:",
        "            reason = 'unknown product_id ' + pid + '; use one of ' + ','.join(known)",
        "            why = 'product'",
        # AUD-24: no account facts (get_account failed or listed no product): nothing can be checked, so nothing is sent
        "        if not reason and not known:",
        "            reason = 'no account facts are loaded'",
        "            why = 'account'",
        *(
            [
                "        if not reason and pid in [k for k in str(vars.get('promised_products') or '').split(',') if k]:",
                "            reason = 'a promise for ' + pid + ' is already recorded and cannot be changed or recorded again'",
                "            why = 'recorded'",
            ]
            if once_per_product
            else []
        ),
        "        allowed = [k for k in str(vars.get('allowed_' + pid) or '').split(',') if k]",
        "        day = None",
        "        try:",
        "            if len(dt) == 10 and dt[4:5] == '-' and dt[7:8] == '-':",
        "                day = datetime.date.fromisoformat(dt)",
        "        except Exception:",
        "            pass",
        "        isiso = day is not None",
        "        today = None",
        "        try:",
        *([f"            today = datetime.date.fromisoformat(str(vars.get('today_iso') or vars.get({clock!r}) or "
           "vars.get('now') or '').strip()[:10])"] if clock != "now" else
          ["            today = datetime.date.fromisoformat(str(vars.get('today_iso') or vars.get('now') or '').strip()[:10])"]),
        "        except Exception:",
        "            pass",
        "        limit = None",
        "        try:",
        *(["            limit = datetime.date.fromisoformat(str(vars.get(pid + '_deadline') or ''))"]
          if not_after_deadline else ["            pass"]),
        "        except Exception:",
        "            pass",
        f"        datebw = {list(business_weekdays)!r}",
        f"        datehol = {py_literal(','.join(holidays))}.split(',')",
        "        after_limit = False",
        "        def day_allowed(d):",
        f"            return (today is None or d > today or (d == today and {allow_today!r})) and (limit is None or d <= limit)"
        f" and ({not business_days_only!r} or (d.weekday() in datebw and d.isoformat() not in datehol))",
    ]
    if enabled_dates:
        lines += [
            "        if not reason and str(vars.get('allowed_count_' + pid) or '') == '0' and not str(vars.get('until_' + pid) or ''):",
            "            reason = 'no promise date is left for ' + pid",
            "            why = 'none'",
            "        if not reason and not isiso:",
            "            reason = 'the date ' + dt + ' is not a real calendar date'",
            "            why = 'date'",
            "        if not reason and isiso and not day_allowed(day):",
            "            reason = 'the date ' + dt + ' is not an allowed promise date for ' + pid",
            "            why = 'date'",
            "            after_limit = limit is not None and day > limit",
            "        if not reason and today is None and allowed and dt not in allowed:",
            "            tail = str(vars.get('until_' + pid) or '')",
            "            beyond = bool(tail) and isiso and dt > allowed[-1] and (tail == 'open' or dt <= tail)",
            "            if not beyond:",
            "                reason = 'the date ' + dt + ' is not an allowed promise date for ' + pid + ' (latest allowed: ' + allowed[-1] + ')'",
            "                why = 'date'",
            "                after_limit = dt > allowed[-1]",
        ]
    between = "" if accept_between else " or (amount != low and amount != full)"
    lines += [
        "        if not reason and amount is not None and full is not None and full > 0:",
        f"            if amount < low or amount > full{between}:",
        "                reason = 'the amount ' + str(amount // 100) + '.' + ('0' + str(amount % 100))[-2:] + ' is not allowed for ' + pid",
        "                why = 'amount'",
        # AUD-24: an amount that is missing or not a number never reaches the bank while the full amount is known
        "        if not reason and amount is None and full is not None:",
        "            reason = 'the amount is not a number'",
        "            why = 'amount_unread'",
        "        if reason:",
        "            vars['promise_guard'] = 'blocked: ' + reason",
        "            vars['promise_guard_blocks'] = str(int(float(vars.get('promise_guard_blocks') or 0)) + 1)",
        "            pname = str(vars.get(pid + '_name') or 'this product')",
        f"            cur = {cur}",
        "            offer_c = amount if (amount is not None and full is not None and amount >= low and amount <= full) else full",
        "            offer = ('%d.%02d' % (offer_c // 100, offer_c % 100)) if offer_c is not None else ''",
        "            say = " + py_literal(PROMISE_FIX_DEFAULT),
        "            if why == 'recorded':",
        "                say = 'I am sorry, the payment promise for your ' + pname + ' is already recorded and cannot be "
        "changed or recorded again in this call.' + (' ' + " + said + " if " + said + " else '')",
        "            elif why == 'none':",
        "                say = 'I am sorry, I cannot record a payment date for your ' + pname + ' in this call.'",
        "            elif why == 'account':",
        "                say = 'I am sorry, I cannot open your account details right now, so I cannot record a payment "
        "promise in this call.'",
        "            elif why == 'amount_unread' and full is not None:",
        "                say = 'I am sorry, I did not catch the amount for your ' + pname + '. The amount due is ' + "
        "('%d.%02d' % (full // 100, full % 100)) + cur + '. Could you pay that?'",
        "            elif why == 'date':",
        "                spoken = [k for k in str(vars.get('allowed_spoken_' + pid) or '').split(',') if k]",
        "                if not isiso:",
        "                    say = 'I am sorry, I did not catch the day for your ' + pname + '. Which day can you pay?'",
        "                elif after_limit:",
        "                    last = str(vars.get(pid + '_deadline_spoken') or '')",
        "                    if not last:",
        "                        last = spoken[-1] if spoken and len(spoken) == len(allowed) else (allowed[-1] if allowed else str(limit or ''))",
    ]
    if after_deadline == "offer_deadline":
        lines.append(
            "                    say = 'I am sorry, the latest date I can record for your ' + pname + ' is ' + last"
            " + '. Could you pay ' + (offer + cur + ' ' if offer else '') + 'on that day?'"
        )
    else:
        lines.append(
            "                    say = 'I am sorry, I cannot record a date after ' + last + ' for your ' + pname"
            " + '. Could you tell me an earlier day?'"
        )
    if shift in ("next_business_day", "previous_business_day"):
        word = "next" if shift == "next_business_day" else "closest"
        pick = (
            "tgt = after[0] if after else ''"
            if shift == "next_business_day"
            else "tgt = before[-1] if before else (after[0] if after else '')"
        )
        lines += [
            "                else:",
            "                    tgt = ''",
            "                    if today is not None:",
            f"                        directions = {[-1, 1] if shift == 'previous_business_day' else [1]!r}",
            "                        for direction in directions:",
            "                            candidate = day",
            "                            if direction > 0 and day < today:",
            f"                                candidate = today - datetime.timedelta(days=1) if {allow_today!r} else today",
            "                            for _ in range((len(datehol) + 1) * 7 + 1):",
            "                                try:",
            "                                    candidate = candidate + datetime.timedelta(days=direction)",
            "                                except Exception:",
            "                                    break",
            "                                if day_allowed(candidate):",
            "                                    tgt = candidate.isoformat()",
            "                                    break",
            "                                if (direction < 0 and candidate < today) or (direction > 0 and limit is not None and candidate > limit):",
            "                                    break",
            "                            if tgt:",
            "                                break",
            "                    else:",
            "                        after = [k for k in allowed if k > dt]",
            "                        before = [k for k in allowed if k < dt]",
            f"                        {pick}",
            "                    if tgt:",
            "                        said = spoken[allowed.index(tgt)] if tgt in allowed and allowed.index(tgt) < len(spoken) else tgt",
            "                        say = 'I am sorry, I cannot record that day for your ' + pname + '. "
            f"The {word} day I can record is ' + said + '. Would that work for you?'",
            "                    else:",
            "                        say = 'I am sorry, I cannot record that day for your ' + pname + '. Could you tell me another day?'",
        ]
    else:
        lines += [
            "                else:",
            "                    say = 'I am sorry, I cannot record that day for your ' + pname + '. Could you tell me another day?'",
        ]
    # AUD-38: the correction proposes an amount (the floor below it, the full amount above it); it never asks the
    # customer how much they can pay
    below_say = (
        "'I am sorry, I cannot record ' + amt_t + cur + ' for your ' + pname + '. The lowest amount I can record is '"
        " + lo_t + cur + '. Could you pay ' + lo_t + cur + '?'"
        if below_floor == "offer_floor_once"
        else "'I am sorry, I cannot record an amount below ' + lo_t + cur + ' for your ' + pname + '.'"
    )
    lines += [
        "            elif why == 'amount' and full is not None:",
        "                lo_t = '%d.%02d' % (low // 100, low % 100)",
        "                hi_t = '%d.%02d' % (full // 100, full % 100)",
        "                amt_t = ('%d.%02d' % (amount // 100, amount % 100)) if amount is not None else ''",
        "                if low >= full:",
        "                    say = 'I am sorry, for your ' + pname + ' I can record only the full amount of ' + hi_t + cur"
        " + '. Can you pay that?'",
        *([
            "                elif amount is not None and amount > full:",
            "                    say = 'I am sorry, the amount due on your ' + pname + ' is ' + hi_t + cur"
            " + ', so I cannot record more. Could you pay ' + hi_t + cur + '?'",
            "                elif amount is not None and amount < low:",
            f"                    say = {below_say}",
        ] if accept_between else []),
        "                else:",
        "                    say = 'I am sorry, for your ' + pname + ' I can record only ' + lo_t + ' or ' + hi_t + cur"
        " + '. Which one do you prefer?'",
        "            vars['promise_guard_say'] = say",
        "            config['url'] = ''",
        "        else:",
        "            vars['promise_guard'] = 'ok'",
        *_batch_stage("promise_pending_product", ",", "pid if pid else '?'", "            "),
        "except Exception:",
        "    pass",
    ]
    return assert_safe_code(strip_code("\n".join(lines)), "pre_hook_promise_guard")


def pre_hook_notes_log(
    product_field: str = "product_id",
    reason_field: str = "reason",
    reasons: Sequence[str] = (),
    *,
    tool_name: str = "add_note",
    guard: bool = True,
    transfer_counters: Sequence[str] = (),
) -> str:
    """Normalise the notes request, guard it and stage it for the acknowledgment post-hook.

    The product id is written in the spelling of ``vars['product_ids']`` and the reason in the spelling of ``reasons``
    (the contract's enum), so the log and the decision code agree whatever case the model used.

    A pre-hook runs before the bank has saved anything. It must not write ``notes_log``; a failed request otherwise
    changes the closing outcome. The request is staged as ``pid:reason`` in the open batch ``vars['note_pending']``
    (``;``-separated, AUD-06: parallel notes are committed together once each is acknowledged); an incomplete request
    stages a placeholder that is never committed.

    **Guard** (``guard``, AUD-26): a note for a product that already has an acknowledged note (``notes_log``) or an
    acknowledged promise (``promised_products``), and any note once one of ``transfer_counters`` is above zero (a
    transfer was made and the policy writes no note on a transfer), is not sent: ``config['url']`` is emptied (the platform
    then sends nothing), ``vars['note_guard']`` says why and ``vars['note_guard_blocks']`` counts it. Only acknowledged
    state is read, so the supported retry of a failed note stays possible.
    """
    base = pre_hook_coerce({product_field: "product_id", reason_field: "token"}, enums={reason_field: reasons})
    counters = ", ".join(repr(c) for c in transfer_counters)
    lines = [
        "try:",
        *_batch_open("note_batch", "note_pending", ";", tool_name, "note_guard_blocks"),
        "    pid = ''",
        "    rsn = ''",
        "    b = config.get('body')",
        "    if isinstance(b, dict):",
        f"        pid = str(b.get({product_field!r}) or '').strip()",
        f"        rsn = str(b.get({reason_field!r}) or '').strip()",
        "    why = ''",
    ]
    if guard:
        lines += [
            "    noted = [k.split(':', 1)[0].strip() for k in str(vars.get('notes_log') or '').split(';') if ':' in k]",
            "    promised = [k for k in str(vars.get('promised_products') or '').split(',') if k]",
            "    if pid and pid in noted:",
            "        why = 'a note for ' + pid + ' is already saved'",
            "    elif pid and pid in promised:",
            "        why = 'a promise for ' + pid + ' is recorded, so it gets no note'",
        ]
        if transfer_counters:
            lines += [
                "    moved = 0",
                f"    for c in [{counters}]:",
                "        moved = moved + int(float(vars.get(c) or 0))",
                "    if not why and moved > 0:",
                "        why = 'the call is transferred, so no note is written'",
            ]
    lines += [
        "    if why:",
        "        vars['note_guard'] = 'blocked: ' + why",
        "        vars['note_guard_blocks'] = str(int(float(vars.get('note_guard_blocks') or 0)) + 1)",
        "        config['url'] = ''",
        "    else:",
        "        vars['note_guard'] = 'ok'",
        *_batch_stage("note_pending", ";", "(pid + ':' + rsn) if pid and rsn else '?'", "        "),
        "except Exception:",
        "    pass",
    ]
    return join_hooks(base, strip_code("\n".join(lines)))


def post_hook_notes_log(success_condition: str) -> str:
    """Append the staged, canonical notes only when the bank acknowledged every request of their batch (AUD-06).

    ``success_condition`` uses the response field identified from the supplied API contract, or the result envelope
    when the contract has no success flag. A successful HTTP execution with a false API flag records no note. A batch is
    committed once, so processing the same result twice is harmless.
    """
    commit = [
        "                log = str(vars.get('notes_log') or '')",
        "                for k in add:",
        "                    if ':' in k and not k.startswith('?'):",
        "                        log = log + k + ';'",
        "                vars['notes_log'] = log",
    ]
    code = _batch_settle("note_batch", "note_pending", ";", success_condition, "k.rsplit(':', 1)[0]", commit)
    return assert_safe_code(code, "post_hook_notes_log")


def post_hook_promise_log(success_condition: str) -> str:
    """Record the staged products as acknowledged only when the bank acknowledged their requests (audit F17 / C-17, AUD-06).

    The twin of :func:`post_hook_notes_log`: ``success_condition`` uses the response field identified from the supplied API
    contract (or the result envelope when the contract has no success flag). A request that failed, whose flag is false or
    whose outcome is unknown adds nothing, so the supported retry stays possible. ``vars['promised_products']`` is a csv of
    product ids without duplicates. Every acknowledged result also counts in ``vars['promise_acks']`` (AUD-07), which the
    routes and the closing decision read instead of the engine's success counter.
    """
    commit = [
        "                done = [k for k in str(vars.get('promised_products') or '').split(',') if k]",
        "                for k in add:",
        "                    if k != '?' and k not in done:",
        "                        done.append(k)",
        "                vars['promised_products'] = ','.join(done)",
    ]
    code = _batch_settle("promise_batch", "promise_pending_product", ",", success_condition, "k", commit,
                         ack_counter=PROMISE_ACKS)
    return assert_safe_code(code, "post_hook_promise_log")


def pre_hook_identity_answers(
    field_name: str,
    answer_name: str,
    formats: Mapping[str, str],
    date_order: Literal["DMY", "MDY", "YMD"] = "DMY",
    lengths: Mapping[str, int] | None = None,
    *,
    track: bool = False,
) -> str:
    """A pre-process hook for the identity tool: normalises the answer by the question it belongs to.

    Args:
        field_name: Body field that says which question this is (the contract's identity ``field``).
        answer_name: Body field with the answer.
        formats: ``field value -> answer_format`` (``date_iso``, ``digits``, ``year``, ``postcode``, ``text``).
        date_order: How an all-numeric date is read (the brief's rule; ``DMY`` = day, month, year).
        lengths: ``field value -> expected digit count`` (the IR's ``digits``; a year expects 4).
        track: Stage the question of every request in ``vars['verify_pending']`` for
            :func:`post_hook_identity_progress` (a response without an overall "all verified" flag, AUD-23).

    A numeric date (``14/03/1988``, ``14 03 1988``, ``1988 03 14``) becomes ``YYYY-MM-DD``; with a correction in the same
    answer (``14/03/1988, no wait, 15/03/1988``) the last date said is used. A ``digits`` / ``year`` answer (AUD-15) is
    never made up from pieces that do not fit: its digit groups are joined when together they have the expected length
    (``4-4 7 1``, ``19 88``); otherwise the last group of the expected length is sent (a correction: ``4401, sorry,
    4410`` -> ``4410``); a single code of letters and digits (``sc123456``) is sent in capitals with its letters; anything
    else is left as the model wrote it (``nineteen 88`` is never cut to ``88``). A ``postcode`` is sent in capitals with
    single spaces. Anything else is left as the model wrote it.
    """
    pairs = ", ".join(f"({name!r}, {kind!r})" for name, kind in formats.items())
    sizes = ", ".join(f"({name!r}, {int(n)!r})" for name, n in (lengths or {}).items() if n)
    order_index = {"DMY": (0, 1, 2), "MDY": (1, 0, 2), "YMD": (2, 1, 0)}[date_order]
    lines = [
        "try:",
        "    b = config.get('body')",
        "    if isinstance(b, dict):",
        f"        kinds = dict([{pairs}])",
        f"        sizes = dict([{sizes}])",
        f"        which = str(b.get({field_name!r}) or '')",
        "        kind = kinds.get(which, '')",
        f"        a = str(b.get({answer_name!r}) or '').strip()",
        "        if a and kind == 'date_iso':",
        "            parts = ''.join(c if c.isdigit() else ' ' for c in a).split()",
        "            if len(parts) > 3 and len(parts) % 3 == 0:",
        "                parts = parts[-3:]",
        "            if len(parts) == 3 and len(parts[0]) == 4:",
        "                a = parts[0] + '-' + parts[1].zfill(2) + '-' + parts[2].zfill(2)",
        "            elif len(parts) == 3 and len(parts[2]) == 4:",
        f"                a = parts[2] + '-' + parts[{order_index[1]}].zfill(2) + '-' + parts[{order_index[0]}].zfill(2)",
        "        elif a and kind in ('digits', 'year'):",
        "            n = int(sizes.get(which) or (4 if kind == 'year' else 0))",
        "            groups = ''.join(c if c.isdigit() else ' ' for c in a).split()",
        "            tokens = ''.join(c if c.isalnum() else ' ' for c in a).split()",
        "            joined = ''.join(groups)",
        "            lettered = len([c for c in a if c.isalpha()]) > 0",
        "            fit = [g for g in groups if len(g) == n]",
        "            if kind == 'digits' and len(tokens) == 1 and lettered and groups:",
        "                a = tokens[0].upper()",
        "            elif joined and n and len(joined) == n:",
        "                a = joined",
        "            elif fit:",
        "                a = fit[-1]",
        "            elif joined and not n and not lettered:",
        "                a = joined",
        "        elif a and kind == 'postcode':",
        "            a = ' '.join(a.upper().split()).strip('.,')",
        "        if a:",
        f"            b[{answer_name!r}] = a",
        *([
            "        queue = [k for k in str(vars.get('verify_pending') or '').split('|') if k]",
            "        queue.append(which or '?')",
            "        vars['verify_pending'] = '|'.join(queue)",
        ] if track else []),
        "except Exception:",
        "    pass",
    ]
    return assert_safe_code(strip_code("\n".join(lines)), "pre_hook_identity_answers")


def post_hook_identity_progress(verified_flag: str, questions: int) -> str:
    """Count the questions the bank verified when its response has no overall "all verified" flag (AUD-23).

    Runs before the instruction post-hook. It takes the oldest question staged by
    :func:`pre_hook_identity_answers` (``track``; requests are answered in the order they were sent) and, when the
    response's per-answer flag ``verified_flag`` is true, adds it to ``vars['verified_fields']``. Once ``questions``
    different questions are verified it writes ``vars['identity_all_verified'] = 'yes'``, the variable the identity node
    routes on: moving on to the account no longer depends on the model counting the answers itself.
    """
    lines = [
        "try:",
        "    if isinstance(result, dict):",
        "        d = result.get('data')",
        "        if not isinstance(d, dict):",
        "            d = dict()",
        "        queue = [k for k in str(vars.get('verify_pending') or '').split('|') if k]",
        "        head = queue[0] if queue else '?'",
        "        vars['verify_pending'] = '|'.join(queue[1:])",
        f"        if str(result.get('status') or '').lower() == 'success' and {flag_true(verified_flag)}:",
        "            done = [k for k in str(vars.get('verified_fields') or '').split('|') if k]",
        "            if head == '?' or head not in done:",
        "                done.append(head if head != '?' else '?' + str(len(done)))",
        "            vars['verified_fields'] = '|'.join(done)",
        f"            if len(done) >= {max(1, int(questions))!r}:",
        "                vars['identity_all_verified'] = 'yes'",
        "except Exception:",
        "    pass",
    ]
    return assert_safe_code(strip_code("\n".join(lines)), "post_hook_identity_progress")


#: The condition (against ``vars``) that the identity post-hook's progress tracking saw every question verified.
IDENTITY_ALL_VERIFIED = "str(vars.get('identity_all_verified') or '') == 'yes'"


def join_hooks(*codes: str) -> str:
    """Concatenate several hook snippets (each already wrapped in its own ``try``); empty ones are skipped."""
    parts = [strip_code(code) for code in codes if code and code.strip()]
    return assert_safe_code("".join(parts), "joined hooks") if parts else ""
