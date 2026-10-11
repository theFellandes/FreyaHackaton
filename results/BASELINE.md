# Baseline measurements (E-1, paper principle P3: measure the seed before rewriting it)

All runs use the kit's official scorer `tools/run_cases.py` (bank ledger + judge model), Freya staging,
`now = 2026-10-14T11:00` for every practice case.

## 1. Kit base agent (`examples/base-agent.json`) — all 13 practice cases, 1 rep

- Date: 2026-10-10 ~11:50 UTC. Workflow slot `base` (workflow `6b96bb63-65bd-483e-bf17-9b3df5460387`,
  agent `63880dc7-16d7-4b3a-b85a-946c131d81a8`). Simulation `fda21692-c4b9-4594-816a-1be3e5a5055d`, 219 s.
- **Result: 0 / 13** (the kit README reports 2/13 for one of its runs — variance).
- Slowest agent turn per call: median 3 s, worst 3 s (latency is not a problem with a 2-node agent).

| Case | Family | Tool calls | Why it failed |
|---|---|---|---|
| PUB-01 | Happy path | verify > verify > get_account > promise > close_call | text-message sentence not said after the promise (rule A-38) |
| PUB-02 | Third party | close_call | no callback scheduled; outcome not_reached/unclear instead of callback |
| PUB-03 | Identity | verify > verify > close_call | outcome no_promise/refused instead of not_verified/failed; app/branch not mentioned |
| PUB-04 | Dates | … > promise > close_call | promise on 2026-11-06 (after the late-band deadline 2026-10-23) |
| PUB-05 | Hardship | … > close_call | reason refused instead of hardship; support team not offered |
| PUB-06 | Amounts | … > promise > close_call | floor offered without "the rest stays overdue" note |
| PUB-07 | Dates | … > promise > close_call | accepted the holiday 2026-10-26 instead of 2026-10-27 |
| PUB-08 | Already paid | … > close_call | outcome missing (paid not in the base enum); "one business day" not said |
| PUB-09 | Restructuring | … > close_call | no transfer_to_agent(restructuring) |
| PUB-10 | Tool failure | … > promise > close_call | no retry after the first create_promise failure |
| PUB-11 | Multi-product | … > promise > close_call | only one create_promise for three products |
| PUB-12 | Multi-product | verify > verify > close_call | phone question not asked again after a wrong answer; closed as failed |
| PUB-13 | Multi-product | … > promise > promise > close_call | loan recorded on the customer's date instead of its own deadline |

Takeaways: every rule family matters; date/deadline/holiday logic, outcome tables, retries, notes and transfers must
be deterministic (code + routes), and fixed sentences from the brief must be said verbatim.

## 2. Kit example builder (`examples/agent_builder.py`, Claude Agent SDK, one skill) — Brief A

- 2026-10-10 ~12:05 UTC: **blocked** — the Claude Agent SDK's bundled CLI answered `Not logged in · Please run /login`
  (the desktop app login is not visible to the CLI and `.env` has no `ANTHROPIC_API_KEY`). Re-run once a key or a CLI
  login is available: `python examples/agent_builder.py briefs/brief-a.md`.

## 3. Hand-authored reference agent (evals/reference/brief_a_ref.py) — development fixture, never submitted

| Version | Brief | Runs | Strict score (all reps pass) | Notes |
|---|---|---|---|---|
| v1 | A | 1 | 9/10 | first try; floor offer missing "rest stays overdue" |
| v2 | A | reps 3 | 7/10 | "recorded" in opening (judge), callback stall, identity mixed answer |
| v2 | B (ref-ab) | reps 3 | 2/3 | PUB-12 attempt counting, goodbye loop |
| v4 | A | reps 3 | 9/10 | only PUB-05 2/3 (support team offered twice) |
| v4 | B (ref-ab) | reps 3 | runs 1-2: 3/3 each (run 3 crashed on a transient SSL error) | |

Lessons are written into the implementation contract addenda (scratchpad) and must be mirrored by builder/compiler.

### Later reference versions (official scorer; strict = a customer counts only if every repetition passes)

| Version | Run | Strict score | Notes |
|---|---|---|---|
| v5 | A, reps 3 | 9/10 | PUB-01 partial phone digits verified as wrong |
| v5 | B, reps 3 | 3/3 | |
| v5 | A styles pieces / in_words / asks_back (1 rep each) | 9/10 each | partial answers, holiday vs deadline, second offer after hardship |
| v6 | A, reps 3 | 9/10 | PUB-05 transferred to restructuring after hardship |
| v6 | A talkative (1 rep) | 6/10 | cut-off messages re-asked / closed as declined |
| v7 | A talkative, reps 2 | 7/10 | |
| v7 | A, reps 3 | 9/10 | |
| v7 | B talkative (1 rep) | 3/3 | |
| v8 | A talkative, reps 2 | 9/10 | hard promise guard (blank URL) + caps |
| v8 | A, reps 3 | 9/10 | PUB-04 1/3: guard blocked, model then closed as service_error (fixed in v10) |
| v8 | B talkative, reps 2 | 0/3 | each case 1/2; 2 hang-ups; slow platform (1,100-1,300 s runs) |
| v10 | A, reps 3 | incomplete | run 2: 8/10 (PUB-03 declined vs failed, PUB-05 restructuring); run 3 crashed on Freya 502 |
| v10 | A talkative, reps 2 | 6/10 | PUB-04/07 closed as refused with no promise, PUB-05 support offered twice, PUB-02 no close; median turn latency up to 21 s (platform slow) |
| v10 | B talkative, reps 2 | incomplete | run 1: 2/3; run 2 crashed on Freya 504 |

Verified platform behaviours from these runs: tool-call transitions (tool_call_routing), passthrough code nodes, computed close with an
unset reason dropped from the request, pre-hook vars persistence (E-8) and pre-hook URL blanking blocks a call without any request (E-9;
the post-hook does not run on that path).

## 4. AUTOMATIC BUILDER — first live acceptance (builder's own output, no hand edits)

Run 2026-10-10 ~22:22 UTC on the code after the quality review (before fix round 2). Command:
`python builder/build.py <briefs> tools/openapi.json --track compiled --self-test smoke`, then `tools/run_cases.py <agent> --brief X --reps 3`.

| Build | Build time | Model calls | Published + exported | Strict score (all 3 reps) | Failing reps |
|---|---|---|---|---|---|
| Brief A (slot brief-a, workflow 58bd49f0…) | 96.2 s in builder trace | 6 | yes, export_ok | **8/10** | PUB-03 2/3 (evaluator reports birthday prompts after the wrong-answer allowance was exhausted); PUB-07 2/3 (2 Nov recorded instead of 27 Oct after a holiday proposal) |
| Brief A+B (slot brief-a-brief-b, workflow b5638c83…) | 132.7 s in builder trace | 8 | yes, export_ok | **3/3 B amendment cases** | — |

These scores combine two different automatically built agents: A was tested on PUB-01–10 and A+B on PUB-11–13. They do not establish an 11/13 score for the A+B agent; its inherited ten A cases remain untested. Both live runs predate the later compiler/runtime repairs. Manifests, exports and traces agree on agent/workflow identities, but the scorer reports do not bind an exact source snapshot or workflow version. See [submission verification](../submission/VALIDATION.md) for the current offline check and remaining acceptance work.

### acceptance2 (final code, 11 Oct)

Run 2026-10-11 10:15 to 10:23 UTC on the final code (commit `6d75847` plus the uncommitted fix rounds; the trace says
`6d75847+dirty`), with the organisers' literal command `python3 builder/build.py <briefs> tools/openapi.json` and default
options (compiled track, staged profile, smoke self-test). `python3` is 3.14 on this machine; the builder handed the run
over to the repository `.venv` (Python 3.13.16), as its `note:` line on stderr shows. Files: `results/acceptance2/`.

| Build | Wall clock (trace) | Model calls | Result | Self-test |
|---|---|---|---|---|
| Brief A (slot brief-a, run 72c4f402f7ec) | 257 s (254.9 s) | 6 | deployed, workflow `58bd49f0-7417-413e-9f8b-19de1836e563` version 3, 49 steps, 7 tools, `export_ok: true`, exit 0 | static_checks PASS, remote_validate PASS, smoke FAIL: Freya HTTP 504 on `POST /api/v2/chat/message` |
| Briefs A+B (slot brief-a-brief-b, run 1135362f4cf4) | 209 s (207.4 s) | 8 | deployed, workflow `b5638c83-1842-4615-ab28-23834f631e2f` version 2, 46 steps, 8 tools, `export_ok: true`, exit 0 | static_checks PASS, remote_validate PASS, smoke FAIL: Freya HTTP 504 on `POST /api/v2/chat/message` |

Both smoke failures are platform timeouts (the chat request waited 60 s and got 504), not agent findings. **Scoring is
pending:** the first scoring attempts stopped when Freya staging returned 504 on the simulation endpoints
(`GET /api/v2/simulations/<id>`), and the re-score was still running when this was written. No customer score of the
final-code agents exists yet. **Update (13:02 UTC):** the Brief A re-score finished: **7/10** strict (runs 7/10, 9/10,
9/10; PUB-03 0/3, PUB-09 2/3, PUB-10 2/3; `results/acceptance2/score_A.log`). The A+B re-score was still running.

**Update (14:05 UTC), acceptance2 final:** the A+B agent scored 6/13 over all 13 cases (3 reps). This number is not a
fair test of that agent: under Brief B rule 8 a single-product customer without a promise gets an `add_note`, so PUB-05
(hardship) and PUB-08 (already paid) correctly carry a note that the Brief A expectations do not have. The real agent
misses in that run were PUB-03 (a complete date inside a cut-off message, fixed below), PUB-07 (1 of 3: holiday treated
as after the deadline), PUB-12 (platform errors made the agent re-ask) and hang-ups during 30-60 s platform delays.

### acceptance3 to acceptance6 (final code, 11 Oct, after the transcript-driven fixes)

Builds used the literal `python3 builder/build.py ...` command (handover to the `.venv`). Brief A cases were scored on
the Brief A agent, Brief B cases on the A+B agent. Extra scenarios are the 13 customers in `evals/extra_cases/`
(dispute, wrong number, declined identity, human, hardship with support, own amount, next Friday, rude, scam,
deceased; B: dispute on one product, paid + restructuring, next Friday + refusal), 1 repetition.

| Round (UTC) | Fixes in this build | Brief A, strict 3 reps | Brief B cases, strict 3 reps | Extras A / B | Notes |
|---|---|---|---|---|---|
| acceptance3 (13:38) | final fix round + cut-off rule order (PUB-03) | **10/10** | **3/3** | 9/10 / 3/3 | extras miss: "next Friday" asked as a vague period |
| acceptance4 (14:25) | + weekday is an exact day | **10/10** | 2/3 | 9/10 / 2/3 | B miss: PUB-13 loan got an attempt-1 date instead of its deadline; extras: security explanation said twice; a sandbox error ("Failed to reach snekbox") caused a duplicate note |
| acceptance5 (14:55) | + ask security questions plainly, B3 before B2 | 8/10 (runs 8, 10, 10) | runs 3/3, 0/3, 0/3 | **10/10** / 0/3 | B runs 2 and 3: every call cut short by the platform (customer hung up, no agent turn); extras B: the practice bank answered HTML error pages to create_promise and transfer_to_agent (the agent handled them honestly) |
| acceptance6 (15:26) | + a day named after a vague answer is a named day | not run | not run | not run | deployed (v7), smoke failed twice on Freya 504; a 3-case check got no agent reply at all for 60 s in every call (platform outage), so the last change is verified offline only |

Self-tests: static checks and remote validation passed in every build; smoke passed whenever Freya answered.
Offline suite at hand-over: 3387 passed, 4 xfailed; ruff clean. Files: `results/acceptance3/` to `results/acceptance6/`.
