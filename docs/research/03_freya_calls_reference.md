# 03 - Freya CALL DATA and OBSERVABILITY reference

Sources: `docs/openapi_prod.json` (OpenAPI 3.1, 96 paths; staging spec is byte-identical for every endpoint below, only `POST /agents` differs), `docs/llms-full.txt` guides, live GETs against staging on 2026-10-10 (saved under `research/live03/`), and the base workflow `live/workflow_base.pretty.json`.
Anything not stated in the spec or docs is marked **INFERRED**. Nothing was POSTed, PUT, PATCHed or DELETEd.

---

## 0. TL;DR for the builder / validator

* **Where the transcript lives:** `GET /call/{callId}?workspaceId=...` -> `data.result.call.transcript` (string) and `data.result.call.messages[]` (`role` required; `message`, `secondsFromStart`, `timestamp`, `duration`, `endTime`, `messageId`, `wasBlocked`/`was_blocked`, `blockedContent`/`blocked_content`). **The spec does not enumerate `role` values, and it does not model tool-call or tool-result messages or node names.** The items are not `additionalProperties:false`, so extra keys (for example `nodeId`, which the unit-test message schema has) may appear. The validator has to handle roles it does not recognise.
* **Structured outputs = "Insights"** (workspace-level field definitions, numeric-string IDs). Types: `string | number | rating | checkbox | range | enum | array`. Sources: `analysis` (LLM post-call extraction; the `description` is the extraction instruction), `tool_field` (the platform copies a tool's field: `toolName` + `toolFieldPath`), `workflow_variable` (read-only, the platform writes it). Attach them to an agent with `PUT /agents/{agentId}/insights` (this creates an **unpublished** version) or through `config.postConversationAnalysis.insights[]`. Read the values from `call.insights` (an object) or with `GET /call?insights=<ids>`.
* So insights **can** capture promise amount/date, an abusive-customer flag, a hardship flag, objection type, verification passed, and read-back done. Each would be an `analysis` field. Promise amount/date and the close outcome can also be `tool_field` values from `create_promise` / `close_call`. Our validator should still treat them as **post-hoc signals, not ground truth**.
* **Variables:** the template syntax is `{{name}}`. The base workflow uses `{{title}}`, `{{last_name}}`, `{{customer_id}}`, `{{now}}`, and tool-arg placeholders (`{{amount}}`, `{{date}}`, `{{product_id}}`, `{{outcome}}`, `{{reason}}`, `{{field}}`, `{{answer}}`). Graders inject customer data through one of these:
  * `POST /call` body `properties`
  * `POST /campaign` `customers[].properties`
  * `POST /chat/message` `customerVariables`
  * `POST /simulations` `customerVariablesMap[personaId]`

  Which one they use is unknown; the simulation path is the most likely (**INFERRED**).
* **Webhooks:** end-of-call-report = `{type:"end-of-call-report", callId, workspaceId, summary, endedReason, durationMs, transcript, status, callEndReasonCategory, customer, startedAt, endedAt, recordingUrl, timestamp}`. It has **no messages array, insights or tags**, so we poll `GET /call/{id}` for those. The campaign outcome webhook is `{callId, campaignId, phoneNumber, outcome: success|failed, failureCategory, message, willRetry, retryScheduledAt, retriesRemaining, attemptNumber}`.
* **Spend:** `GET /workspaces/{workspaceId}/credits` returned live `creditsLeft: 20, creditUsdRate: 0.5, billingUnit: "usd"` (= $10). `GET /analytics/credits` returns daily usage. Per call, check `call.cost` and the analytics property `call.costMicros`.
* **Safety:** closing a chat test call (`POST /chat/end`) fires the end-of-call lifecycle, **including end-of-call tools**. The base workflow's `close_call` has `config.alwaysRunsAt: ["end_of_call"]` and will POST to the mock bank's `/v1/close_call`. Any of our own test calls will therefore write to the mock bank, which may hold grading state.

---

## 1. Conventions

* Base (our key): `https://staging-app.freyavoice.ai/api/v2`. Auth header: `Authorization: Bearer <FREYA_KEY>`. Workspace: `01e07b69-486a-4070-a7fd-3a2fc3120bf2`.
* Envelope:
  * Success: `{success:true, data:{message:"Success", result:{...}}}`.
  * Error: `{success:false, data:{message, result}}`. Live 404 example: `{"success":false,"data":{"message":"Call not found","result":{"error":"Call not found"}}}`.
* Standard error codes on these endpoints: 400 (validation), 401 (missing/invalid token), 403 (workspace ID mismatch or insufficient permissions), 404, 500.
* `workspaceId` is a **query** param on GETs. On several writes it goes in the **body**: `POST /call`, `POST /call/{id}/tags`, `PATCH /call/{id}/tags`, `PUT /insights/{id}`, `PUT /agents/{id}/insights`. Some endpoints declare no workspaceId at all and take the workspace from the token: `POST /insights`, `PUT /call/{callId}/insights`, `POST /chat/*`, `POST /campaign`, `GET /alerts/events`. Live: `GET /alerts/events` works with and without `workspaceId`.
* ID formats:
  * Calls, agents and workflows: UUID.
  * **Insight IDs: numeric strings** (`pattern ^\d+$`) in paths and in `postConversationAnalysis.insights[].id`. They are **numbers** in `insightIds` (`/agents/{id}/insights`) and in `structuredDataIds` / `fields[].id` (analytics).
* Our API key's permissions (Owner role, from live `GET /workspaces`) include `calls:read`, `calls:export`, `calls:pii`, `calls:chat-message`, `insights:*`, `analytics:read`, `billing:read`, `simulations:*`, `campaign:*`, `alerts:read`.

---

## 2. `GET /call` - List calls (operationId `listCalls`)

Needs `calls:read`. `export=true` also needs `calls:export` and returns a ZIP instead of JSON. Per the spec description: "Rows omit `summary` and carry the first 280 characters of `reviewNotes` unless `include` asks for them; tags carry no `metadata`."

### Query params (all strings unless noted)

| param | notes |
|---|---|
| `workspaceId`* | uuid |
| `limit` | default `"20"` |
| `offset` | default `"0"` |
| `sortBy` | enum `startedAt` (default), `duration`, `sentiment`, `relativeTime`, `cost` |
| `sortOrder` | `asc` / `desc` (default `desc`) |
| `callIds` | comma-separated call IDs |
| `startDate` / `endDate` | ISO; startedAt >= start, < end |
| `phoneNumberId` | uuid |
| `customerNumber` | customer phone |
| `campaignId` | uuid: "Return only the calls placed by this campaign" |
| `searchTerm` | free text |
| `status` | free string (values not enumerated; see section 2.2) |
| `type` | free string. **INFERRED** values from the analytics `callType` enum: `inbound`, `outbound`, `web`, `inboundPhoneCall`, `outboundPhoneCall`, `webCall`. `POST /inbound/web` also knows `simulationCall`. |
| `endedReason` | free string |
| `filters` | "Base64-encoded filter state" (format undocumented) |
| `insights` | "Comma-separated insight (structured data) IDs to return values for, max 25" |
| `includeStatusCounts` | `'false'` skips aggregation |
| `countsOnly` | `'true'` returns only `statusCounts` (verified live) |
| `semanticQuery` | 2..500 chars; vector search, applied before filters |
| `archivedFilter` | `active` (default) / `all` / `archived` |
| `export` | `'true'` returns a file |
| `exportFormat` | `csv` / `xlsx` / `markdown` |
| `exportColumns` | JSON array of `{id,label}` |
| `additionalData` | comma-separated extras: `transcript`, `cxInsights` |
| `include` | opt-in heavy fields: `summary`; `reviewNotes` (full text instead of the first 280 chars) |

There is **no `simulationId` or `assistantId` query filter** on `GET /call`. Every row carries `simulationId` and `assistantId`, so filter on the client side, or use `callIds`. The `filters` blob may also work (**INFERRED**).

### 2.1 Response `data.result`

```
calls[]           (row shape = section 3 call object minus: sipCallId, analysis, createdAt, artifact,
                   problems, assertionResults, successCriteriaResult, piiRedactions)
pagination        {total, limit, offset, hasNext, hasPrevious}   (all required)
statusCounts      {all*, live (default 0), processing (default 0), transferred*, successful*, failed*}
searchTruncated   boolean
```

Live (empty workspace): `{"calls":[],"pagination":{"total":0,"limit":5,"offset":0,"hasNext":false,"hasPrevious":false},"statusCounts":{"all":0,"live":0,"processing":0,"transferred":0,"successful":0,"failed":0}}`

### 2.2 Status / endedReason vocabularies (none are enums in the call schema)

* `statusCounts` buckets: `live`, `processing`, `transferred`, `successful`, `failed`. The `/analytics` `status` filter enum is `successful | failed | transferred`.
* `endedReason` examples from the spec:
  * End-of-call webhook: `'customer-ended-call'`, `'assistant-ended-call'`.
  * Campaign `retryConditions` "special endedReason values": `voicemail`, `exceeded-max-duration`, `inactivity-ended-call`, `rescheduled`, `capacity_exceeded`, `capacity_wait_abandoned`, `number_not_configured`, `never_connected`.
  * SIP categories (also in `retryConditions`): `Rejected`, `Unreachable`, `Busy`, `No Answer`, `Network Error`, `Invalid`, `Timeout`, `Cancelled`, `Resource Unavailable`, `Service Unavailable`, `Protocol Error`, `Unspecified`.
  * `POST /chat/end` returns an `endedReason` string.
* Sentiment buckets (analytics): `very-unhappy`, `unhappy`, `neutral`, `happy`, `very-happy`, plus "Untagged" in the UI. **There is no `sentiment` field on the call object.** `sortBy=sentiment` exists, and the UI shows an "Untagged" bucket, so sentiment is most likely stored as a **tag** in `call.tags[]` (**INFERRED**).

---

## 3. `GET /call/{callId}` - Get call details (operationId `getCall`)

Params: path `callId`* (uuid), query `workspaceId`* (uuid). Returns `data.result.call`. 404 means "Call not found".

### 3.1 Full `call` object (`*` = required)

```
id*                     string
externalCallId          string|null          (POST /call externalCallId echo)
assistantId             uuid|null            (agent id)
assistantName           string|null
assistantEntityType     "workflow"|null
assistantWorkflowId     uuid|null
workspaceId*            string
phoneNumberId           string|null
phoneNumber             string|null          (our caller-ID number)
simulationId            uuid|null            (set for simulation runs)
campaign                object|null {id*, assistantId, phoneNumberId, name*, description}
                        (list rows: {id*, name*} only)
customerNumber          string|null
customerLabel           string|null
startedAt / endedAt     string|null (ISO)
duration                number|null          (unit not stated; INFERRED seconds)
status                  string|null
type                    string|null
modality                "voice"|"text"|null  (chat API calls -> "text", INFERRED)
channel                 "whatsapp"|"api"|null
endedReason             string|null
transcript              string|null          (webhook says "Full conversation transcript with timestamps")
messages                array|null  -> items:
    role*               string   (values NOT enumerated in spec)
    message             string   (the text)
    duration            number
    secondsFromStart    number
    timestamp           string
    endTime             number
    messageId           string   (used by /chat/message fromMessageId, /chat/fork, feedback messageId)
    was_blocked / wasBlocked          boolean   (guardrail blocked this output)
    blocked_content / blockedContent  string    (what was blocked)
summary                 string|null          (AI summary; list rows only with include=summary)
cost                    number|null          (unit not stated; INFERRED USD; analytics exposes call.costMicros)
turnLatencyStats        object|null {p50*, p75*, p90, p95*, p99*, avg, count}
recordingUrl            string|null
tags[]                  {tag*, source*, value:number, confidence:number|null [0..1], metadata:object|null}
                        (additionalProperties:false; list rows omit metadata)
customer                {number: string|null, identity: {firstName, lastName, email}}
unresolvedFeedbacksCount / resolvedFeedbacksCount / unresolvedAiFeedbacksCount / unresolvedUserFeedbacksCount  number
reviewStatus / reviewTag / reviewNotes   string|null ; reviewNotesTruncated boolean
reviewAssignees         [{userId*, userName}] | null
isArchived (default false), isImported (default false), originCallId uuid|null, isShared boolean
assertionsSummary       {passed*, failed*, total*} | null
unitTestsCount          number
insights                object (additionalProperties:{}); structured-data values. INFERRED: keyed by insight id (or name), value = typed value
searchScore / searchSnippet  (search hits only)
entityVersionId         uuid|null   ; entityVersionNumber number|null   <- which agent/workflow version ran
updatedAt               string|null
--- detail-only fields ---
sipCallId               string|null
analysis                {} (untyped; post-conversation analysis blob. INFERRED: summary/sentiment/themes/structured data)
createdAt               string|null
artifact                {} (untyped. INFERRED: recording/transcript/log artifacts)
problems[]              auto-detected / manual feedback items (schema 3.3)
assertionResults[]      {id, name, assertion, success: bool|null, reason, relevantMessages: object, timeTaken, metadata}
successCriteriaResult   same shape as one assertionResult
piiRedactions[]         {token*, label*, value (only with calls:pii)} - "PII the voice agent tokenized in this call before storage"
```

### 3.2 Messages: what is and is not specified

* The spec gives only `role` (required) and `message`. It does **not** list role values. It does not model tool calls, tool arguments, tool results or node names on `GET /call/{id}`.
* Evidence that the real payload is richer than the schema (**INFERRED**):
  1. The unit-test `input.messages[]` schema (same family) adds `content`, `nodeId: string|null`, `workflowSnapshot`, and `additionalProperties: {}`.
  2. Unit-test `judgeContext`, simulation assertion `contextConfig`, and `POST /unit-tests` assertions can include `includeRoutingEvents`, `includeExtractionEvents`, `includeToolCalls` and `includeOtherEvents`, plus assertion types `routing` (`node_entered` / `route_target`), `extraction` and `tool_call` (`tool`, `args`, `argsMatch: subset|exact`). The platform therefore stores routing, extraction and tool-call events per call.
  3. The base workflow's transitions read `verify_identity_response.data.all_verified`, so tool results land in variables named `<tool_name>_response`.
  4. The Web SDK streams `toolCallStarted` / `toolCallInProgress {function_name, tool_call_id, arguments}` / `toolCallStopped`.
* `POST /call/export-raw` ZIP contains `calls.json` + `traces/<callId>.json.gz` + `logs/<callId>.json.gz` ("management only"). This is the lossless place where tool calls and results certainly exist.
* **Action item:** run ONE cheap chat test (`POST /chat/message`) and dump one real `GET /call/{id}`. That pins down the role strings (likely candidates: `user`, `assistant`/`bot`, `system`, `tool_calls`/`tool_call_result`; all **INFERRED**) and whether node and tool fields are present. **Warning:** this spends credits, and on `/chat/end` it triggers the end-of-call `close_call` tool against the mock bank. Get the user's OK first.

### 3.3 `problems[]` item (also the feedback schema)

`{id, workspaceId, callId, agentId, title* (minLength 1), description, proposedSolution, solutionNote, type, severity (0..1, default 0.5), status (default "open"), assignedTo, createdBy (default "ai"), messageId, resolved (default false), resolvedAt, resolvedBy, solutionId, resolvedByVersionId, number, createdAt, updatedAt, callStartedAt, customerNumber, entityVersionId, versionNumber, callReviewTag, callReviewNotes, webCallerName, webCallerUserId, hasExternalReference, externalReferences[{id,url,label}], agentName, agentEntityType, agentWorkflowId, resolvedByName, assignees[{userId,userName,assignedAt,assignedBy}], commentsCount, openSolutions[...], solutionCommits[...], isArchived}`

`type` enum: `workflow | decision-llm | stt | tts | vad-nc | latency | platform | ui | general | null`. `POST /feedbacks` also accepts `system | audio | transcription | decision`.

`GET /feedbacks?workspaceId&callId=...` lists them per call (live summary keys: `byType`, `byStatus{open,in-progress,resolved,dismissed}`).

### 3.4 Related per-call reads

| endpoint | what |
|---|---|
| `GET /call/{callId}/recording?workspaceId` | `audio/mpeg` MP3 stream; 404 "Recording not found" |
| `GET /reviews?workspaceId&callId` | `review|null`: `reviewStatus` enum `No Review Status / Pending Review / In Progress / Completed / Not Needed`; `reviewTag` enum `Successful / Failed / Technical Issue / Wrong Number / No Answer / Needs Review / null`; `reviewNotes` |
| `GET /alerts/events?callId=<uuid>` | per-call error events. `source` enum `llm, tts, stt, tool, latency, telephony, webhook, knowledgebase, system`; `severity` enum `info, warning, error, critical`; `{fingerprint, code, message, callId, agentId, agentName, metadata, occurredAt}`. **Use `source=tool` to detect failed tool calls (tool-success gate).** Other params: `limit` (1-100, default 50), `offset`, `search`, `sortBy` (`occurredAt|severity|source`), `sortOrder`, `sources`, `sourcesExclude`, `severities`, `severitiesExclude`, `fingerprint`, `agentId`, `from`, `to`. |
| `POST /call/export-raw` (write-ish: queues a job) | `{workspaceId*, callIds[], archivedFilter, filters, search, semanticQuery, redact (default false), combineAudio (default true)}` returns 202 `{jobId, callCount, statusUrl, downloadUrl}`. Poll `GET /export/{jobId}`, then `GET /export/{jobId}/download`. Max 10,000 calls (413). |
| `GET /call?export=true&exportFormat=markdown&callIds=...` | ZIP with the chosen format (+ recordings/configs/logs if requested) |

Live built-in alert rules (all `isSystem`), useful as health signals: "Tool failure rate" (`monitoring.tool-failure-rate`, source `tool`, threshold 3 per 900 s), "Call error rate", "Turn latency", "No customer speech", "Early hangups", "Model errors", "Voice errors", "Transcription errors", "LLM cold prefills", plus others.

---

## 4. Insights = structured data fields

### 4.1 Definitions

`GET /insights?workspaceId` (`limit` default and max 500, `offset`, `sortBy`, `sortOrder`) returns `{insights[], total, limit, offset}`. Live: 0 insights. Each item has:
`id*, name*, description, label, valueLabels, type*, source (analysis|workflow_variable|tool_field, default analysis), toolName, toolFieldPath, enums[], isRequired*, agentCount*, agents[{id,name,entityType:"workflow"|null,workflowId}], valuesCount*, createdAt*, updatedAt*`

**`POST /insights` (createInsight)**, 201 `{insight}`; 409 "An insight with this name already exists". Body:

| field | type / constraints | default |
|---|---|---|
| `name`* | string 1..100 (unique per workspace) | |
| `description` | string\|null, max 10000. **This is the extraction prompt**: there is no separate prompt field (**INFERRED** that the analysis LLM uses it) | null |
| `type` | `string \| number \| rating \| checkbox \| range \| enum \| array` (immutable after create) | `"string"` |
| `enums` | string[] (for `enum`/`array`) | `[]` |
| `defaultValue` | string\|null | null |
| `rangeMin` / `rangeMax` | number\|null (for `range`/`rating`) | null |
| `isRequired` | boolean | **true** |
| `label` | string\|null max 100 | |
| `valueLabels` | object<string, string max 60> \| null (display labels per value) | |
| `source` | `analysis \| tool_field` (create cannot make `workflow_variable`) | `"analysis"` |
| `toolName` | string\|null max 100 (for `tool_field`) | |
| `toolFieldPath` | string\|null max 200 (for `tool_field`). **INFERRED**: dotted path into the tool's args or response, e.g. `body.amount` or `data.outcome`. The spec does not say which. | |

No `workspaceId` in the spec for this POST; the workspace comes from the token (**INFERRED**).

* `GET /insights/{insightId}?workspaceId` returns the same fields plus `assignedAgents[{id,name}]`, `workspaceId`, `defaultValue`, `rangeMin`, `rangeMax`, `createdBy`, `updatedBy`.
* `PUT /insights/{insightId}`: body `{workspaceId*, name, description, enums, defaultValue, isRequired, rangeMin, rangeMax, label, valueLabels, toolName, toolFieldPath}`. Type is immutable.
* `DELETE /insights/{insightId}?workspaceId` deletes the definition **and all values**.

### 4.2 Assigning insights to an agent (two equivalent paths)

* `GET /agents/{agentId}/insights?workspaceId` returns `{insightIds: number[]}`. Live, base agent: `{"insightIds":[]}`.
* `PUT /agents/{agentId}/insights` body `{workspaceId*, insightIds*: number[]}` returns `{success, insightIds}`. Spec: "Replaces the structured data fields in the agent's config and saves the result as a **new, unpublished version**. Calls use the new set **once that version is published**." So this must be followed by a publish (see the versions research).
* Alternatively, set them in the agent config: `config.postConversationAnalysis.insights: [{id: "^\d+$" (optional), name* (1..100)}]`, max 200 items. Present on POST/PUT `/agents` and GET `/agents/{id}`. For workflow agents the workflow JSON also has `workflow.agent_config.postConversationAnalysis` (live base). **INFERRED**: the workflow copy is what the runtime uses for workflow agents.

### 4.3 Setting or correcting one value on a call

`PUT /call/{callId}/insights` (no workspaceId param). Body `{name*: existing insight name (1..100), value*: string(<=1000) | number | boolean | null}`. `null` clears the value. Returns `{name, value}`.

* 400: value does not fit the type/list.
* 404: call or insight missing.
* **409**: the platform writes this insight (`tool_field` / `workflow_variable`), so it is refused and "store the correction in a separate insight".
* Every write is audit-logged.

### 4.4 Reading values

* Per call: `call.insights` (object) on `GET /call/{id}`.
* Bulk: `GET /call?workspaceId&insights=<id1,id2,...>` (max 25 IDs), with each row's `insights` filled.
* Aggregates: `GET /analytics/structured-data`. Params: `workspaceId`*, `startDate`, `endDate`, `granularity` (`hour|day|month`, default `day`), `agentIds[]`, `structuredDataIds[]` (integers > 0), `assistantIds[]`, `callTypes[]` (`inbound|outbound|web|inboundPhoneCall|outboundPhoneCall|webCall`), `phoneNumberId`, `campaignIds[]` (max 20), `insightFilters` (JSON string `[{insightId, op, values}]` ANDed, max 16000 chars; **`op` vocabulary not documented**). Result:
  * `{workspaceId, timeRange{start,end}, granularity, fields[{id:number, name, type, agentId, agentName}], data[{structuredDataId:number, timeSeries[{date,count,avg,sum,min,max,trueCount,falseCount}], summary{count,avg,sum,min,max,trueCount,falseCount,trueRate}, distribution{}, valueCounts{}}], truncated, generatedAt}`
  * Live: `fields: [], data: []`. Default window = last 30 days (`2026-09-11` to `2026-10-10`).
* `GET /analytics/properties?workspaceId` lists every widget/filter property, insight fields included (`origin:"insight"`, keyed by id). Live system properties:
  * `call.id`, `call.type`, `call.modality`, `call.status`, `call.endedReason`
  * `call.startedAt`, `call.endedAt`, `call.durationSeconds`, `call.costMicros`, `call.userTurnCount`, `call.latencyP50`, `call.latencyP95`
  * `call.assistantId`, `call.phoneNumberId`, `call.campaignId`, `call.phoneNumber`, `call.phoneFriendlyName`
  * `call.isArchived`, `call.isImported`, `call.isAnswered`, `call.isConversed`, `call.isVoicemail`, `call.isFailed`, `call.isSuccessful`, `call.attemptRank`
  * `customer.key`, `agent.name`, `phone.friendlyName`, `campaign.name`

### 4.5 Can insights capture what we need? Yes. Proposed fields (if we choose to create them)

| name | type | source | extraction note (goes in `description`) |
|---|---|---|---|
| `call_outcome` | enum [`promise`,`no_promise`,`not_verified`,`not_reached`] | `tool_field`, `toolName:"close_call"`, `toolFieldPath:"body.outcome"` (**INFERRED** path) or `analysis` | mirrors the mock-bank close outcome |
| `promise_amount` | number | `tool_field` `create_promise` / `body.amount` (**INFERRED**) or `analysis` | "Exact amount the customer agreed to pay, as read back by the agent; empty if none" |
| `promise_date` | string | `tool_field` `create_promise` / `body.date` or `analysis` | "YYYY-MM-DD" |
| `identity_verified` | checkbox | analysis | both security answers verified by tool |
| `readback_confirmed` | checkbox | analysis | agent read back amount + date and the customer said yes before `create_promise` |
| `abusive_customer` | checkbox | analysis | customer used insults/threats/profanity |
| `hardship_flag` | checkbox | analysis | job loss, illness, bereavement, vulnerability |
| `objection_type` | enum [`forgot`,`cant_afford`,`disputes_debt`,`already_paid`,`wrong_person`,`will_pay_later`,`none`] | analysis | primary objection |
| `callback_requested` | checkbox / string | analysis | callback date/time if any |
| `amount_mismatch` | checkbox | analysis | agent quoted a figure not present in tool results |

Caveats:
* Analysis runs **async after the call ends** ("analysis runs async on pipecat; the webhook arrives at the dashboard shortly after with summary, sentiment, themes, problems, and assertions"). Poll until `summary`/`insights` are non-null.
* Each analysis costs LLM tokens (**INFERRED**, probably billed). `isRequired` defaults to `true`; set `false` for flags that are often empty. **INFERRED**: a required field forces the LLM to always output a value.
* These are for our own observability and debugging. The grader decides correctness from its own judgement of the conversation, plus probably the mock-bank state (**INFERRED**).

---

## 5. `postConversationAnalysis` (agent config, and workflow `agent_config`)

Schema (`type: object|null`), identical on POST /agents, PUT /agents/{id}, GET /agents/{id} and GET /agents/defaults:

| field | type | default | meaning |
|---|---|---|---|
| `insights` | `[{id?: "^\d+$", name*: 1..100}]`, maxItems 200 | (absent) | structured-data fields to extract (section 4.2) |
| `summaryPrompt` | string\|null, max 10000 | null | custom prompt for `call.summary` |
| `autoDetectProblems` | boolean | **false** | when true, the AI writes `call.problems[]` / feedbacks (`createdBy:"ai"`) |
| `problemDetectionPrompt` | string\|null, max 10000 | null | override problem-detection prompt |
| `problemDetectionInstructions` | string\|null, max 10000 | null | extra instructions appended (**INFERRED**) |
| `analysisModel` | string\|null, max 100 | null | model for post-call analysis (null = platform default) |

Live: the base agent and the workflow `agent_config` both have all nulls/false and no `insights` key.

Related config:
* `extractionLlmConfig` `{provider*, model*, baseUrl, apiKey}` | null: model for in-call `extraction_schema` work (**INFERRED**).
* `guardrailsConfig` (agent config): `guardrails_enabled`, `rules[{type: exact|regex|dictionary|structured|classifier, pattern, action: refuse|regenerate|log_only, ...}]`, `refuse_message`, `max_regenerate_retries` (0-5, default 2), `regenerate_wait_message`, `input_config{detect_injection,...}`. These produce `wasBlocked`/`blockedContent` on messages.

A money validator should scan the spoken `message`, not `blockedContent`. A `regex` guardrail with `action: regenerate` on unexpected currency patterns is a possible in-call safety net (**INFERRED** feasibility; patterns are static, so it cannot know the per-customer amounts).

---

## 6. Webhooks (outbound from Freya to us)

### 6.1 End-of-call report: `POST /webhooks/end-of-call-report` (operationId `endOfCallReportWebhook`)

The payload Freya sends ("Sent when a call completes"). Our receiver answers 200.

| field | type | req | note |
|---|---|---|---|
| `type` | `"end-of-call-report"` | * | |
| `callId` | uuid | * | |
| `workspaceId` | uuid | * | |
| `summary` | string | | AI summary |
| `endedReason` | string | | e.g. `customer-ended-call`, `assistant-ended-call` |
| `durationMs` | number | * | milliseconds |
| `transcript` | string | | "Full conversation transcript with timestamps" |
| `status` | string | * | "Call status code" |
| `callEndReasonCategory` | string | * | category (values undocumented) |
| `customer` | string | * | customer phone number |
| `startedAt` / `endedAt` | date-time | * | |
| `recordingUrl` | uri | * | |
| `timestamp` | date-time | * | send time |

Not included: messages, tool calls, insights, tags, sentiment, cost. Fetch `GET /call/{callId}` for those.

Where the URL is configured: `workspaceSettings.webhooks` is an `object<string,string>` (default `{}`; live `{}`), settable through `PUT /workspaces` `{workspaceSettings:{webhooks:{...}}}`. Key names are undocumented; **INFERRED** an event name such as `"end-of-call-report"` mapped to a URL. `POST /inbound/web` returns `properties.webhookUrl`, so each call session carries a webhook URL. We do **not** need webhooks: polling `GET /call` is enough and costs nothing extra.

### 6.2 Campaign call outcome: `POST /webhooks/campaign-call-outcome` (operationId `campaignCallOutcomeWebhook`)

Sent to the campaign's `webhookUrl` when a campaign call completes or fails. Payload types: `WebhookCallOutcome` and `WebhookApiError` (initiation failed before a call existed).

| field | type | req | note |
|---|---|---|---|
| `callId` | string | | absent for api_error |
| `campaignId` | string | * | |
| `phoneNumber` | string | * | |
| `outcome` | `success` \| `failed` | * | |
| `failureCategory` | string | | `no_answer`, `busy`, `voicemail`, `timeout`, `api_error`, `llm_fail` (when the campaign has a successPrompt), `other` |
| `message` | string | | ended_reason, timeout message, or LLM evaluation reason |
| `willRetry` | boolean | | |
| `retryScheduledAt` | date-time | | |
| `retriesRemaining` | number | | |
| `attemptNumber` | number | | 1-indexed |

Note: "successPrompt" is mentioned but not present in the `POST /campaign` schema (legacy, **INFERRED**).

---

## 7. Call tags (operations on an existing call)

| op | endpoint | body/params | response |
|---|---|---|---|
| add | `POST /call/{callId}/tags` | `{workspaceId*, tag* (minLength 1), source (default "user"; e.g. 'user','ai'), value: number, confidence: 0..1, metadata: object\|null}` | `{success, tag{tag,source,value,confidence,metadata}, message}` |
| upsert/delete | `PATCH /call/{callId}/tags` | same + `value`/`confidence` nullable + `delete: boolean` ("Updates an existing tag or adds a new one") | same |
| remove | `DELETE /call/{callId}/tags?callId=<uuid>&tag=<name>` | the spec has `callId` both in the path and as a required query param; no workspaceId | `{success, message}` |

Tags appear in `call.tags[]` (detail with metadata; list without). Possible uses:
* After a run, our post-checker can tag calls: `abusive_customer`, `hardship`, `money_mismatch` (value 1, confidence, metadata `{amounts_found:[...], allowed:[...]}`).
* The agent itself **cannot** tag during a call unless a tool calls the Freya API with a key. Do not do that: it would put a secret in the workflow (**INFERRED** recommendation).
* Sentiment is probably a system tag (**INFERRED**).

All tag endpoints are writes, so they need the user's approval before we use them.

---

## 8. Starting calls and how customer variables are injected

### 8.1 `POST /call` - Initiate call (operationId `initiateCall`)

Body (`required: phoneNumber, workspaceId`):

| field | type | note |
|---|---|---|
| `phoneNumber`* | string | customer/destination number (E.164 normalised unless `skipE164Normalization:true`) |
| `workspaceId`* | uuid | |
| `callerNumber` | string | caller ID; used to look up SIP/trunk. "Required for regular calls; campaign calls derive it from the campaign" |
| `callerChannel` | `sip` \| `whatsapp` | default sip |
| `campaignId` | uuid | presence triggers the campaign call flow |
| `properties` | object (any) | **"Custom properties to pass to the agent"**, i.e. the values for `{{customer_id}}`, `{{title}}`, `{{last_name}}`, ... |
| `attemptNumber` | integer > 0 | campaign attempt (1 = initial); "surfaced to the agent as 0-based **call.retry_number**" (so `{{call.retry_number}}` is a built-in var, **INFERRED** syntax) |
| `originCallId` | uuid | call whose **`reschedule_call` tool** produced this follow-up; campaign calls only |
| `externalCallId` | string 1..255 | client tracking; echoed on the call |
| `assistantId` | uuid | override campaign assistant |
| `entityVersionId` | uuid | version of the agent/workflow to run |
| `skipE164Normalization` | boolean | default false |

Response 200 `{success, callId, jobUuid, job_uuid, message, error}`. Errors: 404 (campaign, phone number or campaign customer not found), 503 (voice agent not ready / call server unreachable).

* There is **no agentId field**: the agent is resolved from the caller number's outbound config or the campaign. The guide's example (`customerPhoneNumber`, `agentId`) is stale; the spec wins.
* We have **0 phone numbers**, so a real outbound PSTN call is not possible from our workspace right now.

### 8.2 Campaigns: `POST /campaign` (createCampaign)

Body:

| field | type | default / notes |
|---|---|---|
| `name`* | string 1..100 | |
| `assistantId` | uuid | or `assistantName` |
| `description` | string | |
| `startDate` | date | omit for immediate start |
| `startTime` / `endTime` | `HH:MM` | wall-clock times in `timezone` |
| `timezone` | IANA | `UTC` |
| `activeDays` | int[] | `[0..6]`, 0=Monday |
| `daySchedules` | `{ "<dayIdx>": {startTime, endTime} }` | sparse per-day overrides |
| `phoneNumber` | string | caller ID |
| `phoneChannel` | `sip`/`whatsapp` | |
| `callRetries` | 0..10 | 0 |
| `retryDelaySeconds` | 60..86400 | 300 |
| `retryDelayMode` | `fixed`/`next_day` | `fixed` |
| `retryConditions` | enum[] | default `["No Answer","Busy","Timeout","Unreachable","Network Error","Resource Unavailable","Service Unavailable","voicemail"]` |
| `maxConcurrency` | 1..50 | 5 |
| `webhookUrl` | uri | |
| `customers` | `[{phoneNumber*, properties: object\|null}]` | `[]` |
| `phoneList` | string[] | alternative to `customers` |

Spec example: `customers:[{phoneNumber:"+14155559001", properties:{name:"John Doe", company:"Acme Inc"}}]`.

* Guide: "If your agent uses variables in its system prompt (like `{{customer_name}}`...), those variables appear as columns in the customer table". So properties are the template variables.
* Response: `{status, isScheduled, campaign{... status: draft|scheduled|active|completed|partially_completed, customers, callIds, events[{event,timestamp,metadata,message,data}], version v1|v2 ...}, message}`.
* `GET /campaign/{campaignId}/customers` returns `customers[{id, phoneNumber, properties, retryStatus{status: pending|in_progress|retrying|waiting_capacity|rescheduled|human_callback_requested|skipped|success|failed|canceled|null, attempts, lastAttemptAt, nextRetryAt, capacityDeferrals}, callCount, successfulCallId, createdAt}]`, plus `propertyKeys[]`, `pagination`, `statusCounts`. Note `rescheduled` and `human_callback_requested`: the platform has native callback/reschedule semantics (the `reschedule_call` tool), which is prior art for callback scheduling.
* **`POST /campaign/poc` does NOT exist** in the prod or staging spec (checked: no path contains "poc"). Other campaign endpoints: `GET /campaign`, `DELETE /campaign`, `GET|PATCH|DELETE /campaign/{id}`, `POST /campaign/jobs/cancel-by-phone`.

### 8.3 Text and simulation channels (how graders most likely drive "simulated customers")

* `POST /chat/message` body:
  * `{callId?, agentId?, workflowId?, message* (1..10000), customerVariables?: object, versionId?, fromMessageId?}`
  * Omit `callId` and pass `agentId`/`workflowId` to start a call. Returns `{response, callId, messageId, warning, endedByAgent}`.
  * 422 means "customerVariables too large".
* `POST /chat/end {callId*}` returns `{callId, ended:true, endedReason, alreadyEnded}`. It "fire[s] the end-of-call lifecycle (webhook + post-conversation analysis + end-of-call tools)".
* `POST /chat/fork {parentCallId*, fromMessageId*, versionId?, agentId?, workflowId?}`.
* `POST /simulations`:
  * Body: `{name*, agentId*, mode: voice|chat (default voice), selectedPersonaIds* (1..1000), customerVariablesMap: {personaId: {var: string}}, assertions[uuid], runsPerPersona 1..50, maxConcurrentCalls, maxCallDurationSeconds 60..3600 (default 240), maxRetries (default 3), agentVersionId, start, *LlmOverride...}`.
  * Personas keep `simulationVariables` (most recently used values per agentId).
  * Simulation calls appear in `GET /call` with `simulationId` set.
  * `GET /simulations/version-persona-runs?entityVersionId` gives `runs[{personaId, personaName, simulationId, status: passed|failed|running|not_run, runId, latestAt}]`.
* Agent `config.testVariables` (string map) and workflow `test_variables` (live `{}`) supply default values for dashboard test calls.
* Conclusion (**INFERRED**): the grader passes `customer_id`, `title`, `last_name` (maybe more) as **customer variables / properties**. Our generated prompts must reference them only as `{{customer_id}}`, `{{title}}`, `{{last_name}}`, and must never hard-code values. Money figures must come from `get_account` results, never from variables, unless the brief says so.

---

## 9. Analytics and credits (spend monitoring)

| endpoint | params | result (live value) |
|---|---|---|
| `GET /workspaces/{workspaceId}/credits` | path workspaceId | `{creditsLeft: 20, creditUsdRate: 0.5, billingUnit: "usd", minuteLimit: null, minutePeriodStart: null}`, i.e. **$10** left (20 credits x 0.5) |
| `GET /analytics/credits` | `workspaceId*`, `startDate`, `endDate` (ISO) | `{creditsData:{dailyCredits:{date:...}, totalTransactions: 1, totalCreditsUsed: 0, dailyCostByCategory:{}}}` |
| `GET /analytics` | `workspaceId*`, `startDate`, `endDate`, `phoneNumberId`, `assistantId`, `assistantIds[]`, `granularity` (`hour/day/month`), `metrics[]` (`summary,timeSeries,endedReasons,topCalls,geoDistribution,callThemes,sentimentDistribution`), `status[]` (`successful,failed,transferred`), `sentiment[]`, `minDurationSec`, `maxDurationSec`, `callType[]`, `insightFilters`, `durationRanges` (`[{min?,max?}]` ORed), `campaignIds[]` (<=20), `endBefore` | `summary{totalCalls, answeredCalls, voicemailCalls, totalDurationSec, averageDurationSec, answerRate, conversedCalls}`, `timeSeries[{date,totalCalls,answeredCalls,averageDurationSec,totalDurationSec,sentimentCounts{very-unhappy,unhappy,neutral,happy,very-happy}}]`, `endedReasons[{reason,count}]`, `topCalls[{callId,customerNumber,durationSec,startedAt,status,endedReason}]`, `geoDistribution`, `callThemes[{theme,count}]`, `sentimentDistribution[{bucket,count}]`, `generatedAt`. Live: all zeros, default 30-day window. |
| `GET /analytics/structured-data` | section 4.4 | section 4.4 |
| `GET /analytics/properties` | `workspaceId*` | property catalog (section 4.4) |
| `GET /analytics/widgets` | `workspaceId*` | live `{widgets:[]}` |

Per-call cost: `call.cost` (number|null; unit not stated). `GET /call?sortBy=cost`. The analytics property `call.costMicros` (micro-units, **INFERRED** USD x 1e6).

**Spend guard (recommended):** before any batch of our own test calls, read `creditsLeft` and abort if `creditsLeft * creditUsdRate < threshold`.

---

## 10. Recommended post-check pipeline (validator side)

1. **Collect:** `GET /call?workspaceId&limit=100&offset=..&startDate=<run start>&archivedFilter=all` and filter rows by `assistantId == ourAgentId` (and/or `simulationId`). Then `GET /call/{id}` per call. Wait until `status` is not `live`/`processing` and `summary` is present (poll with backoff; analysis is async).
2. **Transcript normalisation:** prefer `messages[]` (ordered by `secondsFromStart`); fall back to `transcript`. Classify roles defensively:
   * agent: `{"assistant","bot","agent","ai"}`
   * customer: `{"user","customer","human"}`
   * tool: anything containing `"tool"` or `"function"`
   * skip: `system` / unknown
   All role strings are **INFERRED** until verified on one real call. Ignore `blockedContent`; use `message` (or `content`).
3. **Money-consistency check:**
   * Extract currency amounts from **agent** turns. Regex for `£/$/€/TL/₺` plus number, or number plus currency word, digits with `,`/`.` grouping, and also spelled-out numbers (TTS-friendly prompts may write "two hundred fifty pounds").
   * Build the allowed set from: (a) numbers in the brief; (b) numbers inside tool-result messages (`get_account` JSON), if present in `messages[]`; (c) if tool results are not in `messages[]`, re-derive them by calling the mock bank `get_account` yourself. **Not allowed under current safety rules (POST)**, so (c) is a design question for the user.
   * Flag any agent amount not in the allowed set or not equal to a simple sum of allowed amounts.
   * Note: the TTS `textSubstitutions` map `\$` to `dollars` and `%` to `percent` in the **spoken audio** (live config). **INFERRED**: the transcript keeps the LLM text with the symbol.
4. **Gate checks from transcript order:**
   * right-party question before any account detail
   * `verify_identity` success before `get_account`
   * read-back of amount + date followed by customer "yes" before `create_promise`
   * tool success: no `GET /alerts/events?callId=..&sources=tool` errors, and tool-result content OK
   * closing phrase ("Have a good day.") then end
5. **Optional enrichment (writes, need approval):** tag calls (`PATCH /call/{id}/tags`), or create `analysis` insights so the platform extracts flags for us.

---

## 11. Gaps and things to verify with one real call

* `messages[].role` values; whether tool calls/results and `nodeId`/node names appear in `GET /call/{id}`.
* The key format of `call.insights` (insight id vs name) and the value types for `checkbox`/`enum`/`array`.
* `toolFieldPath` semantics (args vs response; dotted path syntax).
* Units of `call.duration` and `call.cost`.
* The `filters` base64 schema; the `insightFilters` `op` vocabulary.
* `workspaceSettings.webhooks` key names.
* Whether workflow agents take `postConversationAnalysis` from `agent.config` or from `workflow.agent_config`. They differ live: agent = openai gpt-4o / deepgram en / cartesia en; workflow agent_config = experimental-5 / Freya STT `tr` / Freya TTS `leyla` `tr`. That is a language risk for English calls; flag it to the workflow-config researcher.
