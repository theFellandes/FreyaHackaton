# 11 — Hackathon rules, scoring and kit v3 (authoritative; supersedes 01–10 where they disagree)

Sources: <https://www.freyavoice.ai/hackathon> (Overview, Data, Evaluation, Submission, Limits, Rules), kit v3 at
`data/build-the-builder-v3/build-the-builder/` (README.md, briefs/, docs/api.md, docs/workflow-format.md,
docs/tools-and-code.md, tools/openapi.json, tools/run_cases.py, tools/score.py, freya.py, examples/, public-test-set.json,
.claude/skills/build-phone-agent/SKILL.md). Read on 2026-10-10.

## 1. What changed versus our first research pass (01–10)

| Earlier assumption | Authoritative fact (kit / hackathon page) |
|---|---|
| No public API to create or update workflows | `POST /api/v2/workflow` creates (the agent is created with it), `POST /api/v2/workflow?dryRun=true` validates without saving, `PUT /api/v2/workflow/{id}` replaces the whole definition (saves an unpublished version), `POST /api/v2/versions/workflow/{id}/publish {versionId}` publishes. `freya.save_workflow(slot, wf)` does create-or-update-and-publish |
| Overwrite the seeded base workflow | Create our own workflows per slot and update them in place (200-workflow cap per workspace) |
| Four tools, close_call as an end-of-call lifecycle tool | Seven tools: `verify_identity`, `get_account`, `create_promise`, `add_note`, `schedule_callback`, `transfer_to_agent`, `close_call`. `close_call` must be the **last tool call, exactly once** |
| Voice calls; Turkish STT/TTS risk | **Text** simulations (`mode: "chat"`). Leave `agent_config` out of the workflow (platform fills it; `freya.update_workflow` re-sets the model). Voice config is irrelevant |
| `{{now}}` undocumented | `now` is a call variable on every call (e.g. `2026-10-14T11:00`). Every date rule uses it, never the real clock |
| Outcome enum promise/no_promise/not_verified/not_reached | Outcome table comes from the brief (Brief A: promise, paid, transferred, no_promise, not_verified, not_reached with reasons). Hidden briefs bring **another outcome table** |
| Our own LLM-simulated customers via chat API | Official harness: `tools/run_cases.py` creates personas, starts a text simulation, reads the bank **ledger** via `/v1/practice/ledger/{customer_id}` (needs `FREYA_BANK_TEAM_KEY`) and the judge verdict per run |
| Message roles unknown | `GET /api/v2/call/{id}` → `messages[]` with `role` assistant/user/system; system messages are JSON engine events: `node_enter`, `function_call_start` (full request), `function_call_complete`, `route_evaluated` (branches + `variable_table`), `error` |

The detailed staging/OpenAPI notes in 01–10 remain useful background (personas, simulations, unit tests, insights,
version history), but the kit docs are the reference for workflow JSON and the builder loop.

## 2. The task, precisely

- Builder input: one brief file (Markdown, or **raw bank material**: a mail, a policy extract, a memo that changes it, a
  code list) **plus its own tool API file** (OpenAPI). Brief B "builds on Brief A", so the builder must accept several
  brief files. Hidden briefs: "other numbers and limits, other identity questions, another outcome table, another
  calendar". The seven tool names stay; their **fields and allowed values can change**.
- Builder output: a published Freya workflow (phone agent) + exported workflow file + a trace.
- Run command used by organizers (submission form field `run_command`):
  `python3 builder/build.py {brief_path} {tool_api_path}`.
- Limits: ≤ 30 min per brief including self-tests; one simulation at a time per workspace (a 10-case run takes 3–11 min);
  phone agent model fixed (`AGENT_LLM` = `experimental-5` in `freya.py`, do not change); builder may use any model.
- Rules: no manual step, no hand edits after the run; the agent may call only the bank tool API; after hidden briefs open
  you may fix bugs but may not write anything from a hidden brief into the builder (no rule, value or name); do not
  probe or load-test the platform; one Freya key per team; synthetic data only.

## 3. Scoring

A customer passes only if all hold (no partial credit):
1. **Outcome** — the bank's ledger is right: outcome + reason, every promise (product_id, amount, date) recorded once,
   every note, callback and transfer as the brief requires; `close_call` is the last call; no `get_account` /
   `create_promise` before verification; no identity/account tools when the right person was not on the line
   (`tools/score.py`).
2. **Policy** — the conversation stayed inside the brief (judge model over the transcript, per-case checks).
3. **Honesty** — e.g. never says "recorded" when the tool call failed (judge model).

Hidden benchmark: 2 briefs × 30 customers (1–3 products), **several calendar days**, **3 runs per case, all must pass**,
score = passed / 60. Customers "talk like people": numbers in two parts, self-corrections, off-topic talk, questions
back, amounts and dates in words (`run_cases.py --style pieces|corrects|talkative|asks_back|in_words`).
Tie-breaks: (1) total promised amount on passed cases, (2) shorter total build time (from traces), (3) earlier submission.
The persona/judge model in practice runs: `google / gemini-3-flash-preview`.

Hints in `run_cases.py` about hidden identity questions: `postcode`, `birth_year`, `company_number`, `founded_year`
(business customers are plausible).

## 4. Timeline (UTC)

| When | What |
|---|---|
| Sat 10 Oct 10:00 | Kit and keys; build against the 13 practice cases |
| Sun 11 Oct 17:00 | Hidden briefs open — run the builder once per brief, fix crashes only |
| Sun 11 Oct 20:00 | Submission closes (form: team, repo, commit, run_command, workflows, traces) |

## 5. Trace requirements

One trace per hidden brief, committed (keys removed): start and end time; every model call (model + one-line purpose);
what was created on Freya (workflow id, agent id, number of steps and tools); any self-test and its result. Optional
coding-agent session logs. Top-10 builders are re-run by organizers and compared with the trace.

## 6. Practice set (public-test-set.json)

13 cases, all on Wed 2026-10-14 11:00: PUB-01 happy path; PUB-02 relative → callback; PUB-03 three wrong answers;
PUB-04 date after deadline; PUB-05 hardship declines support; PUB-06 floor accepted; PUB-07 vague period then holiday;
PUB-08 already paid; PUB-09 restructuring transfer; PUB-10 `create_promise` fails once (fault injection); PUB-11..13
Brief B multi-product (one date for all; partial promise + refused notes; different deadlines). Base agent passes 2/13;
the example builder (kit v3) passed 10/10 on Brief A in one run.

## 7. Platform behaviours that drive design (from kit docs)

- Three model calls per turn: extraction → routing → dialogue. Routing reads only transitions; `task_message` cannot route.
- Deterministic conditions (`equals`, `greater_than`, `is_set`, …) need no model call; booleans arrive as `"true"`/`"True"`.
- `code` nodes (passthrough, `code_exec` in `pre_actions`) compute facts into `vars`; strings written there resolve in later
  prompts. No `{{`/`}}` inside code; never let code raise.
- `api_request` nodes run tool calls on entry (engine-fired, cannot be skipped); `tools` on `conversation` nodes are
  model-called. `response_mapping` paths start with `data.`.
- Post-hooks (`post_process_code`) can add a `what_to_do_next` line to a tool result — fixes silent turns and ignored
  negative results with `experimental-5`.
- `end_call` nodes: farewell comes from the terminal; `endedReason` = terminal `function.name`.
- No `null` anywhere; unknown fields are silently stripped (read back after save); `context_variables` was dropped on save
  in a 10-Oct test.
- Customers hang up after long silences; slow model turns (> ~10 s median) cost passes.
