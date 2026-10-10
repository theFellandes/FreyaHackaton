"""Repository hygiene: what git and Docker pick up, and lint of the dev tools.

* ``.gitignore`` must keep the hackathon deliverables in (the human-readable ``traces/<slot>-<time>.log`` and its
  ``.jsonl``) and the private material out (full model transcripts, the key-health file, secrets). The generic
  ``*.log`` rule once swallowed the trace log while the transcripts and ``data/key_health.json`` were not ignored.
* ``.dockerignore`` must keep local scratch folders out of the build context (a ``.cache`` of audit data once made
  the context 340 MB and shipped it inside the runtime image).
* ``ruff`` must be clean on the dev tools in ``evals/*.py`` (an unused ``noqa`` once made ``ruff check builder tests evals`` fail).

Git is only asked read-only questions (``git check-ignore``); without a ``.git`` directory (the Docker image) or
without a git binary the git checks skip.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

needs_git = pytest.mark.skipif(
    shutil.which("git") is None or not (REPO / ".git").exists(), reason="needs a git checkout"
)


def git_ignores(path: str) -> bool:
    """Whether ``.gitignore`` ignores ``path`` (the file does not have to exist; the index is not consulted)."""
    done = subprocess.run(
        ["git", "check-ignore", "-q", "--no-index", "--", path],
        cwd=REPO, capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert done.returncode in (0, 1), done.stderr
    return done.returncode == 0


@needs_git
@pytest.mark.parametrize(
    "path",
    [
        "traces/brief-a-20261010T192451Z.log",  # the human-readable trace the README tells you to submit
        "traces/brief-a-20261010T192451Z.jsonl",  # its machine-readable twin
        "out/brief-a.workflow.json",
    ],
)
def test_the_submission_artifacts_are_not_ignored(path: str) -> None:
    assert not git_ignores(path)


@needs_git
@pytest.mark.parametrize(
    "path",
    [
        "traces/brief-a-20261010T192451Z.llm.jsonl",  # full prompts (the hidden briefs) and model replies
        "traces/brief-a-20261010T192451Z.agentic.jsonl",  # the whole agentic session
        "data/key_health.json",  # key fingerprints: belongs to the host that wrote it
        ".env",
        "env.sh",
        ".cache/anything.json",
    ],
)
def test_private_and_local_files_are_ignored(path: str) -> None:
    assert git_ignores(path)


def dockerignore_entries() -> set[str]:
    lines = (REPO / ".dockerignore").read_text(encoding="utf-8").splitlines()
    return {line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")}


@pytest.mark.skipif(not (REPO / ".dockerignore").exists(), reason="no .dockerignore in this checkout")
def test_the_docker_build_context_leaves_out_local_scratch_and_secrets() -> None:
    entries = dockerignore_entries()
    assert {".cache", ".env", ".venv*", "traces", "data/key_health.json"} <= entries


@pytest.mark.skipif(
    importlib.util.find_spec("ruff") is None or not (REPO / "evals").is_dir(), reason="needs ruff and evals/"
)
def test_ruff_is_clean_on_the_eval_tools() -> None:
    tools = sorted(str(path) for path in (REPO / "evals").glob("*.py"))
    done = subprocess.run(
        [sys.executable, "-m", "ruff", "check", *tools, "--no-cache", "--output-format", "concise"],
        cwd=REPO, capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert done.returncode == 0, done.stdout + done.stderr
