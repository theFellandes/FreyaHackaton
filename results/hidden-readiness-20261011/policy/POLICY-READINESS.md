# Synthetic policy readiness: concrete local results

**365 focused offline pytest cases passed, with no failures, errors or skips. This is not an official hidden-brief verdict.** The official hidden originals were not supplied to this run. The novel probes below mutate the supplied A+B policy IR and API in memory; they do not test extraction from unfamiliar raw documents, a live LLM, a published Freya workflow or a customer's speech.

[Exact inputs, outputs, commands and hashes](policy-results.json) · [JUnit cases for deduplication](pytest.xml) · [pytest output](pytest-summary.txt) · [reproducible runner](run_policy_readiness.py)

The focused test run began at **2026-10-11T09:51:04.859633+00:00**. The final literal probes were captured at **2026-10-11T09:54:22.062768+00:00**. Each has its own source hash record in the JSON, and source/input hashes remained stable over the captured probe run. The test source snapshot is retained in `tests.source_before_sha256` / `tests.source_after_sha256`; later probe-run hashes are separate. The local readiness runner was edited between phases to extend the identity example, correct the hand-counted Sunday-calendar expectations and preserve complete floor outputs. Application source stayed unchanged between phases; the edits do not invalidate the earlier passing regressions. The final probe hashes include the exact final runner. The tests and probes used the working playground Python 3.12 environment.

## Changed calendar, bands, minimums and products

H01 uses **6 January 2027 at 13:20**, business weekdays **Sunday–Thursday**, and holidays **7 and 10 January**. Friday/Saturday are nonbusiness days. The next business day is **11 January**. Three products share one account:

| Product and literal input | Actual generated deadline | Actual generated floor |
|---|---|---|
| `C-7`, credit card, debt `399.99`, 4 days late; card-specific early deadline 3 business days; computed minimum 12.5% | `2027-01-13` | `50.00` |
| `L-9`, personal loan, debt `912.37`, 15 days late; ordinary late deadline 7 business days; instalment `75.35` | `2027-01-19` | `75.35` |
| `A-2`, car loan, debt `60.01`, 3 days late; ordinary early deadline 12 business days; supplied instalment `500` | `2027-01-26` | **`500.00`**, despite full debt `60.01` |

H02 changes only the call clock to **13 January 2027 at 09:05+03:00**. Actual deadlines recalculate to **18 January / 24 January / 31 January** respectively. Sunday counts as a business day in this synthetic calendar. The implementation keeps the written wall time and drops the offset; it does not convert between timezones.

H01/H02's literal comparison is **false only for `A-2_floor`**: the reference effective-floor invariant gives `60.01`, while generated supplied-field facts retain `500.00`. The deadline expectations match. This report keeps that discrepancy visible instead of converting the probe into a passing claim.

The generated account text actually says:

```text
A-2: car loan. Past-due amount 60.01, 3 days late (early band).
   Floor (instalment amount): 500.00.
   Floor offer - when the customer says the full amount is too much, say exactly: "I can offer you the instalment amount of 500.00 instead. The rest stays overdue."
```

The first offer also advises using `500.00` when the customer says the full `60.01` is too much. The hard guard behaves differently: it caps the effective minimum at the full debt. This is a **presentation/negotiation inconsistency**, not evidence that an excessive write was sent. Computed floor rules have their own cap in generated arithmetic; the discovered vector uses a supplied instalment field. Inspect [field-derived account facts](../../../builder/compiler/codegen.py:623), [computed-floor cap](../../../builder/compiler/codegen.py:835) and [guard cap](../../../builder/compiler/hooks.py:619).

H03 executes eight actual compiled pre-hooks. `allowed` below means the URL survives the guard; no HTTP request was sent.

| Exact request terms | Actual result |
|---|---|
| `C-7`, `49.99`, `2027-01-13` | Blocked; proposes `50.00`. |
| `C-7`, `50.00`, `2027-01-13` | Allowed. |
| `C-7`, `400.00`, `2027-01-13` | Blocked; proposes full `399.99`. |
| `C-7`, `399.99`, holiday `2027-01-07` | Blocked; proposes Monday `2027-01-11`. |
| `C-7`, `50.00`, `2027-01-14` | Blocked; proposes card deadline `2027-01-13`. |
| `L-9`, `75.35`, `2027-01-14` | Allowed: this date is within the loan's later deadline. |
| `A-2`, `60.00`, `2027-01-13` | Blocked; says only full `60.01` can be recorded. |
| `A-2`, `60.01`, `2027-01-13` | Allowed. |

All eight allow/block values match their independently supplied literal expectations. The JSON retains H01's complete generated state and H03's complete hook outputs for both car-loan boundary requests, including policy inputs, request bodies and prepared corrections.

## Changed closing labels and a changed identity contract

H04 renames the outcome/notes vocabulary in the IR and compares the reference outcome helper with generated closing code. These checks supply acknowledged records/state; they do not establish that a conversation produced those records or that a changed bank API accepts the labels.

| Supplied facts | Actual helper and generated result |
|---|---|
| One recorded promise plus another product's refusal | `commitment_saved / null` |
| Both known products noted `settled`, in reverse recording order | `settled / null` |
| Only one of two products noted `settled` | `nothing_agreed / declined_terms` |
| Notes recorded `y:declined_terms`, then `x:account_question`; account order `x,y` | `nothing_agreed / account_question` |

All four helper/generated pairs match the literal expectations. A missing paid note does not become all-paid, and account order controls the first nonpaid reason.

H05 changes identity from two questions to **three**, reorders them, extends the synthetic tool's `field` enum with **`reference_last6`**, and removes the response's overall `all_verified` flag. The bank verdicts are explicit mock inputs.

| Customer-shaped input and supplied bank verdict | Actual normalized answer | Distinct verified fields | Account-ready flag |
|---|---|---|---|
| Phone `12 34`, correct | `1234` | `phone_last4` | Unset |
| DOB `03/04/1980`, correct | `1980-04-03` | `phone_last4,date_of_birth` | Unset |
| Same DOB repeated, correct | `1980-04-03` | Same two fields | Unset |
| Reference `999999`, wrong | `999999` | Same two fields | Unset |
| Reference `12 34 56`, correct | `123456` | All three distinct fields | `identity_all_verified=yes` |

The final instruction says all security answers are verified and the call continues automatically. A repeated correct answer did not satisfy the missing third question, and the wrong reference did not unlock the account. This is actual generated pre/post-hook execution; customer answer extraction, partial/refusal speech and the live engine were not exercised.

## Fresh limits retained alongside passing tests

These are counterexamples/boundary observations, not three additional passing tests:

| Probe | Exact input | Actual output and practical limit |
|---|---|---|
| H06: callback time | At `2026-10-14T11:00`, propose same-day `11:99` or `12:60` | Both pass the complete callback pre-hook with `guard=ok`; invalid minutes are not rejected. `20:00` is blocked. |
| H07: confirmation | Legal promise `p1`, `100`, `2026-10-16`, no stored customer confirmation | Allowed. Legal terms are guarded; exact customer consent is not bound to the request in deterministic state. |
| H08: parallel retry | All pre-hooks first; p1 receives no post-hook, p2/p3 receive mock positive acknowledgments; retry already acknowledged p2 | Count rises `2 → 3`; `promised_products` becomes `p2,p1,p3`, falsely including p1, which never received a positive result. This is an explicit batch-order test double, not a fresh live engine measurement. |

The passing suite includes correct-member retry regressions; it does not refute H08's wrong-member counterexample. No application fixes were made during this validation.

## Reproduce and interpret the test evidence

From the repository root:

```powershell
.venv-playground/Scripts/python.exe -B results/hidden-readiness-20261011/policy/run_policy_readiness.py
```

The runner invokes nine focused modules: product-type bands, computed floors, state routes/dates, closing outcomes, generated calendar parity, hidden tool contracts, parallel acknowledgment staging, note acknowledgments and immutable recorded promises. `-m "not live"` excludes opt-in live tests; the existing test fixtures disable external sockets and developer settings. Pytest cache/temp files and its log are isolated under `.cache/hidden-readiness-20261011/policy`. XML/results stay in this report directory.

`--probes-only` refreshes the literal probes while retaining the recorded test result and its source hashes; it does not pretend to rerun pytest. The JSON contains the exact command for each observation. The runner exits with pytest's status, so inspect probe fields as well: H01/H02 report the floor inconsistency, H03–H05 match their bounded expectations, and H06–H08 expose limits without an aggregate pass label.

The **365** is this selection's execution count. Other team readiness runs may execute some of the same parametrized cases, particularly hidden tool contracts; deduplicate JUnit node identities before reporting a combined unique total. Neither that total nor these preassembled-policy probes prove official hidden-brief success.
