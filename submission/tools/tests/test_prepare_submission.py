"""Offline checks for submission packaging; all generated packets are temporary."""
import contextlib
import hashlib
import importlib.util
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("submission_preparer", TOOLS / "prepare_submission.py")
assert SPEC is not None and SPEC.loader is not None
PREPARE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PREPARE)


class SubmissionPreparationTests(unittest.TestCase):
    def setUp(self):
        self.workspace = Path(__file__).resolve().parent
        self.temporary = tempfile.TemporaryDirectory(prefix="packet-test-", dir=self.workspace)
        self.root = Path(self.temporary.name).resolve()
        self.root.relative_to(self.workspace.resolve())
        self.packet = self.root / "packet"
        self.packet.mkdir()
        self.run_id = "0123456789ab"
        (self.root / "builder").mkdir()
        (self.root / "builder" / "build.py").write_text("# Offline source fixture\n", encoding="utf-8")
        (self.root / "freya.py").write_text("# Offline exporter source fixture\n", encoding="utf-8")
        (self.root / "submission" / "kit" / "case").mkdir(parents=True)
        (self.root / "submission" / "kit" / "case" / "BRIEF.md").write_text("# Offline case fixture\n", encoding="utf-8")
        (self.root / "submission" / "kit" / "case" / "contract.json").write_text('{"offline_fixture": true}\n', encoding="utf-8")
        (self.root / "submission" / "kit" / "case" / "customers.json").write_text('{"offline_fixture": true}\n', encoding="utf-8")
        (self.packet / ".gitattributes").write_bytes(b"* -text -filter\n")
        (self.packet / ".gitignore").write_bytes(b"!trace.log\n")
        (self.root / "bank").mkdir()
        for name in ("bundle.js", "wrangler.jsonc", "contract.json", "customers.json",
                     "source.js", "storage-proof.json", "contract-tests.json"):
            (self.root / "bank" / name).write_text('{"offline_fixture": true}\n', encoding="utf-8")
        artifact = lambda name: {key: value for key, value in self.root_entry("bank/" + name).items()
                                 if key != "size_bytes"}
        self.receipt = {
            "schema_version": 1, "bank_ready": True, "worker_name": "offline-fixture-worker",
            "base_url": "https://test-worker.team.workers.dev",
            "health_url": "https://test-worker.team.workers.dev/__submission_health",
            "deployment_id": "fixture-deployment", "cloudflare_version_id": "fixture-version",
            "deployed_at_utc": "2026-10-11T17:00:00Z",
            "bundle": artifact("bundle.js"), "configuration": artifact("wrangler.jsonc"),
            "contract": artifact("contract.json"), "customer_fixtures": artifact("customers.json"),
            "source_files": [artifact("source.js")],
            "storage": {"backend": "offline-test-fixture", "persistence_verified": True,
                        "customer_isolation_verified": True, "evidence": artifact("storage-proof.json")},
            "contract_tests": {"passed": True, "case_count": 1, "report": artifact("contract-tests.json")},
        }
        self.write("deployment-receipt.json", self.receipt)
        self.readiness = {
            "report_schema_version": 1, "bank_ready": True,
            "deployment_id": self.receipt["deployment_id"],
            "cloudflare_version_id": self.receipt["cloudflare_version_id"],
            "base_url": self.receipt["base_url"], "health_url": self.receipt["health_url"],
            "bundle_sha256": self.receipt["bundle"]["sha256"],
            "contract_sha256": self.receipt["contract"]["sha256"],
            "customer_fixture_sha256": self.receipt["customer_fixtures"]["sha256"],
            "verified_at_utc": "2026-10-11T17:00:57Z",
            "receipt_path": "packet/deployment-receipt.json",
            "receipt_bytes_sha256": self.entry("deployment-receipt.json")["sha256"],
        }
        self.write("readiness-report.json", self.readiness)
        self.summary = {"workflow_id": "test-workflow", "agent_id": "test-agent",
                        "version": 2, "status": "deployed", "export_ok": True}
        self.write("workflow.json", {"workflow_id": "test-workflow", "agent_id": "test-agent",
                                    "workflow": {"initial_node": "start", "nodes": {"start": {"type": "conversation"}}}})
        self.write("build-result.json", {**self.summary, "run_id": self.run_id, "published": True})
        self.trace = self.packet / "trace.log"
        self.trace.write_bytes(
            b"2026-10-11T17:01:00Z | START | run 0123456789ab started\n"
            b"2026-10-11T17:02:00Z | END | run 0123456789ab closed\n"
            + json.dumps(self.summary).encode() + b"\n")
        self.manifest = {
            "format_version": 1, "state": "complete", "capture_id": "test-capture",
            "run_id": self.run_id, "started_at": "2026-10-11T17:01:00Z",
            "ended_at": "2026-10-11T17:02:00Z", "returncode": 0,
            "mock_url": "https://test-worker.team.workers.dev",
            "probe": {"url": "https://test-worker.team.workers.dev/__submission_health",
                      "http_status": 200, "checked_at": "2026-10-11T17:00:59Z", "ok": True,
                      "metadata": {"ready": True, "bank_ready": True,
                                   **{name: self.readiness[name] for name in
                                      ("cloudflare_version_id", "bundle_sha256", "contract_sha256",
                                       "customer_fixture_sha256")}}},
            "case_folder": "submission/kit/case",
            "command": ["python3", "builder/build.py", "submission/kit/case",
                        "https://test-worker.team.workers.dev"],
            "builder_summary": self.summary,
        }
        self.refresh_seal()
        self.config = {"team": "Offline test team",
                       "members": [{"name": "Test Member", "email": "member@valid.test"}],
                       "repo": "https://github.com/test-org/test-repository",
                       "commit": "", "run_command": PREPARE.RUN_COMMAND,
                       "workflow": "packet/workflow.json", "trace": "packet/trace.log",
                       "mock_url": self.manifest["mock_url"]}

    def tearDown(self):
        self.root.relative_to(self.workspace.resolve())
        self.temporary.cleanup()

    def write(self, name, value):
        (self.packet / name).write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8", newline="\n")

    def entry(self, name):
        sha, size = PREPARE.digest(self.packet / name)
        return {"path": name, "sha256": sha, "size_bytes": size}

    def root_entry(self, name):
        sha, size = PREPARE.digest(self.root / name)
        return {"path": name, "sha256": sha, "size_bytes": size}

    def refresh_seal(self):
        self.manifest["trace"] = {**self.entry("trace.log"), "path": "console.raw.log"}
        self.manifest["builder_artifacts"] = {"workflow": self.entry("workflow.json")}
        if "source_snapshot" not in self.manifest:
            entries = [self.root_entry(name) for name in sorted(PREPARE.runtime_files(self.root))]
            tree = hashlib.sha256(json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            self.manifest["source_snapshot"] = {"captured_at": "2026-10-11T17:00:58Z",
                                                "files": entries, "tree_sha256": tree}
            self.manifest["source_after"] = {"captured_at": "2026-10-11T17:02:01Z",
                                             "files": entries, "tree_sha256": tree}
            self.manifest["source_stable"] = True
            case_entries = [self.root_entry("submission/kit/case/" + name)
                            for name in ("BRIEF.md", "contract.json", "customers.json")]
            case_tree = hashlib.sha256(json.dumps(case_entries, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            self.manifest["case_snapshot"] = {"captured_at": "2026-10-11T17:00:58Z",
                                              "files": case_entries, "tree_sha256": case_tree}
            self.manifest["case_after"] = {"captured_at": "2026-10-11T17:02:01Z",
                                           "files": case_entries, "tree_sha256": case_tree}
            self.manifest["case_stable"] = True
        for role, name in (("deployment_receipt", "deployment-receipt.json"),
                           ("readiness_report", "readiness-report.json")):
            self.manifest[role] = {**self.entry(name), "source_path": "packet/" + name}
        self.write("capture.json", self.manifest)
        capture = {key: self.manifest[key] for key in
                   ("capture_id", "run_id", "started_at", "ended_at", "returncode", "mock_url", "probe")}
        capture["manifest_sha256"] = PREPARE.digest(self.packet / "capture.json")[0]
        self.seal = {
            "format_version": 1, "trace_vetted": True, "capture": capture,
            "build": {**self.summary, "run_id": self.run_id},
            "trace_handling": "raw", "raw_capture_sha256": self.entry("trace.log")["sha256"],
            "artifacts": {key: self.entry(name) for key, name in {
                "workflow": "workflow.json", "trace": "trace.log",
                "build_result": "build-result.json", "run_manifest": "capture.json",
                "deployment_receipt": "deployment-receipt.json", "readiness_report": "readiness-report.json"}.items()},
        }
        self.write("seal.json", self.seal)

    def git(self, *args):
        result = subprocess.run(["git", "-C", str(self.root), *args], capture_output=True, check=True)
        return result.stdout.decode().strip()

    def commit(self, normalize=False):
        self.git("init", "-q")
        self.git("config", "user.name", "Offline Test")
        self.git("config", "user.email", "offline@test.invalid")
        self.git("config", "core.autocrlf", "false")
        (self.root / ".gitattributes").write_text(
            "*.json text eol=lf\n" if normalize else "* -text\n", encoding="utf-8")
        self.git("add", ".gitattributes", "packet", "builder", "freya.py", "bank", "submission")
        self.git("commit", "-qm", "Temporary fixture")
        self.config["commit"] = self.git("rev-parse", "HEAD")

    def check(self):
        return PREPARE.check(self.config, self.packet / "seal.json", self.root)

    def test_complete_packet_and_payload(self):
        self.commit()
        self.assertTrue(self.check()["ready"], self.check())
        (self.root / "config.json").write_text(json.dumps(self.config), encoding="utf-8")
        args = ["payload", "--root", str(self.root), "--config", "config.json",
                "--selection", "packet/seal.json", "--output", "submission.json"]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(PREPARE.main(args), 0)
        target = self.root / "submission.json"
        original = target.read_bytes()
        self.assertEqual(set(json.loads(original)), PREPARE.FIELDS)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(PREPARE.main(args), 2)
        self.assertEqual(target.read_bytes(), original)

    def test_blank_draft_and_old_maps_are_rejected(self):
        issues = PREPARE.schema_issues({**self.config, "members": [], "workflow": "",
                                       "trace": "", "mock_url": "", "workflows": {}, "traces": {}})
        fields = {issue["field"] for issue in issues}
        self.assertTrue({"schema", "members", "commit", "workflow", "trace", "mock_url"} <= fields)

    def test_changed_bytes_fail_even_after_local_reseal(self):
        self.commit()
        self.trace.write_bytes(self.trace.read_bytes() + b"added after selected run\n")
        self.refresh_seal()
        report = self.check()
        self.assertFalse(report["ready"])
        self.assertTrue(any("raw committed blob bytes differ" in item["message"] for item in report["issues"]))

    def test_git_normalization_cannot_hide_export_byte_changes(self):
        workflow = self.packet / "workflow.json"
        workflow.write_bytes(workflow.read_bytes().replace(b"\n", b"\r\n"))
        self.refresh_seal()
        self.commit(normalize=True)
        normalized = workflow.read_bytes().replace(b"\r\n", b"\n")
        blob = subprocess.run(["git", "-C", str(self.root), "hash-object", "-w", "--stdin"],
                              input=normalized, capture_output=True, check=True).stdout.decode().strip()
        self.git("update-index", "--cacheinfo", "100644", blob, "packet/workflow.json")
        self.git("commit", "-qm", "Simulate normalized committed bytes")
        self.config["commit"] = self.git("rev-parse", "HEAD")
        report = self.check()
        self.assertFalse(report["ready"])
        self.assertTrue(any(item["field"] == "packet/workflow.json"
                            and "raw committed blob bytes differ" in item["message"] for item in report["issues"]))

    def test_unsafe_paths_and_invalid_worker_urls(self):
        for value in ("../outside", "C:\\outside.json", "/outside.json", "trace.log:stream"):
            with self.assertRaises(ValueError):
                PREPARE.safe_path(self.root, value)
        for url in ("http://team.workers.dev", "https://localhost", "https://127.0.0.1",
                    "https://team.workers.dev/v1", "https://user:password@team.workers.dev"):
            self.assertFalse(PREPARE.https_url(url, origin=True), url)

    def test_own_command_and_python_executable_path_are_supported(self):
        self.config["run_command"] = "python3 -m builder.build {case_folder} {mock_url}"
        self.manifest["command"] = [r"C:\isolated-runtime\python.exe", "-m", "builder.build",
                                    self.manifest["case_folder"], self.manifest["mock_url"]]
        self.refresh_seal()
        self.commit()
        self.assertTrue(self.check()["ready"], self.check())

    def test_runtime_change_after_capture_blocks(self):
        self.commit()
        (self.root / "builder" / "build.py").write_text("# Changed after run\n", encoding="utf-8")
        report = self.check()
        self.assertFalse(report["ready"])
        self.assertTrue(any(item["field"] == "source_snapshot" for item in report["issues"]))

    def test_case_input_change_after_capture_blocks(self):
        self.commit()
        (self.root / "submission" / "kit" / "case" / "BRIEF.md").write_text("# Changed case\n", encoding="utf-8")
        report = self.check()
        self.assertFalse(report["ready"])
        self.assertTrue(any(item["field"] == "case_snapshot" for item in report["issues"]))

    def test_bank_evidence_change_blocks(self):
        self.commit()
        (self.root / "bank" / "bundle.js").write_text("# Changed deployed bundle\n", encoding="utf-8")
        report = self.check()
        self.assertFalse(report["ready"])
        self.assertTrue(any(item["field"] == "deployment_receipt" for item in report["issues"]))

    def test_late_probe_or_wrong_endpoint_blocks(self):
        self.manifest["probe"]["checked_at"] = "2026-10-11T17:01:01Z"
        self.manifest["command"][-1] = "https://another.team.workers.dev"
        self.refresh_seal()
        self.commit()
        report = self.check()
        self.assertFalse(report["ready"])
        self.assertTrue(any(item["field"] == "run_command" for item in report["issues"]))
        self.assertTrue(any("timestamps" in item["message"] for item in report["issues"]))


if __name__ == "__main__":
    unittest.main()
