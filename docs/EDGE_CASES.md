# Customer edge cases and validation checks

**A customer giving the wrong birthday should not pass identity verification.** The bank's `verify_identity` service checks the answer against its customer record. The agent does not initially know the true birthday and must not guess it. With the public Brief A contract, the staged workflow advances to account retrieval only after the service reports that all required answers are verified.

Validation exists at several levels: policy consistency checks during construction, normalization and routing in the generated workflow, and truth checking by the bank. Some conversation behavior still depends on model instructions. The scenarios below separate those protections from proposed acceptance tests. Findings and focused offline runs were inspected on **10 October 2026**; no new live customer simulations were run for this guide.

**Status, 11 October:** the audit round after this inspection fixed several gaps named below (AUD-15 identity answers, AUD-23 counting verified answers, AUD-35 decline limit, AUD-04 success signals and others). Rows that say "now" were updated; the other rows record the 10 October inspection. The current status of every item is in [FIXING_ISSUES.md](FIXING_ISSUES.md).

The companion [banking conversation review](BANKING_CONVERSATION_PRACTICES.md) adds primary-source guidance and tests for persuasion, repeated offers, clarification, audio interruption, confirmation and pending bank actions.

## What happens after an incorrect birthday

[Brief A rules 11–15](../briefs/brief-a.md) require the birthday first, then the last four phone digits. A complete answer is sent to the bank. A readable but incorrect answer is a mismatch; the service returns verification state and remaining wrong answers. Before exhaustion, the generated response instruction asks the same question again and does not reveal the correct answer.

After three wrong answers in total, across either question, the workflow's `wrong_answers_left=0` route enters the identity-failure sentence, gives the app or branch direction and closes with the failed-identity outcome. A correct birthday alone does not authorize account retrieval: the phone answer must also pass. A refusal follows a separate declined-identity path.

An unreadable value is different from a wrong readable birthday. The [API description](../tools/openapi.json) says unreadable values return `ok=false` and do not consume a wrong answer. The known `day_month_swapped` hint asks for the month as a word without a wrong-answer penalty. Neither event authorizes account access.

These mechanisms are visible in [identity response handling](../builder/compiler/toolset.py), [staged identity routes](../builder/compiler/profiles/staged.py) and the [identity prompt](../builder/prompts/agent/identity.v1.j2). The deterministic account gate relies on recognizing the service's response fields; changed or missing fields are part of issue F01 in the [fixing guide](FIXING_ISSUES.md).

## What local validation does and does not enforce

[Policy validation](../builder/understand/validate.py) checks that required identity questions exist, the wrong-answer limit is positive and known question fields belong to the API vocabulary. It warns about duplicate fields and a digits format without a digit count. Those checks validate the extracted policy, not the customer's answer.

The generated [answer pre-hook](../builder/compiler/hooks.py) formats numeric dates according to the policy's date order and strips separators from digit answers. It does not check that February has the proposed day, enforce exact digit length, assemble conversation fragments or choose the final answer from a self-correction. Month names and number words depend on model conversion before the request.

The prompt tells the model to ask only for missing parts, combine fragments, verify complete answers even when mixed with another question, repeat only the failing security question and avoid disclosing account information. Those instructions are useful safeguards, but assertions that their text exists do not prove real conversations follow them.

Controlled hook probes demonstrated the following inputs and outputs. They describe what happens **if the model submits that value**, rather than proving that a live model will submit it.

| Submitted answer | Observed hook behavior | Consequence to verify |
|---|---|---|
| Complete wrong date `1991-07-12` | Forwarded unchanged. | Bank decides truth; keep account locked after a mismatch. |
| Invalid date `31/02/1988` | Formatted as `1988-02-31` and forwarded. | Readability must be handled without calling it a verified mismatch. Add local validity checks for supported formats. |
| Numeric `03/04/1988`, DMY policy | Formatted as `1988-04-03`. | Follow the brief's order; clarify a service swap hint. |
| Numeric `03/14/1988`, DMY policy | Formatted as `1988-14-03` and forwarded. | Do not silently invent a different date order. |
| Partial date `14 March` | Forwarded unchanged. | Model should request the missing year; hook currently cannot prevent submission. |
| Month-word date `14 March 1988` | Unchanged by the hook. | Model conversion to canonical ISO needs conversation tests. |
| Two phone digits `44` | Forwarded unchanged. | Enforce policy-derived completeness before checking. |
| `4 4 7 1` or `0 3 5 8` | Becomes `4471` or `0358`. | Preserve leading zeros as strings. |
| `4401, sorry, 4410` | Becomes `4410` when the IR gives the digit count (the last group of that length, AUD-15); left unchanged when it does not. Never `44014410`. | Without a digit count the bank judges the raw text; clarify. |
| `four four seven one` | Unchanged by the hook. | Number-word conversion is model-dependent. |

The local identity probe and its results (`.cache/project-audit/identity/`, git-ignored, so only on the machine that ran it) preserve these synthetic checks. They also reproduced the refusal-limit gap (since fixed, AUD-35) and response-envelope classification gaps. Portable regression tests in `tests/` are the evidence that counts.

## Identity scenarios to verify

The expected behavior in this table follows public Brief A unless a row explicitly tests a changed synthetic policy. For unfamiliar briefs, extract their rules rather than copying Brief A's limits or questions into production code.

| Scenario | Required acceptance observation | Current protection or gap |
|---|---|---|
| Three complete but wrong birthdays | Three wrong checks in total; no phone advance, account call, debt disclosure or promise; prescribed failed-identity close. | Bank counter and deterministic failure route exist; verify the full sequence. |
| Two wrong birthdays, correct birthday, wrong phone | The last wrong answer exhausts the shared allowance. | Service counter is authoritative under the public contract; no independent local total counter was found. |
| Correct birthday, wrong phone, corrected phone | Repeat only the phone question; retrieve the account only after both pass. | Public PUB-12 is a multi-product case that also specifies this phone retry; question selection remains model-driven. |
| Partial birthday across two or three turns | Ask for missing components, invent no year, submit one complete canonical answer. | Prompt instruction exists; deterministic completeness guard is missing. |
| Invalid date or non-leap-year February 29 | Clarify unreadable input without mislabeling it as a readable mismatch. | Bank documents unreadable rejection; local hook lacks calendar validation. |
| Ambiguous numeric date followed by swap hint | Ask for month as a word; preserve the attempt allowance and verify the clarified date. | Known hint instruction has an existing test; month-word conversion needs execution tests. |
| Correction within one utterance | Submit the final unambiguous answer once, or clarify. | The hook keeps the last group of the expected length (digits, year) or the last date said (AUD-15); without a known length the text is left as said. |
| Correction after a submitted wrong answer | Preserve the wrong check already performed; use the new answer for the next check. | Must test sequence state, not just normalization. |
| Digits spoken separately, with leading zero or in words | Preserve exact required length and leading zero; clarify incomplete input. | Separator stripping works; words and completeness depend on the model. |
| Complete answer plus “what is this about?” | Verify the complete answer first; disclose no account facts before full verification. | Prompt explicitly covers mixed messages; needs conversation evidence. |
| Refusal or asking the purpose instead of answering | Explain and close at the actual brief's decline limit. | The decline limit now follows `identity.decline_max`, and an answer resets the count (AUD-35). |
| Questions about being a robot or a scam | Answer as prescribed and continue without treating an unrelated question as a wrong security answer. | Prompt and intent behavior need realistic style tests. |
| Relative or wrong-number disclosure during identity | Stop security questioning and use the appropriate exit without account disclosure. | Staged escape transitions exist; correct selection depends on the model. |
| Service outage, unknown hint, empty or contradictory response | Keep account access blocked; clarify unreadable answers and follow any service-failure rule defined by the supplied brief. Do not invent a mismatch, verification verdict or closing reason. | Synthetic probes found incorrect “answer is wrong” defaults. |
| Different questions, order, field names and limits | Enforce extracted formats and limits without depending on public names. Zero wrong-answer allowance should be diagnosed as unsupported by current policy validation. | Alias-based field mapping and prompt-only fallback require repair and contract tests. |

The synthetic `max_wrong_total=2` probe changes the prompt to “Two,” but the deterministic exit still waits for service remaining attempts to reach zero. If the service implements the same limit, that is appropriate. Test inconsistent service/brief limits explicitly rather than assuming that this dependency proves a live violation. Do not introduce an age cutoff, future-birthday policy or new security question unless the input rules require it.

## Other customer and policy boundaries

These are proposed acceptance scenarios, not claims that every case currently passes. Use the [fixing guide](FIXING_ISSUES.md) to connect reproduced failures to repairs.

| Area | Cases to exercise | Evidence required |
|---|---|---|
| Dates | Today allowed/forbidden; deadline today or past; weekends; consecutive holidays; alternate business weekdays; month/year boundaries; leap days; next/previous business-day shifts; empty legal interval; deadlines beyond 120 days; a policy with `not_after_deadline=false`. | Generated request and spoken correction agree with the extracted calendar and independent expected dates. No invalid promise reaches the bank. |
| Call time and callbacks | Supplied `now` differs from machine clock; offset/midnight boundaries; callback exactly at each window endpoint, one minute outside, at `now` and before `now`; malformed time; repeated invalid proposals. | Call-variable time governs; only eligible future slots are booked, with correct retry and close behavior. |
| Money | One cent below/at/above floor and full balance; full-only products; zero, negative or missing facts; locale separators; nonfinite values; rounding; amount corrected after read-back. | Correct floor/full interpretation, required disclosures, exact recorded amount and no unauthorized rounding or default. |
| Three products | All promised; none promised; mixed hardship, paid, disputed and refused reasons; failure on first/middle/last product; different deadlines; transfer before/after a promise. | Per-product attempts, canonical IDs, exactly required promises and notes, and correct overall precedence. |
| Confirmation and honesty | Clear yes, ambiguous assent, corrected date/amount, changed mind, failed write, retry success, exhausted retries and uncertain timeout. | Only confirmed authorized writes; no duplicate logical record; spoken success follows acknowledged success. |
| Closing and interruptions | Each outcome row; refusal before/after identity; third party offers to pay; deceased customer; transfer after notes; close failure; abrupt customer hangup. | No prohibited disclosure or tool order; correct ledger and reason; exactly one final close when the policy requires it. Do not claim a completed action after an interrupted call. |
| New policy and API | Raw email plus policy and superseding memo; reordered tables; changed holidays, questions, phrases, enums, response nesting and field names; missing/ambiguous required facts. | Current amendments govern. Unsupported interpretation is diagnosed; generic machinery handles supported variants without embedded final-brief values. |
| Style and repetition | `pieces`, `corrects`, `talkative`, `asks_back` and `in_words`; complete answer mixed with irrelevant text; self-correction near a tool call; repeated case execution. | Inspect every failed repetition and both transcript and ledger. Last-repetition success cannot conceal earlier failure. |
| Policy shapes the IR now expresses | A floor stated as a percentage of the past-due amount, a fixed amount, or "whichever is higher" (`amounts.floor_rule_by_type`); days-late tables per product type (`bands[].product_types`); a persuasion date kind that contradicts its own text (auto-corrected from the text). | Offline tests exec the generated facts and promise guard for each (`test_floor_rules.py`, `test_band_product_types.py`, `test_persuasion_date_cues.py`). Live behaviour on such a brief is unverified. |
| Policy shapes still not expressible (backlog) | A days-late range the documents put outside negotiation with its own action ("over 60 days: transfer"): the agent snaps to the nearest band and negotiates. Deadline kinds other than the n-th business/calendar day, end of this month and a fixed date ("end of next month", a product due-date field): the facts print the rule but the promise guard accepts any date. Outcome rows that depend on the promise terms (full vs partial, today vs later) and split payments: the first `promise_recorded` row always wins (`ROW_SHADOWED` warns). A wider right-party definition ("any director"): only as a residual rule; the opening prompt still says anyone who is not the customer is a third party. | Audit items AUD-10, AUD-39 and AUD-46. A brief with one of these shapes needs a manual check of the compiled facts, guard and outcome table before submission. |
| Tool-contract shapes the compiler now handles | Success flag reworded ("`ok` is false on error.", "True if successful.") or renamed to an undocumented `recorded` / `is_saved`; transfer field renamed (`team`, `destination`) with the IR keyed either way; a required request field the contract fixes (`const`, enum of one, `default`) or, for engine calls, a required enum (first value, compile note); amounts in minor units (`amount_cents`, integer "in cents", or another stated scale such as thousandths, "3 decimal places" or "1 dinar = 1000 fils"; an unreadable scale keeps cents with a compile note); integer product ids; a promise date without `format: date` (`pay_on`); constant header parameters; product ids that differ only by case; a string status field that acknowledges only on its documented success value (11 October). | `tests/unit/test_hidden_tool_contracts.py` (audit items AUD-04, AUD-16, AUD-21, AUD-22, AUD-48, F03/N-2). Live behaviour on such an API is unverified. |
| Tool-contract shapes still not handled (backlog) | A documented non-ISO date format ("DD/MM/YYYY"): the model is told YYYY-MM-DD and the guard sends ISO. Path parameters (`/customers/{customer_id}/...`): the field goes into the body and the URL keeps the placeholder. A credential header or security scheme: reported as a compile note, nothing is sent. Two described booleans that both claim action success: still a compile error. | Audit items AUD-04 (residual) and AUD-48 (3)-(5). Check the compile notes and the request shapes by hand when a hidden API has one of these. |
| Runtime state now made deterministic (round 3) | Parallel `create_promise` / `add_note` calls in one message (every pre-hook runs before any post-hook): the batch staging commits every acknowledged call, nothing on a guess, and recovers after the retry of a failed member (AUD-06). A 200 `ok: false` never routes to the recorded / booked line and never closes as promise / callback (`promise_acked`, `callback_acked`, AUD-07). A clock call variable named other than `now` drives every date rule (AUD-13). A second note for a product, a note for a promised product and (lean) a note after a transfer are not sent (AUD-26). A callback outside the allowed slots is blocked, the allowed times are spoken and the call closes no-callback after the policy's extra asks; `7:30 pm`, `11am`, `1530` become `HH:MM` (AUD-29). An account that could not be loaded closes as the recording-failure outcome; a promise without loaded facts or with a non-numeric amount is blocked (AUD-24). Without an overall `all_verified` flag the hooks count the verified questions (AUD-23). Date-table rows never offer a day the guard blocks; no shift clause when the policy allows calendar days; the facts list non-business days up to the deadline with the day to offer (AUD-17, F21). | `tests/unit/test_parallel_acknowledgement.py`, `tests/unit/test_state_routes_and_dates.py`; live confirmation of the new fixed lines and routes is still needed. |
| Runtime state still open (backlog) | The model may still record a legal date other than the required correction of the customer's own proposal (F21 binding: the guard checks legality only). A failure inside a parallel batch that the model never retries leaves its acknowledged siblings uncommitted (fail safe: the once-per-product guard and the notes decision then see fewer entries). Whether the engine counters are visible inside hooks is unverified; the staging works without them except after a batch whose calls all failed with a 5xx followed by a different call. | Live call records. |

## What the current tests establish

The focused identity selection ran **12 tests successfully, with 69 deselected**, on 10 October 2026 at 22:53:21 Istanbul time. It checks normalizer outputs, route construction, known response instructions and failure wording. The identity test report (`.cache/project-audit/identity/pytest.xml`, machine-local), [hook tests](../tests/unit/test_hooks.py) and [staged compiler tests](../tests/unit/test_compile_staged.py) identify that scope. It did not run full conversations or a new complete suite.

The [robustness suite](../tests/unit/test_robustness.py) also passed its 14 tests. A direct sampler run on the supplied policy fixtures evaluated 1,640 invariants for A and 1,670 for A+B, with no failures. A had two unchecked groups and A+B had one; these include missing named enum coverage and an inapplicable multi-product group for A. Counts are arithmetic and policy checks, not customers.

[The sampler](../builder/robustness.py) does not execute generated workflows, call the bank, judge transcripts or verify real birthday matching. Its passing reports coexist with reproduced generated-hook date failures. Keep helper validation, generated-runtime validation and live behavior evidence separate.

## Running acceptance tests without misleading results

First turn each reproduced hook or response defect into an offline regression. Add scripted sequences for fragments, corrections, refusal counts, tool failures and per-product state. Use independent expected values where practical; two implementations agreeing can share a mistake.

After publication and export integrity are fixed, run official practice tests against the automatically built agent. These example commands spend platform credits and should run serially; substitute the confirmed agent ID:

```sh
python tools/run_cases.py <agent_id> --brief A --reps 3 --out results/builder-a-normal.json
python tools/run_cases.py <agent_id> --brief B --reps 3 --style pieces --out results/builder-ab-pieces.json
python tools/run_cases.py <agent_id> --brief B --only PUB-12 --reps 3 --out results/builder-ab-phone-retry.json
```

Expand to the other relevant styles after focused cases succeed. `--brief B` selects B's public cases; retain a separate A report for the A behavior. PUB-12 includes a wrong phone answer followed by the correct one as part of its multi-product script. The `corrects` style adds payment date or amount corrections and explicitly excludes security-answer self-corrections; test those separately with a supported synthetic conversation setup. The final score requires each customer to pass every repetition, with correct bank records, conversation policy and honesty, as described in the [official kit](../KIT_README.md).

Multiple live calendar days need a supported test mechanism. The official harness obtains session variables from the bank using the case ID; it has no `--now` option. Editing `public-test-set.json`'s date alone does not establish a different live call date. Exercise generated code offline with varied `now` values, then verify live date variation through an appropriate supported session setup. Preserve original kit files and add synthetic test inputs separately.
