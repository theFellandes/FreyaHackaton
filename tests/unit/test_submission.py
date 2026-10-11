"""Local submission checks: synthetic saved evidence, portability, provenance and pending commits."""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from builder import submission
from builder.trace import TraceWriter

Packet = tuple[Path, dict[str, Any], dict[str, bytes]]
SHA = "a" * 40


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


@pytest.fixture
def packet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Packet:
    manifest: dict[str, Any] = {
        "team": "Solo entrant", "repo": "https://example.test/team/repo", "commit": SHA,
        "run_command": "python3 builder/build.py {brief_path} {tool_api_path}",
        "workflows": {}, "traces": {}, "build_results": {},
    }
    for relative in submission.REQUIRED_CODE:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic runtime fixture\n", encoding="utf-8")
    for index, brief in enumerate(submission.BRIEFS, 1):
        run_id = f"{index:012x}"
        workflow = f"out/{brief}.exported.json"
        trace = f"traces/{brief}-{run_id}.jsonl"
        result = f"results/{brief}-{run_id}.build.json"
        manifest["workflows"][brief], manifest["traces"][brief], manifest["build_results"][brief] = workflow, trace, result
        ids = {"workflow_id": f"workflow-{index}", "agent_id": f"agent-{index}", "version": index}
        write_json(tmp_path / workflow, {
            **ids, "updated_at": "2026-10-11T18:01:00Z",
            "workflow": {"initial_node": "end", "nodes": {"end": {"type": "end_call"}}},
        })
        write_json(tmp_path / result, {
            **ids, "slot": brief, "status": "deployed", "export_ok": True,
            "exported": str(tmp_path / workflow), "trace": str(tmp_path / trace),
            "tests": {"passed": 2, "total": 2}, "errors": [],
        })
        records = [
            {"kind": "start", "run_id": run_id, "ts": "2026-10-11T18:00:00Z", "data": {}},
            {"kind": "model_call", "run_id": run_id, "ts": "2026-10-11T18:00:01Z", "data": {
                "provider": "synthetic", "model": "fixture", "purpose": "extract the test policy",
            }},
            {"kind": "summary", "run_id": run_id, "ts": "2026-10-11T18:01:00Z", "data": {
                "run_id": run_id, "slot": brief, "status": "deployed", "model_calls": {"total": 1},
                "freya": {**ids, "published": True, "node_count": 1, "tool_count": 0},
                "extra": {"exported": str(tmp_path / workflow)},
            }},
            {"kind": "end", "run_id": run_id, "ts": "2026-10-11T18:01:01Z", "data": {}},
        ]
        path = tmp_path / trace
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    committed = {path.relative_to(tmp_path).as_posix(): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}

    def fake_git(root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
        assert root == tmp_path
        if args[0] == "rev-parse":
            if args[2].endswith("^{commit}"):
                return subprocess.CompletedProcess(args, 0, (SHA + "\n").encode(), b"")
            relative = args[2].split(":", 1)[1]
            value = hashlib.sha1(committed.get(relative, b"").replace(b"\r\n", b"\n")).hexdigest().encode()
            return subprocess.CompletedProcess(args, 0 if relative in committed else 1, value + b"\n", b"")
        if args[0] == "hash-object":
            value = hashlib.sha1((tmp_path / args[-1]).read_bytes().replace(b"\r\n", b"\n")).hexdigest().encode()
            return subprocess.CompletedProcess(args, 0, value + b"\n", b"")
        if args[0] in ("diff", "ls-files"):
            return subprocess.CompletedProcess(args, 0, b"", b"")
        assert args[:2] == ("cat-file", "-e")
        relative = args[2].split(":", 1)[1]
        return subprocess.CompletedProcess(args, 0 if relative in committed else 1, committed.get(relative, b""), b"")

    monkeypatch.setattr(submission, "_git", fake_git)
    return tmp_path, manifest, committed


def messages(report: submission.SubmissionReport) -> str:
    return "\n".join(issue.message for issue in report.issues)


def build_path(packet: Packet, brief: str = "hidden-1") -> Path:
    root, manifest, _ = packet
    return root / manifest["build_results"][brief]


def test_successful_synthetic_packet_is_ready_but_does_not_claim_customer_success(packet: Packet) -> None:
    root, manifest, _ = packet
    report = submission.check_submission(manifest, root=root)
    assert report.ready and report.commit_checked
    assert not report.issues
    assert len(report.checked_files) == 6
    assert any("hidden-customer scores" in warning for warning in report.warnings)


@pytest.mark.parametrize("status", ["dry_run", "failed", "deployed_unpublished", "deployed_unexported"])
def test_dry_run_and_failed_builds_cannot_be_final_exports(packet: Packet, status: str) -> None:
    root, manifest, _ = packet
    path = build_path(packet)
    build = json.loads(path.read_text(encoding="utf-8"))
    build.update(status=status, workflow_id=None, agent_id=None, version=None, export_ok=None)
    write_json(path, build)
    report = submission.check_submission(manifest, root=root)
    assert not report.ready
    assert "dry runs are not final agents" in messages(report)


@pytest.mark.parametrize("path", [
    "../export.json", "out/../../export.json", "..\\export.json", "out\\..\\export.json",
    "C:\\export.json", "C:export.json", "\\\\host\\share\\export.json", "/export.json",
    "out/export.json:stream", "out\x00file.json",
])
def test_repository_paths_reject_windows_and_posix_escape(tmp_path: Path, path: str) -> None:
    with pytest.raises(ValueError):
        submission.repository_path(tmp_path, path)


def test_repository_paths_accept_portable_backslashes(tmp_path: Path) -> None:
    assert submission.repository_path(tmp_path, "out\\agent.json") == (tmp_path / "out/agent.json").resolve()


def test_missing_inputs_collect_multiple_useful_blockers(packet: Packet) -> None:
    root, _, _ = packet
    report = submission.check_submission({}, root=root)
    assert not report.ready
    fields = {issue.field for issue in report.issues}
    assert {"team", "repo", "commit", "run_command", "workflows", "traces", "build_results"} <= fields
    assert "workflows.hidden-1" in fields and "traces.hidden-2" in fields


def test_missing_artifact_and_wrong_hidden_brief_maps_are_reported(packet: Packet) -> None:
    root, manifest, _ = packet
    (root / manifest["workflows"]["hidden-1"]).unlink()
    manifest["traces"]["public-a"] = manifest["traces"].pop("hidden-2")
    report = submission.check_submission(manifest, root=root)
    assert "missing" in messages(report)
    assert "exactly hidden-1 and hidden-2" in messages(report)


@pytest.mark.parametrize("key,value", [("workflow_id", "another-workflow"), ("agent_id", "another-agent"), ("version", 99)])
def test_wrong_export_identity_or_version_is_rejected(packet: Packet, key: str, value: Any) -> None:
    root, manifest, _ = packet
    path = root / manifest["workflows"]["hidden-1"]
    export = json.loads(path.read_text(encoding="utf-8"))
    export[key] = value
    write_json(path, export)
    report = submission.check_submission(manifest, root=root)
    assert not report.ready
    assert f"Export {key if key != 'version' else 'version'} does not match" in messages(report)


def test_local_workflow_definition_is_not_an_actual_export(packet: Packet) -> None:
    root, manifest, _ = packet
    write_json(root / manifest["workflows"]["hidden-1"], {"initial_node": "end", "nodes": {}})
    report = submission.check_submission(manifest, root=root)
    assert "actual platform export" in messages(report)


def test_selected_export_and_trace_must_be_the_builds_recorded_files(packet: Packet) -> None:
    root, manifest, _ = packet
    manifest["workflows"]["hidden-1"] = manifest["workflows"]["hidden-2"]
    manifest["traces"]["hidden-1"] = manifest["traces"]["hidden-2"]
    report = submission.check_submission(manifest, root=root)
    assert "does not match this build's recorded artifact" in messages(report)
    assert "own selected artifact" in messages(report)
    assert "Trace run identity" in messages(report)


@pytest.mark.parametrize("change", ["publication", "cutoff", "run", "duration", "missing_end"])
def test_trace_must_confirm_same_complete_published_run(packet: Packet, change: str) -> None:
    root, manifest, _ = packet
    path = root / manifest["traces"]["hidden-1"]
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    if change == "publication":
        records[2]["data"]["freya"]["published"] = False
    elif change == "cutoff":
        records[2]["data"]["extra"]["cut_off"] = "wall clock guard fired"
    elif change == "run":
        for record in records:
            record["run_id"] = "badbadbadbad"
    elif change == "duration":
        records[-1]["ts"] = "2026-10-11T18:31:00Z"
    else:
        records.pop()
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    report = submission.check_submission(manifest, root=root)
    assert not report.ready
    assert any(issue.field == "traces.hidden-1" for issue in report.issues)


@pytest.mark.parametrize("flag", ["cut_off", "untested_redeploy"])
def test_build_cutoff_and_untested_replacement_cannot_be_ready(packet: Packet, flag: str) -> None:
    root, manifest, _ = packet
    path = build_path(packet)
    build = json.loads(path.read_text(encoding="utf-8"))
    build[flag] = True
    write_json(path, build)
    assert "untested replacement output" in messages(submission.check_submission(manifest, root=root))


def test_uncommitted_and_modified_artifacts_block_readiness(packet: Packet) -> None:
    root, manifest, committed = packet
    del committed[manifest["workflows"]["hidden-1"]]
    committed[manifest["traces"]["hidden-2"]] = b"previous trace bytes"
    report = submission.check_submission(manifest, root=root)
    assert "absent from the selected commit" in messages(report)
    assert "bytes differ" in messages(report)


def test_modified_code_blocks_readiness_without_mutating_git(packet: Packet, monkeypatch: pytest.MonkeyPatch) -> None:
    root, manifest, _ = packet
    original = submission._git

    def changed_code(repo: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
        if args[0] == "diff":
            return subprocess.CompletedProcess(args, 0, b"builder/build.py\n", b"")
        return original(repo, *args)

    monkeypatch.setattr(submission, "_git", changed_code)
    report = submission.check_submission(manifest, root=root)
    assert "runtime code/config/tests differ" in messages(report)
    assert not report.ready


def test_prepare_uses_explicit_results_and_refuses_dry_run_exports(packet: Packet) -> None:
    root, manifest, _ = packet
    path = build_path(packet)
    build = json.loads(path.read_text(encoding="utf-8"))
    build["status"] = "dry_run"
    write_json(path, build)
    draft = submission.prepare_submission(team="Solo", repo="repo", run_command=manifest["run_command"],
                                          build_results=manifest["build_results"], root=root)
    assert draft["workflows"]["hidden-1"] == draft["traces"]["hidden-1"] == ""
    assert draft["workflows"]["hidden-2"] == manifest["workflows"]["hidden-2"]
    report = submission.check_submission(draft, root=root)
    assert not report.ready and "Pending commit" in messages(report)


def test_prepare_command_writes_draft_but_will_not_overwrite_without_force(packet: Packet, capsys: pytest.CaptureFixture[str]) -> None:
    root, manifest, _ = packet
    args = ["--root", str(root), "prepare", "--output", "submission/draft.json", "--team", "Solo", "--repo", "repo",
            "--run-command", manifest["run_command"], "--hidden-1-result", manifest["build_results"]["hidden-1"],
            "--hidden-2-result", manifest["build_results"]["hidden-2"]]
    assert submission.main(args) == 2  # saved, with an explicitly pending commit
    first = (root / "submission/draft.json").read_bytes()
    assert submission.main(args) == 2
    assert (root / "submission/draft.json").read_bytes() == first
    assert "overwrite permission" in capsys.readouterr().out


def test_payload_emits_only_form_fields_after_success(packet: Packet, capsys: pytest.CaptureFixture[str]) -> None:
    root, manifest, _ = packet
    write_json(root / "submission/draft.json", manifest)
    assert submission.main(["--root", str(root), "payload", "submission/draft.json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == set(submission.FORM_FIELDS)
    assert "build_results" not in payload


def test_malformed_draft_errors_do_not_echo_secret_content(packet: Packet, capsys: pytest.CaptureFixture[str]) -> None:
    root, _, _ = packet
    path = root / "submission/bad.json"
    path.parent.mkdir()
    path.write_text("SECRET_DO_NOT_PRINT{broken", encoding="utf-8")
    assert submission.main(["--root", str(root), "check", "submission/bad.json"]) == 2
    text = capsys.readouterr().out
    assert "SECRET_DO_NOT_PRINT" not in text
    assert json.loads(text)["ready"] is False


@pytest.mark.parametrize("flag", ["--dry-run", "--fake-llm tests/fixtures/policy"])
def test_final_command_cannot_select_dry_run_or_fixture_answers(packet: Packet, flag: str) -> None:
    root, manifest, _ = packet
    manifest["run_command"] += " " + flag
    assert "must not request dry-run" in messages(submission.check_submission(manifest, root=root))


def test_payload_failure_keeps_stdout_empty(packet: Packet, capsys: pytest.CaptureFixture[str]) -> None:
    root, manifest, _ = packet
    manifest["commit"] = ""
    write_json(root / "submission/draft.json", manifest)
    assert submission.main(["--root", str(root), "payload", "submission/draft.json"]) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert json.loads(output.err)["ready"] is False


def test_git_line_ending_normalization_preserves_valid_artifact(packet: Packet) -> None:
    root, manifest, _ = packet
    path = root / manifest["workflows"]["hidden-1"]
    path.write_bytes(path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    assert submission.check_submission(manifest, root=root).ready


def test_real_trace_writer_log_and_companion_match_current_schema(packet: Packet) -> None:
    root, manifest, committed = packet
    brief = "hidden-1"
    result = build_path(packet)
    build = json.loads(result.read_text(encoding="utf-8"))
    run_id = "000000000001"
    trace = TraceWriter(run_id, brief, root / "traces", clock=lambda: datetime(2026, 10, 11, 18, tzinfo=UTC))
    trace.model_call("synthetic", "fixture", None, "extract the test policy", True, 0.1)
    trace.freya("save", workflow_id=build["workflow_id"], agent_id=build["agent_id"], version=build["version"],
                node_count=1, tool_count=0, published=True, slot=brief)
    trace.summary({"status": "deployed", "slot": brief, "exported": build["exported"]})
    trace.close()
    assert trace.log_path is not None and trace.jsonl_path is not None
    manifest["traces"][brief] = trace.log_path.relative_to(root).as_posix()
    build["trace"] = str(trace.log_path)
    write_json(result, build)
    for path in (result, trace.log_path, trace.jsonl_path):
        committed[path.relative_to(root).as_posix()] = path.read_bytes()
    report = submission.check_submission(manifest, root=root)
    assert report.ready, report.to_dict()
    assert trace.jsonl_path.relative_to(root).as_posix() in report.checked_files

    # A mismatched log is caught from START/END even after its run-id summary line was removed.
    text = trace.log_path.read_text(encoding="utf-8")
    text = "\n".join(line for line in text.splitlines() if not line.startswith("run id"))
    trace.log_path.write_text(text.replace(f"run {run_id} started", "run badbadbadbad started"), encoding="utf-8")
    assert not submission.check_submission(manifest, root=root).ready


@pytest.mark.parametrize("evidence", ["purpose", "node_count", "tool_count", "model_total"])
def test_required_trace_call_and_creation_evidence(packet: Packet, evidence: str) -> None:
    root, manifest, _ = packet
    path = root / manifest["traces"]["hidden-1"]
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    if evidence == "purpose":
        records[1]["data"].pop("purpose")
    elif evidence == "model_total":
        records[2]["data"]["model_calls"]["total"] = 2
    else:
        records[2]["data"]["freya"].pop(evidence)
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    report = submission.check_submission(manifest, root=root)
    assert any(issue.field == "traces.hidden-1" for issue in report.issues)


def test_the_commit_check_covers_the_install_files_and_the_startup_module(
    packet: Packet, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AUD-03: requirements.txt, README.md (the package readme) and builder/_startup.py must be in the commit."""
    root, manifest, committed = packet
    pathspecs: list[tuple[str, ...]] = []
    real = submission._git

    def spy(where: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
        if args[0] in ("diff", "ls-files"):
            pathspecs.append(args[args.index("--") + 1 :])
        return real(where, *args)

    monkeypatch.setattr(submission, "_git", spy)
    assert submission.check_submission(manifest, root=root).ready
    assert pathspecs and all({"requirements.txt", "README.md", "builder"} <= set(spec) for spec in pathspecs)
    for required in ("requirements.txt", "builder/_startup.py"):
        assert required in submission.REQUIRED_CODE
        del committed[required]
        report = submission.check_submission(manifest, root=root)
        assert not report.ready and f"Required runtime file is absent from the selected commit: {required}" in messages(
            report
        )
        committed[required] = b"restored\n"


@pytest.mark.parametrize("command", [
    "python3 builder/build.py {case_folder} {mock_url}",
    "python3 builder/build.py {case_folder} {mock_url} 2>&1 | tee traces/final.log",
])
def test_the_case_folder_run_command_is_accepted(packet: Packet, command: str) -> None:
    root, manifest, _ = packet
    manifest["run_command"] = command
    assert "run_command" not in {issue.field for issue in submission.check_submission(manifest, root=root).issues}


@pytest.mark.parametrize("command", ["python3 builder/build.py {case_folder}", "python3 builder/build.py {brief_path}"])
def test_a_run_command_without_its_input_placeholders_is_reported(packet: Packet, command: str) -> None:
    root, manifest, _ = packet
    manifest["run_command"] = command
    assert "run_command" in {issue.field for issue in submission.check_submission(manifest, root=root).issues}
