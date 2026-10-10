"""Tests for builder.freya.practice: kit-brief detection and running tools/run_cases.py through a fake runner.

No subprocess is started and nothing touches the network: ``run_practice`` receives a fake runner that records the
command and writes the ``--out`` JSON the way the kit's harness does.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import builder.freya.practice as practice_mod
from builder import paths
from builder.freya.practice import (
    PracticeError,
    PracticeResult,
    build_practice_command,
    kit_brief_letter,
    kit_brief_letters,
    parse_practice_results,
    practice_env,
    run_practice,
)
from builder.settings import Settings

REPO_ROOT = Path(__file__).resolve().parents[2]
TEST_ENV = {
    "FREYA_API_KEY": "fak_unit_test_placeholder",
    "FREYA_BANK_TEAM_KEY": "team-key-for-unit-tests",
    "PATH": "x",
}


def harness_output(**overrides: Any) -> dict[str, Any]:
    """An ``--out`` document shaped like the one tools/run_cases.py writes."""
    rows = [
        {
            "id": "PUB-01",
            "level": "easy",
            "family": "Promise",
            "pass": True,
            "failed": [],
            "call_id": "call_1",
            "tools": ["verify_identity", "get_account", "create_promise", "close_call"],
            "customer_id": "c_1",
        },
        {
            "id": "PUB-02",
            "level": "medium",
            "family": "Third party",
            "pass": False,
            "failed": ["outcome ('no_promise', 'refused'), expected ('not_reached', 'wrong_number')"],
            "call_id": "call_2",
            "tools": ["close_call"],
            "customer_id": "c_2",
        },
    ]
    document = {
        "agent_id": "ag_1",
        "simulation_id": "sim_9",
        "iteration_id": "it_3",
        "seconds": 412,
        "passed": 1,
        "total": 2,
        "runs": 1,
        "final": [
            {"id": "PUB-01", "family": "Promise", "passes": 1, "runs": 1, "pass": True},
            {"id": "PUB-02", "family": "Third party", "passes": 0, "runs": 1, "pass": False},
        ],
        "rows": rows,
        "all_rows": [rows],
    }
    document.update(overrides)
    return document


class FakeRunner:
    """Stands in for ``subprocess.run``: records the call and writes the harness's ``--out`` file."""

    def __init__(
        self,
        *,
        payload: Any = None,
        returncode: int = 0,
        stdout: str = "PASS PUB-01\nFAIL PUB-02\n",
        stderr: str = "",
        write: bool = True,
        raises: BaseException | None = None,
    ) -> None:
        self.payload = harness_output() if payload is None else payload
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.write = write
        self.raises = raises
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append((command, kwargs))
        if self.raises is not None:
            raise self.raises
        if self.write:
            out = Path(command[command.index("--out") + 1])
            out.write_text(self.payload if isinstance(self.payload, str) else json.dumps(self.payload))
        return subprocess.CompletedProcess(command, self.returncode, self.stdout, self.stderr)


class EventTrace:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, Any]]] = []

    def event(self, kind: str, message: str, **data: Any) -> None:
        self.events.append((kind, message, data))


# ---------------------------------------------------------------------------------------------------------- run_practice


def test_run_practice_builds_the_harness_command_and_parses_the_results(tmp_path: Path) -> None:
    runner = FakeRunner()
    trace = EventTrace()
    out = tmp_path / "results" / "run.json"

    result = run_practice(
        "ag_1",
        "a",
        reps=3,
        style="talkative",
        only=["PUB-01", " PUB-02"],
        out=out,
        timeout_s=900,
        runner=runner,
        env=TEST_ENV,
        trace=trace,
        python="py.exe",
    )

    command, kwargs = runner.calls[0]
    assert command == [
        "py.exe",
        str(paths.KIT_TOOLS_DIR / "run_cases.py"),
        "ag_1",
        "--reps",
        "3",
        "--out",
        str(out),
        "--brief",
        "A",
        "--style",
        "talkative",
        "--only",
        "PUB-01,PUB-02",
    ]
    assert kwargs["cwd"] == str(paths.REPO_ROOT)
    assert kwargs["timeout"] == 900
    assert kwargs["capture_output"] is True and kwargs["text"] is True and kwargs["check"] is False
    assert kwargs["env"]["FREYA_API_KEY"] == TEST_ENV["FREYA_API_KEY"]
    assert kwargs["env"]["PYTHONUTF8"] == "1"
    assert kwargs["env"]["PYTHONIOENCODING"] == "utf-8"
    assert "PYTHONUTF8" not in TEST_ENV  # the caller's mapping is not modified

    assert isinstance(result, PracticeResult)
    assert (result.passed, result.total, result.runs) == (1, 2, 1)
    assert result.simulation_id == "sim_9"
    assert result.iteration_id == "it_3"
    assert result.raw_path == out
    assert result.letter == "A"
    assert result.failed_ids == ["PUB-02"]
    assert result.pass_rate == 0.5
    assert [row["id"] for row in result.rows] == ["PUB-01", "PUB-02"]
    assert result.log_tail == "PASS PUB-01\nFAIL PUB-02"
    assert [message for _, message, _ in trace.events][-1] == "practice 1/2 passed"
    assert trace.events[-1][2]["failed"] == ["PUB-02"]


def test_run_practice_without_letter_or_filters_runs_every_case(tmp_path: Path) -> None:
    runner = FakeRunner()
    run_practice("ag_1", None, out=tmp_path / "r.json", runner=runner, env=TEST_ENV)
    command = runner.calls[0][0]
    assert command[0] == sys.executable
    for flag in ("--brief", "--style", "--only"):
        assert flag not in command
    assert command[command.index("--reps") + 1] == "1"


def test_run_practice_accepts_a_comma_separated_only(tmp_path: Path) -> None:
    runner = FakeRunner()
    run_practice("ag_1", "B", only="PUB-03, PUB-04,", out=tmp_path / "r.json", runner=runner, env=TEST_ENV)
    command = runner.calls[0][0]
    assert command[command.index("--only") + 1] == "PUB-03,PUB-04"


@pytest.mark.parametrize("missing", ["FREYA_API_KEY", "FREYA_BANK_TEAM_KEY"])
def test_run_practice_needs_the_credentials(tmp_path: Path, missing: str) -> None:
    runner = FakeRunner()
    env = {k: v for k, v in TEST_ENV.items() if k != missing}
    with pytest.raises(PracticeError, match=missing):
        run_practice("ag_1", "A", out=tmp_path / "r.json", runner=runner, env=env)
    assert runner.calls == []


def test_run_practice_reports_a_failed_harness(tmp_path: Path) -> None:
    runner = FakeRunner(
        returncode=1,
        write=False,
        stdout="PASS PUB-01\n",
        stderr="Traceback (most recent call last):\nKeyError: 'FREYA_BANK_TEAM_KEY'\n",
    )
    trace = EventTrace()
    with pytest.raises(PracticeError) as caught:
        run_practice("ag_1", "A", out=tmp_path / "r.json", runner=runner, env=TEST_ENV, trace=trace)
    assert caught.value.returncode == 1
    assert "KeyError" in str(caught.value)
    assert caught.value.stderr_tail.endswith("KeyError: 'FREYA_BANK_TEAM_KEY'")
    assert caught.value.log_tail == "PASS PUB-01"
    assert trace.events[-1][2]["ok"] is False


def test_run_practice_never_returns_a_stale_result(tmp_path: Path) -> None:
    out = tmp_path / "r.json"
    out.write_text(json.dumps(harness_output(passed=2)))
    with pytest.raises(PracticeError, match="wrote no results"):
        run_practice("ag_1", "A", out=out, runner=FakeRunner(write=False), env=TEST_ENV)
    assert not out.exists()


def test_run_practice_timeout(tmp_path: Path) -> None:
    timeout = subprocess.TimeoutExpired(["py"], 5, output=b"PASS PUB-01\nFAIL PUB-02\n")
    with pytest.raises(PracticeError, match="did not finish within 5 s") as caught:
        run_practice(
            "ag_1", "A", out=tmp_path / "r.json", timeout_s=5, runner=FakeRunner(raises=timeout), env=TEST_ENV
        )
    assert caught.value.log_tail == "PASS PUB-01\nFAIL PUB-02"


def test_run_practice_cannot_start(tmp_path: Path) -> None:
    with pytest.raises(PracticeError, match="cannot start"):
        run_practice(
            "ag_1",
            "A",
            out=tmp_path / "r.json",
            runner=FakeRunner(raises=FileNotFoundError("py")),
            env=TEST_ENV,
        )


@pytest.mark.parametrize(
    ("payload", "message"),
    [("not json", "cannot read"), ("[1, 2]", "not a JSON object"), ({"total": 3}, "passed/total")],
)
def test_run_practice_rejects_unusable_results(tmp_path: Path, payload: Any, message: str) -> None:
    with pytest.raises(PracticeError, match=message):
        run_practice("ag_1", "A", out=tmp_path / "r.json", runner=FakeRunner(payload=payload), env=TEST_ENV)


def test_run_practice_default_output_goes_to_results(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(paths, "RESULTS_DIR", tmp_path / "results")
    result = run_practice("ag-1234567890", "B", runner=FakeRunner(), env=TEST_ENV)
    assert result.raw_path.parent == tmp_path / "results"
    assert result.raw_path.name.startswith("practice-b-ag-12345-")
    assert result.raw_path.suffix == ".json"


def test_run_practice_anchors_a_relative_output_at_the_repository(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(paths, "REPO_ROOT", tmp_path)
    runner = FakeRunner()
    result = run_practice("ag_1", "A", out="results/r.json", runner=runner, env=TEST_ENV)
    assert result.raw_path == tmp_path / "results" / "r.json"
    assert runner.calls[0][1]["cwd"] == str(tmp_path)


def test_run_practice_validates_arguments(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        run_practice("ag_1", "A", reps=0, out=tmp_path / "r.json", runner=FakeRunner(), env=TEST_ENV)
    with pytest.raises(ValueError):
        run_practice("ag_1", "A", timeout_s=0, out=tmp_path / "r.json", runner=FakeRunner(), env=TEST_ENV)
    with pytest.raises(ValueError):
        build_practice_command("--help", "A", out=tmp_path / "r.json")


def test_parse_results_with_several_runs(tmp_path: Path) -> None:
    path = tmp_path / "r.json"
    path.write_text(json.dumps(harness_output(runs=3, seconds=None, simulation_id="")))
    result = parse_practice_results(path, letter="b")
    assert result.runs == 3
    assert result.seconds is None
    assert result.simulation_id is None
    assert result.letter == "B"
    assert result.model_dump(mode="json")["raw_path"] == str(path)


def test_practice_env_adds_settings_without_touching_os_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(
        _env_file=None,
        freya_api_key="fak_from_settings_placeholder",
        freya_bank_team_key="team-key-from-settings",
        freya_api_url="http://127.0.0.1:9/",
        freya_workspace_id="ws_7",
    )
    monkeypatch.setattr(practice_mod, "get_settings", lambda: settings)
    monkeypatch.delenv("FREYA_WORKSPACE_ID", raising=False)
    before = dict(os.environ)

    env = practice_env({"PATH": "x", "FREYA_AGENT_MODEL": "organiser-choice"})

    assert env["FREYA_API_KEY"] == "fak_from_settings_placeholder"
    assert env["FREYA_BANK_TEAM_KEY"] == "team-key-from-settings"
    assert env["FREYA_API_URL"] == "http://127.0.0.1:9"
    assert env["FREYA_WORKSPACE_ID"] == "ws_7"
    assert env["FREYA_AGENT_MODEL"] == "organiser-choice"
    assert env["PYTHONUTF8"] == "1"
    assert dict(os.environ) == before


# ---------------------------------------------------------------------------------------------------------- kit brief


@pytest.fixture
def kit_briefs(tmp_path: Path) -> Path:
    directory = tmp_path / "briefs"
    directory.mkdir()
    (directory / "brief-a.md").write_bytes(b"# Brief one\n\nRule 1: be polite.\n")
    (directory / "brief-b.md").write_bytes(b"# Brief two\n\nEverything of brief one applies.\n")
    (directory / "notes.md").write_bytes(b"not a brief\n")
    return directory


def doc(text: str | None = None, sha256: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(text=text, sha256=sha256)


def test_kit_brief_letters_lists_only_brief_files(kit_briefs: Path) -> None:
    letters = kit_brief_letters(kit_briefs)
    assert sorted(letters) == ["A", "B"]
    raw = (kit_briefs / "brief-a.md").read_bytes()
    assert hashlib.sha256(raw).hexdigest() in letters["A"]


def test_only_the_first_kit_brief_gives_a(kit_briefs: Path) -> None:
    bundle = SimpleNamespace(documents=[doc("# Brief one\n\nRule 1: be polite.\n")])
    assert kit_brief_letter(bundle, kit_briefs_dir=kit_briefs) == "A"


def test_the_later_kit_brief_wins(kit_briefs: Path) -> None:
    bundle = SimpleNamespace(
        documents=[
            doc("# Brief one\n\nRule 1: be polite.\n"),
            doc("# Brief two\n\nEverything of brief one applies.\n"),
        ]
    )
    assert kit_brief_letter(bundle, kit_briefs_dir=kit_briefs) == "B"


def test_matching_ignores_bom_line_endings_and_surrounding_whitespace(kit_briefs: Path) -> None:
    text = "﻿# Brief two\r\n\r\nEverything of brief one applies.\r\n"
    assert kit_brief_letter(SimpleNamespace(documents=[doc(text)]), kit_briefs_dir=kit_briefs) == "B"
    stripped = "# Brief two\n\nEverything of brief one applies."
    assert kit_brief_letter(SimpleNamespace(documents=[doc(stripped)]), kit_briefs_dir=kit_briefs) == "B"


def test_matching_by_the_loader_sha256(kit_briefs: Path) -> None:
    digest = hashlib.sha256((kit_briefs / "brief-a.md").read_bytes()).hexdigest().upper()
    assert kit_brief_letter(SimpleNamespace(documents=[doc(None, digest)]), kit_briefs_dir=kit_briefs) == "A"


def test_an_edited_or_unknown_brief_gets_no_letter(kit_briefs: Path) -> None:
    edited = SimpleNamespace(documents=[doc("# Brief one\n\nRule 1: be very polite.\n", "0" * 64)])
    assert kit_brief_letter(edited, kit_briefs_dir=kit_briefs) is None
    assert kit_brief_letter(SimpleNamespace(documents=[]), kit_briefs_dir=kit_briefs) is None
    assert (
        kit_brief_letter(SimpleNamespace(documents=[doc("x")]), kit_briefs_dir=kit_briefs / "missing") is None
    )


def test_a_dumped_bundle_dict_works(kit_briefs: Path) -> None:
    bundle = {
        "documents": [{"name": "x.md", "text": "# Brief one\n\nRule 1: be polite.\n", "sha256": "ab"}],
        "sha256": "cd",
    }
    assert kit_brief_letter(bundle, kit_briefs_dir=kit_briefs) == "A"


def test_the_real_kit_briefs_are_recognised() -> None:
    briefs = REPO_ROOT / "briefs"
    first = (briefs / "brief-a.md").read_text(encoding="utf-8")
    second = (briefs / "brief-b.md").read_text(encoding="utf-8")
    assert kit_brief_letter(SimpleNamespace(documents=[doc(first)])) == "A"
    assert kit_brief_letter(SimpleNamespace(documents=[doc(first), doc(second)])) == "B"
