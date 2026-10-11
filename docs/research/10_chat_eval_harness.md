# 07 / gap 4: the chat API as eval harness (version targeting, real tools, observability, cost)

> Status: pre-kit research note (10 October 2026), kept for background. Where it disagrees with the kit ([research/11](11_hackathon_rules_and_kit.md), `docs/api.md`, `docs/workflow-format.md`) or with the code, those win.

Sources: `docs/openapi_staging.json` (same 96 paths as prod) **[SPEC]**; the dashboard i18n table `research/tools/i18n.json` **[UI]**; the dashboard route map in `webapp/js/38t45boimpwp0.js` **[FE]**; GET-only probes run this session against staging **[LIVE]**, with raw outputs in `research/live07_gap4/*.txt`. Anything not stated by a source is marked **INFERRED**.

No POST/PUT/PATCH/DELETE was sent to Freya, and nothing was sent to the mock bank.

---

## 0. Verdict

| Question | Status | Answer |
|---|---|---|
| Which version does `/chat/message` run when `versionId` is omitted? | **NOT resolvable read-only** | The spec is silent. The platform convention points to the **published ("active") version** (moderate confidence, INFERRED). A head or working-copy default is not ruled out. **This does not matter if the harness always pins `versionId`**, which it should. Section 6, Part B, gives a one-turn experiment that settles it. |
| Do `api_call` tools really execute during chat? | **Strongly INFERRED yes; not proven** | The same pipecat runtime serves chat. `/chat/end` fires "end-of-call tools". The unit-test editor says "Tool rules observe real tool calls; results are not mocked." Plan for real POSTs to `freya-bank-mock` on every chat run. |
| Where do tool args/results and node transitions appear afterwards? | **Partly resolved** | Field names are known; role strings are not (see section 3). Tool results also land in workflow variables `<tool>_response`. Failed tools appear in `/alerts/events` (identifiers only, no payloads). `/call/{id}/logs` is closed to our key. |
| Does chat cost credits? | **NOT resolvable read-only, but bounded** | The "chat is free" copy applies only to **minute-contract** workspaces. Ours is USD pay-as-you-go (`minuteLimit: null`, `billingUnit: "usd"`) **[LIVE]**, so chat can be billed. However, the only chat LLM available (`experimental-5`) and Freya STT/TTS publish **no price** (`pricing: {displayPrice: ""}`) **[LIVE]**, and unpriced events "are excluded from the total" **[UI]**. Expected cost per chat ≈ $0 plus any platform fee (INFERRED). One measured run settles it. |

---

## 1. Version targeting

### 1.1 What the spec says
* `POST /chat/message` body: `callId?`, `agentId?`, `workflowId?`, `message` (required, 1..10000), `customerVariables?`, `versionId?` (uuid), `fromMessageId?`. **`versionId` has no description and no default.** The response has no version field: `{response, callId, messageId, warning?, endedByAgent?}`.
* `POST /chat/fork` returns `entityVersionId: uuid|null`. Its description says it "retargets a different `versionId`, `agentId`, or `workflowId`". The value is nullable, so a chat call can exist **without** a version. Example: the wrapper agent `2f8dcbea-…` has 0 versions (`live2/versions_agent.json`) **[LIVE]**.
* `GET /call/{callId}` returns `entityVersionId: uuid|null` and `entityVersionNumber: number|null`. This is the only after-the-fact proof of which version ran.
* `POST /call` body field `entityVersionId`: "Version ID of the entity (agent/workflow) **active** during the call" **[SPEC]**.

### 1.2 Evidence for "default = published/active"
* `PUT /agents/{agentId}/insights`: "saves the result as a new, unpublished version. **Calls use the new set once that version is published.**" **[SPEC]**
* `versionSelector.publishedEntryHint` = "The version live calls use" **[UI]**. `agents.versionHistory.publishConfirmDescription` = "Publishing v{version} will make it the active version served to customers." **[UI]**
* Permissions `workflows:sync` / `agents:sync` = "Publish workflows: **Push workflow updates to the runtime**" **[LIVE /permissions]**, plus FE routes `WORKFLOW_SYNC: /api/v2/workflow/[workflowId]/sync` and `AGENT_SYNC` **[FE]**. Publishing pushes the config to the runtime (pipecat), and `/chat/*` is served by that same runtime ("Agent service" 502/503/504; "Tells pipecat to…").
* Simulations: `POST /simulations/{id}/rerun` has **no request body** **[SPEC]**, and `simulations.simulationDetail.rerunDisabledTooltip` = "Publish a new agent version to rerun this simulation" **[UI]**. `GET /simulations/{id}` returns `publishedVersionId` **[SPEC]**. Unpinned simulation runs use the published version.

### 1.3 Evidence for "default = head / working copy"
* `POST /versions/{entityType}/{entityId}`: "applied to the **working copy**" **[SPEC]**. The working copy is the row returned by `GET /workflow/{id}` / `GET /agents/{id}`.
* `versionSelector.latestEntryHint` = "Follows the newest saved version" **[UI]**, and the simulation new-iteration modal offers `versionLatest` = "latest" next to `versionPublished` = "(published)" **[UI]**. So "latest/head" is a selectable target in the UI, but there it is an explicit choice, not the default.
* `GET /unit-tests/overview` aggregates "against its entity's **mainline head** version" **[SPEC]**. That is a reporting default, not the runtime default.

### 1.4 Decision for the builder (independent of the open question)
1. **Always pin.** On the first turn send `{workflowId, versionId, message, customerVariables}`. For workflow-type agents, versions belong to the **workflow** entity (`entityType: "workflow"`, `entityId` = workflowId), so `versionId` is a workflow version id, e.g. base v1 `171f4844-dd70-4dc0-8924-5a8fd0ff2d2b` **[LIVE]**.
2. **Do not send `versionId` on later turns** (behaviour with `callId` + `versionId` is undocumented). To move a conversation onto another version, use `POST /chat/fork {parentCallId, fromMessageId, versionId}`.
3. **Assert after `/chat/end`:** `GET /call/{id}` → `call.entityVersionId == versionId`. On a mismatch, mark the run INVALID and do not score it.
4. **Publish the final version.** Graded runs ("we run that agent against simulated customers") will most likely be simulations or calls with no version pin, and those use the published version (section 1.2).
5. Draft-vs-published risk: if the builder writes with `POST /versions` and the result has `isPublished:false`, an **unpinned** test may run the old published graph. Pinning removes this risk entirely.

---

## 2. Do tools really fire during chat?

Evidence (none of it is a direct statement for `/chat/message`):
* `POST /chat/end`: "Tells pipecat to fire the end-of-call lifecycle (webhook + post-conversation analysis + **end-of-call tools**)" **[SPEC]**. In the base workflow the end-of-call tool is `close_call` (`config.alwaysRunsAt: ["end_of_call"]`, `handler: "__function__:api_call"`, POST `https://freya-bank-mock.freyavoice.workers.dev/v1/close_call`, body `{outcome:"{{outcome}}", reason:"{{reason}}", customer_id:"{{customer_id}}"}`) **[LIVE workflow]**.
* `unitTests.editor.realToolNote` = "Tool rules observe real tool calls; results are not mocked." **[UI]** Unit tests replay one turn on the same runtime. Their only opt-out is `mockToolResults` on `POST /unit-tests/run-all` **[SPEC]**. `/chat/message` has no such field.
* Workflow transitions read tool results (`verify_identity_response.data.all_verified`) **[LIVE workflow]**. A chat run could not leave the `identity` node unless the tool really executed.
* `calls.table.turnMetrics.stageDesc.api` = "External API / tool call made by a workflow node, blocking the reply while it runs." **[UI]**

Conclusion: **INFERRED (high confidence)**. Every chat run makes real POSTs to the mock bank: `verify_identity` per answer; `get_account` and `create_promise` in `collection`; `close_call` at `/chat/end`.
* **Side-effect rule.** Use a customer_id the mock does **not** know for observability probes. The mock rejects it with `{"ok":false,"error":"unknown customer_id"}`, so no grading state is touched. Use organizer-supplied test ids only once the organizers confirm the mock is stateless or resettable, because `close_call` with a valid id may record an outcome.

---

## 3. Observability: what can be read after a chat call

| Surface | Status | What it gives |
|---|---|---|
| `GET /call/{callId}?workspaceId=` → `call.messages[]` | [SPEC] shape, roles **unknown** | Items: `role` (required, **string with no enum**), `message`, `duration`, `secondsFromStart`, `timestamp`, `endTime`, `messageId`, `wasBlocked`/`was_blocked`, `blockedContent`/`blocked_content`. The unit-test snapshot schema, which is copied from a call (`seedCallId`, `seedMessageId`; [UI] "Snapshots the conversation up to the user message"), adds `content`, `nodeId: string\|null` and `workflowSnapshot`, with `additionalProperties: {}`. Expect those on call messages too (INFERRED). |
| Tool-call/result items inside `messages[]` | [UI] strong hint | `workflows.nodeForm.suppressToolResultsTooltip`: "tool call and result messages are replaced with compact system messages before sending context to the LLM". The stored conversation therefore has separate **tool-call**, **tool-result** and **system** items. The transcript UI renders them as `calls.functionCallStatus.*`: Calling/Complete/Failed/Deferred/Compacted, Arguments, Code, Result, Error, "Entering" node, "Transitioning to", From node/To node, Extracted, Route evaluated, Tool call routing, Arg mismatch ("Tool args didn't match the config's template variables"). Role strings are **unknown**. By analogy with the Vapi-style schema these fields copy, likely candidates are `user`, `assistant`/`bot`, `system`, `tool_calls`, `tool_call_result` (INFERRED only). |
| "Copy transcript → Messages only / With events" | [UI] | Confirms events are stored next to the messages. |
| Detailed transcript export | [UI] `calls.export.fullTranscriptDescription` | "Complete transcript including API calls, node transitions, variable extractions, and system events". The route is `POST /export` (a write; queues a job). |
| Variables tab | [UI] `calls.variables.*` | "written by {node}", confidence, rejected values. "Dates, prompt sections and routing scratch are removed before a call is stored". Tool outputs are variables named `<tool>_response` ([UI] `workflows.variables.class.tool.body`: "A tool writes this when it runs"). The JSON location is probably `messages[].workflowSnapshot` or `artifact` (INFERRED). |
| `call.artifact`, `call.analysis` | [SPEC] untyped (`{}`) | Unknown; dump on first real call. |
| `call.insights` | [SPEC] | Opt-in, needs a write. Insight `source: "tool_field"` ([UI] "the value is exactly what was sent, with no analysis call") or `workflow_variable` ([UI] "Save as insight: at the end of each call, the final value is saved"). This is the cleanest way to get the exact `close_call.outcome` / `create_promise` args into a typed field. |
| `GET /alerts/events?callId=&sources=tool` | [SPEC] + [LIVE] works with our key (0 events) | `{source ∈ llm,tts,stt,tool,latency,telephony,webhook,knowledgebase,system; severity ∈ info,warning,error,critical; code; message; callId; agentId; metadata; occurredAt}`. **Failures only, no payloads.** The ingest spec says "Bodies must carry identifiers and measurements, not transcripts", and metadata is capped at 8192 bytes. A mock reply of HTTP 200 `{"ok":false}` may not count as a failure (INFERRED). The default "Tool failure rate" monitor is enabled ([LIVE] `live03/alert_rules.json`). |
| `GET /call/{id}/cost-events` | [FE] `CALL_COST_EVENTS_GET`; [LIVE] 404 "Call not found" for an unknown id, with or without `workspaceId` (route reachable with our key) | Per-event cost lines. [UI] `calls.costPanel.*` categories: `llm`, `tts`, `stt`, `workflow`, `analysis`, `telephony`, `fee`. Sources: `agent`, `persona` ("Simulation"), `system` ("Platform"). An "unpriced" flag: "events had no model pricing configured and are excluded from the total". The exact JSON shape is unknown. |
| `GET /call/{id}/metrics/tables` | [FE]; [LIVE] 404 for an unknown id (reachable) | Per-turn latency stages, including `api` = "Tool call" (timing only; INFERRED shape). |
| `GET /call/{id}/logs` | [FE]; [LIVE] **403 Forbidden** even for an unknown id, while sibling routes return 404 | Permission-gated before lookup. The raw-export spec says logs/traces are "management only", so **unavailable to our key**. |
| Real-time tool events | [docs] Embed SDK only (`toolCallStarted`, `toolCallInProgress {function_name, tool_call_id, arguments}`, `toolCallStopped`) | Needs WebRTC + `calls:web-token`; not practical for the harness. |

**Parser rule until verified.** Classify each message by keys, not only by `role`:
* `role` ∈ {user, customer, human} → customer
* `role` ∈ {assistant, bot, agent} → agent
* anything else, or items that carry tool-ish keys (`toolCalls`, `tool_calls`, `name`, `arguments`, `result`, `function`) or whose role contains "tool"/"function"/"system" → event

Keep the raw JSON for the judge, and also search `json.dumps(call)` for tool names (`verify_identity`, `create_promise`, `close_call`) as a coarse tool-fired signal.

---

## 4. Cost

* `GET /workspaces/{id}/credits` **[LIVE, this session]**: `{"creditsLeft":20,"creditUsdRate":0.5,"billingUnit":"usd","minuteLimit":null,"minutePeriodStart":null}` ($10, unchanged from earlier).
* `GET /analytics/credits` **[LIVE]**: `totalTransactions: 1, totalCreditsUsed: 0, dailyCostByCategory: {}` (only the initial grant).
* "1 credit = 1 phone minute. Web calls, chat and simulations are free." is `workspace.billing.minutesHint`, shown under `workspace.billing.minutesTitle` = "Minute contract" **[UI]**. Our `minuteLimit` is `null`, so **that promise does not cover this workspace** (INFERRED from the copy's placement).
* `GET /models` (undocumented, [FE] `MODELS_GET`) **[LIVE]**:
  * The only `chat` model offered is `experimental-5` ("Freya YC LLM", contextWindow 65536, `analysisCapable: true`) with `pricing: {"displayPrice": ""}`. `freya-tts`, `freya-stt` and `freya-stt-continuous` also have empty pricing.
  * Third-party models carry numbers: cartesia `{"audioOutput": 31}`, deepgram nova-3 `{"perMinute": 0.0043}`, openai gpt-4o-mini-tts `{"input": 0.6, "audioOutput": 12}`.
  * The base workflow uses `experimental-5` for the LLM, with `postConversationAnalysis.analysisModel: null`.
* Combined with [UI] "events had no model pricing configured and are excluded from the total", **a text chat on the base stack most likely costs ≈ $0 apart from any `fee` (platform fee) line** (INFERRED). Earlier notes (03/04/06) that assumed chat burns credits are plausible but probably overstated. Measure once with credits before/after plus `cost-events`.
* Spend guard for the loop: read credits before each batch and abort if `creditsLeft*creditUsdRate` < $2 or if the per-conversation delta exceeds a threshold (for example $0.05).

---

## 5. GET probes run in this session (raw files in `research/live07_gap4/`)

| Probe | Result |
|---|---|
| `GET /workspaces/{ws}/credits` | 200, `creditsLeft:20`, `billingUnit:"usd"`, `minuteLimit:null` |
| `GET /analytics/credits?workspaceId=` | 200, `totalTransactions:1`, `totalCreditsUsed:0` |
| `GET /call?workspaceId=&limit=5` | 200, `total:0` (no calls exist, so there is nothing to inspect read-only) |
| `GET /alerts/events?limit=5` (with and without `sources=tool`) | 200, `events:[]` |
| `GET /call/{unknown}/cost-events` (with and without `workspaceId`) | 404 `"Call not found"` (route reachable) |
| `GET /call/{unknown}/metrics/tables` | 404 `"Call not found"` |
| `GET /call/{unknown}` | 404 `"Call not found"` |
| `GET /call/{unknown}/logs` | **403 `"Forbidden"`** (gated before lookup) |
| `GET /unit-tests/overview`, `/unit-tests/version-runs`, `/simulations/version-checks?entityVersionId=v1` | 200, all empty |
| `GET /workspaces/logs` | 4 events: SIGNUP, WORKFLOW_CREATED (`userAgent: "Mozilla/5.0 freya-cli"`), AGENT_DELETED, API_KEY_CREATED. No chat or version events |
| `GET /models` | 200; pricing as in section 4 |

Note: Cloudflare returns `403 error 1010` to Python's default `Python-urllib` User-Agent. Send a normal UA (for example `curl/8.9.1`) from the builder's HTTP client. `httpx`/`requests` defaults may also be blocked (UNVERIFIED), so set the UA explicitly.

---

## 6. Minimal safe experiment (needs user approval: it POSTs to Freya and makes Freya POST to the mock)

Script (written, **not run**): `research/tools07/gap4_chat_probe.py` (refuses to run without `--approved`).

**Part A: observability, cost and pinning (base workflow, unknown customer_id, ~4 turns)**
1. `GET /workspaces/{ws}/credits` (baseline). `GET /versions/workflow/252ee2df-…?entityId=…&workspaceId=…` → v1 `171f4844-…` (published).
2. `POST /chat/message`:
   ```json
   {"workflowId":"252ee2df-4066-4b87-8fe6-bfecbab03505","versionId":"171f4844-dd70-4dc0-8924-5a8fd0ff2d2b","message":"Hello?","customerVariables":{"customer_id":"gap4-probe-unknown-0001","title":"Mr","last_name":"Probe","now":"<iso>"}}
   ```
   Record whether `response` is the `on_enter` greeting, and record `warning`.
3. Send turns with `{callId, message}` only: "Yes, speaking." → "My date of birth is 1 January 1980." → "The last four digits are 1234." Stop early on `endedByAgent:true`. The identity node should call `verify_identity`, get `{"ok":false,"error":"unknown customer_id"}`, say goodbye ending with "Have a good day.", and the trigger pattern should end the call.
4. `POST /chat/end {callId}`. This fires `close_call` with the unknown id, which the mock rejects.
5. Poll `GET /call/{callId}?workspaceId=` (5 s interval, ≤ 2 min) until `summary`/`analysis` appear. Then fetch `GET /call/{id}/cost-events`, `GET /call/{id}/metrics/tables`, `GET /alerts/events?callId=…` (with and without `sources=tool`), credits again, and `/analytics/credits`.
6. Record:
   * the distinct `messages[].role` strings and the key set per role
   * where `verify_identity`'s args (`field`, `answer`, `customer_id`) and result appear
   * whether `nodeId` and `workflowSnapshot` are present
   * `artifact` and `analysis` keys
   * `entityVersionId == 171f4844-…`
   * `modality` (expect `"text"`), `channel`, `type`, `endedReason`, `cost`
   * the cost-event lines (priced vs unpriced)
   * the credits delta
   * whether the `{"ok":false}` tool reply produced an alert event

Side effects:
* 1 call record in the workspace.
* About 2 `verify_identity` POSTs and 1 `close_call` POST to the mock, all with an unknown id.
* Expected cost ≈ $0–0.05 (INFERRED).

**Part B: settles the unpinned default (one turn; run only on a builder-owned workflow, never on the base)**
Precondition: the workflow's newest version is **not** published (for example right after the builder's first `POST /versions`, if the response shows `isPublished:false`).
1. Send one **unpinned** `POST /chat/message {workflowId, message:"Hello?", customerVariables}`.
2. `POST /chat/end`.
3. `GET /call/{id}` and compare `entityVersionId` with the published id and the head id. Run: `gap4_chat_probe.py --approved --unpinned-check --workflow-id <id>`.

A text marker is not needed; `entityVersionId` decides it.

**Success-path shapes** (`get_account`, `create_promise`, `close_call outcome=promise`): rerun Part A with an organizer-supplied test customer_id, but only after they confirm that calling the mock with it does not consume or alter grading state.

---

## 7. What the harness should implement now (no further research needed)

1. Pin `versionId` on turn 1, use fork to retarget, and assert `entityVersionId` after the call (section 1.4).
2. Always call `POST /chat/end`, then poll `GET /call/{id}` with a timeout. Analysis is async (`/chat/end` [SPEC]).
3. Use a role-agnostic message classifier and store the raw call JSON (section 3).
4. Tool-success gate signals, in order of reliability:
   1. tool-result events in `messages[]` (once the role strings are known)
   2. `<tool>_response` in `workflowSnapshot` or the variables
   3. `alerts/events?callId=&sources=tool` for hard failures
   4. optional `tool_field` insights for `close_call`/`create_promise` args
5. A credits guard around every batch (section 4).
6. Set an explicit User-Agent on every Freya request (section 5 note).
7. Publish the final version.

## 8. Still open after this report
* `messages[].role` enum and the tool-item key names (Part A).
* The unpinned `/chat/message` default (Part B; moot if always pinned).
* The real per-chat cost and whether a `fee` line applies (Part A).
* Whether `versionId` sent together with `callId` is ignored, rejected or retargets (not needed if the harness follows section 1.4).
* Whether a mock reply of HTTP 200 `{"ok":false}` raises an `alerts` tool event (Part A).
