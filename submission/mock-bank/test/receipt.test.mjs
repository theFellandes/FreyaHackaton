import test from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdtempSync, readFileSync, rmdirSync, unlinkSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { validateReceipt, verifyLive } from "../tools/verify-ready.mjs";

const testRoot = dirname(fileURLToPath(import.meta.url));

function withEvidence(run) {
  const root = mkdtempSync(resolve(testRoot, ".receipt-check-"));
  const names = ["bundle.mjs", "configuration.json", "contract.json", "customers.json", "source.mjs", "storage.json", "contract-tests.json"];
  const artifact = (name) => ({ path: name, sha256: createHash("sha256").update(readFileSync(resolve(root, name))).digest("hex") });
  for (const name of names) writeFileSync(resolve(root, name), name + "\n", "utf8");
  const receipt = {
    schema_version: 1, bank_ready: true, worker_name: "test-bank",
    base_url: "https://bank.example.test/v1", health_url: "https://bank.example.test/__submission_health",
    deployment_id: "deployment-123", cloudflare_version_id: "version-123", deployed_at_utc: "2026-10-11T17:05:00Z",
    bundle: artifact("bundle.mjs"), configuration: artifact("configuration.json"),
    contract: artifact("contract.json"), customer_fixtures: artifact("customers.json"),
    source_files: [artifact("source.mjs")],
    storage: { backend: "contract-selected-storage", persistence_verified: true, customer_isolation_verified: true, evidence: artifact("storage.json") },
    contract_tests: { passed: true, case_count: 1, report: artifact("contract-tests.json") }
  };
  const cleanup = () => {
    for (const name of names) unlinkSync(resolve(root, name));
    rmdirSync(root);
  };
  try {
    const result = run({ root, receipt });
    if (result && typeof result.then === "function") return result.finally(cleanup);
    cleanup();
    return result;
  } catch (error) {
    cleanup();
    throw error;
  }
}

test("receipt verifies immutable evidence and matching live metadata", async () => withEvidence(async ({ root, receipt }) => {
  assert.equal(validateReceipt(receipt, root).hostname, "bank.example.test");
  const result = await verifyLive(receipt, async () => Response.json({
    ready: true, bank_ready: true, cloudflare_version_id: receipt.cloudflare_version_id,
    bundle_sha256: receipt.bundle.sha256, contract_sha256: receipt.contract.sha256,
    customer_fixture_sha256: receipt.customer_fixtures.sha256
  }), root);
  assert.equal(result.bank_ready, true);
}));

test("changed deployed source is rejected before any readiness request", () => withEvidence(({ root, receipt }) => {
  writeFileSync(resolve(root, "source.mjs"), "modified after deployment", "utf8");
  assert.throws(() => validateReceipt(receipt, root), /source_files\[0\] hash differs/);
}));

test("repository-escaping evidence and missing storage verification fail", () => withEvidence(({ root, receipt }) => {
  const escaped = structuredClone(receipt);
  escaped.bundle.path = "../outside-repository.mjs";
  assert.throws(() => validateReceipt(escaped, root), /leaves the repository/);
  receipt.storage.persistence_verified = false;
  assert.throws(() => validateReceipt(receipt, root), /Persistent storage/);
}));

test("ready flags alone cannot conceal a different deployed version", async () => withEvidence(async ({ root, receipt }) => {
  await assert.rejects(verifyLive(receipt, async () => Response.json({
    ready: true, bank_ready: true, cloudflare_version_id: "wrong-version"
  }), root), /Live cloudflare_version_id does not match/);
}));

