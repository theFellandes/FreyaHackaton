"""The case-folder adapter selects generic services from capabilities, using invented contract data."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from builder.graph.nodes import load_inputs
from builder.ingest.tool_api import ToolApi
from builder.understand.contract import DerivedOpenApi
from tests.unit.test_contract_tool_api import FakeLLM
from tests.unit.test_generic_service_contract import inventory_contract, inventory_document


@pytest.mark.parametrize("openapi", [True, False])
async def test_case_folder_exposes_service_mode_and_keeps_original_operations(
    tmp_path: Path, make_ctx: Any, openapi: bool,
) -> None:
    brief = tmp_path / "BRIEF.md"
    brief.write_text("Inbound inventory enquiries use the customer's authenticated session.", encoding="utf-8")
    source = tmp_path / "inventory-contract.md"
    source.write_text(inventory_contract(), encoding="utf-8")
    document = inventory_document()
    llm = FakeLLM() if openapi else FakeLLM(DerivedOpenApi(document=document))
    api_file = tmp_path / "openapi.json"
    if openapi:
        api_file.write_text(json.dumps(document), encoding="utf-8")
    update = await load_inputs._body(
        {"slot": "inventory", "inputs": {"briefs": [str(brief)], "case_folder": str(tmp_path),
                                         "tool_api": str(api_file) if openapi else None,
                                         "contracts": [str(source)],
                                         "tool_base_url": "https://inventory-mock.example"}},
        make_ctx(llm=llm),
    )
    assert "failed" not in update, update.get("errors")
    assert update["service_mode"] is True and update["inputs"]["generic_service"] is True
    api = ToolApi.model_validate(update["tool_api"])
    assert {contract.method for contract in api.tools.values()} == {"GET", "POST"}
    assert set(api.tools) == {"inventory_read", "inventory_reserve"}
    assert {contract.url for contract in api.tools.values()} == {"https://inventory-mock.example/api/inventory"}
    assert update["bundle"]["documents"][0]["text"] == brief.read_text(encoding="utf-8")
    assert len(llm.calls) == (0 if openapi else 1)
