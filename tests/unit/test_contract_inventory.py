"""Invented contracts must retain every explicitly declared operation during extraction."""

from __future__ import annotations

import asyncio
import copy
from pathlib import Path
from typing import Any

import pytest

from builder.ingest.brief_loader import load_brief
from builder.understand.contract import ContractError, DerivedOpenApi, check_derived_document, derive_tool_api
from tests.unit.test_contract_tool_api import FakeLLM


def document(paths: dict[str, list[str]], *, server: str = "https://stock.example/api") -> dict[str, Any]:
    return {"openapi": "3.0.3", "info": {"title": "Invented stock service", "version": "1"},
            "servers": [{"url": server}], "paths": {
                path: {method: {"operationId": f"{method}_{path.strip('/')}",
                                "responses": {"200": {"description": "OK"}}} for method in methods}
                for path, methods in paths.items()}}


CONTRACT = ("Server: https://stock.example/api\nGET /inventory reads stock.\n"
            "POST /inventory adjusts stock.\nPOST /reserve reserves stock.\n")


@pytest.mark.parametrize("paths,missing", [
    ({"/inventory": ["get", "post"]}, "POST /reserve"),
    ({"/inventory": ["get"], "/reserve": ["post"]}, "POST /inventory"),
])
def test_all_source_method_path_pairs_must_exist(paths: dict[str, list[str]], missing: str) -> None:
    problems = check_derived_document(document(paths), CONTRACT, require_collection_roles=False)
    assert any(f"source-declared endpoint {missing} is missing" in problem for problem in problems), problems


@pytest.mark.parametrize("defect", ["nested", "omitted"])
def test_extraction_repairs_malformed_or_incomplete_inventory_before_accepting(
    tmp_path: Path, defect: str,
) -> None:
    complete = document({"/inventory": ["get", "post"], "/reserve": ["post"]})
    broken = copy.deepcopy(complete)
    reserve = broken["paths"].pop("/reserve")
    if defect == "nested":
        broken["paths"]["/inventory"]["/reserve"] = reserve
    source = tmp_path / "stock-contract.md"
    source.write_text(CONTRACT, encoding="utf-8")
    llm = FakeLLM(DerivedOpenApi(document=broken), DerivedOpenApi(document=complete))
    derived = asyncio.run(derive_tool_api([load_brief(source)], llm=llm, require_collection_roles=False))
    assert len(derived.api.tools) == 3 and derived.document == complete
    assert len(llm.calls) == 2
    assert "Invalid PathItem field" in llm.calls[1]["user"] if defect == "nested" \
        else "source-declared endpoint POST /reserve is missing" in llm.calls[1]["user"]


def test_still_missing_source_endpoint_stops_after_one_extraction_repair(tmp_path: Path) -> None:
    source = tmp_path / "stock-contract.md"
    source.write_text(CONTRACT, encoding="utf-8")
    incomplete = document({"/inventory": ["get", "post"]})
    llm = FakeLLM(DerivedOpenApi(document=incomplete), DerivedOpenApi(document=incomplete))
    with pytest.raises(ContractError, match="source-declared endpoint POST /reserve is missing"):
        asyncio.run(derive_tool_api([load_brief(source)], llm=llm, require_collection_roles=False))
    assert len(llm.calls) == 2


def test_labelled_service_table_requires_every_row_without_guessing_prose_names() -> None:
    contract = ("Base: https://stock.example/api\nEvery request is POST.\n"
                "| Service | Description |\n| --- | --- |\n"
                "| stock.read | Read inventory |\n| stock.reserve | Reserve inventory |\n"
                "\nA caller might mention stock.rename; it is not an endpoint.\n")
    partial = document({"/stock.read": ["post"]})
    problems = check_derived_document(partial, contract, require_collection_roles=False)
    assert any("POST stock.reserve is missing" in problem for problem in problems), problems
    assert not any("stock.rename" in problem or "---" in problem for problem in problems)
    complete = document({"/stock.read": ["post"], "/stock.reserve": ["post"]})
    assert check_derived_document(complete, contract, require_collection_roles=False) == []


def test_absolute_endpoint_inventory_respects_operation_server_prefixes() -> None:
    complete = document({"/inventory": ["get"], "/reserve": ["post"]})
    complete["paths"]["/reserve"]["post"]["servers"] = [{"url": "https://reserve.example/requests"}]
    source = ("Default server: https://stock.example/api\n"
              "GET https://stock.example/api/inventory reads inventory.\n"
              "POST https://reserve.example/requests/reserve reserves inventory.\n")
    assert check_derived_document(complete, source, require_collection_roles=False) == []
    complete["paths"].pop("/reserve")
    problems = check_derived_document(complete, source, require_collection_roles=False)
    assert any("POST https://reserve.example/requests/reserve is missing" in problem for problem in problems)
