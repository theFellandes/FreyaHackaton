# Freya Phone Agent Builder

A LangGraph **builder** for the Freya x YC "Build the Builder" hackathon. It reads a bank's brief (a clean
specification, or raw material such as a mail, a policy extract and a memo) and the bank's tool API file. It then
creates, tests, publishes and exports a Freya phone agent with no manual step. A model reads the documents into a typed
policy or source-grounded service plan; a deterministic compiler turns it into the workflow. Collections use code for
dates and amounts; the generic service route follows the original API and policy. See [How it works](docs/HOW_IT_WORKS.md).

```
brief(s) + tool API -> rule ledger -> policy IR -> compiled workflow -> checks + guards -> Freya (save, export) -> self-test -> trace
```

## Final case: start here

**[submission/FINAL_RUNBOOK.md](submission/FINAL_RUNBOOK.md)** is the current procedure for the one-case Turkish final
on Sunday 11 October 2026. The final case opened at 17:00 UTC (20:00 Istanbul); submission closes at 20:00 UTC
(23:00 Istanbul). Read BRIEF.md, then README.md, implement and verify the bank mock on Cloudflare Workers, then run
the builder. Submit one exported workflow and that run's complete trace using the eight-field final form.

The final builder interface, from the repository root after the mock is deployed:

```sh
python3 builder/build.py {case_folder} {mock_url}
```

On this Windows machine, use `.venv-playground/Scripts/python.exe` for builder runs. Capture the selected run with
`submission/tools/capture_run.py`; the exact PowerShell procedure is in
[submission/tools/README.md](submission/tools/README.md). The old two-brief form and `builder.submission` helper
are historical. The release follow-up is paused at the user's request; follow the runbook manually.

## Practice evidence (11 October 2026)

Honest evidence only. A practice customer counts only if all 3 repetitions pass. The hidden briefs differ from the
public ones, so none of this is a hidden-brief score.

| Subject | Result | Source |
|---|---|---|
| Final code, live builds with the literal `python3` command | Every build of 11 October deployed and exported (Brief A in 110-257 s, Briefs A+B in 174-209 s). Static checks and remote validation always passed; the smoke chat passed whenever Freya answered and failed only on Freya HTTP 504s. | [results/BASELINE.md](results/BASELINE.md) section 4 |
| Final code, live score | Brief A: **10 of 10** strict in two consecutive rounds (acceptance3 and 4, 60 of 60 conversations); 8 of 10 in a third round. Brief B cases on the A+B agent: **3 of 3** strict (acceptance3). Extra edge scenarios (dispute, wrong number, declined identity, human, hardship, rude, scam, deceased, ...): 10 of 10 on Brief A. Later B runs were cut short by Freya/practice-bank outages. | [results/BASELINE.md](results/BASELINE.md), `results/acceptance3/` to `results/acceptance6/` |
| Earlier code (10 October), live score | Brief A: **8 of 10**. Briefs A+B: **3 of 3** Brief B cases. | [results/acceptance/summary.txt](results/acceptance/summary.txt) |
| Hand-written reference agent | 9 of 10 on Brief A, 3 of 3 on Brief B | [results/BASELINE.md](results/BASELINE.md) |
| Kit base agent | 0 of 13 | same |
| Offline tests | 3387 passed and 4 expected failures (xfail) at hand-over on 11 October; no network, no keys. They prove mechanics, not extraction from an unseen brief. | `pytest tests` |

Known limits: [Quality review](results/QUALITY_REVIEW.md), [Critical gaps](docs/CRITICAL-GAPS.md) and
[Hidden-brief readiness](docs/HIDDEN-BRIEF-READINESS.md) (external reviewer).

## Hackathon contract

- The final interface is `python3 builder/build.py {case_folder} {mock_url}`. The earlier brief/OpenAPI interface
  remains available for practice. One run may take 30 minutes including self-tests; the builder stops itself at 28.
- Progress goes to stderr. The **last line of stdout** is one JSON line with `slot`, `workflow_id`, `agent_id`,
  `version`, `exported`, `export_ok`, `trace`, `status`, `tests`, `errors` and `policy_issues`. Exit code 0 means
  deployed with a verified export (or a requested dry run); 2 means anything else.
- The phone agent calls only the bank's tool API; its model is fixed by the platform. Nothing from a brief is written
  into `builder/` (`tests/unit/test_compliance.py` checks this). After 17:00 only generic bug fixes are allowed.
- Kit files (`freya.py`, `tools/`, `briefs/brief-*.md`, `examples/`, `public-test-set.json`, `docs/api.md`,
  `docs/workflow-format.md`, `docs/tools-and-code.md`, [KIT_README.md](KIT_README.md)) are the organisers' material.
  Do not edit them.

## Quick start

**This machine.** `.venv-playground/Scripts/python.exe` is the existing healthy Python 3.12 environment.
The root `.venv` cannot start, and the new submission/runtime tokenizer import is blocked by Windows application
control. The following commands are practice examples in PowerShell:

```sh
.venv-playground/Scripts/python.exe builder/build.py briefs/brief-a.md tools/openapi.json --dry-run --fake-llm tests/fixtures/policy   # offline, seconds
.venv-playground/Scripts/python.exe builder/build.py briefs/brief-a.md tools/openapi.json --dry-run       # real model calls, no Freya call
.venv-playground/Scripts/python.exe builder/build.py briefs/brief-a.md tools/openapi.json                 # build, deploy, export, self-test
.venv-playground/Scripts/python.exe builder/build.py briefs/brief-a.md briefs/brief-b.md tools/openapi.json
```

**A clean machine.** Python 3.11 to 3.13 and [uv](https://docs.astral.sh/uv/):

```sh
uv sync --extra agentic --extra ingest --extra dev
cp .env.example .env          # then fill in the keys
uv run python builder/build.py briefs/brief-a.md tools/openapi.json --dry-run
```

Without uv: `python3.13 -m venv .venv && .venv/bin/pip install -r requirements.txt`. Docker:
`docker compose run --rm builder briefs/brief-a.md tools/openapi.json --dry-run`.

`.env` needs `FREYA_API_KEY`, `FREYA_BANK_TEAM_KEY` (a secret of 16+ characters you make up, the same every time) and
at least one LLM key (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, with `_1` to `_20` suffixes for more). Every variable is
documented in [.env.example](.env.example). Without a Freya key the run becomes a dry run and exits 2.

## Run options

| Option | Meaning |
|---|---|
| `--dry-run` | Compile and check locally; no Freya call. The workflow goes to `out/<slot>.workflow.json`. |
| `--slot NAME` | Workflow label on Freya. Default: from the brief file names (`hidden-1.md` gives `hidden-1`). The same slot is updated in place. |
| `--track compiled\|agentic\|auto` | `compiled` (default): model interpretation, then deterministic compilation. `agentic`: a Claude Agent SDK session writes the workflow. `auto`: compiled, agentic as fallback. |
| `--profile staged\|lean` | Workflow shape; `staged` is the default. |
| `--self-test smoke\|none\|walkthrough\|practice\|auto` | What runs after the deploy. `smoke` (default) is one short chat. |
| `--budget-s N` | Wall-clock budget in seconds (default 1680). |
| `--fake-llm DIR` | Answer every model call from fixture files (`tests/fixtures/policy`). Offline, no keys. |

## What a run produces

| Path | Content |
|---|---|
| `out/<slot>.exported.json` | The platform export of the deployed version (what you submit) |
| `out/<slot>.workflow.json`, `.ir.json`, `.ledger.json` | The workflow sent, the extracted policy, the rule ledger |
| `traces/<slot>-<time>.log`, `.jsonl` | The run trace: start and end, every model call with its purpose, what was created on Freya, tests |
| `traces/<slot>-<time>.llm.jsonl` | Full prompts and replies, secrets masked. Git-ignored; never commit. |
| `results/<slot>-<run id>.build.json` | The full result, including `agent_id`, `notes` and `tests_detail` |
| `.freya-agents.json` | Slot to workflow and agent ids, so re-runs update in place |

## Development

```sh
.venv/Scripts/python.exe -m pytest tests -m "not live" -p no:cacheprovider    # offline, no keys
.venv/Scripts/python.exe -m ruff check builder tests evals
.venv/Scripts/python.exe -m dotenv -f .env run -- "$PWD/.venv/Scripts/python.exe" tools/run_cases.py <agent_id> --brief A --reps 3 --out results/run_a.json   # official scorer, loads .env, spends credits
.venv/Scripts/python.exe evals/triage.py results/run_a.json                                           # why did a case fail
.venv/Scripts/python.exe -m streamlit run streamlit_app.py                                            # playground
```

More scenarios on the practice bank: [evals/extra_cases/README.md](evals/extra_cases/README.md). Scorer details:
[evals/README.md](evals/README.md).

## Troubleshooting

| Symptom | Fix |
|---|---|
| `All LLM providers failed` | No usable key, or all dead or rate limited. The trace lists each attempt. Add a key or wait for the cooldown; see [LLM key changer](docs/LLM_KEY_CHANGER.md). |
| `status: blocked` | A guard stopped the build; the trace lists the violations. Nothing was deployed. |
| Smoke `FAIL` with HTTP 502/503/504 | Freya staging problem, not an agent problem. The agent is deployed. |
| `Python 3.14 ... is not supported` and no handover | No usable `.venv` (or uv) was found. Create one as in the quick start. |
| `Workspace plan limit reached` | Too many workflows. Keep `.freya-agents.json` and reuse a slot. |

## Documentation

[Documentation index](docs/README.md) (reading order), [final runbook](docs/FINAL_RUNBOOK.md),
[how it works](docs/HOW_IT_WORKS.md), [submission](docs/SUBMISSION.md), [developer guide](docs/DEVELOPER_GUIDE.md),
[package map](builder/README.md).
