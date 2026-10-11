# Competitive edge and internal logging

Reviewed on 10 October 2026. **Status, 11 October:** the two probe findings below (calendar validity beyond the enumerated dates, F02, and the decoy success boolean, F01/AUD-04) and the credential in build-result payloads (F11) are fixed; current status is in [FIXING_ISSUES.md](FIXING_ISSUES.md). This document distinguishes inspected functionality from proposed work. Source files are changing during the review; a local test pass is not evidence of a completed live customer evaluation or a running logging backend.

## What would give this entry an edge

The builder already has useful ingredients: extracted policy, generated workflows, deterministic hooks, repair checks and detailed traces. We cannot establish an advantage over other teams without their results. The measurable advantage we should pursue is consistent behavior under unfamiliar briefs, schemas and combinations of customer behavior.

Recent schema-role and consistency work is a concrete improvement. The [API-variant tests](../tests/unit/test_compile_api_variants.py) cover renamed request/response fields, identity hints, customer/product IDs and enum spelling. The [consistency tests](../tests/unit/test_compile_consistency.py) address generated retry instructions, deferred multi-product notes, hardship routing and rule-map locations. All **69 checks passed** in this review. Some assertions inspect generated text or isolated hooks; they do not establish full-call reliability.

The [organizer contract](../KIT_README.md) scores bank records, policy compliance and honesty. Each customer must pass every repetition. A correct refusal or transfer can be successful; collecting more promises is not a substitute for passing the case. A dashboard or another deployment channel does not establish that reliability.

These are proposed acceptance scenarios, not claims that the current builder passes them:

| Scenario | Evidence needed |
| --- | --- |
| Customer corrects a birthday halfway through an answer: “May 12—sorry, May 21.” | Only the final complete answer is verified; incomplete or impossible dates are handled according to the brief; the bank remains the authority for a plausible but wrong birthday. No debt disclosure before verification. |
| Customer changes the amount or date after an earlier “yes.” | The write and final readback match the newly confirmed terms. Old consent cannot silently authorize a different agreement. |
| Recording succeeds, then the customer interrupts, repeats the agreement or asks whether it was recorded. | Exactly the authorized bank records remain. Repetition does not duplicate a write or erase a successful one. |
| A bank response explicitly rejects a write, versus a connection timing out after dispatch. | Rejection and unknown outcome are distinguished. The agent cannot claim success without evidence or blindly retry a possibly successful write. Use only reconciliation mechanisms supplied by the bank contract. |
| Three products: one already paid, one disputed, one with a valid promise; a transfer is subsequently requested. | Per-product state and the brief's call-wide rules both hold. The closing outcome, readback and notes match the actual acknowledged records. |
| The same brief meaning arrives with renamed API fields, different enums, opaque mixed-case IDs or amended rules. | Valid schema changes preserve intended behavior; unsupported contracts produce an explicit build failure instead of an apparently usable agent. Policy values come from the new input. |
| The same facts arrive tersely, with clarification questions or with several corrections, on different calendar days. | The bank outcome remains correct without repeating inappropriate offers. Retry counters, holidays and relative dates follow that particular brief. |
| Repetition one fails and the final repetition passes. | The overall case stays failed; its earlier failing call is retained and inspected. Current triage now supports this. |

An edge-case list is only useful when each case has an independently specified expected bank state and conversation assertions. Do not construct the expected result by calling the same helper being tested. Do not impose a universal rule such as “clarification never consumes an attempt”: the organizer briefs contain specific counting rules.

Two additional offline probes found gaps beyond those passing tests. The mandatory [promise pre-hook](../builder/compiler/hooks.py) admits dates later than its enumerated list when they are lexically within `until_<product>`; that branch checks ISO shape but not actual calendar validity or business days. With a long deadline, probes left both a Sunday (`2027-10-17`) and a nonexistent day (`2028-02-30`) enabled. An optional generated date helper is not a substitute for enforcing the check at the write boundary. Separately, [success-field selection](../builder/compiler/roles.py) chooses the first backticked boolean in response prose: naming `audit_logged` before `ok` can cause it to treat the audit flag as the action's acknowledgment. These were isolated source/probe findings, not demonstrated live bank writes. Both are fixed, with long-window and decoy-field regressions (`tests/unit/test_codegen_promise_dates.py`, `tests/unit/test_hidden_tool_contracts.py`).

## Logging we have today

| Capability | Inspected status |
| --- | --- |
| Local human-readable and JSONL builder traces | Implemented: run, stages, model/purpose, platform actions and test events. |
| Local detailed model-attempt log | Implemented: redacted prompt/reply, model, purpose, attempt, token usage and latency in `.llm.jsonl`. |
| Agentic session log | Implemented as a separate `.agentic.jsonl` path. |
| Freya and practice-bank transport monitoring | Implemented in project wrappers. Wrapper requests and engine tool calls are different evidence; standalone kit traffic is outside this monitor's scope. |
| Call inspection | Implemented: fetch routes, tool results and variable snapshots from Freya. |
| Self-hosted logging backend | Langfuse integration and [Docker configuration](../docker-compose.langfuse.yml) exist. Running deployment and successful delivery were not verified in this review. |
| Full durable playground conversation archive | Not established: messages and fetched call/ledger details are kept in session state; conversation download is manual, and trace chat text is clipped. |
| Automatic replay from saved model logs | Not found. Fixture-driven offline execution exists and is a different capability. |

See [Observability](OBSERVABILITY.md) and the [observer factory](../builder/observability/factory.py). Local logs already provide internal storage without operating a separate service. A query index or self-hosted UI can help investigation, but neither is a hackathon submission requirement.

## The next useful evidence bundle

Proposed: one manifest should bind the source revision and local changes, full brief/API hashes, build run ID, workflow ID and version, exported file/hash, trace paths and strict evaluation report. For each case, retain its repetition, style, call date, call ID, redacted transcript, bank ledger and first observed failure. Record unavailable evidence explicitly instead of filling it with inferred success.

This makes it possible to distinguish an extraction error, incorrect policy guard, wrong conversation decision, rejected bank write, unknown transport outcome and an evaluation infrastructure failure. Existing IDs are a starting point; a complete manifest tying all these artifacts together was not found. Correlated operation IDs and events follow the principles described in [OpenTelemetry's trace documentation](https://opentelemetry.io/docs/concepts/signals/traces/).

Build-side collection must stay outside the phone agent's extra tools. The [rules](../KIT_README.md) allow that agent to call only the supplied bank API. Adding a call from the phone agent to our own logging server would break that restriction. Store or collect permitted platform evidence from the builder/test runner instead.

## Limits to address before trusting the logs

`OBSERVABILITY_LOG_PROMPTS=false` affects Langfuse only. Set `LLM_LOG_CONTENT=metadata` separately to omit prompt and reply text from the local model log; the default `full` retains them. Metadata mode still includes error strings and is not general customer-data scrubbing. Credential redaction does not automatically remove arbitrary personal or confidential data. Fresh synthetic checking on 10 October also found a credential retained in a build-result payload despite trace/CLI masking; the result file is now masked too (F11). Ordinary exception logs and conversation downloads need consistent redaction review. [OWASP's logging guidance](https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html) recommends excluding or protecting secrets and sensitive data and restricting log access. See [Observability](OBSERVABILITY.md) for the independent switches.

Trace/observer exceptions are deliberately nonfatal. Therefore a successful build must still undergo a submission check for complete required traces. Flush/close improves persistence but does not guarantee recovery of an unfinished event or survival of every storage failure. A self-hosted server also needs separate delivery, access and retention verification.

## Review validation and priority

The selected observability, trace and evaluation-triage tests produced **78 passes and two optional SDK skips**. One async context test stalled in this environment and was excluded from the successful run; it remains unverified here. The initial test attempt also encountered a temporary-directory setup error, resolved by using a new workspace-local test directory. These checks do not verify a live Langfuse server or actual phone-agent bank actions.

The additional API-variant/consistency run produced **69 passes**. Both successful commands used `.venv-playground`, disabled plugin autoload/cache output and used fresh workspace-local temporary directories. They reported an unknown `asyncio_mode` configuration warning because the successful selections did not load the async plugin.

Priority: first complete a matching automatic build/export/trace/strict evaluation; then close write, confirmation and per-product state gaps with the scenarios above; then add the correlated failure bundle and uniform redaction. Keep backend hosting optional unless local evidence becomes difficult to inspect. Nothing in this document promises a winning score or exhaustive coverage.
