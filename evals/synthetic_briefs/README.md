# Synthetic brief packs (generalisation checks for the understand stage)

Two invented bank briefs with the same policy, written to differ from the public Brief A in every value the hackathon
says hidden briefs change: other bank, other holidays, other bands and deadlines, other identity questions, other
outcome table, other fixed sentences, changed tool fields and enums. Nothing here is a real bank. They live in `evals/`
only (the builder must hold no brief value).

| Pack | Style | Files |
|---|---|---|
| `v01_clean/` | one clean specification in the style of `briefs/brief-a.md` (47 rules + a two-product section) | `brief.md`, `openapi.json` |
| `v02_raw/` | the same policy as raw bank material: a mail thread (with a quoted older mail that holds two stale values), a policy extract, a memo that overrides two values, a CSV code list with one retired row | `Mail_thread_collections_ops.eml`, `Memo_2026-11-02_changes.txt`, `Policy_extract_v3.txt`, `outcome_codes.csv`, `openapi.json`, `expected_notes.md` |

Tool API changes against `tools/openapi.json` (both packs use the same file):

* `verify_identity.field`: `postcode` | `birth_year`; hint `two_digit_year` (was `date_of_birth` | `phone_last4`, `day_month_swapped`)
* `get_account`: product types `card` | `loan` | `vehicle_finance`; fields `days_overdue` (was `days_late`), `min_due`
  (was `minimum_payment`), `monthly_instalment` (was `instalment_amount`); `past_due` and `card_last4` unchanged
* `add_note.reason`: `rejected` | `vulnerable` | `contested` | `settled` | `system_fault`
* `transfer_to_agent.queue`: `payment_plans` | `front_desk` | `care_team`
* `close_call.outcome`: `agreed` | `settled` | `escalated` | `declined` | `unverified` | `unreachable`

## Run (dry run, no Freya call; about 2 to 4 minutes, cheap models)

```
python builder/build.py evals/synthetic_briefs/v01_clean/brief.md evals/synthetic_briefs/v01_clean/openapi.json \
    --slot qr-v01-clean --dry-run --track compiled --self-test none

python builder/build.py evals/synthetic_briefs/v02_raw/Mail_thread_collections_ops.eml \
    evals/synthetic_briefs/v02_raw/Memo_2026-11-02_changes.txt evals/synthetic_briefs/v02_raw/Policy_extract_v3.txt \
    evals/synthetic_briefs/v02_raw/outcome_codes.csv evals/synthetic_briefs/v02_raw/openapi.json \
    --slot qr-v02-raw --dry-run --track compiled --self-test none
```

Do not pass the directory `v02_raw/` (it would also load `expected_notes.md` as a brief). Inspect
`out/<slot>.ir.json`, `out/<slot>.ledger.json`, `out/<slot>.workflow.json` and the trace.

## What the IR of these packs must look like (format contracts)

Both packs exposed defects that the validator now catches (regression targets for a live run):

* persuasion attempt 1 (and 3 of the red band) proposes the next business day after today: the step's `date` is
  `next_business_day_after_today`, never `customer`;
* `disclosure.team_phrase` is the words that complete "I am calling from ___" (no leading "I am calling from", no full stop),
  `product_naming` holds bare names (`card ending {card_last4}`, no amount or days);
* the days field is `days_overdue` and the amount field `past_due` (`disclosure.days_field` / `amount_field`, or readable from
  `state_fields`), whatever order the model lists `state_fields` in;
* the refusal-to-talk intent has a before-verification half with an outcome and an after-verification half with the note.

## Expected values of `v01_clean`

Bank "Meridian Savings Bank"; holidays 2026-10-19, 2026-11-03, 2026-11-27, 2026-12-24, 2026-12-31; bands green 1-10 /
10th business day / 1 attempt, amber 11-30 / 6th / 2, red 31-60 / 3rd / 3; identity `postcode` then `birth_year`, four
wrong answers end the check (unverified / exhausted), hint `two_digit_year` does not count; callback today or next
business day 09:00-17:30 inclusive; vague period = flat refusal; "next <weekday>" = first occurrence after today;
persuasion attempt 1 = next business day after today (full amount or floor), attempt 2 (amber, red) = floor on the
deadline, attempt 3 (red) = floor on the next business day; a failed `create_promise` is NOT retried (declined /
system_fault); floors `min_due` (card) and `monthly_instalment` (loan, vehicle_finance); two products with notes
(`rejected`, `vulnerable`, `contested`, `settled`, `system_fault`), no notes on any transfer; fixed sentences are the
quoted ones in `brief.md` (opening, condolences, text-message sentence, "Can you settle this today?", "Shall I record
this?").
