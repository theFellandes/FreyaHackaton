"""Command-line inputs: which file is the tool API, which files are the brief, and what the build slot is called.

Two command shapes are supported (and mixtures of them):

* ``python3 builder/build.py {brief_path}... {tool_api_path} [{mock_url}]`` - brief file(s) and an OpenAPI file;
* ``python3 builder/build.py {case_folder} {mock_url}`` - a folder of the bank's own documents (brief, README,
  analysis, announcements, questions and answers, a service contract, test customers, example conversations ...).

:func:`classify_inputs` turns the raw arguments into an :class:`InputBundle`:

1. An argument that is an ``http(s)`` URL is the **tool base URL** (the team's mock of the bank's services; every tool
   request of the agent goes there, see :func:`builder.ingest.tool_api.with_base_url`). It may also come from
   ``--tool-base-url`` or the ``TOOL_BASE_URL`` setting; the command line wins over the setting, and two different
   URLs on the command line are an error. Without one, the contract's own server is kept.
2. Every other argument is resolved; a directory is expanded to the files below it (recursively; hidden files,
   ``__MACOSX``, ``~$`` lock files and editor/OS junk skipped; the brief first, then the README, then the rest sorted by
   relative path).
3. A file is **the tool API** when it parses as a JSON or YAML mapping with a top-level ``openapi`` or ``swagger``
   key holding a version number (``3.0.3``, ``2.0``) - whatever its extension. At most one tool API is allowed;
   copies with the same content (a folder that holds the API plus the same API passed again) count once.
4. Every other file is classified (:mod:`builder.ingest.case_folder`): files found in a directory by name and content,
   files named on the command line by name only. **Contract** documents (a service contract in prose) are what the
   tool API is derived from when no OpenAPI file is given (``contract_paths``), otherwise reference only
   (``reference_paths``); **customers** (test data for the mock) never become brief material (``customer_paths``);
   **examples** (example conversations) are brief material placed last and labelled (``example_paths``); unreadable
   files (images, archives) are skipped (``skipped_paths``). Everything else is **brief material** (at least one file
   is required), in command-line order. Either a tool API or a contract document is required.
5. The **slot** (the label under which the workflow is created once and updated afterwards) is the explicit
   ``--slot`` value, else ``settings.build_slot``, else - for a single case folder - a slug of the folder's name, else a
   slug of the brief file stems joined with ``-`` (``brief-a.md`` + ``brief-b.md`` -> ``brief-a-brief-b``). A derived
   slot whose Freya state entry was recorded for *other* inputs (two different briefs both called ``brief.md``) gets the
   first 8 hex digits of the inputs' fingerprint appended, so the second build never overwrites the first one's
   workflow (audit AUD-05). A rerun of the same inputs keeps its slot; an entry without a fingerprint (written before
   it existed) is adopted as it is.

Problems are reported as :class:`InputError` with a message that tells the operator what to fix.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import yaml

if TYPE_CHECKING:
    from builder.settings import Settings

logger = logging.getLogger(__name__)

#: Longest derived slot.
MAX_SLOT_LENGTH = 48
#: Longest explicit slot accepted.
MAX_EXPLICIT_SLOT_LENGTH = 64
#: Files larger than this are never parsed as a tool API (they are treated as brief material).
MAX_TOOL_API_BYTES = 20 * 1024 * 1024
#: File names skipped when a directory is expanded.
_JUNK_NAMES = frozenset({"thumbs.db", "desktop.ini", "__pycache__", "__macosx"})
_URL_ARG_RE = re.compile(r"^https?://", re.IGNORECASE)
_EXPLICIT_SLOT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_VERSION_RE = re.compile(r"^\d+(\.\d+)*\S*$")
#: Hex digits of the inputs' fingerprint appended to a derived slot that collides with other inputs.
SLOT_HASH_LENGTH = 8
#: Slots a derived slug must never take over (the kit seeds a ``base`` workflow).
_RESERVED_SLOTS = frozenset({"base"})


class InputError(ValueError):
    """The inputs cannot be used as given (missing file, no tool API, unreadable brief, ...)."""


@dataclass
class InputBundle:
    """The classified inputs of one build.

    Attributes:
        brief_paths: Brief material, absolute paths, in command-line order (directories expanded; example
            conversations last).
        tool_api_path: The tool API file (OpenAPI / Swagger, JSON or YAML); ``None`` when the tool API is derived from
            the contract documents.
        slot: The build slot (workflow label used by ``freya.save_workflow``).
        inputs_sha: SHA-256 of the brief files (in order) and the tool API, BOM and line endings normalised
            (:func:`input_fingerprint`); recorded with the slot's Freya state entry.
        contract_paths: Service contract documents the tool API is derived from (only without a tool API file).
        reference_paths: Contract documents next to a tool API file (not read: the tool API file is the contract).
        customer_paths: Test customer data for the mock (never brief material; named in the trace only).
        example_paths: The example conversations among ``brief_paths`` (labelled as illustrations).
        skipped_paths: Files that cannot be read as text (images, archives ...).
        tool_base_url: Where every tool request goes (the team's mock); ``None`` keeps the contract's server.
        case_folder: The folder when the inputs are one case folder.
    """

    brief_paths: list[Path]
    tool_api_path: Path | None
    slot: str
    inputs_sha: str = ""
    contract_paths: list[Path] = field(default_factory=list)
    reference_paths: list[Path] = field(default_factory=list)
    customer_paths: list[Path] = field(default_factory=list)
    example_paths: list[Path] = field(default_factory=list)
    skipped_paths: list[Path] = field(default_factory=list)
    tool_base_url: str | None = None
    case_folder: Path | None = None

    def as_dict(self) -> dict[str, Any]:
        """JSON-friendly form for graph state and traces (the case-folder keys only when they have a value)."""
        data: dict[str, Any] = {
            "briefs": [path.as_posix() for path in self.brief_paths],
            "tool_api": self.tool_api_path.as_posix() if self.tool_api_path is not None else "",
            "slot": self.slot,
            "inputs_sha": self.inputs_sha,
        }
        for key, paths in (("contracts", self.contract_paths), ("reference", self.reference_paths),
                           ("customers", self.customer_paths), ("examples", self.example_paths),
                           ("skipped", self.skipped_paths)):
            if paths:
                data[key] = [path.as_posix() for path in paths]
        if self.tool_base_url:
            data["tool_base_url"] = self.tool_base_url
        if self.case_folder is not None:
            data["case_folder"] = self.case_folder.as_posix()
        return data


def classify_inputs(
    args: Iterable[str | Path],
    slot: str | None = None,
    *,
    settings: Settings | None = None,
    tool_base_url: str | None = None,
) -> InputBundle:
    """Split the command-line arguments into brief files, the tool API (or contract documents) and the tool base URL,
    and choose the slot (see the module docstring).

    Args:
        args: File or directory paths and at most one ``http(s)`` URL, as given on the command line.
        slot: An explicit slot (``--slot``); wins over everything else.
        settings: Source of ``build_slot`` and ``tool_base_url``; :func:`builder.settings.get_settings` when omitted.
        tool_base_url: The ``--tool-base-url`` value, if any.

    Returns:
        The classified :class:`InputBundle`.

    Raises:
        InputError: If a path does not exist, no tool API and no contract document or several tool API files are
            found, no brief file remains, two different base URLs are given, a URL is malformed, or the explicit slot
            is not a safe label.
    """
    from builder.ingest.case_folder import (  # imports nothing of builder.inputs
        classify_by_name,
        classify_document,
    )

    raw_args = [str(arg).strip() for arg in args]
    url_args = [arg for arg in raw_args if _URL_ARG_RE.match(arg)]
    base_url = _choose_base_url(url_args, tool_base_url, settings)
    entries = _expand([arg for arg in raw_args if arg and not _URL_ARG_RE.match(arg)])
    files = [path for path, _ in entries]
    tool_apis = [path for path in files if is_tool_api(path)]
    distinct, duplicates = _distinct_tool_apis(tool_apis)
    if duplicates:
        logger.warning(
            "Ignoring %d tool API file(s) identical to %s: %s", len(duplicates), distinct[0].name, _names(duplicates)
        )
    if len(distinct) > 1:
        raise InputError(
            f"Expected exactly one tool API file but found {len(distinct)}: {_names(distinct)}. "
            "Pass only the tool API that belongs to this brief."
        )
    tool_api = distinct[0] if distinct else None
    roles: dict[Path, str] = {}
    for path, root in entries:
        if path in tool_apis:
            continue
        found = classify_document(path) if root is not None else classify_by_name(path)
        roles[path] = found.role if found is not None else "policy"
        if found is not None and found.role != "policy":
            logger.info("input %s: %s (%s)", path.name, found.role, found.reason)
    contracts = [path for path, role in roles.items() if role == "contract"]
    if tool_api is None and not contracts:
        raise InputError(
            "No tool API file found among the inputs. Pass the bank's OpenAPI/Swagger file (JSON or YAML with a "
            "top-level 'openapi' or 'swagger' key) or its service contract document next to the brief. "
            f"Inputs seen: {_names(files)}"
        )
    examples = [path for path, role in roles.items() if role == "examples"]
    briefs = [path for path, role in roles.items() if role == "policy"] + examples
    if not briefs:
        only = tool_api.name if tool_api is not None else _names(contracts)
        raise InputError(f"No brief file given: the only input is the tool API {only}.")
    fingerprint = input_fingerprint(briefs, tool_api, contracts=() if tool_api else contracts)
    directories = list(dict.fromkeys(root for _, root in entries if root is not None))
    folder = directories[0] if len(directories) == 1 and len(entries) == len(
        [entry for entry in entries if entry[1] is not None]) else None
    case_like = folder is not None and any(role != "policy" for role in roles.values())
    stems_from = [folder] if case_like and folder is not None else briefs
    return InputBundle(
        brief_paths=briefs,
        tool_api_path=tool_api,
        slot=choose_slot(stems_from, slot, settings, fingerprint=fingerprint),
        inputs_sha=fingerprint,
        contract_paths=contracts if tool_api is None else [],
        reference_paths=contracts if tool_api is not None else [],
        customer_paths=[path for path, role in roles.items() if role == "customers"],
        example_paths=examples,
        skipped_paths=[path for path, role in roles.items() if role == "skipped"],
        tool_base_url=base_url,
        case_folder=folder if case_like else None,
    )


def normalise_base_url(value: str) -> str:
    """Check a tool base URL (``http(s)://host[:port][/prefix]``) and drop trailing slashes.

    Raises:
        InputError: If it is not an absolute http(s) URL with a host, or it has a query, a fragment or spaces.
    """
    text = value.strip()
    parts = urlsplit(text)
    if (
        parts.scheme.lower() not in ("http", "https")
        or not parts.hostname
        or parts.query
        or parts.fragment
        or any(char.isspace() for char in text)
    ):
        raise InputError(
            f"Invalid tool base URL {text!r}: give the mock's address as http(s)://host[:port][/path], "
            "without query or fragment."
        )
    return f"{parts.scheme.lower()}://{parts.netloc}{parts.path}".rstrip("/")


def _choose_base_url(url_args: Sequence[str], flag: str | None, settings: Settings | None) -> str | None:
    """The command line's base URL (positional or ``--tool-base-url``; they must agree), else the setting."""
    given = [normalise_base_url(url) for url in url_args]
    if flag and flag.strip():
        given.append(normalise_base_url(flag))
    distinct = list(dict.fromkeys(given))
    if len(distinct) > 1:
        raise InputError(f"Two different tool base URLs were given ({', '.join(distinct)}); pass only the mock's URL.")
    if distinct:
        return distinct[0]
    if settings is None:
        from builder.settings import get_settings

        settings = get_settings()
    configured = getattr(settings, "tool_base_url", None)
    return normalise_base_url(configured) if configured else None


def _normalised_bytes(path: Path) -> bytes:
    """The file's bytes without a UTF-8 BOM and with CRLF / CR line endings turned into LF (unreadable: empty)."""
    try:
        data = path.read_bytes()
    except OSError:
        return b""
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    return data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def input_fingerprint(briefs: Sequence[Path], tool_api: Path | None, *, contracts: Sequence[Path] = ()) -> str:
    """SHA-256 over the brief files in order, then the tool API, then the contract documents a tool API is derived from
    (BOM and line endings normalised, so a checkout with other line endings gives the same value).

    The tool base URL is not part of it: the same documents built against another mock update the same workflow.
    """
    digest = hashlib.sha256()
    for path in [*briefs, *([tool_api] if tool_api is not None else []), *contracts]:
        data = _normalised_bytes(path)
        digest.update(f"{len(data)}:".encode())
        digest.update(data)
    return digest.hexdigest()


def _distinct_tool_apis(tool_apis: Sequence[Path]) -> tuple[list[Path], list[Path]]:
    """Group tool API files by parsed content: ``(first of each group in input order, the identical copies)``."""
    distinct: list[Path] = []
    duplicates: list[Path] = []
    seen: set[str] = set()
    for path in tool_apis:
        key = _api_fingerprint(path)
        if key in seen:
            duplicates.append(path)
            continue
        seen.add(key)
        distinct.append(path)
    return distinct, duplicates


def _api_fingerprint(path: Path) -> str:
    """Content identity of a tool API: its parsed document (so BOM, line endings and whitespace do not matter)."""
    try:
        document = _parse_mapping(path.read_bytes().decode("utf-8-sig"))
    except (OSError, UnicodeDecodeError):
        document = None
    if document is None:  # cannot happen for a detected API; never merge two unreadable files
        return f"path:{path}"
    canonical = json.dumps(document, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def expand_paths(args: Iterable[str | Path]) -> list[Path]:
    """Resolve the arguments into a list of unique files (directories expanded, order kept).

    Raises:
        InputError: If an argument does not exist or a directory contains no usable file.
    """
    return [path for path, _ in _expand(args)]


def _expand(args: Iterable[str | Path]) -> list[tuple[Path, Path | None]]:
    """``(file, the directory it was found in or None for a file argument)``, unique, in order."""
    files: list[tuple[Path, Path | None]] = []
    seen: set[Path] = set()
    for raw in args:
        text = str(raw).strip()
        if not text:
            continue
        path = Path(text).expanduser()
        if not path.exists():
            raise InputError(f"Input not found: {text}")
        path = path.resolve()
        found = _files_in(path) if path.is_dir() else [path]
        if path.is_dir() and not found:
            raise InputError(f"Input directory {text} contains no files.")
        for item in found:
            if item not in seen:
                seen.add(item)
                files.append((item, path if path.is_dir() else None))
    if not files:
        raise InputError(
            "No input files given. Usage: build.py <brief>... <tool_api> [<mock_url>] or build.py <case_folder> <mock_url>"
        )
    return files


def _files_in(directory: Path) -> list[Path]:
    """Files below ``directory``: the brief first, then the README, then the rest sorted by relative path; hidden
    entries, ``__MACOSX``, ``~$`` lock files and OS/editor junk are skipped."""
    from builder.ingest.case_folder import document_priority

    found = []
    for item in directory.rglob("*"):
        relative = item.relative_to(directory)
        if any(_is_junk(part) for part in relative.parts):
            continue
        if item.is_file():
            found.append(item)
    return sorted(
        found, key=lambda item: (document_priority(item), item.relative_to(directory).as_posix().lower())
    )


def _is_junk(name: str) -> bool:
    lowered = name.lower()
    return name.startswith((".", "~$")) or lowered in _JUNK_NAMES or lowered.endswith((".pyc", ".tmp", "~"))


def is_tool_api(path: Path) -> bool:
    """True when ``path`` is a JSON or YAML mapping with a top-level ``openapi`` or ``swagger`` version key.

    The key's value must look like a version (``3.0.3``, ``"2.0"``, ``3.1``): a Markdown brief whose heading is a
    YAML comment and which happens to contain a line ``openapi: <prose>`` is therefore still read as a brief.
    """
    try:
        if path.stat().st_size > MAX_TOOL_API_BYTES:
            return False
        text = path.read_bytes().decode("utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return False
    document = _parse_mapping(text)
    if document is None:
        return False
    return any(_VERSION_RE.match(str(document[key])) for key in ("openapi", "swagger") if key in document)


def _parse_mapping(text: str) -> dict[str, Any] | None:
    """Parse ``text`` as JSON, else YAML; return it only when the result is a mapping."""
    try:
        document = json.loads(text)
    except ValueError:
        try:
            document = yaml.safe_load(text)
        except yaml.YAMLError:
            return None
    return document if isinstance(document, dict) else None


def choose_slot(
    briefs: Sequence[Path],
    explicit: str | None,
    settings: Settings | None = None,
    *,
    fingerprint: str | None = None,
) -> str:
    """Pick the slot: ``explicit`` > ``settings.build_slot`` > :func:`slug_for` of the brief stems.

    A derived slot that the Freya state file records for other inputs (a different ``inputs_sha``), or a reserved one,
    gets ``-<first 8 hex digits of fingerprint>`` appended (see the module docstring).

    Raises:
        InputError: If the explicit (or configured) slot is not a safe label.
    """
    if explicit is not None and explicit.strip():
        return _check_explicit_slot(explicit.strip(), "--slot")
    if settings is None:
        from builder.settings import get_settings

        settings = get_settings()
    if settings.build_slot:
        return _check_explicit_slot(settings.build_slot, "BUILD_SLOT")
    base = slug_for([path.stem for path in briefs])
    if fingerprint and (base in _RESERVED_SLOTS or _recorded_for_other_inputs(base, fingerprint, settings)):
        suffix = fingerprint[:SLOT_HASH_LENGTH]
        slot = f"{base[: MAX_SLOT_LENGTH - SLOT_HASH_LENGTH - 1].rstrip('-')}-{suffix}"
        logger.warning(
            "slot %r is recorded for other inputs (a different brief with the same file name); using %r", base, slot
        )
        return slot
    return base


def _recorded_for_other_inputs(slot: str, fingerprint: str, settings: Settings) -> bool:
    """True when the Freya state file holds ``slot`` with an ``inputs_sha`` that differs from ``fingerprint``.

    Tolerant by design: a missing or unreadable state file, or an entry without ``inputs_sha`` (recorded before the
    fingerprint existed), is no collision (the deploy reports a broken state file itself).
    """
    entry = _state_entries(settings).get(slot)
    if not isinstance(entry, dict):
        return False
    recorded = entry.get("inputs_sha")
    return isinstance(recorded, str) and bool(recorded) and recorded != fingerprint


def _state_entries(settings: Settings) -> dict[str, Any]:
    from builder import paths

    configured = getattr(settings, "freya_state_file", None)
    path = Path(configured) if configured else paths.REPO_ROOT / ".freya-agents.json"
    if not path.is_absolute():
        path = paths.REPO_ROOT / path
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def _check_explicit_slot(slot: str, source: str) -> str:
    if len(slot) > MAX_EXPLICIT_SLOT_LENGTH or not _EXPLICIT_SLOT_RE.match(slot):
        raise InputError(
            f"Invalid slot {slot!r} from {source}: use letters, digits, '.', '_' or '-' "
            f"(at most {MAX_EXPLICIT_SLOT_LENGTH} characters, starting with a letter or digit)."
        )
    return slot


def slug_for(stems: Iterable[str]) -> str:
    """Join file stems with ``-`` and reduce them to ``[a-z0-9-]``, at most :data:`MAX_SLOT_LENGTH` characters.

    >>> slug_for(["Brief A", "brief_b"])
    'brief-a-brief-b'
    """
    text = "-".join(stems).replace("\u0130", "I").replace("\u0131", "i")
    text = "".join(char for char in unicodedata.normalize("NFKD", text) if not unicodedata.combining(char))
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    slug = slug[:MAX_SLOT_LENGTH].rstrip("-")
    return slug or "brief"


def _names(paths: Sequence[Path]) -> str:
    return ", ".join(path.name for path in paths) or "(none)"


__all__ = [
    "MAX_SLOT_LENGTH",
    "InputBundle",
    "InputError",
    "choose_slot",
    "classify_inputs",
    "expand_paths",
    "input_fingerprint",
    "is_tool_api",
    "normalise_base_url",
    "slug_for",
]
