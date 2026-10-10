"""The shape of the builder graph: which nodes exist, how they connect, and that no node is a dead end.

The expected sets below are the specification (contract + guard flow + fallback); ``docs/GRAPH.md`` shows the same
diagram. The compiled LangGraph is compared with ``builder.graph.build_graph.edge_list`` and with the sets written out here.
"""

from __future__ import annotations

import inspect
from collections import defaultdict

from langgraph.graph import END, START

from builder.graph.build_graph import build_graph, edge_list
from builder.graph.nodes import NODES
from builder.graph.routing import ROUTERS

EXPECTED_NODES = {
    "load_inputs",
    "guard_inputs",
    "understand_brief",
    "extract_policy",
    "validate_policy",
    "repair_policy",
    "guard_policy",
    "draft_prose",
    "compile_workflow",
    "static_checks",
    "repair_compile",
    "guard_workflow",
    "remote_validate",
    "deploy",
    "self_test",
    "decide",
    "agentic_build",
    "finalize",
}

#: The happy path, edge by edge (each is one of the possible routes of its source).
HAPPY_PATH = [
    (START, "load_inputs"),
    ("load_inputs", "guard_inputs"),
    ("guard_inputs", "understand_brief"),
    ("understand_brief", "extract_policy"),
    ("extract_policy", "validate_policy"),
    ("validate_policy", "guard_policy"),
    ("guard_policy", "draft_prose"),
    ("draft_prose", "compile_workflow"),
    ("compile_workflow", "static_checks"),
    ("static_checks", "guard_workflow"),
    ("guard_workflow", "remote_validate"),
    ("remote_validate", "deploy"),
    ("deploy", "self_test"),
    ("self_test", "decide"),
    ("decide", "finalize"),
    ("finalize", END),
]

LOOPS_AND_FALLBACKS = [
    ("validate_policy", "repair_policy"),
    ("repair_policy", "validate_policy"),
    ("guard_policy", "repair_policy"),
    ("static_checks", "repair_compile"),
    ("guard_workflow", "repair_compile"),
    ("remote_validate", "repair_compile"),
    ("deploy", "repair_compile"),
    ("repair_compile", "compile_workflow"),
    ("decide", "repair_policy"),
    ("guard_inputs", "agentic_build"),
    ("understand_brief", "agentic_build"),
    ("extract_policy", "agentic_build"),
    ("static_checks", "agentic_build"),
    ("agentic_build", "static_checks"),
    ("agentic_build", "finalize"),
]


def compiled_edges() -> set[tuple[str, str]]:
    drawable = build_graph().get_graph()
    return {(edge.source, edge.target) for edge in drawable.edges}


def test_node_set_is_exactly_the_specified_one() -> None:
    drawable = build_graph().get_graph()
    assert set(drawable.nodes) - {START, END, "__start__", "__end__"} == EXPECTED_NODES
    assert set(NODES) == EXPECTED_NODES


def test_every_node_module_exposes_the_langgraph_signature() -> None:
    for name, run in NODES.items():
        assert inspect.iscoroutinefunction(run), name
        assert list(inspect.signature(run).parameters) == ["state", "runtime"], name


def test_edge_list_matches_the_compiled_graph() -> None:
    declared = {(a, b) for a, b, _ in edge_list()}
    normalised = {("__start__" if a == START else a, "__end__" if b == END else b) for a, b in declared}
    actual = {("__start__" if a == START else a, "__end__" if b == END else b) for a, b in compiled_edges()}
    assert actual == normalised


def test_happy_path_and_loops_are_edges() -> None:
    actual = compiled_edges()
    for source, target in [*HAPPY_PATH, *LOOPS_AND_FALLBACKS]:
        assert (source, target) in actual, f"missing edge {source} -> {target}"


def test_every_router_target_is_a_node_and_every_node_has_a_way_out() -> None:
    for source, (_, targets) in ROUTERS.items():
        assert source in EXPECTED_NODES
        assert set(targets) <= EXPECTED_NODES, source
    successors: dict[str, set[str]] = defaultdict(set)
    for a, b, _ in edge_list():
        successors[a].add(b)
    for node in EXPECTED_NODES - {"finalize"}:
        assert successors[node], f"{node} is a dead end"


def test_every_node_is_reachable_and_reaches_the_end() -> None:
    forward: dict[str, set[str]] = defaultdict(set)
    backward: dict[str, set[str]] = defaultdict(set)
    for a, b, _ in edge_list():
        forward[a].add(b)
        backward[b].add(a)

    def reach(start: str, graph: dict[str, set[str]]) -> set[str]:
        seen, todo = {start}, [start]
        while todo:
            for nxt in graph[todo.pop()]:
                if nxt not in seen:
                    seen.add(nxt)
                    todo.append(nxt)
        return seen

    from_start = reach(START, forward)
    to_end = reach(END, backward)
    assert from_start >= EXPECTED_NODES, EXPECTED_NODES - from_start
    assert to_end >= EXPECTED_NODES, EXPECTED_NODES - to_end


def test_finalize_is_the_only_way_to_the_end() -> None:
    assert [a for a, b, _ in edge_list() if b == END] == ["finalize"]


def test_the_deploy_gate_is_only_reachable_through_the_guard_and_the_platform_check() -> None:
    into_deploy = {a for a, b, _ in edge_list() if b == "deploy"}
    into_validate = {a for a, b, _ in edge_list() if b == "remote_validate"}
    assert into_deploy == {"remote_validate"}
    assert into_validate == {"guard_workflow"}
