"""Capture one explicitly supplied command without decoding its stdout or stderr."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone

FORMAT_VERSION = 1
DEFAULT_RUNS = Path(__file__).resolve().parents[1] / "runs"
RUN_MARKERS = re.compile(rb"\| (START|END)\s*\| run (\S+) (started|closed)")
KEY_PATTERNS = (
    re.compile(rb"\bsk-ant-[A-Za-z0-9_\-]{10,}"),
    re.compile(rb"\bsk-[A-Za-z0-9_\-]{20,}"),
    re.compile(rb"\bfak_[A-Za-z0-9_\-]{10,}"),
    re.compile(rb"(?i)(\bBearer\s+)[A-Za-z0-9._\-~+/=]{16,}"),
)
SOURCE_FIXED = ("freya.py", "pyproject.toml", "uv.lock", "requirements.txt", ".python-version")
HEALTH_FIELDS = ("cloudflare_version_id", "bundle_sha256", "contract_sha256", "customer_fixture_sha256")
SUMMARY_FIELDS = ("slot", "workflow_id", "agent_id", "version", "status", "export_ok", "exported", "trace")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_info(path: Path, relative: str) -> dict:
    hasher = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(block)
            size += len(block)
    return {"path": relative, "sha256": hasher.hexdigest(), "size_bytes": size}


def read_object(path: Path) -> dict:
    value = json.loads(path.read_bytes().decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    return value


def atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    data = (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n").encode("utf-8")
    with temporary.open("xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def public_url(value: str) -> urllib.parse.SplitResult:
    url = urllib.parse.urlsplit(value)
    if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password or url.fragment:
        raise ValueError("A public HTTP(S) URL without credentials or a fragment is required")
    if any(pattern.search(value.encode("utf-8")) for pattern in KEY_PATTERNS):
        raise ValueError("Credential-shaped text is not allowed in URLs")
    # Probe metadata must never contain a credential-bearing query string.
    if any(re.search(r"(?i)(?:token|secret|password|api.?key|authorization)", key)
           for key, _ in urllib.parse.parse_qsl(url.query, keep_blank_values=True)):
        raise ValueError("Credential-bearing URL query names are not allowed")
    _ = url.port  # validate the port before attempting network access
    return url


def origin(value: str) -> tuple:
    url = public_url(value)
    return url.scheme, url.hostname.casefold(), url.port or (443 if url.scheme == "https" else 80)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def probe(url: str, timeout: float, expected_status: int | None) -> dict:
    receipt = {"url": url, "checked_at": utc_now(), "http_status": None, "ok": False}
    opener = urllib.request.build_opener(NoRedirect())
    try:
        request = urllib.request.Request(url, method="GET", headers={"User-Agent": "Freya-submission-readiness/1"})
        with opener.open(request, timeout=timeout) as response:
            receipt["http_status"] = response.status
            body = response.read(1024 * 1024 + 1)
            if len(body) > 1024 * 1024:
                raise ValueError("Readiness JSON exceeds the bound")
            metadata = json.loads(body.decode("utf-8"))
            if not isinstance(metadata, dict):
                raise ValueError("Readiness response must be a JSON object")
            receipt["metadata"] = metadata
        status = receipt["http_status"]
        receipt["ok"] = 200 <= status < 300 and (expected_status is None or status == expected_status)
    except urllib.error.HTTPError as exc:
        receipt["http_status"] = exc.code
        receipt["error_type"] = "HTTPError"
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, UnicodeDecodeError) as exc:
        receipt["error_type"] = type(exc).__name__
    return receipt


def trace_identity(data: bytes) -> str | None:
    markers = RUN_MARKERS.findall(data)
    if len(markers) != 2 or markers[0][0] != b"START" or markers[1][0] != b"END":
        return None
    if markers[0][1] != markers[1][1] or markers[0][2] != b"started" or markers[1][2] != b"closed":
        return None
    try:
        return markers[0][1].decode("ascii")
    except UnicodeDecodeError:
        return None


def final_summary(data: bytes) -> dict | None:
    lines = [line for line in data.splitlines() if line.strip()]
    if not lines:
        return None
    try:
        value = json.loads(lines[-1].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def snapshot_builder_outputs(summary: dict | None, cwd: Path) -> dict:
    artifacts = {}
    if summary is None:
        return artifacts
    for name, field in (("workflow", "exported"), ("builder_trace", "trace")):
        value = summary.get(field)
        if not isinstance(value, str) or not value:
            continue
        try:
            source = Path(value)
            source = (source if source.is_absolute() else cwd / source).resolve()
            relative = source.relative_to(cwd).as_posix()
            artifacts[name] = file_info(source, relative)
        except (OSError, ValueError):
            artifacts[name] = {"error": "Builder output could not be hashed within its working directory"}
    return artifacts


def snapshot_files(cwd: Path, selected: list[Path]) -> dict:
    files = []
    for path in sorted(set(selected), key=lambda item: item.as_posix()):
        real = path.resolve(strict=True)
        relative = real.relative_to(cwd).as_posix()
        files.append(file_info(real, relative))
    encoded = json.dumps(files, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {"captured_at": utc_now(), "files": files, "tree_sha256": digest(encoded)}


def source_snapshot(cwd: Path, command: list[str]) -> dict:
    selected = []
    for folder in (cwd / "builder", cwd / ".claude" / "skills"):
        if folder.is_dir():
            selected.extend(path for path in folder.rglob("*") if path.is_file()
                            and "__pycache__" not in path.parts and path.suffix not in (".pyc", ".pyo"))
    selected.extend(cwd / name for name in SOURCE_FIXED if (cwd / name).is_file())
    if len(command) > 1:
        candidate = Path(command[1])
        candidate = candidate if candidate.is_absolute() else cwd / candidate
        if candidate.is_file():
            selected.append(candidate)
    if not selected:
        raise ValueError("No builder source files were available to snapshot")
    return snapshot_files(cwd, selected)


def case_snapshot(cwd: Path, case_folder: Path) -> dict:
    return snapshot_files(cwd, [path for path in case_folder.rglob("*") if path.is_file()
                                and "__pycache__" not in path.parts and path.suffix not in (".pyc", ".pyo")])


def deployment_evidence(receipt_path: Path, report_path: Path, cwd: Path, mock_url: str, probe_url: str) -> tuple:
    receipt_bytes, report_bytes = receipt_path.read_bytes(), report_path.read_bytes()
    receipt = json.loads(receipt_bytes.decode("utf-8"))
    report = json.loads(report_bytes.decode("utf-8"))
    if not isinstance(receipt, dict) or not isinstance(report, dict):
        raise ValueError("Deployment evidence must be JSON objects")
    if receipt.get("schema_version") != 1 or receipt.get("bank_ready") is not True:
        raise ValueError("Missing bank-ready deployment receipt")
    if report.get("report_schema_version") != 1 or report.get("bank_ready") is not True:
        raise ValueError("Missing authoritative bank readiness report")
    if receipt.get("base_url") != mock_url or report.get("base_url") != mock_url:
        raise ValueError("The deployment evidence belongs to another mock URL")
    if receipt.get("health_url") != probe_url or report.get("health_url") != probe_url:
        raise ValueError("The readiness probe differs from the verified deployment health URL")
    for name in ("deployment_id", "cloudflare_version_id"):
        if not isinstance(receipt.get(name), str) or not receipt[name] or receipt[name] != report.get(name):
            raise ValueError("Deployment/version identities do not match")
    if report.get("receipt_bytes_sha256") != digest(receipt_bytes):
        raise ValueError("The readiness report verified different receipt bytes")
    if report.get("receipt_path") != receipt_path.resolve().relative_to(cwd).as_posix():
        raise ValueError("The readiness report verified another receipt path")
    live_expected = {"cloudflare_version_id": receipt["cloudflare_version_id"],
                     "bundle_sha256": receipt.get("bundle", {}).get("sha256"),
                     "contract_sha256": receipt.get("contract", {}).get("sha256"),
                     "customer_fixture_sha256": receipt.get("customer_fixtures", {}).get("sha256")}
    if any(not isinstance(value, str) or not value for value in live_expected.values()):
        raise ValueError("Deployment input hashes are missing")
    if any(report.get(key) != value for key, value in live_expected.items()):
        raise ValueError("The readiness report verified different deployment inputs")
    artifacts = [receipt.get(key) for key in ("bundle", "configuration", "contract", "customer_fixtures")]
    sources = receipt.get("source_files")
    if not isinstance(sources, list) or not sources:
        raise ValueError("Deployment source evidence is missing")
    artifacts.extend(sources)
    storage = receipt.get("storage", {})
    if not isinstance(storage, dict) or not storage.get("backend") or storage.get("persistence_verified") is not True or storage.get("customer_isolation_verified") is not True:
        raise ValueError("Persistent storage and customer isolation have not been verified")
    artifacts.append(storage.get("evidence"))
    tests = receipt.get("contract_tests", {})
    if not isinstance(tests, dict) or tests.get("passed") is not True or not isinstance(tests.get("case_count"), int) or isinstance(tests.get("case_count"), bool) or tests["case_count"] < 1:
        raise ValueError("Live bank contract tests have not passed")
    artifacts.append(tests.get("report"))
    verified = []
    for artifact in artifacts:
        if not isinstance(artifact, dict) or not isinstance(artifact.get("path"), str):
            raise ValueError("Deployment artifact proof is malformed")
        relative = Path(artifact["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Deployment artifact proof path is unsafe")
        real = (cwd / relative).resolve(strict=True)
        real.relative_to(cwd)
        info = file_info(real, relative.as_posix())
        if info["sha256"] != artifact.get("sha256"):
            raise ValueError("Deployment artifact bytes differ from their verified receipt")
        verified.append(info)
    try:
        deployed = datetime.fromisoformat(receipt["deployed_at_utc"].replace("Z", "+00:00"))
        checked = datetime.fromisoformat(report["verified_at_utc"].replace("Z", "+00:00"))
        if deployed.utcoffset() is None or checked.utcoffset() is None or not deployed <= checked <= datetime.now(timezone.utc):
            raise ValueError("Deployment timestamps are not ordered")
    except (KeyError, TypeError) as exc:
        raise ValueError("Deployment timestamps are malformed") from exc
    return receipt_bytes, report_bytes, live_expected, verified


def require_bank_probe(receipt: dict, expected: dict) -> None:
    live = receipt.get("metadata")
    if not receipt.get("ok") or not isinstance(live, dict) or live.get("ready") is not True or live.get("bank_ready") is not True:
        receipt["ok"] = False
        receipt["error_type"] = "BankNotReady"
        return
    if any(live.get(key) != value for key, value in expected.items()):
        receipt["ok"] = False
        receipt["error_type"] = "DeploymentMetadataMismatch"


def run_capture(args: argparse.Namespace) -> tuple[Path, dict]:
    command = list(args.command)
    if command and command[0] == "--":
        command.pop(0)
    if not command:
        raise ValueError("Supply the exact child argv after --")
    cwd = args.cwd.resolve(strict=True)
    if not cwd.is_dir():
        raise ValueError("The working directory must exist")
    case_folder = Path(args.case_folder)
    case_folder = (case_folder if case_folder.is_absolute() else cwd / case_folder).resolve(strict=True)
    case_folder.relative_to(cwd)
    if not case_folder.is_dir() or args.case_folder not in command:
        raise ValueError("The existing case folder must be supplied exactly in the child argv")
    public_url(args.mock_url)
    public_url(args.probe_url)
    if origin(args.mock_url) != origin(args.probe_url):
        raise ValueError("The readiness probe must use the mock URL's origin")
    if args.mock_url not in command:
        raise ValueError("The exact mock URL must be present in the child argv")
    if any(pattern.search(item.encode("utf-8")) for item in command for pattern in KEY_PATTERNS):
        raise ValueError("Do not place credentials in command arguments")
    if not 0 < args.probe_timeout <= 60:
        raise ValueError("Probe timeout must be greater than zero and at most 60 seconds")
    if args.probe_status is not None and not 200 <= args.probe_status < 300:
        raise ValueError("An expected readiness status must be a 2xx status")
    receipt_bytes, report_bytes, live_expected, evidence_files = deployment_evidence(
        args.deployment_receipt.resolve(), args.readiness_report.resolve(), cwd, args.mock_url, args.probe_url)
    runs = args.runs_dir.resolve()
    runs.mkdir(parents=True, exist_ok=True)
    capture_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid.uuid4().hex
    directory = runs / capture_id
    directory.mkdir()  # a unique directory, never reuse or overwrite one
    manifest_path = directory / "run.json"
    manifest = {"format_version": FORMAT_VERSION, "capture_id": capture_id, "run_id": None,
                "state": "preparing", "command": command, "cwd": str(cwd), "mock_url": args.mock_url,
                "case_folder": args.case_folder, "case_folder_path": str(case_folder),
                "created_at": utc_now(), "started_at": None, "ended_at": None, "returncode": None,
                "probe": None, "trace": None, "builder_summary": None, "builder_artifacts": {}}
    for key, data, original in (("deployment_receipt", receipt_bytes, args.deployment_receipt),
                                ("readiness_report", report_bytes, args.readiness_report)):
        filename = key.replace("_", "-") + ".json"
        with (directory / filename).open("xb") as handle:
            handle.write(data)
        manifest[key] = {"path": filename, "sha256": digest(data), "size_bytes": len(data),
                         "source_path": original.resolve().relative_to(cwd).as_posix()}
    manifest["deployment_evidence_files"] = evidence_files
    manifest["source_snapshot"] = source_snapshot(cwd, command)
    manifest["case_snapshot"] = case_snapshot(cwd, case_folder)
    case_hashes = {item["sha256"] for item in manifest["case_snapshot"]["files"]}
    if any(live_expected[key] not in case_hashes for key in ("contract_sha256", "customer_fixture_sha256")):
        raise ValueError("Deployed bank contract/customer inputs do not belong to this case folder")
    manifest["source_stable"] = False
    manifest["case_stable"] = False
    atomic_json(manifest_path, manifest)
    manifest["probe"] = probe(args.probe_url, args.probe_timeout, args.probe_status)
    require_bank_probe(manifest["probe"], live_expected)
    if not manifest["probe"]["ok"]:
        manifest["state"] = "probe_failed"
        manifest["ended_at"] = utc_now()
        atomic_json(manifest_path, manifest)
        return directory, manifest
    trace_path = directory / "console.raw.log"
    manifest["started_at"] = utc_now()
    manifest["state"] = "running"
    atomic_json(manifest_path, manifest)
    start = time.monotonic()
    process = None
    with trace_path.open("xb", buffering=0) as output:
        try:
            process = subprocess.Popen(command, cwd=cwd, stdout=output, stderr=subprocess.STDOUT, shell=False)
            manifest["returncode"] = process.wait()
            manifest["state"] = "complete"
        except KeyboardInterrupt:
            manifest["state"] = "interrupted"
            if process is not None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                manifest["returncode"] = process.returncode
        except OSError as exc:
            manifest["state"] = "launch_failed"
            manifest["error_type"] = type(exc).__name__
        finally:
            os.fsync(output.fileno())
    manifest["ended_at"] = utc_now()
    manifest["source_after"] = source_snapshot(cwd, command)
    manifest["case_after"] = case_snapshot(cwd, case_folder)
    manifest["source_stable"] = manifest["source_snapshot"]["files"] == manifest["source_after"]["files"]
    manifest["case_stable"] = manifest["case_snapshot"]["files"] == manifest["case_after"]["files"]
    manifest["elapsed_s"] = round(time.monotonic() - start, 6)
    manifest["trace"] = file_info(trace_path, trace_path.name)
    raw = trace_path.read_bytes()
    manifest["run_id"] = trace_identity(raw)
    summary = final_summary(raw)
    if summary:
        manifest["builder_summary"] = {key: summary.get(key) for key in SUMMARY_FIELDS}
    manifest["builder_artifacts"] = snapshot_builder_outputs(summary, cwd)
    atomic_json(manifest_path, manifest)
    return directory, manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cwd", type=Path, default=Path.cwd())
    parser.add_argument("--runs-dir", type=Path, default=DEFAULT_RUNS)
    parser.add_argument("--case-folder", required=True, help="Exact case folder argument passed to the builder")
    parser.add_argument("--deployment-receipt", required=True, type=Path)
    parser.add_argument("--readiness-report", required=True, type=Path)
    parser.add_argument("--mock-url", required=True, help="Exact deployed mock URL passed to the builder")
    parser.add_argument("--probe-url", required=True, help="Supplied readiness URL; no bank route is assumed")
    parser.add_argument("--probe-status", type=int, help="Optional expected 2xx readiness status")
    parser.add_argument("--probe-timeout", type=float, default=10)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        directory, manifest = run_capture(args)
        print(json.dumps({"run_dir": str(directory), "state": manifest["state"], "returncode": manifest["returncode"]}))
        if manifest["state"] == "interrupted":
            return 130
        if manifest["state"] != "complete":
            return 2
        return manifest["returncode"]
    except (OSError, ValueError) as exc:
        print("Capture refused: " + type(exc).__name__, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())