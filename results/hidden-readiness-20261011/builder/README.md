# Synthetic builder readiness evidence — 11 October 2026

**653 selected offline tests passed, with zero failures, errors or skips. Actual hidden-brief readiness is not established.** The real hidden files were unavailable when this check began. This report uses public fixtures, invented inputs and controlled model/platform responses. No live provider, Freya deployment or customer simulation was performed, and no application/test source was edited.

The selected test run started at **09:50:57 UTC** and finished at **09:52:20 UTC**. Pytest reports **78.69 seconds**; the wrapper reports **83.188 seconds** including process startup. The interpreter was `.venv-playground/Scripts/python.exe` (Python 3.12). Windows asyncio required local socket setup outside the restricted execution sandbox; the exact offline run received automatic approval. Non-live tests retain the existing credential/dotenv/network guards.

## Reproduce and inspect the evidence

- [evidence.json](evidence.json): exact command, UTC timestamps, selected modules, all 653 test names/results, limits and source-change list.
- [command.json](command.json): the full argument vector; it runs `.venv-playground/Scripts/python.exe -m pytest` with the 17 modules below, `-m "not live"`, isolated `--basetemp`/cache and this directory's JUnit path.
- [pytest.stdout.txt](pytest.stdout.txt), [pytest.stderr.txt](pytest.stderr.txt), [pytest-junit.xml](pytest-junit.xml): captured process output and individual test evidence.
- [source-before.json](source-before.json), [source-after.json](source-after.json): **169 matching SHA-256 inputs**, including all builder Python files, prompts, selected test/support files, fixtures, synthetic inputs and configuration. `source_changed_during_run: []` means the compared before/after hashes matched; it is not continuous monitoring of every repository file.
- [parsed-synthetic-inputs.json](parsed-synthetic-inputs.json): independently parsed clean/raw invented material, normalized lengths, raw hashes, API fields and zero model/platform calls.
- [saved-synthetic-ir-validation.json](saved-synthetic-ir-validation.json): independent revalidation of two already-saved fixture builds, exact errors/warnings, source hashes, trace-declared `--fake-llm` provenance and routing symbols.

The capture helpers are under `.cache/hidden-readiness-20261011/builder/`. The test output, caches and temporary build/state/trace files are isolated under that directory or this evidence directory. Existing tests' fixtures redirect builder outputs and clear credentials; the wrapper additionally removes inherited Freya/model/observer credentials before imports or collection. [Hermetic guards](../../../tests/conftest.py), [graph doubles and isolation](../../../tests/graph/conftest.py).

## Selected tests and their actual scope

All counts include parameterized test variants.

| Module | Passed | What actually ran / evidence boundary |
|---|---:|---|
| [test_inputs.py](../../../tests/unit/test_inputs.py) | 36 | Real path/directory classification, API deduplication, fingerprints, slot collisions, missing/ambiguous inputs and unsafe slots. |
| [test_brief_loader.py](../../../tests/unit/test_brief_loader.py) | 35 | Real normalization and document parsing: Markdown/text/CSV/HTML/email attachments, DOCX/XLSX tables, PDF handling, encodings, input errors and ordered bundles. These tests do not interpret bank meaning. |
| [test_tool_api.py](../../../tests/unit/test_tool_api.py) | 22 | Real OpenAPI/Swagger/YAML parsing, schemas/references, fields/enums and malformed-input refusal. Missing expected tools can load with warnings; universal contract compatibility is not established. |
| [test_understand_fake_llm.py](../../../tests/unit/test_understand_fake_llm.py) | 42 | Real orchestration around a fake model: passage plans, quotes, completeness/requote, supersession/narrowing, extraction prompts, safe fixes, repair and prose. The fake supplies ledger/IR answers; this does **not** demonstrate real LLM extraction of unseen rules. |
| [test_validate_policy.py](../../../tests/unit/test_validate_policy.py) | 69 | Real policy/API/ledger consistency and mechanical correction on supplied fixture/mutated IR, including unusable amount-field errors and renamed vocabulary. Validator output is separate from whether routing makes it a fatal build failure. |
| [test_grounding.py](../../../tests/unit/test_grounding.py) | 12 | Real quote normalization, source matching, invented quotes/phrases and uncovered passages. Grounded quotation does not prove executable policy coverage. |
| [test_guardrails_input.py](../../../tests/unit/test_guardrails_input.py) | 67 | Real configured secret/injection/size/character/API-input guards on controlled cases. Passing these cases does not prove arbitrary malicious input safety. |
| [test_guardrails_output.py](../../../tests/unit/test_guardrails_output.py) | 164 | Literal grounding, dates/numbers/enums, speech/host/prompt/workflow safeguards and known exceptions. The current grounding-only best-effort override is exercised separately by graph tests. |
| [test_persuasion_date_cues.py](../../../tests/unit/test_persuasion_date_cues.py) | 36 | Selected lexical date families and abstention/correction behavior on synthetic text. This is not exhaustive English date understanding. |
| [test_budget.py](../../../tests/unit/test_budget.py) | 13 | Fake-clock monotonic budgets, estimates, reserves and optional-operation deadlines. |
| [test_build_cli.py](../../../tests/unit/test_build_cli.py) | 32 | CLI settings/entry points, invalid inputs/configuration, summaries/traces, requested versus missing-key forced dry run and logging redaction. Any fixture build remains a fake-model build. |
| [test_dry_run.py](../../../tests/graph/test_dry_run.py) | 8 | Local fixture CLI builds and artifacts, with no platform deployment. |
| [test_graph_runs.py](../../../tests/graph/test_graph_runs.py) | 36 | Full graph control flow with `FixtureGateway`/`FakeFreya`: repairs, fallback, best-effort versus hard blocks, self-test decisions, finalization and cutoff. Its “unseen brief” practice test patches applicability to `None`; it does not parse an actual hidden brief. |
| [test_nodes.py](../../../tests/graph/test_nodes.py) | 35 | Node boundaries, strategies, exception/cancellation reporting, statuses, round/version evidence and finalization. |
| [test_routing.py](../../../tests/graph/test_routing.py) | 19 | Pure routing conditions and caps/affordability; no actual model or platform operation. |
| [test_deploy.py](../../../tests/graph/test_deploy.py) | 23 | Real client/node against an in-memory platform: draft/publication/export/readback/retry/capacity decisions. It does not establish the current remote Freya contract. |
| [test_wall_clock_guard.py](../../../tests/graph/test_wall_clock_guard.py) | 4 | Controlled blocked/background platform calls, cutoff grace, retained deployment and interrupted self-test evidence. |

Incompatible input shapes are tested as parse/input errors, validation issues and controlled graph failures. This selection does not cover every compiler semantic-role refusal; contract-renaming/compiler-specific evidence belongs to the independently collected compiler readiness report.

## Real parsing of invented clean and raw materials

The independent parser reads the files under `evals/synthetic_briefs/`; these are explicitly invented banks. `expected_notes.md` is excluded from raw bank input rather than being passed as a brief.

| Variant | Material parsed | Observed result |
|---|---|---|
| `v01_clean` | One `brief.md` and its API | One normalized Markdown document, **11,608 characters**, seven expected tools, no missing/extra tools. |
| `v02_raw` | Email thread, amendment memo, policy extract, outcome CSV and API | Four nonempty normalized documents, **13,993 characters**, seven expected tools, no missing/extra tools. |

This establishes that these documents and contracts can be read, preserved and classified. It does not establish that the correct memo overrides, identity questions, dates, floors or outcomes would be inferred by a real model. Those semantic decisions are mocked in the selected understanding tests.

## Material finding: a `dry_run` summary can conceal unresolved validator errors

Two existing local task outputs were explicitly created with `--dry-run --fake-llm`; their trace start events record the fixture directories and model events record fixture models. The fixture directory contents were not independently audited here. The saved final IR, ledger and API were revalidated using the current real `validate_policy()`.

| Existing saved fixture build | Saved summary | Current final-IR validation |
|---|---|---|
| [`v-integ-v01-e639fd1ee443.build.json`](../../v-integ-v01-e639fd1ee443.build.json) | `status:"dry_run"`, `errors:[]`, local tests `1/1` | **4 errors and 2 warnings remain.** |
| [`v-integ-v02-1a498c53e17d.build.json`](../../v-integ-v02-1a498c53e17d.build.json) | `status:"dry_run"`, `errors:[]`, local tests `1/1` | **0 errors and 0 warnings** in these validator checks. |

The clean-spec fixture's exact remaining findings are:

1. **`INTENT_NOTE_BEFORE_VERIFICATION`**, `intents[3]`: `refuse_to_talk` is `any` scope but only writes note `rejected`. Before identity verification no note tool is available; that case needs the document-prescribed closing outcome rather than only a note action.
2. **Three `PRODUCT_NAMING_STATEMENT` errors**, `disclosure.product_naming.card`, `.loan`, `.vehicle_finance`: product names include `{past_due}` and `{days_overdue}`, although the surrounding debt statement already includes amount and days. The validator requires these entries to identify the product rather than duplicate the debt statement.
3. **Two `ROW_SHADOWED` warnings**, `closing.rows[19]` (`product_notes -> declined/rejected`) and `closing.rows[8]` (`hardship -> declined/vulnerable`): earlier rows for the same situation and verification state win, so those rows never apply.

These were not newly injected defects. The [original trace](../../../traces/v-integ-v01-20261011T094611Z.log) already records **4 errors/2 warnings after the second repair** at lines 64–65, then a policy guard with **zero violations** at line 70. The new [JSON revalidation](saved-synthetic-ir-validation.json) preserves the exact `PolicyIssue` objects and hashes of the final IR/ledger/API/result/trace/current validator and routing code.

The reason this continues is deliberate current routing:

```text
policy_issues contains errors
→ can_repair_policy is false after the two default repairs
→ fallback_target returns finalize because Agent SDK fallback is disabled
→ after_validate_policy deliberately converts that result to guard_policy
→ policy literal guard reports zero violations
→ prose/compile/static checks/guard_workflow
→ local dry_run finalization
```

See [`after_validate_policy()`](../../../builder/graph/routing.py), [`can_repair_policy()`/`fallback_target()`](../../../builder/graph/routing.py), [`validate_policy._body()`](../../../builder/graph/nodes/validate_policy.py) and [`finalize_run()`](../../../builder/graph/nodes/finalize.py). Validator issues are stored in `state.policy_issues`; the final error list is built from `state.errors`, so `errors:[]` does not imply there were no unresolved validator errors. This is the **validator best-effort path**, separate from the newer policy guard's grounding-only override.

The raw-material fixture passing its final validator checks establishes only that its supplied IR satisfies those checks. Neither fake-model build establishes real semantic extraction, actual hidden performance, a published workflow or a verified platform export. The `exported` paths in both dry-run summaries refer to local definitions.

## Remaining acceptance boundaries

No actual hidden inputs, hidden customer runs, live model extraction, remote publication/readback, clean-checkout evaluator reproduction or final submitted packet were tested. The green selection confirms tested local mechanisms, including intentionally permissive branches; it does not erase the four remaining v01 policy errors or prove exhaustive behavior. No fixes were attempted in this read-only readiness check.
