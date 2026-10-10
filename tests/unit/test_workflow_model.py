"""Tests for builder.compiler.workflow_model: the typed, tolerant mirror of the workflow JSON."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from builder.compiler.workflow_model import (
    Always,
    ApiRequestNode,
    CodeNode,
    ConversationNode,
    EndCallNode,
    Group,
    MatchesIntent,
    OtherNode,
    VarCondition,
    Workflow,
    strip_none,
)

REPO = Path(__file__).resolve().parents[2]


def _transition(condition: dict[str, Any]) -> dict[str, Any]:
    return {"target_node": "b", "description": "d", "condition": condition}


def test_base_agent_round_trips_without_nulls() -> None:
    raw = json.loads((REPO / "examples" / "base-agent.json").read_text(encoding="utf-8"))
    workflow = Workflow.from_json(raw)
    assert workflow.to_json() == strip_none(raw)


def test_unknown_keys_are_kept_so_platform_exports_survive() -> None:
    raw = {
        "initial_node": "a",
        "nodes": {"a": {"name": "a", "node_type": "conversation", "position": {"x": 1}, "assertions": [], "transitions": []}},
        "nodePositions": {"a": {"x": 0}},
    }
    out = Workflow.from_json(raw).to_json()
    assert out["nodes"]["a"]["position"] == {"x": 1}
    assert out["nodePositions"] == {"a": {"x": 0}}


def test_node_types_and_other_types() -> None:
    raw = {
        "initial_node": "c",
        "nodes": {
            "c": {"node_type": "conversation"},
            "r": {"node_type": "api_request"},
            "k": {"node_type": "code"},
            "e": {"node_type": "end_call"},
            "t": {"node_type": "transfer_call", "phone": "123"},
        },
    }
    nodes = Workflow.from_json(raw).nodes
    assert isinstance(nodes["c"], ConversationNode)
    assert isinstance(nodes["r"], ApiRequestNode)
    assert isinstance(nodes["k"], CodeNode)
    assert isinstance(nodes["e"], EndCallNode)
    assert isinstance(nodes["t"], OtherNode)
    assert nodes["t"].model_extra == {"phone": "123"}


@pytest.mark.parametrize(
    ("condition", "model"),
    [
        ({"operator": "always"}, Always),
        ({"operator": "matches_intent", "description": "wants a human"}, MatchesIntent),
        ({"variable": "x", "operator": "equals", "operand": "true"}, VarCondition),
        ({"variable": "x", "operator": "is_set"}, VarCondition),
        ({"logic": "or", "conditions": [{"variable": "x", "operator": "equals", "operand": 1}]}, Group),
    ],
)
def test_condition_shapes_pick_the_right_model(condition: dict[str, Any], model: type) -> None:
    raw = {"initial_node": "a", "nodes": {"a": {"node_type": "conversation", "transitions": [_transition(condition)]}}}
    parsed = Workflow.from_json(raw).nodes["a"].transitions
    assert parsed is not None and isinstance(parsed[0].condition, model)
    assert Workflow.from_json(raw).to_json() == raw


def test_nested_and_group_round_trips() -> None:
    condition = {"logic": "or", "conditions": [{"logic": "and", "conditions": [
        {"variable": "n", "operator": "greater_than", "operand": 0}, {"variable": "m", "operator": "equals", "operand": "1"}]}]}
    raw = {"initial_node": "a", "nodes": {"a": {"node_type": "conversation", "transitions": [_transition(condition)]}}}
    assert Workflow.from_json(raw).to_json() == raw


def test_graph_helpers() -> None:
    raw = {
        "initial_node": "a",
        "nodes": {
            "a": {"node_type": "conversation", "max_turn_transition": "z",
                  "transitions": [{"target_node": "b", "is_tool_call": True}],
                  "tools": [{"type": "function", "function": {"name": "t1"}}]},
            "b": {"node_type": "code", "passthrough_default_transition": "z",
                  "pre_actions": [{"type": "function", "function": {"name": "code_exec"}}]},
            "z": {"node_type": "end_call"},
        },
        "global_nodes": {"g": {"node_type": "conversation", "condition": {"operator": "always"}}},
    }
    workflow = Workflow.from_json(raw)
    assert workflow.node_names() == ["a", "b", "z", "g"]
    assert set(workflow.edges()) == {("a", "b", "tool_call"), ("a", "z", "max_turn"), ("b", "z", "passthrough")}
    assert workflow.tool_names() == {"t1", "code_exec"}
    assert workflow.get("g") is not None and workflow.get("missing") is None


def test_to_json_drops_none_everywhere() -> None:
    workflow = Workflow.from_json({"initial_node": "a", "nodes": {"a": {"node_type": "conversation"}}})
    assert "null" not in json.dumps(workflow.to_json())
    assert strip_none({"a": None, "b": [1, None, {"c": None, "d": 2}]}) == {"b": [1, {"d": 2}]}


def test_missing_required_keys_are_rejected() -> None:
    with pytest.raises(ValueError):
        Workflow.from_json({"nodes": {}})
