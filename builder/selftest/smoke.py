"""Smoke self-test: one short chat conversation with the deployed agent (about a minute, no bank data involved).

The test starts a chat on the platform with test call variables (``customer_id = c_selftest``, test names, a ``now`` that
is the next weekday at 11:00 derived from the real date) and sends two harmless caller turns. It checks, generically:

* every agent reply is non-empty (the workflow starts, speaks and keeps answering);
* no word the policy forbids before identity verification (``ir.forbidden_before_verification``) appears in the first
  two replies - the caller never verified anything;
* the opening contains the fixed elements the policy prescribes (the opening sentence and the right-party question),
  compared fuzzily as a share of words, because the platform may reword whitespace and placeholders.

The summary names the checks that really ran (``details["checks"]``): without a policy IR (agentic track) only
"every reply is non-empty" can be checked, and the summary says that the opening and the secrecy checks were not made;
an IR without opening phrases or forbidden words checks nothing of that kind and says "checked: 0".

The conversation is ended afterwards (best effort). Failures never raise: they are returned as a result dict.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any

from builder.freya.background import CancelToken, run_blocking
from builder.policy.ir import PolicyIR

#: What the simulated caller says (neutral: it confirms nothing and answers no security question).
USER_TURNS: tuple[str, ...] = ("Hello?", "Sorry, who is this and what is this about?")
#: Share of an element's words that must appear in the reply for the element to count as present.
OPENING_MATCH = 0.6
#: Test values for call variables, matched by name fragment (first match wins).
_VALUE_BY_FRAGMENT: tuple[tuple[str, str], ...] = (
    ("customer_id", "c_selftest"),
    ("first", "Alex"),
    ("last", "Morgan"),
    ("sur", "Morgan"),
    ("title", "Mr"),
    ("name", "Alex Morgan"),
)
#: Variables used when the policy IR is not known (agentic track).
DEFAULT_VARIABLES: tuple[str, ...] = ("customer_id", "first_name", "last_name", "title", "now")

_WORD = re.compile(r"[a-z0-9]+")
_PLACEHOLDER = re.compile(r"\{\{?\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}?\}")


def next_weekday_at_eleven(today: date) -> str:
    """ISO ``YYYY-MM-DDT11:00`` of the next weekday after ``today`` (Friday -> Monday)."""
    day = today + timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return f"{day.isoformat()}T11:00"


def call_variables_for_test(ir: PolicyIR | None, now_iso: str) -> dict[str, str]:
    """Call variables for the smoke chat: the IR's declared ones (else :data:`DEFAULT_VARIABLES`) with test values."""
    names = ir.call_variable_names() if ir is not None else list(DEFAULT_VARIABLES)
    values: dict[str, str] = {}
    for name in dict.fromkeys([*names, "customer_id", "now"]):
        lowered = name.lower()
        if lowered == "now":
            values[name] = now_iso
            continue
        values[name] = next((v for fragment, v in _VALUE_BY_FRAGMENT if fragment in lowered), "test")
    return values


def render_phrase(template: str, variables: dict[str, str]) -> str:
    """Fill ``{name}`` / ``{{name}}`` placeholders of a prescribed phrase with the test variables."""
    return _PLACEHOLDER.sub(lambda m: variables.get(m.group(1), m.group(0)), template or "")


def words(text: str) -> list[str]:
    """Lower-case alphanumeric words of ``text`` (apostrophes and punctuation dropped)."""
    return _WORD.findall(text.lower().replace("'", "").replace(chr(0x2019), ""))


def coverage_of(element: str, reply: str) -> float:
    """Share of the words of ``element`` that occur in ``reply`` (1.0 for an element without words)."""
    wanted = words(element)
    if not wanted:
        return 1.0
    have = set(words(reply))
    return sum(1 for w in wanted if w in have) / len(wanted)


def forbidden_found(reply: str, forbidden: list[str]) -> list[str]:
    """The forbidden words / phrases that occur (as whole words, case-insensitive) in ``reply``."""
    found = []
    for phrase in forbidden:
        if phrase.strip() and re.search(
            rf"(?<![A-Za-z0-9]){re.escape(phrase.strip())}(?![A-Za-z0-9])", reply, re.I
        ):
            found.append(phrase)
    return found


def inspect_replies(
    ir: PolicyIR | None, variables: dict[str, str], replies: list[str]
) -> tuple[list[str], list[str]]:
    """``(problems, checks)``: what is wrong with the agent's replies, and which checks really ran.

    ``checks`` only lists a check that had something to check: forbidden words are counted from the IR's list (blank
    entries skipped) and opening elements from the phrases that are not empty. Without an IR just the emptiness of the
    replies is checked.
    """
    problems: list[str] = []
    for index, reply in enumerate(replies, start=1):
        if not reply.strip():
            problems.append(f"reply {index} is empty")
    checks = ["replies non-empty"]
    if ir is None:
        return problems, checks
    forbidden = [word for word in ir.forbidden_before_verification if word.strip()]
    leaked = [w for reply in replies[:2] for w in forbidden_found(reply, forbidden)]
    if leaked:
        problems.append(f"forbidden before verification, but said: {', '.join(dict.fromkeys(leaked))}")
    checks.append(f"forbidden words checked: {len(forbidden)}")
    first = replies[0] if replies else ""
    elements = 0
    for label, template in (
        ("opening sentence", ir.phrases.opening_sentence),
        ("right-party question", ir.phrases.right_party_question),
    ):
        element = render_phrase(template, variables)
        if not element.strip():
            continue
        elements += 1
        if coverage_of(element, first) < OPENING_MATCH:
            problems.append(f"the opening lacks the {label}: {element[:80]!r}")
    checks.append(f"opening elements checked: {elements}")
    return problems, checks


def check_replies(ir: PolicyIR | None, variables: dict[str, str], replies: list[str]) -> list[str]:
    """The problems found in the agent's replies (empty list = pass)."""
    return inspect_replies(ir, variables, replies)[0]


async def run_smoke(
    freya: Any,
    workflow_id: str,
    ir: PolicyIR | None,
    trace: Any,
    now: datetime | None = None,
    *,
    cancel: CancelToken | None = None,
) -> dict[str, Any]:
    """Run the smoke conversation and return ``{"name", "passed", "summary", "details"}``.

    Args:
        freya: The Freya client (``chat`` and ``end_chat``; blocking calls run in a worker thread).
        workflow_id: The deployed workflow.
        ir: The policy IR (for call variables and checks) or ``None`` on the agentic track.
        trace: The run trace (only used for the conversation events).
        now: The real current time (injectable); the call date is derived from it.
        cancel: Token of the build; the chat calls run in abandonable threads bound to it.
    """
    now = now or datetime.now()
    variables = call_variables_for_test(ir, next_weekday_at_eleven(now.date()))
    turns: list[dict[str, str]] = []
    call_id: str | None = None
    problems: list[str] = []
    checks: list[str] = []
    try:
        for message in USER_TURNS:
            turn = await run_blocking(
                freya.chat,
                workflow_id,
                message,
                variables if call_id is None else None,
                call_id,
                token=cancel,
            )
            call_id = turn.call_id
            turns.append({"user": message, "agent": turn.response})
            if getattr(turn, "ended_by_agent", False):
                break
        problems, checks = inspect_replies(ir, variables, [t["agent"] for t in turns])
    except Exception as exc:  # a platform problem is a failed test, not a crash
        problems.append(f"{type(exc).__name__}: {exc}"[:300])
    finally:
        if call_id is not None:
            try:
                await run_blocking(freya.end_chat, call_id, token=cancel)
            except Exception as exc:  # best effort
                trace.event("test", f"could not end the smoke chat: {type(exc).__name__}")
    passed = not problems
    if passed:
        parts = [f"{len(turns)} turn(s) answered", *checks]
        if ir is None:
            parts.append(
                "not checked: opening elements and forbidden words (no policy IR; reachability only)"
            )
        summary = "; ".join(parts)
    else:
        summary = "; ".join(problems)[:300]
    return {
        "name": "smoke",
        "passed": passed,
        "summary": summary,
        "details": {
            "call_id": call_id,
            "turns": turns,
            "problems": problems,
            "checks": checks,
            "variables": sorted(variables),
        },
    }


__all__ = [
    "DEFAULT_VARIABLES",
    "OPENING_MATCH",
    "USER_TURNS",
    "call_variables_for_test",
    "check_replies",
    "coverage_of",
    "forbidden_found",
    "inspect_replies",
    "next_weekday_at_eleven",
    "render_phrase",
    "run_smoke",
]
