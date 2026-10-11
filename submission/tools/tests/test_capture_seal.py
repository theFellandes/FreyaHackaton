"""Isolated stdlib tests: synthetic child processes and a loopback readiness server only."""
from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import capture_run
import seal_run


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = self.server.payload
        if isinstance(body, dict):
            body = json.dumps(body).encode("utf-8")
        self.send_response(self.server.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class CaptureSealTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="freya-capture-seal-test-")
        self.cwd = Path(self.temp.name).resolve()
        self.assertEqual(self.cwd.parent, Path(tempfile.gettempdir()).resolve())
        self.assertTrue(self.cwd.name.startswith("freya-capture-seal-test-"))
        (self.cwd / "builder").mkdir()
        self.case = self.cwd / "case"
        self.case.mkdir()
        (self.case / "brief.md").write_bytes(b"Synthetic case input\r\n")
        (self.cwd / "freya.py").write_bytes(b"# Synthetic reference client\n")
        (self.cwd / "requirements.txt").write_bytes(b"# No dependencies in test fixture\n")
        self.child = self.cwd / "builder" / "build.py"
        self.child.write_text("pass\n", encoding="utf-8")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.status = 200
        self.server.payload = {}
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.mock_url = "http://127.0.0.1:" + str(self.server.server_port)
        self.probe_url = self.mock_url + "/ready"
        self.receipt_path = self.cwd / "deployment.json"
        self.report_path = self.cwd / "readiness.json"
        now = capture_run.utc_now()
        artifacts = {}
        for name in ("bundle", "configuration", "contract", "customer_fixtures", "source", "storage", "tests"):
            path = self.cwd / (name + ".dat")
            path.write_bytes(("Synthetic " + name).encode("ascii"))
            artifacts[name] = {"path": path.name, "sha256": capture_run.digest(path.read_bytes())}
            if name in ("contract", "customer_fixtures"):
                (self.case / path.name).write_bytes(path.read_bytes())
        receipt = {"schema_version": 1, "bank_ready": True, "worker_name": "synthetic-test",
                   "deployment_id": "deployment-test", "cloudflare_version_id": "version-test",
                   "deployed_at_utc": now, "base_url": self.mock_url, "health_url": self.probe_url,
                   **{name: artifacts[name] for name in ("bundle", "configuration", "contract", "customer_fixtures")},
                   "source_files": [artifacts["source"]],
                   "storage": {"backend": "test", "persistence_verified": True, "customer_isolation_verified": True,
                               "evidence": artifacts["storage"]},
                   "contract_tests": {"passed": True, "case_count": 1, "report": artifacts["tests"]}}
        self.receipt_path.write_bytes(json.dumps(receipt, indent=2).encode("utf-8") + b"\n")
        expected = {"cloudflare_version_id": "version-test", "bundle_sha256": artifacts["bundle"]["sha256"],
                    "contract_sha256": artifacts["contract"]["sha256"],
                    "customer_fixture_sha256": artifacts["customer_fixtures"]["sha256"]}
        report = {"report_schema_version": 1, "bank_ready": True, "deployment_id": "deployment-test",
                  "base_url": self.mock_url, "health_url": self.probe_url, **expected,
                  "verified_at_utc": now, "duration_ms": 1, "receipt_path": self.receipt_path.name,
                  "receipt_bytes_sha256": capture_run.digest(self.receipt_path.read_bytes())}
        self.report_path.write_bytes(json.dumps(report, indent=2).encode("utf-8") + b"\n")
        self.server.payload = {"ready": True, "bank_ready": True, **expected}

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        # TemporaryDirectory owns this exact verified, task-specific path.
        self.assertTrue(self.cwd.name.startswith("freya-capture-seal-test-"))
        self.assertEqual(self.cwd.parent, Path(tempfile.gettempdir()).resolve())
        self.temp.cleanup()

    def args(self):
        return capture_run.build_parser().parse_args([
            "--cwd", str(self.cwd), "--runs-dir", str(self.cwd / "runs"),
            "--case-folder", "case", "--mock-url", self.mock_url, "--probe-url", self.probe_url,
            "--deployment-receipt", str(self.receipt_path), "--readiness-report", str(self.report_path),
            "--", sys.executable, "builder/build.py", "case", self.mock_url])

    def run_child(self, code):
        self.child.write_text(code, encoding="utf-8", newline="\n")
        return capture_run.run_capture(self.args())

    def successful_run(self, extra=b"", mutate_source=False):
        code = '''import json, os
from pathlib import Path
root = Path.cwd()
workflow_path = root / "exported.json"
trace_path = root / "builder.log"
workflow = {"workflow_id": "workflow-test", "agent_id": "agent-test", "workflow": {
    "initial_node": "start", "nodes": {"start": {"prompt": "Synthetic test"}}}}
workflow_path.write_bytes(json.dumps(workflow, ensure_ascii=False).encode("utf-8") + b"\\r\\n")
start = b"2026-10-11T17:00:00.000Z | START  | run abc123456789 started | slot=case\\r\\n"
end = b"2026-10-11T17:00:00.100Z | END    | run abc123456789 closed | slot=case\\r\\n"
trace_path.write_bytes(start + end)
build = {"slot": "case", "workflow_id": "workflow-test", "agent_id": "agent-test", "version": 1,
         "status": "deployed", "export_ok": True, "exported": str(workflow_path), "trace": str(trace_path)}
(root / "result.json").write_bytes(json.dumps(build).encode("utf-8") + b"\\n")
os.write(1, start)
os.write(2, EXTRA)
os.write(1, end)
os.write(1, json.dumps(build).encode("utf-8") + b"\\r\\n")
'''
        code = code.replace("EXTRA", repr(extra))
        if mutate_source:
            code += 'Path(__file__).write_text(Path(__file__).read_text() + "\\n# changed\\n")\n'
        return self.run_child(code)

    def seal(self, directory, trace=None, output=None, **kwargs):
        return seal_run.seal_run(run_manifest=directory / "run.json", workflow=self.cwd / "exported.json",
                                 trace=trace or directory / "console.raw.log", build_result=self.cwd / "result.json",
                                 output=output or self.cwd / "selected", vet_trace=True, **kwargs)

    def test_exact_non_utf8_bytes_both_streams_and_exit_code(self):
        first = b"\x00\x80stdout\r\npartial\r"
        second = b"\x1b[31mstderr\xff\n"
        third = b"end-no-newline"
        code = "import os,sys\nos.write(1, " + repr(first) + ")\nos.write(2, " + repr(second) + ")\nos.write(1, " + repr(third) + ")\nsys.exit(23)\n"
        directory, manifest = self.run_child(code)
        self.assertEqual((directory / "console.raw.log").read_bytes(), first + second + third)
        self.assertEqual(manifest["returncode"], 23)
        self.assertEqual(manifest["trace"]["sha256"], capture_run.digest(first + second + third))
        self.assertEqual(manifest["state"], "complete")
        self.assertTrue(manifest["source_stable"])

    def test_large_both_stream_output_is_complete(self):
        directory, manifest = self.run_child("import os\nos.write(1,b'a' * 131072)\nos.write(2,b'b' * 131072)\n")
        self.assertEqual((directory / "console.raw.log").read_bytes(), b"a" * 131072 + b"b" * 131072)
        self.assertEqual(manifest["returncode"], 0)

    def test_repeated_runs_never_overwrite(self):
        first, _ = self.run_child("import os\nos.write(1,b'first')\n")
        original = (first / "run.json").read_bytes()
        second, _ = self.run_child("import os\nos.write(1,b'second')\n")
        self.assertNotEqual(first, second)
        self.assertEqual((first / "console.raw.log").read_bytes(), b"first")
        self.assertEqual((first / "run.json").read_bytes(), original)
        self.assertEqual((second / "console.raw.log").read_bytes(), b"second")

    def test_html_200_cannot_launch_child(self):
        self.server.payload = b"<html>ok</html>"
        directory, manifest = self.run_child("from pathlib import Path\nPath('child.started').write_text('yes')\n")
        self.assertEqual(manifest["state"], "probe_failed")
        self.assertFalse((self.cwd / "child.started").exists())
        self.assertFalse((directory / "console.raw.log").exists())

    def test_wrong_live_version_cannot_launch_child(self):
        self.server.payload["cloudflare_version_id"] = "another-version"
        _, manifest = self.run_child("from pathlib import Path\nPath('child.started').write_text('yes')\n")
        self.assertEqual(manifest["state"], "probe_failed")
        self.assertFalse((self.cwd / "child.started").exists())

    def test_changed_receipt_is_rejected_before_launch(self):
        self.receipt_path.write_bytes(self.receipt_path.read_bytes() + b"\n")
        with self.assertRaisesRegex(ValueError, "different receipt bytes"):
            self.run_child("pass\n")
        self.assertFalse((self.cwd / "runs").exists())

    def test_bank_for_another_case_is_refused_before_launch(self):
        (self.case / "contract.dat").write_bytes(b"Other released contract")
        with self.assertRaisesRegex(ValueError, "do not belong to this case folder"):
            self.run_child("from pathlib import Path\nPath('child.started').write_text('yes')\n")
        self.assertFalse((self.cwd / "child.started").exists())

    def test_redaction_preserves_nonsecret_header_bytes(self):
        raw = b"beArEr  " + b"x" * 32 + b"\r\n"
        self.assertEqual(seal_run.redact_bytes(raw, []), b"beArEr  ***\r\n")

    def test_launch_failure_retains_empty_trace_and_failure_state(self):
        args = self.args()
        args.command[1] = str(self.cwd / "nonexistent-python.exe")
        directory, manifest = capture_run.run_capture(args)
        self.assertEqual(manifest["state"], "launch_failed")
        self.assertIsNone(manifest["returncode"])
        self.assertEqual((directory / "console.raw.log").read_bytes(), b"")

    def test_exact_seal_including_crlf_and_verification(self):
        directory, capture = self.successful_run()
        result = self.seal(directory)
        self.assertEqual(result["build"]["run_id"], "abc123456789")
        self.assertEqual((self.cwd / "selected" / "workflow.json").read_bytes(), (self.cwd / "exported.json").read_bytes())
        self.assertEqual((self.cwd / "selected" / "trace.log").read_bytes(), (directory / "console.raw.log").read_bytes())
        self.assertEqual(result["capture"]["manifest_sha256"], result["artifacts"]["run_manifest"]["sha256"])
        self.assertEqual((self.cwd / "selected" / ".gitattributes").read_bytes(), b"* -text -filter\n")
        self.assertEqual(seal_run.verify_seal(self.cwd / "selected"), result)
        self.assertEqual(len(result["artifacts"]), 6)
        self.assertTrue(capture["case_stable"])

    def test_seal_refuses_changed_raw_capture(self):
        directory, _ = self.successful_run()
        raw = directory / "console.raw.log"
        raw.write_bytes(raw.read_bytes() + b"\n")
        with self.assertRaisesRegex(seal_run.EvidenceError, "Raw capture bytes changed"):
            self.seal(directory)
        self.assertFalse((self.cwd / "selected").exists())

    def test_seal_refuses_export_overwritten_after_run(self):
        directory, _ = self.successful_run()
        export = self.cwd / "exported.json"
        changed = json.loads(export.read_bytes())
        changed["workflow"]["nodes"]["start"]["prompt"] = "Changed after run"
        export.write_bytes(json.dumps(changed).encode("utf-8"))
        with self.assertRaisesRegex(seal_run.EvidenceError, "end-of-run exporter snapshot"):
            self.seal(directory)

    def test_seal_refuses_source_changed_during_run(self):
        directory, manifest = self.successful_run(mutate_source=True)
        self.assertFalse(manifest["source_stable"])
        with self.assertRaisesRegex(seal_run.EvidenceError, "source or case inputs changed"):
            self.seal(directory)

    def test_seal_refuses_existing_destination(self):
        directory, _ = self.successful_run()
        self.seal(directory)
        before = (self.cwd / "selected" / "seal.json").read_bytes()
        with self.assertRaisesRegex(seal_run.EvidenceError, "already exists"):
            self.seal(directory)
        self.assertEqual((self.cwd / "selected" / "seal.json").read_bytes(), before)

    def test_verifier_detects_sealed_trace_mutation(self):
        directory, _ = self.successful_run()
        self.seal(directory)
        selected = self.cwd / "selected" / "trace.log"
        selected.write_bytes(selected.read_bytes().replace(b"\r\n", b"\n"))
        with self.assertRaisesRegex(seal_run.EvidenceError, "trace bytes changed"):
            seal_run.verify_seal(self.cwd / "selected")

    def test_secret_trace_refused_until_only_known_secrets_removed(self):
        directory, _ = self.successful_run(extra=b"NOTE fak_" + b"a" * 32 + b"\r\n")
        with self.assertRaisesRegex(seal_run.EvidenceError, "credential-shaped"):
            self.seal(directory)
        raw = (directory / "console.raw.log").read_bytes()
        vetted = directory / "vetted.log"
        vetted.write_bytes(seal_run.redact_bytes(raw, []))
        result = self.seal(directory, trace=vetted, allow_known_secret_redaction=True)
        self.assertEqual(result["trace_handling"], "known_secret_redaction")
        self.assertEqual((directory / "console.raw.log").read_bytes(), raw)
        self.assertEqual(seal_run.verify_seal(self.cwd / "selected"), result)

    def test_manual_nonsecret_trace_edits_are_refused(self):
        directory, _ = self.successful_run()
        edited = directory / "edited.log"
        edited.write_bytes(b"Edited heading\n" + (directory / "console.raw.log").read_bytes())
        with self.assertRaisesRegex(seal_run.EvidenceError, "beyond known secret removal"):
            self.seal(directory, trace=edited, allow_known_secret_redaction=True)


if __name__ == "__main__":
    unittest.main()