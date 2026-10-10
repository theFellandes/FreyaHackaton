# 12 — Lessons from τ^τ-bench (arXiv 2609.04611) for our builder

Paper: Quan Shi, Keshav Dhandhania, Karthik Narasimhan, Victor Barres, "τ^τ-Bench: An Environment for End-To-End,
Realistic Agent Construction", arXiv 2609.04611, 4 Sep 2026. <https://arxiv.org/abs/2609.04611>

## 1. Why it matters here

τ^τ-bench makes **agent construction** the task: a developer agent receives business records, an interactive client, a
client REST API, an inherited codebase and serving limits, and must deliver a customer-service agent that is scored by
deployment against **held-out simulated users** (final database state + rubric-judged messaging). That is exactly our
hackathon: brief (records) → builder (developer) → Freya workflow (constructed agent) → hidden simulated customers scored
on the bank ledger plus a judge.

Headline numbers: the best developer configuration (Claude Opus 5 in Claude Code) passes **23.9%** of evaluation
simulations against an expert reference ceiling of **82.2%**. Banking is hardest (5.9% vs 79.8% reference) because of
fact density (up to 580 atomic facts per task).

## 2. The six failure modes and what we do about each

| # | Failure in the paper | What it looks like in our setting | Countermeasure in our builder (see ARCHITECTURE / TASKS) |
|---|---|---|---|
| 1 | **Greps instead of reading.** Developers opened < 80 of ~1,700 files and built from keyword hits; shipped agents "simply do not know the policy". | Brief A says "Most failed cases come from one rule that was skipped." The hidden raw-material brief mixes a mail, a policy extract, an amending memo and a code list. | **Read everything, decompose into atomic rules.** The `understand` stage feeds every brief file in full to the model and produces a *rule ledger* (one entry per atomic rule, with verbatim quote and source). A deterministic **coverage audit** checks that every sentence/numbered rule of the brief maps to ≥ 1 ledger entry and every ledger entry maps to ≥ 1 enforcement mechanism (code, route, tool schema, prompt line, test). Amendments (memo overrides) are resolved explicitly into a *supersession log*. |
| 2 | **Never asks the client.** At most 4 questions; builds that asked ≥ 4 questions scored ~3× higher. | No client at run time, but ambiguities exist (e.g. third party vs wrong number; vague dates). During development we *can* ask the organizers (support channel). | (a) Dev time: maintain `OPEN_QUESTIONS` and actually send them to the organizer support channel. (b) Run time: an **ambiguity register** — each ambiguity gets an explicit interpretation chosen by rule (prefer the reading that keeps the call inside the brief; prefer the conservative close) and is written to the trace. |
| 3 | **Reads the seed, then rewrites it** without measuring it. Inherited airline seeds scored 0.12/0.36 untouched; nobody ran them. | The kit ships a working seed: `examples/base-agent.json` (2/13) and `examples/agent_builder.py` (10/10 on Brief A in one run). | **Measure the seed first** (P0 baseline task): run base agent and example builder on the practice set with `--reps 3` before writing our own. Reuse working kit code (`freya.py`, `run_cases.py`, `score.py`, the skill) instead of rewriting it. Keep the example builder as a **fallback track** inside our LangGraph builder. |
| 4 | **Defects, loud and quiet.** Loud failures noticed; quiet ones (pagination cursor, commit-then-timeout) missed; agents retried ambiguous writes or lied about status. | `create_promise` can fail (PUB-10 fault injection); `verify_identity` returns `hint: day_month_swapped`, `wrong_answers_left`, `ok:false` for unreadable dates; `get_account` refuses before verification; model invents `product_id`; API fields/enums change per hidden brief. | Tool schemas generated **from the tool API file** (never hard-coded fields/enums). Post-hooks add `what_to_do_next` to every result; failure counters route deterministically (retry once, then `service_error`). Honesty guard: "recorded" only on `ok: true`. Product ids injected by code, not typed by the model. |
| 5 | **Budget math ignored** (overspend penalties zeroed scores; or half the budget left unused). | Budgets: **30 min build limit**, build time is tie-break #2, one simulation at a time (3–11 min per run), Freya credits, Anthropic spend, agent latency (customers hang up on slow turns). | A **time budget manager** in the graph (deadline-aware: skips optional self-tests when the remaining time is short). Lean prompts (latency). Trace records time per stage. Promised amount is tie-break #1 → always ask for the full amount first (also a brief rule). |
| 6 | **Tests that cannot fail.** Developers weakened failing assertions or faked the API to match their agent. | Temptation to "fix" the judge's checks or to tune to the 13 public cases. | Ground truth comes from the **official** scorer (ledger + judge), never from our own agent. Never edit expected outcomes to match behaviour. **Generalisation suite**: synthetic variant briefs we author (other holidays, bands, identity questions, outcome names, raw-material form) with hand-labelled expected policy IR; the builder is tested on them offline. Never tune on the public cases alone. |

## 3. Additional findings worth acting on

- **Architecture matters more than the default tool loop.** 92% of builds were a single LLM tool loop; a one-line hint
  ("route by intent, review tool calls") doubled a telecom score from 31% to 67%. Builds that gate output behind
  deterministic policy guards were the stronger ones. → Our compiled workflows use stage nodes, deterministic routes on
  tool results, code nodes for all arithmetic/dates, and post-hook guards. We **A/B at least two agent architectures**
  (lean vs staged) on the practice set rather than shipping the first design that runs.
- **Developers stop early.** Twelve runs submitted with half the clock left. → Use the remaining build time for one more
  validation pass when it is cheap (but respect the 30-min limit and the build-time tie-break).
- **Cheating attempts were common (17–42% of runs)** — probing graders, mining hidden data. → Our rules forbid probing the
  platform; the builder must never read hidden-test data or embed hidden-brief content. The trace makes the build auditable.
- **Expert reference agents use their serving budget fully (0.96×)** and a wider variety of designs. Our serving model is
  fixed (`experimental-5`), so the levers are structure, prompt economy and deterministic code.

## 4. Builder principles derived from the paper (used throughout the plan)

1. Read the whole brief; decompose into atomic rules; prove coverage.
2. Resolve conflicts and amendments explicitly; log interpretations.
3. Measure the seed and reuse what works.
4. Generate tool contracts from the tool API file; handle loud and quiet tool failures in code.
5. Put rules in routes and code; keep prompts short; guard outputs deterministically.
6. Validate against ground truth you did not author (official ledger + judge), plus a held-out generalisation suite.
7. Explore at least two architectures with measurements before committing.
8. Respect budgets (30 min, one simulation at a time, latency) with an explicit budget manager and a trace.
