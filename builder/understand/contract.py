"""Derive the tool API (OpenAPI 3.0 JSON) from a service contract written in prose.

A bank may describe its services in a Word / PDF / Markdown contract instead of an OpenAPI file. :func:`derive_tool_api`
asks the model ONCE to write the OpenAPI document (prompt ``contract/openapi_from_contract.v1.j2``): the contract's paths
and methods, request and response schemas whose property names and enum values are exactly the contract's (never
translated), English descriptions that carry the meaning (the compiler finds field roles by them), and an
``x-freya-role`` per operation (:mod:`builder.understand.tool_roles`).

:func:`check_derived_document` then checks the answer against the contract text, deterministically:

* it loads as a tool API (:func:`builder.ingest.tool_api.parse_tool_api`);
* every operation's FULL path occurs in the contract (case-insensitive, leading / trailing slash optional, ``{x}``
  matching the contract's own placeholder spelling ``{id}`` / ``:id`` / ``<id>``), alone or right after the server's
  path prefix - a reordered, recombined or shortened path is caught here; the path does not repeat that prefix;
* the operation's METHOD is the one the contract gives next to that path (same line or table row, else the nearest
  lines around it; when the contract names one method only, that one); a path without any stated method is accepted
  with a note;
* an absolute server URL keeps the path prefix (``/api``, ``/v1``) the contract states for that host;
* every property name and string enum value occurs in the contract as a whole word - a translated name is caught here;
* every operation has a valid ``x-freya-role``; no canonical role is given twice; the required roles are present.

On problems ONE repair call quotes them; a document that is still wrong ends the build with :class:`ContractError`
(the rejected document is saved next to the output for inspection). The accepted document is saved as
``out/<slot>.openapi.json`` and referenced in the trace.
"""

from __future__ import annotations

import json
import re
from bisect import bisect_right
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

from builder.ingest.brief_loader import BriefBundle, BriefDocument
from builder.ingest.tool_api import FieldSpec, ToolApi, parse_tool_api
from builder.inputs import InputError
from builder.llm.providers import LLMRole
from builder.prompts import prompt_id, render_prompt_parts
from builder.understand._common import MAX_OUTPUT_TOKENS, LLMLike, call_structured, trace_error, trace_event
from builder.understand.tool_roles import CANONICAL_ROLES, OTHER, REQUIRED_ROLES, ROLE_EXTENSION

PROMPT = "contract/openapi_from_contract.v1.j2"
_HTTP_METHODS = ("get", "post", "put", "patch", "delete")


class ContractError(InputError):
    """No usable tool API could be derived from the contract documents."""


class DerivedOpenApi(BaseModel):
    """Answer of the derivation call."""

    document: dict[str, Any] = Field(description="The OpenAPI 3.0.3 document, as a JSON object.")
    notes: list[str] = Field(default_factory=list, description="What of the contract could not be expressed.")


@dataclass(frozen=True)
class DerivedToolApi:
    """An accepted derived tool API: the OpenAPI document, the parsed API (every operation a tool) and its file."""

    document: dict[str, Any]
    api: ToolApi
    path: Path | None


def _word_in(word: str, text: str) -> bool:
    return re.search(rf"(?<![A-Za-z0-9_]){re.escape(word)}(?![A-Za-z0-9_])", text) is not None


def _clean(text: str) -> str:
    """The contract text with Markdown escapes removed (``musteri\\_no`` -> ``musteri_no``)."""
    return re.sub(r"\\([_*`\[\]{}()#+\-.!|])", r"\1", text)


def _field_names(fields: Iterable[FieldSpec]) -> Iterable[FieldSpec]:
    for spec in fields:
        yield spec
        yield from _field_names(spec.items or ())


#: An HTTP method named in the contract (upper case: ``get`` / ``post`` are ordinary English words).
_METHOD_RE = re.compile(r"(?<![A-Za-z])(GET|POST|PUT|PATCH|DELETE)(?![A-Za-z])")
#: A path parameter as contracts write it: ``{id}``, ``:id`` or ``<id>``.
_PLACEHOLDER = r"(?:\{[^{}/\s]+\}|:[^\W\d][\w-]*|<[^<>/\s]+>)"
#: The start of a path-like token (``/word`` or ``/{``, not inside a URL or a longer word).
_PATH_START = re.compile(r"(?<![\w/.:-])/(?:[^\W\d_]|\{)")
#: A host before a path (``https://api.bank.example:8443``; the scheme may be left out).
_HOST = r"(?:(?:https?://)?[\w-]+(?:\.[\w-]+)*\.[^\W\d_]{2,}(?::\d+)?)"
_URL_RE = re.compile(r"https?://[^\s|<>\"'`()\[\]]+")
_HYPHENS = str.maketrans(dict.fromkeys(map(chr, (0x2010, 0x2011, 0x2012, 0x2013, 0x2014, 0x2015, 0x2212)), "-"))
#: Non-blank lines looked at above and below a path whose own line names no method.
_METHOD_LINES = 3


@dataclass(frozen=True)
class DocumentReview:
    """The verdict on a derived document: ``problems`` reject it (they go to the repair call), ``notes`` do not."""

    problems: list[str]
    notes: list[str] = field(default_factory=list)


def _path_body(path: str) -> tuple[str, int]:
    """A regex of ``path`` without its leading slash (``{x}`` = any placeholder spelling) and its segment count."""
    segments = [part for part in path.strip("/").split("/") if part]
    body = "/".join(
        "".join(_PLACEHOLDER if piece.startswith("{") and piece.endswith("}") else re.escape(piece)
                for piece in re.split(r"(\{[^{}]*\})", segment) if piece)
        for segment in segments
    )
    return body, len(segments)


def _path_pattern(path: str, prefix: str) -> re.Pattern[str] | None:
    """The contract spellings of ``path`` as a path of its own - alone or after a host and / or the server ``prefix``
    (a slash-less spelling counts for paths of two segments or more) - or None for the root path."""
    body, count = _path_body(path)
    if not count:
        return None
    lead = rf"(?:{_HOST}?(?:{re.escape(prefix)})?/{body}" + (f"|{body})" if count > 1 else ")")
    return re.compile(rf"(?<![\w/.{{}}<>-]){lead}(?![\w{{<-])(?!/[\w{{<:-])", re.IGNORECASE)


def _bare_pattern(path: str) -> re.Pattern[str]:
    """``path`` anywhere, also inside a longer path (to say where the contract has it)."""
    body, _ = _path_body(path)
    return re.compile(rf"/{body}(?![\w{{<-])(?!/[\w{{<:-])", re.IGNORECASE)


class _Lines:
    """The contract text by line, to find the method stated next to a path."""

    def __init__(self, text: str) -> None:
        self.lines = text.split("\n")
        self.starts: list[int] = []
        offset = 0
        for line in self.lines:
            self.starts.append(offset)
            offset += len(line) + 1

    def methods_near(self, start: int, end: int) -> set[str]:
        """Methods named on the line of ``text[start:end]`` (the nearest one with no other path between), else on the
        nearest non-blank lines around it that name no path themselves (never across a Markdown heading below)."""
        index = bisect_right(self.starts, start) - 1
        line, base = self.lines[index], self.starts[index]
        a, b = start - base, end - base
        best: tuple[int, set[str]] | None = None
        for match in _METHOD_RE.finditer(line):
            if match.end() <= a:
                gap, distance = line[match.end():a], a - match.end()
            elif match.start() >= b:
                gap, distance = line[b:match.start()], match.start() - b
            else:
                continue
            if _PATH_START.search(gap) or self._owned_elsewhere(line, match.end(), a):
                continue
            if best is None or distance < best[0]:
                best = (distance, {match.group(1)})
            elif distance == best[0]:
                best[1].add(match.group(1))
        if best is not None:
            return best[1]
        found: dict[int, set[str]] = {}
        for step in (-1, 1):
            seen, cursor = 0, index + step
            while 0 <= cursor < len(self.lines) and seen < _METHOD_LINES:
                other = self.lines[cursor]
                cursor += step
                if not other.strip():
                    continue
                seen += 1
                heading = other.lstrip().startswith("#")
                if heading and step == 1:
                    break
                methods = set(_METHOD_RE.findall(other))
                if methods and not _PATH_START.search(other):
                    found.setdefault(seen, set()).update(methods)
                    break
                if heading:
                    break
        return found[min(found)] if found else set()

    @staticmethod
    def _owned_elsewhere(line: str, after: int, own: int) -> bool:
        """True when the method ending at ``after`` is written right before a path other than the one at ``own``
        (``... call GET /other first``): that method belongs to the other path."""
        rest = line[after:]
        start = after + len(rest) - len(rest.lstrip(" \t|`*:'\"("))
        if start == own:
            return False
        return _PATH_START.match(line, start) is not None or line.startswith(("http://", "https://"), start)


def _review_paths(paths: Mapping[str, Any], base_url: str, text: str) -> tuple[list[str], list[str]]:
    """Problems and notes of the derived paths, methods and server prefix measured against the contract ``text``."""
    problems: list[str] = []
    notes: list[str] = []
    text = text.translate(_HYPHENS)
    lines = _Lines(text)
    prefix = urlsplit(base_url).path.rstrip("/") if base_url else ""
    # methods stated apart from any path ("every request is a POST"): one of them only = the method of every path
    only = {method for line in lines.lines if not _PATH_START.search(line) for method in _METHOD_RE.findall(line)}
    for raw_path, item in paths.items():
        path = str(raw_path)
        methods = [method for method in _HTTP_METHODS if isinstance(item, Mapping) and isinstance(item.get(method),
                                                                                                    Mapping)]
        pattern = _path_pattern(path, prefix)
        if pattern is None:
            continue
        if prefix and (path.lower() + "/").startswith(prefix.lower() + "/") \
                and re.search(re.escape(prefix + path), text, re.IGNORECASE) is None:
            problems.append(f"path {path} repeats the server's path prefix {prefix} (servers[0].url "
                            f"'{base_url}'): the agent would call {prefix}{path}; write the path without the prefix, "
                            f"as {path[len(prefix):] or '/'}")
        matches = list(pattern.finditer(text))
        if not matches:
            inside = _bare_pattern(path).search(text)
            if inside is not None:
                left = inside.start()
                while left > 0 and not text[left - 1].isspace() and text[left - 1] not in "|`'\"(<[*":
                    left -= 1
                longer = text[left:inside.end()]
                problems.append(f"path {path} does not occur in the contract as a path of its own, only inside the "
                                f"longer '{longer}': copy the contract's full path (or put the part before it in "
                                "servers[0].url when the contract states it as the base URL)")
            else:
                problems.append(f"path {path} does not occur in the contract (copy the contract's paths exactly, "
                                "segment for segment; never invent, reorder or recombine an endpoint)")
            continue
        expected: set[str] = set()
        for match in matches:
            expected |= lines.methods_near(match.start(), match.end())
        source = "for this path"
        if not expected and len(only) == 1:
            expected, source = set(only), "as the method of every request"
        if not expected:
            notes.append(f"the contract states no HTTP method for {path}; kept "
                         f"{', '.join(method.upper() for method in methods)}")
            continue
        for method in methods:
            if method.upper() not in expected:
                want = " or ".join(f"'{name} {path}'" for name in sorted(expected))
                problems.append(f"{method.upper()} {path}: the contract gives {' / '.join(sorted(expected))} {source} "
                                f"(expected {want} from the contract); use the contract's method")
    server = _server_problem(base_url, text, [str(path) for path in paths])
    if server:
        problems.append(server)
    return problems, notes


def _server_problem(base_url: str, text: str, paths: Sequence[str]) -> str | None:
    """A problem when an absolute server URL drops or changes the path prefix the contract states for its host."""
    server = urlsplit(base_url)
    if not server.netloc:
        return None
    host = server.netloc.lower()
    stated: dict[str, str] = {}
    for raw in _URL_RE.findall(text):
        url = urlsplit(raw.rstrip(".,;:!?*_"))
        if url.netloc.lower() != host:
            continue
        own = url.path.rstrip("/")
        stated.setdefault(own.lower(), own)
        for path in paths:
            tail = path.rstrip("/")
            if tail and own.lower().endswith(tail.lower()):
                stated.setdefault(own[: len(own) - len(tail)].lower(), own[: len(own) - len(tail)])
    if not stated or server.path.rstrip("/").lower() in stated:
        return None
    bases = sorted({value for value in stated.values()}, key=len)
    return (f"servers[0].url '{base_url}' drops or changes the base path the contract states for {server.netloc} "
            f"({' or '.join(repr(base or '/') for base in bases)}): keep the contract's base URL with its path prefix "
            f"(for example '{server.scheme}://{server.netloc}{bases[-1] if bases[-1] else ''}') and write the paths "
            "after it")


def check_derived_document(
    document: Any, contract_text: str, *, require_collection_roles: bool = True,
) -> list[str]:
    """Problems of a derived OpenAPI document measured against the contract text (empty = accepted)."""
    return review_derived_document(document, contract_text, require_collection_roles=require_collection_roles).problems


def review_derived_document(
    document: Any, contract_text: str, *, require_collection_roles: bool = True,
) -> DocumentReview:
    """Problems and notes of a derived OpenAPI document measured against the contract text (see the module doc)."""
    if not isinstance(document, Mapping):
        return DocumentReview(["the answer is not a JSON object"])
    problems: list[str] = []
    version = str(document.get("openapi") or "")
    if not version.startswith("3"):
        problems.append(f"'openapi' must be \"3.0.3\" (got {version or 'nothing'})")
    paths = document.get("paths")
    if not isinstance(paths, Mapping) or not paths:
        return DocumentReview([*problems, "the document has no 'paths'"])
    text = _clean(contract_text)
    try:
        api = parse_tool_api(document, every_operation=True)
    except InputError as exc:
        return DocumentReview([*problems, f"it is not a valid OpenAPI document: {exc}"])
    path_problems, notes = _review_paths(paths, api.base_url, text)
    problems.extend(path_problems)
    roles: dict[str, list[str]] = {}
    for raw_path, item in paths.items():
        path = str(raw_path)
        if not isinstance(item, Mapping):
            continue
        for method in _HTTP_METHODS:
            operation = item.get(method)
            if not isinstance(operation, Mapping):
                continue
            role = operation.get(ROLE_EXTENSION)
            where = f"{method.upper()} {path}"
            if role is None and not require_collection_roles:
                continue
            if role not in (*CANONICAL_ROLES, OTHER):
                problems.append(f"{where}: '{ROLE_EXTENSION}' must be one of {', '.join((*CANONICAL_ROLES, OTHER))}"
                                f" (got {role!r})")
            elif role != OTHER:
                roles.setdefault(str(role), []).append(where)
    for contract in api.tools.values():
        where = f"{contract.method} {contract.path}"
        for spec in _field_names([*contract.request_fields, *contract.response_fields]):
            if spec.name != "body" and not _word_in(spec.name, text):
                problems.append(f"{where}: field '{spec.name}' does not occur in the contract (use the contract's "
                                "field names exactly, untranslated; never add a field)")
            for value in spec.enum or ():
                if value and not value.replace(".", "").isdigit() and value not in ("true", "false") \
                        and not _word_in(value, text):
                    problems.append(f"{where}: enum value '{value}' of '{spec.name}' does not occur in the contract "
                                    "(copy the contract's values exactly)")
    for role, operations in roles.items():
        if len(operations) > 1:
            problems.append(f"'{ROLE_EXTENSION}: {role}' is given to {len(operations)} operations "
                            f"({', '.join(operations)}); each role belongs to at most one operation")
    missing = [role for role in REQUIRED_ROLES if role not in roles] if require_collection_roles else []
    if missing:
        problems.append(f"no operation has the role(s) {', '.join(missing)}: give that role to the contract's operation "
                        "that does this job; if the contract really has no such operation, leave it out")
    return DocumentReview(list(dict.fromkeys(problems)), notes)


async def derive_tool_api(
    documents: Sequence[BriefDocument],
    *,
    llm: LLMLike,
    trace: Any = None,
    deadline: float | None = None,
    out_path: Path | None = None,
    require_collection_roles: bool = True,
) -> DerivedToolApi:
    """Derive, check (one repair) and save the tool API of the contract ``documents`` (see the module docstring).

    ``require_collection_roles=False`` accepts any documented service operations. HTTP paths, methods and schema
    grounding are checked identically; no endpoint is added to supply a collections role.

    Raises:
        ContractError: No contract text, or the document is still invalid after the repair call.
    """
    if not documents or not any(doc.text.strip() for doc in documents):
        raise ContractError("The contract documents hold no text; no tool API can be derived from them.")
    bundle = BriefBundle.from_documents(documents)
    contract_text = bundle.combined_text()
    problems: list[str] = []
    previous: dict[str, Any] | None = None
    for attempt in (1, 2):
        prompt = render_prompt_parts(
            PROMPT, files=bundle.names, contract_text=contract_text, roles=list(CANONICAL_ROLES),
            required=list(REQUIRED_ROLES) if require_collection_roles else [], problems=problems, previous=previous,
        )
        purpose = (f"derive the tool API (OpenAPI) from the service contract ({prompt_id(PROMPT)})"
                   + (" - repair" if attempt == 2 else ""))
        answer = await call_structured(
            llm, purpose=purpose, schema=DerivedOpenApi, system=prompt.system, user=prompt.user, role=LLMRole.STRONG,
            max_tokens=MAX_OUTPUT_TOKENS, deadline=deadline,
        )
        document = answer.document
        review = review_derived_document(document, contract_text, require_collection_roles=require_collection_roles)
        problems = review.problems
        if not problems:
            api = parse_tool_api(document, sha256=_sha(document), every_operation=True)
            if out_path is not None:
                _save(out_path, document)
            trace_event(
                trace, "contract",
                f"tool API derived from {', '.join(bundle.names)}: {len(api.tools)} operation(s)"
                + (f", saved as {out_path.name}" if out_path is not None else ""),
                file=str(out_path) if out_path is not None else None, attempt=attempt,
                operations=[f"{c.method} {c.path} ({_role_of(document, c.path, c.method)})" for c in api.tools.values()],
                notes=[*answer.notes, *review.notes],
            )
            return DerivedToolApi(document=document, api=api, path=out_path)
        trace_error(trace, f"the derived tool API has {len(problems)} problem(s)", attempt=attempt,
                    problems=problems[:20])
        previous = document
    rejected = out_path.with_suffix(".rejected.json") if out_path is not None else None
    if rejected is not None and previous is not None:
        _save(rejected, previous)
    raise ContractError(
        "No usable tool API could be derived from the contract after one repair: " + "; ".join(problems[:8])
        + (f" (the last attempt is saved as {rejected.name})" if rejected is not None else "")
    )


def _role_of(document: Mapping[str, Any], path: str, method: str) -> str:
    item = (document.get("paths") or {}).get(path) or {}
    operation = item.get(method.lower()) if isinstance(item, Mapping) else None
    return str(operation.get(ROLE_EXTENSION)) if isinstance(operation, Mapping) else "?"


def _sha(document: Mapping[str, Any]) -> str:
    import hashlib

    return hashlib.sha256(json.dumps(document, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _save(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


__all__ = [
    "ContractError",
    "DerivedOpenApi",
    "DerivedToolApi",
    "DocumentReview",
    "check_derived_document",
    "derive_tool_api",
    "review_derived_document",
]
