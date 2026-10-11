"""Smoke self-test: one short chat conversation with the deployed agent (about a minute, no bank data involved).

The test starts a chat on the platform with test call variables (``customer_id = c_selftest``, test names, a ``now`` that
is the next weekday at 11:00 derived from the real date) and sends two harmless caller turns, in the language the agent
speaks when a short built-in table has it (:func:`user_turns`; English otherwise). It checks, generically:

* every agent reply is non-empty (the workflow starts, speaks and keeps answering);
* no word the policy forbids before identity verification (``ir.forbidden_before_verification``) appears in the first
  two replies - the caller never verified anything;
* the opening contains the fixed elements the policy prescribes (the opening sentence and the right-party question),
  compared fuzzily as a share of words, because the platform may reword whitespace and placeholders.

The summary names the checks that really ran (``details["checks"]``): without a policy IR (agentic track) only
"every reply is non-empty" can be checked, and the summary says that the opening and the secrecy checks were not made;
an IR without opening phrases or forbidden words checks nothing of that kind and says "checked: 0".

The conversation is ended afterwards (best effort). Failures never raise: they are returned as a result dict.

A transient platform failure (HTTP 502/503/504, a timeout, a dropped connection - :func:`is_transient_platform_error`;
Freya staging has answered ``POST /api/v2/chat/message`` with 504 after a minute) says nothing about the agent, so the
whole short conversation is repeated once (a fresh chat; the broken one is ended) after a short pause - but only while
the build's budget still covers another smoke run plus the reserve and the cancel token's deadline is not too close.
Both attempts are traced (``smoke attempt 1 failed: HTTP 504 (platform); retrying``) and listed in
``details["attempts"]``; ``details["platform_error"]`` says whether the final failure was such a platform error. A
real reply problem (an empty reply, a wrong opening, a forbidden word) or any other error is never retried.
"""

from __future__ import annotations

import asyncio
import re
import unicodedata
from datetime import date, datetime, timedelta
from typing import Any

from builder.budget import Budget, estimate
from builder.compiler.codegen import clock_variable
from builder.compiler.language import is_english, spoken_language
from builder.freya.background import CancelToken, PlatformCancelled, run_blocking
from builder.freya.client import FreyaError
from builder.policy.ir import PolicyIR

#: What the simulated caller says (neutral: it confirms nothing and answers no security question).
USER_TURNS: tuple[str, ...] = ("Hello?", "Sorry, who is this and what is this about?")
#: The same neutral turns in other languages (ISO code -> turns); a language without an entry gets :data:`USER_TURNS`.
USER_TURNS_BY_LANGUAGE: dict[str, tuple[str, ...]] = {
    "tr": ("Alo?", "Pardon, kiminle görüşüyorum?"),
    "de": ("Hallo?", "Entschuldigung, wer ist da und worum geht es?"),
    "fr": ("Allô ?", "Pardon, qui est à l'appareil et c'est à quel sujet ?"),
    "es": ("¿Diga?", "Perdone, ¿quién es y de qué se trata?"),
}
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
#: HTTP statuses of the platform's gateway after which the conversation is repeated once.
TRANSIENT_STATUSES = frozenset({502, 503, 504})
#: Conversations at most (the first one plus one repeat after a transient platform error).
SMOKE_ATTEMPTS = 2
#: Pause before the repeat, in seconds.
SMOKE_RETRY_PAUSE_S = 5.0
#: Seconds that must stay free after a repeated smoke run (the self-test plan's reserve).
RETRY_RESERVE_S = 120.0

_WORD = re.compile(r"[^\W_]+")
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
    # AUD-13: the test time goes to the variable the date code reads; ``now`` is added only when that is the clock, so a
    # renamed clock is really exercised instead of being hidden by an extra ``now``
    clock = clock_variable(ir) if ir is not None else "now"
    values: dict[str, str] = {}
    for name in dict.fromkeys([*names, "customer_id", clock]):
        lowered = name.lower()
        if name == clock or lowered == "now":
            values[name] = now_iso
            continue
        values[name] = next((v for fragment, v in _VALUE_BY_FRAGMENT if fragment in lowered), "test")
    return values


def render_phrase(template: str, variables: dict[str, str]) -> str:
    """Fill ``{name}`` / ``{{name}}`` placeholders of a prescribed phrase with the test variables."""
    return _PLACEHOLDER.sub(lambda m: variables.get(m.group(1), m.group(0)), template or "")


def _fold(text: str) -> str:
    """Lower case without accents (``İyi`` and ``iyi``, ``Görüşme`` and ``gorusme`` compare equal); ASCII is unchanged."""
    lowered = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in lowered if not unicodedata.combining(c))


def words(text: str) -> list[str]:
    """Lower-case alphanumeric words of ``text`` in any script (apostrophes and punctuation dropped, accents folded)."""
    return _WORD.findall(_fold(text.replace("'", "").replace(chr(0x2019), "")))


def user_turns(ir: PolicyIR | None) -> tuple[str, ...]:
    """The caller's turns: in the agent's language when :data:`USER_TURNS_BY_LANGUAGE` has it, else English."""
    if ir is None:
        return USER_TURNS
    code = spoken_language(ir)
    if is_english(code):
        return USER_TURNS
    return USER_TURNS_BY_LANGUAGE.get(code[:2], USER_TURNS)


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


def is_transient_platform_error(exc: BaseException) -> bool:
    """True for a failure of the platform, not of the agent: HTTP 502/503/504, a network error or a timeout."""
    if isinstance(exc, PlatformCancelled):
        return False
    if isinstance(exc, FreyaError):
        if exc.kind == "transport":
            return True
        return exc.kind == "http" and exc.status in TRANSIENT_STATUSES
    return isinstance(exc, TimeoutError | ConnectionError)


def describe_error(exc: BaseException) -> str:
    """A short name of a failure for the trace (``HTTP 504``, ``network error or timeout`` ...)."""
    if isinstance(exc, FreyaError):
        if exc.kind == "http" and exc.status is not None:
            return f"HTTP {exc.status}"
        if exc.kind == "transport":
            return "network error or timeout"
    if isinstance(exc, TimeoutError):
        return "timeout"
    return type(exc).__name__


def retry_blocker(budget: Budget | None, cancel: CancelToken | None, pause_s: float) -> str | None:
    """Why another smoke run must not start (``None`` when it fits before the budget's reserve and the deadline)."""
    if cancel is not None and cancel.cancelled:
        return f"the build was cancelled ({cancel.reason})"
    needed = estimate("smoke") + max(0.0, pause_s)
    if budget is not None and not budget.allows(needed, RETRY_RESERVE_S):
        return f"not enough time left ({budget.remaining():.0f} s)"
    left = cancel.remaining() if cancel is not None else None
    if left is not None and left < needed:
        return f"the build deadline is too close ({left:.0f} s)"
    return None


async def _converse(
    freya: Any,
    workflow_id: str,
    ir: PolicyIR | None,
    variables: dict[str, str],
    trace: Any,
    cancel: CancelToken | None,
) -> tuple[list[dict[str, str]], str | None, list[str], list[str], Exception | None]:
    """One smoke conversation: ``(turns, call_id, problems, checks, error)``; the chat is ended afterwards."""
    turns: list[dict[str, str]] = []
    call_id: str | None = None
    problems: list[str] = []
    checks: list[str] = []
    error: Exception | None = None
    try:
        for message in user_turns(ir):
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
        error = exc
        text = f"{type(exc).__name__}: {exc}"
        if is_transient_platform_error(exc):
            text = f"platform error ({describe_error(exc)}): {text}"
        # the replies already answered are still checked: a wrong opening before a 504 is a real reply problem
        problems = inspect_replies(ir, variables, [t["agent"] for t in turns])[0] if turns else []
        problems.append(text[:300])
    finally:
        if call_id is not None:
            try:
                await run_blocking(freya.end_chat, call_id, token=cancel)
            except Exception as exc:  # best effort
                trace.event("test", f"could not end the smoke chat: {type(exc).__name__}")
    return turns, call_id, problems, checks, error


async def run_smoke(
    freya: Any,
    workflow_id: str,
    ir: PolicyIR | None,
    trace: Any,
    now: datetime | None = None,
    *,
    cancel: CancelToken | None = None,
    budget: Budget | None = None,
    retry_pause_s: float | None = None,
) -> dict[str, Any]:
    """Run the smoke conversation and return ``{"name", "passed", "summary", "details"}``.

    Args:
        freya: The Freya client (``chat`` and ``end_chat``; blocking calls run in a worker thread).
        workflow_id: The deployed workflow.
        ir: The policy IR (for call variables and checks) or ``None`` on the agentic track.
        trace: The run trace (only used for the conversation events).
        now: The real current time (injectable); the call date is derived from it.
        cancel: Token of the build; the chat calls run in abandonable threads bound to it, and no repeat starts once
            it is cancelled or its deadline is too close.
        budget: The build's budget; a repeat after a transient platform error starts only while it still covers
            another smoke run plus :data:`RETRY_RESERVE_S`.
        retry_pause_s: Pause before the repeat (default :data:`SMOKE_RETRY_PAUSE_S`; tests pass 0).
    """
    now = now or datetime.now()
    pause_s = SMOKE_RETRY_PAUSE_S if retry_pause_s is None else max(0.0, float(retry_pause_s))
    variables = call_variables_for_test(ir, next_weekday_at_eleven(now.date()))
    attempts: list[dict[str, Any]] = []
    earlier: list[str] = []  # "attempt 1 failed: HTTP 504 (platform)" for every repeated attempt
    not_retried = ""
    while True:
        number = len(attempts) + 1
        turns, call_id, problems, checks, error = await _converse(
            freya, workflow_id, ir, variables, trace, cancel
        )
        # a reply problem found in the turns answered before the error is never retried (len(problems) > 1: the
        # platform error is always the last problem)
        transient = error is not None and is_transient_platform_error(error) and len(problems) == 1
        described = describe_error(error) if error is not None else None
        attempts.append({"attempt": number, "call_id": call_id, "error": described, "transient": transient})
        if not transient:
            break
        failed = f"smoke attempt {number} failed: {described} (platform)"
        if number >= SMOKE_ATTEMPTS:
            trace.event("test", f"{failed}; giving up after {number} attempts", attempt=number)
            break
        blocker = retry_blocker(budget, cancel, pause_s)
        if blocker:
            not_retried = f"not retried: {blocker}"
            trace.event("test", f"{failed}; {not_retried}", attempt=number)
            break
        trace.event("test", f"{failed}; retrying", attempt=number, pause_s=pause_s)
        earlier.append(f"attempt {number} failed: {described} (platform)")
        if pause_s > 0:
            await asyncio.sleep(pause_s)
    passed = not problems
    if passed:
        parts = [f"{len(turns)} turn(s) answered", *checks]
        if ir is None:
            parts.append(
                "not checked: opening elements and forbidden words (no policy IR; reachability only)"
            )
        if earlier:
            parts.append(f"passed on attempt {len(attempts)} ({'; '.join(earlier)})")
        summary = "; ".join(parts)
    else:
        last = "; ".join(problems)
        if earlier:
            last = f"attempt {len(attempts)}: {last}"
        summary = "; ".join([*earlier, last, *([not_retried] if not_retried else [])])[:300]
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
            "attempts": attempts,
            "platform_error": bool(attempts[-1]["transient"]),
        },
    }


__all__ = [
    "DEFAULT_VARIABLES",
    "OPENING_MATCH",
    "SMOKE_ATTEMPTS",
    "SMOKE_RETRY_PAUSE_S",
    "TRANSIENT_STATUSES",
    "USER_TURNS",
    "USER_TURNS_BY_LANGUAGE",
    "call_variables_for_test",
    "check_replies",
    "coverage_of",
    "describe_error",
    "forbidden_found",
    "inspect_replies",
    "is_transient_platform_error",
    "next_weekday_at_eleven",
    "render_phrase",
    "retry_blocker",
    "run_smoke",
    "user_turns",
]
