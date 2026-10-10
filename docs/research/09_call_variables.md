# 07 — Gap 3: call-variable injection (names, channel) and whether `{{now}}` exists

Status: **partly resolved.** The system-variable names are now settled, and they correct an earlier assumption. Whether `{{now}}` is substituted, and which channel and keys the graders use, cannot be settled with GET-only access. Section 8 gives a minimal write experiment that needs approval, and section 9 has a paste-ready question for the organisers.

Tags: **[SPEC]** = openapi_staging.json; **[UI]** = web-app i18n dictionary (`research/tools/i18n.json`) or web-app JS; **[LIVE]** = GET results; **[DOCS]** = llms-full.txt; **[INFERRED]** = not stated anywhere.

---

## 0. TL;DR

1. **Correction: system-variable tokens are snake_case.** `system.isoUtcNow`, `system.todaysDateTr` and `system.uuid4` are only i18n *description keys*. The tokens the editor inserts are listed below **[UI JS]**. Writing `{{system.isoUtcNow}}` in a prompt will almost certainly stay as literal text **[INFERRED from the UI list; confirm with experiment §8, field C]**.
   - `{{system.iso_utc_now}}`: "Current date and time in ISO 8601 UTC format."
   - `{{system.todays_date_tr}}`, `{{system.todays_date_en_cst}}`, `{{system.todays_date_en_est}}`: today's date in Turkish, US-CST and US-EST locale.
   - `{{system.uuid4()}}` (with parentheses): a fresh 32-character UUID with no hyphens.
   - `{{call.phone_number}}`, `{{call.call_id}}`, `{{call.customer_phone_number}}`, `{{call.direction}}` (inbound/outbound/web/simulation), `{{call.duration}}` (integer).
   - `{{sip_headers}}` (object).
   - `{{attachment.id}}`, `{{attachment.type}}`, `{{attachment.mime_type}}`, `{{attachment.filename}}`, `{{attachment.ids}}`.
   - Also `call.retry_number`, 0-based, campaign calls only **[SPEC: POST /call attemptNumber description]**.
2. **`{{now}}` is not a documented or autocompleted platform variable.** It is absent from both UI variable lists (staging and production builds are identical), from the i18n `promptVariables` block, from the docs and from the spec. Only the hackathon base workflow uses it (`collection.task_message` ends with `Now: {{now}}.`). It therefore resolves only in two cases: the grader sends a customer variable named `now`, or the backend has an undocumented alias. **Which of the two cannot be told without a write call.**
3. **The channel does not matter to the builder; the names do.** All four channels (simulation `customerVariablesMap`, chat `customerVariables`, `POST /call` `properties`, campaign `customers[].properties`) fill the same "before the call" variable table that `{{name}}` reads **[UI class text, INFERRED unification]**. The builder cannot choose the grader's channel. It can only choose which names it references.
   - The base, which the organisers built "for hackathon builds", references exactly `customer_id`, `title`, `last_name` and `now`. Treat those as the grader's contract, and add no other call-start names unless the brief lists them.
4. **Unsent variables fail silently [UI]:**
   - In a prompt or TTS, the placeholder is read out loud.
   - In a condition, the comparison is false every time.
   - In a tool field, the field is dropped if the placeholder is the whole value, or the literal braces are sent if it is mixed with other text.
5. **Safe design: use both `{{now}}` and `{{system.iso_utc_now}}`, with a precedence rule.** Put the date lines at the end of each node's `task_message`, not in `workflow.system_prompt`: the UI warns that this breaks the prefix cache for every node, which revises fix #16 in 05. Never put `now` or customer data in `test_variables`.
6. **Safety finding: never run chat probes against the base workflow.** The base `close_call` tool has `alwaysRunsAt: ["end_of_call"]`, and `POST /chat/end` "fire[s] the end-of-call lifecycle (webhook + post-conversation analysis + end-of-call tools)" **[SPEC /chat/end]**. A probe on the base would therefore POST `/v1/close_call` to the mock bank, which may hold grading state. The earlier proposal in 02 §"first probe" (`/chat/message` with `workflowId: 252ee2df-…`) has this problem.

---

## 1. System variables: exact tokens (evidence)

The web-app JS module `977291` exports `DEFAULT_PROMPT_VARIABLES` and `getDefaultPromptVariables(t)`.

| Build | Chunk | Offset of `DEFAULT_PROMPT_VARIABLES` |
|---|---|---|
| Staging | `scratchpad/webapp/js/2zee5lmq0q484.js` | 158389 |
| Production | `scratchpad/research/gap3_dl/prod/2c7wqckyhebjc.js` (fetched from `https://app.freyavoice.ai/login`, a public static GET) | 158389 |

The two lists are byte-identical in both builds:

```
DEFAULT_PROMPT_VARIABLES = [
 {value:"call.phone_number",detail:"string",...}, {value:"call.call_id",...},
 {value:"call.customer_phone_number",...}, {value:"call.direction",...},
 {value:"call.duration",detail:"integer",...},
 {value:"system.todays_date_tr",...}, {value:"system.todays_date_en_cst",...},
 {value:"system.todays_date_en_est",...}, {value:"system.iso_utc_now",...},
 ...ATTACHMENT_VARIABLES ]            // attachment.id/type/mime_type/filename/ids

getDefaultPromptVariables(t) = [ {value:"sip_headers",detail:"object"}, call.phone_number, call.call_id,
 call.customer_phone_number, call.direction, call.duration, system.todays_date_tr,
 system.iso_utc_now, {value:"system.uuid4()"...}, ...attachment.* ]
```

- The descriptions come from i18n `promptVariables.system.isoUtcNow`, `.todaysDateTr` and `.uuid4`. That is why the camelCase forms appeared in earlier notes.
- i18n also describes `call.callEndReason` and `call.analysis.{summary,tags,structuredDataObject}`, which are only meaningful in end-of-call tools. Their snake_case tokens are probably `call.call_end_reason` and `call.analysis.*` **[INFERRED]**.
- **No `now`, `today`, `current_date` or similar appears in either list.**
- i18n `workflows.variables.class.platform.body`: "The platform computes this … some of these are frozen at the moment the call started." `system.iso_utc_now` is probably frozen at call start, which is fine for date reasoning **[INFERRED]**.
- i18n `calls.variables.strippedNote`: "Dates, prompt sections and routing scratch are removed before a call is stored". Date values therefore will **not** show up in `GET /call/{id}` afterwards. The experiment must have the agent echo them.
- The docs (llms-full.txt) list no system variables at all. The only mentions are "Use `{{variable}}` for dynamic values", the campaign "variables … appear as columns", and the Embed SDK `customerVariables`, described as "Variables injected into the agent's system prompt at runtime" **[DOCS]**. The spec contains no occurrence of `iso_utc_now`, `todays_date`, `{{now` or `system.` **[SPEC grep]**.

---

## 2. Is `{{now}}` real?

| Evidence | Points to |
|---|---|
| Absent from both UI autocomplete lists, i18n, docs and spec | not a platform variable |
| Organisers' base uses `Now: {{now}}.`, placed only in the collection node | organisers expect it to resolve, either because the grader sends `now` or because of a hidden alias |
| `workflow.test_variables` in the base is `{}` **[LIVE]** | the dashboard would flag `now` as "Won't resolve here: Nothing sets now. The agent will read the braces out loud." unless it is a platform name **[UI finding.noProducerPrompt; INFERRED that the editor classifies with the same list]** |
| `calls.variables.strippedNote` mentions "Dates" | the engine does keep date-type values during a call, which may be just the `system.*` dates |

**Verdict:** unresolved. P(grader sends `now`) and P(hidden alias) are both plausible. P(neither) is not negligible either: the base may simply be sloppy, and an LLM that sees a literal `{{now}}` still "works" and just guesses dates. The design in §7 is correct in all three cases.

A further reason to prefer `{{now}}` when it is present: if the grader freezes the simulated date (to make "next Friday" deterministic) and checks `create_promise.date` against that date, then using `system.iso_utc_now` alone would produce off-by-N-days promises **[INFERRED risk]**.

---

## 3. Injection channels: exact spec shapes

| Channel | Spec path | Shape | Value types | Notes |
|---|---|---|---|---|
| Simulation | `POST /simulations` body `customerVariablesMap` | `{[personaId]: {[name]: string}}`, "Per-persona variable values for this run, keyed by personaId. Saved as the persona's most-recently-used values for the simulated agent." | **strings only** | Persona records then carry `simulationVariables: {[agentId]: {[name]: string}}`, "Most-recently-used simulation variable values, keyed by simulated agentId" (returned only with `include=simulationVariables`). |
| Chat | `POST /chat/message` body `customerVariables` | `object`, `additionalProperties: {}` | any JSON | 422 "customerVariables too large". Bound at call start: the UI dev panel says "Parsed as JSON and sent on the next call start." So send it on the first message, the one without `callId`. Sending it on later turns probably has no effect **[INFERRED]**. `POST /chat/fork` has no variables field and inherits the parent's `workflowSnapshot` **[SPEC]**. |
| Phone call | `POST /call` body `properties` | `object`, `additionalProperties: {}`, "Custom properties to pass to the agent" | any JSON | `attemptNumber` is "surfaced to the agent as 0-based call.retry_number". We have 0 phone numbers **[LIVE]**. |
| Campaign | `POST /campaign` `customers[].properties` | object or null; example `{name, company}` | any JSON | Docs: "variables in its system prompt (like `{{customer_name}}`) … appear as columns in the customer table". UI: "These values will be added as customer properties". |
| Unit test | `POST /unit-tests` `input.customerVariables` | object | any | Used for replay only. |
| Embed SDK | `FreyaClient({customerVariables})` | `Record<string,string>` | string | Also `metadata`: "forwarded to the agent session", not template variables **[DOCS]**. |
| Defaults | workflow `test_variables`; agent `config.testVariables` (`Record<string,string>`) | | | See §6. |

i18n `workflows.variables.class.beforeCall.body` describes all of these as one class: "Supplied from outside the agent before the first word, a campaign column, a SIP header, a start-of-call tool, or your test value. If nobody sends it, the placeholder is read out loud exactly as you typed it."

i18n `workflows.variablesPanel.inputTooltip`: "Input variables are provided when starting the workflow (e.g. customer name, phone number). They are available in all nodes."

**Which channel do the graders use?** Not knowable from the spec, docs or GET. Our workspace has 0 personas, 0 simulations, 0 calls, 0 campaigns and 0 phone numbers **[LIVE]**, and no project file or brief names a channel.

The phrase "we run that agent against simulated customers" maps most naturally onto Freya Simulations (personas plus `customerVariablesMap`), or onto an external simulator driving `/chat/message` **[INFERRED]**. Simulation values are strings only, so treat every customer variable as a string.

---

## 4. Names the graders send

- **Known with high confidence:** `customer_id`, `title`, `last_name`. The organisers' base references them in TTS `on_enter` (`{{title}} {{last_name}}`) and in every mock-bank tool body (`customer_id: "{{customer_id}}"`). The base is described as "Minimal two-node collections agent on freya-bank-mock … Base for hackathon builds." **[LIVE GET /workflow/252ee2df-…]**. If the graders did not send these, the base itself could not pass a single case.
- **Unknown:** `now`, and anything else, such as amounts, product, DOB or phone_last4. No source lists more. Money figures should come from `get_account` results anyway.
- **UI behaviour that matters:** the simulation dialog says "Variables detected from the selected agent are pre-listed and remembered per persona". If a grader configures simulations by hand, every extra `{{name}}` our workflow introduces becomes an empty field they may never fill. If they configure by API with a fixed map, extra names are simply never sent.

---

## 5. Resolution rules for unsent variables, and the api_call fill rule [UI]

### Findings (`workflows.variables.finding.*`)

| Key | Text |
|---|---|
| `noProducerPrompt` | "Nothing sets {name}. The agent will read the braces out loud." |
| `noProducerCondition` | "The comparison comes out false every time, with no error." |
| `noProducerToolOnly` | "…this field is nothing but that placeholder, so the field is removed from the request entirely." |
| `noProducerToolMixed` | "The literal braces are sent to your API inside this field." |
| `spacedName` | "Remove the spaces inside the braces. The engine matches the text exactly." So `{{ now }}` ≠ `{{now}}`. |
| `wrongSurface` | "{name} only works in {allowed}. Here it stays as raw text." |
| `noProducerCode` / `rawField` | Code is never substituted; use `vars['name']`. |

### api_call body defaults (`workflows.fieldFate.*` and `workflows.llmSchema.*`)

- **Call-start or saved variable:** "The engine fills it before the call, so the agent never sees or changes it", unless Overridable is on. `llmSchema.hiddenHeading` reads "filled by the system; the model never sees them", with reason `reasonCallInit` = "from call-start variable".
- **Variable produced during the call** ("lifted"): the model sees the parameter. "At call time the saved value replaces the model's answer; if extraction produced nothing, the model's answer is sent as the fallback" (`tableWins`).
- **Unknown name:** "no saved variable with that name exists in this workflow … the agent fills the value instead" (`explainUnknown`). With no description, the model only sees "Template variable: {name}" (`noInstruction`).
- **No-model actions** (on-enter actions, which run without the LLM): an unknown name means the field is not sent (`explainUnknownNoModel`). If the value mixes in other text, the literal `{{…}}` is sent, "a broken payload" (`explainUnknownNoModelMixed`).
- **Per-call values** (`explainPerCall`): "A dynamic system value (like a fresh id or the current date). The engine generates it new on each call."

### Consequences

- (a) **Collision hazard.** If a customer variable, extraction variable or tool-response name equals a tool-arg placeholder (for example a grader-sent `amount` or `date`), the engine overrides or hides the model's argument for `create_promise`. Keep LLM-filled placeholder names distinct from every call-start and extraction name.
  - The base names (`amount`, `date`, `product_id`, `outcome`, `reason`, `field`, `answer`) are presumably safe with respect to the grader, because the organisers chose them alongside their own variables.
  - New names we add must avoid plausible customer-data names: `name`, `phone`, `balance`, `amount_due`, `due_date`, `currency`, `dob` and so on.
- (b) **Whether the hidden/visible split is decided statically or dynamically is unknown.**
  - Static: from the workflow definition's declared inputs.
  - Dynamic: from the variable table at call start.
  - The base declares no inputs (`test_variables: {}`), yet relies on `{{customer_id}}` being engine-filled. That suggests the runtime substitutes from the table at call time whatever the UI classification says **[INFERRED]**.
  - Keep the base pattern (`customer_id: "{{customer_id}}"` as the whole value of the field) and never ask the model for `customer_id`.
- (c) **Tool responses become variables named `<tool>_response.…`.** The base transition uses `verify_identity_response.data.all_verified` **[LIVE]**, and the UI says "one API call can create names you never declared". Gates should test these tool-response variables, never a before-call variable, because a missing before-call variable makes a condition silently false.

---

## 6. `test_variables` / `testVariables`: a refinement

- `workflows.variablesPanel.testValueInfo`: "Test values are only used for web test calls from this page. They are not sent as customer variables, the backend reads them directly from the workflow definition."
- **But** `simulations.customerVariables.description` says: "Optional values passed to the agent under test, **overriding its test variables**." So in simulations, test variables act as defaults that persona variables override **[UI; INFERRED that this also covers workflow `test_variables`]**.
- **Builder rule: keep `test_variables` = `{}`.**
  - A sample `customer_id` or `title` there would silently replace a value the grader forgot to send.
  - A literal `now` there would freeze a stale date and might shadow a platform or grader value.
  - The agent-level `config.testVariables` defaults to `{}` **[LIVE /agents/defaults]**.

---

## 7. Safe builder design (actionable)

1. **Call-start names allow-list:** `{customer_id, title, last_name}`, plus `now` (optional, with a fallback), plus any variable the bank brief explicitly names, spelled exactly as in the brief. Reference nothing else as a call-start variable.
2. **Date block** at the **end** of every node `task_message` that deals with dates. For prefix caching, the UI warns that `prefixCacheWorkflowSystemPrompt` is "{variable} in the workflow system prompt breaks the prefix cache for every node", and that `prefixCacheStaticVarEarly` means "Move it to the end of the prompt". Never put it in TTS `on_enter` or `firstMessage`.
   ```
   Current date/time from the bank: {{now}}
   Platform clock (UTC, ISO 8601): {{system.iso_utc_now}}
   Use the first line if it contains a real date. If it is empty or still shows curly braces, use the platform clock.
   Never say these lines, or any text in curly braces, out loud. Resolve relative dates ("next Friday",
   "end of the month") against this date, and read back the full date (weekday, day, month) before recording.
   ```
   If the brief states a timezone, add `{{system.todays_date_en_est}}` or `_cst` only when it matches; otherwise UTC is the honest default.
3. **TTS / `on_enter` / `firstMessage`:** only `{{title}}` and `{{last_name}}`, as the base does, because unsent values are spoken literally. Never `{{customer_id}}` (an internal id) and never `{{now}}`.
4. **Tool bodies:**
   - Keep `"customer_id": {"type":"string","default":"{{customer_id}}"}` as the entire field value, with no description, so the engine fills it.
   - LLM-filled fields get descriptive defaults (`{{amount}}`, `{{date}}`, …) plus a description, and must not collide with names from rule 1, extraction variables or tool-response roots.
   - In no-model actions (pre_actions, on-enter actions, end-of-call tools), use only call-start variables or fixed values.
5. **Conditions:** gate on tool-response variables (`verify_identity_response.data.all_verified`, `create_promise_response.…`), never on before-call variables.
6. **Static lint in the builder** (a cheap, deterministic node):
   - Extract every `{{…}}` from the generated JSON and classify it as call-start / platform / tool-arg / tool-response / extraction.
   - Reject camelCase `system.isoUtcNow`-style tokens, spaced names, unknown names in TTS or prompt text, and tool-arg ↔ call-start collisions.
   - Assert that `test_variables == {}`.
   - The platform list is: `system.iso_utc_now`, `system.todays_date_tr`, `system.todays_date_en_cst`, `system.todays_date_en_est`, `system.uuid4()`, `call.phone_number`, `call.call_id`, `call.customer_phone_number`, `call.direction`, `call.duration`, `call.retry_number`, `sip_headers`, `attachment.*`.
7. **Local eval harness:** always send `customerVariables = {customer_id, title, last_name, now}`, with `now` as an ISO string, on the first `/chat/message`. Also run a variant **without** `now` to exercise the fallback.

---

## 8. Minimal safe experiment (requires explicit user approval: POST calls, spends credits, mutates workspace)

**Goal:** settle three things in one go:
- (i) whether `{{now}}` is a platform alias;
- (ii) whether the snake_case `system.*` tokens resolve and the camelCase ones do not;
- (iii) whether chat `customerVariables` fill arbitrary names, and whether a sent `now` wins.

**Do not use the base workflow.** `/chat/end` fires its end-of-call `close_call`, which POSTs to the mock bank.

**Cost:** about 2 one-turn chat calls, plus post-call analysis, on the default LLM; cents of the roughly $10 balance. It creates 1 agent and 2 call records.

1. **Create a throwaway regular agent** with no HTTP tools: `POST https://staging-app.freyavoice.ai/api/v2/agents?workspaceId=01e07b69-486a-4070-a7fd-3a2fc3120bf2`.
   - Body: `{"name":"zz-var-probe","config": <live/agents_defaults_en.json → data.result.config>}`, with these overrides:
     - `llmConfig.firstMessageMode = "assistant-speaks-first"`
     - `llmConfig.firstMessage = PROBE`. The first message is substituted by the engine with no LLM paraphrase, so it is the cleanest readout.
     - `llmConfig.systemPrompt = "You are a test probe. Never call tools. When the user says DUMP, reply with exactly this line, character for character: " + PROBE`
     - `llmConfig.tools = [end_call only]`, as in the defaults.
   - PROBE is:
     ```
     A[{{now}}] B[{{system.iso_utc_now}}] C[{{system.isoUtcNow}}] D[{{system.todays_date_en_est}}] E[{{system.todays_date_tr}}] F[{{call.direction}}] G[{{customer_id}}] H[{{probe_var}}]
     ```
2. **Run 1:** `POST /chat/message` with `{"agentId":"<new id>","message":"DUMP","customerVariables":{"customer_id":"probe-1","probe_var":"pv-ok"}}`.
   - Record `response`.
   - Then `GET /call/{callId}?workspaceId=…` and read the first assistant entry in `messages[]`, which is the substituted firstMessage.
   - Then `POST /chat/end {"callId":…}`. The agent has no end-of-call tools, so nothing external is hit.
3. **Run 2:** the same, plus `"now":"2030-01-02T03:04:05Z"` in `customerVariables`, then `/chat/end`.
4. **Optional cleanup** (user's call): `DELETE /agents?agentId=<id>&workspaceId=…`.

**Reading the result:**

| Field | Outcome | Meaning |
|---|---|---|
| A, run 1 | literal `{{now}}` | no alias. Graders must send `now` or we rely on the fallback. |
| A, run 1 | a timestamp | hidden alias exists. Note its format and timezone. |
| A, run 2 | `2030-…` | a customer-sent `now` is substituted and wins. |
| B | ISO timestamp | confirms the fallback token |
| C | literal | confirms the camelCase form is wrong |
| D, E | | the locale formats |
| F | | the `call.direction` value for chat (`web`?) |
| G, H | `probe-1`, `pv-ok` | the chat channel fills arbitrary names |

(Optional run 3: send an empty string, `"now":""`, to see whether empty values substitute as empty or stay literal.)

Even without this experiment, the §7 design is safe under every outcome. The experiment mainly decides whether the fallback line is ever needed, and verifies the snake_case tokens.

---

## 9. Paste-ready question for the organisers

> For grading runs, how are customer variables injected: Freya simulation `customerVariablesMap`, `/chat/message` `customerVariables`, `POST /call` `properties`, or campaign properties? What are the exact keys? We see `customer_id`, `title` and `last_name` in the base. Do you also send `now`, and if so in what format and timezone? Is `{{now}}` a platform variable? The UI only lists `system.iso_utc_now` and `system.todays_date_*`. Are any other keys sent (amounts, product, due date)? Are the dates the mock bank accepts for `create_promise` checked against real time or against your `now`?

---

## 10. Evidence index

| Item | Location |
|---|---|
| UI system-variable lists (staging) | `scratchpad/webapp/js/2zee5lmq0q484.js`, around offset 158000–159800 (`DEFAULT_PROMPT_VARIABLES`, `getDefaultPromptVariables`) |
| Same lists (production) | `scratchpad/research/gap3_dl/prod/2c7wqckyhebjc.js`, same offsets |
| i18n | `scratchpad/research/tools/i18n.json`: `promptVariables`, `workflows.variables.*`, `workflows.fieldFate.*`, `workflows.llmSchema.*`, `workflows.variablesPanel.*`, `simulations.customerVariables.*`, `agents.test.developer.customerVariables.*`, `campaigns.form.*`, `calls.variables.*` |
| Grep helpers | `research/tools/i18n_grep.py`, `research/tools/jsgrep.py`, `research/tools/spec_find.py`, `research/tools/spec_op.py` |
| Spec paths | `/simulations` post `customerVariablesMap`; `/chat/message` post `customerVariables` (+ 422); `/call` post `properties`, `attemptNumber`; `/campaign` post `customers[].properties`; `/unit-tests` post `input.customerVariables`; `/personas*` `simulationVariables`; `/chat/end` post description (end-of-call tools fire); `/chat/fork` post (no variables field); `/agents*` `config.testVariables` |
| Base workflow | `scratchpad/live/workflow_base.json`: tokens `{{title}}` ×2, `{{last_name}}` ×2, `{{customer_id}}` ×4, `{{now}}` ×1, and tool args `{{amount}}`, `{{date}}`, `{{product_id}}`, `{{outcome}}`, `{{reason}}`, `{{field}}`, `{{answer}}`. `test_variables: {}`, `system_prompt: ""`. `close_call.config.alwaysRunsAt: ["end_of_call"]`. |
| Docs | llms-full.txt: line ~2809 ("Use `{{variable}}`"), ~3156 (Embed `customerVariables`), ~3745–3752 (campaign variable columns). No system-variable list anywhere. |
| Authenticated editor chunks | Not reachable: `/workflows*` returns 307 to `/login` (GET, no login attempted). The editor's own classifier code could not be inspected. |
