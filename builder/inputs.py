"""Command-line inputs: which file is the tool API, which files are the brief, and what the build slot is called.

The organisers run ``python3 builder/build.py {brief_path} {tool_api_path}``, but a brief may also arrive as several
files (Brief B builds on Brief A; raw bank material can be a mail, a policy extract, a memo that amends it and a code
list) and the arguments may name directories. :func:`classify_inputs` turns the raw arguments into an
:class:`InputBundle`:

1. Every argument is resolved; a directory is expanded to the files below it (recursively, sorted by relative path,
   hidden files and editor/OS junk skipped).
2. A file is **the tool API** when it parses as a JSON or YAML mapping with a top-level ``openapi`` or ``swagger``
   key holding a version number (``3.0.3``, ``2.0``) - whatever its extension. Exactly one tool API is required.
3. Every other file is **brief material** (at least one is required), kept in command-line order.
4. The **slot** (the label under which the workflow is created once and updated afterwards) is the explicit
   ``--slot`` value, else ``settings.build_slot``, else a slug of the brief file stems joined with ``-``
   (``brief-a.md`` + ``brief-b.md`` -> ``brief-a-brief-b``).

Problems are reported as :class:`InputError` with a message that tells the operator what to fix.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

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
_JUNK_NAMES = frozenset({"thumbs.db", "desktop.ini", "__pycache__"})
_EXPLICIT_SLOT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_VERSION_RE = re.compile(r"^\d+(\.\d+)*\S*$")


class InputError(ValueError):
    """The inputs cannot be used as given (missing file, no tool API, unreadable brief, ...)."""


@dataclass
class InputBundle:
    """The classified inputs of one build.

    Attributes:
        brief_paths: Brief material, absolute paths, in command-line order (directories expanded).
        tool_api_path: The tool API file (OpenAPI / Swagger, JSON or YAML).
        slot: The build slot (workflow label used by ``freya.save_workflow``).
    """

    brief_paths: list[Path]
    tool_api_path: Path
    slot: str

    def as_dict(self) -> dict[str, Any]:
        """JSON-friendly form for graph state and traces."""
        return {
            "briefs": [path.as_posix() for path in self.brief_paths],
            "tool_api": self.tool_api_path.as_posix(),
            "slot": self.slot,
        }


def classify_inputs(
    args: Iterable[str | Path], slot: str | None = None, *, settings: Settings | None = None
) -> InputBundle:
    """Split command-line paths into brief files and the tool API file, and choose the slot.

    Args:
        args: File or directory paths, as given on the command line.
        slot: An explicit slot (``--slot``); wins over everything else.
        settings: Source of ``build_slot``; :func:`builder.settings.get_settings` when omitted.

    Returns:
        The classified :class:`InputBundle`.

    Raises:
        InputError: If a path does not exist, no or several tool API files are found, no brief file remains, or the
            explicit slot is not a safe label.
    """
    files = expand_paths(args)
    tool_apis = [path for path in files if is_tool_api(path)]
    if not tool_apis:
        raise InputError(
            "No tool API file found among the inputs. Pass the bank's OpenAPI/Swagger file (JSON or YAML with a "
            f"top-level 'openapi' or 'swagger' key) next to the brief. Inputs seen: {_names(files)}"
        )
    if len(tool_apis) > 1:
        raise InputError(
            f"Expected exactly one tool API file but found {len(tool_apis)}: {_names(tool_apis)}. "
            "Pass only the tool API that belongs to this brief."
        )
    tool_api = tool_apis[0]
    briefs = [path for path in files if path != tool_api]
    if not briefs:
        raise InputError(f"No brief file given: the only input is the tool API {tool_api.name}.")
    return InputBundle(brief_paths=briefs, tool_api_path=tool_api, slot=choose_slot(briefs, slot, settings))


def expand_paths(args: Iterable[str | Path]) -> list[Path]:
    """Resolve the arguments into a list of unique files (directories expanded, order kept).

    Raises:
        InputError: If an argument does not exist or a directory contains no usable file.
    """
    files: list[Path] = []
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
            if item not in files:
                files.append(item)
    if not files:
        raise InputError("No input files given. Usage: build.py <brief>... <tool_api>")
    return files


def _files_in(directory: Path) -> list[Path]:
    """Files below ``directory`` sorted by relative path, skipping hidden entries and OS/editor junk."""
    found = []
    for item in directory.rglob("*"):
        relative = item.relative_to(directory)
        if any(_is_junk(part) for part in relative.parts):
            continue
        if item.is_file():
            found.append(item)
    return sorted(found, key=lambda item: item.relative_to(directory).as_posix().lower())


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


def choose_slot(briefs: Sequence[Path], explicit: str | None, settings: Settings | None = None) -> str:
    """Pick the slot: ``explicit`` > ``settings.build_slot`` > :func:`slug_for` of the brief stems.

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
    return slug_for([path.stem for path in briefs])


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
    slug = re.sub(r"[^a-z0-9]+", "-", "-".join(stems).lower()).strip("-")
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
    "is_tool_api",
    "slug_for",
]
