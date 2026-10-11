# Documentation index

One line per document. Read the first group in order; use the others when you need them.

## Start here (in this order)

1. [README](../README.md): what the builder is, current honest status, quick start.
2. [Final runbook](FINAL_RUNBOOK.md): the hidden-brief day procedure, step by step, with UTC and Istanbul times.
3. [How it works](HOW_IT_WORKS.md): the two planes (builder and phone agent), the pipeline and the diagrams.

## Operations

- [Submission](SUBMISSION.md): what the entry consists of and how the submission helper (`builder.submission`) checks it.
- [Submission folder](../submission/README.md): the local draft, the form example and the email draft.
- [Evals](../evals/README.md): the official scorer `tools/run_cases.py`, the reference agent and failure triage.
- [Extra live cases](../evals/extra_cases/README.md): 13 extra scenarios on the practice-bank customers.
- [Baseline measurements](../results/BASELINE.md): every live score, from the kit base agent to the builder's own agents.
- [LLM key changer](LLM_KEY_CHANGER.md): key discovery, provider fallback, dead keys, rate limits.
- [Observability](OBSERVABILITY.md): traces, the LLM log, logging privacy, optional Langfuse, Freya pacing.

## Technical reference

- [Developer guide](DEVELOPER_GUIDE.md): how the code is organised and how to change it safely.
- [Package map](../builder/README.md): one line per module of `builder/`.
- [Builder graph](GRAPH.md) and [graph package](../builder/graph/README.md): stages, state, routing, repair loops.
- [Understanding a brief](UNDERSTANDING.md): rule ledger, grounding, amendments, policy extraction and validation.
- [Compiler](COMPILER.md): profiles, generated code, prompts, tools, hooks and rule coverage.
- [Guardrails](GUARDRAILS.md): input and output guards (grounding, tool constraints, injection markers, secrets).
- [Diagrams](graphs/README.md): Mermaid sources generated from the code (`python -m builder.viz`).
- [Synthetic briefs](../evals/synthetic_briefs/README.md): invented briefs used to test generality offline.

## Quality and audits

- [Quality review](../results/QUALITY_REVIEW.md): closure report of the audit and fix rounds.
- [Fixing issues](FIXING_ISSUES.md): open findings, priorities and the checks that close each one.
- [Customer edge cases](EDGE_CASES.md): difficult customer behaviour and the acceptance matrix.
- [Critical gaps](CRITICAL-GAPS.md): gaps found by the external reviewer ("Sol"), 11 October.
- [Hidden-brief readiness](HIDDEN-BRIEF-READINESS.md): the external reviewer's checks under changed inputs, 11 October.
- [Validation and lessons](VALIDATION_AND_LESSONS.md): which kind of evidence proves what (snapshot of 10 October).
- [Submission validation](../submission/VALIDATION.md): dated offline verification records.
- [Codebase guide](codebase-guide/README.md): an external, generated walkthrough of the code. It is a snapshot and may
  lag the current code; trust the source and tests when they differ.

## Background

- [Project story](PROJECT_STORY.md): how the builder was developed and why (10 October).
- [Official rules comparison](HACKATHON_RULES_COMPARISON.md): the entry against the organisers' contract.
- [Banking conversation practices](BANKING_CONVERSATION_PRACTICES.md): sourced guidance on persuasion, confirmation and
  interruption.
- [Competitive edge and logging](COMPETITIVE_EDGE_AND_LOGGING.md): hard scenarios and logging gaps (10 October).
- [Research notes](research/): platform observations and background reading. `research/11_hackathon_rules_and_kit.md`
  summarises the rules and the kit.

## Historical planning documents

Planning-phase documents, kept for history. They describe intended designs, not the current code.

- [Architecture](ARCHITECTURE.md)
- [Folder structure](FOLDER_STRUCTURE.md)
- [Implementation plan](IMPLEMENTATION_PLAN.md)
- [Tasks](TASKS.md)

## Kit documents (organisers' material, never edited)

- [Kit README](../KIT_README.md): the hackathon contract and the kit contents.
- [API](api.md), [workflow format](workflow-format.md), [tools and code](tools-and-code.md): the Freya platform reference.

When a project document and a kit document disagree, the kit and the official page (<https://www.freyavoice.ai/hackathon>)
win.
