"""Planner response guidance follows the parsed API namespace, including collisions."""

from __future__ import annotations

import re

from builder.compiler.profiles.service import service_response_name_instructions
from builder.ingest.tool_api import parse_tool_api


def test_response_guidance_uses_actual_unique_tool_keys_instead_of_stage_or_source_aliases() -> None:
    api = parse_tool_api({
        "openapi": "3.0.3", "info": {"title": "Invented inventory", "version": "1"},
        "servers": [{"url": "https://inventory.example"}], "paths": {"/stock": {
            "get": {"operationId": "Inventory.Lookup", "responses": {"200": {"description": "Stock"}}},
            "post": {"operationId": "Inventory Lookup", "responses": {"200": {"description": "Stock"}}},
        }},
    }, every_operation=True)
    assert len(api.tools) == 2  # Both source spellings normalize alike; parser gives each a unique key.
    guidance = service_response_name_instructions(api)
    actual_names = set(re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*_(?:response|args)\b", guidance))
    expected = {f"{tool}_{suffix}" for tool in api.tools for suffix in ("response", "args")}
    assert actual_names == expected
    assert "lookup_response" not in actual_names  # A plausible stage name is not an actual tool key.
    assert "stock_response" not in actual_names  # Neither is the path's final segment.
