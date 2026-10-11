"""Input guard: what enters the build (brief documents and the tool API file) is checked, and wrapped as data.

:func:`check_inputs` runs before any LLM sees a word. It never edits the inputs. Its findings:

========================  =========  ==============================================================================
Check (``guard`` id)      Severity   Meaning
========================  =========  ==============================================================================
``empty``                 block      The briefs contain no text at all (nothing to build from).
``size``                  warn/block > 300 000 characters in total warns, > 3 000 000 blocks (cost, context limits).
``characters``            warn       Control characters, invisible/bidirectional characters, undecodable bytes.
``binary``                block      A document that is binary garbage, not text: an unread zip container, NUL bytes, a
                                     flood of undecodable or control characters (the loader could not convert the file).
``injection``             warn       A sentence that talks to the model instead of describing a bank rule. The text
                                     stays data and is never obeyed (see :func:`wrap_untrusted`).
``secret``                warn       A key-like token (``fak_...``, ``sk-...``) inside a brief or the tool API file.
``tool_url``              block      A tool has no absolute URL (the agent could not call it).
``tool_https``            warn       A tool URL is not https.
``tool_hosts``            warn       The tools do not share one host.
``tool_missing``          warn       One of the seven expected tool names is missing.
========================  =========  ==============================================================================

:func:`wrap_untrusted` marks a document as bank *data* for the prompts of the understand stages; the system prompt
(:mod:`builder.guardrails.prompt_guard`) tells the model never to follow instructions found inside such a block.
"""

from __future__ import annotations

import html
import re
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from builder.guardrails.models import GuardReport
from builder.guardrails.patterns import control_characters, find_injections, find_key_tokens

if TYPE_CHECKING:
    from builder.ingest.brief_loader import BriefBundle
    from builder.ingest.tool_api import ToolApi

__all__ = [
    "BLOCK_CHARS",
    "MAX_FINDINGS_PER_KIND",
    "UNTRUSTED_TAG",
    "WARN_CHARS",
    "check_inputs",
    "escape_untrusted",
    "wrap_bundle",
    "wrap_untrusted",
]

WARN_CHARS = 300_000
"""Total brief characters above which the guard warns (roughly 75k tokens)."""
BLOCK_CHARS = 3_000_000
"""Total brief characters above which the guard blocks."""
MAX_FINDINGS_PER_KIND = 12
"""Findings of one kind reported per document before the rest is summarised in one line."""
UNTRUSTED_TAG = "untrusted_document"

_CLOSING_TAG = re.compile(r"<\s*/\s*" + UNTRUSTED_TAG + r"\s*>", re.IGNORECASE)
_OPENING_TAG = re.compile(r"<\s*" + UNTRUSTED_TAG + r"\b", re.IGNORECASE)


def escape_untrusted(text: str) -> str:
    """``text`` with any ``<untrusted_document>`` tag in it neutralised (``&lt;/untrusted_document&gt;``).

    For document text that is shown to a model outside :func:`wrap_untrusted` (a passage or a line excerpt): a forged
    closing tag must not be able to end the untrusted block the surrounding prompt describes.
    """
    safe = _CLOSING_TAG.sub(f"&lt;/{UNTRUSTED_TAG}&gt;", text or "")
    return _OPENING_TAG.sub(f"&lt;{UNTRUSTED_TAG}", safe)


def wrap_untrusted(name: str, text: str) -> str:
    """Wrap ``text`` as ``<untrusted_document name="...">...</untrusted_document>``.

    A closing tag (or another opening tag) inside the text is escaped, so a document cannot end its own block early
    and continue as if it were an instruction outside it. The text is otherwise untouched (quotes stay quotes: the
    ledger needs verbatim text).
    """
    safe = escape_untrusted(text)
    return f'<{UNTRUSTED_TAG} name="{html.escape(name or "document", quote=True)}">\n{safe.rstrip(chr(10))}\n</{UNTRUSTED_TAG}>'


def wrap_bundle(bundle: BriefBundle, *, labeled: bool = False) -> str:
    """Every document of ``bundle`` wrapped with :func:`wrap_untrusted`, separated by blank lines.

    With ``labeled`` each block starts with the ``=== FILE: name (kind) ===`` line of
    :meth:`~builder.ingest.brief_loader.BriefBundle.combined_text`, so the model still sees the file kind. This is the
    form the understand prompts (rule ledger, extraction, repair) use.
    """
    blocks = []
    for document in bundle.documents:
        text = f"=== FILE: {document.name} ({document.kind}) ===\n{document.text}" if labeled else document.text
        blocks.append(wrap_untrusted(document.name, text))
    return "\n\n".join(blocks)


# --- the guard ---------------------------------------------------------------------------------------------------------


def check_inputs(bundle: BriefBundle, tool_api: ToolApi) -> GuardReport:
    """Check the brief documents and the tool API before the build starts (see the module table).

    Never raises. ``blocked`` means the build cannot meaningfully continue.
    """
    report = GuardReport(stage="inputs")
    _check_size(bundle, report)
    for document in bundle.documents:
        _check_document(document.name, document.text, report)
    _check_tool_api(tool_api, report)
    return report


def _check_size(bundle: BriefBundle, report: GuardReport) -> None:
    total = sum(len(document.text) for document in bundle.documents)
    if not any(document.text.strip() for document in bundle.documents):
        report.block("empty", "The brief documents contain no text.", "briefs")
    elif total > BLOCK_CHARS:
        report.block("size", f"The briefs hold {total:,} characters (limit {BLOCK_CHARS:,}).", "briefs")
    elif total > WARN_CHARS:
        report.warn(
            "size",
            f"The briefs hold {total:,} characters (warning above {WARN_CHARS:,}); the build will be slow.",
            "briefs",
        )
    for document in bundle.documents:
        if not document.text.strip():
            report.warn("empty", f"{document.name} produced no text.", document.name)


_CONTROL = re.compile("[\x00-\x08\x0e-\x1f\x7f]")


def _binary_reason(text: str) -> str:
    """Why ``text`` is binary garbage rather than a document (``""`` when it reads as text).

    The loader decodes any file as text, so an Office or archive file it could not convert arrives as the characters of
    its bytes. Signs: the zip signature, NUL characters, or a large share of replacement / control characters. A few
    stray odd characters in an otherwise normal document are not enough.
    """
    size = len(text)
    if text.startswith("PK\x03\x04"):
        return "it starts with the signature of a zip container (an Office or archive file that could not be converted)"
    nul = text.count("\x00")
    if nul >= 3 and nul / size > 0.005:
        return f"it contains {nul} NUL characters (UTF-16 or binary content)"
    replacement = text.count("\ufffd")
    if replacement >= 10 and replacement / size > 0.02:
        return f"{replacement} of its {size:,} characters are undecodable bytes"
    control = len(_CONTROL.findall(text))
    if control >= 10 and control / size > 0.02:
        return f"{control} of its {size:,} characters are control characters"
    return ""


def _check_document(name: str, text: str, report: GuardReport) -> None:
    reason = _binary_reason(text) if text else ""
    if reason:
        report.block(
            "binary",
            f"{name} looks like a binary file, not text ({reason}); nothing can be built from it. Save it as text, "
            f"Markdown, CSV or PDF and run again.",
            name,
        )
    for kind, lines in control_characters(text).items():
        report.warn(
            "characters",
            f"{kind} characters on {_lines(lines)}; they may hide text from a reader or come from a bad decoding.",
            f"{name}:{lines[0]}",
        )
    hits = find_injections(text)
    for hit in hits[:MAX_FINDINGS_PER_KIND]:
        report.warn(
            "injection",
            f"text addressed to the model ({hit.name}): {hit.excerpt!r}. It is treated as data and never obeyed.",
            f"{name}:{hit.line}",
        )
    if len(hits) > MAX_FINDINGS_PER_KIND:
        report.warn("injection", f"{len(hits) - MAX_FINDINGS_PER_KIND} more injection-like passages.", name)
    for key in find_key_tokens(text)[:MAX_FINDINGS_PER_KIND]:
        report.warn("secret", f"a {key.kind} ({key.preview}) is written in the document.", name)


def _lines(lines: list[int]) -> str:
    shown = ", ".join(str(line) for line in lines[:5])
    return f"line {shown}" if len(lines) == 1 else f"lines {shown}" + (" ..." if len(lines) > 5 else "")


def _check_tool_api(tool_api: ToolApi, report: GuardReport) -> None:
    hosts: dict[str, list[str]] = {}
    for name, contract in tool_api.tools.items():
        parts = urlsplit(contract.url)
        location = f"tool_api.{name}"
        if not parts.scheme or not parts.hostname:
            report.block(
                "tool_url",
                f"Tool {name} has no absolute URL ({contract.url!r}); the agent could not call it.",
                location,
            )
            continue
        if parts.scheme != "https":
            report.warn("tool_https", f"Tool {name} is called over {parts.scheme}, not https.", location)
        if parts.username or parts.password:
            report.warn("secret", f"The URL of tool {name} carries credentials.", location)
        hosts.setdefault(parts.hostname.lower(), []).append(name)
    if len(hosts) > 1:
        listing = "; ".join(f"{host}: {', '.join(names)}" for host, names in hosts.items())
        report.warn("tool_hosts", f"The tools use {len(hosts)} different hosts ({listing}).", "tool_api")
    missing = tool_api.missing_tools()
    if missing:
        report.warn(
            "tool_missing", f"Expected tools missing from the tool API: {', '.join(missing)}.", "tool_api"
        )
    described = tool_api.describe()
    for hit in find_injections(described)[:MAX_FINDINGS_PER_KIND]:
        report.warn(
            "injection",
            f"text addressed to the model in the tool API ({hit.name}): {hit.excerpt!r}.",
            "tool_api",
        )
    for key in find_key_tokens(described)[:MAX_FINDINGS_PER_KIND]:
        report.warn("secret", f"a {key.kind} ({key.preview}) is written in the tool API.", "tool_api")
