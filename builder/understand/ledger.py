"""Step 1 of understanding a brief: the rule ledger.

The ledger is the audit trail of the whole build: one entry per *atomic* rule of the input documents, each with a
verbatim quote, its source file, a category, the agent stages it applies to and its supersession relations. The policy
IR cites ledger ids; the compiler places the quoted text in the node that enforces it; coverage and the walkthrough
audit are computed against it.

Why several calls. A structured answer is capped at 16 000 tokens, while a dense brief yields 100+ rules with
quotes. :func:`build_rule_ledger` therefore splits every document into *passages* (paragraph boundaries, at most
``max_chunk_chars`` characters) and asks for the rules of one passage per call, always giving the model the *full* text
of all files as context ("read everything", cross references and amendments stay visible). The calls run concurrently.
Because outputs are bounded per passage, no call can be truncated by the output cap.

Pipeline (each later step is skipped when it has nothing to do):

1. **Extract** - one ``RuleDraft`` list per passage (prompt ``understand/rule_ledger.v1.j2``). A model answer whose
   fields ran on into raw JSON or other garbage (a statement thousands of characters long, a ``"statement":`` key inside
   a quote) is asked for again once; what stays malformed is cleaned (a clean quote replaces a garbled statement, an
   unsalvageable draft is dropped) and traced, never kept as it is.
2. **Merge** - ids made unique, drafts become :class:`~builder.policy.ir.RuleEntry` with their source file.
3. **Ground** - quotes are checked against the documents (:mod:`builder.understand.grounding`); near misses are
   snapped to the exact source text; quotes found nowhere go to a *re-quote* call, and rules the model cannot
   re-quote are dropped as invented (``understand/ledger_requote.v1.j2``).
4. **Complete** - lines of the documents that no quote covers are shown to the model once more
   (``understand/ledger_complete.v1.j2``); skipped rules are added, an empty answer is fine.
5. **Reconcile** - with several documents (or amendment cue words in one), a call links amending rules to the rules
   they replace and adds cross-document ambiguities (``understand/ledger_reconcile.v1.j2``). A link is either a full
   replacement (the old rule becomes superseded) or *partial* (the old rule stays in force and lists the amending rule
   in ``narrowed_by``). File order is not precedence: the texts decide.
6. **Normalise** - supersession links made two-sided, dangling references dropped, ambiguity ids unique.

Ambiguities and supersessions are written to the trace so a reviewer can follow every interpretation the builder made.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from builder.guardrails.input_guard import escape_untrusted, wrap_bundle, wrap_untrusted
from builder.llm.providers import LLMRole
from builder.policy.ir import (
    RULE_CATEGORIES,
    STAGE_SCOPES,
    Ambiguity,
    RuleEntry,
    RuleLedger,
)
from builder.prompts import render_prompt_parts
from builder.understand._common import (
    MAX_OUTPUT_TOKENS,
    LLMLike,
    TraceLike,
    UnderstandError,
    call_structured,
    trace_ambiguity,
    trace_decision,
    trace_error,
    trace_event,
)
from builder.understand.grounding import (
    ground_ledger,
    normalize_text,
    snap_quotes,
    uncovered_passages,
)

if TYPE_CHECKING:
    from builder.ingest.brief_loader import BriefBundle
    from builder.ingest.tool_api import ToolApi

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_CHUNK_CHARS",
    "LedgerChunk",
    "LedgerPart",
    "LedgerReconciliation",
    "RuleDraft",
    "SupersessionLink",
    "build_rule_ledger",
    "document_prefixes",
    "ledger_prompt_lines",
    "plan_chunks",
]

DEFAULT_CHUNK_CHARS = 5500
"""Largest passage (characters) whose rules are extracted in one call: about 40-50 rules, well below the output cap."""

_RECONCILE_CUES = re.compile(
    r"\b(replace[sd]?|replacing|supersed\w*|overrid\w*|no longer|amend\w*|correction|disregard|"
    r"ignore (?:the|what|my|all|any) (?:previous|earlier|above|old)|from now on|cancels?|void)\b",
    re.IGNORECASE,
)
_FILLER_WORDS = frozenset(
    {"brief", "memo", "email", "mail", "policy", "rules", "rule", "doc", "document", "note", "notes", "amendment",
     "update", "bank", "collections", "collection", "spec", "specification", "guide", "instructions", "file"}
)
_JSON_KEY = re.compile(r'"(?:id|quote|category|statement|scope|supersedes|rules|ambiguities)"\s*:|\\"(?:id|quote|statement)\\"')
_FOREIGN_BRACKETS = re.compile(r"[\u3010\u3011\uff1a\uff0c\u300c\u300d]")  # CJK / full-width brackets, colon, comma
_MAX_STATEMENT_CHARS = 700
_STATEMENT_TO_QUOTE_RATIO = 4
_COMPLETENESS_BATCH_CHARS = 4500
_COMPLETENESS_MAX_CALLS = 3
_MAX_UNGROUNDED_SHARE = 0.4


# --------------------------------------------------------------------------------------------------------------------
# LLM answer models (lenient: unknown keys ignored, enum values normalised)
# --------------------------------------------------------------------------------------------------------------------

_SCOPE_ALIASES = {
    "all": "global", "any": "global", "everywhere": "global", "general": "global",
    "negotiate": "negotiation", "negotiating": "negotiation", "payment": "negotiation", "persuasion": "negotiation",
    "callback": "third_party", "thirdparty": "third_party", "third-party": "third_party", "relative": "third_party",
    "verification": "identity", "security": "identity",
    "debt": "disclosure", "account": "disclosure",
    "close": "closing", "closure": "closing", "end": "closing",
    "confirm": "confirmation", "record": "recording",
}  # fmt: skip
_CATEGORY_ALIASES = {
    "right-party": "right_party", "rightparty": "right_party", "third-party": "third_party", "thirdparty": "third_party",
    "promise-date": "promise_date", "promisedate": "promise_date", "multi-product": "multi_product",
    "multiproduct": "multi_product", "style": "conduct", "behaviour": "conduct", "behavior": "conduct",
    "dates": "calendar", "date": "calendar", "amounts": "amount", "intents": "intent", "objection": "intent",
    "outcome": "closing", "outcomes": "closing", "verification": "identity", "greeting": "opening",
}  # fmt: skip


class RuleDraft(BaseModel):
    """One rule as the model writes it (the source file is added by the builder)."""

    model_config = ConfigDict(extra="ignore")

    id: str = ""
    quote: str = ""
    category: str = Field(default="other", json_schema_extra={"enum": list(RULE_CATEGORIES)})
    statement: str = ""
    scope: list[str] = Field(
        default_factory=lambda: ["global"],
        json_schema_extra={"items": {"type": "string", "enum": list(STAGE_SCOPES)}},
    )
    supersedes: list[str] = Field(default_factory=list)

    @field_validator("category", mode="before")
    @classmethod
    def _category(cls, value: Any) -> str:
        text = str(value or "other").strip().lower().replace(" ", "_")
        text = _CATEGORY_ALIASES.get(text, text)
        return text if text in RULE_CATEGORIES else "other"

    @field_validator("scope", mode="before")
    @classmethod
    def _scope(cls, value: Any) -> list[str]:
        items = [value] if isinstance(value, str) else list(value or [])
        cleaned: list[str] = []
        for item in items:
            text = _SCOPE_ALIASES.get(str(item).strip().lower().replace(" ", "_"), str(item).strip().lower())
            text = text.replace(" ", "_")
            if text in STAGE_SCOPES and text not in cleaned:
                cleaned.append(text)
        return cleaned or ["global"]

    @field_validator("supersedes", mode="before")
    @classmethod
    def _supersedes(cls, value: Any) -> list[str]:
        return [value] if isinstance(value, str) else list(value or [])


class AmbiguityDraft(BaseModel):
    """An ambiguity as the model writes it."""

    model_config = ConfigDict(extra="ignore")

    id: str = ""
    rule_ids: list[str] = Field(default_factory=list)
    question: str = ""
    options: list[str] = Field(default_factory=list)
    chosen: str = ""
    rationale: str = ""


class LedgerPart(BaseModel):
    """The model's answer for one passage: its rules and the ambiguities it saw."""

    model_config = ConfigDict(extra="ignore")

    rules: list[RuleDraft] = Field(default_factory=list)
    ambiguities: list[AmbiguityDraft] = Field(default_factory=list)


class SupersessionLink(BaseModel):
    """``new_rule`` replaces every rule in ``replaces`` - or only narrows them when ``partial``.

    A partial link means the old rule stays in force except in the cases ``new_rule`` covers (``note`` names them): the
    old rule is not superseded, it lists ``new_rule`` in ``narrowed_by``.
    """

    model_config = ConfigDict(extra="ignore")

    new_rule: str
    replaces: list[str] = Field(default_factory=list)
    note: str = ""
    partial: bool = False


class LedgerReconciliation(BaseModel):
    """The model's answer for the reconcile step."""

    model_config = ConfigDict(extra="ignore")

    links: list[SupersessionLink] = Field(default_factory=list)
    ambiguities: list[AmbiguityDraft] = Field(default_factory=list)


class Requote(BaseModel):
    """One re-quoted rule: ``found`` False means the documents hold no such rule."""

    model_config = ConfigDict(extra="ignore")

    id: str
    found: bool = False
    quote: str = ""


class RequoteResult(BaseModel):
    """The model's answer for the re-quote step."""

    model_config = ConfigDict(extra="ignore")

    items: list[Requote] = Field(default_factory=list)


# --------------------------------------------------------------------------------------------------------------------
# Passage planning
# --------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class LedgerChunk:
    """A passage of one document whose rules are extracted in one call."""

    index: int
    total: int
    source: str
    prefix: str
    text: str
    first_line: int


def document_prefixes(bundle: BriefBundle) -> list[str]:
    """One short id prefix per document of ``bundle`` (same order), unique across the bundle.

    Derived from the file name: filler words (``brief``, ``memo``, ``policy`` ...) are skipped and the first remaining
    word gives the prefix (up to three letters, upper case): ``brief-a.md`` becomes ``A``. Names without a usable word
    get ``D<position>``.
    """
    used: set[str] = set()
    out: list[str] = []
    for position, doc in enumerate(bundle.documents, start=1):
        words = [w for w in re.split(r"[^A-Za-z0-9]+", Path(doc.name).stem) if w and w.lower() not in _FILLER_WORDS]
        head = words[0] if words else ""
        base = head[:3].upper() if head and not head.isdigit() else f"D{position}"
        candidate, counter = base, 2
        while candidate in used:
            candidate = f"{base}{counter}"
            counter += 1
        used.add(candidate)
        out.append(candidate)
    return out


def _blocks(lines: list[str]) -> list[tuple[int, list[str]]]:
    """Group ``lines`` into paragraph blocks (separated by blank lines) as ``(first line number, lines)``."""
    blocks: list[tuple[int, list[str]]] = []
    current: list[str] = []
    start = 1
    for number, line in enumerate(lines, start=1):
        if line.strip():
            if not current:
                start = number
            current.append(line)
        elif current:
            blocks.append((start, current))
            current = []
    if current:
        blocks.append((start, current))
    return blocks


def _split_line(line: str, limit: int) -> list[str]:
    """Cut one over-long line at sentence ends (or at a space when a sentence is itself too long)."""
    if len(line) <= limit:
        return [line]
    pieces: list[str] = []
    current = ""
    for sentence in re.split(r"(?<=[.!?;])\s+", line):
        while len(sentence) > limit:
            cut = sentence.rfind(" ", 0, limit)
            cut = cut if cut > 0 else limit
            if current:
                pieces.append(current)
                current = ""
            pieces.append(sentence[:cut])
            sentence = sentence[cut:].lstrip()
        if current and len(current) + 1 + len(sentence) > limit:
            pieces.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        pieces.append(current)
    return pieces


def _split_block(first: int, block: list[str], limit: int) -> list[tuple[int, list[str]]]:
    """Split a block longer than ``limit`` characters at line boundaries (over-long lines at sentence ends)."""
    pieces: list[tuple[int, list[str]]] = []
    current: list[str] = []
    size = 0
    start = first
    for offset, original in enumerate(block):
        for line in _split_line(original, limit):
            if current and size + len(line) + 1 > limit:
                pieces.append((start, current))
                current, size, start = [], 0, first + offset
            current.append(line)
            size += len(line) + 1
    if current:
        pieces.append((start, current))
    return pieces


def plan_chunks(bundle: BriefBundle, max_chars: int = DEFAULT_CHUNK_CHARS) -> list[LedgerChunk]:
    """Split every document into passages of at most ``max_chars`` characters (whole paragraphs where possible).

    Chunks never cross documents. A heading starts a new chunk once the current one is more than half full, so
    sections stay together. A document that fits in ``max_chars`` is one chunk.
    """
    prefixes = document_prefixes(bundle)
    raw: list[tuple[str, str, int, str]] = []  # source, prefix, first line, text
    for doc, prefix in zip(bundle.documents, prefixes, strict=True):
        if not doc.text.strip():
            continue
        if len(doc.text) <= max_chars:
            raw.append((doc.name, prefix, 1, doc.text.strip("\n")))
            continue
        pieces: list[tuple[int, list[str]]] = []
        for first, block in _blocks(doc.text.split("\n")):
            if sum(len(line) + 1 for line in block) > max_chars:
                pieces.extend(_split_block(first, block, max_chars))
            else:
                pieces.append((first, block))
        current: list[str] = []
        current_first = 1
        size = 0
        for first, block in pieces:
            text = "\n".join(block)
            is_heading = block[0].lstrip().startswith("#")
            too_big = current and size + len(text) + 2 > max_chars
            heading_break = current and is_heading and size > max_chars * 7 // 10
            if too_big or heading_break:
                raw.append((doc.name, prefix, current_first, "\n\n".join(current)))
                current, size = [], 0
            if not current:
                current_first = first
            current.append(text)
            size += len(text) + 2
        if current:
            raw.append((doc.name, prefix, current_first, "\n\n".join(current)))
    total = len(raw)
    return [
        LedgerChunk(index=i, total=total, source=src, prefix=prefix, text=text, first_line=line)
        for i, (src, prefix, line, text) in enumerate(raw, start=1)
    ]


# --------------------------------------------------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------------------------------------------------


async def build_rule_ledger(
    llm: LLMLike,
    bundle: BriefBundle,
    tool_api: ToolApi,
    trace: TraceLike | None,
    *,
    deadline: float | None = None,
    optional_deadline: float | None = None,
    max_chunk_chars: int = DEFAULT_CHUNK_CHARS,
    max_parallel: int = 3,
) -> RuleLedger:
    """Read every file of ``bundle`` and return its :class:`RuleLedger` (see the module docstring for the steps).

    Args:
        llm: The LLM gateway (anything with an async ``structured`` method).
        bundle: The loaded brief documents; they are read in full.
        tool_api: The tool contracts, shown to the model as vocabulary for rule statements.
        trace: Run trace; every ambiguity and supersession is written to it. ``None`` silences tracing.
        deadline: ``time.monotonic()`` value after which the gateway starts no new attempt.
        optional_deadline: The same for the optional calls whose failure is swallowed (re-quote, completeness check,
            amendment links); defaults to ``deadline``. An earlier value keeps a slow provider on an optional call from
            leaving the mandatory stages after it without time (audit AUD-53).
        max_chunk_chars: Passage size limit (characters) for one extraction call.
        max_parallel: How many extraction calls may run at the same time.

    Returns:
        The ledger: rules in document order, two-sided supersession links, ambiguity register, source file names.

    Raises:
        UnderstandError: The model produced no rule at all.
        builder.llm.errors.AllProvidersFailed: An extraction call failed on every provider.
    """
    ctx = _Context(llm, bundle, tool_api, trace, deadline)
    ctx.optional_deadline = optional_deadline if optional_deadline is not None else deadline
    chunks = plan_chunks(bundle, max_chunk_chars)
    trace_event(trace, "ledger", f"reading {len(bundle.documents)} file(s) in {len(chunks)} passage(s)",
                files=bundle.names, passages=len(chunks))
    parts = await _extract_parts(ctx, chunks, max_parallel)
    ledger = _merge_parts(chunks, parts, bundle)
    if not ledger.rules:
        raise UnderstandError("the rule ledger is empty: the model found no rule in the input documents")
    ledger = await _ground(ctx, ledger)
    ledger = await _complete(ctx, ledger)
    ledger = await _reconcile(ctx, ledger)
    ledger = _normalise(ledger, bundle, trace)
    _report(ledger, trace)
    return ledger


class _Context:
    """What the pipeline steps share."""

    def __init__(self, llm: LLMLike, bundle: BriefBundle, tool_api: ToolApi, trace: TraceLike | None,
                 deadline: float | None) -> None:
        self.llm = llm
        self.bundle = bundle
        self.trace = trace
        self.deadline = deadline
        self.optional_deadline = deadline  # optional calls (re-quote, completeness, reconcile); see build_rule_ledger
        self.full_text = wrap_bundle(bundle, labeled=True)  # what the prompts show: documents as untrusted data
        self.source_text = bundle.combined_text()  # the plain text, for checks against what the model wrote
        self.tool_api_text = tool_api.describe() if tool_api is not None else ""
        self.prefixes = document_prefixes(bundle)


# --------------------------------------------------------------------------------------------------------------------
# Step 1-2: extract and merge
# --------------------------------------------------------------------------------------------------------------------


_RETRY_NOTE = (
    "Your previous answer for this passage was malformed: a rule field ran on into raw JSON or other text that is not "
    "part of one rule ({problems}). Answer again. Every rule gets its own entry; \"quote\" and \"statement\" hold plain "
    "text of that one rule only (a statement is one short sentence), never JSON syntax, brackets of other scripts or "
    "the next rule."
)


def draft_problem(draft: RuleDraft, source_text: str) -> str:
    """Why ``draft`` looks like corrupted model output (``""`` when it looks fine).

    Corruption is a field that runs on past the end of the rule: a statement far longer than its quote, raw JSON keys
    (``"statement":``) or full-width brackets inside a quote or statement. Text the documents really contain is not
    corruption, so a pattern counts only when ``source_text`` does not contain it too.
    """
    problems: list[str] = []
    statement = draft.statement.strip()
    quote = draft.quote.strip()
    if len(statement) > max(_MAX_STATEMENT_CHARS, _STATEMENT_TO_QUOTE_RATIO * len(quote)):
        problems.append(f"the statement is {len(statement)} characters long")
    for label, text in (("statement", statement), ("quote", quote)):
        for pattern, what in ((_JSON_KEY, "a raw JSON key"), (_FOREIGN_BRACKETS, "full-width brackets")):
            found = pattern.search(text)
            if found is not None and not pattern.search(source_text):
                problems.append(f"the {label} holds {what} ({found.group(0)!r})")
    return "; ".join(problems)


def _clean_draft(draft: RuleDraft, source_text: str) -> RuleDraft | None:
    """``draft`` made usable: the garbled statement is dropped (the quote stands in); ``None`` when no quote is left."""
    quote = draft.quote.strip()
    for pattern in (_JSON_KEY, _FOREIGN_BRACKETS):
        found = pattern.search(quote)
        if found is not None and not pattern.search(source_text):
            quote = quote[: found.start()].rstrip(" \t\n\"',;[{(")
    if len(quote) < 8:
        return None
    return draft.model_copy(update={"quote": quote, "statement": ""})


async def _extract_parts(ctx: _Context, chunks: list[LedgerChunk], max_parallel: int) -> list[LedgerPart]:
    gate = asyncio.Semaphore(max(1, max_parallel))

    async def ask(chunk: LedgerChunk, retry_note: str) -> LedgerPart:
        prompt = render_prompt_parts(
            "understand/rule_ledger.v1.j2",
            files=ctx.bundle.names,
            tool_api_text=ctx.tool_api_text,
            prefix=chunk.prefix,
            source=chunk.source,
            part_index=chunk.index,
            part_total=chunk.total,
            full_text=ctx.full_text,
            target_text=wrap_untrusted(chunk.source, chunk.text),
            target_is_all=chunk.total == 1,
            categories=list(RULE_CATEGORIES),
            scopes=list(STAGE_SCOPES),
            retry_note=retry_note,
        )
        again = " (again)" if retry_note else ""
        async with gate:
            return await call_structured(
                ctx.llm,
                purpose=f"rule ledger: {chunk.source} passage {chunk.index}/{chunk.total}{again}",
                schema=LedgerPart,
                system=prompt.system,
                user=prompt.user,
                role=LLMRole.STRONG,
                max_tokens=MAX_OUTPUT_TOKENS,
                deadline=ctx.deadline,
            )

    def malformed(part: LedgerPart) -> dict[int, str]:
        found = {index: draft_problem(draft, ctx.source_text) for index, draft in enumerate(part.rules)}
        return {index: why for index, why in found.items() if why}

    async def one(chunk: LedgerChunk) -> LedgerPart:
        part = await ask(chunk, "")
        bad = malformed(part)
        if bad:
            where = f"passage {chunk.index}/{chunk.total} ({chunk.source})"
            reasons = "; ".join(sorted(set(bad.values())))
            trace_error(ctx.trace, f"{where}: {len(bad)} malformed rule(s) in the model's answer ({reasons[:300]}); "
                        "asking for the passage again", rule_ids=[part.rules[i].id for i in bad])
            part = await ask(chunk, _RETRY_NOTE.format(problems=reasons[:300]))
            bad = malformed(part)
            if bad:
                kept: list[RuleDraft] = []
                for index, draft in enumerate(part.rules):
                    if index not in bad:
                        kept.append(draft)
                        continue
                    cleaned = _clean_draft(draft, ctx.source_text)
                    outcome = "its statement was replaced by its quote" if cleaned else "it was dropped"
                    trace_error(ctx.trace, f"{where}: rule {draft.id or '?'} is still malformed "
                                f"({bad[index][:200]}); {outcome}", rule_id=draft.id)
                    if cleaned is not None:
                        kept.append(cleaned)
                part = part.model_copy(update={"rules": kept})
        trace_event(ctx.trace, "ledger", f"passage {chunk.index}/{chunk.total} ({chunk.source}): "
                    f"{len(part.rules)} rules, {len(part.ambiguities)} ambiguities")
        return part

    results = await asyncio.gather(*(one(chunk) for chunk in chunks), return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException):
            raise result
    return [r for r in results if isinstance(r, LedgerPart)]


def _merge_parts(chunks: list[LedgerChunk], parts: list[LedgerPart], bundle: BriefBundle) -> RuleLedger:
    rules: list[RuleEntry] = []
    ambiguities: list[AmbiguityDraft] = []
    seen_quote: dict[str, str] = {}
    counter = 0
    for chunk, part in zip(chunks, parts, strict=True):
        for draft in part.rules:
            quote = draft.quote.strip()
            if not quote:
                continue
            counter += 1
            rule_id = draft.id.strip() or f"{chunk.prefix}-X{counter}"
            norm = normalize_text(quote)
            if rule_id in seen_quote:
                if seen_quote[rule_id] == norm:
                    continue  # the same rule twice
                n = 2
                while f"{rule_id}-dup{n}" in seen_quote:
                    n += 1
                rule_id = f"{rule_id}-dup{n}"
            seen_quote[rule_id] = norm
            rules.append(
                RuleEntry(
                    id=rule_id,
                    source=chunk.source,
                    quote=quote,
                    category=draft.category,  # type: ignore[arg-type]  # validated by RuleDraft
                    statement=draft.statement.strip() or quote,
                    scope=draft.scope,  # type: ignore[arg-type]
                    supersedes=[s for s in draft.supersedes if s != rule_id],
                )
            )
        ambiguities.extend(part.ambiguities)
    ledger = RuleLedger(rules=rules, source_files=bundle.names)
    return ledger.model_copy(update={"ambiguities": _finish_ambiguities(ambiguities, ledger, [])})


def _finish_ambiguities(drafts: list[AmbiguityDraft], ledger: RuleLedger, existing: list[Ambiguity]) -> list[Ambiguity]:
    """Turn drafts into valid, uniquely numbered ambiguities that follow ``existing`` ones."""
    known = set(ledger.ids())
    out = list(existing)
    taken = {a.id for a in out}
    for draft in drafts:
        if not draft.question.strip() or not draft.chosen.strip():
            continue
        options = [o for o in draft.options if o.strip()]
        if draft.chosen not in options:
            options.append(draft.chosen)
        ident = draft.id.strip() or f"AMB-{len(out) + 1}"
        while ident in taken:
            ident = f"{ident}x"
        taken.add(ident)
        out.append(
            Ambiguity(
                id=ident,
                rule_ids=[r for r in draft.rule_ids if r in known],
                question=draft.question.strip(),
                options=options,
                chosen=draft.chosen.strip(),
                rationale=draft.rationale.strip() or "closest to the wording of the documents",
            )
        )
    return out


# --------------------------------------------------------------------------------------------------------------------
# Step 3: grounding
# --------------------------------------------------------------------------------------------------------------------


async def _ground(ctx: _Context, ledger: RuleLedger) -> RuleLedger:
    ledger, changes = snap_quotes(ledger, ctx.bundle)
    for line in changes:
        trace_event(ctx.trace, "grounding", line)
    report = ground_ledger(ledger, ctx.bundle)
    bad = report.ungrounded
    if not bad:
        return ledger
    if len(bad) > _MAX_UNGROUNDED_SHARE * len(ledger.rules):
        trace_error(ctx.trace, f"{len(bad)} of {len(ledger.rules)} quotes are not found in the documents; "
                    "grounding is unreliable for this input, the quotes are kept as written",
                    rule_ids=[c.rule_id for c in bad])
        return ledger
    by_id = {rule.id: rule for rule in ledger.rules}
    items = [{"id": c.rule_id, "statement": by_id[c.rule_id].statement,
              "quote": " ".join(by_id[c.rule_id].quote.split())} for c in bad if c.rule_id in by_id]
    prompt = render_prompt_parts("understand/ledger_requote.v1.j2", files=ctx.bundle.names, full_text=ctx.full_text,
                                 items=items)
    try:
        answer = await call_structured(ctx.llm, purpose="rule ledger: re-quote unfound quotes", schema=RequoteResult,
                                       system=prompt.system, user=prompt.user, role=LLMRole.FAST,
                                       max_tokens=MAX_OUTPUT_TOKENS, deadline=ctx.optional_deadline)
    except Exception as exc:  # the ledger is still usable; report instead of failing the build
        trace_error(ctx.trace, f"re-quote call failed ({type(exc).__name__}); unfound quotes are kept", error=str(exc)[:300])
        return ledger
    fixes = {item.id: item for item in answer.items}
    kept: list[RuleEntry] = []
    for rule in ledger.rules:
        if rule.id not in {c.rule_id for c in bad}:
            kept.append(rule)
            continue
        item = fixes.get(rule.id)
        if item is not None and item.found and item.quote.strip():
            kept.append(rule.model_copy(update={"quote": item.quote.strip()}))
        else:
            trace_error(ctx.trace, f"rule {rule.id} dropped: its quote is not in the documents (invented?)",
                        rule_id=rule.id, quote=rule.quote[:200])
    ledger = ledger.model_copy(update={"rules": kept})
    ledger, changes = snap_quotes(ledger, ctx.bundle)
    still = {c.rule_id for c in ground_ledger(ledger, ctx.bundle).ungrounded}
    if still:
        trace_error(ctx.trace, f"re-quoted rules still not found, dropped: {', '.join(sorted(still))}",
                    rule_ids=sorted(still))
        ledger = ledger.model_copy(update={"rules": [r for r in ledger.rules if r.id not in still]})
    return ledger


# --------------------------------------------------------------------------------------------------------------------
# Step 4: completeness
# --------------------------------------------------------------------------------------------------------------------


async def _complete(ctx: _Context, ledger: RuleLedger) -> RuleLedger:
    passages = uncovered_passages(ledger, ctx.bundle)
    if not passages:
        return ledger
    by_source: dict[str, list[str]] = {doc.name: doc.text.split("\n") for doc in ctx.bundle.documents}
    prefix_of = {doc.name: pre for doc, pre in zip(ctx.bundle.documents, ctx.prefixes, strict=True)}
    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    size = 0
    for p in passages:
        lines = by_source.get(p.source, [])
        before = ""
        for k in range(p.line - 2, -1, -1):
            if k < len(lines) and lines[k].strip():
                before = lines[k].strip()[:160]
                break
        entry = {"source": p.source, "line": p.line, "text": p.text, "before": before}
        if current and size + len(p.text) > _COMPLETENESS_BATCH_CHARS:
            batches.append(current)
            current, size = [], 0
        current.append(entry)
        size += len(p.text)
    if current:
        batches.append(current)
    if len(batches) > _COMPLETENESS_MAX_CALLS:
        trace_error(ctx.trace, f"{len(passages)} passages are not covered by the ledger; only the first "
                    f"{_COMPLETENESS_MAX_CALLS} batches are checked", passages=len(passages))
        batches = batches[:_COMPLETENESS_MAX_CALLS]
    added: list[RuleEntry] = []
    drafts_amb: list[AmbiguityDraft] = []
    known_ids = {rule.id for rule in ledger.rules}
    for number, batch in enumerate(batches, start=1):
        hint = prefix_of.get(batch[0]["source"], "X")
        prompt = render_prompt_parts(
            "understand/ledger_complete.v1.j2", files=ctx.bundle.names, prefix_hint=hint,
            passages=[{**e, "text": escape_untrusted(e["text"]), "before": escape_untrusted(e["before"])} for e in batch],
            categories=list(RULE_CATEGORIES), scopes=list(STAGE_SCOPES), known_ids=", ".join(sorted(known_ids)),
        )
        try:
            part = await call_structured(
                ctx.llm, purpose=f"rule ledger: completeness check {number}/{len(batches)}", schema=LedgerPart,
                system=prompt.system, user=prompt.user, role=LLMRole.STRONG, max_tokens=MAX_OUTPUT_TOKENS,
                deadline=ctx.optional_deadline,
            )
        except Exception as exc:
            trace_error(ctx.trace, f"completeness call failed ({type(exc).__name__}); ledger kept as it is",
                        error=str(exc)[:300])
            break
        for draft in part.rules:
            quote = draft.quote.strip()
            if not quote:
                continue
            ident = draft.id.strip() or f"{hint}-M{len(added) + 1}"
            while ident in known_ids:
                ident = f"{ident}x"
            known_ids.add(ident)
            source = _source_of_passage(batch, quote)
            added.append(RuleEntry(id=ident, source=source, quote=quote, category=draft.category,  # type: ignore[arg-type]
                                   statement=draft.statement.strip() or quote, scope=draft.scope))  # type: ignore[arg-type]
        drafts_amb.extend(part.ambiguities)
    if not added and not drafts_amb:
        trace_event(ctx.trace, "ledger", f"completeness check: {len(passages)} uncovered passage(s), no missing rule")
        return ledger
    merged = ledger.model_copy(update={"rules": [*ledger.rules, *added]})
    merged, _ = snap_quotes(merged, ctx.bundle)
    dropped = {c.rule_id for c in ground_ledger(merged, ctx.bundle).ungrounded} & {r.id for r in added}
    if dropped:
        trace_error(ctx.trace, f"completeness rules with unfound quotes dropped: {', '.join(sorted(dropped))}")
        merged = merged.model_copy(update={"rules": [r for r in merged.rules if r.id not in dropped]})
    ambiguities = _finish_ambiguities(drafts_amb, merged, merged.ambiguities)
    trace_event(ctx.trace, "ledger", f"completeness check added {len(added) - len(dropped)} rule(s)",
                rule_ids=[r.id for r in added if r.id not in dropped])
    return merged.model_copy(update={"ambiguities": ambiguities})


def _source_of_passage(batch: list[dict[str, Any]], quote: str) -> str:
    """The file whose uncovered passage contains ``quote`` (first file of the batch when none does)."""
    needle = normalize_text(quote)
    for entry in batch:
        if needle and needle in normalize_text(entry["text"]):
            return str(entry["source"])
    return str(batch[0]["source"])


# --------------------------------------------------------------------------------------------------------------------
# Step 5: reconcile
# --------------------------------------------------------------------------------------------------------------------


def _needs_reconcile(bundle: BriefBundle) -> bool:
    if len(bundle.documents) > 1:
        return True
    return any(_RECONCILE_CUES.search(doc.text) for doc in bundle.documents)


def ledger_prompt_lines(ledger: RuleLedger, *, quote_chars: int = 0, include_superseded: bool = True) -> str:
    """One compact line per rule for prompts: ``id | file | category | scope | statement [| "quote"]``.

    Superseded rules are marked ``[SUPERSEDED by X]`` (or omitted with ``include_superseded=False``); rules that an
    amendment narrows stay in the list, marked ``[NARROWED by X]``. ``quote_chars`` > 0 appends the start of the quote.
    """
    replaced_by: dict[str, str] = {}
    for rule in ledger.rules:
        if rule.superseded_by:
            replaced_by.setdefault(rule.id, rule.superseded_by)
        for old in rule.supersedes:
            replaced_by.setdefault(old, rule.id)
    lines: list[str] = []
    for rule in ledger.rules:
        newer = replaced_by.get(rule.id)
        if newer and not include_superseded:
            continue
        mark = f" [SUPERSEDED by {newer}]" if newer else ""
        if not newer and rule.narrowed_by:
            mark = f" [NARROWED by {', '.join(rule.narrowed_by)}]"
        quote = ""
        if quote_chars > 0:
            flat = " ".join(rule.quote.split())
            quote = f' | "{flat[:quote_chars]}{"..." if len(flat) > quote_chars else ""}"'
        lines.append(f"{rule.id} | {rule.source} | {rule.category} | {','.join(rule.scope)} | "
                     f"{' '.join(rule.statement.split())}{mark}{quote}")
    return "\n".join(lines)


async def _reconcile(ctx: _Context, ledger: RuleLedger) -> RuleLedger:
    if not _needs_reconcile(ctx.bundle):
        return ledger
    prompt = render_prompt_parts(
        "understand/ledger_reconcile.v1.j2",
        files=ctx.bundle.names,
        full_text=ctx.full_text,
        rule_lines=ledger_prompt_lines(ledger),
        existing_ambiguities="\n".join(f"- {a.id}: {a.question}" for a in ledger.ambiguities),
    )
    try:
        answer = await call_structured(
            ctx.llm, purpose="rule ledger: link amendments and cross-document ambiguities",
            schema=LedgerReconciliation, system=prompt.system, user=prompt.user, role=LLMRole.STRONG,
            max_tokens=MAX_OUTPUT_TOKENS, deadline=ctx.optional_deadline,
        )
    except Exception as exc:
        trace_error(ctx.trace, f"reconcile call failed ({type(exc).__name__}); supersession links come from the "
                    "extraction step only", error=str(exc)[:300])
        return ledger
    return _apply_links(ledger, answer)


def _apply_links(ledger: RuleLedger, answer: LedgerReconciliation) -> RuleLedger:
    """Record the model's links as ``supersedes`` entries and in the supersession log.

    Links are applied in the order the model gave them; a link that would close a cycle with the links before it is
    skipped. A partial link supersedes nothing: it lists the amending rule in each old rule's ``narrowed_by`` and writes
    "B-4 narrows A-28: ..." to the log, so the old rule stays active for the cases the amendment does not cover.
    """
    rules = {rule.id: rule.model_copy(deep=True) for rule in ledger.rules}
    replaced_by: dict[str, list[str]] = {}
    for rule in rules.values():
        for old in rule.supersedes:
            replaced_by.setdefault(old, []).append(rule.id)
        if rule.superseded_by:
            replaced_by.setdefault(rule.id, []).append(rule.superseded_by)
    log = list(ledger.supersessions)
    for link in answer.links:
        new = rules.get(link.new_rule)
        if new is None:
            continue
        if link.partial:
            narrowed = [old for old in dict.fromkeys(link.replaces) if old in rules and old != new.id]
            for old in narrowed:
                rules[old].narrowed_by = list(dict.fromkeys([*(rules[old].narrowed_by or []), new.id]))
            if narrowed:
                note = f": {link.note.strip()}" if link.note.strip() else ""
                log.append(f"{new.id} narrows {', '.join(narrowed)}{note} (they stay in force otherwise)")
            continue
        replaced: list[str] = []
        for old in dict.fromkeys(link.replaces):
            if old not in rules or old == new.id or _reaches(replaced_by, new.id, old):
                continue
            replaced_by.setdefault(old, []).append(new.id)
            if old not in new.supersedes:
                new.supersedes.append(old)
            replaced.append(old)
        if replaced:
            note = f": {link.note.strip()}" if link.note.strip() else ""
            log.append(f"{new.id} replaces {', '.join(replaced)}{note}")
    updated = ledger.model_copy(update={"rules": [rules[r.id] for r in ledger.rules], "supersessions": log})
    return updated.model_copy(
        update={"ambiguities": _finish_ambiguities(answer.ambiguities, updated, updated.ambiguities)}
    )


def _resolve_supersessions(rules: list[RuleEntry], trace: TraceLike | None) -> list[RuleEntry]:
    """Make supersession links consistent: known ids only, no self links, no cycles, both fields agree.

    Edges ``old -> new`` come from every rule's ``supersedes`` and ``superseded_by``; an edge that would close a cycle
    is dropped (the earlier edge wins). ``superseded_by`` keeps the first replacing rule; ``supersedes`` lists every
    rule a rule replaces.
    """
    known = {rule.id for rule in rules}
    edges: list[tuple[str, str]] = []
    for rule in rules:
        edges.extend((old, rule.id) for old in rule.supersedes)
        if rule.superseded_by:
            edges.append((rule.id, rule.superseded_by))
    replaced_by: dict[str, list[str]] = {}
    kept: list[tuple[str, str]] = []
    for old, new in dict.fromkeys(edges):
        if old not in known or new not in known or old == new:
            trace_event(trace, "ledger", f"supersession link {old} -> {new} dropped (unknown id or self link)")
            continue
        if _reaches(replaced_by, new, old):
            trace_event(trace, "ledger", f"supersession link {new} replaces {old} dropped (would form a cycle)")
            continue
        replaced_by.setdefault(old, []).append(new)
        kept.append((old, new))
    out: list[RuleEntry] = []
    for rule in rules:
        newer = replaced_by.get(rule.id)
        narrowed = [i for i in dict.fromkeys(rule.narrowed_by or []) if i in known and i != rule.id]
        out.append(
            rule.model_copy(
                update={"supersedes": [old for old, new in kept if new == rule.id],
                        "superseded_by": newer[0] if newer else None,
                        "narrowed_by": narrowed or None}
            )
        )
    return out


def _reaches(replaced_by: dict[str, list[str]], start: str, target: str) -> bool:
    """True when following ``replaced_by`` links from ``start`` reaches ``target``."""
    seen: set[str] = set()
    stack = [start]
    while stack:
        current = stack.pop()
        if current == target:
            return True
        if current in seen:
            continue
        seen.add(current)
        stack.extend(replaced_by.get(current, []))
    return False


def _normalise(ledger: RuleLedger, bundle: BriefBundle, trace: TraceLike | None) -> RuleLedger:
    """Final consistency pass over the assembled ledger."""
    return ledger.model_copy(
        update={"rules": _resolve_supersessions(ledger.rules, trace), "source_files": bundle.names}
    )


def _report(ledger: RuleLedger, trace: TraceLike | None) -> None:
    active = len(ledger.active_rules())
    trace_event(trace, "ledger", f"ledger ready: {len(ledger.rules)} rules ({active} active), "
                f"{len(ledger.ambiguities)} ambiguities, {len(ledger.superseded_ids())} superseded",
                rules=len(ledger.rules), active=active, ambiguities=len(ledger.ambiguities))
    for ambiguity in ledger.ambiguities:
        trace_ambiguity(trace, ambiguity.rule_ids, ambiguity.question, ambiguity.chosen, ambiguity.rationale)
    for line in ledger.supersessions:
        trace_decision(trace, f"supersession: {line}")
    for rule in ledger.rules:
        if rule.superseded_by and not any(rule.id in line for line in ledger.supersessions):
            trace_decision(trace, f"supersession: {rule.superseded_by} replaces {rule.id}")
