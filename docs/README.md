# Project documentation

The Freya Collections Builder reads bank brief files and a tool API specification, then produces a Freya phone workflow. These documents explain how the project was developed, how its components fit together, and how to interpret its validation results. Choose a route below according to the work you want to do.

## Choose a starting point

- **Understand how we built it:** read [Project story](PROJECT_STORY.md) for the problem, development approach, design choices, and the relationship between reference agents and the automatic builder.
- **Install and reproduce a run:** use the [repository README](../README.md) for dependencies, configuration, command options, offline fixture runs, generated files, and deployment commands. A fixture run exercises the pipeline with supplied model answers; a real model run also tests extraction from the input documents.
- **Change the implementation:** start with the [Developer guide](DEVELOPER_GUIDE.md), then inspect the relevant modules and tests. It maps responsibilities and explains how changes to policy fields, prompts, compiler behavior, and checks connect.
- **Understand the evidence and remaining work:** read [Validation and lessons](VALIDATION_AND_LESSONS.md). Use it to distinguish structural checks, synthetic probes, saved practice results, and final evaluation readiness.
- **Repair the detected issues:** use [Fixing issues](FIXING_ISSUES.md) for priorities, proposed changes and the checks required to close each finding.
- **Test difficult customer behavior:** read [Customer edge cases](EDGE_CASES.md) for birthday verification, partial answers, corrections and the broader acceptance matrix.
- **Improve banking conversations:** use [Banking conversation practices](BANKING_CONVERSATION_PRACTICES.md) for sourced guidance on persuasion, repetition, barge-in, confirmation, accessibility and 36 additional acceptance scenarios.
- **Assess the entry against the event:** read [Official rules comparison](HACKATHON_RULES_COMPARISON.md) for the fetched organizer contract, desired outcomes, current strengths, newer runtime changes and remaining submission evidence.
- **Prioritize a competitive edge:** read [Competitive edge and internal logging](COMPETITIVE_EDGE_AND_LOGGING.md) for difficult acceptance scenarios, existing logging, evidence gaps and the value of an optional backend.

## Follow the builder pipeline

The module guides provide focused explanations. Their linked source files are useful entry points when tracing actual behavior.

| Area | Guide | Source entry points |
|---|---|---|
| Orchestration | [Builder graph](GRAPH.md) explains stages, state, routing, repair loops, and fallback. | [CLI](../builder/build.py), [graph assembly](../builder/graph/build_graph.py), [routing](../builder/graph/routing.py) |
| Understanding | [Understanding a brief](UNDERSTANDING.md) explains quoted rules, grounding, amendments, policy extraction, validation, and repair. | [brief loader](../builder/ingest/brief_loader.py), [tool API parser](../builder/ingest/tool_api.py), [ledger](../builder/understand/ledger.py), [policy model](../builder/policy/ir.py) |
| Compilation | [Compiler](COMPILER.md) explains workflow profiles, generated code, prompts, tools, hooks, and rule coverage. | [compiler](../builder/compiler/compile.py), [staged profile](../builder/compiler/profiles/staged.py), [hooks](../builder/compiler/hooks.py), [lint](../builder/checks/lint.py) |
| Guarding inputs and outputs | [Guardrails](GUARDRAILS.md) describes checks for source grounding, tool constraints, injection markers, and secrets. | [input guard](../builder/guardrails/input_guard.py), [output guard](../builder/guardrails/output_guard.py) |
| Model access | [LLM key changer](LLM_KEY_CHANGER.md) explains credential discovery, provider fallback, retry decisions, structured output, and deadlines. | [gateway](../builder/llm/gateway.py), [providers](../builder/llm/providers.py), [keys](../builder/llm/keys.py) |
| Observability | [Observability and rate limiting](OBSERVABILITY.md) explains traces, prompt and reply logs, optional Langfuse integration, and pacing. | [trace writer](../builder/trace.py), [observer factory](../builder/observability/factory.py), [Freya client](../builder/freya/client.py) |

## Read validation results in context

The [tests](../tests/) include unit checks and graph runs with fake collaborators. The [evaluation tools](../evals/) and [saved results](../results/) serve a different purpose: inspecting agent behavior and individual failures. A passing local check, a deployed workflow, and a customer passing every required repetition establish different things. Follow the validation guide when comparing them, and keep each result attached to its workflow, run, and test conditions.

## Use the right source of authority

The [official kit README](../KIT_README.md) governs the hackathon contract. The organizer references describe the platform: [API](api.md), [workflow format](workflow-format.md), and [tools and code](tools-and-code.md). Read those before interpreting a project design choice as an event requirement.

For implemented behavior, inspect the current source and tests alongside the module guides. Saved exports and traces establish what a particular run produced. Documentation describes the design and its evidence; it does not certify that every possible brief or conversation is handled.

[Architecture](ARCHITECTURE.md), [Implementation plan](IMPLEMENTATION_PLAN.md), [Tasks](TASKS.md), and [Folder structure](FOLDER_STRUCTURE.md) preserve earlier planning. They help explain decisions, but planned milestones and proposed layouts are not a completion checklist. The [research notes](research/) preserve platform observations and background reading; confirm their conclusions against the official kit and current implementation before using them to make a change.
