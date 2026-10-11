import test from "node:test";
import assert from "node:assert/strict";
import { DatabaseSync } from "node:sqlite";
import { mkdtempSync, unlinkSync, rmdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { createBankStore } from "../src/bank-store.mjs";

function adapter(db) {
  return { sql: { exec(query, ...bindings) {
    const statement = db.prepare(query);
    const rows = statement.columns().length ? statement.all(...bindings).map((row) => ({ ...row })) : (statement.run(...bindings), []);
    return { toArray: () => rows };
  } }, transactionSync(callback) {
    db.exec("BEGIN");
    try { const value = callback(); db.exec("COMMIT"); return value; }
    catch (error) { db.exec("ROLLBACK"); throw error; }
  } };
}
function record(recordId, customerNo = "90000001", sessionId = "session-a") {
  return { recordId, customerNo, sessionId, service: "statement.pdf.send", requestData: { emailAddress: "fixture@example.test" },
    http: 200, response: { data: { exitState: "DONE" }, result: { returnCode: "1" } },
    acknowledgments: [{ kind: "statement_pdf_send", cardNumber: "540000******1234", data: { exitState: "DONE" } }],
    createdAtUtc: "2026-10-11T17:45:00Z" };
}

test("SQL records exact responses and repeated sends as distinct acknowledged operations", () => {
  const db = new DatabaseSync(":memory:");
  try {
    const store = createBankStore(adapter(db)); store.initialize();
    store.recordOperation(record("first")); store.recordOperation(record("second"));
    const state = store.snapshot("90000001", "session-a");
    assert.equal(state.operationCount, 2); assert.equal(state.acknowledgments.length, 2);
    assert.deepEqual(state.operations.map((x) => x.record_id), ["first", "second"]);
    assert.deepEqual(JSON.parse(db.prepare("SELECT response_json FROM operations WHERE record_id = ?").get("first").response_json), record("first").response);
    assert.equal(JSON.stringify(state).includes("fixture@example.test"), false);
    assert.equal(store.probe().ready, true);
  } finally { db.close(); }
});

test("customer and session filters independently isolate records and acknowledgments", () => {
  const db = new DatabaseSync(":memory:");
  try {
    const store = createBankStore(adapter(db)); store.initialize();
    store.recordOperation(record("a")); store.recordOperation(record("b", "90000001", "session-b"));
    store.recordOperation(record("c", "92122831", "session-a"));
    for (const [customer, session, id] of [["90000001", "session-a", "a"], ["90000001", "session-b", "b"], ["92122831", "session-a", "c"]]) {
      const state = store.snapshot(customer, session);
      assert.equal(state.operationCount, 1); assert.deepEqual(state.operations.map((x) => x.record_id), [id]);
      assert.equal(state.acknowledgments.length, 1);
    }
    assert.equal(store.snapshot("92122831", "session-b").operationCount, 0);
  } finally { db.close(); }
});

test("acknowledgment write failure rolls back operation and session updates together", () => {
  const db = new DatabaseSync(":memory:");
  try {
    const store = createBankStore(adapter(db)); store.initialize(); store.recordOperation(record("before"));
    const bad = record("bad"); bad.acknowledgments[0].cardNumber = null;
    assert.throws(() => store.recordOperation(bad));
    const state = store.snapshot("90000001", "session-a");
    assert.equal(state.operationCount, 1); assert.deepEqual(state.operations.map((x) => x.record_id), ["before"]);
    assert.equal(state.acknowledgments.length, 1);
  } finally { db.close(); }
});

test("disk SQLite records survive database close and helper reconstruction", () => {
  const testDir = dirname(fileURLToPath(import.meta.url));
  const directory = mkdtempSync(resolve(testDir, ".sqlite-persistence-"));
  const databasePath = resolve(directory, "bank.sqlite");
  let db;
  try {
    db = new DatabaseSync(databasePath);
    const store = createBankStore(adapter(db)); store.initialize(); store.recordOperation(record("persisted"));
    db.close(); db = new DatabaseSync(databasePath);
    const restored = createBankStore(adapter(db)); restored.initialize();
    assert.equal(restored.snapshot("90000001", "session-a").operationCount, 1);
    assert.equal(restored.snapshot("90000001", "session-a").operations[0].record_id, "persisted");
    assert.equal(restored.snapshot("90000001", "session-b").operationCount, 0);
  } finally { db?.close(); unlinkSync(databasePath); rmdirSync(directory); }
});
