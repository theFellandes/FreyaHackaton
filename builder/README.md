# builder: package map

The builder reads a bank's brief files and tool API file and produces a published Freya phone agent. The command is
`python builder/build.py <brief>... <tool_api>`; [`build.py`](build.py) wires the pieces below and runs the
[LangGraph pipeline](graph/README.md). How the pieces fit together is in [docs/HOW_IT_WORKS.md](../docs/HOW_IT_WORKS.md).

```
ingest -> understand -> policy (IR) -> compiler -> checks -> guardrails -> freya -> selftest
                 \_ llm (key changer) _/                                     \_ trace + observability
```

## Packages

| Package | What it does |
|---|---|
| [`graph/`](graph/README.md) | The LangGraph pipeline: state, context, routing, one module per node |
| [`ingest/`](ingest/) | Reads the inputs: `brief_loader` (text, Markdown, mail, HTML, tables, optional PDF) and `tool_api` (OpenAPI or Swagger to typed tool contracts) |
| [`understand/`](understand/) | Brief to rule ledger (`ledger`), grounding of every quote (`grounding`), ledger to policy (`extract`), validation (`validate`), safe fixes (`fixes`) and model repair (`repair`), node prose (`prose`) |
| [`policy/`](policy/) | The typed policy `PolicyIR` and the rule ledger (`ir`), plus plain reference code for business-day dates (`calendar`), cent arithmetic (`money`), call outcomes (`outcomes`) and intents |
| [`compiler/`](compiler/) | `PolicyIR`, tool API and ledger become the Freya workflow: generated code nodes (`codegen`), hooks, tools, prompts, the two profiles (`profiles/staged.py`, `profiles/lean.py`), editor layout |
| [`checks/`](checks/) | Offline checks of a workflow: lint rules L001 to L029 (`lint`), rule coverage (`coverage`), a sandbox that runs the generated Python (`code_sandbox`) |
| [`guardrails/`](guardrails/) | Deterministic guards for inputs, policy, prose and workflow, and the preamble every model call carries |
| [`llm/`](llm/) | The key changer: one gateway for every model call, key pools and dead-key memory, provider adapters, error classes, client-side rate limits |
| [`freya/`](freya/) | Everything on the Freya platform: a typed client over the kit's `freya.py`, deadline-bounded HTTP, polite pacing, the practice harness, stored-workflow diff |
| [`selftest/`](selftest/) | Tests of the deployed agent: smoke chat, walkthrough audit, the practice cases |
| [`agentic/`](agentic/) | The fallback track: a Claude Agent SDK session that writes the workflow file |
| [`observability/`](observability/) | The local LLM log (always on) and optional Langfuse |
| [`prompts/`](prompts/) | Versioned Jinja2 templates of the builder's own model calls, and of the agent prompts (`agent/`) |

## Modules at the top level

| Module | What it does |
|---|---|
| [`build.py`](build.py) | The command line, the wall-clock guard, and the single JSON result line on stdout |
| [`_startup.py`](_startup.py), [`_compat.py`](_compat.py) | Standard-library-only checks that run before any dependency is imported (interpreter version, missing packages) |
| [`settings.py`](settings.py) | Typed configuration from the environment and `.env` |
| [`inputs.py`](inputs.py) | Which command-line file is the tool API, which are briefs, and the slot name |
| [`budget.py`](budget.py) | The wall-clock budget and the stage estimates routing uses |
| [`trace.py`](trace.py) | The run trace: a readable `.log` and a `.jsonl` twin, with secrets masked |
| [`paths.py`](paths.py) | Well-known folders (`out/`, `traces/`, `results/`, `data/`) |
| [`testing.py`](testing.py) | Offline test doubles: `FixtureGateway` (model answers from files) and `FakeFreya` |
| [`viz.py`](viz.py) | Mermaid diagrams of the builder graph and of a generated workflow (`python -m builder.viz`) |
| [`submission.py`](submission.py) | Prepares and checks the local submission draft; never touches git or the network |
| [`diagnostics.py`](diagnostics.py), [`robustness.py`](robustness.py), [`monitor_views.py`](monitor_views.py), [`playground.py`](playground.py), [`playground_views.py`](playground_views.py) | Helpers of the Streamlit playground (`streamlit_app.py`): offline views and checks |

## Rules the code follows

- **Nothing brief-specific here.** Dates, amounts, outcome names and sentences reach the compiler only through the `PolicyIR`
  and the tool API. `tests/unit/test_compliance.py` scans this folder for brief literals.
- **Fields are found by role.** The compiler reads field names, flags and enums from the tool API file, never from constants
  ([`compiler/roles.py`](compiler/roles.py)).
- **Never crash, always close the trace.** Every graph node catches its own errors and routes to `finalize`.
- **No secrets in output.** Traces, the LLM log and the workflow are masked and checked for key-like text.

## Where to look

| Question | Start with |
|---|---|
| What does a node do, and where does it go next? | [`graph/README.md`](graph/README.md), [docs/GRAPH.md](../docs/GRAPH.md) |
| How is a brief understood? | [docs/UNDERSTANDING.md](../docs/UNDERSTANDING.md) |
| What does the generated agent look like, and why? | [docs/COMPILER.md](../docs/COMPILER.md), [docs/graphs/](../docs/graphs/README.md) |
| What stops a bad input or output? | [docs/GUARDRAILS.md](../docs/GUARDRAILS.md) |
| How do keys, providers and rate limits work? | [docs/LLM_KEY_CHANGER.md](../docs/LLM_KEY_CHANGER.md) |
| Where do traces and logs go? | [docs/OBSERVABILITY.md](../docs/OBSERVABILITY.md) |
| How do I change the code? | [docs/DEVELOPER_GUIDE.md](../docs/DEVELOPER_GUIDE.md) |
