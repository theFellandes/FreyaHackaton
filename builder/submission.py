"""Prepare and check a local final-submission draft without contacting Freya or changing Git.

The draft has the six form fields plus a local-only ``build_results`` map. ``workflows``, ``traces`` and
``build_results`` use exactly ``hidden-1`` and ``hidden-2`` as keys, with repository-relative file paths as values.
Only ``payload`` emits the six form fields, after checks pass. These checks establish local artifact consistency,
not customer success, current platform publication, complete secret redaction or acceptance by the organizers.

Run from the repository: ``python -m builder.submission check submission/draft.json``. See ``--help`` for preparation.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from builder._startup import check_python, missing_dependency_message, start_failure  # standard library only

#: What ``python -m builder.submission`` prints when it cannot start (its own report shape, not a build summary).
_START_FAILURE = {"ready": False, "issues": [{"field": "interpreter", "message": "see the error on stderr"}]}

if __name__ == "__main__":  # audit AUD-02: hand an unsupported interpreter over (or say so) before any dependency
    check_python(rerun=["-m", "builder.submission"], summary=_START_FAILURE)

try:
    from builder import paths
    from builder.freya.client import count_nodes, count_tools
except ImportError as _import_error:
    if __name__ != "__main__":
        raise
    start_failure(
        missing_dependency_message(_import_error), rerun=["-m", "builder.submission"], summary=_START_FAILURE
    )

BRIEFS = ("hidden-1", "hidden-2")
FORM_FIELDS = ("team", "repo", "commit", "run_command", "workflows", "traces")
LOCAL_FIELDS = (*FORM_FIELDS, "build_results")
MAX_FILE_BYTES = 32 * 1024 * 1024
CODE_PATHS = (
    "builder", ".claude/skills", "tests", "pyproject.toml", "uv.lock", ".python-version", "freya.py",
    "tools", "examples", "briefs", "public-test-set.json", "docs/api.md", "docs/workflow-format.md",
    "docs/tools-and-code.md", "Dockerfile", "docker-compose.yml", ".dockerignore",
    "requirements.txt", "README.md", ".gitignore",
)
#: Files without which a clean checkout of the commit cannot even start (``_startup`` is imported first; pip users
#: install from ``requirements.txt``) - audit AUD-03.
REQUIRED_CODE = (
    "builder/build.py", "builder/_startup.py", "freya.py", "pyproject.toml", "uv.lock", "requirements.txt",
)


@dataclass(frozen=True)
class Issue:
    field: str
    message: str


@dataclass
class SubmissionReport:
    issues: list[Issue] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    checked_files: list[str] = field(default_factory=list)
    commit_checked: bool = False

    @property
    def ready(self) -> bool:
        return not self.issues and self.commit_checked

    def to_dict(self) -> dict[str, Any]:
        return {"ready": self.ready, **asdict(self)}

    def problem(self, name: str, message: str) -> None:
        self.issues.append(Issue(name, message))


def repository_path(root: Path, value: Any) -> Path:
    """Resolve a portable relative path, rejecting Windows drives, traversal, ADS and escaping symlinks."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("A repository-relative file path is required.")
    normalized = value.replace("\\", "/")
    parts = PurePosixPath(normalized).parts
    if (
        PureWindowsPath(value).drive or PureWindowsPath(value).root
        or PurePosixPath(normalized).is_absolute() or ".." in parts
        or any(":" in part for part in parts) or "\x00" in value
    ):
        raise ValueError("Use a relative path inside the repository, without a drive or traversal.")
    try:
        target = (root / normalized).resolve()
        target.relative_to(root.resolve())
    except (OSError, ValueError) as exc:
        raise ValueError("The path resolves outside the repository or is invalid.") from exc
    return target


def _recorded_path(root: Path, value: Any) -> Path:
    """Build results may record absolute paths, but they must still resolve inside this checkout."""
    if isinstance(value, str) and Path(value).is_absolute():
        try:
            target = Path(value).resolve()
            target.relative_to(root.resolve())
            return target
        except (OSError, ValueError) as exc:
            raise ValueError("The build records a path outside this checkout.") from exc
    return repository_path(root, value)


def _text(path: Path) -> str:
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("The file exceeds the local check's size limit.")
    return path.read_text(encoding="utf-8-sig")


def _object(path: Path) -> dict[str, Any]:
    value = json.loads(_text(path))
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object.")
    return value


def _version(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, str) and value.isdecimal() and int(value) > 0:
        return int(value)
    return None


def _path_field(root: Path, value: Any, name: str, report: SubmissionReport) -> Path | None:
    try:
        target = repository_path(root, value)
        if not target.is_file():
            raise ValueError("The referenced file is missing or is not a regular file.")
    except (OSError, ValueError) as exc:
        report.problem(name, str(exc))
        return None
    report.checked_files.append(target.relative_to(root).as_posix())
    return target


def _read_object(path: Path, name: str, report: SubmissionReport) -> dict[str, Any] | None:
    try:
        return _object(path)
    except (OSError, UnicodeError, ValueError):
        report.problem(name, "Could not read a bounded UTF-8 JSON object; inspect the file locally.")
        return None


def _trace_summary(path: Path, root: Path, report: SubmissionReport, name: str) -> dict[str, Any] | None:
    """Read structured builder traces, or the corresponding summary from a plain-text trace."""
    try:
        text = _text(path)
        structured = path if path.suffix == ".jsonl" else path.with_suffix(".jsonl")
        if structured.is_file():
            structured.resolve().relative_to(root)
            records = [json.loads(line) for line in _text(structured).splitlines() if line.strip()]
            if not all(isinstance(record, dict) for record in records):
                raise ValueError("Invalid trace records.")
            kinds = [record.get("kind") for record in records]
            if "start" not in kinds or "end" not in kinds or kinds.index("start") >= kinds.index("end"):
                raise ValueError("The trace needs ordered start and end records.")
            run_ids = {record.get("run_id") for record in records}
            if len(run_ids) != 1 or not next(iter(run_ids)):
                raise ValueError("The trace contains missing or conflicting run identities.")
            summaries = [record["data"] for record in records if record.get("kind") == "summary"]
            if len(summaries) != 1 or not isinstance(summaries[0], dict):
                raise ValueError("A single structured build summary is required.")
            if path != structured:
                if not re.search(r"\| START\s*\|", text) or not re.search(r"\| END\s*\|", text):
                    raise ValueError("The selected text trace lacks start/end records.")
                log_run = re.search(r"^run id\s*:\s*(\S+)", text, re.MULTILINE)
                marker_ids = re.findall(r"\| (?:START|END)\s*\| run (\S+) (?:started|closed)", text)
                if len(marker_ids) != 2 or set(marker_ids) != run_ids or (log_run and log_run[1] not in run_ids):
                    raise ValueError("The text trace and companion have different run identities.")
                report.checked_files.append(structured.relative_to(root).as_posix())
            summary = dict(summaries[0])
            start = next(record for record in records if record.get("kind") == "start")
            end = next(record for record in records if record.get("kind") == "end")
            summary["started_at"] = start.get("ts")
            summary["ended_at"] = end.get("ts")
            summary["run_id"] = next(iter(run_ids))
            summary["_model_records"] = [record.get("data") for record in records if record.get("kind") == "model_call"]
            return summary
        if not re.search(r"\| START\s*\|", text) or not re.search(r"\| END\s*\|", text):
            raise ValueError("The trace lacks start/end records.")
        values = {key.strip(): value for key, value in re.findall(
            r"^\s*(?:- )?([\w ()]+)\s*:\s*(.*?)\s*$", text, re.MULTILINE
        )}
        marker_ids = re.findall(r"\| (?:START|END)\s*\| run (\S+) (?:started|closed)", text)
        if len(marker_ids) != 2 or set(marker_ids) != {values.get("run id")}:
            raise ValueError("Conflicting text trace identities.")
        models = []
        for line in text.splitlines():
            parts = [part.strip() for part in line.split("|")]
            if len(parts) >= 6 and parts[1] == "MODEL" and "/" in parts[2]:
                provider, model = parts[2].split("/", 1)
                models.append({"provider": provider, "model": model, "purpose": parts[5]})
        model_total = re.match(r"\d+", values.get("model calls", ""))
        report.warnings.append(f"{name}: using a text summary; structured trace evidence is preferable.")
        return {
            "run_id": values.get("run id", ""), "slot": values.get("slot", ""),
            "status": values.get("status", ""), "started_at": values.get("started", ""),
            "ended_at": values.get("ended", ""),
            "freya": {"workflow_id": values.get("workflow id", ""), "agent_id": values.get("agent id", ""),
                      "version": values.get("published version", ""), "published": values.get("published") == "True",
                      "node_count": int(values.get("steps (nodes)", "-1")), "tool_count": int(values.get("tools", "-1"))},
            "model_calls": {"total": int(model_total[0]) if model_total else -1}, "_model_records": models,
            "extra": {"cut_off": values.get("cut_off", "") not in ("", "False", "false")},
        }
    except (OSError, UnicodeError, ValueError, KeyError, TypeError):
        report.problem(name, "Trace is malformed, incomplete, or lacks consistent start/end and summary evidence.")
        return None


def _check_trace(summary: Mapping[str, Any], build: Mapping[str, Any], result_path: Path,
                 name: str, report: SubmissionReport, counts: tuple[int, int] | None = None) -> None:
    freya = summary.get("freya")
    if not isinstance(freya, dict):
        report.problem(name, "Trace has no deployment identity evidence.")
        freya = {}
    for key in ("workflow_id", "agent_id"):
        if freya.get(key) != build.get(key):
            report.problem(name, f"Trace {key} does not match the selected build.")
    if _version(freya.get("version")) != _version(build.get("version")):
        report.problem(name, "Trace version does not match the selected build.")
    if freya.get("published") is not True:
        report.problem(name, "Trace does not confirm publication of the selected version.")
    for index, key in enumerate(("node_count", "tool_count")):
        value = freya.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            report.problem(name, f"Trace needs numeric {key} evidence.")
        elif counts is not None and value != counts[index]:
            report.problem(name, f"Trace {key} differs from the exported workflow.")
    model_records = summary.get("_model_records") or []
    calls = summary.get("model_calls") or {}
    total = calls.get("total") if isinstance(calls, dict) else None
    if isinstance(total, bool) or not isinstance(total, int) or total != len(model_records):
        report.problem(name, "Trace model-call totals do not match its individual call records.")
    for record in model_records:
        if not isinstance(record, dict) or any(not isinstance(record.get(key), str) or not record[key].strip()
                                               for key in ("provider", "model", "purpose")):
            report.problem(name, "Every recorded model call needs its provider, model and one-line purpose.")
            break
        if "\n" in record["purpose"] or "\r" in record["purpose"]:
            report.problem(name, "A model-call purpose must fit on one line.")
    if summary.get("status") != "deployed" or summary.get("slot") != build.get("slot"):
        report.problem(name, "Trace status/slot does not match a deployed build.")
    match = re.search(r"-([0-9a-f]{12})\.build\.json$", result_path.name)
    expected_run = build.get("run_id") or (match[1] if match else None)
    if expected_run and summary.get("run_id") != expected_run:
        report.problem(name, "Trace run identity does not match the build result.")
    extra = summary.get("extra") or {}
    if isinstance(extra, dict) and (extra.get("cut_off") or extra.get("untested_redeploy")):
        report.problem(name, "Trace reports a cutoff or an untested replacement version.")
    try:
        start = datetime.fromisoformat(str(summary.get("started_at", "")).replace("Z", "+00:00"))
        end = datetime.fromisoformat(str(summary.get("ended_at", "")).replace("Z", "+00:00"))
        elapsed = (end - start).total_seconds()
        if not math.isfinite(elapsed) or not 0 <= elapsed <= 1800:
            raise ValueError("Invalid duration.")
    except (TypeError, ValueError, OverflowError):
        report.problem(name, "Trace timestamps must be ordered and within the 30-minute build limit.")


def _check_brief(manifest: Mapping[str, Any], brief: str, root: Path, report: SubmissionReport) -> None:
    selected: dict[str, Path] = {}
    counts: tuple[int, int] | None = None
    for group in ("workflows", "traces", "build_results"):
        mapping = manifest.get(group)
        value = mapping.get(brief) if isinstance(mapping, dict) else None
        target = _path_field(root, value, f"{group}.{brief}", report)
        if target is not None:
            selected[group] = target
    if "build_results" not in selected:
        return
    build = _read_object(selected["build_results"], f"build_results.{brief}", report)
    if build is None:
        return
    name = f"build_results.{brief}"
    if build.get("status") != "deployed" or build.get("export_ok") is not True:
        report.problem(name, "A deployed build with export_ok=true is required; dry runs are not final agents.")
    for key in ("workflow_id", "agent_id", "slot"):
        if not isinstance(build.get(key), str) or not build[key].strip():
            report.problem(name, f"The build needs a nonempty {key}.")
    if _version(build.get("version")) is None:
        report.problem(name, "A positive deployed version number is required.")
    if build.get("published") is False or build.get("cut_off") or build.get("untested_redeploy"):
        report.problem(name, "The build reports unpublished, cut off, or untested replacement output.")
    if build.get("errors"):
        report.warnings.append(f"{name}: build errors are recorded; review them before submission.")
    for group, key in (("workflows", "exported"), ("traces", "trace")):
        if group not in selected:
            continue
        try:
            if _recorded_path(root, build.get(key)) != selected[group]:
                raise ValueError("The selected file does not match this build's recorded artifact.")
        except (OSError, ValueError) as exc:
            report.problem(f"{group}.{brief}", str(exc))
    if "workflows" in selected:
        export = _read_object(selected["workflows"], f"workflows.{brief}", report)
        if export is not None:
            for key in ("workflow_id", "agent_id"):
                if export.get(key) != build.get(key):
                    report.problem(f"workflows.{brief}", f"Export {key} does not match the selected build.")
            workflow = export.get("workflow")
            if (not isinstance(workflow, dict) or not isinstance(workflow.get("nodes"), dict)
                or not isinstance(workflow.get("initial_node"), str)
                or workflow["initial_node"] not in workflow["nodes"]):
                report.problem(f"workflows.{brief}", "Expected an actual platform export containing a workflow.")
            else:
                counts = count_nodes(workflow), count_tools(workflow)
            if "version" in export and _version(export["version"]) != _version(build.get("version")):
                report.problem(f"workflows.{brief}", "Export version does not match the selected build.")
    if "traces" in selected:
        summary = _trace_summary(selected["traces"], root, report, f"traces.{brief}")
        if summary is not None:
            _check_trace(summary, build, selected["build_results"], f"traces.{brief}", report, counts)


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, timeout=15, check=False)


def _check_commit(root: Path, commit: Any, report: SubmissionReport) -> None:
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-fA-F]{7,64}", commit):
        report.problem("commit", "Pending commit: supply a local commit SHA after reviewing and committing the packet.")
        return
    try:
        resolved = _git(root, "rev-parse", "--verify", f"{commit}^{{commit}}")
        if resolved.returncode:
            report.problem("commit", "The supplied commit cannot be resolved in this repository.")
            return
        sha = resolved.stdout.decode("ascii").strip()
        changed = _git(root, "diff", "--name-only", sha, "--", *CODE_PATHS)
        untracked = _git(root, "ls-files", "--others", "--exclude-standard", "--", *CODE_PATHS)
        if changed.returncode or untracked.returncode:
            report.problem("commit", "Could not inspect current code coverage using read-only Git commands.")
            return
        if changed.stdout.strip() or untracked.stdout.strip():
            report.problem("commit", "Pending commit: runtime code/config/tests differ from the selected commit or are untracked.")
        for relative in REQUIRED_CODE:
            if _git(root, "cat-file", "-e", f"{sha}:{relative}").returncode:
                report.problem("commit", f"Required runtime file is absent from the selected commit: {relative}.")
        for relative in sorted(set(report.checked_files)):
            blob = _git(root, "rev-parse", "--verify", f"{sha}:{relative}")
            if blob.returncode:
                report.problem(relative, "Artifact is absent from the selected commit; a commit is pending.")
                continue
            current = _git(root, "hash-object", f"--path={relative}", "--", relative)
            if current.returncode:
                report.problem(relative, "Could not check Git-normalized artifact bytes.")
            elif blob.stdout.strip() != current.stdout.strip():
                report.problem(relative, "Git-normalized artifact bytes differ from the selected commit; a commit is pending.")
        report.commit_checked = True
    except (OSError, UnicodeError, subprocess.TimeoutExpired):
        report.problem("commit", "Read-only Git verification could not finish; no Git state was changed.")


def check_submission(manifest: Mapping[str, Any], *, root: Path | None = None) -> SubmissionReport:
    """Collect blockers without printing file contents or modifying files, Git or remote services."""
    root = (root or paths.REPO_ROOT).resolve()
    report = SubmissionReport(warnings=[
        "Local evidence cannot establish use of the actual hidden briefs, hidden-customer scores, current remote publication, or complete secret redaction.",
        "Exports without an embedded version rely on the corresponding build and trace; no remote read-back is performed.",
    ])
    for name in FORM_FIELDS[:4]:
        if not isinstance(manifest.get(name), str) or not manifest[name].strip():
            report.problem(name, "A nonempty form field is required.")
    command = manifest.get("run_command", "")
    if isinstance(command, str) and ("{brief_path}" not in command or "{tool_api_path}" not in command):
        report.problem("run_command", "Include both {brief_path} and {tool_api_path} placeholders.")
    if isinstance(command, str) and re.search(r"--(?:dry-run|fake-llm)(?:[\s=]|$)", command):
        report.problem("run_command", "The final run command must not request dry-run or fixture model answers.")
    for group in ("workflows", "traces", "build_results"):
        mapping = manifest.get(group)
        if not isinstance(mapping, dict) or set(mapping) != set(BRIEFS):
            report.problem(group, "Provide a map with exactly hidden-1 and hidden-2.")
        elif all(isinstance(mapping.get(brief), str) and mapping[brief] for brief in BRIEFS):
            try:
                if repository_path(root, mapping[BRIEFS[0]]) == repository_path(root, mapping[BRIEFS[1]]):
                    report.problem(group, "Each final brief needs its own selected artifact/result path.")
            except ValueError:
                pass  # the per-brief path checks below explain the problem
    if set(manifest) - set(LOCAL_FIELDS):
        report.problem("manifest", "Unexpected fields: keep the official fields and the local build_results map only.")
    for brief in BRIEFS:
        _check_brief(manifest, brief, root, report)
    report.checked_files = sorted(set(report.checked_files))
    _check_commit(root, manifest.get("commit"), report)
    return report


def prepare_submission(*, team: str, repo: str, run_command: str, build_results: Mapping[str, str],
                       commit: str = "", root: Path | None = None) -> dict[str, Any]:
    """Scaffold solely from explicitly selected manifests; never turn a dry-run local file into an export."""
    root = (root or paths.REPO_ROOT).resolve()
    draft: dict[str, Any] = {"team": team, "repo": repo, "commit": commit, "run_command": run_command,
                             "workflows": dict.fromkeys(BRIEFS, ""), "traces": dict.fromkeys(BRIEFS, ""),
                             "build_results": {brief: build_results.get(brief, "") for brief in BRIEFS}}
    for brief in BRIEFS:
        try:
            result = repository_path(root, draft["build_results"][brief])
            build = _object(result)
            if build.get("status") != "deployed" or build.get("export_ok") is not True:
                continue
            for group, key in (("workflows", "exported"), ("traces", "trace")):
                draft[group][brief] = _recorded_path(root, build.get(key)).relative_to(root).as_posix()
        except (OSError, UnicodeError, ValueError):
            continue  # retain the explicit selection and blanks; check reports the blockers
    return draft


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=paths.REPO_ROOT, help="repository checkout (default: this project)")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("check", "payload"):
        commands.add_parser(name).add_argument("draft", help="repository-relative draft JSON path")
    prepare = commands.add_parser("prepare")
    prepare.add_argument("--output", required=True, help="repository-relative draft JSON path")
    for name in ("team", "repo", "run-command"):
        prepare.add_argument(f"--{name}", required=True)
    prepare.add_argument("--commit", default="")
    for brief in BRIEFS:
        prepare.add_argument(f"--{brief}-result", required=True, help="explicit repository-relative build result")
    prepare.add_argument("--force", action="store_true", help="allow replacing the selected draft only")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        if args.command == "prepare":
            draft = prepare_submission(team=args.team, repo=args.repo, run_command=args.run_command,
                                       commit=args.commit, root=root,
                                       build_results={brief: getattr(args, brief.replace("-", "_") + "_result") for brief in BRIEFS})
            target = repository_path(root, args.output)
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("w" if args.force else "x", encoding="utf-8", newline="\n") as handle:
                json.dump(draft, handle, indent=2, ensure_ascii=True)
                handle.write("\n")
        else:
            draft = _object(repository_path(root, args.draft))
        report = check_submission(draft, root=root)
        if args.command == "payload" and report.ready:
            print(json.dumps({name: draft[name] for name in FORM_FIELDS}, indent=2, ensure_ascii=True))
        else:
            print(json.dumps(report.to_dict(), indent=2, ensure_ascii=True),
                  file=sys.stderr if args.command == "payload" else sys.stdout)
        return 0 if report.ready else 2
    except (OSError, UnicodeError, ValueError):
        print(json.dumps({"ready": False, "issues": [{"field": "draft", "message":
            "Draft could not be read/written: check its path, JSON format and overwrite permission."}]}),
            file=sys.stderr if args.command == "payload" else sys.stdout)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
