# Contract implementation checklist

Every item remains pending until supported by the actual hidden release. Read BRIEF.md first, then README.md, the service contract and test customers. Do not substitute the v3 public practice bank.

- [ ] Save the released ZIP and extracted files unchanged; record SHA-256.
- [ ] Identify every operation, request/response schema, method, authentication requirement and exact failure behavior.
- [ ] Identify how evaluation supplies customer IDs and maps customer/account/identity fixtures.
- [ ] Implement only the actual contract's state transitions, verification rules, terminal actions and ledger behavior.
- [ ] Confirm the account remains on Workers Free and every required storage resource has a free allowance. Do not upgrade or enable paid features.
- [ ] Choose persistent storage from required semantics. Prefer D1 or SQLite-backed Durable Objects for frequently updated call state; avoid defaulting to Workers KV, whose free allowance is 1,000 writes per day. Provision only needed bindings/resources. See [Workers/storage pricing](https://developers.cloudflare.com/workers/platform/pricing/) and [Durable Objects pricing](https://developers.cloudflare.com/durable-objects/platform/pricing/).
- [ ] Estimate writes per call and the expected evaluation volume. Run repeated functional tests locally, keep live verification small, and reserve daily storage/request quota for evaluation. Record the estimate and remaining allowance in deployment evidence.
- [ ] Verify persistence across independent requests/Worker instances and customer isolation; save evidence. Check concurrency where relevant.
- [ ] Test every operation and supplied customer. Cover malformed/missing fields, unknown customers, auth failures, enums, error shapes, state transitions, retries/duplicates, dates and terminal behavior as the contract requires.
- [ ] Keep policy decisions in the component the kit assigns them to; do not silently repair the agent's errors.
- [ ] Replace the 503 bootstrap and deployment guard after adapter, fixtures, authentication and storage are implemented.
- [ ] Save the exact deployed bundle/source/configuration hashes; deploy before the builder.
- [ ] Verify external HTTPS reachability and run contract tests against the deployed bank.
- [ ] Produce deployment-receipt.json from real deployment output and saved reports.
- [ ] Run receipt/live verification before each builder run; retain that evidence with the run.
- [ ] Ensure compiled agent URLs target the verified bank through a kit-permitted input/configuration step.
- [ ] After selection preserve agent/export/trace and freeze bank code/fixtures. Keep deployed bank and persistent records online through results.

Bootstrap tests do not complete deployment, contract testing, storage validation or builder readiness.
