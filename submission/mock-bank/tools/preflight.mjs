import { existsSync, readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const major = Number(process.versions.node.split(".")[0]);
const result = {
  phase: "implementation_local_preflight",
  bank_ready: false,
  node_version: process.versions.node,
  node_supported: major >= 22,
  wrangler_installed: existsSync(resolve(root, "node_modules/wrangler/package.json")),
  pinned_wrangler: JSON.parse(readFileSync(resolve(root, "package.json"), "utf8")).devDependencies.wrangler,
  cloudflare_api_token_present: Boolean(process.env.CLOUDFLARE_API_TOKEN),
  cloudflare_account_id_present: Boolean(process.env.CLOUDFLARE_ACCOUNT_ID),
  deployment_receipt_present: existsSync(resolve(root, "deployment-receipt.json")),
  note: "This preflight only reads local runtime/dependency state and environment presence; it makes no authentication or deployment request."
};
console.log(JSON.stringify(result, null, 2));
process.exitCode = result.node_supported ? 0 : 2;
