"""The state that flows through the builder graph.

LangGraph keeps one :class:`BuilderState` per run. Every node reads what it needs and returns a *partial* update; the
keys annotated with ``add`` are accumulated (their updates are appended), every other key is overwritten. All values
are JSON-serialisable (Pydantic objects are stored as ``model_dump(mode="json")``), so a state can be written to a
trace, a checkpoint or a result file without special handling.

Key groups
----------
Identity and inputs
    ``run_id``, ``slot``, ``started_at``, ``inputs`` (``{"briefs": [...], "tool_api": "..."}``), ``dry_run``,
    ``limits`` (caps copied from the settings so the routing functions stay pure).
Understanding
    ``bundle`` (loaded briefs), ``tool_api`` (parsed contracts), ``ledger`` (rule ledger), ``ir`` (policy IR),
    ``policy_issues`` (validator / guard / self-test findings for the repair step), ``policy_unresolved`` (the issues
    the last policy gate let the run go on with: validator errors and warnings, guard findings - reported by
    ``finalize``), ``policy_repairs``, ``prose``.
Compiling
    ``profile``, ``compiled`` (``{"workflow", "rule_map", "profile", "notes"}``), ``coverage``, ``lint``,
    ``compile_repairs``, ``compile_repair_log``, ``compile_feedback``.
Platform
    ``remote_validation``, ``deploy`` (``DeployInfo`` dump), ``workflow_path`` (local file), ``exported``
    (the platform's export of the deployed version, or ``None`` when that export failed or did not match),
    ``self_tests``, ``selftest_repairs``, ``repair_requested``, ``untested_redeploy`` (a repair round re-deployed the
    workflow but no self-test ran on the new version), ``tested_version`` (the version number the last self-test that
    really ran was run against), ``redeploy_skipped`` (a repair round was not deployed because a deploy plus a smoke
    test no longer fitted into the budget: the tested version stays live, reported and exported).
Control
    ``track`` (``compiled`` or ``agentic``), ``agentic_attempts``, ``failed`` (name of the node that could not do its
    work, empty otherwise), ``blocked`` (a guard stopped the build), ``status``, ``budget_remaining_s`` (recorded by
    every node so :mod:`builder.graph.routing` can decide without a clock), ``errors`` / ``notes`` /
    ``guard_reports`` (append-only logs) and ``final`` (the summary written by the ``finalize`` node).
"""

from __future__ import annotations

from operator import add
from typing import Annotated, Any, Literal, TypedDict

#: The two ways a workflow can be produced.
TrackName = Literal["compiled", "agentic"]

#: How a build ended (``status`` after the ``finalize`` node). ``deployed_unpublished``: a workflow exists on the
#: platform but its newest version could not be confirmed as published (calls and simulations would run an older one);
#: ``deployed_unexported``: it is live but no verified platform export of it exists (the export file is part of the
#: submission).
FinalStatus = Literal[
    "deployed", "deployed_unpublished", "deployed_unexported", "dry_run", "blocked", "failed"
]


class BuilderState(TypedDict, total=False):
    """State of one build run (see the module docstring for the key groups)."""

    # identity and inputs
    run_id: str
    slot: str
    started_at: str
    inputs: dict[str, Any]
    dry_run: bool
    limits: dict[str, Any]

    # understanding
    bundle: dict[str, Any]
    tool_api: dict[str, Any]
    service_mode: bool
    service_plan: dict[str, Any]
    service_attempts: int
    ledger: dict[str, Any]
    ir: dict[str, Any]
    policy_issues: list[dict[str, Any]]
    policy_unresolved: list[dict[str, Any]]
    policy_repairs: int
    prose: dict[str, Any]

    # compiling
    profile: str
    compiled: dict[str, Any]
    coverage: dict[str, Any]
    lint: dict[str, Any]
    compile_repairs: int
    compile_repair_log: list[str]
    compile_feedback: str

    # platform
    remote_validation: dict[str, Any]
    deploy: dict[str, Any]
    workflow_path: str
    exported: str | None
    untested_redeploy: bool
    tested_version: int | None
    redeploy_skipped: bool
    self_tests: Annotated[list[dict[str, Any]], add]
    selftest_repairs: int
    repair_requested: bool

    # control
    track: TrackName
    agentic_attempts: int
    failed: str
    blocked: bool
    status: str
    budget_remaining_s: float
    errors: Annotated[list[str], add]
    notes: Annotated[list[str], add]
    guard_reports: Annotated[list[dict[str, Any]], add]
    final: dict[str, Any]


#: Keys that hold large Pydantic dumps; never copied into traces.
HEAVY_KEYS: frozenset[str] = frozenset({"bundle", "tool_api", "ledger", "ir", "prose", "compiled", "service_plan"})


def initial_state(
    *, run_id: str, slot: str, started_at: str, inputs: dict[str, Any], dry_run: bool = False
) -> BuilderState:
    """The state a run starts with (``build.py`` passes it to ``graph.ainvoke``)."""
    return BuilderState(
        run_id=run_id,
        slot=slot,
        started_at=started_at,
        inputs=inputs,
        dry_run=dry_run,
        policy_repairs=0,
        compile_repairs=0,
        selftest_repairs=0,
        agentic_attempts=0,
        failed="",
        blocked=False,
        errors=[],
        notes=[],
        self_tests=[],
        guard_reports=[],
    )


__all__ = ["HEAVY_KEYS", "BuilderState", "FinalStatus", "TrackName", "initial_state"]
