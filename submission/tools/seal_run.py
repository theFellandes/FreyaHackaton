"""Seal one vetted builder run as exact bytes; verify its immutable artifact bundle."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re
import sys

try:
    from .capture_run import KEY_PATTERNS, digest, final_summary, origin, read_object, trace_identity, utc_now
except ImportError:
    from capture_run import KEY_PATTERNS, digest, final_summary, origin, read_object, trace_identity, utc_now

ARTIFACT_NAMES = {"workflow": "workflow.json", "trace": "trace.log", "build_result": "build-result.json",
                  "run_manifest": "capture.json", "deployment_receipt": "deployment-receipt.json",
                  "readiness_report": "readiness-report.json"}
SUPPORT_FILES = {".gitattributes": b"* -text -filter\n", ".gitignore": b"!trace.log\n"}
IDENTITY_FIELDS = ("workflow_id", "agent_id", "version", "status", "export_ok", "slot", "exported", "trace")


class EvidenceError(ValueError):
    """A fixed, safe message without any source file contents or secret values."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise EvidenceError(message)


def known_secrets(names: list[str]) -> list[bytes]:
    values = []
    for name in names:
        require(name in os.environ and len(os.environ[name]) >= 4,
                "A requested secret environment variable is missing or too short")
        values.append(os.environ[name].encode("utf-8"))
    return sorted(set(values), key=len, reverse=True)


def redact_bytes(raw: bytes, values: list[bytes]) -> bytes:
    """Only remove known secret substrings and recognized key-shaped tokens."""
    result = raw
    for value in values:
        result = result.replace(value, b"***")
    for pattern in KEY_PATTERNS:
        if b"Bearer" in pattern.pattern:
            result = pattern.sub(lambda match: match.group(1) + b"***", result)
        else:
            result = pattern.sub(b"***", result)
    return result


def scan_clean(data: bytes, values: list[bytes], label: str) -> None:
    require(not any(value in data for value in values), label + " contains a configured secret")
    require(not any(pattern.search(data) for pattern in KEY_PATTERNS), label + " contains credential-shaped text")


def object_bytes(data: bytes, label: str) -> dict:
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceError(label + " is not a UTF-8 JSON object") from exc
    require(isinstance(value, dict), label + " is not a JSON object")
    return value


def positive_version(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def validate_capture(capture: dict) -> None:
    require(capture.get("format_version") == 1 and capture.get("state") == "complete",
            "The capture is incomplete or uses an unsupported format")
    require(capture.get("source_stable") is True and capture.get("case_stable") is True,
            "Builder source or case inputs changed during the captured run")
    for prefix in ("source", "case"):
        before = capture.get(prefix + "_snapshot")
        after = capture.get(prefix + "_after")
        require(isinstance(before, dict) and isinstance(after, dict) and before.get("files")
                and before.get("files") == after.get("files"), "Missing stable source/input file snapshots")
        encoded = json.dumps(before["files"], sort_keys=True, separators=(",", ":")).encode("utf-8")
        require(digest(encoded) == before.get("tree_sha256") == after.get("tree_sha256"),
                "Source/input snapshot digests differ")
    require(capture.get("returncode") == 0, "The builder did not exit successfully")
    require(isinstance(capture.get("capture_id"), str) and bool(capture["capture_id"]), "Missing capture identity")
    require(isinstance(capture.get("run_id"), str) and bool(capture["run_id"]), "Missing complete builder run identity")
    require(isinstance(capture.get("command"), list) and all(isinstance(arg, str) for arg in capture["command"]),
            "Capture argv is malformed")
    require(isinstance(capture.get("case_folder"), str) and capture["case_folder"] in capture["command"],
            "The captured command did not receive its case folder")
    require(capture.get("mock_url") in capture["command"], "The captured command did not receive the mock URL")
    receipt = capture.get("probe")
    require(isinstance(receipt, dict) and receipt.get("ok") is True, "Missing successful pre-build readiness probe")
    status = receipt.get("http_status")
    require(isinstance(status, int) and not isinstance(status, bool) and 200 <= status < 300,
            "The pre-build probe did not return a 2xx status")
    require(origin(receipt.get("url", "")) == origin(capture.get("mock_url", "")),
            "The readiness receipt belongs to another mock origin")
    try:
        checked = datetime.fromisoformat(receipt["checked_at"].replace("Z", "+00:00"))
        start = datetime.fromisoformat(capture["started_at"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(capture["ended_at"].replace("Z", "+00:00"))
        require(checked.utcoffset() is not None and start.utcoffset() is not None and end.utcoffset() is not None,
                "Capture timestamps must include timezones")
        require(checked <= start <= end, "The mock probe must precede the ordered builder run")
        require((end - start).total_seconds() <= 1800, "The captured build exceeded 30 minutes")
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, EvidenceError):
            raise
        raise EvidenceError("Capture timestamps are malformed") from exc


def validate_build(build: dict, capture: dict, trace: bytes, workflow: dict) -> str:
    require(build.get("status") == "deployed" and build.get("export_ok") is True,
            "A deployed build with a verified platform export is required")
    require(not build.get("cut_off") and not build.get("untested_redeploy") and build.get("published") is not False,
            "Cutoff, unpublished, and untested replacement builds cannot be sealed")
    for field in ("workflow_id", "agent_id"):
        require(isinstance(build.get(field), str) and bool(build[field].strip()), "Missing deployment identity")
        require(workflow.get(field) == build[field], "The platform export belongs to another deployment")
    require(positive_version(build.get("version")), "Missing positive deployed version")
    if "version" in workflow:
        require(workflow["version"] == build["version"], "The exported version differs from the selected build")
    require(isinstance(workflow.get("workflow"), dict), "Expected the actual Freya export wrapper")
    run_id = trace_identity(trace)
    require(bool(run_id) and run_id == capture["run_id"], "The selected trace belongs to another or incomplete run")
    if build.get("run_id") is not None:
        require(build["run_id"] == run_id, "Build-result run identity differs from the selected trace")
    stdout = final_summary(trace)
    require(isinstance(stdout, dict), "The complete console trace must retain its final builder JSON")
    summary = capture.get("builder_summary")
    require(isinstance(summary, dict), "The capture lacks the final builder JSON evidence")
    for field in IDENTITY_FIELDS:
        require(build.get(field) == summary.get(field) == stdout.get(field),
                "Build-result identity or artifact paths differ from the captured builder JSON")
    return run_id


def relative_capture_trace(capture_path: Path, capture: dict) -> Path:
    info = capture.get("trace")
    require(isinstance(info, dict), "The capture has no raw trace metadata")
    value = info.get("path")
    require(isinstance(value, str) and bool(value), "The raw trace path is missing")
    relative = Path(value)
    require(not relative.is_absolute() and ".." not in relative.parts, "The raw trace path is unsafe")
    source = (capture_path.parent / relative).resolve(strict=True)
    require(source.parent == capture_path.parent.resolve(), "The raw capture escapes its run directory")
    return source


def deployment_capture_file(run_manifest: Path, capture: dict, key: str) -> bytes:
    info = capture.get(key)
    require(isinstance(info, dict) and isinstance(info.get("path"), str), "Deployment evidence is missing")
    expected = key.replace("_", "-") + ".json"
    require(info["path"] == expected, "Deployment evidence path is unsafe")
    source = run_manifest.parent / expected
    require(source.is_file() and not source.is_symlink(), "Deployment evidence file is missing or unsafe")
    data = source.read_bytes()
    require(digest(data) == info.get("sha256") and len(data) == info.get("size_bytes"),
            "Deployment evidence changed after capture")
    return data


def validate_deployment_docs(data: dict, capture: dict) -> None:
    receipt = object_bytes(data["deployment_receipt"], "Deployment receipt")
    report = object_bytes(data["readiness_report"], "Readiness report")
    require(receipt.get("schema_version") == 1 and receipt.get("bank_ready") is True,
            "Missing bank-ready deployment receipt")
    require(report.get("report_schema_version") == 1 and report.get("bank_ready") is True,
            "Missing authoritative readiness report")
    require(receipt.get("base_url") == report.get("base_url") == capture.get("mock_url"),
            "Deployment evidence belongs to another mock URL")
    probe = capture["probe"]
    require(receipt.get("health_url") == report.get("health_url") == probe.get("url"),
            "Deployment health URL differs from the pre-build probe")
    require(report.get("receipt_bytes_sha256") == digest(data["deployment_receipt"]),
            "Readiness report verified different receipt bytes")
    require(report.get("receipt_path") == capture["deployment_receipt"].get("source_path"),
            "Readiness report verified another receipt path")
    for key in ("deployment_id", "cloudflare_version_id"):
        require(isinstance(receipt.get(key), str) and bool(receipt[key]) and receipt[key] == report.get(key),
                "Deployment/version identity mismatch")
    live = probe.get("metadata")
    require(isinstance(live, dict) and live.get("ready") is True and live.get("bank_ready") is True,
            "The live bank did not explicitly report readiness")
    expected = {"cloudflare_version_id": receipt["cloudflare_version_id"],
                "bundle_sha256": receipt.get("bundle", {}).get("sha256"),
                "contract_sha256": receipt.get("contract", {}).get("sha256"),
                "customer_fixture_sha256": receipt.get("customer_fixtures", {}).get("sha256")}
    case_hashes = {item.get("sha256") for item in capture["case_snapshot"]["files"]}
    require(expected["contract_sha256"] in case_hashes and expected["customer_fixture_sha256"] in case_hashes,
            "The deployed bank contract/customer inputs do not belong to the selected case folder")
    require(all(isinstance(value, str) and value and report.get(key) == value == live.get(key)
                for key, value in expected.items()), "Live deployment inputs differ from the verified bank")
    for key in ("deployment_receipt", "readiness_report"):
        info = capture.get(key, {})
        require(info.get("sha256") == digest(data[key]) and info.get("size_bytes") == len(data[key]),
                "Deployment evidence digest mismatch")
    try:
        deployed = datetime.fromisoformat(receipt["deployed_at_utc"].replace("Z", "+00:00"))
        verified = datetime.fromisoformat(report["verified_at_utc"].replace("Z", "+00:00"))
        probed = datetime.fromisoformat(probe["checked_at"].replace("Z", "+00:00"))
        require(deployed <= verified <= probed, "The bank was not verified before the builder probe")
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, EvidenceError):
            raise
        raise EvidenceError("Deployment evidence timestamps are malformed") from exc


def seal_run(*, run_manifest: Path, workflow: Path, trace: Path, build_result: Path, output: Path,
             vet_trace: bool, allow_known_secret_redaction: bool = False, secret_env: list[str] | None = None) -> dict:
    require(vet_trace, "Review the selected trace and acknowledge it with --vet-trace")
    require(not output.exists(), "The seal destination already exists; use a fresh directory")
    values = known_secrets(secret_env or [])
    captured = run_manifest.read_bytes()
    capture = object_bytes(captured, "Capture manifest")
    validate_capture(capture)
    raw = relative_capture_trace(run_manifest.resolve(), capture).read_bytes()
    raw_info = capture["trace"]
    require(digest(raw) == raw_info.get("sha256") and len(raw) == raw_info.get("size_bytes"),
            "Raw capture bytes changed after the run")
    selected_trace = trace.read_bytes()
    try:
        selected_trace.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EvidenceError("The vetted trace must be plain UTF-8 text") from exc
    handling = "raw"
    if selected_trace != raw:
        require(allow_known_secret_redaction, "The trace differs from its raw capture")
        require(selected_trace == redact_bytes(raw, values), "The trace contains changes beyond known secret removal")
        handling = "known_secret_redaction"
    data = {"workflow": workflow.read_bytes(), "trace": selected_trace,
            "build_result": build_result.read_bytes(), "run_manifest": captured,
            "deployment_receipt": deployment_capture_file(run_manifest, capture, "deployment_receipt"),
            "readiness_report": deployment_capture_file(run_manifest, capture, "readiness_report")}
    validate_deployment_docs(data, capture)
    for label, contents in data.items():
        scan_clean(contents, values, label)
    export = object_bytes(data["workflow"], "Workflow export")
    build = object_bytes(data["build_result"], "Build result")
    run_id = validate_build(build, capture, selected_trace, export)
    recorded = capture.get("builder_artifacts", {}).get("workflow")
    require(isinstance(recorded, dict) and recorded.get("sha256") == digest(data["workflow"])
            and recorded.get("size_bytes") == len(data["workflow"]),
            "Export bytes differ from the capture's end-of-run exporter snapshot")
    # The selected export is matched by bytes, not by a mutable output filename.
    artifacts = {name: {"path": ARTIFACT_NAMES[name], "sha256": digest(contents), "size_bytes": len(contents)}
                 for name, contents in data.items()}
    manifest = {"format_version": 1, "sealed_at": utc_now(), "trace_handling": handling,
                "raw_capture_sha256": raw_info["sha256"],
                "capture": {key: capture[key] for key in ("capture_id", "run_id", "started_at", "ended_at",
                                                          "returncode", "mock_url", "probe")},
                "build": {"run_id": run_id, **{key: build[key] for key in
                                               ("workflow_id", "agent_id", "version", "status", "export_ok")}},
                "artifacts": artifacts, "trace_vetted": True,
                "secret_scan": {"generic_key_shapes": True, "environment_names": sorted(set(secret_env or []))}}
    manifest["capture"]["manifest_sha256"] = artifacts["run_manifest"]["sha256"]
    output.parent.mkdir(parents=True, exist_ok=True)
    output.mkdir()  # exclusive creation; partial directories are never reused
    for name, contents in data.items():
        with (output / ARTIFACT_NAMES[name]).open("xb") as handle:
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())
    for name, contents in SUPPORT_FILES.items():
        with (output / name).open("xb") as handle:
            handle.write(contents)
    with (output / "seal.json").open("xb") as handle:
        handle.write((json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=True) + "\n").encode("utf-8"))
        handle.flush()
        os.fsync(handle.fileno())
    verify_seal(output)
    return manifest


def verify_seal(directory: Path) -> dict:
    directory = directory.resolve(strict=True)
    seal = read_object(directory / "seal.json")
    require(seal.get("format_version") == 1 and seal.get("trace_vetted") is True, "Unsupported or unvetted seal")
    artifacts = seal.get("artifacts")
    require(isinstance(artifacts, dict) and set(artifacts) == set(ARTIFACT_NAMES), "The sealed artifact set changed")
    expected = set(ARTIFACT_NAMES.values()) | set(SUPPORT_FILES) | {"seal.json"}
    require({path.name for path in directory.iterdir()} == expected, "The seal directory file set changed")
    data = {}
    for name, filename in ARTIFACT_NAMES.items():
        info = artifacts[name]
        require(isinstance(info, dict) and info.get("path") == filename, "The sealed artifact path changed")
        path = directory / filename
        require(path.is_file() and not path.is_symlink(), "A sealed artifact is absent or is a symlink")
        data[name] = path.read_bytes()
        require(digest(data[name]) == info.get("sha256") and len(data[name]) == info.get("size_bytes"),
                name + " bytes changed after sealing")
        scan_clean(data[name], [], name)
    for name, contents in SUPPORT_FILES.items():
        require((directory / name).read_bytes() == contents, "Artifact preservation attributes changed")
    capture = object_bytes(data["run_manifest"], "Capture manifest")
    validate_capture(capture)
    validate_deployment_docs(data, capture)
    require(seal.get("capture", {}).get("manifest_sha256") == artifacts["run_manifest"]["sha256"],
            "Capture provenance digest changed")
    for key in ("capture_id", "run_id", "started_at", "ended_at", "returncode", "mock_url", "probe"):
        require(seal["capture"].get(key) == capture.get(key), "Capture provenance metadata changed")
    require(capture.get("trace", {}).get("sha256") == seal.get("raw_capture_sha256"), "Raw capture digest changed")
    handling = seal.get("trace_handling")
    require(handling in ("raw", "known_secret_redaction"), "Unknown trace handling")
    if handling == "raw":
        require(artifacts["trace"]["sha256"] == seal["raw_capture_sha256"], "The raw selected trace changed")
    export = object_bytes(data["workflow"], "Workflow export")
    build = object_bytes(data["build_result"], "Build result")
    run_id = validate_build(build, capture, data["trace"], export)
    require(seal.get("build") == {"run_id": run_id, **{key: build[key] for key in
                                                     ("workflow_id", "agent_id", "version", "status", "export_ok")}},
            "Deployment provenance metadata changed")
    recorded = capture.get("builder_artifacts", {}).get("workflow", {})
    require(recorded.get("sha256") == artifacts["workflow"]["sha256"]
            and recorded.get("size_bytes") == artifacts["workflow"]["size_bytes"], "Exporter snapshot differs")
    return seal


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    sealing = commands.add_parser("seal")
    for name in ("run-manifest", "workflow", "trace", "build-result", "output"):
        sealing.add_argument("--" + name, required=True, type=Path)
    sealing.add_argument("--vet-trace", action="store_true")
    sealing.add_argument("--allow-known-secret-redaction", action="store_true")
    sealing.add_argument("--secret-env", action="append", default=[])
    commands.add_parser("verify").add_argument("directory", type=Path)
    redacting = commands.add_parser("redact", help="Produce only known-secret removal from an untouched raw capture")
    redacting.add_argument("--run-manifest", required=True, type=Path)
    redacting.add_argument("--output", required=True, type=Path)
    redacting.add_argument("--secret-env", action="append", default=[])
    args = parser.parse_args(argv)
    try:
        if args.command == "redact":
            capture = read_object(args.run_manifest)
            validate_capture(capture)
            raw = relative_capture_trace(args.run_manifest.resolve(), capture).read_bytes()
            require(digest(raw) == capture["trace"]["sha256"] and len(raw) == capture["trace"]["size_bytes"],
                    "Raw capture bytes changed after the run")
            values = known_secrets(args.secret_env)
            vetted = redact_bytes(raw, values)
            scan_clean(vetted, values, "vetted trace")
            with args.output.open("xb") as handle:
                handle.write(vetted)
                handle.flush()
                os.fsync(handle.fileno())
            print(json.dumps({"redacted_trace": str(args.output), "sha256": digest(vetted), "size_bytes": len(vetted)}))
            return 0
        if args.command == "verify":
            seal = verify_seal(args.directory)
        else:
            seal = seal_run(run_manifest=args.run_manifest, workflow=args.workflow, trace=args.trace,
                            build_result=args.build_result, output=args.output, vet_trace=args.vet_trace,
                            allow_known_secret_redaction=args.allow_known_secret_redaction, secret_env=args.secret_env)
        print(json.dumps({"verified": True, "run_id": seal["build"]["run_id"], "workflow_id": seal["build"]["workflow_id"]}))
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        message = str(exc) if isinstance(exc, EvidenceError) else type(exc).__name__
        print("Seal refused: " + message, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())