"""Check and emit the current single-case submission, using a sealed selected run.

This helper does not build, deploy, edit a workflow, change Git, or send email.
Run inspect/validate with --config and --selection (a seal.json). Payload is
written only when the exact sealed artifact bytes exist in the selected commit.
"""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import re
import shlex
import subprocess
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

FIELDS = {"team", "members", "repo", "commit", "run_command", "workflow", "trace", "mock_url"}
RUN_COMMAND = "python3 builder/build.py {case_folder} {mock_url}"
ARTIFACTS = {"workflow", "trace", "build_result", "run_manifest", "deployment_receipt", "readiness_report"}
HEX_SHA = re.compile(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}")
HEX_DIGEST = re.compile(r"[0-9a-f]{64}")
EMAIL = re.compile(r"[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+")
PLACEHOLDER = re.compile(r"(YOUR_|PASTE_|FINAL_COMMIT|TODO|REPLACE|CHANGEME|<[^>]*>)", re.I)
ROOT = Path(__file__).resolve().parents[2]


def digest(path: Path) -> tuple[str, int]:
    sha, size = hashlib.sha256(), 0
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            sha.update(block)
            size += len(block)
    return sha.hexdigest(), size


def safe_path(root: Path, value: Any, *, must_exist: bool = True) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("a nonempty repository-relative file path is required")
    normalized = value.replace("\\", "/")
    parts = PurePosixPath(normalized).parts
    if (PureWindowsPath(value).drive or PureWindowsPath(value).root
            or PurePosixPath(normalized).is_absolute() or ".." in parts
            or any(":" in part for part in parts) or "\0" in value):
        raise ValueError("path must stay inside its root without a drive or traversal")
    candidate = root / normalized
    for component in [candidate, *candidate.parents]:
        if component == root:
            break
        if component.is_symlink() or getattr(component, "is_junction", lambda: False)():
            raise ValueError("artifact paths must not use symlinks or junctions")
    target = candidate.resolve()
    target.relative_to(root.resolve())
    if must_exist and not target.is_file():
        raise ValueError("referenced regular file is missing")
    return target


def object_file(path: Path) -> dict[str, Any]:
    if path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError("JSON metadata exceeds 16 MiB")
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object")
    return value


def text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip()) and not PLACEHOLDER.search(value)


def https_url(value: Any, *, origin: bool = False) -> bool:
    if not text(value) or any(char.isspace() for char in value):
        return False
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        if (parsed.scheme != "https" or not host or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed.port not in (None, 443)):
            return False
        if host.lower() in {"localhost", "example.com", "example.org", "example.net"} or "." not in host:
            return False
        if host.lower().endswith((".localhost", ".local", ".invalid", ".example")):
            return False
        try:
            if not ipaddress.ip_address(host).is_global:
                return False
        except ValueError:
            if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", host):
                return False
        return not origin or parsed.path in ("", "/")
    except ValueError:
        return False


def schema_issues(config: dict[str, Any]) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    def issue(field: str, message: str) -> None:
        issues.append({"field": field, "message": message})
    if set(config) != FIELDS:
        issue("schema", "use exactly the eight current single-case fields; old workflows/traces maps are invalid")
    if not text(config.get("team")):
        issue("team", "supply the actual team name")
    members = config.get("members")
    if not isinstance(members, list) or not 1 <= len(members) <= 2:
        issue("members", "supply one or two members, each with name and email")
    else:
        for index, member in enumerate(members):
            if not isinstance(member, dict) or set(member) != {"name", "email"}:
                issue(f"members.{index}", "member must contain exactly name and email")
                continue
            if not text(member["name"]):
                issue(f"members.{index}.name", "supply the member name")
            email = member["email"]
            if (not text(email) or not EMAIL.fullmatch(email)
                    or email.rsplit("@", 1)[-1].lower() in {"example.com", "example.org", "example.net"}):
                issue(f"members.{index}.email", "supply the real member email")
        emails = [member.get("email", "").lower() for member in members if isinstance(member, dict)
                  and isinstance(member.get("email"), str)]
        if len(set(emails)) != len(emails):
            issue("members", "member emails must be distinct")
    if not https_url(config.get("repo")):
        issue("repo", "supply the real HTTPS repository URL")
    if not isinstance(config.get("commit"), str) or not HEX_SHA.fullmatch(config["commit"]):
        issue("commit", "supply the full 40- or 64-character local commit SHA after committing the selected packet")
    try:
        command = config.get("run_command")
        if not text(command) or "\n" in command or "\r" in command:
            raise ValueError("missing command")
        tokens = shlex.split(command)
        placeholders = re.findall(r"\{([^{}]+)\}", command)
        if sorted(placeholders) != ["case_folder", "mock_url"] or not tokens:
            raise ValueError("wrong placeholders")
        if any(token in {"|", "||", ";", "&&", ">", ">>", "<", "2>&1"} for token in tokens):
            raise ValueError("shell pipeline is not an argv command")
    except ValueError:
        issue("run_command", "supply your builder argv template with exactly one {case_folder} and one {mock_url}")
    for name in ("workflow", "trace"):
        value = config.get(name)
        if not text(value):
            issue(name, "supply the selected sealed artifact's repository-relative path")
        elif "\\" in value or PurePosixPath(value).as_posix() != value:
            issue(name, "use a normalized portable path with forward slashes")
    if not https_url(config.get("mock_url"), origin=True):
        issue("mock_url", "supply the deployed public HTTPS Worker origin without credentials, query or fragment")
    return issues


def command_matches(template: Any, manifest: dict[str, Any]) -> bool:
    """Compare argv, allowing a Python executable path to differ between hosts."""
    command, folder, mock = manifest.get("command"), manifest.get("case_folder"), manifest.get("mock_url")
    if (not isinstance(template, str) or not isinstance(folder, str) or not folder
            or not isinstance(mock, str) or not isinstance(command, list)
            or not all(isinstance(arg, str) and arg for arg in command)):
        return False
    try:
        tokens = shlex.split(template)
    except ValueError:
        return False
    expanded = [token.replace("{case_folder}", folder).replace("{mock_url}", mock) for token in tokens]
    if len(expanded) != len(command) or not expanded:
        return False
    python = re.compile(r"python(?:[23](?:\.\d+)?)?(?:\.exe)?", re.I)
    for index, (expected, actual) in enumerate(zip(expanded, command)):
        if expected == actual:
            continue
        if index == 0 and python.fullmatch(PureWindowsPath(expected).name) and python.fullmatch(PureWindowsPath(actual).name):
            continue
        # Script file separators differ between Windows capture and Linux replay.
        if expected.replace("\\", "/") == actual.replace("\\", "/"):
            continue
        return False
    return True


def runtime_files(root: Path) -> set[str]:
    roots = ("builder", ".claude/skills")
    result = set()
    for name in roots:
        directory = root / name
        if directory.is_dir():
            for path in directory.rglob("*"):
                if path.is_file() and "__pycache__" not in path.parts and path.suffix not in {".pyc", ".pyo"}:
                    result.add(path.relative_to(root).as_posix())
    for name in ("freya.py", "pyproject.toml", "uv.lock", "requirements.txt", ".python-version"):
        if (root / name).is_file():
            result.add(name)
    return result


def source_issues(manifest: dict[str, Any], root: Path, start: Any, end: Any) -> tuple[list[dict[str, str]], list[tuple[str, str, int]]]:
    issues, files = [], []
    def issue(message: str) -> None:
        issues.append({"field": "source_snapshot", "message": message})
    before, after = manifest.get("source_snapshot"), manifest.get("source_after")
    if not isinstance(before, dict) or not isinstance(after, dict) or manifest.get("source_stable") is not True:
        issue("source must have matching snapshots captured before and after the selected builder run")
        return issues, files
    entries = before.get("files")
    if not isinstance(entries, list) or not entries or entries != after.get("files"):
        issue("runtime source file list or hashes changed during the run")
        return issues, files
    encoded = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if hashlib.sha256(encoded).hexdigest() != before.get("tree_sha256") or before.get("tree_sha256") != after.get("tree_sha256"):
        issue("source tree digest differs from its recorded file list")
    seen = set()
    for entry in entries:
        try:
            if not isinstance(entry, dict):
                raise ValueError("invalid source entry")
            path = safe_path(root, entry.get("path"))
            relative = path.relative_to(root).as_posix()
            sha, size = entry.get("sha256"), entry.get("size_bytes")
            if not isinstance(sha, str) or not HEX_DIGEST.fullmatch(sha) or type(size) is not int or size < 0:
                raise ValueError("invalid source hash/size")
            if relative in seen or digest(path) != (sha, size):
                raise ValueError("source changed or duplicated")
            seen.add(relative)
            files.append((relative, sha, size))
        except (OSError, ValueError):
            issue("a source snapshot entry is invalid, duplicated or differs from current runtime bytes")
    required = runtime_files(root)
    if not required or "builder/build.py" not in seen or "freya.py" not in seen or not required <= seen:
        issue("snapshot must cover all builder files/templates, .claude skills and runtime manifests")
    try:
        captured = datetime.fromisoformat(str(before.get("captured_at", "")).replace("Z", "+00:00"))
        started = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
        finished = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
        captured_after = datetime.fromisoformat(str(after.get("captured_at", "")).replace("Z", "+00:00"))
        if any(moment.tzinfo is None for moment in (captured, started, finished, captured_after)) or not captured <= started <= finished <= captured_after:
            raise ValueError("snapshots must surround the run")
    except (TypeError, ValueError):
        issue("source snapshot timestamp must precede the selected builder process")
    return issues, files


def case_issues(manifest: dict[str, Any], root: Path) -> tuple[list[dict[str, str]], list[tuple[str, str, int]]]:
    issues, files = [], []
    def issue(message: str) -> None:
        issues.append({"field": "case_snapshot", "message": message})
    before, after = manifest.get("case_snapshot"), manifest.get("case_after")
    if not isinstance(before, dict) or not isinstance(after, dict) or manifest.get("case_stable") is not True:
        issue("selected case inputs need stable snapshots before and after the builder")
        return issues, files
    entries = before.get("files")
    if not isinstance(entries, list) or not entries or entries != after.get("files"):
        issue("case input file list or hashes changed during the run")
        return issues, files
    encoded = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if hashlib.sha256(encoded).hexdigest() != before.get("tree_sha256") or before.get("tree_sha256") != after.get("tree_sha256"):
        issue("case tree digest differs from its recorded file list")
    for entry in entries:
        try:
            if not isinstance(entry, dict):
                raise ValueError("invalid case entry")
            path = safe_path(root, entry.get("path"))
            sha, size = entry.get("sha256"), entry.get("size_bytes")
            if not isinstance(sha, str) or not HEX_DIGEST.fullmatch(sha) or type(size) is not int or size < 0:
                raise ValueError("invalid case hash")
            if digest(path) != (sha, size):
                raise ValueError("case changed")
            files.append((path.relative_to(root).as_posix(), sha, size))
        except (OSError, ValueError):
            issue("a selected case input is missing, invalid or changed")
    return issues, files


def deployment_issues(paths: dict[str, Path], manifest: dict[str, Any], capture: dict[str, Any],
                      root: Path, mock_url: Any) -> tuple[list[dict[str, str]], list[Path]]:
    issues, evidence = [], []
    def issue(message: str) -> None:
        issues.append({"field": "deployment_receipt", "message": message})
    if not {"deployment_receipt", "readiness_report"} <= set(paths):
        issue("sealed deployment receipt and independent readiness report are required")
        return issues, evidence
    try:
        receipt = object_file(paths["deployment_receipt"])
        report = object_file(paths["readiness_report"])
        if receipt.get("schema_version") != 1 or receipt.get("bank_ready") is not True:
            issue("receipt must explicitly attest schema_version=1 and bank_ready=true")
        if report.get("report_schema_version") != 1 or report.get("bank_ready") is not True:
            issue("independent readiness report must explicitly confirm bank readiness")
        for name in ("worker_name", "deployment_id", "cloudflare_version_id"):
            if not text(receipt.get(name)):
                issue("receipt is missing its Worker or Cloudflare deployment/version identity")
        if receipt.get("base_url") != mock_url or not https_url(receipt.get("base_url"), origin=True):
            issue("deployment receipt must identify the selected public HTTPS Worker origin")
        health = receipt.get("health_url")
        if health != str(mock_url).rstrip("/") + "/__submission_health":
            issue("receipt must name this Worker's /__submission_health URL")
        for role in ("deployment_receipt", "readiness_report"):
            info = manifest.get(role)
            if (not isinstance(info, dict)
                    or (info.get("sha256"), info.get("size_bytes")) != digest(paths[role])):
                issue("sealed service evidence differs from the pre-build capture")
        bindings = {"deployment_id": receipt.get("deployment_id"),
                    "cloudflare_version_id": receipt.get("cloudflare_version_id"),
                    "base_url": receipt.get("base_url"), "health_url": receipt.get("health_url"),
                    "bundle_sha256": (receipt.get("bundle") or {}).get("sha256"),
                    "contract_sha256": (receipt.get("contract") or {}).get("sha256"),
                    "customer_fixture_sha256": (receipt.get("customer_fixtures") or {}).get("sha256")}
        case_digests = {entry.get("sha256") for entry in (manifest.get("case_snapshot") or {}).get("files", [])
                        if isinstance(entry, dict)}
        for name in ("contract_sha256", "customer_fixture_sha256"):
            if bindings[name] not in case_digests:
                issue("deployed contract and customer fixtures must match the captured case inputs")
        for name, expected in bindings.items():
            if report.get(name) != expected:
                issue(f"independent readiness report disagrees with receipt {name}")
        if report.get("receipt_bytes_sha256") != digest(paths["deployment_receipt"])[0]:
            issue("readiness report authenticates different deployment receipt bytes")
        if report.get("receipt_path") != (manifest.get("deployment_receipt") or {}).get("source_path"):
            issue("readiness report and capture refer to different original deployment receipts")
        probe = capture.get("probe") or {}
        metadata = probe.get("metadata") or {}
        if (probe.get("url") != health or type(probe.get("http_status")) is not int
                or not 200 <= probe["http_status"] < 300
                or metadata.get("ready") is not True or metadata.get("bank_ready") is not True):
            issue("pre-build live probe must attest readiness at the receipt's health URL")
        for name in ("cloudflare_version_id", "bundle_sha256", "contract_sha256", "customer_fixture_sha256"):
            if metadata.get(name) != bindings[name]:
                issue(f"pre-build live probe differs from verified deployment {name}")
        deployed = datetime.fromisoformat(str(receipt.get("deployed_at_utc", "")).replace("Z", "+00:00"))
        verified = datetime.fromisoformat(str(report.get("verified_at_utc", "")).replace("Z", "+00:00"))
        started = datetime.fromisoformat(str(capture.get("started_at", "")).replace("Z", "+00:00"))
        if any(moment.tzinfo is None for moment in (deployed, verified, started)) or not deployed <= verified <= started:
            issue("deployment and independent live verification must precede the selected builder run")
        records = [receipt.get(name) for name in ("bundle", "configuration", "contract", "customer_fixtures")]
        sources = receipt.get("source_files")
        if not isinstance(sources, list) or not sources:
            issue("receipt must record bank source files")
        else:
            records.extend(sources)
        storage = receipt.get("storage") or {}
        if (not text(storage.get("backend")) or storage.get("persistence_verified") is not True
                or storage.get("customer_isolation_verified") is not True):
            issue("receipt must attest tested persistent storage and customer isolation")
        records.append(storage.get("evidence"))
        tests = receipt.get("contract_tests") or {}
        if tests.get("passed") is not True or type(tests.get("case_count")) is not int or tests["case_count"] < 1:
            issue("receipt must attest passing live contract tests with at least one case")
        records.append(tests.get("report"))
        for entry in records:
            try:
                if not isinstance(entry, dict):
                    raise ValueError("missing receipt artifact")
                path = safe_path(root, entry.get("path"))
                if digest(path)[0] != entry.get("sha256"):
                    raise ValueError("deployed evidence differs")
                evidence.append(path)
            except (OSError, ValueError):
                issue("a deployed bundle/configuration/contract/fixture/source/test artifact is missing or changed")
    except (OSError, UnicodeError, ValueError, TypeError, AttributeError):
        issue("deployment evidence is malformed or cannot be checked")
    return issues, evidence


def git_blob_digest(root: Path, commit: str, relative: str) -> tuple[str, int]:
    """Hash the raw committed blob; bypass checkout filters and line-ending normalization."""
    process = subprocess.Popen(["git", "-C", str(root), "cat-file", "blob", f"{commit}:{relative}"],
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    sha, size = hashlib.sha256(), 0
    assert process.stdout is not None
    with process.stdout:
        for block in iter(lambda: process.stdout.read(1024 * 1024), b""):
            sha.update(block)
            size += len(block)
    if process.wait(timeout=30):
        raise ValueError("file is absent from the selected commit")
    return sha.hexdigest(), size


def check(config: dict[str, Any], selection: Path, root: Path) -> dict[str, Any]:
    issues = schema_issues(config)
    checked: list[str] = []
    paths: dict[str, Path] = {}
    source_files: list[tuple[str, str, int]] = []
    deployment_files: list[Path] = []
    def issue(field: str, message: str) -> None:
        issues.append({"field": field, "message": message})
    seal: dict[str, Any] = {}
    try:
        selection = selection.resolve()
        selection.relative_to(root)
        seal = object_file(selection)
        try:
            tools_dir = str(Path(__file__).resolve().parent)
            if tools_dir not in sys.path:
                sys.path.insert(0, tools_dir)
            from seal_run import verify_seal
            verify_seal(selection.parent)
        except (ImportError, OSError, UnicodeError, ValueError, TypeError, KeyError):
            issue("selection", "sealer verification failed: run/trace/file-set provenance is incomplete or changed")
        if seal.get("trace_vetted") is not True:
            issue("selection.trace_vetted", "trace must be vetted by the sealer before packaging")
        if seal.get("format_version") != 1:
            issue("selection", "unsupported seal format; expected format_version 1")
        artifacts = seal.get("artifacts")
        if not isinstance(artifacts, dict) or set(artifacts) != ARTIFACTS:
            issue("selection.artifacts", "seal must contain run artifacts plus deployment_receipt and readiness_report")
            artifacts = {}
        for name, entry in artifacts.items():
            try:
                if not isinstance(entry, dict):
                    raise ValueError("invalid artifact entry")
                path = safe_path(selection.parent, entry.get("path"))
                path.relative_to(root)
                expected = entry.get("sha256")
                if not isinstance(expected, str) or not HEX_DIGEST.fullmatch(expected):
                    raise ValueError("missing artifact SHA256")
                size = entry.get("size_bytes")
                if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
                    raise ValueError("missing positive artifact byte count")
                if digest(path) != (expected, size):
                    raise ValueError("sealed bytes changed")
                paths[name] = path
                checked.append(path.relative_to(root).as_posix())
            except (OSError, ValueError):
                issue(f"selection.artifacts.{name}", "artifact is missing, unsafe, malformed or differs from its seal")
        for name in ("workflow", "trace"):
            try:
                if safe_path(root, config.get(name)) != paths.get(name):
                    issue(name, "submission path must point to the exact selected sealed artifact")
            except (OSError, ValueError):
                issue(name, "selected artifact path is missing or unsafe")
        capture = seal.get("capture")
        build = seal.get("build")
        if not isinstance(capture, dict):
            issue("selection.capture", "capture evidence is missing")
            capture = {}
        if not isinstance(build, dict):
            issue("selection.build", "selected build evidence is missing")
            build = {}
        if (not text(capture.get("run_id")) or type(capture.get("returncode")) is not int
                or capture.get("returncode") != 0):
            issue("selection.capture", "selected capture needs its run ID and zero process exit code")
        if capture.get("mock_url") != config.get("mock_url"):
            issue("mock_url", "Worker URL must match the captured run")
        probe = capture.get("probe")
        if not isinstance(probe, dict) or probe.get("ok") is not True:
            issue("selection.capture.probe", "a successful Worker probe before the builder is required")
        if "run_manifest" in paths:
            if capture.get("manifest_sha256") != digest(paths["run_manifest"])[0]:
                issue("selection.capture", "capture manifest hash differs from the selected run manifest")
            manifest = object_file(paths["run_manifest"])
            findings, source_files = source_issues(manifest, root, capture.get("started_at"), capture.get("ended_at"))
            issues.extend(findings)
            findings, case_files = case_issues(manifest, root)
            issues.extend(findings)
            source_files.extend(case_files)
            findings, deployment_files = deployment_issues(paths, manifest, capture, root, config.get("mock_url"))
            issues.extend(findings)
            if (manifest.get("format_version") != 1 or manifest.get("state") != "complete"
                    or type(manifest.get("returncode")) is not int or manifest.get("returncode") != 0):
                issue("selection.run_manifest", "captured process must be complete and successful")
            for name in ("run_id", "mock_url", "probe", "started_at", "ended_at"):
                if manifest.get(name) != capture.get(name):
                    issue("selection.run_manifest", f"capture seal differs from run manifest field {name}")
            if not command_matches(config.get("run_command"), manifest):
                issue("run_command", "expanded builder template does not match the captured argv and case folder")
            try:
                start = datetime.fromisoformat(str(capture.get("started_at", "")).replace("Z", "+00:00"))
                end = datetime.fromisoformat(str(capture.get("ended_at", "")).replace("Z", "+00:00"))
                probed = datetime.fromisoformat(str(probe.get("checked_at", "")).replace("Z", "+00:00"))
                if start.tzinfo is None or end.tzinfo is None or probed.tzinfo is None:
                    raise ValueError("timestamps need timezone")
                if not 0 <= (end - start).total_seconds() <= 1800 or probed > start:
                    raise ValueError("probe must precede a build lasting at most thirty minutes")
            except (AttributeError, TypeError, ValueError):
                issue("selection.capture", "probe/start/end timestamps must be ordered and the build within thirty minutes")
            recorded_trace = manifest.get("trace") or {}
            if not isinstance(recorded_trace, dict) or recorded_trace.get("sha256") != seal.get("raw_capture_sha256"):
                issue("selection.run_manifest", "raw capture digest disagrees with the run manifest")
            snapshots = manifest.get("builder_artifacts") or {}
            exported_snapshot = snapshots.get("workflow") if isinstance(snapshots, dict) else None
            if (not isinstance(exported_snapshot, dict) or "workflow" not in paths
                    or (exported_snapshot.get("sha256"), exported_snapshot.get("size_bytes"))
                    != digest(paths["workflow"])):
                issue("workflow", "export differs from the snapshot captured when the selected builder run ended")
            summary = manifest.get("builder_summary") or {}
            if not isinstance(summary, dict):
                issue("selection.run_manifest", "captured builder final summary is missing")
            else:
                for name in ("workflow_id", "agent_id", "version", "status", "export_ok"):
                    if summary.get(name) != build.get(name):
                        issue("selection.run_manifest", f"captured final summary disagrees with selected build {name}")
        if capture.get("run_id") != build.get("run_id"):
            issue("selection.build", "builder run identity differs between the capture and selected build")
        if build.get("status") != "deployed" or build.get("export_ok") is not True:
            issue("selection.build", "selected build must be deployed with export_ok=true")
        for name in ("run_id", "workflow_id", "agent_id"):
            if not text(build.get(name)):
                issue(f"selection.build.{name}", "selected build identity is missing")
        version = build.get("version")
        if isinstance(version, bool) or not isinstance(version, int) or version <= 0:
            issue("selection.build.version", "selected build needs a positive published version number")
        if seal.get("trace_handling") not in {"raw", "known_secret_redaction"}:
            issue("selection.trace_handling", "trace must be raw output or the sealer's known-secret redaction")
        raw_hash = seal.get("raw_capture_sha256")
        if not isinstance(raw_hash, str) or not HEX_DIGEST.fullmatch(raw_hash):
            issue("selection.raw_capture_sha256", "raw process capture digest is missing")
        elif seal.get("trace_handling") == "raw" and "trace" in paths and raw_hash != digest(paths["trace"])[0]:
            issue("trace", "raw trace differs from the raw process capture digest")
        if "workflow" in paths:
            export = object_file(paths["workflow"])
            for name in ("workflow_id", "agent_id"):
                if export.get(name) != build.get(name):
                    issue("workflow", f"export {name} differs from the selected build")
            workflow = export.get("workflow")
            if (not isinstance(workflow, dict) or not isinstance(workflow.get("nodes"), dict)
                    or workflow.get("initial_node") not in workflow["nodes"]):
                issue("workflow", "expected a real Freya platform export with a valid initial node")
        if "build_result" in paths:
            result = object_file(paths["build_result"])
            for name in ("workflow_id", "agent_id", "version", "status", "export_ok"):
                if result.get(name) != build.get(name):
                    issue("selection.build", f"copied build result disagrees with seal field {name}")
            if result.get("cut_off") or result.get("untested_redeploy") or result.get("published") is False:
                issue("selection.build", "selected run is cut off, untested after replacement, or unpublished")
    except (OSError, UnicodeError, ValueError, TypeError):
        issue("selection", "could not read a bounded selection seal inside this repository")
    commit = config.get("commit")
    commit_checked = False
    if isinstance(commit, str) and HEX_SHA.fullmatch(commit):
        try:
            resolved = subprocess.run(["git", "-C", str(root), "rev-parse", "--verify", f"{commit}^{{commit}}"],
                                      capture_output=True, check=False, timeout=15)
            if resolved.returncode or resolved.stdout.decode("ascii").strip().lower() != commit.lower():
                raise ValueError("commit is not a resolvable full local commit SHA")
            for relative, sha, size in source_files:
                if git_blob_digest(root, commit, relative) != (sha, size):
                    issue(relative, "committed runtime bytes differ from the source captured before the selected run")
            for path in [*paths.values(), *deployment_files, selection]:
                relative = path.relative_to(root).as_posix()
                if git_blob_digest(root, commit, relative) != digest(path):
                    issue(relative, "raw committed blob bytes differ from the selected local artifact")
            commit_checked = True
        except (OSError, UnicodeError, ValueError, subprocess.TimeoutExpired):
            issue("commit", "selected commit cannot be verified or a sealed artifact is absent from it")
    return {"ready": not issues and commit_checked, "issues": issues, "checked_files": sorted(set(checked)),
            "commit_checked": commit_checked,
            "limits": ["Local checks do not prove organizer acceptance, hidden customer scores, "
                       "or that the Worker will remain reachable until results are announced."]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("inspect", "validate", "payload"))
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--config", required=True, help="repository-relative single-case configuration JSON")
    parser.add_argument("--selection", required=True, help="repository-relative selected run seal.json")
    parser.add_argument("--commit", help="use this real commit SHA in place of a blank draft field")
    parser.add_argument("--output", help="payload only: create this repository-relative JSON file; never overwrite")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        config = object_file(safe_path(root, args.config))
        if args.commit is not None:
            config["commit"] = args.commit
        selection = safe_path(root, args.selection, must_exist=False)
        report = check(config, selection, root)
        if args.command != "payload" or not report["ready"]:
            print(json.dumps(report, indent=2, ensure_ascii=False))
            return 0 if report["ready"] else 2
        output = json.dumps({key: config[key] for key in
                            ("team", "members", "repo", "commit", "run_command", "workflow", "trace", "mock_url")},
                            indent=2, ensure_ascii=False) + "\n"
        if args.output:
            normalized = args.output.replace("\\", "/")
            parts = PurePosixPath(normalized).parts
            if (PureWindowsPath(args.output).drive or PureWindowsPath(args.output).root
                    or PurePosixPath(normalized).is_absolute() or ".." in parts
                    or any(":" in part for part in parts) or "\0" in normalized):
                raise ValueError("output must be repository-relative")
            target = (root / normalized).resolve()
            target.relative_to(root)
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(output)
            print(json.dumps({"ready": True, "payload": target.relative_to(root).as_posix()}))
        else:
            sys.stdout.write(output)
        return 0
    except (OSError, UnicodeError, ValueError, TypeError):
        print(json.dumps({"ready": False, "issues": [{"field": "input",
              "message": "configuration, selection or output could not be accessed safely; no existing file was replaced"}]}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
