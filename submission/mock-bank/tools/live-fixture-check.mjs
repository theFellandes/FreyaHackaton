import assert from "node:assert/strict";
import { createHash, randomUUID } from "node:crypto";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const baseUrl = "https://freya-hackathon-bank-oguzhan.freya-submission-bank-preparation.workers.dev";
const expectedVersion = "af006baa-a9c0-4dab-bb5a-3b7a0ead0d14";
const sha256 = (bytes) => createHash("sha256").update(bytes).digest("hex");
const fixtureBytes = readFileSync(resolve(root, "../case/test-environment/test-customers.json"));
const fixtures = JSON.parse(fixtureBytes);
const configurationBytes = readFileSync(resolve(root, "wrangler.deployment.jsonc"));
const configuration = JSON.parse(configurationBytes);
const expectedHashes = {
  bundle_sha256: sha256(readFileSync(resolve(root, "evidence/deployment/compiled/index.js"))),
  contract_sha256: sha256(readFileSync(resolve(root, "../case/sources/documents/2026-10-01-service-contract.md"))),
  customer_fixture_sha256: sha256(fixtureBytes),
};
const runId = new Date().toISOString().replaceAll(":", "-").replaceAll(".", "-") + "-" + randomUUID().slice(0, 8);
const sessionId = "live-fixtures-" + runId;
const report = { report_schema_version: 1, phase: "live_contract_acceptance", run_id: runId, base_url: baseUrl,
  cloudflare_version_id: expectedVersion, session_id: sessionId, max_parallel_requests: 8, retries: 0,
  started_at_utc: new Date().toISOString(), configuration_sha256: sha256(configurationBytes),
  ...expectedHashes, health_checks: [], fixture_checks: [], root_alias_checks: [], error_checks: [], state_checks: [] };

function fixtureRows() {
  const rows = [];
  for (const customer of fixtures.customers) for (const [service, value] of Object.entries(customer.services)) {
    function walk(entry, path = []) {
      if (Object.hasOwn(entry, "http")) {
        const data = {};
        if (["cards.list", "contact.email.get", "statement.pdf.send"].includes(service)) data.customerNo = customer.customerNo;
        if (path[0]) data.cardNumber = path[0];
        if (path[1]) Object.assign(data, { newStatementWeek: Number(path[1]),
          statementDay: customer.services["statement.week.get"][path[0]].response.data?.statementDay ?? 1 });
        if (service === "contact.email.get") data.intAddressInfoType = "E";
        if (service === "statement.pdf.send") {
          data.cardNumber = Object.keys(customer.services["cards.balance"])[0];
          Object.assign(data, { currencyType: "TL", statementDate: customer.services["cards.balance"][data.cardNumber].response.data.statementDate,
            emailAddress: customer.services["contact.email.get"].response.data?.ibanEmail || "alternate@fixture.example" });
        }
        const success = entry.http === 200 && entry.response.result?.returnCode === "1";
        const related = path[0] && customer.services["statement.week.get"]?.[path[0]]?.response?.data?.relatedCardNumber;
        const expectedAcknowledgments = success && service === "statement.week.change" ? related ? 2 : 1
          : success && service === "statement.pdf.send" ? 1 : 0;
        rows.push({ customerNo: customer.customerNo, service, cardKey: path[0], weekKey: path[1], data,
          expected: entry, expectedAcknowledgments }); return;
      }
      for (const [key, child] of Object.entries(entry)) walk(child, [...path, key]);
    }
    walk(value);
  }
  return rows;
}

async function request(path, data) {
  const started = performance.now();
  const response = await fetch(baseUrl + path, {
    ...(data === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ data, call: { sessionId } }) }), signal: AbortSignal.timeout(30000),
  });
  const text = await response.text();
  let body;
  try { body = JSON.parse(text); } catch { throw new Error(`Non-JSON HTTP ${response.status}: ${text.slice(0, 1000)}`); }
  return { http: response.status, body, elapsed_ms: Math.round(performance.now() - started), response_sha256: sha256(text) };
}

async function pool(rows, callback) {
  let next = 0;
  const results = new Array(rows.length);
  await Promise.all(Array.from({ length: Math.min(8, rows.length) }, async () => {
    for (;;) {
      const index = next++;
      if (index >= rows.length) return;
      results[index] = await callback(rows[index], index);
    }
  }));
  return results;
}

async function health(label) {
  const result = { name: label, passed: false };
  try {
    const actual = await request("/__submission_health");
    result.http = actual.http; result.health = actual.body;
    assert.equal(actual.http, 200); assert.equal(actual.body.ready, true); assert.equal(actual.body.bank_ready, true);
    assert.equal(actual.body.cloudflare_version_id, expectedVersion);
    assert.equal(actual.body.storage_backend, "sqlite-durable-object");
    assert.equal(actual.body.customer_count, 34); assert.equal(actual.body.canonical_card_count, 65);
    for (const [field, value] of Object.entries(expectedHashes)) assert.equal(actual.body[field], value, field);
    result.passed = true;
  } catch (error) { result.error = error.message; }
  report.health_checks.push(result);
  return result.passed;
}

async function compare(row, prefix) {
  const result = { customerNo: row.customerNo, service: row.service, cardKey: row.cardKey, weekKey: row.weekKey,
    path: prefix + row.service, expected_http: row.expected.http, passed: false };
  try {
    const actual = await request(result.path, row.data);
    Object.assign(result, { actual_http: actual.http, elapsed_ms: actual.elapsed_ms, response_sha256: actual.response_sha256 });
    assert.equal(actual.http, row.expected.http); assert.deepEqual(actual.body, row.expected.response);
    if (row.expected.delaySeconds) {
      result.expected_delay_ms = row.expected.delaySeconds * 1000;
      assert.ok(actual.elapsed_ms >= result.expected_delay_ms - 500, "fixture delay was not observed");
    }
    result.passed = true;
  } catch (error) { result.error = error.message; }
  return result;
}

const unknownCustomer = "99999999", unknownCard = "540000******0000";
const negativeCases = [
  ["cards.list", { customerNo: unknownCustomer }, 404, "FB_IVR_CHANNEL_102"],
  ["cards.balance", { cardNumber: unknownCard }, 404, "FB_IVR_CHANNEL_120"],
  ["statement.week.get", { cardNumber: unknownCard }, 404, "FB_IVR_CHANNEL_121"],
  ["statement.week.decide", { cardNumber: unknownCard, newStatementWeek: 2, statementDay: 5 }, 400, "INVALID_ARGUMENTS_ERROR"],
  ["statement.week.change", { cardNumber: unknownCard, newStatementWeek: 2, statementDay: 5 }, 400, "INVALID_ARGUMENTS_ERROR"],
  ["contact.email.get", { customerNo: unknownCustomer, intAddressInfoType: "E" }, 400, "INVALID_ARGUMENTS_ERROR"],
  ["statement.pdf.send", { customerNo: unknownCustomer, cardNumber: unknownCard, currencyType: "TL", statementDate: "05.10.2026", emailAddress: "unknown@fixture.example" }, 404, "FB_IVR_CHANNEL_107"],
  ["cards.balance", {}, 400, "INVALID_ARGUMENTS_ERROR"],
];

try {
  assert.equal(expectedHashes.bundle_sha256, configuration.vars.BANK_BUNDLE_SHA256);
  assert.equal(expectedHashes.contract_sha256, configuration.vars.BANK_CONTRACT_SHA256);
  assert.equal(expectedHashes.customer_fixture_sha256, configuration.vars.BANK_CUSTOMER_FIXTURE_SHA256);
  const rows = fixtureRows(); assert.equal(rows.length, 617);
  report.fixture_case_count = rows.length;
  if (!await health("before_live_fixtures")) throw new Error("Live health/version/hashes did not verify; fixture calls were not started.");
  let completed = 0, failed = 0;
  report.fixture_checks = await pool(rows, async (row) => {
    const result = await compare(row, "/voice/v1/");
    completed++; if (!result.passed) failed++;
    if (completed % 100 === 0 || completed === rows.length) console.log(JSON.stringify({ phase: "fixtures", completed, total: rows.length, failed }));
    return result;
  });
  const aliases = [...new Set(rows.map((row) => row.service))].map((service) => rows.find((row) => row.service === service));
  report.root_alias_checks = await pool(aliases, (row) => compare(row, "/"));
  report.error_checks = await pool(negativeCases, async ([service, data, http, code]) => {
    const result = { service, expected_http: http, expected_code: code, passed: false };
    try {
      const actual = await request("/voice/v1/" + service, data);
      Object.assign(result, { actual_http: actual.http, actual_code: actual.body.result?.code, elapsed_ms: actual.elapsed_ms });
      assert.equal(actual.http, http); assert.equal(actual.body.result?.returnCode, code);
      assert.equal(actual.body.result?.code, code); assert.equal(actual.body.result?.message, actual.body.result?.messageText);
      assert.equal(Object.hasOwn(actual.body, "data"), false);
      result.passed = true;
    } catch (error) { result.error = error.message; }
    return result;
  });
  const expectedState = new Map(fixtures.customers.map((customer) => [customer.customerNo, { operations: 0, acknowledgments: 0 }]));
  for (const row of [...rows, ...aliases]) {
    const counts = expectedState.get(row.customerNo); counts.operations++; counts.acknowledgments += row.expectedAcknowledgments;
  }
  report.state_checks = await pool([...expectedState], async ([customerNo, counts]) => {
    const result = { customerNo, expected_operations: counts.operations, expected_acknowledgments: counts.acknowledgments, passed: false };
    try {
      const actual = await request("/__submission_state?" + new URLSearchParams({ customerNo, sessionId }));
      Object.assign(result, { actual_http: actual.http, actual_operations: actual.body.operationCount,
        actual_acknowledgments: actual.body.acknowledgments?.length });
      assert.equal(actual.http, 200); assert.equal(actual.body.customerNo, customerNo); assert.equal(actual.body.sessionId, sessionId);
      assert.equal(actual.body.operationCount, counts.operations); assert.equal(actual.body.operations.length, counts.operations);
      assert.equal(actual.body.acknowledgments.length, counts.acknowledgments);
      assert.equal(new Set(actual.body.operations.map((entry) => entry.record_id)).size, counts.operations);
      result.passed = true;
    } catch (error) { result.error = error.message; }
    return result;
  });
  await health("after_live_fixtures");
} catch (error) { report.error = error.message; }

const groups = [report.health_checks, report.fixture_checks, report.root_alias_checks, report.error_checks, report.state_checks];
report.case_count = groups.reduce((count, group) => count + group.length, 0);
report.failed_case_count = groups.flat().filter((check) => !check.passed).length;
report.passed = !report.error && report.fixture_checks.length === 617 && report.failed_case_count === 0;
report.completed_at_utc = new Date().toISOString();
const outputDir = resolve(root, "evidence"); mkdirSync(outputDir, { recursive: true });
const outputPath = resolve(outputDir, "live-fixtures-" + runId + ".json");
writeFileSync(outputPath, JSON.stringify(report, null, 2) + "\n", { encoding: "utf8", flag: "wx" });
console.log(JSON.stringify({ passed: report.passed, fixture_cases: report.fixture_checks.length,
  fixture_failures: report.fixture_checks.filter((check) => !check.passed).length,
  root_alias_cases: report.root_alias_checks.length, error_cases: report.error_checks.length, state_customer_cases: report.state_checks.length,
  total_checks: report.case_count, total_failed_checks: report.failed_case_count, session_id: sessionId,
  report_path: outputPath, report_sha256: sha256(readFileSync(outputPath)), error: report.error ?? null }, null, 2));
if (!report.passed) process.exitCode = 1;
