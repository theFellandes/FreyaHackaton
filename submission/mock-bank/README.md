# Bank Worker preparation

Status: **preparation only; no bank mock is implemented or deployed**. The hidden contract and test customers are pending. No endpoint, auth rule, outcome enum, identity threshold or fixture has been copied from the public practice bank.

The bootstrap returns HTTP 503 and ready:false for /__submission_health and every other request. The npm deploy script exits 2. Direct Wrangler commands can bypass that guard; do not use them until the real service is implemented.

## Available files

- wrangler.jsonc: module entrypoint, proposed Worker name and workers.dev routing. No account, storage resource or secrets were provisioned.
- src/index.mjs: not-ready bootstrap to replace after the actual kit arrives.
- tools/preflight.mjs: runtime/dependency/auth-variable presence, without secret values or authentication requests.
- tools/verify-ready.mjs: rejects incomplete evidence or changed artifacts, then checks live readiness and deployment/input hashes.
- deployment-receipt.schema.json: required evidence format. No real receipt exists yet.
- test/preparation.test.mjs: checks that preparation cannot be mistaken for a deployed bank.
- CONTRACT_CHECKLIST.md: work to complete from the actual release.

Wrangler **4.149.0** was verified against [official npm metadata](https://registry.npmjs.org/wrangler/latest) on 11 October 2026. It requires Node >=22. Available bundled Node is 24.19.0. The local pinned dependency is installed with pnpm and a folder-scoped .pnpm-store; pnpm-lock.yaml records exact resolutions. This does not authenticate or deploy.

See [Wrangler configuration](https://developers.cloudflare.com/workers/wrangler/configuration/) and [Worker commands](https://developers.cloudflare.com/workers/wrangler/commands/workers/) for the base configuration and deployment options.

## Local checks

From this directory, with Node on PATH:

    node --test
    node tools/preflight.mjs

When the real implementation is complete, configure Cloudflare credentials through the environment or supported Wrangler login. Never paste tokens into tracked files. Add actual storage bindings only after the contract determines their need. Replace the deployment guard with a reviewed command for the tested service; capture its entire output and record its deployment ID and URL.

## Evidence before a builder run

Create deployment-receipt.json only from an actual deployment. Paths are relative to the FreyaHackaton repository. Preserve the exact uploaded bundle and SHA-256, configuration, every deployed source file, original contract/customer files, and test reports. Record deployment ID, UTC time, HTTPS origin, persistence evidence and customer-isolation evidence.

The implemented /__submission_health must return HTTP 200 with ready:true, bank_ready:true, cloudflare_version_id, bundle_sha256, contract_sha256 and customer_fixture_sha256 matching the receipt. This metadata route is preparation infrastructure; it does not define the bank contract.

Immediately before building:

    node tools/verify-ready.mjs deployment-receipt.json --report ../runs/RUN_ID/bank-readiness.json

Exit 0 means artifact hashes and live metadata agree. It does not replace review of actual contract/persistence reports. Run real contract checks before the build too. The bootstrap cannot pass.

Set the builder input's service origin exactly as the hidden kit permits before the run. Preserve released files unchanged and record any permitted URL configuration separately. The builder derives tool URLs from its OpenAPI server: changing a practice harness environment variable alone does not redirect agent tools. Freeze deployment and fixtures once the submitted run is selected; keep the Worker and persistent state online until results are out.

## Version and hash metadata without circular changes

[Cloudflare version metadata](https://developers.cloudflare.com/workers/runtime-apis/bindings/version-metadata/) supplies env.CF_VERSION_METADATA.id at runtime. The configuration declares that binding. The receipt records cloudflare_version_id separately from deployment_id; the live probe verifies the version ID. Record the deployment ID from Cloudflare deployment output/API independently.

Do not hardcode the eventual Cloudflare ID or a bundle's own SHA-256 inside that bundle. First produce and retain the exact deployment bundle; hash it. Set BANK_BUNDLE_SHA256, BANK_CONTRACT_SHA256 and BANK_CUSTOMER_FIXTURE_SHA256 as public runtime variable bindings in the final deployment configuration, rather than substituting them into source. Hash the final configuration after those variables are set. Upload that exact bundle without rebundling (a reviewed --no-bundle deployment path), capture the created version and deployment IDs, and confirm live version/hash metadata. See [Wrangler bundling](https://developers.cloudflare.com/workers/wrangler/bundling/) and [runtime environment variables](https://developers.cloudflare.com/workers/configuration/environment-variables/).

The metadata helper src/readiness-metadata.mjs reads those bindings. It cannot mark a bank ready. The bootstrap remains 503 and false even if metadata bindings are populated. Versioned Worker code does not version external database contents: the fixture/state hashes and persistence evidence still matter.

The verifier report includes report_schema_version, bank_ready, deployment_id, cloudflare_version_id, base_url, health_url, bundle_sha256, contract_sha256, customer_fixture_sha256, verified_at_utc, duration_ms, receipt_path and receipt_bytes_sha256. It checks the receipt's original bytes again after the live request. An optional --report path is created exclusively inside submission; its parent must already exist. Keep receipt and report original bytes with the captured builder run.
