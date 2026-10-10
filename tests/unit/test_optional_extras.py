"""The suite must be green on a machine that lacks the optional extras (agentic, ingest, ui, obs).

The README quick start, the Docker ``dev`` image and CI hosts do not always install every extra. A test that needs one
must skip itself (or stub availability) rather than fail or abort collection. The check runs the modules that touch
each extra in a child pytest in which the package is hidden by ``tests/hide_optional_packages.py``; the child sees the
same hermetic fixtures as this process (no network, no ``.env``).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

#: ``(hidden package, extra that provides it, test modules that must still pass or skip)``.
CASES = [
    ("claude_agent_sdk", "agentic", ["tests/graph/test_agentic.py", "tests/unit/test_llm_keys.py"]),
    ("pypdf", "ingest", ["tests/unit/test_brief_loader.py"]),
    ("streamlit", "ui", ["tests/unit/test_streamlit_monitor.py", "tests/unit/test_streamlit_app.py"]),
    ("langfuse", "obs", ["tests/unit/test_observability.py"]),
]


@pytest.mark.parametrize(("package", "extra", "modules"), CASES, ids=[case[0] for case in CASES])
def test_the_modules_pass_or_skip_without_the_optional_package(
    package: str, extra: str, modules: list[str]
) -> None:
    env = {**os.environ, "HIDE_PKGS": package, "PYTHONPATH": str(REPO)}
    command = [
        sys.executable, "-m", "pytest", *modules,
        "-p", "tests.hide_optional_packages", "-p", "no:cacheprovider", "-p", "no:langsmith",
        "-o", "addopts=", "-q", "-rf",
    ]  # fmt: skip
    done = subprocess.run(
        command, cwd=REPO, env=env, capture_output=True, text=True, timeout=300, check=False
    )
    output = (done.stdout + done.stderr).strip()
    tail = "\n".join(output.splitlines()[-25:])
    # pytest exits with 5 ("no tests collected") when every module skips itself at import time: a pass here
    all_skipped = done.returncode == 5 and " skipped" in output and "error" not in output.lower()
    assert done.returncode == 0 or all_skipped, (
        f"without the '{extra}' extra ({package} hidden) the suite breaks:\n{tail}"
    )
