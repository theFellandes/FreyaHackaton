# Freya Collections Builder

A LangGraph **builder** for the Freya "Build the Builder" hackathon. It reads a bank's collections brief (a clean
specification, or raw bank material such as a mail, a policy extract and a memo) together with that bank's tool API
file, and it creates, tests and publishes a Freya phone agent on its own. No manual step, no hand edit afterwards.

```
brief(s) + tool API  ->  rule ledger  ->  policy IR  ->  compiled workflow  ->  checks  ->  Freya  ->  self-test  ->  trace
   (any bank)          (quoted rules)   (typed model)   (nodes, code, hooks)   (lint, guards)  (save, export)
```

Bank-specific dates, limits, outcome names and sentences are intended to come from each run's brief through a typed
policy model (`PolicyIR`). The compiler is deterministic for fixed policy, tool API, ledger, prose and profile inputs;
model extraction and prose can vary between builds. Its rule map records intended enforcement locations, while behavioral
tests are needed to establish that those mechanisms work.

Read [how we built it](docs/PROJECT_STORY.md) for the development approach, or use the
[documentation index](docs/README.md) to find the component guides and validation evidence.

- [Status](#status)
- [Hackathon contract](#hackathon-contract)
- [Quick start (native, with uv)](#quick-start-native-with-uv)
- [Quick start (Docker)](#quick-start-docker)
- [Configuration and the LLM key changer](#configuration-and-the-llm-key-changer)
- [Development commands](#development-commands)
- [What a run produces](#what-a-run-produces)
- [Submission](#submission)
- [Troubleshooting](#troubleshooting)
- [Interactive playground](#interactive-playground)
- [Documentation map](#documentation-map)

## Status

Evidence snapshot from **10 October 2026**. Later source changes require their own verification. See
[Validation and lessons](docs/VALIDATION_AND_LESSONS.md) for conditions, results and remaining acceptance work.

| Part | State |
|---|---|
| Offline unit and graph suite | Earlier full audit: 1,950 passed, 8 failed, 2 skipped. A later focused compiler run passed 366 tests; the staged-route assertion is corrected. That focused result does not replace the full-suite snapshot. `ruff check builder tests` passed in the earlier audit. |
| Fixture-based graph runs | Exercise orchestration and generated checks using supplied model answers and fake platform responses; they do not test real extraction or deployment. |
| Real-model builder dry run | Saved Brief A run used six successful model calls, took 93.4 seconds and produced a 30-node workflow with no static errors. It did not deploy or run customer simulations and predates later edits. |
| Live reference-agent practice runs | Saved Freya and mock-bank reports exist for hand-authored development agents. Their scores are separate from automatic builder results. |
| Current automatic builder acceptance | No completed live deployment and acceptance report was established by the audited evidence. Synthetic probes also found compiler and runtime issues requiring review. |

## Hackathon contract

- The organisers run one command per final brief: `python3 builder/build.py {brief_path} {tool_api_path}`. Several brief
  files may be given (Brief B builds on Brief A). A directory argument is expanded to its files.
- One run may take 30 minutes at most, including self-tests. The default budget is 28 minutes (`BUILD_TIME_BUDGET_S=1680`).
- The builder prints progress on stderr and, as the **last line of stdout**, one JSON line:
  `{"slot", "track", "workflow_id", "agent_id", "version", "exported", "trace", "status", ...}`.
  `status` is `deployed`, `deployed_unpublished`, `dry_run`, `blocked` or `failed`. Exit code 0 follows a reported
  deployment or clean dry run, and 2 signals an unsuccessful status. Also inspect `export_ok`, `cut_off`, errors and
  version identity: current publication/export handling has improved, but a zero exit alone does not establish a
  complete submission artifact. Handled failures are recorded in the trace and final JSON line.
- The phone agent may call only the bank's tool API. The agent's model is set by the platform (`AGENT_LLM` in `freya.py`).
  The builder must not change it (`FREYA_AGENT_PROVIDER` and `FREYA_AGENT_MODEL` stay unset).
- Nothing from a final brief may be written into the builder: no rule, value, date or name. After the final briefs open you
  may fix crashes only. `tests/unit/test_compliance.py` scans `builder/` for brief literals.
- The kit files (`freya.py`, `tools/`, `docs/workflow-format.md`, `docs/tools-and-code.md`, `docs/api.md`, `briefs/`,
  `examples/`, `public-test-set.json`) are the organisers' material. Do not edit them. See `KIT_README.md`.

## Quick start (native, with uv)

Python 3.11 to 3.13 (the repository pins 3.13 in `.python-version`) and [uv](https://docs.astral.sh/uv/).

```sh
# 1. dependencies: the runtime plus the Claude Agent SDK (agentic fallback), PDF input and the dev tools
uv sync --extra agentic --extra ingest --extra dev          # add --extra ui for the Streamlit page, --extra obs for Langfuse

# 2. configuration: copy the template and fill in what you have (the file .env is ignored by git)
cp .env.example .env                                        # PowerShell: Copy-Item .env.example .env
#    FREYA_API_KEY, FREYA_BANK_TEAM_KEY (a secret of 16+ characters you make up) and at least one LLM key

# 3. check everything locally, without Freya: the LLM reads the brief, the compiler builds the workflow, the checks run
uv run python builder/build.py briefs/brief-a.md tools/openapi.json --dry-run

# 4. the real run: validate, deploy (or update the slot in place), export, self-test
uv run python builder/build.py briefs/brief-a.md tools/openapi.json
uv run python builder/build.py briefs/brief-a.md briefs/brief-b.md tools/openapi.json     # Brief B builds on Brief A
```

Without a virtual-environment manager the same works from any interpreter that has the dependencies installed, for example
`.venv/Scripts/python.exe builder/build.py ...` on Windows or `.venv/bin/python builder/build.py ...` elsewhere. The organisers'
`python3` must be that interpreter, so activate the environment before the final run.

A run without `FREYA_API_KEY` is downgraded to a dry run (the trace says so). A run without any usable LLM key stops in
`understand_brief` with `All LLM providers failed`; the trace lists every provider and why it was skipped. See
[Configuration](#configuration-and-the-llm-key-changer).

Useful options of `builder/build.py` (`--help` lists them all):

| Option | Meaning |
|---|---|
| `--dry-run` | Compile and check locally. No Freya call at all. The workflow goes to `out/<slot>.workflow.json`. |
| `--slot NAME` | Workflow label on Freya. Default: derived from the brief file names (`brief-a`). The same slot is updated in place. |
| `--track auto\|compiled\|agentic` | `compiled` uses model interpretation followed by deterministic compilation. `agentic` is a Claude Agent SDK session that writes the workflow. `auto` (default) tries compiled first and falls back to agentic when time is left. |
| `--profile staged\|lean` | Workflow shape. `staged` (default) separates conversation stages. `lean` uses a smaller graph. |
| `--self-test auto\|none\|smoke\|walkthrough\|practice` | What runs after the deploy. `none` and `smoke` spend the fewest credits. |
| `--budget-s N` | Wall-clock budget in seconds (default 1680). |
| `--fake-llm DIR` | Answer every model call from fixture files in `DIR`. Offline, no keys. Forces the compiled track. |

## Quick start (Docker)

The image runs the same entry point as the native install. The kit files are baked in, `out/`, `traces/`, `results/`, `data/`
and `state/` are mounted from the host, and `.env` never enters the image (compose passes only the variables it lists).

```sh
docker compose build                                                        # image freya-builder:latest
docker compose run --rm builder briefs/brief-a.md tools/openapi.json --dry-run
docker compose run --rm builder briefs/brief-a.md tools/openapi.json        # build and deploy
docker compose run --rm builder briefs/brief-a.md briefs/brief-b.md tools/openapi.json --slot brief-b
docker compose run --rm builder inputs/my-brief.md inputs/my-tools.json --dry-run     # files from ./inputs (mounted read-only)
docker compose run --rm --build tests                                       # unit tests with no network
```

Put keys in `.env` next to `docker-compose.yml`; compose reads it only to fill the `${VAR}` references in the file. To use the
`claude_code` provider inside a container, set `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`) or mount your login as
described in `docker-compose.yml`. `docker-compose.langfuse.yml` starts a self-hosted Langfuse (see `docs/OBSERVABILITY.md`).

## Configuration and the LLM key changer

Everything is configured through environment variables or `.env`; real environment variables win. `.env.example` documents
every variable with its default. The important ones:

| Variable | Purpose |
|---|---|
| `FREYA_API_KEY` | Workspace key (starts with `fak_`). Required for anything but `--dry-run`. Works only against the staging host. |
| `FREYA_API_URL` | Platform base URL. Default `https://staging-app.freyavoice.ai`. |
| `FREYA_BANK_TEAM_KEY` | A secret of 16+ characters that you make up. Needed for practice runs. Use the same one every time. |
| `FREYA_WORKSPACE_ID` | Optional. Default: the first workspace the key can see. |
| `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` | LLM keys. Each provider also reads `_1` to `_20` suffixes and a comma-separated `*_API_KEYS` list. |
| `CLAUDE_CODE_OAUTH_TOKEN` | Optional. The `claude_code` provider otherwise uses the local Claude login. |
| `LLM_PROVIDERS` | Provider chain, tried in order. Default `anthropic,openai,claude_code`. |
| `*_STRONG_MODEL`, `*_FAST_MODEL` | Model per provider and role. |
| `BUILDER_TRACK`, `AGENT_PROFILE`, `SELF_TEST`, `BUILD_TIME_BUDGET_S`, `DRY_RUN` | Same as the command line options. |

**Key changer.** Every model call goes through one gateway (`builder/llm/`). A key without credits or an invalid key is marked
dead and skipped, a rate-limited key cools down, and the gateway moves to the next key of the provider, then to the next
provider in `LLM_PROVIDERS`. Dead keys are remembered in `data/key_health.json` (fingerprints only, never secrets) for six
hours, so later runs do not retry them. A provider with no usable key is skipped without a call; with only an OpenAI key in
`.env` the chain is simply `openai`. Details: `docs/LLM_KEY_CHANGER.md`.

**Cheap models by default.** The strong role (understanding and extraction) and the fast role (audits, prose) use the cheaper
tiers: `claude-sonnet-5-5` / `claude-haiku-5-5` for Anthropic and the Claude login, `gpt-5.6-terra` / `gpt-5.6-luna` for
OpenAI. To spend more on one step set for example `OPENAI_STRONG_MODEL=gpt-5.6-sol`. To spend less, use `--self-test none`
or `smoke`, `--profile lean`, and run with `--dry-run` while you iterate.

Trace and observer writers apply secret masking. The audit verified trace redaction in a synthetic error case but found
gaps in ordinary Python logging and returned errors. See the [redaction limitation](docs/VALIDATION_AND_LESSONS.md#compiler-and-runtime-counterexamples)
before sharing diagnostic output.

## Development commands

Run these from the repository root. On Windows without uv use `.venv/Scripts/python.exe` instead of `uv run python`.

```sh
# tests and lint (no network, no keys; sockets to non-loopback hosts are blocked by tests/conftest.py)
uv run pytest -m "not live"
uv run ruff check builder tests

# offline end-to-end: the model calls are answered from tests/fixtures/policy (brief_a_*.json and brief_ab_*.json)
uv run python builder/build.py briefs/brief-a.md tools/openapi.json --dry-run --fake-llm tests/fixtures/policy
uv run python builder/build.py briefs/brief-a.md briefs/brief-b.md tools/openapi.json --dry-run --fake-llm tests/fixtures/policy

# practice runs against a deployed agent (kit tool; needs FREYA_API_KEY and FREYA_BANK_TEAM_KEY; spends credits)
uv run python tools/run_cases.py <agent_id> --brief A --out results/run_a.json
uv run python tools/run_cases.py <agent_id> --brief B --reps 3 --style pieces      # styles: pieces corrects talkative asks_back in_words
uv run python tools/run_cases.py <agent_id> --only PUB-04,PUB-07                  # selected cases

# why did a case fail? prints the transcript, node entries, tool calls and route decisions of the failed cases
uv run python evals/triage.py results/run_a.json            # --all for every case, --vars for the variable table

# the hand-authored reference agent (9 of 10 on Brief A) that the compiler is compared with
uv run python evals/reference/brief_a_ref.py --write                        # evals/reference/out/ref-a.workflow.json
uv run python evals/reference/brief_a_ref.py --write --multi --slot ref-ab  # Brief A+B variant
```

`<agent_id>` is the `agent_id` in the builder's JSON line (and in `results/<slot>-<run>.build.json`). One simulation runs at a
time per workspace; a ten-customer run takes 3 to 11 minutes. One green run is a hint, not proof: the final scoring needs three
passes in a row for a customer to count.

## What a run produces

| Path | Content |
|---|---|
| `out/<slot>.workflow.json` | The workflow definition that was sent to Freya (in a dry run: the one that would be sent). |
| `out/<slot>.exported.json` | The platform export of the deployed workflow, in the kit's format. This is what you submit. |
| `out/<slot>.ledger.json`, `out/<slot>.ir.json` | The rule ledger and the policy IR, for inspection. |
| `out/<slot>.rejected.workflow.json` | A workflow that failed the checks and was not deployed. |
| `traces/<slot>-<time>.log` and `.jsonl` | The run trace: start and end, every model call (model and one-line purpose), stages, what was created on Freya, tests. Plain text, secrets masked. This is the trace you submit. |
| `traces/<slot>-<time>.llm.jsonl` | Every LLM attempt with prompt and reply, secrets masked. For debugging; do not submit if the prompts must stay private. |
| `traces/<run>.agentic.jsonl` | Redacted log of an agentic fallback session, if one ran. |
| `results/<slot>-<run>.build.json` | The full result of the run. |
| `.freya-agents.json` | Kit state: slot to workflow and agent id, so the next run updates the same workflow (200 workflows per workspace). |
| `data/key_health.json` | Dead-key memory of the key changer. |

## Submission

The team does every git step by hand; the builder never runs git. For each final brief:

1. Use the interpreter that has the dependencies (see the quick start) and keep `.env` filled in.
2. Run the builder **once per brief** with no manual step:
   `python3 builder/build.py {brief_path} {tool_api_path}` (for the second final brief, the same with its own files).
   Do not edit the created agent by hand. Do not write anything from the final briefs into the code. A crash may be fixed.
3. Read the JSON line at the end of stdout: `status` should be `deployed`; note `workflow_id`, `agent_id`, `trace`.
4. The export is in `out/<slot>.exported.json`. To export again yourself:
   `python3 -c "import freya; freya.export_workflow('<workflow_id>', '<brief>.workflow.json')"`.
5. Keep one trace per final brief: `traces/<slot>-<time>.log` (and its `.jsonl`). Review diagnostic artifacts for secrets
   before committing; prefix searches alone do not establish complete redaction.
6. Commit the code, the traces and the exported workflow files. Never commit `.env`, `env.sh` or `data/key_health.json`.
   Fill in the submission form: team, repository, commit, `run_command`, the workflows and the traces.

The submission closes on Sunday 11 October at 20:00 UTC; the final briefs open at 17:00 UTC. The budget of one build is 28 minutes, so leave room
to run twice (the second run updates the same slot in place).

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `Not logged in` / the `claude_code` provider is skipped | The Claude login on this machine is not valid. Run `claude login`, then delete `data/key_health.json` (it only holds dead-key fingerprints). Until then the chain uses the other providers. |
| `All LLM providers failed` | No usable key, or every key is dead or rate limited. The trace lists each attempt with its error class. Add a key, wait for the cooldown, or fix a wrong model name. |
| A model name is rejected (`BAD_REQUEST` then the next provider) | The default id is not available to your key. Override it, for example `OPENAI_STRONG_MODEL=<id>` or `ANTHROPIC_FAST_MODEL=<id>`. |
| The run says it is a dry run although you did not pass `--dry-run` | `FREYA_API_KEY` is empty. Set it in `.env`. |
| `Workspace plan limit reached` | Too many workflows. The builder reuses the slot in `.freya-agents.json`; do not delete that file, or use `--slot` with an existing name. |
| `error code: 1010` | Request without a browser-like `User-Agent`. `freya.py` sets one; do not bypass it. |
| Exit code 2 and `status: blocked` | A guard stopped the build (injection marker in the input, a value in the policy that the briefs do not contain, a secret in the workflow). The trace lists the violations. Nothing was deployed. |
| `status: failed` | Read the `errors` field of the JSON line and the last `STAGE` lines of the trace. With `--track auto` the agentic fallback ran if time was left. |
| Lint warning L018 | The negotiation prompt is long (several-product briefs). A warning only. |
| Score does not move after a change | An updated workflow must be published; the builder publishes through `save`. A plain `PUT` does not. See `docs/api.md`. |
| Practice customer fails, reason unclear | `uv run python evals/triage.py results/<run>.json`. |
| `ModuleNotFoundError: streamlit` | Install the extra: `uv sync --extra ui`. |
| The build was cut off | Inspect the trace and platform state. A worker-thread operation can continue after graph cancellation; the audit documents this limit. Use `--self-test smoke` or `none` to reduce optional work. |
| `python3` picks another interpreter | Activate the virtual environment, or call its interpreter by path. |

## Interactive playground

A Streamlit page to try the policy engine and a deployed agent without a build. It needs the `ui` extra:

```sh
uv sync --extra ui --extra dev
uv run --extra ui streamlit run streamlit_app.py            # usually http://localhost:8501
```

- **Policy lab:** pick Brief A or A+B, change customer and account facts, and exercise amounts, business-day deadlines,
  callbacks and closing outcomes. Edit the policy JSON in memory or upload your own PolicyIR.
- **Chat with agent:** pick a saved workflow from `.freya-agents.json` (or enter its id), prepare a public practice case (this
  creates a fresh mock-bank customer) and talk to the agent turn by turn. Engine events, bank records and the transcript are
  shown. It uses Freya credits.
- **Inspect inputs:** load briefs and a tool API file and see the normalised text and contracts. PDF input needs `--extra ingest`.
- **Practice results:** inspect saved or uploaded `run_cases.py` reports.
- **Robustness checks:** offline boundary checks derived from any PolicyIR and tool API (weekdays, holidays, floors, callback
  windows, outcome precedence, API enums). They are invariants, not a prediction of the final score.
- **Request monitor:** read JSONL traces, errors and latency, and call engine events on demand.

The policy, input and report views need no credentials. Live chat reads `FREYA_API_KEY` and `FREYA_BANK_TEAM_KEY` from `.env`.

## Documentation map

| Document | Content |
|---|---|
| [Documentation index](docs/README.md) | Reader routes, source entry points and how to distinguish plans from implementation and evidence. |
| [How we built it](docs/PROJECT_STORY.md) | Development approach, baseline, reference agent, ledger, policy model, compiler and runtime decisions. |
| [Validation and lessons](docs/VALIDATION_AND_LESSONS.md) | Dated test results, saved practice scores, audit limitations and remaining acceptance milestones. |
| [Fixing issues](docs/FIXING_ISSUES.md) | Prioritized repair backlog with concrete exit checks for reproduced defects and readiness gaps. |
| [Customer edge cases](docs/EDGE_CASES.md) | Identity validation, incorrect birthdays, conversational boundaries and proposed acceptance scenarios. |
| [Banking conversation practices](docs/BANKING_CONVERSATION_PRACTICES.md) | Primary-source research, current enforcement gaps and acceptance scenarios for persuasion, repetition, barge-in and bank writes. |
| [Official rules comparison](docs/HACKATHON_RULES_COMPARISON.md) | Fetched organizer sources, desired outcomes, current strengths and the evidence still needed for submission readiness. |
| `docs/DEVELOPER_GUIDE.md` | Module map, data flow, how to extend the builder, compliance rules. Start here when changing code. |
| `docs/GRAPH.md` | The LangGraph: nodes, state, routing, fallbacks, self-tests, run commands. |
| `docs/UNDERSTANDING.md` | Rule ledger, grounding, policy IR extraction, validation and repair. |
| `docs/COMPILER.md` | The compiler, the two profiles, generated code, rule coverage, lint rules L001 to L027. |
| `docs/GUARDRAILS.md` | Input, policy, prose and workflow guards. |
| `docs/LLM_KEY_CHANGER.md` | Key rotation, provider chain, error classes, adding a provider. |
| `docs/OBSERVABILITY.md` | Traces, the LLM log, Langfuse, rate limiting. |
| `docs/ARCHITECTURE.md`, `docs/IMPLEMENTATION_PLAN.md`, `docs/TASKS.md`, `docs/FOLDER_STRUCTURE.md` | Planning documents written before the implementation. The module docs above are authoritative. |
| `docs/research/` | Research notes; `11_hackathon_rules_and_kit.md` summarises the rules. |
| `docs/workflow-format.md`, `docs/tools-and-code.md`, `docs/api.md`, `KIT_README.md` | Organiser material (the platform reference). |
