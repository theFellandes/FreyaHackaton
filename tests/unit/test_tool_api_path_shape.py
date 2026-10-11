"""Malformed operation nesting must trigger extraction repair, never inventory loss."""

from __future__ import annotations

from typing import Any

import pytest

from builder.ingest.tool_api import ToolApiError, parse_tool_api
from builder.understand.contract import review_derived_document


def document(paths: dict[str, Any]) -> dict[str, Any]:
    return {"openapi": "3.0.3", "servers": [{"url": "https://elm.example"}], "paths": paths}


@pytest.mark.parametrize("key", ["components", "notes", "accounts", "", 42])
def test_non_route_paths_keys_are_not_silently_skipped(key: Any) -> None:
    malformed = document({"/lookup": {"post": {}}, key: {}})
    with pytest.raises(ToolApiError, match="Invalid Paths key"):
        parse_tool_api(malformed, every_operation=True)


@pytest.mark.parametrize("field", ["/activate", "components", "notes", "paths", "POST", "operationId"])
def test_nested_routes_or_document_metadata_inside_a_path_item_are_rejected(field: str) -> None:
    malformed = document({"/lookup": {"post": {}, field: {"post": {}}}})
    with pytest.raises(ToolApiError, match="Invalid PathItem field"):
        parse_tool_api(malformed, every_operation=True)


@pytest.mark.parametrize("value", [None, "post", [], 17])
def test_path_item_must_be_an_object(value: Any) -> None:
    with pytest.raises(ToolApiError, match=r"PathItem.*must be an object"):
        parse_tool_api(document({"/working": {"post": {}}, "/broken": value}))


@pytest.mark.parametrize("method", ["post", "get", "put", "delete", "patch", "head", "options", "trace"])
def test_every_standard_method_requires_an_operation_object(method: str) -> None:
    with pytest.raises(ToolApiError, match=r"Operation.*must be an object"):
        parse_tool_api(document({"/working": {"post": {}}, "/broken": {method: None}}))


@pytest.mark.parametrize("field,value", [("$ref", 42), ("summary", []), ("description", {}),
                                         ("parameters", {}), ("parameters", ["bad"]), ("servers", "bad")])
def test_path_metadata_has_its_documented_shape(field: str, value: Any) -> None:
    with pytest.raises(ToolApiError, match=r"PathItem.*must be"):
        parse_tool_api(document({"/lookup": {"post": {}, field: value}}))


def test_legitimate_path_ref_servers_parameters_and_extensions_remain_usable() -> None:
    source = document({
        "/lookup": {"$ref": "#/components/pathItems/Lookup", "summary": "Lookup member",
                    "description": "Read only", "x-owner": {"notes": ["internal"]}},
        "x-generated": {"post": {"operationId": "not_a_tool"}, "/hidden": {}},
    })
    source["components"] = {"pathItems": {"Lookup": {
        "servers": [{"url": "https://elm.example/service"}],
        "parameters": [{"$ref": "#/components/parameters/Member"}],
        "post": {"operationId": "member_lookup", "x-extra": {}, "responses": {"200": {"description": "OK"}}},
        "head": {}, "options": {}, "trace": {},
    }}, "parameters": {"Member": {"name": "member_ref", "in": "query", "schema": {"type": "string"}}}}
    api = parse_tool_api(source, every_operation=True)
    assert api.tool_names == ["member_lookup"]
    assert api.require("member_lookup").url == "https://elm.example/service/lookup"
    assert api.require("member_lookup").field("member_ref").type == "string"


def test_ref_target_is_checked_instead_of_hiding_nested_operations() -> None:
    source = document({"/lookup": {"$ref": "#/components/pathItems/Lookup"}})
    source["components"] = {"pathItems": {"Lookup": {"post": {}, "/activate": {"post": {}}}}}
    with pytest.raises(ToolApiError, match="Invalid PathItem field"):
        parse_tool_api(source)


def test_swagger_path_parameters_refs_and_vendor_extensions_are_preserved() -> None:
    source = {"swagger": "2.0", "host": "elm.example", "basePath": "/v2", "paths": {
        "/lookup": {"$ref": "#/x-path-items/Lookup"}, "x-notes": ["metadata"],
    }, "x-path-items": {"Lookup": {"parameters": [{"name": "member_ref", "in": "query", "type": "string"}],
                                   "post": {"responses": {"200": {"description": "OK"}}}, "x-audit": True}}}
    api = parse_tool_api(source)
    assert api.tool_names == ["lookup"]
    assert api.require("lookup").field("member_ref").type == "string"


def test_malformed_extraction_becomes_a_repair_problem_not_an_auth_only_tool_api() -> None:
    source = document({"/access": {"post": {}, "/lookup": {"post": {}}}, "components": {}})
    review = review_derived_document(source, "POST /access and POST /lookup at https://elm.example",
                                     require_collection_roles=False)
    assert any("not a valid OpenAPI document" in problem for problem in review.problems)
    assert any("Invalid PathItem field" in problem for problem in review.problems)
