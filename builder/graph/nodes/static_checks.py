"""Node ``static_checks``: lint the workflow offline, with the same rules for both tracks.

:func:`builder.checks.lint.lint_workflow` (rules L001-L029: no ``null``, tools only on conversation nodes, reachable
nodes, code that compiles and has no double braces, resolvable ``{{variables}}``, tool URLs and enums equal to the tool
API, the close call last ...). ``lint["ok"]`` is true when there is no error; warnings are reported but never block.

On the agentic track the declared call variables are unknown (there is no IR), so rule L014 (unresolvable variable) is
downgraded to a warning there; every other rule is enforced as on the compiled track.
"""

from __future__ import annotations

from typing import Any

from langgraph.runtime import Runtime

from builder.checks.lint import lint_workflow
from builder.graph.context import BuilderContext
from builder.graph.nodes._common import (
    failure,
    get_ir,
    get_tool_api,
    lint_call_variables,
    run_node,
    workflow_of,
)
from builder.graph.state import BuilderState

NAME = "static_checks"

#: Lint rules that cannot be judged without the IR and are therefore only warnings on the agentic track.
AGENTIC_SOFT_RULES = frozenset({"L014"})


async def _body(state: BuilderState, ctx: BuilderContext) -> dict[str, Any]:
    workflow = workflow_of(state)
    if workflow is None:
        return failure(NAME, "there is no workflow to check")
    agentic = state.get("track") == "agentic"
    report = lint_workflow(
        workflow, get_tool_api(state), lint_call_variables(None if agentic else get_ir(state))
    )
    errors = [f for f in report.errors if not (agentic and f.rule in AGENTIC_SOFT_RULES)]
    warnings = [*report.warnings, *(f for f in report.errors if agentic and f.rule in AGENTIC_SOFT_RULES)]
    lint = {
        "ok": not errors,
        "errors": [str(f) for f in errors],
        "warnings": [str(f) for f in warnings],
        "summary": f"{len(errors)} errors, {len(warnings)} warnings",
    }
    ctx.trace.event(
        "lint",
        f"static checks: {lint['summary']} ({'ok' if lint['ok'] else 'FAILED'})",
        errors=lint["errors"][:20],
        warnings=lint["warnings"][:10],
    )
    ctx.trace.test("static_checks", lint["ok"], lint["summary"])
    update: dict[str, Any] = {"lint": lint}
    if not lint["ok"]:
        update["compile_feedback"] = "\n".join(lint["errors"][:25])
    return update


async def run(state: BuilderState, runtime: Runtime[BuilderContext]) -> dict[str, Any]:
    """LangGraph entry point of the node."""
    return await run_node(NAME, state, runtime.context, _body)
