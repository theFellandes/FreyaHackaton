# Validation and lessons from building the Freya collections builder

The project uses several kinds of evidence to develop an automatic builder: official practice runs of a hand-authored reference agent, tests of policy calculations and generated code, controlled runs with fake models and APIs, and a real model dry run. Together they show substantial implementation progress. They do not yet establish reliable customer handling by the current automatically generated, published workflow.

This distinction matters because a planned feature, an implemented mechanism and a verified behavior are different claims. The evidence below is a development snapshot from 10 October 2026. Source changes continued during the audit, so later changes require their own verification.

## What the hackathon requires

The deliverable is a program that reads unfamiliar bank briefs and their tool API, then creates the phone agent without a manual editing step. Final evaluation checks bank records, conversation policy and honesty. Customers may have up to three products, speak naturally and call on different calendar days. A customer counts only when all three repetitions pass. The exported workflow is what the organisers load and score. These requirements come from the [official kit](../KIT_README.md), especially its scoring, final and rules sections.

The builder therefore needs to demonstrate both interpretation of the brief and correct agent execution. A locally valid workflow establishes neither a successful bank transaction nor correct spoken confirmation. Likewise, a good reference agent establishes useful platform behavior without proving that the builder can reproduce it from new material.

## Learning from the reference agent

Development began by measuring the kit base agent with the official harness. The saved baseline was 0 of 13 customers, with failures involving callbacks, identity, dates, required wording, retries and multiple products. The kit's own reported result differed, illustrating why one run is insufficient. [Baseline measurements](../results/BASELINE.md) record the individual failures and the distinction between the example agent and builder.

The [reference implementation](../evals/reference/brief_a_ref.py) deliberately contains public-brief facts. It is a development fixture, explicitly described as hand-authored and never submitted. It lets us explore deterministic code, tool hooks and routing before reproducing those mechanisms through the policy model and compiler.

Selected completed reports show both progress and remaining variance:

| Saved reference report | Customers passing every repetition | Repetitions |
|---|---:|---:|
| [Brief A normal v8](../results/ref_a_v8_reps3.json) | 9 of 10 | 3 |
| [Brief A talkative v8](../results/ref_a_v8_talkative.json) | 9 of 10 | 2 |
| [Brief A+B normal v5](../results/ref_ab_v5_reps3.json) | 3 of 3 | 3 |
| [Brief A+B talkative v8](../results/ref_ab_v8_talkative.json) | 0 of 3 | 2 |

The last report is especially instructive: every customer passed one repetition and failed the other. A green final repetition can conceal a strict score of zero. Filenames identify the recorded development runs; these results do not establish the behavior of subsequently modified reference or compiler source.

## What offline tests establish

The test suite exercises calendar and amount calculations, identity and tool failures, generated code, graph routing, malformed inputs, credential redaction and recovery behavior. Generated workflow snippets run in a [local sandbox](../builder/checks/code_sandbox.py). Some expectations use explicit dates or outcomes; other tests compare generated behavior with the project's reference policy helpers. The latter can detect disagreement while missing a mistake shared by both implementations.

[FixtureGateway and FakeFreya](../builder/testing.py) make complete graph runs reproducible without spending credits. Fixtures provide expected ledger and policy responses, while the fake platform provides validation, saving, export and chat responses. The [understanding tests](../tests/unit/test_understand_fake_llm.py) explicitly test orchestration around these answers. A passing fake-model run verifies those mechanics; it does not show that a real model interprets an unfamiliar memo correctly.

The [test safety fixtures](../tests/conftest.py) remove developer configuration and credentials and block non-loopback socket connections. Graph fixtures also prevent an accidental real coding-agent session. These controls help make offline results independent of accounts and network availability.

## The real model dry run

The [saved Brief A trace](../traces/brief-a-20261010T192451Z.log) records six successful OpenAI model calls and a 93.4-second build. The pipeline extracted a ledger and policy, drafted prose and compiled a workflow with 30 nodes. Static checks reported zero errors and one warning about a long negotiation prompt.

The [result file](../results/brief-a-2597e9130d4d.build.json) says `dry_run`, has no workflow or agent ID, and records no deployed self-tests. Its one passing test is the static check. The local workflow is therefore evidence of successful real-model compilation, rather than a platform export or a passing customer simulation. Compiler and understanding code changed after this run; the trace cannot validate those later revisions.

## The dated test snapshot

An offline suite run started at **22:33:59 Istanbul time on 10 October 2026**, using Python 3.12.14 and pytest 9.1.1, with plugin autoload disabled and the asyncio plugin explicitly loaded. It completed with **1,950 passed, 8 failed and 2 skipped** tests in approximately 41 seconds.

Seven failures were in [agentic tests](../tests/graph/test_agentic.py), where the environment lacked the optional Claude Agent SDK and mocks did not fully isolate availability checks. One failure in [staged compiler tests](../tests/unit/test_compile_staged.py) expected the first negotiation transition to target `promise_done`; the implementation targeted `promise_fix`. This is a test and implementation mismatch requiring review, not evidence by itself that either route is correct.

Both skips were real Langfuse SDK compatibility tests in [observability tests](../tests/unit/test_observability.py), because Langfuse was absent. Ruff passed for `builder` and `tests`; extending the check to `evals` found an unused suppression in `triage.py`. The [local JUnit report](../.cache/project-audit/pytest-full.xml) preserves this snapshot but lives in an ignored cache and is not a portable submission artifact. These counts describe that run, not an enduring current status.

## Lessons from targeted audits

Two small offline probes exposed limits in the validation tools. The [walkthrough auditor](../builder/selftest/walkthrough.py) accepted an empty report with `checked=0` and claimed all active rules were audited. An audit should distinguish completed checks from an empty response before claiming success.

The [triage command](../evals/triage.py) reads only the last repetition's `rows`. On the saved A+B talkative report, its default failure view printed nothing because that repetition passed, although all three customers failed the strict result. Diagnosis should use `final` to select failed customers and `all_rows` to inspect the failing repetitions.

The [coverage report](../builder/checks/coverage.py) counts rules with a mapped workflow location. That mapping supports traceability; it does not independently prove that the location enforces the rule correctly. Similarly, the [practice self-test](../builder/selftest/practice.py) runs one repetition with default style when applicable and affordable. Its purpose is quick feedback within the build budget, leaving stronger acceptance checks necessary.

## Compiler and runtime counterexamples

The audit also used small synthetic policies, tool responses and fake platform calls to challenge assumptions outside the public practice cases. The table records observed results, not fixes. The [local audit manifest](../.cache/project-audit/assessment.json) and compiler and runtime reproduction scripts in the same ignored directory preserve the inputs and outputs on this machine. Those files are local development evidence and need deliberate packaging if they are to accompany a shared report.

| Area | Observed counterexample | Implementation to review |
|---|---|---|
| Changed API fields | Renaming the closing outcome field to `result_code` produced missing/unknown field lint errors. Changing a success flag from `ok` to `success` made a successful promise response look like failure, with no lint error. | [Tool construction](../builder/compiler/tools.py), [response hooks](../builder/compiler/hooks.py) |
| Date boundaries | An empty allowed-date list let an after-deadline date through. A separate 120-day enumeration limit rejected a valid date under a longer synthetic deadline. | [Generated date code](../builder/compiler/codegen.py), [request hooks](../builder/compiler/hooks.py) |
| Opaque product identifiers | A supplied `ABC123` identifier became `abc123` and was rejected as an unknown product. | [Request hooks](../builder/compiler/hooks.py) |
| Failure semantics | A zero-retry policy still produced an instruction to retry once. A failed note call had already updated local note state and affected the closing reason. | [Prompts](../builder/compiler/prompts.py), [hooks](../builder/compiler/hooks.py) |
| Publication status | A fake save response with `published=false` was reported as `deployed`. | [Deployment](../builder/graph/nodes/deploy.py), [finalization](../builder/graph/nodes/finalize.py) |
| Export integrity | An export failure could report the local pre-platform workflow as the exported file. After a repair, the reported version could be version two while the retained export still contained version one. | [Deployment](../builder/graph/nodes/deploy.py), [finalization](../builder/graph/nodes/finalize.py) |
| Cancellation | A synchronous save completed in its worker thread after graph cancellation. The probe used a 0.03-second guard and a 0.25-second operation; it did not demonstrate an actual 30-minute overrun. | [Build entry point](../builder/build.py), [deployment](../builder/graph/nodes/deploy.py) |
| Error redaction | A synthetic configured secret was absent from the trace but present in ordinary Python logging and a returned error. No exposure of a real key was established. | [Freya client](../builder/freya/client.py), [trace writer](../builder/trace.py), [finalization](../builder/graph/nodes/finalize.py) |

These counterexamples show why structural validity and public-brief success cannot establish generalization. Changed contracts, empty sets, identifiers and failed side effects deserve explicit regression checks. They also show why an acceptance record should confirm publication and match its export to the version actually scored.

## Remaining acceptance milestones

The next milestone is a complete current-source builder run that publishes an agent, reads it back and exports it, preserving the input, source version, policy, workflow and trace together. Its generated agent should then face the official scorer with three repetitions, realistic customer styles and several call dates. Failures need examination of both bank records and transcripts.

Separate synthetic briefs and changed tool contracts should challenge interpretation and compilation before final briefs arrive. Regression tests should reproduce observed failures, including incomplete audits and failures hidden by a passing final repetition. Finally, the timed build and exported workflow should be exercised in the intended submission environment. These are remaining verification steps; the existing evidence does not establish that they have already passed.

Environment and packaging remain part of that work. During the audit, the main `.venv` could not launch its configured Python executable; the test snapshot used `.venv-playground`. Docker was not built or run. Most implementation files were also untracked at the inspected Git HEAD, so that commit alone did not reproduce the working project. This documentation work leaves staging and committing to a later step.

## Later official-contract comparison

The [official-rules comparison](HACKATHON_RULES_COMPARISON.md) refreshed the source and organizer material later on 10 October. Eleven relevant files in the downloaded version 3 kit matched the repository exactly. Current publication, export and cancellation code has changed since the historical runtime probes above; those old observations must not be treated as newly reproduced current failures.

A focused current compiler run passed 366 tests in 10.92 seconds, including the corrected staged-route assertion. Current compiler and identity counterexamples still reproduce. The refreshed report also includes the newer A v10 talkative reference result, 6/10 across two repetitions. No completed current automatic published-builder score was established by the inspected saved artifacts. This narrower evidence supplements the earlier full-suite snapshot without replacing it.
