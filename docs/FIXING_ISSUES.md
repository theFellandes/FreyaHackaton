# Fixing issues before the Freya hackathon submission

The highest-value work is to make the **automatically built agent** correct, verify that its submitted export matches the published version, and test realistic conversations repeatedly. High scores from a hand-authored reference agent and passing helper tests are useful development evidence, but they do not establish that the submission pipeline succeeds.

This backlog records findings inspected on **10 October 2026**. Items remain open until their exit checks pass; explicit refresh notes distinguish addressed regressions from historical findings. It combines reproduced defects, environment problems and missing acceptance evidence; those categories are identified below. The companion [edge case guide](EDGE_CASES.md) explains birthday validation and supplies a broader scenario matrix. [Validation and lessons](VALIDATION_AND_LESSONS.md) preserves the earlier measurements.

The [banking conversation review](BANKING_CONVERSATION_PRACTICES.md) adds primary-source research and 36 proposed scenarios for persuasion, repetition, clarification, confirmation and audio interruption. Its follow-up inspection adds F16–F20 below; these are builder enforcement gaps and acceptance work, not observed live-customer failure counts.

**Later source refresh:** the [official-rules comparison](HACKATHON_RULES_COMPARISON.md) found newer publication, export and cancellation handling addressing parts of F05 and F08. Those sections preserve historical counterexamples, not freshly reproduced current runtime failures. Current source still needs end-to-end acceptance, and export/cutoff success reporting remains a concern. A focused 366-test compiler run passed; the old staged-route assertion described in F13 is corrected. The earlier full-suite count remains a dated snapshot.

**Further logging/edge review:** 69 current API-variant and consistency checks passed, addressing several F01/F03 regressions. The selected logging/trace/triage checks produced 78 passes, two optional SDK skips and one excluded stalled async test. F10's repetition selection is now covered by passing regressions. Two fresh helper probes found a decoy success-boolean mapping (F01) and invalid dates beyond the enumerated horizon (F02). Details and proposed evidence collection are in [Competitive edge and internal logging](COMPETITIVE_EDGE_AND_LOGGING.md). These runs do not establish full live agent behavior.

## Priorities and evidence

**P0** means an issue can invalidate the artifact, violate a policy or undermine the evidence used to accept it. **P1** improves generalization, diagnosis or reproducibility. **P2** covers optional integrations after the submission path works. These priorities guide repair order; they are not estimates of hidden-case frequency or a predicted competition ranking.

Current offline compiler and runtime rechecks reproduced selected earlier counterexamples. Local reproduction scripts and results are in [compiler recheck](../.cache/project-audit/compiler-recheck/), [runtime recheck](../.cache/project-audit/runtime-recheck/) and [identity probes](../.cache/project-audit/identity/). These ignored cache directories are machine-local evidence. Each repaired regression should become a portable project test, using synthetic inputs and no real credentials.

## P0 submission and policy defects

### F01 Resolve tool field roles from the supplied contract

**Historical defects, partially addressed.** Earlier probes found that renaming closing, acknowledgment and identity fields produced invalid requests or incorrect response handling. Current [API-variant regressions](../tests/unit/test_compile_api_variants.py) for renamed fields now pass. However, a fresh synthetic probe of [semantic role selection](../builder/compiler/roles.py) maps action success to `audit_logged` when that unrelated boolean is mentioned before `ok` in the response description. Ambiguity and decoy fields still need an independent regression. See [tool assembly](../builder/compiler/toolset.py), [API actions](../builder/compiler/tools.py) and [hooks](../builder/compiler/hooks.py).

Introduce an explicit semantic mapping for request and response roles, derived from the supplied API and checked before compilation. Use it consistently in schemas, hooks, routes and outcome code. Ambiguous required roles should produce actionable feedback rather than an apparently valid workflow with guessed semantics.

**Exit check:** synthetic API variants with renamed fields in all seven operations compile and execute correctly, including notes, callbacks and transfers; success and failure responses remain distinguishable. Unsupported mappings produce a clear unsuccessful build. No public-brief field values are added to production constants.

### F02 Fail safely when allowed promise dates are empty

**Historical defects, with a fresh boundary failure.** Earlier probes found fail-open empty lists and rejection beyond a 120-day enumeration horizon. The current hook explicitly blocks `allowed_count_<product>=0` and permits later dates within the policy tail. A fresh helper probe shows that the latter branch admits a Sunday and a nonexistent date (`2028-02-30`): it checks string bounds and ISO shape without full calendar/business-day enforcement. This is an isolated hook probe, not a demonstrated live write or full account-facts run. Inspect [date generation](../builder/compiler/codegen.py), [promise guarding](../builder/compiler/hooks.py) and the [probe description](COMPETITIVE_EDGE_AND_LOGGING.md).

Represent “no legal date,” “facts unavailable” and “dates available” explicitly. Enforce the extracted calendar and deadline policy independently of whether an enumeration happens to contain values, including policies with `not_after_deadline=false`. Either support longer valid windows or reject unsupported policy shapes before deployment; silently truncating them is unsafe.

**Exit check:** expired deadlines, weekend month ends and no-business-day windows send no invalid bank request. Valid dates beyond 120 days remain valid when the policy permits them. Generated hooks agree with independent boundary expectations, including holiday shifting across a deadline.

### F03 Preserve product identifiers exactly

**Historical defect; canonical-ID regressions now pass.** Earlier probes lowercased opaque identifiers such as `ABC123`. Current [API-variant tests](../tests/unit/test_compile_api_variants.py) verify uppercase product IDs, canonical note IDs and enum spelling. Treat the earlier counterexample as historical. Retain the exit checks for all writes and ambiguous aliases; similar IDs must not be silently merged by case-insensitive matching in [hooks](../builder/compiler/hooks.py).

Keep the canonical ID returned by `get_account` unchanged. If natural-language aliases are supported, map them to a unique canonical ID without altering that ID. Ambiguous aliases require clarification.

**Exit check:** uppercase, mixed-case, numeric and punctuation-containing synthetic IDs survive every promise and note call unchanged. Similar IDs are not merged, and invented IDs produce no bank write.

### F04 Update recorded state only after acknowledgment

**Confirmed defect.** The note pre-hook updates `notes_log` before the bank confirms the write. A failed note can consequently affect the final closing reason. Inspect [note hooks](../builder/compiler/hooks.py), [tool assembly](../builder/compiler/toolset.py) and [outcome code](../builder/compiler/codegen.py).

Keep intended actions separate from confirmed records. Commit note state in a successful response path, with duplicate protection. Apply the same review to promises, callbacks and transfers. When a timeout leaves the write status unknown, use only reconciliation or retry behavior supported by the actual API.

Fresh [conversation probes](../.cache/project-audit/conversation-practices/capability_probe_results.json) reconfirm that a failed note remains in the log and a retry appends it again. They also show a failure-only transfer counter producing a transferred closing outcome; the required failed-transfer outcome is not established. Separately, the single-product staged success route uses `create_promise_success_count`, while response instructions inspect the bank's `ok` flag. Actual platform counter semantics for transport success with bank failure remain unverified. Check these paths against acknowledgment and the supplied closing policy before treating them as correct.

**Exit check:** a failed write does not appear as a recorded note or justify a success sentence. A successful retry records exactly one logical action. Mixed-product closing uses acknowledged records and the policy's precedence.

### F05 Require a published version and a matching export

**Confirmed defects.** `published=false` can still become `status=deployed`. If export fails, finalization can report the proposed local workflow as the export. After a repair, version two can be reported while the retained export contains version one. See [deployment](../builder/graph/nodes/deploy.py) and [finalization](../builder/graph/nodes/finalize.py).

Make publication and export integrity explicit acceptance conditions. Tie workflow ID, version and export metadata together. Clear stale export state after every new save. Keep the proposed workflow available as a diagnostic artifact with its own identity.

**Exit check:** unpublished saves, failed exports and stale exports cannot produce a successful submission result. A repair followed by successful export reports the current published version. Read-back or export differences are explained and checked before scoring.

### F06 Distinguish identity mismatch from unreadable input and service failure

**Confirmed robustness gap.** The normalizer does not validate calendar dates or required answer lengths. Synthetic unknown hints, empty data and an error response containing a data object can also receive the instruction “This answer is wrong.” These are controlled response-envelope probes, not observed live platform incidents. See [identity hooks](../builder/compiler/hooks.py) and [identity response rules](../builder/compiler/toolset.py).

Generate format and completeness checks from each `IdentityQuestion`. Keep the bank as the authority for whether a syntactically valid answer matches the customer. Parse response status before labeling a mismatch; unknown or missing verification state must never unlock the account. Preserve the brief's clarification and retry rules.

**Exit check:** malformed dates and incomplete answers receive clarification; complete but incorrect birthdays follow bank-reported attempt state. Unreadable input, hint-based clarification and service errors are not described as verified mismatches. Neither a partial match nor an unknown response permits account access. See the [identity matrix](EDGE_CASES.md#identity-scenarios-to-verify).

### F07 Honor retry and refusal limits from policy

**Confirmed defects.** Setting promise retries to zero still leaves a “call it once more” instruction in the negotiation prompt. Setting `identity.decline_max` to three still produces a second-refusal closing instruction. The identity wrong-answer route also assumes the service's remaining counter matches the brief; that is a contract-consistency requirement rather than a proven live failure. Inspect [prompts](../builder/compiler/prompts.py), [identity template](../builder/prompts/agent/identity.v1.j2), [tool assembly](../builder/compiler/toolset.py) and [identity routes](../builder/compiler/profiles/staged.py).

Use each policy-defined limit consistently in prompts, response instructions, runtime counters and routes. Promise retries, wrong identity answers and refusals need distinct counters. Treat malformed answers, blocked requests and service failures as separate events. Avoid counting the same event twice.

**Exit check:** zero, one and multiple configured retries behave as specified. Refusal limits of one, two and three close at the right point. Wrong answers across different questions share the required total; hint-based clarification preserves the remaining count. Service-policy disagreement is detected or handled explicitly.

### F08 Make the wall-clock limit apply to active operations

**Confirmed cancellation limitation.** A synchronous save in a worker thread continued after graph cancellation in the earlier scaled probe. The inspected runtime source is unchanged. This demonstrates the mechanism, not an actual 30-minute overrun. See [build orchestration](../builder/build.py), [budget](../builder/budget.py) and [deployment](../builder/graph/nodes/deploy.py).

Propagate remaining time into transport and subprocess timeouts, reserve time for export and finalization, and use operations whose termination can be controlled. Track uncertain in-flight outcomes explicitly. The current practice path also waits for the simulation lock without a timeout, using a subprocess budget calculated before that wait; this is a source-confirmed risk whose busy-lock case was not newly executed. Bound lock acquisition and recompute the remaining budget afterward.

**Exit check:** a deliberately slow fake save, export and self-test complete process shutdown within the configured deadline plus a documented bounded cleanup allowance that fits inside the kit's limit. Include executor shutdown in the measurement. A held simulation lock expires within the remaining budget. Cancellation cannot silently publish an unreported version; uncertain deployment outcomes preserve recovery information. The intended submission environment preserves a usable trace.

### F09 Require meaningful walkthrough evidence

**Confirmed defect.** An empty response with `checked=0` and no issues can claim every active rule was audited. The synthetic recheck also found that opposite code values produce identical walkthrough text. See [walkthrough auditing](../builder/selftest/walkthrough.py).

Supply the relevant code, hooks and routes to the auditor and validate which rule IDs it actually evaluated. Treat missing or incomplete work as incomplete, including workflow content lost to truncation. Keep structural rule mapping separate from behavioral evidence; [coverage](../builder/checks/coverage.py) currently counts declared locations. An unavailable optional auditor can remain explicitly skipped.

**Exit check:** zero checked rules, duplicate IDs, missing rules and unsupported claims cannot pass as a complete audit. Altering an enforced policy value changes the auditor's input. Omitted content is identified or audited in another chunk. A mapped but incorrectly implemented rule can fail an independent test.

## P0 conversation enforcement follow-up

### F16 Bind confirmation to the exact pending proposal

**Confirmed enforcement gap.** A generated promise pre-hook leaves a valid synthetic request enabled without confirmation state. The [tool description](../builder/compiler/toolset.py) and [negotiation prompt](../builder/prompts/agent/negotiate.v1.j2) require readback and clear agreement, but the [pre-hook](../builder/compiler/hooks.py) checks product, money and date rather than confirmed terms. This proves a missing gate; it does not prove a live model submitted an unconfirmed promise.

Track the current proposal, its revision, completed required readback and the relevant customer confirmation. Require those to match at execution. Changes to material terms invalidate earlier confirmation. Use actual customer-turn and delivery evidence available from the platform; a model-created approval flag alone is insufficient.

**Exit check:** no confirmation, agreement to another question, incomplete readback, ambiguous assent, changed product/date/amount and “yes, but change the date” cannot authorize an unsupported write. A correct, complete confirmation enables exactly its proposed action. Combined confirmation follows the extracted multi-product policy. See C18–C23 in the [conversation matrix](BANKING_CONVERSATION_PRACTICES.md#additional-acceptance-scenarios).

### F17 Enforce one immutable acknowledged promise per product

**Confirmed enforcement gap.** In synthetic generated-hook probes, identical same-product requests and changed valid terms remain enabled after injecting prior success state. The prompt prohibits those actions under the public contract; the hook has no acknowledged-product guard. No duplicate bank acceptance or live model behavior was measured.

Maintain per-product acknowledged records, updated only on the bank's specified success response. Block later writes that violate the extracted once-only or immutability rule. Distinguish pending and unknown results from acknowledged records; preserve supported retry behavior for known failures without guessing new API fields.

**Exit check:** repeated requests, node reentry and post-recording corrections produce no second promise or unauthorized overwrite. Another product remains independently eligible. Unknown outcomes receive only API-supported handling. Repeating a status sentence never requires repeating the bank write. See C24–C29.

### F18 Separate support clarification from declining support

**Source-confirmed prompt conflict.** [Context generation](../builder/compiler/context.py) instructs that an answer other than “yes” declines an offered handoff. A question such as “What would support help with?” or a request for time can consequently be treated as decline, despite general clarification and cut-off-speech guidance. The live effect is unmeasured.

Generate explicit acceptance, refusal, clarification, waiting and unreadable-input branches consistent with policy. Preserve the pending offer during clarification and avoid spending a refusal allowance twice. Retain policy-specific exceptions: public Brief A explicitly counts asking the call's purpose instead of answering a security question as an identity decline.

**Exit check:** clarification, interrupted speech, small talk, clear refusal and alternative affirmative wording each take the prescribed branch. A single support offer is not repeatedly reissued, and a clarification does not create a declined-support note or premature farewell. See C08–C10.

### F19 Repair incomplete persuasion ladders and retain offer history

**Confirmed synthetic compilation defect; additional enforcement gap.** With one applicable step and two allowed attempts, [policy validation](../builder/understand/validate.py) emits `PERSUASION_STEPS_SHORT` warnings, and [routing](../builder/graph/routing.py) proceeds without errors. [Code generation](../builder/compiler/codegen.py) repeats the final step to fill the allowance, producing identical successive offers. The [padding probe](../.cache/project-audit/conversation-practices/persuasion_padding_results.json) preserves the counterexample. No full deployed build was run.

Repair or diagnose incomplete extracted ladders before compilation instead of inventing a step by duplication. Preserve per-product attempts, offered/rejected terms and policy overrides across node reentry. Separate proposals, clarification and hardship from flat refusal. Respect explicitly authorized reuse of an amount or required wording; no-repeat logic must understand the whole offer and its policy context.

**Exit check:** missing, reordered and band-specific steps cannot silently produce an unauthorized repeated offer. Distinct prescribed steps remain intact. Interleaved refusals, proposals, product changes and hardship preserve the correct independent state. Exhaustion stops further persuasion under the supplied policy. See C01–C07.

## P1 diagnosis and readiness

| Issue | Required change | Exit check |
|---|---|---|
| F10 Failures hidden by the final repetition — addressed in current offline checks | [Triage](../evals/triage.py) now walks `all_rows` and supports failing attempts, all repetitions and a selected repetition. | Passing [triage regressions](../tests/unit/test_eval_triage.py) cover first-fail/last-pass, every failing repetition, all repetitions and legacy result files. |
| F11 Error redaction gaps | Redact external exception text before ordinary logging, returned errors and result JSON; retain trace masking. See [node error handling](../builder/graph/nodes/_common.py), [CLI](../builder/build.py) and [finalization](../builder/graph/nodes/finalize.py). | A synthetic configured secret is absent from stdout, stderr, logs, trace files and returned artifacts, including failed API calls. No real key is needed for the test. |
| F12 Agentic credential forwarding | Eligibility accepts an explicitly configured OAuth token, but the inspected child environment omits it. Align availability checks with the [agentic runner](../builder/agentic/runner.py). | An injected fake child runner receives the intended synthetic credential; logs never reveal it. A real optional SDK session is verified separately. |
| F13 Reproducible test environment | Repair the broken main interpreter environment; install intended optional dependencies or isolate their availability checks in tests. Review the stale `promise_done` route expectation rather than deleting the assertion blindly. | A clean intended environment runs the appropriate suite. Missing optional capabilities are explicit. The earlier 1,950/8/2 snapshot is replaced only after a new complete run. |
| F14 Automatic builder acceptance missing | Build and score the generated workflow, preserving exact input files, source hashes, policy, version, export and trace. | Completed official reports establish behavior for the automatically built A and A+B workflows, rather than substituting reference-agent scores. |
| F15 Packaging and deployment environment unverified | Exercise the chosen native or Docker submission path from a fresh checkout. Current Git HEAD does not yet contain most implementation files; Docker execution was not established by the audit. | The intended environment reproduces the build and artifacts. Prepare a secret-free release set; staging and committing remain a later user-authorized step. |
| F20 Real audio interruption acceptance missing | Inspect actual platform defaults and supported speech/idle controls. Test stopped playback, cleared audio, complete caller input, readback delivery, pending writes and farewell timing. Current chat scores and absent explicit voice settings do not establish audio behavior. | Measured real-call evidence covers C07, C16, C18, C22 and C30–C36 in the [conversation guide](BANKING_CONVERSATION_PRACTICES.md#additional-acceptance-scenarios), with accurate bank-state assertions. Targets follow actual-platform baselining. |

Optional Langfuse compatibility tests were skipped because its SDK was absent. Verify that integration if it will be enabled during submission; keep it P2 when it is optional. Extra dashboard features should follow the scored workflow rather than displace its verification work.

F20 concerns acceptance for real voice use. The documented hackathon practice evaluation uses text; this research does not establish audio testing as an additional submission requirement.

## Closing the backlog

For each repair, first preserve a small failing synthetic test, implement the generic mechanism, then run the affected tests. Record the result and source snapshot before marking the issue complete. A changed prompt or a clean lint report alone does not close a behavior defect.

After the blockers are repaired, run the full offline suite and both fixture builds. Then perform a current-source live build, verify publication and export, and run practice cases serially with three repetitions. Start with normal style, then the identity and conversation styles described in [edge cases](EDGE_CASES.md). Run focused regressions before spending time on a broad sweep.

Preserve failed repetitions, bank records and transcripts. Use them to improve the reusable builder, and rerun affected cases after a change. The final evaluation also uses different call dates and unseen policy/API variants, so public-case success needs the synthetic generalization matrix alongside it. The kit's scoring order is customer success, then promised amount on passed customers, build time and submission time; improving promises or speed while customers still fail does not address the main score. See the [official kit](../KIT_README.md).
