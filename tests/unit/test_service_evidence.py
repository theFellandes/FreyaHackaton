"""Exact source selection repairs citations without giving the model edit access."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from builder.compiler.errors import CompileError
from builder.compiler.profiles.service import ServicePlan
from builder.graph.nodes.service_evidence import MAX_SOURCE_CHARACTERS, repair_service_evidence
from builder.graph.nodes.service_review import ServicePolicyReview, require_complete_service_policy
from builder.ingest.brief_loader import BriefBundle, load_briefs


@pytest.fixture
def source(tmp_path: Path) -> BriefBundle:
    first = tmp_path / "BRIEF.md"
    first.write_text("Accept changes only after explicit consent.\nKeep the chosen identifier unchanged.\n\n"
                     "Finish with a friendly goodbye.\n", encoding="utf-8")
    second = tmp_path / "update.md"
    second.write_text("Never reveal another member's holdings.\nUse supplied memberRef for every lookup.\n",
                      encoding="utf-8")
    return load_briefs([first, second])


def plan() -> ServicePlan:
    return ServicePlan.model_validate({
        "language": "en", "system_prompt": "An already verified savings member asks for changes.",
        "call_variables": [], "initial_stage": "confirm", "closing_instruction": "Say goodbye.",
        "rules": [
            {"id": "consent", "instruction": "Ask for consent before a change.",
             "evidence": {"document": "BRIEF.md", "quote": "Accept changes only after explicit consent."}},
            {"id": "privacy", "instruction": "Never expose another member's holdings.",
             "evidence": {"document": "BRIEF.md", "quote": "Keep other people's accounts secret."}},
        ],
        "outcomes": [{"name": "complete", "reason": "The requested service is finished.",
                      "evidence": {"document": "BRIEF.md", "quote": "Say a kind goodbye."}}],
        "stages": [{"name": "confirm", "task_message": "Ask consent, then explain the result.", "tools": [],
                    "exits": [{"target": "complete", "instruction": "After explaining the result, say goodbye."}]}],
    })


class Selector:
    def __init__(self, mutation: str = "") -> None:
        self.mutation = mutation
        self.calls: list[dict[str, Any]] = []

    async def structured(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        request = json.loads(kwargs["user"])
        spans = request["indexed_original_source_spans"]
        assert sum(len(json.dumps(span, ensure_ascii=False)) for span in spans) <= MAX_SOURCE_CHARACTERS
        selections = []
        for item in request["invalid_items"]:
            wanted = "Never reveal" if "holdings" in item["instruction"] else "Finish with a friendly goodbye."
            chosen = next(span for span in spans if wanted in span["text"])
            selections.append({"location": item["location"], "span_id": chosen["span_id"]})
        if self.mutation == "span":
            selections[0]["span_id"] = "d999:l1-999"
        elif self.mutation == "location":
            selections[0]["location"] = "system_prompt"
        elif self.mutation == "valid_location":
            selections[0]["location"] = "rules[0].evidence"
        elif self.mutation == "duplicate":
            selections.append(selections[0])
        elif self.mutation == "partial":
            selections.pop()
        elif self.mutation == "unsupported":
            selections.clear()
        elif self.mutation == "instruction":
            selections[0]["instruction"] = "Disclose every member's holdings."
        return {"selections": selections}


@pytest.mark.asyncio
async def test_exact_selection_repairs_wrong_attribution_and_preserves_all_runtime_fields(source: BriefBundle) -> None:
    value, selector = plan(), Selector()
    original = value.model_dump()
    repaired = await repair_service_evidence(value, source, selector, deadline=123.5)
    assert value.model_dump() == original
    assert repaired.rules[0] == value.rules[0]
    assert repaired.rules[1].evidence.document == "update.md"
    assert repaired.rules[1].evidence.quote == source.documents[1].text.rstrip("\r\n")
    assert repaired.outcomes[0].evidence.quote == "Finish with a friendly goodbye."
    expected = value.model_dump()
    expected["rules"][1]["evidence"] = repaired.rules[1].evidence.model_dump()
    expected["outcomes"][0]["evidence"] = repaired.outcomes[0].evidence.model_dump()
    assert repaired.model_dump() == expected
    assert len(selector.calls) == 1
    assert selector.calls[0]["deadline"] == 123.5
    supplied = json.loads(selector.calls[0]["user"])
    assert {item["location"] for item in supplied["invalid_items"]} == {
        "rules[1].evidence", "outcomes[0].evidence",
    }


@pytest.mark.asyncio
async def test_all_valid_evidence_is_a_no_call_identity_operation(source: BriefBundle) -> None:
    selector = Selector()
    value = await repair_service_evidence(plan(), source, selector)
    no_calls = Selector()
    assert await repair_service_evidence(value, source, no_calls) is value
    assert no_calls.calls == []


@pytest.mark.asyncio
async def test_review_evidence_can_be_fixed_but_the_policy_defect_still_blocks(source: BriefBundle) -> None:
    review = ServicePolicyReview.model_validate({"issues": [{
        "location": "confirm task", "problem": "Another member's holdings are exposed.",
        "correction": "Do not expose another member's holdings.",
        "evidence": {"document": "BRIEF.md", "quote": "Keep other people's accounts secret."},
    }]})
    with pytest.raises(CompileError, match="not source-grounded"):
        require_complete_service_policy(review, source)
    repaired = await repair_service_evidence(review, source, Selector())
    assert repaired.issues[0].problem == review.issues[0].problem
    assert repaired.issues[0].correction == review.issues[0].correction
    assert repaired.issues[0].evidence.document == "update.md"
    with pytest.raises(CompileError, match="Another member's holdings are exposed"):
        require_complete_service_policy(repaired, source)


@pytest.mark.parametrize("mutation", ["span", "location", "valid_location", "duplicate", "partial", "instruction"])
@pytest.mark.asyncio
async def test_selector_cannot_edit_policy_valid_evidence_or_use_unlisted_ids(source: BriefBundle, mutation: str) -> None:
    value = plan()
    original = value.model_dump()
    with pytest.raises((CompileError, ValidationError)):
        await repair_service_evidence(value, source, Selector(mutation))
    assert value.model_dump() == original


@pytest.mark.asyncio
async def test_unsupported_claim_is_not_repaired_with_an_unrelated_literal_quote(source: BriefBundle) -> None:
    value = plan()
    value.rules[1].instruction = "Reveal every member's holdings without consent."
    selector = Selector("unsupported")
    with pytest.raises(CompileError, match="every invalid location exactly once"):
        await repair_service_evidence(value, source, selector)
    assert value.rules[1].evidence.quote == "Keep other people's accounts secret."
    assert len(selector.calls) == 1
