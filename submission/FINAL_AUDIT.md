# Final preparation audit — 11 October 2026

The preparation uses the **one-case Turkish final** and its eight fields: team, members, repo, commit, run_command,
workflow, trace, mock_url. The official [Submit, Rules and FAQ](https://www.freyavoice.ai/hackathon#submit) were fetched
again during this audit and agree with the saved requirements. No final ZIP was downloaded before release.

Use [FINAL_RUNBOOK.md](FINAL_RUNBOOK.md) as the procedure. The kit opens at **20:00 Istanbul**; the hard deadline is
**23:00 Istanbul**. The release follow-up stays **paused**, as explicitly requested by the user. The user sends the email.

## Checks completed

| Check | Evidence and limit |
| --- | --- |
| Submission downloader, capture, sealing and packet validation | 31 isolated tests passed. Sandbox directory permissions and a temporary Windows file lock caused initial audit errors; clean isolated reruns passed. |
| Mock preparation guards | 9 local Node tests passed. The skeleton returns HTTP 503 with ready/bank_ready false; it cannot be accepted as a working bank. No final contract or deployment exists yet. |
| Builder adaptation | 213 focused offline tests passed, covering inputs, document roles, contract derivation, mock URL routing, CLI, legacy compatibility and header handling. A later recheck passed all 24 current contract tests. Claude changed source/tests during the audit, so the broader result is evidence for the loaded snapshot. |
| Cloudflare access | Wrangler whoami verified an OAuth login and one account. Codex's Cloudflare MCP was registered and authenticated. Seventeen official Cloudflare skills were installed globally. No tokens were printed or committed. |
| Cost constraint | No paid plan was enabled and no service was deployed. Target Workers Free and free persistent storage. LLM and Freya usage is separate from Cloudflare hosting. |
| Source checkpoint | Existing commit 3cec449a841eba5a40205c77f2d69f512174d145 contains the first adaptation checkpoint. Claude's subsequent generic fixes remain in progress. |
| Submission form and docs | draft.json now has the current singular form; old draft/readiness and two-brief docs are preserved under history. README and docs pointers lead to the canonical runbook. |

## Claude's final adaptation check

No builder source or test was edited by this audit. Before using the adaptation, its owner should rerun the focused
tests and lint after changes settle. Two findings require attention:

- `builder/understand/contract.py` checks path segments separately. A nonexistent recombination of existing segments
  and a changed HTTP method both passed `check_derived_document` in the final audit recheck. Validate exact HTTP method/path pairs against the prose contract;
  add a generic regression and fix in the owner's active adaptation if still unresolved.
- The latest observed lint had SIM108 at `builder/ingest/tool_api.py:425` after the relative-server fix. This is a
  moving-source finding; rerun lint on the finished code.

Compiled headers include fixed values declared by the contract. Required credentials without a fixed value and
OpenAPI security schemes are omitted with notes. `FREYA_BANK_TEAM_KEY` is not automatically sent in compiled tools.
Validate the final contract's intended authentication and every tool URL before selecting a run.

## The 0% simulation rows

The saved [A+B score log](../results/acceptance5/score_AB_B.log) records 3/3 passing in the first repetition, followed
by two 0/3 repetitions whose calls ended early. The [latest score](../results/acceptance6/score_dates.json) records
nine calls with no tool execution, all ended by the customer. Its [build log](../results/acceptance6/build_A.log)
contains two approximately 60-second HTTP 504 smoke failures. This supports a likely Freya runtime/service failure.
It does not prove the underlying cause or validate the new adaptation: raw transcripts and an exact version binding
are missing. The scorer's zero latency value here means no measured reply, not a fast response.

## Still needed

- Fill the real member email in draft.json and confirm the recipient from the kit email. Both remain unconfirmed.
- Complete Claude's adaptation checks and preserve a source checkpoint before release.
- After release, read BRIEF.md then README.md; implement and test the actual contract/customer mock, including
  persistent isolated state. Deploy and verify it before the selected builder run.
- Keep original downloads unchanged. Use a reviewed committable case folder: packet validation requires the exact
  case inputs, builder source and deployment evidence in the artifact commit, alongside the sealed export and trace.
- Capture a successful final run, seal its export/trace/result, commit and push the evidence, validate submission.json,
  and have the user send it before 23:00 Istanbul. Keep the Worker available through results.

Preparation is **not ready to submit** while those facts and final artifacts are absent. See
[preparation-status.json](preparation-status.json) and [readiness.json](readiness.json).
