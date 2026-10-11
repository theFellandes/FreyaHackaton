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
    assert settings.builder_track == "compiled" and not settings.dry_run  # the original is untouched


def test_defaults_come_from_the_settings(settings: Any) -> None:
    args = build_parser().parse_args(["b.md", "api.json"])
    assert settings_from_args(args, settings) is settings
    assert args.slot is None and args.fake_llm is None and not args.dry_run
    assert args.max_policy_repairs is None


def test_strong_model_flag_overrides_only_its_setting(settings: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_STRONG_MODEL", "environment-model")
    base = settings.model_copy(update={"openai_strong_model": "configured-model"})
    args = build_parser().parse_args(["b.md", "api.json", "--openai-strong-model", " gpt-6.1-sol "])
    effective = settings_from_args(args, base)
    assert effective.openai_strong_model == "gpt-6.1-sol"
    assert base.openai_strong_model == "configured-model"
    assert effective.model_dump(exclude={"openai_strong_model"}) == base.model_dump(exclude={"openai_strong_model"})
    assert settings_from_args(build_parser().parse_args(["b.md", "api.json"]), base) is base
    import os

    assert os.environ["OPENAI_STRONG_MODEL"] == "environment-model"


@pytest.mark.parametrize("value", ["", " \t "])
def test_strong_model_flag_rejects_blank_values(settings: Any, value: str) -> None:
    args = build_parser().parse_args(["b.md", "api.json", "--openai-strong-model", value])
    with pytest.raises(InputError, match="nonblank model name"):
        settings_from_args(args, settings)


@pytest.mark.parametrize("limit", [0, 4])
def test_policy_repair_flag_overrides_only_its_existing_setting(settings: Any, limit: int) -> None:
    base = settings.model_copy(update={"max_policy_repairs": 7})
    args = build_parser().parse_args(["b.md", "api.json", "--max-policy-repairs", str(limit)])
    effective = settings_from_args(args, base)
    assert effective.max_policy_repairs == limit and base.max_policy_repairs == 7
    assert effective.max_compile_repairs == base.max_compile_repairs
    assert effective.build_time_budget_s == base.build_time_budget_s
    assert settings_from_args(build_parser().parse_args(["b.md", "api.json"]), base) is base


def test_negative_policy_repair_flag_is_reported_without_starting_the_builder(
    capsys: pytest.CaptureFixture[str], tmp_dirs: SimpleNamespace, brief_a_path: Path, kit_openapi_path: Path,
) -> None:
    code = main([str(brief_a_path), str(kit_openapi_path), "--max-policy-repairs", "-1", "--dry-run"])
    summary = last_json_line(capsys.readouterr().out)
    assert code == 2 and summary["status"] == "failed"
    assert "--max-policy-repairs must be a non-negative integer" in summary["errors"][0]


@pytest.mark.parametrize("value", ["2.5", "many"])
def test_policy_repair_flag_requires_an_integer(value: str) -> None:
    with pytest.raises(SystemExit) as exit_info:
        build_parser().parse_args(["b.md", "api.json", "--max-policy-repairs", value])
    assert exit_info.value.code == 2


def test_the_bare_command_reproduces_the_accepted_live_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AUD-01: ``python3 builder/build.py {brief} {tool_api}`` runs compiled + smoke (the only configuration measured
    live); auto, agentic, walkthrough and practice stay reachable as explicit opt-ins."""
    from builder.settings import Settings

    for name in ("BUILDER_TRACK", "SELF_TEST"):
        monkeypatch.delenv(name, raising=False)
    base = Settings(_env_file=None)
    effective = settings_from_args(build_parser().parse_args(["b.md", "api.json"]), base)
    assert (effective.builder_track, effective.self_test) == ("compiled", "smoke")
    for track in ("auto", "agentic", "compiled"):
        assert Settings(_env_file=None, builder_track=track).builder_track == track
    for mode in ("auto", "walkthrough", "practice", "none", "smoke"):
        assert Settings(_env_file=None, self_test=mode).self_test == mode


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


def test_an_invalid_environment_value_is_reported_not_raised(
    capsys: pytest.CaptureFixture[str],
    tmp_dirs: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    brief_a_path: Path,
    kit_openapi_path: Path,
) -> None:
    """A bad value in the environment / .env used to end in a bare pydantic traceback with no summary line (REPO-12)."""
    from builder.settings import get_settings

    monkeypatch.setenv("BUILD_TIME_BUDGET_S", "not-a-number-1234567890")
    get_settings.cache_clear()
    code = main([str(brief_a_path), str(kit_openapi_path), "--dry-run"])
    captured = capsys.readouterr()
    summary = last_json_line(captured.out)
    assert code == 2 and summary["status"] == "failed" and summary["workflow_id"] is None
    assert "build_time_budget_s" in summary["errors"][0] and "invalid configuration" in summary["errors"][0]
    assert "not-a-number-1234567890" not in captured.out + captured.err  # the value itself is never echoed
    assert Path(summary["trace"]).parent == tmp_dirs.traces


def test_an_unwritable_trace_directory_still_produces_a_summary(
    capsys: pytest.CaptureFixture[str], tmp_dirs: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, brief_a_path: Path
) -> None:
    def broken() -> None:
        raise PermissionError("read-only file system")

    monkeypatch.setattr(paths, "ensure_dirs", broken)
    code = main([str(brief_a_path)])  # no tool API: the failure path writes a trace, which cannot be written here
    summary = last_json_line(capsys.readouterr().out)
    assert code == 2 and summary["status"] == "failed"
    assert "no trace written" in summary["errors"][0]


def test_the_summary_line_never_carries_a_secret(
    capsys: pytest.CaptureFixture[str],
    tmp_dirs: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    brief_a_path: Path,
    kit_openapi_path: Path,
) -> None:
    """Error texts can quote a request; the printed JSON line is redacted like the trace files (audit P-08)."""
    from builder.settings import get_settings

    key = "fak_" + "Zq7Lm2Xa9Rt4Vb6Nc8Hd3"
    monkeypatch.setenv("FREYA_API_KEY", key)
    get_settings.cache_clear()

    async def leaky(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"slot": "s", "status": "failed", "errors": [f"deploy: 401 for Authorization: Bearer {key}"]}

    monkeypatch.setattr("builder.build.run_build", leaky)
    code = main([str(brief_a_path), str(kit_openapi_path), "--dry-run"])
    out = capsys.readouterr().out
    assert code == 2 and key not in out
    assert "***" in last_json_line(out)["errors"][0]


@pytest.mark.parametrize("error", [PermissionError("cannot read .env"), OSError("disk error"), RuntimeError("boom")])
def test_any_error_while_reading_settings_or_inputs_still_ends_with_a_summary_line(
    capsys: pytest.CaptureFixture[str],
    tmp_dirs: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    brief_a_path: Path,
    kit_openapi_path: Path,
    error: Exception,
) -> None:
    """Whatever goes wrong before the build starts - an unreadable ``.env``, an input that cannot be read, a bug - the
    last line of stdout is the JSON summary and the exit code is 2; never a bare traceback (REPO-12, P-20)."""

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise error

    monkeypatch.setattr("builder.build.classify_inputs", broken)
    code = main([str(brief_a_path), str(kit_openapi_path), "--dry-run"])
    captured = capsys.readouterr()
    summary = last_json_line(captured.out)
    assert code == 2 and summary["status"] == "failed" and summary["workflow_id"] is None
    assert str(error) in summary["errors"][0] or type(error).__name__ in summary["errors"][0]
    assert "Traceback" not in captured.err


def test_an_unreadable_settings_source_still_ends_with_a_summary_line(
    capsys: pytest.CaptureFixture[str],
    tmp_dirs: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    brief_a_path: Path,
    kit_openapi_path: Path,
) -> None:
    def broken(*args: Any, **kwargs: Any) -> Any:
        raise PermissionError("[Errno 13] Permission denied: '.env'")

    monkeypatch.setattr("builder.build.get_settings", broken)
    code = main([str(brief_a_path), str(kit_openapi_path), "--dry-run"])
    summary = last_json_line(capsys.readouterr().out)
    assert code == 2 and summary["status"] == "failed" and "Permission denied" in summary["errors"][0]


@pytest.mark.parametrize("broken_part", ["ensure_dirs", "trace_file"])
def test_an_unwritable_trace_directory_with_valid_inputs_still_produces_a_summary(
    capsys: pytest.CaptureFixture[str],
    tmp_dirs: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    brief_a_path: Path,
    kit_openapi_path: Path,
    broken_part: str,
) -> None:
    """The inputs are fine, the build starts, and the trace folder cannot be created or its file opened (P-20):
    no traceback, the summary line, exit 2."""

    def broken(*args: Any, **kwargs: Any) -> None:
        raise PermissionError("read-only file system")

    if broken_part == "ensure_dirs":
        monkeypatch.setattr(paths, "ensure_dirs", broken)
    else:
        monkeypatch.setattr("builder.build.TraceWriter", broken)
    fixtures = paths.REPO_ROOT / "tests" / "fixtures" / "policy"
    code = main([str(brief_a_path), str(kit_openapi_path), "--dry-run", "--fake-llm", str(fixtures)])
    captured = capsys.readouterr()
    summary = last_json_line(captured.out)
    assert code == 2 and summary["status"] == "failed" and summary["workflow_id"] is None
    assert "could not start" in summary["errors"][0] and "PermissionError" in summary["errors"][0]
    assert "Traceback" not in captured.err


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


# ------------------------------------------------------------------------------------------------ AUD-55


def test_a_missing_freya_key_without_dry_run_exits_2_and_says_why(
    capsys: pytest.CaptureFixture[str], tmp_dirs: SimpleNamespace, brief_a_path: Path, kit_openapi_path: Path
) -> None:
    """A real build that could not deploy (no key) is still built as a dry run, but it is not a success."""
    fixtures = paths.REPO_ROOT / "tests" / "fixtures" / "policy"
    code = main([str(brief_a_path), str(kit_openapi_path), "--fake-llm", str(fixtures), "--slot", "t-nokey"])
    summary = last_json_line(capsys.readouterr().out)
    assert code == 2
    assert summary["status"] == "dry_run" and summary["slot"] == "t-nokey"
    assert any("FREYA_API_KEY" in error for error in summary["errors"])
    assert summary.get("dry_run_requested") is False


def test_a_dry_run_asked_for_in_the_environment_still_exits_0(
    capsys: pytest.CaptureFixture[str],
    tmp_dirs: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    brief_a_path: Path,
    kit_openapi_path: Path,
) -> None:
    from builder.settings import get_settings

    monkeypatch.setenv("DRY_RUN", "true")
    get_settings.cache_clear()
    fixtures = paths.REPO_ROOT / "tests" / "fixtures" / "policy"
    code = main([str(brief_a_path), str(kit_openapi_path), "--fake-llm", str(fixtures)])
    summary = last_json_line(capsys.readouterr().out)
    assert code == 0 and summary["status"] == "dry_run"
    assert not any("FREYA_API_KEY" in error for error in summary["errors"])
    get_settings.cache_clear()


def test_a_configuration_error_keeps_the_explicit_slot(
    capsys: pytest.CaptureFixture[str],
    tmp_dirs: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    brief_a_path: Path,
    kit_openapi_path: Path,
) -> None:
    from builder.settings import get_settings

    monkeypatch.setenv("LLM_MAX_CONCURRENCY", "abc")
    get_settings.cache_clear()
    code = main([str(brief_a_path), str(kit_openapi_path), "--dry-run", "--slot", "t-cfg"])
    summary = last_json_line(capsys.readouterr().out)
    assert code == 2 and summary["status"] == "failed" and summary["slot"] == "t-cfg"
    assert Path(summary["trace"]).name.startswith("t-cfg-")
    get_settings.cache_clear()


def test_an_unsafe_slot_on_a_configuration_error_falls_back_to_inputs(
    capsys: pytest.CaptureFixture[str],
    tmp_dirs: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    brief_a_path: Path,
    kit_openapi_path: Path,
) -> None:
    from builder.settings import get_settings

    monkeypatch.setenv("LLM_MAX_CONCURRENCY", "abc")
    get_settings.cache_clear()
    code = main([str(brief_a_path), str(kit_openapi_path), "--dry-run", "--slot", "../evil"])
    summary = last_json_line(capsys.readouterr().out)
    assert code == 2 and summary["slot"] == "inputs"
    assert Path(summary["trace"]).parent == tmp_dirs.traces
    get_settings.cache_clear()


# ------------------------------------------------------------------------------------------------ A11


def test_log_output_of_the_cli_masks_secrets() -> None:
    """Python's last-resort handler printed warnings unmasked; the CLI's own handler masks them (tracebacks too)."""
    import io
    import logging

    from builder.build import install_log_redaction
    from builder.trace import _Redactor

    secret = "synthetic-secret-value-0123456789"
    root = logging.Logger("isolated-root")
    redactor = _Redactor()
    handler = install_log_redaction(redactor, root)
    assert handler is not None
    stream = io.StringIO()
    handler.setStream(stream)  # type: ignore[attr-defined]
    redactor.add([secret])
    root.warning("401 for key %s", secret)
    try:
        raise RuntimeError(f"request failed with {secret}")
    except RuntimeError:
        root.exception("crash")
    root.info("not shown %s", secret)
    text = stream.getvalue()
    assert secret not in text and "***" in text and "Traceback" in text and "not shown" not in text
    assert install_log_redaction(redactor, root) is None  # never stacks a second handler
