---
name: collections-workflow-patterns
description: Proven patterns for Freya phone agents that follow a bank's collections brief - code nodes for dates and amounts, tool-call exits, post-hooks, deterministic routes, closing last. Use together with build-phone-agent when writing a collections workflow from a brief.
---

# Collections workflow patterns

These patterns come from a hand-built reference agent that passed the official scorer and were then confirmed by repeated
practice runs (a case only counts when every repetition passes). They say **how** to build; every number, date, name,
sentence, outcome and queue must come from the brief and the tool API file you were given. Never copy a value from an
example in this file or from another brief.

Read `docs/workflow-format.md` and `docs/tools-and-code.md` first - they are the authority on the JSON. Then read every
brief file in full (a later file may amend an earlier one) and the tool API file (field names and allowed values change
from brief to brief; the seven tool names stay).

## The shape of the agent

One node per stage, in this order, with code doing the arithmetic and the model doing the talking:

1. `setup` (code, initial, passthrough): reads `vars['now']`; writes a DATE TABLE of the next ~50 days (ISO date, weekday
   name, today/tomorrow, "this week / next week / in N weeks", business day or weekend or HOLIDAY). A non-business row
   carries "-> if the customer names this day, offer <next business day> instead".
2. `opening` (conversation): `on_enter` is a **tts** line with the brief's fixed opening and the right-party question
   (`on_enter_first_entry_only: true`). Decide who answered: customer, third party, wrong number, deceased, unclear.
3. `third_party` (conversation): disclose nothing; ask when the customer can be reached; the model calls
   `schedule_callback`; a post-hook tells it what to say.
4. `identity` (conversation): the model calls `verify_identity` once per answer. Routing is deterministic on the tool
   result (see below). Too many wrong answers -> a fixed failure line -> end.
5. `load_account` (api_request): `get_account` as a pre-action with `maxRetries` 2 and a node-level `response_mapping`
   (`data.products` -> a variable). Only reachable after verification.
6. `account_facts` (code): per product - spoken name, amounts with two decimals, days late, band, deadline, floor,
   allowed promise dates, the exact sentences the brief prescribes with that product's values (debt statement, floor offer).
7. `negotiate` (conversation): `create_promise`, `add_note`, `transfer_to_agent` as tools with hooks.
8. `end_<situation>` (code, one per way the call can end) and `handoff_<queue>` (api_request: `transfer_to_agent` with the
   queue baked in): they only record what happened (`vars['chosen_end'] = ...`) and go to `decide`.
9. `decide` (code): precedence rules -> `vars['final_outcome']`, and `vars['final_reason']` **only when the outcome has a
   reason** (an unset variable is dropped from the request).
10. `close` (api_request): `close_call` with `{{final_outcome}}` / `{{final_reason}}`, then `hangup` (end_call, silent).

## Patterns that decide whether a case passes

**Do arithmetic in code, never in the prompt.** Business days, holidays, deadlines, "last business day of this week",
floors, rounding to cents: compute them in a `code` node and put the results into variables the prompt shows
(`{{date_table}}`, `{{products_facts}}`). Code rules: read and write through `vars`, wrap in `try/except`, never raise,
no double braces anywhere in the code, only `json`, `re`, `datetime`, `math`. Embed data tables as JSON inside a Python
string and write braces of that JSON as `\x7b` / `\x7d`, so no literal brace pair appears in the source.

**Say prescribed sentences with a tts line, not a prompt.** When the brief fixes the words at a fixed point (opening,
debt statement plus first ask, failure line, post-promise sentence, callback confirmation, farewell), use
`on_enter: {"type": "tts", "text": "{{some_variable}}"}` where a code node built the exact sentence. A model asked to
"say X" sometimes skips or rewords it.

**Leave a stage with a tool-call transition.** `{"target_node": ..., "is_tool_call": true, "condition": {"operator":
"matches_intent", "description": ...}}`: the engine moves immediately and the model says goodbye in the same message.
Name the exits after the stage they lead to (`transition_to_identity`, `transition_to_end_<situation>`,
`transition_to_handoff_<queue>`) and tell the model to say goodbye in the message that calls it.

**Route on tool results, not on what the customer said.** Compare the response variable
(`verify_identity_response.data.all_verified`) with **both** `true` and `True`; use the counters
(`create_promise_success_count`, `schedule_callback_success_count`, `<tool>_failure_count`) for "after the terminal
action succeeded, go to a passthrough node whose tts says the prescribed sentence and goodbye, then to the end node".
A model that announces success before calling the tool and then has nothing left to say leaves the customer on a silent
line.

**Post-hook every tool.** `post_process_code` writes `result['data']['what_to_do_next']` from the result: what is true now
(recorded / not recorded), what to say, which tool to call next, retry once with identical values on a failure, then
apologise and end. Pop `result['header']`. Never let the model claim "recorded" unless `ok` is true.

**Pre-hooks normalise and guard.** `pre_process_code` coerces amounts to numbers with two decimals, dates to
`YYYY-MM-DD`, product ids to the known form, and may block a call that breaks a hard rule (unknown product, a date that is
not allowed, an amount outside floor..full) by emptying `config['url']` and setting a variable the post-hook explains.
The blocking trick is not yet proven on the platform - keep the rule in the prompt as well.

**Identity.** Call `verify_identity` whenever the message contains an answer, even mixed with a question. Only complete
answers are sent (a date needs day, month and year; digits need the exact count): ask for the missing part and combine.
A wrong answer counts only through the tool result (`wrong_answers_left`). Before verification say nothing that reveals
the purpose of the call - none of the words the brief forbids.

**Persuasion by script.** Precompute per product the exact attempts the brief allows ("Attempt 1 - ask: ...", "After
attempt N is refused: make no further offer for this product") and tell the model to make every scripted attempt, never
more and never fewer. Moving a date is not an attempt; offering the deadline for the same amount is not an attempt.
When the brief says an offer is made once (or is the only offer), count it in the script and never repeat it.

**Fixed lines must not contain words a check could misread.** If a per-case check forbids a word before an event
(for example "recorded" before a promise is recorded), do not use that word in fixed lines before the event.

**Goodbye loop.** Add a transition "the agent already said goodbye and the customer only says goodbye" to an `end_auto`
node (`chosen_end = 'auto'`) and let `decide` use an extracted `call_situation` as the fallback. Give every
conversation node `max_node_turns` with a `max_turn_transition` to a computed end (generous: 30 to 60 turns).

**Talkative customers.** A cut-off or small-talk message with nothing usable gets only a short acknowledgement - never a
repeated question, never counted as a refusal. A message that contains an answer, day, time, date or amount is acted on at
once. A callback is booked as soon as a valid day and time are given, without a confirmation question.

**Closing.** `close_call` is the last tool call, exactly once, after every promise and note and after the goodbye. The
outcome follows the brief's precedence table; a recorded promise usually wins over later refusals. With several
products: one promise per product, a note for every product without a promise, and a product counts as "paid" only
when every product has a paid note.

## Before you finish

- The file is valid JSON with **no `null`** anywhere and no `agent_config`.
- Every tool call uses the URL, field names and allowed values of the tool API file; the agent calls nothing else.
- Every node is reachable from `initial_node` and every path ends in an `end_call` node.
- No code contains a double brace; every `{{variable}}` in a prompt or parameter is a call variable, a variable a code
  node writes, an extraction field, a response-mapping variable or a tool counter.
- Tools sit only on conversation nodes; `pre_actions` only on api_request and code nodes.
- Save nothing to Freya and start no simulation unless the task tells you to; the builder checks, deploys and tests the
  file itself.
