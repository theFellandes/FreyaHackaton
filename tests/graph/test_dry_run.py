"""The whole graph, offline: ``build.py`` on Brief A + the kit's tool API with a fixture gateway and no Freya.

The run goes through every stage up to the workflow guard (load, guard, understand, extract, validate, prose, compile,
lint) and must leave: a lint-clean workflow file, a trace with START / END / MODEL lines and the summary block, and
exactly one JSON summary line as the last line of stdout. No model, no key and no network are involved.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from builder.build import main
from builder.checks.lint import lint_workflow
from builder.ingest.tool_api import load_tool_api
from builder.policy.ir import PolicyIR

SUMMARY_KEYS = {"slot", "track", "workflow_id", "agent_id", "version", "exported", "trace", "status"}


def run_cli(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict[str, Any], str]:
    code = main(list(argv))
    captured = capsys.readouterr()
    last = [line for line in captured.out.splitlines() if line.strip()][-1]
    return code, json.loads(last), captured.err


@pytest.fixture
def dry_run_result(
    capsys: pytest.CaptureFixture[str],
    tmp_dirs: SimpleNamespace,
    brief_a_path: Path,
    kit_openapi_path: Path,
    fixture_dir: Path,
) -> tuple[int, dict[str, Any], str]:
    return run_cli(
        capsys,
        str(brief_a_path),
        str(kit_openapi_path),
        "--dry-run",
        "--fake-llm",
        str(fixture_dir),
        "--slot",
        "t-dry",
    )


def test_exit_code_and_summary_line(
    dry_run_result: tuple[int, dict[str, Any], str], tmp_dirs: SimpleNamespace
) -> None:
    code, summary, _ = dry_run_result
    assert code == 0
    assert set(summary) >= SUMMARY_KEYS
    assert summary["status"] == "dry_run"
    assert summary["track"] == "compiled"
    assert summary["slot"] == "t-dry"
    assert summary["workflow_id"] is None and summary["agent_id"] is None
    assert Path(summary["trace"]).parent == tmp_dirs.traces
    assert Path(summary["exported"]) == tmp_dirs.out / "t-dry.workflow.json"


def test_the_workflow_file_is_lint_clean(
    dry_run_result: tuple[int, dict[str, Any], str], kit_openapi_path: Path, fixture_dir: Path
) -> None:
    _, summary, _ = dry_run_result
    workflow = json.loads(Path(summary["workflow_file"]).read_text(encoding="utf-8"))
    ir = PolicyIR.model_validate(json.loads((fixture_dir / "brief_a_ir.json").read_text(encoding="utf-8")))
    report = lint_workflow(
        workflow, load_tool_api(kit_openapi_path), {*ir.call_variable_names(), "customer_id", "now"}
    )
    assert report.errors == [], report.format()
    assert "null" not in json.dumps(workflow).replace('"null"', "")  # no JSON null anywhere
    assert workflow["initial_node"] in workflow["nodes"]


def test_the_trace_has_start_end_model_calls_and_the_summary(
    dry_run_result: tuple[int, dict[str, Any], str],
) -> None:
    _, summary, _ = dry_run_result
    log = Path(summary["trace"])
    text = log.read_text(encoding="utf-8")
    lines = text.splitlines()
    assert " | START  | " in lines[0]
    assert any(" | MODEL  | " in line for line in lines)
    assert any(" | END    | " in line for line in lines)
    assert "BUILD SUMMARY" in text and "status       : dry_run" in text
    records = [
        json.loads(line) for line in log.with_suffix(".jsonl").read_text(encoding="utf-8").splitlines()
    ]
    kinds = [record["kind"] for record in records]
    assert kinds[0] == "start" and kinds[-1] == "end"
    assert kinds.count("summary") == 1
    assert kinds.count("model_call") >= 4  # ledger passages, completeness, IR, prose
    stages = [r["data"]["stage"] for r in records if r["kind"] == "stage" and r["data"]["status"] == "start"]
    assert stages == [
        "load_inputs",
        "guard_inputs",
        "understand_brief",
        "extract_policy",
        "validate_policy",
        "guard_policy",
        "draft_prose",
        "compile_workflow",
        "static_checks",
        "guard_workflow",
        "finalize",
    ]
    guards = [r["message"] for r in records if r["kind"] == "guard"]
    assert [g.split(" | ")[0] for g in guards] == ["inputs", "ledger", "policy", "prose", "workflow"]


def test_a_dry_run_makes_no_freya_call_and_writes_the_inspection_files(
    dry_run_result: tuple[int, dict[str, Any], str], tmp_dirs: SimpleNamespace
) -> None:
    _, _, err = dry_run_result
    assert (tmp_dirs.out / "t-dry.ledger.json").is_file() and (tmp_dirs.out / "t-dry.ir.json").is_file()
    assert any(p.name.endswith(".build.json") for p in tmp_dirs.results.iterdir())
    assert "FREYA" not in err.split("BUILD SUMMARY")[0].replace(
        "FREYA_API", ""
    )  # no FREYA trace line before the summary
    llm_log = next(tmp_dirs.traces.glob("*.llm.jsonl"))
    assert llm_log.read_text(encoding="utf-8").strip()  # the LLM observer log exists and is not empty


def test_progress_goes_to_stderr_and_only_the_summary_to_stdout(
    capsys: pytest.CaptureFixture[str],
    tmp_dirs: SimpleNamespace,
    brief_a_path: Path,
    kit_openapi_path: Path,
    fixture_dir: Path,
) -> None:
    main([str(brief_a_path), str(kit_openapi_path), "--dry-run", "--fake-llm", str(fixture_dir)])
    captured = capsys.readouterr()
    assert len(captured.out.strip().splitlines()) == 1
    json.loads(captured.out)
    assert " | STAGE  | " in captured.err


def test_a_dry_run_with_the_lean_profile(
    capsys: pytest.CaptureFixture[str],
    tmp_dirs: SimpleNamespace,
    brief_a_path: Path,
    kit_openapi_path: Path,
    fixture_dir: Path,
) -> None:
    code, summary, _ = run_cli(
        capsys,
        str(brief_a_path),
        str(kit_openapi_path),
        "--dry-run",
        "--fake-llm",
        str(fixture_dir),
        "--profile",
        "lean",
    )
    assert code == 0 and summary["status"] == "dry_run"
    workflow = json.loads(Path(summary["workflow_file"]).read_text(encoding="utf-8"))
    assert len(workflow["nodes"]) < 15  # the lean profile is the small one


def test_both_kit_briefs_use_the_matching_fixture_pair(
    capsys: pytest.CaptureFixture[str],
    tmp_dirs: SimpleNamespace,
    kit_brief_paths: list[Path],
    kit_openapi_path: Path,
    fixture_dir: Path,
) -> None:
    code, summary, _ = run_cli(
        capsys, *map(str, kit_brief_paths), str(kit_openapi_path), "--dry-run", "--fake-llm", str(fixture_dir)
    )
    assert code == 0, summary
    assert summary["slot"] == "brief-a-brief-b"
    ledger = json.loads((tmp_dirs.out / "brief-a-brief-b.ledger.json").read_text(encoding="utf-8"))
    assert ledger["source_files"] == ["brief-a.md", "brief-b.md"]


def test_dry_run_trace_never_contains_the_secrets(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_dirs: SimpleNamespace,
    brief_a_path: Path,
    kit_openapi_path: Path,
    fixture_dir: Path,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-unit-secret-0123456789abcdef")
    monkeypatch.setenv("FREYA_API_KEY", "fak_unit_test_secret_0123456789")
    _, summary, err = run_cli(
        capsys, str(brief_a_path), str(kit_openapi_path), "--dry-run", "--fake-llm", str(fixture_dir)
    )
    texts: list[Callable[[], str]] = [
        lambda: Path(summary["trace"]).read_text(encoding="utf-8"),
        lambda: Path(summary["trace"]).with_suffix(".jsonl").read_text(encoding="utf-8"),
        lambda: err,
    ]
    for read in texts:
        content = read()
        assert "sk-test-unit-secret-0123456789abcdef" not in content
        assert "fak_unit_test_secret_0123456789" not in content
