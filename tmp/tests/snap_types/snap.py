import hashlib, json, sys
from pathlib import Path
from builder.compiler.compile import compile_workflow
from builder.ingest.tool_api import load_tool_api
from builder.policy.ir import PolicyIR, RuleLedger
out = Path(sys.argv[1]); out.mkdir(parents=True, exist_ok=True)
api = load_tool_api(Path("tools/openapi.json"))
for d in ("real_ir", "policy"):
    for b in ("brief_a", "brief_ab"):
        ir = PolicyIR.model_validate_json(Path(f"tests/fixtures/{d}/{b}_ir.json").read_text(encoding="utf-8"))
        led = RuleLedger.model_validate_json(Path(f"tests/fixtures/{d}/{b}_ledger.json").read_text(encoding="utf-8"))
        for prof in ("staged", "lean"):
            c = compile_workflow(ir, api, led, profile=prof)
            s = json.dumps(c.workflow, ensure_ascii=False, indent=1, sort_keys=True)
            (out / f"{d}_{b}_{prof}.json").write_text(s, encoding="utf-8")
            print(d, b, prof, hashlib.sha256(s.encode()).hexdigest()[:16])
