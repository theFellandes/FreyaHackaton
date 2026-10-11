# 02 — Freya testing & evaluation reference (Chat API, Personas, Simulations, Unit tests, Versions)

> Status: pre-kit research note (10 October 2026), kept for background. Where it disagrees with the kit ([research/11](11_hackathon_rules_and_kit.md), `docs/api.md`, `docs/workflow-format.md`) or with the code, those win.

Scope: everything needed to evaluate a Freya agent programmatically for the bank-collections hackathon builder.
Sources, in order of authority:
1. `docs/openapi_staging.json` (OpenAPI 3.1, 96 paths). This is the spec for the host our key works on. It matches `openapi_prod.json` for every testing endpoint except trivial `include=` preview additions on `GET /simulations/{id}`.
2. `docs/llms-full.txt`: the guides. They say very little about testing; see section 10.
3. The dashboard's own i18n string table (UI copy), recovered from the Next.js HTML that the `/workspaces/{id}/credits` probe returned. Saved as `research/tools/i18n.json` and cited below as **[UI]**. It is useful for semantics the spec does not state (judge behaviour, run outcomes, pricing hints).
4. Frontend route constants in `webapp/js/38t45boimpwp0.js`, cited as **[FE]**. They name endpoints that are missing from the OpenAPI spec.
5. Live GET probes against staging (GET only, no POST/PUT/PATCH/DELETE), cited as **[LIVE]**.

Anything not stated by a source above is marked **INFERRED**.

---

## 0. TL;DR

* **Fastest loop: `POST /chat/message`.** It drives the real agent or workflow turn by turn over text. A turn returns `{response, callId, messageId, warning?, endedByAgent?}`. The response carries **no tool calls or node transitions**. Read those afterwards with `GET /call/{callId}` (`messages`, `artifact`, `analysis`, `assertionResults`) once the call has been closed with `POST /chat/end`.
* **Chat runs the real runtime (pipecat), so `api_call` tools really fire** against `freya-bank-mock`, including the end-of-call `close_call` tool when the call ends. Evidence: `/chat/end` "fire[s] the end-of-call lifecycle (webhook + post-conversation analysis + end-of-call tools)", and [UI] `unitTests.editor.realToolNote` says "Tool rules observe real tool calls; results are not mocked." Only the unit-test runner has an explicit `mockToolResults` switch.
* **Cost.** [UI] `workspace.billing.minutesHint` says "1 credit = 1 phone minute. Web calls, chat and simulations are free." That copy belongs to minute-contract plans. Our workspace runs on a USD balance (about $10), and calls still record `cost` in USD. Unit tests use credits ([UI] "Uses about {count}× the credits"). Measure before scaling up (section 11).
* **Native simulations** (`POST /simulations`):
  * Inputs: personas, natural-language assertions, and per-persona `customerVariablesMap` (customer_id, title, last_name).
  * Modes: `"voice"` (default) or `"chat"`.
  * Execution: asynchronous. You poll status and iterations; an LLM judge grades each run as pass, fail, inconclusive or error.
  * This is very probably what the graders use (INFERRED, section 13).
* **Unit tests** (`POST /unit-tests` + `POST /unit-tests/run-all`) grade **one agent turn** given a snapshot that ends with a user message. The judge uses an `expectation` plus deterministic rules (`routing`, `extraction`, `tool_call`). They suit gate checks such as "no `create_promise` before read-back yes", and `mockToolResults` can keep them off the mock bank.
* **Version targeting:**
  * chat: `versionId`
  * fork: `versionId`
  * simulations: `agentVersionId`
  * unit tests: `entityVersionId` (on run-all and when reading results)
  * per-version roll-ups: `GET /simulations/version-checks` and `GET /unit-tests/version-runs` (both take `entityVersionId`)
* **Recommended setup (section 12):**
  * A primary local loop of our own LLM customer personas driving `/chat/message`, with our own judge reading `GET /call/{callId}`.
  * A small Freya unit-test pack for the gates.
  * A final gate of a native **voice** simulation. Chat cannot catch STT/TTS problems, and the base workflow ships with Turkish STT/TTS while its prompts are English (section 14).

---

## 1. Conventions

| Item | Value |
|---|---|
| Base URL (our key works only here) | `https://staging-app.freyavoice.ai/api/v2` |
| Auth | `Authorization: Bearer <FREYA_KEY>` (`bearerAuth`, http bearer) |
| Envelope (success) | `{"success": true, "data": {"message": "Success", "result": {...}}}` |
| Envelope (error) | `{"success": false, "data": {"message": "<text>", "result": null \| {"error": "<text>"}}}` |
| `workspaceId` | Query param on most GET/list endpoints; a **body** field on `POST /unit-tests` and `POST /unit-tests/run-all`. **Absent** on `/chat/*`, `POST /personas`, `POST /simulations`, `/simulations/{id}/start\|rerun`, `GET /personas/{id}`, `GET /simulations/{id}`. Those resolve the workspace from the key; 403 means "Workspace ID mismatch or insufficient permissions". |
| Our workspace | `01e07b69-486a-4070-a7fd-3a2fc3120bf2` |
| Base workflow id | `252ee2df-4066-4b87-8fe6-bfecbab03505` (versions: entityType `workflow`, entityId = this id; v1 = `171f4844-dd70-4dc0-8924-5a8fd0ff2d2b`, `isPublished: true`) |
| Wrapper agent for that workflow | `2f8dcbea-fad5-4c09-ae0b-35cff67e9a14` (`entityType: "workflow"`, `workflowId` = above). Simulations take `agentId`, so pass this wrapper id for workflow agents (INFERRED; `GET /call` exposes `assistantId` + `assistantEntityType: "workflow"` + `assistantWorkflowId`). |
| Undocumented APIs reachable with the API key [LIVE] | `GET /assertions` (200), `GET /agents/{agentId}/assertions` (200, `{assertionIds: []}`), `GET /simulations/{id}/iterations/{iterationId}` (JSON 404 "Iteration not found" for a dummy id, so the route exists), `GET /simulations/{id}/persona-trends` (route exists), `GET /call/{id}/cost-events` (route exists), `GET /unit-tests/stream` (200 `text/event-stream`, first event `{"type":"connected",...}`). `GET /call/{id}/logs` returned **403 Forbidden** for a dummy id: it may need a permission the key lacks, or it may just be how not-found is reported. `GET /simulation-runs` returned an HTML 404, so that route is not deployed. `GET /api-keys` → "Authentication method 'api_key' is not allowed for this route". |

Relevant permission ids (from `GET /permissions`) [LIVE]:
* `calls:chat-message`: "Send chat messages: Push chat messages into an active call."
* `chat:read`: "Use chat preview: Use chat-style previews of agents."
* `agents:test`: "Run TTS and behavioural tests on agents."
* `workflows:test`: "Run test calls and API requests against workflows."
* `personas:read|write|delete`
* `simulations:read|write|delete|execute` (execute = "Trigger, rerun, retry, or cancel simulation runs")
* `simulations:verdict-callback`: "Internal: voice-agent posts a unit-test/simulation verdict."
* `assertions:read|write|delete|assign`
* `versions:read|publish`
* `calls:read`
* `calls:web-token`: needed for Embed SDK tokens.

We could not list our key's own permissions (`/api-keys` refuses API-key auth).

---

## 2. Chat API (text, turn by turn)

### 2.1 `POST /chat/message` (operationId `postChatMessage`)
Spec description: "Drives a Freya agent or workflow turn-by-turn over text. Omit `callId` and provide `agentId` or `workflowId` to start a new call; the response returns a `callId` to pass on subsequent turns. Branch by passing `fromMessageId` pointing at an earlier assistant turn."

Request body (`application/json`). Only `message` is required.
```ts
{
  callId?: string(uuid)          // omit on the first turn
  agentId?: string(uuid)         // start a call against an agent (or a workflow's wrapper agent)
  workflowId?: string(uuid)      // ...or directly against a workflow
  message: string                // minLength 1, maxLength 10000  (REQUIRED even on the first turn)
  customerVariables?: object     // additionalProperties: {}  -> fills {{customer_id}}, {{title}}, {{last_name}}, ...
  versionId?: string(uuid)       // pin a specific agent/workflow version (else current working copy / head — INFERRED)
  fromMessageId?: string         // rewind/branch from an earlier ASSISTANT turn of this call
}
```
Response 200 `data.result`:
```ts
{ response: string, callId: string(uuid), messageId: string, warning?: string, endedByAgent?: boolean }
```
Errors:
* 400 Validation error
* 401 Missing or invalid token
* 403 Workspace ID mismatch or insufficient permissions
* 404 Call not found
* **409 Call has no resume anchor (no prior assistant turn)**: raised by `fromMessageId` misuse
* **422 customerVariables too large**
* 500 Server error
* 502 Agent service returned no response
* 503 Agent service not configured
* 504 Agent service timed out

Notes:
* **First turn.** `message` is required, so the first request must carry text such as `"Hello?"` or `"Yes, speaking."`. The base workflow uses `firstMessageMode: "assistant-speaks-first"`, and its identity node has `on_enter` TTS `"Hello, this is the digital assistant of Freya Bank. Am I speaking with {{title}} {{last_name}}?"`. Whether the first `response` contains only that greeting, or the greeting plus a reply to our first message, is **UNVERIFIED**. Verify it live (section 15). A safe opener is a neutral first user message such as `"Hello?"`, which is what a callee says on pickup.
* **Variables.** `customerVariables` is the runtime counterpart of the Embed SDK's `customerVariables` ("Variables injected into the agent's system prompt at runtime"). In simulations, [UI] says per-persona variables "overrid[e] its test variables". Agent config has `testVariables: Record<string,string>` and workflows have `test_variables: {}`; these are presumably the fallback values used in tests (INFERRED). Pass at least `customer_id`, `title`, `last_name`. The base collection node also uses `{{now}}`, which is **not** in the documented built-in variable list (built-ins: `call.*`, `system.todaysDateTr`, `system.isoUtcNow`, `system.uuid4`), so pass `now` explicitly in tests unless the builder replaces it (INFERRED).
* **`endedByAgent: true`.** The agent ended the call, either through the `end_call` tool or the `trigger_pattern` "Have a good day". Stop sending turns and still call `/chat/end` to force the end-of-call lifecycle; it is idempotent (INFERRED).
* **Tool calls and node transitions are not in the chat response.** Get them from `GET /call/{callId}` (section 3). [UI] says the full transcript "includ[es] API calls, node transitions, variable extractions, and system events".
* **Rewind.** `fromMessageId` = an earlier **assistant** `messageId` re-drives from that anchor inside the same call. The dashboard calls this "Restart here" ([UI] `agents.test.transcript.resumeConversationHere`).
* **Real tools.** `api_call` tools run against the configured URL (`freya-bank-mock`). Every chat test therefore performs real `verify_identity`, `get_account`, `create_promise` and `close_call` POSTs (INFERRED from runtime design plus the evidence in section 0).

### 2.2 `POST /chat/fork` (operationId `postChatFork`)
Spec description: "Creates a brand-new chat call that copies the parent call's messages up to and including `fromMessageId` and retargets a different `versionId`, `agentId`, or `workflowId`. The forked call is dormant until the caller drives it via POST /chat/message. Cross-entity forks are allowed; the inherited workflowSnapshot may be partially incompatible with the new target (pipecat's resume path degrades gracefully)."

```ts
// body (required: parentCallId, fromMessageId)
{ parentCallId: uuid, fromMessageId: string(minLength 1), versionId?: uuid, agentId?: uuid, workflowId?: uuid }
// 200 data.result
{ callId: uuid, messageId: string, parentCallId: uuid, parentAnchorMessageId: string, entityVersionId: uuid | null }
```
Errors: 404 "Parent call, anchor message, or target version not found"; 422 "Parent has no agent or workflow to inherit".

**Testing use: A/B regression from a shared prefix.** Take a golden conversation prefix (for example, the customer has just verified identity) and fork it onto a new `versionId`. Then replay only the critical tail. This saves the identity turns on every scenario and keeps the prefix identical across versions. Caveat: the node state is restored from the inherited `workflowSnapshot`, which may not match a restructured graph.

### 2.3 `POST /chat/end` (operationId `postChatEnd`)
Spec description: "Closes a chat call. Tells pipecat to fire the end-of-call lifecycle (webhook + post-conversation analysis + end-of-call tools) — analysis runs async on pipecat; the webhook arrives at the dashboard shortly after with summary, sentiment, themes, problems, and assertions. Idempotent: a second call on an already-ended call returns 200 with `alreadyEnded: true` without re-triggering pipecat."
```ts
// body
{ callId: uuid }          // required
// 200 data.result
{ callId: uuid, ended: true, endedReason: string, alreadyEnded?: boolean }
```
Errors: 404 Call not found, 503, 504.

What it triggers:
1. End-of-call tools: tools with `config.alwaysRunsAt: ["end_of_call"]`. In the base workflow that is the global `close_call` tool, which POSTs `{outcome, reason?, customer_id}` to the mock bank. Its `pre_process_code` drops `reason` when outcome is `promise`/`paid`.
2. Post-conversation analysis: summary, sentiment, themes, problems if `autoDetectProblems`, assertions, success criteria. `postConversationAnalysis.analysisModel` is "The LLM model used for post-conversation analysis, assertions, and success criteria evaluation" [UI].
3. The end-of-call webhook (section 3.3).

These run asynchronously, so poll `GET /call/{callId}` until `summary`, `assertionResults` or `analysis` are populated.

**Always call `/chat/end` at the end of every local test conversation.** Otherwise `close_call` never fires, and close_call correctness is very likely graded (section 13).

### 2.4 Other text channels (not recommended for the loop)
* `POST /inbound/web` registers an inbound web call. The body includes `simulationId?` and `modality?: "voice"|"text"`; the response type enum is `"webCall" | "simulationCall"`. This shows that simulations run as web calls with a voice or text modality (INFERRED). It is a machine-facing endpoint.
* Embed SDK (`guides/embed`): `FreyaClient({mode: 'chat'})` over WebRTC with `customerVariables`. It emits `toolCallStarted`, `toolCallInProgress` (`function_name`, `arguments`, `tool_call_id`) and `toolCallStopped` in real time. The token comes from `POST /api/token/embed` (needs `calls:web-token`). This is the only documented way to see tool calls live during a conversation, but driving WebRTC from Python is heavy.

---

## 3. Reading results of any call (chat, simulation, voice)

### 3.1 `GET /call/{callId}?workspaceId=...` (operationId `getCall`)
`data.result.call` fields useful for evaluation:
```ts
{
  id, assistantId?, assistantName?, assistantEntityType?: "workflow"|null, assistantWorkflowId?,
  simulationId?: uuid|null,                       // set for simulation runs
  startedAt?, endedAt?, duration?, status?, type?, modality?: "voice"|"text"|null, channel?: "whatsapp"|"api"|null,
  endedReason?: string|null,
  transcript?: string|null,
  messages?: Array<{ role: string, message?: string, duration?, secondsFromStart?, timestamp?, endTime?, messageId?,
                     wasBlocked?/was_blocked?, blockedContent?/blocked_content? }> | null,
  summary?: string|null, cost?: number|null,       // cost is USD ([UI] "Cost attributed to the call in USD")
  turnLatencyStats?: { p50, p75, p90?, p95, p99, avg?, count? } | null,
  tags?: Array<{ tag, source, value?, confidence?, metadata? }>,
  assertionsSummary?: { passed: number, failed: number, total: number } | null,
  assertionResults?: Array<AssertionResult>,
  successCriteriaResult?: AssertionResult,          // persona successCriteria verdict for simulation runs
  unitTestsCount?: number,
  insights?: object, analysis?: any, artifact?: any,   // untyped in spec; tool calls / node events likely live here or in messages (INFERRED)
  problems?: Array<{ title, description?, type?: "workflow"|"decision-llm"|"stt"|"tts"|"vad-nc"|"latency"|"platform"|"ui"|"general"|null, severity?, messageId?, ... }>,
  entityVersionId?: uuid|null, entityVersionNumber?: number|null,   // which version handled the call
  piiRedactions?: Array<{ token, label, value? }>
}
AssertionResult = { id?, name?, assertion?, success?: boolean|null, reason?: string|null,
                    relevantMessages?: Record<string,string>, timeTaken?: number|null, metadata?: object }
```
`success: null` corresponds to "inconclusive" / "N/A" in the UI ([UI] `assertions.card.na`, `assertions.callDetails.inconclusive`).

The spec types `messages[].role` only as `string`. Expect `assistant`/`user` plus tool and system roles (INFERRED; roles are not enumerated). **Verify live and adapt the parser.**

### 3.2 `GET /call` (list)
Query parameters:
* `workspaceId` (required)
* `limit` (default "20"), `offset`
* `sortBy` ∈ `startedAt|duration|sentiment|relativeTime|cost`, `sortOrder`
* `callIds` (comma-separated), `startDate`, `endDate`, `searchTerm`, `status`, `type`, `endedReason`
* `filters` (Base64-encoded filter state)
* `insights`
* `additionalData` (comma-separated extras: `transcript`, `cxInsights`)
* `include` (`summary`, `reviewNotes`)
* `semanticQuery`, `archivedFilter`, `export*`

There is **no `simulationId` filter param**. To find simulation run calls, use `GET /simulations/{id}/iterations/{iterationId}` (undocumented, below) or filter rows client-side on `simulationId` (INFERRED).

Undocumented [FE]:
* `GET /call/{callId}/logs`: 403 with our key on a dummy id
* `GET /call/{callId}/cost-events`
* `GET /call/{callId}/metrics/tables`

### 3.3 End-of-call webhook (`POST /webhooks/end-of-call-report`, documented as outbound payload)
```ts
{ type: "end-of-call-report", callId: uuid, workspaceId: uuid, summary?: string, endedReason?: string /* e.g. 'customer-ended-call','assistant-ended-call' */,
  durationMs: number, transcript?: string, status: string, callEndReasonCategory: string, customer: string,
  startedAt: date-time, endedAt: date-time, recordingUrl: uri, timestamp: date-time }
```
We cannot receive it locally without a public URL. Polling `GET /call/{callId}` is simpler.

---

## 4. Personas

### 4.1 `POST /personas` (operationId `createPersona`): bulk create, 201
```ts
{ personas: Array<{                     // minItems 1, maxItems 200
    name: string,                       // REQUIRED, minLength 1
    prompt: string,                     // REQUIRED, 1..100000 chars — who the simulated customer is + scenario
    successCriteria?: string,           // <=100000 — graded per run -> call.successCriteriaResult
    systemPrompt?: string,              // <=100000 — overrides the persona-bot system prompt (INFERRED)
    evaluationPrompt?: string,          // <=100000 — custom grading prompt (INFERRED; UI labels "Evaluation Prompt")
    tags?: string[],
    agentIds?: uuid[]                   // agents this persona is linked to
}> }
```
201 `data.result.personas[]`:
* `id`, `workspaceId`, `name`
* `prompt|null`, `successCriteria|null`, `systemPrompt|null`, `evaluationPrompt|null`
* `tags[]`, `agentIds[]`
* `simulationVariables` (`Record<agentId, Record<string,string>>`, default `{}`): "Most-recently-used simulation variable values, keyed by simulated agentId"
* `versionNumber` (personas are versioned)
* `createdAt`, `updatedAt`, `createdBy`, `updatedBy`

### 4.2 `POST /personas/preview` (operationId `previewPersonas`): AI-generate drafts, 200, nothing saved
```ts
{ agentId: uuid /*REQUIRED*/, prompt: string /*REQUIRED, minLength 1*/, count?: integer /*1..50, default 3*/ }
// -> { personas: Array<{ name, prompt, successCriteria, tags[] }> }
```
[UI]: "Personas will be generated based on this agent's purpose and configuration." The dashboard caps the description at 2,000 characters.

### 4.3 Read, update, delete
* `GET /personas?workspaceId=` (required). Other params: `search`, `filters` (Base64), `sortBy`, `sortOrder`, `limit`, `offset`, `ids`, `include`, `export*`.
  * List rows return `prompt` and `successCriteria` truncated to 280 chars (with `promptTruncated` / `successCriteriaTruncated`).
  * `systemPrompt`, `evaluationPrompt` and `simulationVariables` appear only when `include` names them. Any `include` caps `limit` at 100.
  * Response: `{personas[], total}`.
* `GET /personas/{personaId}` → `{persona}`, full fields. No workspaceId param.
* `PUT /personas/{personaId}`: any of `name`, `prompt`, `successCriteria|null`, `systemPrompt|null`, `evaluationPrompt|null`, `tags`, `agentIds` → `{persona}`.
* `DELETE /personas/{personaId}?force=true|false` refuses with **409** while simulations use the persona ("the response lists them"). `force=true` deletes it anyway and removes it from those simulations.

---

## 5. Assertions (natural-language checks graded by the analysis LLM)

[UI] `assertions.description`: "Define assertions to evaluate agent behavior. Assertions can be assigned to agents, workflow nodes, and simulations." An assertion has a name, a prompt and a point value, plus an optional **judge context** covering routing events, extraction events, tool calls and other system events ("Excluding irrelevant content reduces false positives").

* **Inside simulations.** `POST /simulations` takes `assertions?: string[]`. The dashboard copy for that field says "List the pass/fail checks you want every run to evaluate. Add one per line or sentence." The placeholders include `Evaluate "is false": Call involves a swear word.`, so the strings are probably plain-language assertion prompts. `GET /simulations/{id}` returns them as objects `{id, workspaceId, name, prompt, contextConfig?: {includeRoutingEvents?, includeExtractionEvents?, includeToolCalls?, includeOtherEvents?} | null, createdAt, updatedAt}`. Whether the strings are prompt text or existing assertion ids is **UNVERIFIED**. They are not typed as uuid, which suggests text.
* **On workflow nodes.** Each node has `"assertions": []` in the workflow JSON; the item format is unknown.
* **Library (undocumented).** [FE] lists:
  * `GET|POST /assertions`, `POST /assertions/preview`, `GET|PUT|DELETE /assertions/{assertionId}`
  * `GET|PUT /agents/{agentId}/assertions`
  * [LIVE]: `GET /assertions?workspaceId=` → `{assertions:[], total:0}`; `GET /agents/{id}/assertions` → `{assertionIds:[]}`
* **Results.** Results land on the call in `assertionResults[]` and `assertionsSummary` (section 3.1). They are evaluated after the call by `postConversationAnalysis.analysisModel`.

---

## 6. Simulations (Freya-native simulated customers)

### 6.1 `POST /simulations` (operationId `createSimulation`), returns **200**
Required: `name`, `agentId`, `selectedPersonaIds`.
```ts
{
  name: string,                                   // REQUIRED, minLength 1
  description?: string,
  tags?: string[],                                // maxItems 50 ("Collections" in UI)
  agentId: uuid,                                  // REQUIRED — for a workflow use its wrapper agent id (INFERRED)
  mode?: "voice" | "chat",                        // default "voice"
  selectedPersonaIds: string[],                   // REQUIRED, 1..1000
  assertions?: string[],                          // see §5
  customerVariablesMap?: Record<personaId, Record<string,string>>,
        // "Per-persona variable values for this run, keyed by personaId. Saved as the persona's most-recently-used
        //  values for the simulated agent."  [UI]: "Optional values passed to the agent under test, overriding its test variables."
  maxConcurrentCalls?: integer,                   // 1..1000   (settings default 1)
  maxCallDurationSeconds?: integer,               // 60..3600  (settings default 240)
  maxRetries?: integer,                           // 0..10     (settings default 3)
  runsPerPersona?: integer,                       // 1..50     (settings default 1) — "Higher values help measure consistency"
  assistantLlmOverride?:  { provider<=100, model<=200 } | null,
  personaLlmOverride?:    { provider, model, reasoningEffort?: "none"|"minimal"|"low"|"medium"|"high"|"xhigh" } | null,
  analysisLlmOverride?:   { provider, model, reasoningEffort?: same enum } | null,
  routingLlmOverride?:    { provider, model } | null,
  extractionLlmOverride?: { provider, model } | null,
  agentVersionId?: uuid,                          // pin the version under test
  start?: boolean   // "When false, the simulation is created as a draft and no calls are placed until POST /simulations/{id}/start."
                    // default when omitted: starts immediately (INFERRED)
}
```
Response `data.result.simulation`:
```ts
{ id, workspaceId, agentId|null, name, description?, tags?,
  status: "draft"|"running"|"paused"|"completed",
  settings: { maxConcurrentCalls?=1, maxCallDurationSeconds?=240, maxRetries?=3, runsPerPersona?=1 },
  metrics?: { totalRuns?=0, passedRuns?=0, failedRuns?=0, averageLatencyMs?, averageDurationSeconds?, agentSpeakingPercent? (0..1) } | null,
  latestPassRate?: number(0..1)|null, personaCount?, createdBy?, updatedBy?, createdAt, updatedAt }
```
The UI also offers "Hang up on transfer: End the simulation when the call is handed to a human agent". That option has no API field.

### 6.2 Lifecycle endpoints
* `POST /simulations/{id}/start` (no body) dispatches the initial iteration of a draft. **409** "Simulation is not a draft or has no pending iteration". Returns `{simulation}`.
* `POST /simulations/{id}/rerun` "creates a new iteration".
  * The spec shows **no request body**. Response: `{success: boolean, iteration?: {id, simulationId, iterationIndex, label|null, status}, runsStarted?: integer}`.
  * The UI's "New Iteration" modal sets the agent version, assistant/persona/analysis/routing/extraction LLM overrides, `maxConcurrentCalls` and `runsPerPersona`. Those match `iteration.params`, so rerun probably accepts an undocumented body with those keys (INFERRED).
  * [UI] `rerunDisabledTooltip`: "Publish a new agent version to rerun this simulation". The UI greys out rerun until a new version is published. Whether the API enforces this is **UNVERIFIED**.
* `PUT /simulations/{id}`: any of `name`, `description|null`, `tags`, `status` (enum above), `agentId`, `selectedPersonaIds`, `assertions`, `maxConcurrentCalls`, `maxCallDurationSeconds`, `maxRetries`, `runsPerPersona`, `metrics`. Returns the same shape as GET.
* `DELETE /simulations/{id}` "Deletes a simulation and all associated data."
* Undocumented [FE]:
  * `POST /simulations/{id}/cancel`
  * `POST /simulations/{id}/iterations/{iterationId}/retry`
  * `GET /simulations/{id}/iterations/{iterationId}`: iteration detail, most likely holding the per-run rows (persona, transcript, recording, turns, outcome, reason, assertions, started, per the UI table headers)
  * `GET /simulations/{id}/persona-trends`
  * `.../iterations/{iterationId}/runs/{runId}`: SIMULATION_RUN_OVERRIDE, manual verdict override (INFERRED)

### 6.3 Reading results
* `GET /simulations?workspaceId=&status=&search=&filters=&sortBy=&sortOrder=&limit=&offset=` → `{simulations[], total}`.
* `GET /simulations/{id}?include=` →
  ```ts
  { simulation, personas[] /*280-char previews unless include=*/, assertions[] /*see §5*/,
    metrics?: {totalRuns, passedRuns, failedRuns, averageLatencyMs, averageDurationSeconds, agentSpeakingPercent} | null,
    publishedVersionId?: uuid|null }
  ```
* `GET /simulations/{id}/iterations?limit=(default 100, max 200)&offset=` lists iterations newest first:
  ```ts
  { iterations: Array<{ id, simulationId, workspaceId?, iterationIndex, label|null, notes|null, status: string, source: string,
      totalRuns, passedRuns, failedRuns, inconclusiveRuns?=0, errorRuns?=0, analyzingRuns?=0, passRate: number(0..1)|null,
      agentVersionId?: uuid|null, agentVersionNumber?: number|null, agentLlm?: {provider, model}|null,
      runAssistantLlms?: {recorded: [{provider, model}], unrecordedRuns}|null, agentName?,
      params?: { assistantLlmOverride?, personaLlmOverride?, analysisLlmOverride?, routingLlmOverride?, extractionLlmOverride?,
                 runsPerPersona?, maxConcurrentCalls? },
      createdAt?, updatedAt?, startedAt?, completedAt? }>, total }
  ```
* Per-version roll-ups:
  * `GET /simulations/version-checks?workspaceId=&entityVersionId=` (both required) →
    `{checks: [{simulationId, name, status: "passed"|"failed"|"running"|"not_run", failedRuns, score?: 0..1|null, latestIterationId, latestAt|null}]}`. This is the latest iteration per simulation that ran against that version.
  * `GET /simulations/version-persona-runs?workspaceId=&entityVersionId=` →
    `{runs: [{personaId, personaName, simulationId, simulationName, tags[], status: "passed"|"failed"|"running"|"not_run", runId?, latestAt?}]}`. One row per persona on each simulation's latest iteration.
* `GET /simulations/tags?workspaceId=` → `{tags[]}`.

**Run outcomes** [UI] `simulations.iteration.table.outcomes`: `pass`, `fail`, `inconclusive`, `error`, `queued`, `running`, `analyzing`, `retrying`.

**Verdict semantics.** Each run is a call (`call.simulationId` is set). The grading inputs are the persona's `successCriteria` (giving `call.successCriteriaResult`), the simulation `assertions` (giving `call.assertionResults`), and possibly the persona's `evaluationPrompt`. [UI] `retryLlmFail`: "LLM Fail - Call didn't meet success criteria". The UI also shows "Hallucination rate", "Avg turns" and "Assistant speaking %". How assertions and success criteria combine into the run's pass/fail is **UNVERIFIED**. Most likely success criteria decide the outcome and assertions are tracked separately ("Tracked Assertions") (INFERRED).

**Voice vs text.** `mode: "voice"` (default) or `"chat"`; [UI] "Choose whether to simulate voice calls or text chat conversations." Voice runs exercise STT, TTS, VAD and interruptions. Chat runs do not.

---

## 7. Unit tests (single-turn graded checks)

### 7.1 What they test
[UI]:
* "Snapshots the conversation up to the user message that prompted this turn. The test runs against future versions and is graded against your expectation below."
* "Unit tests grade the agent's response to a user turn, so they need at least one user message before the selected turn."
* "An LLM judge will grade the agent's response against this on every run."

Pass logic:
* With an expectation and rules: "Passes when all {count} rules pass and the judge accepts."
* With an expectation only: "Passes when the judge accepts."
* With rules only: "Passes when all {count} rules pass." The judge is "optional when rules exist" (`rubricRequired`: "Provide an expectation or at least one rule").

Tool behaviour: "Tool rules observe real tool calls; results are not mocked." The run-all request has `mockToolResults` to override this.

Prerequisite: "Save this {entityType} at least once to enable unit tests". A saved version must exist; the base workflow has v1.

### 7.2 `POST /unit-tests` (operationId `createUnitTest`), returns 201
Required: `workspaceId`, `entityType`, `entityId`, `input`.
```ts
{
  workspaceId: uuid, entityType: "agent" | "workflow", entityId: string(minLength 1),
  name?: string(1..180),                     // default: snippet of expectation
  expectation?: string(<=50000),             // LLM-judge rubric for the graded turn
  assertions?: Array<                        // maxItems 50 — deterministic "rules"
      { type: "routing",    signal?: "node_entered" | "route_target" (default "node_entered"), target: string /*node id*/, negate?: boolean=false }
    | { type: "extraction", name: string, match?: "exists"|"equals"|"contains"|"regex"|"absent" (default "exists"), value?: any, minConfidence?: number(0..1) }
    | { type: "tool_call",  tool: string, args?: object, argsMatch?: "subset" | "exact" (default "subset"), negate?: boolean=false }
  >,
  input: {                                   // REQUIRED — "cold-storage shape"
    schemaVersion: 1,
    callType: "web" | "voice",
    messages: Array<{ role: string, message?: string, content?: string, messageId?, nodeId?: string|null,
                      secondsFromStart?, duration?, timestamp?, endTime?, wasBlocked?/was_blocked?,
                      blockedContent?/blocked_content?, workflowSnapshot?: any }>,   // minItems 1; LAST must be role "user"
    customerVariables?: object,              // e.g. {customer_id, title, last_name, now}
    authoredAgainstVersionId?: uuid
  },
  judgeContext?: { includeRoutingEvents?, includeExtractionEvents?, includeToolCalls?, includeOtherEvents? } | null,
  seedCallId?: uuid, seedMessageId?: string, // when created from a real call turn
  tags?: string[]
}
```
[UI] validation: each message needs a string `role` and a `message` or `content` string; the last message must have `role: "user"`; `schemaVersion` must be 1; `callType` must be "web" or "voice". UI labels: routing = "Enters node {target}" / "Routes to {target}"; extraction = "Extracts {name}"; tool_call = "Calls tool {tool}" / "Does not call tool {tool}".

Response: the created test (`id`, all of the above, `ordinal`, `isArchived`, `seedCallId|null`, `seedMessageId|null`, `createdBy`, `updatedBy`, `createdAt`, `updatedAt`).

Open: whether `workflowSnapshot` or `nodeId` is needed so that a mid-call snapshot starts in the right node (for example, the `collection` node). Without them the runner probably replays from the initial node. **UNVERIFIED.**

### 7.3 `POST /unit-tests/run-all` (operationId `runAllUnitTests`), returns **202**
Required: `workspaceId`, `entityType`, `entityId`(uuid).
```ts
{ workspaceId: uuid, entityType: "agent"|"workflow", entityId: uuid,
  entityVersionId?: uuid,                 // version to run against (else head/current — INFERRED)
  unitTestIds?: uuid[] (1..200),          // subset
  onlyFailed?: boolean,
  mockToolResults?: object,               // additionalProperties: {} — shape undocumented; likely { "<tool_name>": <result JSON> } (INFERRED)
  judgeLlmOverride?: { provider, model }, assistantLlmOverride?: { provider, model },
  repeat?: integer (1..20, default 1) }   // flakiness measurement; "Uses about N× the credits"
// 202 data.result
{ enqueued: Array<{ unitTestId: uuid, runIds: string[], jobIds: string[] }>, skipped: string[] }   // dedups against pending jobs
```

### 7.4 Results
* `GET /unit-tests/runs/{runId}`:
  ```ts
  { id, workspaceId, unitTestId, entityVersionId, unitTestJobId|null,
    outcome: "queued"|"running"|"pass"|"fail"|"error",
    verdictReason: string|null, agentResponse: string|null, agentResponseMessageId: string|null,
    latencyMs|null, judgeLatencyMs|null, judgeModel|null,
    messages: object[]|null,                      // full conversation incl. the graded turn
    assertionResults: Array<{ index, type, passed: boolean, detail?: string|null, observed?: any }> | null,
    metadata: object, isStale: boolean, startedAt|null, completedAt|null, createdAt, updatedAt }
  ```
* `GET /unit-tests/{unitTestId}/runs?workspaceId=&entityVersionId=` (both required) lists non-stale runs: `{runs: [{id, outcome, verdictReason, judgeModel, latencyMs, judgeLatencyMs, completedAt, createdAt}], total}`.
* `GET /unit-tests?workspaceId=&entityType=&entityId=` (required). Optional: `includeArchived`, `entityVersionId`, `limit` (default 200, max 500), `offset`. Returns `{tests[] (no input/assertions/judgeContext), total, aggregates: [{unitTestId, entityVersionId, passCount, failCount, errorCount, runningCount, latestRunId, latestOutcome, latestVerdictReason, latestCompletedAt, latestJudgeModel}]}`.
* `GET /unit-tests/version-runs?workspaceId=&entityType=&entityId=` returns `{versionRuns: [{entityVersionId, versionNumber, versionName, branchId, branchName, isBranchHead, passCount, failCount, errorCount, runningCount, simPassCount, simFailCount, simRunningCount, simPendingCount?, latestRunAt}], total, limit, offset}`. It combines unit-test and simulation counts per version.
* `GET /unit-tests/overview?workspaceId=` groups by entity (`entityName`, `workflowId`, `headVersionId`, `headVersionNumber`, `tests[]`) plus `aggregates[]`.
* `GET /unit-tests/{id}`, `PATCH /unit-tests/{id}` (name, expectation, assertions, input, judgeContext, ordinal, isArchived, tags; [UI] "Saving snapshot changes marks previous runs of this test stale"), `DELETE /unit-tests/{id}` (soft archive).
* Live progress: `GET /unit-tests/stream?workspaceId=` (SSE, undocumented, works with the key [LIVE]).

---

## 8. Versions and how tests target them

| Thing under test | How to pin a version | How to read per-version results |
|---|---|---|
| Chat | `POST /chat/message {versionId}` (only meaningful on the first turn — INFERRED) | `GET /call/{id}` → `entityVersionId`, `entityVersionNumber` |
| Fork | `POST /chat/fork {versionId}` | response `entityVersionId` |
| Simulation | `POST /simulations {agentVersionId}`; rerun/new iteration (UI lets you pick "Agent Version") | `iterations[].agentVersionId/agentVersionNumber`; `GET /simulations/version-checks?entityVersionId=`; `/version-persona-runs` |
| Unit test | `POST /unit-tests/run-all {entityVersionId}`; test records `input.authoredAgainstVersionId` | `runs.entityVersionId`; `GET /unit-tests/{id}/runs?entityVersionId=`; `GET /unit-tests/version-runs` |
| Branches | `GET /branches/{branchId}/activity` → `{commits[], simulations[] (iterationId, agentVersionId, passRate, baseline{...}), unitTests[] (runId, outcome, baseline{...}), byCommit[]}` | baseline = comparison vs base version |

Version listing:
* `GET /versions/{entityType}/{entityId}?entityId=&workspaceId=` (entityType ∈ `agent|workflow`; note that `entityId` is required **both** in the path and in the query) returns `{versions: [{id, versionNumber, parentVersionId, isPublished, publishedAt?, name?, branchId?, configDiffSummary, closedFeedbacks?}], total, limit, offset, openBranches?}`.
* `POST /versions/{entityType}/{entityId}` body `{entityId, workspaceId, configSnapshot, parentVersionId?, description?}`. "For entities with a live working copy (agent, workflow) the snapshot is validated against that entity's config schema and applied to the working copy, so an invalid snapshot is rejected with 422."

For workflow agents, versions belong to the workflow entity: `entityType: "workflow"`, `entityId` = workflowId. So `agentVersionId` and `entityVersionId` are workflow version ids such as `171f4844-...` (INFERRED from `versions_base.pretty.json` + `version-checks` wording "agent/workflow version").

[FE] also lists `POST /api/v2/workflow` and `PUT /api/v2/workflow/{workflowId}` (`WORKFLOW_POST`, `WORKFLOW_PUT`, `WORKFLOW_BY_ID_PUT`) and `POST /workflow/test-api-request`. These are not in OpenAPI, but the dashboard uses them. This is relevant to the open question of how workflows get created; check whether the API key is accepted there.

---

## 9. Permissions and auth gotchas
* Some routes reject API keys outright (`/api-keys`: "Authentication method 'api_key' is not allowed for this route. Allowed methods: access_token, username_password"). Machine-only permissions (`calls:register`, `calls:resume`, `simulations:verdict-callback`) are internal.
* If `/chat/message` returns 403, the key probably lacks `calls:chat-message` or `chat:read` (INFERRED).

## 10. What the guides say (very little)
* Creating an agent (`getting-started/creating-agent`): "Navigate to the **Simulations** page → Create a test simulation → Make a test call … Iterate on the prompt and settings based on the results."
* Creating a workflow: "Use the workflow simulation feature; test different scenarios and conditions; verify that routing logic works; check that all integrations function."
* `iterating-agents` describes **AI Studio**, a UI-only chat assistant that rewrites the system prompt (Evaluation, Planning and Execution phases; preserves `{{variable}}`). There is no API for it.
* Best practices: "Test Edge Cases", "Automate Testing", "Test Audio Quality … on actual phone calls, not just in simulations".
* Knowledgebase has a "live test chat" (`POST /knowledgebase/test-search` [FE]); not relevant here.
* There is **no** guide page on simulations, personas or unit tests. `guides/features/simulations` is linked from the knowledgebase page but returns 404.
* The older `docs/openapi.json` (58 paths) mentions `/solutions/*` ("generate", "runs", "review") and `/problems/*`. That looks like an automated optimisation feature that proposes branches, and versions have `sourceRunId` ("Optimization run / proposal that minted this version"). It is not in the current spec, so do not rely on it.

## 11. Cost and quotas
* [UI] (minute-contract plans): "1 credit = 1 phone minute. Web calls, chat and simulations are free."
* Our workspace runs on a USD balance (about $10), and calls record `cost` (USD) plus `cost-events`, so LLM usage may still be metered.
* Unit tests: [UI] "Each test runs N×. Uses about N× the credits."
* **Action:** before any loop, `GET /workspaces/{id}/credits`, run **one** chat conversation, `GET` credits again, and compute the cost per conversation.
* Limits:
  * Simulations: `maxConcurrentCalls` up to 1000 (default 1); `maxCallDurationSeconds` 60–3600 (default 240).
  * Chat: message ≤ 10000 chars; `customerVariables` size-capped (422).
  * The UI's quick-create caps attached personas at 5 per simulation create flow ("maximum 5"); the API allows up to 1000.

---

## 12. Recommendation: our local regression loop

### 12.1 Options compared
| | A. Own LLM customers → `/chat/message` | B. Freya native simulations | C. Freya unit tests |
|---|---|---|---|
| Speed | Fast. Synchronous turns, parallel conversations; each turn costs one agent LLM round-trip | Slow and async (queue, then calls up to 240 s in voice, then analysis); poll | Medium (async jobs, single turn) |
| Control | Full: scripted or LLM personas, deterministic branches, per-turn checks, rewind with `fromMessageId`, A/B with `/chat/fork` | Persona prompt plus success criteria only | Exact turn snapshot plus deterministic rules |
| Fidelity to graders | Medium. Text only, no STT/TTS/VAD/barge-in | Highest (same harness as graders, INFERRED); voice mode is the real thing | Low (one turn) |
| Visibility into tools/nodes | After `/chat/end` via `GET /call/{id}` | Same, per run | `assertionResults` with `observed`, plus `messages` |
| Side effects | Real mock-bank calls (verify/get/create_promise/close_call) | Same | Real unless `mockToolResults` |
| Cost | Chat "free" (to be verified) | "free" (to be verified) | credits |
| Needs POST to Freya | yes (user approval) | yes | yes |

### 12.2 Recommended layered loop
1. **Static lint (local, no network).** Validate the generated workflow JSON:
   * every `{{var}}` is a known runtime or customer variable
   * every tool has url, method and body
   * `close_call` has `alwaysRunsAt: ["end_of_call"]` and covers every outcome
   * transitions point to existing nodes
   * gate prompts are present (right-party, identity, read-back, tool-success, close)
   * no hard-coded amounts in prompts
   * English STT/TTS (`sttConfig.language`, `ttsConfig.language`/`voiceId`)
2. **Layer A, the primary inner loop.** Run a scenario matrix through `/chat/message`:
   * Scenarios: one or more per objection class (forgot, can't afford, disputes the debt, already paid, wrong person, will pay later), plus rude/abusive, hardship/vulnerable, identity fail, declines identity, unclear/not reached, happy path, and partial payment.
   * Implement each customer as a small LLM persona with a **hidden fact sheet**: title, last_name, DOB, phone_last4, the truth about the debt, and willingness. Add a few fully **scripted** personas so regression runs are deterministic.
   * Per conversation: `customerVariables={customer_id,title,last_name,now}`; loop until `endedByAgent` or N turns; call `/chat/end`; poll `GET /call/{callId}` until `endedReason` is set and analysis has arrived; then judge.
   * Judge in two parts:
     * (a) **Deterministic checks** on the transcript and tool events:
       * `verify_identity` called once per answer before any account detail is disclosed
       * `get_account` called before quoting amounts
       * every amount spoken ∈ {amounts from the `get_account` result}
       * `create_promise` only after an explicit yes to the read-back, with args matching what was read back
       * exactly one `close_call` whose `outcome` and `reason` match the scenario's expected label
       * agent ends with "Have a good day."
     * (b) **LLM rubric** for tone: persistence vs. abuse handling, empathy for hardship, no invented figures, no disclosure to a wrong person.
   * Use `/chat/fork` to rerun only the post-identity tail on a new version.
3. **Layer C, gate unit tests** (optional, cheap to maintain). Seed them from good and bad turns of Layer-A calls (`seedCallId` / `seedMessageId`). Use `tool_call` rules with `negate` (for example, "Does not call tool create_promise" when the customer has not confirmed). Use `routing` (`node_entered: collection` only after both verifications). Pass `mockToolResults` to avoid mock-bank writes.
4. **Layer B, the release gate.** Create personas mirroring the matrix (`prompt` = role-play brief, `successCriteria` = the expected outcome label plus behaviours). Run one native simulation in `mode: "voice"` (plus `"chat"` for quick confirmation), with `customerVariablesMap` per persona, `agentVersionId` = candidate version, and `runsPerPersona` 2–3 for flakiness. Read `GET /simulations/{id}/iterations` → `passRate`, then per-run calls.

### 12.3 Minimal Python sketch (chat driver)
```python
import os, time, httpx
B = "https://staging-app.freyavoice.ai/api/v2"; W = "01e07b69-486a-4070-a7fd-3a2fc3120bf2"
H = {"Authorization": f"Bearer {os.environ['FREYA_KEY']}"}

def chat_turn(client, text, call_id=None, workflow_id=None, version_id=None, cv=None):
    body = {"message": text}
    if call_id: body["callId"] = call_id
    else:
        body["workflowId"] = workflow_id          # or agentId
        if version_id: body["versionId"] = version_id
        if cv: body["customerVariables"] = cv
    r = client.post(f"{B}/chat/message", json=body, headers=H, timeout=90)
    r.raise_for_status(); res = r.json()["data"]["result"]
    return res["callId"], res["response"], res.get("endedByAgent", False), res.get("warning")

def run_conversation(customer_llm, workflow_id, cv, max_turns=30):
    with httpx.Client() as c:
        call_id, user = None, "Hello?"
        transcript = []
        for _ in range(max_turns):
            call_id, agent, ended, warn = chat_turn(c, user, call_id, workflow_id, cv=cv if call_id is None else None)
            transcript.append(("agent", agent))
            if ended: break
            user = customer_llm(transcript)          # our persona LLM, returns next customer utterance
            transcript.append(("user", user))
        c.post(f"{B}/chat/end", json={"callId": call_id}, headers=H, timeout=60)
        for _ in range(30):                          # wait for async end-of-call analysis
            call = c.get(f"{B}/call/{call_id}", params={"workspaceId": W}, headers=H).json()["data"]["result"]["call"]
            if call.get("endedReason") and (call.get("summary") or call.get("analysis")): break
            time.sleep(2)
        return call_id, transcript, call               # judge on call["messages"], call["artifact"], tool events
```
(`chat_turn` sends `customerVariables` only on the first turn. Whether they persist across turns is INFERRED: they are bound at call start, just as the dashboard's developer panel "sent on the next call start".)

### 12.4 LangGraph integration hint
* Make evaluation a subgraph: `build_or_update_agent → lint → run_scenarios (fan-out Send per scenario) → judge → aggregate → (if score < target) critique_and_patch_prompts → loop`.
* Keep scenario definitions, expected `close_call` labels and allowed amounts as data. The builder reads the brief, extracts the amounts and gates, and the judge compares the transcript against them.

---

## 13. Who are the graders' "simulated customers"? (INFERRED)
Most likely Freya-native **Simulations** run by the organisers against the agent our builder creates:
* Personas, one per case, each carrying a role-play brief with that customer's secret identity answers and situation (objection class, hardship, abusive, wrong person, …).
* `customerVariablesMap` supplying `customer_id`, `title`, `last_name` (and maybe `now`) per persona.
* Very likely **`mode: "voice"`**: the task says "phone agent", and the platform default is voice. Chat mode is possible.

Scoring "share of cases handled correctly" probably combines:
1. **Mock-bank state per `customer_id`.** Evidence:
   * The mock bank rejects unknown ids, so customers are seeded.
   * `close_call` has a closed `outcome` enum (`promise|no_promise|not_verified|not_reached`) plus a `reason` taxonomy (`refused`, `failed`, `declined`, `wrong_number`, `unclear`, as written in the base tool's descriptions).
   * `create_promise` records `product_id`, `amount`, `date`.

   These look designed for automatic grading: did the call end with the right outcome and reason, and was a correct promise created (or correctly not created)?
2. **Persona `successCriteria` and assertions** judged by the analysis LLM (tone, gates, no invented amounts, handling of abuse and hardship).

Implications:
* Every conversation path must end with exactly one correct `close_call` and must never create a promise without explicit confirmation.
* Wrong person: do not disclose debt details, and close with `not_reached` (`wrong_number`).
* Identity failure (wrong answers): `not_verified`, `failed`. Refusal to answer: `not_verified`, `declined`.
* Verified but refused to pay: `no_promise`, `refused`.

## 14. Risks specific to testing
* **STT/TTS language mismatch.** The base workflow `agent_config` has `sttConfig.language: "tr"` and `ttsConfig.language: "tr"`, `voiceId: "leyla"`, with Turkish interruption and acknowledgement phrases, while all prompts say "Speak English". Text chat tests will pass and still hide this. Voice simulations (likely the grader) would mis-transcribe English speech. Set English STT/TTS in the builder and confirm with at least one voice simulation.
* **Mock bank side effects.** Chat tests, simulations and unit tests without mocks really POST to `freya-bank-mock`. If it holds grading state keyed by `customer_id`, our tests could pollute it. We also do not know valid test `customer_id`s (unknown ids → `{"ok":false,"error":"unknown customer_id"}`). Get the ids from the brief or the organisers; otherwise point a local copy of the tools at our own mock URL during the inner loop.
* **Async analysis.** `/chat/end` analysis is asynchronous. Judge only after `GET /call/{id}` shows results, with a timeout.
* **No tool events in the chat response.** Tool-level checks depend on the undocumented `messages` role format or `artifact` in `GET /call`.
* **`fromMessageId` and forks reuse `workflowSnapshot`.** Node state can be inconsistent after a graph change ("degrades gracefully").
* **Persona-LLM vs. our LLM.** Native simulations use Freya's default persona model unless `personaLlmOverride` is set. Our local customer LLM may behave differently, so calibrate with one native simulation per release.
* **Simulation `assertions: string[]` semantics** are unverified (text vs. ids).
* **Rerun** may require publishing a new version (per the UI tooltip).

## 15. First live verification checklist (needs the user's explicit OK, because these are POSTs)
1. `GET /workspaces/{id}/credits` → baseline.
2. `POST /chat/message {workflowId: 252ee2df-..., message: "Hello?", customerVariables: {customer_id: "<valid id from brief>", title: "Mr", last_name: "<x>", now: "<iso>"}}`. Record whether the response is the greeting, what `messageId` looks like, and whether `warning` is set.
3. Two or three more turns; then `POST /chat/end`; then poll `GET /call/{callId}?workspaceId=`. Capture the exact `messages[].role` values, the `artifact` and `analysis` shapes, where the tool calls (`verify_identity`, `close_call`) appear, `endedReason`, `cost`, and `entityVersionId`.
4. Repeat `GET /workspaces/{id}/credits` → cost per chat.
5. Optional: `POST /personas` (1 persona) + `POST /simulations {mode: "chat", start: false, ...}` → `POST /simulations/{id}/start` → poll `GET /simulations/{id}/iterations` and the undocumented `GET /simulations/{id}/iterations/{iterationId}` to capture the per-run row shape.
