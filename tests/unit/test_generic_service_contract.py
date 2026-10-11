"""Invented inventory services must keep their contract without adopting collections roles."""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from typing import Any

import pytest

from builder.ingest.brief_loader import load_brief
from builder.ingest.tool_api import parse_tool_api, with_base_url
from builder.trace import NullTrace
from builder.understand.contract import ContractError, DerivedOpenApi, check_derived_document, derive_tool_api
from builder.understand.tool_roles import REQUIRED_ROLES, canonical_tool_api
from tests.unit._case_folder_data import turkish_api_document
from tests.unit.test_contract_tool_api import FakeLLM


def inventory_document() -> dict[str, Any]:
    """Two unrelated operations share a path; neither verifies identity nor records a payment promise."""
    response = {"description": "Inventory result", "content": {"application/json": {"schema": {
        "type": "object", "properties": {"available": {"type": "integer"}},
    }}}}
    return {
        "openapi": "3.0.3", "info": {"title": "Inventory", "version": "1"},
        "servers": [{"url": "https://inventory.example/api"}],
        "paths": {"/inventory": {
            "get": {"operationId": "inventory.read", "responses": {"200": copy.deepcopy(response)}},
            "post": {"operationId": "inventory.reserve", "requestBody": {"content": {"application/json": {
                "schema": {"type": "object", "properties": {"sku": {"type": "string"},
                                                             "quantity": {"type": "integer"}},
                           "required": ["sku", "quantity"]},
            }}}, "responses": {"200": copy.deepcopy(response)}},
        }},
    }


def inventory_contract() -> str:
    return ("Server: https://inventory.example/api\nGET /inventory reads available inventory.\n"
            "POST /inventory reserves inventory with required sku (string), quantity (integer).\n"
            "Both operations return available (integer).\n")


def test_generic_api_keeps_every_operation_and_does_not_request_role_mapping() -> None:
    document = inventory_document()
    llm = FakeLLM()
    original = parse_tool_api(document)
    api = asyncio.run(canonical_tool_api(original, document, llm=llm, trace=NullTrace(),
                                        require_collection_roles=False))
    assert api.model_dump() == parse_tool_api(document, every_operation=True).model_dump()
    assert len(original.tools) == 1 and len(api.tools) == 2
    assert {contract.method for contract in api.tools.values()} == {"GET", "POST"}
    assert not set(REQUIRED_ROLES) & set(api.tools)
    assert llm.calls == []
    moved = with_base_url(api, "https://inventory-mock.example")
    assert {contract.url for contract in moved.tools.values()} == {"https://inventory-mock.example/api/inventory"}
    assert moved.tools["inventory_reserve"].required_fields == ["sku", "quantity"]


def test_generic_contract_accepts_absent_collection_hints_but_strict_default_still_rejects() -> None:
    document = inventory_document()
    text = inventory_contract()
    assert check_derived_document(document, text, require_collection_roles=False) == []
    assert any("no operation has the role" in problem for problem in check_derived_document(document, text))


def test_generic_contract_is_derived_in_one_call_without_fabricating_required_roles(tmp_path: Path) -> None:
    source = tmp_path / "inventory-contract.md"
    source.write_text(inventory_contract(), encoding="utf-8")
    document = inventory_document()
    llm = FakeLLM(DerivedOpenApi(document=document))
    saved = tmp_path / "derived.openapi.json"
    derived = asyncio.run(derive_tool_api([load_brief(source)], llm=llm, trace=NullTrace(), out_path=saved,
                                          require_collection_roles=False))
    assert derived.document == document == json.loads(saved.read_text(encoding="utf-8"))
    assert len(derived.api.tools) == 2 and len(llm.calls) == 1
    assert "No collections role is required" in llm.calls[0]["system"]


@pytest.mark.parametrize("mutation", ["method", "path"])
def test_generic_contract_still_rejects_changed_http_operations(tmp_path: Path, mutation: str) -> None:
    document = inventory_document()
    if mutation == "method":
        document["paths"]["/inventory"]["put"] = document["paths"]["/inventory"].pop("post")
    else:
        document["paths"]["/inventory/new"] = document["paths"].pop("/inventory")
    problems = check_derived_document(document, inventory_contract(), require_collection_roles=False)
    assert problems
    source = tmp_path / "inventory-contract.md"
    source.write_text(inventory_contract(), encoding="utf-8")
    llm = FakeLLM(DerivedOpenApi(document=document), DerivedOpenApi(document=document))
    with pytest.raises(ContractError):
        asyncio.run(derive_tool_api([load_brief(source)], llm=llm, trace=NullTrace(),
                                   require_collection_roles=False))
    assert len(llm.calls) == 2


def test_complete_collection_capabilities_keep_existing_canonicalization_in_optional_mode() -> None:
    document = turkish_api_document()
    llm = FakeLLM()
    api = asyncio.run(canonical_tool_api(parse_tool_api(document), document, llm=llm,
                                        require_collection_roles=False))
    strict = asyncio.run(canonical_tool_api(parse_tool_api(document), document))
    assert api.model_dump() == strict.model_dump()
    assert set(REQUIRED_ROLES) <= set(api.tools) and llm.calls == []
