"""Offline synthetic policy-readiness probes; never an official hidden verdict."""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
CACHE = ROOT / ".cache/hidden-readiness-20261011/policy"
sys.path.insert(0, str(ROOT))
SELECTED = [
    "tests/unit/test_band_product_types.py",
    "tests/unit/test_floor_rules.py",
    "tests/unit/test_state_routes_and_dates.py",
    "tests/unit/test_outcomes.py",
    "tests/unit/test_codegen_calendar_parity.py",
    "tests/unit/test_hidden_tool_contracts.py",
    "tests/unit/test_parallel_acknowledgement.py",
    "tests/unit/test_compiled_note_acknowledgment.py",
    "tests/unit/test_compiled_promise_once.py",
]


def snapshot():
    files = {*(ROOT / "builder").rglob("*.py"), *(ROOT / "builder").rglob("*.j2"),
             *(ROOT / "tests/fixtures/policy").glob("*.json"),
             ROOT / "tests/conftest.py", ROOT / "tools/openapi.json", ROOT / "pyproject.toml",
             Path(__file__), *(ROOT / name for name in SELECTED)}
    return {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(files)}


def main():
    started = dt.datetime.now(dt.timezone.utc).isoformat()
    before = snapshot()
    OUT.mkdir(parents=True, exist_ok=True)
    CACHE.mkdir(parents=True, exist_ok=True)
    (CACHE / "temp").mkdir(exist_ok=True)
    env = dict(os.environ)
    env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1", TMP=str(CACHE / "temp"), TEMP=str(CACHE / "temp"))
    command = [sys.executable, "-m", "pytest", *SELECTED, "-m", "not live", "-o",
               f"cache_dir={CACHE / 'pytest-cache'}", f"--basetemp={CACHE / 'pytest-temp'}",
               f"--junitxml={OUT / 'pytest.xml'}", "--disable-warnings"]
    if "--probes-only" in sys.argv:
        previous = json.loads((OUT / "policy-results.json").read_text(encoding="utf-8"))
        tests = previous["tests"]
        tests.setdefault("source_before_sha256", previous["source_before_sha256"])
        tests.setdefault("source_after_sha256", previous["source_after_sha256"])
        tests.setdefault("completed_utc", previous["completed_at_utc"])
        print("Reusing the recorded focused test run; refreshing literal probes only.", flush=True)
    else:
        print("Running nine offline regression modules...", flush=True)
        test_started = dt.datetime.now(dt.timezone.utc).isoformat()
        process = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True)
        (CACHE / "pytest-output.txt").write_text(process.stdout + process.stderr, encoding="utf-8")
        (OUT / "pytest-summary.txt").write_text(process.stdout + process.stderr, encoding="utf-8")
        print(process.stdout[-2000:], flush=True)
        tests = {"command": command, "started_utc": test_started, "exit_code": process.returncode,
                 "completed_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
                 "stdout": process.stdout, "stderr": process.stderr, "source_before_sha256": before,
                 "source_after_sha256": snapshot()}
        if (OUT / "pytest.xml").exists():
            suites = ET.parse(OUT / "pytest.xml").getroot()
            tests["totals"] = {key: sum(int(node.get(key, "0")) for node in suites.findall("testsuite"))
                               for key in ("tests", "failures", "errors", "skipped")}
    from builder.checks.code_sandbox import extract_code_snippets, run_hook, run_snippet
    from builder.compiler.compile import compile_workflow
    from builder.compiler import codegen
    from builder.ingest.tool_api import load_tool_api
    from builder.policy.ir import PolicyIR, RuleLedger
    from builder.policy.money import effective_floor
    from builder.policy.outcomes import CallSituation, NoteRecord, PromiseRecord, compute_outcome

    fixture = ROOT / "tests/fixtures/policy"
    data = json.loads((fixture / "brief_ab_ir.json").read_text(encoding="utf-8"))
    ledger = RuleLedger.model_validate(json.loads((fixture / "brief_ab_ledger.json").read_text(encoding="utf-8")))
    api = load_tool_api(ROOT / "tools/openapi.json")
    original = PolicyIR.model_validate(data)
    synthetic = copy.deepcopy(data)
    synthetic["calendar"].update(business_weekdays=[0, 1, 2, 3, 6], holidays=["2027-01-07", "2027-01-10"])
    card_early = copy.deepcopy(synthetic["bands"][0])
    card_early.update(name="card_early_synthetic", product_types=["credit_card"],
                      deadline={"kind": "nth_business_day_after_today", "n": 3})
    card_late = copy.deepcopy(synthetic["bands"][1])
    card_late.update(name="card_late_synthetic", product_types=["credit_card"],
                     deadline={"kind": "nth_business_day_after_today", "n": 2})
    synthetic["bands"] += [card_early, card_late]
    synthetic["amounts"]["floor_field_by_type"].pop("credit_card", None)
    synthetic["amounts"]["floor_rule_by_type"] = {"credit_card": {"percent_of_amount": 12.5}}
    ir = PolicyIR.model_validate(synthetic)
    products = [
        {"product_id": "C-7", "type": "credit_card", "card_last4": "3082", "past_due": 399.99,
         "days_late": 4, "minimum_payment": 15},
        {"product_id": "L-9", "type": "personal_loan", "past_due": 912.37, "days_late": 15,
         "instalment_amount": 75.35},
        {"product_id": "A-2", "type": "car_loan", "past_due": 60.01, "days_late": 3,
         "instalment_amount": 500},
    ]
    wf = compile_workflow(ir, api, ledger).workflow
    snippets = extract_code_snippets(wf)
    tools = {t["function"]["name"]: t for t in wf["nodes"]["negotiate"]["tools"]}
    rows = []

    def record(number, title, inputs, actual, expected=None, kind="fresh local generated code/hooks", limit=None):
        row = {"id": f"H{number:02}", "title": title, "evidence": kind, "input": inputs,
               "actual": actual, "expected": expected, "matches_literal_expectation": actual == expected
               if expected is not None else None}
        if limit:
            row["limit"] = limit
        rows.append(row)

    def facts(now):
        state = run_snippet(snippets["setup"], {"now": now, "customer_id": "synthetic-1"})
        return run_snippet(snippets["account_facts"], {**state, "products": copy.deepcopy(products)})

    def selected_facts(state):
        return {key: state.get(key) for key in ("today_iso", "next_business_day_iso", "C-7_band", "C-7_deadline",
                "C-7_floor", "L-9_band", "L-9_deadline", "L-9_floor", "A-2_band", "A-2_deadline", "A-2_floor")}

    now = "2027-01-06T13:20"
    state = facts(now)
    expected = {"today_iso": "2027-01-06", "next_business_day_iso": "2027-01-11",
                "C-7_band": "card_early_synthetic", "C-7_deadline": "2027-01-13", "C-7_floor": "50.00",
                "L-9_band": "late", "L-9_deadline": "2027-01-19", "L-9_floor": "75.35",
                "A-2_band": "early", "A-2_deadline": "2027-01-26", "A-2_floor": "60.01"}
    record(1, "Changed call date, Sunday business day, two holidays, typed bands and floors", {
        "now": now, "calendar": synthetic["calendar"], "typed_bands": [card_early, card_late],
        "amount_policy": synthetic["amounts"], "products": products},
        selected_facts(state), expected)
    rows[-1]["full_generated_state"] = state
    rows[-1]["reference_effective_floor_A-2"] = str(effective_floor("60.01", "500"))
    rows[-1]["differing_fields"] = [key for key in expected if expected[key] != state.get(key)]
    rows[-1]["limit"] = "The reference effective floor is capped at total debt. Generated supplied-field facts/offer wording retain 500.00, while the hard guard uses 60.01."
    shifted = facts("2027-01-13T09:05+03:00")
    later = copy.deepcopy(expected)
    later.update(today_iso="2027-01-13", next_business_day_iso="2027-01-14", **{
        "C-7_deadline": "2027-01-18", "L-9_deadline": "2027-01-24", "A-2_deadline": "2027-01-31"})
    record(2, "A later call clock recalculates every product deadline", {"now": "2027-01-13T09:05+03:00"},
           selected_facts(shifted), later, limit="Offset is dropped: supplied wall time is retained, not timezone-converted.")
    rows[-1]["differing_fields"] = [key for key in later if later[key] != shifted.get(key)]

    def pre(tool, variables, body):
        return run_hook(tool["pre_process_code"], vars=copy.deepcopy(variables),
                        config={"url": "https://offline.invalid/never-called", "body": dict(body)})

    guard_vectors = [("C-7", 49.99, "2027-01-13", False), ("C-7", 50, "2027-01-13", True),
                     ("C-7", 400, "2027-01-13", False), ("C-7", 399.99, "2027-01-07", False),
                     ("C-7", 50, "2027-01-14", False), ("L-9", 75.35, "2027-01-14", True),
                     ("A-2", 60, "2027-01-13", False), ("A-2", 60.01, "2027-01-13", True)]
    decisions = []
    full_floor_boundary_outputs = []
    for pid, amount, day, _ in guard_vectors:
        body = {"customer_id": "synthetic-1", "product_id": pid, "amount": amount, "date": day}
        result = pre(tools["create_promise"], state, body)
        decisions.append({"request": body, "allowed_to_send": bool(result["config"]["url"]),
                          "guard": result["vars"].get("promise_guard"),
                          "correction": result["vars"].get("promise_guard_say")})
        if pid == "A-2":
            full_floor_boundary_outputs.append({"input_request": body, "full_hook_output": result})
    record(3, "One-cent amount boundaries and distinct product deadlines", guard_vectors, decisions,
           kind="fresh local pre-hooks; URL retention only, no request sent")
    rows[-1]["expected_allowed"] = [x[3] for x in guard_vectors]
    rows[-1]["matches_literal_expectation"] = [x["allowed_to_send"] for x in decisions] == rows[-1]["expected_allowed"]
    rows[-1]["full_floor_boundary_outputs"] = full_floor_boundary_outputs

    mapping = {"promise": "commitment_saved", "no_promise": "nothing_agreed", "paid": "settled",
               "not_verified": "identity_missing", "not_reached": "customer_missing", "transferred": "human_handoff",
               "refused": "declined_terms", "dispute": "account_question"}

    def rename(value):
        if isinstance(value, dict):
            return {k: rename(v) for k, v in value.items()}
        if isinstance(value, list):
            return [rename(v) for v in value]
        return mapping.get(value, value) if isinstance(value, str) else value

    remapped = PolicyIR.model_validate(rename(data))
    situations = [
        ("one promise plus refusal", CallSituation(verified=True, promises=[PromiseRecord("x", 100, "2027-01-13")],
             notes=[NoteRecord("y", "declined_terms")], product_order=["x", "y"]),
             {"identity_verified": "yes", "promise_acks": "1", "notes_log": "y:declined_terms;",
              "product_ids": "x,y", "chosen_end": "flat_refusal"}, ["commitment_saved", None]),
        ("all paid", CallSituation(verified=True, notes=[NoteRecord("y", "settled"), NoteRecord("x", "settled")],
             product_order=["x", "y"]), {"identity_verified": "yes", "promise_acks": "0",
             "notes_log": "y:settled;x:settled;", "product_ids": "x,y", "chosen_end": "flat_refusal"}, ["settled", None]),
        ("one missing paid note", CallSituation(verified=True, notes=[NoteRecord("x", "settled")], product_order=["x", "y"]),
             {"identity_verified": "yes", "promise_acks": "0", "notes_log": "x:settled;", "product_ids": "x,y",
              "chosen_end": "flat_refusal"}, ["nothing_agreed", "declined_terms"]),
        ("first nonpaid in account order", CallSituation(verified=True, notes=[NoteRecord("y", "declined_terms"),
             NoteRecord("x", "account_question")], product_order=["x", "y"]), {"identity_verified": "yes",
             "promise_acks": "0", "notes_log": "y:declined_terms;x:account_question;", "product_ids": "x,y",
             "chosen_end": "flat_refusal"}, ["nothing_agreed", "account_question"]),
    ]
    outcomes = []
    for label, situation, variables, literal in situations:
        helper = list(compute_outcome(remapped, situation).as_tuple())
        generated = run_snippet(codegen.decide_outcome_snippet(remapped), variables)
        got = [generated["final_outcome"], generated.get("final_reason")]
        outcomes.append({"case": label, "variables": variables, "helper": helper, "generated": got,
                         "expected": literal, "match": helper == got == literal})
    record(4, "Renamed outcome and note labels with mixed products", {"label_mapping": mapping}, outcomes,
           kind="fresh reference helper and generated decide snippet; supplied acknowledged facts")
    rows[-1]["matches_literal_expectation"] = all(x["match"] for x in outcomes)
    rows[-1]["limit"] = "Component check: no raw-document extraction or changed bank API enum validation was exercised."

    identity_api = load_tool_api(ROOT / "tools/openapi.json")
    identity_data = copy.deepcopy(data)
    existing = identity_data["identity"]["questions"]
    identity_data["identity"]["questions"] = [existing[1], existing[0], {
        "field": "reference_last6", "ask": "What are the last six digits of your customer reference?",
        "answer_format": "digits", "digits": 6, "normalisation": "Remove spaces between digits."}]
    identity_ir = PolicyIR.model_validate(identity_data)
    next(f for f in identity_api.tools["verify_identity"].request_fields if f.name == "field").enum.append("reference_last6")
    identity_api.tools["verify_identity"].response_fields = [f for f in identity_api.tools["verify_identity"].response_fields
                                                             if f.name != "all_verified"]
    identity_wf = compile_workflow(identity_ir, identity_api, ledger).workflow
    verify = next(t for t in identity_wf["nodes"]["identity"]["tools"] if t["function"]["name"] == "verify_identity")
    identity_state = {}
    progress = []
    for field, answer, verified in [("phone_last4", "12 34", True), ("date_of_birth", "03/04/1980", True),
                                    ("date_of_birth", "03/04/1980", True), ("reference_last6", "999999", False),
                                    ("reference_last6", "12 34 56", True)]:
        staged = pre(verify, identity_state, {"customer_id": "synthetic-1", "field": field, "answer": answer})
        response = {"status": "success", "data": {"ok": True, "verified": verified, "wrong_answers_left": 2}}
        got = run_hook(verify["post_process_code"], vars=staged["vars"], result=response)
        identity_state = got["vars"]
        progress.append({"field": field, "answer_after_normalization": staged["config"]["body"]["answer"],
                         "injected_verified": verified, "verified_fields": identity_state.get("verified_fields"),
                         "identity_all_verified": identity_state.get("identity_all_verified"),
                         "instruction": got["result"]["data"].get("what_to_do_next")})
    record(5, "Reordered three-question identity contract without overall flag", {
           "removed_response_field": "all_verified", "extended_api_field_enum": "reference_last6",
           "questions": identity_data["identity"]["questions"]},
           progress, kind="fresh local pre/post-hooks with supplied bank verdicts")
    rows[-1]["matches_literal_expectation"] = [p["identity_all_verified"] for p in progress] == [None, None, None, None, "yes"]
    rows[-1]["limit"] = "The bank verdict was injected; no customer recognition or partial/refusal speech was tested."

    # Fresh residual probes intentionally expose limits instead of treating positive tests as certification.
    base_wf = compile_workflow(original, api, ledger).workflow
    base_snippets = extract_code_snippets(base_wf)
    base_setup = run_snippet(base_snippets["setup"], {"now": "2026-10-14T11:00"})
    callback = next(t for t in base_wf["nodes"]["third_party"]["tools"] if t["function"]["name"] == "schedule_callback")
    callbacks = []
    for value in ("11:99", "12:60", "20:00"):
        got = pre(callback, base_setup, {"customer_id": "synthetic-1", "date": "2026-10-14", "time": value})
        callbacks.append({"time": value, "normalized": got["config"]["body"]["time"],
                          "allowed_to_send": bool(got["config"]["url"]), "guard": got["vars"].get("callback_guard")})
    record(6, "Residual callback invalid-minute boundary", {"now": "2026-10-14T11:00", "times": ["11:99", "12:60", "20:00"]},
           callbacks, kind="fresh local complete callback pre-hook; no HTTP", limit="Invalid minutes must be rejected; actual values below show current behavior.")
    base_products = [{"product_id": f"p{i}", "type": "personal_loan", "past_due": 100 * i,
                      "days_late": 5, "instalment_amount": 30} for i in (1, 2, 3)]
    base_facts = run_snippet(base_snippets["account_facts"], {**base_setup, "products": base_products})
    promise_tool = next(t for t in base_wf["nodes"]["negotiate"]["tools"] if t["function"]["name"] == "create_promise")
    consent = pre(promise_tool, base_facts, {"customer_id": "synthetic-1", "product_id": "p1", "amount": 100,
                                          "date": "2026-10-16"})
    record(7, "Residual exact-consent boundary", {"amount": 100, "date": "2026-10-16", "stored_confirmation": None},
           {"allowed_to_send": bool(consent["config"]["url"]), "guard": consent["vars"].get("promise_guard")},
           kind="fresh local promise pre-hook; no customer conversation", limit="Legal terms pass without a bound consent record; clear yes remains a prompt requirement.")

    ok = {"status": "success", "data": {"ok": "true"}}

    def batch(variables, calls):
        variables = copy.deepcopy(variables)
        sent = []
        for body, response in calls:
            got = pre(promise_tool, variables, body)
            variables = got["vars"]
            sent.append(bool(got["config"]["url"]))
        for (_, response), allowed in zip(calls, sent):
            if allowed and response is not None:
                variables = run_hook(promise_tool["post_process_code"], vars=variables, result=copy.deepcopy(response))["vars"]
        for (_, response), allowed in zip(calls, sent):
            counter = "create_promise_" + ("success_count" if allowed and response is not None else "failure_count")
            variables[counter] = int(variables.get(counter) or 0) + 1
        return variables, sent

    bodies = [{"customer_id": "synthetic-1", "product_id": f"p{i}", "amount": 100, "date": "2026-10-16"} for i in (1, 2, 3)]
    initial, sent = batch(base_facts, [(bodies[0], None), (bodies[1], ok), (bodies[2], ok)])
    wrong_retry, retry_sent = batch(initial, [(bodies[1], ok)])
    record(8, "Residual parallel failed-member attribution", {"parallel_batch": ["p1: no post-hook", "p2: ok", "p3: ok"],
           "retry": "p2 (already positively acknowledged)"}, {"initial_sent": sent, "initial_promise_acks": initial.get("promise_acks"),
           "initial_promised_products": initial.get("promised_products"), "retry_sent": retry_sent,
           "after_retry_promise_acks": wrong_retry.get("promise_acks"), "after_retry_promised_products": wrong_retry.get("promised_products")},
           kind="fresh local explicit batch double: all pre-hooks then mock post-hooks, supplied HTTP counters",
           limit="p1 has no positive bank response in this probe. If it appears after wrong-member retry, attribution is incorrect. No live engine was called.")
    after = snapshot()
    changed = [name for name in sorted(set(before) | set(after)) if before.get(name) != after.get(name)]
    report = {"started_at_utc": started, "completed_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
              "probe_command": [sys.executable, "-B", str(Path(__file__)), *sys.argv[1:]],
              "scope": "synthetic local readiness only; official hidden briefs not supplied", "python": sys.version,
              "tests": tests, "probes": rows, "source_stable": not changed, "changed_sources": changed,
              "source_before_sha256": before, "source_after_sha256": after}
    (OUT / "policy-results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps({"tests": tests.get("totals"), "exit_code": tests["exit_code"], "source_stable": not changed,
                      "probes": [{"id": r["id"], "matches": r["matches_literal_expectation"]} for r in rows]}, indent=2), flush=True)
    return tests["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
