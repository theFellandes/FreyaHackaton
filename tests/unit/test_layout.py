"""Tests for builder.compiler.layout (editor node positions)."""

from __future__ import annotations

import json
import pathlib

import pytest

from builder.compiler.layout import COLUMN_GAP, MAX_ROWS_PER_COLUMN, ROW_GAP, compute_layout

REPO = pathlib.Path(__file__).resolve().parents[2]


def _node(*targets: str, **extra: object) -> dict:
    return {"transitions": [{"target_node": t} for t in targets], **extra}


def test_linear_chain_reads_left_to_right() -> None:
    wf = {"initial_node": "a", "nodes": {"a": _node("b"), "b": _node("c"), "c": _node()}}
    pos = compute_layout(wf)
    assert [pos[k]["x"] for k in "abc"] == [0.0, COLUMN_GAP, 2 * COLUMN_GAP]
    assert {pos[k]["y"] for k in "abc"} == {0.0}


def test_loops_do_not_stretch_the_layout() -> None:
    # negotiate -> fix -> say -> negotiate is a loop; the back edge must be ignored
    wf = {"initial_node": "n", "nodes": {"n": _node("fix", "end"), "fix": _node("say"), "say": _node("n"), "end": _node()}}
    pos = compute_layout(wf)
    assert pos["n"]["x"] == 0.0
    assert pos["fix"]["x"] == COLUMN_GAP
    assert pos["say"]["x"] == 2 * COLUMN_GAP


def test_join_node_lands_after_all_its_inputs() -> None:
    wf = {"initial_node": "s", "nodes": {"s": _node("a", "b"), "a": _node("d"), "b": _node("c"), "c": _node("d"), "d": _node()}}
    pos = compute_layout(wf)
    assert pos["d"]["x"] > pos["c"]["x"] > pos["b"]["x"]


def test_no_two_nodes_share_a_position_and_tall_columns_wrap() -> None:
    ends = [f"end_{i}" for i in range(MAX_ROWS_PER_COLUMN + 3)]
    nodes = {"s": _node(*ends), **{e: _node("decide") for e in ends}, "decide": _node()}
    pos = compute_layout({"initial_node": "s", "nodes": nodes})
    coords = {(p["x"], p["y"]) for p in pos.values()}
    assert len(coords) == len(pos)
    assert max(pos[e]["y"] for e in ends) <= (MAX_ROWS_PER_COLUMN - 1) * ROW_GAP
    assert pos["decide"]["x"] > max(pos[e]["x"] for e in ends)


def test_passthrough_and_max_turn_targets_count_as_edges() -> None:
    wf = {"initial_node": "a", "nodes": {"a": _node(passthrough_default_transition="b"), "b": _node(max_turn_transition="c"), "c": _node()}}
    pos = compute_layout(wf)
    assert pos["a"]["x"] < pos["b"]["x"] < pos["c"]["x"]


def test_global_nodes_get_a_row_above() -> None:
    wf = {"initial_node": "a", "nodes": {"a": _node()}, "global_nodes": {"g1": {}, "g2": {}}}
    pos = compute_layout(wf)
    assert pos["g1"]["y"] < 0 and pos["g2"]["y"] < 0


def test_empty_workflow() -> None:
    assert compute_layout({"nodes": {}}) == {}


@pytest.mark.parametrize("name", ["ref-a", "ref-ab"])
def test_reference_workflows_are_fully_and_uniquely_placed(name: str) -> None:
    path = REPO / "evals" / "reference" / "out" / f"{name}.workflow.json"
    if not path.exists():
        pytest.skip("reference JSON not generated")
    wf = json.loads(path.read_text(encoding="utf-8"))
    pos = compute_layout(wf)
    assert set(pos) == set(wf["nodes"])
    assert len({(p["x"], p["y"]) for p in pos.values()}) == len(pos)
    assert pos[wf["initial_node"]]["x"] == 0.0
