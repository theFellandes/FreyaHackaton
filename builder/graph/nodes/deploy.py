"""Node ``deploy``: save the workflow on the platform (create the first time, update the same slot afterwards) and export it.

Steps: write ``out/<slot>.workflow.json`` (what is about to be sent), :meth:`FreyaClient.save` (creates or updates the
slot's workflow, makes sure the newest version is published), confirm the publication (retried: a version that is not
published is not deployed - simulations and calls run the published version only), read the stored definition back and
report fields the platform silently stripped (:func:`builder.freya.diff.stripped_fields`), and export the stored workflow
to ``out/<slot>.exported.json`` (the exported workflow file of the submission).

The export is checked before it replaces anything: it is written to a temporary file, must parse, name the deployed
workflow and hold exactly the definition that was just read back, and is retried a few times (reads are idempotent).
If no usable export comes out of that, ``exported`` is set to ``None`` explicitly and the stale file of an earlier
round is removed - the hackathon rules want the exported file to be the platform's export of the *same* version that
was published and tested, so a missing export is reported (``errors``, ``export_ok: false`` in the final summary) and
never papered over with the locally sent definition.

A rejection of the *content* (HTTP 400/409/422 or :class:`~builder.freya.client.FreyaValidationError`) is reported as
``remote_validation = {"ok": False, "kind": "invalid"}`` so the routing can repair the compile; any other platform failure
marks the node as failed. A failed update keeps the ``deploy`` info of the earlier, still published version - also when
the new version cannot be confirmed as published.

Every platform call runs in an abandonable daemon thread (:func:`builder.freya.background.run_blocking`) bound to
``ctx.cancel``: if the wall-clock guard fires, the node returns at once and the thread starts no further request.

A repair round replaces a version that was already deployed *and tested*. If the budget no longer covers a deploy plus a
smoke test (:data:`REDEPLOY_STAGES`), the round is not deployed at all (``redeploy_skipped``): publishing a version that
nothing can test would leave the platform serving something other than what the trace, the self-tests and the export
describe. The earlier version stays live, reported and exported, and the run ends.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
from pathlib import Path
from typing import Any

from langgraph.runtime import Runtime

from builder.budget import DEFAULT_RESERVE_S, estimate
from builder.freya.background import PlatformCancelled, run_blocking
from builder.freya.client import DeployInfo, FreyaError, FreyaValidationError
from builder.freya.diff import stripped_fields
from builder.graph.context import BuilderContext
from builder.graph.nodes._common import (
    MAX_ERROR_CHARS,
    failure,
    run_node,
    short,
    workflow_file,
    workflow_of,
    write_workflow_file,
)
from builder.graph.state import BuilderState

NAME = "deploy"

#: HTTP statuses that mean "the platform does not accept this definition".
CONTENT_STATUSES = frozenset({400, 409, 422})

#: Extra tries to confirm the publication after ``save`` reported it unconfirmed, and the pause before each (x attempt).
PUBLISH_RETRIES = 2
PUBLISH_RETRY_DELAY_S = 2.0
#: Export rounds (each round already has the client's own retries) and the pause before each retry (x round).
EXPORT_ATTEMPTS = 3
EXPORT_RETRY_DELAY_S = 2.0
#: No retry rounds are started when less than this many seconds of the budget are left.
RETRY_MIN_BUDGET_S = 30.0
#: Stages that must still fit (with the usual reserve) for a repair round to be deployed: the deploy and one test of it.
REDEPLOY_STAGES: tuple[str, ...] = ("deploy", "smoke")


#: A create refused for capacity names a limit and what is counted (both must appear; AUD-56). Only a *create* can hit
#: the workspace's workflow cap, so an update with such wording stays a content rejection.
_CAPACITY_RE = re.compile(r"\b(limit|quota|maximum|capacity|too many|cap)\b", re.IGNORECASE)
_COUNTED_RE = re.compile(r"\b(workflows|agents)\b|number of (workflows|agents)", re.IGNORECASE)


def is_capacity_rejection(exc: FreyaError) -> bool:
    """True when a create was refused because the workspace holds no more workflows (not a problem of the content)."""
    body = exc.body or ""
    return (
        exc.kind == "http"
        and exc.action == "create"
        and exc.status in {400, 403, 409, 422, 429}
        and bool(_CAPACITY_RE.search(body))
        and bool(_COUNTED_RE.search(body))
    )


def is_content_rejection(exc: FreyaError) -> bool:
    """True when the platform rejected the workflow's content (as opposed to auth / network / server trouble)."""
    return isinstance(exc, FreyaValidationError) or (exc.kind == "http" and exc.status in CONTENT_STATUSES)


def _unlink(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.unlink(missing_ok=True)


def check_export(path: Path, workflow_id: str, stored: dict[str, Any] | None) -> str:
    """Why the export file at ``path`` cannot be trusted, or ``""`` when it is the deployed workflow's export.

    The kit writes ``{"workflow_id", "agent_id", "updated_at", "workflow"}``. It must parse, belong to ``workflow_id``
    and - when the definition was read back after the save - hold exactly that definition (an older or newer version
    would differ).
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return f"the export file is unreadable ({type(exc).__name__})"
    if not isinstance(data, dict) or not isinstance(data.get("workflow"), dict):
        return "the export file holds no workflow definition"
    if str(data.get("workflow_id")) != workflow_id:
        return f"the export is for workflow {data.get('workflow_id')}, not {workflow_id}"
    if stored is not None and data["workflow"] != stored:
        return "the export does not match the stored workflow that was just read back (another version?)"
    return ""


async def _confirm_publication(ctx: BuilderContext, info: DeployInfo) -> DeployInfo:
    """Retry the publication check while ``info.published`` is not ``True`` (the confirming read can lag)."""
    ensure = getattr(ctx.freya, "ensure_published", None)
    attempts = 0
    while (
        info.published is not True
        and callable(ensure)
        and attempts < PUBLISH_RETRIES
        and ctx.budget.remaining() > RETRY_MIN_BUDGET_S
    ):
        attempts += 1
        await asyncio.sleep(PUBLISH_RETRY_DELAY_S * attempts)
        try:
            number, confirmed = await run_blocking(ensure, info.workflow_id, token=ctx.cancel)
        except PlatformCancelled:
            raise
        except FreyaError as exc:
            ctx.trace.event(
                "freya", f"publish retry {attempts} failed: {short(exc)}", workflow_id=info.workflow_id
            )
            continue
        info = info.model_copy(
            update={"version": number if number is not None else info.version, "published": bool(confirmed)}
        )
        ctx.trace.event(
            "freya",
            f"publish retry {attempts}: " + ("confirmed" if confirmed else "still not confirmed"),
            workflow_id=info.workflow_id,
            version=info.version,
        )
    return info


def _unpublished(
    state: BuilderState, ctx: BuilderContext, info: DeployInfo, deploy_info: dict[str, Any], local: Path
) -> dict[str, Any]:
    """The update for a version that is stored but not confirmed published: a failed deploy, no self-tests."""
    message = (
        f"version {info.version} of workflow {info.workflow_id} could not be confirmed as published "
        "(calls and simulations would run an older version)"
    )
    ctx.trace.test("deploy", False, message)
    ctx.trace.error("deploy: " + message, workflow_id=info.workflow_id, version=info.version)
    update = failure(NAME, message, workflow_path=str(local))
    previous = state.get("deploy") or {}
    if not (previous.get("workflow_id") == info.workflow_id and previous.get("published") is True):
        update["deploy"] = deploy_info  # nothing published to fall back on: report the ids (published False)
    return update


async def _reference(
    ctx: BuilderContext, workflow_id: str, fallback: dict[str, Any] | None
) -> dict[str, Any] | None:
    """Read the stored definition again (best effort): the reference an export is compared with."""
    try:
        return await run_blocking(ctx.freya.get_workflow, workflow_id, token=ctx.cancel)
    except PlatformCancelled:
        raise
    except Exception as exc:
        ctx.trace.event("freya", f"could not read the stored workflow back: {short(exc)}")
        return fallback


async def _export(
    ctx: BuilderContext, slot: str, info: DeployInfo, stored: dict[str, Any] | None
) -> tuple[Path | None, str, bool]:
    """Export the deployed workflow, verify the file and move it into place.

    Returns:
        ``(path, "", cross_checked)`` on success - ``cross_checked`` is ``False`` when no read-back of the stored
        definition was available, so the file could only be checked for being the deployed workflow's - or
        ``(None, reason, False)`` when no usable export came out.
    """
    target = workflow_file(slot, ".exported.json")
    part = workflow_file(slot, ".exported.json.part")
    problem = "no export was attempted"
    for attempt in range(1, EXPORT_ATTEMPTS + 1):
        _unlink(part)
        try:
            if stored is None or (attempt > 1 and "does not match" in problem):
                # the earlier read-back failed, or one of the two reads lagged behind: take a fresh reference
                stored = await _reference(ctx, info.workflow_id, stored)
            await run_blocking(ctx.freya.export, info.workflow_id, part, token=ctx.cancel)
            problem = check_export(part, info.workflow_id, stored)
        except PlatformCancelled:
            _unlink(part)
            raise
        except Exception as exc:  # FreyaError, OSError ...: try again, then give up loudly
            problem = short(exc, 200)
        if not problem:
            try:
                os.replace(part, target)  # atomic: the target is never half written
            except OSError as exc:
                problem = f"could not move the export into place ({type(exc).__name__}: {exc})"
            else:
                return target, "", stored is not None
        ctx.trace.event(
            "freya",
            f"export attempt {attempt}/{EXPORT_ATTEMPTS} unusable: {problem}",
            workflow_id=info.workflow_id,
        )
        if attempt < EXPORT_ATTEMPTS and ctx.budget.remaining() > RETRY_MIN_BUDGET_S:
            await asyncio.sleep(EXPORT_RETRY_DELAY_S * attempt)
        else:
            break
    _unlink(part)
    _unlink(target)  # an export of an older version must not stay behind as if it were the current one
    return None, problem, False


def _redeploy_skipped(state: BuilderState, ctx: BuilderContext) -> dict[str, Any] | None:
    """The update for a repair round that cannot be tested in the time left, or ``None`` when it may be deployed."""
    round_no = int(state.get("selftest_repairs", 0))
    live = state.get("deploy") or {}
    if round_no <= 0 or not live.get("workflow_id") or live.get("published") is not True:
        return None  # the first deployment is never skipped; there is no tested version to protect
    needed = sum(estimate(stage) for stage in REDEPLOY_STAGES)
    if ctx.budget.allows(needed, DEFAULT_RESERVE_S):
        return None
    message = (
        f"repair round {round_no} was not deployed: {ctx.budget.remaining():.0f} s left cannot cover a deploy and a "
        f"test of it; the earlier tested version {live.get('version')} stays live"
    )
    ctx.trace.decision(f"{NAME}: {message}")
    return {"redeploy_skipped": True, "notes": [f"{NAME}: {message}"]}


def _inputs_sha(state: BuilderState) -> str | None:
    """The inputs' fingerprint recorded with the slot (:func:`builder.inputs.input_fingerprint`), when known."""
    inputs = state.get("inputs") or {}
    value = inputs.get("inputs_sha") if isinstance(inputs, dict) else None
    return str(value) if value else None


async def _body(state: BuilderState, ctx: BuilderContext) -> dict[str, Any]:
    workflow = workflow_of(state)
    if workflow is None:
        return failure(NAME, "there is no workflow to deploy")
    if ctx.freya is None:
        return failure(NAME, "no Freya client (dry run)")
    skipped = _redeploy_skipped(state, ctx)
    if skipped is not None:
        return skipped
    slot = state["slot"]
    local = write_workflow_file(slot, workflow)
    try:
        info = await run_blocking(
            ctx.freya.save, slot, workflow, inputs_sha=_inputs_sha(state), token=ctx.cancel
        )
    except FreyaError as exc:
        if is_capacity_rejection(exc):  # no repair can make room in the workspace: say so and stop
            ctx.trace.error("the workspace cannot hold another workflow", error=short(exc))
            return failure(
                NAME,
                f"workspace workflow limit reached on create (not a workflow problem): {short(exc)}",
                workflow_path=str(local),
            )
        if is_content_rejection(exc):
            problems = getattr(exc, "problems", None) or [short(exc)]
            ctx.trace.test(
                "deploy", False, f"platform rejected the workflow on save: {short(problems[0], 160)}"
            )
            return {
                "workflow_path": str(local),
                "remote_validation": {
                    "ok": False,
                    "kind": "invalid",
                    "problems": [short(p, 300) for p in problems[:10]],
                },
                "compile_feedback": "\n".join(short(p, 300) for p in problems[:10]),
            }
        ctx.trace.error("deploy failed", error=short(exc))
        return failure(NAME, short(exc), workflow_path=str(local))

    info = await _confirm_publication(ctx, info)
    deploy_info = {**info.model_dump(mode="json"), "track": state.get("track", "compiled")}
    if info.published is not True:
        return _unpublished(state, ctx, info, deploy_info, local)

    update: dict[str, Any] = {
        "workflow_path": str(local),
        "deploy": deploy_info,
        "remote_validation": {"ok": True, "kind": "deployed", "problems": []},
        "notes": [],
    }
    stored: dict[str, Any] | None = None
    try:
        stored = await run_blocking(ctx.freya.get_workflow, info.workflow_id, token=ctx.cancel)
        stripped = stripped_fields(workflow, stored)
        if stripped:
            ctx.trace.event("freya", f"the platform stripped {len(stripped)} field(s)", paths=stripped[:20])
            update["notes"].append(f"{NAME}: platform stripped {len(stripped)} field(s), e.g. {stripped[0]}")
    except PlatformCancelled:
        raise
    except Exception as exc:  # read-back is a check, not a requirement
        stored = None
        ctx.trace.event("freya", f"could not read the stored workflow back: {short(exc)}")
    exported, problem, cross_checked = await _export(ctx, slot, info, stored)
    update["exported"] = str(exported) if exported is not None else None  # explicit: no earlier path survives
    if exported is not None and not cross_checked:
        ctx.trace.event(
            "freya", "the export could not be cross-checked against a read-back", workflow_id=info.workflow_id
        )
        update["notes"].append(
            f"{NAME}: the export names the deployed workflow but could not be compared with a read-back of it"
        )
    if exported is None:
        ctx.trace.error("could not export the stored workflow", error=problem)
        update["notes"].append(f"{NAME}: export failed ({short(problem, 120)})")
        update["errors"] = [
            f"{NAME}: no usable platform export of version {info.version}: {short(problem, 200)}"[
                :MAX_ERROR_CHARS
            ]
        ]
    return update


async def run(state: BuilderState, runtime: Runtime[BuilderContext]) -> dict[str, Any]:
    """LangGraph entry point of the node."""
    return await run_node(NAME, state, runtime.context, _body)
