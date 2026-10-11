# evals: measuring the agents

Development tools, not part of the builder. They answer two questions: how good is an agent on the practice customers,
and why did a customer fail. Nothing in `evals/` is imported by `builder/`, so brief-specific values may live here.

| Path | What it is |
|---|---|
| [`reference/brief_a_ref.py`](reference/brief_a_ref.py) | A hand-written phone agent for the public Brief A. It is the ceiling the compiler is compared with, never submitted |
| [`reference/out/`](reference/out/) | Its workflows: `ref-a.workflow.json` (Brief A) and `ref-ab.workflow.json` (Brief A and B) |
| [`triage.py`](triage.py) | Prints the timeline of every failing attempt of a results file |
| [`synthetic_briefs/`](synthetic_briefs/README.md) | Two invented banks with different values, to check that the builder reads a brief it has not seen |

## The scorer

The official scorer is the kit tool `tools/run_cases.py` (do not edit it). It opens a practice session per case on the mock
bank, creates the simulated customers, runs one simulation against a deployed agent, then scores each customer from the bank's
ledger plus the judge's reading of the conversation. It needs `FREYA_API_KEY` and `FREYA_BANK_TEAM_KEY` in `.env`, and it spends credits. One simulation
runs at a time per workspace; ten customers take 3 to 11 minutes.

```sh
python tools/run_cases.py <agent_id> --brief A --out results/run_a.json            # Brief A cases, one repetition
python tools/run_cases.py <agent_id> --brief B --reps 3 --out results/run_b.json   # three repetitions
python tools/run_cases.py <agent_id> --brief A --reps 3 --style talkative          # a different way of talking
python tools/run_cases.py <agent_id> --only PUB-04,PUB-07                          # selected cases
```

- `<agent_id>` is the `agent_id` of the builder's JSON line (also in `results/<slot>-<run>.build.json`).
- `--reps N` repeats the simulation. **A customer counts only when every repetition passes**, so `--reps 3` is the number to
  trust; one green run is a hint.
- `--style` changes how the simulated customer talks while the facts stay the same: `pieces` (long numbers in two parts),
  `corrects` (says a wrong day, then corrects it), `talkative`, `asks_back` (asks a question before answering), `in_words`
  (amounts and dates in words).
- `--cases FILE` chooses the case file (default `public-test-set.json`), `--brief A|B` filters it, and
  `--rerun-if-slow N` discards a run made while the platform was slow (median slowest turn over 10 seconds) and repeats it,
  up to N times, after five minutes.
- `--out FILE` saves every repetition (`all_rows`), the last one (`rows`) and the strict verdict per case (`final`).
  Each run also adds one line to `results/log.jsonl`.

## Finding out why a customer failed

```sh
python evals/triage.py results/run_a.json                  # every failing attempt of every lost case
python evals/triage.py results/run_a.json --only PUB-04 --vars   # all repetitions of one case, with the variable table
python evals/triage.py results/run_a.json --rep last       # one repetition (1-based number, or `last`)
python evals/triage.py results/run_a.json --all            # every case, every repetition
```

For each attempt it fetches the call from Freya and prints what the customer said, what the agent said, node entries, tool
calls with arguments and results, route decisions and engine errors. It reads the key from the environment or `.env`.
The report code is tested in `tests/unit/test_eval_triage.py`.

## The reference agent

`reference/brief_a_ref.py` builds the Brief A agent by hand from Python snippets. The staged profile of the compiler was
modelled on it, and `tests/unit/test_compile_staged.py::test_structure_matches_the_validated_reference` compares the two
structures.

```sh
python evals/reference/brief_a_ref.py                       # writes evals/reference/out/ref-a.workflow.json
python evals/reference/brief_a_ref.py --multi --slot ref-ab # Brief A and B: adds the notes rules and the add_note tool
python evals/reference/brief_a_ref.py --validate            # also asks Freya to validate it (no save)
python evals/reference/brief_a_ref.py --deploy              # also saves and publishes it under --slot (writes to Freya)
```

The script always writes the workflow file; the flags only add the Freya steps. The default slot is `ref-a`.

## Synthetic briefs

[`synthetic_briefs/`](synthetic_briefs/README.md) holds two invented banks with the same policy in two forms: one clean
specification and one set of raw material (a mail thread, a policy extract, a memo that overrides two values, a code list).
Every value the hackathon says changes in hidden briefs is different. A dry run makes no Freya call but does call a model
(an LLM key is needed, about 2 to 4 minutes with the default models), for example
`python builder/build.py evals/synthetic_briefs/v01_clean/brief.md evals/synthetic_briefs/v01_clean/openapi.json
--slot qr-v01-clean --dry-run --track compiled --self-test none`. Read `out/<slot>.ir.json` against the expected values
listed in that README.

## Results history

- [`results/BASELINE.md`](../results/BASELINE.md): the measurements in order. The kit base agent scored 0 of 13, the reference
  agent reached 9 of 10 on Brief A and 3 of 3 B amendment cases on A+B, and the builder's own first live builds scored 8 of 10
  (Brief A) and 3 of 3 B amendment cases (A+B). These numbers live in BASELINE.md; this list does not track them.
- `results/log.jsonl`: one line per scorer run (time, agent, repetitions, passed, failed cases).
- `results/ref_*.json` and `.log`: the saved runs of the reference agent by version and style.
- `results/acceptance/`: the live acceptance of the automatic builder; `summary.txt` has the scores, `build_*.json` the build
  results and `score_*.json` the full scorer files. `results/acceptance2/` holds the second live acceptance (11 October).
- `results/<slot>-<run>.build.json`: the result line of each builder run.
