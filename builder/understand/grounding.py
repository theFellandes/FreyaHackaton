"""Grounding and coverage: prove that the rule ledger is quoted from the briefs and that it misses nothing.

Two independent audits, both deterministic (no LLM):

**Grounding** - every ledger rule carries a verbatim ``quote``. :func:`check_grounding` verifies that each quote is
really in the brief material. Text is compared after *normalisation* (case, whitespace, curly quotes, dash variants,
Markdown emphasis and table pipes are ignored), so a model that drops a backtick or re-wraps a line is still grounded.
When a quote is not an exact substring, a fuzzy fallback compares it with same-length windows of the source with
:mod:`difflib` and accepts a ratio of at least ``0.9``. :func:`snap_quotes` then replaces such near-miss quotes by the
exact source text so the ledger ends up truly verbatim. Quotes that match nothing are reported: they may be invented
rules, which the builder must never keep silently.

**Coverage** - :func:`uncovered_passages` lists lines of the briefs that no ledger quote overlaps (word-trigram
overlap). The ledger builder feeds them back to the model for a completeness pass ("read everything": a rule that
was skipped is the most common cause of a failed call). :func:`check_phrase_grounding` applies the same idea to the
fixed sentences of the IR: a prescribed sentence that cannot be found in the briefs is probably invented.

All functions are pure; they take the :class:`~builder.policy.ir.RuleLedger` and the
:class:`~builder.ingest.brief_loader.BriefBundle` as they are.
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from builder.policy.ir import PolicyIR, RuleEntry, RuleLedger

if TYPE_CHECKING:
    from builder.ingest.brief_loader import BriefBundle, BriefDocument

__all__ = [
    "FUZZY_THRESHOLD",
    "GroundingReport",
    "NormalizedText",
    "Passage",
    "QuoteCheck",
    "check_grounding",
    "check_phrase_grounding",
    "find_in_text",
    "ground_ledger",
    "normalize_text",
    "normalize_with_offsets",
    "snap_quotes",
    "uncovered_passages",
]

FUZZY_THRESHOLD = 0.9
"""Minimum :class:`difflib.SequenceMatcher` ratio for a quote that is not an exact substring."""

_MIN_FUZZY_CHARS = 12
_QUOTE_MAP: dict[str, str] = {
    **dict.fromkeys(map(chr, (0x2018, 0x2019, 0x201A, 0x201B, 0x2032, 0x00B4)), "'"),
    **dict.fromkeys(map(chr, (0x201C, 0x201D, 0x201E, 0x201F, 0x2033, 0x00AB, 0x00BB)), '"'),
    **dict.fromkeys(map(chr, (0x2010, 0x2011, 0x2012, 0x2013, 0x2014, 0x2015, 0x2212)), "-"),
}
"""Typographic quotes and dashes mapped to their ASCII look-alikes (code points spelled out on purpose)."""
_DROPPED = frozenset("*`") | frozenset(map(chr, (0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF, 0x00AD)))
"""Markdown emphasis characters and zero-width / soft-hyphen characters, ignored when comparing text."""
_SPACE_LIKE = frozenset("|")
_WORD = re.compile(r"[a-z0-9_]+(?:'[a-z]+)?")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
_HEADING = re.compile(r"^\s*#{1,6}\s")
_FILE_MARK = re.compile(r"^\s*=== FILE:")
_TABLE_RULE = re.compile(r"^[\s|:\-+]+$")
_LIST_MARK = re.compile(r"^\s*(?:[-*+•]\s+|\d+[.)]\s+)")


# --------------------------------------------------------------------------------------------------------------------
# Normalisation
# --------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class NormalizedText:
    """Normalised text plus, for every normalised character, its index in the original string."""

    text: str
    offsets: tuple[int, ...]

    def original_span(self, start: int, end: int) -> tuple[int, int]:
        """Map a ``[start, end)`` span of the normalised text back to a span of the original string."""
        if not self.offsets or start >= end:
            return (0, 0)
        return (self.offsets[start], self.offsets[min(end, len(self.offsets)) - 1] + 1)


def _clusters(raw: str) -> list[tuple[int, str]]:
    """Split ``raw`` into ``(start index, characters)`` clusters: a base character plus its combining marks."""
    out: list[tuple[int, str]] = []
    for index, char in enumerate(raw):
        if out and unicodedata.combining(char):
            start, text = out[-1]
            out[-1] = (start, text + char)
        else:
            out.append((index, char))
    return out


def normalize_with_offsets(raw: str) -> NormalizedText:
    """Normalise ``raw`` for comparison and keep a map back to ``raw``.

    Rules: Unicode NFKC, lower case, curly quotes and dash variants become ``'``, ``"`` and ``-``, Markdown emphasis
    (``*`` and backticks) and zero-width characters are dropped, table pipes count as spaces, and every run of white
    space becomes one space (none at the ends).
    """
    chars: list[str] = []
    offsets: list[int] = []
    pending_space = False
    for start, cluster in _clusters(raw or ""):
        for piece in unicodedata.normalize("NFKC", cluster):
            if piece in _DROPPED:
                continue
            piece = _QUOTE_MAP.get(piece, piece)
            if piece.isspace() or piece in _SPACE_LIKE:
                pending_space = True
                continue
            if piece == "-" and chars and chars[-1] == "-" and not pending_space:
                continue  # "--" and a dash character are the same punctuation
            if pending_space and chars:
                chars.append(" ")
                offsets.append(start)
            pending_space = False
            for lowered in piece.lower():
                chars.append(lowered)
                offsets.append(start)
    return NormalizedText("".join(chars), tuple(offsets))


def normalize_text(raw: str) -> str:
    """The normalised form of ``raw`` (see :func:`normalize_with_offsets`)."""
    return normalize_with_offsets(raw).text


# --------------------------------------------------------------------------------------------------------------------
# Finding a quote in a text
# --------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Found:
    start: int
    end: int
    ratio: float


def find_in_text(needle: str, haystack: NormalizedText, *, min_ratio: float = FUZZY_THRESHOLD) -> _Found | None:
    """Locate the already normalised ``needle`` in ``haystack``: exact first, then the best fuzzy window.

    Returns ``None`` when there is no exact hit and no same-length-ish window with a ratio of at least ``min_ratio``
    (needles shorter than 12 characters are never matched fuzzily - too ambiguous).
    """
    if not needle:
        return None
    index = haystack.text.find(needle)
    if index >= 0:
        return _Found(index, index + len(needle), 1.0)
    if len(needle) < _MIN_FUZZY_CHARS:
        return None
    best: _Found | None = None
    for start, end in _candidate_windows(needle, haystack.text):
        window = haystack.text[start:end]
        matcher = difflib.SequenceMatcher(None, needle, window, autojunk=False)
        if matcher.real_quick_ratio() < min_ratio or matcher.quick_ratio() < min_ratio:
            continue
        ratio = matcher.ratio()
        if ratio >= min_ratio and (best is None or ratio > best.ratio):
            best = _Found(start, end, ratio)
    return best


def _candidate_windows(needle: str, text: str) -> list[tuple[int, int]]:
    """Windows of ``text`` worth comparing with ``needle``: around occurrences of its longest words, else a sweep."""
    size = len(needle)
    lengths = sorted({size, max(1, round(size * 0.92)), round(size * 1.08)})
    anchors = sorted({w for w in re.findall(r"[a-z0-9_']{5,}", needle)}, key=len, reverse=True)[:3]
    starts: set[int] = set()
    for word in anchors:
        inside = needle.find(word)
        position = text.find(word)
        seen = 0
        while position >= 0 and seen < 25:
            base = position - inside
            starts.update(range(base - 6, base + 7, 2))
            position = text.find(word, position + 1)
            seen += 1
    if not starts:
        step = max(1, size // 4)
        starts = set(range(0, max(1, len(text) - size + 1), step))
    windows: list[tuple[int, int]] = []
    for start in sorted(starts):
        if start < 0 or start >= len(text):
            continue
        windows.extend((start, min(len(text), start + length)) for length in lengths)
    return windows


# --------------------------------------------------------------------------------------------------------------------
# Ledger grounding
# --------------------------------------------------------------------------------------------------------------------

QuoteStatus = Literal["exact", "fuzzy", "moved", "missing", "empty"]


@dataclass(frozen=True)
class QuoteCheck:
    """The grounding verdict of one ledger rule.

    ``status``: ``exact`` (normalised substring of its own source), ``fuzzy`` (near match, ratio in ``ratio``),
    ``moved`` (found, but in another file than ``rule.source``; ``found_in`` names it), ``missing``, ``empty``.
    ``original`` is the matching source text (for ``fuzzy`` and ``moved``), ready to replace the quote.
    """

    rule_id: str
    status: QuoteStatus
    ratio: float = 0.0
    found_in: str = ""
    original: str = ""


@dataclass
class GroundingReport:
    """Grounding verdicts of a whole ledger."""

    checks: list[QuoteCheck] = field(default_factory=list)

    def with_status(self, *statuses: QuoteStatus) -> list[QuoteCheck]:
        """The checks whose status is one of ``statuses``."""
        return [check for check in self.checks if check.status in statuses]

    @property
    def ungrounded(self) -> list[QuoteCheck]:
        """Rules whose quote is not in the briefs (or is empty)."""
        return self.with_status("missing", "empty")

    @property
    def ok(self) -> bool:
        """True when every quote is grounded (exact, fuzzy or merely attributed to the wrong file)."""
        return not self.ungrounded


def _doc_matches(doc: BriefDocument, source: str) -> bool:
    wanted = source.strip().replace("\\", "/").lower()
    if not wanted:
        return False
    names = {doc.name.lower(), str(doc.path).replace("\\", "/").lower()}
    return wanted in names or any(name.endswith("/" + wanted) or wanted.endswith("/" + name) for name in names)


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def ground_ledger(ledger: RuleLedger, bundle: BriefBundle) -> GroundingReport:
    """Check every quote of ``ledger`` against the documents of ``bundle`` (see the module docstring)."""
    docs = [(doc, normalize_with_offsets(doc.text)) for doc in bundle.documents]
    report = GroundingReport()
    for rule in ledger.rules:
        report.checks.append(_check_rule(rule, docs))
    return report


def _check_rule(rule: RuleEntry, docs: list[tuple[BriefDocument, NormalizedText]]) -> QuoteCheck:
    needle = normalize_text(rule.quote)
    if not needle:
        return QuoteCheck(rule.id, "empty")
    own = [pair for pair in docs if _doc_matches(pair[0], rule.source)]
    others = [pair for pair in docs if pair not in own]
    best: tuple[float, BriefDocument, NormalizedText, _Found] | None = None
    for group, moved in ((own, False), (others, True)):
        for doc, text in group:
            found = find_in_text(needle, text)
            if found is None:
                continue
            if found.ratio >= 1.0:
                return QuoteCheck(rule.id, "moved" if moved else "exact", 1.0, doc.name if moved else "")
            if best is None or found.ratio > best[0]:
                best = (found.ratio, doc, text, found)
    if best is None:
        return QuoteCheck(rule.id, "missing")
    ratio, doc, text, found = best
    start, end = text.original_span(found.start, found.end)
    return QuoteCheck(rule.id, "fuzzy", ratio, doc.name, _collapse(doc.text[start:end]))


def check_grounding(ledger: RuleLedger, bundle: BriefBundle) -> list[str]:
    """Problems with the ledger's quotes, one readable string each; an empty list means every quote is grounded.

    A quote is grounded when its normalised text is a substring of the normalised brief text (or matches a
    same-length window with a difflib ratio of at least 0.9). Quotes that are empty or found nowhere are reported
    with the rule id and the start of the quote.
    """
    problems: list[str] = []
    report = ground_ledger(ledger, bundle)
    by_id = {rule.id: rule for rule in ledger.rules}
    for check in report.ungrounded:
        rule = by_id.get(check.rule_id)
        quote = _collapse(rule.quote) if rule else ""
        if check.status == "empty":
            problems.append(f"{check.rule_id}: empty quote")
        else:
            shown = quote if len(quote) <= 90 else quote[:87] + "..."
            problems.append(f"{check.rule_id}: quote not found in the briefs: {shown!r}")
    return problems


def snap_quotes(ledger: RuleLedger, bundle: BriefBundle) -> tuple[RuleLedger, list[str]]:
    """Return a copy of ``ledger`` whose near-miss quotes are replaced by the exact source text.

    Quotes found only fuzzily are replaced by the matching passage of the briefs (white space collapsed); quotes that
    exist in another file than their ``source`` get the right ``source``. Missing quotes are left untouched (the
    caller decides what to do with them). The second item lists what was changed, for the trace.
    """
    report = ground_ledger(ledger, bundle)
    verdicts = {check.rule_id: check for check in report.checks}
    changes: list[str] = []
    rules: list[RuleEntry] = []
    for rule in ledger.rules:
        check = verdicts.get(rule.id)
        update: dict[str, str] = {}
        if check is not None and check.status == "fuzzy" and check.original:
            update["quote"] = check.original
            if check.found_in and not _source_is(bundle, rule.source, check.found_in):
                update["source"] = check.found_in
            changes.append(f"{rule.id}: quote snapped to the source text (ratio {check.ratio:.2f})")
        elif check is not None and check.status == "moved" and check.found_in:
            update["source"] = check.found_in
            changes.append(f"{rule.id}: source corrected to {check.found_in}")
        rules.append(rule.model_copy(update=update) if update else rule)
    return ledger.model_copy(update={"rules": rules}), changes


def _source_is(bundle: BriefBundle, source: str, name: str) -> bool:
    return any(_doc_matches(doc, source) and doc.name == name for doc in bundle.documents)


# --------------------------------------------------------------------------------------------------------------------
# Coverage
# --------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Passage:
    """A line (or sentence of a long line) of a brief that no ledger quote overlaps."""

    source: str
    line: int
    text: str

    def __str__(self) -> str:
        return f"{self.source}:{self.line}: {self.text}"


def _words(text: str) -> list[str]:
    return _WORD.findall(normalize_text(text))


def _shingles(words: list[str]) -> set[tuple[str, ...]]:
    if len(words) < 3:
        return {(word,) for word in words}
    return {tuple(words[i : i + 3]) for i in range(len(words) - 2)}


def _is_skippable(line: str) -> bool:
    if not line.strip() or _HEADING.match(line) or _FILE_MARK.match(line):
        return True
    return bool(_TABLE_RULE.match(line))


def uncovered_passages(
    ledger: RuleLedger, bundle: BriefBundle, *, min_words: int = 4, threshold: float = 0.6
) -> list[Passage]:
    """Lines of the briefs that no ledger quote covers.

    A line is covered when at least ``threshold`` of its word trigrams occur in some ledger quote (superseded rules
    count: they are in the ledger). Headings, blank lines, table separators and lines of fewer than ``min_words``
    words are ignored; long lines are checked sentence by sentence. The result is a worklist for the completeness
    pass: most entries are harmless prose, but a missing rule shows up here.
    """
    quote_shingles: set[tuple[str, ...]] = set()
    quote_words: set[str] = set()
    for rule in ledger.rules:
        words = _words(rule.quote)
        quote_words.update(words)
        quote_shingles.update(_shingles(words))
    out: list[Passage] = []
    for doc in bundle.documents:
        for number, line in enumerate(doc.text.split("\n"), start=1):
            if _is_skippable(line):
                continue
            body = _LIST_MARK.sub("", line)
            pieces = _SENTENCE_SPLIT.split(body) if len(body) > 360 else [body]
            for piece in pieces:
                words = _words(piece)
                if len(words) < min_words:
                    continue
                if _coverage(words, quote_shingles, quote_words) < threshold:
                    out.append(Passage(doc.name, number, _collapse(piece)))
    return out


def _coverage(words: list[str], quote_shingles: set[tuple[str, ...]], quote_words: set[str]) -> float:
    shingles = _shingles(words)
    if not shingles:
        return 1.0
    if all(len(s) == 1 for s in shingles):
        return sum(1 for s in shingles if s[0] in quote_words) / len(shingles)
    return len(shingles & quote_shingles) / len(shingles)


# --------------------------------------------------------------------------------------------------------------------
# Fixed sentences of the IR
# --------------------------------------------------------------------------------------------------------------------


def check_phrase_grounding(ir: PolicyIR, bundle: BriefBundle, *, min_ratio: float = 0.8) -> list[str]:
    """Fixed sentences of the IR that cannot be found in the briefs (probably invented), as readable strings.

    Placeholders such as ``{title}`` are cut out and the remaining fragments must each be found (exact or fuzzy at
    ``min_ratio``) in some document. Only the sentences the IR claims the brief prescribes are checked: the fields
    of ``phrases``, ``recording`` sentences, the identity questions, the confirmation question and the persuasion
    first ask. An empty list means every checked sentence is grounded.
    """
    texts = [normalize_with_offsets(doc.text) for doc in bundle.documents]
    candidates: list[tuple[str, str]] = []
    phrases = ir.phrases
    for name in type(phrases).model_fields:
        value = getattr(phrases, name)
        if isinstance(value, str) and value.strip():
            candidates.append((f"phrases.{name}", value))
        elif isinstance(value, dict):
            candidates.extend((f"phrases.extra.{k}", v) for k, v in value.items() if str(v).strip())
    for name in ("failure_sentence", "success_sentence", "immutable_sentence"):
        candidates.append((f"recording.{name}", getattr(ir.recording, name)))
    candidates.extend((f"identity.questions[{i}].ask", q.ask) for i, q in enumerate(ir.identity.questions))
    candidates.append(("confirmation.question", ir.confirmation.question))
    candidates.append(("persuasion.first_ask", ir.persuasion.first_ask))
    problems: list[str] = []
    for path, sentence in candidates:
        if not sentence.strip():
            continue
        if not _sentence_grounded(sentence, texts, min_ratio):
            shown = sentence if len(sentence) <= 90 else sentence[:87] + "..."
            problems.append(f"{path}: sentence not found in the briefs: {shown!r}")
    return problems


def _sentence_grounded(sentence: str, texts: list[NormalizedText], min_ratio: float) -> bool:
    """True when every placeholder-free fragment of ``sentence`` occurs in some document."""
    holes = re.compile(r"\{\{?[A-Za-z_][A-Za-z0-9_]*\}?\}")
    fragments = [normalize_text(part) for part in holes.split(sentence)]
    fragments = [f for f in fragments if len(f) >= 4]
    if not fragments:
        return True
    return all(any(find_in_text(f, text, min_ratio=min_ratio) is not None for text in texts) for f in fragments)
