# ruff: noqa
"""Snapshot the English practice compiles (real_ir + policy, brief_a/brief_ab, staged/lean)."""
import json, sys, hashlib
from pathlib import Path
from builder.compiler.compile import compile_workflow
from builder.ingest.tool_api import load_tool_api
from builder.policy.ir import PolicyIR, RuleLedger
REPO = Path(".").resolve()
API = load_tool_api(REPO / "tools" / "openapi.json")
out = Path(sys.argv[1]); out.mkdir(parents=True, exist_ok=True)
for d in ("real_ir", "policy"):
    for b in ("brief_a", "brief_ab"):
        ir = PolicyIR.model_validate_json((REPO/"tests/fixtures"/d/f"{b}_ir.json").read_text(encoding="utf-8"))
        led = RuleLedger.model_validate_json((REPO/"tests/fixtures"/d/f"{b}_ledger.json").read_text(encoding="utf-8"))
        for p in ("staged", "lean"):
            c = compile_workflow(ir, API, led, profile=p)
            blob = json.dumps({"workflow": c.workflow, "notes": c.notes}, ensure_ascii=False, indent=1, sort_keys=True)
            (out/f"{d}_{b}_{p}.json").write_text(blob, encoding="utf-8")
            print(d, b, p, hashlib.sha256(blob.encode()).hexdigest()[:16])
