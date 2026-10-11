# Quality review closure report

Date: 11 October 2026. Scope: the Python LangGraph builder in this repository. Written by the closure pass that re-ran every item of the external audit, the backlog F01-F22, the previous review and the G1-G3 round. No Freya call was made, no git command changed state, and no source, test or doc file was edited; the only file written for this report is this one.

**Submission-preparation follow-up:** the closure figures below remain the producer's dated results. An independent check after completion passed 2,690 tests with three optional-package skips in the separate Python 3.12 environment, with clean lint and 236 unchanged source/configuration hashes. Fresh probes also found a decoy numeric identity-counter mapping and the lone-provider circuit/cooldown corner. [Preparation verification](../submission/VALIDATION.md) records these limits. The public-score wording and acceptance commands below have been clarified so three B amendment cases cannot be read as complete A+B coverage.

## Summary

- **Offline state is green.** The full suite passes: 2,693 tests, 0 failed, 0 errors, 0 skipped (junit report of the run). With `streamlit`, `pypdf`, `claude_agent_sdk` and `langfuse` hidden it runs 2,676 tests, 0 failed, 7 skipped. `ruff check builder tests evals` reports no problem.
- **151 rows checked: 113 closed, 29 open, 6 user-action, 3 not-a-defect.** All 11 audit claims were re-run: 8 closed, claim 1 is yours (git/venv), claims 9 and 11 are fixed in their main part with a low residual. The open rows that can still cost customer success are F06, F07, F14, F16, F21 and C-16; the rest are low-risk residuals. All are discussed under 'Open risks'.
- **Live evidence exists but is older than the final source.** `results/acceptance/summary.txt`: Brief A 8 of 10 customers (3 of 3 repetitions each), A+B 3 of 3 B amendment cases. The inherited ten A cases were not tested on A+B; these are two different agents. The builds started at 22:22 and 22:32 UTC on 10 Oct and scoring ended by 22:40 UTC, before the G1-G3 changes (the compiled workflow had 35 nodes, today's code compiles 36). This closure did not run Freya.
- **Three new findings** came out of the closure: a test that writes into `results/` (N-1), product-id matching without exact-match precedence (N-2), and the tracked `.freya-agents.json` that holds your staging workflow ids (N-3).
- **The task's trace-log warning is out of date:** `.gitignore` now re-includes `!traces/*.log`, so the human-readable traces are no longer excluded (verified with `git check-ignore -v` and `git status`).

## What was re-run

| Check | Result |
|---|---|
| `pytest tests` (junit XML, `.venv`, Python 3.13) | 2,693 passed, 0 failed, 0 errors, 0 skipped, 141 s |
| Same suite, four optional packages hidden (`-p tests.hide_optional_packages`) | 2,676 run, 0 failed, 7 skipped |
| `pytest tests -q` exactly as given | exit 0, dots only: the repo's `addopts` already holds `-q`, so a second `-q` hides the summary line. Use `pytest tests` or `--junitxml` for the count |
| `ruff check builder tests evals` | All checks passed |
| Reproduction scripts of the review (`.cache/quality-review/*`), offline ones only | 16 pipeline, 19 compiler, 8 understand, 4 repo scripts re-run; 10 are obsolete in whole or in part (see below). Outputs: `.cache/quality-review/closure/repro/` (git-ignored) |
| New probes written for this closure | P-04/P-05 deadline and retry, A7b/C-17/C-16 hooks, F06/F07 identity, F03 ids, in-memory compile of both fixtures |
| In-memory compile of the kit fixtures (staged) | A: 36 nodes, lint 0 errors 0 warnings, 101 of 101 rules mapped, negotiate prompt 5,954 of 6,000 characters. A+B: 33 nodes, 1 L018 warning (8,559 characters), 119 of 119 mapped. Two compiles give identical workflows; every node gets a layout position |
| Secret scan of the final tree (location-only script `.cache/quality-review/repo/secret_scan.py`, values never printed) | 14,446 text files scanned; 0 hits for the values of the five secret variables in `.env`; 146 pattern hits, all placeholders or fake tokens of test fixtures, caches and the Langfuse compose file |
| Taken from the integration step, not re-run here | dry runs through the CLI, container runs with `--network none`, reference regeneration (byte-identical), kit-file integrity, `docker build` |

Ten of the scripts are obsolete in whole or in part because they assert the old, defective behaviour (the regression tests replace them): `repro_claim3_4_export_publish` crashes on `exported: None` (the fix); `audit_repro`, `repro_7b` and `repro_case` raise `KeyError: 'notes_log'` because the note pre-hook no longer logs before the bank answers (the fix; a patched copy of audit_repro was used); `repro_guard_repair` stops at its own 'model called' stub because a guard block now reaches the model (the fix); `repro_403_model_dead_key` finds no health file (the fix); `repro_claim8` and part 1 of `repro_no_deadline_and_smoke_no_remediation` predate the in-flight grace and the constructor deadline; `fuzz_calendar` and `gaps.py` case G2 encode the old fail-open and old promise state. Scripts that still show a real, open problem: `repro_claim11_redaction` (A11), `repro_no_deadline...` part 2 (P-17), `repro_slot_collision` (P-13), `repro_redeploy_rejected_local_file` (P-15), `repro_state_file` (P-18), `repro_selftest_labels` (P-11), `gaps.py` case G1 (C-16).

### Live evidence (`results/acceptance/summary.txt`, not re-run)

| Brief | Build | Result |
|---|---|---|
| A | exit 0, 99 s, workflow `58bd49f0...`, agent `826cdf9b...`, version 1, published, export_ok true, own checks 3 of 3 | 8 of 10 customers pass all 3 repetitions. Per run: 10/10, 10/10, 8/10. Misses: PUB-03 2/3 (identity check did not end after the third wrong answer), PUB-07 2/3 (2 November recorded where 27 October was required) |
| A+B | exit 0, 135 s, workflow `b5638c83...`, agent `4ae00ca3...`, version 1, published, export_ok true, own checks 3 of 3 | PUB-11, PUB-12, PUB-13: 3 of 3 customers, 3 of 3 repetitions |

The live traces show the kit's plain `save` with one attempt: the new create/update path (editor positions), the bounded transport and the write retries have never run against the real platform.

## Closure table

Status values: **closed** (fixed, regression test or reproduction no longer fails), **open**, **not-a-defect**, **user-action** (only you can do it). 'Not live' means verified offline only.

### 1. External audit claims 1-11 and the extra risks

16 rows: 11 closed, 4 open, 1 user-action, 0 not-a-defect.

| id | claim | status | evidence / test |
|---|---|---|---|
| A1 | Git: HEAD lacks the project; clean install not established | user-action | HEAD is now 6d75847 'V1 of the project' (560 tracked files) but 86 modified and 57 untracked entries are not committed (read-only `git status`). A clean checkout of HEAD cannot run: builder/build.py imports builder/_startup.py, builder/freya/client.py imports builder/compiler/layout.py and builder/freya/transport.py, and all three are untracked (so is requirements.txt). Clean-install routes were verified by the review (docker build exit 0, offline dry runs in the image). `.venv/pyvenv.cfg` still points to a uv-managed CPython 3.13 under AppData\Roaming: recreate it. |
| A2 | Tool-API field variation: renamed close_call fields, renamed success flag | closed | Re-ran audit_repro. Before: lint L012 errors and `{success:true}` read as 'promise NOT recorded'. After: close body uses `result_code`/`reason_code` with 0 lint errors and the post-hook says 'The promise IS recorded'. api_variants.py: renamed close fields, success flags, product id, customer id and queue names all compile; the one ambiguous contract (two verification flags) is a CompileError. `test_compile_api_variants.py` 67 tests pass. |
| A3 | Export file older than the published version; export failure replaced by the local file | closed | tests/graph/test_deploy.py (P-01, 7 tests). The original repro now ends with `exported: None` (it crashes on the None), so the substitution is gone. Live builds A and B: export_ok true. |
| A4 | Unconfirmed publication reported as deployed | closed | repro_claim4_summary_text now prints status `deployed_unpublished`; tests/graph/test_deploy.py (P-02, 7 tests). |
| A5 | Date guard: empty allowed list fails open; enumeration stops at 120 days | closed | audit_repro before/after: empty list now `blocked: no promise date is left for p1`; 2027-03-15 inside a 180-day window now `ok` (was blocked). repro_5 (5a/5b/5c) matches the policy engine. C-06/C-07 tests (20) and the differential test in test_codegen_promise_dates.py pass. |
| A6 | Case-sensitive ids lowercased (ABC123 rejected) | closed | audit_repro: body keeps `ABC123`, guard `ok` (before: `abc123`, blocked). C-08 tests (10). Edge case that remains: N-2. |
| A7a | retries_on_failure=0 still says 'call it once more' | closed | retries.py: 0 gives 'do not try again' in prompt rule 9 and in the post-hook; 1, 2, 3 give matching texts. test_compile_consistency.py (C-09). |
| A7b | A failed add_note is logged before the response and treated as recorded | closed | Probe on the compiled hooks: notes_log is '' after the pre-hook and after a failed reply, `p1:refused;` only after an acknowledged reply. 20 tests in test_compiled_note_acknowledgment.py. |
| A8 | Wall-clock guard does not stop the platform worker thread | closed | tests/graph/test_wall_clock_guard.py::test_the_guard_does_not_wait_for_a_blocked_platform_call (6 s block, run_build back in under 3 s). The original repro (default 15 s in-flight grace for a deploy) returns after 4.1 s for a 4 s call, which is the bounded design. The kit's 120 s urlopen timeout is capped by builder/freya/transport.py (fake and loopback only, not live). |
| A9 | '100% coverage' counts declarations; walkthrough passes with checked=0 | open | Walkthrough half closed: 0 examined gives passed=false 'inconclusive', unknown rule ids are traced (7 tests). Coverage half unchanged: the label still reads '101 of 101 active rules enforced (100%)' and a junk location still counts (repro_claim9 a2). Mitigations: 0 dangling locations in 4 compiles (rulemap_check) and the auditor sees the map as an unverified claim. Residual P-06, F09. |
| A10 | evals/triage.py inspects only the last repetition | closed | REPO-05 test plus tests/unit/test_eval_triage.py (15 tests). |
| A11 | Secrets masked in trace files but not in logging, error strings, result JSON | open | Printed final JSON line is now masked (build._summary_line, test_the_summary_line_never_carries_a_secret). repro_claim11 still shows a synthetic secret inside an error text in results/<slot>-<run>.build.json and in Python log output (stdout line and trace files are clean in real runs). Residual P-08, F11. |
| A-x1 | Agentic fallback does not forward an OAuth token that only .env knows | closed | repro_oauth_forward: token forwarded to the child env = True; test_a_login_session_gets_the_oauth_token_that_only_the_env_file_knows. |
| A-x2 | Negotiation instructions conflict (immediate add_note vs 'no notes on a transfer'; service-error note vs close) | closed | conflicts.py: notes are deferred 'write it only at the very end', B7 is consistent. C-11/C-12 tests (9 each). |
| A-x3 | Confirmation and duplicate prevention rely on model compliance | open | Duplicates are now blocked by the hook (F17, 40 tests). Binding a confirmation to the pending proposal is not built (F16). |
| A-x4 | Practice self-test is 1 repetition, default style; offline practice cases inject counters | open | Label still reads 'brief A: N/N practice cases passed' with no repetition note (P-11). The offline counter injection is a documented test technique (not a defect). Live 3-repetition score of the same kind of agent: 8/10. |

### 2. Backlog items F01-F22 (docs/FIXING_ISSUES.md)

22 rows: 11 closed, 9 open, 2 user-action, 0 not-a-defect.

| id | claim | status | evidence / test |
|---|---|---|---|
| F01 | Resolve tool field roles from the contract | closed | tests/unit/test_compile_api_variants.py 67 tests (staged and lean, decoy `audit_logged`, opaque names). Live interpretation of an unfamiliar brief is still an acceptance check. |
| F02 | Fail safely when allowed promise dates are empty | closed | test_codegen_promise_dates.py + test_codegen_calendar_parity.py 64 tests (empty list, 2028-02-30, Sunday beyond the horizon, leap day, 180-day deadline). |
| F03 | Preserve product identifiers exactly | open | Spelling is preserved (A6). Exit check 'similar ids are not merged' fails for ids that differ only by case or punctuation: N-2 (probe: `AB-1` is sent as `Ab-1`). |
| F04 | Update recorded state only after acknowledgment | closed | 28 tests (note acknowledgment, duplicate post-hook, stale arguments, all-paid outcome) plus the probe in A7b. |
| F05 | Published version and matching export | closed | tests/graph/test_deploy.py 17 tests. Live: A and B builds published v1, export_ok true. Predates the later source changes. |
| F06 | Identity mismatch vs unreadable input vs service failure | open | Probe on the compiled verify_identity hooks: '4401, sorry, 4410' is sent as 44014410, '31/02/1990' as 1990-02-31, '12' as '12'; empty data, an error envelope and an unknown shape all produce 'This answer is wrong' (never unlocks, but is mislabeled). Shape guessing at compile time is fixed (C-05). |
| F07 | Honor retry and refusal limits from policy | open | Retries: closed (C-09). identity.decline_max is still hard-coded: decline_max 1, 2 and 3 compile to the same 'A second refusal ...' line. Distinct counters not verified. |
| F08 | Wall-clock limit applies to active operations | closed | 46 tests (test_wall_clock_guard, test_freya_transport, test_freya_background, test_budget); P-04 probe. Residual: the practice node takes the simulation slot without a timeout (selftest/practice.py:58). |
| F09 | Meaningful walkthrough evidence | open | Zero-checked and few-checked audits no longer pass (7 tests). The auditor still receives a condensed workflow text without hook or code bodies (MAX_WORKFLOW_CHARS 48 000) and no chunking. |
| F10 | Failures hidden by the final repetition | closed | tests/unit/test_eval_triage.py 15 tests. |
| F11 | Error redaction gaps | open | Same as A11: stdout line closed, results JSON and Python log output open (low). 80 redaction tests pass. |
| F12 | Agentic credential forwarding | closed | A-x1 repro and test; runner and provider both forward the token. |
| F13 | Reproducible test environment | user-action | Code side closed: builder/_startup.py refuses unsupported Pythons with install commands (python3 3.14.6 here exits 2), requirements.txt pinned to the lock (28 tests), suite green with all four optional packages hidden (2676 run, 0 failed). The main .venv itself still points at the AppData uv Python. |
| F14 | Repeated automatic-builder customer acceptance | open | Live (builds at 22:22-22:33 UTC on 10 Oct, source before G1-G3): Brief A 8/10 customers over 3 repetitions (PUB-03 2/3, PUB-07 2/3), A+B 3/3. Not re-run on the final source. |
| F15 | Packaging and deployment environment | user-action | Commit not made (see A1): the runtime files _startup.py, layout.py, transport.py and requirements.txt are not in HEAD. Docker build and offline dry runs passed in the integration step (working tree). |
| F16 | Bind confirmation to the exact pending proposal | open | No confirmation state exists in builder/compiler/hooks.py; readback/agreement is prompt text only. |
| F17 | One immutable acknowledged promise per product | closed | 40 tests; probe: first create_promise sent, second for the same product blocked ('already recorded'), both profiles' hooks. Remaining concerns listed in the doc (canonical-id collisions N-2, platform counters). |
| F18 | Support clarification vs declining support | closed | tests/unit/test_compile_offer_branches.py 16 tests (accept, decline, clarification, wait branches, both profiles). Live effect not measured. |
| F19 | Incomplete persuasion ladders, offer history | closed | tests/unit/test_codegen_persuasion_ladder.py 11 tests (no padding, caps, band filters, compile note). Conversation acceptance open. |
| F20 | Real audio interruption acceptance | open | Needs real-call evidence; nothing in the offline suite can close it. |
| F21 | Bind date corrections to the customer's original proposal | open | PUB-07 2/3 live (2 November recorded where 27 October was required). The hook validates the submitted date, not that it is the required correction. |
| F22 | Model-fallback regressions (7 gateway tests) | closed | tests/unit/test_llm_gateway.py and test_llm_errors.py pass in the full run (REPO-11, REPO-14). |

### 3. Previous review, compiler area

22 rows: 20 closed, 2 open, 0 user-action, 0 not-a-defect.

| id | claim | status | evidence / test |
|---|---|---|---|
| C-01 | close_call outcome/reason names hard-coded | closed | Renamed fields compile, 0 lint errors (audit_repro, api_variants); 5 tests. |
| C-02 | Response flag `ok` hard-coded in hooks | closed | {success:true} reads as recorded; decoy boolean cannot override; 21 tests. |
| C-03 | get_account item keys and request fields hard-coded | closed | Keys come from the ToolApi; repro_ids now gives product_ids '' instead of a fabricated 'p1'; 5 tests. |
| C-04 | create_promise roles by name heuristics | closed | api_variants (product_code, loan_id, account_number, item, extra optional field): guard present; 6 tests. |
| C-05 | verify_identity response shape guessed | closed | Renamed counters/flags handled, ambiguous contract is a CompileError; 18 tests. |
| C-06 | Promise guard fails open when allowed list is empty | closed | audit_repro, repro_5 5a: blocked; 5 tests. |
| C-07 | Allowed dates capped at 120/45 days | closed | repro_5 5b/5c, repro_audit5 (180-day deadline, 126 dates); 15 tests. |
| C-08 | Product ids and enum values lower-cased | closed | audit_repro: ABC123 kept; mixed-case enums keep the contract spelling; 10 tests. Edge: N-2. |
| C-09 | retries_on_failure ignored by the template | closed | retries.py 0..3; test_compile_consistency.py. |
| C-10 | Failed add_note appended before the HTTP call | closed | A7b probe; 20 tests. |
| C-11 | Immediate add_note vs 'no notes on any transfer' | closed | Notes deferred to the end by prompt and tool description (9 tests). The engine flush after a transfer decision was not built: a model that forgets the final add_note leaves no note (decide falls back to the chosen situation). |
| C-12 | service_error note vs immediate close | closed | conflicts.py conflict 2 reads consistently; 9 tests. |
| C-13 | Two transitions to one target in third_party | closed | dup_targets: [] for A and AB, staged and lean; lint L028; 2 tests. |
| C-14 | PUB-03 root cause: complete answer withheld | closed | Compiled identity step now says a complete answer is checked at once, also inside a cut-off message (hidden_like output; 6 tests). Needs a live PUB-03 run with repetitions. |
| C-15 | Hardship precedence only as prompt text | closed | Latch/guard nodes in the staged profile (12 tests). Lint rule for 'every overridden exit is guarded' not built; lean profile keeps prompt-only precedence. Needs a live PUB-05 run. |
| C-16 | No deterministic guard on schedule_callback | open | gaps.py G1 and probe: a callback on 2026-10-20 at 22:30 (window 10:00-19:00) is sent in both fixtures; the schedule_callback pre-hook only coerces types. The window is stated in the prompt only. |
| C-17 | No one-promise-per-product enforcement | closed | Probe: second request for an acknowledged product blocked; 3 + 40 tests. |
| C-18 | Engine transfer maxRetries 0 vs close_call 2 / lean 1 | open | Unchanged (staged.py:521 and :961 use 2, toolset.py:391 uses 1). No behavioural failure was ever shown; consistency issue only (low). |
| C-19 | Empty phrases rendered as empty-quote instructions | closed | blank_phrases.py now raises a CompileError for a missing required opening; 3 tests. |
| C-20 | soften_recording_notice grammar | closed | 15 tests in test_compiler_lessons.py. |
| C-21 | Lint blind to hard-coded names and duplicate targets | closed | Lint rules L028 and L029; 4 tests. |
| C-22 | rule_map credits mechanisms that do not exist | closed | rulemap_check: 0 dangling locations in A/AB staged/lean; 6 tests. |

### 4. Previous review, pipeline area

22 rows: 12 closed, 9 open, 0 user-action, 1 not-a-defect.

| id | claim | status | evidence / test |
|---|---|---|---|
| P-01 | Export older than published; export failure substituted | closed | A3. |
| P-02 | Unconfirmed publication reported as deployed | closed | A4. Not done: comparing practice agentVersionNumber with the deployed version (optional). |
| P-03 | Wall-clock guard vs worker thread | closed | A8; 33 transport/background/guard tests. Live not verified. |
| P-04 | No absolute deadline reaches Freya calls | closed | Probe: client with a past deadline raises PlatformCancelled, 0 platform calls, no back-off sleep; 6 tests (budget, background, graph run). |
| P-05 | One transient 503 fails the whole deploy; no write retried | closed | Probe: 503 on create is retried (back-off 2 s), exactly 1 workflow exists; 17 client tests. Residual: a lost create response that the name search cannot reconcile may leave one stray workflow. Not live-verified. |
| P-06 | 'N of N rules enforced (100%)' counts any non-empty string | open | A9: label unchanged (low). |
| P-07 | Walkthrough passes with checked=0 | closed | repro_claim9 b1/b2 give passed=false 'inconclusive'; 7 tests. Chunking and per-rule verdicts not built (F09). |
| P-08 | Secrets not redacted in final JSON, results JSON, log output | open | A11: stdout line closed, results JSON and logging open (low). |
| P-09 | After the guard fires, self-tests that ran are dropped | closed | repro_guard_partial_summary lists all 3 passed tests in JSON and in the BUILD SUMMARY block; 9 tests. |
| P-10 | 'All self-tests passed' for a round in which none ran | closed | repro_selftest_labels: 'no self-test ran for the redeployed version'; 3 + 7 tests. No rollback is attempted: the live version stays and is flagged `untested_redeploy`/`tested_version`. |
| P-11 | Practice result labelled without repetitions/style | open | repro_selftest_labels: label 'brief A: 10/10 practice cases passed', runner uses reps=1, default style (low). |
| P-12 | Smoke summary claims checks that did not run | closed | 3 tests (empty policy, reachability only). |
| P-13 | Same brief file name gives the same slot (overwrite) | open | repro_slot_collision: slot 'brief' for both, second build overwrites the first workflow. Slot is the kit's label; use --slot when file names repeat. |
| P-14 | 'exported' filled with files never exported | closed | repro_failed_build_exported: exported null for a failed build; only a dry run reports a local file; 3 tests. |
| P-15 | workflow_file points at the rejected definition after a rejected re-deploy | open | repro_redeploy_rejected_local_file: final.workflow_file holds the rejected v2 while exported/deployed is v1 (low, cosmetic). |
| P-16 | remote_validate reported as passed though no dry run happened | closed | collect_tests lists it only for ok/deployed/invalid, not skipped/infra; test_tests_for_the_summary. After an infra error plus a successful deploy it is reported as accepted, which is true. |
| P-17 | Failed smoke test triggers no remediation | open | repro: decide with a failed smoke and 1679 s left gives repair_requested false. |
| P-18 | Empty or corrupt .freya-agents.json aborts the deploy | open | repro_state_file: FreyaError 'state file ... is unreadable; fix or remove it' (clear message, no recovery) (low). |
| P-19 | No FREYA_API_KEY silently becomes a dry run, exit 0 | not-a-defect | By design and tested: status `dry_run`, no deploy, trace error line 'FREYA_API_KEY is not set'. The final JSON `errors` stays empty. |
| P-20 | Invalid settings or unwritable trace dir escape main() | closed | repro_trace_dir_unwritable: exit 2 with the JSON line; MAX_POLICY_REPAIRS=notanumber: exit 2, JSON line and a trace; 5 tests. |
| P-21 | Trace 'tools' count includes code_exec | open | count_tools still counts pre_action function names (cosmetic). |
| P-22 | Langfuse flush has no timeout | open | observer.flush() unchanged before the final line; Langfuse is optional and off. |

### 5. Previous review, understand area

20 rows: 19 closed, 1 open, 0 user-action, 0 not-a-defect.

| id | claim | status | evidence / test |
|---|---|---|---|
| U-01 | Persuasion-ladder date vocabulary is Brief-A specific | closed | 18 tests; every date kind equals the reference calendar (test_codegen_persuasion_dates.py). |
| U-02 | team_phrase / product_naming have no format contract | closed | 17 tests (IR, validator, fake-LLM run). |
| U-03 | Amount/days fields guessed from names | closed | Explicit disclosure.amount_field/days_field; with all validator issues shown, repro_state_fields reports AMOUNT_FIELD_MISSING and DAYS_FIELD_MISSING for the two bad cases; 17 + 3 compiler tests. |
| U-04 | Policy-guard blocks never reach a repair | closed | repro_guard_repair now reaches the model for GUARD_UNGROUNDED_DATE (its 'model called' stub fires); test_repair_sends_a_policy_guard_block_to_the_model. |
| U-05 | Open-ended top band falsely blocked below 1000 | closed | repro_guard_fp2: days_late_max 46..9999 all ok; 8 tests. |
| U-06 | Supersession applied wholesale | closed | 14 tests. |
| U-07 | Corrupted model output accepted into the ledger | closed | test_a_malformed_passage_answer_is_asked_again. |
| U-08 | xlsx loads as binary, docx tables lose rows | closed | loader_formats: codes.xlsx reads as 'code \| outcome \| reason' rows; 5 tests. |
| U-09 | Ledger guard blocks legitimate amending memos | closed | check_ledger blocked: False for the 'ignore/disregard the earlier rule' memo; 9 tests. |
| U-10 | Tool API field variation (see C-01/C-02) | closed | C-01, C-02, C-05 evidence. |
| U-11 | retries_on_failure=0 ignored | closed | C-09 evidence. |
| U-12 | Case-sensitive ids lowercased | closed | C-08 evidence. |
| U-13 | Date enumeration stops after 120 days | closed | C-06/C-07 evidence. |
| U-14 | Note-only intents rendered as add_note before verification | closed | 2 tests. |
| U-15 | Validator false positive REASON_MISSING | closed | 1 test. |
| U-16 | Over-superseding after a partial amendment | closed | test_a_partial_amendment_narrows_the_old_rule_instead_of_superseding_it. |
| U-17 | 'digital assistant' and Brief-A paraphrases hard-coded | closed | 16 persona tests; grep of builder/ finds the phrase only in builder/testing.py:361 (a test double's default reply). |
| U-18 | wrap_untrusted not used for documents | closed | test_documents_reach_every_understand_prompt_as_untrusted_data. |
| U-19 | Postcode/text identity answers not normalised | open | hidden_like: 'SW1A 1AA' is sent as spoken; only dates and digits are normalised (low; the bank decides). |
| U-20 | ruff check on evals reports an error | closed | ruff clean on builder, tests, evals; test_ruff_is_clean_on_the_eval_tools. |

### 6. Previous review, repo area

21 rows: 16 closed, 1 open, 2 user-action, 2 not-a-defect.

| id | claim | status | evidence / test |
|---|---|---|---|
| REPO-01 | 7 agentic tests fail without claude_agent_sdk | closed | Suite with streamlit, pypdf, claude_agent_sdk, langfuse hidden: 2676 run, 0 failed, 7 skipped. |
| REPO-02 | pytest aborts at collection without streamlit | closed | Same hidden-package run; test_optional_extras.py (4). |
| REPO-03 | 3 loader tests fail without pypdf | closed | Same hidden-package run. |
| REPO-04 | OAuth token known only through .env not forwarded | closed | A-x1. |
| REPO-05 | triage inspects only the last repetition | closed | A10. |
| REPO-06 | HEAD contains none of the project | user-action | Partly overtaken: HEAD is the 'V1' snapshot. 143 paths still differ (A1). |
| REPO-07 | .gitignore '*.log' drops the trace log | closed | .gitignore now re-includes `!traces/*.log`; `git check-ignore -v` names that rule and `git status` lists the trace logs as untracked (not ignored); `*.llm.jsonl` and `*.agentic.jsonl` stay ignored; 9 hygiene tests. |
| REPO-08 | `python3 builder/build.py ...` fails on this host | user-action | Code side done: python3 here is 3.14.6 and the command now exits 2 with a JSON summary line and three install routes (24 startup tests). The host still has no suitable python3 on PATH. |
| REPO-09 | .dockerignore does not exclude .cache | closed | .dockerignore line 56 `.cache`; hygiene test. |
| REPO-10 | Non-AllProvidersFailed error in repair loops 48 times | closed | test_a_crashing_policy_repair_uses_up_its_rounds_instead_of_looping. |
| REPO-11 | Single provider loses 120 s after 3 transient failures | closed | repro_circuit_single_provider: call 2 is OK after a failed call 1; test_an_open_circuit_with_one_provider_still_retries_with_backoff. |
| REPO-12 | Invalid environment crashes the CLI | closed | P-20 evidence. |
| REPO-13 | Recorded Brief A extraction fails the current validator | not-a-defect | The replay of the 19:24 UTC recording still shows 6 errors, but it comes from an older extraction prompt. The live builds at 22:23 UTC passed validate_policy in 0.1 s with no repair. |
| REPO-14 | Per-model 403 kills the whole key for 6 h | closed | repro_403: the second model still answers and no health file is written; 9 tests. |
| REPO-15 | Empty process variable shadows .env in key discovery | closed | repro_empty_env_shadow: 1 and 1 keys found; test. |
| REPO-16 | Unit tests read the developer's real .env | closed | 3 tests incl. the hermetic fixture that fails loudly on a real .env read. |
| REPO-17 | ruff fails on evals | closed | U-20. |
| REPO-18 | evals/triage.py does not load .env | closed | test_the_dotenv_reaches_the_kit_client_before_it_is_imported. |
| REPO-19 | Langfuse compose publishes the UI on all interfaces with default credentials | open | docker-compose.langfuse.yml: `3000:3000`, 'changeme' fallbacks and a zero ENCRYPTION_KEY remain (documented in the file header). Optional integration (low). |
| REPO-20 | Builder code contains text copied from Brief A | closed | grep of builder/ for Brief A sentences, ids and values: no hit (only the generic example 'nineteen eighty' in staged.py). |
| REPO-21 | Reviewer overwrote out/brief-a.{ir,ledger,workflow}.json | not-a-defect | Process side effect, superseded: the acceptance build rewrote those files at 22:23 UTC (they hold a 35-node compile, older than today's 36-node compile). |

### 7. This round (G1, G2, G3)

25 rows: 24 closed, 0 open, 1 user-action, 0 not-a-defect.

| id | claim | status | evidence / test |
|---|---|---|---|
| G1 REPO-10 | Repair loop on unexpected exception | closed | REPO-10. |
| G1 REPO-12 / P-20 | Invalid env / unwritable trace dir: summary line, exit 2 | closed | P-20. |
| G1 P-04 | Absolute deadline for every Freya call, poll, retry | closed | P-04. |
| G1 P-05 | Bounded retry of deploy writes | closed | P-05 (fake platform only, not live). |
| G1 P-03 remainder | Bounded transport (kit's 120 s timeout capped) | closed | test_freya_transport.py plus a loopback run with the real freya.py (from the integration step). Not live. |
| G1 P-10 remainder | Untested redeploy flagged | closed | P-10. No rollback, by design of the fix. |
| G1 EDITOR LAYOUT | nodePositions on create and update | closed | 11 tests (layout, deployed workflow sent with positions for every node, fake records them). All 36 compiled nodes get unique positions (compile_all probe). Live: the acceptance builds predate it; the new create/update body was never sent to the real platform by this builder. |
| G1 REPO-08 | Organisers' python3 command | user-action | REPO-08. |
| G2 C-10 / F04 | Acknowledged-only note state | closed | A7b, F04. |
| G2 F17 / C-17 | One acknowledged promise per product | closed | F17. |
| G2 C-14 | Complete identity answer checked at once | closed | C-14 (offline). |
| G2 F18 | Support clarification vs decline | closed | F18. |
| G2 F19 | Persuasion ladder not padded | closed | F19. |
| G2 U-17 remainder | Persona noun from the policy | closed | U-17. |
| G2 U-03 compiler half | Compiler uses explicit amount/days fields (verification) | closed | U-03: codegen delegates to the IR; repro shows the validator issues; 3 compiler tests. |
| G2 success detection by explicit role | No 'any true boolean' (verification) | closed | Decoy `audit_logged` tests in test_compile_api_variants.py (staged and lean). |
| G2 promise guard validates the date itself | Calendar check independent of the list (verification) | closed | 26 tests: impossible dates, Sunday beyond the horizon, leap day, nonstandard weekdays. |
| G2 docs and rule map | Docs refreshed, rule map names real mechanisms | closed | rulemap_check 0 dangling; L028/L029, layout, transport, LLM_LOG_CONTENT are documented. Docs keep dated counts (2,325/2,470/2,512) older than today's 2,693. |
| G3 logging-privacy | LLM_LOG_CONTENT=metadata keeps texts out of .llm.jsonl | closed | 7 tests incl. an end-to-end file check. Default stays `full` (the file is git-ignored but holds the briefs). |
| G3 REPO-14 | Per-model 403 | closed | REPO-14. |
| G3 REPO-15 | Empty env var shadow | closed | REPO-15. |
| G3 REPO-16 | Hermetic unit tests | closed | REPO-16. |
| G3 REPO-11 | Single-provider circuit | closed | REPO-11. |
| G3 REPO-18 | triage loads .env | closed | REPO-18. |
| G3 triage-final-selection | Repetition selection in triage | closed | 15 triage tests. |

### 8. New findings of this closure

3 rows: 0 closed, 3 open, 0 user-action, 0 not-a-defect.

| id | claim | status | evidence / test |
|---|---|---|---|
| N-1 | A test writes into the real results/ directory | closed (AUD-59) | test_the_summary_names_the_last_tested_version_after_an_untested_redeploy (tests/graph/test_nodes.py) calls finalize_run without redirecting paths. Every suite run (re)creates the untracked file results/s-test-run.build.json (written at 02:40 during this closure; the only repo file my runs changed). Fix: use the `tmp_dirs` fixture. Closed: the test uses `tmp_dirs` and asserts the file lands there; an autouse fixture in `tests/conftest.py` points every test's output folders at a temporary folder (`tests/unit/test_test_isolation.py`); the stray file was deleted. |
| N-2 | product_id matching has no exact-match precedence | open | Probe with ids `Ab-1` and `aB-1`: a request for `aB-1` is sent as `Ab-1` (and blocked on its amount), `AB-1` is sent as `Ab-1`. The hook normalises case, spaces, dashes and underscores and takes the first hit (hooks.py 'product_id' branch). Low (rare id sets) but F03's exit check is not met. |
| N-3 | .freya-agents.json is tracked and holds your staging workflow ids | open | Tracked since the V1 commit, not in .gitignore (only in .dockerignore): 5 slots (base, brief-a, brief-a-brief-b, ref-a, ref-ab). With another workspace key, the first build of slot `brief-a` tries to update a foreign id: a 404 recreates (tested), any other status such as 403 fails the deploy (_is_missing_workflow handles only 404; not testable without Freya). |

## Open risks

1. **Final source is not proven live.** The live scores belong to the 22:23 UTC compile. Everything since then has only offline proof: the deploy path with editor positions, the bounded transport and write retries (G1), the identity fix for PUB-03 (C-14), the support-offer branches (F18), the unpadded persuasion ladder (F19), the acknowledged note and promise hooks (F04, F17), explicit amount/days fields (U-03). A fresh live build and a 3-repetition score of the final commit is the one thing that would change the picture.
2. **Known conversation misses are root-caused, not re-measured.** PUB-03 (identity exhaustion) and PUB-07 (holiday proposal corrected to the wrong legal date). F21 stays open: the hook checks that a date is legal, not that it is the required correction of the customer's proposal.
3. **Enforcement still resting on the prompt:** confirmation binding (F16), `decline_max` (F07), identity answer validation and mislabeled unknown responses (F06), callbacks outside the allowed window (C-16), a failed smoke test without repair (P-17), hardship precedence in the lean profile (C-15), real audio behaviour (F20).
4. **Prompt size.** The Brief A negotiate prompt is 5,954 of the 6,000 characters of lint rule L018; any new rule trips it (A+B already warns at 8,559).
5. **Submission hygiene.** `.freya-agents.json` is tracked with staging workflow ids (N-3); `LLM_LOG_CONTENT` defaults to `full`, so `traces/*.llm.jsonl` holds the briefs and every model reply (git-ignored, keep it out of any archive); the suite re-creates `results/s-test-run.build.json` on every run (N-1).
6. **Residual limits of fixed items:** a lost create response that the name search cannot reconcile can leave one stray workflow (P-05); an untested redeploy is flagged but not rolled back (P-10); the practice node waits for the simulation slot without a timeout (F08); a blocked `create_promise` in the lean profile reaches the model as the generic 'Missing required field: url' error; the engine does not flush pending notes after a transfer decision (C-11); secrets can still reach `results/*.build.json` and Python log output inside an error text (A11).
7. **Optional pieces untested live:** Langfuse (flush without timeout, P-22; compose file with default credentials, REPO-19) and the agentic fallback with a real SDK session.
8. **Docs carry dated numbers** (2,325, 2,470 and 2,512 passed tests in docs/FIXING_ISSUES.md); today's count is 2,693. A formatting difference exists in builder/llm/providers.py (`ruff format`; `ruff check` passes, format is not a gate).

## USER ACTIONS

1. **Git (I changed nothing). HEAD cannot run as it stands:** `builder/_startup.py`, `builder/compiler/layout.py`, `builder/freya/transport.py` and `requirements.txt` are imported or needed at runtime and are still untracked, together with 11 new test files, `builder/submission.py`, `builder/viz.py`, `.claude/` (the agentic fallback loads the skills) and `submission/`. Commit everything except `.env`, `env.sh`, `.venv*`, `.cache`, `data/key_health.json` and the other git-ignored paths (they are already ignored; `data/build-the-builder-v3*` is the kit copy and is already in HEAD). Do not commit `traces/*.llm.jsonl` and `traces/*.agentic.jsonl` (ignored on purpose). Currently 86 modified and 57 untracked entries are waiting.
2. **Trace logs.** The task note said `.gitignore` `*.log` excludes the trace logs that must be submitted. That was true earlier; the file now has `!traces/*.log` right after `*.log` (and it is already committed: `git status` shows `.gitignore` unmodified), and `git status` lists `traces/*.log` as untracked, not ignored. Check once more with `git status` before committing. `results/acceptance/score_*.log` are still ignored (their content is in `score_*.json` and `summary.txt`).
3. **`.freya-agents.json`** (N-3): remove it from the index or reset it to `{}` before you submit (`git rm --cached .freya-agents.json` and add it to `.gitignore`), so an evaluator's key never meets your workflow ids. It is also what links your slots to the live workflows, so keep a local copy.
4. **Environment.** Recreate the venv with `uv sync --extra agentic --extra ingest --extra dev` (the current `.venv` points to a uv Python under AppData\Roaming; add `--extra ui` or `--extra obs` only if you use them). The organisers' `python3 builder/build.py ...` needs a Python 3.11-3.13 behind `python3`: on this host `python3` is 3.14.6 and the command stops with exit 2 and install instructions. `uv run python builder/build.py ...` or `docker compose run --rm builder ...` avoid the problem.
5. **Final live acceptance (spends credits, needs your go).** From the selected source, build Brief A and Brief A+B and score with three repetitions. For A use `uv run python tools/run_cases.py <A_agent_id> --brief A --reps 3 --out results/run_a.json`. For the complete A+B public set omit the brief filter: `uv run python tools/run_cases.py <AB_agent_id> --reps 3 --out results/run_ab_all.json`; `--brief B` selects only PUB-11–13. Keep each practice export and build trace as development evidence. Final submission preparation must instead select the two actual hidden-brief exports/traces and authorized final commit, as described in docs/SUBMISSION.md. Nothing in this review authorizes Git changes or delivery.
6. **Cleanup of byproducts (your call).** `results/s-test-run.build.json` (re-created by every test run); `traces/inputs-20261010T234455Z.{log,jsonl}` (my invalid-environment check); review leftovers in `traces/` and `results/` named `qr-*`, `integ-*`, `integ2-*`, `docs-*`, `audit-*`; 440 MB in `.cache/`; old venvs `.venv-playground` and `.venv-py314-old`; Docker images `freya-builder:review`, `review2`, `review-tests`, `tests` (`docker rmi`).
7. **Langfuse is optional.** If you turn it on: `uv sync --extra obs`, change every `changeme` secret in `docker-compose.langfuse.yml`, publish port 3000 on `127.0.0.1` only, and set `LLM_LOG_CONTENT=metadata` before you share any trace directory.
8. **Decide on the open rows** in the table (low unless noted): C-16, F06, F07, F16, P-17 are the ones that change agent behaviour; N-1 and N-2 are small fixes with clear tests to write first.

## Side effects of this closure

- Written: `results/QUALITY_REVIEW.md`. Created by my checks: `traces/inputs-20261010T234455Z.log` and `.jsonl` (untracked), plus `.cache/quality-review/closure/` (copies of the outputs, probes and the junit reports; git-ignored). The test suite itself rewrote `results/s-test-run.build.json` (N-1).
- Not touched: `results/acceptance/`, `out/brief-a*`, `.freya-agents.json`, `.env`, the kit files.
