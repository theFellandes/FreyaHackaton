"""Compliance of the whole ``builder/`` package with the hackathon rule "nothing brief-specific in the builder".

The builder must work for briefs it has never seen. Holiday dates, the bank's name, customer names, sentences of a
brief and the bank's outcome names belong in ``tests/fixtures`` and ``evals/`` only. The compiler has its own, deeper
check (``test_compile_staged.py``); this test covers every file of the package, including prompts and docstrings.

``test_builder_holds_no_brief_literals`` (AUD-58) derives the literals from the brief files themselves: the kit briefs by
default, plus every file named by the environment variable ``COMPLIANCE_BRIEFS`` (``os.pathsep``-separated paths or
globs, relative to the repository root). After the hidden briefs are published, every crash fix is checked with::

    COMPLIANCE_BRIEFS="inputs/hidden-1/*:inputs/hidden-2/*" pytest tests/unit/test_compliance.py

(``;`` instead of ``:`` on Windows). The hidden files stay where they are (``inputs/`` is git-ignored); nothing of them
is copied into the repository. The scan covers ``builder/`` and the project's own skill
(``.claude/skills/collections-workflow-patterns``; the kit's ``build-phone-agent`` skill is organiser material).
"""

from __future__ import annotations

import glob
import os
import re
from collections.abc import Iterable
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
BUILDER = REPO / "builder"
OWN_SKILLS = REPO / ".claude" / "skills" / "collections-workflow-patterns"
_SUFFIXES = {".py", ".j2", ".md", ".json", ".yaml", ".yml", ".txt"}

FORBIDDEN = [
    # holiday dates and other calendar facts of the public briefs
    r"2026-10-26", r"2026-11-11", r"2026-11-26", r"2026-12-25", r"2027-01-01",
    # names of the bank and of the practice customers
    r"Freya Bank", r"\bDaniel\b", r"\bReyes\b",
    # sentences of a brief
    r"text message confirming", r"credit card ending",
    # the card number of the public practice customer
    r"\b4821\b",
    # the bank's outcome names as constants (generic situation keys such as ``flat_refusal`` are fine)
    r"""["'](no_promise|not_verified|not_reached)["']""",
]


def _files(*roots: Path) -> list[Path]:
    return [
        path for root in roots if root.is_dir() for path in root.rglob("*")
        if path.is_file() and path.suffix in _SUFFIXES and "__pycache__" not in path.parts
    ]


def _builder_files() -> list[Path]:
    return _files(BUILDER)


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


# ---- AUD-58: literals taken from the brief files themselves --------------------------------------------------------------

_LITERAL_PATTERNS: dict[str, re.Pattern[str]] = {
    "date": re.compile(r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)"),
    "amount": re.compile(r"(?<![\d.])\d[\d,]*\.\d{2}(?!\d)"),
    "number": re.compile(r"(?<![\d.])\d{3,}(?![\d.])"),
    "time": re.compile(r"(?<!\d)\d{1,2}:\d{2}(?!\d)"),
    "quote": re.compile(r'"([^"\n]+)"'),
    "name": re.compile(r"(?<=[a-z,;:] )((?:[A-Z][a-z]+ ){1,3}[A-Z][a-z]+)\b"),
}
#: Reviewed hits, one justification each. Every entry is a literal a reviewer accepted as generic wording.
ALLOWLIST: dict[str, str] = {
    "end of the month": "generic vague-period example of the negotiate prompt (rule 3); it is live A/B prompt wording, "
                        "so it is not reworded without a live check",
    "when my salary comes": "generic vague-period example of the negotiate prompt (rule 3), as above",
    "1234": "neutral placeholder digits (an id example in the hooks' docstrings, money-parsing samples in robustness.py)",
}


def brief_literals(texts: Iterable[str]) -> set[tuple[str, str]]:
    """``(kind, literal)`` pairs the briefs state: ISO dates, decimal amounts, numbers of 3+ digits (not a bare year or an
    HTTP status), clock times not on the hour, quoted text of 4+ words and capitalised names of 2-4 words."""
    found: set[tuple[str, str]] = set()
    for text in texts:
        for kind, pattern in _LITERAL_PATTERNS.items():
            for match in pattern.finditer(text):
                token = (match.group(1) if pattern.groups else match.group(0)).strip()
                if kind == "number" and re.fullmatch(r"(?:19|20)\d\d|[1-5]\d\d", token):
                    continue
                if kind == "time" and token.endswith(":00"):
                    continue  # whole hours (09:00, 11:00) are everywhere in generic date code and docstrings
                if kind == "quote" and len(token.split()) < 4:
                    continue
                found.add((kind, token))
    return found


def scan(literals: Iterable[tuple[str, str]], files: Iterable[Path], root: Path) -> list[str]:
    """``path:line: literal`` for every whole-token occurrence of a literal that is not in :data:`ALLOWLIST`."""
    patterns: list[tuple[str, re.Pattern[str]]] = []
    for kind, token in sorted(set(literals)):
        if token in ALLOWLIST:
            continue
        numeric = kind in ("date", "amount", "number", "time")
        before, after = (r"(?<![\d.])", r"(?!\d)") if numeric else (r"(?<![\w.])", r"(?!\w)")
        patterns.append((token, re.compile(before + re.escape(token) + after)))
    hits: list[str] = []
    for path in files:
        text = path.read_text(encoding="utf-8", errors="ignore")
        for token, pattern in patterns:
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                hits.append(f"{path.relative_to(root).as_posix()}:{line}: {token}")
    return hits


def compliance_brief_files() -> list[Path]:
    """The kit briefs plus every file ``COMPLIANCE_BRIEFS`` names (paths or globs, relative to the repository)."""
    files = sorted((REPO / "briefs").glob("*.md"))
    for entry in filter(None, os.environ.get("COMPLIANCE_BRIEFS", "").split(os.pathsep)):
        pattern = entry if Path(entry).is_absolute() else str(REPO / entry)
        matched = [Path(p) for p in sorted(glob.glob(pattern)) if Path(p).is_file()]
        if not matched:
            pytest.fail(f"COMPLIANCE_BRIEFS names {entry!r}, which matches no file")
        files.extend(matched)
    return files


def test_builder_holds_no_brief_literals() -> None:
    briefs = compliance_brief_files()
    literals = brief_literals(path.read_text(encoding="utf-8", errors="ignore") for path in briefs)
    assert len(literals) > 10  # the kit briefs alone give dates, a bank name, a card number and quoted sentences
    assert scan(literals, _files(BUILDER, OWN_SKILLS), REPO) == []


def test_the_literal_scan_finds_invented_values_and_honours_the_allowlist(tmp_path: Path) -> None:
    brief = ('Holidays: 2031-02-03. Calls come from Acme Mutual Trust about card 7731, 129.50 overdue, between 08:45 '
             'and 17:00. Say "Your account is on hold today." and treat "end of the month" as vague.')
    literals = brief_literals([brief])
    assert {("date", "2031-02-03"), ("name", "Acme Mutual Trust"), ("number", "7731"), ("amount", "129.50"),
            ("time", "08:45"), ("quote", "Your account is on hold today."), ("quote", "end of the month")} <= literals
    assert ("time", "17:00") not in literals
    tree = tmp_path / "builder"
    tree.mkdir()
    (tree / "leak.py").write_text('HOLIDAYS = ["2031-02-03"]  # Acme Mutual Trust\nX = 77310\n', encoding="utf-8")
    (tree / "prompt.j2").write_text('A vague period ("end of the month") is not a day.\n', encoding="utf-8")
    assert sorted(scan(literals, _files(tree), tmp_path)) == [
        "builder/leak.py:1: 2031-02-03", "builder/leak.py:1: Acme Mutual Trust"]


def test_compliance_briefs_extends_the_scan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    hidden = tmp_path / "hidden.md"
    hidden.write_text("Holidays: 2031-02-03.", encoding="utf-8")
    monkeypatch.setenv("COMPLIANCE_BRIEFS", str(hidden))
    assert compliance_brief_files()[-1] == hidden
    monkeypatch.setenv("COMPLIANCE_BRIEFS", str(tmp_path / "nothing-*.md"))
    with pytest.raises(pytest.fail.Exception, match="matches no file"):
        compliance_brief_files()
