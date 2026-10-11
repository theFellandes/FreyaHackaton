"""Node ``finalize``: write the closing files, the trace summary and the one-line result of the build.

:func:`finalize_run` does the work and is also called by ``build.py`` when the graph itself cannot finish (the wall-clock
guard fired or something outside the nodes failed), so the trace summary is written exactly once on every path
(``ctx.finalized``). It

* decides the final ``status``: ``deployed`` (an agent exists on the platform, its newest version is published and
  its platform export was written), ``deployed_unpublished`` (it exists but the newest version could not be confirmed
  as published - exit code 2), ``deployed_unexported`` (it is live but no verified platform export of it exists -
  exit code 2, the exported file is part of the submission), ``dry_run`` (a lint-clean, guard-clean workflow was written locally), ``blocked`` (a guard stopped the build) or
  ``failed``;
* writes ``out/<slot>.workflow.json`` (the definition that was sent) when the workflow passed the checks - otherwise
  the rejected definition goes to ``out/<slot>.rejected.workflow.json`` for diagnosis - plus ``out/<slot>.ledger.json``
  and ``out/<slot>.ir.json`` so the understanding step can be inspected;
* writes the hackathon trace summary (:meth:`builder.trace.TraceWriter.summary`: start, end, model calls, Freya
  objects, tests and results, track, slot). The tests are those of the state merged with the ones the trace recorded
  itself (plus tests that started but never finished, marked ``aborted``), so a build cut off by the wall-clock guard
  still lists every test that ran;
* writes ``results/<slot>-<run id>.build.json`` with the complete outcome (errors, notes, tests).

The returned ``final`` dict is what ``build.py`` prints as the last line: ``slot``, ``track``, ``workflow_id``,
``agent_id``, ``version``, ``exported``, ``trace``, ``status`` (plus ``workflow_file``, ``tests`` and ``errors``).
``exported`` is the platform export of the deployed version, never the locally sent definition: when the deployed
workflow has no usable export it is ``None``, ``export_ok`` is ``False`` and ``errors`` says so. Only a dry run (no
platform at all) reports its local workflow file there. ``cut_off`` is ``True`` when the wall-clock guard ended the
build, ``untested_redeploy`` when a repair round re-deployed a version that no self-test ran on (``tested_version`` then
names the last version a test did run against; ``version`` and ``exported`` stay those of what is live), and
``redeploy_skipped`` when a repair round was *not* deployed because a deploy plus a smoke test no longer fitted into the
budget - the tested version stays live and exported.

``policy_issues`` (CRITICAL-GAPS gap 3) lists every issue the last policy gate let the build go on with
(``state["policy_unresolved"]``: validator errors left after the repairs, warnings, best-effort guard findings), each
as ``code``, ``severity``, ``message``, ``rule_ids`` and ``path``. The build is not stopped for them (no agent is a
certain zero), but it never looks clean either: unresolved errors add a failed ``policy_validation`` test, the notes
get a one-line summary, the trace a ``WARN`` line per issue and the FINAL block a count. Not reported on the agentic
track, whose workflow does not come from the IR.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any

from langgraph.runtime import Runtime

from builder import paths
from builder.graph.context import BuilderContext
from builder.graph.nodes._common import run_node, workflow_file, workflow_of, write_json, write_workflow_file
from builder.graph.state import BuilderState

logger = logging.getLogger(__name__)

NAME = "finalize"

#: Checks the state keeps only the latest result of (they re-run after every repair); the trace's earlier runs of
#: them are not added to the summary again.
LATEST_ONLY_TESTS: tuple[str, ...] = ("static_checks", "remote_validate")

#: Statuses with which ``build.py`` exits 0.
SUCCESS_STATUSES = frozenset({"deployed", "dry_run"})


def decide_status(state: Mapping[str, Any]) -> str:
    """The final status of a run (see the module docstring)."""
    deploy = state.get("deploy") or {}
    if deploy.get("workflow_id"):
        # ``published`` is False only when the platform did not confirm it; a record without the key is hand-built
        if deploy.get("published") is False:
            return "deployed_unpublished"
        # the deploy node sets ``exported`` to None when the export failed; a state without the key is unknown
        if "exported" in state and not state.get("exported"):
            return "deployed_unexported"
        return "deployed"
    if state.get("blocked") or state.get("status") == "blocked":
        return "blocked"
    lint = state.get("lint") or {}
    if (
        (state.get("dry_run") or (state.get("limits") or {}).get("dry_run"))
        and lint.get("ok")
        and workflow_of(dict(state))
    ):
        return "dry_run"
    return "failed"


#: Longest message kept per reported policy issue.
MAX_POLICY_ISSUE_CHARS = 400


def unresolved_policy_issues(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The issues the last policy gate let the build go on with (see the module docstring), deduplicated."""
    if state.get("track") == "agentic":
        return []
    issues: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for raw in state.get("policy_unresolved") or []:
        if not isinstance(raw, Mapping):
            continue
        issue = {
            "code": str(raw.get("code") or "UNKNOWN"),
            "severity": "warning" if raw.get("severity") == "warning" else "error",
            "message": str(raw.get("message") or "")[:MAX_POLICY_ISSUE_CHARS],
            "rule_ids": [str(rule_id) for rule_id in raw.get("rule_ids") or []],
            "path": str(raw.get("path") or ""),
        }
        key = (issue["code"], issue["path"], issue["message"])
        if key not in seen:
            seen.add(key)
            issues.append(issue)
    return issues


def policy_issue_summary(issues: list[dict[str, Any]], repairs: int = 0) -> str:
    """One line: how many unresolved policy errors and warnings there are, and their codes (``""`` for none)."""
    if not issues:
        return ""
    errors = sum(1 for issue in issues if issue["severity"] == "error")
    codes = ", ".join(dict.fromkeys(issue["code"] for issue in issues))
    return (
        f"{errors} error(s), {len(issues) - errors} warning(s) unresolved after {repairs} policy repair(s) "
        f"({codes})"
    )


def collect_tests(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The tests of the run for the trace summary: policy acceptance, static checks, platform validation, self-tests.

    ``policy_validation`` is listed (as failed) only when policy errors are left unresolved: a build that continued
    best effort must not read as "all checks passed".
    """
    tests: list[dict[str, Any]] = []
    unresolved = unresolved_policy_issues(state)
    if any(issue["severity"] == "error" for issue in unresolved):
        tests.append(
            {
                "name": "policy_validation",
                "passed": False,
                "summary": policy_issue_summary(unresolved, int(state.get("policy_repairs") or 0)),
            }
        )
    lint = state.get("lint") or {}
    if lint:
        tests.append(
            {"name": "static_checks", "passed": bool(lint.get("ok")), "summary": lint.get("summary", "")}
        )
    remote = state.get("remote_validation") or {}
    if remote.get("kind") in ("ok", "deployed", "invalid"):
        tests.append(
            {
                "name": "remote_validate",
                "passed": bool(remote.get("ok")),
                "summary": "; ".join(remote.get("problems") or []) or "the platform accepted the workflow",
            }
        )
    for test in state.get("self_tests") or []:
        if (test.get("details") or {}).get("skipped"):
            continue
        suffix = f"#{test['round'] + 1}" if test.get("round") else ""
        tests.append(
            {
                "name": f"{test['name']}{suffix}",
                "passed": bool(test["passed"]),
                "summary": test.get("summary", ""),
            }
        )
    return tests


def finalize_run(
    state: Mapping[str, Any], ctx: BuilderContext, *, cut_off: str | None = None
) -> dict[str, Any]:
    """Write the closing files and the trace summary (once) and return the ``final`` summary dict.

    Args:
        state: The last known state (the graph's, or a partial one built from ``ctx.progress``).
        ctx: The run context; ``ctx.finalized`` makes the call idempotent.
        cut_off: Set by ``build.py`` when the wall-clock guard ended the build: a sentence saying so (and which node
            was interrupted). It shows in the final JSON (``cut_off: true``), the trace summary and ``errors``.
    """
    status = decide_status(state)
    slot = str(state.get("slot") or ctx.trace.slot)
    deploy = state.get("deploy") or {}
    workflow = workflow_of(dict(state))
    local: str | None = state.get("workflow_path") or None
    if workflow is not None and not local:
        try:
            passed_checks = bool((state.get("lint") or {}).get("ok")) and not state.get("blocked")
            suffix = ".workflow.json" if passed_checks else ".rejected.workflow.json"
            local = str(write_workflow_file(slot, workflow, suffix))
        except OSError as exc:
            logger.warning("could not write the workflow file: %s", exc)
    for key, suffix in (("ledger", ".ledger.json"), ("ir", ".ir.json")):
        if state.get(key):
            try:
                write_json(workflow_file(slot, suffix), state[key])
            except OSError as exc:
                logger.warning("could not write %s: %s", suffix, exc)
    tests = collect_tests(state)
    # the trace knows tests the state does not (a cut-off build) and those that were started but never finished
    merge = getattr(ctx.trace, "merged_tests", None)
    if callable(merge):
        tests = merge(tests, latest_only=LATEST_ONLY_TESTS)
    track = state.get("track") or ctx.progress.get("track") or ""
    policy_issues = unresolved_policy_issues({**dict(state), "track": track})
    policy_line = policy_issue_summary(policy_issues, int(state.get("policy_repairs") or 0))
    notes = list(state.get("notes") or [])
    if policy_line:
        notes.insert(0, f"policy: {policy_line}; the build went on best effort (see policy_issues)")
    errors = list(state.get("errors") or [])
    exported: str | None
    if deploy.get("workflow_id"):
        exported = state.get("exported") or None  # the platform export of the deployed version, or nothing
        if not exported and not any("export" in str(error) for error in errors):
            errors.append("export: the deployed workflow has no platform export file")
    elif status == "dry_run":
        exported = local  # there is no platform in a dry run: the local file is all there is
    else:
        exported = state.get("exported") or None
    export_ok: bool | None = bool(exported) if deploy.get("workflow_id") else None
    if cut_off:
        errors.append(cut_off)
    final: dict[str, Any] = {
        "slot": slot,
        "track": track,
        "workflow_id": deploy.get("workflow_id"),
        "agent_id": deploy.get("agent_id"),
        "version": deploy.get("version"),
        "exported": exported,
        "export_ok": export_ok,
        "trace": str(ctx.trace.log_path) if getattr(ctx.trace, "log_path", None) else "",
        "status": status,
        "workflow_file": local,
        "tests": {"passed": sum(1 for t in tests if t["passed"]), "total": len(tests)},
        "errors": errors[:10],
        "policy_issues": policy_issues,
    }
    if cut_off:
        final["cut_off"] = True
    if state.get("untested_redeploy"):
        final["untested_redeploy"] = True
        if state.get("tested_version") is not None:
            final["tested_version"] = state["tested_version"]  # the last version a self-test really ran against
    if state.get("redeploy_skipped"):
        final["redeploy_skipped"] = True
    if ctx.finalized:
        return final
    ctx.finalized = True
    for issue in policy_issues:  # one WARN line per unresolved policy issue
        try:
            ctx.trace.event(
                "warn",
                f"unresolved policy {issue['severity']} {issue['code']}: {issue['message']}",
                code=issue["code"],
                severity=issue["severity"],
                rule_ids=issue["rule_ids"] or None,
                path=issue["path"] or None,
            )
        except Exception:  # the trace must never take the run down
            logger.exception("could not write a policy issue to the trace")
    try:
        summary: dict[str, Any] = {
            "track": track or None,
            "status": status,
            "slot": slot,
            "workflow_id": deploy.get("workflow_id"),
            "agent_id": deploy.get("agent_id"),
            "version": deploy.get("version"),
            "node_count": deploy.get("node_count"),
            "tool_count": deploy.get("tool_count"),
            "exported": final["exported"],
            "tests": tests,
            "profile": state.get("profile"),
            "coverage": (state.get("coverage") or {}).get("summary"),
            "notes": notes[:20],
            "errors_list": errors[:10],
        }
        # shown only when they say something (the block prints every extra key)
        for key, value in (
            ("export_ok", export_ok),
            ("cut_off", cut_off),
            ("untested_redeploy", True if state.get("untested_redeploy") else None),
            ("tested_version", state.get("tested_version") if state.get("untested_redeploy") else None),
            ("redeploy_skipped", True if state.get("redeploy_skipped") else None),
            ("policy_issues", policy_line or None),
        ):
            if value is not None:
                summary[key] = value
        ctx.trace.summary(summary)
    except Exception:  # the summary must never take the run down
        logger.exception("could not write the trace summary")
    try:
        write_json(
            paths.RESULTS_DIR / f"{slot}-{state.get('run_id', ctx.trace.run_id)}.build.json",
            _without_secrets(
                ctx,
                {**final, "notes": notes, "tests_detail": state.get("self_tests") or []},
            ),
        )
    except OSError as exc:
        logger.warning("could not write the build result file: %s", exc)
    return final


def _without_secrets(ctx: BuilderContext, value: Any) -> Any:
    """``value`` with every known secret and key-shaped token masked in its strings (audit A11: an error text can
    quote a request). Uses the trace's secrets plus the run's own; paths are left as they are."""
    from builder.trace import _Redactor

    own = _Redactor(ctx.secrets)
    by_trace = getattr(ctx.trace, "redact_secrets", None)

    def clean(item: Any) -> Any:
        if isinstance(item, str):
            text = own.text(item, shorten_paths=False)
            return by_trace(text) if callable(by_trace) else text
        if isinstance(item, Mapping):
            return {clean(key) if isinstance(key, str) else key: clean(entry) for key, entry in item.items()}
        if isinstance(item, list | tuple):
            return [clean(entry) for entry in item]
        return item

    try:
        return clean(value)
    except Exception:  # redaction must never cost the result file; the key-shaped patterns alone still apply
        logger.debug("could not redact the result file", exc_info=True)
        return json.loads(_Redactor().text(json.dumps(value, ensure_ascii=False, default=str), shorten_paths=False))


async def _body(state: BuilderState, ctx: BuilderContext) -> dict[str, Any]:
    final = finalize_run(state, ctx)
    return {"final": final, "status": final["status"]}


async def run(state: BuilderState, runtime: Runtime[BuilderContext]) -> dict[str, Any]:
    """LangGraph entry point of the node."""
    return await run_node(NAME, state, runtime.context, _body)
