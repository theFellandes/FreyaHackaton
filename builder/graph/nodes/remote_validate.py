"""Node ``remote_validate``: dry-run the workflow on the platform (``POST /api/v2/workflow?dryRun=true``, nothing stored).

The platform knows rules no offline lint can: it is the authority on whether a definition is accepted. Result kinds in
``state["remote_validation"]``:

* ``ok`` - accepted, go on to ``deploy``;
* ``invalid`` - the platform rejected the content; its messages become ``compile_feedback`` and the run goes to
  ``repair_compile`` (or the agentic fallback);
* ``infra`` - authentication, network or server trouble: not a defect of the workflow, so the run still tries to deploy
  (the save call reports the same trouble clearly if it persists);
* ``skipped`` - no Freya client (dry run).
"""

from __future__ import annotations

from typing import Any

from langgraph.runtime import Runtime

from builder.freya.background import run_blocking
from builder.freya.client import FreyaError, FreyaValidationError
from builder.graph.context import BuilderContext
from builder.graph.nodes._common import failure, run_node, short, workflow_of
from builder.graph.state import BuilderState

NAME = "remote_validate"


async def _body(state: BuilderState, ctx: BuilderContext) -> dict[str, Any]:
    workflow = workflow_of(state)
    if workflow is None:
        return failure(NAME, "there is no workflow to validate")
    if ctx.freya is None:
        return {"remote_validation": {"ok": True, "kind": "skipped", "problems": []}, "_skipped": True}
    try:
        await run_blocking(ctx.freya.validate, state["slot"], workflow, token=ctx.cancel)
    except FreyaValidationError as exc:
        problems = exc.problems or [short(exc)]
        ctx.trace.test("remote_validate", False, f"platform rejected the workflow: {short(problems[0], 160)}")
        return {
            "remote_validation": {
                "ok": False,
                "kind": "invalid",
                "problems": [short(p, 300) for p in problems[:10]],
            },
            "compile_feedback": "\n".join(short(p, 300) for p in problems[:10]),
        }
    except FreyaError as exc:
        ctx.trace.error("the platform validation call failed; will still try to deploy", error=short(exc))
        return {
            "remote_validation": {"ok": False, "kind": "infra", "problems": [short(exc)]},
            "notes": [f"{NAME}: platform validation unavailable ({short(exc, 120)})"],
        }
    ctx.trace.test("remote_validate", True, "the platform accepted the workflow (dry run)")
    return {"remote_validation": {"ok": True, "kind": "ok", "problems": []}}


async def run(state: BuilderState, runtime: Runtime[BuilderContext]) -> dict[str, Any]:
    """LangGraph entry point of the node."""
    return await run_node(NAME, state, runtime.context, _body)
