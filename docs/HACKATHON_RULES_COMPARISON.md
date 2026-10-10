# Project assessment against the official hackathon

**The project has a good engineering foundation and addresses the correct deliverable. Its current evidence supports a substantial builder in development; submission readiness and competitive customer reliability remain unverified.** More dashboard features would not resolve the main uncertainty: whether an automatic build produces an exported agent that repeatedly passes the official scorer.

This comparison was made on **10 October 2026**, using current source, saved results and the live [official hackathon page](https://www.freyavoice.ai/hackathon#rules). The page was read in the browser after the web fetcher failed. Its Overview, Data, Evaluation, Submit, Rules and FAQ tabs were inspected. The linked [version 3 kit](https://www.freyavoice.ai/hackathon/build-the-builder-v3.zip) was downloaded, and eleven relevant organizer files matched the repository byte for byte by SHA-256: README, both briefs, OpenAPI, practice cases, Freya client, three platform documents and two scoring tools. The repository names the kit README [KIT_README.md](../KIT_README.md).

The [kit comparison](../.cache/project-audit/hackathon-rules/kit-comparison.json) and [current source/result snapshot](../.cache/project-audit/hackathon-rules/current-evidence.json) preserve this check locally. They are ignored audit artifacts, not a submission package. Source was changing during review, so historical findings below are distinguished from current behavior.

## What is good about the project

The architecture fits the event's core problem. [Input ingestion](../builder/ingest/brief_loader.py), a source-grounded rule ledger, [typed policy](../builder/policy/ir.py), deterministic compilation and [graph orchestration](../builder/graph/build_graph.py) form an automatic interpretation-to-workflow pipeline. That is a concrete implementation of the required builder.

Policy calculations become runtime code and hooks rather than relying entirely on conversational instructions. With the public contract, identity routing gates account access, calendar and money code prepare permitted terms, and closing logic handles multiple products. The ledger and generated artifacts make interpretation and enforcement locations inspectable. These are useful engineering strengths, although a rule-to-location map does not establish correct execution.

Testing and diagnosis are substantial. A focused run during this review of the hook, code-generation, staged/lean compiler, compiler-lessons, tool-builder and policy-validation tests produced **366 passed in 10.92 seconds**, using Python 3.12.14, disabled plugin autoload and explicit `pytest_asyncio.plugin`. The earlier staged-transition assertion now expects `promise_fix` before `promise_done`. This focused result does not replace the earlier full-suite snapshot, certify edits made afterward or invalidate counterexamples outside those assertions.

The current deployment implementation has also improved: publication confirmation, export retry and identity checking, comparison with read-back when available, atomic export replacement and stale-target deletion are present. Deadline handling now uses cancellation tokens, daemon workers and deployment snapshots. These changes deserve credit; their complete live behavior and submission timing were not demonstrated by this comparison.

## Requirement comparison

The contract below comes from the verified [kit](../KIT_README.md) and associated organizer files. “Implemented” describes source mechanisms; “verified” requires the relevant execution evidence.

| Requirement | Current project fit | Assessment |
|---|---|---|
| One command reads a brief and its API, then creates an agent without hand edits | [CLI](../builder/build.py) and graph implement extraction, validation, compilation, deployment, testing and repair. | Strong architectural alignment. A complete automatic published run is still missing from the inspected manifests. |
| Handle unfamiliar specification and raw bank material | Ledger and policy extraction support source grounding and amendments. Clean and raw-material dry-run artifacts exist. | Useful mechanisms and compilation evidence; unfamiliar-document interpretation needs independent acceptance. |
| Changed API fields, enums, identity questions, limits and calendars | OpenAPI ingestion and typed policy capture these concepts. | Partial generalization. Current synthetic probes still reproduce hardcoded field/response assumptions and changed-limit conflicts. |
| Keep the organizer's phone-agent model | [Freya client](../builder/freya/client.py), kit client and settings preserve/enforce the fixed model. | Implemented in source. Reserved agent-model override variables must stay unset. |
| Agent calls only the supplied bank API | Final workflow guards reject off-host/nonliteral API URLs and secrets; generated tools use the supplied contract. | Good protection, with incomplete endpoint/method validation. Unknown on-host paths and some runtime rewrites are warnings. |
| Reuse workflows, publish and submit actual exports | Slot reuse and improved publication/export paths exist. | Improved source behavior. A zero exit still does not guarantee a usable export or an uncut build. |
| Finish within 30 minutes including self-tests | Default budget is 28 minutes, with guard and in-flight allowances. | Intended default leaves headroom; current end-to-end timing remains unmeasured. Larger configured budgets can exceed the event limit. |
| Trace each run's timing, model/purpose calls, created objects and tests | [Trace writer](../builder/trace.py) records these facts, including interrupted tests. | Implemented. Final trace must correspond to the submitted exported version and exclude secrets. |
| Correct bank records, conversation policy and honest status | Generated routes, hooks, prompts and outcome computation address all three. | Enforcement gaps remain, particularly acknowledgment, confirmation, duplicate actions and API semantics. |
| Reliable natural conversation, multiple products and call dates | Policy supports multiple products and uses the supplied call date; official styles are available. | Current automatic-agent acceptance across styles, dates and repetitions is not established. |
| Reproduce from the submitted commit | Builder, dependencies and packaging exist in the working tree. | Current HEAD does not capture most implementation files. A clean-checkout reproduction is required before submission; nothing was committed for this review. |

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
| Disputed debt | No argument or promise for that product; dispute outcome/note according to the brief. |
| Restructuring or human/support request | When the prescribed conditions are met, use the correct transfer queue and reason; retain earlier recorded promises and omit unrecorded agreements. |
| Recording failure | Only permitted retries, truthful status, appropriate error handling and preservation of other acknowledged products. |
| Several products | Independent deadlines/attempts, required combined readback, exact notes and correct overall outcome precedence. |
| Any completed call | Exactly one `close_call`, as the final tool call. |

Brief B also prohibits notes before or after any transfer. If no promise exists, its closing reason depends on the first non-paid product in bank order. The [ledger scorer](../tools/score.py) compares complete record lists, including duplicates; the [harness](../tools/run_cases.py) also requires transcript policy and honesty checks.

The live [Evaluation tab](https://www.freyavoice.ai/hackathon#evaluation) explicitly says the mock API can record invalid terms. The bank will not enforce the brief for the agent. The final uses two unfamiliar briefs, thirty customers each, varied call dates and three repetitions; every repetition must pass. This makes generic enforcement and consistent conversation behavior central to the score.

## What the saved results prove

At the captured source/result snapshot, thirteen top-level builder manifests contained eleven dry runs and two failures; none contained a workflow ID. No completed official customer report tied to a current automatically built, published workflow and matching export was established. This is a limit of the inspected evidence, not a claim that every external platform run failed.

Saved hand-authored reference runs establish useful development behavior:

| Report | Customers passing every repetition | Repetitions |
|---|---:|---:|
| [A v8 normal](../results/ref_a_v8_reps3.json) | 9/10 | 3 |
| [A v10 talkative](../results/ref_a_v10_talkative.json) | 6/10 | 2 |
| [A+B v5 normal](../results/ref_ab_v5_reps3.json) | 3/3 | 3 |
| [A+B v8 talkative](../results/ref_ab_v8_talkative.json) | 0/3 | 2 |

These are different reference versions and conditions, not a single current performance curve. The A v10 talkative failures include callback completion, date negotiation and repeated hardship handling. Newer A normal and A+B talkative log files end with platform errors before final reports; partial progress is not a completed strict score. The [reference source](../evals/reference/brief_a_ref.py) identifies itself as a development fixture with public-brief values intentionally embedded.

## What prevents a stronger verdict

Current compiler probes still reproduce renamed-field and response-flag problems, empty-date enforcement bypass, long-window truncation, opaque-ID corruption, changed-limit conflicts and notes entering state before acknowledgment. Confirmation and immutable once-only action rules still lack deterministic gates. The [repair backlog](FIXING_ISSUES.md) and [conversation review](BANKING_CONVERSATION_PRACTICES.md) explain the counterexamples and acceptance criteria.

A follow-up at 23:35:40 Istanbul rechecked the newly changed validation source: missing persuasion steps still caused zero validation errors, and synthetic compilation still generated identical successive offers. That targeted check did not rerun the broader suite.

Historical publication/export and cancellation findings need a refreshed interpretation. New source addresses significant parts of F05 and F08. Remaining source-level concerns include `deployed`/exit zero despite missing export or cutoff, and an export check limited to workflow identity when read-back fails. These are distinct from the old local-export fallback and stale-file defects; current live acceptance should evaluate the new implementation.

The highest-value next evidence is one complete, unedited automatic A build and one A+B build, each with confirmed publication, matching actual export, full trace and strict customer evaluation of the same published version represented by that verified export. Then challenge changed APIs, policy limits, raw-material amendments, natural styles and varied call dates. Repeat that acceptance for any replacement version after repair; built-in one-repetition public practice is quick feedback rather than final reliability evidence.

WhatsApp, knowledge-base content, extra integrations, insight dashboards and acoustic barge-in are not listed as required deliverables in the inspected contract. The competition evaluates text conversations. They may matter for later real-world deployment, but they should not displace automatic-builder correctness, export integrity and repeatable customer success.
