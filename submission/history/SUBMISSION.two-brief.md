# Submission

The step-by-step procedure for the hidden-brief day is in **[FINAL_RUNBOOK.md](FINAL_RUNBOOK.md)**. This page explains
what the entry consists of and what the submission helper checks.

## What the organisers ask for

From the official page (<https://www.freyavoice.ai/hackathon>, Submit, FAQ, Evaluation and Rules tabs, read 11 October):

- At 17:00 UTC (20:00 Istanbul) every team gets two hidden briefs by email. Each is one Markdown file with its own tool
  API file. There are no practice cases for them.
- Run the builder on each brief and keep everything it prints. You may run it more than once. Submit one phone agent per
  brief, with the trace of the run that made it. Do not edit a phone agent after the run.
- Export each phone agent with `freya.export_workflow` (the builder does this) and commit the files.
- Before 20:00 UTC (23:00 Istanbul), email the form to the address in the kit email. Fields: `team`, `repo`, `commit`,
  `run_command`, `workflows`, `traces`.
- A trace must show start and end time, every model call (model and a one-line purpose), what was created on Freya
  (workflow id, agent id, number of steps and tools) and any self-test with its result. Plain text, API keys removed.
- The top ten entries are cloned at the submitted commit and rebuilt; an entry that scores clearly lower on rebuild is
  removed. Limit: 30 minutes per brief, self-tests included.

## Which files go in the form

One recommendation, used everywhere in this repository:

| Form field | Path | Made by |
|---|---|---|
| `workflows.hidden-1` | `submission/hidden-1.workflow.json` | `cp out/hidden-1.exported.json submission/hidden-1.workflow.json` |
| `traces.hidden-1` | `traces/hidden-1.log` | the `tee` of the build command (everything the run printed) |
| `run_command` | `python3 builder/build.py {brief_path} {tool_api_path}` | the organisers' suggested command |

The same for `hidden-2`. These are the paths of the official example. The builder's own files
(`out/hidden-N.exported.json`, `traces/hidden-N-<time>.log` and `.jsonl`, `results/hidden-N-<run id>.build.json`) hold
the same content and are committed too.

## The submission helper

`builder/submission.py` checks a local draft. It never runs git commands that change anything, never contacts Freya and
never sends email.

```sh
.venv/Scripts/python.exe -m builder.submission prepare --output submission/draft.json --force --team "..." --repo "..." --run-command "python3 builder/build.py {brief_path} {tool_api_path}" --hidden-1-result results/hidden-1-<run id>.build.json --hidden-2-result results/hidden-2-<run id>.build.json [--commit <sha>]
.venv/Scripts/python.exe -m builder.submission check submission/draft.json
.venv/Scripts/python.exe -m builder.submission payload submission/draft.json
```

- `prepare` writes the draft. It takes the export and trace paths from each build result file, so the draft points to
  the builder's own files, not to the form paths above. Without `--force` it refuses to overwrite a draft. It then
  runs the check and exits 0 only when everything passes.
- `check` exits 0 only when: each build is `deployed` with `export_ok: true` and not cut off; the export matches the
  build's workflow and agent ids and version; the trace has ordered start and end records, one run id, the right
  workflow id, agent id, version and published flag, step and tool counts equal to the export's, one record per model
  call with provider, model and a one-line purpose, and a duration of at most 30 minutes; and the commit contains the
  runtime code and these artifacts unchanged. Before you commit it reports `Pending commit`; that is expected.
- `payload` prints the six form fields of a ready draft. It would print the builder's own paths. We do not use it for
  the email; the runbook's email template uses the form paths above.

A passing check proves local consistency only. It is not a customer score and not proof that every secret was removed;
the runbook has a separate secret check.

## Environment on this machine

- `.venv/Scripts/python.exe` is Python 3.13.16 and has all dependencies; the offline suite passes there (3380 passed and 4 xfail on
  11 October).
- `python3` is Python 3.14. The builder does not support 3.14, so `python3 builder/build.py ...` hands the run over to the
  repository `.venv` and prints a `note:` line on stderr. Both 11 October live builds used this literal command.
- Earlier notes that "the primary `.venv` is broken" came from another reviewer's sandbox, which could not reach the
  `.venv`'s base interpreter. They do not apply here.

## Other files

- [`submission/draft.json`](../submission/draft.json): the local draft; its final-run fields stay empty until the hidden
  builds exist.
- [`submission/submission.example.json`](../submission/submission.example.json): the shape of the form (its paths are
  placeholders that point to the builder's own files; the email uses the form paths above).
- [`submission/EMAIL_DRAFT.md`](../submission/EMAIL_DRAFT.md): email wording. Its recipient address is unconfirmed;
  use the address in the kit email.
- [`submission/VALIDATION.md`](../submission/VALIDATION.md): dated offline verification records (history).
