import { createHash } from "node:crypto";
import { readFileSync, realpathSync, writeFileSync } from "node:fs";
import { dirname, isAbsolute, relative, resolve, sep } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const projectRoot = resolve(dirname(fileURLToPath(import.meta.url)), "../../..");

export function validateReceipt(receipt, root = projectRoot) {
  const fail = (message) => { throw new Error(message); };
  const nonempty = (value) => typeof value === "string" && value.trim().length > 0;
  if (!receipt || receipt.schema_version !== 1 || receipt.bank_ready !== true)
    fail("Receipt must explicitly confirm schema_version=1 and bank_ready=true.");
  for (const key of ["worker_name", "deployment_id", "cloudflare_version_id", "deployed_at_utc"])
    if (!nonempty(receipt[key])) fail("Missing " + key + ".");
  if (!receipt.deployed_at_utc.endsWith("Z") || Number.isNaN(Date.parse(receipt.deployed_at_utc)))
    fail("Deployment timestamp must be a UTC date-time.");
  const base = new URL(receipt.base_url);
  const health = new URL(receipt.health_url);
  if (base.protocol !== "https:" || health.protocol !== "https:" ||
      base.username || base.password || health.username || health.password ||
      health.origin !== base.origin || health.pathname !== "/__submission_health" ||
      health.search || health.hash)
    fail("Require HTTPS bank origin and its /__submission_health URL without credentials or query.");
  const checkArtifact = (artifact, label) => {
    if (!artifact || !nonempty(artifact.path) || isAbsolute(artifact.path) ||
        !/^[a-f0-9]{64}$/.test(artifact.sha256 ?? ""))
      fail("Invalid " + label + " path or SHA-256.");
    const absolute = resolve(root, artifact.path);
    const rel = relative(root, absolute);
    if (!rel || rel === ".." || rel.startsWith(".." + sep) || isAbsolute(rel))
      fail(label + " leaves the repository.");
    const real = realpathSync(absolute);
    const realRel = relative(realpathSync(root), real);
    if (realRel === ".." || realRel.startsWith(".." + sep) || isAbsolute(realRel))
      fail(label + " resolves outside the repository.");
    const sha256 = createHash("sha256").update(readFileSync(real)).digest("hex");
    if (sha256 !== artifact.sha256) fail(label + " hash differs from deployed evidence.");
  };
  for (const key of ["bundle", "configuration", "contract", "customer_fixtures"])
    checkArtifact(receipt[key], key);
  if (!Array.isArray(receipt.source_files) || !receipt.source_files.length)
    fail("Source files must be recorded.");
  for (const [index, source] of receipt.source_files.entries()) checkArtifact(source, "source_files[" + index + "]");
  if (!receipt.storage || !nonempty(receipt.storage.backend) ||
      receipt.storage.persistence_verified !== true || receipt.storage.customer_isolation_verified !== true)
    fail("Persistent storage and customer isolation must be tested.");
  checkArtifact(receipt.storage.evidence, "storage evidence");
  if (!receipt.contract_tests || receipt.contract_tests.passed !== true ||
      !Number.isInteger(receipt.contract_tests.case_count) || receipt.contract_tests.case_count < 1)
    fail("Live contract tests must pass.");
  checkArtifact(receipt.contract_tests.report, "contract test report");
  return health;
}

export async function verifyLive(receipt, fetchFn = fetch, root = projectRoot) {
  const health = validateReceipt(receipt, root);
  const response = await fetchFn(health, { cache: "no-store", signal: AbortSignal.timeout(15000) });
  if (!response.ok) throw new Error("Bank health HTTP " + response.status + ".");
  const data = await response.json();
  if (data.ready !== true || data.bank_ready !== true)
    throw new Error("Live bank explicitly reports not ready.");
  for (const [key, expected] of Object.entries({
    cloudflare_version_id: receipt.cloudflare_version_id,
    bundle_sha256: receipt.bundle.sha256,
    contract_sha256: receipt.contract.sha256,
    customer_fixture_sha256: receipt.customer_fixtures.sha256
  })) {
    if (data[key] !== expected) throw new Error("Live " + key + " does not match the receipt.");
  }
  return {
    bank_ready: true, deployment_id: receipt.deployment_id,
    cloudflare_version_id: receipt.cloudflare_version_id, base_url: receipt.base_url,
    health_url: health.href, bundle_sha256: receipt.bundle.sha256,
    contract_sha256: receipt.contract.sha256,
    customer_fixture_sha256: receipt.customer_fixtures.sha256
  };
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  try {
    const args = process.argv.slice(2);
    const receiptPath = args[0] || resolve(dirname(fileURLToPath(import.meta.url)), "../deployment-receipt.json");
    if (args.length !== 1 && args.length !== 0 && !(args.length === 3 && args[1] === "--report"))
      throw new Error("Usage: node tools/verify-ready.mjs RECEIPT [--report REPORT]");
    const receiptBytes = readFileSync(receiptPath);
    const receipt = JSON.parse(receiptBytes.toString("utf8"));
    const receiptSha256 = createHash("sha256").update(receiptBytes).digest("hex");
    const started = Date.now();
    const verified = await verifyLive(receipt);
    const afterSha256 = createHash("sha256").update(readFileSync(receiptPath)).digest("hex");
    if (afterSha256 !== receiptSha256) throw new Error("Receipt changed during verification.");
    const report = {
      report_schema_version: 1, ...verified,
      verified_at_utc: new Date().toISOString(), duration_ms: Date.now() - started,
      receipt_path: relative(projectRoot, resolve(receiptPath)).split(sep).join("/"),
      receipt_bytes_sha256: receiptSha256
    };
    if (args[2]) {
      const submissionRoot = realpathSync(resolve(projectRoot, "submission"));
      const reportPath = resolve(args[2]);
      const parent = realpathSync(dirname(reportPath));
      const rel = relative(submissionRoot, parent);
      if (rel === ".." || rel.startsWith(".." + sep) || isAbsolute(rel))
        throw new Error("Readiness report must stay within the submission directory.");
      writeFileSync(reportPath, JSON.stringify(report, null, 2) + "\n", { flag: "wx" });
    }
    console.log(JSON.stringify(report, null, 2));
  } catch (error) {
    console.error("Bank pre-build verification failed: " + error.message);
    process.exitCode = 2;
  }
}

