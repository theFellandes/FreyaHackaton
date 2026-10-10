# v02_raw: expected IR values (the same policy as v01_clean, as raw bank material, with two overrides)

Files (build with all four documents plus `openapi.json`; the memo sorts BEFORE the policy extract on purpose, so file
order must not decide precedence):

* `Mail_thread_collections_ops.eml` - mail of 26 Oct with greeting, signature, disclaimer, quoted history; its quoted
  history holds two stale values (callback window to 18:30, identity ends after two wrong answers)
* `Memo_2026-11-02_changes.txt` - amends two values (identity limit 4 -> 3, red band deadline 3rd -> 2nd business day)
  and rejects a "5-band model" draft
* `Policy_extract_v3.txt` - numbered policy clauses (calendar, bands, amounts, promise, recording, other statements,
  two products, closing)
* `outcome_codes.csv` - code list with outcome / reason, including a RETIRED row `legacy_hold` / `review`

Deliberate traps: stale quoted values in the mail; the memo overrides two values in two other documents; a retired
four-band remark in 4.2; a retired CSV row that is not in the `close_call` enum; decoy remarks (SMS wording, call centre
hours, dashboards) that are not rules; `past_due` keeps its name while `days_late` is `days_overdue` and the floors are
`min_due` / `monthly_instalment`; product types are `card` / `loan` / `vehicle_finance`.

## The 15 most important expected values

| # | IR path | Expected | Source |
|---|---|---|---|
| 1 | `bank_name` | Meridian Savings Bank (not "Freya Bank") | all |
| 2 | `phrases.opening_sentence` | Good day, this is Meridian Bank's virtual assistant, and this conversation is recorded. | mail |
| 3 | `phrases.right_party_question` | May I speak with {first_name} {last_name}? | mail |
| 4 | `calendar.holidays` | 2026-10-19, 2026-11-03, 2026-11-27, 2026-12-24, 2026-12-31 (Brief A's 2026-10-26 etc. must NOT appear) | policy 4.1 |
| 5 | `bands` | green 1-10 / 10th business day / 1 attempt; amber 11-30 / 6th / 2; red 31-60 / **2nd** (memo, not 3rd) / 3; no 90-day band | policy 4.2 + memo item 2 |
| 6 | `identity.questions[].field` | `postcode` then `birth_year` (never date_of_birth / phone_last4) | mail |
| 7 | `identity.max_wrong_total` | **3** (memo item 1; mail says 4, quoted draft says 2) | memo > mail > quoted history |
| 8 | `third_party.callback` | today_or_next_business_day, 09:00-17:30 inclusive (NOT 18:30), success unreachable/rebook, failure unreachable/no_rebook | mail |
| 9 | `closing.rows` (outcome/reason) | agreed; settled; escalated plan/agent/care; declined rejected/vulnerable/contested/system_fault; unverified exhausted/declined_checks; unreachable rebook/no_rebook/wrong_person/deceased/unclear; NO `legacy_hold` row | csv + `close_call` enum |
| 10 | `amounts.floor_field_by_type` | card -> `min_due`; loan -> `monthly_instalment`; vehicle_finance -> `monthly_instalment` | policy 5.2 |
| 11 | `promise_dates` | vague_period = ask_once_then_refusal; next_weekday_semantics = next_occurrence; non-business day -> next business day; after_deadline = offer_deadline | policy 4.4-4.6 |
| 12 | `recording.retries_on_failure` + `failure_outcome` | **0** retries; declined / system_fault | policy 7.3 |
| 13 | `phrases.recorded_sentence`, `phrases.deceased_sentence` | "A confirmation will be sent to you by text message." / "My condolences to you and your family." | policy 7.4 / mail |
| 14 | `persuasion.steps` | attempt 1 next business day after today (full or floor); attempt 2 floor on deadline (amber, red); attempt 3 floor on next business day (red only); first_ask "Can you settle this today?" | policy 6.1-6.2 |
| 15 | `multi_product` | enabled, max 2 products, notes reasons = rejected/vulnerable/contested/settled/system_fault, `notes_on_transfer` false, closing_reason_rule first_non_paid_note (the "paid" reason is `settled`), all_paid_outcome = settled | policy 9.x |

Stating the arrears (mail, question 4): `disclosure.team_phrase` completes "I am calling from ..." (it must not repeat
"I am calling from" itself, because the compiler wraps it), `disclosure.product_naming` is a noun phrase only (card ->
"card ending {card_last4}", loan -> "personal loan", vehicle_finance -> "vehicle finance"), `disclosure.state_fields` =
`past_due`, `days_overdue`. `phrases.scam_answer` = verify by phoning the customer service number on the latest statement.

Other values worth checking: `identity.on_failed` unverified/exhausted, `identity.on_declined` unverified/declined_checks,
`phrases.paid_sentence` mentions two business days, hardship transfer queue `care_team`, restructuring queue
`payment_plans`, human queue `front_desk`; `forbidden_before_verification` = arrears, overdue, payment, card, loan and
product names; `currency_word` must stay empty or come from the documents (none is named).

Ledger expectations: the memo's two items supersede policy 4.2 (red row) and the mail's identity-limit sentence; the
quoted history lines are superseded or ignored; the `legacy_hold` row is not an active rule.
