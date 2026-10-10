"""Assembly of the builder graph (LangGraph ``StateGraph``).

``build_graph()`` registers the nodes of :mod:`builder.graph.nodes`, the conditional edges of
:data:`builder.graph.routing.ROUTERS` and the few static edges, and compiles the graph with
``context_schema=BuilderContext``. Run it with the context as a ``context=`` argument::

    graph = build_graph()
    final_state = await graph.ainvoke(initial_state(...), context=ctx, config={"recursion_limit": 100})

Happy path (see ``docs/GRAPH.md`` for the full diagram)::

    load_inputs -> guard_inputs -> understand_brief -> extract_policy -> validate_policy -> guard_policy
      -> draft_prose -> compile_workflow -> static_checks -> guard_workflow -> remote_validate -> deploy
      -> self_test -> decide -> finalize

with the loops ``validate_policy <-> repair_policy``, ``static_checks/guard_workflow/remote_validate -> repair_compile ->
compile_workflow``, ``decide -> repair_policy`` (after a failing self-test) and the fallback ``* -> agentic_build ->
static_checks``. A dry run ends after ``guard_workflow``.
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from builder.graph.context import BuilderContext
from builder.graph.nodes import NODES
from builder.graph.routing import FINALIZE, LOAD_INPUTS, ROUTERS, STATIC_EDGES
from builder.graph.state import BuilderState

#: Recursion limit used by ``build.py`` (the longest legitimate path with all repair loops is about 60 steps).
RECURSION_LIMIT = 100


def edge_list() -> list[tuple[str, str, bool]]:
    """Every edge as ``(source, target, conditional)`` - what the topology test and the docs compare against."""
    edges: list[tuple[str, str, bool]] = [(START, LOAD_INPUTS, False), (FINALIZE, END, False)]
    edges += [(a, b, False) for a, b in STATIC_EDGES]
    for source, (_, targets) in ROUTERS.items():
        edges += [(source, target, True) for target in targets]
    return edges


def build_graph(checkpointer: Any = None) -> Any:
    """Create and compile the builder graph.

    Args:
        checkpointer: Optional LangGraph checkpointer (the builder does not need one; the state is plain JSON, so a
            checkpointer works if resuming is ever wanted).

    Returns:
        The compiled graph (``ainvoke`` / ``astream`` take the state and ``context=BuilderContext``).
    """
    graph = StateGraph(BuilderState, context_schema=BuilderContext)
    for name, run in NODES.items():
        graph.add_node(name, run)
    graph.add_edge(START, LOAD_INPUTS)
    for source, (router, targets) in ROUTERS.items():
        graph.add_conditional_edges(source, router, list(targets))
    for source, target in STATIC_EDGES:
        graph.add_edge(source, target)
    graph.add_edge(FINALIZE, END)
    return graph.compile(checkpointer=checkpointer)


__all__ = ["RECURSION_LIMIT", "build_graph", "edge_list"]
