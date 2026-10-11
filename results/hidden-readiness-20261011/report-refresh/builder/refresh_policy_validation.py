"""Offline, read-only-source refresh of the policy-validation reporting gap.

Uses retained historical evidence when original synthetic build artifacts are absent.
Runs only pure routing helpers and finalize_run's already-finalized, no-write path.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[4]
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
for env_name in list(os.environ):
    if env_name.startswith(("FREYA_", "ANTHROPIC_", "OPENAI_", "CLAUDE_", "LANGFUSE_")):
        os.environ.pop(env_name, None)
os.environ["FREYA_BUILDER_ENV_FILE"] = str(HERE / "absent.env")

from builder.graph import routing
from builder.graph.nodes.finalize import collect_tests, finalize_run

HISTORICAL_PATH = ROOT / "results/hidden-readiness-20261011/builder/followup/saved-synthetic-ir-validation.json"
SOURCE_FILES = (
    "builder/understand/validate.py",
    "builder/graph/routing.py",
    "builder/graph/nodes/validate_policy.py",
    "builder/graph/nodes/guard_policy.py",
    "builder/graph/nodes/finalize.py",
    "builder/graph/nodes/_common.py",
    "builder/build.py",
    "tests/graph/test_routing.py",
)


def hashes() -> dict[str, str]:
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in SOURCE_FILES}


started = datetime.now(UTC).isoformat()
before = hashes()
historical = json.loads(HISTORICAL_PATH.read_text(encoding="utf-8"))
rows = []
for saved in historical["builds"]:
    old_hashes = saved["source_sha256"]
    original_paths = [name for name in old_hashes if not name.startswith("builder/")]
    availability = {name: (ROOT / name).is_file() for name in original_paths}
    issues = saved["issues"]
    state = {
        "policy_issues": issues,
        "policy_repairs": 2,
        "limits": {"allow_agentic": False},
        "budget_remaining_s": 1600,
    }
    cases = {}
    for name, changes in (
        ("no_agentic_two_repairs", {}),
        ("agentic_allowed_two_repairs", {"limits": {"allow_agentic": True}}),
        ("no_affordable_repair_or_agentic", {"policy_repairs": 0, "budget_remaining_s": 0, "limits": {"allow_agentic": True}}),
    ):
        probe = {**state, **changes}
        cases[name] = {
            "has_policy_errors": routing.has_policy_errors(probe),
            "can_repair_policy": routing.can_repair_policy(probe),
            "fallback_target": routing.fallback_target(probe),
            "next_node": routing.after_validate_policy(probe),
        }

    # Supplied lint/workflow represent an already-compiled state, not a new build.
    # finalized=True and workflow_path prevent file creation and trace mutation.
    final_state = {
        **state,
        "slot": "report-refresh-probe",
        "track": "compiled",
        "dry_run": True,
        "blocked": False,
        "errors": [],
        "compiled": {"workflow": {"nodes": [{"id": "constructed-helper-probe"}]}},
        "workflow_path": str(HERE / "NOT-CREATED.workflow.json"),
        "lint": {"ok": True, "summary": "supplied helper-probe lint; no compiler or lint execution"},
    }
    ctx = SimpleNamespace(
        trace=SimpleNamespace(slot="report-refresh-probe", run_id="helper", log_path=None),
        progress={},
        finalized=True,
    )
    final = finalize_run(final_state, ctx)
    rows.append({
        "slot": saved["slot"],
        "variant": saved["variant"],
        "historical_revalidated_utc": historical["checked_utc"],
        "original_artifact_availability": availability,
        "fresh_ir_revalidation_performed": False,
        "fresh_ir_revalidation_limitation": "Original final IR/ledger/build/trace artifacts are absent; exact issues are from the preserved 10:00:43 UTC revalidation, not a fresh IR run.",
        "source_unchanged_since_historical_revalidation": {
            name: before[name] == old_hashes[name]
            for name in SOURCE_FILES if name in old_hashes
        },
        "historical_saved_status": saved["saved_status"],
        "historical_saved_errors": saved["saved_errors"],
        "historical_saved_tests": saved["saved_tests"],
        "historical_error_count": saved["error_count"],
        "historical_warning_count": saved["warning_count"],
        "exact_historical_issues": issues,
        "historical_product_naming": saved["product_naming"],
        "historical_refuse_to_talk_intents": saved["refuse_to_talk_intents"],
        "historical_declared_fixture_provenance": saved["declared_fixture_provenance"],
        "current_routing_helper_results": cases,
        "current_clear_policy_guard_next_node": routing.after_guard_policy({**state, "blocked": False}),
        "current_blocked_policy_guard_next_node_after_two_repairs": routing.after_guard_policy({**state, "blocked": True}),
        "current_finalize_helper_probe": {
            "kind": "Constructed state; actual current finalize_run, no-write idempotent path; not an end-to-end build",
            "supplied_unresolved_policy_issues": issues,
            "summary": final,
            "tests_detail": collect_tests(final_state),
            "workflow_file_created": (HERE / "NOT-CREATED.workflow.json").exists(),
        },
    })

after = hashes()
report = {
    "kind": "Current policy-routing/final-summary source audit and pure helper probes with historical synthetic evidence",
    "started_utc": started,
    "finished_utc": datetime.now(UTC).isoformat(),
    "python": sys.version,
    "llm_calls": 0,
    "platform_calls": 0,
    "new_builds": 0,
    "pytest_modules_run": [],
    "historical_evidence_path": HISTORICAL_PATH.relative_to(ROOT).as_posix(),
    "historical_evidence_sha256": hashlib.sha256(HISTORICAL_PATH.read_bytes()).hexdigest(),
    "source_before_sha256": before,
    "source_after_sha256": after,
    "source_changed_during_refresh": [name for name in SOURCE_FILES if before[name] != after[name]],
    "source_anchors": {
        "validation_issues_only": "builder/graph/nodes/validate_policy.py:58",
        "validation_repair_gate": "builder/graph/routing.py:89",
        "validation_best_effort": "builder/graph/routing.py:132-141",
        "clear_guard_continues": "builder/graph/routing.py:144-150",
        "grounding_only_override": "builder/graph/nodes/guard_policy.py:38-59",
        "status_ignores_policy_issues": "builder/graph/nodes/finalize.py:58-78",
        "tests_exclude_policy_validation": "builder/graph/nodes/finalize.py:81-109",
        "summary_errors_from_state_errors": "builder/graph/nodes/finalize.py:147-172",
        "trace_errors_list": "builder/graph/nodes/finalize.py:200",
        "persisted_build_result_omits_policy_issues": "builder/graph/nodes/finalize.py:215-222",
        "cli_exit_ignores_errors": "builder/build.py:574-578",
        "naming_rule": "builder/understand/validate.py:877-883",
        "shadowed_row_rule": "builder/understand/validate.py:987-997",
        "note_before_identity_rule": "builder/understand/validate.py:1004-1010",
        "intentional_best_effort_test": "tests/graph/test_routing.py:64-80",
    },
    "conclusion": "Current helpers reproduce guard_policy continuation with unresolved v01 issues and a constructed dry_run final summary with errors[] and tests 1/1. Historical v01 final IR had 4 errors/2 warnings; v02 had 0/0. Current validator/router/validator-node/finalizer hashes match that revalidation. Original artifacts are absent, so no fresh final-IR revalidation is claimed.",
    "limits": "Fixture-model synthetic artifacts only; no proof of actual LLM extraction, conversation correctness, live deployment, unseen hidden-brief accuracy or official customer scoring.",
    "builds": rows,
}
(HERE / "policy-validation-refresh.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
print(json.dumps({
    "report": str(HERE / "policy-validation-refresh.json"),
    "source_changed": report["source_changed_during_refresh"],
    "builds": [{
        "slot": row["slot"],
        "historical_error_count": row["historical_error_count"],
        "historical_warning_count": row["historical_warning_count"],
        "fresh_ir_revalidation_performed": row["fresh_ir_revalidation_performed"],
        "current_capped_route": row["current_routing_helper_results"]["no_agentic_two_repairs"]["next_node"],
        "current_constructed_summary_status": row["current_finalize_helper_probe"]["summary"]["status"],
        "current_constructed_summary_errors": row["current_finalize_helper_probe"]["summary"]["errors"],
    } for row in rows],
}, indent=2))
