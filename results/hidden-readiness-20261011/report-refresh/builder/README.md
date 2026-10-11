# Policy validation and final-summary gap — offline refresh

The current routing and finalizer permit unresolved policy-validation errors to continue and produce `status: "dry_run"`, `errors: []`, and `tests: {passed: 1, total: 1}`. This refresh executed the actual current pure routing helpers and the finalizer's already-finalized path against a constructed state containing the previously captured v01 issues. It did not run a new build, compile a workflow, validate the supplied lint verdict, call a provider, or call Freya.

[Machine-readable evidence](policy-validation-refresh.json) contains UTC times, exact issues, original-artifact availability, source hashes before/after, source-line anchors, and each helper result. [Reproducible runner](refresh_policy_validation.py):

```powershell
& .\.venv-playground\Scripts\python.exe results/hidden-readiness-20261011/report-refresh/builder/refresh_policy_validation.py
```

The eight audited source/test files did not change between the refresh's before/after hash captures. The current validator, router, validation node, and finalizer hashes also match the preserved [10:00:43 UTC followup validation](../../builder/followup/saved-synthetic-ir-validation.json). The original `out/v-integ-v01.*`, `out/v-integ-v02.*`, build-result JSON files, and traces are now absent. Consequently, **4 errors / 2 warnings for v01 and 0 / 0 for v02 are the last actual final-IR revalidation counts, not a new IR revalidation**. The synthetic API files remain available.

| Captured v01 issue | Exact path / value | Concrete implication |
| --- | --- | --- |
| Error `INTENT_NOTE_BEFORE_VERIFICATION` | `intents[3]`: `refuse_to_talk`, scope `any`, note `rejected`, `outcome: null` | The IR does not specify the required close outcome for this refusal before identity verification, where the note tool is unavailable. A conversation failure was not replayed in this refresh. |
| Error `PRODUCT_NAMING_STATEMENT` | `disclosure.product_naming.card` includes `{past_due}` and `{days_overdue}` | The name already contains the amount and lateness that the debt statement adds separately. |
| Error `PRODUCT_NAMING_STATEMENT` | `disclosure.product_naming.loan` includes those same fields | Same duplicated-statement defect. |
| Error `PRODUCT_NAMING_STATEMENT` | `disclosure.product_naming.vehicle_finance` includes those same fields | Same duplicated-statement defect. |
| Warning `ROW_SHADOWED` | `closing.rows[19]`, `product_notes -> declined/rejected` | An earlier row wins for every applicable verification state, so this row never determines the outcome. |
| Warning `ROW_SHADOWED` | `closing.rows[8]`, `hardship -> declined/vulnerable` | Same unreachable-row defect. |

The current validator implements these conditions at [validate.py](../../../../builder/understand/validate.py), lines 877–883, 987–997, and 1004–1010. The exact issue messages and ledger rule IDs are in the JSON.

The path is deliberate in the current implementation:

1. The [validation node](../../../../builder/graph/nodes/validate_policy.py), line 58, stores issues in `policy_issues`; it does not append them to `errors`. The trace records their counts and formatted messages at lines 31–37.
2. [Routing](../../../../builder/graph/routing.py), lines 132–141, attempts repair while rounds and time remain. After the default two repairs, when agentic fallback is unavailable, it changes the fallback's `finalize` target into `guard_policy`. The existing [routing test](../../../../tests/graph/test_routing.py), lines 64–80, explicitly expects this best-effort continuation. The current helper probe returned `guard_policy` for both exhausted repairs and an unaffordable repair/fallback.
3. A clear policy guard continues to `draft_prose` at routing lines 148–149. The historical v01 trace had zero guard violations; semantic validation errors are a different check from numeric grounding, enums, secrets, hosts, and injection checks.
4. [Finalization](../../../../builder/graph/nodes/finalize.py), line 147, copies `state.errors`; lines 172 and 200 emit that list in the CLI result and trace summary. `collect_tests`, lines 81–109, counts static checks, platform validation and self-tests, not policy validation. `decide_status`, lines 58–78, does not consult `policy_issues`. The persisted build result, lines 215–222, also omits `policy_issues`.
5. [CLI exit selection](../../../../builder/build.py), lines 574–578, uses the status and forced-dry-run flag, not the unresolved issue list. Thus an explicitly requested dry run can exit successfully with this gap. This is a source finding; this refresh did not execute the CLI.

The policy guard has a **separate** best-effort exception at [guard_policy.py](../../../../builder/graph/nodes/guard_policy.py), lines 38–59: when no repair remains or is affordable and every blocking finding is an ungrounded date, amount or number, it clears the routing block and adds a decision message to both notes and errors. That exception therefore does report a message. It does not explain the captured v01 `errors: []`; v01's guard had no blocking findings. Hard or mixed guard violations remain blocked. The module docstring's “validator accepted it” and “an unvetted policy is never compiled” wording should not be treated as an accurate description of all current routes; the executable branches govern behavior.

This is a material reporting and acceptance boundary: a caller reading only the final summary cannot tell that semantic policy validation failed, and `1/1` is a static-check result rather than proof of correct collection conversations. With dry run disabled, the same unresolved-validation route can reach later platform steps if their own checks accept the workflow; a live deployment with these particular issues was **not** demonstrated. The historical inputs and model responses were synthetic fixtures (`--fake-llm`, trace model `fixture/fixture`). None of this establishes real extraction accuracy, hidden-brief readiness, or official customer scores.
