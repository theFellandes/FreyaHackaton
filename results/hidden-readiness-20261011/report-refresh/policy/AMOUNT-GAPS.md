# Current floor and amount-unit gaps

**The supplied-floor wording discrepancy remains in current source. Amount adaptation also assumes 100 minor units per major unit.** The second limitation fails a deliberately supplied 1000-unit contract; it is a conditional compatibility gap, not a claim about the unseen official hidden API.

[Fresh exact inputs/outputs and source hashes](amount-results.json) · [offline probe runner](probe_amount_gaps.py) · [earlier policy evidence](../../policy/POLICY-READINESS.md)

Captured **2026-10-11T10:30:07.167849+00:00**, with identical source/input hashes before and after. Five bounded local probes compiled the supplied A+B fixture or a declared synthetic variant and executed its generated code/pre-hooks. No pytest suite, raw-document extraction, LLM, Freya request or bank write was run for this refresh. Existing earlier reports were preserved. `allowed_to_send` means the pre-hook retained its URL in the sandbox.

## 1. A supplied minimum above the debt still produces an excessive spoken offer

F01 supplies a car loan with `past_due=60.01`, `days_late=3`, `instalment_amount=500`, at `2026-10-14T11:00`. The policy maps car-loan minimums to `instalment_amount`, with no computed floor rule for that type.

Actual generated facts retain `floor=500.00` and tell the agent:

```text
Floor (instalment amount): 500.00.
Floor offer - when the customer says the full amount is too much, say exactly: "I can offer you the instalment amount of 500.00 instead. The rest stays overdue."
```

The first attempt also advises using the `500.00` instalment when the customer says the full `60.01` is too much. Actual complete promise pre-hooks behave as follows:

| Proposed amount/date | Actual guard |
|---|---|
| `60.00`, `2026-10-16` | Blocked; says only full `60.01` can be recorded. |
| `60.01`, `2026-10-16` | Allowed. |
| `500.00`, `2026-10-16` | Blocked; says only full `60.01` can be recorded. |

This is a concrete inconsistency between offer text and permitted terms. The hard guard prevents the excessive promise; the customer may still be offered an impossible or confusing minimum before correction. F02 supplies the same product plus `FloorRule(fixed=500)` and gets `floor=60.01` in the facts and offer text. The cap exists for that computed-rule path, which does not cover the supplied-field-only path.

Source: [field-derived floor read](../../../../builder/compiler/codegen.py:623), [floor offer construction](../../../../builder/compiler/codegen.py:650), [computed-rule cap](../../../../builder/compiler/codegen.py:835), [hard guard cap](../../../../builder/compiler/hooks.py:621). Both compiled variants produced no lint errors; lint cleanliness did not detect the inconsistency.

## 2. Currency scale is fixed at cents

U01 is the supported control. Its integer account amounts and promise amount explicitly use **100 minor units per major unit**:

```text
account past_due=36500, minimum_payment=10950
→ generated debt=365.00, floor=109.50
model amount="120.50"
→ request amount=12050, allowed_to_send=true
```

U02 changes that explicit API description to **“Amount in minor units; 1000 minor units equal one major currency unit.”** The account now returns `past_due=365000`, `minimum_payment=109500`, still representing major amounts `365.00` and `109.50`. Compilation succeeds with no lint errors. Actual output is:

| Stage/input | Expected under the explicit synthetic contract | Actual generated output |
|---|---|---|
| Read `365000` debt | `365.00` major units | **`3650.00`** |
| Read `109500` minimum | `109.50` major units | **`1095.00`** |
| Propose major `120.50` | Request `120500`; eligible | Request **`12050`**; blocked below the incorrectly read floor |
| Propose major `1095.00` | Above the actual `365.00` debt; reject | Request **`109500`**; allowed by the incorrectly scaled guard |

That last retained request represents **109.50** under the explicit 1000-unit contract, although the model's proposal was **1095.00**. No request was sent, but the complete pre-hook demonstrates a tenfold disagreement between customer-shaped terms and bank units. This is more consequential than a display-only mismatch if a final API uses that scale.

[Minor-unit detection](../../../../builder/compiler/roles.py:101) returns a boolean from field names/descriptions; it does not retain a scale. [Account adaptation](../../../../builder/compiler/codegen.py:839) treats integer minor values as cents. [Promise coercion](../../../../builder/compiler/hooks.py:183) uses the generated two-decimal cent parser. U03 independently confirms that the reference money helper changes `120.505` into **`120.51`**; [its precision is fixed to `0.01`](../../../../builder/policy/money.py:51). These observations support cents-scale contracts; they do not establish arbitrary per-currency precision or scale support.

The report retains the exact synthetic description and account/promise inputs, full generated account text, request bodies and guard corrections. A plain integer type alone does not identify minor units, and unfamiliar scale/wording needs its own contract check. The unseen official hidden contract may continue to use cents; this probe does not predict otherwise.

## 3. Critical customer and hidden-input evidence still missing from this refresh

| Required evidence | What this refresh actually establishes | Remaining check |
|---|---|---|
| Final builder reading official hidden documents/API | Preassembled IR/API variants can exercise selected generated mechanics. | Run extraction on the actual raw inputs; review grounded ledger/IR, amounts, calendar, amendments, field roles and units before judging the compiled output. |
| Latest source/version passing customer conversations | Generated facts and pre-hooks execute locally with chosen inputs. | Repeated current conversations on the published/exported version, with transcripts and bank records. Earlier saved A/B results belong to their older runs/agents. |
| Correct date/amount extracted from customer speech and accepted terms | The supplied model-shaped amount/date is normalized and guarded. | Confirm original proposal correction, exact read-back/clear yes, and its binding to the actual write. Local helper success does not prove those conversational steps. |
| Failed/parallel bank writes attributed to the correct product | This refresh covers amount pre-hooks only. | Current matching acknowledgments/retry behavior with request identities and actual bank evidence; the earlier explicit wrong-member batch counterexample remains separate evidence. |
| Phone/audio behavior | No audio or remote call was used. | Recording notice, interruptions/cut-off answers, speech recognition and terminal speech/end-call behavior on the final phone agent. |

These are evidence gaps, not invented hidden-score failures. Resolve them with the actual final inputs/version rather than adding the old test counts or fixture success to a current acceptance claim.

To reproduce these bounded probes from the repository root:

```powershell
.venv-playground/Scripts/python.exe -B results/hidden-readiness-20261011/report-refresh/policy/probe_amount_gaps.py
```

It writes only `amount-results.json` beside this report. No application source was changed.
