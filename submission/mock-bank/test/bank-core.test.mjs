import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { BANK_SERVICES, createBankService, sessionObjectName } from "../src/bank-core.mjs";

const fixtures = JSON.parse(readFileSync(new URL("../../case/test-environment/test-customers.json", import.meta.url), "utf8"));
const records = [], delays = [];
const service = createBankService(fixtures, { sleep: async (ms) => { delays.push(ms); } });
const env = { BANK_SESSIONS: { getByName: (name) => ({
  recordOperation: async (record) => { records.push({ name, record }); return { recorded: true }; },
  probe: async () => ({ ready: true }),
  snapshot: async (customerNo, sessionId) => ({ customerNo, sessionId,
    operationCount: records.filter((row) => row.name === name).length }),
}) } };
function request(operation, data, { prefix = "/voice/v1/", call, headers = {}, method = "POST" } = {}) {
  return new Request(`https://bank.example${prefix}${operation}`, {
    method, headers, ...(method === "POST" ? { body: JSON.stringify({ data, ...(call === undefined ? {} : { call }) }) } : {}),
  });
}
function fixtureRows() {
  const rows = [];
  for (const customer of fixtures.customers) for (const [operation, root] of Object.entries(customer.services)) {
    function walk(value, path = []) {
      if (Object.hasOwn(value, "http")) {
        const data = {};
        if (["cards.list", "contact.email.get", "statement.pdf.send"].includes(operation)) data.customerNo = customer.customerNo;
        if (path[0]) data.cardNumber = path[0];
        if (path[1]) Object.assign(data, { newStatementWeek: Number(path[1]), statementDay: customer.services["statement.week.get"][path[0]].response.data?.statementDay ?? 1 });
        if (operation === "contact.email.get") data.intAddressInfoType = "E";
        if (operation === "statement.pdf.send") {
          data.cardNumber = Object.keys(customer.services["cards.balance"])[0];
          Object.assign(data, { currencyType: "TL", statementDate: customer.services["cards.balance"][data.cardNumber].response.data.statementDate,
            emailAddress: customer.services["contact.email.get"].response.data?.ibanEmail || "alternate@fixture.example" });
        }
        rows.push({ operation, data, leaf: value }); return;
      }
      for (const [key, child] of Object.entries(value)) walk(child, [...path, key]);
    }
    walk(root);
  }
  return rows;
}

test("all 617 fixture responses match through canonical and root HTTP routes", async () => {
  const rows = fixtureRows();
  assert.equal(rows.length, 617);
  assert.equal(service.customerCount, 34);
  assert.equal(service.canonicalCardCount, 65);
  const start = records.length, delayStart = delays.length;
  for (const prefix of ["/voice/v1/", "/"]) for (const row of rows) {
    const response = await service.fetch(request(row.operation, row.data, { prefix }), env);
    assert.equal(response.status, row.leaf.http, JSON.stringify({ prefix, operation: row.operation, data: row.data }));
    assert.deepEqual(await response.json(), row.leaf.response);
    assert.equal(response.headers.get("cache-control"), "no-store");
  }
  assert.equal(records.length - start, 1234);
  assert.deepEqual(delays.slice(delayStart), [12000, 12000]);
});

test("numeric customers and weeks, ignored headers/call and optional card-only customer work", async () => {
  const expected = fixtures.customers[0].services["cards.list"].response;
  const response = await service.fetch(request("cards.list", { customerNo: 90000001, aiFlow: "not-validated" }, {
    call: { ani: "sensitive-call-ani", arbitrary: "ignored" }, headers: { Authorization: "Bearer not-validated", "Content-Type": "text/plain" },
  }), env);
  assert.deepEqual(await response.json(), expected);
  const cardOnly = await service.fetch(request("cards.balance", { cardNumber: "540000******1234" }), env);
  assert.equal(cardOnly.status, 200);
  const numeric = service.evaluate("statement.week.decide", { data: { cardNumber: "540000******1234", newStatementWeek: 2, statementDay: 5 } });
  const string = service.evaluate("statement.week.decide", { data: { cardNumber: "540000******1234", newStatementWeek: "2", statementDay: "5" } });
  assert.deepEqual(numeric.response, string.response);
  assert.equal(JSON.stringify(records.at(-2)).includes("sensitive-call-ani"), false);
  assert.equal(JSON.stringify(records.at(-2)).includes("not-validated\""), true);
  assert.equal(JSON.stringify(records.at(-2)).includes("Bearer"), false);
});

test("unknown customers/cards and sparse weeks use contracted errors without data", () => {
  const unknown = "99999999", unknownCard = "540000******0000";
  const cases = [
    ["cards.list", { customerNo: unknown }, 404, "FB_IVR_CHANNEL_102"],
    ["cards.balance", { cardNumber: unknownCard }, 404, "FB_IVR_CHANNEL_120"],
    ["statement.week.get", { cardNumber: unknownCard }, 404, "FB_IVR_CHANNEL_121"],
    ["statement.week.decide", { cardNumber: unknownCard, newStatementWeek: 2, statementDay: 5 }, 400, "INVALID_ARGUMENTS_ERROR"],
    ["statement.week.change", { cardNumber: unknownCard, newStatementWeek: 2, statementDay: 5 }, 400, "INVALID_ARGUMENTS_ERROR"],
    ["contact.email.get", { customerNo: unknown, intAddressInfoType: "E" }, 400, "INVALID_ARGUMENTS_ERROR"],
    ["statement.pdf.send", { customerNo: unknown, cardNumber: unknownCard, currencyType: "TL", statementDate: "05.10.2026", emailAddress: "any@fixture.example" }, 404, "FB_IVR_CHANNEL_107"],
    ["statement.week.decide", { cardNumber: "540000******1234", newStatementWeek: 1, statementDay: 5 }, 400, "INVALID_ARGUMENTS_ERROR"],
  ];
  for (const [operation, data, http, code] of cases) {
    const answer = service.evaluate(operation, { data });
    assert.equal(answer.http, http); assert.equal(answer.response.result.returnCode, code);
    assert.equal(answer.response.result.code, code);
    assert.equal(answer.response.result.message, answer.response.result.messageText);
    assert.equal(Object.hasOwn(answer.response, "data"), false);
  }
});

test("missing required fields and malformed requests never fabricate success", async () => {
  for (const operation of BANK_SERVICES) {
    const answer = service.evaluate(operation, { data: {} });
    assert.equal(answer.http, 400); assert.equal(answer.response.result.returnCode, "INVALID_ARGUMENTS_ERROR");
  }
  for (const body of [null, {}, { data: [] }, { data: null }]) assert.equal(service.evaluate("cards.list", body).http, 400);
  assert.equal(service.evaluate("contact.email.get", { data: { customerNo: "90000001", intAddressInfoType: "" } }).http, 400);
  assert.equal(service.evaluate("statement.week.change", { data: { cardNumber: "540000******1234", newStatementWeek: 5, statementDay: 5 } }).http, 400);
  assert.equal(service.evaluate("cards.balance", { data: { cardNumber: "1234" } }).http, 404);
  const badJson = await service.fetch(new Request("https://bank.example/voice/v1/cards.list", { method: "POST", body: "{" }), env);
  assert.equal(badJson.status, 400);
  assert.equal((await service.fetch(request("cards.list", undefined, { method: "GET" }), env)).status, 405);
});

test("backend sends to any nonempty address and repeated side effects remain separate calls", async () => {
  const data = { customerNo: "90000001", cardNumber: "540000******1234", currencyType: "TL", statementDate: "05.10.2026", emailAddress: "different@fixture.example" };
  const start = records.length;
  for (let attempt = 0; attempt < 2; attempt++) {
    const response = await service.fetch(request("statement.pdf.send", data, { call: { sessionId: "repeat-send" } }), env);
    assert.deepEqual(await response.json(), fixtures.customers[0].services["statement.pdf.send"].response);
  }
  const calls = records.slice(start);
  assert.equal(calls.length, 2); assert.notEqual(calls[0].record.recordId, calls[1].record.recordId);
  assert.equal(calls[0].record.acknowledgments[0].data.emailAddress, data.emailAddress);
});

test("change acknowledges both linked cards while preview and all fixture templates stay unchanged", async () => {
  const customer = fixtures.customers.find((c) => c.customerNo === "92220795");
  const data = { cardNumber: "540000******3201", newStatementWeek: 2, statementDay: 16 };
  const preview = service.evaluate("statement.week.decide", { data });
  assert.deepEqual(preview.acknowledgments, []);
  const change = service.evaluate("statement.week.change", { data });
  assert.deepEqual(change.acknowledgments.map((a) => a.cardNumber), ["540000******3201", "540000******3202"]);
  for (let attempt = 0; attempt < 2; attempt++) assert.equal((await service.fetch(request("statement.week.change", data), env)).status, 200);
  assert.deepEqual(service.evaluate("statement.week.get", { data: { cardNumber: data.cardNumber } }).response,
    customer.services["statement.week.get"][data.cardNumber].response);
});

test("session/customer routing stays separate and metadata route reveals no request contents", async () => {
  const start = records.length;
  for (const [customerNo, sessionId] of [["90000001", "one"], ["90000001", "two"], ["92122831", "one"]]) {
    await service.fetch(request("cards.list", { customerNo }, { headers: { "x-session-id": sessionId }, call: { ani: "secret-ani" } }), env);
    assert.equal(records.at(-1).name, sessionObjectName(customerNo, sessionId));
  }
  assert.equal(new Set(records.slice(start).map((row) => row.name)).size, 3);
  const state = await service.fetch(new Request("https://bank.example/__submission_state?customerNo=90000001&sessionId=one"), env);
  assert.deepEqual(await state.json(), { customerNo: "90000001", sessionId: "one", operationCount: 1 });
  assert.equal((await service.fetch(new Request("https://bank.example/__submission_state?customerNo=99999999"), env)).status, 400);
});

test("health probes storage and metadata alone cannot make unavailable storage ready", async () => {
  const metadata = { CF_VERSION_METADATA: { id: "v-test" }, BANK_BUNDLE_SHA256: "a".repeat(64), BANK_CONTRACT_SHA256: "b".repeat(64), BANK_CUSTOMER_FIXTURE_SHA256: "c".repeat(64) };
  const response = await service.fetch(new Request("https://bank.example/__submission_health"), { ...env, ...metadata });
  const health = await response.json();
  assert.equal(response.status, 200); assert.equal(health.ready, true); assert.equal(health.bank_ready, true);
  assert.equal(health.cloudflare_version_id, "v-test"); assert.equal(health.bundle_sha256, metadata.BANK_BUNDLE_SHA256);
  const missing = await service.fetch(new Request("https://bank.example/__submission_health"), metadata);
  assert.equal(missing.status, 503); assert.equal((await missing.json()).bank_ready, false);
  const unavailable = await service.fetch(request("cards.list", { customerNo: "90000001" }), {});
  assert.equal(unavailable.status, 500); assert.equal((await unavailable.json()).result.code, "FBE00099");
});

test("duplicate global canonical ownership fails fixture initialization", () => {
  const first = structuredClone(fixtures.customers[0]), second = structuredClone(first);
  second.customerNo = "00000002";
  assert.throws(() => createBankService({ customers: [first, second] }), /multiple customers/);
});
