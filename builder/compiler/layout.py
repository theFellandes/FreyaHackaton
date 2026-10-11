"""Readable node positions for the Freya visual editor.

Freya stores the editor layout as record-level ``nodePositions`` (``{node_key: {"x": .., "y": ..}}``), next to the
``workflow`` definition (it is rejected *inside* the definition). Without positions the editor stacks every node on top of
each other, which makes a compiled workflow unreadable. Positions do not change behaviour or scoring.

The layout is generic (it only looks at the graph):

* columns = longest-path layers from ``initial_node`` over all forward edges (transitions, passthrough defaults and
  max-turn targets); back edges are ignored so loops do not stretch the graph;
* the main conversation path reads left to right, side branches sit below it;
* within a column nodes are ordered by the average row of their predecessors (barycenter), which keeps edges short;
* very tall columns (for example many ``end_*`` code nodes feeding one ``decide`` node) wrap into extra sub-columns;
* ``global_nodes`` get their own row above the graph.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

COLUMN_GAP = 520.0
ROW_GAP = 420.0
MAX_ROWS_PER_COLUMN = 6
GLOBAL_ROW_Y = -ROW_GAP


def _targets(node: Mapping[str, Any]) -> list[str]:
    """Every node key this node can move to, in declaration order (duplicates removed)."""
    out: list[str] = []
    for transition in node.get("transitions") or []:
        target = transition.get("target_node")
        if isinstance(target, str):
            out.append(target)
    for key in ("passthrough_default_transition", "max_turn_transition"):
        target = node.get(key)
        if isinstance(target, str) and target:
            out.append(target)
    seen: set[str] = set()
    return [t for t in out if not (t in seen or seen.add(t))]


def _forward_edges(nodes: Mapping[str, Mapping[str, Any]], start: str) -> dict[str, list[str]]:
    """Edges of the graph with back edges (those closing a cycle in a DFS from ``start``) removed."""
    edges = {key: [t for t in _targets(node) if t in nodes and t != key] for key, node in nodes.items()}
    forward: dict[str, list[str]] = {key: [] for key in nodes}
    state: dict[str, int] = {}  # 1 = on stack, 2 = done

    def visit(root: str) -> None:
        stack: list[tuple[str, int]] = [(root, 0)]
        state[root] = 1
        while stack:
            key, index = stack.pop()
            children = edges[key]
            if index < len(children):
                stack.append((key, index + 1))
                child = children[index]
                if state.get(child) == 1:
                    continue  # back edge: closes a loop
                forward[key].append(child)
                if child not in state:
                    state[child] = 1
                    stack.append((child, 0))
            else:
                state[key] = 2

    if start in nodes:
        visit(start)
    for key in nodes:  # unreachable islands still get a place
        if key not in state:
            visit(key)
    return forward


def _layers(forward: Mapping[str, list[str]], order: list[str], start: str) -> dict[str, int]:
    """Longest-path layer of every node (Kahn order over the forward DAG)."""
    indegree = {key: 0 for key in order}
    for targets in forward.values():
        for target in targets:
            indegree[target] += 1
    layer = {key: 0 for key in order}
    queue = [key for key in order if indegree[key] == 0]
    if start in queue:  # the initial node first, so ties keep the main path on top
        queue.remove(start)
        queue.insert(0, start)
    while queue:
        key = queue.pop(0)
        for target in forward[key]:
            layer[target] = max(layer[target], layer[key] + 1)
            indegree[target] -= 1
            if indegree[target] == 0:
                queue.append(target)
    return layer


def compute_layout(workflow: Mapping[str, Any]) -> dict[str, dict[str, float]]:
    """Return ``{node_key: {"x": float, "y": float}}`` for every node and global node of ``workflow``."""
    nodes: Mapping[str, Mapping[str, Any]] = workflow.get("nodes") or {}
    order = list(nodes)
    if not order:
        return {}
    start = workflow.get("initial_node") if workflow.get("initial_node") in nodes else order[0]
    forward = _forward_edges(nodes, start)
    layer = _layers(forward, order, start)
    predecessors: dict[str, list[str]] = {key: [] for key in order}
    for key, targets in forward.items():
        for target in targets:
            predecessors[target].append(key)

    columns: dict[int, list[str]] = {}
    for key in order:
        columns.setdefault(layer[key], []).append(key)

    row_of: dict[str, float] = {}
    positions: dict[str, dict[str, float]] = {}
    x_offset = 0.0
    for column in sorted(columns):
        members = columns[column]
        # barycenter ordering: follow the rows of the predecessors; declaration order breaks ties
        members.sort(key=lambda k: (
            sum(row_of.get(p, 0.0) for p in predecessors[k]) / len(predecessors[k]) if predecessors[k] else 0.0,
            order.index(k),
        ))
        chunks = [members[i:i + MAX_ROWS_PER_COLUMN] for i in range(0, len(members), MAX_ROWS_PER_COLUMN)]
        for chunk_index, chunk in enumerate(chunks):
            for row, key in enumerate(chunk):
                row_of[key] = float(row)
                positions[key] = {"x": x_offset + chunk_index * COLUMN_GAP, "y": row * ROW_GAP}
        x_offset += COLUMN_GAP * len(chunks)

    for index, key in enumerate(workflow.get("global_nodes") or {}):
        positions[key] = {"x": index * COLUMN_GAP, "y": GLOBAL_ROW_Y}
    return positions
