"""Invented test data for case-folder inputs: a Turkish-like bank folder, Office files built with ``zipfile``, and a
contract whose operations and fields carry non-English names.

Nothing here comes from a real case: the documents are written for the tests, the tool contract is the kit's practice
API with every path renamed and some field names translated (so the fixture policies of ``tests/fixtures`` still
compile against it).
"""

from __future__ import annotations

import copy
import io
import json
import zipfile
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
KIT_API = REPO / "tools" / "openapi.json"

#: Kit path -> invented path of the same operation.
PATHS = {
    "/verify_identity": "/musteri/kimlik-dogrula",
    "/get_account": "/musteri/hesap-ozeti",
    "/create_promise": "/odeme/taahhut",
    "/add_note": "/kayit/not",
    "/schedule_callback": "/arama/geri-ara",
    "/transfer_to_agent": "/arama/aktar",
    "/close_call": "/arama/sonlandir",
}
#: Kit request field -> invented field name (enum values and response fields stay, the fixture IRs refer to them).
FIELDS = {"customer_id": "musteri_no", "answer": "cevap", "field": "soru", "amount": "tutar", "date": "tarih"}
CONTRACT_SERVER = "https://servis.banka.example/v1"


def make_docx(paragraphs: list[str], rows: list[list[str]] = ()) -> bytes:  # type: ignore[assignment]
    """A minimal Word file: paragraphs, then one table."""
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

    def paragraph(text: str) -> str:
        return f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"

    table = "".join(
        "<w:tr>" + "".join(f"<w:tc>{paragraph(cell)}</w:tc>" for cell in row) + "</w:tr>" for row in rows
    )
    body = "".join(paragraph(text) for text in paragraphs) + (f"<w:tbl>{table}</w:tbl>" if rows else "")
    document = f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="{ns}"><w:body>{body}</w:body></w:document>'
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", document.encode("utf-8"))
    return buffer.getvalue()


def make_xlsx(rows: list[list[str]]) -> bytes:
    """A minimal Excel file with one sheet of inline strings."""
    main = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    xml_rows = []
    for r, row in enumerate(rows, start=1):
        cells = "".join(
            f'<c r="{chr(65 + c)}{r}" t="inlineStr"><is><t>{value}</t></is></c>' for c, value in enumerate(row)
        )
        xml_rows.append(f'<row r="{r}">{cells}</row>')
    sheet = f'<worksheet xmlns="{main}"><sheetData>{"".join(xml_rows)}</sheetData></worksheet>'
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("xl/workbook.xml", f'<workbook xmlns="{main}"><sheets><sheet name="Liste" sheetId="1"/></sheets></workbook>')
        archive.writestr("xl/worksheets/sheet1.xml", sheet.encode("utf-8"))
    return buffer.getvalue()


def _rename(schema: Any) -> None:
    if not isinstance(schema, dict):
        return
    properties = schema.get("properties")
    if isinstance(properties, dict):
        schema["properties"] = {FIELDS.get(name, name): value for name, value in properties.items()}
        if "required" in schema:
            schema["required"] = [FIELDS.get(name, name) for name in schema["required"]]
        for value in schema["properties"].values():
            _rename(value)
    _rename(schema.get("items"))


def turkish_api_document(*, roles: bool = True, extra_operation: bool = True) -> dict[str, Any]:
    """The kit API with invented paths and field names; ``roles`` adds an ``x-freya-role`` to every operation."""
    kit = json.loads(KIT_API.read_text(encoding="utf-8"))
    document: dict[str, Any] = {**copy.deepcopy(kit), "servers": [{"url": CONTRACT_SERVER}], "paths": {}}
    document["info"] = {"title": "Banka servisleri", "version": "1.0"}
    for kit_path, item in kit["paths"].items():
        item = copy.deepcopy(item)
        operation = item["post"]
        operation["operationId"] = PATHS[kit_path].strip("/").replace("/", "_").replace("-", "_")
        if roles:
            operation["x-freya-role"] = kit_path.strip("/")
        _rename(operation["requestBody"]["content"]["application/json"]["schema"])
        document["paths"][PATHS[kit_path]] = item
    if extra_operation:
        document["paths"]["/kampanya/sorgula"] = {
            "post": {
                "operationId": "kampanya_sorgula",
                "summary": "Look up the campaigns the customer may join.",
                **({"x-freya-role": "other"} if roles else {}),
                "requestBody": {"content": {"application/json": {"schema": {
                    "type": "object", "properties": {"musteri_no": {"type": "string"}}, "required": ["musteri_no"]}}}},
                "responses": {"200": {"description": "ok", "content": {"application/json": {"schema": {
                    "type": "object", "properties": {"basarili": {"type": "boolean"}}}}}}},
            }
        }
    return document


def _field_lines(schema: dict[str, Any], indent: str = "- ") -> list[str]:
    lines = []
    for name, spec in (schema.get("properties") or {}).items():
        text = f"{indent}`{name}` ({spec.get('type', 'object')})"
        if spec.get("enum"):
            text += " değerler: " + ", ".join(f"`{value}`" for value in spec["enum"])
        lines.append(text)
        if spec.get("type") == "array" and isinstance(spec.get("items"), dict):
            lines.extend(_field_lines(spec["items"], "  " + indent))
    return lines


def contract_markdown(document: dict[str, Any]) -> str:
    """A prose service contract describing ``document`` (paths, methods, field names, enum values)."""
    lines = ["# Servis Sözleşmesi", "", f"Sunucu adresi: {CONTRACT_SERVER}", ""]
    for path, item in document["paths"].items():
        for method, operation in item.items():
            lines += [f"## {method.upper()} {path}", "", operation.get("summary") or "İşlem.", "", "İstek gövdesi:"]
            schema = operation["requestBody"]["content"]["application/json"]["schema"]
            lines += _field_lines(schema)
            lines += ["", "Yanıt (200):"]
            response = operation["responses"]["200"].get("content", {}).get("application/json", {}).get("schema", {})
            lines += _field_lines(response)
            lines.append("")
    return "\n".join(lines)


BRIEF = """# Görev

Gecikmiş borcu olan müşteriyi arayın. Önce kimliği doğrulayın, sonra borcu söyleyin ve ödeme sözü alın.
"""
README = """# Bu klasör

- BRIEF.md: görev tanımı (önce okuyun)
- analiz.docx: iş analizi
- servis-sozlesmesi.md: servis sözleşmesi
- test-musterileri.xlsx: sahte servis için test müşterileri
- ornek-konusmalar/: örnek konuşmalar
"""
DIALOGUE = """Temsilci: Merhaba, ben bankanın dijital asistanıyım.
Müşteri: Merhaba.
Temsilci: Doğum tarihinizi söyler misiniz?
Müşteri: On iki mart seksen beş.
Temsilci: Teşekkürler, ödemeniz gecikmiş görünüyor.
Müşteri: Cuma günü ödeyebilirim.
"""
CUSTOMERS = [
    ["musteri_no", "ad", "dogum_tarihi", "tel_son4"],
    ["M-1001", "Deneme Kişi", "1985-03-12", "1234"],
    ["M-1002", "Örnek Kişi", "1990-07-01", "9876"],
]


def write_case_folder(root: Path, *, openapi: dict[str, Any] | None = None, brief: str = BRIEF,
                      brief_name: str = "BRIEF.md") -> Path:
    """An invented bank case folder below ``root`` (documents of every supported kind, plus junk and an image)."""
    folder = root / "Banka-Vaka-Klasörü"
    (folder / "ornek-konusmalar").mkdir(parents=True)
    (folder / "duyurular").mkdir()
    (folder / "__MACOSX").mkdir()
    (folder / brief_name).write_text(brief, encoding="utf-8")
    (folder / "README.md").write_text(README, encoding="utf-8")
    (folder / "analiz.docx").write_bytes(make_docx(
        ["İş analizi: arama akışı", "Kimlik doğrulama üç denemeyle sınırlıdır."],
        [["Adım", "Açıklama"], ["1", "Kimlik doğrulama"], ["2", "Borç bilgisi"]],
    ))
    (folder / "duyurular" / "duyuru-01.md").write_text("# Duyuru\nHafta sonu geri arama yapılmaz.\n", encoding="utf-8")
    (folder / "soru-cevap.txt").write_text("S: Taksit yapılabilir mi?\nC: Hayır, yalnızca ödeme sözü alınır.\n",
                                           encoding="utf-8")
    document = openapi if openapi is not None else turkish_api_document()
    (folder / "servis-sozlesmesi.md").write_text(contract_markdown(document), encoding="utf-8")
    if openapi is not None:
        (folder / "servis.openapi.json").write_text(json.dumps(openapi, ensure_ascii=False, indent=2), encoding="utf-8")
    (folder / "test-musterileri.xlsx").write_bytes(make_xlsx(CUSTOMERS))
    (folder / "ornek-konusmalar" / "konusma-1.md").write_text(DIALOGUE, encoding="utf-8")
    (folder / "ornek-konusmalar" / "konusma-2.txt").write_text(DIALOGUE, encoding="utf-8")
    (folder / "akis-semasi.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    (folder / "__MACOSX" / "._BRIEF.md").write_bytes(b"\x00\x05\x16\x07junk")
    (folder / "~$analiz.docx").write_bytes(b"lock")
    (folder / ".DS_Store").write_bytes(b"\x00\x00\x00\x01Bud1")
    return folder
