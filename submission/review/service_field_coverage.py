"""Potential policy coverage gaps; evidence only, never new policy requirements."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from builder.ingest.brief_loader import BriefBundle
from builder.ingest.tool_api import FieldSpec, ToolApi


def _leaves(fields: Iterable[FieldSpec], prefix: str = "") -> Iterable[tuple[str, FieldSpec]]:
    for field in fields:
        path = f"{prefix}.{field.name}" if prefix else field.name
        if field.items:
            yield from _leaves(field.items, path + ("[]" if field.type == "array" else ""))
        else:
            yield path, field


def _condition_text(value: object) -> Iterable[str]:
    if isinstance(value, Mapping):
        if isinstance(value.get("description"), str):
            yield value["description"]
        for child in value.get("conditions", []):
            yield from _condition_text(child)


def _runtime_text(candidate: Mapping[str, Any]) -> str:
    workflow = candidate.get("workflow", candidate)
    texts = [workflow.get("system_prompt", "")]
    for node in workflow.get("nodes", {}).values():
        texts.extend([node.get("task_message", ""), node.get("on_enter", {}).get("text", "")])
        for tool in node.get("tools", []):
            texts.append(tool.get("function", {}).get("description", ""))
        for edge in node.get("transitions", []):
            texts.append(edge.get("description", ""))
            texts.extend(_condition_text(edge.get("condition", {})))
    return "\n".join(text for text in texts if isinstance(text, str))


def service_policy_field_context(
    candidate: Mapping[str, Any], api: ToolApi, bundle: BriefBundle, *,
    max_fields: int = 64, max_excerpt_chars: int = 12000, max_sources_per_field: int = 3,
) -> dict[str, Any]:
    """Find response names missing from operative runtime text and quote exact original mentions.

    Names in evidence, schemas, hook code or transport parameters do not establish
    policy implementation. Every retained name receives an equal excerpt budget;
    an early diagnostic name cannot consume another field's allowance. Source
    offsets refer to document.text, whose raw-file hash is also provided.
    """
    if max_fields < 0 or max_excerpt_chars < 0 or max_sources_per_field < 0:
        raise ValueError("coverage limits must be non-negative")
    runtime = _runtime_text(candidate)
    fields: dict[str, list[dict[str, str]]] = {}
    for tool, contract in api.tools.items():
        for path, spec in _leaves(contract.response_fields):
            location = {"tool": tool, "path": path, "type": spec.type}
            if location not in fields.setdefault(spec.name, []):
                fields[spec.name].append(location)
    absent = sorted(name for name in fields if not re.search(rf"(?<!\w){re.escape(name)}(?!\w)", runtime))
    names = absent[:max_fields]
    allowance = max_excerpt_chars // len(names) if names else 0
    contexts: list[dict[str, Any]] = []
    for name in names:
        pattern = re.compile(rf"(?<!\w){re.escape(name)}(?!\w)")
        found: list[tuple[Any, re.Match[str], re.Match[str]]] = []
        for document in bundle.documents:
            for paragraph in re.finditer(r"[^\n]+(?:\n(?!\s*\n)[^\n]+)*", document.text):
                mention = pattern.search(paragraph.group())
                if mention is not None:
                    found.append((document, paragraph, mention))
                    break
        selected = found[:max_sources_per_field]
        each = allowance // len(selected) if selected else 0
        sources: list[dict[str, Any]] = []
        for document, paragraph, mention in selected:
            if each < len(name):
                continue
            center = paragraph.start() + mention.start()
            start = max(paragraph.start(), center - max(0, (each - len(name)) // 2))
            end = min(paragraph.end(), start + each)
            start = max(paragraph.start(), min(start, end - each))
            sources.append({"document": document.name, "document_sha256": document.sha256,
                            "start_char": start, "end_char": end,
                            "text": document.text[start:end]})
        contexts.append({"field": name, "api_locations": fields[name][:4],
                         "omitted_api_locations": max(0, len(fields[name]) - 4), "sources": sources,
                         "omitted_source_documents": len(found) - len(sources)})
    return {"absent_response_field_names": names, "omitted_field_count": len(absent) - len(names),
            "potential_uncovered_response_fields": contexts,
            "instruction": "These are potential coverage gaps, not new requirements. For each original-source "
            "mention, resolve authoritative operative policy and check actual conditions, spoken output, and "
            "customer actions. Diagnostic/internal metadata alone is not a defect. Names absent from runtime "
            "may be intentionally unused; do not invent policy or integrations. Whole original sources remain "
            "the authority; excerpts only focus the review."}
