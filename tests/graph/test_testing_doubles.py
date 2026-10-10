"""The offline test doubles in ``builder.testing``: they are test infrastructure *and* the engine of ``--fake-llm``."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from builder.compiler.prose import NodeProse
from builder.freya.client import FreyaError, FreyaValidationError
from builder.ingest.brief_loader import load_briefs
from builder.llm.providers import LLMRole
from builder.policy.ir import PolicyIR
from builder.testing import FakeFreya, FixtureGateway, FixtureMissing, load_fixture_gateway
from builder.trace import NullTrace
from builder.understand import build_rule_ledger, draft_prose, extract_policy
from builder.understand.ledger import LedgerPart
from tests.graph.conftest import load_ir, load_ledger

# ------------------------------------------------------------------------------------------------ the gateway


async def test_the_fixture_gateway_plays_the_whole_understand_pipeline(
    brief_a_path: Path, kit_tool_api: Any, null_trace: NullTrace
) -> None:
    bundle = load_briefs([brief_a_path])
    gateway = FixtureGateway(
        load_ledger("brief_a"), load_ir("brief_a"), NodeProse(per_node={"opening": "- Be short. [A-1b]"}),
        trace=null_trace,
    )  # fmt: skip
    ledger = await build_rule_ledger(gateway, bundle, kit_tool_api, null_trace)
    assert sorted(ledger.ids()) == sorted(load_ledger("brief_a").ids())
    ir = await extract_policy(gateway, bundle, kit_tool_api, ledger, null_trace)
    assert ir.to_json_dict() == load_ir("brief_a").to_json_dict()
    prose = await draft_prose(gateway, ir, ledger, "staged", null_trace)
    assert "Be short" in prose.per_node["opening"]
    assert len(null_trace.of_kind("model_call")) == len(gateway.calls) >= 5
    assert all(e["data"]["provider"] == "fixture" for e in null_trace.of_kind("model_call"))
    assert {c.schema for c in gateway.calls} == {"LedgerPart", "PolicyIR", "ProseDraft"}
    assert gateway.purposes("extract")[0].role == LLMRole.STRONG.value


async def test_the_fixture_gateway_fails_on_request_and_on_unknown_schemas() -> None:
    gateway = FixtureGateway(load_ledger("brief_a"), load_ir("brief_a"), fail_schemas=["PolicyIR"])
    with pytest.raises(FixtureMissing, match="PolicyIR"):
        await gateway.structured(purpose="p", schema=PolicyIR, system="", user="")

    class Unknown(LedgerPart):
        pass

    with pytest.raises(FixtureMissing, match="no answer"):
        await gateway.structured(purpose="p", schema=Unknown, system="", user="")
    assert len(gateway.calls) == 2
    text = await gateway.text(purpose="t", system="", user="")
    assert text.value == "" and gateway.available_providers() == ["fixture"]


async def test_responders_override_the_default_answers() -> None:
    marker = load_ir("brief_a").model_copy(update={"bank_name": "Override Bank"})
    gateway = FixtureGateway(
        load_ledger("brief_a"), load_ir("brief_a"), responders={"PolicyIR": lambda call: marker}
    )
    result = await gateway.structured(purpose="p", schema=PolicyIR, system="s", user="u")
    assert result.value.bank_name == "Override Bank" and result.provider == "fixture"


async def test_the_observer_receives_every_generation_with_the_full_prompt() -> None:
    seen: list[dict[str, Any]] = []

    class Observer:
        def log_generation(self, **kwargs: Any) -> None:
            seen.append(kwargs)

    gateway = FixtureGateway(load_ledger("brief_a"), load_ir("brief_a"), observer=Observer())
    await gateway.structured(purpose="extract", schema=PolicyIR, system="SYS", user="USER")
    assert seen[0]["system"] == "SYS" and seen[0]["user"] == "USER" and seen[0]["output"]["bank_name"]
    assert seen[0]["provider"] == "fixture" and seen[0]["ok"] is True


# ------------------------------------------------------------------------------------------------ loading fixtures


def test_load_fixture_gateway_picks_the_pair_matching_the_documents(fixture_dir: Path) -> None:
    only_a = load_fixture_gateway(fixture_dir, ["brief-a.md"])
    both = load_fixture_gateway(fixture_dir, ["brief-a.md", "brief-b.md"])
    assert only_a.ledger.source_files == ["brief-a.md"]
    assert both.ledger.source_files == ["brief-a.md", "brief-b.md"]
    assert load_fixture_gateway(
        fixture_dir, ["unknown.md"]
    ).ledger.source_files  # falls back to the first pair


def test_load_fixture_gateway_reads_the_plain_file_names_and_extras(
    fixture_dir: Path, tmp_path: Path
) -> None:
    shutil.copy(fixture_dir / "brief_a_ledger.json", tmp_path / "ledger.json")
    shutil.copy(fixture_dir / "brief_a_ir.json", tmp_path / "ir.json")
    (tmp_path / "prose.json").write_text(
        json.dumps({"per_node": {"identity": "- one question at a time"}}), encoding="utf-8"
    )
    (tmp_path / "walkthrough.json").write_text(
        json.dumps([{"rule_id": "A-1b", "verdict": "unclear", "where": "w", "explanation": "e"}]),
        encoding="utf-8",
    )
    gateway = load_fixture_gateway(tmp_path, ["whatever.md"])
    assert gateway.prose is not None and gateway.prose.per_node == {"identity": "- one question at a time"}
    assert gateway.walkthrough_issues[0]["rule_id"] == "A-1b"


def test_load_fixture_gateway_without_fixtures_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match=r"ledger\.json"):
        load_fixture_gateway(tmp_path, ["a.md"])


# ------------------------------------------------------------------------------------------------ the fake platform


def test_fake_freya_creates_then_updates_one_workflow(tmp_path: Path) -> None:
    freya = FakeFreya()
    first = freya.save("slot", {"nodes": {"a": {}}, "global_nodes": {}})
    second = freya.save("slot", {"nodes": {"a": {}, "b": {}}})
    assert first.created and not second.created and first.workflow_id == second.workflow_id
    assert (first.version, second.version) == (1, 2) and second.node_count == 2
    assert freya.get_workflow(first.workflow_id)["nodes"].keys() == {"a", "b"}
    exported = freya.export(first.workflow_id, tmp_path / "deep" / "x.json")
    assert json.loads(exported.read_text(encoding="utf-8"))["workflow_id"] == first.workflow_id
    with pytest.raises(FreyaError):
        freya.get_workflow("wf_other")


def test_fake_freya_rejects_and_fails_as_told() -> None:
    freya = FakeFreya(reject_validations=[["p1", "p2"]], reject_saves=[["w1"]], fail_save=False)
    with pytest.raises(FreyaValidationError) as caught:
        freya.validate("n", {})
    assert caught.value.problems == ["p1", "p2"] and caught.value.status == 422
    assert freya.validate("n", {}) == {"valid": True}  # only the first call was rejected
    with pytest.raises(FreyaValidationError):
        freya.save("s", {})
    assert freya.save("s", {}).created
    with pytest.raises(FreyaError) as transport:
        FakeFreya(fail_save=True).save("s", {})
    assert transport.value.kind == "transport"


def test_fake_freya_chat_script() -> None:
    freya = FakeFreya(opening="Hello!", replies=["one", "two"])
    info = freya.save("s", {})
    a = freya.chat(info.workflow_id, "hi", {"customer_id": "c"})
    b = freya.chat(info.workflow_id, "who?", None, a.call_id)
    c = freya.chat(info.workflow_id, "and?", None, a.call_id)
    d = freya.chat(info.workflow_id, "more?", None, a.call_id)
    assert [x.response for x in (a, b, c, d)] == ["Hello!", "one", "two", "two"]
    assert (
        a.call_id == b.call_id
        and freya.chat_variables[0] == {"customer_id": "c"}
        and freya.chat_variables[1] is None
    )
    assert freya.get_call(a.call_id)["messages"][0] == {"user": "hi", "agent": "Hello!"}
    assert freya.end_chat(a.call_id) == {"ended": True} and freya.workspace_id() == "ws_fake"
    with freya.simulation_slot():
        pass
