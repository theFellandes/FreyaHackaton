"""Invented field coverage examples; no calls or case-specific policy."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from builder.ingest.brief_loader import load_briefs
from builder.ingest.tool_api import parse_tool_api
from submission.review.service_field_coverage import service_policy_field_context


def api() -> Any:
    fields = {"amount": {"type": "number"}, "conversionDelta": {"type": "number"},
              "traceToken": {"type": "string"}}
    return parse_tool_api({"openapi": "3.0.3", "servers": [{"url": "https://inventory.example"}], "paths": {
        "/balances": {"post": {"responses": {"200": {"description": "OK", "content": {"application/json": {
            "schema": {"type": "object", "properties": {"payload": {"type": "object", "properties": fields},
                "records": {"type": "array", "items": {"type": "object", "properties": fields}}}}}}}}}}}},
        every_operation=True)


def test_source_evidence_schemas_and_hooks_do_not_count_as_runtime_implementation(tmp_path: Path) -> None:
    source = tmp_path / "policy.md"
    text = "Read amount.\n\nIf conversionDelta is positive, announce the conversion.\n\ntraceToken is for logs only."
    source.write_text(text, encoding="utf-8")
    bundle = load_briefs([source])
    candidate = {"workflow": {"system_prompt": "Read amount from the returned payload.", "nodes": {"read": {
        "task_message": "Explain amount.", "tools": [{"function": {"description": "Read balances.",
            "parameters": {"properties": {"conversionDelta": {}}}}, "post_process_code": "traceToken conversionDelta"}],
        "transitions": []}}}, "evidence": {"quote": "conversionDelta traceToken"}}
    report = service_policy_field_context(candidate, api(), bundle)
    assert report["absent_response_field_names"] == ["conversionDelta", "traceToken"]
    gap = report["potential_uncovered_response_fields"][0]
    assert len(gap["api_locations"]) == 2
    assert {item["path"] for item in gap["api_locations"]} == {"payload.conversionDelta", "records[].conversionDelta"}
    excerpt = gap["sources"][0]
    assert excerpt["text"] == text[excerpt["start_char"]:excerpt["end_char"]]
    assert excerpt["document_sha256"] == bundle.documents[0].sha256
    assert "announce the conversion" in excerpt["text"]
    assert "Diagnostic/internal metadata alone is not a defect" in report["instruction"]


def test_limits_distribute_excerpts_across_all_retained_names(tmp_path: Path) -> None:
    source = tmp_path / "policy.md"
    source.write_text(" ".join([f"{name} " + "context " * 100 for name in ("amount", "conversionDelta", "traceToken")]),
                      encoding="utf-8")
    bundle = load_briefs([source])
    report = service_policy_field_context({"nodes": {}}, api(), bundle, max_excerpt_chars=180)
    contexts = report["potential_uncovered_response_fields"]
    assert len(contexts) == 3
    assert sum(len(item["text"]) for context in contexts for item in context["sources"]) <= 180
    assert all(context["field"] in context["sources"][0]["text"] for context in contexts)
    capped = service_policy_field_context({"nodes": {}}, api(), bundle, max_fields=1)
    assert len(capped["absent_response_field_names"]) == 1 and capped["omitted_field_count"] == 2
    assert service_policy_field_context({"nodes": {}}, api(), bundle, max_fields=0)["omitted_field_count"] == 3


@pytest.mark.parametrize("limit", [{"max_fields": -1}, {"max_excerpt_chars": -1}, {"max_sources_per_field": -1}])
def test_negative_limits_rejected(limit: dict[str, int], tmp_path: Path) -> None:
    source = tmp_path / "policy.md"
    source.write_text("Inventory policy", encoding="utf-8")
    with pytest.raises(ValueError, match="non-negative"):
        service_policy_field_context({"nodes": {}}, api(), load_briefs([source]), **limit)
