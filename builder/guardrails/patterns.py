"""Text patterns shared by the guards: prompt-injection markers, key-like tokens, invisible characters.

Everything here is a heuristic over plain text and has no dependencies, so it can be imported from anywhere
(including the LLM gateway) without pulling in the rest of the builder.

* **Injection markers** (:data:`INJECTION_PATTERNS`) are phrases that address the *model* instead of describing a
  bank rule ("ignore the previous instructions", ``</system>``). A phrase can also occur in an honest brief ("you are
  now verified"), so every pattern is flagged *strong* (almost never honest) or *weak* (often honest). Briefs only
  ever produce warnings (the text stays data and is never obeyed); strong markers inside a *derived* artifact (policy
  IR, drafted prose) block, because those artifacts are written by a model and must hold only bank rules - except the
  *amendment-like* ones ("disregard the earlier brief on X", "override the previous rules on Y"): an amending memo is
  worded exactly so, a rule statement that keeps the verb is policy, and blocking it would end the build, so those
  warn.
* **Key-like tokens** (:data:`KEY_PATTERNS`) are the shapes of the credentials this project handles
  (``fak_...``, ``sk-ant-...``, ``sk-...``) plus a few common others.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = [
    "INJECTION_PATTERNS",
    "KEY_PATTERNS",
    "InjectionHit",
    "KeyHit",
    "blank_keys",
    "control_characters",
    "find_injections",
    "find_key_tokens",
    "line_of",
]


@dataclass(frozen=True)
class InjectionPattern:
    """One prompt-injection heuristic.

    ``strong`` markers are (almost) never part of an honest bank document. Among them, ``amendment_like`` ones are
    worded the way an amending memo is ("disregard the earlier brief on X", "override the previous rules on Y",
    "forget the 5-band draft"): they still warn when found in an input document, but a *derived* artifact (rule
    statement, policy IR, drafted prose) that repeats them is bank policy, not text addressed to the model, so they warn
    there instead of blocking. Markers that talk to the model itself (``ignore your instructions``, role tags, chat
    template tokens, ``BEGIN SYSTEM``, ``reveal the system prompt``) block there.
    """

    name: str
    regex: re.Pattern[str]
    strong: bool
    amendment_like: bool = False

    @property
    def blocks_derived(self) -> bool:
        """True when this marker blocks inside a derived artifact (strong and not amendment-like)."""
        return self.strong and not self.amendment_like


_FILLER = r"(?:(?:all|any|the|your|these|those|every|of|my|previous|prior|above|earlier|preceding|safety|system)\s+){0,4}"
_MODEL_TARGETS = r"(?:instructions?|prompts?|directions?|guidelines?)"
_MEMO_TARGETS = r"(?:rules?|messages?|brief)"

INJECTION_PATTERNS: tuple[InjectionPattern, ...] = (
    InjectionPattern("ignore_instructions", re.compile(rf"\bignore\s+{_FILLER}{_MODEL_TARGETS}\b", re.I), True),
    InjectionPattern(
        "ignore_rules", re.compile(rf"\bignore\s+{_FILLER}{_MEMO_TARGETS}\b", re.I), True, amendment_like=True
    ),
    InjectionPattern(
        "disregard_instructions", re.compile(rf"\bdisregard\s+{_FILLER}{_MODEL_TARGETS}\b", re.I), True
    ),
    InjectionPattern(
        "disregard_rules",
        re.compile(rf"\bdisregard\s+{_FILLER}{_MEMO_TARGETS}\b", re.I),
        True,
        amendment_like=True,
    ),
    InjectionPattern(
        "forget_instructions",
        re.compile(r"\bforget\s+(?:all\s+)?(?:of\s+)?your\s+(?:\w+\s+)?instructions", re.I),
        True,
    ),
    InjectionPattern(
        "forget_everything",
        re.compile(r"\bforget\s+(?:everything|all\s+(?:of\s+)?(?:that|this|the|previous))", re.I),
        True,
        amendment_like=True,
    ),
    InjectionPattern(
        "override_instructions",
        re.compile(
            r"\boverride\s+(?:the\s+|your\s+|all\s+)?(?:system|safety|previous)?\s*(?:instructions?|prompt)\b", re.I
        ),
        True,
    ),
    InjectionPattern(
        "override_rules",
        re.compile(r"\boverride\s+(?:the\s+|your\s+|all\s+)?(?:system|safety|previous)?\s*rules\b", re.I),
        True,
        amendment_like=True,
    ),
    InjectionPattern(
        "do_not_follow",
        re.compile(r"\bdo\s+not\s+follow\s+(?:the\s+|these\s+|any\s+|your\s+)?instructions?\b", re.I),
        True,
    ),
    InjectionPattern(
        "do_not_follow_rules",
        re.compile(r"\bdo\s+not\s+follow\s+(?:the\s+|these\s+|any\s+|your\s+)?(?:brief|rules)\b", re.I),
        True,
        amendment_like=True,
    ),
    InjectionPattern(
        "role_tag",
        re.compile(r"</?\s*(?:system|assistant|user)\s*>", re.I),
        True,
    ),
    InjectionPattern(
        "chat_template_token",
        re.compile(
            r"<\|?\s*(?:im_start|im_end|endoftext|system|assistant)\s*\|?>|\[/?INST\]|<<\s*/?SYS\s*>>", re.I
        ),
        True,
    ),
    InjectionPattern(
        "begin_block",
        re.compile(r"\bBEGIN\s+(?:SYSTEM|INSTRUCTIONS?|PROMPT)\b"),
        True,
    ),
    InjectionPattern(
        "reveal_prompt",
        re.compile(
            r"\b(?:reveal|print|repeat|show)\s+(?:me\s+)?(?:your|the)\s+(?:system|hidden|initial)\s+(?:prompt|instructions?)",
            re.I,
        ),
        True,
    ),
    InjectionPattern("you_are_now", re.compile(r"\byou\s+are\s+now\b", re.I), False),
    InjectionPattern("system_prompt", re.compile(r"\bsystem\s+prompt\b", re.I), False),
    InjectionPattern(
        "developer_message", re.compile(r"\bdeveloper\s+(?:message|prompt|instructions?)\b", re.I), False
    ),
    InjectionPattern("new_instructions", re.compile(r"\bnew\s+instructions?\s*:", re.I), False),
)


@dataclass(frozen=True)
class InjectionHit:
    """A matched injection marker: the pattern name, whether it is strong, the 1-based line and a short excerpt.

    ``blocks_derived`` says whether the marker blocks inside a derived artifact (see :class:`InjectionPattern`);
    ``False`` for weak markers and for strong ones that read like an amending memo.
    """

    name: str
    strong: bool
    line: int
    excerpt: str
    blocks_derived: bool = False


def line_of(text: str, index: int) -> int:
    """The 1-based line number of character ``index`` in ``text``."""
    return text.count("\n", 0, max(0, index)) + 1


def _excerpt(text: str, start: int, end: int, *, width: int = 60) -> str:
    left = max(0, start - 15)
    snippet = text[left : min(len(text), max(end, start) + width - (start - left))]
    return " ".join(snippet.split())


def find_injections(text: str, *, strong_only: bool = False) -> list[InjectionHit]:
    """Every injection marker in ``text`` (in text order), with line numbers.

    Args:
        text: The text to scan.
        strong_only: Report only the strong markers.
    """
    hits: list[InjectionHit] = []
    safe = blank_keys(text or "")  # a key next to a marker must not leak through the excerpt
    for pattern in INJECTION_PATTERNS:
        if strong_only and not pattern.strong:
            continue
        for match in pattern.regex.finditer(text or ""):
            excerpt = _excerpt(safe, match.start(), match.end())
            hits.append(
                InjectionHit(
                    pattern.name, pattern.strong, line_of(text, match.start()), excerpt, pattern.blocks_derived
                )
            )
    hits.sort(key=lambda hit: hit.line)
    return hits


# --- key-like tokens ---------------------------------------------------------------------------------------------------

_NOT_PRECEDED = r"(?<![A-Za-z0-9])"

KEY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("freya_key", re.compile(_NOT_PRECEDED + r"fak_[A-Za-z0-9_\-]{8,}")),
    ("anthropic_key", re.compile(_NOT_PRECEDED + r"sk-ant-[A-Za-z0-9_\-]{8,}")),
    ("openai_style_key", re.compile(_NOT_PRECEDED + r"sk-[A-Za-z0-9_\-]{16,}")),
    ("langfuse_public_key", re.compile(_NOT_PRECEDED + r"pk-lf-[A-Za-z0-9\-]{8,}")),
    ("aws_access_key", re.compile(_NOT_PRECEDED + r"AKIA[0-9A-Z]{16}\b")),
    (
        "github_token",
        re.compile(_NOT_PRECEDED + r"(?:ghp|gho|ghs|ghu)_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}"),
    ),
    ("slack_token", re.compile(_NOT_PRECEDED + r"xox[abprs]-[A-Za-z0-9\-]{10,}")),
    ("bearer_token", re.compile(r"\bBearer\s+(?!\{\{)[A-Za-z0-9._\-]{20,}")),
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
)


@dataclass(frozen=True)
class KeyHit:
    """A key-like token: its kind and a masked preview (never the whole value)."""

    kind: str
    preview: str


_PREFIX = re.compile(r"^[A-Za-z]{2,10}(?:[_-][A-Za-z]{2,6})?[_-]")


def _preview(value: str) -> str:
    """The non-secret lead-in of a key (``fak_``, ``sk-ant-``) and an ellipsis; never a character of the secret part."""
    match = _PREFIX.match(value)
    return (match.group(0) if match else value[:4]) + "..."


def _key_spans(text: str) -> list[tuple[int, int, str]]:
    """Non-overlapping ``(start, end, kind)`` spans of key-like tokens; the longest match at a position wins."""
    spans: list[tuple[int, int, str]] = []
    for kind, regex in KEY_PATTERNS:
        spans.extend((m.start(), m.end(), kind) for m in regex.finditer(text or ""))
    spans.sort(key=lambda span: (span[0], -(span[1] - span[0])))
    kept: list[tuple[int, int, str]] = []
    covered_until = -1
    for start, end, kind in spans:
        if end <= covered_until:  # inside an earlier, longer match ("Bearer fak_...")
            continue
        covered_until = max(covered_until, end)
        kept.append((start, end, kind))
    return kept


def find_key_tokens(text: str) -> list[KeyHit]:
    """Key-like tokens in ``text`` (one hit per distinct, non-overlapping match) with a masked preview (``fak_...``)."""
    hits: list[KeyHit] = []
    seen: set[str] = set()
    for start, end, kind in _key_spans(text):
        value = text[start:end]
        if value not in seen:
            seen.add(value)
            hits.append(KeyHit(kind, _preview(value)))
    return hits


def blank_keys(text: str) -> str:
    """``text`` with every key-like token overwritten by ``*`` (same length, so offsets stay valid)."""
    chars = list(text or "")
    for start, end, _kind in _key_spans(text):
        chars[start:end] = "*" * (end - start)
    return "".join(chars)


# --- invisible / control characters ------------------------------------------------------------------------------------

_BIDI_AND_INVISIBLE = frozenset(
    map(
        chr,
        (
            *range(0x200B, 0x2010),
            *range(0x202A, 0x202F),
            *range(0x2060, 0x2065),
            *range(0x2066, 0x206A),
            0xFEFF,
        ),
    )
)


def control_characters(text: str) -> dict[str, list[int]]:
    """Suspicious characters of ``text`` grouped by kind, each with the 1-based lines they occur on.

    Kinds: ``control`` (C0/C1 controls except tab, newline and carriage return), ``invisible`` (zero-width characters
    and bidirectional overrides, which can hide or reorder text), ``replacement`` (U+FFFD, an undecodable byte).
    """
    found: dict[str, list[int]] = {}
    line = 1
    for char in text or "":
        if char == "\n":
            line += 1
            continue
        code = ord(char)
        kind = ""
        if code == 0xFFFD:
            kind = "replacement"
        elif char in _BIDI_AND_INVISIBLE:
            kind = "invisible"
        elif (code < 0x20 and char not in "\t\r") or 0x7F <= code <= 0x9F:
            kind = "control"
        if kind:
            lines = found.setdefault(kind, [])
            if not lines or lines[-1] != line:
                lines.append(line)
    return found
