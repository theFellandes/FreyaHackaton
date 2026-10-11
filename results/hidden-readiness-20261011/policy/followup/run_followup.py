"""Refresh changed floor regressions and existing literal probes; preserve the original capture."""
from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

OUT = Path(__file__).resolve().parent
PARENT = OUT.parent
ROOT = Path(__file__).resolve().parents[4]
CACHE = ROOT / ".cache/hidden-readiness-20261011/policy/followup"
ORIGINAL = PARENT / "policy-results.json"


def main():
    original_hash = hashlib.sha256(ORIGINAL.read_bytes()).hexdigest()
    original = json.loads(ORIGINAL.read_text(encoding="utf-8"))
    spec = importlib.util.spec_from_file_location("policy_readiness_followup", PARENT / "run_policy_readiness.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    OUT.mkdir(parents=True, exist_ok=True)
    (CACHE / "temp").mkdir(parents=True, exist_ok=True)
    before = module.snapshot()
    started = dt.datetime.now(dt.timezone.utc).isoformat()
    command = [sys.executable, "-m", "pytest", "tests/unit/test_floor_rules.py", "-m", "not live", "-o",
               f"cache_dir={CACHE / 'pytest-cache'}", f"--basetemp={CACHE / 'pytest-temp'}",
               f"--junitxml={OUT / 'pytest.xml'}", "--disable-warnings"]
    env = dict(os.environ)
    env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1", TMP=str(CACHE / "temp"), TEMP=str(CACHE / "temp"))
    process = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True)
    output = process.stdout + process.stderr
    (OUT / "pytest-summary.txt").write_text(output, encoding="utf-8")
    (CACHE / "pytest-output.txt").write_text(output, encoding="utf-8")
    after = module.snapshot()
    xml = ET.parse(OUT / "pytest.xml").getroot()
    totals = {key: sum(int(node.get(key, "0")) for node in xml.findall("testsuite"))
              for key in ("tests", "failures", "errors", "skipped")}
    test_record = {"command": command, "started_utc": started,
                   "completed_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
                   "exit_code": process.returncode, "stdout": process.stdout, "stderr": process.stderr,
                   "totals": totals, "source_before_sha256": before, "source_after_sha256": after}
    # Seed only the follow-up destination so the unchanged existing runner can refresh probes without rerunning 365 tests.
    seed = dict(original)
    seed["tests"] = test_record
    (OUT / "policy-results.json").write_text(json.dumps(seed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    module.OUT = OUT
    module.CACHE = CACHE
    saved_argv = sys.argv
    sys.argv = [str(PARENT / "run_policy_readiness.py"), "--probes-only"]
    try:
        probe_exit = module.main()
    finally:
        sys.argv = saved_argv
    report = json.loads((OUT / "policy-results.json").read_text(encoding="utf-8"))
    report["followup"] = {
        "scope": "Current floor-rule module plus the original eight literal probes only; original report preserved.",
        "wrapper_command": [sys.executable, "-B", str(Path(__file__))],
        "wrapper_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "original_report": ORIGINAL.relative_to(ROOT).as_posix(),
        "original_report_sha256_before": original_hash,
        "original_report_sha256_after": hashlib.sha256(ORIGINAL.read_bytes()).hexdigest(),
        "test_sources_stable": before == after,
        "changed_since_original_probe": [name for name in sorted(before)
            if before[name] != original["source_after_sha256"].get(name)],
    }
    (OUT / "policy-results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(output[-1500:])
    print(json.dumps({"tests": totals, "test_sources_stable": before == after,
                      "probe_sources_stable": report["source_stable"],
                      "original_preserved": report["followup"]["original_report_sha256_after"] == original_hash,
                      "changed_since_original": report["followup"]["changed_since_original_probe"],
                      "floor_discrepancy": report["probes"][0]["actual"]["A-2_floor"],
                      "helper_effective_floor": report["probes"][0]["reference_effective_floor_A-2"]}, indent=2))
    return probe_exit


if __name__ == "__main__":
    raise SystemExit(main())
