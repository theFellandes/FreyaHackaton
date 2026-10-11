import assert from "node:assert/strict";
import { createHash, randomUUID } from "node:crypto";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, isAbsolute, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const evidenceDir = resolve(root, "evidence");
const baseUrl = "http://127.0.0.1:8787";
const fixtureBytes = readFileSync(resolve(root, "../case/test-environment/test-customers.json"));
const fixtures = JSON.parse(fixtureBytes);
const sha256 = (bytes) => createHash("sha256").update(bytes).digest("hex");
const fixtureHash = sha256(fixtureBytes);
const customer = fixtures.customers.find((entry) => entry.customerNo === "90000001");
const slowCustomer = fixtures.customers.find((entry) => entry.customerNo === "94907600");

function evidencePath(path) {
  const absolute = resolve(root, path), rel = relative(evidenceDir, absolute);
  if (!rel || isAbsolute(rel) || rel.startsWith("..")) throw new Error("Evidence path must be a file inside evidence/.");
  return absolute;
}
function save(path, value) {
  const absolute = evidencePath(path);
  writeFileSync(absolute, JSON.stringify(value, null, 2) + "\n", { encoding: "utf8", flag: "wx" });
  return absolute;
}
async function get(path) {
  const started = performance.now();
  const response = await fetch(baseUrl + path, { signal: AbortSignal.timeout(25000) });
  return { http: response.status, body: await response.json(), elapsed_ms: Math.round(performance.now() - started) };
}
async function post(service, data, sessionId) {
  const started = performance.now();
  const response = await fetch(baseUrl + "/voice/v1/" + service, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ data, call: { sessionId } }), signal: AbortSignal.timeout(25000),
  });
  return { http: response.status, body: await response.json(), elapsed_ms: Math.round(performance.now() - started) };
}
async function state(customerNo, sessionId) {
  const result = await get("/__submission_state?" + new URLSearchParams({ customerNo, sessionId }));
  assert.equal(result.http, 200); return result.body;
}
function exactAnswer(actual, expected) {
  assert.equal(actual.http, expected.http); assert.deepEqual(actual.body, expected.response);
}
function check(report, name, details) { report.checks.push({ name, passed: true, ...details }); }

async function before(plan, report) {
  const health = await get("/__submission_health");
  assert.equal(health.http, 200); assert.equal(health.body.ready, true); assert.equal(health.body.bank_ready, true);
  assert.equal(health.body.storage_backend, "sqlite-durable-object");
  assert.equal(health.body.customer_count, 34); assert.equal(health.body.canonical_card_count, 65);
  check(report, "health_sqlite_probe", { http: health.http, health: health.body });
  assert.equal((await state("90000001", plan.session_id)).operationCount, 0);
  check(report, "fresh_session_empty", { operation_count: 0 });
  const listed = await post("cards.list", { customerNo: 90000001 }, plan.session_id);
  exactAnswer(listed, customer.services["cards.list"]);
  check(report, "cards_list_exact_fixture", { http: listed.http, response_sha256: sha256(JSON.stringify(listed.body)) });
  const changed = await post("statement.week.change", {
    cardNumber: "540000******1234", newStatementWeek: 2, statementDay: 5,
  }, plan.session_id);
  exactAnswer(changed, customer.services["statement.week.change"]["540000******1234"]["2"]);
  check(report, "week_change_exact_fixture_card_only", { http: changed.http, response_sha256: sha256(JSON.stringify(changed.body)) });
  const saved = await state("90000001", plan.session_id);
  assert.equal(saved.operationCount, 2); assert.equal(saved.operations.length, 2); assert.equal(saved.acknowledgments.length, 1);
  assert.deepEqual(saved.operations.map((entry) => entry.service), ["cards.list", "statement.week.change"]);
  assert.equal(saved.acknowledgments[0].kind, "statement_week_change");
  assert.equal(saved.acknowledgments[0].card_number, "540000******1234");
  report.record_ids_before_restart = saved.operations.map((entry) => entry.record_id);
  report.state_before_restart = saved;
  check(report, "records_and_change_acknowledgment", { operation_count: 2, acknowledgment_count: 1 });
  assert.equal((await state("90000001", plan.other_session_id)).operationCount, 0);
  check(report, "same_customer_other_session_isolated", { operation_count: 0 });
  assert.equal((await state("92122831", plan.session_id)).operationCount, 0);
  check(report, "other_customer_same_session_isolated", { operation_count: 0 });
  report.customer_isolation_verified = true; report.session_isolation_verified = true;
  report.persistence_verified = false; report.ready_for_restart = true;
}

async function after(plan, report) {
  const beforeBytes = readFileSync(evidencePath(plan.before_report));
  const prior = JSON.parse(beforeBytes);
  assert.equal(prior.passed, true); assert.equal(prior.ready_for_restart, true);
  assert.equal(prior.run_id, plan.run_id);
  report.before_report = plan.before_report;
  report.before_report_sha256 = sha256(beforeBytes);
  const health = await get("/__submission_health");
  assert.equal(health.http, 200); assert.equal(health.body.bank_ready, true);
  check(report, "health_after_restart", { http: health.http, health: health.body });
  const restored = await state("90000001", plan.session_id);
  assert.deepEqual(restored, prior.state_before_restart);
  assert.equal(restored.operationCount, 2); assert.equal(restored.acknowledgments.length, 1);
  check(report, "restart_preserved_exact_records_and_acknowledgment", { operation_count: 2, acknowledgment_count: 1,
    record_ids: restored.operations.map((entry) => entry.record_id) });
  assert.equal((await state("90000001", plan.other_session_id)).operationCount, 0);
  assert.equal((await state("92122831", plan.session_id)).operationCount, 0);
  check(report, "restart_preserved_customer_and_session_isolation", { other_session_count: 0, other_customer_count: 0 });
  const data = { customerNo: "90000001", cardNumber: "540000******1234", currencyType: "TL",
    statementDate: "05.10.2026", emailAddress: "ornek@freyabank.example" };
  for (let index = 0; index < 2; index++) {
    const result = await post("statement.pdf.send", data, plan.session_id);
    exactAnswer(result, customer.services["statement.pdf.send"]);
    check(report, "pdf_send_" + (index + 1) + "_exact_fixture", { http: result.http, response_sha256: sha256(JSON.stringify(result.body)) });
  }
  const saved = await state("90000001", plan.session_id);
  assert.equal(saved.operationCount, 4); assert.equal(saved.operations.length, 4); assert.equal(saved.acknowledgments.length, 3);
  assert.equal(new Set(saved.operations.map((entry) => entry.record_id)).size, 4);
  assert.deepEqual(saved.acknowledgments.map((entry) => entry.kind), ["statement_week_change", "statement_pdf_send", "statement_pdf_send"]);
  report.state_after_duplicate_sends = saved;
  check(report, "duplicate_sends_recorded_separately", { operation_count: 4, acknowledgment_count: 3 });
  const unchanged = await post("statement.week.get", { cardNumber: "540000******1234" }, plan.readback_session_id);
  exactAnswer(unchanged, customer.services["statement.week.get"]["540000******1234"]);
  check(report, "fixture_template_unchanged_after_change", { http: unchanged.http, response_sha256: sha256(JSON.stringify(unchanged.body)) });
  const expectedSlow = slowCustomer.services["cards.balance"]["540000******7211"];
  const slow = await post("cards.balance", { cardNumber: "540000******7211" }, plan.slow_session_id);
  exactAnswer(slow, expectedSlow);
  const expectedMs = expectedSlow.delaySeconds * 1000;
  assert.ok(slow.elapsed_ms >= expectedMs - 500, `expected approximately ${expectedMs}ms delay; observed ${slow.elapsed_ms}ms`);
  check(report, "actual_slow_fixture_delay", { http: slow.http, expected_delay_ms: expectedMs,
    observed_elapsed_ms: slow.elapsed_ms, minimum_accepted_elapsed_ms: expectedMs - 500, response_sha256: sha256(JSON.stringify(slow.body)) });
  assert.equal((await state("94907600", plan.slow_session_id)).operationCount, 1);
  report.storage_backend = "sqlite-durable-object";
  report.persistence_verified = true; report.customer_isolation_verified = true; report.session_isolation_verified = true;
}

const phase = process.argv[2];
if (!["before", "after"].includes(phase)) throw new Error("Usage: node tools/workerd-acceptance.mjs before | after evidence/PLAN.json");
mkdirSync(evidenceDir, { recursive: true });
let plan, planPath;
if (phase === "before") {
  const runId = new Date().toISOString().replaceAll(":", "-").replaceAll(".", "-") + "-" + randomUUID().slice(0, 8);
  plan = { schema_version: 1, run_id: runId, base_url: baseUrl, customer_fixture_sha256: fixtureHash,
    session_id: "workerd-" + runId, other_session_id: "workerd-other-" + runId,
    slow_session_id: "workerd-slow-" + runId, readback_session_id: "workerd-readback-" + runId,
    before_report: "evidence/workerd-" + runId + ".before.json", after_report: "evidence/workerd-" + runId + ".after.json" };
  planPath = "evidence/workerd-" + runId + ".plan.json";
  save(planPath, plan);
} else {
  planPath = process.argv[3];
  if (!planPath) throw new Error("An existing before-phase plan is required.");
  plan = JSON.parse(readFileSync(evidencePath(planPath), "utf8"));
  assert.equal(plan.base_url, baseUrl); assert.equal(plan.customer_fixture_sha256, fixtureHash);
}
const report = { report_schema_version: 1, phase: phase + "_restart", run_id: plan.run_id, base_url: baseUrl,
  customer_fixture_sha256: fixtureHash, session_id: plan.session_id, started_at_utc: new Date().toISOString(), checks: [] };
try {
  await (phase === "before" ? before : after)(plan, report);
  report.passed = true;
} catch (error) {
  report.passed = false; report.error = error.message; process.exitCode = 1;
}
report.completed_at_utc = new Date().toISOString();
const reportPath = save(phase === "before" ? plan.before_report : plan.after_report, report);
console.log(JSON.stringify({ passed: report.passed, phase, checks_passed: report.checks.length, ready_for_restart: report.ready_for_restart ?? false,
  plan_path: planPath, report_path: reportPath, error: report.error ?? null }, null, 2));
