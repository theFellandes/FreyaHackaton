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
