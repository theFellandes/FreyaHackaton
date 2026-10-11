# How we built the Freya Collections Builder

The project turns bank instructions and a tool API specification into a Freya collections workflow. The hackathon asks for a builder that can do this for unfamiliar briefs without a person editing the resulting agent. That requirement shaped the project: the reusable deliverable is the program that interprets and compiles a policy, while the public briefs and reference agents provide development examples.

This account describes the implementation and saved development evidence available on **10 October 2026** (status notes of 11 October are marked as such; current status is in [FIXING_ISSUES.md](FIXING_ISSUES.md) and [results/BASELINE.md](../results/BASELINE.md)). It explains the decisions visible in the repository without reconstructing an unsupported hour-by-hour history. The [validation and lessons document](VALIDATION_AND_LESSONS.md) records measured results and outstanding verification; the [developer guide](DEVELOPER_GUIDE.md) maps the current modules.

## Establishing the actual task

The official kit changed several assumptions from the early platform research. It supplied workflow creation, validation and publication APIs, seven bank tools, text-based practice simulations, and a call variable named `now`. The [kit research notes](research/11_hackathon_rules_and_kit.md) explicitly record these corrections. We therefore built against the organizer's workflow format and official scorer, and used the supplied call date for policy calculations.

The brief is more than a conversational script. It can specify identity questions, payment floors, business-day deadlines, holidays, callbacks, transfers, required wording and closing outcomes. An amendment can change an earlier instruction, and a customer can have several products. The associated API is part of the input too: request fields and allowed values can change. The [official kit](../KIT_README.md) is the source for those requirements; early research and architecture proposals remain background material.

That led to two distinct responsibilities. The builder runs in Python and can read documents, call models, validate structures and operate the Freya API. The deployed agent runs in Freya and must receive its behavior through the generated nodes, prompts, routes, tools and code hooks. A Python helper available during construction cannot enforce a conversation rule unless the compiler translates the mechanism into the runtime workflow.

## Measuring the starting point

We used the official practice harness to examine the supplied base agent. The [saved baseline](../results/BASELINE.md) records one local run scoring **0 of 13 customers**. Failures included accepting dates after a deadline, missing callback or transfer actions, incorrect closing outcomes, missing required sentences and incomplete handling of multiple products. The kit reported a different result for its own run, so this measurement is a local baseline rather than a universal score.

The baseline exposed the importance of checking both the conversation and the bank's records. Saying that a promise has been recorded does not make it true; a successful tool response and the appropriate ledger entry matter. Likewise, a technically successful tool call can accompany a conversation that violates the brief.

The example SDK builder could not provide a local baseline because its CLI authentication failed. We recorded that blocked attempt instead of treating it as a successful comparison. The [research notes on τ^τ-bench](research/12_tau_tau_bench_lessons.md) also informed the development approach: inspect failures, retain useful mechanisms, compare against independent records and reserve time for validation. Proposed experiments in planning documents should not be read as completed experiments.

## Using a reference agent as a development fixture

The hand-authored [Brief A reference agent](../evals/reference/brief_a_ref.py) gave us a place to explore Freya's runtime behavior directly. Its public-brief values are deliberate: the file identifies itself as a development fixture that is never submitted. It helped investigate staged conversation flow, identity attempt handling, date and amount calculations, tool response processing and closing behavior.

The compiler then needed reusable versions of those mechanisms. Tests compare selected generated behavior with reference helpers, making it easier to notice when the two implementations diverge. Agreement is useful evidence, but both can share an incorrect assumption. Explicit boundary cases and the official bank ledger remain necessary checks.

Reference-agent results describe that hand-authored workflow. They do not establish that the automatic builder extracted the same policy or generated the same behavior. The saved reports include strong normal-style results and failures under a more talkative style; the validation document keeps their versions and repetitions attached to each score.

## Making interpretation inspectable

We split understanding into a **rule ledger** and a **typed policy model**. The ledger captures individual instructions with source quotations, scope and amendment relationships. This provides a record of what the model understood before those instructions become executable behavior. See the [ledger implementation](../builder/understand/ledger.py) and [understanding guide](UNDERSTANDING.md).

The `PolicyIR` then organizes that interpretation into fields the compiler can use: identity checks, calendars, account bands, amount rules, intents and outcomes. [Policy validation](../builder/understand/validate.py) checks consistency, while guards check grounding and other constraints before compilation. Repair steps receive feedback when extraction or validation fails.

This intermediate representation makes changes easier to follow. A bank-specific holiday belongs in the extracted calendar; a generic business-day calculation belongs in reusable code. A required sentence can retain its wording, while a closing outcome becomes a structured value. The model is still responsible for interpretation, and a typed result can still omit or misunderstand a rule. The ledger and checks make those errors more inspectable rather than eliminating them.

## Compiling the policy into runtime behavior

The [compiler entry point](../builder/compiler/compile.py) accepts the policy, tool API, ledger, optional node prose and selected profile. It performs no model calls or external I/O. Models can draft node prose beforehand; the compiler assembles the workflow and a rule-to-mechanism map from the supplied inputs.

That gives a precise determinism boundary: fixed policy, API, ledger, prose and profile inputs produce deterministic compilation. Reading the same brief again can yield different model extraction or prose, so the entire build is not guaranteed to reproduce identical JSON.

The default [staged profile](../builder/compiler/profiles/staged.py) separates parts of the conversation and uses generated code and routes for policy facts and closing behavior. A [lean profile](../builder/compiler/profiles/lean.py) provides a smaller graph and gives the model more closing responsibility. Both exist in the implementation; the repository does not establish that a completed comparative experiment proved one superior.

[Generated code](../builder/compiler/codegen.py) handles calendar and monetary facts so the conversation model can work from prepared values. [Tool hooks](../builder/compiler/hooks.py) inspect requests and responses and update runtime state. The workflow includes a `promise_fix` path for correcting invalid promise requests blocked before the bank call, alongside handling for service failures. These mechanisms still require behavioral verification, particularly when the input API differs from the public kit.

The compiler's rule map connects ledger entries to workflow locations. It is a traceability aid. A mapping entry shows where enforcement is intended; it does not prove that the code or prompt implements the rule correctly. The [compiler guide](COMPILER.md) explains the profiles and generated structures in more detail.

## Connecting the stages with LangGraph

The [builder graph](../builder/graph/build_graph.py) coordinates the pipeline. Its main path loads and guards inputs, extracts the ledger and policy, validates and guards the policy, drafts prose, compiles, runs static checks, guards the workflow, validates remotely, deploys, self-tests and finalizes. Prose guarding occurs inside the prose-drafting step.

Repair routes let the pipeline revise the policy model or revisit compilation after feedback. With `--track auto` the builder can try a Claude Agent SDK fallback when compilation cannot finish and sufficient budget remains; the default `compiled` track does not. A dry run stops after local workflow guarding and skips remote validation and deployment. The [graph guide](GRAPH.md) documents the routing and state transitions.

The default budget is 28 minutes, leaving a margin within the kit's 30-minute limit. Optional work is selected with the remaining time in mind. Time accounting and cancellation are separate concerns: the 10 October audit found that a synchronous save running in a worker thread could continue after graph cancellation. Platform calls now run in abandonable daemon threads with deadline-capped request timeouts (11 October status), so the guard returns on time; a request already in flight gets a short bounded grace.

## Preserving useful evidence

The deployment code writes the proposed local workflow, saves through the Freya client, attempts to read the stored definition back and requests an export. Traces and result JSON record stages, checks, errors and identifiers. [Observability documentation](OBSERVABILITY.md) explains the logging and optional integrations.

Offline fixtures let us exercise graph behavior without accounts or platform credits. Unit checks also execute generated snippets and test policy calculations. A saved real-model Brief A dry run demonstrates that extraction and compilation can work together. The validation document explains why none of these alone certifies a published agent's customer score, and records shortcomings in publication status, export handling and error redaction found by synthetic audit probes.

The development approach has produced a substantial builder and a way to inspect its decisions. Later saved automatic builds were published and exported: A passed 8/10 customers over three repetitions, and A+B passed its three B amendment cases. Those runs predate the latest repairs; the ten inherited A cases remain untested on A+B. After Claude finished, independent offline verification passed 2,690 tests with three optional-package skips and clean lint on unchanged source. The remaining work is to bind a fresh live build, its actual export and repeated customer score to the selected source and published version. See [submission verification](../submission/VALIDATION.md) for the evidence and gaps. Installation commands are in the [repository README](../README.md), and the [documentation index](README.md) provides routes into each component.
