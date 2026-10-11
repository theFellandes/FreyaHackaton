# Focused current-validator follow-up

This follow-up preserves the earlier [653-test capture](../README.md) as historical evidence. The policy validator changed after that capture, so only `tests/unit/test_validate_policy.py` was rerun. No application/test source was edited and no real model/platform call was made.

**69 tests passed, with zero failures, errors or skips.** The follow-up compared 144 relevant source/prompt/fixture/configuration/test hashes before and after the run; all matched. `tests/unit/test_floor_rules.py` was hashed to record its newer state but was not executed in this focused follow-up.

- [evidence.json](evidence.json): exact UTC start/end, Python version, command, elapsed time, counts, individual tests and source-change list.
- [command.json](command.json), [pytest.stdout.txt](pytest.stdout.txt), [pytest.stderr.txt](pytest.stderr.txt), [pytest-junit.xml](pytest-junit.xml): reproducible command and captured execution.
- [source-before.json](source-before.json), [source-after.json](source-after.json): current hashes, separate from the earlier capture.
- [saved-synthetic-ir-validation.json](saved-synthetic-ir-validation.json): read-only revalidation using the current validator, exact errors/warnings, artifact/source hashes, fixture provenance and routing explanation.

The saved fixture-build findings **persist unchanged**:

| Saved synthetic IR | Current errors | Current warnings | Meaning |
|---|---:|---:|---|
| `out/v-integ-v01.ir.json` | **4** | **2** | `INTENT_NOTE_BEFORE_VERIFICATION` for the any-scope refusal that only records a note; three `PRODUCT_NAMING_STATEMENT` errors for card/loan/vehicle-finance names containing debt amounts/days; two `ROW_SHADOWED` warnings. |
| `out/v-integ-v02.ir.json` | **0** | **0** | No findings from the current deterministic validator on this supplied fixture IR. |

Both saved builds declared `--fake-llm --dry-run` in their original traces. They remain synthetic interpretation fixtures, not real hidden-brief extraction or deployed workflows. V01's `status:"dry_run"` and `errors:[]` do not mean its IR passed validation: the current router deliberately continues to `guard_policy` after two policy repairs when Agent SDK fallback is disabled; finalization does not copy `policy_issues` into the final error list. The JSON records that same `guard_policy` route for the supplied exhausted-repair state. This is separate from the grounding-only guard exception.

Passing these 69 tests confirms the tested validator behavior at the recorded follow-up hashes. It does not eliminate the four v01 errors or establish actual hidden-customer readiness. Earlier files were left unchanged.
