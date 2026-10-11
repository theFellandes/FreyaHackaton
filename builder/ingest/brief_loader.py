"""Load brief material of any common format into plain text, without losing a word.

A brief can be one Markdown file or "raw bank material": an e-mail, a policy extract, a memo that amends it, a code
list. The LLM stages read every file in full, so this module converts each one to faithful plain text and wraps the
set in a :class:`BriefBundle` whose :meth:`BriefBundle.combined_text` labels every file::

    === FILE: brief-a.md (markdown) ===
    # Brief A: ...

Supported formats (chosen by file extension):

============================  ============  ==========================================================================
Extension                     ``kind``      Conversion
============================  ============  ==========================================================================
.md .markdown .mdown .mkd     markdown      text as-is
.txt .text                    text          text as-is
.eml                          email         From/To/Cc/Date/Subject headers, then the text body (HTML bodies
                                            stripped), then every attachment (text, JSON, PDF; others listed by name)
.csv .tsv                     csv           raw text
.json                         json          pretty-printed (raw text if it is not valid JSON)
.pdf                          pdf           text of every page via ``pypdf`` (``InputError`` when it is missing)
.html .htm .xhtml             html          tags stripped, block elements and ``<br>`` become line breaks, table cells
                                            are separated by `` | ``, scripts and styles dropped
anything else                 other         Excel ``.xlsx`` (every sheet, one line per row, cells separated by `` | ``) or
                                            Word ``.docx`` text (table rows as `` | `` lines) when the file is one,
                                            otherwise UTF-8 text with undecodable bytes replaced
============================  ============  ==========================================================================

Text decoding honours UTF-8/UTF-16 byte-order marks, detects UTF-16 without one, falls back to Windows-1252 for
legacy files and finally to UTF-8 with replacement characters. A file that still comes out as binary garbage (a zip
container nothing could read, NUL bytes, a flood of replacement characters) is not rejected here; the input guard
blocks the build (:func:`builder.guardrails.input_guard.check_inputs`). All output is normalised to ``\\n`` line
endings with the BOM removed. Nothing is ever truncated. ``sha256`` of a document is the hash of the **raw file bytes**, so a document can be matched with a
known copy of the same file.
"""

from __future__ import annotations

import codecs
import email.policy
import hashlib
import io
import json
import logging
import posixpath
import re
import zipfile
from collections.abc import Callable, Iterable
from email.message import EmailMessage
from email.parser import BytesParser
from html.parser import HTMLParser
from pathlib import Path
from typing import Literal
from xml.etree import ElementTree

from pydantic import BaseModel, ConfigDict

from builder.inputs import InputError

logger = logging.getLogger(__name__)

BriefKind = Literal["markdown", "text", "email", "csv", "json", "pdf", "html", "other"]

#: File extension -> document kind.
KIND_BY_SUFFIX: dict[str, BriefKind] = {
    ".md": "markdown",
    ".markdown": "markdown",
    ".mdown": "markdown",
    ".mkd": "markdown",
    ".txt": "text",
    ".text": "text",
    ".eml": "email",
    ".csv": "csv",
    ".tsv": "csv",
    ".json": "json",
    ".pdf": "pdf",
    ".html": "html",
    ".htm": "html",
    ".xhtml": "html",
}

#: How deep forwarded messages (message/rfc822 attachments) are unpacked.
_MAX_EMAIL_DEPTH = 3
_BOM = chr(0xFEFF)  # a byte-order mark decoded into the text
_EMAIL_HEADERS = ("From", "To", "Cc", "Date", "Subject")
_WORD_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_MAX_OFFICE_PART_BYTES = 40_000_000


class BriefDocument(BaseModel):
    """One brief file converted to text.

    Attributes:
        path: Where the file was read from.
        name: The file name (with extension), used as the label in :meth:`BriefBundle.combined_text`.
        kind: The detected format (see the module table).
        text: The complete converted text (``\\n`` line endings, no BOM, never truncated).
        sha256: SHA-256 of the raw file bytes.
        label: What the document is when it is not plain policy material (an example conversation of a case
            folder); shown in its ``=== FILE`` header. Empty for ordinary brief files.
    """

    model_config = ConfigDict(frozen=True)

    path: Path
    name: str
    kind: BriefKind
    text: str
    sha256: str
    label: str = ""


class BriefBundle(BaseModel):
    """All brief documents of one build, in input order.

    Attributes:
        documents: The documents, in the order the files were given.
        sha256: SHA-256 over the documents' hashes in order - identifies the exact set of brief contents.
    """

    model_config = ConfigDict(frozen=True)

    documents: list[BriefDocument]
    sha256: str

    @classmethod
    def from_documents(cls, documents: Iterable[BriefDocument]) -> BriefBundle:
        """Build a bundle and compute its combined hash."""
        docs = list(documents)
        digest = hashlib.sha256("\n".join(doc.sha256 for doc in docs).encode("ascii")).hexdigest()
        return cls(documents=docs, sha256=digest)

    @property
    def names(self) -> list[str]:
        """The documents' file names, in order."""
        return [doc.name for doc in self.documents]

    def combined_text(self) -> str:
        """Every document as ``=== FILE: {name} ({kind}) ===`` followed by its text, separated by blank lines.

        A labelled document says what it is in its header: ``=== FILE: {name} ({kind}; {label}) ===``.
        """
        return "\n".join(
            f"=== FILE: {doc.name} ({doc.kind}{'; ' + doc.label if doc.label else ''}) ===\n"
            f"{doc.text.rstrip(chr(10))}\n"
            for doc in self.documents
        )


def kind_for(path: Path) -> BriefKind:
    """The document kind for ``path``, from its extension (``other`` when unknown)."""
    return KIND_BY_SUFFIX.get(path.suffix.lower(), "other")


#: Header label of an example conversation of a case folder (policy material of the lowest priority).
EXAMPLE_LABEL = (
    "example conversation - an illustration of how a call can go, not a rule; the brief and the other documents win "
    "where they differ"
)


def load_briefs(paths: Iterable[Path | str], *, examples: Iterable[Path | str] = ()) -> BriefBundle:
    """Load every brief file into one :class:`BriefBundle` (order kept).

    Args:
        paths: The brief files.
        examples: Those of ``paths`` that are example conversations; they carry :data:`EXAMPLE_LABEL`.

    Raises:
        InputError: If no path is given, a file cannot be read, or a PDF cannot be converted.
    """
    labelled = {Path(path) for path in examples}
    documents = []
    for path in paths:
        document = load_brief(Path(path))
        if Path(path) in labelled:
            document = document.model_copy(update={"label": EXAMPLE_LABEL})
        documents.append(document)
    if not documents:
        raise InputError("No brief files to load.")
    return BriefBundle.from_documents(documents)


def load_brief(path: Path | str) -> BriefDocument:
    """Load one file as a :class:`BriefDocument` (see the module table for the conversions).

    Raises:
        InputError: If the file cannot be read, or it is a PDF and ``pypdf`` is missing or fails.
    """
    path = Path(path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise InputError(f"Cannot read brief file {path}: {exc.strerror or exc}") from exc
    kind = kind_for(path)
    text = normalise_text(_CONVERTERS[kind](raw, path))
    if not text.strip():
        logger.warning("brief file %s produced no text", path.name)
    return BriefDocument(
        path=path, name=path.name, kind=kind, text=text, sha256=hashlib.sha256(raw).hexdigest()
    )


# --- text helpers ------------------------------------------------------------------------------------------------------


def normalise_text(text: str) -> str:
    """Convert CRLF / CR line endings to LF and drop a leading byte-order mark."""
    return text.replace("\r\n", "\n").replace("\r", "\n").removeprefix(_BOM)


def decode_text(raw: bytes, name: str = "") -> str:
    """Decode file bytes: BOM-aware UTF-8/UTF-16, else strict UTF-8, else Windows-1252, else UTF-8 with U+FFFD."""
    if raw.startswith(codecs.BOM_UTF8):
        return raw[len(codecs.BOM_UTF8) :].decode("utf-8", errors="replace")
    if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return raw.decode("utf-16", errors="replace")
    bomless = _bomless_utf16(raw)
    if bomless is not None:
        return raw.decode(bomless, errors="replace")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        text = raw.decode("cp1252")
        logger.warning("%s is not UTF-8; decoded as Windows-1252", name or "input")
        return text
    except UnicodeDecodeError:
        logger.warning("%s is not valid UTF-8; undecodable bytes replaced", name or "input")
        return raw.decode("utf-8", errors="replace")


def _bomless_utf16(raw: bytes) -> str | None:
    """``utf-16-le`` / ``utf-16-be`` when ``raw`` looks like UTF-16 text without a byte-order mark, else ``None``.

    Mostly-Latin UTF-16 has a zero byte next to almost every character: in the odd positions for little endian, in the
    even positions for big endian. UTF-8 text never holds a NUL byte, so a high share of them is decisive.
    """
    sample = raw[:4096]
    if len(sample) < 4 or b"\x00" not in sample:
        return None
    pairs = len(sample) // 2
    odd = sample[1 : pairs * 2 : 2].count(0) / pairs
    even = sample[0 : pairs * 2 : 2].count(0) / pairs
    if odd >= 0.3 and odd > even * 3:
        return "utf-16-le"
    if even >= 0.3 and even > odd * 3:
        return "utf-16-be"
    return None


def _plain(raw: bytes, path: Path) -> str:
    return decode_text(raw, path.name)


def _json(raw: bytes, path: Path) -> str:
    text = decode_text(raw, path.name)
    try:
        return json.dumps(json.loads(text), indent=2, ensure_ascii=False)
    except ValueError:
        logger.warning("%s is not valid JSON; kept as raw text", path.name)
        return text


def _other(raw: bytes, path: Path) -> str:
    zipped = raw.startswith(b"PK\x03\x04")
    if path.suffix.lower() == ".xlsx" or zipped:
        sheets = xlsx_text(raw)
        if sheets is not None:
            return sheets
    if path.suffix.lower() == ".docx" or zipped:
        docx = docx_text(raw)
        if docx is not None:
            return docx
    return decode_text(raw, path.name)


# --- HTML --------------------------------------------------------------------------------------------------------------


class _HTMLToText(HTMLParser):
    """Collects visible text; block elements become line breaks, table cells are joined with `` | ``."""

    _BLOCK = frozenset(
        {
            "p", "div", "section", "article", "header", "footer", "main", "aside", "nav", "blockquote", "pre",
            "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "dl", "dt", "dd", "table", "thead", "tbody", "tfoot",
            "tr", "address", "figure", "figcaption", "form", "fieldset", "title", "caption",
        }
    )  # fmt: skip
    _SKIP = frozenset({"script", "style", "noscript", "template", "svg"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0
        self._pre_depth = 0
        self._cells_in_row = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag in ("br", "hr"):
            self._chunks.append("\n")
        elif tag == "li":
            self._chunks.append("\n- ")
        elif tag in ("td", "th"):
            if self._cells_in_row:
                self._chunks.append(" | ")
            self._cells_in_row += 1
        elif tag in self._BLOCK:
            self._chunks.append("\n")
        if tag == "tr":
            self._cells_in_row = 0
        if tag == "pre":
            self._pre_depth += 1

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("br", "hr"):
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in self._BLOCK or tag == "li":
            self._chunks.append("\n")
        if tag == "pre":
            self._pre_depth = max(0, self._pre_depth - 1)

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        self._chunks.append(data if self._pre_depth else re.sub(r"\s+", " ", data))

    def text(self) -> str:
        lines = [line.strip() for line in "".join(self._chunks).split("\n")]
        joined = "\n".join(lines)
        return re.sub(r"\n{3,}", "\n\n", joined).strip()


def html_to_text(html: str) -> str:
    """Strip tags from ``html`` keeping line structure (see :class:`_HTMLToText`); entities are unescaped."""
    parser = _HTMLToText()
    parser.feed(html)
    parser.close()
    return parser.text()


def _html(raw: bytes, path: Path) -> str:
    return html_to_text(decode_text(raw, path.name))


# --- PDF and Word ------------------------------------------------------------------------------------------------------


def pdf_text(raw: bytes, name: str) -> str:
    """Extract the text of every page of a PDF (pages separated by ``--- page N ---`` markers when several).

    Raises:
        InputError: If ``pypdf`` is not installed or the PDF cannot be parsed.
    """
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise InputError(
            f"{name} is a PDF, and reading PDFs needs the 'pypdf' package (install the 'ingest' extra)."
        ) from exc
    try:
        reader = PdfReader(io.BytesIO(raw))
        if reader.is_encrypted:
            reader.decrypt("")
        pages = [(page.extract_text() or "").strip() for page in reader.pages]
    except Exception as exc:  # pypdf raises many exception types for damaged files
        raise InputError(f"Could not read the PDF {name}: {exc}") from exc
    if len(pages) == 1:
        return pages[0]
    return "\n\n".join(f"--- page {number} ---\n{text}" for number, text in enumerate(pages, start=1))


def _pdf(raw: bytes, path: Path) -> str:
    return pdf_text(raw, path.name)


def _read_member(archive: zipfile.ZipFile, name: str) -> bytes:
    """One part of an Office archive; a part that would unpack to an absurd size is refused (zip bomb)."""
    if archive.getinfo(name).file_size > _MAX_OFFICE_PART_BYTES:
        raise ValueError(f"{name} is too large")
    return archive.read(name)


def _word_paragraph(paragraph: ElementTree.Element) -> str:
    pieces = []
    for node in paragraph.iter():
        if node.tag == f"{_WORD_NS}t":
            pieces.append(node.text or "")
        elif node.tag == f"{_WORD_NS}tab":
            pieces.append("\t")
        elif node.tag in (f"{_WORD_NS}br", f"{_WORD_NS}cr"):
            pieces.append("\n")
    return "".join(pieces)


def _word_blocks(element: ElementTree.Element, lines: list[str]) -> None:
    """Append the text blocks of ``element`` to ``lines``: a paragraph is a line, a table row is a `` | `` line."""
    for child in element:
        if child.tag == f"{_WORD_NS}p":
            lines.append(_word_paragraph(child))
        elif child.tag == f"{_WORD_NS}tbl":
            for row in child.findall(f"{_WORD_NS}tr"):
                cells = []
                for cell in row.findall(f"{_WORD_NS}tc"):
                    texts = (_word_paragraph(p).replace("\n", " ").strip() for p in cell.iter(f"{_WORD_NS}p"))
                    cells.append(" ".join(text for text in texts if text))
                lines.append(" | ".join(cells))
        else:
            _word_blocks(child, lines)  # body, content controls, tracked changes ...


def docx_text(raw: bytes) -> str | None:
    """Text of a Word ``.docx`` file, or ``None`` when ``raw`` is not one.

    A paragraph is one line. A table row is one line with its cells separated by `` | `` (the way the HTML conversion
    writes tables), so a bands table or an outcome table stays readable as rows.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            document = _read_member(archive, "word/document.xml")
        root = ElementTree.fromstring(document)
    except (zipfile.BadZipFile, KeyError, ElementTree.ParseError, ValueError):
        return None
    lines: list[str] = []
    _word_blocks(root, lines)
    return "\n".join(lines)


# --- Excel -------------------------------------------------------------------------------------------------------------


def _local(tag: str) -> str:
    """The tag without its namespace (``{ns}row`` -> ``row``)."""
    return tag.rsplit("}", 1)[-1]


def _xlsx_shared_strings(document: bytes) -> list[str]:
    strings = []
    for item in ElementTree.fromstring(document):
        if _local(item.tag) != "si":
            continue
        pieces = []
        for child in item:
            if _local(child.tag) == "t":
                pieces.append(child.text or "")
            elif _local(child.tag) == "r":  # a rich-text run
                pieces.extend(node.text or "" for node in child if _local(node.tag) == "t")
        strings.append("".join(pieces))
    return strings


def _xlsx_sheet_parts(archive: zipfile.ZipFile, names: set[str]) -> list[tuple[str, str]]:
    """``(sheet name, part path)`` in workbook order, following the workbook relationships when the file has them."""
    book = ElementTree.fromstring(_read_member(archive, "xl/workbook.xml"))
    entries = [
        (element.get("name") or "", element.get(f"{_REL_NS}id") or "")
        for element in book.iter()
        if _local(element.tag) == "sheet"
    ]
    targets: dict[str, str] = {}
    if "xl/_rels/workbook.xml.rels" in names:
        rels = ElementTree.fromstring(_read_member(archive, "xl/_rels/workbook.xml.rels"))
        for rel in rels.iter():
            if _local(rel.tag) == "Relationship":
                target = rel.get("Target") or ""
                full = target.lstrip("/") if target.startswith("/") else f"xl/{target}"
                targets[rel.get("Id") or ""] = posixpath.normpath(full)
    parts = sorted(
        (name for name in names if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", name)),
        key=lambda name: int(re.search(r"(\d+)\.xml$", name).group(1)),  # type: ignore[union-attr]
    )
    found = []
    for index, (name, rel_id) in enumerate(entries):
        part = targets.get(rel_id) or (parts[index] if index < len(parts) else "")
        if part in names:
            found.append((name or f"Sheet{index + 1}", part))
    return found or [(f"Sheet{index + 1}", part) for index, part in enumerate(parts)]


def _xlsx_column(ref: str) -> int | None:
    letters = re.match(r"[A-Za-z]+", ref or "")
    if letters is None:
        return None
    number = 0
    for char in letters.group(0).upper():
        number = number * 26 + ord(char) - 64
    return number - 1


def _xlsx_cell(cell: ElementTree.Element, shared: list[str]) -> str:
    kind = cell.get("t") or "n"
    value = next((child.text or "" for child in cell if _local(child.tag) == "v"), "")
    if kind == "inlineStr":
        return "".join(node.text or "" for node in cell.iter() if _local(node.tag) == "t")
    if kind == "s":
        try:
            return shared[int(value)]
        except (ValueError, IndexError):
            return ""
    if kind == "b":
        return "TRUE" if value.strip() == "1" else "FALSE"
    return value


def _xlsx_rows(document: bytes, shared: list[str]) -> list[str]:
    rows = []
    for row in ElementTree.fromstring(document).iter():
        if _local(row.tag) != "row":
            continue
        cells: dict[int, str] = {}
        for position, cell in enumerate(child for child in row if _local(child.tag) == "c"):
            column = _xlsx_column(cell.get("r") or "")
            cells[position if column is None else column] = " ".join(_xlsx_cell(cell, shared).split())
        if not any(cells.values()):
            continue
        last = max(index for index, text in cells.items() if text)
        rows.append(" | ".join(cells.get(index, "") for index in range(last + 1)))
    return rows


def xlsx_text(raw: bytes) -> str | None:
    """Text of an Excel ``.xlsx`` file, or ``None`` when ``raw`` is not one.

    One line per non-empty row, cells separated by `` | `` (empty cells keep their column), trailing empty cells
    dropped. With several sheets each starts with a ``--- sheet: Name ---`` line. Numbers appear as stored (a date is
    its Excel serial number: the file does not say it is a date without its style sheet).
    """
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            names = set(archive.namelist())
            if "xl/workbook.xml" not in names:
                return None
            shared = _xlsx_shared_strings(_read_member(archive, "xl/sharedStrings.xml")) if (
                "xl/sharedStrings.xml" in names
            ) else []
            sheets = [
                (name, _xlsx_rows(_read_member(archive, part), shared)) for name, part in _xlsx_sheet_parts(archive, names)
            ]
    except (zipfile.BadZipFile, KeyError, ElementTree.ParseError, ValueError):
        return None
    sheets = [(name, rows) for name, rows in sheets if rows]
    if len(sheets) == 1:
        return "\n".join(sheets[0][1])
    return "\n\n".join(f"--- sheet: {name} ---\n" + "\n".join(rows) for name, rows in sheets)


# --- e-mail ------------------------------------------------------------------------------------------------------------


def email_to_text(raw: bytes) -> str:
    """Render an RFC 822 message: headers, body (plain text preferred over HTML) and every attachment."""
    message = BytesParser(policy=email.policy.default).parsebytes(raw)
    if not isinstance(
        message, EmailMessage
    ):  # policy.default always yields EmailMessage; stay safe regardless
        return decode_text(raw)
    return _render_message(message, depth=0)


def _render_message(message: EmailMessage, depth: int) -> str:
    headers = [
        f"{header}: {' '.join(str(message[header]).split())}" for header in _EMAIL_HEADERS if message[header]
    ]
    sections: list[str] = []
    body = message.get_body(preferencelist=("plain", "html"))
    if body is not None:
        sections.append(_part_text(body))
    for part in message.iter_attachments():
        sections.append(_attachment_text(part, depth))
    blocks = ["\n".join(headers), *(section.strip() for section in sections)]
    return "\n\n".join(block for block in blocks if block)


def _part_text(part: EmailMessage) -> str:
    """Text content of a MIME part (HTML stripped); charset problems fall back to a lenient decode."""
    try:
        content = part.get_content()
    except (LookupError, KeyError, ValueError):
        content = part.get_payload(decode=True) or b""
    if isinstance(content, bytes):
        content = decode_text(content)
    if not isinstance(content, str):
        return ""
    content = normalise_text(content)
    return html_to_text(content) if part.get_content_subtype() == "html" else content


def _attachment_text(part: EmailMessage, depth: int) -> str:
    content_type = part.get_content_type()
    filename = part.get_filename() or content_type
    if content_type == "message/rfc822":
        inner = part.get_content()
        if isinstance(inner, EmailMessage) and depth < _MAX_EMAIL_DEPTH:
            return f"--- attached message ---\n{_render_message(inner, depth + 1)}"
    if part.get_content_maintype() == "text" or content_type == "application/json":
        return f"--- attachment: {filename} ---\n{_part_text(part)}"
    payload = part.get_payload(decode=True) or b""
    if content_type == "application/pdf" or filename.lower().endswith(".pdf"):
        try:
            return f"--- attachment: {filename} ---\n{pdf_text(payload, filename)}"
        except InputError as exc:
            logger.warning("e-mail attachment %s not read: %s", filename, exc)
    docx = docx_text(payload) if filename.lower().endswith(".docx") else None
    if docx is not None:
        return f"--- attachment: {filename} ---\n{docx}"
    sheets = xlsx_text(payload) if filename.lower().endswith(".xlsx") else None
    if sheets is not None:
        return f"--- attachment: {filename} ---\n{sheets}"
    return f"[attachment not read: {filename} ({content_type}, {len(payload)} bytes)]"


def _email(raw: bytes, path: Path) -> str:
    return email_to_text(raw)


_CONVERTERS: dict[BriefKind, Callable[[bytes, Path], str]] = {
    "markdown": _plain,
    "text": _plain,
    "email": _email,
    "csv": _plain,
    "json": _json,
    "pdf": _pdf,
    "html": _html,
    "other": _other,
}


__all__ = [
    "EXAMPLE_LABEL",
    "KIND_BY_SUFFIX",
    "BriefBundle",
    "BriefDocument",
    "BriefKind",
    "decode_text",
    "docx_text",
    "email_to_text",
    "html_to_text",
    "kind_for",
    "load_brief",
    "load_briefs",
    "normalise_text",
    "pdf_text",
    "xlsx_text",
]
