"""Well-known filesystem locations of the builder.

Every other module resolves files through these constants instead of the current working directory, so the builder
behaves the same whether it is started as ``python builder/build.py`` from the repository root, as
``python -m builder.build`` from somewhere else, or inside the Docker image (where the repository lives in ``/app``).

Layout (relative to :data:`REPO_ROOT`)::

    builder/            the Python package (BUILDER_DIR)
    builder/prompts/    Jinja2 prompt templates (PROMPTS_DIR)
    out/                exported workflows and other build products (OUT_DIR)
    traces/             run traces, one .log + .jsonl pair per run (TRACES_DIR)
    results/            self-test and practice-run results (RESULTS_DIR)
    data/               local runtime data such as the LLM key health file (DATA_DIR)
    tools/              the hackathon kit's tools (openapi.json, run_cases.py, score.py) - never edited (KIT_TOOLS_DIR)

Tests that need isolated output directories should monkeypatch the attributes of this module (the shared
``tmp_dirs`` fixture in ``tests/conftest.py`` does that); code that wants to honour such patches reads the attribute
at call time (``paths.OUT_DIR``) rather than binding it at import time.
"""

from __future__ import annotations

from pathlib import Path

#: The repository root (the directory that holds ``builder/``, ``tools/`` and the kit's ``freya.py``).
REPO_ROOT: Path = Path(__file__).resolve().parent.parent
#: The ``builder`` package directory.
BUILDER_DIR: Path = REPO_ROOT / "builder"
#: Jinja2 prompt templates used by the LLM stages and the compiler.
PROMPTS_DIR: Path = BUILDER_DIR / "prompts"
#: Build products: exported workflow JSON files and intermediate artefacts.
OUT_DIR: Path = REPO_ROOT / "out"
#: Run traces (``<slot>-<UTC timestamp>.log`` and ``.jsonl``).
TRACES_DIR: Path = REPO_ROOT / "traces"
#: Self-test and practice-run results.
RESULTS_DIR: Path = REPO_ROOT / "results"
#: Local runtime data (for example the LLM key health file).
DATA_DIR: Path = REPO_ROOT / "data"
#: The hackathon kit's ``tools/`` directory (reference material, never edited).
KIT_TOOLS_DIR: Path = REPO_ROOT / "tools"
#: The ``.env`` file read by :mod:`builder.settings`.
ENV_FILE: Path = REPO_ROOT / ".env"


def output_dirs() -> tuple[Path, ...]:
    """Return the directories the builder writes to, read from this module at call time.

    Module globals are looked up when the function runs, so monkeypatching ``builder.paths.OUT_DIR`` (and friends)
    in a test redirects the result.
    """
    return (OUT_DIR, TRACES_DIR, RESULTS_DIR, DATA_DIR)


def ensure_dirs(*extra: Path) -> list[Path]:
    """Create the builder's output directories (and any ``extra`` ones) if they do not exist yet.

    Args:
        *extra: Additional directories to create alongside ``out/``, ``traces/``, ``results/`` and ``data/``.

    Returns:
        The directories that exist after the call, in creation order (duplicates removed).
    """
    created: list[Path] = []
    for directory in (*output_dirs(), *extra):
        path = Path(directory)
        if path in created:
            continue
        path.mkdir(parents=True, exist_ok=True)
        created.append(path)
    return created


__all__ = [
    "BUILDER_DIR",
    "DATA_DIR",
    "ENV_FILE",
    "KIT_TOOLS_DIR",
    "OUT_DIR",
    "PROMPTS_DIR",
    "REPO_ROOT",
    "RESULTS_DIR",
    "TRACES_DIR",
    "ensure_dirs",
    "output_dirs",
]
