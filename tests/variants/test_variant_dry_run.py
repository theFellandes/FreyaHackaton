"""The whole graph (``build.py --dry-run --fake-llm``) on the synthetic brief with structural variants of its tool contract.

The fixture gateway answers the extraction calls with the variant's IR and ledger; everything else is the real compiled
track: input guard, understanding, extraction (with its safe fixes), validation, policy guard, prose, compile, lint and the
workflow guard. The run must end ``dry_run`` with exit code 0, and the workflow it writes must be exactly what the
compiler makes from the IR and ledger the run saved next to it - so the offline engine tests of
``test_structural_variants.py`` describe the agent the build would deploy.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from builder.build import main
from builder.checks.lint import lint_workflow
from builder.compiler.compile import compile_workflow
from builder.graph.nodes._common import lint_call_variables
from builder.ingest.tool_api import load_tool_api
from builder.policy.ir import PolicyIR, RuleLedger
from tests.variants import variants as V
from tests.variants.engine import Engine


def _dry_run(variant: V.Variant, root: Path, capsys: pytest.CaptureFixture[str], slot: str,
             *extra: str) -> tuple[int, dict[str, Any], Path]:
    fixtures = root / "fixtures"
    fixtures.mkdir()
    (fixtures / "ir.json").write_text(json.dumps(variant.ir), encoding="utf-8")
    (fixtures / "ledger.json").write_text(json.dumps(variant.ledger), encoding="utf-8")
    api = root / "tools.openapi.json"
    api.write_text(json.dumps(variant.openapi), encoding="utf-8")
    code = main([str(V.SYNTHETIC_PACK / "brief.md"), str(api), "--dry-run", "--fake-llm", str(fixtures),
                 "--slot", slot, *extra])
    last = [line for line in capsys.readouterr().out.splitlines() if line.strip()][-1]
    return code, json.loads(last), api


@pytest.mark.parametrize("profile", ["staged", "lean"])
@pytest.mark.parametrize("make", [V.base, lambda: V.renamed_contract(V.base())], ids=["base", "renamed_contract"])
def test_the_graph_builds_the_variant_and_writes_a_reproducible_workflow(
    tmp_dirs: SimpleNamespace, capsys: pytest.CaptureFixture[str], make: Any, profile: str,
) -> None:
    code, summary, api_path = _dry_run(make(), tmp_dirs.root, capsys, "v-variant", "--profile", profile)
    assert code == 0 and summary["status"] == "dry_run" and summary["errors"] == [], summary
    workflow = json.loads(Path(summary["workflow_file"]).read_text(encoding="utf-8"))
    ir = PolicyIR.model_validate_json((tmp_dirs.out / "v-variant.ir.json").read_text(encoding="utf-8"))
    ledger = RuleLedger.model_validate_json((tmp_dirs.out / "v-variant.ledger.json").read_text(encoding="utf-8"))
    api = load_tool_api(api_path)
    assert compile_workflow(ir, api, ledger, None, profile).workflow == workflow
    assert lint_workflow(workflow, api, lint_call_variables(ir)).errors == []
    trace = Path(summary["trace"]).read_text(encoding="utf-8")
    assert "uncovered: none" in trace
    if profile == "staged":  # the lean rule map also names staged nodes (each such rule keeps a real location)
        assert " 0 dangling" in trace


def test_the_built_renamed_contract_closes_with_its_own_field_names(
    tmp_dirs: SimpleNamespace, capsys: pytest.CaptureFixture[str],
) -> None:
    code, summary, _ = _dry_run(V.renamed_contract(V.base()), tmp_dirs.root, capsys, "v-variant")
    assert code == 0
    workflow = json.loads(Path(summary["workflow_file"]).read_text(encoding="utf-8"))
    engine = Engine(workflow, {"customer_id": "c-9", "first_name": "Ann", "last_name": "Lee", "title": "Ms",
                               "now": "2026-11-02T10:00"})
    engine.go(workflow["initial_node"])
    engine.move("end_wrong_number")
    assert engine.sent("close_call") == [{"customer_ref": "c-9", "disposition": "unreachable",
                                          "disposition_reason": "wrong_person"}]
