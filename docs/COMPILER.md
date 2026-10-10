# Compiler

`builder/compiler/` turns the structured policy of a brief into a Freya workflow, and `builder/checks/` verifies the
result before anything is deployed. Both are **pure**: no network, no LLM call, no file written. The same inputs always
give the same workflow.

```text
PolicyIR  ─┐
ToolApi   ─┼─► compile_workflow(ir, tool_api, ledger, prose, profile) ─► CompiledWorkflow(workflow, rule_map, notes)
RuleLedger ┤                                                                   │
NodeProse ─┘  (optional, LLM-drafted)                                          ├─► checks.lint.lint_workflow
                                                                               ├─► checks.coverage.coverage_report
                                                                               └─► checks.code_sandbox.run_snippet (tests)
```

Contents: [1 Inputs and outputs](#1-inputs-and-outputs) · [2 The staged agent](#2-the-staged-agent-default) ·
[3 Node-by-node table](#3-node-by-node-table-staged) · [4 Variables written by code](#4-variables-written-by-code-nodes-and-hooks) ·
[5 Rule to mechanism](#5-rule-to-mechanism-mapping) · [6 Prompts and prose](#6-prompts-and-prose) · [7 Tools and hooks](#7-tools-and-hooks) ·
[8 Lean profile](#8-the-lean-profile) · [9 Lint rules](#9-lint-rules) · [10 Verified and unverified](#10-verified-and-unverified-platform-behaviours) ·
[11 Extending](#11-extending-the-compiler) · [12 Tests](#12-tests)

## 1. Inputs and outputs

| Input | What the compiler takes from it |
|---|---|
| `PolicyIR` (`builder/policy/ir.py`) | Every brief-specific value: sentences, calendar, bands and deadlines, floors, persuasion steps, intents, outcome table, queues, product naming, identity questions |
| `ToolApi` (`builder/ingest/tool_api.py`) | URLs, methods, request fields, enums and response fields of the 7 tools. The tool *names* are fixed by the hackathon; **fields, flags and allowed values are not**: every field the compiler touches is found by its role in the contract (`compiler/roles.py`, section 7) - the success flag of a response, the customer-id field, the outcome and reason fields of `close_call`, the product / amount / date fields of `create_promise`, the id and type keys of the `get_account` items, the queue field, the shape of the `verify_identity` response. A role the agent cannot do without that cannot be found is a `CompileError`; the compiler never falls back to an invented name |
| `RuleLedger` | Rules that no IR section cites are placed **verbatim, with their ids** in the prompt of the stage they apply to; the rule map is built against it |
| `NodeProse` (`compiler/prose.py`) | Optional extra text per node drafted by the `draft_prose` stage; defused and appended (`prose.clean_text`) |

`compile_workflow(ir, tool_api, ledger, prose=None, profile="staged") -> CompiledWorkflow`:

* `workflow` - the JSON for the REST `workflow` key. It is validated through `workflow_model.Workflow` and never contains
  `null` (also not inside an embedded code string: IR data is embedded with `None` entries removed).
* `rule_map` - `{ledger rule id: [where it is enforced]}`, for example `node:negotiate/task_message`, `code:account_facts`,
  `tool:verify_identity/post_process_code`. Superseded rules are never listed.
* `notes` - non-fatal findings for the trace (a queue that is not in the `transfer_to_agent` enum, a missing `now` variable ...).

`CompileError` is raised when a tool the policy needs is missing from the tool API (`verify_identity`, `get_account`,
`create_promise`, `close_call`; `schedule_callback` when there is a callback rule; `transfer_to_agent` when a transfer exists;
the notes tool for several-product briefs), the IR has no identity question, or a field role cannot be found: no outcome field in
`close_call`, a required `close_call` field the compiler cannot fill, an optional one that may hold the reason while the outcome
table has reasons, no product / amount / date field in `create_promise` (the promise guard could not be generated), no product-id
or product-type key in the `get_account` items, no boolean in the `verify_identity` response that says whether an answer was
accepted, or a policy without an opening sentence whose brief requires a recording notice in the opening.

Nothing in `builder/` names a bank, a holiday, an outcome, a product or a sentence of a brief. The test
`test_the_compiler_sources_hold_no_brief_specific_values` scans the sources for such tokens.

## 2. The staged agent (default)

The staged profile reproduces the structure of the hand-authored reference `evals/reference/brief_a_ref.py` (9/10 on the
official scorer in its first run, then improved by the lessons below). `test_structure_matches_the_validated_reference`
compares both workflows: same node types and the same edges at the level of roles (`end_*`, `handoff_*`, named nodes).

```mermaid
flowchart TD
    setup["setup (code)"] --> opening
    opening -- "tool: confirmed" --> identity
    opening -- "tool: someone else" --> third_party
    opening -- "tool" --> endN["end_&lt;situation&gt; (code)"]
    opening -- "tool" --> handoff["handoff_&lt;queue&gt; (api_request)"]
    third_party -- "schedule_callback_success_count &gt; 0" --> callback_done
    callback_done --> endN
    third_party -- "tool / goodbye loop" --> endN
    identity -- "all_verified true|True" --> load_account
    identity -- "wrong_answers_left = 0" --> identity_failed
    identity_failed --> endN
    identity -- "tool" --> endN
    identity -- "tool: not the customer" --> third_party
    identity -- "tool" --> handoff
    load_account["load_account (get_account)"] --> account_facts["account_facts (code)"]
    account_facts --> negotiate
    negotiate -- "promise_guard contains blocked" --> promise_fix["promise_fix (code)"]
    promise_fix --> promise_fix_say["promise_fix_say (tts)"]
    promise_fix_say --> negotiate
    negotiate -- "create_promise ok and product_count = 1" --> promise_done
    promise_done --> endN
    negotiate -- "tool" --> endN
    negotiate -- "tool" --> handoff
    negotiate -- "tool: an overridden exit" --> guard["guard_&lt;intent&gt; (code)"]
    guard -- "&lt;intent&gt;_blocked = yes" --> guard_say["guard_&lt;intent&gt;_say (tts)"]
    guard_say --> negotiate
    guard -- "always" --> handoff
    negotiate -- "goodbye loop / max turns" --> end_auto["end_auto (code)"]
    end_auto --> decide
    endN --> decide["decide (code)"]
    handoff --> decide
    decide --> close["close (close_call)"]
    close --> hangup["hangup (end_call)"]
```

Design principles (each is a measured lesson of the reference runs; see the *Addendum* section of the implementation contract):

| # | Principle | Mechanism in the compiled workflow |
|---|---|---|
| 1 | The model never calculates dates or amounts | `setup` and `account_facts` code nodes write the date table, deadlines, floors, allowed dates and scripted attempts as text |
| 2 | A sentence the brief prescribes at a fixed point is spoken by the engine, not by the model | `on_enter: tts` for the opening, `{{debt_statement}}`, `callback_done`, `promise_done`, `identity_failed` |
| 3 | A fixed line must not contain a word a judge can misread | The opening "This call is recorded" becomes "We record this call" (`context.soften_recording_notice`), because "recorded" is also the word of the promise honesty rule |
| 4 | Moving on after a terminal action is deterministic | Counter conditions (`schedule_callback_success_count > 0`; `create_promise_success_count > 0` AND `product_count = 1`) route to a passthrough node that speaks the prescribed sentence and says goodbye |
| 5 | The model says goodbye and leaves in the same message | Tool-call transitions (`is_tool_call`): the model calls `transition_to_end_*` / `transition_to_handoff_*` |
| 6 | The model never names an outcome | `end_<situation>` nodes only write `chosen_end`; `decide` applies the IR precedence to counters, notes and the chosen situation |
| 7 | A goodbye loop must end | `matches_intent` "only goodbyes left" -> `end_auto`; `max_turn_transition` -> `end_auto`; the `call_situation` extraction is the outcome fallback |
| 8 | Persuasion attempts are counted per product | `account_facts` writes "Attempt k - ask: ..." scripts per product with exact questions and "After attempt N is refused: make NO further offer" |
| 9 | A mixed answer + question must still be verified | The identity prompt: "call verify_identity FIRST, before you say anything" |
| 10 | A tool result needs an instruction | Post-hooks write `what_to_do_next` into `result.data` |
| 11 | Several products | `add_note` pre-hook logs `pid:reason;` into `vars['notes_log']`; `decide` applies the notes closing rule |
| 12 | A promise the rules forbid must not be recorded, even for a talkative customer | **Hard promise guard**: the `create_promise` pre-hook blocks an unknown product, a date outside `allowed_<pid>` and an amount outside floor..full by emptying `config['url']` (experiment E-9, verified: no request is sent, the engine reports `Missing required field: url`, and **the post-hook does not run**). The explanation therefore comes from principle 23 |
| 13 | A day the customer names that is not a business day has a ready answer | Date table rows for weekends and holidays end with "-> if the customer names this day, offer <next business day> instead" (or the previous one / "cannot be recorded", per `non_business_day_shift`) |
| 14 | Only complete identity answers are checked | The identity prompt and the tool's field note say so: ask only for the missing part and combine both parts |
| 15 | Talkative customers | System prompt: a cut-off or small-talk-only message gets "I understand, go on." and is never a refusal; a message that holds an answer, day, time, date or amount is acted on at once; a callback is booked as soon as a valid day and time are given |
| 16 | An intent that takes precedence says what it forbids | `IntentRule.overrides` becomes "Once this applies, it comes before the rules for X: never <do X> in this call"; an offer with precedence is made once and is the only offer |
| 17 | Turn caps are generous and lead to a computed end | opening 30, third party 30, identity 60, negotiation 60 (a cap counts customer messages; a talkative customer spends them on fragments); every cap leads to a computed end (`end_auto` or the stage's own end node); `decide` closes an unverified `end_auto` as failed when no wrong answers are left (`verify_identity_response.data.wrong_answers_left` is `0`), else as declined |
| 18 | Amounts are exact | **One input contract** in the Decimal helper (`policy.money.to_money`) and in the generated code (`pycode.MONEY_BODY`, used by `account_facts`, `check_promise` and the hooks): an int is whole units, a float is read through its text form, in text only `0-9 , . -` are kept (currency words, symbols and spaces drop out), a `-` is a sign only in first place, with both `,` and `.` the last one is the decimal mark, a lone `,` with one or two digits after it is a decimal comma (`637,50`), any other `,` and several `.` are thousands separators (`1,250.00`), anything else is not an amount. Rounding is half-up to whole cents with string and integer arithmetic in the generated code (`ROUND_HALF_UP` in the helper): 1.005 -> 1.01, 0.125 -> 0.13, 2125.1 -> 2125.10. The floor / full comparisons of the check snippet, the promise guard and `check_promise_amount` are done on those cents, so they agree at the half-cent boundary |
| 19 | `paid` needs a paid note for every product | `policy.outcomes`, the `decide` snippet and the reference agent: with no promise, the call closes as all-paid only when every product in `product_ids` has a `paid` note (the latest note of a product counts); a product without a note is not paid; a non-paid note decides with the reason of the first such product in `get_account` order; when existing notes are all paid but some product has none, the notes do not decide and the chosen situation (default: the persuasion stop outcome) does |
| 20 | The opening needs an explicit, complete confirmation (reference v9) | The opening prompt: a reply that is cut off, or that contains "no" or says who is speaking ("this is her ..."), is NOT a confirmation - let the person finish ("I understand, go on.") before deciding; only an explicit complete confirmation calls `transition_to_identity` |
| 21 | Every step before verification can be left because the person is not the customer (reference v9) | `identity` has tool-call exits to `third_party` and to the wrong-number end (from the IR's `wrong_number` intent or `third_party.wrong_number`) next to the declined / human exits, and the prompt says "say nothing about the account". A relative's cut-off "no, this is her ..." used to be sent to identity, which had no way back |
| 22 | The model must not call a day after the deadline "a business day" (reference v10) | `account_facts` re-renders `date_table`: every row after a product's deadline ends with `\| after the deadline of pN - not allowed for pN`, or, after every product's deadline, `\| AFTER THE DEADLINE - not allowed: offer the deadline instead` (only `not allowed` when the policy does not offer the deadline; no marks when promises may pass the deadline). The marks follow the hints for weekends and holidays |
| 23 | A blocked promise is corrected out loud, not left as a generic error (reference v10) | **Guard recovery**: when the pre-hook blocks it also writes `promise_guard_blocks` (counter) and `promise_guard_say`, the spoken correction built from the product's spoken name (`<pid>_name`), the latest allowed day (`<pid>_deadline_spoken`) for a date after the deadline - with the same amount, or the full amount when the amount was invalid too -, the next allowed day (`allowed_spoken_<pid>`; the closest one under a previous-business-day policy; a plain "another day" under `reject`) for a day that is not allowed, or the allowed amount range (only the full amount for a product without a floor, "x or y" when between-amounts are not accepted), with the brief's `currency_word`. `negotiate`'s **first** transition (`promise_guard` contains `blocked`) leads to `promise_fix` (code: `promise_guard = 'handled'`, `promise_fix_text = promise_guard_say`) and `promise_fix_say` (`tts` `{{promise_fix_text}}`, passthrough) back to `negotiate`, whose debt statement is first-entry only. `decide` counts service failures as `create_promise_failure_count - promise_guard_blocks`, because the engine counts a blocked call as a failed one |
| 24 | An intent that takes precedence is a latch and a guard, not only a sentence (audit C-15) | `IntentRule.overrides` ("once hardship applies it comes before the restructuring rule") was only prompt text; in one run of three the extraction flipped from `hardship` to `restructuring` after "I just need a discount" and the call ended in the restructuring queue. For every intent `X` with `overrides` and every overridden intent `Y` that has an exit in the stage: `negotiate` extracts `<X>_seen` (`yes`/`no`, "at ANY point in this call", `min_confidence` 0.6) next to `call_situation`; the exit of `Y` leads to the node `guard_<Y>` (code, passthrough) which sets `<Y>_blocked` from `<X>_seen` (or `call_situation`) and routes `<Y>_blocked = yes` -> `guard_<Y>_say` (`tts`, the brief's `override_refusal` phrase or "I am sorry, I cannot arrange that.") -> `negotiate`, else always -> the real exit. **The engine names the tool of a tool-call transition after the node it leads to**, so the prompt of the stage tells the model to call `transition_to_guard_<Y>` (`exit_bullet(..., redirect=...)`); a test checks that every `transition_to_*` a prompt names is a tool the node offers. Not yet validated on the platform (no Freya writes in the audit) |
| 25 | The engine merges transitions that share a target (audit C-13) | A node never has two transitions to one target: the goodbye-loop breaker of `third_party` leads to `end_auto_third_party` (records the same situation as `end_callback_not_booked`), as the reference's `end_auto_tp`. `ProfileBuilder.merge_duplicate_targets` joins two tool-call exits to one node and raises `CompileError` for any other pair; lint L028 checks every workflow |
| 26 | The retry rule, the failure sentence and the notes agree (audit C-09, C-11, C-12) | `context.recording_failure_texts` builds the prompt rule 9 and the `create_promise` post-hook from `recording.retries_on_failure` and one give-up clause: 0 retries -> "do not try again"; 1 -> "once more ... if it fails again"; N >= 2 -> "at most N more ... fails a (N+1)th time" in the prompt and "failure number 1 to N" in the hook. With several products and a service-error note reason the give-up clause is the B7 behaviour (say the failure sentence, note the reason for that product, go on with the next one); only a single-product call ends with `transition_to_end_recording_failed`. When the policy forbids notes on a transfer (`notes_on_transfer` false) every note is **deferred**: the bullets say "add_note reason X (do not call add_note yet - write it only at the very end)", B5 says "Call add_note only at the very end, when every product is handled and the call is not being transferred", the tool description says the same, so a note can never be written before a transfer. A policy that wants notes on a transfer keeps the immediate calls |
| 27 | An empty list of allowed dates means "nothing is allowed" (audit C-06, C-07) | `account_facts` enumerates every allowed day up to the deadline (no cap at 120 days; with no deadline, or a deadline beyond one year, up to a one-year horizon). `allowed_count_<pid>` is `0` when no day is left (the deadline is before today or before the first business day): the guard blocks every date with "I am sorry, I cannot record a payment date for your <name> in this call." and the prompt says "Allowed promise dates: none ... Record NO promise". A list cut at the horizon writes `until_<pid>` (`open`, or the ISO deadline): the guard accepts later ISO dates (up to the deadline). The prompt lists up to 30 days one by one and summarises longer lists as a range with the bank holidays inside; the date table is capped at 70 days. The differential test compares the allowed dates with `policy.calendar.check_promise_date` over deadline kinds x clocks x date rules |
| 28 | Ids and enum values keep the spelling of the data (audit C-08) | The hooks match the model's product id ignoring case, spaces and dashes and write back the id of `product_ids` (`CC-4821`); enum fields (`add_note` reason ...) are written back in the contract's spelling; `notes_log`, the guard and `check_promise` use the canonical id; `decide` maps note ids and reasons case-insensitively before it applies the paid rule |
| 29 | Fields are found by role (audit C-01 .. C-05) | See section 7, "Field roles" |

## 3. Node-by-node table (staged)

Names shown for the Brief A fixture. `<situation>` and `<queue>` come from the IR; only nodes that a transition uses are created
(the lint rule L005 would flag an orphan).

| Node | Type | Entry | Tools / actions | Transitions (in order) | Purpose |
|---|---|---|---|---|---|
| `setup` | code, initial, passthrough | silent | `codegen.setup_snippet` | always -> `opening` | Today's facts, date table, first proposal date, callback facts |
| `opening` | conversation | `tts`: opening sentence + right-party question, first entry only | none | tool-calls: `identity`, `third_party`, `end_unclear_party`, one per before-verification intent (`end_wrong_number`, `end_deceased`, `end_refuse_to_talk`, `handoff_general` ...) | Find out who answered (explicit complete confirmation only); max 30 turns -> `end_unclear_party` |
| `third_party` | conversation | model | `schedule_callback` (pre + post hook) | var: `schedule_callback_success_count > 0` -> `callback_done`; intent: goodbye loop -> `end_auto_third_party`; tool-calls: `end_callback_booked`, `end_callback_not_booked`, `end_wrong_number`, `end_deceased` | Say nothing about the debt; ask when the customer can be reached; book inside the allowed times |
| `end_auto_third_party` | code, passthrough | silent | `vars['chosen_end'] = 'callback_not_booked'` | always -> `decide` | The goodbye-loop breaker needs a node of its own (one transition per target, principle 25) |
| `callback_done` | conversation, passthrough | `tts`: "The callback is booked." + goodbye | none | always -> `end_callback_booked` | Deterministic farewell |
| `identity` | conversation | `llm` with the exact first question, first entry only | `verify_identity` (pre + post hook) | var: `verify_identity_response.data.all_verified` true\|True -> `load_account`; var: `...wrong_answers_left` = 0 -> `identity_failed`; tool-calls: `end_identity_declined`, `third_party`, intent exits (`handoff_general`, `end_refuse_to_talk`, `end_wrong_number`) | Ask the questions in order, one at a time; max 60 turns -> `end_auto` |
| `identity_failed` | conversation, passthrough | `tts`: failure sentence + goodbye | none | always -> `end_identity_failed` | Deterministic failure line |
| `load_account` | api_request | engine | `get_account` (`maxRetries` 2), node-level mapping `data.products` -> `products` | always -> `account_facts` | Fetch the products |
| `account_facts` | code | silent | `codegen.account_facts_snippet` | always -> `negotiate` | Per-product facts, scripts, `debt_statement` |
| `negotiate` | conversation | `tts`: `{{debt_statement}}`, first entry only | `create_promise` (pre + post hook), `add_note` (several-product briefs) | var: `promise_guard` contains `blocked` -> `promise_fix`; var: AND(`create_promise_success_count > 0`, `product_count = 1`) -> `promise_done`; intent: goodbye loop -> `end_auto`; tool-calls: `end_promise_recorded`, `end_<stop>`, `end_recording_failed`, one per after-verification intent (`handoff_support`, `end_hardship`, `end_already_paid`, `end_dispute`, `handoff_restructuring`, `handoff_general`, `end_refuse_to_talk` ...) | Get the promise; max 60 turns -> `end_auto`; extraction `call_situation` (enum of the end situations, `min_confidence` 0.6) |
| `guard_<intent>` | code, passthrough | silent | `codegen.guard_snippet`: `<intent>_blocked` from the latch `<X>_seen` and `call_situation` | var: `<intent>_blocked = yes` -> `guard_<intent>_say`; always -> the real exit (`handoff_<queue>` / `end_<situation>`) | Only for an exit that an intent with `overrides` forbids (principle 24); Brief A: `guard_restructuring` |
| `guard_<intent>_say` | conversation, passthrough | `tts`: "I am sorry, I cannot arrange that." | none | always -> `negotiate` | The engine, not the model, refuses |
| `promise_fix` | code, passthrough | silent | `codegen.promise_fix_snippet` | always -> `promise_fix_say` | Mark the block `handled`; copy `promise_guard_say` to `promise_fix_text` |
| `promise_fix_say` | conversation, passthrough | `tts`: `{{promise_fix_text}}` | none | always -> `negotiate` | Speak the correction the guard prepared, then resume |
| `promise_done` | conversation, passthrough | `tts`: "Your promise is recorded." + success sentence + goodbye | none | always -> `end_promise_recorded` | Deterministic post-promise sentence |
| `end_<situation>` | code, passthrough | silent | `vars['chosen_end'] = '<situation>'` | always -> `decide` | Record how the call ended |
| `handoff_<queue>` | api_request | engine | `transfer_to_agent` with the queue baked | always -> `decide` | Transfer |
| `decide` | code | silent | `codegen.decide_outcome_snippet` | always -> `close` | `final_outcome`, `final_reason` |
| `close` | api_request | engine | `close_call` with `{{final_outcome}}` / `{{final_reason}}` (`maxRetries` 2) | always -> `hangup` | The single close, always last |
| `hangup` | end_call | silent | terminal `call_closed`, `message_mode: none` | none | Hang up silently (the goodbye was said before) |

Counts for the kit fixtures: Brief A 33 nodes, Brief A+B 30 nodes (the intents that become notes need no end node).

## 4. Variables written by code nodes and hooks

| Writer | Variables | Meaning |
|---|---|---|
| `setup` | `setup_state` (`ok` / `now_missing` / `error: ...`) | Health flag; a missing `now` falls back to the real clock |
| | `today_iso`, `today_spoken`, `weekday_name`, `now_time`, `today_is_business_day` (`yes`/`no`) | Facts about today from `vars['now']` |
| | `next_business_day_iso`, `next_business_day_spoken` | The next business day |
| | `date_table` | One line per day, at least 50 days (more when a deadline is further): `2026-10-16 = Friday 16 October 2026 [this week] business day`; flags `weekend, not a business day` and `BANK HOLIDAY, not a business day` |
| | `attempt1_date`, `attempt1_spoken` | The date of persuasion step 1 (when it is a computable kind) |
| | `deadline_<band>` | ISO deadline per band (empty when the rule is `custom`) |
| | `callback_facts` | "A callback can be booked only for: today ... or ..." from the callback rule and the clock |
| `account_facts` | `account_facts` | The LATE PRODUCTS block: name, past-due, days late, band, floor, deadline, the exact floor-offer sentence, scripted attempts, allowed dates |
| | `debt_statement` | The sentence the agent says on entry to `negotiate`; always set (a generic line on failure) |
| | `product_ids` (csv), `product_count` | Product ids in `get_account` order; routing condition |
| | `<pid>_deadline`, `<pid>_deadline_spoken`, `<pid>_floor`, `<pid>_past_due`, `<pid>_band`, `<pid>_name` | Per product, for `check_promise_snippet`, the promise guard (the spoken name and deadline go into its correction) and tests |
| | `allowed_<pid>`, `allowed_count_<pid>`, `until_<pid>`, `allowed_spoken_<pid>` | Allowed promise dates of the product as an ISO csv (what the promise guard checks the date against); how many (`0` = none left: the guard blocks every date); only for a list cut at the one-year horizon: `open` or the ISO deadline up to which later dates are accepted; the first 90 days as spoken text, same order (what the guard names when it offers the next allowed day) |
| | `date_table` (re-rendered) | The `setup` table with the deadline marks of principle 22 |
| | `account_state`, `identity_verified` (`yes`) | Health flag; the verification flag `decide` reads |
| `end_<situation>` | `chosen_end` | The situation key (`auto` for `end_auto`) |
| `decide` | `final_outcome`, `final_reason`, `decide_situation`, `decide_state` | `final_reason` is written only when the outcome has a reason (an unset variable is dropped from the request) |
| `add_note` pre-hook | `notes_log` | `pid:reason;` entries |
| `create_promise` pre-hook | `promise_guard`, `promise_guard_blocks`, `promise_guard_say` | `ok` or `blocked: <reason>`; how many calls were blocked (text counter); the spoken correction of a block |
| `promise_fix` | `promise_guard`, `promise_fix_text` | `handled` (so the transition fires once per block); the text `promise_fix_say` speaks |
| negotiate extraction | `call_situation` | Fallback situation for `end_auto` |
| | `<intent>_seen` (`yes` / `no`) | The latch of an intent with `overrides` (`hardship_seen`), read by the guard nodes |
| `guard_<intent>` | `<intent>_blocked` (`yes` / `no`) | Routing condition of the guard |
| engine | `<tool>_success_count`, `<tool>_failure_count`, `<node>_success_count`, `<tool>_response.*`, `<tool>_args.*` | Read by transitions and by `decide` |

Two more snippets are generated and tested but not wired into the staged graph: `check_callback_snippet` (`callback_check`,
`callback_suggestion` from `chk_cb_date` / `chk_cb_time`) and `check_promise_snippet` (`promise_check`, `promise_date_check`,
`promise_amount_check`, `promise_suggestion` from `chk_product_id` / `chk_amount` / `chk_date`). They mirror
`policy.calendar.check_callback`, `check_promise_date` and `money.check_promise_amount` and are meant for code nodes (a repair
profile); inside a tool call the enforcement is the promise-guard hook.

### The decide snippet

`decide_outcome_snippet` mirrors `policy.outcomes.compute_outcome` (the reference engine) and is tested against it on a grid of
133 call situations for Brief A and 645 for Brief A+B (every combination of party, verification, transfer queue, intent, notes and callback state the code can represent), plus the 13 public cases.

0. Service failures of `create_promise` are `create_promise_failure_count - promise_guard_blocks` (never below 0): the engine counts a blocked call as failed, but it is not a service failure.
1. Facts from `vars`: promised (`create_promise_success_count`), notes (`notes_log`, else the notes tool's own `_args`), transfer
   queue (a `handoff_*` node that was entered), callback booked, `chosen_end`, verified.
2. Active situation keys, structural first (`promise_recorded`, `product_notes`, `recording_failed`, identity, party, callback), then
   the transfer key and the chosen key.
3. A promise plus a transfer with `closing.transfer_after_promise_outcome` -> that outcome (a missing reason is the transfer
   row's reason); a promise with `promise_wins` -> the promise row.
4. Otherwise the first active key in `closing.resolved_precedence()` that resolves: `product_notes` through the notes rule
   (`first_non_paid_note`: the reason of the first non-paid note in `get_account` order; `all_paid_outcome` only when **every**
   product in `product_ids` has a paid note - a product with no note is not paid, and when the existing notes are all paid but
   some product has none, the notes do not decide and the chosen situation does), any key through its
   outcome row (rows can require a verified or an unverified call), then the IR section's own outcome.
5. Fallback: `persuasion.stop_outcome` when verified, `identity.on_declined` for a customer who was reached, else
   `right_party.unclear_outcome`.

Rows the table lacks but the compiler needs (the persuasion stop, intent exits, structural situations) are synthesised by
`naming.closing_rows` and appended, so table rows always win.

## 5. Rule to mechanism mapping

Every IR section carries `rule_ids`. The compiler records where each section is enforced:

| IR section / ledger category | Mechanism | `rule_map` locations |
|---|---|---|
| `phrases` | Opening `tts`, `{{debt_statement}}`, `callback_done`, `promise_done`, `identity_failed`, system prompt (robot / scam / unknown answers) | `node:opening/on_enter`, `node:negotiate/on_enter`, `node:promise_done`, `system_prompt` |
| `right_party` | Opening prompt and tool-call exits | `node:opening/task_message`, `node:opening/transitions` |
| `third_party`, `callback` | Third-party prompt, `schedule_callback` + hooks, `callback_facts`, `callback_done` | `node:third_party/...`, `tool:schedule_callback`, `code:setup`, `node:callback_done` |
| `identity` | Identity prompt, `verify_identity` hooks, deterministic routes, `identity_failed` | `node:identity/...`, `tool:verify_identity/post_process_code`, `node:identity_failed` |
| `disclosure` | Product names and the debt statement in code | `code:account_facts`, `node:negotiate/on_enter` |
| `calendar`, `bands` | Date table, deadlines, allowed dates | `code:setup`, `code:account_facts` |
| `promise_dates` | Rules 3 of the negotiation prompt, allowed-dates list | `node:negotiate/task_message`, `code:account_facts` |
| `amounts` | Floors and the exact floor-offer sentence in code, rule 4 of the prompt | `code:account_facts`, `node:negotiate/task_message` |
| `persuasion` | Attempt scripts in code, rule 5 of the prompt, the stop exit | `code:account_facts`, `node:negotiate/task_message`, `code:decide` |
| `intents` | One prompt bullet per intent with an exit, one tool-call transition and an `end_*` / `handoff_*` node | `node:<stage>/task_message`, `node:<stage>/transitions`, `node:end_<situation>`, `code:decide` |
| `confirmation`, `recording` | Rules 8-11 of the prompt, `create_promise` hooks, `promise_done` | `node:negotiate/task_message`, `tool:create_promise/post_process_code`, `node:promise_done` |
| `multi_product` | Block B1-B8 of the prompt, `add_note` pre-hook, notes closing rule | `node:negotiate/task_message`, `tool:add_note/pre_process_code`, `code:decide` |
| `closing` (rows, precedence) | `decide` + `close` | `code:decide`, `node:close` |
| `residual_rules`, uncovered ledger rules | Verbatim text with the id in the prompt of the rule's scope | `system_prompt` (scope global), `node:<stage>/task_message` |

`checks.coverage.coverage_report(ledger, rule_map)` returns `covered`, `uncovered` and `superseded`; superseded rules are never
required and never enforced. The kit fixtures are fully covered in both profiles (`test_every_active_rule_of_the_kit_briefs_is_enforced_somewhere`).

### Intent classification (`naming.intent_exit`)

| IntentRule | Becomes |
|---|---|
| has an `outcome`, no transfer | `end` exit: a bullet, a tool-call transition to `end_<situation>`; the situation is the outcome-table row with the same outcome and reason (else the intent key) |
| has a `transfer_to_agent` action | `handoff` exit to `handoff_<queue>`; with an `offer`, "ask once, accept -> transfer, decline -> `declined_outcome` (end) or a note" |
| has a `note_reason` and the brief has several products | `note` guidance: call the notes tool with that reason and continue |
| nothing of the above | guidance only: no exit. Catalog intents that the fixed prompt text already covers (`proposes_date`, `flat_refusal`, ...) are skipped, other response texts become a bullet |

## 6. Prompts and prose

Templates live in `builder/prompts/agent/*.v1.j2` and are rendered by `compiler/prompts.py`:

* Jinja delimiters are `<< >>`, `<% %>` and `<# #>`, so the literal `{{variable}}` references of Freya pass through untouched.
* `StrictUndefined`: a template that needs a value the compiler did not pass fails at compile time.
* A loader keeps the line break after inline block tags, which `trim_blocks` would otherwise eat.

| Template | Used for |
|---|---|
| `system_prompt.v1.j2` | Global rules: style, the before-verification ban (`forbidden_before_verification`), robot / scam / unknown answers, goodbye rule, global ledger rules |
| `opening.v1.j2` | Who is on the line: confirmation examples, the second ask, exits |
| `third_party.v1.j2` | Disclose nothing, ask when reachable, `{{callback_facts}}`, booking rules |
| `identity.v1.j2` | The questions in order, how to send each answer, hints, decline handling |
| `negotiate.v1.j2` | `{{account_facts}}`, `{{date_table}}`, rules 1-5 (promise), intent bullets, several-product block B1-B8, rules 8-11 (confirm and record) |
| `lean.v1.j2` | Wrapper of the lean profile: the stage sections plus the outcome table |

Fixed rules of every prompt: short turns, one question per turn, amounts with two decimals (and the currency word only when the
brief has one), speak after every tool result, never claim "recorded" before the tool result says so, say goodbye in the same
message as the finishing tool.

**Rule tagging.** Section headings and intent bullets carry the ledger ids they implement (`[A-19, A-20, ...]`, at most four ids and
a count) so a reader can trace a sentence to the brief. Ledger rules that no IR section cites (`context.uncovered_rules`) are
appended verbatim under "Other rules of the brief for this step"; superseded rules are never included.

**Prose.** `NodeProse.per_node[<node>]` is appended at the end of the node's prompt, `system_prompt_extra` at the end of the system
prompt. The lean profile reads the keys `opening`, `third_party` and `identity` for its `call` node and `negotiate` for its other node.

**Free-form phrase keys.** `Phrases.extra` keys are chosen by the extraction stage. The compiler reads (exact key, or any key ending
in `_<name>`): `disclosure_intro` / `intro` (the first sentence of the debt statement), `today_question_all` (the question for
several products), `floor_offer` (template with `{floor_label}`, `{floor_amount}`), `below_floor`, `unknown_answer`,
`decline_explanation`. A missing key falls back to a neutral sentence built from other IR fields.

## 7. Tools and hooks

Model-called tools are built from the contracts (`tools.model_tool`): `customer_id` is baked as `{{customer_id}}`, every other required
field is a leaf under `body.properties` with `default: "{{<name>}}"` (no variable of that name exists, so the model fills it), the
contract's type, enum and description, `body.required`. Engine calls (`tools.engine_call`) bake every field.

| Tool | Hooks | Effect |
|---|---|---|
| `verify_identity` | pre: `pre_hook_identity_answers` | Numeric dates (`14/03/1988`) become `YYYY-MM-DD` in the brief's date order; digit answers keep digits only |
| | post | `what_to_do_next`: all verified -> "say nothing, the call continues"; unreadable -> ask again; each IR hint (`counts_as_wrong` decides the note); verified -> "ask the next question"; no wrong answers left -> the end sentence; else "wrong, do not reveal the answer" |
| `create_promise` | pre: `pre_hook_coerce` + `pre_hook_promise_guard` | Product id completed from `product_ids`, amount as a number rounded half-up to cents, date as `YYYY-MM-DD`; a missing value is never written back empty. Then the **promise guard** and its spoken correction (section 2, rows 12 and 23) |
| | post | Recorded -> say the success sentence; not recorded -> "never say it is recorded", retry once with the same values, then the failure sentence and `transition_to_end_recording_failed`. A branch for the platform's "missing URL" answer is kept as a safety net, but the post-hook does not run for a blocked call (E-9), so it is not what the customer hears |
| `add_note` | pre: `pre_hook_notes_log` | Normalises product id and reason, appends to `notes_log` |
| | post | Saved / not saved instruction |
| `schedule_callback` | pre: date and time normalised; post | Booked -> "say nothing, the system confirms"; not booked -> check against the allowed times and ask again |

All hooks are wrapped in `try ... except Exception: pass`, use `isinstance` checks, drop `result['header']` and contain no `{{`.
The hook generators (`post_hook`, `pre_hook_coerce`, ...) assert that.

Code rules for every generated snippet (`pycode.assert_safe_code`, lint L008): reads and writes through `vars`, never raises (a
fallback writes the variables later prompts read), strings for flags, only `json` / `datetime` imports, no `{{` / `}}` (IR data is
embedded as a JSON string whose braces are written `\x7b` / `\x7d`), no f-strings, no lambdas or closures, no `strftime` (it imports `time`).

## 8. The lean profile

`profile="lean"` compiles six nodes: `setup`, `call`, `load_account`, `account_facts`, `negotiate`, `hangup`.

```mermaid
flowchart LR
    setup --> call
    call -- "all_verified true|True" --> load_account --> account_facts --> negotiate
    call -- "close_call_success_count &gt; 0" --> hangup
    negotiate -- "close_call_success_count &gt; 0" --> hangup
```

`call` is opening, third party and identity in one node with the tools `verify_identity`, `schedule_callback`, `transfer_to_agent`
and `close_call`. `negotiate` has `create_promise`, the notes tool, `transfer_to_agent` and `close_call`. The prompts carry the
outcome table (`lean.closing_text`) and the model chooses the row; the engine only ends the call after a successful `close_call`.
The same code nodes, hooks, prompt templates and rule map are used. It trades the deterministic outcome engine and the fixed
sentences for fewer nodes; experiments E-3 and E-4 compare the two.

## 9. Lint rules

`checks.lint.lint_workflow(wf, tool_api, call_variables) -> LintReport`; `report.ok` is true when no `error` is left; warnings go to
the trace. Works on dicts, so it also checks the reference agents and platform exports.

| Rule | Sev. | Check |
|---|---|---|
| L001 | error | No `null` anywhere |
| L002 | error | `tools` only on conversation nodes (a workflow-level tool needs `alwaysRunsAt`: warning) |
| L003 | error | `pre_actions` only on api_request / code nodes |
| L004 | error | Passthrough nodes have `passthrough_default_transition` and end with an `always` transition; api/code nodes must be passthrough |
| L005 | error | Every node reachable from `initial_node` (global nodes are roots) |
| L006 | error | Every node can reach an `end_call` node |
| L007 | error | `initial_node`, transition, default and max-turn targets exist; `__previous__` only in global nodes |
| L008 | error / warn | Code and hooks: no `{{`/`}}`, compile; warning for imports beyond json/re/datetime and for a long code node without `try` |
| L009 | error | `response_mapping`: paths start with `data.`, identifiers as variables, node level for pre-actions (a mapping inside a pre-action is ignored) |
| L010 | error | `equals` on a boolean tests both spellings in one OR group; no bare boolean `equals` |
| L011 | error | `api_call` parameters: only url/method/headers/body, `default` never `const`, body fields are leaves with `default`, `required` names exist |
| L012 | error | Tool name in the tool API; url and method equal the contract; body fields are request fields; required fields present; enum values within the contract; baked literals within the enum |
| L013 | error | `close_call` reachable before every `end_call`, never twice on a path (a model-called close counts as once; a lifecycle end-of-call tool satisfies the rule) |
| L014 | error | Every `{{variable}}` and condition variable resolves: call variables, `test_variables`, `vars['x'] =` in code (dynamic suffixes too), extraction names, mapped names, `<tool>_response.*`, `<tool>_args.*`, `<tool\|node>_success_count`, `system.*`, `call.*`, `<node>.current_turns` |
| L015 | error | Extraction fields set `min_confidence` |
| L016 | error | `end_call`: silent `on_enter`, no transitions, `disable_inactivity_monitor` true, `end_call` handler |
| L017 | error | `max_node_turns` comes with `max_turn_transition` |
| L018 | warning | `task_message` or `system_prompt` over 6000 characters |
| L019 | error | No `agent_config` |
| L020 | warning | Tool descriptions say when to CALL the tool |
| L021 | error / warn | Top-level condition groups use `or`; bare variable conditions and deterministic transitions listed after a `matches_intent` one warn |
| L022 | warning | `{name}` IR placeholder left in spoken text |
| L023 | error | `is_tool_call` only on conversation nodes |
| L024 | warning | Node `name` differs from its key |
| L025 | error | Transition has a `target_node`; `matches_intent` has a description |
| L026 | warning | Duplicate tool names in a node |
| L027 | error | `tts` entry has text |

The staged workflows of the kit briefs lint with zero errors; the only warning is L018 for the several-product negotiation prompt
(about 7 500 characters; Brief A's is 5 500; the hand-written reference's was 6 572 before the v6-v8 lessons). The validated reference agents also lint with zero errors, which is the
check that the rules do not reject what is known to work.

`checks.code_sandbox` runs snippets and hooks locally with the platform's documented limits (only `json`, `re`, `datetime`, `math`; no
file or network; a trace-based time limit). It refuses `import time`, which is how the `strftime` pitfall was found.

## 10. Verified and unverified platform behaviours

**Verified** (kit documentation "verified in a working builder", or measured on the reference agent with the official scorer):

* A passthrough `code` node can be the initial node; `vars` holds the call variables; strings written to `vars` resolve as
  `{{var}}` in later prompts; `json`, `re`, `datetime` import.
* `api_request` node with a `get_account` pre-action and node-level `response_mapping` `data.products` -> `products`: the products reach
  the following code node (as a list, a JSON string or flattened keys; the snippet reads all three).
* Tool-call transitions fire immediately and the model says goodbye in the same message.
* `verify_identity` post-hook `what_to_do_next`, deterministic routes on `...all_verified` (`true` / `True`) and `wrong_answers_left` = 0.
* `close_call` with an unset `{{final_reason}}`: the field is dropped from the request.
* **E-8**: a variable written in a pre-hook (`vars['x'] = ...`) persists in the call's variable table and is visible to routing
  (`route_evaluated` showed `promise_guard`). `notes_log` via the `add_note` pre-hook and the guard's `promise_guard*` variables
  are therefore sound.
* **E-9**: `config['url'] = ''` in a pre-hook sends no request; the platform emits `function_call_error: Missing required field: url`,
  sets `<tool>_response.status` to `error` (status code 400) and increments `<tool>_failure_count`. **The post-hook does not run on
  this path**, so the model only sees a generic error; the spoken correction is the job of `promise_fix` (principle 23) and `decide`
  subtracts `promise_guard_blocks` from the failure counter.
* `on_enter` `tts` with a code-computed `{{variable}}`; deterministic routes on `<tool>_success_count` and `product_count`; the goodbye-loop
  breaker with a `call_situation` extraction fallback; the "We record this call" phrasing; per-product attempt scripts.

**Not verified** (implemented from the documentation or by analogy; the traces and the practice run will show them):

* The **recovery flow** as a whole (`promise_guard` contains `blocked` -> `promise_fix` -> `promise_fix_say` -> `negotiate`): its parts are
  verified (E-8, E-9, tts passthrough nodes, a first-entry-only `on_enter`) and it is the design of reference v10, but its effect on the
  official score is not measured yet. `decide` still falls back to `add_note_success_count` and `add_note_args.*` (last note only) when
  the notes log is empty.
* The `verify_identity` **pre-hook** that normalises answers; `pre_hook_coerce` on `schedule_callback`.
* The wording of the spoken corrections is generic English with the product's name, the brief's currency word and spoken dates; how
  customers react to it is not measured.
* A transfer node whose call failed counts as a transfer (`<handoff>_failure_count`).
* `maxRetries` 2 on `close_call` (documented as safe by the API's "nothing was recorded; may be repeated" error text, not measured).
* Dates are spoken with English weekday and month names whatever `ir.language` says.
* A callback is allowed "today" even on a non-business day when the rule is `today_or_next_business_day` (as `policy.calendar` defines it).
* The **lean** profile as a whole: model-called `close_call`, routing on `close_call_success_count`, no fixed sentences.
* `max_turn_transition` behaviour at the cap ("not confirmed" in the kit docs); the compiler always sets it.
* Several-product flows beyond PUB-11..13: the reference reached 3/3 on Brief B in two of three runs; one run hit a transient SSL error.

## 11. Extending the compiler

* **A new mechanism**: add the node in `profiles/staged.py` (`_Staged`), a snippet in `codegen.py` if it needs arithmetic, a row in
  the table of section 3, a lint rule if it can silently fail, and a test that executes the snippet in `code_sandbox`.
* **A new IR field**: read it in the context builders (`ProfileBuilder.*_text`), render it in the template, record its `rule_ids` with
  `rm(...)`, add it to the rule-to-mechanism table.
* **A new lint rule**: add a method `lNNN` to `_Linter`, list it in `run`, the module table and `RULES`, and add a failing and a passing
  example to `tests/unit/test_lint.py`.
* **A new profile**: subclass `ProfileBuilder`, register it in `compile.compile_workflow`; `test_coverage` and the lint tests should
  pass for it unchanged.

## 12. Tests

| File | Covers |
|---|---|
| `test_workflow_model.py` | Round trip of `examples/base-agent.json`, condition and node models, graph helpers |
| `test_tools_builder.py` | Tool builders against `tools/openapi.json`, brace-free embedding |
| `test_hooks.py` | Every hook executed in the sandbox |
| `test_code_sandbox.py` | Import restrictions, time limit, snippet extraction |
| `test_codegen.py` | `setup`, `account_facts`, `check_*` and `decide` against `policy.*` for the 13 practice cases and a grid of situations; the generic-policy outcome test asserts outcome AND reason; amount verdicts are checked against the Decimal helper on the raw input and with literal half-cent vectors |
| `test_money.py` | The Decimal helper, the money input contract as literal vectors, a differential run of the helper and the generated integer-cent code on ~100 inputs, floor / full comparisons at the half-cent boundary |
| `test_outcomes.py` | The reference outcome engine; the paid-invariant table (literal `(outcome, reason)`) run against the engine, the generated `decide` code and the hand-written reference agent |
| `test_lint.py` | One failing and one passing example per rule; reference agents lint clean |
| `test_compile_staged.py` | Fixture IRs lint clean, structure equals the reference, mechanisms, prompts, all 13 cases through the generated code nodes, no brief-specific values |
| `test_compile_lean.py` | The lean graph, outcome table in the prompts, same facts as the staged code nodes |
| `test_compiler_lessons.py` | Reference v6-v10 lessons with literal expectations: promise guard (also in whole cents at the half-cent boundary), the spoken correction of every kind of block (after the deadline, a day that is not allowed under each shift policy, amounts, unknown product, missing facts), the recovery nodes, the deadline marks of the date table, money vectors, date-table hints, turn caps (30 / 30 / 60 / 60), identity completeness and escape exits, the strict opening confirmation, precedence lines |
| `test_coverage.py` | Coverage report and full coverage of the kit briefs |
| `test_prompts_prose.py` | Jinja environment, prose cleaning, context helpers, naming and intent classification |

```bash
.venv/Scripts/python.exe -m pytest tests/unit/test_codegen.py tests/unit/test_lint.py tests/unit/test_compile_staged.py -q
.venv/Scripts/python.exe -m ruff check builder tests
```
