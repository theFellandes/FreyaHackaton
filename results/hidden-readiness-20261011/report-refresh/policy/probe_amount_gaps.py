"""Bounded current-source floor/unit probes; no live calls or source changes."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[4]
OUT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def capture():
    paths = {*(ROOT / "builder").rglob("*.py"), *(ROOT / "builder").rglob("*.j2"),
             *(ROOT / "tests/fixtures/policy").glob("brief_a*_*.json"), ROOT / "tools/openapi.json",
             ROOT / "tests/unit/test_floor_rules.py", ROOT / "tests/unit/test_hidden_tool_contracts.py",
             Path(__file__)}
    return {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(paths)}


def source_line(path, text):
    return next(i for i, line in enumerate((ROOT / path).read_text(encoding="utf-8").splitlines(), 1) if text in line)


def main():
    started = dt.datetime.now(dt.timezone.utc).isoformat()
    before = capture()
    from builder.checks.code_sandbox import extract_code_snippets, run_hook, run_snippet
    from builder.checks.lint import lint_workflow
    from builder.compiler.compile import compile_workflow
    from builder.ingest.tool_api import load_tool_api
    from builder.policy.ir import PolicyIR, RuleLedger
    from builder.policy.money import effective_floor, to_money

    fixture = ROOT / "tests/fixtures/policy"
    ir = PolicyIR.model_validate(json.loads((fixture / "brief_ab_ir.json").read_text(encoding="utf-8")))
    ledger = RuleLedger.model_validate(json.loads((fixture / "brief_ab_ledger.json").read_text(encoding="utf-8")))
    api = load_tool_api(ROOT / "tools/openapi.json")
    now = "2026-10-14T11:00"
    rows = []

    def probe(identifier, policy, contract, product, amounts):
        compiled = compile_workflow(policy, contract, ledger)
        snippets = extract_code_snippets(compiled.workflow)
        state = run_snippet(snippets["setup"], {"now": now, "customer_id": "synthetic-refresh"})
        state = run_snippet(snippets["account_facts"], {**state, "products": [product]})
        tool = next(t for t in compiled.workflow["nodes"]["negotiate"]["tools"]
                    if t["function"]["name"] == "create_promise")
        requests = []
        for amount in amounts:
            body = {"customer_id": "synthetic-refresh", "product_id": product["product_id"],
                    "amount": amount, "date": "2026-10-16"}
            result = run_hook(tool["pre_process_code"], vars=state,
                              config={"url": "https://offline.invalid/not-sent", "body": body})
            requests.append({"model_input": body, "normalized_body": result["config"]["body"],
                             "allowed_to_send": bool(result["config"]["url"]),
                             "promise_guard": result["vars"].get("promise_guard"),
                             "correction": result["vars"].get("promise_guard_say")})
        pid = product["product_id"]
        lint = lint_workflow(compiled.workflow, contract, set(policy.call_variable_names()))
        row = {"id": identifier, "input": {"now": now, "product": product,
               "amount_policy": policy.amounts.model_dump(mode="json")},
               "actual": {"due": state.get(pid + "_past_due"), "floor": state.get(pid + "_floor"),
               "account_facts": state.get("account_facts"), "debt_statement": state.get("debt_statement"),
               "requests": requests, "lint_errors": [x.model_dump(mode="json") for x in lint.errors]},
               "evidence": "Fresh generated setup/account code and complete compiled pre-hook; URL retention only, no HTTP."}
        rows.append(row)
        return row

    floor_product = {"product_id": "F-1", "type": "car_loan", "past_due": 60.01,
                     "days_late": 3, "instalment_amount": 500}
    supplied = probe("F01", ir, api, floor_product, [60, 60.01, 500])
    supplied["reference_effective_floor"] = str(effective_floor(60.01, 500))
    supplied["status"] = "Mismatch persists: supplied floor/offer 500.00, effective guard floor 60.01."
    computed = ir.model_copy(deep=True)
    from builder.policy.ir import FloorRule
    computed.amounts.floor_rule_by_type["car_loan"] = FloorRule(fixed=500)
    capped = probe("F02", computed, api, floor_product, [60.01, 500])
    capped["status"] = "Computed-rule path caps its floor at 60.01; this does not repair the supplied-field-only path."

    def unit_api(scale):
        changed = api.model_copy(deep=True)
        description = f"Amount in minor units; {scale} minor units equal one major currency unit."
        for field in changed.tools["get_account"].field("products", "response").items or []:
            if field.name in ("past_due", "minimum_payment"):
                field.type, field.description = "integer", description
        amount = changed.tools["create_promise"].field("amount", "request")
        amount.type, amount.description = "integer", description
        return changed, description

    for identifier, scale in (("U01", 100), ("U02", 1000)):
        contract, description = unit_api(scale)
        product = {"product_id": "U-1", "type": "credit_card", "card_last4": "3082",
                   "past_due": 365 * scale, "minimum_payment": int(109.5 * scale), "days_late": 8}
        row = probe(identifier, ir, contract, product, ["120.50"] if scale == 100 else ["120.50", "1095.00"])
        row["input"]["explicit_contract_description"] = description
        row["expected_by_explicit_contract"] = {"due_major": "365.00", "floor_major": "109.50",
                                                 "request_minor": int(120.5 * scale), "allowed_to_send": True}
        request = row["actual"]["requests"][0]
        for recorded in row["actual"]["requests"]:
            recorded["bank_major_by_explicit_contract"] = f"{recorded['normalized_body']['amount'] / scale:.2f}"
        row["matches_contract"] = row["actual"]["due"] == "365.00" and row["actual"]["floor"] == "109.50" and (
            request["normalized_body"]["amount"] == int(120.5 * scale)) and request["allowed_to_send"]
    rows.append({"id": "U03", "evidence": "Fresh reference money helper only.",
                 "input": "120.505", "actual": str(to_money("120.505")),
                 "meaning": "All money is rounded to two decimal places. No per-currency precision exists in this helper."})
    after = capture()
    refs = [("builder/compiler/codegen.py", "floor = to_cents(p.get(ffield))"),
            ("builder/compiler/codegen.py", "floor = min(floor, due)"),
            ("builder/compiler/codegen.py", "return int(round(float(v)))"),
            ("builder/compiler/hooks.py", "low = full"),
            ("builder/compiler/hooks.py", "if kind == \"money_minor\""),
            ("builder/compiler/roles.py", "def minor_units("),
            ("builder/policy/money.py", "_CENT = Decimal(\"0.01\")")]
    report = {"started_at_utc": started, "completed_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
              "scope": "Bounded synthetic floor/currency-unit refresh; no pytest, extraction, LLM or platform calls.",
              "command": [sys.executable, "-B", str(Path(__file__))], "python": sys.version,
              "source_before_sha256": before, "source_after_sha256": after, "source_stable": before == after,
              "changed_sources": [p for p in sorted(set(before) | set(after)) if before.get(p) != after.get(p)],
              "probes": rows, "source_refs": [{"path": path, "line": source_line(path, text), "contains": text}
                                                 for path, text in refs]}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "amount-results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"captured": report["completed_at_utc"], "source_stable": report["source_stable"],
          "rows": [{"id": r["id"], "floor": r["actual"].get("floor") if isinstance(r["actual"], dict) else None,
                    "matches_contract": r.get("matches_contract")} for r in rows]}, indent=2))


if __name__ == "__main__":
    main()
