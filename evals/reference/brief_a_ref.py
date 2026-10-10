"""Hand-authored REFERENCE phone agent for the kit's Brief A (development fixture, never submitted).

Why this exists (paper principle: an expert reference ceiling + "measure, don't guess"): before the compiler
generates agents from arbitrary briefs, we learn on the public brief which platform mechanisms actually pass the
official scorer (ledger + judge). Everything that works here is then reproduced by the compiler from the PolicyIR.
Brief A values appear here ON PURPOSE because this file is a fixture under evals/, not builder code.

Usage (repo root, Freya env vars loaded):
    python evals/reference/brief_a_ref.py --write            # write evals/reference/out/ref-a.workflow.json
    python evals/reference/brief_a_ref.py --validate         # dry-run validation on Freya
    python evals/reference/brief_a_ref.py --deploy           # save_workflow('ref-a', ...) + publish, prints ids
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
OUT = pathlib.Path(__file__).resolve().parent / "out"
BANK = "https://freya-bank-mock.freyavoice.workers.dev/v1"

HOLIDAYS = ["2026-10-26", "2026-11-11", "2026-11-26", "2026-12-25", "2027-01-01"]

# ----------------------------------------------------------------------------------------------- code snippets
# Rules for code (kit docs): read/write through `vars`, never raise, no double braces, only json/re/datetime.

SETUP_CODE = r'''
import datetime
import json
try:
    hol = set(json.loads(HOLIDAYS_JSON))
    raw = str(vars.get('now') or '').strip().replace('Z', '')
    try:
        now = datetime.datetime.fromisoformat(raw[:19])
    except Exception:
        now = datetime.datetime.fromisoformat(raw[:10] + 'T09:00')
    today = now.date()
    WD = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
    MO = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December']
    def isbd(d):
        return d.weekday() < 5 and d.isoformat() not in hol
    def spoken(d):
        return WD[d.weekday()] + ' ' + str(d.day) + ' ' + MO[d.month - 1] + ' ' + str(d.year)
    def nth_bd_after(d, n):
        x = d
        c = 0
        while c < n:
            x = x + datetime.timedelta(days=1)
            if isbd(x):
                c += 1
        return x
    def next_bd(d):
        x = d + datetime.timedelta(days=1)
        while not isbd(x):
            x = x + datetime.timedelta(days=1)
        return x
    def last_bd_of_week(d, offset):
        monday = d - datetime.timedelta(days=d.weekday()) + datetime.timedelta(days=7 * offset)
        x = monday + datetime.timedelta(days=6)
        while x >= monday and not isbd(x):
            x = x - datetime.timedelta(days=1)
        return x
    this_monday = today - datetime.timedelta(days=today.weekday())
    lines = []
    for i in range(0, 50):
        d = today + datetime.timedelta(days=i)
        wk = (d - this_monday).days // 7
        week = 'this week' if wk == 0 else ('next week' if wk == 1 else ('in ' + str(wk) + ' weeks'))
        if isbd(d):
            kind = 'business day'
        else:
            nb2 = next_bd(d)
            alt = ' -> if the customer names this day, offer ' + spoken(nb2) + ' (' + nb2.isoformat() + ') instead'
            kind = ('BANK HOLIDAY, not a business day' if d.isoformat() in hol else 'weekend, not a business day') + alt
        rel = ' (today)' if i == 0 else (' (tomorrow)' if i == 1 else '')
        lines.append(d.isoformat() + ' = ' + spoken(d) + rel + ' [' + week + '] ' + kind)
    a1 = last_bd_of_week(today, 0)
    if a1 <= today:
        a1 = last_bd_of_week(today, 1)
    vars['today_iso'] = today.isoformat()
    vars['today_spoken'] = spoken(today)
    vars['now_time'] = now.strftime('%H:%M')
    vars['date_table'] = '\n'.join(lines)
    vars['attempt1_date'] = a1.isoformat()
    vars['attempt1_spoken'] = spoken(a1)
    vars['deadline_early'] = nth_bd_after(today, 12).isoformat()
    vars['deadline_late'] = nth_bd_after(today, 7).isoformat()
    nb = next_bd(today)
    cb = []
    if isbd(today) and now.strftime('%H:%M') < '19:00':
        cb.append('today ' + spoken(today) + ' (' + today.isoformat() + ') at a time later than ' + now.strftime('%H:%M') + ' and no later than 19:00')
    cb.append('the next business day ' + spoken(nb) + ' (' + nb.isoformat() + ') from 10:00 to 19:00')
    vars['callback_facts'] = 'Now is ' + spoken(today) + ', ' + now.strftime('%H:%M') + '. A callback can be booked only for: ' + ' or '.join(cb) + '. Nothing else is allowed.'
    vars['setup_state'] = 'ok'
except Exception as e:
    vars['setup_state'] = 'error: ' + str(e)[:200]
    vars['date_table'] = vars.get('date_table') or ''
    vars['callback_facts'] = vars.get('callback_facts') or ''
'''.replace("HOLIDAYS_JSON", repr(json.dumps(HOLIDAYS)))

ACCOUNT_FACTS_CODE = r'''
import datetime
import json
try:
    hol = set(json.loads(HOLIDAYS_JSON))
    WD = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
    MO = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December']
    def isbd(d):
        return d.weekday() < 5 and d.isoformat() not in hol
    def spoken(d):
        return WD[d.weekday()] + ' ' + str(d.day) + ' ' + MO[d.month - 1]
    def nth_bd_after(d, n):
        x = d
        c = 0
        while c < n:
            x = x + datetime.timedelta(days=1)
            if isbd(x):
                c += 1
        return x
    def money(x):
        try:
            return '%.2f' % float(x)
        except Exception:
            return str(x)
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
        for k in list(vars.keys()):
            ks = str(k)
            if '.products.' in ks:
                tail = ks.split('.products.', 1)[1]
                parts = tail.split('.', 1)
                if len(parts) == 2 and parts[0].isdigit():
                    found.setdefault(int(parts[0]), dict())[parts[1]] = vars.get(k)
        return [found[i] for i in sorted(found)]
    raw = str(vars.get('now') or '').strip().replace('Z', '')
    today = datetime.date.fromisoformat(raw[:10])
    a1 = datetime.date.fromisoformat(str(vars.get('attempt1_date')))
    products = load_products()
    out = []
    ids = []
    stmts = []
    deadlines = dict()
    for i, p in enumerate(products):
        if not isinstance(p, dict):
            continue
        pid = str(p.get('product_id') or ('p' + str(i + 1)))
        ids.append(pid)
        t = str(p.get('type') or '')
        if t == 'credit_card':
            name = 'credit card ending ' + str(p.get('card_last4') or '')
            floor = p.get('minimum_payment')
            floor_name = 'minimum payment'
        elif t == 'personal_loan':
            name = 'personal loan'
            floor = p.get('instalment_amount')
            floor_name = 'instalment amount'
        elif t == 'car_loan':
            name = 'car loan'
            floor = p.get('instalment_amount')
            floor_name = 'instalment amount'
        else:
            name = t.replace('_', ' ')
            floor = p.get('minimum_payment') or p.get('instalment_amount')
            floor_name = 'floor'
        late = int(float(p.get('days_late') or 0))
        if late <= 7:
            band = 'early'
            dl = nth_bd_after(today, 12)
            attempts = 1
        else:
            band = 'late'
            dl = nth_bd_after(today, 7)
            attempts = 2
        allowed = []
        allowed_iso = []
        d = today
        while d <= dl:
            if isbd(d):
                allowed.append(d.isoformat() + ' ' + WD[d.weekday()])
                allowed_iso.append(d.isoformat())
            d = d + datetime.timedelta(days=1)
        vars['allowed_' + pid] = ','.join(allowed_iso)
        att1 = a1 if a1 <= dl else dl
        s = []
        s.append(pid + ': ' + name + '. Past-due amount ' + money(p.get('past_due')) + ' dollars, ' + str(late) + ' days late (' + band + ' band).')
        s.append('   Floor (' + floor_name + '): ' + money(floor) + ' dollars. Deadline (latest promise date): ' + dl.isoformat() + ' = ' + spoken(dl) + '.')
        s.append('   Floor offer - when the customer says the full amount is too much, say exactly: "I can offer you the ' + floor_name + ' of ' + money(floor) + ' dollars instead. The rest of the amount stays overdue."')
        s.append('   Persuasion attempts for ' + pid + ': EXACTLY ' + str(attempts) + ' (' + band + ' band). Make them one at a time, only after a refusal or a non-answer:')
        s.append('     Attempt 1 - ask: "Could you pay the ' + money(p.get('past_due')) + ' dollars for your ' + name + ' on ' + spoken(att1) + '?" (if they already said the full amount is too much, use the floor ' + money(floor) + ' dollars instead).')
        if attempts >= 2:
            s.append('     Attempt 2 - ask: "Could you pay at least the ' + floor_name + ' of ' + money(floor) + ' dollars by ' + spoken(dl) + '? The rest of the amount stays overdue."')
        s.append('     After ' + ('attempt 2' if attempts >= 2 else 'attempt 1') + ' is refused: make NO further offer for ' + pid + '.')
        s.append('   Allowed promise dates: ' + ', '.join(allowed) + '.')
        out.append('\n'.join(s))
        stmts.append('Your ' + name + ' has a past-due amount of ' + money(p.get('past_due')) + ' dollars and is ' + str(late) + ' days late.')
        vars[pid + '_deadline'] = dl.isoformat()
        vars[pid + '_name'] = name
        vars[pid + '_deadline_spoken'] = spoken(dl)
        deadlines[pid] = dl
        vars[pid + '_floor'] = money(floor)
        vars[pid + '_past_due'] = money(p.get('past_due'))
    if len(stmts) == 1:
        vars['debt_statement'] = 'Thank you. I am calling from the collections team about a late payment. ' + stmts[0] + ' Can you make this payment today?'
    else:
        vars['debt_statement'] = 'Thank you. I am calling from the collections team about late payments on ' + str(len(stmts)) + ' products. ' + ' '.join(stmts) + ' Can you pay all of them today?'
    vars['product_ids'] = ','.join(ids)
    marked = []
    for line in str(vars.get('date_table') or '').split('\n'):
        try:
            d0 = datetime.date.fromisoformat(line[:10])
        except Exception:
            marked.append(line)
            continue
        late_for = [k for k in ids if k in deadlines and d0 > deadlines[k]]
        if late_for and len(late_for) == len(ids):
            line = line + ' | AFTER THE DEADLINE - not allowed: offer the deadline instead'
        elif late_for:
            line = line + ' | after the deadline of ' + ', '.join(late_for) + ' - not allowed for ' + ', '.join(late_for)
        marked.append(line)
    vars['date_table'] = '\n'.join(marked)
    vars['product_count'] = str(len(ids))
    vars['account_facts'] = 'LATE PRODUCTS (discuss them in this order):\n' + '\n'.join(out)
    vars['account_state'] = 'ok' if ids else 'empty'
except Exception as e:
    vars['account_state'] = 'error: ' + str(e)[:200]
    vars['account_facts'] = vars.get('account_facts') or 'The account facts could not be computed. Say you do not have that information.'
'''.replace("HOLIDAYS_JSON", repr(json.dumps(HOLIDAYS)))

DECIDE_CODE = r'''
try:
    def cnt(k):
        try:
            return int(float(vars.get(k) or 0))
        except Exception:
            return 0
    chosen = str(vars.get('chosen_end') or '')
    out = ''
    rsn = ''
    # Paid invariant: "paid" needs a 'paid' note for EVERY known product (product_ids order); a product with no note
    # is not paid. The latest note of a product counts. Some note not paid -> no_promise with the reason of the first
    # such product in product order. Products without any note -> no verdict here: fall back to the chosen end.
    order = [x for x in str(vars.get('product_ids') or '').split(',') if x]
    notes = dict()
    for item in str(vars.get('notes_log') or '').split(';'):
        if ':' in item:
            k, v = item.split(':', 1)
            if k.strip() and v.strip():
                notes[k.strip()] = v.strip()
    keys = [k for k in order if k in notes] + [k for k in notes if k not in order]
    unpaid = [notes[k] for k in keys if notes[k] != 'paid']
    note_out = None
    if unpaid:
        note_out = ('no_promise', unpaid[0])
    elif notes and all(k in notes for k in (order or list(notes))):
        note_out = ('paid', '')
    if cnt('handoff_restructuring_success_count') > 0 or chosen == 'handoff_restructuring':
        out, rsn = 'transferred', 'restructuring'
    elif cnt('handoff_support_success_count') > 0 or chosen == 'handoff_support':
        out, rsn = 'transferred', 'support'
    elif cnt('handoff_human_success_count') > 0 or chosen == 'handoff_human':
        out, rsn = 'transferred', 'human'
    elif cnt('create_promise_success_count') > 0:
        out = 'promise'
    elif note_out is not None:
        out, rsn = note_out
    elif chosen == 'paid':
        out = 'paid'
    elif chosen in ('refused', 'hardship', 'dispute', 'service_error'):
        out, rsn = 'no_promise', chosen
    elif chosen == 'promise':
        out, rsn = 'no_promise', ('service_error' if cnt('create_promise_failure_count') - cnt('promise_guard_blocks') >= 2 else 'refused')
    elif chosen == 'auto' and str(vars.get('verify_identity_response.data.all_verified') or '').lower() != 'true':
        left = str(vars.get('verify_identity_response.data.wrong_answers_left') or '')
        out, rsn = 'not_verified', ('failed' if left in ('0', '0.0') else 'declined')
    elif chosen == 'auto':
        sit = str(vars.get('call_situation') or '')
        if sit == 'paid':
            out = 'paid'
        elif sit in ('dispute', 'hardship', 'refused'):
            out, rsn = 'no_promise', sit
        else:
            out, rsn = 'no_promise', 'refused'
    elif chosen in ('failed', 'declined'):
        out, rsn = 'not_verified', chosen
    elif chosen in ('callback', 'no_callback', 'wrong_number', 'deceased', 'unclear'):
        if chosen == 'callback' and cnt('schedule_callback_success_count') == 0:
            chosen = 'no_callback'
        out, rsn = 'not_reached', chosen
    else:
        out, rsn = 'not_reached', 'unclear'
    vars['final_outcome'] = out
    if rsn:
        vars['final_reason'] = rsn
    vars['decide_state'] = 'ok'
except Exception as e:
    vars['final_outcome'] = vars.get('final_outcome') or 'not_reached'
    vars['decide_state'] = 'error: ' + str(e)[:200]
'''

VERIFY_POST = r'''
try:
    if isinstance(result, dict):
        result.pop('header', None)
        d = result.get('data')
        if isinstance(d, dict):
            hint = str(d.get('hint') or '')
            left = d.get('wrong_answers_left')
            if str(d.get('all_verified')).lower() == 'true':
                msg = 'Both security answers are verified. Say nothing now; the call continues automatically.'
            elif str(d.get('ok')).lower() == 'false' and not hint:
                msg = 'The answer could not be read. Ask for the same answer again (a date of birth with day, month and year). This does not count as a wrong answer.'
            elif hint == 'day_month_swapped':
                msg = 'Day and month were given the other way round. Ask the customer to say the month as a word. This does not count as a wrong answer.'
            elif str(d.get('verified')).lower() == 'true':
                msg = 'This answer is correct. Now ask the next security question that has not been answered yet.'
            elif str(left) in ('0', '0.0'):
                msg = 'Three wrong answers: the identity check has ended. Do not state the debt.'
            else:
                msg = 'This answer is wrong. Do not say what the right answer is. Ask the same question again.'
            d['what_to_do_next'] = msg
except Exception:
    pass
'''

PROMISE_PRE = r'''
try:
    b = config.get('body')
    if isinstance(b, dict):
        a = b.get('amount')
        if isinstance(a, str):
            a = a.replace(',', '').replace('$', '').replace('dollars', '').strip()
        try:
            b['amount'] = round(float(a), 2)
        except Exception:
            pass
        pid = str(b.get('product_id') or '').strip().lower()
        if pid and not pid.startswith('p') and pid.isdigit():
            pid = 'p' + pid
        if pid:
            b['product_id'] = pid
        dt = str(b.get('date') or '').strip()[:10]
        b['date'] = dt
        reason = ''
        known = [x for x in str(vars.get('product_ids') or '').split(',') if x]
        if known and pid not in known:
            reason = 'unknown product_id ' + pid + '; use one of ' + ','.join(known)
        allowed = [x for x in str(vars.get('allowed_' + pid) or '').split(',') if x]
        if not reason and allowed and dt not in allowed:
            reason = 'the date ' + dt + ' is not an allowed promise date for ' + pid + ' (latest allowed: ' + allowed[-1] + ')'
        try:
            amt = float(b.get('amount'))
            fl = float(vars.get(pid + '_floor') or 0)
            full = float(vars.get(pid + '_past_due') or 0)
            if not reason and full > 0 and (amt < fl - 0.001 or amt > full + 0.001):
                reason = 'the amount ' + ('%.2f' % amt) + ' is outside ' + ('%.2f' % fl) + ' to ' + ('%.2f' % full)
        except Exception:
            pass
        if reason:
            vars['promise_guard'] = 'blocked: ' + reason
            vars['promise_guard_blocks'] = str(int(float(vars.get('promise_guard_blocks') or 0)) + 1)
            pname = str(vars.get(pid + '_name') or 'this product')
            if allowed and dt not in allowed and dt > allowed[-1]:
                say = 'I am sorry, the latest date I can record for your ' + pname + ' is ' + str(vars.get(pid + '_deadline_spoken') or allowed[-1]) + '. Could you pay ' + ('%.2f' % float(b.get('amount') or 0)) + ' dollars on that day?'
            elif allowed and dt not in allowed:
                nxt = [x for x in allowed if x > dt]
                say = 'I am sorry, I cannot record that day for your ' + pname + '. ' + ('The next day I can record is ' + nxt[0] + '. Would that work for you?' if nxt else 'Could you tell me another day?')
            elif 'amount' in reason:
                say = 'I am sorry, for your ' + pname + ' I can record an amount from ' + str(vars.get(pid + '_floor')) + ' to ' + str(vars.get(pid + '_past_due')) + ' dollars. Which amount can you pay?'
            else:
                say = 'I am sorry, I could not record that. Let me check the details again.'
            vars['promise_guard_say'] = say
            config['url'] = ''
        else:
            vars['promise_guard'] = 'ok'
except Exception:
    pass
'''

PROMISE_POST = r'''
try:
    if isinstance(result, dict):
        result.pop('header', None)
        d = result.get('data')
        if isinstance(d, dict):
            if str(d.get('ok')).lower() == 'true':
                d['what_to_do_next'] = 'The promise IS recorded. If this was the only product, say nothing: the system confirms it. Otherwise tell the customer it is recorded, say exactly: You will receive a text message confirming this promise. Then continue with the next product.'
            else:
                d['what_to_do_next'] = 'The promise was NOT recorded. Never say it is recorded. If this is the first failure for this product, call create_promise once more with exactly the same values now. If it already failed once before, tell the customer you could not record it, thank them, say goodbye and call transition_to_end_service_error.'
        elif 'url' in str(result.get('message') or result.get('error') or '').lower():
            result['what_to_do_next'] = 'The promise was NOT recorded: the date or amount is not allowed for this product. Do not say it is recorded. Offer an allowed date (for a date after the deadline, offer the deadline for the same amount) or an amount from the floor to the full amount, read back and ask again.'
        elif result.get('status') != 'success':
            result['what_to_do_next'] = 'The promise was NOT recorded (service error). If this is the first failure, call create_promise once more with the same values; if it already failed before, tell the customer you could not record it, say goodbye and call transition_to_end_service_error.'
except Exception:
    pass
'''

NOTE_PRE = r'''
try:
    b = config.get('body')
    if isinstance(b, dict):
        pid = str(b.get('product_id') or '').strip().lower()
        rsn = str(b.get('reason') or '').strip().lower()
        b['product_id'] = pid
        b['reason'] = rsn
        log = str(vars.get('notes_log') or '')
        vars['notes_log'] = log + pid + ':' + rsn + ';'
except Exception:
    pass
'''

NOTE_POST = r'''
try:
    if isinstance(result, dict):
        result.pop('header', None)
        d = result.get('data')
        if isinstance(d, dict):
            if str(d.get('ok')).lower() == 'true':
                d['what_to_do_next'] = 'The note is saved. Continue with the next product, or close the call if every product is handled.'
            else:
                d['what_to_do_next'] = 'The note was NOT saved. Call add_note once more with the same values.'
except Exception:
    pass
'''

CALLBACK_POST = r'''
try:
    if isinstance(result, dict):
        result.pop('header', None)
        d = result.get('data')
        if isinstance(d, dict):
            if str(d.get('ok')).lower() == 'true':
                d['what_to_do_next'] = 'The callback is booked. Say nothing; the system confirms it and closes the call.'
            else:
                d['what_to_do_next'] = 'The callback was NOT booked. Do not say it is booked. Check the date and time against the allowed callback times and ask again.'
except Exception:
    pass
'''

GUARD_RESTRUCTURING = r'''
try:
    seen = str(vars.get('hardship_seen') or '').strip().lower()
    sit = str(vars.get('call_situation') or '').strip().lower()
    vars['restructuring_blocked'] = 'yes' if (seen == 'yes' or sit == 'hardship') else 'no'
except Exception:
    vars['restructuring_blocked'] = 'no'
'''

# ----------------------------------------------------------------------------------------------- builders


def code_node(name: str, code: str, nxt: str) -> dict:
    return {
        "name": name, "node_type": "code", "passthrough": True, "passthrough_default_transition": nxt,
        "on_enter": {"type": "silent"}, "disable_inactivity_monitor": True,
        "pre_actions": [{"type": "function", "function": {"name": "code_exec", "handler": "__function__:code_exec",
                         "parameters": {"type": "object", "properties": {
                             "code": {"type": "string", "default": code},
                             "timeout_ms": {"type": "number", "default": 10000}}}}}],
        "transitions": [{"target_node": nxt, "description": "always", "condition": {"operator": "always"}}],
    }


def set_choice_node(name: str, choice: str) -> dict:
    return code_node(name, f"vars['chosen_end'] = '{choice}'\n", "decide")


def api_body(fields: dict[str, dict]) -> dict:
    return {"type": "object", "properties": fields, "required": [k for k, v in fields.items() if v.pop("_req", False)]}


def http_params(path: str, body: dict) -> dict:
    return {"type": "object", "properties": {
        "url": {"type": "string", "default": f"{BANK}/{path}"},
        "method": {"type": "string", "default": "POST"},
        "headers": {"type": "object", "properties": {"Content-Type": {"type": "string", "default": "application/json"}}},
        "body": body}}


def engine_call_node(name: str, tool: str, body_defaults: dict[str, str], nxt: str, retries: int = 0,
                     mapping: list[dict] | None = None) -> dict:
    body = {"type": "object", "properties": {k: {"type": "string", "default": v} for k, v in body_defaults.items()},
            "required": []}
    node = {
        "name": name, "node_type": "api_request", "passthrough": True, "passthrough_default_transition": nxt,
        "on_enter": {"type": "silent"}, "disable_inactivity_monitor": True,
        "pre_actions": [{"type": "function", "config": {"timeoutMs": 12000, "maxRetries": retries},
                         "function": {"name": tool, "handler": "__function__:api_call", "description": tool,
                                      "parameters": http_params(tool, body)}}],
        "transitions": [{"target_node": nxt, "description": "always", "condition": {"operator": "always"}}],
    }
    if mapping:
        node["response_mapping"] = mapping
    return node


def tool_transition(target: str, description: str) -> dict:
    return {"target_node": target, "description": description, "is_tool_call": True,
            "condition": {"operator": "matches_intent", "description": description}}


def bool_true(var: str) -> list[dict]:
    return [{"variable": var, "operator": "equals", "operand": "true"},
            {"variable": var, "operator": "equals", "operand": "True"}]


NOTE_TOOL = {
    "type": "function", "config": {"timeoutMs": 12000, "maxRetries": 0},
    "function": {
        "name": "add_note", "handler": "__function__:api_call",
        "description": "Record why ONE product ended without a promise. CALL it once for every product that gets no promise, with that product's id and reason.",
        "parameters": http_params("add_note", {"type": "object", "properties": {
            "customer_id": {"type": "string", "default": "{{customer_id}}"},
            "product_id": {"type": "string", "default": "{{product_id}}", "description": "The product id from LATE PRODUCTS, e.g. p2."},
            "reason": {"type": "string", "enum": ["refused", "hardship", "dispute", "paid", "service_error"], "default": "{{reason}}",
                       "description": "Why this product has no promise: refused, hardship, dispute, paid or service_error."}},
            "required": ["product_id", "reason"]})},
    "pre_process_code": NOTE_PRE,
    "post_process_code": NOTE_POST,
}

VERIFY_TOOL = {
    "type": "function", "config": {"timeoutMs": 12000, "maxRetries": 0},
    "function": {
        "name": "verify_identity", "handler": "__function__:api_call",
        "description": "Check ONE security answer. CALL it every time the customer gives an answer to a security question, with that single answer.",
        "parameters": http_params("verify_identity", {"type": "object", "properties": {
            "customer_id": {"type": "string", "default": "{{customer_id}}"},
            "field": {"type": "string", "enum": ["date_of_birth", "phone_last4"], "default": "{{field}}",
                      "description": "Which question this answer belongs to: date_of_birth or phone_last4."},
            "answer": {"type": "string", "default": "{{answer}}",
                       "description": "date_of_birth as YYYY-MM-DD (convert spoken dates; numbers-only dates are day, month, year). phone_last4 as exactly four digits. Never send a partial answer."}},
            "required": ["field", "answer"]})},
    "post_process_code": VERIFY_POST,
}

PROMISE_TOOL = {
    "type": "function", "config": {"timeoutMs": 12000, "maxRetries": 0},
    "function": {
        "name": "create_promise", "handler": "__function__:api_call",
        "description": "Record ONE payment promise for ONE product. CALL it only after you read back product, amount and date and the customer clearly said yes.",
        "parameters": http_params("create_promise", {"type": "object", "properties": {
            "customer_id": {"type": "string", "default": "{{customer_id}}"},
            "product_id": {"type": "string", "default": "{{product_id}}", "description": "The product id from LATE PRODUCTS, e.g. p1."},
            "amount": {"type": "number", "default": "{{amount}}", "description": "Promised amount as a number with 2 decimals, e.g. 420.00."},
            "date": {"type": "string", "default": "{{date}}", "description": "Promise date YYYY-MM-DD, one of the allowed promise dates."}},
            "required": ["product_id", "amount", "date"]})},
    "pre_process_code": PROMISE_PRE,
    "post_process_code": PROMISE_POST,
}

CALLBACK_TOOL = {
    "type": "function", "config": {"timeoutMs": 12000, "maxRetries": 0},
    "function": {
        "name": "schedule_callback", "handler": "__function__:api_call",
        "description": "Book a callback for the customer. CALL it only when the person gave a day and time inside the allowed callback times.",
        "parameters": http_params("schedule_callback", {"type": "object", "properties": {
            "customer_id": {"type": "string", "default": "{{customer_id}}"},
            "date": {"type": "string", "default": "{{date}}", "description": "Callback date YYYY-MM-DD."},
            "time": {"type": "string", "default": "{{time}}", "description": "Callback time HH:MM, 24-hour."}},
            "required": ["date", "time"]})},
    "post_process_code": CALLBACK_POST,
}

SYSTEM_PROMPT = """You are Freya Bank's digital assistant on an outbound phone call. Speak English.
Style: one or two short sentences per turn. Ask one question at a time.
If the customer's message is cut off (it stops mid-sentence) or is only small talk without an answer, and it contains nothing you can act on, reply only with a short acknowledgement such as "I understand, go on." Do not repeat your question, do not count it as a refusal or a non-answer, and do not end the call because of it. If a message (even a cut-off one) already contains the information you need - an answer, a day, a time, a date or an amount - act on it right away. If the cut-off message started a question to you, invite them to finish it. Never end a turn with nothing: after every tool result, either speak to the customer or call the next tool.
Before identity is verified never say "collections", "debt", "payment", an amount or a product, to anyone.
If asked whether you are a robot or an AI: say you are Freya Bank's digital assistant, then continue.
If asked "Is this a scam?": say they can verify the call by phoning Freya Bank's customer service number, then continue.
If asked something you cannot answer from the facts you were given: say you do not have that information. Never invent figures, dates or reasons.
Call a tool first and speak about its result afterwards: never say that something is booked, recorded or done before the tool result confirms it.
Every call must end with a goodbye: thank the customer and say goodbye in the same message in which you call the finishing tool (a tool whose name starts with transition_to_end or transition_to_handoff)."""

OPENING_TASK = """Find out who answered. Your opening line (who you are, that the call is recorded, and "Am I speaking with {{title}} {{last_name}}?") has already been said.
- Only "yes", "that's me", "speaking" or the customer's own name confirms that you are speaking with {{title}} {{last_name}}. "Hello", "go ahead" and "who is this" do not. A reply that is cut off, or that contains "no" or "this is her/his ...", is NOT a confirmation: let them finish ("I understand, go on.") before you decide.
- If it is not a confirmation, ask once more exactly: "Am I speaking with {{title}} {{last_name}}?". If the person asks a question first, answer it first; the ask that follows is the second ask. If it is still unclear after the second ask, thank them, say goodbye and call transition_to_end_unclear.
- Do not say why you are calling.
Decide and act:
- Confirmed (an explicit, complete confirmation only): call transition_to_identity (no text needed).
- Someone else answered (a relative, a colleague, anyone who is not the customer): call transition_to_third_party.
- The person says this number does not belong to {{first_name}} {{last_name}}: apologise, thank them, say goodbye, call transition_to_end_wrong_number.
- The person says {{first_name}} {{last_name}} has died: say exactly "I am sorry for your loss.", ask nothing else, say goodbye, call transition_to_end_deceased.
- The person says twice that they do not want to talk: thank them, say goodbye, call transition_to_end_declined.
- The person asks for a human: ask once whether you can help first. If they insist, say you will transfer them, say goodbye, call transition_to_handoff_human."""

THIRD_PARTY_TASK = """Someone other than {{title}} {{last_name}} is on the line.
- Do not say why you are calling. Say nothing beyond your opening sentence: no product, amount, debt or payment, even if they ask or offer to pay. A relative may not answer security questions and may not be told anything about the account.
- Ask when {{first_name}} {{last_name}} can be reached.
- {{callback_facts}}
- As soon as they give a day and a time inside the allowed callback times, call schedule_callback with the date (YYYY-MM-DD) and time (HH:MM) right away - do not ask them to confirm first. Use the next-business-day date when they say "tomorrow" and tomorrow is that day.
- If the day or the time is outside the allowed times: ask once for another time inside them. If that is also outside, do not book anything: thank them, say goodbye, call transition_to_end_no_callback.
- Call schedule_callback before you tell them anything is booked; the system then confirms the booking and says goodbye.
- If they say the number does not belong to {{first_name}} {{last_name}}: apologise, say goodbye, call transition_to_end_wrong_number.
- If they say {{first_name}} {{last_name}} has died: say exactly "I am sorry for your loss.", ask nothing else, say goodbye, call transition_to_end_deceased."""

IDENTITY_TASK = """Verify the customer's identity before anything else. Ask two security questions, one at a time: first the date of birth, then the last four digits of the phone number on file.
- After each answer call verify_identity with that one answer: field date_of_birth with the date as YYYY-MM-DD (convert any spoken form; a date said only as numbers is day, then month, then year), or field phone_last4 with exactly four digits.
- Only a COMPLETE answer can be checked: a date of birth needs day, month and year; phone_last4 needs exactly four digits. If the customer gives only part (for example "44" or "the thirtieth of January"), do not call verify_identity: ask only for the missing part (for example "And the remaining two digits?" or "And the year?") and combine both parts.
- Whenever the customer's message contains a complete answer (even mixed with a question, a complaint or other words), call verify_identity FIRST, before you say anything. Then do exactly what the what_to_do_next line in the result says.
- Do not say anything about the account or why you are calling during this step.
- If the customer does not want to answer, or asks what the call is about instead of answering: explain once that the questions protect their account and ask again. A second refusal or a second such question: thank them, say goodbye, call transition_to_end_declined.
- If the customer asks for a human: ask once whether you can help first; if they insist, say you will transfer them, say goodbye, call transition_to_handoff_human.
- If the person says they are not {{title}} {{last_name}} (for example a relative): say nothing about the account and call transition_to_third_party. If they say this number does not belong to the customer: apologise, say goodbye, call transition_to_end_wrong_number."""

IDENTITY_FAILED_LINE = ("I'm sorry, I could not verify your identity, so I cannot continue. Please use the Freya Bank "
                        "mobile app or visit a branch. Thank you for your time, goodbye.")

NEGOTIATE_ENTER = """Say that you are calling from the collections team about a late payment. Then for each product in LATE PRODUCTS, in order, one sentence each: name it exactly as written there, its past-due amount and how many days late it is. Then ask exactly: "Can you make this payment today?" Say nothing else."""

NEGOTIATE_TASK = """The customer is verified. Facts computed by the system - use them exactly and never calculate dates or amounts yourself:
{{account_facts}}

DATE TABLE - turn whatever the customer says into a date with this table. "This <weekday>" is the one marked [this week]; "next <weekday>" is the one marked [next week]:
{{date_table}}

GETTING THE PROMISE
1. You already stated the debt and asked "Can you make this payment today?". Ask for the full past-due amount first. Never ask how much they can pay: you propose every amount.
2. A date or an amount the customer proposes is never a refusal; handle it with rules 3 and 4.
3. Dates. A promise date must be one of the product's allowed promise dates (today or later, a business day, not after its deadline).
   - A weekend day or bank holiday that is not after the deadline: offer the next business day instead (the DATE TABLE names it). This is NOT the after-deadline case.
   - A vague period ("next week", "end of the month", "when my salary comes"): ask once for the exact day. If they still give none, propose the deadline.
   - A date after the deadline: say the latest date you can record is the deadline (say the date) and offer it for the SAME amount (the full past-due amount unless a lower amount was already agreed). Moving the date is not a persuasion attempt and must not lower the amount. If they refuse the deadline, that is a flat refusal (rule 5).
4. Amounts. If they say the full amount is too much: offer the floor once, using the product's floor-offer sentence word for word (it says that the rest stays overdue). If they refuse the floor: flat refusal (rule 5). An amount they propose from the floor up to the full amount (both included) is accepted. Below the floor: say it cannot be recorded and offer the floor once; if they do not accept it: flat refusal.
5. A flat refusal or a non-answer uses one persuasion attempt ("no" to paying today with no date and no amount is a flat refusal). Make the product's attempts exactly as scripted in LATE PRODUCTS, in order, one per refusal - never more attempts than the product allows, never fewer. When the attempts are used up, stop: do not repeat an offer they refused. Thank them, say goodbye, call transition_to_end_refused.
6. Illness, a death in the family or job loss: say one short sentence of sympathy and make NO persuasion attempt. Offer ONCE to connect them with the customer support team; never repeat that offer and make no other offer of any kind (no transfer to another team, no plan, no discount) unless the customer explicitly asks for a new payment plan or lower instalments. If they accept: say you will transfer them, say goodbye, call transition_to_handoff_support. If they decline (or answer anything other than yes): thank them, say goodbye, call transition_to_end_hardship - do not ask again. (If they still name a date or an amount themselves, record the promise normally.) Once hardship was mentioned, this rule comes before rules 5 and 14: never transfer to restructuring and never persuade in this call. A request for a discount or a waiver is never a request for a new payment plan: just say you cannot offer a discount.
7. Never offer a discount, a waiver, an instalment plan or any term not listed here.

CONFIRMING AND RECORDING
8. Read back the product, the amount and the date, then ask exactly "Do you confirm?". Call create_promise only after a clear yes. If they change the date or the amount, check it with rules 3 and 4 and read back again.
9. Before you read back, check the date is in the product's allowed promise dates and the amount is between its floor and full amount. create_promise takes product_id from LATE PRODUCTS (p1, ...), the amount as a number and the date as YYYY-MM-DD. Say the promise is recorded only after the result says it is recorded, then say exactly: "You will receive a text message confirming this promise." If it fails, call it once more with the same values; if it fails again, say you could not record it, thank them, say goodbye, call transition_to_end_service_error.
10. One promise per product. A recorded promise cannot be changed or cancelled in this call; if asked, say they can change it in the mobile app.
11. After the text-message sentence: thank the customer, say goodbye and call transition_to_end_promise in the same message.

OTHER THINGS CUSTOMERS SAY
12. "I already paid": say that payments can take one business day to appear, take no promise, thank them, say goodbye, call transition_to_end_paid.
13. "This debt is not mine", "I never had this product", or the product was closed or paid off in the past: do not argue and take no promise. Thank them, say goodbye, call transition_to_end_dispute.
14. A new payment plan or lower instalments: do not describe any plan. Say you will transfer them to the team that handles this, say goodbye, call transition_to_guard_restructuring.
15. A human: ask once whether you can help first. If they insist, say you will transfer them, say goodbye, call transition_to_handoff_human.
16. If they say twice that they do not want to talk: thank them, say goodbye, call transition_to_end_refused.
{{multi_product_rules}}
Say amounts with two decimals and the word dollars (for example 420.00 dollars) and dates as weekday plus day and month (for example Friday 16 October)."""


MULTI_PRODUCT_RULES = """SEVERAL PRODUCTS (when LATE PRODUCTS lists more than one)
B1. State every late product, one sentence each, before asking for anything. Then ask whether they can pay all of them today.
B2. If not, go through the products one by one in the listed order. Do not ask about today again for each product. Start each product at attempt 1. Persuasion attempts are counted per product, with each product's own deadline. Even if the customer already said they will not pay a product, still make every scripted attempt for that product, one at a time, before you note it as refused.
B3. If the customer names one date for everything and it is after one product's deadline, record that product on its own deadline and say so in the read-back; the other products keep the date the customer named.
B4. Read all promises back together and ask "Do you confirm?" once. Then call create_promise once per product.
B5. The customer may promise some products and refuse others. For EACH product without a promise remember its reason: refused, hardship, dispute, paid or service_error. Call add_note only at the very end, when every product is handled and the call is not being transferred: once for each product without a promise, with that product_id and its reason. This also applies when no product gets a promise.
B6. A dispute or an "already paid" claim on one product does not end the call: remember the reason for it (do not call add_note yet) and continue with the other products.
B7. A request for a new payment plan still transfers the call (rule 14). Promises already recorded stay; promises agreed but not yet recorded are not recorded. On any transfer add no notes at all.
B8. If create_promise fails twice for one product, keep the recorded ones and remember the reason service_error for the failed product (do not call add_note yet).
B9. Close: when every product is handled and the customer did not ask for a transfer, first call add_note for each product without a promise. Then, if at least one promise was recorded, finish with transition_to_end_promise; otherwise finish with transition_to_end_refused (the system computes the outcome from the notes)."""


MULTI_FAILURE = ("then remember the reason service_error for that product (do not call add_note yet) and continue "
                 "with the next product (rule B8)")


def multi_variants() -> tuple[str, dict, dict]:
    """The negotiation task, create_promise and add_note of a several-product call: a promise that fails twice is noted
    for that product and the call goes on (rule B8), notes are written at the very end (audit C-11 / C-12)."""
    task = NEGOTIATE_TASK.replace(
        "say you could not record it, thank them, say goodbye, call transition_to_end_service_error.",
        f"say you could not record it, {MULTI_FAILURE}.")
    promise = json.loads(json.dumps(PROMISE_TOOL))
    promise["post_process_code"] = PROMISE_POST.replace(
        "tell the customer you could not record it, thank them, say goodbye and call transition_to_end_service_error.",
        f"tell the customer you could not record it, {MULTI_FAILURE}.").replace(
        "tell the customer you could not record it, say goodbye and call transition_to_end_service_error.",
        f"tell the customer you could not record it, {MULTI_FAILURE}.")
    note = json.loads(json.dumps(NOTE_TOOL))
    note["function"]["description"] = (
        "Record why ONE product ended without a promise. CALL it only at the very end of the call, once every product is "
        "handled and the call is not being transferred - never before a transfer - once for every product that gets no "
        "promise, with that product's id and reason.")
    return task, promise, note


def build(multi: bool = False) -> dict:
    nodes: dict[str, dict] = {}
    nodes["setup"] = code_node("setup", SETUP_CODE, "opening")
    nodes["opening"] = {
        "name": "opening", "node_type": "conversation",
        "on_enter": {"type": "tts", "text": "Hello, this is the digital assistant calling from Freya Bank. We record this call. Am I speaking with {{title}} {{last_name}}?"},
        "on_enter_first_entry_only": True,
        "task_message": OPENING_TASK,
        "max_node_turns": 30, "max_turn_transition": "end_unclear",
        "transitions": [
            tool_transition("identity", "The person confirmed they are the customer."),
            tool_transition("third_party", "Someone other than the customer answered."),
            tool_transition("end_wrong_number", "The number does not belong to the customer; you said goodbye."),
            tool_transition("end_deceased", "The customer has died; you said you are sorry for their loss and goodbye."),
            tool_transition("end_unclear", "Still unclear who answered after the second ask; you said goodbye."),
            tool_transition("end_declined", "The person said twice they do not want to talk; you said goodbye."),
            tool_transition("handoff_human", "The person insists on a human; you said you will transfer them."),
        ],
    }
    nodes["third_party"] = {
        "name": "third_party", "node_type": "conversation",
        "task_message": THIRD_PARTY_TASK, "tools": [CALLBACK_TOOL],
        "max_node_turns": 30, "max_turn_transition": "end_no_callback",
        "transitions": [
            {"target_node": "callback_done", "description": "The callback was booked",
             "condition": {"logic": "or", "conditions": [
                 {"variable": "schedule_callback_success_count", "operator": "greater_than", "operand": 0}]}},
            {"target_node": "end_auto_tp", "description": "The conversation is over",
             "condition": {"operator": "matches_intent", "description": "The agent already said goodbye and the customer now only says goodbye or thanks to end the call (for example 'Thank you, goodbye')."}},
            tool_transition("end_callback", "The callback is booked; you said goodbye."),
            tool_transition("end_no_callback", "No callback inside the allowed times; you said goodbye."),
            tool_transition("end_wrong_number", "The number does not belong to the customer; you said goodbye."),
            tool_transition("end_deceased", "The customer has died; you said you are sorry for their loss and goodbye."),
        ],
    }
    nodes["identity"] = {
        "name": "identity", "node_type": "conversation",
        "on_enter": {"type": "llm", "prompt": "Ask for the customer's date of birth. Say nothing about the account."},
        "on_enter_first_entry_only": True,
        "task_message": IDENTITY_TASK, "tools": [VERIFY_TOOL],
        "max_node_turns": 60, "max_turn_transition": "end_auto",
        "transitions": [
            {"target_node": "load_account", "description": "Both security answers verified",
             "condition": {"logic": "or", "conditions": bool_true("verify_identity_response.data.all_verified")}},
            {"target_node": "identity_failed", "description": "No wrong answers left",
             "condition": {"logic": "or", "conditions": [
                 {"variable": "verify_identity_response.data.wrong_answers_left", "operator": "equals", "operand": "0"}]}},
            tool_transition("end_declined", "The customer declined the security questions twice; you said goodbye."),
            tool_transition("handoff_human", "The customer insists on a human; you said you will transfer them."),
            tool_transition("third_party", "The person says they are not the customer (for example a relative or a colleague)."),
            tool_transition("end_wrong_number", "The person says this number does not belong to the customer; you said goodbye."),
        ],
    }
    nodes["callback_done"] = {
        "name": "callback_done", "node_type": "conversation",
        "on_enter": {"type": "tts", "text": "Thank you, the callback is booked. Goodbye."},
        "task_message": "The callback is booked. Say nothing more. No tool call is allowed in this turn.",
        "passthrough": True, "passthrough_default_transition": "end_callback",
        "transitions": [{"target_node": "end_callback", "description": "always", "condition": {"operator": "always"}}],
    }
    nodes["promise_done"] = {
        "name": "promise_done", "node_type": "conversation",
        "on_enter": {"type": "tts", "text": "Your promise is recorded. You will receive a text message confirming this promise. Thank you for your time. Goodbye."},
        "task_message": "The promise is recorded. Say nothing more. No tool call is allowed in this turn.",
        "passthrough": True, "passthrough_default_transition": "end_promise",
        "transitions": [{"target_node": "end_promise", "description": "always", "condition": {"operator": "always"}}],
    }
    nodes["promise_fix"] = code_node("promise_fix", "vars['promise_guard'] = 'handled'\nvars['promise_fix_text'] = str(vars.get('promise_guard_say') or 'I am sorry, I could not record that. Let me check the details again.')\n", "promise_fix_say")
    nodes["promise_fix_say"] = {
        "name": "promise_fix_say", "node_type": "conversation",
        "on_enter": {"type": "tts", "text": "{{promise_fix_text}}"},
        "task_message": "Say nothing more now. No tool call is allowed in this turn.",
        "passthrough": True, "passthrough_default_transition": "negotiate",
        "transitions": [{"target_node": "negotiate", "description": "always", "condition": {"operator": "always"}}],
    }
    nodes["identity_failed"] = {
        "name": "identity_failed", "node_type": "conversation",
        "on_enter": {"type": "tts", "text": IDENTITY_FAILED_LINE},
        "task_message": "The identity check failed. Say nothing more. No tool call is allowed in this turn.",
        "passthrough": True, "passthrough_default_transition": "end_failed",
        "transitions": [{"target_node": "end_failed", "description": "always", "condition": {"operator": "always"}}],
    }
    nodes["load_account"] = engine_call_node("load_account", "get_account", {"customer_id": "{{customer_id}}"},
                                             "account_facts", retries=2,
                                             mapping=[{"path": "data.products", "variable": "products"}])
    nodes["account_facts"] = code_node("account_facts", ACCOUNT_FACTS_CODE, "negotiate")
    nodes["negotiate"] = {
        "name": "negotiate", "node_type": "conversation",
        "on_enter": {"type": "tts", "text": "{{debt_statement}}"},
        "on_enter_first_entry_only": True,
        "task_message": (multi_variants()[0] if multi else NEGOTIATE_TASK).replace(
            "{{multi_product_rules}}", MULTI_PRODUCT_RULES if multi else ""),
        "tools": [multi_variants()[1], multi_variants()[2]] if multi else [PROMISE_TOOL],
        "max_node_turns": 40, "max_turn_transition": "end_auto",
        "extraction_schema": [
            {"name": "call_situation", "type": "string", "min_confidence": 0.6,
             "enum_values": ["none", "paid", "dispute", "hardship", "refused", "human"],
             "description": "What best describes the customer's position on paying so far: paid (says they already paid), dispute (says the debt is not theirs or the product was closed), hardship (illness, a death in the family or job loss), refused (will not pay), human (insists on a person), none (none of these yet). Once hardship applies it stays the answer, whatever else they ask for later. A request for a discount or a waiver is NOT a request for a new payment plan."},
            # the latch of the hardship precedence: call_situation is overwritten by every later message (PUB-05 v10)
            {"name": "hardship_seen", "type": "string", "min_confidence": 0.6, "enum_values": ["yes", "no"],
             "description": "yes if at ANY point in this call the customer said something that fits: illness, a death in the family or job loss. It stays yes for the rest of the call, whatever they say later (a later request for a discount or a waiver does not change it). Otherwise no."},
        ],
        "transitions": [
            {"target_node": "promise_fix", "description": "The promise guard blocked a create_promise call",
             "condition": {"logic": "or", "conditions": [
                 {"variable": "promise_guard", "operator": "contains", "operand": "blocked"}]}},
            {"target_node": "promise_done", "description": "The single product's promise is recorded",
             "condition": {"logic": "or", "conditions": [{"logic": "and", "conditions": [
                 {"variable": "create_promise_success_count", "operator": "greater_than", "operand": 0},
                 {"variable": "product_count", "operator": "equals", "operand": "1"}]}]}},
            {"target_node": "end_auto", "description": "The conversation is over",
             "condition": {"operator": "matches_intent", "description": "The agent already said goodbye and the customer now only says goodbye or thanks to end the call (for example 'Thank you, goodbye')."}},
            tool_transition("end_promise", "A promise was recorded, the text-message sentence was said and you said goodbye."),
            tool_transition("end_paid", "The customer already paid; you said payments can take one business day and goodbye."),
            tool_transition("end_dispute", "The customer disputes the debt; you said goodbye."),
            tool_transition("end_hardship", "Hardship and the customer declined the support team; you said goodbye."),
            tool_transition("end_refused", "Persuasion attempts used up or the customer refuses to talk; you said goodbye."),
            tool_transition("end_service_error", "create_promise failed twice; you said you could not record it and goodbye."),
            tool_transition("handoff_support", "Hardship and the customer accepted the support team; you said you will transfer them."),
            tool_transition("guard_restructuring", "The customer wants a new payment plan or lower instalments; you said you will transfer them."),
            tool_transition("handoff_human", "The customer insists on a human; you said you will transfer them."),
        ],
    }
    for choice in ("promise", "paid", "dispute", "hardship", "refused", "service_error", "callback", "no_callback",
                   "wrong_number", "deceased", "unclear", "declined", "failed"):
        nodes[f"end_{choice}"] = set_choice_node(f"end_{choice}", choice)
    for queue in ("support", "restructuring", "human"):
        q = "general" if queue == "human" else queue
        nodes[f"handoff_{queue}"] = engine_call_node(f"handoff_{queue}", "transfer_to_agent",
                                                     {"customer_id": "{{customer_id}}", "queue": q}, "decide")
    # Hardship precedence (audit C-15): once hardship was expressed the transfer to restructuring is blocked by the engine
    # (a variable condition on the latch), not left to the prompt. The same pattern as promise_fix / promise_fix_say.
    nodes["guard_restructuring"] = code_node("guard_restructuring", GUARD_RESTRUCTURING, "handoff_restructuring")
    nodes["guard_restructuring"]["transitions"].insert(0, {
        "target_node": "guard_restructuring_say", "description": "The customer already expressed hardship, which comes before restructuring",
        "condition": {"logic": "or", "conditions": [
            {"variable": "restructuring_blocked", "operator": "equals", "operand": "yes"}]}})
    nodes["guard_restructuring_say"] = {
        "name": "guard_restructuring_say", "node_type": "conversation",
        "on_enter": {"type": "tts", "text": "I am sorry, I cannot arrange that."},
        "task_message": "Say nothing more now. No tool call is allowed in this turn.",
        "passthrough": True, "passthrough_default_transition": "negotiate",
        "transitions": [{"target_node": "negotiate", "description": "always", "condition": {"operator": "always"}}],
    }
    nodes["end_auto"] = set_choice_node("end_auto", "auto")
    nodes["end_auto_tp"] = set_choice_node("end_auto_tp", "no_callback")
    nodes["decide"] = code_node("decide", DECIDE_CODE, "close")
    nodes["close"] = engine_call_node("close", "close_call", {"customer_id": "{{customer_id}}",
                                                               "outcome": "{{final_outcome}}",
                                                               "reason": "{{final_reason}}"}, "hangup")
    nodes["hangup"] = {
        "name": "hangup", "node_type": "end_call", "on_enter": {"type": "silent"},
        "disable_inactivity_monitor": True, "transitions": [],
        "terminal": {"type": "function", "function": {"name": "call_closed", "handler": "__function__:end_call",
                                                     "parameters": {"type": "object", "properties": {
                                                         "message_mode": {"type": "string", "default": "none"}}}}},
    }
    return {"initial_node": "setup", "system_prompt": SYSTEM_PROMPT, "nodes": nodes, "global_nodes": {}}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--validate", action="store_true")
    ap.add_argument("--deploy", action="store_true")
    ap.add_argument("--slot", default="ref-a")
    ap.add_argument("--multi", action="store_true", help="add Brief B (multi-product) rules and the add_note tool")
    a = ap.parse_args()
    wf = build(multi=a.multi)
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{a.slot}.workflow.json"
    path.write_text(json.dumps(wf, indent=1), encoding="utf-8")
    print(f"wrote {path} ({len(wf['nodes'])} nodes)")
    if a.validate or a.deploy:
        sys.path.insert(0, str(ROOT))
        import freya  # kit client, reads env at import
        if a.validate:
            print("dry-run:", json.dumps(freya.validate_workflow(a.slot, wf))[:800])
        if a.deploy:
            print(json.dumps(freya.save_workflow(a.slot, wf)))


if __name__ == "__main__":
    main()
