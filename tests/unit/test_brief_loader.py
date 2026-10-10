"""Tests for builder.ingest.brief_loader: every supported format, normalisation, hashing and the combined text."""

from __future__ import annotations

import builtins
import hashlib
import importlib.util
import io
import json
import zipfile
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import pytest

from builder.ingest.brief_loader import (
    BriefBundle,
    decode_text,
    html_to_text,
    kind_for,
    load_brief,
    load_briefs,
)
from builder.inputs import InputError

#: PDF reading needs ``pypdf`` (the ``ingest`` extra); the tests that read a real PDF skip without it.
HAS_PYPDF = importlib.util.find_spec("pypdf") is not None
needs_pypdf = pytest.mark.skipif(not HAS_PYPDF, reason="pypdf (the 'ingest' extra) is not installed")

EN_DASH = chr(0x2013)
BOM = chr(0xFEFF)
REPLACEMENT = chr(0xFFFD)


def make_pdf(pages: list[str]) -> bytes:
    """A minimal valid PDF with one Helvetica text line per page (hand-built, correct xref table)."""
    objects: list[bytes] = []
    page_ids = [3 + 2 * index for index in range(len(pages))]
    font_id = 3 + 2 * len(pages)
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    kids = " ".join(f"{page_id} 0 R" for page_id in page_ids)
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode())
    for index, text in enumerate(pages):
        content_id = page_ids[index] + 1
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {content_id} 0 R "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> >>".encode()
        )
        stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
        objects.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets:
        out.write(f"{offset:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()


def make_docx(paragraphs: list[str]) -> bytes:
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body = "".join(f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>" for text in paragraphs)
    document = f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="{ns}"><w:body>{body}</w:body></w:document>'
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", document)
    return buffer.getvalue()


def write(tmp_path: Path, name: str, data: bytes | str) -> Path:
    path = tmp_path / name
    path.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)
    return path


# --- markdown / text --------------------------------------------------------------------------------------------------


def test_kit_markdown_brief_is_loaded_verbatim(brief_a_path: Path) -> None:
    raw = brief_a_path.read_bytes()
    doc = load_brief(brief_a_path)
    assert doc.kind == "markdown"
    assert doc.name == "brief-a.md"
    assert doc.path == brief_a_path
    assert doc.sha256 == hashlib.sha256(raw).hexdigest()
    assert doc.text == raw.decode("utf-8").replace("\r\n", "\n")
    assert "\r" not in doc.text


def test_crlf_bom_and_lone_cr_are_normalised(tmp_path: Path) -> None:
    path = write(tmp_path, "memo.txt", b"\xef\xbb\xbfLine one\r\nLine two\rLine three\n")
    doc = load_brief(path)
    assert doc.kind == "text"
    assert doc.text == "Line one\nLine two\nLine three\n"


def test_utf16_and_legacy_encodings(tmp_path: Path) -> None:
    utf16 = write(tmp_path, "a.txt", (BOM + "Café €10").encode("utf-16-le"))
    assert load_brief(utf16).text == "Café €10"
    cp1252 = write(tmp_path, "b.txt", "Café €10 “quoted”".encode("cp1252"))
    assert load_brief(cp1252).text == "Café €10 “quoted”"
    assert decode_text(b"bad \x81 byte") == "bad " + REPLACEMENT + " byte"  # 0x81 is undefined in cp1252 too


def test_large_text_is_never_truncated(tmp_path: Path) -> None:
    body = "".join(f"Rule {index}: keep every word.\n" for index in range(60000))  # ~1.7 MB
    doc = load_brief(write(tmp_path, "big.md", body))
    assert doc.text == body
    assert doc.text.endswith("Rule 59999: keep every word.\n")


# --- csv / json -----------------------------------------------------------------------------------------------------------


def test_csv_and_tsv_are_kept_raw(tmp_path: Path) -> None:
    csv = load_brief(write(tmp_path, "codes.csv", 'code,meaning\r\nA1,"late, early"\r\n'))
    tsv = load_brief(write(tmp_path, "codes.TSV", "code\tmeaning\nA1\tlate\n"))
    assert (csv.kind, csv.text) == ("csv", 'code,meaning\nA1,"late, early"\n')
    assert (tsv.kind, tsv.text) == ("csv", "code\tmeaning\nA1\tlate\n")


def test_json_is_pretty_printed_and_invalid_json_kept(tmp_path: Path) -> None:
    doc = load_brief(write(tmp_path, "limits.json", '{"bands":[{"name":"early","days":[1,7]}],"note":"é"}'))
    assert doc.kind == "json"
    assert json.loads(doc.text) == {"bands": [{"name": "early", "days": [1, 7]}], "note": "é"}
    assert '\n  "bands": [' in doc.text and "é" in doc.text
    broken = load_brief(write(tmp_path, "broken.json", '{"a": 1,,}'))
    assert broken.text == '{"a": 1,,}'


# --- html -----------------------------------------------------------------------------------------------------------------


def test_html_is_stripped_with_line_structure(tmp_path: Path) -> None:
    html = """<html><head><title>Policy memo</title><style>p {color: red}</style>
    <script>var secret = "x";</script></head>
    <body><h1>Amendment&nbsp;2</h1><p>Rule 6 now   reads:<br>callbacks until 18:00 &amp; not later.</p>
    <ul><li>first</li><li>second</li></ul>
    <table><tr><th>Band</th><th>Days</th></tr><tr><td>early</td><td>1&ndash;7</td></tr></table>
    <pre>keep   spacing</pre></body></html>"""
    doc = load_brief(write(tmp_path, "memo.html", html))
    assert doc.kind == "html"
    lines = doc.text.split("\n")
    assert lines[0] == "Policy memo"
    assert "Amendment 2" in lines  # a non-breaking space is normalised like any other whitespace
    assert "Rule 6 now reads:" in lines
    assert "callbacks until 18:00 & not later." in lines
    assert "- first" in lines and "- second" in lines
    assert "Band | Days" in lines
    assert f"early | 1{EN_DASH}7" in lines
    assert "keep   spacing" in lines
    assert "color" not in doc.text and "secret" not in doc.text
    assert "\n\n\n" not in doc.text


def test_html_to_text_handles_self_closing_breaks() -> None:
    assert html_to_text("a<br/>b<hr/>c") == "a\nb\nc"


# --- email ----------------------------------------------------------------------------------------------------------------


def _email_bytes(with_html_alternative: bool = True) -> bytes:
    message = EmailMessage()
    message["From"] = "Policy Team <policy@bank.example>"
    message["To"] = "builders@bank.example"
    message["Date"] = "Mon, 12 Oct 2026 09:30:00 +0000"
    message["Subject"] = "=?utf-8?q?Amendment_=E2=80=93_callbacks?="
    message.set_content("Hello,\n\nRule 6 changes: callbacks end at 18:00.\n")
    if with_html_alternative:
        message.add_alternative("<p>HTML <b>copy</b> that must not duplicate the text.</p>", subtype="html")
    message.add_attachment("code,meaning\nX1,refused\n", subtype="csv", filename="codes.csv")
    message.add_attachment(
        make_pdf(["Attached PDF rule"]), maintype="application", subtype="pdf", filename="annex.pdf"
    )
    message.add_attachment(b"\x89PNG\r\n", maintype="image", subtype="png", filename="logo.png")
    return message.as_bytes()


def test_email_headers_body_and_attachments(tmp_path: Path) -> None:
    doc = load_brief(write(tmp_path, "mail.eml", _email_bytes()))
    assert doc.kind == "email"
    text = doc.text
    assert text.startswith(
        "From: Policy Team <policy@bank.example>\nTo: builders@bank.example\n"
        f"Date: Mon, 12 Oct 2026 09:30:00 +0000\nSubject: Amendment {EN_DASH} callbacks\n\n"
    )
    assert "Rule 6 changes: callbacks end at 18:00." in text
    assert "HTML copy" not in text  # plain text alternative preferred, not duplicated
    assert "--- attachment: codes.csv ---\ncode,meaning\nX1,refused" in text
    if HAS_PYPDF:
        assert "--- attachment: annex.pdf ---\nAttached PDF rule" in text
    else:  # without the 'ingest' extra a PDF attachment is reported, not read
        assert "[attachment not read: annex.pdf" in text
    assert "[attachment not read: logo.png (image/png, 6 bytes)]" in text


def test_html_only_email_and_forwarded_message(tmp_path: Path) -> None:
    inner = EmailMessage()
    inner["From"] = "manager@bank.example"
    inner["Subject"] = "Original rule"
    inner.set_content("<p>Deadline is the <b>7th</b> business day.</p>", subtype="html")
    outer = EmailMessage()
    outer["From"] = "a@bank.example"
    outer["Subject"] = "Fwd: rule"
    outer.set_content("See below.")
    outer.add_attachment(inner)
    doc = load_brief(write(tmp_path, "fwd.eml", outer.as_bytes()))
    assert "See below." in doc.text
    assert "--- attached message ---\nFrom: manager@bank.example\nSubject: Original rule" in doc.text
    assert "Deadline is the 7th business day." in doc.text


# --- pdf ------------------------------------------------------------------------------------------------------------------


@needs_pypdf
def test_pdf_pages_are_extracted(tmp_path: Path) -> None:
    single = load_brief(write(tmp_path, "one.pdf", make_pdf(["Only page text"])))
    assert (single.kind, single.text) == ("pdf", "Only page text")
    multi = load_brief(write(tmp_path, "two.PDF", make_pdf(["First page", "Second page"])))
    assert multi.text == "--- page 1 ---\nFirst page\n\n--- page 2 ---\nSecond page"


def test_pdf_without_pypdf_is_an_input_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real_import = builtins.__import__

    def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "pypdf" or name.startswith("pypdf."):
            raise ImportError("No module named 'pypdf'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(InputError, match="pypdf"):
        load_brief(write(tmp_path, "policy.pdf", make_pdf(["x"])))


@needs_pypdf
def test_damaged_pdf_is_an_input_error(tmp_path: Path) -> None:
    with pytest.raises(InputError, match="Could not read the PDF"):
        load_brief(write(tmp_path, "broken.pdf", b"this is not a pdf at all"))


# --- other ----------------------------------------------------------------------------------------------------------------


def test_docx_and_unknown_extensions(tmp_path: Path) -> None:
    docx = load_brief(write(tmp_path, "memo.docx", make_docx(["Memo title", "Rule 7 is replaced."])))
    assert (docx.kind, docx.text) == ("other", "Memo title\nRule 7 is replaced.")
    yaml_doc = load_brief(write(tmp_path, "limits.yaml", "bands:\r\n  early: 7\r\n"))
    assert (yaml_doc.kind, yaml_doc.text) == ("other", "bands:\n  early: 7\n")
    zipped = load_brief(write(tmp_path, "archive.zip", b"PK\x03\x04garbage"))
    assert zipped.kind == "other"  # not a docx: decoded as text, never an exception


@pytest.mark.parametrize(
    ("name", "kind"),
    [("a.md", "markdown"), ("a.MARKDOWN", "markdown"), ("a.txt", "text"), ("a.eml", "email"), ("a.csv", "csv"),
     ("a.json", "json"), ("a.pdf", "pdf"), ("a.htm", "html"), ("a.xhtml", "html"), ("a.rtf", "other"), ("a", "other")],
)  # fmt: skip
def test_kind_for(name: str, kind: str) -> None:
    assert kind_for(Path(name)) == kind


# --- bundle ---------------------------------------------------------------------------------------------------------------


def test_bundle_combined_text_format_and_order(tmp_path: Path) -> None:
    first = write(tmp_path, "brief.md", "# Brief\nRule 1.\n")
    second = write(tmp_path, "memo.txt", "Memo text")
    bundle = load_briefs([first, second])
    assert bundle.names == ["brief.md", "memo.txt"]
    assert bundle.combined_text() == (
        "=== FILE: brief.md (markdown) ===\n# Brief\nRule 1.\n\n=== FILE: memo.txt (text) ===\nMemo text\n"
    )


def test_bundle_hash_is_content_based_and_order_sensitive(tmp_path: Path) -> None:
    a = write(tmp_path, "a.md", "alpha")
    b = write(tmp_path, "b.md", "beta")
    ab = load_briefs([a, b])
    assert load_briefs([a, b]).sha256 == ab.sha256
    assert load_briefs([b, a]).sha256 != ab.sha256
    expected = hashlib.sha256(
        (hashlib.sha256(b"alpha").hexdigest() + "\n" + hashlib.sha256(b"beta").hexdigest()).encode()
    ).hexdigest()
    assert ab.sha256 == expected
    b.write_text("beta changed", encoding="utf-8")
    assert load_briefs([a, b]).sha256 != ab.sha256


def test_bundle_round_trips_through_json(kit_brief_paths: list[Path]) -> None:
    bundle = load_briefs(kit_brief_paths)
    restored = BriefBundle.model_validate(json.loads(json.dumps(bundle.model_dump(mode="json"))))
    assert restored == bundle
    assert "=== FILE: brief-b.md (markdown) ===" in restored.combined_text()


def test_errors(tmp_path: Path) -> None:
    with pytest.raises(InputError, match="No brief files"):
        load_briefs([])
    with pytest.raises(InputError, match="Cannot read brief file"):
        load_brief(tmp_path / "missing.md")


def test_empty_file_loads_with_a_warning(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    doc = load_brief(write(tmp_path, "empty.md", b""))
    assert doc.text == ""
    assert "produced no text" in caplog.text


# --- U-08: spreadsheets, tables and BOM-less UTF-16 ----------------------------------------------------------------------------


def make_xlsx(sheets: dict[str, list[list[str | int | float | None]]], *, inline: bool = False) -> bytes:
    """A minimal .xlsx: shared strings (or inline strings), one sheet part per entry, sheet names in workbook.xml."""
    main = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    strings: list[str] = []
    parts: dict[str, str] = {}
    for number, rows in enumerate(sheets.values(), start=1):
        xml_rows = []
        for r, row in enumerate(rows, start=1):
            cells = []
            for c, value in enumerate(row):
                if value is None:
                    continue
                ref = f"{chr(65 + c)}{r}"
                if isinstance(value, str) and inline:
                    cells.append(f'<c r="{ref}" t="inlineStr"><is><t>{value}</t></is></c>')
                elif isinstance(value, str):
                    strings.append(value)
                    cells.append(f'<c r="{ref}" t="s"><v>{len(strings) - 1}</v></c>')
                else:
                    cells.append(f'<c r="{ref}"><v>{value}</v></c>')
            xml_rows.append(f'<row r="{r}">{"".join(cells)}</row>')
        parts[f"xl/worksheets/sheet{number}.xml"] = (
            f'<worksheet xmlns="{main}"><sheetData>{"".join(xml_rows)}</sheetData></worksheet>'
        )
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    sheet_tags = "".join(
        f'<sheet name="{name}" sheetId="{i}" r:id="rId{i}" xmlns:r="{rel}"/>' for i, name in enumerate(sheets, start=1)
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("xl/workbook.xml", f'<workbook xmlns="{main}"><sheets>{sheet_tags}</sheets></workbook>')
        if not inline:
            shared = "".join(f"<si><t>{text}</t></si>" for text in strings)
            archive.writestr("xl/sharedStrings.xml", f'<sst xmlns="{main}">{shared}</sst>')
        for name, xml in parts.items():
            archive.writestr(name, xml)
    return buffer.getvalue()


def make_docx_with_table(before: str, rows: list[list[str]], after: str) -> bytes:
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

    def paragraph(text: str) -> str:
        return f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"

    table = "".join(
        "<w:tr>" + "".join(f"<w:tc>{paragraph(cell)}</w:tc>" for cell in row) + "</w:tr>" for row in rows
    )
    body = paragraph(before) + f"<w:tbl>{table}</w:tbl>" + paragraph(after)
    document = f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="{ns}"><w:body>{body}</w:body></w:document>'
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", document)
    return buffer.getvalue()


def test_xlsx_code_list_is_read_row_by_row(tmp_path: Path) -> None:
    """A spreadsheet used to load as zip-container garbage (PK header, [Content_Types].xml) without any signal."""
    data = make_xlsx({"Codes": [["code", "outcome", "reason"], ["OC01", "agreed", None], ["OC02", "escalated", "plan"]]})
    doc = load_brief(write(tmp_path, "codes.xlsx", data))
    assert doc.kind == "other"
    assert doc.text == "code | outcome | reason\nOC01 | agreed\nOC02 | escalated | plan"
    assert "PK" not in doc.text and "Content_Types" not in doc.text


def test_xlsx_with_several_sheets_inline_strings_and_numbers(tmp_path: Path) -> None:
    data = make_xlsx(
        {"Bands": [["band", "from", "to"], ["early", 1, 7], ["late", 8, 45.5]], "Notes": [["only note"]]}, inline=True
    )
    text = load_brief(write(tmp_path, "limits.xlsx", data)).text
    assert text == "--- sheet: Bands ---\nband | from | to\nearly | 1 | 7\nlate | 8 | 45.5\n\n--- sheet: Notes ---\nonly note"


def test_a_cell_gap_keeps_the_columns_aligned(tmp_path: Path) -> None:
    data = make_xlsx({"S": [["a", None, "c"]]})
    assert load_brief(write(tmp_path, "gap.xlsx", data)).text == "a |  | c"


def test_docx_table_rows_stay_rows(tmp_path: Path) -> None:
    """U-08: every cell used to be its own line, so a bands table became a vertical word list."""
    rows = [
        ["Days late", "Band", "Deadline", "Attempts"],
        ["1 to 7", "early", "12th business day", "1"],
        ["8 to 45", "late", "7th business day", "2"],
    ]
    doc = load_brief(write(tmp_path, "policy.docx", make_docx_with_table("Bands", rows, "End of table.")))
    assert doc.text.split("\n") == [
        "Bands",
        "Days late | Band | Deadline | Attempts",
        "1 to 7 | early | 12th business day | 1",
        "8 to 45 | late | 7th business day | 2",
        "End of table.",
    ]


def test_utf16_without_a_byte_order_mark_is_decoded(tmp_path: Path) -> None:
    for codec in ("utf-16-le", "utf-16-be"):
        text = "Memo: the callback window closes at 17:00."
        assert load_brief(write(tmp_path, f"{codec}.txt", text.encode(codec))).text == text
    assert decode_text(b"plain ascii") == "plain ascii"
