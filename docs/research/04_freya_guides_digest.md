# 04 - Freya guides digest (for a high-scoring debt-collection voice agent)

Sources read in full: every non-API-reference section of `docs/llms-full.txt` (Getting Started x9, Learning Freyavoice AI x4,
Fundamentals x5, Features x3, Telephony x5, Embed SDK x2, Model API intro/STT/TTS). The docs sitemap
(`https://docs.freyavoice.ai/sitemap.xml`) lists exactly these guide pages, so nothing is missing from llms-full. Pages that the
guides link to but that do not exist (all return 404): `/guides/features/ai-studio`, `/guides/features/simulations`,
`/guides/telephony/phone-number-management` (also probed: feedbacks, problems, solutions, insights, personas, unit-tests,
global-nodes, variables, tools, branches, versions: all 404).

Because the guides are generic, this digest also pulls in, and labels as such:
- **[SPEC]** facts from `docs/openapi_prod.json` (field names, enums, defaults).
- **[BASE]** facts read from the hackathon base workflow `live/workflow_base.pretty.json` and agent `live/agent_base.pretty.json`.
- **[LIVE]** facts from read-only GETs I made: `GET /agents/defaults?language=en` (saved `live/agents_defaults_en.json`) and
  `GET /versions/workflow/{id}/{versionId}` (saved `live/version_base_v1.json`).
- **INFERRED** = my reading; not stated in the docs or spec.

---

## 0. Top takeaways for the builder

1. **The guides do not document the workflow engine.** Nothing in the docs covers `global_nodes`, `transitions`, `on_enter`,
   `task_message`/`role_message`, `extraction_schema` on nodes, `max_turn_transition`, `passthrough` or `return_to_previous_on_interrupt`.
   Everything about them comes from the base workflow JSON plus inference. The workflow node schema is also absent from the OpenAPI spec.
   `GET /workflow/{id}` returns `"workflow": {}` (untyped). Plan to learn by GET-ing working examples and by experimenting.
2. **The workflow config shape is confirmed [LIVE].** A workflow version `configSnapshot` has exactly these keys:
   `nodes`, `tools`, `agent_config`, `global_nodes`, `initial_node`, `system_prompt`, `test_variables`. This matches the `workflow.workflow`
   object of `GET /workflow/{id}`. `POST /versions/{entityType}/{entityId}` "Creates a new version ... the snapshot is validated
   against that entity's config schema and applied to the working copy". That makes it the most likely write path for workflows
   (INFERRED; another agent should confirm).
3. **Language mismatch.** The workflow's embedded `agent_config` uses Freya STT/TTS with `language: "tr"`, voice `leyla` and Turkish
   interruption and backchannel phrase lists. The node prompts say "Speak English". Even `GET /agents/defaults?language=en` returns
   the Turkish Freya stack [LIVE]. The separate agent record, however, has an English stack: Deepgram `nova-3` `en` and Cartesia
   `sonic-3.6` `en`. If simulations run in **voice** mode, the builder should rewrite `agent_config.sttConfig`/`ttsConfig`
   to English, including the English phrase lists. In **chat** mode this matters little. (See section 6.)
4. **The call ends when the agent says the trigger phrase [BASE].** `end_call` has `trigger_pattern: "Have a good day|have a good day"`.
   The tool description says the phrase "ends the call by itself". The agent must say it only at the true close, after the
   needed tool calls have succeeded, and must never use it as small talk.
5. **Money pronunciation hazard.** The default `textSubstitutions` replace `\$` with `dollars`, so "$120" becomes "dollars120" in TTS.
   `speechNormalization.generalNumbers` defaults to `false`, and the `advanced.money/dates/numbers` flags all default to `false` [SPEC].
   The prompt should say amounts and dates in words in a fixed format ("one hundred twenty dollars", "March 5th"). Alternatively
   enable normalization or set `normalizationPrompt`.
6. **Docs-backed tuning:** lower `temperature` for deterministic behaviour ("Lower values (0.1-0.3) produce more focused,
   deterministic responses"). Base is 0.7. Use `maxTokens` to keep replies short, include examples in prompts, and set explicit boundaries.
7. **Iteration loop available via API (no dashboard needed):** `POST /chat/message` (text turn-by-turn against `workflowId`),
   `POST /chat/end` (fires post-call analysis: summary, problems, assertions), simulations and personas, and unit tests with
   routing, extraction and tool-call assertions. Note that every one of these is a POST, which spends credits and is off-limits
   to research agents.

---

## 1. Mental model

### 1.1 Hierarchy (docs + [SPEC]/[BASE])
- **Workspace** is the isolation boundary for agents, workflows, phone numbers, members, settings, integrations and credits.
  Workspace settings `language` options per docs: "English (en), Czech (cs), Spanish (es)". [LIVE] workspace language is `en`.
- **Agent**: "System Prompt ... Model Configuration ... Voice Settings ... Functions ... Context Management" (Core Concepts).
  In the API an agent record has `entityType`. The base agent is `entityType: "workflow"` with `workflowId` pointing at the
  workflow [BASE]. A workflow-type agent is the wrapper that phone numbers, simulations and chat target.
- **Workflow** is "visual, node-based systems that allow you to create sophisticated call routing and processing logic".
  Docs list node types: Agent, Function, Condition, Transfer, Wait, Loop. The JSON actually stored [BASE] uses
  `node_type: "conversation"` for both nodes. The docs' node taxonomy is UI marketing language and does not map 1:1 to JSON (INFERRED).
- **Workflow JSON (`workflow.workflow`) [BASE]:**
  - `initial_node`: node key where execution starts (`"identity"`).
  - `nodes`: map of node-key to node object.
  - `global_nodes`: map, `{}` in base (semantics below are INFERRED).
  - `tools`: **workflow-level tools** (base: `close_call` with `config.alwaysRunsAt: ["end_of_call"]`).
  - `system_prompt`: workflow-level prompt, `""` in base. INFERRED: shared by every node, prepended to node prompts.
  - `agent_config`: the embedded agent config (llmConfig, sttConfig, ttsConfig, dtmfConfig, privacyConfig, complianceConfig,
    piiRedactionConfig, voicemailDetection, callTimeoutSettings, postConversationAnalysis, routingLlmConfig, extractionLlmConfig, ...).
    [SPEC] `PUT /custom/abbreviations` explicitly edits "a workflow's embedded agent config". This is strong evidence that
    **the workflow's `agent_config` (not the agent record's `config`) drives workflow calls** (INFERRED, but well supported).
  - `test_variables`: default variable values for testing.

### 1.2 Node object fields [BASE] with INFERRED meaning
| field | base value | meaning (INFERRED unless noted) |
|---|---|---|
| `name` | node key | display name |
| `node_type` | `"conversation"` | LLM-driven conversational node |
| `role_message` | `""` | persona/system part of the node prompt (Pipecat-Flows `role_messages` analogue) |
| `task_message` | long prompt | the node's task instructions. **This is where the node-level prompt lives.** |
| `on_enter` | `{type:"tts", text:"..."}` | static utterance spoken when the node is entered (supports `{{vars}}`) |
| `on_enter_first_entry_only` | false | only speak `on_enter` the first time the node is entered |
| `tools` | list | node-scoped tools (see 1.3) |
| `transitions` | list | declarative edges (see 2) |
| `pre_actions` | [] | actions run before the node's first LLM turn |
| `extraction_schema` | [] | typed fields to extract (same item shape as tool `extraction_schema` in [SPEC]: `name,type(enum string/integer/float/boolean/date/datetime/enum),description,extraction_prompt,validation_pattern,max_retries,required,overridable`) |
| `response_mapping` | [] | map tool-response paths into variables (`path, variable, description` per [SPEC] tool schema) |
| `assertions` | [] | per-node checks (likely used by simulations/unit tests) |
| `interruptible` | identity `false`, collection `true` | whether the caller can barge in on agent speech in this node |
| `max_node_turns` | 80 | turn cap in node |
| `max_turn_transition` | null | target node when the turn cap is hit |
| `passthrough` / `passthrough_default_transition` | false / null | non-conversational routing node that immediately follows a default edge |
| `return_to_previous_on_interrupt` | false | return to the previous node after this node handles an interruption (fits global nodes) |
| `truncation_strategy` / `truncation_window_size` | `"keep_all"` / 10 | how much history the node's LLM sees |
| `suppress_tool_results` | false | hide tool results from the LLM context |
| `disable_inactivity_monitor` | false | opt the node out of the silence timeout |

Lineage (INFERRED): the platform runs on **Pipecat**. The chat API says "Tells pipecat to fire the end-of-call lifecycle".
The node fields mirror Pipecat Flows (`role_messages`, `task_messages`, `pre_actions`, functions with transitions),
extended with declarative `transitions`.

### 1.3 Tools
- Docs: functions "allow agents to interact with external systems". Best practices: "Keep Functions Focused", "Validate Inputs",
  "Return Structured Data", "Handle Errors Gracefully ... return error information in a consistent format", and
  "Graceful Degradation: ... helpful messages rather than technical errors".
- Three scopes [BASE]:
  1. node `tools` (verify_identity, get_account, create_promise, end_call);
  2. workflow-level `tools` (close_call);
  3. `agent_config.llmConfig.tools` (a generic end_call).
- Tool item shape [SPEC], from `llmConfig.tools[]`:
  - `type:"function"`.
  - `function{name, handler, description, parameters, transition_to}`.
  - `config{maxPerNTurns, alwaysRunsAt[enum each_turn|start_of_call|end_of_call], timeoutMs, maxRetries(0-5), preSpeech{mode static|auto, message, interruptible, audio_asset_id, audio_loop, audio_volume}, interruptible}`.
  - Also `response_mapping[]`, `extraction_schema[]`, `pre_process_code`, `post_process_code`.
- Handlers seen [BASE]: `__function__:api_call` (HTTP call; `url/method/headers/body` are passed as parameters with defaults)
  and `__function__:end_call`.
- `pre_process_code` is Python run on the tool `config` before the call [BASE]. In the base it strips `reason` when
  `outcome in ('promise','paid')`. Note: `'paid'` is **not** in the `outcome` enum. This hints that an "already paid" outcome
  may exist or be tolerated (INFERRED; check the mock/brief).
- `transition_to` on a function [SPEC] lets a tool call move to a node (INFERRED).
- `preSpeech` lets the agent say something like "One moment while I check" during slow tools. It suits get_account/create_promise
  given the 10-12 s timeouts.

### 1.4 Variables
- Syntax `{{variable}}` everywhere: prompts, `on_enter`, tool defaults, `firstMessage`.
  - AI Studio "understands Freya's variable syntax ... Use `{{variable}}` for dynamic values" and preserves them.
  - Campaigns: "If your agent uses **variables** in its system prompt (like `{{customer_name}}` ...), those variables appear as
    columns in the customer table".
  - Embed SDK: `customerVariables`, "Variables injected into the agent's system prompt at runtime".
- Runtime sources of variables [SPEC]: `customerVariables` on `POST /chat/message`, `customerVariablesMap` (per persona) on
  `POST /simulations`, campaign customer properties, and `test_variables`/`testVariables` defaults.
- Base workflow variables: `{{title}}`, `{{last_name}}`, `{{customer_id}}`, `{{now}}` (platform-provided current time,
  INFERRED), plus tool-argument placeholders `{{field}}`, `{{answer}}`, `{{date}}`, `{{amount}}`, `{{product_id}}`, `{{outcome}}`, `{{reason}}`.
  - INFERRED: placeholders for customer variables are filled by the platform.
  - INFERRED: placeholders for arguments the LLM should supply (field/answer/amount/date) are filled from the LLM's tool-call arguments.
- Tool results become variables: transition conditions read `verify_identity_response.data.all_verified` [BASE], so the
  pattern is `<tool_name>_response.<json path>` (INFERRED from the example).
- Docs (Workflows guide): "Input Variables", "Node Outputs", "Global Variables", "Type Safety: Variables have types (string,
  number, boolean, object)".

### 1.5 Node-level vs global prompts
- Global layer: `workflow.system_prompt` (empty in base) and `agent_config.llmConfig.systemPrompt`. In base that field holds the
  generic "You are a helpful assistant ..." default, which is probably unused or overridden in workflow mode (INFERRED; verify
  via a chat test).
- Node layer: `task_message` (+ `role_message`). The base puts **everything** into `task_message`: role, language, turn
  length, the step list, and the closing phrase.
- [SPEC] also offers `llmConfig.postHistoryPrompt` (string, max 200000). INFERRED: a prompt injected **after** the
  conversation history. It is a good place for "never invent figures; one question per turn" recency reminders.
- Recommended split (INFERRED):
  - Shared rules go in `system_prompt`: identity, tone, money rule, abuse/hardship policy, objection taxonomy, closing-phrase rule.
  - The step-specific gate goes in each node's `task_message`.

---

## 2. How transitions are evaluated

Documented (generic only):
- Workflow execution: "Entry Point ... Node Processing ... Data Flow ... Conditional Routing: Conditions determine which path
  to take ... Completion: The workflow completes when reaching an end node or transfer".
- Architecture: the "Condition Evaluator determines routing paths".
- Best practice: "Optimize Condition Logic: Place the most common conditions first to reduce evaluation time". This implies
  ordered evaluation with first match winning (INFERRED).
- "Handle Loops: Be careful with loops to avoid infinite execution."

Observed shape [BASE]:
```json
"transitions": [{
  "condition": {"logic": "or", "conditions": [
     {"variable": "verify_identity_response.data.all_verified", "operator": "equals", "operand": "true", "mode": "exact"},
     {"variable": "verify_identity_response.data.all_verified", "operator": "equals", "operand": "True", "mode": "exact"}]},
  "description": "both security answers verified",
  "target_node": "collection"}]
```
- Comparison is string-based. The base checks both `"true"` and `"True"` (JSON vs Python bool stringification). **Always
  OR both spellings for booleans** (INFERRED from the example).
- `logic` is presumably `"and" | "or"`. Only `operator: "equals"` and `mode: "exact"` are observed; other operators are unknown (open question).
- `description` on a transition plus the existence of `agent_config.routingLlmConfig` [SPEC] suggest that **LLM-evaluated
  transitions** exist (description-only edges judged by a routing LLM). INFERRED; untested.
- Unit-test assertion types [SPEC] confirm routing events exist: `{"type":"routing","signal":"node_entered"|"route_target","target":...,"negate":bool}`.
- No transition leaves `collection`. The call ends via the end_call trigger phrase, and `close_call` runs at `end_of_call`.

---

## 3. Prompt and voice best practices (everything the docs say, plus what the base shows)

### 3.1 Documented
- System prompt is "the most important part of your agent". It defines personality/tone, what the agent can and cannot do,
  how to respond to situations, and "Instructions for handling edge cases".
- "**Be Specific**: Vague prompts lead to unpredictable behavior." "**Provide Examples**: Include examples of good interactions
  in your prompt." "**Set Boundaries**". "Test Iteratively".
- Agent best practices: "Best Practices: Guidelines for the agent to follow, such as always confirming important information or
  asking clarifying questions when needed." This supports a commitment read-back.
- Temperature: "Lower values (0.1-0.3) produce more focused, deterministic responses. Higher values (0.7-1.0) ... creative".
- Max tokens: "Limit the maximum response length ... ensures responses stay concise."
- Context: "Be aware of context window limits"; strategies "append new messages, summarize old ones, or use sliding windows".
  This maps to the node `truncation_strategy` and `llmConfig.maxContextWindow` [SPEC].
- Errors: "Graceful Degradation ... helpful messages rather than technical errors"; "Fallback Strategies: Define fallback
  behaviors when functions fail".
- Outbound: "Ensure your agent's opening message clearly identifies who is calling and why"; "Professional Greeting", "Clear
  Purpose", "Respectful Interaction", "**Opt-Out Option**: Always provide clear opt-out options"; "Respect opt-out requests immediately".
- Inbound: "Quick Greeting: Agents should greet callers quickly to avoid silence."
- Voice content: "Clear Scripts", "Appropriate Tone", "**Pronunciation**: Ensure proper pronunciation of technical terms or
  names", "**Pacing**: Structure content for natural pacing and pauses", "Emphasis: Use voice emphasis to highlight important information".
- Speaking speed: "Slower speeds (0.8-0.9x) for complex information · Normal speeds (1.0x) ... · Faster speeds (1.1-1.2x)
  for simple confirmations". Stability: "Higher stability (0.7-0.9) for consistent, professional delivery · Lower stability
  (0.3-0.5) for more natural". Mapping [BASE]: `ttsConfig.additionalSettings.speed` = 1, `stability` = 0.5, `similarity_boost` = 0.75.
- Knowledgebase prompting example: "search the knowledgebase before responding. Always cite the source document".
- AI Studio tips: "Iterate in small steps. Make one type of improvement at a time"; "Always review AI-generated changes".

### 3.2 Not documented, but shown by the base workflow (house style)
- "Speak English, one or two short sentences per turn." This gives the turn length and implies one question per turn.
- Ask security questions one at a time: "ask for their date of birth, then the last four digits", and "call verify_identity for each answer".
- No disclosure before verification: "You do not know why the bank is calling. If asked, say you can explain after the security check."
- Tool-before-talk: "call get_account, then tell the customer the past due amount". Tool description: "Call it once, before you
  say anything about the account."
- Read-back gate: "read the amount and the date back, and call create_promise after the customer says yes". Tool description:
  "Call it only after the customer said yes to the read-back of amount and date."
- Fixed closing phrase: end with "Have a good day."
- Dates: tool args are `YYYY-MM-DD`, and `{{now}}` is injected so the LLM can resolve "next Friday".

### 3.3 Recommended for numbers, dates and currency (INFERRED; the docs give no rules)
- In speech, write amounts as words plus currency name ("two hundred forty-five euros and fifty cents"). Never write symbols:
  `$` becomes "dollars" before the number, giving the wrong word order.
- In tool calls, keep machine formats (`amount` as a number, `date` as `YYYY-MM-DD`). Speak dates as "Friday, March 6th".
- Only quote figures that appear verbatim in `get_account` results or the brief. Repeat the identical figure in the read-back.
- Alternatively, or in addition, enable `ttsConfig.speechNormalization.advanced.{money,dates,numbers}` or write a
  `ttsConfig.normalizationPrompt` (max 8000) [SPEC]. Effects are untested.

### 3.4 Interruption / turn-taking settings [SPEC defaults; BASE values]
- `ttsConfig.stopSpeakingPlan` (caller barge-in):
  - `numberOfWords` (default 1, 0-10; base 2), `voiceSeconds` (default 0.4, max 0.5; base 0.1), `backOffSeconds` (default 0.4, max 3).
  - `acknowledgementPhrases`: backchannels that should **not** interrupt.
  - `interruptionPhrases`: always interrupt.
  - The base lists are **Turkish**. The agent record has English lists:
    - ack: "yeah, yes, yep, yup, uh-huh, mm-hmm, mhm, hmm, okay, ok, right, sure, got it, i see, of course, alright, exactly, gotcha, makes sense"
    - interrupt: "stop, wait, hold on, hang on, hold up, one moment, one second, just a second, wait wait, excuse me"
- `ttsConfig.startSpeakingPlan.waitSeconds` (default 0.4; base 0.5) and `backgroundNoisePhrases`.
- `llmConfig.disableInterruptionsDuringFirstMessage` (base true). Node `interruptible` (identity false, collection true).
- `llmConfig.firstMessageMode` enum [SPEC]: `assistant-speaks-first-with-model-generated-message | assistant-speaks-first | assistant-waits-for-user`
  (spec default `assistant-waits-for-user`; base `assistant-speaks-first`). Base `firstMessage` equals the identity node's
  `on_enter` text. INFERRED: in a workflow the `on_enter` is what is spoken. Check for a duplicated greeting.
- STT turn detection [BASE]: `sttConfig.additionalSettings.smartTurnEnabled: true`, `krispEnabled`, `deepFilterEnabled`
  (noise suppression), and `turnAnalyzerConfig.provider` enum `local|fal` [SPEC].
- `sttConfig.confidenceReask` [SPEC]: `enabled` (default false), `threshold` 0.3, `reaskMode phrase|llm`, `reaskPhrases`,
  `maxConsecutiveReasks` (default 2), `disableOnDigitNodes`. It re-asks on low STT confidence and could help DOB/last-4 capture in voice mode.

### 3.5 End-call phrasing and call control
- `end_call` [BASE] parameters:
  - `message_mode:"none"`, `interruptible:true`
  - `trigger_pattern:"Have a good day|have a good day"` (regex on the agent's own utterance; INFERRED)
  - `trigger_timeout:3` (seconds after the phrase; INFERRED)
  - `trigger_pattern_both:"goodbye|bye|have a good day|you too|thank you|thanks|take care"` (INFERRED: phrases that close the call when exchanged by both sides)
  - Description: "You normally do NOT call this ... Call it without any text only if the customer answers after your goodbye."
- Risk (INFERRED): an early "thank you"/"bye" exchange could end the call via `trigger_pattern_both`. Keep those words
  out of mid-call agent turns.
- `llmConfig.farewellMessage` and `retryExhaustionMessage` (max 2000) exist [SPEC]; semantics undocumented.
- `callTimeoutSettings` [SPEC]:
  - `silenceTimeout` (base 10 s), `maxRetries` (base 2), `checkInMessage`, `closureMessage`, `silenceTimeoutPrompt`
  - `maximumDuration` (default 600, max 14400)
  - `maxDurationEndCall{messageMode llm|custom|none, spokenMessage, llmPrompt, interruptible, triggerTimeout}`
  - `dynamicWait{enabled, maxWaitSeconds 300, maxRetries, checkInMessage, checkInPrompt, vad*}`. INFERRED: handles "hold on, let me grab my card".
- `voicemailDetection` [SPEC]: `enabled` (default false), `messageMode llm|custom|none|detect_only`, `staticMessage`, `voicemailResponseDelay`.
- `dtmfConfig` [SPEC]: `enabled` (default false), `timeout`, `terminationDigit` (`#`), `prefix:"DTMF: "`. Keypad entry of last-4/DOB is possible in voice mode.

---

## 4. Workflow design patterns (documented) and how they map to collections

Documented patterns (Workflows guide), quoted as diagrams:
- **Simple Routing**: `Start → Agent Node → End`
- **Conditional Routing**: `Start → Condition Node → [True Path] → Agent A / [False Path] → Agent B`
- **Multi-Agent Handoff**: `Start → Agent A (Initial Greeting) → Condition → Agent B (Support) / Agent C (Sales)`
- **Human Escalation**: `Start → Agent → Condition (Needs Human?) → Transfer to Human / Continue with Agent`
- **Function Integration**: `Start → Function (Fetch Customer Data) → Agent (Uses Data) → Function (Update System) → End`
- **Parallel Processing** of independent functions.
- Plus "Error Nodes", "Fallback Paths", and "Keep Workflows Focused", "Minimize Nodes: Each node adds processing time",
  "Start simple with a single agent node, then gradually add complexity", "Complex workflows can be difficult to debug".

**There is no documentation of global nodes.** `global_nodes` is an empty map in the base. INFERRED design based on the field
names (`global_nodes`, node-level `return_to_previous_on_interrupt`, transition `description`, `routingLlmConfig`):
- A global node is reachable from any node when its trigger fires. "Interrupt" handlers return to the previous node.
- Candidate global nodes for this task:
  - `wrong_person` (third party / wrong number): no disclosure; close with `outcome=not_reached`, reason `wrong_number`.
  - `abusive` (warn once, then close politely).
  - `hardship_vulnerable` (empathetic; offer smaller amount/later date only if the brief allows; else no_promise or callback).
  - `callback_request` (schedule a time; outcome per brief).
  - `dispute` and `already_paid` (do not argue; record; close per brief).
  - `opt_out` / `do_not_call`.
- The exact JSON shape for a global node trigger is unknown. Fallback if it cannot be learned: put these branches as rules in
  `system_prompt` and every node's `task_message`. This works without platform features.

Mapping the base gates to required gates:
- **Right-party:** identity node `on_enter` asks "Am I speaking with {{title}} {{last_name}}?".
- **Identity verification:** DOB, then last-4 via `verify_identity`; transition on `all_verified`.
- **Account lookup before speaking figures:** `get_account`.
- **Commitment read-back, then create_promise:** enforced only by prompt and tool description.
- **Tool success before confirming:** not enforced. Add "only say it's recorded if create_promise returned ok"; the mock returns `{"ok":false,"error":...}` on failure.
- **Close:** `close_call` at `end_of_call` records `outcome`/`reason` from the conversation.

`close_call` enums [BASE] (likely graded):
- `outcome`:
  - `promise`: "a payment promise was recorded"
  - `no_promise`: "verified customer, no promise"
  - `not_verified`: "the identity check did not pass"
  - `not_reached`: "the right person was not on the line"
- `reason` per its description: "Leave out for promise. no_promise: refused. not_verified: failed (wrong answers) or declined
  (would not answer). not_reached: wrong_number or unclear." So the reason values are `refused`, `failed`, `declined`,
  `wrong_number`, `unclear` (INFERRED from the wording).

Extraction: [SPEC] `extraction_schema` items support typed fields with `validation_pattern`, `max_retries`, `required`, and an
`extractionLlmConfig`. INFERRED use: extract `promise_amount`/`promise_date`/`objection_type` deterministically and
transition on them (e.g., a read-back node).

Transfer to human: docs mention Transfer Nodes and "Human Escalation", but no transfer node or tool appears in the base or spec.
Shape unknown.

---

## 5. Voice configuration and languages

### 5.1 What is configured where [BASE/SPEC]
- `agent_config.sttConfig`:
  - `provider/model/language`; `language` defaults to `"multi"` [SPEC].
  - `additionalSettings{diarize,punctuate,streaming,aicEnabled,krispEnabled,smartTurnEnabled,webrtcApmEnabled,deepFilterEnabled}`;
    the agent record also has `keyterms:"Freya"` (Deepgram key-term boosting).
  - `confidenceReask`.
- `agent_config.ttsConfig`:
  - `provider/model/voiceId/language` (default `"multi"`)
  - `additionalSettings{speed,stability,similarity_boost}`
  - `startSpeakingPlan`, `stopSpeakingPlan`
  - `textSubstitutions[{pattern(regex, max 500), replacement, caseInsensitive}]`
  - `speechNormalization{emails=true, urls=true, emojis=true, phoneNumbers=false, identityNumbers=false, generalNumbers=false, names=false, advanced{numbers, money, dates, times, percentages, fractions, units, ordinals, abbreviations, iban, symbols — all default false}}`
  - `normalizationPrompt` (max 8000)
- `textSubstitutions` can also be replaced alone via `PUT /custom/abbreviations` ("The full `abbreviations` list is replaced — there is no per-rule patch"; it creates a new version) [SPEC].

### 5.2 Languages
- Workspace languages per docs: en, cs, es.
- Freya's own models are Turkish/Czech-centric:
  - The Freya transcription API language hint is `enum ["tr","cs"]` ("supported: tr, cs") [SPEC].
  - Freya TTS voices are `leyla` (default), `zeynep`, `alev` (female) and `ali`, `alper`, `mustafa` (male). Docs examples are Turkish.
  - The model TTS endpoint supports emotion tags `[pause], [eee], [throat clearing], [mm-hmm], [laughter], [deep breath], [hmm], [cough], [breath]`.
    INFERRED: the in-call `freya-tts` may honour these too; untested. Do not rely on it.
- Third-party stacks are evidently supported: the agent record uses `deepgram`/`nova-3`/`en` and `cartesia`/`sonic-3.6`/`en` with voiceId `9287676d-f0cc-423f-ac03-3b3c7242f091`.
- Docs: "Language Routing: Route to agents that speak the caller's language"; "Language Switching: Support language switching during conversations" (no mechanism described).

### 5.3 Implications of `tts/stt language "tr"` + "Speak English" (flag)
- **Voice-mode simulations:**
  - Turkish STT on English speech risks mis-transcribing DOB digits, last-4 and names, so identity checks fail. It also triggers wrong objection classification.
  - A Turkish voice reading English text risks a heavy accent and Turkish-style number reading. This matters most for amounts/dates, since `generalNumbers` is off.
  - The Turkish `interruptionPhrases`/`acknowledgementPhrases` will not match English backchannels. "okay" is in both lists, but "yeah/right/sure" are missing.
  - Recommendation (INFERRED): have the builder set the workflow's `agent_config.sttConfig` to `deepgram/nova-3/en` and `ttsConfig` to
    `cartesia/sonic-3.6/en` with the English phrase lists, copied from the agent record values that the platform itself created.
    Verify with one voice simulation, because staging credentials for those providers are unconfirmed.
- **Chat-mode simulations / `POST /chat/message`:** STT/TTS are bypassed (`modality: "text"` exists on calls [SPEC]), so the
  mismatch is mostly harmless. Some TTS normalization or substitution may still run on text output; unverified.
- The base keeps `"tr"` even though the hackathon prompts are English. It is possible the graders run chat/text simulations.
  This is an **open question to settle first.**

---

## 6. Compliance, collections, banking, privacy

What the docs actually say (no collections-specific guidance exists):
- Campaign use cases include "payment follow-ups"; example campaign name "April Payment Reminders".
- Outbound compliance: "TCPA Compliance (US) - Obtain consent before calling", "GDPR Compliance (EU)", "Do Not Call Lists -
  Maintain and respect DNC lists", "Caller ID", "Opt-out - Provide clear opt-out mechanisms", "Ensure compliance with local
  calling regulations (TCPA, GDPR, etc.)", "Respect opt-out requests immediately and maintain a do-not-call list",
  "Avoid calling outside business hours / Consider time zones".
- Telephony overview: "**Recording Consent**: Obtain consent before recording calls (where required)".
- Architecture: "PII Handling: Personal information is handled according to privacy regulations", "Audit Logging".

Config knobs [SPEC; BASE values]:
- `privacyConfig{enableRecording (base true), getRecordingConsent (false), recordingConsentText (max 500), stopSecs, preSpeechMs, maxDurationSecs}`.
  INFERRED: with `getRecordingConsent:true` the platform plays/asks the consent text at call start. That adds a turn before the
  right-party check, so only enable it if the brief requires it.
- `piiRedactionConfig{enabled:false, maskEmail, maskPhone, maskPaymentCard, maskNationalId, maskIban (all true), customRules[{label, pattern, enabled}]}`.
  INFERRED: redacts transcripts/logs. Enabling it could mask DOB/last-4 in transcripts graders read; leave off unless the brief asks.
- `complianceConfig{pciDss, mifidIi, euAiAct, soc2}`: all false, behaviour undocumented.
- `guardrailsConfig`:
  - `guardrails_enabled`, `custom_rules_enabled`
  - `rules[{type exact|regex|dictionary|structured|classifier, pattern, action refuse|regenerate|log_only, case_sensitive, classifier_threshold}]`
  - `refuse_message` (default "I'm sorry, I can't help with that."), `max_regenerate_retries` (default 2),
    `regenerate_wait_message` ("One moment, please."), classifier settings
  - `input_config{detect_injection, detect_weird_input, normalize, max_input_chars, ...}`
  - INFERRED use: a `regenerate` regex rule against forbidden content (e.g., threats, legal-action language), or as a
    safety net against prompt-injection from "customers". Risky: a refusal message mid-call could hurt scores. Test before use.
- Suppression list (`/suppression-list`, DNC) blocks outbound dialing. Not relevant to simulations.

Collections domain practices (industry knowledge, **not in Freya docs**, INFERRED):
- No debt disclosure before right-party plus verification. The base already does this.
- Identify the caller and purpose after verification.
- Don't threaten, don't argue disputes; record them.
- Offer options in hardship and avoid pressure on vulnerable customers.
- Honour "stop calling"/opt-out.
- Never invent balances, fees or settlement offers. Quote only tool/brief figures.
- Read back promise amount and date.
- Confirm only after the tool succeeded.

---

## 7. Knowledgebase, analytics/insights, campaigns (relevant bits)

- **Knowledgebase** (beta):
  - Workspace-level library queried at call time via hybrid search, returning results "in under 200ms".
  - "available to all agents in the same workspace automatically. No per-agent configuration is needed".
  - Content types: Articles (PDF/TXT/MD/DOCX up to 10 MB, or synced web pages) and Snippets (recommended format `# Title` /
    `## Question` / answer).
  - **No KB endpoints exist in the API spec** (only `kb:*` permissions), so the builder cannot populate it programmatically.
    Put brief facts directly into prompts instead. Also, the KB is shared workspace-wide, so stale content could leak into every agent.
- **Analytics:**
  - Calls tab: Total Calls with caller-sentiment breakdown, Average Duration, Total Talk Time, a status pie (Connected,
    Abandoned, Failed, Voicemail), and a top-15 Call Themes treemap.
  - Structured Data tab: widgets over **Insights**.
- **Insights** [SPEC `POST /insights`]: structured fields with `type string|number|rating|checkbox|range|enum|array`,
  `source analysis|tool_field` (+ `toolName`, `toolFieldPath`). They are assigned to agents via `PUT /agents/{id}/insights` or
  `postConversationAnalysis.insights`. Useful for tracking promise rate or objection type across test runs.
- **postConversationAnalysis** [SPEC]: `summaryPrompt`, `autoDetectProblems` (base false), `problemDetectionPrompt`,
  `problemDetectionInstructions`, `analysisModel`, `insights[]`. Turning on `autoDetectProblems` with a collections-specific
  `problemDetectionPrompt` gives automatic per-call QA. Note that it costs analysis credits.
- **Campaigns:**
  - Customers in E.164; prompt variables become columns; retries 0-10 (default 0) with a 300 s default delay.
  - The outcome webhook has failure categories `no_answer|busy|voicemail|timeout|api_error|llm_fail|other`; `llm_fail`
    applies "if successPrompt configured", so a campaign can have an LLM success judge.
  - Not needed for simulation scoring.

---

## 8. Iteration features (AI Studio, Simulations, Feedbacks/Problems/Solutions, Unit tests, Versions)

- **AI Studio** (guide "Iterate Your Agents"):
  - UI-only split-screen prompt editor. You chat requests ("Add instructions for handling angry customers"), it shows
    evaluation/planning/execution "thinking", and it preserves `{{variables}}`.
  - **No API**, so the builder cannot use it. Humans could use it to brainstorm prompt text.
- **Simulations** [SPEC]:
  - `POST /simulations{name, agentId, selectedPersonaIds(required), mode voice|chat (default voice), assertions[uuid], customerVariablesMap{personaId:{var:value}}, runsPerPersona(1-50), maxConcurrentCalls, maxCallDurationSeconds(60-3600), maxRetries, *LlmOverride, agentVersionId, start(bool; false = draft)}`.
  - Personas: `POST /personas{personas:[{name, prompt, successCriteria, systemPrompt, evaluationPrompt, tags, agentIds}]}`.
  - Results: `GET /simulations/version-checks` and `GET /simulations/version-persona-runs` (pass/fail per version).
  - Guides say only "Test in Simulation", "Test Edge Cases", "Test with Real Data", "Automate Testing".
  - INFERRED: the hackathon graders likely use this exact machinery (personas + successCriteria/evaluationPrompt), so
    writing our own personas for the objection taxonomy mirrors grading.
- **Chat API** [SPEC]:
  - `POST /chat/message{message, agentId|workflowId, callId, customerVariables, versionId, fromMessageId}` drives the
    workflow turn-by-turn over text. `fromMessageId` branches from an earlier assistant turn.
  - `POST /chat/fork` retargets to another `versionId`.
  - `POST /chat/end` fires end-of-call tools (i.e., `close_call`) and analysis.
  - This is the cheapest deterministic regression loop. Careful: end-of-call tools hit the mock bank's `close_call`, which may hold grading state.
- **Unit tests** [SPEC]:
  - `POST /unit-tests{workspaceId, entityType agent|workflow, entityId, input{schemaVersion:1, callType web|voice, messages[...], customerVariables}, expectation, assertions[...], judgeContext{includeRoutingEvents, includeExtractionEvents, includeToolCalls, includeOtherEvents}, seedCallId, seedMessageId}`.
  - Assertion types:
    - `routing{signal node_entered|route_target, target, negate}`
    - `extraction{name, match exists|equals|contains|regex|absent, value, minConfidence}`
    - `tool_call{tool, args, argsMatch subset|exact, negate}`
  - `POST /unit-tests/run-all`. These are ideal gate tests, e.g. "create_promise NOT called before read-back confirmed"
    (`negate:true`), or "collection node not entered when verification fails".
- **Feedbacks / Problems / Solutions** [SPEC]:
  - Feedback `{title, description, proposedSolution, type enum workflow|decision-llm|stt|tts|vad-nc|latency|platform|ui|general|system|audio|transcription|decision, severity 0-1 (default 0.5), status, callId, agentId, messageId}`.
  - Calls expose `problems[]` (same shape) and `unresolvedAiFeedbacksCount`. INFERRED: "problems" are AI-generated
    feedbacks from `autoDetectProblems`.
  - "Solutions" appear as `openSolutions[{branchId, name, entityType, entityId}]` and `solutionCommits`. There are permissions
    `solutions:generate|accept`, and a version field `sourceRunId` ("Optimization run / proposal that minted this version").
    INFERRED: the platform can auto-propose fixes as **branches** that are merged with `POST /branches/{id}/rebase` (summary "Merge branch").
  - No solution-generation endpoint is in the public spec.
- **Reviews**: statuses `No Review Status | Pending Review | In Progress | Completed | Not Needed`; tags
  `Successful | Technical Issue | Wrong Number | No Answer | Needs Review`.
- **Versions/branches:**
  - Docs: "Version Your Configurations", "Tag Releases", "Have Rollback Plan".
  - [SPEC] versions have `isPublished`. Branch merge can "Apply" (draft) or "Apply & Publish".
  - INFERRED: make sure the version the graders run is the **published** one.
- **Assertions:** simulation `assertions` are UUIDs of assertion entities (`assertions:*` permissions). The spec has no
  `/assertions` endpoints, so creating them may be UI-only (open question).

---

## 9. Embed SDK and Model APIs (low relevance)

- **Embed SDK:**
  - The backend mints a token via `POST /api/token/embed{agentId, allowedOrigin, ttlSeconds (default 300)}` using a key with `calls:web-token`.
  - The browser uses `new window.Freya.FreyaClient({baseUrl, token, mode:"call"|"chat", metadata, customerVariables, iceServers})`.
  - Events: `stateChanged`, `botTranscript`, `userTranscript{final}`, `botLlmText`, `toolCallStarted/InProgress/Stopped`, etc.
  - Tokens are single-use. Could serve as a manual demo UI; not needed for the builder.
- **Model APIs:**
  - `stt.freyavoice.ai/v1/audio/transcriptions` (OpenAI-compatible; wav/mp3/flac/ogg/m4a/webm up to 15 MB; `model freya-stt`).
  - WebSocket streaming beta.
  - `tts.freyavoice.ai/v1/audio/speech` (voices listed in 5.2).
  - Standalone and independent of the Call API.

---

## 10. Open questions (to resolve by experiment or organizers)
1. Do graders run simulations in `voice` or `chat` mode? This decides whether the tr/en STT/TTS fix (section 5.3) is critical.
2. Is the workflow's `agent_config` definitely what runs (vs the agent record `config`)? Do `agent_config.llmConfig.systemPrompt`/`firstMessage` apply in workflow mode?
3. What is the exact JSON shape of a `global_nodes` entry (trigger condition, description-based LLM trigger, return behaviour)?
4. Which transition operators and modes exist beyond `equals`/`exact`? How do LLM-judged transitions (`description` + `routingLlmConfig`) work? When are transitions evaluated (after each user turn, after each tool result)?
5. Exact semantics of `end_call` `trigger_pattern_both` and `trigger_timeout`, and whether a premature "thank you ... bye" can end calls.
6. Which LLM `provider/model` values are valid on staging? Base uses `experimental-5`; the agent record uses `openai/gpt-4o`.
7. Does the mock bank / grader accept a `paid` outcome (hinted by `close_call.pre_process_code`), and what are the exact `reason` values?
8. Does `getRecordingConsent` inject an extra turn? Does PII redaction affect what graders see?
9. Are `{{now}}` and other system variables documented anywhere? Only `{{now}}` is observed.
10. How are simulation `assertions` (UUIDs) created, given there is no `/assertions` endpoint in the spec?

## 11. Risks
- Building on undocumented workflow fields means behaviour must be validated empirically, and each test costs credits (about $10 balance).
- A premature closing phrase ends calls; workflow-level `close_call` then records an outcome from an incomplete conversation.
- Currency symbol substitution plus disabled number normalization can produce wrong spoken amounts in voice mode.
- The Turkish STT/TTS stack under English prompts can break identity verification in voice mode.
- Chat/simulation test runs fire `close_call` against the shared mock bank, which may pollute grading state.
- The KB cannot be written via the API and is workspace-global. Don't depend on it.
- Guardrail refusals or regenerations, or PII masking, could change transcripts in ways graders penalise.
