"""Node ``repair_compile``: change how the workflow is compiled after a lint, guard or platform rejection.

The compiler is deterministic, so a rejected workflow is repaired by changing its *inputs*, using strategies in order,
each at most once per policy round (a model call would only guess at the JSON):

1. ``drop_prose`` - recompile with the template-only prompts (the drafted prose is the only free text that can
   introduce a bad variable, URL or word);
2. ``switch_profile`` - compile with the other agent profile (``staged`` <-> ``lean``).

The counters ``compile_repairs`` and ``compile_repair_log`` bound the loop; with no strategy left the node reports
``failed`` and the routing falls back. The feedback that triggered the repair (lint findings, guard violations or the
platform's messages) goes to the trace.
"""

from __future__ import annotations

from typing import Any

from langgraph.runtime import Runtime

from builder.graph.context import BuilderContext
from builder.graph.nodes._common import failure, run_node
from builder.graph.state import BuilderState

NAME = "repair_compile"

#: Strategies in the order they are tried.
STRATEGIES: tuple[str, ...] = ("drop_prose", "switch_profile")


def next_strategy(state: BuilderState) -> str | None:
    """The first strategy not yet used in this policy round that can change something (``None`` when exhausted)."""
    done = set(state.get("compile_repair_log") or [])
    prose = state.get("prose") or {}
    if "drop_prose" not in done and (prose.get("system_prompt_extra") or prose.get("per_node")):
        return "drop_prose"
    if "switch_profile" not in done:
        return "switch_profile"
    return None


async def _body(state: BuilderState, ctx: BuilderContext) -> dict[str, Any]:
    strategy = next_strategy(state)
    feedback = state.get("compile_feedback", "")
    if strategy is None:
        return failure(NAME, "no repair strategy left", compile_feedback=feedback)
    update: dict[str, Any] = {
        "compile_repairs": int(state.get("compile_repairs", 0)) + 1,
        "compile_repair_log": [*(state.get("compile_repair_log") or []), strategy],
        "blocked": False,
        "lint": {},
        "remote_validation": {},
    }
    if strategy == "drop_prose":
        update["prose"] = {}
    else:
        current = state.get("profile") or ctx.settings.agent_profile
        update["profile"] = "lean" if current == "staged" else "staged"
    ctx.trace.decision(
        f"compile repair round {update['compile_repairs']}: {strategy}"
        + (f" (now {update['profile']})" if "profile" in update else ""),
        feedback=feedback[:600],
    )
    return update


async def run(state: BuilderState, runtime: Runtime[BuilderContext]) -> dict[str, Any]:
    """LangGraph entry point of the node."""
    return await run_node(NAME, state, runtime.context, _body)
