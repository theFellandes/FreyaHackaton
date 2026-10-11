"""Start-up checks of ``builder/build.py`` (audit REPO-08): the organisers run ``python3 builder/build.py ...``.

On a host whose ``python3`` is outside ``>=3.11,<3.14`` or lacks the dependencies the run used to die with a bare
``ImportError`` traceback: no JSON line, exit 1, no hint. It now ends with an actionable message on stderr, the usual
one-line JSON summary (``status: failed``) as the last line of stdout, and exit code 2.

The end-to-end cases run ``builder/build.py`` in a subprocess. The Python version is faked by assigning
``sys.version_info`` before the script runs as ``__main__`` (no second interpreter needed), and a broken dependency
by a shadowing package on ``PYTHONPATH``.

Safety (audit AUD-02 incident): a child process does not inherit the in-process clean-up of ``tests/conftest.py``. If a
start-up guard ever lets a run through, the child would read the developer's ``.env`` and build for real (it did once:
a live publish over an accepted slot). So every child here gets ``$FREYA_BUILDER_ENV_FILE`` pointing at a file that
does not exist, no credential variables, ``DRY_RUN=1``, and every build invocation passes ``--dry-run --fake-llm``.
``test_a_child_process_never_sees_the_developer_keys`` proves the first three.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from builder import _startup
from builder._startup import (
    PYTHON_OVERRIDE_VARIABLE,
    REEXEC_GUARD_VARIABLE,
    fallback_interpreter,
    incomplete_checkout_message,
    install_help,
    is_builder_module_missing,
    missing_dependency_message,
    python_support_problem,
    script_slot,
    start_failure_summary,
)
from builder.paths import ENV_FILE_VARIABLE

REPO_ROOT = Path(__file__).resolve().parents[2]
BUILD_PY = REPO_ROOT / "builder" / "build.py"
#: Offline arguments for every build a child runs: never a Freya call, never a model call.
OFFLINE = ("--dry-run", "--fake-llm", "tests/fixtures/policy")
#: Credential-shaped environment names a child must never inherit.
_CREDENTIAL_RE = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD)", re.IGNORECASE)


def last_json_line(text: str) -> dict:
    return json.loads([line for line in text.splitlines() if line.strip()][-1])


# ---------------------------------------------------------------------------------------------------- the pure parts


@pytest.mark.parametrize("version", [(3, 11, 0), (3, 12, 7), (3, 13, 16)])
def test_the_supported_interpreters_pass(version: tuple[int, int, int]) -> None:
    assert python_support_problem(version) is None


@pytest.mark.parametrize("version", [(3, 10, 14), (3, 9, 1), (3, 14, 0), (3, 14, 6), (3, 15, 0)])
def test_other_interpreters_get_a_message_that_names_the_version_and_the_range(
    version: tuple[int, int, int],
) -> None:
    message = python_support_problem(version)
    assert message is not None
    assert ".".join(map(str, version)) in message
    assert "3.11" in message and "3.13" in message


def test_the_current_interpreter_is_judged_by_default() -> None:
    expected = (3, 11) <= sys.version_info[:2] < (3, 14)
    assert (python_support_problem() is None) is expected


def test_the_help_names_the_exact_install_commands() -> None:
    text = install_help(python="/opt/py/bin/python")
    assert "uv sync --extra agentic --extra ingest" in text
    assert "uv run python builder/build.py" in text
    assert "/opt/py/bin/python -m pip install -r requirements.txt" in text
    assert "for the interpreter that just ran" not in install_help(current_interpreter=False)
    assert "docker compose run --rm builder" in text
    assert "3.13" in text


def test_a_missing_package_is_named_with_the_interpreter() -> None:
    message = missing_dependency_message(ModuleNotFoundError("No module named 'pydantic'", name="pydantic"))
    assert "pydantic" in message and sys.executable in message
    assert "requirements.txt" in install_help()


@pytest.mark.parametrize(
    ("argv", "slot"),
    [
        (["briefs/brief-a.md", "tools/openapi.json"], "brief-a"),
        (["--dry-run", "briefs/x.md", "api.json", "--track", "compiled"], "x"),
        (["--slot", "mine", "briefs/x.md"], "mine"),
        (["--budget-s", "30", "--fake-llm", "fx", "dir/brief-b.md"], "brief-b"),
        (["--dry-run"], "inputs"),
        ([], "inputs"),
    ],
)
def test_the_slot_of_a_failed_start_is_guessed_from_the_arguments(argv: list[str], slot: str) -> None:
    assert script_slot(argv) == slot


def test_the_summary_has_the_contract_keys_and_a_fix() -> None:
    summary = start_failure_summary("the interpreter is wrong", ["briefs/brief-a.md"])
    assert summary["status"] == "failed" and summary["slot"] == "brief-a"
    assert {"slot", "track", "workflow_id", "agent_id", "version", "exported", "trace", "status", "errors"} <= set(
        summary
    )
    assert summary["workflow_id"] is None and summary["exported"] is None
    assert summary["errors"][0] == "the interpreter is wrong"
    assert any("uv sync --extra agentic --extra ingest" in error for error in summary["errors"])
    json.dumps(summary)  # serialisable


# ---------------------------------------------------------------------------------------------------- end to end


def child_environment(env: dict[str, str] | None = None) -> dict[str, str]:
    """The environment of a child process: no credentials, no dotenv file, a dry run, no hand-over (unless ``env``)."""
    environment = {
        name: value
        for name, value in os.environ.items()
        if not _CREDENTIAL_RE.search(name) and not name.upper().startswith(("FREYA_", "LLM_", "LANGFUSE_"))
    }
    environment.update(
        {
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            ENV_FILE_VARIABLE: str(REPO_ROOT / ".env.disabled-in-unit-tests"),  # never created
            "DRY_RUN": "1",
            REEXEC_GUARD_VARIABLE: "1",  # pin the no-hand-over path; hand-over tests remove it explicitly
        }
    )
    for name, value in (env or {}).items():
        if value is None:
            environment.pop(name, None)
        else:
            environment[name] = value
    return environment


def run_python(code: str, env: dict[str, str] | None = None, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=child_environment(env),
        cwd=str(cwd or REPO_ROOT),
        timeout=180,
    )


def run_script(
    *args: str, prelude: str = "", env: dict[str, str] | None = None, cwd: Path | None = None, script: Path = BUILD_PY
) -> subprocess.CompletedProcess[str]:
    """Run ``builder/build.py`` as ``__main__`` in a fresh interpreter, after ``prelude`` (Python code)."""
    code = (
        "import runpy, sys\n"
        f"{prelude}\n"
        f"sys.argv = [{str(script)!r}, *{list(args)!r}]\n"
        f"runpy.run_path({str(script)!r}, run_name='__main__')\n"
    )
    return run_python(code, env=env, cwd=cwd)


def test_a_child_process_never_sees_the_developer_keys() -> None:
    """The net under every subprocess test: even with the real ``.env`` present, a child resolves no key and dry-runs."""
    result = run_python(
        "from builder.settings import Settings\n"
        "s = Settings()\n"
        "print(s.freya_api_key is None, s.freya_bank_team_key is None, s.dry_run)\n"
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["True", "True", "True"]


def test_an_unsupported_python_ends_with_a_message_a_json_line_and_exit_2() -> None:
    result = run_script(
        "briefs/brief-a.md",
        "tools/openapi.json",
        *OFFLINE,
        prelude="sys.version_info = (3, 15, 6, 'final', 0)",
    )
    assert result.returncode == 2, result.stderr
    assert "Traceback" not in result.stderr
    assert "3.15.6" in result.stderr and "3.11" in result.stderr and "uv sync --extra agentic --extra ingest" in result.stderr
    assert "for the interpreter that just ran" not in result.stderr  # pip cannot fix a Python that is too new
    summary = last_json_line(result.stdout)
    assert summary["status"] == "failed" and summary["workflow_id"] is None and summary["slot"] == "brief-a"
    assert "3.15.6" in summary["errors"][0]
    assert "re-running" not in result.stderr  # the loop guard of a handed-over run is respected


def test_an_unsupported_python_hands_the_run_over_to_a_supported_interpreter() -> None:
    """AUD-02: ``python3`` too new, a supported interpreter available -> the same command runs there (exit code 0)."""
    result = run_script(
        "--help",
        prelude="sys.version_info = (3, 15, 6, 'final', 0)",
        env={REEXEC_GUARD_VARIABLE: None, PYTHON_OVERRIDE_VARIABLE: sys.executable},
    )
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout.lower()
    assert "re-running with" in result.stderr


def test_a_handed_over_build_keeps_the_json_line_last_and_the_childs_exit_code(tmp_path: Path) -> None:
    slot = "v-startup-handover"
    try:
        result = run_script(
            "briefs/brief-a.md",
            "tools/openapi.json",
            *OFFLINE,
            "--slot",
            slot,
            prelude="sys.version_info = (3, 15, 6, 'final', 0)",
            env={REEXEC_GUARD_VARIABLE: None, PYTHON_OVERRIDE_VARIABLE: sys.executable},
        )
        assert result.returncode == 0, result.stderr[-2000:]
        summary = last_json_line(result.stdout)
        assert summary["status"] == "dry_run" and summary["slot"] == slot
    finally:
        for folder, pattern in (("out", f"{slot}.*"), ("results", f"{slot}-*"), ("traces", f"{slot}-*")):
            for leftover in (REPO_ROOT / folder).glob(pattern):
                leftover.unlink(missing_ok=True)


def test_the_hand_over_is_refused_inside_a_handed_over_run() -> None:
    result = run_script(
        "briefs/brief-a.md",
        "tools/openapi.json",
        *OFFLINE,
        prelude="sys.version_info = (3, 15, 6, 'final', 0)",
        env={PYTHON_OVERRIDE_VARIABLE: sys.executable},  # the loop guard is set by child_environment
    )
    assert result.returncode == 2 and "re-running" not in result.stderr
    assert last_json_line(result.stdout)["status"] == "failed"


def test_the_fallback_interpreter_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert fallback_interpreter({REEXEC_GUARD_VARIABLE: "1", PYTHON_OVERRIDE_VARIABLE: "/x/python"}, tmp_path) is None
    assert fallback_interpreter({PYTHON_OVERRIDE_VARIABLE: "/x/python"}, tmp_path) == ["/x/python"]
    assert fallback_interpreter({}, tmp_path) is None  # no .venv, no uv
    venv = tmp_path / ".venv"
    python = venv / ("Scripts" if os.name == "nt" else "bin") / ("python.exe" if os.name == "nt" else "python")
    python.parent.mkdir(parents=True)
    python.write_text("")
    (venv / "pyvenv.cfg").write_text("home = /usr\nversion_info = 3.13.16\n")
    assert fallback_interpreter({}, tmp_path) == [str(python)]
    (venv / "pyvenv.cfg").write_text("home = /usr\nversion_info = 3.99.0\n")  # an unsupported .venv is skipped
    assert fallback_interpreter({}, tmp_path) is None
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    monkeypatch.setattr(shutil, "which", lambda name: "/bin/uv" if name == "uv" else None)
    command = fallback_interpreter({}, tmp_path)
    assert command is not None and command[:4] == ["/bin/uv", "run", "--locked", "--extra"] and command[-1] == "python"


def test_a_missing_builder_module_is_an_incomplete_checkout_not_a_package() -> None:
    missing = ModuleNotFoundError("No module named 'builder.freya.transport'", name="builder.freya.transport")
    assert is_builder_module_missing(missing)
    assert not is_builder_module_missing(ModuleNotFoundError("No module named 'pydantic'", name="pydantic"))
    message = incomplete_checkout_message(missing)
    assert "incomplete" in message and "builder.freya.transport" in message and "pip install" not in message


@pytest.mark.parametrize("removed", ["_startup.py", "freya/transport.py"])
def test_an_incomplete_checkout_ends_with_a_json_line_and_exit_2(tmp_path: Path, removed: str) -> None:
    """AUD-03: a commit that leaves out a builder module must not end in a bare traceback (exit 1, no JSON line)."""
    copy = tmp_path / "repo"
    shutil.copytree(REPO_ROOT / "builder", copy / "builder", ignore=shutil.ignore_patterns("__pycache__"))
    (copy / "builder" / removed).unlink()
    script = copy / "builder" / "build.py"
    result = run_script(
        str(REPO_ROOT / "briefs" / "brief-a.md"),
        str(REPO_ROOT / "tools" / "openapi.json"),
        "--dry-run",
        cwd=tmp_path,
        script=script,
    )
    assert result.returncode == 2, result.stderr[-2000:]
    assert "Traceback" not in result.stderr
    summary = last_json_line(result.stdout)
    assert summary["status"] == "failed" and summary["workflow_id"] is None
    assert "incomplete" in summary["errors"][0]
    assert not any("required Python package" in error for error in summary["errors"])


def test_a_python_that_is_too_old_is_refused_before_any_modern_syntax_runs() -> None:
    result = run_script("a.md", "t.json", *OFFLINE, prelude="sys.version_info = (3, 10, 4, 'final', 0)")
    assert result.returncode == 2 and "3.10.4" in result.stderr
    assert last_json_line(result.stdout)["status"] == "failed"


def test_a_missing_dependency_ends_with_install_commands_a_json_line_and_exit_2(tmp_path: Path) -> None:
    broken = tmp_path / "shadow" / "pydantic"
    broken.mkdir(parents=True)
    (broken / "__init__.py").write_text("raise ImportError(\"No module named 'pydantic_core'\")\n")
    result = run_script(
        "briefs/brief-a.md",
        "tools/openapi.json",
        *OFFLINE,
        env={"PYTHONPATH": str(tmp_path / "shadow")},
    )
    assert result.returncode == 2, result.stderr
    assert "Traceback" not in result.stderr
    assert "pydantic_core" in result.stderr
    assert "uv sync --extra agentic --extra ingest" in result.stderr
    assert f"{sys.executable} -m pip install -r requirements.txt" in result.stderr
    assert "docker compose run --rm builder" in result.stderr
    summary = last_json_line(result.stdout)
    assert summary["status"] == "failed" and summary["workflow_id"] is None and summary["slot"] == "brief-a"
    assert "pydantic_core" in summary["errors"][0]


def test_a_healthy_environment_is_not_disturbed() -> None:
    result = run_script("--help")
    assert result.returncode == 0 and "usage:" in result.stdout.lower()


def test_requirements_txt_pins_the_runtime_for_pip_users() -> None:
    text = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8")
    names = {line.split("==")[0].split(" ")[0].strip().lower() for line in text.splitlines() if "==" in line}
    assert {"langgraph", "pydantic", "pydantic-settings", "jinja2", "pyyaml", "openai", "anthropic"} <= names
    assert "claude-agent-sdk" in names and "pypdf" in names  # the agentic and ingest extras
    assert "pytest" not in names and "ruff" not in names  # no dev tools
    assert "streamlit" not in names and "langfuse" not in names  # the optional extras stay optional
    assert all("--hash" not in line for line in text.splitlines())  # plain pins, readable with any pip


def test_module_has_no_third_party_imports() -> None:
    """``_startup`` must run on an interpreter that has none of the dependencies."""
    source = Path(_startup.__file__).read_text(encoding="utf-8")
    imported = {
        line.split()[1].split(".")[0]
        for line in source.splitlines()
        if line.startswith(("import ", "from ")) and "__future__" not in line
    }
    assert imported <= set(sys.stdlib_module_names)


def test_the_submission_checker_reaches_the_guard_before_any_dependency(tmp_path: Path) -> None:
    """AUD-02: ``python -m builder.submission`` on an interpreter without the dependencies used to end in a traceback."""
    broken = tmp_path / "shadow" / "pydantic"
    broken.mkdir(parents=True)
    (broken / "__init__.py").write_text("raise ImportError(\"No module named 'pydantic_core'\")\n")
    result = subprocess.run(
        [sys.executable, "-m", "builder.submission", "check", "submission/does-not-exist.json"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=child_environment({"PYTHONPATH": f"{tmp_path / 'shadow'}{os.pathsep}{REPO_ROOT}"}),
        cwd=str(REPO_ROOT),
        timeout=120,
    )
    assert result.returncode == 2, result.stderr
    assert "Traceback" not in result.stderr and "pydantic_core" in result.stderr
    assert "uv sync --extra agentic --extra ingest" in result.stderr
    assert last_json_line(result.stdout)["ready"] is False
