"""The graph nodes, one module each. Every module exposes ``NAME`` and ``async def run(state, runtime) -> dict``.

``NODES`` maps node names to their ``run`` functions in the order of the happy path; ``build_graph`` registers them all.
The guard nodes (``guard_inputs``, ``guard_policy``, ``guard_workflow``) implement the guardrail flow between the working
stages; see ``docs/GRAPH.md`` for the topology and ``docs/GUARDRAILS.md`` for what each guard checks.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from builder.graph.nodes import (
    agentic_build,
    compile_workflow,
    decide,
    deploy,
    draft_prose,
    extract_policy,
    finalize,
    guard_inputs,
    guard_policy,
    guard_workflow,
    load_inputs,
    remote_validate,
    repair_compile,
    repair_policy,
    self_test,
    static_checks,
    understand_brief,
    validate_policy,
)

NodeFn = Callable[..., Awaitable[dict[str, Any]]]

_MODULES = (
    load_inputs,
    guard_inputs,
    understand_brief,
    extract_policy,
    validate_policy,
    repair_policy,
    guard_policy,
    draft_prose,
    compile_workflow,
    static_checks,
    repair_compile,
    guard_workflow,
    remote_validate,
    deploy,
    self_test,
    decide,
    agentic_build,
    finalize,
)

#: Node name -> LangGraph node function.
NODES: dict[str, NodeFn] = {module.NAME: module.run for module in _MODULES}

__all__ = ["NODES", "NodeFn"]
