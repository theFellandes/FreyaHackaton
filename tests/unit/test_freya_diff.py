"""Tests for builder.freya.diff: detecting fields the platform silently stripped, and null detection."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from builder.freya.diff import join_path, null_paths, stripped_fields

REPO_ROOT = Path(__file__).resolve().parents[2]


def _workflow() -> dict[str, Any]:
    """A small workflow with the shapes that matter: nodes, tools, pre_actions, enums, transitions."""
    return {
        "initial_node": "setup",
        "system_prompt": "Be brief.",
        "global_nodes": {},
        "nodes": {
            "setup": {
                "name": "setup",
                "node_type": "code",
                "passthrough": True,
                "pre_actions": [
                    {
                        "type": "function",
                        "function": {"name": "code_exec", "handler": "__function__:code_exec"},
                    }
                ],
                "transitions": [{"target_node": "talk", "condition": {"operator": "always"}}],
            },
            "talk": {
                "name": "talk",
                "node_type": "conversation",
                "task_message": "Talk.",
                "role_message": "",
                "tools": [
                    {
                        "type": "function",
                        "config": {"timeoutMs": 12000, "maxRetries": 0},
                        "function": {
                            "name": "lookup",
                            "parameters": {
                                "type": "object",
                                "properties": {"kind": {"type": "string", "enum": ["a", "b", "c"]}},
                                "required": ["kind"],
                            },
                        },
                        "post_process_code": "pass",
                    }
                ],
                "extraction_schema": [{"name": "x", "type": "string", "min_confidence": 0.7}],
                "transitions": [],
            },
        },
    }


def test_identical_documents_have_no_stripped_fields() -> None:
    sent = _workflow()
    assert stripped_fields(sent, copy.deepcopy(sent)) == []


def test_keys_added_by_the_platform_are_ignored() -> None:
    sent = _workflow()
    stored = copy.deepcopy(sent)
    stored["agent_config"] = {"llmConfig": {"provider": "p", "model": "m"}}
    stored["nodes"]["talk"]["max_node_turns"] = 10
    stored["nodes"]["talk"]["tools"][0]["config"]["interruptible"] = True
    stored["id"] = "wf_1"
    assert stripped_fields(sent, stored) == []


def test_platform_owned_top_level_keys_are_not_compared() -> None:
    sent = _workflow()
    sent["agent_config"] = {"llmConfig": {"provider": "mine"}}
    sent["nodePositions"] = {"talk": {"x": 1, "y": 2}}
    stored = copy.deepcopy(_workflow())
    assert stripped_fields(sent, stored) == []


def test_tools_stripped_from_a_node_are_reported_per_item() -> None:
    sent = _workflow()
    stored = copy.deepcopy(sent)
    stored["nodes"]["talk"]["tools"] = []
    assert stripped_fields(sent, stored) == ["nodes.talk.tools[0]"]


def test_a_missing_key_is_reported_once_not_per_descendant() -> None:
    sent = _workflow()
    stored = copy.deepcopy(sent)
    del stored["nodes"]["setup"]["pre_actions"]
    assert stripped_fields(sent, stored) == ["nodes.setup.pre_actions"]


def test_dropped_hook_and_changed_scalar_are_reported_at_their_paths() -> None:
    sent = _workflow()
    stored = copy.deepcopy(sent)
    del stored["nodes"]["talk"]["tools"][0]["post_process_code"]
    stored["nodes"]["talk"]["tools"][0]["config"]["timeoutMs"] = 30000
    assert stripped_fields(sent, stored) == [
        "nodes.talk.tools[0].config.timeoutMs",
        "nodes.talk.tools[0].post_process_code",
    ]


def test_context_variables_dropped_on_save_is_detected() -> None:
    sent = _workflow()
    sent["context_variables"] = {"items": [{"name": "x", "placement": "post_history"}]}
    stored = _workflow()
    assert stripped_fields(sent, stored) == ["context_variables"]


def test_reordered_scalar_lists_are_not_a_difference_but_a_lost_value_is() -> None:
    sent = _workflow()
    stored = copy.deepcopy(sent)
    enum_path = stored["nodes"]["talk"]["tools"][0]["function"]["parameters"]["properties"]["kind"]
    enum_path["enum"] = ["c", "a", "b"]
    assert stripped_fields(sent, stored) == []
    enum_path["enum"] = ["c", "a"]
    assert stripped_fields(sent, stored) == [
        "nodes.talk.tools[0].function.parameters.properties.kind.enum[1]"
    ]


def test_duplicate_scalars_are_compared_as_a_multiset() -> None:
    assert stripped_fields({"xs": ["a", "a"]}, {"xs": ["a"]}) == ["xs[1]"]


def test_booleans_are_not_equal_to_numbers_or_strings() -> None:
    assert stripped_fields({"flag": True}, {"flag": 1}) == ["flag"]
    assert stripped_fields({"flag": True}, {"flag": "true"}) == ["flag"]
    assert stripped_fields({"n": 2}, {"n": 2.0}) == []


def test_type_change_of_a_container_is_reported() -> None:
    assert stripped_fields({"a": {"b": 1}}, {"a": "text"}) == ["a"]
    assert stripped_fields({"a": [1]}, {"a": {"0": 1}}) == ["a"]


def test_empty_sent_values_dropped_by_the_platform_are_ignored_by_default() -> None:
    sent = _workflow()
    stored = copy.deepcopy(sent)
    del stored["global_nodes"]
    del stored["nodes"]["talk"]["role_message"]
    stored["nodes"]["talk"]["transitions"] = None
    assert stripped_fields(sent, stored) == []
    assert stripped_fields(sent, stored, ignore_empty=False) == [
        "global_nodes",
        "nodes.talk.role_message",
        "nodes.talk.transitions",
    ]


def test_value_replaced_by_null_is_reported() -> None:
    assert stripped_fields({"a": {"b": "x"}}, {"a": {"b": None}}) == ["a.b"]


def test_nothing_stored_reports_every_top_level_key() -> None:
    assert stripped_fields({"initial_node": "a", "nodes": {"a": {}}}, None) == ["initial_node", "nodes"]


def test_custom_ignored_keys() -> None:
    assert stripped_fields({"test_variables": {"a": "1"}}, {}, ignore_top_level={"test_variables"}) == []


def test_odd_keys_are_quoted_in_paths() -> None:
    sent = {"nodes": {"odd.key": {"x": 1}, "Content-Type": "a"}}
    assert stripped_fields(sent, {"nodes": {}}) == ['nodes["odd.key"]', "nodes.Content-Type"]


def test_join_path() -> None:
    assert join_path("", "a") == "a"
    assert join_path("a", "b") == "a.b"
    assert join_path("a", 3) == "a[3]"
    assert join_path("a", "has space") == 'a["has space"]'


def test_null_paths_finds_every_null() -> None:
    document = {"a": None, "b": [1, None, {"c": None}], "d": {"e": "ok"}}
    assert null_paths(document) == ["a", "b[1]", "b[2].c"]
    assert null_paths({"a": "", "b": 0, "c": False, "d": []}) == []
    assert null_paths(None) == ["$"]


@pytest.mark.parametrize("example", ["base-agent.json"])
def test_kit_example_against_itself_and_with_tools_stripped(example: str) -> None:
    sent = json.loads((REPO_ROOT / "examples" / example).read_text(encoding="utf-8"))
    assert stripped_fields(sent, copy.deepcopy(sent)) == []
    stored = copy.deepcopy(sent)
    first_node = next(iter(stored["nodes"]))
    stored["nodes"][first_node]["tools"] = []
    found = stripped_fields(sent, stored)
    assert found
    assert all(path.startswith(f"nodes.{first_node}.tools[") for path in found)
