# Project assessment against the official hackathon

**The project has a good engineering foundation and addresses the correct deliverable. Its current evidence supports a substantial builder in development; submission readiness and competitive customer reliability remain unverified.** More dashboard features would not resolve the main uncertainty: whether an automatic build produces an exported agent that repeatedly passes the official scorer.

The official-page comparison was made on **10 October 2026** and project evidence was refreshed on **11 October after Claude finished**. The live [official hackathon page](https://www.freyavoice.ai/hackathon#rules) was read in the browser after the web fetcher failed. Its Overview, Data, Evaluation, Submit, Rules and FAQ tabs were inspected. The linked [version 3 kit](https://www.freyavoice.ai/hackathon/build-the-builder-v3.zip) was downloaded, and eleven relevant organizer files matched the repository byte for byte by SHA-256: README, both briefs, OpenAPI, practice cases, Freya client, three platform documents and two scoring tools. Their hashes still match in the settled-source verification. The repository names the kit README [KIT_README.md](../KIT_README.md).

The kit comparison and the source/result snapshot of this check (`.cache/project-audit/hackathon-rules/`, git-ignored, so only on the machine that ran it) are audit artifacts, not a submission package. Source was changing during review, so historical findings below are distinguished from current behavior.

**Status, 11 October afternoon:** this assessment predates the AUD fix round. Several counterexamples named below are fixed (exact product ids, identity refusal limits, acknowledgement-gated routing, redaction of result files). The status of every item is in [FIXING_ISSUES.md](FIXING_ISSUES.md); measured scores are in [results/BASELINE.md](../results/BASELINE.md). The official page facts used for the hidden-brief day are in [SUBMISSION.md](SUBMISSION.md).

## What is good about the project

The architecture fits the event's core problem. [Input ingestion](../builder/ingest/brief_loader.py), a source-grounded rule ledger, [typed policy](../builder/policy/ir.py), deterministic compilation and [graph orchestration](../builder/graph/build_graph.py) form an automatic interpretation-to-workflow pipeline. That is a concrete implementation of the required builder.

Policy calculations become runtime code and hooks rather than relying entirely on conversational instructions. With the public contract, identity routing gates account access, calendar and money code prepare permitted terms, and closing logic handles multiple products. The ledger and generated artifacts make interpretation and enforcement locations inspectable. These are useful engineering strengths, although a rule-to-location map does not establish correct execution.

Testing and diagnosis are substantial. On the evening of 10 October, independent offline verification produced **2,690 passed and three optional-package skips** in 134.44 seconds, with clean Ruff results across `builder`, `tests` and `evals` (the suite had grown to 3,314 tests by 11 October afternoon). All 236 checked source, fixture, skill and configuration files stayed unchanged. [Preparation verification](../submission/VALIDATION.md) preserves the commands, hashes and earlier runs. A passing suite does not invalidate counterexamples outside its assertions or establish hidden-customer acceptance.

The current deployment implementation has also improved: publication confirmation, export retry and identity checking, comparison with read-back when available, atomic export replacement and stale-target deletion are present. Deadline handling now uses cancellation tokens, daemon workers and deployment snapshots. Older automatic A and A+B builds have live publication, exports and measured timing; those runs predate this revised deployment path. Its complete live behavior remains unverified.

## Requirement comparison

The contract below comes from the verified [kit](../KIT_README.md) and associated organizer files. “Implemented” describes source mechanisms; “verified” requires the relevant execution evidence.

| Requirement | Current project fit | Assessment |
|---|---|---|
| One command reads a brief and its API, then creates an agent without hand edits | [CLI](../builder/build.py) and graph implement extraction, validation, compilation, deployment, testing and repair. | Automatic public A and A+B publication/export runs now exist; they predate later repairs. Current-source live acceptance remains required. |
| Handle unfamiliar specification and raw bank material | Ledger and policy extraction support source grounding and amendments. Clean and raw-material dry-run artifacts exist. | Useful mechanisms and compilation evidence; unfamiliar-document interpretation needs independent acceptance. |
| Changed API fields, enums, identity questions, limits and calendars | OpenAPI ingestion and typed policy capture these concepts; boolean roles and calendar guards have regressions. | Partial generalization. Exact-ID precedence (N-2) and identity refusal limits (AUD-35) were fixed on 11 October; a numeric identity-counter decoy has not been re-checked. |
| Keep the organizer's phone-agent model | [Freya client](../builder/freya/client.py), kit client and settings preserve/enforce the fixed model. | Implemented in source. Reserved agent-model override variables must stay unset. |
| Agent calls only the supplied bank API | Final workflow guards and generated tools restrict API use to the supplied contract. | Inspect the selected export's tool definitions and runtime argument handling; do not add external logging or channel tools to the scored agent. |
| Reuse workflows, publish and submit actual exports | Slot reuse and improved publication/export paths exist. | Improved source behavior. A zero exit still does not guarantee a usable export or an uncut build. |
| Finish within 30 minutes including self-tests | Default budget is 28 minutes, with guard and in-flight allowances. | Older build traces measure 96.2 seconds for A and 132.7 for A+B, including their self-checks. Unfamiliar final builds and active-operation cutoff still require verification. Keep each configured budget within the official limit. |
| Trace each run's timing, model/purpose calls, created objects and tests | [Trace writer](../builder/trace.py) records these facts, including interrupted tests. | Implemented. Final trace must correspond to the submitted exported version and exclude secrets. |
| Correct bank records, conversation policy and honest status | Generated routes, hooks, prompts and outcome computation address all three; sequential acknowledged-promise duplicate blocking is verified offline. | Routing on bank acknowledgements instead of engine counters was fixed on 11 October (AUD-07). Exact-term consent is a prompt rule by design; date-correction provenance (F21) remains open. |
| Reliable natural conversation, multiple products and call dates | Policy supports multiple products and uses the supplied call date; official styles are available. | Current automatic-agent acceptance across styles, dates and repetitions is not established. |
| Reproduce from the submitted commit | Builder, dependencies and packaging exist; most implementation is tracked. | Current runtime changes and project skills are not all committed. The [submission checker](../builder/submission.py) checks selected-commit coverage; clean-checkout reproduction remains required. |

The official [FAQ](https://www.freyavoice.ai/hackathon#faq) adds an important constraint: hidden briefs arrive without practice cases, and the hosted practice bank serves only the public cases. Do not assume the builder can create arbitrary hidden-brief practice customers. The current practice selector limits that path to public briefs; use supported chat checks and local synthetic enforcement tests for other inputs. Hidden raw material is supplied as one Markdown file with its own API file.

## The desired outcome is policy-correct resolution

The [public briefs](../briefs/brief-a.md) and [Brief B amendment](../briefs/brief-b.md) require successful outcomes that may contain no promise. Eight of the thirteen [practice cases](../public-test-set.json) expect promises; five expect callback, identity failure, hardship, paid or transfer handling. Maximizing promises is therefore not the same as maximizing passed customers.

| Customer situation | Desired records and behavior under the public briefs |
|---|---|
| Valid, clearly confirmed payment terms | Exact product, amount and date, one recorded promise per product, required confirmation sentence and final closing. |
| Relative or other third party answers | No debt disclosure or identity/account calls; permitted callback or prescribed not-reached outcome. |
| Identity fails or is declined | No account/debt access; prescribed verification outcome and guidance. |
| Hardship | Stop persuasion, follow support acceptance/decline policy; a volunteered valid proposal remains possible. |
| Already paid | No unnecessary promise; paid outcome in A, appropriate per-product note and continuation in B. |
| Disputed debt | No argument or promise for that product; dispute outcome/note according to the brief. The Evaluation tab: the passing outcome is a recorded dispute and no promise. |
| Restructuring or human/support request | When the prescribed conditions are met, use the correct transfer queue and reason; retain earlier recorded promises and omit unrecorded agreements. |
| Recording failure | Only permitted retries, truthful status, appropriate error handling and preservation of other acknowledged products. |
| Several products | Independent deadlines/attempts, required combined readback, exact notes and correct overall outcome precedence. |
| Any completed call | Exactly one `close_call`, as the final tool call. |

Brief B also prohibits notes before or after any transfer. If no promise exists, its closing reason depends on the first non-paid product in bank order. The [ledger scorer](../tools/score.py) compares complete record lists, including duplicates; the [harness](../tools/run_cases.py) also requires transcript policy and honesty checks.

The live [Evaluation tab](https://www.freyavoice.ai/hackathon#evaluation) explicitly says the mock API can record invalid terms. The bank will not enforce the brief for the agent. The final uses two unfamiliar briefs, thirty customers each (one to three products, several calendar days) and three repetitions; every repetition must pass (ledger outcome, judge policy and judge honesty). A correct refusal counts. Ties are broken by the total promised amount on passed cases, then the shorter total build time from the traces, then the earlier submission. This makes generic enforcement and consistent conversation behavior central to the score.

## What the saved results prove

The earlier audit snapshot contained dry runs and failures without published workflow IDs. Later saved automatic runs now provide stronger evidence:

| Automatic build | Strict score, three repetitions | Builder trace | Model calls |
| --- | --- | --- | --- |
| [A](../results/acceptance/score_A.json), workflow `58bd49f0…` v1 | 8/10 customers; 28/30 conversations | 96.2 seconds | 6 |
| [A+B](../results/acceptance/score_B.json), workflow `b5638c83…` v1 | 3/3 **B amendment cases**; 9/9 conversations | 132.7 seconds | 8 |

Manifests, actual exports and traces agree on their agent/workflow identities and publication/export success. These scores belong to two agents; the inherited ten A cases remain untested on A+B. Both runs predate later repairs, and scorer reports do not bind an explicit workflow version or source hash. Neither row is a final hidden-brief submission artifact.

Saved hand-authored reference runs establish useful development behavior:

| Report | Customers passing every repetition | Repetitions |
|---|---:|---:|
| [A v8 normal](../results/ref_a_v8_reps3.json) | 9/10 | 3 |
| [A v10 talkative](../results/ref_a_v10_talkative.json) | 6/10 | 2 |
| [A+B v5 normal](../results/ref_ab_v5_reps3.json) | 3/3 | 3 |
| [A+B v8 talkative](../results/ref_ab_v8_talkative.json) | 0/3 | 2 |

These are different reference versions and conditions, not a single current performance curve. The A v10 talkative failures include callback completion, date negotiation and repeated hardship handling. Newer A normal and A+B talkative log files end with platform errors before final reports; partial progress is not a completed strict score. The [reference source](../evals/reference/brief_a_ref.py) identifies itself as a development fixture with public-brief values intentionally embedded.

## What prevents a stronger verdict

Current source repairs the reviewed decoy-boolean, full-calendar, premature-note-state and sequential duplicate-promise examples. On 10 October, fresh probes also reproduced exact-ID corruption, unrelated numeric-counter inference, incomplete/error identity handling, absent exact-term consent and a synthetic credential in persisted result errors; the exact-ID, identity-normalisation and redaction items were fixed on 11 October, and exact-term consent stays a prompt rule by design. A legally valid date is still not bound to the required correction of the customer's original proposal (F21). The [repair backlog](FIXING_ISSUES.md) and [conversation review](BANKING_CONVERSATION_PRACTICES.md) explain the remaining counterexamples and acceptance criteria.

The earlier duplicated-offer probe is historical: the compiler now uses available persuasion steps without padding the final step. A band with more attempts than steps is now a validation error that the repair loop must resolve (AUD-30). Conversation state across re-entry, clarification and hardship still needs acceptance.

Historical publication/export and cancellation findings need a refreshed interpretation. New source addresses significant parts of F05 and F08. Missing actual exports now produce `deployed_unexported`, so the older missing-export/zero-exit claim is no longer current. A later cutoff or test failure can still coexist with a reported deployment, and export checks are weaker when read-back fails. Current live acceptance should evaluate the revised implementation. [Submission preparation](SUBMISSION.md) adds local checks without claiming an official score.

The highest-value next evidence is fresh, unedited automatic A and A+B builds from the selected source, each with confirmed publication, matching actual export, full trace and repeated customer evaluation. For A+B, evaluate all thirteen public cases rather than only the three selected by `--brief B`. Then challenge changed APIs, policy limits, raw-material amendments, natural styles and varied call dates. Repeat acceptance after replacement builds; built-in one-repetition public practice is quick feedback rather than final reliability evidence.

WhatsApp, knowledge-base content, extra integrations, insight dashboards and acoustic barge-in are not listed as required deliverables in the inspected contract. The competition evaluates text conversations. They may matter for later real-world deployment, but they should not displace automatic-builder correctness, export integrity and repeatable customer success.
