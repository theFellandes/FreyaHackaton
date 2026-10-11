"""What each document of a bank's case folder is: policy material, the service contract, test customers or examples.

A bank may hand over a folder of its own documents instead of one brief and an OpenAPI file: a brief, a README that says
what each file is, an analysis, announcements, questions and answers, a flow description, a service contract, test
customers (data for the team's mock of the bank's services) and example conversations, in Markdown, Word, PDF, Excel or
plain text, with names in any language. :func:`classify_document` sorts one file into a :data:`DocumentRole`:

=============  ==========================================================================================================
``policy``     what the agent must do: brief, README, analysis, announcements, questions and answers, flow, rules
``contract``   the service contract / API description (the tool API is read or derived from it)
``customers``  test customer records for the mock; never policy material (they are named in the trace only)
``examples``   example conversations; policy material labelled as illustrations, lower priority than the brief
``skipped``    what cannot be read as text: images (a flow drawing), legacy binary Office files, archives, media
=============  ==========================================================================================================

The decision is generic: keywords of the file name in several languages (accents and case folded, so ``SÖZLEŞME`` and
``sozlesme`` are the same word), then content cues for a neutral name (HTTP methods with endpoint paths = contract;
turn-by-turn speaker lines = examples; a table or JSON list of records with identifier and identity columns =
customers). Nothing here knows a particular bank or case.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

DocumentRole = Literal["policy", "contract", "customers", "examples", "skipped"]

#: Files that hold no readable text for the builder (a flow drawing, a scan, a recording, an archive).
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".webp", ".svg", ".heic", ".ico"})
UNREADABLE_SUFFIXES = frozenset({
    ".doc", ".xls", ".ppt", ".pptx", ".odp", ".key", ".zip", ".rar", ".7z", ".gz", ".tar", ".exe", ".dll", ".bin",
    ".mp3", ".mp4", ".wav", ".m4a", ".mov", ".avi", ".vsd", ".vsdx", ".psd", ".ai", ".sketch", ".fig",
})
#: Formats whose content is a table or a record list (a customers file by name needs no further evidence).
_TABULAR_SUFFIXES = frozenset({".csv", ".tsv", ".xlsx", ".json", ".yaml", ".yml"})

# --- keywords (folded: lower case, no accents; matched as whole tokens or, for long words, inside a token) ---------------
_BRIEF_WORDS = ("brief", "briefing")
_README_WORDS = ("readme", "benioku", "liesmich", "leame", "lisezmoi")
_CONTRACT_WORDS = ("sozlesme", "sozlesmesi", "contract", "contrat", "contrato", "kontrat", "vertrag", "openapi",
                   "swagger", "endpoint", "endpoints", "api", "apis", "postman", "wsdl", "entegrasyon", "integration")
_CUSTOMER_WORDS = ("musteri", "musteriler", "musterileri", "customer", "customers", "kunde", "kunden", "cliente",
                   "clientes", "client", "clients", "persona", "personas", "personalar", "borclu", "borclular",
                   "debtor", "debtors")
_CUSTOMER_DATA_WORDS = ("test", "list", "liste", "listesi", "data", "veri", "verisi", "verileri", "mock", "sahte",
                        "fake", "dummy", "kayit", "kayitlari", "records", "seed", "fixture", "fixtures")
_CONVERSATION_WORDS = ("konusma", "konusmalar", "konusmalari", "conversation", "conversations", "dialog", "dialogs",
                       "dialogue", "dialogues", "diyalog", "diyaloglar", "gorusme", "gorusmeler", "gorusmesi",
                       "transcript", "transcripts", "transkript", "chat", "chats", "gesprach", "conversacion",
                       "conversaciones", "dialogo", "dialogos")
_EXAMPLE_WORDS = ("example", "examples", "sample", "samples", "ornek", "ornekler", "ejemplo", "exemple")
_SCENARIO_WORDS = ("scenario", "scenarios", "senaryo", "senaryolar")
_SOURCE_FOLDERS = ("meeting", "meetings", "toplanti", "toplantilar", "email", "emails", "mail", "mails")
_POLICY_WORDS = ("analiz", "analizi", "analysis", "analyse", "duyuru", "duyurular", "duyurusu", "announcement",
                 "announcements", "soru", "sorular", "cevap", "cevaplar", "sss", "faq", "faqs", "qa", "question",
                 "questions", "answer", "answers", "akis", "akisi", "flow", "politika", "politikasi", "policy",
                 "policies", "kural", "kurallar", "kurallari", "rule", "rules", "prosedur", "proseduru", "procedure",
                 "procedures", "yonerge", "talimat", "talimatlar", "guideline", "guidelines", "memo", "iletisim",
                 "gereksinim", "requirements", "prompt", "prompts", "ivr", "meeting", "meetings", "toplanti",
                 "toplantilar", "minutes", "session", "oturum")

# --- content cues ----------------------------------------------------------------------------------------------------------
_ENDPOINT_RE = re.compile(r"\b(GET|POST|PUT|PATCH|DELETE)\b[`*\s]+(?:https?://[^\s/`]+)?(/[\w{}:.\-/]*)")
_SCHEMA_CUE_RE = re.compile(r'"type"\s*:|\b(?:request|response|istek|yan[\u0131i]t|body|g[\u00f6o]vde|payload|schema)\b',
                            re.IGNORECASE)
_SPEAKER_RE = re.compile(r"^\s*(?:[-*>]\s*)?\**\s*([^\W\d_][\w .'()-]{0,24}?)\s*\**\s*:\s*\**\s*\S", re.UNICODE)
_NOT_SPEAKERS = frozenset({
    "soru", "cevap", "question", "answer", "q", "a", "s", "c", "not", "note", "notlar", "notes", "ornek", "example",
    "kural", "rule", "adim", "step", "madde", "tarih", "date", "konu", "subject", "from", "to", "kimden", "kime", "cc",
    "bilgi", "info", "uyari", "warning", "onemli", "important", "aciklama", "description", "sonuc", "result", "amac",
    "purpose", "kaynak", "source", "durum", "status", "ornegin", "e g", "eg", "ie", "url", "http", "https", "tip",
    "type", "alan", "field", "deger", "value", "yanit", "response", "istek", "request", "hedef", "goal", "baslik",
    "title", "ozet", "summary", "sure", "time", "saat", "kosul", "condition", "eylem", "action", "cikti", "output",
})
_ID_COLUMNS = ("id", "no", "numara", "numarasi", "number", "tckn", "tcno", "kimlik", "ref", "kod", "code")
_PERSON_COLUMNS = ("ad", "adi", "isim", "name", "soyad", "soyadi", "surname", "dogum", "birth", "dob", "tel", "telefon",
                   "phone", "gsm", "email", "eposta", "adres", "address", "tckn", "kimlik")
_SPLIT_RE = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class ClassifiedDocument:
    """One file of a case folder and what it is (``reason`` says why, for the trace)."""

    path: Path
    role: DocumentRole
    reason: str


def fold(text: str) -> str:
    """Lower case without accents (``SÖZLEŞME`` -> ``sozlesme``; the dotted capital and the dotless small i too)."""
    text = text.replace("\u0130", "i").replace("\u0131", "i").casefold()
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def name_tokens(path: Path) -> list[str]:
    """The folded words of a file name (stem only)."""
    spaced = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", path.stem)
    return [token for token in _SPLIT_RE.split(fold(spaced)) if token]


def _has(tokens: Iterable[str], words: Iterable[str]) -> bool:
    token_list = list(tokens)
    for word in words:
        for token in token_list:
            if token == word or (len(word) >= 5 and word in token):
                return True
    return False


def document_priority(path: Path) -> int:
    """Reading order inside a folder: the brief first (0), then the README (1), then everything else (2)."""
    tokens = name_tokens(path)
    if _has(tokens, _BRIEF_WORDS):
        return 0
    if _has(tokens, _README_WORDS):
        return 1
    return 2


def classify_by_name(path: Path, *, root: Path | None = None) -> ClassifiedDocument | None:
    """The role the file name alone settles, or ``None`` when the name is neutral (or only says "customers")."""
    suffix = path.suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        return ClassifiedDocument(path, "skipped", "an image cannot be read as text")
    if suffix in UNREADABLE_SUFFIXES:
        return ClassifiedDocument(path, "skipped", f"{suffix} files cannot be read as text")
    tokens = name_tokens(path)
    if _has(tokens, _BRIEF_WORDS):
        return ClassifiedDocument(path, "policy", "brief")
    if _has(tokens, _README_WORDS):
        return ClassifiedDocument(path, "policy", "readme")
    parent_tokens = [token for part in path.relative_to(root).parts[:-1] for token in name_tokens(Path(part))] \
        if root is not None else []
    explicit_examples = _has(tokens, _EXAMPLE_WORDS) and _has(tokens, (*_CONVERSATION_WORDS, *_SCENARIO_WORDS))
    if explicit_examples or _has(parent_tokens, _EXAMPLE_WORDS):
        return ClassifiedDocument(path, "examples", "the file or folder explicitly says examples")
    if _has(tokens, _CONTRACT_WORDS):
        return ClassifiedDocument(path, "contract", "the name says service contract / API")
    if _has(tokens, _POLICY_WORDS):
        return ClassifiedDocument(path, "policy", "the name says policy material")
    if _has(tokens, _CUSTOMER_WORDS) and (suffix in _TABULAR_SUFFIXES or _has(tokens, _CUSTOMER_DATA_WORDS)):
        return ClassifiedDocument(path, "customers", "the name says test customers")
    if _has(parent_tokens, _SOURCE_FOLDERS):
        return ClassifiedDocument(path, "policy", "an authoritative source mail or meeting, not a sample call")
    if _has(tokens, _CONVERSATION_WORDS):
        return ClassifiedDocument(path, "examples", "the name says example conversations")
    return None


def classify_document(path: Path, text: str | None = None, *, root: Path | None = None) -> ClassifiedDocument:
    """Classify one file of a case folder (see the module docstring).

    Args:
        path: The file.
        text: Its converted text when the caller already has it; otherwise it is read when the name does not decide.
    """
    by_name = classify_by_name(path, root=root)
    if by_name is not None:
        return by_name
    content = text if text is not None else _read_text(path)
    customers_named = _has(name_tokens(path), _CUSTOMER_WORDS)
    by_content = classify_by_content(content)
    if by_content is not None:
        return ClassifiedDocument(path, by_content[0], by_content[1])
    if customers_named:  # "customer segments.md" with no records in it is policy prose about customers
        return ClassifiedDocument(path, "policy", "the name mentions customers but the content holds no records")
    return ClassifiedDocument(path, "policy", "default")


def classify_by_content(text: str) -> tuple[DocumentRole, str] | None:
    """The role the content shows (contract, examples or customers), or ``None``."""
    if not text.strip():
        return None
    endpoints = {match.group(2) for match in _ENDPOINT_RE.finditer(text)}
    if len(endpoints) >= 2 or (endpoints and _SCHEMA_CUE_RE.search(text)):
        return "contract", f"{len(endpoints)} HTTP endpoint(s) described"
    if _looks_like_dialogue(text):
        return "examples", "turn-by-turn dialogue"
    if _looks_like_records(text):
        return "customers", "a table of customer records"
    return None


def _looks_like_dialogue(text: str) -> bool:
    lines = [line for line in text.splitlines() if line.strip()]
    speakers: list[str] = []
    words = 0
    for line in lines:
        match = _SPEAKER_RE.match(line)
        if match is None:
            continue
        label = " ".join(_SPLIT_RE.split(fold(match.group(1)))).strip()
        if len(label) >= 2 and label not in _NOT_SPEAKERS:
            speakers.append(label)
            words += len(line[match.end(1):].split())
    if len(speakers) < 4 or len(speakers) < 0.4 * len(lines) or words < 3 * len(speakers):
        return False  # key-value lists (``Name: X``) are short; spoken turns are sentences
    counts = {label: speakers.count(label) for label in set(speakers)}
    frequent = [label for label, count in counts.items() if count >= 2]
    return len(frequent) >= 2 and len(counts) <= 6


def _header_says_people(cells: Iterable[str]) -> bool:
    tokens = {token for cell in cells for token in _SPLIT_RE.split(fold(cell)) if token}
    return bool(tokens & set(_ID_COLUMNS)) and bool(tokens & set(_PERSON_COLUMNS))


def _looks_like_records(text: str) -> bool:
    stripped = text.strip()
    if stripped[:1] in "[{":
        try:
            data = json.loads(stripped)
        except ValueError:
            data = None
        if data is not None:
            return _records_in_json(data)
    lines = [line.strip() for line in stripped.splitlines() if line.strip()]
    for index, line in enumerate(lines[:-2]):
        separator = next((sep for sep in ("|", "\t", ";", ",") if line.count(sep) >= 2), None)
        if separator is None:
            continue
        header = [cell for cell in line.strip("|").split(separator)]
        if not _header_says_people(header):
            continue
        rows = [row for row in lines[index + 1:] if row.count(separator) >= 2 and not set(row) <= set("|-: ")]
        if len(rows) >= 2:
            return True
    return False


def _records_in_json(data: object) -> bool:
    if isinstance(data, dict):
        return any(_records_in_json(value) for value in data.values() if isinstance(value, list | dict))
    if isinstance(data, list):
        records = [item for item in data if isinstance(item, dict)]
        return len(records) >= 2 and _header_says_people(str(key) for key in records[0])
    return False


def _read_text(path: Path) -> str:
    """The converted text of ``path`` for the content cues (empty when it cannot be read)."""
    from builder.ingest.brief_loader import (
        load_brief,  # brief_loader imports builder.inputs, which imports this module
    )

    try:
        return load_brief(path).text
    except Exception as exc:  # an unreadable file is classified by its name; the loader reports it later
        logger.debug("could not read %s for classification: %s", path.name, exc)
        return ""


__all__ = [
    "IMAGE_SUFFIXES",
    "UNREADABLE_SUFFIXES",
    "ClassifiedDocument",
    "DocumentRole",
    "classify_by_content",
    "classify_by_name",
    "classify_document",
    "document_priority",
    "fold",
    "name_tokens",
]
