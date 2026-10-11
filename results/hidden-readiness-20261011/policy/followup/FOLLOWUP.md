# Policy freshness follow-up

**The current floor-rule module passed all 17 offline cases**, with no failures, errors or skips. Only this module was rerun; the earlier 365-case report was preserved byte-for-byte. The eight literal probes were refreshed using the existing runner with its output/cache redirected to this follow-up directory.

[Follow-up JSON and hashes](policy-results.json) · [JUnit cases](pytest.xml) · [pytest output](pytest-summary.txt) · [wrapper used](run_followup.py) · [original policy report](../POLICY-READINESS.md)

The final probes completed at **2026-10-11T10:00:45.420347+00:00**. Both the targeted pytest phase and probe phase report stable source hashes. At the final freshness comparison, every captured source hash still matched the current file, including `builder/compiler/hooks.py`. Relative to the original policy-probe snapshot, `builder/understand/validate.py` and `tests/unit/test_floor_rules.py` changed. The original JSON's SHA256 before/after this follow-up is retained and equal in `followup.original_report_sha256_before` / `followup.original_report_sha256_after`.

The refreshed results retain the same boundaries:

| Probe | Current actual result |
|---|---|
| H01/H02: changed calendar/date, product-specific deadlines and supplied floor | All deadline values agree with literal expectations. Both comparisons fail only for `A-2_floor`: facts/offer text use `500.00` on debt `60.01`; reference effective floor is `60.01`. |
| H03: eight literal amount/date guard vectors | Every allow/block expectation matches; car `60.00` is blocked and `60.01` is permitted. The floor display inconsistency therefore persists while the bank-write guard remains restrictive. |
| H04: four remapped multi-product close examples | Reference helper and generated decision match all four literal outputs. |
| H05: reordered three-question identity/no overall flag | Repeated DOB and wrong reference do not unlock the account; the third distinct correct question sets `identity_all_verified=yes`. |
| H06: invalid callback minutes | `11:99` and `12:60` still pass with `guard=ok`; `20:00` is blocked. |
| H07: exact consent | Legal amount/date still pass with no stored bound confirmation. |
| H08: wrong-member parallel retry | Retry of acknowledged p2 still produces `promised_products=p2,p1,p3`, including p1 without a positive response in the explicit local batch double. |

These are synthetic local observations, not official hidden-brief or live customer verdicts. Passing floor regressions do not erase the separate floor wording, callback-minute, consent or parallel-attribution limits. Source changes after this capture require their own bounded follow-up; the original 365 tests were not repeated or represented as a current full-suite certification.

Reproduce this follow-up from the repository root:

```powershell
.venv-playground/Scripts/python.exe -B results/hidden-readiness-20261011/policy/followup/run_followup.py
```

The wrapper runs only `test_floor_rules.py`, then imports the unchanged existing policy runner, sets the follow-up output/cache directory and invokes its `--probes-only` path. `followup.wrapper_command` records this outer invocation; `probe_command` records the existing runner's inner argument list, which requires that wrapper's output override to preserve the original report. Temporary/cache/log files stay under `.cache/hidden-readiness-20261011/policy/followup`. The 17 cases overlap the earlier selection and must not simply be added to its unique count. No application source was edited and no network/LLM/platform call was made.
