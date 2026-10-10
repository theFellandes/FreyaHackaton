"""Compliance of the whole ``builder/`` package with the hackathon rule "nothing brief-specific in the builder".

The builder must work for briefs it has never seen. Holiday dates, the bank's name, customer names, sentences of a
brief and the bank's outcome names belong in ``tests/fixtures`` and ``evals/`` only. The compiler has its own, deeper
check (``test_compile_staged.py``); this test covers every file of the package, including prompts and docstrings.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BUILDER = REPO / "builder"

FORBIDDEN = [
    # holiday dates and other calendar facts of the public briefs
    r"2026-10-26", r"2026-11-11", r"2026-11-26", r"2026-12-25", r"2027-01-01",
    # names of the bank and of the practice customers
    r"Freya Bank", r"\bDaniel\b", r"\bReyes\b",
    # sentences of a brief
    r"text message confirming", r"credit card ending",
    # the bank's outcome names as constants (generic situation keys such as ``flat_refusal`` are fine)
    r"""["'](no_promise|not_verified|not_reached)["']""",
]


def _builder_files() -> list[Path]:
    return [
        path for path in BUILDER.rglob("*")
        if path.is_file() and path.suffix in {".py", ".j2", ".md", ".json", ".yaml", ".yml", ".txt"}
        and "__pycache__" not in path.parts
    ]


def test_builder_holds_no_brief_specific_values() -> None:
    files = _builder_files()
    assert len(files) > 50  # the scan really saw the package
    offenders: list[str] = []
    for path in files:
        text = path.read_text(encoding="utf-8", errors="ignore")
        for pattern in FORBIDDEN:
            for match in re.finditer(pattern, text):
                offenders.append(f"{path.relative_to(REPO)}: {match.group(0)}")
    assert offenders == []
