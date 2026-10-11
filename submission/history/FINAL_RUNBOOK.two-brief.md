# Final runbook: hidden-brief day (Sunday 11 October 2026)

This is the one procedure for the hidden briefs. Follow it top to bottom. Every command is meant for **Git Bash**, run from
the repository root. Do not use Windows PowerShell 5.1 for the build: its `tee` writes UTF-16, and the trace file must be
plain text.

```sh
cd /c/Users/Felladien/Documents/Projects/Python/FreyaHackaton
```

Rules that apply all day: the official page (Submit, FAQ, Evaluation and Rules tabs at
<https://www.freyavoice.ai/hackathon>) and the kit README. Short version: one phone agent per hidden brief, made by the
builder with no hand edits, plus the trace of the run that made it; email the form before 20:00 UTC.

## Timeline

Istanbul is UTC+3.

| UTC | Istanbul | Step |
|---|---|---|
| 16:00 | 19:00 | [1. Preflight](#1-preflight-t-60) |
| 16:50 | 19:50 | Ready: Git Bash open in the repo, no scoring job running, the kit email open |
| 17:00 | 20:00 | Hidden briefs arrive by email. [2. Save the files](#2-save-the-four-files) |
| about 17:05 | 20:05 | [3. Build hidden-1](#3-build), then hidden-2 (each takes about 3 to 6 minutes) |
| 17:20 to 18:30 | 20:20 to 21:30 | [4. Inspect](#4-inspect-before-you-accept), optional [chat check](#5-optional-manual-chat-check), [re-runs](#6-re-runs) or [generic fixes](#9-after-1700-generic-fixes-only) |
| 18:30 | 21:30 | Freeze. [7. Package](#7-package-the-submission) and [8. Commit](#8-commit-and-push) |
| 19:00 | 22:00 | [Send the email](#10-send-the-email) (target) |
| **20:00** | **23:00** | **Submission closes** |

## 1. Preflight (T-60)

Run these at 16:00 UTC. None of them prints a secret.

**Git state.** Commit any pending work now (you run git yourself; the builder never does). A clean start makes it easy
to show later that changes after 17:00 were generic fixes.

```sh
git status --short | head -20
git log --oneline -1
```

**Python.** `python3` on this machine is 3.14. The builder does not run on 3.14, so it hands itself over to the repository
`.venv` automatically. The `.venv` must say 3.13.

```sh
python3 --version                    # Python 3.14.x is fine (handover)
.venv/Scripts/python.exe --version   # must print Python 3.13.16
```

**Offline tests (optional, about 3 minutes).** All should pass; no network, no keys.

```sh
.venv/Scripts/python.exe -m pytest tests -m "not live" -p no:cacheprovider
```

**Keys present.** Prints `set` or `MISSING` and a key count per LLM provider, never a value. You need both Freya lines
`set` and at least one LLM key (or a working `claude login` for the `claude_code` provider).

```sh
.venv/Scripts/python.exe -c "from builder.settings import get_settings; from builder.llm.keys import discover_all_keys; s = get_settings(); print('FREYA_API_KEY', 'set' if s.freya_api_key else 'MISSING'); print('FREYA_BANK_TEAM_KEY', 'set' if s.freya_bank_team_key else 'MISSING'); print('LLM keys per provider', {p: len(k) for p, k in discover_all_keys().items()})"
```

**Freya reachable, workflow headroom.** Two GET requests (no credits). The workspace limit is 200 workflows; the two
hidden builds add two. The list may be only one page, so the count is a lower bound.

```sh
.venv/Scripts/python.exe -m dotenv -f .env run -- "$PWD/.venv/Scripts/python.exe" -c "
import freya
ws = freya.workspace_id()
print('Freya reachable, workspace found:', bool(ws))
r = freya.api('GET', '/api/v2/workflow', workspaceId=ws)
items = next((r[k] for k in ('workflows', 'items', 'results', 'data') if k in r), []) if isinstance(r, dict) else r
print('workflows listed:', len(items) if isinstance(items, list) else '?', '(limit 200)')
"
```

`.freya-agents.json` currently records 5 slots. A 401 means a wrong Freya key. A 502 or 504 means staging is slow:
retry in a few minutes.

**Offline rehearsal (seconds).** Proves the `python3` command and the handover work. Then delete what it wrote.

```sh
python3 builder/build.py briefs/brief-a.md tools/openapi.json --dry-run --fake-llm tests/fixtures/policy --slot rehearsal 2>&1 | tail -1
rm -f out/rehearsal.* results/rehearsal-*.build.json traces/rehearsal-*
```

The last line must contain `"status": "dry_run"`.

**Free the platform.** Stop or finish any scoring run (`tools/run_cases.py`) before 16:50 UTC. The builds do not need a
simulation slot, but staging has been slow and you want its full attention.

## 2. Save the four files

The email has two hidden briefs. Each is **one Markdown file** with **its own tool API file**. Save them, unchanged, as:

| Email attachment | Save as |
|---|---|
| first brief | `briefs/hidden-1.md` |
| its tool API | `briefs/hidden-1.openapi.json` |
| second brief | `briefs/hidden-2.md` |
| its tool API | `briefs/hidden-2.openapi.json` |

Use the email's own numbering. If a tool API file is YAML, keep its real extension (`.yaml`) and use that name in the
commands. Download the attachments; do not retype them. Check them:

```sh
ls -la briefs/hidden-*
head -5 briefs/hidden-1.md briefs/hidden-2.md
.venv/Scripts/python.exe -c "import json; [json.load(open(p, encoding='utf-8-sig')) for p in ('briefs/hidden-1.openapi.json', 'briefs/hidden-2.openapi.json')]; print('both tool API files parse')"   # JSON files only
```

Do not edit these files after the first build. The slot name comes from the brief file name (`hidden-1`). If the
content of a brief changes between runs, the builder treats it as different inputs and adds a hash to the slot
(`hidden-1-1a2b3c4d`), which creates a second workflow.

## 3. Build

One brief at a time. The organisers' command, with everything it prints kept in `traces/hidden-1.log`:

```sh
python3 builder/build.py briefs/hidden-1.md briefs/hidden-1.openapi.json 2>&1 | tee traces/hidden-1.log; echo "exit=${PIPESTATUS[0]}"
```

Then the second brief:

```sh
python3 builder/build.py briefs/hidden-2.md briefs/hidden-2.openapi.json 2>&1 | tee traces/hidden-2.log; echo "exit=${PIPESTATUS[0]}"
```

What to expect:

- The first lines say `uuid_utils could not be loaded ...` and
  `note: this interpreter cannot run the builder; re-running with: ...\.venv\Scripts\python.exe`. Both are normal: that
  is the handover from 3.14 to the `.venv`.
- Progress lines follow (`START`, `MODEL`, `FREYA`, `TEST`, `STAGE`), then a `BUILD SUMMARY` block, then one JSON line.
- Time: about 3 to 6 minutes. On 11 October the public briefs took 257 s and 209 s. The builder stops itself at
  28 minutes; the limit is 30.
- `exit=0` means deployed. `exit=2` means anything else (read the JSON line).

The equivalent explicit command, if the handover ever misbehaves:

```sh
.venv/Scripts/python.exe builder/build.py briefs/hidden-1.md briefs/hidden-1.openapi.json 2>&1 | tee traces/hidden-1.log; echo "exit=${PIPESTATUS[0]}"
```

Do not add flags. The official `run_command` has none, and the default self-test (`smoke`) keeps the build short, which
matters for the tie-break on total build time.

No Git Bash at all? From `cmd`, not PowerShell 5.1:
`.venv\Scripts\python.exe builder\build.py briefs\hidden-1.md briefs\hidden-1.openapi.json > traces\hidden-1.log 2>&1`
(no live output; open the file to follow it).

### Read the final JSON line

```sh
tail -1 traces/hidden-1.log | .venv/Scripts/python.exe -m json.tool
```

**Good** = `"status": "deployed"` and `"export_ok": true`. The line also has `slot`, `workflow_id`, `agent_id`,
`version`, `exported` (the platform export), `trace` (the builder's own trace file), `tests`, `errors` and
`policy_issues`.

| `status` | Meaning | What to do |
|---|---|---|
| `deployed` | Published, export written (exit 0) | Inspect (step 4). If `export_ok` is not `true`, treat as `deployed_unexported`. |
| `deployed_unexported` | Live on Freya, but no verified export file (exit 2) | Re-run the same command. The workflow is updated in place and the export is retried. |
| `deployed_unpublished` | Saved, but publishing was not confirmed (exit 2) | Usually a Freya 5xx. Wait a few minutes, re-run. |
| `dry_run` | Nothing sent to Freya (exit 2 unless you asked for it) | The Freya key was missing. Run the key check in step 1, fix `.env`, re-run. |
| `blocked` | A guard stopped the build; nothing deployed (exit 2) | Read `errors` and the `GUARD`/`WARN` lines in the trace. If it is a builder bug, fix it generically (step 9) and re-run. |
| `failed` | The build broke (exit 2) | Read `errors`. LLM problem: see contingencies. Builder crash: generic fix, re-run. |
| `"cut_off": true` (extra key) | The 28-minute guard ended the build; whatever was deployed is reported | The submission helper's check rejects such a run. Re-run if time allows; otherwise submit what was deployed. |

## 4. Inspect before you accept

Spend about 10 minutes per brief. You are looking for builder bugs, not for things to hand-edit (never edit the agent).

1. **The JSON line.** `tests`, `errors`, `policy_issues`. Every entry in `policy_issues` is something the policy check
   could not settle; read each one.
2. **The trace's test lines.**

   ```sh
   grep -n "| TEST\|| WARN\|| ERROR\|| GUARD" traces/hidden-1.log
   ```

   Expected: `PASS static_checks`, `PASS remote_validate`, `PASS smoke` (or a smoke `FAIL`, see below), `GUARD` lines
   saying `ok`, no `ERROR`. Each `WARN` line is an unresolved policy issue.
   - A smoke `FAIL` caused by **HTTP 502, 503 or 504** (for example `FreyaError: chat: HTTP 504 on POST
     /api/v2/chat/message`) is a **platform problem, not an agent problem**. The agent is deployed; accept it. (Both
     11 October public builds had exactly this.)
   - A smoke `FAIL` for an empty reply, a wrong opening or a forbidden word before verification is an agent problem.
     Look for a generic builder cause; fix and re-run if there is time.
3. **The policy the builder extracted:** `out/hidden-1.ir.json`. Compare it with the brief: identity questions, dates
   and holidays, deadlines per band, minimum amounts, outcome and reason names, transfer queues, fixed sentences.
   `out/hidden-1.ledger.json` lists the rules it read; every numbered rule of the brief should be there.
4. **The notes:** `results/hidden-1-<run id>.build.json` has `notes`, `tests_detail` and `errors`.

```sh
ls -t results/hidden-1-*.build.json | head -1
```

## 5. Optional manual chat check

The FAQ says to check a hidden agent with `POST /api/v2/chat/message`. The playground does that:

```sh
.venv/Scripts/python.exe -m streamlit run streamlit_app.py
```

In the browser: **Chat with agent**, Saved workflow `hidden-1`, Customer source **Custom variables**. Never pick
"Public practice case" for a hidden agent: it creates a customer on the practice bank, which only knows PUB-01 to
PUB-13 of the public briefs. With custom variables the bank tools will usually find no customer, so use the chat only to
check the opening, the right-party question and that nothing is disclosed before verification. Each message spends
Freya credits. Chatting does not change the agent.

## 6. Re-runs

Re-runs are allowed. Pick **one** run per brief and keep its trace.

- A re-run of the same command updates the **same workflow** in place (slot `hidden-1`). The live agent is always the
  one from the **last** run. So the run you submit must be the last run of that slot. You cannot go back to an earlier
  version; run again instead.
- `tee` overwrites `traces/hidden-1.log`. Keep the earlier output first:

  ```sh
  mv traces/hidden-1.log traces/hidden-1.run1.log
  ```

- The builder also writes its own `traces/hidden-1-<time>.log` and `.jsonl` for every run; keep them all.

## 7. Package the submission

The form paths follow the official example: the export copied to `submission/`, and the `tee` output in `traces/`.

```sh
cp out/hidden-1.exported.json submission/hidden-1.workflow.json
cp out/hidden-2.exported.json submission/hidden-2.workflow.json
cmp out/hidden-1.exported.json submission/hidden-1.workflow.json && cmp out/hidden-2.exported.json submission/hidden-2.workflow.json && echo "copies identical"
```

If the slot got a hash suffix (step 2), use the `exported` path from the JSON line instead.

**Consistency check with the submission helper.** `builder.submission` checks a local draft against the build results.
It takes the export and trace paths from each build result file, so its draft points to `out/hidden-N.exported.json`
and the builder's own `traces/hidden-N-<time>.log`. Those hold the same content as the form files above. Use the helper
only as a check; write the email from the template in step 10.

```sh
R1=$(ls -t results/hidden-1-*.build.json | head -1); R2=$(ls -t results/hidden-2-*.build.json | head -1); echo "$R1 $R2"
.venv/Scripts/python.exe -m builder.submission prepare --output submission/draft.json --force --team "Oğuzhan Güngör" --repo https://github.com/theFellandes/FreyaHackaton --run-command "python3 builder/build.py {brief_path} {tool_api_path}" --hidden-1-result "$R1" --hidden-2-result "$R2"
.venv/Scripts/python.exe -m builder.submission check submission/draft.json
```

Before the commit, `check` exits 2 with `Pending commit`; that is expected. Every other issue it lists needs a look.
After step 8, repeat `prepare` with `--commit <sha>` added and run `check` again: it should print `"ready": true`.
`prepare` and `check` use only read-only git commands, never contact Freya and never send email.

**Secret check.** Prints only file names, never a value:

```sh
.venv/Scripts/python.exe -c "import sys; from builder.settings import get_settings, secret_values; s = [v for v in secret_values(get_settings()) if len(v) >= 8]; [print(p, 'SECRET FOUND' if any(v in open(p, encoding='utf-8', errors='ignore').read() for v in s) else 'clean') for p in sys.argv[1:]]" traces/hidden-1.log traces/hidden-2.log submission/hidden-1.workflow.json submission/hidden-2.workflow.json
```

## 8. Commit and push

You run these yourself.

**Commit:** all source (`builder/`, `tests/`, `evals/`, `docs/`, `README.md`, `pyproject.toml`, `uv.lock`,
`requirements.txt`), `.claude/skills/`, `submission/hidden-*.workflow.json`, `traces/hidden-*.log`, the builder's
`traces/hidden-*-<time>.log` and `.jsonl`, `out/hidden-*`, `results/hidden-*.build.json`, and the hidden brief files in
`briefs/` (they are the inputs; the organisers' own command reads them from there). Never copy brief content into
`builder/`.

**Never commit:** `.env`, `env.sh`, `.venv*`, `data/key_health.json` (or anything new in `data/`), `.cache/`,
`traces/*.llm.jsonl` and `traces/*.agentic.jsonl` (full prompts). `.gitignore` already excludes all of these except a
new file in `data/` (only `data/key_health.json` is ignored there), so check `git status` for one.

```sh
git add -A
git status --short | head -50
git diff --cached --name-only | grep -E '(^|/)\.env$|env\.sh|key_health|\.llm\.jsonl|\.agentic\.jsonl|^\.venv'   # must print nothing
.venv/Scripts/python.exe -c "import subprocess; from builder.settings import get_settings, secret_values; s = [v for v in secret_values(get_settings()) if len(v) >= 8]; files = subprocess.run(['git', 'diff', '--cached', '--name-only', '--diff-filter=d'], capture_output=True, text=True).stdout.split(); bad = [f for f in files if any(v in open(f, encoding='utf-8', errors='ignore').read() for v in s)]; print(len(files), 'staged files checked;', ('SECRET FOUND IN: ' + ', '.join(bad)) if bad else 'no known secret found')"
git commit -m "Hidden briefs: phone agents, exports and traces"
git push origin master
git rev-parse HEAD
```

The SHA from `git rev-parse HEAD` goes into the form. Open the repository on GitHub at that commit and check that
`submission/hidden-1.workflow.json` and `traces/hidden-1.log` are there, and that the organisers can open the repository.

## 9. After 17:00: generic fixes only

Allowed after the briefs open: generic bug fixes to the builder, and re-runs. Not allowed: any rule, value, date, name,
sentence or outcome from a hidden brief in builder code, prompts, tests or fixtures; any hand edit of a phone agent or
an export.

Before committing a fix, run the literal scan. It reads every `briefs/*.md`, so the hidden briefs saved in step 2 are
included automatically:

```sh
.venv/Scripts/python.exe -m pytest tests/unit/test_compliance.py -p no:cacheprovider
```

To scan more files (for example the tool API files), name them in `COMPLIANCE_BRIEFS`, separated by `;` (Windows Python
uses `;` even in Git Bash):

```sh
COMPLIANCE_BRIEFS="briefs/hidden-1.openapi.json;briefs/hidden-2.openapi.json" .venv/Scripts/python.exe -m pytest tests/unit/test_compliance.py -p no:cacheprovider
```

A hit names a file and a literal. If the literal really comes from the brief, remove it from the code. If it is a
coincidence with generic code, write that in the commit message. Never edit the test. Then run the offline suite and
lint, and re-run the build of every brief whose agent was made before the fix, so the commit matches the agents:

```sh
.venv/Scripts/python.exe -m pytest tests -m "not live" -p no:cacheprovider
.venv/Scripts/python.exe -m ruff check builder tests evals
```

## 10. Send the email

Send before **20:00 UTC (23:00 Istanbul)** to the address in the kit email (`submission/EMAIL_DRAFT.md` has a draft;
confirm the address there against the kit email). Body, with your commit SHA:

```json
{
  "team": "Oğuzhan Güngör",
  "repo": "https://github.com/theFellandes/FreyaHackaton",
  "commit": "PASTE_THE_SHA_FROM_GIT_REV_PARSE_HEAD",
  "run_command": "python3 builder/build.py {brief_path} {tool_api_path}",
  "workflows": {
    "hidden-1": "submission/hidden-1.workflow.json",
    "hidden-2": "submission/hidden-2.workflow.json"
  },
  "traces": {
    "hidden-1": "traces/hidden-1.log",
    "hidden-2": "traces/hidden-2.log"
  }
}
```

Keep both placeholders `{brief_path}` and `{tool_api_path}` exactly as written. Check the team and repository values
against the kit email.

## Contingencies

| Problem | What to do |
|---|---|
| Freya 502 or 504 during the build | The builder retries writes (429, 502, 503, 504) by itself and repeats a failed smoke chat once. If the build still ends `deployed_unpublished` or `deployed_unexported`, wait 2 to 5 minutes and re-run the same command. |
| Smoke `FAIL` with HTTP 5xx, status `deployed` | Platform problem. Accept the run. |
| `All LLM providers failed`, status `failed` in `understand_brief` | The key changer tried every key. Read the `MODEL` lines. Add another key to `.env` (`OPENAI_API_KEY_2`, `ANTHROPIC_API_KEY`, ...) or wait for the rate-limit cooldown. A key wrongly marked dead: delete `data/key_health.json`. Then re-run. See [LLM key changer](LLM_KEY_CHANGER.md). |
| `blocked` or `failed` for a builder reason | Read `errors` and the trace. Fix the bug generically (step 9), run the tests, re-run the build. |
| `Workspace plan limit reached` | Too many workflows. Do not delete anything in a hurry. Keep `.freya-agents.json`; a re-run of the same slot updates in place. |
| Out of time | Submit what deployed. A `deployed` run whose smoke test failed on a 5xx can still be submitted; a late email cannot. |

## Related

[README](../README.md), [Submission](SUBMISSION.md) (background on the checker), [How it works](HOW_IT_WORKS.md),
[documentation index](README.md).
