"""Derive the tool API (OpenAPI 3.0 JSON) from a service contract written in prose.

A bank may describe its services in a Word / PDF / Markdown contract instead of an OpenAPI file. :func:`derive_tool_api`
asks the model ONCE to write the OpenAPI document (prompt ``contract/openapi_from_contract.v1.j2``): the contract's paths
and methods, request and response schemas whose property names and enum values are exactly the contract's (never
translated), English descriptions that carry the meaning (the compiler finds field roles by them), and an
``x-freya-role`` per operation (:mod:`builder.understand.tool_roles`).

:func:`check_derived_document` then checks the answer against the contract text, deterministically:

* it loads as a tool API (:func:`builder.ingest.tool_api.parse_tool_api`);
* every static path segment, property name and string enum value occurs in the contract as a whole word - a translated
  name or an invented endpoint is caught here;
* every operation has a valid ``x-freya-role``; no canonical role is given twice; the required roles are present.

On problems ONE repair call quotes them; a document that is still wrong ends the build with :class:`ContractError`
(the rejected document is saved next to the output for inspection). The accepted document is saved as
``out/<slot>.openapi.json`` and referenced in the trace.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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


def check_derived_document(document: Any, contract_text: str) -> list[str]:
    """Problems of a derived OpenAPI document measured against the contract text (empty = accepted)."""
    if not isinstance(document, Mapping):
        return ["the answer is not a JSON object"]
    problems: list[str] = []
    version = str(document.get("openapi") or "")
    if not version.startswith("3"):
        problems.append(f"'openapi' must be \"3.0.3\" (got {version or 'nothing'})")
    paths = document.get("paths")
    if not isinstance(paths, Mapping) or not paths:
        return [*problems, "the document has no 'paths'"]
    text = _clean(contract_text)
    try:
        api = parse_tool_api(document, every_operation=True)
    except InputError as exc:
        return [*problems, f"it is not a valid OpenAPI document: {exc}"]
    roles: dict[str, list[str]] = {}
    for raw_path, item in paths.items():
        path = str(raw_path)
        for segment in (part for part in path.strip("/").split("/") if part and not part.startswith("{")):
            if not _word_in(segment, text):
                problems.append(f"path {path}: '{segment}' does not occur in the contract (copy the contract's paths "
                                "exactly; never invent an endpoint)")
                break
        if not isinstance(item, Mapping):
            continue
        for method in _HTTP_METHODS:
            operation = item.get(method)
            if not isinstance(operation, Mapping):
                continue
            role = operation.get(ROLE_EXTENSION)
            where = f"{method.upper()} {path}"
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
    missing = [role for role in REQUIRED_ROLES if role not in roles]
    if missing:
        problems.append(f"no operation has the role(s) {', '.join(missing)}: give that role to the contract's operation "
                        "that does this job; if the contract really has no such operation, leave it out")
    return list(dict.fromkeys(problems))


async def derive_tool_api(
    documents: Sequence[BriefDocument],
    *,
    llm: LLMLike,
    trace: Any = None,
    deadline: float | None = None,
    out_path: Path | None = None,
) -> DerivedToolApi:
    """Derive, check (one repair) and save the tool API of the contract ``documents`` (see the module docstring).

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
            required=list(REQUIRED_ROLES), problems=problems, previous=previous,
        )
        purpose = (f"derive the tool API (OpenAPI) from the service contract ({prompt_id(PROMPT)})"
                   + (" - repair" if attempt == 2 else ""))
        answer = await call_structured(
            llm, purpose=purpose, schema=DerivedOpenApi, system=prompt.system, user=prompt.user, role=LLMRole.STRONG,
            max_tokens=MAX_OUTPUT_TOKENS, deadline=deadline,
        )
        document = answer.document
        problems = check_derived_document(document, contract_text)
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
                notes=answer.notes,
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


__all__ = ["ContractError", "DerivedOpenApi", "DerivedToolApi", "check_derived_document", "derive_tool_api"]
