# Freya statement and debt mock

The released contract and all 34 synthetic customers are implemented. The local Node suite checks all 617 fixture responses through both supported HTTP prefixes, plus SQLite persistence and isolation. Workerd and live deployment must be verified separately before creating a deployment receipt.

Services accept POST JSON with a nested `data` object at `/voice/v1/<service>` or `/<service>`. The seven service names and fields come from [the released contract](../case/sources/documents/2026-10-01-service-contract.md). Fixture bodies, status codes, missing currency blocks, and the one 12-second delay are preserved. Customer numbers accept strings or integers. Card-only operations resolve the customer's exact full masked card key without requiring a customer number; last-four matching is never used. The test environment does not check bearer tokens, headers, or call metadata.

SQLite Durable Objects retain operation records and successful statement-week/PDF acknowledgments. Linked-card changes acknowledge both cards. Each repeated call is recorded separately; the mock does not impose agent consent, registered-email, or yearly-change policies. Fixture responses remain unchanged after recorded side effects, as required by the test contract.

For separate conversation records, provide `call.sessionId`, `call.callId`, `call.conversationId`, or an `x-session-id` header consistently on every service call. Without a stable identifier, records use `customer-default` for that customer. GET `/__submission_state?customerNo=90000001&sessionId=YOUR_ID` reports counts and acknowledgment metadata for known synthetic customers; it exposes no auth headers, call ANI, raw request bodies, or email addresses. There is no reset API.

GET `/__submission_health` probes SQLite and returns readiness with version/hash metadata. Missing or inaccessible storage returns 503. The deployment script remains blocked until the actual Workerd persistence/restart check is complete; deployment and receipt generation belong to the final submission procedure.

## Available files

- wrangler.jsonc: Worker entrypoint, workers.dev routing, SQLite `BANK_SESSIONS` binding, class migration, and observability.
- src/index.mjs: imports the unchanged released fixtures and exports the Worker and Durable Object class.
- src/bank-core.mjs: contract validation, routing, exact card ownership, fixture responses, and safe state metadata.
- src/bank-session.mjs and src/bank-store.mjs: Durable Object RPC and transactional SQLite audit state.
- src/fixture-engine.mjs: pure exact fixture lookup with independent returned bodies.
- tools/preflight.mjs: runtime/dependency/auth-variable presence, without secret values or authentication requests.
- tools/verify-ready.mjs: rejects incomplete evidence or changed artifacts, then checks live readiness and deployment/input hashes.
- deployment-receipt.schema.json: required evidence format. No real receipt exists yet.
- test/: complete fixture HTTP checks, SQLite rollback/reopen/isolation checks, and readiness/receipt guards.
- CONTRACT_CHECKLIST.md: work to complete from the actual release.

Wrangler **4.149.0** was verified against [official npm metadata](https://registry.npmjs.org/wrangler/latest) on 11 October 2026. It requires Node >=22. Available bundled Node is 24.19.0. The local pinned dependency is installed with pnpm and a folder-scoped .pnpm-store; pnpm-lock.yaml records exact resolutions. This does not authenticate or deploy.

See [Wrangler configuration](https://developers.cloudflare.com/workers/wrangler/configuration/) and [Worker commands](https://developers.cloudflare.com/workers/wrangler/commands/workers/) for the base configuration and deployment options.

## Local checks

From this directory, with Node on PATH:

    node --test
    node tools/preflight.mjs

Configure Cloudflare credentials through the environment or supported Wrangler login. Never paste tokens into tracked files. Before deployment, run Wrangler locally with an isolated `--persist-to` directory, record operations in two sessions and two customers, stop it, and restart with the identical directory. Confirm the records survived and remained isolated. Then replace the deployment guard with the reviewed command for the tested service; capture its output and record its deployment ID and URL.

## Evidence before a builder run

Create deployment-receipt.json only from an actual deployment. Paths are relative to the FreyaHackaton repository. Preserve the exact uploaded bundle and SHA-256, configuration, every deployed source file, original contract/customer files, and test reports. Record deployment ID, UTC time, HTTPS origin, persistence evidence and customer-isolation evidence.

The deployed /__submission_health must return HTTP 200 with ready:true, bank_ready:true, cloudflare_version_id, bundle_sha256, contract_sha256 and customer_fixture_sha256 matching the receipt. This metadata route is submission infrastructure; it does not define the bank contract.

Immediately before building:

    node tools/verify-ready.mjs deployment-receipt.json --report ../runs/RUN_ID/bank-readiness.json

Exit 0 means artifact hashes and live metadata agree. It does not replace review of actual contract/persistence reports. Run real contract checks before the build too.

Set the builder input's service origin exactly as the hidden kit permits before the run. Preserve released files unchanged and record any permitted URL configuration separately. The builder derives tool URLs from its OpenAPI server: changing a practice harness environment variable alone does not redirect agent tools. Freeze deployment and fixtures once the submitted run is selected; keep the Worker and persistent state online until results are out.

## Version and hash metadata without circular changes

[Cloudflare version metadata](https://developers.cloudflare.com/workers/runtime-apis/bindings/version-metadata/) supplies env.CF_VERSION_METADATA.id at runtime. The configuration declares that binding. The receipt records cloudflare_version_id separately from deployment_id; the live probe verifies the version ID. Record the deployment ID from Cloudflare deployment output/API independently.

Do not hardcode the eventual Cloudflare ID or a bundle's own SHA-256 inside that bundle. First produce and retain the exact deployment bundle; hash it. Set BANK_BUNDLE_SHA256, BANK_CONTRACT_SHA256 and BANK_CUSTOMER_FIXTURE_SHA256 as public runtime variable bindings in the final deployment configuration, rather than substituting them into source. Hash the final configuration after those variables are set. Upload that exact bundle without rebundling (a reviewed --no-bundle deployment path), capture the created version and deployment IDs, and confirm live version/hash metadata. See [Wrangler bundling](https://developers.cloudflare.com/workers/wrangler/bundling/) and [runtime environment variables](https://developers.cloudflare.com/workers/configuration/environment-variables/).

The metadata helper src/readiness-metadata.mjs reads those bindings. Metadata alone cannot mark the bank ready: the health route also requires a successful SQLite probe. Versioned Worker code does not version external database contents: the fixture/state hashes and persistence evidence still matter.

The verifier report includes report_schema_version, bank_ready, deployment_id, cloudflare_version_id, base_url, health_url, bundle_sha256, contract_sha256, customer_fixture_sha256, verified_at_utc, duration_ms, receipt_path and receipt_bytes_sha256. It checks the receipt's original bytes again after the live request. An optional --report path is created exclusively inside submission; its parent must already exist. Keep receipt and report original bytes with the captured builder run.
