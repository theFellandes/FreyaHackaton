import test from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { readFileSync } from "node:fs";
import { createBankService } from "../src/bank-core.mjs";
import { validateReceipt } from "../tools/verify-ready.mjs";

const worker = createBankService(JSON.parse(readFileSync(new URL("../../case/test-environment/test-customers.json", import.meta.url), "utf8")));

test("missing storage cannot be treated as a ready bank", async () => {
  const response = await worker.fetch(new Request("https://example.test/__submission_health"));
  assert.equal(response.status, 503);
  assert.equal(response.headers.get("cache-control"), "no-store");
  const body = await response.json();
  assert.equal(body.ready, false);
  assert.equal(body.bank_ready, false);
});

test("unknown service requests fail without fabricating bank results", async () => {
  const response = await worker.fetch(new Request("https://example.test/unspecified-contract-operation", {
    method: "POST", body: "{}"
  }));
  assert.equal(response.status, 400);
  const body = await response.json();
  assert.equal(body.result.returnCode, "INVALID_ARGUMENTS_ERROR");
  assert.equal(Object.hasOwn(body, "data"), false);
  assert.equal(Object.hasOwn(body, "products"), false);
});

test("preparation cannot deploy through its advertised command", () => {
  const result = spawnSync(process.execPath, [fileURLToPath(new URL("../tools/deploy-blocked.mjs", import.meta.url))], {
    encoding: "utf8"
  });
  assert.equal(result.status, 2);
  assert.match(result.stderr, /Deployment is blocked/);
});

test("receipt checker rejects absent or unready deployment evidence", () => {
  assert.throws(() => validateReceipt(null), /bank_ready=true/);
  assert.throws(() => validateReceipt({ schema_version: 1, bank_ready: false }), /bank_ready=true/);
  assert.throws(() => validateReceipt({ schema_version: 1, bank_ready: true }), /Missing worker_name/);
});

test("runtime metadata cannot make missing storage ready", async () => {
  const response = await worker.fetch(new Request("https://example.test/__submission_health"), {
    CF_VERSION_METADATA: { id: "runtime-version" },
    BANK_BUNDLE_SHA256: "a".repeat(64),
    BANK_CONTRACT_SHA256: "b".repeat(64),
    BANK_CUSTOMER_FIXTURE_SHA256: "c".repeat(64)
  });
  const body = await response.json();
  assert.equal(response.status, 503);
  assert.equal(body.ready, false);
  assert.equal(body.bank_ready, false);
  assert.equal(body.cloudflare_version_id, "runtime-version");
  assert.equal(body.bundle_sha256, "a".repeat(64));
});
