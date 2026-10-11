"""Output guards: what the build *derives* (ledger, policy IR, drafted prose, the workflow) is checked before it moves on.

Each derived artifact is written by a model or by the compiler from a model's output, so each gets an independent,
deterministic check against the sources it must come from. A guard never edits; it returns a
:class:`~builder.guardrails.models.GuardReport` and the graph decides (repair within budget, else ``blocked``).

=====================  ===================================================================================================
Function               What it proves
=====================  ===================================================================================================
:func:`check_ledger`   every rule quote is in the briefs (no invented rule), ids are unique, the ledger is not empty
:func:`check_policy`   every date, amount and number of the IR appears in the briefs or the tool API; every enum value
                       is one the tool API allows; no injection marker or key-like token in any IR text
:func:`check_prose`    drafted prose adds no amount or date the IR lacks and mentions no tool the API lacks
:func:`check_workflow` the agent calls only the bank's tool API host, carries no secret, speaks no forbidden word before
                       verification, leaves ``agent_config`` to the platform
=====================  ===================================================================================================

Literal matching ("does this number appear in the source?") is done after *normalisation*: thousands separators and
decimal commas are read both ways, number words count (``three`` = 3, ``twice`` = 2, ``two weeks`` = 14 days), a date is
the same date in any common spelling (``2031-03-17``, ``17 March 2031``, ``Mar 17``, ``17/03/2031``), and a date without a
year in the source matches any year.

Three deliberate softenings keep a correct IR from being blocked: structural numbers (``0``, ``1``, priorities, ordinals)
are not checked, a *small* ungrounded count (below 10) is a warning, not a block (spoken counts such as "ask once more
then give up" are often not written as a number), and the end of the *top* lateness band is an open bound ("31 days or
more" has no number to copy, so a model writes 60, 99, 365 or 999): it never blocks. Amounts, dates and larger numbers
always block.

This module imports nothing heavy at import time (the grounding helpers are imported inside :func:`check_ledger`), so
the LLM gateway can import the guardrails package without a cycle.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Iterator, Mapping
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import urlsplit

from builder.guardrails.models import GuardReport
from builder.guardrails.patterns import find_injections, find_key_tokens

if TYPE_CHECKING:
    from builder.ingest.brief_loader import BriefBundle
    from builder.ingest.tool_api import FieldSpec, ToolApi
    from builder.policy.ir import PolicyIR, RuleLedger

__all__ = [
    "PLATFORM_FUNCTIONS",
    "PROMPT_WARN_NODE_CHARS",
    "PROMPT_WARN_TOTAL_CHARS",
    "PROSE_WARN_CHARS",
    "VERIFY_TOOL",
    "check_ledger",
    "check_policy",
    "check_prose",
    "check_workflow",
]

PROSE_WARN_CHARS = 6000
"""Prose per node above this many characters warns (the compiler caps it far lower; this catches uncapped drafts)."""
PROMPT_WARN_TOTAL_CHARS = 60_000
"""All prompt text of a workflow above this warns (the agent pays for the prompt on every turn)."""
PROMPT_WARN_NODE_CHARS = 20_000
"""System prompt plus one node's own prompt text above this warns."""
VERIFY_TOOL = "verify_identity"
"""The tool whose node marks the end of the 'before verification' part of the call."""
PLATFORM_FUNCTIONS = frozenset({"end_call", "code_exec", "api_call"})
"""Built-in platform functions: not bank tools, but legitimate to mention."""

_MAX_FINDINGS = 25


# ------------------------------------------------------------------------------------------------------------------------
# Literal extraction and the source corpus
# ------------------------------------------------------------------------------------------------------------------------

_MONTHS: dict[str, int] = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3, "april": 4, "apr": 4, "may": 5,
    "june": 6, "jun": 6, "july": 7, "jul": 7, "august": 8, "aug": 8, "september": 9, "sept": 9, "sep": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}  # fmt: skip
_MONTH = "(" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + ")"
_ORD = r"(?:st|nd|rd|th)?"
_DASH = "[-\u2013\u2014]"
_ISO_DATE = re.compile(r"(?<!\d)(\d{4})-(\d{1,2})-(\d{1,2})(?!\d)")
_DATE_RANGE = re.compile(
    rf"(?<!\d)(\d{{1,2}}){_ORD}\s*(?:{_DASH}|to|and|&)\s*(\d{{1,2}}){_ORD}\s+(?:of\s+)?{_MONTH}\b\.?(?:,?\s*(\d{{4}}))?",
    re.IGNORECASE,
)
_DATE_DMY = re.compile(
    rf"(?<!\d)(\d{{1,2}}){_ORD}\s+(?:of\s+)?{_MONTH}\b\.?(?:,?\s*(\d{{4}}))?", re.IGNORECASE
)
_DATE_MDY = re.compile(rf"\b{_MONTH}\b\.?\s+(\d{{1,2}}){_ORD}\b(?!\s*:)(?:,?\s*(\d{{4}}))?", re.IGNORECASE)
_DATE_NUMERIC = re.compile(r"(?<![\d.,/])(\d{1,2})[/.](\d{1,2})[/.](\d{4}|\d{2})(?![\d])")

_NUMBER_TOKEN = re.compile(r"\d[\d.,]*\d|\d")
_CURRENCY_BEFORE = re.compile(r"[$\u20ac\u00a3\u20ba\u00a5]\s?$")
_CURRENCY_AFTER = re.compile(
    r"\s?(?:%|percent\b|usd\b|eur\b|gbp\b|try\b|tl\b|dollars?\b|euros?\b|pounds?\b|lira\b|cents?\b|pence\b)",
    re.IGNORECASE,
)

_SMALL_WORDS: dict[str, int] = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
    "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}  # fmt: skip
_OTHER_WORDS: dict[str, int] = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8,
    "ninth": 9, "tenth": 10, "eleventh": 11, "twelfth": 12, "twentieth": 20, "thirtieth": 30,
    "once": 1, "twice": 2, "thrice": 3, "single": 1, "double": 2, "both": 2, "pair": 2, "fortnight": 14,
}  # fmt: skip
_WEEKS = re.compile(r"\b(\d+|" + "|".join(_SMALL_WORDS) + r")[\s-]+weeks?\b", re.IGNORECASE)


def _valid_date(month: int, day: int) -> bool:
    return 1 <= month <= 12 and 1 <= day <= 31


def _extract_dates(text: str) -> tuple[set[tuple[int | None, int, int]], str]:
    """Date literals of ``text`` as ``(year or None, month, day)`` and ``text`` with those spans blanked out."""
    found: set[tuple[int | None, int, int]] = set()
    work = text or ""

    def blank(match: re.Match[str]) -> str:
        return " " * len(match.group(0))

    def iso(match: re.Match[str]) -> str:
        year, month, day = (int(match.group(i)) for i in (1, 2, 3))
        if _valid_date(month, day):
            found.add((year, month, day))
        return blank(match)

    def date_range(match: re.Match[str]) -> str:
        if match.group(3) == "may":  # lower case "may" is the verb ("Attempt 2 may name ...")
            return match.group(0)
        first, last = int(match.group(1)), int(match.group(2))
        month = _MONTHS[match.group(3).lower()]
        year = int(match.group(4)) if match.group(4) else None
        if first <= last <= 31:
            found.update((year, month, day) for day in range(first, last + 1) if _valid_date(month, day))
        return blank(match)

    def dmy(match: re.Match[str]) -> str:
        if match.group(2) == "may":
            return match.group(0)
        day, month = int(match.group(1)), _MONTHS[match.group(2).lower()]
        if _valid_date(month, day):
            found.add((int(match.group(3)) if match.group(3) else None, month, day))
        return blank(match)

    def mdy(match: re.Match[str]) -> str:
        if match.group(1) == "may":
            return match.group(0)
        month, day = _MONTHS[match.group(1).lower()], int(match.group(2))
        if _valid_date(month, day):
            found.add((int(match.group(3)) if match.group(3) else None, month, day))
        return blank(match)

    def numeric(match: re.Match[str]) -> str:
        first, second, raw_year = int(match.group(1)), int(match.group(2)), match.group(3)
        year = int(raw_year) if len(raw_year) == 4 else 2000 + int(raw_year)
        for day, month in ((first, second), (second, first)):  # day-first and month-first are both possible
            if _valid_date(month, day):
                found.add((year, month, day))
        return blank(match)

    for pattern, handler in (
        (_ISO_DATE, iso),
        (_DATE_RANGE, date_range),
        (_DATE_DMY, dmy),
        (_DATE_MDY, mdy),
        (_DATE_NUMERIC, numeric),
    ):
        work = pattern.sub(handler, work)
    return found, work


# --- spellings read on the SOURCE side only (audit AUD-14) ------------------------------------------------------------
# Raw bank material writes dates in many ways. A correct IR date that the corpus cannot read was blocked as "invented"
# (an ISO holiday list against "Christmas Day" or "Wed 25 and Thu 26 November"), and the only repair that cleared the
# block deleted the true holiday. These readings widen what the documents are understood to state; the IR side is read
# with :func:`_extract_dates` alone, so they can remove false blocks but never add one.

_WEEKDAY = r"(?:mon|tues?|wed(?:nes)?|thu(?:rs?)?|fri|sat(?:ur)?|sun)(?:day)?\.?"
_LIST_SEP = r"\s*(?:,|and|&|or)\s*"
_DATE_DASH_NUMERIC = re.compile(r"(?<![\d./-])(\d{1,2})-(\d{1,2})-(\d{4})(?![\d-])")
_DATE_YEAR_FIRST = re.compile(r"(?<![\d./-])(\d{4})[/.](\d{1,2})[/.](\d{1,2})(?![\d])")
_DATE_DAY_DASH_MONTH = re.compile(rf"(?<![\d-])(\d{{1,2}})-{_MONTH}\b(?:-(\d{{4}}))?", re.IGNORECASE)
_DATE_MONTH_DASH_DAY = re.compile(rf"\b{_MONTH}-(\d{{1,2}})(?![\d])(?:-(\d{{4}}))?", re.IGNORECASE)
_DATE_DAY_LIST = re.compile(
    rf"((?:{_WEEKDAY}\s+)?\d{{1,2}}{_ORD}(?:{_LIST_SEP}(?:{_WEEKDAY}\s+)?\d{{1,2}}{_ORD})+)\s+(?:of\s+)?{_MONTH}\b"
    rf"\.?(?:,?\s*(\d{{4}}))?",
    re.IGNORECASE,
)
_DATE_WEEKDAY_DAY = re.compile(rf"\b{_WEEKDAY}\s+(\d{{1,2}}){_ORD}\s+(?:of\s+)?{_MONTH}\b\.?(?:,?\s*(\d{{4}}))?", re.IGNORECASE)
_DATE_MONTH_LIST = re.compile(
    rf"\b{_MONTH}\.?\s+(\d{{1,2}}{_ORD}(?:{_LIST_SEP}\d{{1,2}}{_ORD})+)\b(?:,?\s*(\d{{4}}))?", re.IGNORECASE
)
_UNIT_ORDINALS = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9,
}  # fmt: skip
_ORDINAL_DAYS: dict[str, int] = {
    **_UNIT_ORDINALS,
    "tenth": 10, "eleventh": 11, "twelfth": 12, "thirteenth": 13, "fourteenth": 14, "fifteenth": 15,
    "sixteenth": 16, "seventeenth": 17, "eighteenth": 18, "nineteenth": 19, "twentieth": 20, "thirtieth": 30,
    **{f"twenty-{word}": 20 + value for word, value in _UNIT_ORDINALS.items()},
    **{f"twenty {word}": 20 + value for word, value in _UNIT_ORDINALS.items()},
    "thirty-first": 31, "thirty first": 31,
}  # fmt: skip
_ORDINAL_WORD = "(" + "|".join(re.escape(word) for word in sorted(_ORDINAL_DAYS, key=len, reverse=True)) + ")"
_DATE_ORDINAL_OF = re.compile(rf"\b{_ORDINAL_WORD}\s+(?:day\s+)?of\s+{_MONTH}\b", re.IGNORECASE)
_DATE_MONTH_ORDINAL = re.compile(rf"\b{_MONTH}\s+(?:the\s+)?{_ORDINAL_WORD}\b", re.IGNORECASE)
#: Holidays on the same day every year, by their common English names (generic calendar vocabulary).
_FIXED_HOLIDAYS = (
    (re.compile(r"\bchristmas\s+eve\b", re.IGNORECASE), 12, 24),
    (re.compile(r"\bchristmas(?!\s+eve)\b(?:\s+day\b)?", re.IGNORECASE), 12, 25),
    (re.compile(r"\bboxing\s+day\b", re.IGNORECASE), 12, 26),
    (re.compile(r"\bnew\s+year['\u2019]?s?\s+eve\b", re.IGNORECASE), 12, 31),
    (re.compile(r"\bnew\s+year['\u2019]?s?\s+day\b", re.IGNORECASE), 1, 1),
)
#: ``1 500`` / ``1 500,00`` (space, no-break space or narrow no-break space as the thousands separator).
_SPACED_THOUSANDS = re.compile(r"(?<![\d.,])\d{1,3}(?:[ \u00a0\u202f]\d{3})+(?:[.,]\d+)?(?![\d])")


def _is_verb_may(month: str) -> bool:
    return month == "may"  # lower case "may" is the verb ("Attempt 2 may name ...")


def _source_dates(text: str) -> set[tuple[int | None, int, int]]:
    """The dates a SOURCE text states: :func:`_extract_dates` plus the spellings above (never used on the IR side)."""
    found, _ = _extract_dates(text)
    text = text or ""

    def add(year: int | None, month: int, day: int) -> None:
        if _valid_date(month, day):
            found.add((year, month, day))

    for match in _DATE_DASH_NUMERIC.finditer(text):
        first, second, year = int(match.group(1)), int(match.group(2)), int(match.group(3))
        add(year, second, first)  # day-first
        add(year, first, second)  # month-first
    for match in _DATE_YEAR_FIRST.finditer(text):
        add(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    for match in _DATE_DAY_DASH_MONTH.finditer(text):
        if not _is_verb_may(match.group(2)):
            add(int(match.group(3)) if match.group(3) else None, _MONTHS[match.group(2).lower()], int(match.group(1)))
    for match in _DATE_MONTH_DASH_DAY.finditer(text):
        if not _is_verb_may(match.group(1)):
            add(int(match.group(3)) if match.group(3) else None, _MONTHS[match.group(1).lower()], int(match.group(2)))
    for match in _DATE_DAY_LIST.finditer(text):
        if _is_verb_may(match.group(2)):
            continue
        month = _MONTHS[match.group(2).lower()]
        year = int(match.group(3)) if match.group(3) else None
        for day in re.findall(r"(?<![A-Za-z])\d{1,2}", match.group(1)):
            add(year, month, int(day))
    for match in _DATE_WEEKDAY_DAY.finditer(text):
        if not _is_verb_may(match.group(2)):
            add(int(match.group(3)) if match.group(3) else None, _MONTHS[match.group(2).lower()], int(match.group(1)))
    for match in _DATE_MONTH_LIST.finditer(text):
        if _is_verb_may(match.group(1)):
            continue
        month = _MONTHS[match.group(1).lower()]
        year = int(match.group(3)) if match.group(3) else None
        for day in re.findall(r"\d{1,2}", match.group(2)):
            add(year, month, int(day))
    for match in _DATE_ORDINAL_OF.finditer(text):
        if not _is_verb_may(match.group(2)):
            add(None, _MONTHS[match.group(2).lower()], _ORDINAL_DAYS[" ".join(match.group(1).lower().split())])
    for match in _DATE_MONTH_ORDINAL.finditer(text):
        if not _is_verb_may(match.group(1)):
            add(None, _MONTHS[match.group(1).lower()], _ORDINAL_DAYS[" ".join(match.group(2).lower().split())])
    for pattern, month, day in _FIXED_HOLIDAYS:
        if pattern.search(text):
            add(None, month, day)
    return found


def _decimals(token: str) -> set[Decimal]:
    """Every reading of a numeric token: ``1,000.50`` and ``1.000,50`` are 1000.50; ``1.005`` is 1.005 and 1005."""
    out: set[Decimal] = set()
    for candidate in (token.replace(",", ""), token.replace(".", "").replace(",", ".")):
        try:
            out.add(Decimal(candidate))
        except InvalidOperation:
            continue
    return out


def _number_words(text: str) -> set[Decimal]:
    """Numbers written as words in ``text`` (``three``, ``twenty-one``, ``one hundred and twenty``, ``twice``)."""
    words = re.findall(r"[a-z]+", (text or "").lower())
    out: set[Decimal] = set()
    index = 0
    while index < len(words):
        word = words[index]
        if word in _OTHER_WORDS:
            out.add(Decimal(_OTHER_WORDS[word]))
        if word not in _SMALL_WORDS:
            index += 1
            continue
        total = current = 0
        position = index
        while position < len(words):
            piece = words[position]
            if piece in _SMALL_WORDS:
                current += _SMALL_WORDS[piece]
                out.add(Decimal(_SMALL_WORDS[piece]))
            elif piece == "hundred":
                current = (current or 1) * 100
            elif piece == "thousand":
                total += (current or 1) * 1000
                current = 0
            elif piece == "and" and position + 1 < len(words) and words[position + 1] in _SMALL_WORDS:
                pass
            else:
                break
            position += 1
        out.add(Decimal(total + current))
        index = max(position, index + 1)
    return out


def _week_numbers(text: str) -> set[Decimal]:
    """``two weeks`` / ``2-week`` = 14 days: the day counts a brief expresses in weeks."""
    out: set[Decimal] = set()
    for match in _WEEKS.finditer(text or ""):
        raw = match.group(1).lower()
        count = int(raw) if raw.isdigit() else _SMALL_WORDS[raw]
        out.add(Decimal(count * 7))
    return out


_LIST_MARKER = re.compile(r"(?m)^[ \t]*(?:#{1,6}[ \t]+)?(?:[-*+][ \t]+)?\(?\d{1,3}[.)][ \t]+")
_REFERENCE = re.compile(
    r"(?i)\b(?:rules?|sections?|steps?|items?|points?|clauses?|articles?|paragraphs?)[ \t]*\d{1,3}"
    r"(?:[ \t]*(?:,|and|&|-|to)[ \t]*\d{1,3})*|#\d{1,3}\b"
)
_RULE_ID = re.compile(r"\b[A-Za-z]{1,6}-\d{1,3}[a-z]?\b")


def _without_numbering(text: str) -> str:
    """``text`` minus list numbering (``12. Ask ...``), rule references (``rule 23``) and ids (``A-26b``).

    Those numbers label the document; they are not values. Without this, a brief with 47 numbered rules would
    "state" every number up to 47.
    """
    return _RULE_ID.sub(" ", _REFERENCE.sub(" ", _LIST_MARKER.sub("", text or "")))


class _Corpus:
    """The numbers and dates a source text states, in normalised form."""

    def __init__(self, texts: Iterable[str]) -> None:
        self.numbers: set[Decimal] = set()
        self.dates: set[tuple[int | None, int, int]] = set()
        for text in texts:
            self.add(text)

    def add(self, text: str) -> None:
        self.dates |= _source_dates(text)
        values = _without_numbering(text)
        for token in _NUMBER_TOKEN.findall(values):
            self.numbers |= _decimals(token)
        for token in _SPACED_THOUSANDS.findall(values):
            self.numbers |= _decimals(re.sub(r"[ \u00a0\u202f]", "", token))
        self.numbers |= _number_words(values)
        self.numbers |= _week_numbers(values)

    def has_date(self, year: int | None, month: int, day: int) -> bool:
        return any(
            (m, d) == (month, day) and (y is None or year is None or y == year) for y, m, d in self.dates
        )

    def has_number(self, token_or_value: str | Decimal, *, neighbours: bool = False) -> bool:
        values = _decimals(token_or_value) if isinstance(token_or_value, str) else {token_or_value}
        if values & self.numbers:
            return True
        return neighbours and any(
            v + step in self.numbers for v in values for step in (Decimal(1), Decimal(-1))
        )


def _amount_literals(text: str) -> list[str]:
    """Number tokens of ``text`` (dates already removed) that read as money, percentages or large numbers.

    A token counts when it has a currency symbol or word next to it, a ``%``, a thousands separator, a decimal part,
    or at least three digits. Small bare integers are counts, covered by the numeric-field check instead.
    """
    out: list[str] = []
    for match in _NUMBER_TOKEN.finditer(text or ""):
        token = match.group(0)
        before = text[max(0, match.start() - 2) : match.start()]
        after = text[match.end() : match.end() + 14]
        digits = sum(ch.isdigit() for ch in token)
        if (
            _CURRENCY_BEFORE.search(before)
            or _CURRENCY_AFTER.match(after)
            or digits >= 3
            or re.search(r"[.,]\d{1,2}$", token)
        ):
            out.append(token)
    return out


def _grounding_problems(text: str, corpus: _Corpus) -> list[tuple[str, str]]:
    """``(kind, literal)`` for every date or amount in ``text`` that the corpus does not state (kind: date/amount)."""
    dates, remainder = _extract_dates(text)
    problems: list[tuple[str, str]] = []
    for year, month, day in sorted(dates, key=lambda d: (d[0] or 0, d[1], d[2])):
        if not corpus.has_date(year, month, day):
            shown = f"{year:04d}-{month:02d}-{day:02d}" if year else f"{month:02d}-{day:02d}"
            problems.append(("date", shown))
    for token in _amount_literals(remainder):
        if not corpus.has_number(token):
            problems.append(("amount", token))
    return problems


# ------------------------------------------------------------------------------------------------------------------------
# Generic walkers
# ------------------------------------------------------------------------------------------------------------------------

#: IR keys whose content is illustrative or an identifier, not a rule: skipped when grounding literals.
_ILLUSTRATIVE_KEYS = frozenset(
    {"rule_ids", "rule_id", "source", "example", "examples", "confirm_examples", "not_confirm_examples"}
)
#: Numeric IR fields that are structural (ordinals, priorities, weekday numbers), never checked against the sources.
_STRUCTURAL_NUMERIC_KEYS = frozenset(
    {"business_weekdays", "attempt", "precedence", "max_products", "min_occurrences"}
)
#: Numeric IR fields that may be one off the number a source states ("more than 7 days late" -> a band starts at 8).
_NEIGHBOUR_NUMERIC_KEYS = frozenset({"days_late_min", "days_late_max"})
_OPEN_ENDED_DAYS = 99
"""An ungrounded end of the top band at or above this is the conventional "and above" and passes silently; a smaller
ungrounded one only warns (it is probably a real limit the brief does not state)."""


def _walk_scalars(
    obj: Any, skip: frozenset[str], path: str = "", key: str = ""
) -> Iterator[tuple[str, str, Any]]:
    """Yield ``(path, enclosing dict key, scalar)`` for every scalar of a JSON-like structure."""
    if isinstance(obj, Mapping):
        for name, value in obj.items():
            if name in skip:
                continue
            yield from _walk_scalars(value, skip, f"{path}.{name}" if path else str(name), str(name))
    elif isinstance(obj, list | tuple):
        for index, value in enumerate(obj):
            yield from _walk_scalars(value, skip, f"{path}[{index}]", key)
    else:
        yield path, key, obj


def _secret_values(secrets: Iterable[Any] | None) -> list[str]:
    """Plain strings of secret values (``str`` or pydantic ``SecretStr``); short values are ignored (too ambiguous)."""
    values: list[str] = []
    for secret in secrets or ():
        if secret is None:
            continue
        raw = secret.get_secret_value() if hasattr(secret, "get_secret_value") else str(secret)
        if len(raw) >= 8:
            values.append(raw)
    return values


def _truncate(text: str, width: int = 80) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= width else text[: width - 3] + "..."


#: Markers that address a model by their form (role tags, chat-template tokens, ``BEGIN SYSTEM``, "reveal the system
#: prompt"): they block in a derived artifact even when an input document contains them. The others ("disregard the
#: previous instructions on X") are ordinary memo English when a document says them.
_STRUCTURAL_MARKERS = frozenset({"role_tag", "chat_template_token", "begin_block", "reveal_prompt"})


def document_markers(bundle: BriefBundle) -> frozenset[str]:
    """The wording markers (not the structural ones) that the input documents themselves contain (audit AUD-14).

    A derived text (rule statement, policy IR) that repeats such a marker restates the bank's own wording - an amending
    memo saying "please disregard the previous instructions on callback hours" - instead of adding text addressed to
    the model, so it warns instead of ending the build blocked.
    """
    try:
        found = {hit.name for hit in find_injections(bundle.combined_text(), strong_only=True)}
    except Exception:  # a guard never raises; without the set every marker keeps blocking
        return frozenset()
    return frozenset(found - _STRUCTURAL_MARKERS)


def _scan_text_safety(
    text: str,
    location: str,
    report: GuardReport,
    *,
    label: str,
    warn_only: bool = False,
    source_text: str = "",
    documented: frozenset[str] = frozenset(),
) -> None:
    """Injection markers and key-like tokens (always block) in ``text``.

    A strong marker that talks to the model (``ignore your instructions``, role tags ...) blocks, or warns when
    ``warn_only``. A marker worded like an amending memo ("disregard the earlier brief on X", see
    :class:`~builder.guardrails.patterns.InjectionPattern`) only warns: a rule statement that keeps the memo's verb is
    bank policy, and a block would end the build. A marker the ``source_text`` (the rule's own verbatim quote) contains
    too is the document's wording restated, not an addition of the model, and also only warns; so does a wording
    marker in ``documented`` (:func:`document_markers`: the input documents say it themselves).
    """
    quoted = {hit.name for hit in find_injections(source_text, strong_only=True)} if source_text else set()
    for hit in find_injections(text, strong_only=True)[:3]:
        message = f"{label} contains text addressed to a model ({hit.name}): {hit.excerpt!r}."
        if warn_only or not hit.blocks_derived or hit.name in quoted or hit.name in documented:
            report.warn("injection", message, location)
        else:
            report.block("injection", message, location)
    for key in find_key_tokens(text)[:3]:
        report.block("secret", f"{label} contains a {key.kind} ({key.preview}).", location)


# ------------------------------------------------------------------------------------------------------------------------
# check_ledger
# ------------------------------------------------------------------------------------------------------------------------


def check_ledger(ledger: RuleLedger, bundle: BriefBundle) -> GuardReport:
    """The rule ledger against the briefs.

    Blocks: no rules, duplicate ids, a quote that is empty or found nowhere in the briefs (an invented rule), a
    marker addressed to the model or a key-like token in a rule statement (amending-memo wording such as "disregard the
    earlier brief" only warns, and so does a marker the rule's own quote contains). Warns: a quote that matches only approximately (fuzzy), or
    sits in another file than its ``source``, and supersession links to unknown ids.
    """
    from builder.understand.grounding import ground_ledger  # lazy: keeps this package import-cycle free

    report = GuardReport(stage="ledger")
    if not ledger.rules:
        report.block("ledger_empty", "The rule ledger has no rules.", "ledger")
        return report
    for rule_id in ledger.duplicate_ids():
        report.block("ledger_ids", f"Rule id {rule_id!r} is used more than once.", f"ledger.{rule_id}")
    by_id = {rule.id: rule for rule in ledger.rules}
    checks = ground_ledger(ledger, bundle).checks
    missing = [check.rule_id for check in checks if check.status == "missing"]
    unreliable = len(missing) > _unreliable_share() * len(ledger.rules)
    if unreliable:
        # The ledger keeps the quotes as written in this case (grounding is unreliable for the input); blocking each
        # of them ended the build with no repair. The IR is extracted from the full documents and check_policy still
        # grounds every literal it states.
        report.warn(
            "grounding_unreliable",
            f"{len(missing)} of {len(ledger.rules)} quotes are not found verbatim in the briefs; quote grounding is "
            f"unreliable for this input (e.g. {', '.join(missing[:5])}).",
            "ledger",
        )
    documented = document_markers(bundle)
    for check in checks:
        rule = by_id.get(check.rule_id)
        quote = _truncate(rule.quote if rule else "")
        if check.status == "empty":
            report.block("ungrounded", "The rule has an empty quote.", f"ledger.{check.rule_id}")
        elif check.status == "missing" and unreliable:
            continue
        elif check.status == "missing":
            report.block(
                "ungrounded",
                f"The quote is not in the briefs (invented rule?): {quote!r}.",
                f"ledger.{check.rule_id}",
            )
        elif check.status == "fuzzy":
            report.warn(
                "approximate_quote",
                f"The quote matches the briefs only approximately (ratio {check.ratio:.2f}).",
                f"ledger.{check.rule_id}",
            )
        elif check.status == "moved":
            report.warn(
                "wrong_source",
                f"The quote is in {check.found_in}, not in {rule.source if rule else '?'}.",
                f"ledger.{check.rule_id}",
            )
    for rule in ledger.rules:
        _scan_text_safety(
            rule.statement,
            f"ledger.{rule.id}.statement",
            report,
            label="The rule statement",
            source_text=rule.quote,
            documented=documented,
        )
        _scan_text_safety(
            rule.quote, f"ledger.{rule.id}.quote", report, label="The rule quote", warn_only=True
        )
        for other in (*rule.supersedes, *([rule.superseded_by] if rule.superseded_by else [])):
            if other not in by_id:
                report.warn(
                    "ledger_ids", f"Supersession points at unknown rule {other!r}.", f"ledger.{rule.id}"
                )
    return report


# ------------------------------------------------------------------------------------------------------------------------
# check_policy
# ------------------------------------------------------------------------------------------------------------------------


def _unreliable_share() -> float:
    """The share of unfound quotes above which the ledger keeps them as written (one value for both places)."""
    try:
        from builder.understand.ledger import (
            _MAX_UNGROUNDED_SHARE,  # lazy: keeps this package import-cycle free
        )

        return float(_MAX_UNGROUNDED_SHARE)
    except Exception:
        return 0.4


def check_policy(ir: PolicyIR, bundle: BriefBundle, tool_api: ToolApi) -> GuardReport:
    """The policy IR against the briefs and the tool API (see the module docstring)."""
    report = GuardReport(stage="policy")
    corpus = _Corpus([*(document.text for document in bundle.documents), tool_api.describe()])
    data = ir.to_json_dict()
    _check_ir_literals(data, corpus, report)
    _check_ir_text_safety(data, report, document_markers(bundle))
    _check_ir_against_tool_api(ir, tool_api, report)
    return report


def _top_band_end_path(data: dict[str, Any]) -> str:
    """JSON path of the ``days_late_max`` of the band that reaches furthest (the open-ended one), or ``""``."""
    bands = data.get("bands")
    if not isinstance(bands, list) or not bands:
        return ""
    ends = [
        (band.get("days_late_max"), -index) for index, band in enumerate(bands) if isinstance(band, dict)
    ]
    ends = [(end, order) for end, order in ends if isinstance(end, int | float) and not isinstance(end, bool)]
    if not ends:
        return ""
    return f"bands[{-max(ends)[1]}].days_late_max"


def _check_ir_literals(data: dict[str, Any], corpus: _Corpus, report: GuardReport) -> None:
    found = 0
    open_end = _top_band_end_path(data)
    for path, key, value in _walk_scalars(data, _ILLUSTRATIVE_KEYS):
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, str):
            for kind, literal in _grounding_problems(value, corpus):
                found += 1
                if found <= _MAX_FINDINGS:
                    report.block(
                        "ungrounded_date" if kind == "date" else "ungrounded_amount",
                        f"The {kind} {literal} is not in the briefs or the tool API (invented?): {_truncate(value, 60)!r}.",
                        path,
                    )
        elif isinstance(value, int | float):
            _check_numeric_field(path, key, value, corpus, report, open_ended=path == open_end)
    if found > _MAX_FINDINGS:
        report.block("ungrounded_amount", f"{found - _MAX_FINDINGS} more ungrounded dates or amounts.", "ir")


def _check_numeric_field(
    path: str, key: str, value: float, corpus: _Corpus, report: GuardReport, *, open_ended: bool = False
) -> None:
    number = Decimal(str(value))
    if key in _STRUCTURAL_NUMERIC_KEYS or number in (0, 1):
        return
    if corpus.has_number(number, neighbours=key in _NEIGHBOUR_NUMERIC_KEYS):
        return
    if open_ended:
        # The end of the top band: "31 days or more" gives no number to copy. A larger end cannot move a customer the
        # brief covers into another band, so it never blocks; a small unexplained one is only worth a look.
        if number < _OPEN_ENDED_DAYS:
            report.warn(
                "ungrounded_number",
                f"The number {value} ends the top band but is not stated in the briefs; use a large number "
                f"(for example 999) when the band has no upper limit.",
                path,
            )
        return
    small = number == number.to_integral_value() and abs(number) < 10
    message = f"The number {value} is not stated in the briefs or the tool API (invented?)."
    if small:
        report.warn("ungrounded_number", message, path)
    else:
        report.block("ungrounded_number", message, path)


def _check_ir_text_safety(
    data: dict[str, Any], report: GuardReport, documented: frozenset[str] = frozenset()
) -> None:
    for path, _key, value in _walk_scalars(data, frozenset({"rule_ids", "rule_id"})):
        if isinstance(value, str) and value:
            _scan_text_safety(value, path, report, label="The policy text", documented=documented)


def _expected(tool_api: ToolApi, tool: str, name: str, where: str = "request") -> list[str] | None:
    contract = tool_api.tools.get(tool)
    spec = contract.get_field(name, where) if contract is not None else None  # type: ignore[arg-type]
    return list(spec.enum) if spec is not None and spec.enum else None


def _product_fields(tool_api: ToolApi) -> list[FieldSpec]:
    contract = tool_api.tools.get("get_account")
    if contract is None:
        return []
    for spec in contract.response_fields:
        if spec.items:
            return list(spec.items)
    return []


def _check_member(
    report: GuardReport, value: str | None, allowed: list[str] | None, label: str, location: str
) -> None:
    if value is None or not allowed or value in allowed:
        return
    report.block(
        "enum", f"{label} {value!r} is not allowed by the tool API (allowed: {', '.join(allowed)}).", location
    )


def _check_ir_against_tool_api(ir: PolicyIR, tool_api: ToolApi, report: GuardReport) -> None:
    # AUD-20: read each vocabulary from the field that holds its role (the compiler's view), not the kit's field name
    from builder.policy.ir import ANY_PRODUCT
    from builder.understand.validate import tool_vocabulary

    vocab = tool_vocabulary(tool_api)
    outcomes, reasons = vocab.outcomes, vocab.reasons
    for path, ref in ir.outcome_refs():
        _check_member(report, ref.outcome, outcomes, "Outcome", f"{path}.outcome")
        _check_member(report, ref.reason, reasons, "Reason", f"{path}.reason")
    _check_member(report, ir.multi_product.notes_outcome, outcomes, "Outcome", "multi_product.notes_outcome")

    id_fields = _expected(tool_api, VERIFY_TOOL, vocab.name_of("selector", "field"))
    for index, question in enumerate(ir.identity.questions):
        _check_member(
            report, question.field, id_fields, "Identity field", f"identity.questions[{index}].field"
        )
    hints = _expected(tool_api, VERIFY_TOOL, "hint", "response")
    for index, hint in enumerate(ir.identity.hints):
        _check_member(report, hint.value, hints, "Hint", f"identity.hints[{index}].value")

    note_tool = ir.multi_product.note_tool
    note_reasons = _expected(tool_api, note_tool, vocab.name_of("note_reason", "reason") if note_tool == "add_note"
                             else "reason")
    for index, reason in enumerate(ir.multi_product.note_reasons):
        _check_member(report, reason, note_reasons, "Note reason", f"multi_product.note_reasons[{index}]")
    _check_member(
        report,
        ir.multi_product.paid_note_reason,
        note_reasons,
        "Note reason",
        "multi_product.paid_note_reason",
    )
    for index, intent in enumerate(ir.intents):
        _check_member(
            report, intent.note_reason, note_reasons, "Note reason", f"intents[{index}].note_reason"
        )
        for position, action in enumerate(intent.actions):
            _check_action(
                tool_api, action.tool, action.params, f"intents[{index}].actions[{position}]", report
            )

    fields = _product_fields(tool_api)
    product_types = vocab.product_types
    names = {spec.name for spec in fields}
    for product_type in [key for key in ir.disclosure.product_naming if key != ANY_PRODUCT]:  # "*" is no type
        _check_member(
            report, product_type, product_types, "Product type", f"disclosure.product_naming.{product_type}"
        )
    for product_type in [key for key in ir.amounts.floor_field_by_type if key != ANY_PRODUCT]:
        _check_member(
            report, product_type, product_types, "Product type", f"amounts.floor_field_by_type.{product_type}"
        )
    if names:
        for product_type, field in ir.amounts.floor_field_by_type.items():
            if field not in names:
                report.warn(
                    "product_field",
                    f"Floor field {field!r} is not a field of the get_account products ({', '.join(sorted(names))}).",
                    f"amounts.floor_field_by_type.{product_type}",
                )
        for index, field in enumerate(ir.disclosure.state_fields):
            if field not in names:
                report.warn(
                    "product_field",
                    f"State field {field!r} is not a field of the get_account products.",
                    f"disclosure.state_fields[{index}]",
                )


def _check_action(
    tool_api: ToolApi, tool: str, params: Mapping[str, str], location: str, report: GuardReport
) -> None:
    contract = tool_api.tools.get(tool)
    if contract is None:
        report.block(
            "unknown_tool",
            f"The IR calls tool {tool!r}, which the tool API does not have.",
            f"{location}.tool",
        )
        return
    for name, value in params.items():
        spec = contract.get_field(name, "request")
        if spec is None:
            report.warn("tool_param", f"{tool} has no request field {name!r}.", f"{location}.params.{name}")
        elif spec.enum and "{" not in value and value not in spec.enum:
            report.block(
                "enum",
                f"{tool}.{name}={value!r} is not allowed by the tool API (allowed: {', '.join(spec.enum)}).",
                f"{location}.params.{name}",
            )


# ------------------------------------------------------------------------------------------------------------------------
# check_prose
# ------------------------------------------------------------------------------------------------------------------------


class _ProseLike(Protocol):
    system_prompt_extra: str
    per_node: dict[str, str]


_SNAKE = r"[a-z][a-z0-9]*(?:_[a-z0-9]+)+"
_TOOL_MENTION = re.compile(
    rf"\b({_SNAKE})\s*\("  # name(
    rf"|(?i:\b(?:tool|function))\s+[`'\"]?({_SNAKE})\b"  # tool name
    rf"|[`'\"]({_SNAKE})[`'\"]\s+(?i:tool|function)\b"  # `name` tool
    rf"|(?i:\b(?:call|calls|calling|invoke|invokes))\s+(?:(?i:the)\s+)?[`'\"]?({_SNAKE})\b"  # call name
)


def _known_names(ir: PolicyIR, tool_api: ToolApi) -> set[str]:
    names: set[str] = set(tool_api.tools) | set(PLATFORM_FUNCTIONS) | set(ir.call_variable_names())

    def add_fields(fields: Iterable[FieldSpec]) -> None:
        for spec in fields:
            names.add(spec.name)
            add_fields(spec.items or ())

    for contract in tool_api.tools.values():
        add_fields(contract.request_fields)
        add_fields(contract.response_fields)
    return names


def check_prose(
    prose: _ProseLike,
    ir: PolicyIR,
    *,
    tool_api: ToolApi | None = None,
    ledger: RuleLedger | None = None,
) -> GuardReport:
    """Drafted node prose against the IR.

    Blocks: a date or amount the IR (and, when given, the ledger and tool API) does not state; a tool named that the tool
    API does not have (needs ``tool_api``); an injection marker or key-like token. Warns: prose longer than
    :data:`PROSE_WARN_CHARS` per node.

    Args:
        prose: Anything with ``system_prompt_extra`` and ``per_node`` (a ``NodeProse``).
        ir: The policy IR the prose must stay within.
        tool_api: When given, tool mentions are checked against it.
        ledger: When given, the ledger's quotes and statements also count as sources for amounts and dates.
    """
    report = GuardReport(stage="prose")
    sources = [json.dumps(ir.to_json_dict(), ensure_ascii=False)]
    if ledger is not None:
        sources.extend(f"{rule.quote}\n{rule.statement}" for rule in ledger.rules)
    if tool_api is not None:
        sources.append(tool_api.describe())
    corpus = _Corpus(sources)
    known = _known_names(ir, tool_api) if tool_api is not None else set()
    pieces = [("prose.system_prompt_extra", prose.system_prompt_extra or "")]
    pieces.extend((f"prose.per_node.{name}", text or "") for name, text in prose.per_node.items())
    for location, text in pieces:
        if not text.strip():
            continue
        if len(text) > PROSE_WARN_CHARS:
            report.warn(
                "prose_length",
                f"The prose is {len(text):,} characters (warning above {PROSE_WARN_CHARS:,}).",
                location,
            )
        for kind, literal in _grounding_problems(text, corpus):
            report.block(
                "ungrounded_date" if kind == "date" else "ungrounded_amount",
                f"The {kind} {literal} is not in the policy IR (invented?).",
                location,
            )
        if tool_api is not None:
            for match in _TOOL_MENTION.finditer(text):
                name = next(group for group in match.groups() if group)
                if name not in known:
                    report.block(
                        "unknown_tool",
                        f"The prose names {name!r}, which is not a tool of the tool API.",
                        location,
                    )
        _scan_text_safety(text, location, report, label="The prose")
    return report


# ------------------------------------------------------------------------------------------------------------------------
# check_workflow
# ------------------------------------------------------------------------------------------------------------------------

_URL_IN_TEXT = re.compile(r"https?://[^\s\"'<>)\]]+", re.IGNORECASE)
_PROMPT_KEYS = frozenset({"system_prompt", "role_message", "task_message", "prompt"})
_URL_ASSIGNMENT = re.compile(r"""\[\s*['"]url['"]\s*\]\s*=(?!=)([^\n]*)""")


def _iter_dicts(obj: Any, path: str = "") -> Iterator[tuple[str, dict[str, Any]]]:
    if isinstance(obj, dict):
        yield path, obj
        for key, value in obj.items():
            yield from _iter_dicts(value, f"{path}.{key}" if path else str(key))
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            yield from _iter_dicts(value, f"{path}[{index}]")


def _endpoint(url: str) -> tuple[str, int | None] | None:
    """``(host, effective port)`` of an absolute URL, or ``None`` when it has no literal host."""
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        port = parts.port or {"https": 443, "http": 80}.get(parts.scheme)
    except ValueError:
        return None
    if not host or "{" in host or "{{" in parts.netloc:
        return None
    return host, port


def _tool_endpoints(tool_api: ToolApi) -> set[tuple[str, int | None]]:
    urls = [contract.url for contract in tool_api.tools.values()]
    if tool_api.base_url:
        urls.append(tool_api.base_url)
    return {endpoint for url in urls if (endpoint := _endpoint(url)) is not None}


def _tool_paths(tool_api: ToolApi) -> set[str]:
    return {contract.url.split("?")[0].rstrip("/") for contract in tool_api.tools.values()}


def _api_call_url(function: dict[str, Any]) -> tuple[bool, str | None]:
    """``(is an api_call, its fixed url default)`` for a tool function dict."""
    handler = str(function.get("handler") or "")
    if not handler.endswith("api_call"):
        return False, None
    parameters = function.get("parameters")
    properties = parameters.get("properties") if isinstance(parameters, dict) else None
    spec = properties.get("url") if isinstance(properties, dict) else None
    default = spec.get("default") if isinstance(spec, dict) else None
    return True, default if isinstance(default, str) and default.strip() else None


def _called_tools(node: Mapping[str, Any]) -> set[str]:
    """Tool names a node can call: function names and the last path segment of api_call URLs."""
    names: set[str] = set()
    for _path, item in _iter_dicts(node):
        if "handler" not in item:
            continue
        if isinstance(item.get("name"), str):
            names.add(item["name"])
        is_api, url = _api_call_url(item)
        if is_api and url:
            names.add(url.split("?")[0].rstrip("/").rsplit("/", 1)[-1])
    return names


def _check_api_urls(wf: Mapping[str, Any], tool_api: ToolApi, report: GuardReport) -> None:
    endpoints = _tool_endpoints(tool_api)
    known_paths = _tool_paths(tool_api)
    allowed = ", ".join(sorted(f"{host}" for host, _port in endpoints)) or "none"
    api_urls: set[str] = set()
    for path, item in _iter_dicts(wf):
        is_api, url = _api_call_url(item)
        if not is_api:
            continue
        location = f"{path}.parameters.properties.url.default"
        if url is None:
            report.block(
                "foreign_url",
                "An api_call has no fixed url default; the model could send the call anywhere.",
                location,
            )
            continue
        api_urls.add(url)
        endpoint = _endpoint(url)
        if endpoint is None:
            report.block(
                "foreign_url", f"The api_call url {_truncate(url, 70)!r} has no literal host.", location
            )
        elif endpoint not in endpoints:
            report.block(
                "foreign_url",
                f"The api_call url {_truncate(url, 70)!r} is not on the tool API host (allowed: {allowed}).",
                location,
            )
        elif "{{" not in url and url.split("?")[0].rstrip("/") not in known_paths:
            report.warn(
                "unknown_endpoint",
                f"The api_call url {_truncate(url, 70)!r} is not a tool of the tool API.",
                location,
            )
    for path, item in _iter_dicts(wf):
        code = item.get("pre_process_code")
        if not isinstance(code, str):
            continue
        for assignment in _URL_ASSIGNMENT.finditer(code):
            if assignment.group(1).strip() not in ("''", '""'):  # blanking the url cancels the call: safe
                report.warn(
                    "url_rewrite",
                    "pre_process_code assigns the request url at run time.",
                    f"{path}.pre_process_code",
                )
                break
    found = 0
    for path, _key, value in _walk_scalars(wf, frozenset()):
        if not isinstance(value, str) or value in api_urls:
            continue
        for match in _URL_IN_TEXT.finditer(value):
            endpoint = _endpoint(match.group(0).rstrip(".,;"))
            if endpoint is not None and endpoint not in endpoints:
                found += 1
                if found <= 5:
                    report.warn(
                        "foreign_url_text",
                        f"The text mentions a URL off the tool API host: {_truncate(match.group(0), 60)!r}.",
                        path,
                    )


def _check_secrets(wf: Mapping[str, Any], secrets: Iterable[Any] | None, report: GuardReport) -> None:
    values = _secret_values(secrets)
    reported: set[str] = set()
    for path, _key, value in _walk_scalars(wf, frozenset()):
        if not isinstance(value, str):
            continue
        for index, secret in enumerate(values):
            if secret in value:
                report.block(
                    "secret", f"A configured secret value (#{index + 1}) is written into the workflow.", path
                )
        for key in find_key_tokens(value):
            marker = f"{path}:{key.kind}"
            if marker not in reported:
                reported.add(marker)
                report.block("secret", f"A {key.kind} ({key.preview}) is written into the workflow.", path)


def _node_map(wf: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    nodes = wf.get("nodes")
    if isinstance(nodes, Mapping):
        return {str(name): node for name, node in nodes.items() if isinstance(node, dict)}
    if isinstance(nodes, list):
        return {str(node["name"]): node for node in nodes if isinstance(node, dict) and "name" in node}
    return {}


def _successors(node: Mapping[str, Any]) -> list[str]:
    targets: list[str] = []
    for transition in node.get("transitions") or ():
        if isinstance(transition, dict) and isinstance(transition.get("target_node"), str):
            targets.append(transition["target_node"])
    for key in ("passthrough_default_transition", "max_turn_transition"):
        if isinstance(node.get(key), str):
            targets.append(node[key])
    return targets


def _spoken_lines(node: Mapping[str, Any], base: str) -> list[tuple[str, str]]:
    """Fixed spoken lines of a node: a ``tts`` entry line and any ``spoken_message`` (value or schema default)."""
    lines: list[tuple[str, str]] = []
    on_enter = node.get("on_enter")
    if isinstance(on_enter, dict) and on_enter.get("type") == "tts" and isinstance(on_enter.get("text"), str):
        lines.append((f"{base}.on_enter.text", on_enter["text"]))
    for path, item in _iter_dicts(node, base):
        message = item.get("spoken_message")
        if isinstance(message, str) and message.strip():
            lines.append((f"{path}.spoken_message", message))
        elif (
            isinstance(message, dict)
            and isinstance(message.get("default"), str)
            and message["default"].strip()
        ):
            lines.append((f"{path}.spoken_message.default", message["default"]))
    return lines


def _check_pre_verification_speech(
    wf: Mapping[str, Any], forbidden: Iterable[str], report: GuardReport
) -> None:
    words = [word.strip().lower() for word in forbidden if word and word.strip()]
    nodes = _node_map(wf)
    if not words or not nodes:
        return
    start = wf.get("initial_node")
    if not isinstance(start, str) or start not in nodes:
        report.warn(
            "verification_flow",
            "The workflow has no valid initial_node; speech before verification was not checked.",
            "initial_node",
        )
        return
    verifying = {name for name, node in nodes.items() if VERIFY_TOOL in _called_tools(node)}
    if not verifying:
        report.warn(
            "verification_flow",
            f"No node calls {VERIFY_TOOL}; only the first node and the global nodes were checked for forbidden words.",
            "nodes",
        )
    before: list[str] = []
    seen: set[str] = set()
    queue = [start]
    while queue:
        name = queue.pop(0)
        if name in seen or name not in nodes:
            continue
        seen.add(name)
        before.append(name)
        if name in verifying or (not verifying and name == start):
            continue  # what follows the verifying node happens after (or instead of) verification
        queue.extend(_successors(nodes[name]))
    lines = [line for name in before for line in _spoken_lines(nodes[name], f"nodes.{name}")]
    global_nodes = wf.get("global_nodes")
    if isinstance(global_nodes, Mapping):  # an interrupt can fire at any time, including before verification
        for name, node in global_nodes.items():
            if isinstance(node, dict):
                lines.extend(_spoken_lines(node, f"global_nodes.{name}"))
    for location, text in lines:
        lowered = " ".join(text.lower().split())
        for word in words:
            if re.search(r"(?<!\w)" + re.escape(" ".join(word.split())), lowered):
                report.block(
                    "forbidden_word",
                    f"The word {word!r} is spoken before identity is verified: {_truncate(text, 70)!r}.",
                    location,
                )


def _prompt_chars(obj: Any) -> int:
    total = 0
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in _PROMPT_KEYS and isinstance(value, str):
                total += len(value)
            elif key != "system_prompt":
                total += _prompt_chars(value)
    elif isinstance(obj, list):
        total += sum(_prompt_chars(item) for item in obj)
    return total


def _check_prompt_size(wf: Mapping[str, Any], report: GuardReport) -> None:
    system = wf.get("system_prompt")
    base = len(system) if isinstance(system, str) else 0
    nodes = _node_map(wf)
    total = base + sum(_prompt_chars(node) for node in nodes.values()) + _prompt_chars(wf.get("global_nodes"))
    if total > PROMPT_WARN_TOTAL_CHARS:
        report.warn(
            "prompt_size",
            f"The workflow carries {total:,} characters of prompt text (warning above {PROMPT_WARN_TOTAL_CHARS:,}).",
            "workflow",
        )
    for name, node in nodes.items():
        size = base + _prompt_chars(node)
        if size > PROMPT_WARN_NODE_CHARS:
            report.warn(
                "prompt_size",
                f"The model reads {size:,} characters of prompt in node {name} (warning above {PROMPT_WARN_NODE_CHARS:,}).",
                f"nodes.{name}",
            )


def check_workflow(
    wf: Mapping[str, Any],
    tool_api: ToolApi,
    secrets: Iterable[Any] | None = (),
    ir: PolicyIR | None = None,
) -> GuardReport:
    """The compiled workflow (plain dict, the platform's JSON) against the tool API, the secrets and the IR.

    Blocks: an ``api_call`` whose ``url`` default is missing or not on the tool API host (the phone agent may call only the
    bank's tools); a secret value (``secrets``) or a key-like token (``fak_``, ``sk-``, ``sk-ant-`` ...) anywhere in the
    workflow; a ``forbidden_before_verification`` word of the IR in a fixed spoken line the caller can hear before
    ``verify_identity`` succeeds (needs ``ir``). Warns: ``agent_config`` present (the platform fills the defaults), an
    ``api_call`` to an unknown path, a foreign URL in prompt text, prompts that are very large.

    Args:
        wf: The workflow as a dict (``initial_node``, ``system_prompt``, ``nodes``, ``global_nodes``).
        tool_api: The parsed tool API (its URLs define the allowed hosts).
        secrets: Secret values that must never appear (``str`` or ``SecretStr``); values under 8 characters are ignored.
        ir: The policy IR; ``None`` skips the speech check.
    """
    if hasattr(wf, "to_json"):
        wf = wf.to_json()  # a typed Workflow
    report = GuardReport(stage="workflow")
    if not isinstance(wf, Mapping):
        report.block(
            "workflow_shape", f"The workflow is a {type(wf).__name__}, not a JSON object.", "workflow"
        )
        return report
    _check_api_urls(wf, tool_api, report)
    _check_secrets(wf, secrets, report)
    if "agent_config" in wf:
        report.warn(
            "agent_config",
            "The workflow sets agent_config; leave model and voice settings to the platform.",
            "agent_config",
        )
    if ir is not None:
        _check_pre_verification_speech(wf, ir.forbidden_before_verification, report)
    _check_prompt_size(wf, report)
    return report
