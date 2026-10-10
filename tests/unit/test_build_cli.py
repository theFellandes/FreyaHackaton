"""The command line of ``builder/build.py``: flags, settings overrides, failure handling, exit codes and the script bootstrap.

The full pipeline through ``main()`` is covered in ``tests/graph/test_dry_run.py``; here the focus is on the edges: bad
arguments, inputs that cannot be classified, the "never crash" promise (a summary line and a trace even then) and the
three ways to start the program.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from builder import paths
from builder.build import build_parser, main, settings_from_args
from builder.inputs import InputError

BUILD_PY = paths.REPO_ROOT / "builder" / "build.py"


@pytest.fixture(autouse=True)
def _isolated_key_health(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Dead-credential bookkeeping of a test must never reach the real ``data/key_health.json``."""
    monkeypatch.setenv("LLM_KEY_HEALTH_FILE", str(tmp_path / "key_health.json"))


def last_json_line(text: str) -> dict[str, Any]:
    return json.loads([line for line in text.splitlines() if line.strip()][-1])


# ------------------------------------------------------------------------------------------------ arguments


def test_flags_override_the_settings(settings: Any) -> None:
    args = build_parser().parse_args(
        [
            "b.md",
            "api.json",
            "--dry-run",
            "--track",
            "agentic",
            "--profile",
            "lean",
            "--self-test",
            "smoke",
            "--budget-s",
            "900",
        ]
    )
    effective = settings_from_args(args, settings)
    assert effective.dry_run and effective.builder_track == "agentic" and effective.agent_profile == "lean"
    assert effective.self_test == "smoke" and effective.build_time_budget_s == 900
    assert settings.builder_track == "auto" and not settings.dry_run  # the original is untouched


def test_defaults_come_from_the_settings(settings: Any) -> None:
    args = build_parser().parse_args(["b.md", "api.json"])
    assert settings_from_args(args, settings) is settings
    assert args.slot is None and args.fake_llm is None and not args.dry_run


def test_a_fixture_run_never_uses_the_agentic_track(settings: Any) -> None:
    args = build_parser().parse_args(
        ["b.md", "api.json", "--fake-llm", "tests/fixtures/policy", "--track", "agentic"]
    )
    assert settings_from_args(args, settings).builder_track == "compiled"


def test_a_negative_budget_is_rejected(settings: Any) -> None:
    args = build_parser().parse_args(["b.md", "api.json", "--budget-s", "-5"])
    with pytest.raises(InputError, match="positive"):
        settings_from_args(args, settings)


@pytest.mark.parametrize("flag", [["--track", "magic"], ["--profile", "huge"], ["--self-test", "always"]])
def test_unknown_choices_are_usage_errors(flag: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        build_parser().parse_args(["b.md", "api.json", *flag])
    assert exit_info.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_no_arguments_is_a_usage_error() -> None:
    with pytest.raises(SystemExit) as exit_info:
        main([])
    assert exit_info.value.code == 2


# ------------------------------------------------------------------------------------------------ never crash


def test_inputs_without_a_tool_api_fail_with_a_summary_line_and_a_trace(
    capsys: pytest.CaptureFixture[str], tmp_dirs: SimpleNamespace, brief_a_path: Path
) -> None:
    code = main([str(brief_a_path)])
    captured = capsys.readouterr()
    summary = last_json_line(captured.out)
    assert code == 2 and summary["status"] == "failed" and summary["workflow_id"] is None
    assert "No tool API file found" in summary["errors"][0] and "error:" in captured.err
    trace = Path(summary["trace"])
    assert trace.parent == tmp_dirs.traces and "BUILD SUMMARY" in trace.read_text(encoding="utf-8")


def test_a_missing_file_fails_the_same_way(
    capsys: pytest.CaptureFixture[str], tmp_dirs: SimpleNamespace
) -> None:
    code = main([str(tmp_dirs.root / "nope.md"), str(tmp_dirs.root / "nope.json")])
    summary = last_json_line(capsys.readouterr().out)
    assert code == 2 and summary["status"] == "failed" and "not found" in summary["errors"][0].lower()


def test_a_bad_budget_flag_is_reported_not_raised(
    capsys: pytest.CaptureFixture[str], tmp_dirs: SimpleNamespace, brief_a_path: Path, kit_openapi_path: Path
) -> None:
    code = main([str(brief_a_path), str(kit_openapi_path), "--budget-s", "-1", "--dry-run"])
    assert code == 2 and last_json_line(capsys.readouterr().out)["status"] == "failed"


def test_a_missing_fixture_directory_still_produces_a_summary(
    capsys: pytest.CaptureFixture[str], tmp_dirs: SimpleNamespace, brief_a_path: Path, kit_openapi_path: Path
) -> None:
    code = main(
        [str(brief_a_path), str(kit_openapi_path), "--dry-run", "--fake-llm", str(tmp_dirs.root / "empty")]
    )
    captured = capsys.readouterr()
    summary = last_json_line(captured.out)
    assert code == 2 and summary["status"] == "failed"
    trace_text = Path(summary["trace"]).read_text(encoding="utf-8")
    assert " | END    | " in trace_text and "BUILD SUMMARY" in trace_text  # the trace is finalised even so


def test_exit_code_zero_for_a_clean_dry_run(
    capsys: pytest.CaptureFixture[str], tmp_dirs: SimpleNamespace, brief_a_path: Path, kit_openapi_path: Path
) -> None:
    fixtures = paths.REPO_ROOT / "tests" / "fixtures" / "policy"
    ok = main([str(brief_a_path), str(kit_openapi_path), "--dry-run", "--fake-llm", str(fixtures)])
    assert ok == 0 and last_json_line(capsys.readouterr().out)["status"] == "dry_run"


# ------------------------------------------------------------------------------------------------ the three entry points


def run_python(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *args], cwd=cwd, capture_output=True, text=True, timeout=120, encoding="utf-8"
    )


def test_it_runs_as_a_script_from_any_directory(tmp_path: Path) -> None:
    done = run_python(str(BUILD_PY), "--help", cwd=tmp_path)
    assert done.returncode == 0, done.stderr
    assert "usage: build.py" in done.stdout and "--fake-llm" in done.stdout


def test_it_runs_as_a_module(tmp_path: Path) -> None:
    done = run_python("-m", "builder.build", "--help", cwd=paths.REPO_ROOT)
    assert done.returncode == 0, done.stderr
    assert "--dry-run" in done.stdout


def test_the_script_keeps_the_builder_directory_off_the_import_path(tmp_path: Path) -> None:
    """``builder/freya/`` must not shadow the kit's ``freya.py`` (nor ``builder/trace.py`` the stdlib ``trace``)."""
    probe = (
        "import runpy, sys\n"
        f"sys.argv = [{str(BUILD_PY)!r}, '--help']\n"
        "try:\n"
        f"    runpy.run_path({str(BUILD_PY)!r}, run_name='__main__')\n"
        "except SystemExit:\n"
        "    pass\n"
        "from pathlib import Path\n"
        f"bad = [p for p in sys.path if Path(p or '.').resolve() == Path({str(BUILD_PY.parent)!r}).resolve()]\n"
        "print('BAD' if bad else 'CLEAN')\n"
    )
    done = run_python("-c", probe, cwd=tmp_path)
    assert "CLEAN" in done.stdout, done.stdout + done.stderr


def test_the_console_script_points_at_main() -> None:
    text = (paths.REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'freya-builder = "builder.build:main"' in text
