"""Pre- and post-hooks for model-called tools: small, safe Python snippets that run around the HTTP call.

Why hooks exist (``docs/tools-and-code.md``): after a tool result the model must speak or call the next tool, but bare
data such as ``{"ok": true, "verified": false}`` often leads to silence or to ignoring a negative result. A **post-hook**
adds one plain instruction line, ``what_to_do_next``, to ``result["data"]``; the model then has something to obey. A
**pre-hook** cleans the model's arguments (amounts as numbers, ISO dates, digit-only answers) in ``config["body"]`` and
may write call variables for later steps.

Rules every generated hook follows (verified in a working builder, or documented):

* the whole body is inside ``try`` / ``except Exception: pass`` - a hook never raises;
* types are checked with ``isinstance`` before use; no imports, no ``{{`` / ``}}``;
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
    "PROMISE_FIX_DEFAULT",
    "CoerceKind",
    "all_of",
    "envelope_ok",
    "flag_false",
    "flag_true",
    "is_count",
    "is_empty",
    "is_value",
    "is_zero",
    "join_hooks",
    "post_hook",
    "pre_hook_coerce",
    "pre_hook_identity_answers",
    "pre_hook_notes_log",
    "pre_hook_promise_guard",
]

CoerceKind = Literal["money", "date_iso", "digits", "time_hhmm", "text", "token", "product_id"]

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


def _coerce_block(field: str, kind: CoerceKind, enum: Sequence[str] = ()) -> list[str]:
    """Statements (indented 8 spaces, inside ``if isinstance(b, dict):``) that normalise ``b[field]``."""
    key = repr(field)
    if kind == "money":
        return [
            f"        x = b.get({key})",
            *("        " + line for line in MONEY_BODY.splitlines()),
            "        if c is not None:",
            f"            b[{key}] = c / 100.0",
        ]
    if kind == "date_iso":
        return [
            f"        v = str(b.get({key}) or '').strip()[:10]",
            "        if v:",
            f"            b[{key}] = v",
        ]
    if kind == "digits":
        return [
            f"        v = ''.join(c for c in str(b.get({key}) or '') if c.isdigit())",
            "        if v:",
            f"            b[{key}] = v",
        ]
    if kind == "time_hhmm":
        return [
            f"        v = str(b.get({key}) or '').strip().replace('.', ':')",
            "        parts = v.split(':')",
            "        if len(parts) >= 2 and parts[0].strip().isdigit() and parts[1].strip()[:2].isdigit():",
            f"            b[{key}] = parts[0].strip().zfill(2) + ':' + parts[1].strip()[:2]",
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
        # the id of the data wins (``CC-4821``): matched ignoring case, spaces, dashes; a bare number completes to an id
        return [
            f"        v = str(b.get({key}) or '').strip().replace(' ', '')",
            "        if v:",
            "            ids = [x for x in str(vars.get('product_ids') or '').split(',') if x]",
            "            low = ''.join(c for c in v.lower() if c.isalnum())",
            "            hit = ''",
            "            for x in ids:",
            "                if ''.join(c for c in x.lower() if c.isalnum()) == low:",
            "                    hit = x",
            "                    break",
            "            if not hit:",
            "                for x in ids:",
            "                    if ''.join(c for c in x.lower() if c.isalnum()).lstrip('abcdefghijklmnopqrstuvwxyz') == low:",
            "                        hit = x",
            "                        break",
            f"            b[{key}] = hit or v",
        ]
    return [
        f"        v = str(b.get({key}) or '').strip()",
        "        if v:",
        f"            b[{key}] = v",
    ]


def pre_hook_coerce(fields: Mapping[str, CoerceKind], enums: Mapping[str, Sequence[str]] | None = None) -> str:
    """A pre-process hook that normalises request body fields.

    ``fields`` maps a body field to its kind: ``money`` (number with two decimals), ``date_iso`` (``YYYY-MM-DD``),
    ``digits`` (digits only), ``time_hhmm``, ``text`` (stripped), ``token`` or ``product_id``.

    * ``token`` with allowed values in ``enums`` is written back in the contract's own spelling (``Hardship`` stays
      ``Hardship``; ``hardship`` becomes it; a value outside the enum keeps its own spelling for the service to judge);
      without allowed values it is stripped and lower-cased.
    * ``product_id`` is matched against ``vars['product_ids']`` ignoring case, spaces and dashes and written back as the
      id of the data (``CC-4821``); a bare number is completed to the id format listed there.
    """
    allowed = enums or {}
    lines = ["try:", "    b = config.get('body')", "    if isinstance(b, dict):"]
    for field, kind in fields.items():
        lines += _coerce_block(field, kind, allowed.get(field) or ())
    if not fields:
        lines.append("        pass")
    lines += ["except Exception:", "    pass"]
    return assert_safe_code(strip_code("\n".join(lines)), "pre_hook_coerce")


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
) -> str:
    """A pre-process hook that blocks a promise the rules do not allow (the HARD PROMISE GUARD) and prepares the spoken fix.

    Run it after :func:`pre_hook_coerce` (it reads the cleaned body). It checks the request against what ``account_facts``
    wrote - ``product_ids`` (csv), ``allowed_<pid>`` (csv of allowed ISO dates), ``allowed_spoken_<pid>`` (the same dates
    spoken, same order), ``<pid>_floor``, ``<pid>_past_due``, ``<pid>_name`` and ``<pid>_deadline_spoken`` - and blocks when
    the product id is unknown, the date is not an allowed date, or the amount is outside the floor..full range (or is
    neither the floor nor the full amount when ``accept_between`` is false). Missing facts never block, with one exception:
    ``allowed_count_<pid>`` = ``0`` (written by ``account_facts``) means that no day is left before the deadline, and every
    date is then blocked - an empty list of allowed dates is "nothing is allowed", not "no information". A list cut at the
    horizon (``until_<pid>`` = ``open``, or an ISO date up to which later days are allowed) accepts later ISO dates.
    The product id is matched ignoring case (the data's spelling is used for every lookup).

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
    """
    pf, af, df = repr(product_field), repr(amount_field), repr(date_field)
    cur = py_literal(f" {currency.strip()}") if currency.strip() else "''"
    lines = [
        "try:",
        "    b = config.get('body')",
        "    if isinstance(b, dict):",
        f"        pid = str(b.get({pf}) or '').strip()",
        f"        dt = str(b.get({df}) or '').strip()[:10]",
        f"        x = b.get({af})",
        *("        " + line for line in MONEY_BODY.splitlines()),
        "        amount = c",
        "        known = [k for k in str(vars.get('product_ids') or '').split(',') if k]",
        "        if known and pid not in known:",
        "            for k in known:",
        "                if k.lower() == pid.lower():",
        "                    pid = k",
        "                    break",
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
        "        allowed = [k for k in str(vars.get('allowed_' + pid) or '').split(',') if k]",
        "        isiso = len(dt) == 10 and dt[4:5] == '-' and dt[7:8] == '-'",
    ]
    if enabled_dates:
        lines += [
            "        if not reason and str(vars.get('allowed_count_' + pid) or '') == '0':",
            "            reason = 'no promise date is left for ' + pid",
            "            why = 'none'",
            "        if not reason and allowed and dt not in allowed:",
            "            tail = str(vars.get('until_' + pid) or '')",
            "            beyond = bool(tail) and isiso and dt > allowed[-1] and (tail == 'open' or dt <= tail)",
            "            if not beyond:",
            "                reason = 'the date ' + dt + ' is not an allowed promise date for ' + pid + ' (latest allowed: ' + allowed[-1] + ')'",
            "                why = 'date'",
        ]
    between = "" if accept_between else " or (amount != low and amount != full)"
    lines += [
        "        if not reason and amount is not None and full is not None and full > 0:",
        f"            if amount < low or amount > full{between}:",
        "                reason = 'the amount ' + str(amount // 100) + '.' + ('0' + str(amount % 100))[-2:] + ' is not allowed for ' + pid",
        "                why = 'amount'",
        "        if reason:",
        "            vars['promise_guard'] = 'blocked: ' + reason",
        "            vars['promise_guard_blocks'] = str(int(float(vars.get('promise_guard_blocks') or 0)) + 1)",
        "            pname = str(vars.get(pid + '_name') or 'this product')",
        f"            cur = {cur}",
        "            offer_c = amount if (amount is not None and full is not None and amount >= low and amount <= full) else full",
        "            offer = ('%d.%02d' % (offer_c // 100, offer_c % 100)) if offer_c is not None else ''",
        "            say = " + py_literal(PROMISE_FIX_DEFAULT),
        "            if why == 'none':",
        "                say = 'I am sorry, I cannot record a payment date for your ' + pname + ' in this call.'",
        "            elif why == 'date':",
        "                spoken = [k for k in str(vars.get('allowed_spoken_' + pid) or '').split(',') if k]",
        "                if not isiso:",
        "                    say = 'I am sorry, I did not catch the day for your ' + pname + '. Which day can you pay?'",
        "                elif dt > allowed[-1]:",
        "                    last = str(vars.get(pid + '_deadline_spoken') or '')",
        "                    if not last:",
        "                        last = spoken[-1] if len(spoken) == len(allowed) else allowed[-1]",
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
            "                    after = [k for k in allowed if k > dt]",
            "                    before = [k for k in allowed if k < dt]",
            f"                    {pick}",
            "                    if tgt:",
            "                        said = spoken[allowed.index(tgt)] if allowed.index(tgt) < len(spoken) else tgt",
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
    amount_say = (
        "'I am sorry, for your ' + pname + ' I can record an amount from ' + lo_t + ' to ' + hi_t + cur"
        " + '. Which amount can you pay?'"
        if accept_between
        else "'I am sorry, for your ' + pname + ' I can record only ' + lo_t + ' or ' + hi_t + cur"
        " + '. Which one do you prefer?'"
    )
    lines += [
        "            elif why == 'amount' and full is not None:",
        "                lo_t = '%d.%02d' % (low // 100, low % 100)",
        "                hi_t = '%d.%02d' % (full // 100, full % 100)",
        "                if low >= full:",
        "                    say = 'I am sorry, for your ' + pname + ' I can record only the full amount of ' + hi_t + cur"
        " + '. Can you pay that?'",
        "                else:",
        f"                    say = {amount_say}",
        "            vars['promise_guard_say'] = say",
        "            config['url'] = ''",
        "        else:",
        "            vars['promise_guard'] = 'ok'",
        "except Exception:",
        "    pass",
    ]
    return assert_safe_code(strip_code("\n".join(lines)), "pre_hook_promise_guard")


def pre_hook_notes_log(
    product_field: str = "product_id", reason_field: str = "reason", reasons: Sequence[str] = ()
) -> str:
    """A pre-process hook for the notes tool: normalises both fields and appends ``<product>:<reason>;`` to ``vars['notes_log']``.

    The product id is written in the spelling of ``vars['product_ids']`` and the reason in the spelling of ``reasons``
    (the contract's enum), so the log and the decision code agree whatever case the model used.

    The decision code reads ``notes_log`` to apply the brief's multi-product closing rule. VERIFIED on the platform
    (experiment E-8): a write to ``vars`` made in a pre-hook persists in the call's variable table and is visible to
    routing and to later nodes. The decision code still falls back to the tool's own ``add_note_args.*`` variables and
    success counter when the log is empty.
    """
    base = pre_hook_coerce({product_field: "product_id", reason_field: "token"}, enums={reason_field: reasons})
    log = "\n".join(
        [
            "try:",
            "    b = config.get('body')",
            "    if isinstance(b, dict):",
            f"        pid = str(b.get({product_field!r}) or '').strip()",
            f"        rsn = str(b.get({reason_field!r}) or '').strip()",
            "        if pid and rsn:",
            "            vars['notes_log'] = str(vars.get('notes_log') or '') + pid + ':' + rsn + ';'",
            "except Exception:",
            "    pass",
        ]
    )
    return join_hooks(base, log)


def pre_hook_identity_answers(
    field_name: str,
    answer_name: str,
    formats: Mapping[str, str],
    date_order: Literal["DMY", "MDY", "YMD"] = "DMY",
) -> str:
    """A pre-process hook for the identity tool: normalises the answer by the question it belongs to.

    Args:
        field_name: Body field that says which question this is (the contract's identity ``field``).
        answer_name: Body field with the answer.
        formats: ``field value -> answer_format`` (``date_iso``, ``digits``, ``year``, ``postcode``, ``text``).
        date_order: How an all-numeric date is read (the brief's rule; ``DMY`` = day, month, year).

    A numeric date (``14/03/1988``, ``14 03 1988``, ``1988 03 14``) becomes ``YYYY-MM-DD``; ``digits`` and ``year``
    answers keep only their digits. Anything else is left as the model wrote it.
    """
    pairs = ", ".join(f"({name!r}, {kind!r})" for name, kind in formats.items())
    order_index = {"DMY": (0, 1, 2), "MDY": (1, 0, 2), "YMD": (2, 1, 0)}[date_order]
    lines = [
        "try:",
        "    b = config.get('body')",
        "    if isinstance(b, dict):",
        f"        kinds = dict([{pairs}])",
        f"        kind = kinds.get(str(b.get({field_name!r}) or ''), '')",
        f"        a = str(b.get({answer_name!r}) or '').strip()",
        "        if a and kind == 'date_iso':",
        "            parts = ''.join(c if c.isdigit() else ' ' for c in a).split()",
        "            if len(parts) == 3 and len(parts[0]) == 4:",
        "                a = parts[0] + '-' + parts[1].zfill(2) + '-' + parts[2].zfill(2)",
        "            elif len(parts) == 3 and len(parts[2]) == 4:",
        f"                a = parts[2] + '-' + parts[{order_index[1]}].zfill(2) + '-' + parts[{order_index[0]}].zfill(2)",
        "        elif a and kind in ('digits', 'year'):",
        "            a = ''.join(c for c in a if c.isdigit()) or a",
        "        if a:",
        f"            b[{answer_name!r}] = a",
        "except Exception:",
        "    pass",
    ]
    return assert_safe_code(strip_code("\n".join(lines)), "pre_hook_identity_answers")


def join_hooks(*codes: str) -> str:
    """Concatenate several hook snippets (each already wrapped in its own ``try``); empty ones are skipped."""
    parts = [strip_code(code) for code in codes if code and code.strip()]
    return assert_safe_code("".join(parts), "joined hooks") if parts else ""
