"""Tests for builder.freya.client (FreyaClient) and builder.freya.kit (load_kit).

No network: the kit is replaced by a ``types.SimpleNamespace`` whose functions talk to :class:`FakePlatform`, an
in-memory imitation of the Freya routes the builder uses (envelope already unwrapped, like ``freya.api``). One
contract test also executes the real kit ``freya.py`` with its ``api`` function replaced by the fake platform and
``urllib.request.urlopen`` blocked, to prove the wrapper agrees with the kit's actual create/update semantics.
"""

from __future__ import annotations

import copy
import importlib.util
import itertools
import json
import re
import sys
import types
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

import builder.freya.client as client_mod
import builder.freya.kit as kit_mod
from builder.freya.client import (
    ChatTurn,
    DeployInfo,
    FreyaClient,
    FreyaError,
    FreyaValidationError,
    count_nodes,
    count_tools,
    default_state_file,
    tool_names,
)
from builder.freya.kit import KitError, load_kit
from builder.trace import NullTrace

REPO_ROOT = Path(__file__).resolve().parents[2]
EVENT_LLM = {"provider": "experimental-5", "model": "experimental-5"}
PLATFORM_DEFAULT_LLM = {"provider": "openai", "model": "platform-default", "temperature": 0.3}


# ---------------------------------------------------------------------------------------------------------- fakes


def http_error(method: str, path: str, status: int, body: str) -> RuntimeError:
    """The exception the kit's ``api`` raises for an HTTP error status."""
    return RuntimeError(f"{method} {path} -> {status}: {body}")


class FakePlatform:
    """In-memory Freya: workflows, versions, publish, dry run, chat and calls. Records every call."""

    def __init__(self, *, default_llm: dict[str, Any] | None = None, honour_publish: bool = True) -> None:
        self.default_llm = dict(default_llm or PLATFORM_DEFAULT_LLM)
        self.honour_publish = honour_publish
        self.workflows: dict[str, dict[str, Any]] = {}
        self.versions: dict[str, list[dict[str, Any]]] = {}
        self.calls: list[tuple[str, str, Any, dict[str, Any]]] = []
        self.failures: dict[tuple[str, str], list[BaseException]] = {}
        self.dry_run_result: Any = {"valid": True}
        self.chat_result: dict[str, Any] | None = None
        self._ids = itertools.count(1)

    # -- test helpers
    def fail(self, method: str, path: str, *errors: BaseException) -> None:
        self.failures.setdefault((method, path), []).extend(errors)

    def count(self, method: str, pattern: str) -> int:
        return sum(1 for m, p, _, _ in self.calls if m == method and re.fullmatch(pattern, p))

    def seed(self, *, published: bool = True, llm: dict[str, Any] | None = None) -> str:
        """Create a workflow directly (as if made in an earlier run)."""
        record = self._create("seeded", {"initial_node": "a", "nodes": {}})
        record["workflow"]["agent_config"] = {"llmConfig": dict(llm or EVENT_LLM)}
        self.versions[record["id"]][-1]["isPublished"] = published
        return str(record["id"])

    # -- the route table
    def api(self, method: str, path: str, body: Any = None, **params: Any) -> Any:
        self.calls.append((method, path, copy.deepcopy(body), dict(params)))
        queued = self.failures.get((method, path))
        if queued:
            raise queued.pop(0)
        if (method, path) == ("GET", "/api/v2/workspaces"):
            return {"workspaces": [{"id": "ws_1"}]}
        if (method, path) == ("POST", "/api/v2/workflow"):
            if params.get("dryRun") == "true":
                return copy.deepcopy(self.dry_run_result)
            return {"workflow": copy.deepcopy(self._create(body["name"], body["workflow"]))}
        if match := re.fullmatch(r"/api/v2/workflow/([^/]+)", path):
            record = self._record(method, path, match[1])
            if method == "PUT":
                self._store(record, body["workflow"])
            return {"workflow": copy.deepcopy(record)}
        if (match := re.fullmatch(r"/api/v2/versions/workflow/([^/]+)", path)) and method == "GET":
            self._record(method, path, match[1])
            return {"versions": copy.deepcopy(self.versions[match[1]])}
        if (match := re.fullmatch(r"/api/v2/versions/workflow/([^/]+)/publish", path)) and method == "POST":
            self._record(method, path, match[1])
            if self.honour_publish:
                for version in self.versions[match[1]]:
                    version["isPublished"] = version["id"] == body["versionId"]
            return {"version": {"id": body["versionId"]}}
        if (method, path) == ("POST", "/api/v2/chat/message"):
            if self.chat_result is not None:
                return copy.deepcopy(self.chat_result)
            return {"response": f"echo: {body['message']}", "callId": body.get("callId") or "call_1"}
        if (method, path) == ("POST", "/api/v2/chat/end"):
            return {"ended": True}
        if (match := re.fullmatch(r"/api/v2/call/([^/]+)", path)) and method == "GET":
            return {"call": {"id": match[1], "messages": [{"role": "assistant", "message": "Hello"}]}}
        raise AssertionError(f"unexpected Freya call {method} {path}")

    def _create(self, name: str, definition: dict[str, Any]) -> dict[str, Any]:
        number = next(self._ids)
        record = {
            "id": f"wf_{number}",
            "agentId": f"ag_{number}",
            "name": name,
            "updatedAt": "2026-10-10T12:00:00Z",
        }
        self.workflows[record["id"]] = record
        self.versions[record["id"]] = []
        self._store(record, definition)
        return record

    def _store(self, record: dict[str, Any], definition: dict[str, Any]) -> None:
        stored = copy.deepcopy(definition)
        stored.setdefault(
            "agent_config", {"llmConfig": dict(self.default_llm)}
        )  # the platform fills a default
        record["workflow"] = stored
        versions = self.versions[record["id"]]
        versions.append(
            {
                "id": f"{record['id']}_v{len(versions) + 1}",
                "versionNumber": len(versions) + 1,
                "isPublished": False,
            }
        )

    def _record(self, method: str, path: str, workflow_id: str) -> dict[str, Any]:
        if workflow_id not in self.workflows:
            raise http_error(method, path, 404, json.dumps({"success": False, "error": "Workflow not found"}))
        return self.workflows[workflow_id]


def make_kit(platform: FakePlatform) -> SimpleNamespace:
    """A stand-in for the kit module with the same function names and semantics, backed by ``platform``."""
    api = platform.api

    def update_workflow(workflow_id: str, workflow: dict[str, Any]) -> int:
        api("PUT", f"/api/v2/workflow/{workflow_id}", {"workflow": workflow})
        stored = api("GET", f"/api/v2/workflow/{workflow_id}", workspaceId="ws_1")["workflow"]["workflow"]
        config = stored.get("agent_config") or {}
        llm = config.get("llmConfig") or {}
        if (llm.get("provider"), llm.get("model")) != (EVENT_LLM["provider"], EVENT_LLM["model"]):
            config["llmConfig"] = {**llm, **EVENT_LLM}
            api("PUT", f"/api/v2/workflow/{workflow_id}", {"workflow": {**stored, "agent_config": config}})
        versions = api("GET", f"/api/v2/versions/workflow/{workflow_id}", workspaceId="ws_1")["versions"]
        newest = max(versions, key=lambda v: v["versionNumber"])
        if not newest.get("isPublished"):
            api("POST", f"/api/v2/versions/workflow/{workflow_id}/publish", {"versionId": newest["id"]})
        return int(newest["versionNumber"])

    def save_workflow(
        slot: str, workflow: dict[str, Any], state_file: str = ".freya-agents.json"
    ) -> dict[str, Any]:
        file = Path(state_file)
        state = json.loads(file.read_text()) if file.exists() else {}
        if slot in state:
            state[slot]["version"] = update_workflow(state[slot]["workflow_id"], workflow)
        else:
            created = api("POST", "/api/v2/workflow", {"name": f"{slot} 1010-120000", "workflow": workflow})
            state[slot] = {
                "workflow_id": created["workflow"]["id"],
                "agent_id": created["workflow"]["agentId"],
            }
        file.write_text(json.dumps(state, indent=1))
        return state[slot]

    def export_workflow(workflow_id: str, path: str) -> str:
        record = api("GET", f"/api/v2/workflow/{workflow_id}", workspaceId="ws_1")["workflow"]
        Path(path).write_text(json.dumps({"workflow_id": record["id"], "workflow": record["workflow"]}))
        return path

    return SimpleNamespace(
        api=api,
        workspace_id=lambda: "ws_1",
        validate_workflow=lambda name, workflow: api(
            "POST", "/api/v2/workflow", {"name": name, "workflow": workflow}, dryRun="true"
        ),
        save_workflow=save_workflow,
        update_workflow=update_workflow,
        export_workflow=export_workflow,
        get_call=lambda call_id: api("GET", f"/api/v2/call/{call_id}")["call"],
        AGENT_LLM=dict(EVENT_LLM),
    )


class RecordingTrace:
    """Collects ``trace.freya`` events."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def freya(self, action: str, **data: Any) -> None:
        self.events.append((action, data))

    def actions(self) -> list[str]:
        return [action for action, _ in self.events]

    def last(self, action: str) -> dict[str, Any]:
        return [data for name, data in self.events if name == action][-1]


def sample_workflow() -> dict[str, Any]:
    return {
        "initial_node": "setup",
        "system_prompt": "Be brief.",
        "nodes": {
            "setup": {
                "name": "setup",
                "node_type": "code",
                "pre_actions": [{"type": "function", "function": {"name": "code_exec"}}],
                "transitions": [{"target_node": "talk", "condition": {"operator": "always"}}],
            },
            "talk": {
                "name": "talk",
                "node_type": "conversation",
                "tools": [
                    {"type": "function", "function": {"name": "lookup"}},
                    {"type": "function", "function": {"name": "close"}},
                ],
                "transitions": [],
            },
            "end": {"name": "end", "node_type": "end_call", "terminal": {"function": {"name": "hang_up"}}},
        },
        "global_nodes": {"help": {"name": "help", "tools": [{"function": {"name": "lookup"}}]}},
        "tools": [
            {"type": "function", "config": {"alwaysRunsAt": ["end_of_call"]}, "function": {"name": "log"}}
        ],
    }


@pytest.fixture
def platform() -> FakePlatform:
    return FakePlatform()


@pytest.fixture
def trace() -> RecordingTrace:
    return RecordingTrace()


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
def client(platform: FakePlatform, trace: RecordingTrace, sleeps: list[float], tmp_path: Path) -> FreyaClient:
    return FreyaClient(
        trace, make_kit(platform), state_file=tmp_path / "state" / "agents.json", sleep=sleeps.append
    )


def read_state(tmp_path: Path) -> dict[str, Any]:
    return json.loads((tmp_path / "state" / "agents.json").read_text())


# ---------------------------------------------------------------------------------------------------------- save


def test_first_save_creates_sets_event_model_and_publishes(
    client: FreyaClient, platform: FakePlatform, trace: RecordingTrace, tmp_path: Path
) -> None:
    info = client.save("brief-x", sample_workflow())

    # v1 = create, v2 = re-PUT by update_workflow, v3 = the same with the event model; v3 is published
    assert info == DeployInfo(
        workflow_id="wf_1",
        agent_id="ag_1",
        version=3,
        created=True,
        node_count=4,
        tool_count=4,
        slot="brief-x",
        published=True,
    )
    stored = platform.workflows["wf_1"]["workflow"]
    assert stored["agent_config"]["llmConfig"]["provider"] == "experimental-5"
    assert stored["agent_config"]["llmConfig"]["model"] == "experimental-5"
    assert platform.versions["wf_1"][-1]["isPublished"] is True
    assert read_state(tmp_path) == {"brief-x": {"workflow_id": "wf_1", "agent_id": "ag_1"}}
    assert platform.count("POST", r"/api/v2/workflow") == 1
    assert "set_agent_llm" in trace.actions()
    assert trace.last("save")["created"] is True
    assert trace.last("save")["workflow_id"] == "wf_1"


def test_first_save_publishes_version_one_when_the_model_is_already_right(
    platform: FakePlatform, trace: RecordingTrace, tmp_path: Path
) -> None:
    platform.default_llm = dict(EVENT_LLM)
    client = FreyaClient(trace, make_kit(platform), state_file=tmp_path / "agents.json")

    info = client.save("slot", sample_workflow())

    assert (info.created, info.version, info.published) == (True, 1, True)
    assert platform.count("PUT", r"/api/v2/workflow/.+") == 0
    assert platform.count("POST", r"/api/v2/versions/workflow/wf_1/publish") == 1
    publish_body = next(b for m, p, b, _ in platform.calls if p.endswith("/publish"))
    assert publish_body == {"versionId": "wf_1_v1"}
    assert "set_agent_llm" not in trace.actions()
    assert trace.last("publish")["confirmed"] is True


def test_second_save_updates_the_same_workflow_without_republishing(
    client: FreyaClient, platform: FakePlatform, tmp_path: Path
) -> None:
    first = client.save("slot", sample_workflow())
    publishes_before = platform.count("POST", r".+/publish")
    workflow = sample_workflow()
    workflow["system_prompt"] = "Changed."

    second = client.save("slot", workflow)

    assert second.created is False
    assert second.workflow_id == first.workflow_id
    assert second.agent_id == first.agent_id
    assert len(platform.workflows) == 1
    assert platform.workflows["wf_1"]["workflow"]["system_prompt"] == "Changed."
    assert second.version == max(v["versionNumber"] for v in platform.versions["wf_1"])
    assert second.published is True
    # the kit's update published; the client only confirmed it
    assert platform.count("POST", r".+/publish") == publishes_before + 1
    assert read_state(tmp_path)["slot"]["version"] == second.version


def test_save_recreates_a_slot_whose_workflow_was_deleted(
    client: FreyaClient, platform: FakePlatform, trace: RecordingTrace, tmp_path: Path
) -> None:
    state = tmp_path / "state" / "agents.json"
    state.parent.mkdir(parents=True)
    state.write_text(
        json.dumps(
            {
                "slot": {"workflow_id": "wf_gone", "agent_id": "ag_gone"},
                "other": {"workflow_id": "wf_keep", "agent_id": "ag_keep"},
            }
        )
    )

    info = client.save("slot", sample_workflow())

    assert info.created is True
    assert info.workflow_id == "wf_1"
    saved = read_state(tmp_path)
    assert saved["slot"]["workflow_id"] == "wf_1"
    assert saved["other"] == {"workflow_id": "wf_keep", "agent_id": "ag_keep"}
    assert trace.last("forget_slot")["stale"] == {"workflow_id": "wf_gone", "agent_id": "ag_gone"}


def test_a_404_from_another_route_never_creates_a_second_workflow(
    client: FreyaClient, platform: FakePlatform, tmp_path: Path
) -> None:
    client.save("slot", sample_workflow())
    route = "/api/v2/versions/workflow/wf_1"
    platform.fail("GET", route, http_error("GET", route, 404, "<html>not found</html>"))

    with pytest.raises(FreyaError) as caught:
        client.save("slot", sample_workflow())

    assert (caught.value.status, caught.value.path) == (404, route)
    assert list(platform.workflows) == ["wf_1"]
    assert read_state(tmp_path)["slot"]["workflow_id"] == "wf_1"


def test_save_failure_is_not_retried_and_carries_the_status(
    client: FreyaClient, platform: FakePlatform, trace: RecordingTrace, sleeps: list[float]
) -> None:
    platform.fail("POST", "/api/v2/workflow", http_error("POST", "/api/v2/workflow", 503, "upstream down"))

    with pytest.raises(FreyaError) as caught:
        client.save("slot", sample_workflow())

    assert caught.value.status == 503
    assert caught.value.kind == "http"
    assert caught.value.body == "upstream down"
    assert caught.value.retryable is True
    assert platform.count("POST", r"/api/v2/workflow") == 1  # writes are never retried
    assert sleeps == []
    assert trace.last("save")["ok"] is False


def test_save_refuses_null_values_without_calling_freya(client: FreyaClient, platform: FakePlatform) -> None:
    workflow = sample_workflow()
    workflow["nodes"]["talk"]["role_message"] = None

    with pytest.raises(FreyaValidationError) as caught:
        client.save("slot", workflow)

    assert caught.value.kind == "local"
    assert caught.value.problems == ["null value at nodes.talk.role_message"]
    assert platform.calls == []


def test_save_with_a_corrupt_state_file_fails_before_any_call(
    client: FreyaClient, platform: FakePlatform, tmp_path: Path
) -> None:
    state = tmp_path / "state" / "agents.json"
    state.parent.mkdir(parents=True)
    state.write_text("{not json")

    with pytest.raises(FreyaError, match="state file") as caught:
        client.save("slot", sample_workflow())

    assert caught.value.kind == "local"
    assert platform.calls == []


def test_save_reports_an_unconfirmed_publish(trace: RecordingTrace, tmp_path: Path) -> None:
    platform = FakePlatform(default_llm=EVENT_LLM, honour_publish=False)
    client = FreyaClient(trace, make_kit(platform), state_file=tmp_path / "agents.json")

    info = client.save("slot", sample_workflow())

    assert info.published is False
    assert trace.last("publish")["ok"] is False


def test_save_rejects_an_empty_slot(client: FreyaClient) -> None:
    with pytest.raises(ValueError):
        client.save("  ", sample_workflow())


def test_save_argument_overrides_the_client_state_file(client: FreyaClient, tmp_path: Path) -> None:
    other = tmp_path / "elsewhere" / "state.json"
    client.save("slot", sample_workflow(), state_file=other)
    assert json.loads(other.read_text())["slot"]["workflow_id"] == "wf_1"
    assert not (tmp_path / "state" / "agents.json").exists()


# ---------------------------------------------------------------------------------------------------------- validate


def test_validate_returns_the_dry_run_result(
    client: FreyaClient, platform: FakePlatform, trace: RecordingTrace
) -> None:
    assert client.validate("slot", sample_workflow()) == {"valid": True}
    method, path, body, params = platform.calls[-1]
    assert (method, path, params) == ("POST", "/api/v2/workflow", {"dryRun": "true"})
    assert body["name"] == "slot"
    assert trace.last("validate")["ok"] is True


def test_validate_maps_a_422_to_a_validation_error_with_problems(
    client: FreyaClient, platform: FakePlatform
) -> None:
    body = json.dumps(
        {
            "success": False,
            "data": {
                "message": "Invalid workflow",
                "errors": ["nodes.talk.transitions[0].target_node: unknown"],
            },
        }
    )
    platform.fail("POST", "/api/v2/workflow", http_error("POST", "/api/v2/workflow", 422, body))

    with pytest.raises(FreyaValidationError) as caught:
        client.validate("slot", sample_workflow())

    assert caught.value.status == 422
    assert caught.value.body == body
    assert "nodes.talk.transitions[0].target_node: unknown" in caught.value.problems
    assert "Invalid workflow" in caught.value.problems


def test_validate_keeps_a_plain_text_error_body_as_the_problem(
    client: FreyaClient, platform: FakePlatform
) -> None:
    platform.fail(
        "POST", "/api/v2/workflow", http_error("POST", "/api/v2/workflow", 400, "bad schema at nodes")
    )
    with pytest.raises(FreyaValidationError) as caught:
        client.validate("slot", sample_workflow())
    assert caught.value.problems == ["bad schema at nodes"]


def test_validate_does_not_blame_the_workflow_for_an_auth_error(
    client: FreyaClient, platform: FakePlatform
) -> None:
    platform.fail("POST", "/api/v2/workflow", http_error("POST", "/api/v2/workflow", 401, "invalid key"))
    with pytest.raises(FreyaError) as caught:
        client.validate("slot", sample_workflow())
    assert not isinstance(caught.value, FreyaValidationError)
    assert caught.value.status == 401


def test_validate_reads_an_explicit_invalid_result(client: FreyaClient, platform: FakePlatform) -> None:
    platform.dry_run_result = {"valid": False, "errors": [{"path": "nodes.x", "message": "bad"}]}
    with pytest.raises(FreyaValidationError) as caught:
        client.validate("slot", sample_workflow())
    assert caught.value.problems == [json.dumps({"path": "nodes.x", "message": "bad"})]


def test_validate_ignores_an_empty_error_list(client: FreyaClient, platform: FakePlatform) -> None:
    platform.dry_run_result = {"errors": [], "warnings": ["unused"]}
    assert client.validate("slot", sample_workflow()) == {"errors": [], "warnings": ["unused"]}


def test_validate_retries_a_server_error(
    client: FreyaClient, platform: FakePlatform, sleeps: list[float]
) -> None:
    platform.fail("POST", "/api/v2/workflow", http_error("POST", "/api/v2/workflow", 502, "bad gateway"))
    assert client.validate("slot", sample_workflow()) == {"valid": True}
    assert platform.count("POST", r"/api/v2/workflow") == 2
    assert sleeps == [2.0]


def test_validate_refuses_null_values(client: FreyaClient, platform: FakePlatform) -> None:
    with pytest.raises(FreyaValidationError):
        client.validate("slot", {"initial_node": None, "nodes": {}})
    assert platform.calls == []


# ---------------------------------------------------------------------------------------------------------- reads


def test_get_workflow_returns_the_inner_definition(client: FreyaClient, platform: FakePlatform) -> None:
    workflow_id = platform.seed()
    definition = client.get_workflow(workflow_id)
    assert definition["initial_node"] == "a"
    assert "agent_config" in definition
    method, path, _, params = platform.calls[-1]
    assert (method, path, params) == ("GET", f"/api/v2/workflow/{workflow_id}", {"workspaceId": "ws_1"})


def test_get_workflow_with_an_unexpected_shape_raises(trace: RecordingTrace) -> None:
    kit = SimpleNamespace(api=lambda *a, **k: {"workflow": {"id": "wf"}}, workspace_id=lambda: "ws")
    with pytest.raises(FreyaError, match="no workflow definition") as caught:
        FreyaClient(trace, kit).get_workflow("wf")
    assert caught.value.kind == "response"


def test_get_workflow_404_is_not_retried(
    client: FreyaClient, platform: FakePlatform, sleeps: list[float]
) -> None:
    with pytest.raises(FreyaError) as caught:
        client.get_workflow("wf_missing")
    assert caught.value.status == 404
    assert sleeps == []


def test_export_writes_through_the_kit_and_creates_directories(
    client: FreyaClient, platform: FakePlatform, trace: RecordingTrace, tmp_path: Path
) -> None:
    workflow_id = platform.seed()
    target = tmp_path / "out" / "nested" / "slot.workflow.json"

    result = client.export(workflow_id, target)

    assert result == target
    assert json.loads(target.read_text())["workflow_id"] == workflow_id
    assert trace.last("export") == {"ok": True, "workflow_id": workflow_id, "path": str(target)}


def test_get_call_retries_network_errors_then_gives_up(trace: RecordingTrace, sleeps: list[float]) -> None:
    attempts: list[str] = []

    def get_call(call_id: str) -> dict[str, Any]:
        attempts.append(call_id)
        raise TimeoutError("timed out")

    client = FreyaClient(
        trace, SimpleNamespace(get_call=get_call), read_retries=2, retry_backoff_s=0.5, sleep=sleeps.append
    )
    with pytest.raises(FreyaError) as caught:
        client.get_call("call_9")
    assert caught.value.kind == "transport"
    assert attempts == ["call_9"] * 3
    assert sleeps == [0.5, 1.0]
    assert trace.last("get_call")["kind"] == "transport"


def test_get_call_returns_the_call(client: FreyaClient) -> None:
    call = client.get_call("call_7")
    assert call["id"] == "call_7"
    assert call["messages"][0]["message"] == "Hello"


def test_workspace_id_comes_from_the_kit(client: FreyaClient) -> None:
    assert client.workspace_id() == "ws_1"


def test_workflow_status_reports_newest_draft_separately_from_published_version(
    client: FreyaClient, platform: FakePlatform
) -> None:
    workflow_id = platform.seed(published=True)
    platform._store(platform.workflows[workflow_id], sample_workflow())
    before = copy.deepcopy(platform.versions)

    status = client.workflow_status(workflow_id)

    assert status == {
        "workflow_id": workflow_id,
        "agent_id": "ag_1",
        "name": "seeded",
        "version": 2,
        "newest_version_id": f"{workflow_id}_v2",
        "newest_version_number": 2,
        "newest_published": False,
        "published_version_number": 1,
        "updated_at": "2026-10-10T12:00:00Z",
        "node_count": 4,
        "tool_count": 4,
    }
    assert platform.versions == before
    assert all(method == "GET" for method, _, _, _ in platform.calls)
    assert {path for _, path, _, _ in platform.calls} == {
        f"/api/v2/workflow/{workflow_id}",
        f"/api/v2/versions/workflow/{workflow_id}",
    }


def test_workflow_status_reports_published_newest_version(
    client: FreyaClient, platform: FakePlatform
) -> None:
    workflow_id = platform.seed(published=True)

    status = client.workflow_status(workflow_id)

    assert status["newest_published"] is True
    assert status["published_version_number"] == status["newest_version_number"] == 1
    assert all(method == "GET" for method, _, _, _ in platform.calls)


@pytest.mark.parametrize("flag", [None, "true", "false", 0, 1])
def test_workflow_status_with_ambiguous_publication_flag_stays_unknown(
    client: FreyaClient,
    platform: FakePlatform,
    flag: Any,
) -> None:
    workflow_id = platform.seed()
    platform.versions[workflow_id][-1]["isPublished"] = flag
    status = client.workflow_status(workflow_id)
    assert status["newest_published"] is None
    assert status["published_version_number"] is None


def test_workflow_status_with_missing_publication_flag_stays_unknown(
    client: FreyaClient,
    platform: FakePlatform,
) -> None:
    workflow_id = platform.seed()
    platform.versions[workflow_id][-1].pop("isPublished")
    assert client.workflow_status(workflow_id)["newest_published"] is None


def test_workflow_status_without_versions_reports_unknown_version(
    client: FreyaClient, platform: FakePlatform
) -> None:
    workflow_id = platform.seed()
    platform.versions[workflow_id] = []

    status = client.workflow_status(workflow_id)

    assert status["newest_version_number"] is None
    assert status["newest_version_id"] is None
    assert status["newest_published"] is None
    assert status["published_version_number"] is None


@pytest.mark.parametrize("wrapped", [True, False])
def test_simulation_status_reads_once_without_starting_or_polling(wrapped: bool) -> None:
    requests: list[tuple[str, str]] = []
    simulation = {"id": "sim-7", "status": "running", "completedIterations": 2, "totalIterations": 5}

    def api(method: str, path: str) -> dict[str, Any]:
        requests.append((method, path))
        return {"simulation": simulation} if wrapped else simulation

    trace = NullTrace()
    client = FreyaClient(trace, SimpleNamespace(api=api))

    assert client.simulation_status("sim-7") == simulation
    assert requests == [("GET", "/api/v2/simulations/sim-7")]
    request = trace.of_kind("request")[0]["data"]
    assert request["simulation_id"] == "sim-7"
    assert request["operation"] == "simulation_status"
    assert request["method"] == "GET"


@pytest.mark.parametrize("response", [[], {"simulation": None}, {"simulation": []}])
def test_simulation_status_rejects_invalid_response(response: Any) -> None:
    client = FreyaClient(None, SimpleNamespace(api=lambda *args: response))

    with pytest.raises(FreyaError, match="unexpected response shape"):
        client.simulation_status("sim-7")


def test_request_telemetry_records_successful_chat_without_payload(platform: FakePlatform) -> None:
    timestamps = iter([10.0, 10.025])
    trace = NullTrace()
    client = FreyaClient(trace, make_kit(platform), monotonic=lambda: next(timestamps))

    client.chat("wf-7", "Sensitive customer reply", {"password": "never-log-this-value"})

    events = trace.of_kind("request")
    assert len(events) == 1
    request = events[0]["data"]
    assert request["service"] == "freya"
    assert request["scope"] == "wrapper"
    assert request["operation"] == "chat"
    assert request["method"] == "POST"
    assert request["path"] == "/api/v2/chat/message"
    assert request["ok"] is True
    assert request["duration_ms"] == 25.0
    assert request["attempt"] == request["max_attempts"] == 1
    assert request["retry_scheduled"] is False
    assert request["workflow_id"] == "wf-7"
    assert request["call_id"] == "call_1"
    assert "status" not in request  # the kit does not expose successful HTTP status codes
    assert "Sensitive customer reply" not in json.dumps(events)
    assert "never-log-this-value" not in json.dumps(events)
    assert trace.of_kind("freya")[-1]["data"]["action"] == "chat"


def test_request_telemetry_records_each_read_retry_with_duration_and_shared_id(
    platform: FakePlatform,
) -> None:
    path = "/api/v2/call/call_7"
    platform.fail("GET", path, http_error("GET", path, 503, "private-error-body"), TimeoutError("timed out"))
    timestamps = iter([0.0, 0.1, 1.0, 1.2, 2.0, 2.3])
    sleeps: list[float] = []
    trace = NullTrace()
    client = FreyaClient(
        trace,
        make_kit(platform),
        sleep=sleeps.append,
        retry_backoff_s=0.5,
        monotonic=lambda: next(timestamps),
    )

    assert client.get_call("call_7")["id"] == "call_7"

    requests = [event["data"] for event in trace.of_kind("request")]
    assert len(requests) == 3
    assert [request["attempt"] for request in requests] == [1, 2, 3]
    assert [request["ok"] for request in requests] == [False, False, True]
    assert [request["duration_ms"] for request in requests] == [100.0, 200.0, 300.0]
    assert [request["retry_scheduled"] for request in requests] == [True, True, False]
    assert [request["retry_delay_ms"] for request in requests] == [500.0, 1000.0, 0.0]
    assert requests[0]["status"] == 503
    assert requests[0]["error_type"] == "RuntimeError"
    assert requests[1]["error_kind"] == "transport"
    assert "status" not in requests[1]
    assert len({request["request_id"] for request in requests}) == 1
    assert all(request["max_attempts"] == 3 for request in requests)
    assert all(request["method"] == "GET" and request["path"] == path for request in requests)
    assert all(request["call_id"] == "call_7" for request in requests)
    assert "private-error-body" not in json.dumps(requests)
    assert sleeps == [0.5, 1.0]


def test_failed_chat_request_telemetry_does_not_retry_or_include_error_body(
    client: FreyaClient, platform: FakePlatform, trace: RecordingTrace
) -> None:
    platform.fail(
        "POST", "/api/v2/chat/message", http_error("POST", "/api/v2/chat/message", 503, "secret-body")
    )

    with pytest.raises(FreyaError):
        client.chat("wf-7", "Hello", call_id="existing-call")

    request = trace.last("request")
    assert request["ok"] is False
    assert request["status"] == 503
    assert request["retryable"] is True
    assert request["retry_scheduled"] is False
    assert request["attempt"] == request["max_attempts"] == 1
    assert request["call_id"] == "existing-call"
    assert "secret-body" not in json.dumps(request)
    assert platform.count("POST", "/api/v2/chat/message") == 1


def test_wrapper_request_telemetry_preserves_unexpected_exception_type(trace: RecordingTrace) -> None:
    failure = AssertionError("unexpected local error")

    def workspace_id() -> str:
        raise failure

    client = FreyaClient(trace, SimpleNamespace(workspace_id=workspace_id))

    with pytest.raises(AssertionError) as caught:
        client.workspace_id()

    assert caught.value is failure
    assert trace.last("request")["error_type"] == "AssertionError"
    assert trace.last("request")["retry_scheduled"] is False


def test_request_telemetry_failure_does_not_break_successful_call(platform: FakePlatform) -> None:
    class BrokenRequestTrace(RecordingTrace):
        def event(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError("trace unavailable")

    trace = BrokenRequestTrace()
    client = FreyaClient(trace, make_kit(platform))

    assert client.get_call("call_7")["id"] == "call_7"


# ---------------------------------------------------------------------------------------------------------- chat


def test_chat_first_turn_sends_variables_without_nulls(client: FreyaClient, platform: FakePlatform) -> None:
    turn = client.chat("wf_1", "Hello?", {"customer_id": "c_1", "title": "Mr", "nickname": None})

    assert turn == ChatTurn(
        response="echo: Hello?", call_id="call_1", raw={"response": "echo: Hello?", "callId": "call_1"}
    )
    method, path, body, _ = platform.calls[-1]
    assert (method, path) == ("POST", "/api/v2/chat/message")
    assert body == {
        "workflowId": "wf_1",
        "message": "Hello?",
        "customerVariables": {"customer_id": "c_1", "title": "Mr"},
    }


def test_chat_next_turn_continues_the_call(
    client: FreyaClient, platform: FakePlatform, trace: RecordingTrace
) -> None:
    client.chat("wf_1", "Yes, speaking.", call_id="call_1", version_id="v_3")
    body = platform.calls[-1][2]
    assert body == {"workflowId": "wf_1", "message": "Yes, speaking.", "callId": "call_1", "versionId": "v_3"}
    event = trace.last("chat")
    assert event["call_id"] == "call_1"
    assert event["response"] == "echo: Yes, speaking."


def test_chat_turn_flags(client: FreyaClient, platform: FakePlatform) -> None:
    platform.chat_result = {
        "response": "Goodbye.",
        "callId": "call_2",
        "endedByAgent": True,
        "warning": "slow",
    }
    turn = client.chat("wf_1", "Bye")
    assert turn.ended_by_agent is True
    assert turn.warning == "slow"
    assert ChatTurn(response="", call_id="c").ended_by_agent is False


def test_chat_without_a_call_id_in_the_response_fails(client: FreyaClient, platform: FakePlatform) -> None:
    platform.chat_result = {"response": "Hi"}
    with pytest.raises(FreyaError, match="callId"):
        client.chat("wf_1", "Hello?")


def test_chat_turns_are_never_retried(
    client: FreyaClient, platform: FakePlatform, sleeps: list[float]
) -> None:
    platform.fail("POST", "/api/v2/chat/message", http_error("POST", "/api/v2/chat/message", 504, "timeout"))
    with pytest.raises(FreyaError) as caught:
        client.chat("wf_1", "Hello?")
    assert caught.value.status == 504
    assert platform.count("POST", r"/api/v2/chat/message") == 1
    assert sleeps == []


def test_end_chat_posts_the_call_id(
    client: FreyaClient, platform: FakePlatform, trace: RecordingTrace
) -> None:
    assert client.end_chat("call_1") == {"ended": True}
    assert platform.calls[-1][:3] == ("POST", "/api/v2/chat/end", {"callId": "call_1"})
    assert trace.last("chat_end") == {"ok": True, "call_id": "call_1"}


def test_long_chat_text_is_clipped_in_the_trace(client: FreyaClient, trace: RecordingTrace) -> None:
    client.chat("wf_1", "word " * 500)
    assert len(trace.last("chat")["message"]) <= 400


# ---------------------------------------------------------------------------------------------------------- helpers


def test_counts_on_the_kit_example() -> None:
    workflow = json.loads((REPO_ROOT / "examples" / "base-agent.json").read_text(encoding="utf-8"))
    assert count_nodes(workflow) == len(workflow["nodes"]) + len(workflow.get("global_nodes") or {})
    expected = {t["function"]["name"] for n in workflow["nodes"].values() for t in n.get("tools", [])}
    expected |= {t["function"]["name"] for t in workflow.get("tools", [])}
    assert tool_names(workflow) == expected
    assert count_tools(workflow) == len(expected)


def test_tool_names_cover_pre_actions_global_nodes_and_workflow_tools_but_not_terminals() -> None:
    assert tool_names(sample_workflow()) == {"code_exec", "lookup", "close", "log"}
    assert count_nodes(sample_workflow()) == 4
    assert count_nodes({"nodes": "broken"}) == 0
    assert (
        tool_names({"nodes": {"a": {"tools": [{"function": {}}, "junk", {"function": {"name": ""}}]}}})
        == set()
    )


def test_http_error_text_is_parsed() -> None:
    error = client_mod._translate("save", http_error("PUT", "/api/v2/workflow/x", 409, "conflict\nline two"))
    assert (error.status, error.body, error.kind) == (409, "conflict\nline two", "http")
    assert (error.method, error.path) == ("PUT", "/api/v2/workflow/x")
    assert error.retryable is False
    assert client_mod._translate("save", http_error("GET", "/x", 429, "slow down")).retryable is True
    other = client_mod._translate("save", RuntimeError("something else"))
    assert (other.status, other.kind) == (None, "response")
    assert client_mod._translate("save", KeyError("workflow")).kind == "response"


def test_default_state_file_follows_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        client_mod, "get_settings", lambda: SimpleNamespace(freya_state_file=tmp_path / "s.json")
    )
    assert default_state_file() == tmp_path / "s.json"
    monkeypatch.setattr(
        client_mod, "get_settings", lambda: SimpleNamespace(freya_state_file=Path("state/s.json"))
    )
    assert default_state_file() == kit_mod.repo_root() / "state" / "s.json"
    monkeypatch.setattr(client_mod, "get_settings", lambda: SimpleNamespace(freya_state_file=None))
    assert default_state_file() == kit_mod.repo_root() / ".freya-agents.json"


def test_client_without_a_state_file_uses_the_default(
    monkeypatch: pytest.MonkeyPatch, platform: FakePlatform, tmp_path: Path
) -> None:
    monkeypatch.setattr(client_mod, "default_state_file", lambda: tmp_path / "default.json")
    FreyaClient(None, make_kit(platform)).save("slot", sample_workflow())
    assert json.loads((tmp_path / "default.json").read_text())["slot"]["workflow_id"] == "wf_1"


def test_kit_is_loaded_lazily(monkeypatch: pytest.MonkeyPatch, platform: FakePlatform) -> None:
    loads: list[int] = []

    def fake_load() -> SimpleNamespace:
        loads.append(1)
        return make_kit(platform)

    monkeypatch.setattr(client_mod, "load_kit", fake_load)
    client = FreyaClient(RecordingTrace())
    assert loads == []
    assert client.workspace_id() == "ws_1"
    assert client.workspace_id() == "ws_1"
    assert loads == [1]


# ---------------------------------------------------------------------------------------------------------- the real kit


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a unit test tried to open a network connection")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)


@pytest.fixture
def kit_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test values for the variables the kit reads at import; the organisers' model overrides are removed."""
    monkeypatch.setenv("FREYA_API_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("FREYA_API_KEY", "fak_unit_test_placeholder")
    monkeypatch.setenv("FREYA_WORKSPACE_ID", "ws_1")
    monkeypatch.delenv("FREYA_AGENT_PROVIDER", raising=False)
    monkeypatch.delenv("FREYA_AGENT_MODEL", raising=False)


def load_real_kit(platform: FakePlatform) -> ModuleType:
    """Execute the kit's freya.py privately and route its ``api`` to the fake platform."""
    spec = importlib.util.spec_from_file_location("freya_kit_under_test", REPO_ROOT / "freya.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.api = platform.api
    return module


@pytest.mark.usefixtures("no_network", "kit_env")
def test_wrapper_agrees_with_the_real_kit_semantics(trace: RecordingTrace, tmp_path: Path) -> None:
    platform = FakePlatform()
    kit = load_real_kit(platform)
    assert kit.AGENT_LLM == EVENT_LLM
    client = FreyaClient(trace, kit, state_file=tmp_path / "agents.json")

    created = client.save("slot", sample_workflow())
    assert created.created is True
    assert (
        platform.workflows[created.workflow_id]["workflow"]["agent_config"]["llmConfig"]["model"]
        == "experimental-5"
    )
    assert created.published is True
    assert created.version == max(v["versionNumber"] for v in platform.versions[created.workflow_id])

    workflow = sample_workflow()
    workflow["system_prompt"] = "Second build."
    updated = client.save("slot", workflow)
    assert updated.created is False
    assert updated.workflow_id == created.workflow_id
    assert updated.version is not None and created.version is not None and updated.version > created.version
    assert updated.published is True
    assert platform.workflows[created.workflow_id]["workflow"]["system_prompt"] == "Second build."

    export = client.export(created.workflow_id, tmp_path / "out" / "slot.json")
    assert json.loads(export.read_text())["workflow"]["system_prompt"] == "Second build."
    assert client.get_call("call_5")["id"] == "call_5"


# ---------------------------------------------------------------------------------------------------------- load_kit


@pytest.fixture
def fresh_kit_loader(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[int]]:
    """Reset the kit cache, restore sys.path / sys.modules after the test, and count env applications."""
    applied: list[int] = []
    monkeypatch.setattr(kit_mod, "_KIT", None)
    monkeypatch.setattr(kit_mod, "_apply_env", lambda: applied.append(1))
    monkeypatch.setattr(sys, "path", list(sys.path))
    original = sys.modules.pop("freya", None)
    try:
        yield applied
    finally:
        sys.modules.pop("freya", None)
        if original is not None:
            sys.modules["freya"] = original


@pytest.mark.usefixtures("no_network", "kit_env")
def test_load_kit_executes_the_kit_file_once_with_the_environment(fresh_kit_loader: list[int]) -> None:
    kit = load_kit()

    assert Path(kit.__file__).resolve() == (REPO_ROOT / "freya.py").resolve()
    assert kit.URL == "http://127.0.0.1:9"
    assert kit.AGENT_LLM == EVENT_LLM
    assert sys.modules["freya"] is kit
    assert str(kit_mod.repo_root()) in sys.path
    assert load_kit() is kit
    assert fresh_kit_loader == [1]
    assert load_kit(reload=True) is not kit
    assert fresh_kit_loader == [1, 1]


@pytest.mark.usefixtures("no_network", "kit_env")
def test_load_kit_does_not_clobber_a_foreign_freya_module(fresh_kit_loader: list[int]) -> None:
    foreign = types.ModuleType("freya")
    foreign.__file__ = str(REPO_ROOT / "builder" / "freya" / "__init__.py")
    sys.modules["freya"] = foreign

    kit = load_kit()

    assert sys.modules["freya"] is foreign
    assert callable(kit.save_workflow)


def test_load_kit_without_the_kit_file_fails_clearly(
    fresh_kit_loader: list[int], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(kit_mod.paths, "REPO_ROOT", tmp_path)
    with pytest.raises(KitError, match="not found"):
        load_kit()


def test_load_kit_rejects_a_file_that_is_not_the_kit(
    fresh_kit_loader: list[int], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "freya.py").write_text("x = 1\n")
    monkeypatch.setattr(kit_mod.paths, "REPO_ROOT", tmp_path)
    with pytest.raises(KitError, match="does not look like"):
        load_kit()
