"""The builder's LangGraph: state, context, routing, nodes and the assembled graph (see ``docs/GRAPH.md``).

Importing this package pulls in LangGraph; the pure pieces (:mod:`builder.graph.state`, :mod:`builder.graph.routing`)
can be imported on their own.
"""

from __future__ import annotations

from builder.graph.build_graph import RECURSION_LIMIT, build_graph
from builder.graph.context import BuilderContext
from builder.graph.state import BuilderState, initial_state

__all__ = ["RECURSION_LIMIT", "BuilderContext", "BuilderState", "build_graph", "initial_state"]
