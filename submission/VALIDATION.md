# Submission preparation verification

Independent verification completed **11 October 2026 at 02:59 Istanbul** (10 October 23:59 UTC), after all six phases of Claude's workflow returned. These results describe the current uncommitted workspace. They do not certify a final submitted commit or hidden-customer performance. [verification.json](verification.json) records commands, environment, earlier runs and saved public evidence; [the settled snapshot](settled-source-snapshot.json) contains the 236 checked hashes.

## Settled offline results

| Check | Result |
| --- | --- |
| Full offline suite | **2,690 passed, 3 skipped** in 134.44 seconds; exit 0. |
| Ruff over `builder`, `tests` and `evals` | All checks passed; exit 0. |
| Source consistency | All **236** checked source, fixture, project-skill and configuration files unchanged between the before/after snapshots. |
| Official kit integrity | All 11 reviewed organizer files still match the archive hashes. |
| Git | HEAD `6d758474a8010f21b133cca0e8080b7053a9af7f`; index empty. No staging, commit or push performed. |
| Submission checker | Exit 1 as expected: draft is incomplete, with no selected commit or hidden-brief exports, traces and build manifests. |

Tests used the existing `.venv-playground/Scripts/python.exe` (Python 3.12.14, pytest 9.1.1), with bytecode writes and automatic plugin loading disabled, the asyncio plugin explicitly enabled, no pytest cache and a fresh temporary directory. The command was `python -m pytest -m "not live" -p no:cacheprovider -rs --basetemp <fresh-temp>`. The skips were one Claude Agent SDK test and two Langfuse tests because those optional packages were absent. No live API call or dependency installation was initiated by this verification.

One existing test wrote `results/s-test-run.build.json` outside its temporary fixture at the time of this verification, which preserved and restored that synthetic file. The defect has since been closed (AUD-59): every test's output folders now point at a temporary folder, and the stray file was deleted; see N-1 in [Claude's closure report](../results/QUALITY_REVIEW.md).

Earlier dated runs remain useful history: 2,470 passes/3 skips on a stable snapshot; 2,512 passes/3 skips while eight files changed; then 577 passes/7 failures/2 skips while further edits arrived. The seven gateway failures and two Ruff findings from that changing-source period do not persist in the settled suite above.

## What improved and what remains open

The settled suite covers semantic success flags, full-calendar promise guards, acknowledged note state, sequential per-product promise blocking, pacing after late thread wakeups, model-specific availability fallback, retry/deadline handling and multi-product continuation. Missing persuasion steps no longer cause the compiler to pad the ladder by repeating its last step. These are specific mechanisms, not proof that every policy or conversation is correct.

Fresh read-only compilation and in-memory probes still establish the following gaps:

| Issue | Verified limit | Backlog |
| --- | --- | --- |
| Identity | Partial and impossible answers can reach verification; some error/zero-remaining response combinations produce mismatch or retry wording. A decoy numeric balance can be inferred as the attempt counter. | F01, F06, F07 |
| Exact product IDs | With distinct IDs `p1` and `P1`, an exact request for `P1` can be rewritten to `p1`. | F03 |
| Consent | A valid promise request remains enabled without a stored readback and confirmation bound to its exact product, amount and date. | F16 |
| Date correction | The guard blocks the original holiday correctly but can accept a different legal date without proving it is the required correction of the customer's proposal. | F21 |
| Acknowledgment and closing | Sequential positive acknowledgments block duplicates, but closing still relies on engine success counters. Bank-negative/engine-positive reachability and late-response correlation remain unverified. | F04, F17 |
| Error privacy | A synthetic credential can remain in the build-result payload even when the trace and printed CLI JSON mask it; ordinary exception logging also has gaps. | F11 |
| Provider recovery | An open circuit with a lone cooling provider does not receive the same cooldown wait as a closed circuit. | F22 |

Additional limits are recorded in [Fixing issues](../docs/FIXING_ISSUES.md) and [Quality review](../results/QUALITY_REVIEW.md): identity refusal limits, callback-window enforcement, prompt-size warnings, uncertain writes, active HTTP completion, simulation-lock waits and permissive exports when remote read-back is unavailable. These probes do not establish an actual late live write, wrong remote export or the precise cause of a saved customer failure.

Claude's integration report records host and Docker dry runs for A and A+B, including container execution with networking disabled. This is separate producer evidence; this verification did not rebuild the image or test a clean checkout. The primary local `.venv` still points to an unavailable interpreter. Fresh locked installation and execution with the evaluator's credentials remain acceptance work.

## Saved public evidence

| Build | Strict customer score | Individual conversations | Builder trace | Model calls |
| --- | --- | --- | --- | --- |
| [A](../results/acceptance/score_A.json), agent `826cdf9b…`, workflow `58bd49f0…` v1 | **8/10**, all three repetitions required | 28/30 | 96.2 seconds | 6 |
| [A+B](../results/acceptance/score_B.json), agent `4ae00ca3…`, workflow `b5638c83…` v1 | **3/3 B amendment cases**, all three repetitions required | 9/9 | 132.7 seconds | 8 |

Build manifests, exports and traces agree on identities, publication and export success. Each build's three self-checks are static checks, platform validation and a two-turn smoke chat; they are separate from customer scoring. The A build has one static warning. [Baseline history](../results/BASELINE.md) preserves these runs.

These are **two different agents**. The ten inherited A cases were not tested on the A+B agent, so combining the rows does not establish an 11/13 result for that agent. Both builds and scores predate later source changes. The scoring reports do not bind an explicit workflow version or source hash, and no live run of the settled source was initiated here.

`PUB-03` and `PUB-07` each passed 2/3 repetitions, with their misses in repetition 3. The saved evaluator reason reports more birthday prompts after the wrong-answer allowance was exhausted; the bank-record comparison reports 2 November instead of the required 27 October after a holiday proposal. Full failed-call transcripts were not found locally, so exact payloads and routing causes remain unproven. These public-case values are evidence and must not become production constants.

## Final packet still required

The [readiness report](readiness.json) remains `ready: false`. Build the two released hidden briefs automatically with their own APIs, retain the actual exports and matching traces, select and review the packet, commit only when authorized, and confirm the recipient from the kit email. Use [Submission preparation](../docs/SUBMISSION.md) to regenerate the official JSON. No email was sent.
