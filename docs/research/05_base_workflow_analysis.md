# 05 — Base workflow deep analysis ("Freya Bank Collections Base (2 nodes)")

Sources read: `live/workflow_base.pretty.json` (GET /workflow/252ee2df-…), `live/agent_base.pretty.json` (GET /agents/2f8dcbea-…),
`live/agents_defaults.pretty.json` (GET /agents/defaults), `live/versions_base.pretty.json`, `docs/openapi_prod.json`
(tool item schema under `POST /agents` → `config.llmConfig.tools.items`), `docs/llms-full.txt`, other agents' spec dumps in
`research/tools/`. Mock bank: only `GET /health` was called (`{"ok":true}`).

Convention: **SPEC** = stated in OpenAPI/docs; **BASE** = literally present in the base workflow JSON; **INFERRED** = my reading,
not documented anywhere. The workflow node schema itself is NOT in the OpenAPI spec (`GET /workflow/{workflowId}` declares
`"workflow": {}` = untyped), so nearly all node-level semantics below are INFERRED from field names + how the base uses them.

---

## 0. TL;DR (what matters most for the builder)

1. The grading contract is almost certainly carried by the **mock bank calls**: `verify_identity` → `get_account` → `create_promise`
   → `close_call(outcome, reason)`. `close_call` is a workflow-level tool with `config.alwaysRunsAt: ["end_of_call"]`, so **every
   call ends with exactly one outcome record**: `promise | no_promise | not_verified | not_reached`, with reason
   `refused` (no_promise), `failed|declined` (not_verified), `wrong_number|unclear` (not_reached), and **no reason for promise**.
   Getting that label right per persona is probably the single biggest scoring lever.
2. Freya personas carry `successCriteria` + `evaluationPrompt` and simulations report `passedRuns/failedRuns/latestPassRate`
   (SPEC). INFERRED: "share of cases handled correctly" = Freya simulation pass rate (LLM judge on transcript) and/or a check of
   the mock bank's recorded state. Optimise for both: correct tool calls AND a clean, compliant transcript.
3. The base's best idea is structural: the **identity node has no account tool and "does not know why the bank is calling"**,
   and the move to `collection` is a **deterministic** check on `verify_identity_response.data.all_verified`. Keep this
   information barrier; never let an LLM-judged transition or a global node reach account data before verification.
4. Biggest base defects: (a) embedded `agent_config` runs **Turkish STT/TTS (`language: "tr"`, voice `leyla`) with English
   prompts**; (b) `temperature: 0.7`; (c) no handling of wrong person / third party / decline / busy / hardship / dispute /
   already-paid / abuse; (d) one refusal ends the call (no persistence); (e) single "past due amount" assumption although
   `get_account` returns *products* (p1, p2…) and `create_promise` is per `product_id`; (f) free-text `reason` (no enum, no
   validation) although `pre_process_code` shows the mock is strict about `reason`; (g) call end depends on the literal phrase
   "Have a good day".
5. `pre_process_code` already references an outcome **`'paid'`** that is NOT in the enum → strong hint that the organisers'
   full contract (the brief) may add outcomes (e.g. already-paid). The builder must take the outcome/reason table **from the
   brief**, falling back to the base enum.
6. Simulation default `settings.maxCallDurationSeconds` = **240 s** (SPEC default) vs agent `maximumDuration` 600. Keep calls
   short: persistence must be bounded (≈2 attempts per objection) or calls get cut before the promise/close.

---

## 1. Node-by-node walkthrough

### 1.0 Workflow envelope (BASE)

| Field | Value | Meaning |
|---|---|---|
| `workflow.initial_node` | `"identity"` | start node |
| `workflow.nodes` | `identity`, `collection` | conversation nodes (`node_type: "conversation"`) |
| `workflow.global_nodes` | `{}` | INFERRED: nodes reachable from any node when their condition fires (interrupt handlers) |
| `workflow.tools` | `[close_call]` | workflow-level tools (INFERRED: available in all nodes; this one auto-runs at end of call) |
| `workflow.system_prompt` | `""` | INFERRED: global prompt prepended to every node's prompt — unused by base |
| `workflow.test_variables` | `{}` | INFERRED: dashboard test values for `{{…}}` variables (agent config SPEC has `testVariables: Record<string,string>`) |
| `workflow.agent_config` | full agent config (llm/stt/tts/…) | INFERRED: runtime voice/LLM config for this workflow (see 1.6) |
| `nodePositions`, `node.position` | x/y | canvas only; builder must emit something sane |
| `version` | `"2.0.0"`, published version 1 (`versions_base`) | |

Per-node fields present on both nodes (values BASE; semantics INFERRED unless marked):

| Field | identity | collection | Semantics (INFERRED) |
|---|---|---|---|
| `on_enter` `{type:"tts", text}` | greeting | "Thank you, that's verified…" | fixed TTS spoken on entering node (not LLM generated) |
| `on_enter_first_entry_only` | false | false | if true, on_enter only on first visit |
| `role_message` | `""` | `""` | persona/system part of node prompt |
| `task_message` | prompt | prompt | node instructions (the real prompt) |
| `tools` | verify_identity, end_call | get_account, create_promise, end_call | tools exposed to LLM in this node |
| `transitions[]` | 1 (→collection) | none | `{condition:{logic, conditions:[{mode, operand, operator, variable}]}, description, target_node}` |
| `interruptible` | **false** | true | whether caller speech barges in on agent TTS |
| `max_node_turns` | 80 | 80 | safety cap on turns in node |
| `max_turn_transition` | null | null | probably "after N turns go to node X" |
| `passthrough` / `passthrough_default_transition` | false / null | same | non-conversational routing node |
| `pre_actions` | [] | [] | actions (tool calls?) on node entry — candidate for auto `get_account` (UNVERIFIED) |
| `response_mapping` | [] | [] | map tool-response paths → variables (SPEC on tool level: `{path, variable, description}`) |
| `extraction_schema` | [] | [] | structured extraction (SPEC on tool level: `name, type ∈ {string,integer,float,boolean,date,datetime,enum}, description, extraction_prompt, validation_pattern, max_retries, required, overridable`) |
| `assertions` | [] | [] | per-node checks for tests/simulations |
| `truncation_strategy` / `truncation_window_size` | keep_all / 10 | same | history truncation |
| `suppress_tool_results` | false | false | if true, LLM would not see tool output — must stay false (amounts come from tool output) |
| `disable_inactivity_monitor` | false | false | silence handling stays on |
| `return_to_previous_on_interrupt` | false | false | for global nodes: return to the node that was interrupted |

### 1.1 Node `identity` (initial)

- `on_enter` (tts): **"Hello, this is the digital assistant of Freya Bank. Am I speaking with {{title}} {{last_name}}?"**
  Identical to `agent_config.llmConfig.firstMessage` with `firstMessageMode: "assistant-speaks-first"` and
  `disableInterruptionsDuringFirstMessage: true`. Note the AI self-disclosure ("digital assistant") and that the bank name is
  revealed to whoever picks up (acceptable; the debt is not).
- `task_message` (verbatim essentials):
  - "Your only job here is to verify the customer's identity. Speak English, one or two short sentences per turn."
  - "Once the customer confirms who they are, ask for their date of birth, then the last four digits of their phone number,
    and call verify_identity for each answer."
  - "You do not know why the bank is calling. If asked, say you can explain after the security check."  ← information barrier
  - "If the identity check fails, say goodbye, ending with "Have a good day.""
- Tool **`verify_identity`** — `handler: "__function__:api_call"`, `config: {timeoutMs: 12000, maxRetries: 0}`:
  - `url` default `https://freya-bank-mock.freyavoice.workers.dev/v1/verify_identity`, `method` `POST`,
    `headers.Content-Type` `application/json`.
  - `body.required: ["field","answer"]`;
    `field`: `enum ["date_of_birth","phone_last4"]`, default `"{{field}}"`, description "Which answer this is.";
    `answer`: string, default `"{{answer}}"`, description "date_of_birth as YYYY-MM-DD, phone_last4 as four digits.";
    `customer_id`: default `"{{customer_id}}"` (not required → filled from call variable, INFERRED).
  - Tool description: "Check one security answer. Call it once for each answer the customer gives."
  - So: **one call per answer**; the mock evidently keeps per-customer state and returns an aggregate `all_verified`
    (INFERRED: true only once both fields have been verified correctly for this `customer_id`).
- Tool **`end_call`** — `handler: "__function__:end_call"`, `config.timeoutMs: 10000`, params (all defaults, nothing for LLM):
  `message_mode: "none"`, `interruptible: true`, `trigger_pattern: "Have a good day|have a good day"`,
  `trigger_timeout: 3`, `trigger_pattern_both: "goodbye|bye|have a good day|you too|thank you|thanks|take care"`.
  Description: "You normally do NOT call this: the phrase "Have a good day." ends the call by itself. Call it without any
  text only if the customer answers after your goodbye." (see 1.5)
- **Transition** → `collection`, `description: "both security answers verified"`:
  `condition.logic: "or"` of two `{mode:"exact", operator:"equals", variable:"verify_identity_response.data.all_verified"}`
  with `operand: "true"` and `operand: "True"`.
  INFERRED from this:
  - every tool's result is stored in a variable named **`<tool_name>_response`**; `.data` is either the api_call wrapper's
    body field or a `data` object inside the mock's JSON (cannot tell without a test call — see Open questions);
  - values are compared as **strings**, and the runtime is Python (hence `"True"` = `str(True)`; also `pre_process_code` is
    Python) — always add both spellings for boolean checks;
  - the variable is **overwritten on every call**, so the check reflects the latest verify response (fine, because
    `all_verified` is cumulative server-side).
- `interruptible: false` → the caller cannot barge in during identity-node speech (bad for "wrong number!" interruptions).
- No `{{now}}` in this node; no attempt limit; no exits other than the goodbye phrase.

### 1.2 Node `collection`

- `on_enter` (tts): **"Thank you, that's verified. I'm calling about your Freya Bank account, do you have a minute?"**
  (first disclosure of purpose happens only here, after deterministic verification — good). The trailing question invites
  "no, call me later", which the prompt does not handle.
- `task_message` (verbatim essentials):
  - "The customer is verified. Speak English, one or two short sentences per turn."
  - "First call get_account, then tell the customer the past due amount and ask when they can pay it."
  - "Get a payment date, read the amount and the date back, and call create_promise after the customer says yes."
  - "When the promise is recorded or the customer will not pay, say goodbye, ending with "Have a good day.""
  - "Now: {{now}}."  ← only place where the current date is injected (needed for relative dates like "next Friday").
- Tool **`get_account`** (api_call, `timeoutMs 12000`, `maxRetries 0`): `POST …/v1/get_account`, `body.required: []`,
  `body.customer_id` default `"{{customer_id}}"`. Description: "The customer's late products. Call it once, before you say
  anything about the account." → response is a **list of late products** (INFERRED shape: product_id like `p1`, product
  type/name, past-due amount, maybe currency/days past due/minimum).
- Tool **`create_promise`** (api_call, `timeoutMs 12000`, `maxRetries 0`): `POST …/v1/create_promise`,
  `body.required: ["product_id","amount","date"]`:
  `date` string default `"{{date}}"` "Payment date as YYYY-MM-DD."; `amount` **number** default `"{{amount}}"` "Amount the
  customer will pay."; `product_id` string default `"{{product_id}}"` "product_id from the account result, for example p1.";
  `customer_id` default `"{{customer_id}}"`. Description: "Record a payment promise. Call it only after the customer said yes
  to the read-back of amount and date." → the read-back gate exists only as prompt + tool description.
  One promise = one product. `maxRetries: 0` is correct here (POST is not idempotent).
- Tool `end_call`: identical to identity's.
- **No transitions** (terminal node). Persistence = none ("or the customer will not pay, say goodbye").

### 1.3 Workflow-level tool `close_call`

- `handler: "__function__:api_call"`, `config: {timeoutMs: 12000, maxRetries: 0, alwaysRunsAt: ["end_of_call"]}`.
  SPEC: `alwaysRunsAt` items enum = `"each_turn" | "start_of_call" | "end_of_call"`. INFERRED: at call end the platform asks an
  LLM to fill the tool's LLM-facing params from the full conversation (tool description: "Record how the call ended, from the
  conversation.") and fires the HTTP call — also when the customer hangs up or the call times out.
- `POST …/v1/close_call`, `body.required: ["outcome"]`:
  - `outcome`: `enum ["promise","no_promise","not_verified","not_reached"]`, default `"{{outcome}}"`, description:
    "promise: a payment promise was recorded. no_promise: verified customer, no promise. not_verified: the identity check did
    not pass. not_reached: the right person was not on the line."
  - `reason`: string (no enum!), default `"{{reason}}"`, description: "Leave out for promise. no_promise: refused.
    not_verified: failed (wrong answers) or declined (would not answer). not_reached: wrong_number or unclear."
  - `customer_id`: default `"{{customer_id}}"`.
- `pre_process_code` (SPEC: tool item has `pre_process_code` / `post_process_code`, string|null; semantics INFERRED):
  ```python
  try:
      b = config.get('body')
      if isinstance(b, dict) and b.get('outcome') in ('promise', 'paid'):
          b.pop('reason', None)
  except Exception:
      pass
  ```
  INFERRED semantics: Python executed before the HTTP request with `config` = the resolved tool-call dict
  (`url`, `body`, `method`, `headers`); in-place mutation changes the request. Purpose: when `outcome` is `promise` the LLM may
  leave `reason` empty or the unresolved literal `"{{reason}}"` may remain → the organisers strip it, which implies **the mock
  validates `reason` strictly** (probably rejects a reason on promise and an unknown reason elsewhere). The `'paid'` branch
  hints at an extra outcome (already paid) in some version of the contract.
- Outcome decision table implied by the descriptions:

| Situation at end of call | outcome | reason |
|---|---|---|
| ≥1 `create_promise` succeeded | `promise` | (omit) |
| verified, no promise recorded (refused, disputes, hardship w/o promise, already paid?, hung up) | `no_promise` | `refused` |
| identity check failed (wrong DOB / last4) | `not_verified` | `failed` |
| right person but would not answer security questions (incl. hung up / "call me later" during verification — INFERRED) | `not_verified` | `declined` |
| person says it is not them / wrong number | `not_reached` | `wrong_number` |
| could not establish who is on the line (third party, silence, garbled, voicemail, hung up at greeting) | `not_reached` | `unclear` (INFERRED for third party) |

### 1.4 How the pieces encode the gates (BASE)

| Gate | Mechanism in base | Strength |
|---|---|---|
| Right party | greeting asks "Am I speaking with {{title}} {{last_name}}?"; prompt "Once the customer confirms who they are" | prompt only, no exit path |
| Identity verification | verify_identity per answer + deterministic transition on `all_verified` | **strong** (structural) |
| No disclosure before verification | identity node lacks get_account and is told it does not know the purpose | **strong** (structural) |
| Commitment read-back | prompt + create_promise description | weak (prompt) |
| Tool success | nothing (prompt says "When the promise is recorded") | weak |
| Close | end_call trigger phrase + close_call `alwaysRunsAt end_of_call` | medium (phrase-fragile) |

### 1.5 end_call mechanics (INFERRED from params + description)

- `trigger_pattern` "Have a good day|have a good day" is a regex/alternation matched against the **agent's own speech**; when
  it matches, the call is hung up automatically after `trigger_timeout` = 3 s; `message_mode: "none"` = no extra farewell
  spoken when end_call fires; `interruptible: true` = farewell can be barged in.
- `trigger_pattern_both` ("goodbye|bye|have a good day|you too|thank you|thanks|take care") is probably only armed after the
  farewell started (matched on either party's speech to finish the exchange). Evidence: the collection `on_enter` itself
  starts with "Thank you", so if this pattern were always live the base would hang up immediately on entering collection.
- The agent-level `llmConfig.tools` also has a parameterless `end_call` ("triggered SILENTLY in the background"); node-level
  end_call (with triggers) presumably overrides it inside nodes.
- Fragility: the agent saying "have a good day" mid-call ends the call; saying "Have a nice day"/"Goodbye" (or translating to
  another language) does **not** end it → dead air until `silenceTimeout` 10 s × `maxRetries` 2 → wasted call budget.

### 1.6 Embedded `agent_config` vs agent record vs platform defaults

| Setting | `workflow.agent_config` (BASE) | agent record `config` | `GET /agents/defaults` |
|---|---|---|---|
| LLM | provider/model `experimental-5`, temp **0.7**, maxTokens 1000 | `openai` / `gpt-4o`, temp 0.7 | `experimental-5`, 0.7 |
| firstMessage | identity greeting | "Hello! How can I help you today?" | "Selam! Sana nasıl yardımcı olabilirim?" |
| systemPrompt | generic "You are a helpful assistant…" | same | same |
| STT | `Freya` / `freya-stt`, **language `tr`**, smartTurn on | `deepgram` / `nova-3`, `en`, keyterms "Freya" | Freya `tr` |
| TTS | `Freya` / `freya-tts`, voice **`leyla`**, **language `tr`**, Turkish interruption/ack phrases | `cartesia` / `sonic-3.6`, voiceId `9287676d-f0cc-423f-ac03-3b3c7242f091`, `en`, English phrases | Freya `tr`, leyla |
| textSubstitutions | `$`→"dollars", `%`→"percent", … `turkishairlines` | same | same |
| callTimeoutSettings | silenceTimeout 10, maxRetries 2, maximumDuration 600 | same | same |
| voicemailDetection | disabled | disabled | disabled |

INFERRED: the workflow's embedded `agent_config` is what runs (it was clearly edited: firstMessage = greeting), i.e. platform
defaults + greeting. In **voice** simulations (simulation `mode` default is `"voice"`, SPEC) English callers would be
transcribed by a Turkish STT and spoken by a Turkish voice. The agent record's English cartesia/deepgram block is a ready-made
English config to copy. Must be confirmed with one test call (Open questions).

---

## 2. Variables in play

| Variable | Where used | Source | Notes |
|---|---|---|---|
| `{{title}}` | identity `on_enter`, `firstMessage` | per-call customer variable (simulation `customerVariablesMap[personaId]`, chat `customerVariables`, `POST /call` `properties`, campaign columns) — SPEC for the mechanisms; names INFERRED from base | e.g. "Mr"/"Ms"; may be empty |
| `{{last_name}}` | same | same | |
| `{{customer_id}}` | `body.customer_id` default in all 4 mock tools | same | never LLM-filled (not in `required`); keys the mock's per-customer state |
| `{{now}}` | collection `task_message` | INFERRED platform system variable (current date/time) | format/timezone unknown; absent from identity node |
| `{{field}}`, `{{answer}}` | verify_identity body | INFERRED: LLM-filled tool args (placeholder default = "ask the model") | `required` |
| `{{product_id}}`, `{{amount}}`, `{{date}}` | create_promise body | LLM-filled | `required`; amount must be a JSON number |
| `{{outcome}}`, `{{reason}}` | close_call body | LLM-filled at end_of_call | `outcome` required; `reason` optional, stripped for promise |
| `verify_identity_response.*` | transition condition | INFERRED: auto variable `<tool>_response` | likely also `get_account_response`, `create_promise_response`, `close_call_response` |

INFERRED rule the builder should follow: a parameter whose `default` is `"{{x}}"` where `x` is a known call variable is
auto-substituted; if `x` is not a known variable, the parameter is surfaced to the LLM (listed in `required`). Fixed plumbing
(`url`, `method`, `headers`) uses literal defaults. Risk: a builder-introduced placeholder that collides with a customer
variable name (e.g. calling an LLM arg `amount` while a persona also has an `amount` variable) would be silently overridden —
keep LLM arg names distinct from customer variable names.

Campaign doc (SPEC): "variables detected in your agent's prompt appear as columns in the customer table" — so any extra
customer variables the brief implies (e.g. `first_name`, `product_name`) would need to be supplied by the organisers' harness;
the builder must **not** depend on variables beyond `title`, `last_name`, `customer_id` (+ `now`) unless the brief lists them.

---

## 3. Mock bank contract (known vs inferred)

| Endpoint | Request body (BASE) | Response (known / INFERRED) |
|---|---|---|
| `GET /health` | – | `{"ok":true}` (observed) |
| `POST /v1/verify_identity` | `{field: "date_of_birth"|"phone_last4", answer, customer_id}` | INFERRED `{ok:true, data:{field, verified/match: bool, all_verified: bool, (attempts_left?)}}` |
| `POST /v1/get_account` | `{customer_id}` | INFERRED `{ok:true, data:{products:[{product_id:"p1", type/name, past_due/amount_due, currency?, days_past_due?, min_payment?}], (total?)}}` |
| `POST /v1/create_promise` | `{product_id, amount:number, date:"YYYY-MM-DD", customer_id}` | INFERRED `{ok:true, data:{promise_id…}}` or `{ok:false, error:"…"}` for bad product/amount/date (horizon, past date, > due, < minimum) |
| `POST /v1/close_call` | `{outcome, reason?, customer_id}` | INFERRED validation of outcome/reason combo |
| unknown customer_id | – | `{"ok":false,"error":"unknown customer_id"}` (established earlier) ; unknown path → `{"ok":false,"error":"not found"}` (`mock_probe.txt`) |

INFERRED statefulness: `all_verified` being cumulative across two separate HTTP calls means the mock stores per-customer
session state — the same store can record *order* (get_account before verification?), every promise (amount/date/product) and
the close outcome. That is exactly what an automated grader needs. Treat every POST as scored.

---

## 4. Inferred grading contract and scenario matrix

### 4.1 What "handled correctly" most likely means

A persona run passes when (INFERRED, combining mock state + persona `successCriteria` LLM judging):
1. **Right outcome label**: `close_call.outcome` (+ `reason`) equals the persona's expected label.
2. **Gate order**: no account/debt detail (amount, product, "late payment") before `all_verified`; `get_account` only after
   verification; nothing disclosed to a third party / wrong person.
3. **Promise correctness** (promise personas): `create_promise` called (only after explicit read-back "yes") with the
   right `product_id`, `amount` = what the customer agreed to (usually the past-due amount, or a policy-valid partial), `date`
   = the date the customer gave, correctly converted to ISO relative to `{{now}}`; not called for refusals; not duplicated.
4. **Money consistency**: every figure spoken exists in the tool result/brief; read-back matches the tool call; no invented
   fees, discounts, totals, interest or consequences.
5. **Conduct**: persistent but respectful (handles the objection at least once before accepting no), empathetic with
   hardship/vulnerability (no pressure, no threats), boundary + polite close on abuse, doesn't claim success when a tool failed,
   ends the call cleanly.

### 4.2 Scenario matrix (expected behaviour → expected mock record)

"V" = verify_identity calls; "GA" = get_account; "CP" = create_promise; "CC" = close_call(outcome/reason).

| # | Simulated customer | Expected agent behaviour | V | GA | CP | CC outcome / reason |
|---|---|---|---|---|---|---|
| 1 | Happy path: confirms, verifies, agrees to pay full past-due on a date within horizon | disclose amount from tool, get date, read back amount+date, CP after "yes", confirm, close | 2 ok | 1 | 1 (p1, full, date) | promise / – |
| 2 | Forgot to pay | brief reminder, ask for date, read-back, CP | 2 | 1 | 1 | promise |
| 3 | Will pay later ("next Friday", "on payday the 25th") | pin down an exact calendar date ≤ horizon, read back weekday+date, CP | 2 | 1 | 1 | promise |
| 4 | Will pay later but beyond horizon ("in 2 months") | negotiate within policy (earlier date / partial now); if still no | 2 | 1 | 0–1 | promise if agreed else no_promise/refused |
| 5 | Can't afford full, offers partial | if brief allows partials ≥ minimum: record partial amount exactly as agreed; else explain and offer allowed options | 2 | 1 | 1 (partial) | promise |
| 6 | Hardship / vulnerable (job loss, illness, bereavement) | empathy first, no pressure/consequences, offer brief's hardship options (smaller amount, later date, hardship team); accept no if they can't commit | 2 | 1 | 0–1 | promise if they freely commit, else no_promise/refused |
| 7 | Disputes the debt ("I never had this card", "amount is wrong") | don't argue, don't insist, explain dispute channel from brief, no promise | 2 | 1 | 0 | no_promise / refused |
| 8 | Already paid | don't push; ask when/how paid (if brief says), explain it may take time to reflect / channel to send proof; no promise | 2 | 1 | 0 | no_promise / refused (or `paid` if the brief defines it — see pre_process_code) |
| 9 | Flat refusal ("I'm not paying") | at least one respectful attempt (benefit / consequence allowed by brief / smaller amount), then accept | 2 | 1 | 0 | no_promise / refused |
| 10 | Multiple late products (p1, p2) | state each product's amount exactly as returned; one CP per product the customer commits to; read back each | 2 | 1 | 1 per product | promise |
| 11 | Abusive / rude after verification | stay calm, one boundary warning, continue if they calm down; if abuse continues, close politely | 2 | 1 | 0–1 | promise if recorded else no_promise/refused |
| 12 | Abusive before verification | same boundary, close | 0–1 | 0 | 0 | not_verified / declined |
| 13 | Wrong number ("no Smith here") | apologise, no details, close | 0 | 0 | 0 | not_reached / wrong_number |
| 14 | Third party (spouse/child/colleague: "he's not here", "I'm his wife, tell me") | do not disclose purpose/debt even if they insist, do not verify them, say we'll try again, close | 0 | 0 | 0 | not_reached / unclear (INFERRED; possibly wrong_number) |
| 15 | Refuses to verify ("I don't give my DOB on the phone") | explain it protects them, ask once more, then close; never disclose | 0 | 0 | 0 | not_verified / declined |
| 16 | Wrong DOB or wrong last4 | don't say which answer was wrong; allow brief-defined retry (default: one re-ask); then close | 1–3 | 0 | 0 | not_verified / failed |
| 17 | Asks "why are you calling / what's this about?" before verification | "I can explain right after a quick security check", continue verification | 2 | 1 | … | per rest of call |
| 18 | Gives DOB in natural language / other format ("5th of March 85") | normalise to `1985-03-05`, confirm if ambiguous (two-digit year, dd/mm vs mm/dd) | 2 | | | |
| 19 | Busy, "call me later" at greeting/before verification | ask for a better time (if brief has callback), close without detail | 0 | 0 | 0 | not_verified / declined (INFERRED; maybe not_reached/unclear) |
| 20 | Busy after verification ("do you have a minute?" → "no") | try for a quick commitment (one sentence with amount, ask for date); if not, schedule callback per brief | 2 | 1 | 0–1 | promise or no_promise/refused |
| 21 | Silence / garbled / hangs up immediately | re-prompt once, close | 0 | 0 | 0 | not_reached / unclear |
| 22 | Speaks another language | follow brief (switch language if allowed, else apologise and continue in brief language) | | | | per rest |
| 23 | Asks for discount / fee waiver / interest info not in tool result | never invent; say they can't change the amount / refer per brief | | | | |
| 24 | Agrees, then changes amount/date during read-back | re-read the corrected values, CP only after final "yes" | 2 | 1 | 1 | promise |
| 25 | create_promise returns error (date too far / amount invalid) | don't claim success; explain constraint, renegotiate once, retry CP with valid values | 2 | 1 | 2 (1 failed) | promise if 2nd ok else no_promise |
| 26 | Customer deceased (relative answers) | condolences, no collection, close | 0 | 0 | 0 | not_reached / unclear |
| 27 | Asks for a human / complaint | follow brief (give number / note request), no transfer tool in base | | | | per state |

---

## 5. Weaknesses / bugs in the base (and builder fix)

| # | Issue (BASE fact) | Impact | Fix (where) |
|---|---|---|---|
| 1 | `sttConfig.language: "tr"`, `ttsConfig.language: "tr"`, voice `leyla`, Turkish interruption/ack/background phrases, while prompts say "Speak English" | voice sims: garbage transcripts, wrong accent, wrong barge-in words | agent_config: set language from brief; copy English block (deepgram nova-3 `en` + cartesia sonic-3.6 voice) from agent record or pick an English Freya voice; English ack/interrupt phrases (builder constants) |
| 2 | `temperature: 0.7` | variable tool args, amount/date drift | 0.1–0.3 |
| 3 | `llmConfig.systemPrompt` generic, `workflow.system_prompt: ""`, `role_message: ""` | no global rules (money, tone, compliance) | put shared rules in `workflow.system_prompt` (+ node `role_message`) |
| 4 | identity: no wrong-person / third-party / decline / busy / silence handling | wrong close labels, possible disclosure | prompt branches + optional separate right-party node |
| 5 | no max verification attempts; "If the identity check fails, say goodbye" (ambiguous: first wrong answer?) | either over-retrying (mock may lock) or no retry | brief-driven `max_attempts`, never reveal which answer failed |
| 6 | identity `interruptible: false` | caller can't cut in ("wrong number!") | set true (keep `disableInterruptionsDuringFirstMessage` if wanted) |
| 7 | collection on_enter asks "do you have a minute?" with no busy/callback path | unhandled branch, wasted turns | drop the question or handle it; get_account before speaking |
| 8 | "tell the customer the past due amount" (singular) vs products list; CP per product | multi-product personas fail; LLM may sum and invent a total | per-product disclosure; one CP per product; totals only if tool/brief provides |
| 9 | read-back only in prompt | CP before explicit yes | prompt protocol + tool description + (optional) commitment node / extraction |
| 10 | no tool-failure handling | agent says "recorded" after `ok:false` | prompt rule + deterministic transition on `create_promise_response.data.ok` (path to verify) |
| 11 | one refusal → goodbye | fails "persistent" criteria | objection playbook with bounded attempts (2) |
| 12 | no hardship / vulnerability / dispute / already-paid / abuse guidance | judge failures on conduct | prompt sections (and/or global nodes) |
| 13 | `reason` is free text, no enum; LLM fills it at end_of_call | wrong/missing reason → mock rejects or mislabels | enum per outcome in schema + richer close_call description (decision table) + validating `pre_process_code` |
| 14 | outcome enum hard-coded; `'paid'` appears in code only | brief may define more outcomes | derive outcome/reason table from brief |
| 15 | call end relies on literal "Have a good day" (+3 s) | early hang-up if said mid-call; dead air if paraphrased/translated | make closing phrase a builder constant used verbatim in every prompt, forbid it elsewhere, localise both prompt and `trigger_pattern` |
| 16 | `{{now}}` only in collection | no date context elsewhere | put `Now: {{now}}` in `workflow.system_prompt` |
| 17 | relative-date arithmetic by LLM | wrong ISO date in CP | read back weekday + full date; customer confirms; low temperature |
| 18 | no amount/date normalisation | `"1,250.00 TL"` or `"17/10"` sent | `pre_process_code` on create_promise: coerce amount to float (2 dp), validate ISO date |
| 19 | `textSubstitutions` only map `$`→"dollars" | other currencies read badly (TTS) | currency substitutions from brief |
| 20 | `maxRetries: 0` everywhere | transient failure = lost step | keep 0 for create_promise/close_call (non-idempotent); verify/get_account: keep 0 unless mock idempotency confirmed (retries may count as failed attempts) |
| 21 | close_call is a workflow-level tool → INFERRED possibly callable by the LLM mid-call | double close records | tell the model never to call close_call itself (it runs automatically) |
| 22 | no `test_variables` | dashboard testing awkward | fill with a sample customer |
| 23 | No consequence/compliance guidance (recording notice, no threats, no legal claims) | judge failures | brief-driven "allowed consequences" list only |
| 24 | Sim default 240 s cap vs long objection loops | promise never reached | short turns, bounded persistence |

---

## 6. Mapping the 5 requirements onto an improved graph

### 6.1 Target graph

```
                 ┌──────────────────────── global (INFERRED semantics; use sparingly) ──────────────────────────┐
                 │ G_abuse  (LLM condition: repeated insults/threats) → warn once, then polite close            │
                 │ G_hardship (LLM condition: distress/vulnerability) → empathy, options; return_to_previous     │
                 │   (only allowed AFTER verification; before verification the identity prompt handles it)      │
                 └───────────────────────────────────────────────────────────────────────────────────────────┘

[right_party] --(LLM: person confirms they are {{title}} {{last_name}})--> [identity] --(exact: verify_identity_response.data.all_verified == true|True)--> [collection]
     |  wrong number / third party / busy / silence: handled in-node, closing phrase           |  declined / failed (after max attempts): closing phrase
     v                                                                                           v
  close (close_call auto: not_reached)                                                     close (close_call auto: not_verified)

[collection]  (get_account first → disclosure per product → objection playbook → negotiation within policy
               → read-back → create_promise → check ok) --(optional exact: create_promise_response.data.ok == true AND
               single-product)--> [wrap_up]  (confirm recorded figures, payment channel, closing phrase)
     |  refusal after bounded attempts / dispute / already paid / hardship-no-commit: closing phrase
     v
  close (close_call auto: promise | no_promise)
```

**Recommended minimal-risk variant (what I would ship first):** keep the base's 2 nodes (`identity` doing right-party +
verification, `collection` doing disclosure + objections + commitment + close), add a strong `workflow.system_prompt`,
rewrite both `task_message`s, enum-ise and validate `close_call`, fix agent_config. Every extra LLM-judged transition and
global node is a new failure mode (and global nodes risk firing before verification). Add `right_party` / `wrap_up` /
global nodes only if tests show the single-node prompts misbehave.

### 6.2 Where each requirement lives

| Requirement | Prompt (system / node) | Transition | Tool / tool config | Builder-side constant / code |
|---|---|---|---|---|
| Right-party gate | identity prompt: confirm name before anything; branches for wrong number, third party, busy, silence; never disclose | (optional) right_party → identity LLM condition | – | greeting template, bank name |
| Identity gate | ask fields in brief order, one verify per answer, normalise formats, never reveal which answer failed, max attempts | **exact** on `all_verified` (keep both "true"/"True") | verify_identity (field enum from brief) | verification fields, max attempts |
| No disclosure pre-verification | "you do not know why the bank is calling" (keep) | structural | get_account only in collection | – |
| Commitment read-back | read back product, amount (exact digits from tool), weekday + date; wait for explicit yes; re-read after changes | optional commitment/wrap_up node | create_promise description repeats rule; `pre_process_code` normalises amount/date | date/amount format strings |
| Tool-success gate | "never say recorded unless create_promise returned ok; on error explain and renegotiate once" | optional exact on `create_promise_response.data.ok` | inspect `<tool>_response` | – |
| Close gate | single closing phrase verbatim, only at the very end; outcome decision table | – | end_call `trigger_pattern` = closing phrase; close_call enum + reason enum + validating `pre_process_code` | closing phrase, outcome→reason table |
| Persistent but flag rudeness | objection playbook, ≤2 attempts per objection; abuse: calm boundary once, then close | optional G_abuse | (flagging only if brief/mock defines a field; else Freya `postConversationAnalysis.insights` / call tags — not visible to the mock) | max_attempts, abuse policy |
| Hardship / vulnerable | empathy first, no consequences, offer brief options, accept no, referral text | optional G_hardship (post-verification only) | – | hardship options, referral contact |
| Money consistency | "only figures from get_account or the brief; never sum/round/convert unless given; same digits in read-back and tool args" | – | `post_process_code` on get_account to precompute total/formatted strings (UNVERIFIED semantics); `pre_process_code` on create_promise to coerce amount | currency code/symbol, decimal format, TTS substitutions |
| Objection taxonomy | forgot → remind + date; can't afford → partial/instalment within policy; disputes → no argue, dispute channel, no promise; already paid → don't press, ask when/how, no promise; wrong person → apologise, close, not_reached; will pay later → pin exact date ≤ horizon | – | – | policy numbers |
| Promise-to-pay extraction | product, amount, date | – | create_promise args; optional node `extraction_schema` (`promise_amount: float`, `promise_date: date`, `validation_pattern: ^\d{4}-\d{2}-\d{2}$`) | – |
| Callback scheduling | ask for a convenient day/time window, confirm, close | – | no mock endpoint → record via outcome only (Freya `reschedule_call` exists for **campaign** calls only per `POST /call.originCallId` description; don't rely on it in sims) | callback policy |

### 6.3 Suggested validating `pre_process_code` for close_call (builder renders the tables from the brief)

```python
try:
    b = config.get('body')
    if isinstance(b, dict):
        allowed = {'promise': None, 'no_promise': ['refused'],
                   'not_verified': ['failed', 'declined'], 'not_reached': ['wrong_number', 'unclear']}
        o = str(b.get('outcome') or '').strip().lower().replace('-', '_').replace(' ', '_')
        if o in allowed:
            b['outcome'] = o
            r = str(b.get('reason') or '').strip().lower().replace('-', '_').replace(' ', '_')
            if allowed[o] is None:
                b.pop('reason', None)
            elif r not in allowed[o]:
                b['reason'] = allowed[o][0]
except Exception:
    pass
```
(Keeps the base's semantics, maps junk/`{{reason}}` to the first allowed reason. Pair it with `enum` on `reason` and a
decision table in the description. Do not invent outcomes here; if the brief defines `paid`, add it to `allowed`.)

---

## 7. The "collections brief" (builder input) — likely content and spec model

### 7.1 What it most likely contains (INFERRED)

Bank identity and voice (bank name, assistant name/AI disclosure, language/locale, tone); who to call (title + last name,
customer id variable names); verification rules (fields, order, formats, attempts, what to say before verification, third
party rule); what to disclose (per-product past-due amount, currency, due date); payment policy (full vs partial, minimum
payment absolute or %, max days until promise date, instalments, payment channels — app/branch/transfer, whether payment can
be taken on the call: almost certainly not); allowed vs forbidden statements (consequences like late fees or credit-bureau
reporting only if listed; no threats/legal claims; no discounts); hardship/vulnerability policy (signals, offers, referral
number); dispute and already-paid procedures; abuse policy; callback policy; outcome codes/reasons; closing phrase; maybe
calling-hours/recording notices; the mock API base URL and endpoint contracts (same as base).

### 7.2 Proposed Pydantic spec (builder's intermediate representation)

```python
from typing import Literal, Optional
from pydantic import BaseModel, Field

class Money(BaseModel):
    currency_code: str = "USD"              # from brief; drives TTS substitutions + prompt wording
    currency_spoken: str = "dollars"
    decimals: int = 2

class Verification(BaseModel):
    fields: list[Literal["date_of_birth", "phone_last4"]] = ["date_of_birth", "phone_last4"]
    max_attempts_per_field: int = 2           # 1 retry
    reveal_failed_field: bool = False
    pre_verification_purpose_line: str = "I can explain right after a quick security check."
    allow_third_party_disclosure: bool = False

class PaymentPolicy(BaseModel):
    require_full_past_due: bool = False
    allow_partial: bool = True
    min_payment_amount: Optional[float] = None
    min_payment_pct: Optional[float] = None   # of past-due
    max_promise_days: int = 14                # horizon from {{now}}
    allow_same_day: bool = True
    allow_instalments: bool = False
    max_instalments: Optional[int] = None
    instalment_max_interval_days: Optional[int] = None
    one_promise_per_product: bool = True
    payment_channels: list[str] = []          # e.g. "mobile app", "online banking", "branch"
    can_take_payment_on_call: bool = False

class ConductPolicy(BaseModel):
    tone: str = "warm, concise, professional"
    max_objection_attempts: int = 2
    allowed_consequences: list[str] = []      # only these may be mentioned
    forbidden_statements: list[str] = ["threats", "legal action unless listed", "discounts/waivers"]
    abuse_warnings_before_close: int = 1
    hardship_signals: list[str] = ["job loss", "illness", "bereavement", "financial distress"]
    hardship_options: list[str] = []          # e.g. smaller amount, later date, hardship team
    hardship_referral: Optional[str] = None   # phone/URL text from brief
    dispute_instructions: Optional[str] = None
    already_paid_instructions: Optional[str] = None
    callback_allowed: bool = True
    human_handoff_text: Optional[str] = None

class OutcomeSpec(BaseModel):
    outcomes: dict[str, Optional[list[str]]] = {
        "promise": None, "no_promise": ["refused"],
        "not_verified": ["failed", "declined"], "not_reached": ["wrong_number", "unclear"]}
    descriptions: dict[str, str] = {}         # per outcome/reason, copied into close_call description

class MockApi(BaseModel):
    base_url: str = "https://freya-bank-mock.freyavoice.workers.dev"
    verify_path: str = "/v1/verify_identity"
    account_path: str = "/v1/get_account"
    promise_path: str = "/v1/create_promise"
    close_path: str = "/v1/close_call"

class CollectionsBrief(BaseModel):
    bank_name: str = "Freya Bank"
    assistant_label: str = "digital assistant"
    language: str = "en"                      # drives STT/TTS + prompts + closing phrase
    timezone: Optional[str] = None
    customer_variables: list[str] = ["title", "last_name", "customer_id"]
    greeting: str = "Hello, this is the digital assistant of {bank}. Am I speaking with {{title}} {{last_name}}?"
    closing_phrase: str = "Have a good day."
    money: Money = Money()
    verification: Verification = Verification()
    payment: PaymentPolicy = PaymentPolicy()
    conduct: ConductPolicy = ConductPolicy()
    outcomes: OutcomeSpec = OutcomeSpec()
    api: MockApi = MockApi()
    compliance_notes: list[str] = []          # recording notice, calling hours, AI disclosure
    verbatim_rules: list[str] = Field(default_factory=list)  # any brief rule not captured above, pasted into prompts
```

Builder pipeline (INFERRED design): brief text → LLM structured extraction into `CollectionsBrief` (defaults = base
values) → deterministic rendering of workflow JSON from a template derived from the base (same tool plumbing, enums from
`outcomes`, prompts assembled from policy fields + `verbatim_rules`) → create/update on Freya.

---

## 8. Open questions / cheap experiments (need user approval: they POST and spend credits)

1. Which config runs for a workflow agent: `workflow.agent_config` (tr/leyla/experimental-5) or the agent record `config`
   (en/cartesia/gpt-4o)? One chat or voice test reveals it.
2. Exact response shapes of the 4 mock endpoints and whether `.data` in `verify_identity_response.data.all_verified` is the
   api_call wrapper or the mock body (one chat-mode run via `POST /chat/message` with a known test `customer_id` would show
   tool results). Needed for any new deterministic transition (e.g. `create_promise_response.data.ok`).
3. Does the mock limit verification attempts / lock? Does it reject `create_promise` beyond a horizon or below a minimum?
4. Expected `reason` for third party ("unclear" vs "wrong_number") and for "busy before verification" (`not_verified/declined`
   vs `not_reached/unclear`) — the brief should settle this.
5. Is `paid` (already paid) an outcome in the real contract? (`pre_process_code` mentions it.)
6. Semantics of `pre_actions`, `global_nodes`, `return_to_previous_on_interrupt`, transition `mode` values other than
   `"exact"` (LLM-judged?), `post_process_code` variable names, `trigger_pattern_both` arming.
7. Are simulations run in `voice` (default) or `chat` mode, and with what `maxCallDurationSeconds` (default 240)?
8. Is `close_call` exposed to the LLM mid-call (double record risk)?
