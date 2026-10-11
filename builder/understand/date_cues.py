"""Read the persuasion date kind a step's own text states (audit AUD-18).

The extraction model writes each persuasion step twice: as a closed date ``kind`` (which the compiled code turns into
a calendar date) and as ``text`` in the documents' words. The kind decides the date the agent proposes on every call,
so a kind that contradicts its own text is wrong deterministically, on every repetition. Nothing else compares the two.

:func:`classify_proposal_date` is a small, generic, confidence-gated reader of English date vocabulary ("the next
business day after today", "the last business day of this week; if today is that day ...", "on the deadline", "in 3
calendar days"). It answers only when the text names exactly one date family and carries no extra condition it cannot
express; otherwise it abstains (``None``). :func:`corrected_proposal_date` adds the rules that keep a correction safe:

* a ``custom`` kind is never overridden (the agent already follows the text for it);
* the conditional week kind is never downgraded to the plain week kind (a shortened text such as "the last business
  day of this week" does not prove the condition is absent);
* a ``customer`` kind (the customer names the day) is replaced only when the text plainly *proposes* a date and does
  not ask the customer to choose one, and never by ``deadline`` ("a date by the deadline" is a limit, not a proposal).

The safe fixes (:mod:`builder.understand.fixes`) apply the correction; the validator reports any step a correction
would change (``PERSUASION_DATE_KIND``), so a contradiction never passes silently. No brief wording lives here: only
general calendar words.
"""

from __future__ import annotations

import re

__all__ = ["classify_proposal_date", "corrected_proposal_date"]

_DAY = r"(?:business|working|banking)\s+day"
_NUM = (r"\d+|one|two|three|four|five|six|seven|eight|nine|ten|first|second|third|fourth|fifth|sixth|seventh|"
        r"eighth|ninth|tenth")
_CARDINALS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"]
_ORDINALS = ["first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth", "tenth"]
_WORDS = {word: index for index, word in enumerate(_CARDINALS)}
_WORDS.update({word: index + 1 for index, word in enumerate(_ORDINALS)})
_ANCHOR = r"(?:today|the\s+call(?:\s+date)?|the\s+date\s+of\s+the\s+call|the\s+day\s+of\s+the\s+call)"

_CUES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("last_week", re.compile(rf"\blast\s+{_DAY}\s+of\s+(?:this|the\s+current|the)\s+week\b"
                             r"|\bend\s+of\s+(?:this|the)\s+week\b", re.IGNORECASE)),
    ("if_today_last", re.compile(r"\bif\s+(?:today\s+is\s+(?:that|this|the\s+last)|that\s+(?:day\s+)?is\s+today"
                                 r"|it\s+is\s+today)\b|\bof\s+(?:the\s+)?(?:next|following)\s+week\b", re.IGNORECASE)),
    ("next_bd", re.compile(rf"\b(?:next|following)\s+{_DAY}\b|\bfirst\s+{_DAY}\s+after\s+{_ANCHOR}|\btomorrow\b",
                           re.IGNORECASE)),
    ("nth_bd", re.compile(rf"\b(?P<n>{_NUM})(?:st|nd|rd|th)?\s+{_DAY}s?\s+(?:after|from)\s+{_ANCHOR}"
                          rf"|\bin\s+(?P<m>{_NUM})\s+{_DAY}s\b", re.IGNORECASE)),
    ("nth_cal", re.compile(rf"\b(?P<n>{_NUM})(?:st|nd|rd|th)?\s+(?:calendar\s+)?days?\s+(?:after|from)\s+{_ANCHOR}"
                           rf"|\bin\s+(?P<m>{_NUM})\s+(?:calendar\s+)?days\b", re.IGNORECASE)),
    ("deadline", re.compile(r"\b(?:on|by)\s+(?:the|its|their|the\s+product's)\s+deadline\b"
                            r"|\blatest\s+(?:possible|allowed|recordable)\s+(?:promise\s+)?date\b", re.IGNORECASE)),
    ("today", re.compile(r"\b(?:pay(?:ing|ment)?|propose)\s+(?:it\s+)?today\b|\bfor\s+today\b", re.IGNORECASE)),
)
# A condition other than the amount clause ("... or the floor if the customer already said ...") is a rule the plain
# kinds may not express: abstain and keep the model's kind.
_CONDITION = re.compile(r"\b(?:if|unless|except|otherwise|when|instead)\b"
                        r"(?!\s+the\s+customer\s+(?:already\s+)?(?:said|says|has\s+said))", re.IGNORECASE)
_IF_TODAY_LAST = re.compile(r"\bif\s+(?:today\s+is\s+(?:that|this|the\s+last)|that\s+(?:day\s+)?is\s+today"
                            r"|it\s+is\s+today)\b", re.IGNORECASE)
_DEADLINE_WORD = re.compile(r"\bdeadline\b", re.IGNORECASE)
_PROPOSES = re.compile(r"\b(?:propose|suggest|offer)\w*\b", re.IGNORECASE)
_CUSTOMER_CHOOSES = re.compile(r"\b(?:ask|asks|asking|choose|chooses|chosen|pick|picks|names?|named|prefer\w*)\b",
                               re.IGNORECASE)

_FAMILY_KIND = {"next_bd": "next_business_day_after_today", "deadline": "deadline", "today": "today"}


def classify_proposal_date(text: str) -> tuple[str, int | None] | None:
    """``(date kind, n)`` the text states, or ``None`` when it states no date rule, several, or a conditional one.

    >>> classify_proposal_date("Propose the floor amount on the next business day after today.")
    ('next_business_day_after_today', None)
    >>> classify_proposal_date("Propose the last business day of this week. If today is that day, propose the last "
    ...                        "business day of next week.")
    ('last_business_day_next_week_if_today_is_last', None)
    >>> classify_proposal_date("Propose the next business day, unless that is after the deadline.") is None
    True
    """
    text = text or ""
    hits = {name: match for name, pattern in _CUES if (match := pattern.search(text))}
    if "nth_bd" in hits or "nth_cal" in hits:  # "3rd business day after today" also reads as "business day"
        hits.pop("next_bd", None)
        if "nth_bd" in hits:
            hits.pop("nth_cal", None)
    families = {"last_week" if name in ("last_week", "if_today_last") else name for name in hits}
    if len(families) != 1:
        return None
    family = families.pop()
    if family != "deadline" and _DEADLINE_WORD.search(text):
        return None
    if family == "last_week":
        if "last_week" not in hits:
            return None
        if "if_today_last" in hits:
            rest = _IF_TODAY_LAST.sub(" ", text)
            return None if _CONDITION.search(rest) else ("last_business_day_next_week_if_today_is_last", None)
        return None if _CONDITION.search(text) else ("last_business_day_this_week", None)
    if _CONDITION.search(text):
        return None
    if family in ("nth_bd", "nth_cal"):
        raw = (hits[family].group("n") or hits[family].group("m")).lower()
        n = int(raw) if raw.isdigit() else _WORDS[raw]
        if family == "nth_bd":
            return ("next_business_day_after_today", None) if n == 1 else ("nth_business_day_after_today", n)
        return ("nth_calendar_day_after_today", n)
    return (_FAMILY_KIND[family], None)


def corrected_proposal_date(kind: str, n: int | None, text: str) -> tuple[str, int | None] | None:
    """The ``(kind, n)`` a step must have instead of ``(kind, n)`` given its ``text``, or ``None`` to keep it."""
    if kind == "custom":
        return None
    found = classify_proposal_date(text)
    if found is None:
        return None
    new_kind, new_n = found
    if new_kind == kind and (new_n is None or new_n == n):
        return None
    if kind == "last_business_day_next_week_if_today_is_last" and new_kind == "last_business_day_this_week":
        return None
    if kind == "customer" and (new_kind == "deadline" or not _PROPOSES.search(text)
                               or _CUSTOMER_CHOOSES.search(text)):
        return None
    return found
