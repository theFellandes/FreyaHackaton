# 01 — Freya agent / workflow configuration reference (create + update programmatically)

> Status: pre-kit research note (10 October 2026), kept for background. Where it disagrees with the kit ([research/11](11_hackathon_rules_and_kit.md), `docs/api.md`, `docs/workflow-format.md`) or with the code, those win.

Sources (all read-only):
- `docs/openapi_staging.json` (the spec served by https://staging-app.freyavoice.ai/api/openapi.json, 96 paths). It is identical to `openapi_prod.json` for every path in this document except that staging adds `config.backgroundSoundVolume` (number|null, 0..2) to agent configs. **Use the staging spec**, because our key only works on staging.
- `docs/llms-full.txt` guides. They are marketing-level and contain **no** workflow node schema. They do give the embed SDK `customerVariables` semantics.
- Live GETs against staging (saved in `scratchpad/live/` and `scratchpad/live2/`): base workflow, base agent, `/agents/defaults`, `/versions/workflow/...` (incl. the full `configSnapshot`), `/versions/.../graph`, `/branches`, `/custom/abbreviations`, `/permissions`.
- Labels: **[SPEC]** = stated in the OpenAPI spec. **[DOCS]** = stated in the docs. **[LIVE]** = observed in a live GET response. **[INFERRED]** = my deduction, not stated anywhere. Treat INFERRED items as hypotheses until a write call confirms them.

IDs in our workspace (staging):
| thing | id |
|---|---|
| workspaceId | `01e07b69-486a-4070-a7fd-3a2fc3120bf2` |
| base workflow id | `252ee2df-4066-4b87-8fe6-bfecbab03505` (name "Freya Bank Collections Base (2 nodes)", record `version` "2.0.0") |
| base workflow's agent id | `2f8dcbea-fad5-4c09-ae0b-35cff67e9a14` (entityType "workflow") |
| base workflow version 1 | `171f4844-dd70-4dc0-8924-5a8fd0ff2d2b` (`isPublished: true`, `publishedAt: null`) |

The base workflow was created at 09:53:27.056Z, one second after the workspace (09:53:26.170Z), and its `createdBy` is the workspace owner. **[INFERRED]** The hackathon platform seeds every new workspace with this base workflow, so the graders' workspace probably has one too. The builder should therefore *find it by name* (`GET /workflow?search=Collections Base`) rather than hard-code ids.

---

## 0. Transport conventions

- Base URL: `https://staging-app.freyavoice.ai/api/v2`. Header: `Authorization: Bearer <FREYA_KEY>`. Security scheme: `bearerAuth` (http bearer) **[SPEC]**.
- Envelope **[SPEC]**: success `{"success": true, "data": {"message": "Success", "result": {...}}}`. Error `{"success": false, "data": {"message": "<error>", "result": null | {...}}}`. Example **[LIVE]**: `{"success":false,"data":{"message":"No agent or workflow found with this id","result":{"error":"No agent or workflow found with this id"}}}`.
- `workspaceId`:
  - It is a **required query param** on most GETs and on `DELETE /agents` **[SPEC]**.
  - It is a **required body field** on `PUT /agents/{agentId}`, `POST /versions/...` and `POST /unit-tests` **[SPEC]**.
  - `POST /agents` has **no** workspaceId in its schema, but the spec's own request **example puts `workspaceId` in the body** **[SPEC]**.
  - `/branches*` and `/custom/abbreviations` take no workspaceId at all **[SPEC]**.
  - `GET /agents/{agentId}` succeeded **without** `workspaceId` even though the spec marks it required, and so did `GET /custom/abbreviations?id=...` **[LIVE]**.
  - **[INFERRED]** The API key is bound to the workspace and the server resolves it from the token. Recommendation: always send `workspaceId` both as a query param and, on POST/PUT, in the JSON body. Extra keys are harmless; a missing one can be fatal.
- Common error codes on agent writes **[SPEC]**: `400` validation, `401` bad token, `403` "Workspace ID mismatch or insufficient permissions", `404`, `409` "An agent with this name already exists" (**names are unique per workspace**), `422` "The config selects a model that is outside its allowed hours" (some LLM models are time-gated).
- Owner permissions (live `/permissions`): `agents:write` ("Create, update, and configure agents"), `agents:sync` ("Publish agents: Push agent configuration changes to the runtime"), `workflows:write` ("Create and edit workflows"), `workflows:sync` ("Publish workflows: Push workflow updates to the runtime"), `workflows:test`, `calls:import`, `calls:chat-message`.

---

## 1. Agent endpoints

### 1.1 `GET /agents` (listAgents) **[SPEC]**
Query parameters:
| name | type | notes |
|---|---|---|
| `workspaceId` | uuid | required |
| `limit` | int | 1..100, default 50 |
| `offset` | int | default 0 |
| `search` | string | |
| `sortBy` | string | |
| `sortOrder` | `asc` \| `desc` | |
| `entityType` | `workflow` \| `agent` \| `all` | default `all`. "'workflow' for workflow-agents, 'agent' for regular agents" |

Each list item has `id`, `name`, `entityType` (`"workflow"` | null), `workflowId` (uuid|null), `isImported`, `createdAt`, `updatedAt`. The result also carries `total`, `limit` and `offset`.

### 1.2 `GET /agents/{agentId}` (getAgent)
Takes `agentId` (path) and `workspaceId` (query, required per spec, but not enforced live). It returns `result.agent`:

```
{ id, workspaceId, userId|null, name, entityType: "workflow"|null, workflowId|null,
  config: <AgentConfig>, isImported (default false), createdAt, updatedAt }
```

### 1.3 `GET /agents/defaults` (getAgentDefaults)
Takes `workspaceId` (required) and `language` (optional, ≤10 chars). It returns `result.config` = the platform default AgentConfig, described as "used by the editor's reset-to-default controls".

**[LIVE]** `language=en` returned the *same* body as no language. The defaults are **Turkish**:
- llm: `provider` / `model` `"experimental-5"`, firstMessage `"Selam! Sana nasıl yardımcı olabilirim?"`.
- stt: `Freya` / `freya-stt`, language `tr`.
- tts: `Freya` / `freya-tts`, voice `leyla`, language `tr`.
- Turkish interruption and acknowledgement phrases.

The defaults also include `"testVariables": {}`. **Do not use the defaults unchanged for an English agent.**

### 1.4 `POST /agents` (createAgent). Response `201 "Agent created"`.
Request body **[SPEC]**. Only `name` is required:
```jsonc
{
  "name": "string 1..100 (unique in workspace, else 409)",
  "language": "string ≤10 (optional)",
  "config": { /* AgentConfig (§3). If present, it REQUIRES llmConfig, ttsConfig, sttConfig, complianceConfig */ }
  // spec example also sends "workspaceId": "<uuid>" here
}
```
- The schema has **no `entityType`, no `workflowId`, no `workflow` field**, and no `additionalProperties:false` either, so unknown keys are probably silently stripped **[INFERRED]**.
- The response is `result.agent`, with the same shape as GET. For agents created this way, `entityType` will be `null` **[INFERRED from the enum `["workflow", null]`]**.
- Spec example body:
```json
{"workspaceId":"…","name":"Customer Support Agent","config":{
  "llmConfig":{"provider":"openai","model":"gpt-4o","systemPrompt":"You are a helpful customer support agent...","firstMessage":"Hello! How can I help you today?","firstMessageMode":"assistant-speaks-first","maxTokens":1000,"temperature":0.7},
  "ttsConfig":{"provider":"elevenlabs","model":"eleven_multilingual_v2","voiceId":"21m00Tcm4TlvDq8ikWAM","language":"en","additionalSettings":{}},
  "sttConfig":{"provider":"deepgram","model":"nova-2","language":"en","additionalSettings":{}},
  "complianceConfig":{"pciDss":false,"mifidIi":false,"euAiAct":false,"soc2":false}}}
```
(The example also puts `speed`, `stability`, `similarityBoost`, `temperature`, `punctuate` and `diarize` at the top level of tts/stt. Those are NOT schema fields; they belong in `additionalSettings`.)

### 1.5 `PUT /agents/{agentId}` (updateAgent): "Updates an existing agent's name, configuration, or files."
Request body **[SPEC]**:
```jsonc
{
  "agentId": "uuid (required, duplicate of path)",
  "workspaceId": "uuid (required)",
  "name": "string 1..100 (optional)",
  "config": { /* AgentConfig, but NOTHING required: partial allowed */ },
  "commitMessage": "string ≤200 (optional)",
  "commitDescription": "string ≤5000 (optional)"
}
```
- `commitMessage` / `commitDescription` imply that a PUT mints a **version** (a commit) **[INFERRED]**. The spec does not say whether that version is published.
- Partial config: the spec does not say whether it deep-merges or replaces subtrees **[UNKNOWN]**. Safest: GET the agent, mutate, and PUT back whole sub-objects.
- Response 200: `result.agent`.
- For a **workflow-type agent** this endpoint edits only the *agents-table* config. That config differs from what the workflow runs (§2.1), so **do not use PUT /agents to change a workflow** **[INFERRED, strongly evidenced]**.

### 1.6 `DELETE /agents` (deleteAgent): "Deletes an agent and all associated versions."
- Query: `agentId` (uuid, required), `workspaceId` (uuid, required). No body.
- Response: `result: {success: bool, message: string}`.
- **[UNKNOWN]** Whether deleting a workflow-type agent also deletes its workflow.

---

## 2. Workflow-type agents: can they be created or updated through the public API?

### 2.1 Facts
1. **There is no `POST/PUT/PATCH/DELETE /workflow`** in the spec. The only operations are `GET /workflow`, `GET /workflow/{workflowId}` and `GET /workflow/templates` **[SPEC]**.
2. In `GET /workflow/{id}`, the field `workflow.workflow` (the graph) is typed `{}`, i.e. unspecified **[SPEC]**. The full graph schema below is reverse-engineered from the live base workflow.
3. A workflow-type agent is two rows:
   - The agents row: `entityType:"workflow"`, `workflowId`, plus its own `config`. Live, that config was `openai/gpt-4o`, `cartesia sonic-3.6`, `deepgram nova-3`, all `en`.
   - The workflows row, whose `workflow.agent_config` is **different**: `experimental-5`, `Freya` tts `leyla` `tr`, `Freya` stt `tr`.

   **[INFERRED]** The runtime uses `workflow.agent_config`. The `/custom/abbreviations` docs confirm this: "on a workflow's **embedded agent config**". The agents-row config of a workflow agent is vestigial.
4. **`POST /versions/{entityType}/{entityId}` is the documented write path for workflows** **[SPEC]**. The spec says: "Creates a new version of an entity with the provided configuration snapshot. For entities with a live working copy (agent, workflow) the snapshot is validated against that entity's config schema and **applied to the working copy**, so an invalid snapshot is rejected with 422."
   - `entityType` ∈ {`agent`, `workflow`}.
   - For a workflow, `entityId` = the **workflowId**, not the agent id. Live, versions are listed under `/versions/workflow/252ee2df-…`, and `/versions/agent/2f8dcbea-…` is empty.
5. **[LIVE]** For a workflow, `configSnapshot` == `GET /workflow/{id}` → `result.workflow.workflow`, byte-for-byte. Its keys are `nodes`, `tools`, `agent_config`, `global_nodes`, `initial_node`, `system_prompt`, `test_variables`. It does NOT contain `name`, `description`, `nodePositions`, `canvasViewport` or `previewImage`; those are workflow-record metadata.
6. **Publishing**: versions carry `isPublished`, `publishedAt` and `publishedBy`. "Calls use the new set once that version is published" (from `PUT /agents/{agentId}/insights`, which "saves the result as a new, **unpublished** version") **[SPEC]**. So **calls run the published version**, and a write can produce an unpublished draft. Publish mechanisms in the spec:
   - `POST /branches/{branchId}/rebase` with `"publish": true`: "3-way merges the branch onto mainline, minting a mainline draft (Apply) or publishing it (Apply & Publish)" **[SPEC]**. This is the only *explicit* publish switch.
   - `PUT /versions/{entityType}/{entityId}/{versionId}`: "Updates version metadata". **No request-body schema in the spec.** The older v2 spec had `PUT /agents/{agentId}/versions/{versionId}`, "Activate agent version: Sets the specified version as the active version", also with no body. **[INFERRED]** The new PUT is the activate/publish call, possibly taking a body such as `{"isPublished": true}` and/or `{"name": "...", "description": "..."}`. Unverified.
   - Whether `POST /versions` publishes immediately is **[UNKNOWN]**. Check `result.version.isPublished` in its response.
   - Escape hatch: simulations (`POST /simulations` → `agentVersionId`), chat (`POST /chat/message` → `versionId`) and calls (`POST /call` → `entityVersionId`) can all **pin an explicit version** **[SPEC]**. That lets you test a draft even before you know how to publish it.
7. **Branches** (`/branches`, entityType ∈ `agent`|`workflow`|`persona`|`kb_document`) **[SPEC]**:
   - `POST /branches` (201) body:
     - required: `entityType`, `entityId` (uuid), `name` (1..200)
     - optional: `config` (object = full edited snapshot), `patch` (RFC-6902 array, minItems 1, `op` ∈ `add`|`remove`|`replace`|`test`, `path`, `value`), `description` (≤5000, "PR body"; `closes #N` links feedbacks), `baseVersionId` (uuid; default probably mainline head **[INFERRED]**), `closesFeedbackIds` (uuid[] ≤500).
     - Result: `{success, branchId, branchName, headVersionId, headVersionNumber, skippedFeedbackIds[], attached[], skipped[]}`.
   - `POST /branches/{branchId}/commit` (200) body: `config` | `patch`, plus `name` (≤200), `description` (≤5000) and `expectedHeadVersionId` (uuid, optimistic lock). Result: `{success, headVersionId, headVersionNumber, attached, skipped}`.
   - `POST /branches/{branchId}/rebase` ("Merge branch") body: `publish` (bool), `force` (bool), `resolutions` ([{`path`, `editedValue`}]).
     - Result: `{success, newVersionId|null, newVersionNumber|null, published, resolvedFeedbackIds[], conflicts[{path, liveValue, baseValue, branchValue, operation: "set"|"text_diff"}], checksState: "pass"|"fail"|"running"|"notRun", checks, error}`.
     - **409** is returned "with `checksState` and `checks` when the head's checks are failing, running, or **not run** unless `force` is true". **So a programmatic merge must send `force: true`**, because a new branch has no simulation/unit-test checks.
     - **409** is also returned with per-path conflicts if mainline drifted.
   - `POST /branches/{branchId}/discard` has no body. `PATCH /branches/{branchId}` updates `{name, description}`. `GET /branches?entityType=&entityId=&status=open|merged|discarded&workspaceId=` lists branches. `GET /branches/{id}` returns the merge view with a dry-run conflict preview. `GET /branches/{id}/activity` returns commits, simulations and unit-test runs.
8. **Creating a *new* workflow**: no documented endpoint. Candidates, best first:
   - (i) Reuse and overwrite the seeded base workflow (fully supported by #4 and #7).
   - (ii) Probe `POST /agents` with undocumented fields such as `"entityType":"workflow"` and a `"workflow"` graph. **[INFERRED: likely stripped → creates a plain agent]**. Check `result.agent.entityType` and `workflowId` in the response.
   - (iii) `POST /call/import` (multipart: `workspaceId`, `file` = ZIP). "Phase A inserts bundled workflows/agents/versions (ON CONFLICT DO NOTHING, flagged is_imported)". You would need a ZIP in the `/call/export-raw` format (calls.json, agents.json, workflows.json, versions.json, ...). You get one by exporting a real call, e.g. one created via `/chat/message`, then rewriting the ids. This is hacky; imported rows are flagged `isImported` and need permission `calls:import`. Use it only as a last resort.
   - (iv) The web dashboard ("Create Workflow" / "Copy" icon) uses internal routes that are not in the public spec. Do not use it; it requires a login.

### 2.2 Recommended call sequences

**(a) Create a new single-prompt agent**

```http
POST /agents?workspaceId=WS
{"workspaceId":"WS","name":"Collections Agent 2026-10-10T12:00Z","language":"en",
 "config":{
   "llmConfig":{"provider":"experimental-5","model":"experimental-5","systemPrompt":"…","firstMessage":"Hello, this is the digital assistant of Freya Bank. Am I speaking with {{title}} {{last_name}}?","firstMessageMode":"assistant-speaks-first","temperature":0.3,"maxTokens":1000,"tools":[ /* §5 tool objects */ ]},
   "ttsConfig":{"provider":"cartesia","model":"sonic-3.6","voiceId":"9287676d-f0cc-423f-ac03-3b3c7242f091","language":"en","textSubstitutions":[…],"speechNormalization":{…}},
   "sttConfig":{"provider":"deepgram","model":"nova-3","language":"en","additionalSettings":{"keyterms":"Freya","punctuate":true,"diarize":false,"krispEnabled":true,"aicEnabled":false}},
   "complianceConfig":{"pciDss":false,"mifidIi":false,"euAiAct":false,"soc2":false},
   "callTimeoutSettings":{…},"postConversationAnalysis":{…}}}
→ 201 result.agent.id  (409 if name exists → PUT /agents/{id} instead)
```

The cartesia/deepgram English values above are the ones observed on the live base agent row. The `experimental-5` values are the platform default.

Then, optionally:
- `GET /versions/agent/{agentId}?entityId=…&workspaceId=…` to see whether a version exists and whether it `isPublished`.
- If an update and publish is needed, use the branch route with `entityType:"agent"` (sequence (c)).

**(b) Clone / create a workflow agent from the base workflow JSON**

There is no create endpoint, so in practice "clone" means **take the base graph, transform it, and write it back over the seeded base workflow**:
1. `GET /workflow?workspaceId=WS&search=Collections%20Base` → `workflowId`, `agentId`.
2. `GET /workflow/{workflowId}?workspaceId=WS` → `g = result.workflow.workflow` (the graph).
3. Build the new graph `g2` (nodes, tools, agent_config, ...) from the brief.
4. Write it with (c).

Optional probe before step 4 (needs user approval; it mutates the workspace): `POST /agents` with `{"name":"probe-x","entityType":"workflow","workflow":g2,"config":<g2.agent_config>}`. If the response has `entityType:"workflow"` and a `workflowId`, then cloning works; otherwise delete the probe (`DELETE /agents?agentId=…&workspaceId=…`).

**(c) Update an existing workflow and publish it**

Route 1, explicit publish (preferred for certainty):
```http
POST /branches
{"entityType":"workflow","entityId":"<workflowId>","name":"builder <timestamp>","config": g2,
 "description":"generated by builder from brief <hash>"}
→ result.branchId, result.headVersionId

POST /branches/<branchId>/rebase
{"publish": true, "force": true}
→ result.published == true, result.newVersionId
   409 → inspect result.conflicts / checksState; on conflicts re-create the branch from the
         current head (or pass resolutions[{path, editedValue}])
```

Route 2, direct version (simplest; publish state must be checked):
```http
POST /versions/workflow/<workflowId>
{"entityId":"<workflowId>","workspaceId":"WS","configSnapshot": g2,
 "description":"builder <timestamp>"}            // optional parentVersionId = current head
→ 200 result.version {id, versionNumber, isPublished, configSnapshot, configDiff, …}
   422 → snapshot failed the workflow schema (the message is a good schema oracle)
If result.version.isPublished == false:
PUT /versions/workflow/<workflowId>/<versionId>?entityId=<workflowId>&versionId=<versionId>&workspaceId=WS
{"isPublished": true}                              // [INFERRED body; unverified]
```

Verify afterwards (GETs only):
- `GET /workflow/{id}` → the graph equals `g2`.
- `GET /versions/workflow/{id}?entityId={id}&workspaceId=WS` → the newest version has `isPublished:true`.
- `GET /versions/workflow/{id}/graph?entityId={id}&workspaceId=WS`.

Smoke test (costs credits):
```http
POST /chat/message {"workflowId":"<workflowId>","message":"Hello?","customerVariables":{"customer_id":"…","title":"Mr","last_name":"Smith"}}
```
Pass `versionId` to pin a version. The response carries `result.{response, callId, messageId, warning, endedByAgent}`. Continue the call with `{"callId":…, "message":…}` and close it with `POST /chat/end`.

Notes:
- The `GET /versions...` endpoints list `entityId` (and `versionId`) as **required query params in addition to the path params** **[SPEC]**. Send both; the live GETs did.
- `PUT /custom/abbreviations` also "creates a new version". Changes to `textSubstitutions` can simply go into `g2.agent_config.ttsConfig.textSubstitutions`, so one write is enough.

---

## 3. AgentConfig schema (the `config` of `/agents` = `agent_config` inside a workflow) **[SPEC]**

The top-level keys appear in this exact order in the spec. Required when the object is present: `llmConfig`, `ttsConfig`, `sttConfig`, `complianceConfig`. Live configs also carry `dtmfConfig`, `syncSource`, `privacyConfig`, `routingLlmConfig`, `backgroundSoundUrl`, `backgroundSoundVolume`, `piiRedactionConfig`, `voicemailDetection`, `callTimeoutSettings`, `extractionLlmConfig`, `postConversationAnalysis` and `testVariables`.

### 3.1 `llmConfig` (required: `provider`, `model`, `systemPrompt`)
| field | type / constraint | default |
|---|---|---|
| `provider` | string 1..50 (**free string; no enum in spec**) | — |
| `model` | string 1..100 (**free string**) | — |
| `baseUrl` | uri \| null ≤2048 | |
| `apiKey` | string \| null ≤500 | |
| `clientType` | `"freya"` \| `"openai"` \| null | |
| `systemPrompt` | string **1**..200000 (must be non-empty) | |
| `postHistoryPrompt` | string \| null ≤200000 | |
| `firstMessage` | string \| null ≤2000 | null |
| `firstMessageMode` | `"assistant-speaks-first-with-model-generated-message"` \| `"assistant-speaks-first"` \| `"assistant-waits-for-user"` \| null | `"assistant-waits-for-user"` |
| `disableInterruptionsDuringFirstMessage` | bool | false (platform default config uses true) |
| `maxTokens` | int 1..4000 | 1000 |
| `maxContextWindow` | int \| null 1024..10,000,000 | |
| `temperature` | number 0..2 | 0.7 |
| `tools` | Tool[] (§5) | [] |
| `additionalSettings` | object \| null (free-form) | {} |
| `retryExhaustionMessage` | string \| null ≤2000 | |
| `farewellMessage` | string \| null ≤2000 | |

Provider/model values (no enum anywhere in the spec or docs):
- **Observed [LIVE]**: `provider:"experimental-5", model:"experimental-5"` (platform default, also used in the base workflow's agent_config) and `provider:"openai", model:"gpt-4o"` (base agents-row).
- The spec example uses `openai/gpt-4o`. The older spec also listed `thinkingBudgetEnabled` and `thinkingBudget` (1000..100000), which the current spec removed.
- `422` "model outside its allowed hours" means some models are time-gated. Keep `experimental-5` unless there is a reason to change.
- Simulations accept `reasoningEffort` ∈ `none|minimal|low|medium|high|xhigh` in `*LlmOverride` only, not in llmConfig.

### 3.2 `ttsConfig` (required: `provider`, `voiceId`)
| field | type / constraint | default |
|---|---|---|
| `provider` | string 1..50 | |
| `model` | string \| null ≤100 | null |
| `voiceId` | string 1..100 | |
| `language` | string \| null | `"multi"` |
| `baseUrl`, `apiKey`, `clientType` | as llm | |
| `additionalSettings` | object \| null | {} (live: `{"speed":1,"stability":0.5,"similarity_boost":0.75}`) |
| `startSpeakingPlan` | object \| null: `waitSeconds` (0..5, default 0.4), `backgroundNoisePhrases` (string[] ≤200, each 1..100) | |
| `stopSpeakingPlan` | object \| null: `numberOfWords` (0..10, default 1), `voiceSeconds` (0..0.5, default 0.4), `backOffSeconds` (0..3, default 0.4), `acknowledgementPhrases[]`, `interruptionPhrases[]` (≤200 each) | |
| `textSubstitutions` | [{`pattern` 1..500 (regex; live values are escaped like `"\\$"`, `"\\+"`), `replacement` ≤500, `caseInsensitive` bool}] \| null | |
| `speechNormalization` | object \| null (below) | |
| `normalizationPrompt` | string \| null ≤8000 | |

`speechNormalization` fields:
- `emails` (true), `urls` (true), `emojis` (true), `phoneNumbers` (false), `identityNumbers` (false), `generalNumbers` (false), `names` (false).
- `advanced`: object \| null with booleans, all default false: `numbers`, `money`, `dates`, `times`, `percentages`, `fractions`, `units`, `ordinals`, `abbreviations`, `iban`, `symbols`.

**Language and voice warning [LIVE]**:
- The base *workflow's* `agent_config` speaks with Freya TTS voice `leyla`, `language:"tr"`, Turkish stop/start phrases, and **STT `language:"tr"`**.
- The Freya model APIs only expose `voice: ["leyla"]` and transcription `language: ["tr","cs"]` **[SPEC /audio/*]**.
- For English voice simulations, switch to the English values seen on the base agents-row:
```json
"ttsConfig": {"provider":"cartesia","model":"sonic-3.6","voiceId":"9287676d-f0cc-423f-ac03-3b3c7242f091","language":"en",
  "startSpeakingPlan":{"waitSeconds":0.4,"backgroundNoisePhrases":[]},
  "stopSpeakingPlan":{"voiceSeconds":0.3,"numberOfWords":1,"backOffSeconds":0.4,
    "acknowledgementPhrases":["yeah","yes","yep","yup","uh-huh","mm-hmm","mhm","hmm","okay","ok","right","sure","got it","i see","of course","alright","exactly","gotcha","makes sense"],
    "interruptionPhrases":["stop","wait","hold on","hang on","hold up","one moment","one second","just a second","wait wait","excuse me"]},
  "additionalSettings":{"speed":1,"stability":0.5,"similarity_boost":0.75}, "textSubstitutions":[…], "speechNormalization":{…}},
"sttConfig": {"provider":"deepgram","model":"nova-3","language":"en",
  "additionalSettings":{"keyterms":"Freya","aicEnabled":false,"krispEnabled":true,"punctuate":true,"diarize":false}}
```
In chat-mode simulations (`mode:"chat"`) TTS and STT are probably irrelevant **[INFERRED]**.

### 3.3 `sttConfig` (required: `provider`)
- `provider` (1..50), `model` (string\|null, default null), `language` (default `"multi"`), `baseUrl`, `apiKey`, `clientType`.
- `additionalSettings` (free-form). Live keys: `diarize`, `punctuate`, `streaming`, `aicEnabled`, `krispEnabled`, `smartTurnEnabled`, `webrtcApmEnabled`, `deepFilterEnabled`, `keyterms`.
- `confidenceReask`: object\|null with `enabled` (false), `threshold` (0..1, 0.3), `reduction` (`"min"`), `disableOnDigitNodes` (true), `reaskMode` (`"phrase"`|`"llm"`, default "phrase"), `reaskPhrases` (string[] ≤20 \| null), `maxConsecutiveReasks` (0..5, default 2).

### 3.4 `complianceConfig` (required)
`pciDss`, `mifidIi`, `euAiAct`, `soc2`: all bool, default false.

### 3.5 `callTimeoutSettings` (object\|null)
| field | type / constraint | spec default (live default) |
|---|---|---|
| `silenceTimeout` | int ≥0 | 0 (live 10) |
| `maximumDuration` | int 0..14400 s | 600 |
| `checkInMessage` | string\|null ≤2000 | |
| `closureMessage` | string\|null ≤2000 | |
| `maxRetries` | int 0..10 | 2 |
| `silenceTimeoutPrompt` | string\|null ≤2000 | |
| `maxDurationEndCall` | object\|null: `messageMode` (`"llm"`\|`"custom"`\|`"none"`, default "llm"), `spokenMessage`, `llmPrompt` (≤2000), `interruptible` (false), `triggerTimeout` (number\|null) | |
| `dynamicWait` | object\|null: `enabled` (false), `maxWaitSeconds` (10..3600, 300), `maxRetries` (0..10, 1), `checkInMessage`, `checkInPrompt`, `vadConfidence`, `vadMinVolume` (0..1), `vadStartSecs` (0..5), `vadStopSecs` (0..10) | |

### 3.6 `voicemailDetection` (object\|null)
`enabled` (false), `messageMode` (`"llm"`|`"custom"`|`"none"`|`"detect_only"`, default `"detect_only"`), `staticMessage` (≤2000), `voicemailResponseDelay` (0..30, default 2).

### 3.7 `dtmfConfig` (object\|null)
`enabled` (false), `timeout` (0..30, 2), `terminationDigit` (`0`-`9`|`#`|`*`, default `#`), `prefix` (default `"DTMF: "`).

### 3.8 `postConversationAnalysis` (object\|null)
- `insights` [{`id` (digits string), `name` 1..100}] ≤200
- `summaryPrompt` (≤10000), `autoDetectProblems` (false), `problemDetectionPrompt`, `problemDetectionInstructions` (≤10000 each), `analysisModel` (≤100, null = default).

Structured-data "insights" are managed separately via `/insights` and `PUT /agents/{agentId}/insights` `{workspaceId, insightIds:number[]}`. That PUT creates an **unpublished** version. Insight `type` ∈ `string|number|rating|checkbox|range|enum|array`, `source` ∈ `analysis|tool_field` (reads also show `workflow_variable`), with `toolName` and `toolFieldPath` for tool-field insights.

### 3.9 `privacyConfig` (object\|null)
`enableRecording` (false), `getRecordingConsent` (false), `recordingConsentText` (≤500), `stopSecs`, `preSpeechMs`, `maxDurationSecs` (≥0).

### 3.10 Other configs
- `turnAnalyzerConfig`: `provider` (`"local"`|`"fal"`), `url`, `modelPath`.
- `guardrailsConfig` (object\|null). This is output/input filtering, not abuse handling:
  - `guardrails_enabled` (false), `custom_rules_enabled` (false).
  - `rules` [{`id`, `type` (`exact`|`regex`|`dictionary`|`structured`|`classifier`), `pattern`, `action` (`refuse`|`regenerate`|`log_only`), `enabled` (true), `description`, `case_sensitive` (false), `classifier_threshold` (0..1)}].
  - `refuse_message` (default "I'm sorry, I can't help with that."), `max_regenerate_retries` (0..5, 2), `regenerate_wait_message` ("One moment, please."), `classifier_enabled`, `classifier_provider`, `classifier_model`.
  - `input_config` {`enabled` (false), `normalize` (true), `detect_injection` (true), `detect_weird_input` (true), `run_classifier` (true), `refuse_message`, `max_input_chars` (1..100000, 8000)}.
  - **[INFERRED]** A `regex` rule with `action:"regenerate"` could block currency strings that were not in tool results, but it cannot know the "allowed" numbers. Prompt-level grounding is the main defence.
- `piiRedactionConfig` (object\|null): `enabled` (false), `maskEmail`, `maskPhone`, `maskPaymentCard`, `maskNationalId`, `maskIban` (all true), `customRules` [{`label` (`^[A-Z0-9_]+$`, 1..40), `pattern` (1..500), `enabled` (true)}]. **[INFERRED]** Keep `enabled:false`: masking might hide the DOB / phone_last4 the agent needs.
- `routingLlmConfig` and `extractionLlmConfig` (object\|null, required `provider`, `model`; plus `baseUrl`, `apiKey`). Both are null in the base, meaning platform defaults.
  - **[INFERRED]** The routing LLM evaluates LLM-judged transitions. The extraction LLM fills `extraction_schema` variables and the arguments of auto-run tools such as `close_call` (`alwaysRunsAt:["end_of_call"]`).
- `backgroundSoundUrl` (≤200 \| null), `backgroundSoundVolume` (0..2 \| null, staging only).
- `syncSource` {`url`, `apiKey`, `syncedAt`} \| null: pulls config from another Freya environment. Leave null.
- `testVariables`: `{ [name]: string }`. These are default values for `{{variables}}` in editor tests. Workflows use graph-level `test_variables` instead.

---

## 4. Workflow graph schema (= version `configSnapshot` for entityType `workflow`)

**There is no published schema.** Everything below is **[LIVE]** from the base workflow; enumerations beyond the observed values are **[INFERRED]**. Validate by writing with `POST /versions/workflow/{id}` and reading 422 messages.

### 4.1 Top level
```jsonc
{
  "nodes":        { "<node_id>": Node, ... },     // dict keyed by node id; node.name == key in base
  "tools":        [ Tool, ... ],                  // workflow-level (global) tools, e.g. close_call
  "agent_config": AgentConfig,                    // §3, the runtime voice/LLM config of the workflow
  "global_nodes": { },                            // base: {} — [INFERRED] nodes reachable from any node (e.g. abuse/wrong-person handlers); shape unknown
  "initial_node": "identity",                     // id of the start node
  "system_prompt": "",                            // [INFERRED] prepended to every node's prompt
  "test_variables": { }                           // [INFERRED] {name: value} for editor tests
}
```
Record-level metadata stays outside the snapshot: `name`, `description`, `version` ("2.0.0"), `tags`, `nodePositions {node_id:{x,y}}`, `canvasViewport {x,y,zoom}|null`, `previewImage` (svg). Nodes *also* carry `position {x,y}` inside the snapshot. Give new nodes a position, e.g. `{x:500, y:600*i}`, so the dashboard canvas renders them.

### 4.2 Node: every field observed, with base values
| field | base value(s) | meaning / notes |
|---|---|---|
| `name` | `"identity"`, `"collection"` | node id; keep equal to the dict key |
| `node_type` | `"conversation"` | **only observed value.** **[INFERRED]** others exist (the docs list Agent/Function/Condition/Transfer/Wait/Loop node *concepts*) but their string values are unknown |
| `role_message` | `""` | **[INFERRED]** persona/role text (pipecat-flows `role_messages` analogue) |
| `task_message` | multi-line string | the node's instructions (prompt). Supports `{{variables}}` (e.g. `Now: {{now}}.`) |
| `on_enter` | `{"type":"tts","text":"…{{title}} {{last_name}}?"}` | spoken on entering the node. **`type` observed: `"tts"` only.** Text is templated |
| `on_enter_first_entry_only` | false | only speak `on_enter` the first time the node is entered |
| `pre_actions` | `[]` | **[INFERRED]** actions before the LLM turn (pipecat-flows: `{"type":"tts_say","text":…}`, `end_conversation`, `function`). Shape unverified |
| `tools` | Tool[] | tools available **in this node only** (§5) |
| `transitions` | `[{condition, description, target_node}]` | see §4.3 |
| `max_turn_transition` | null | **[INFERRED]** auto-transition after N turns, e.g. `{"turns":N,"target_node":"x"}`. Shape unknown |
| `max_node_turns` | 80 | turn cap inside the node |
| `passthrough` | false | **[INFERRED]** a node that does not wait for user input and immediately evaluates transitions (a router or function node) |
| `passthrough_default_transition` | null | **[INFERRED]** target node id when no passthrough condition matches |
| `interruptible` | identity:false, collection:true | whether the caller can barge in while the agent speaks |
| `return_to_previous_on_interrupt` | false | **[INFERRED]** go back to the previous node after a global-node detour |
| `disable_inactivity_monitor` | false | disables the silence check-in in this node |
| `truncation_strategy` | `"keep_all"` | context policy. **[INFERRED]** other values like a sliding window use `truncation_window_size` |
| `truncation_window_size` | 10 | |
| `suppress_tool_results` | false | **[INFERRED]** hide raw tool JSON from the LLM context |
| `extraction_schema` | `[]` | **[INFERRED]** same item shape as tool-level `extraction_schema` in the spec: `{name, type: string\|integer\|float\|boolean\|date\|datetime\|enum, description, extraction_prompt, validation_pattern, max_retries, required, overridable}`. Extracted values become variables usable in conditions |
| `response_mapping` | `[]` | **[INFERRED]** same as tool-level `{path, variable, description}` |
| `assertions` | `[]` | **[INFERRED]** node-level checks. Unit-test assertions in the spec are `{type:"routing", signal:"node_entered"\|"route_target", target, negate}`, `{type:"extraction", name, match:"exists"\|"equals"\|"contains"\|"regex"\|"absent", value, minConfidence}` and `{type:"tool_call", tool, args, argsMatch:"subset"\|"exact", negate}` |
| `position` | `{x,y}` | canvas position |

### 4.3 Transition / condition grammar (observed)
```json
{
  "target_node": "collection",
  "description": "both security answers verified",
  "condition": {
    "logic": "or",
    "conditions": [
      {"variable": "verify_identity_response.data.all_verified", "operator": "equals", "operand": "true", "mode": "exact"},
      {"variable": "verify_identity_response.data.all_verified", "operator": "equals", "operand": "True", "mode": "exact"}
    ]
  }
}
```
- Observed enum values: `logic` = `"or"`; `operator` = `"equals"`; `mode` = `"exact"`.
- **[INFERRED]** `logic` also accepts `"and"`. Plausible other operators (`not_equals`, `contains`, `exists`, `gt`/`lt`) and modes are unverified. Note that the unit-test `match` enum is `exists|equals|contains|regex|absent`, which may be the same family.
- Operands are **strings**. The base checks both `"true"` and `"True"` because the runtime is Python, which stringifies `True` as `"True"` (`pre_process_code` is Python). **Always cover both spellings for booleans.**
- `variable` is a dotted path into a variable store (§5.4). Tool responses land in `<tool_name>_response`.
- `description` is a human label. **[INFERRED]** When `condition` is absent, the routing LLM may use `description` as an LLM-judged transition. Unverified.
- `transitions: []` (the collection node) means a terminal node; the call ends via `end_call` (§5.2).

### 4.4 Minimal 2-node example (abridged from the base)
```json
{
  "initial_node": "identity", "system_prompt": "", "global_nodes": {}, "test_variables": {},
  "nodes": {
    "identity": {
      "name": "identity", "node_type": "conversation", "position": {"x": 500, "y": -600},
      "role_message": "", "task_message": "…verify DOB then phone_last4, call verify_identity per answer…",
      "on_enter": {"type": "tts", "text": "Hello, this is the digital assistant of Freya Bank. Am I speaking with {{title}} {{last_name}}?"},
      "on_enter_first_entry_only": false, "pre_actions": [], "tools": [ "<verify_identity>", "<end_call>" ],
      "transitions": [ { "target_node": "collection", "description": "both security answers verified",
        "condition": {"logic": "or", "conditions": [
          {"mode": "exact", "operand": "true", "operator": "equals", "variable": "verify_identity_response.data.all_verified"},
          {"mode": "exact", "operand": "True", "operator": "equals", "variable": "verify_identity_response.data.all_verified"}]}}],
      "max_turn_transition": null, "max_node_turns": 80, "passthrough": false, "passthrough_default_transition": null,
      "interruptible": false, "return_to_previous_on_interrupt": false, "disable_inactivity_monitor": false,
      "truncation_strategy": "keep_all", "truncation_window_size": 10, "suppress_tool_results": false,
      "extraction_schema": [], "response_mapping": [], "assertions": []
    },
    "collection": { "…same keys…": "…", "transitions": [], "interruptible": true,
      "on_enter": {"type": "tts", "text": "Thank you, that's verified. I'm calling about your Freya Bank account, do you have a minute?"},
      "task_message": "…call get_account first … read back amount and date … create_promise after yes … Now: {{now}}." }
  },
  "tools": [ "<close_call with config.alwaysRunsAt:[\"end_of_call\"] + pre_process_code>" ],
  "agent_config": { "…AgentConfig §3…": "…" }
}
```
The full base graph is at `scratchpad/live/workflow_base.pretty.json` → `data.result.workflow.workflow`.

---

## 5. Tools

### 5.1 Tool object **[SPEC]** (identical in `llmConfig.tools`, node `tools`, and workflow `tools`)
```jsonc
{
  "type": "function",                              // enum ["function"]
  "function": {                                    // required: name, handler, description, parameters
    "name": "verify_identity",
    "handler": "__function__:api_call",            // string (no enum in spec)
    "description": "shown to the LLM",
    "parameters": { "type": "object", "properties": {…}, "required": [...] },  // JSON-Schema-ish; "items" allowed
    "transition_to": "node_id"                     // optional — [INFERRED] jump to node after this tool runs
  },
  "config": {
    "timeoutMs": 12000,                            // int > 0
    "maxRetries": 0,                               // int 0..5
    "maxPerNTurns": 1,                             // int > 0 — rate limit
    "alwaysRunsAt": ["end_of_call"],               // subset of each_turn | start_of_call | end_of_call
    "interruptible": true,
    "preSpeech": {"mode": "static"|"auto", "message": "One moment…", "interruptible": true,
                  "audio_asset_id": "…", "audio_loop": false, "audio_volume": 0.5}   // filler while tool runs
  },
  "response_mapping": [ {"path": "data.total_due", "variable": "total_due", "description": ""} ],
  "extraction_schema": [ {"name": "promise_date", "type": "date", "description": "…", "extraction_prompt": "…",
                          "validation_pattern": "^\\d{4}-\\d{2}-\\d{2}$", "max_retries": 1, "required": true, "overridable": false} ],
  "pre_process_code":  "python string | null",
  "post_process_code": "python string | null"
}
```

### 5.2 Handlers
- **`__function__:api_call`** **[LIVE]**: an HTTP request built from `parameters.properties.*.default`.
  - `url.default`: the endpoint.
  - `method.default`: one of `"POST"`, `"GET"`, etc.
  - `headers.properties.<Header>.default`, e.g. `Content-Type: application/json`.
  - `body`: `{type:"object", required:[…LLM-supplied fields…], properties:{field:{type, default:"{{field}}", description, enum?}}}`.
  - Body defaults are templates:
    - `{{customer_id}}` is resolved from customer variables.
    - `{{field}}`, `{{answer}}`, `{{amount}}`, `{{date}}`, `{{product_id}}`, `{{outcome}}` and `{{reason}}` are **LLM/extraction-supplied arguments**. **[INFERRED]** A placeholder whose name is not a known variable becomes an argument the model must fill; names listed in `body.required` are mandatory. Each body property's `description` and `enum` are what the LLM sees, so write them carefully.
  - `type:"number"` on `amount` with default `"{{amount}}"` means the template is cast after substitution **[INFERRED]**.
- **`__function__:end_call`** **[LIVE]**: hangs up. In nodes it is configured through parameter defaults:
  - `message_mode` = `"none"`. **[INFERRED]** It is also `llm`|`custom`, like `maxDurationEndCall.messageMode`.
  - `interruptible` = true.
  - `trigger_pattern` = `"Have a good day|have a good day"`: a regex. When **the agent says** it, the call ends automatically after `trigger_timeout` = 3 seconds.
  - `trigger_pattern_both` = `"goodbye|bye|have a good day|you too|thank you|thanks|take care"`. **[INFERRED]** A closing exchange where both sides say these also ends the call.
  - The base tool description tells the LLM: "You normally do NOT call this: the phrase 'Have a good day.' ends the call by itself."
  - In `llmConfig.tools` (agent level) the same handler has `properties: {}`.
- Other handlers are not documented. Hints: permission `calls:transfer`, docs "Transfer Nodes", and "reschedule_call tool" (from `POST /call` `originCallId`, campaign calls only). **[INFERRED]** handler names such as `__function__:transfer_call` and `__function__:reschedule_call`. Unverified; do not rely on them.

### 5.3 Workflow-level tools and `pre_process_code` **[LIVE]**
`close_call` sits in the workflow-level `tools` with `config.alwaysRunsAt:["end_of_call"]`, so it fires automatically when the call ends, whichever node is active. Its args (`outcome` enum `promise|no_promise|not_verified|not_reached`, and `reason`) must be **filled from the conversation**. **[INFERRED]** The extraction LLM fills them, guided only by the `description` strings, which makes those descriptions scoring-critical.

`pre_process_code` is Python executed before the request, with a mutable `config` dict (`config['body']`, and probably `url`, `method`, `headers`):
```python
try:
    b = config.get('body')
    if isinstance(b, dict) and b.get('outcome') in ('promise', 'paid'):
        b.pop('reason', None)
except Exception:
    pass
```
Note the `'paid'` literal even though the enum lacks it. **[INFERRED]** The mock may accept an extra outcome such as `paid` for "already paid". Verify with the brief/mock docs, not by POSTing.

`post_process_code` (string|null) exists in the spec. **[INFERRED]** It post-processes the tool response (e.g. a `response` variable). Shape unknown.

### 5.4 Variables / `{{templating}}`
- Syntax: `{{name}}`, and dotted paths for objects. Templating works in `task_message`, `on_enter.text`, `firstMessage`, `systemPrompt` and tool parameter defaults **[LIVE/DOCS]**. The docs say "Use `{{variable}}` for dynamic values".
- **Customer variables** are injected at call start:
  - Embed SDK `customerVariables: Record<string,string>`, "Variables injected into the agent's system prompt at runtime" **[DOCS]**.
  - `POST /chat/message` `customerVariables` (object; 422 if too large) **[SPEC]**.
  - `POST /simulations` `customerVariablesMap: {personaId: {name: string}}` **[SPEC]**.
  - `POST /call` `properties` ("Custom properties to pass to the agent") **[SPEC]**.
  - Campaign customer-table columns are auto-created from `{{vars}}` found in the prompt **[DOCS]**.
  - Base uses: `{{customer_id}}`, `{{title}}`, `{{last_name}}`.
- **System variables**:
  - `{{now}}` (used in the base collection node; format unverified, probably an ISO datetime **[INFERRED]**).
  - `call.retry_number` (0-based campaign attempt) **[SPEC]**.
- **Tool response variables**: the JSON response of tool `X` is stored as `X_response`, addressed by dotted path, e.g. `verify_identity_response.data.all_verified` **[LIVE]**. The mock returns `{"ok":bool, …}`; on success presumably `{"ok":true,"data":{…}}` **[INFERRED]**. `response_mapping` (`path` → `variable`) can alias a response field to a short variable name.
- **Extracted variables**: from `extraction_schema` (node- or tool-level), with `name` = variable name **[INFERRED]**.
- Booleans compare as strings, so check `"true"` and `"True"` (§4.3).

---

## 6. TTS abbreviations: `/custom/abbreviations` **[SPEC]**

- **GET** `/custom/abbreviations?id=<uuid>` (preferred) or `?name=<1..200>`.
  - Resolver order: agents table first, then workflows. A name that is ambiguous returns 409; not found returns 404.
  - Result: `{type:"agent"|"workflow", id, name, abbreviations:[{pattern, replacement, caseInsensitive?}]}`.
  - **[LIVE]** `id=<workflowId>` works. `id=<workflow-type agentId>` fails with "No agent or workflow found with this id". **For workflows, use the workflowId.**
- **PUT** `/custom/abbreviations` with body `{id? | name?, abbreviations:[{pattern (1..500), replacement (≤500), caseInsensitive?}]}` (required: `abbreviations`).
  - It **replaces the whole list** (no per-rule patch). It "Behaves like a UI save: persists to the JSONB config and creates a new version".
  - Result adds `versionId` (uuid|null). The spec example also sends `workspaceId`.
- These are exactly `ttsConfig.textSubstitutions`, which for a workflow means `agent_config.ttsConfig.textSubstitutions`. Writing them inside the graph snapshot is equivalent and saves one call.
- **Default list [LIVE]**: `@→at`, `\$→dollars`, `%→percent`, `&→and`, `\+→plus`, `turkishairlines→turkish airlines (ci)`.
  - **Pitfall**: `\$`→`dollars` is a plain substitution, so `"$1,250.00"` would be voiced roughly as "dollars1,250.00" (word order wrong).
  - Options:
    - (1) Keep all money in text as `"1,250.00 dollars"` (or the brief's currency word) by prompt rule, and drop or keep the `\$` rule as a safety net.
    - (2) Add a regex rule with a capture group, e.g. pattern `\$\s?([0-9][0-9,]*(?:\.[0-9]{1,2})?)` → replacement `$1 dollars`. **[UNVERIFIED]** backreference support.
    - (3) Turn on `speechNormalization.advanced.money:true` (and `numbers`), and/or set `normalizationPrompt`, so the TTS normaliser verbalises amounts **[INFERRED behaviour]**.
  - The currency of the mock accounts is unknown. Read it from the brief or the `get_account` field names; it could be TRY/EUR.
- Rule patterns are regexes (`\$`, `\+` are escaped in live data). Use `caseInsensitive:true` for words.

---

## 7. Builder implementation checklist (Python / httpx)

1. Discover: `GET /workflow?workspaceId&search=` and `GET /agents?workspaceId&entityType=workflow`. Cache `workflowId`, `agentId` and the current head version id (`GET /versions/workflow/{id}`).
2. Load the base graph (`GET /workflow/{id}`) and deep-copy it. Overwrite:
   - `agent_config` (English STT/TTS, `firstMessage` == initial `on_enter.text`, temperature ~0.2-0.4)
   - nodes, transitions, tools, `textSubstitutions`, `close_call` descriptions
3. Validate locally:
   - unique node ids, `initial_node` exists, every `target_node` exists
   - each tool `name` unique per scope
   - `systemPrompt` non-empty (minLength 1; required by llmConfig validation even in workflows **[INFERRED]**)
   - string lengths within limits
4. Write with branch create + `rebase {publish:true, force:true}` (or `POST /versions` + check `isPublished`).
5. Read back and assert equality. Smoke-test via `/chat/message` with `workflowId` and `customerVariables`.
6. Idempotency: before writing, compare the new graph to the current head's `configSnapshot` and skip if equal. Branch names include a timestamp. On 409, refetch and retry once.

Minimal Python sketch (write calls shown; they need the user's go-ahead before running on the live workspace):
```python
import httpx, os, copy, time
B = "https://staging-app.freyavoice.ai/api/v2"; WS = os.environ["FREYA_WORKSPACE_ID"]
H = {"Authorization": f"Bearer {os.environ['FREYA_KEY']}", "Content-Type": "application/json"}
c = httpx.Client(base_url=B, headers=H, timeout=60)
def ok(r):
    j = r.json()
    if not j.get("success"): raise RuntimeError(f"{r.status_code} {j['data']['message']}")
    return j["data"]["result"]
wf = ok(c.get("/workflow", params={"workspaceId": WS, "search": "Collections Base"}))["workflows"][0]
graph = ok(c.get(f"/workflow/{wf['id']}", params={"workspaceId": WS}))["workflow"]["workflow"]
g2 = build_graph(copy.deepcopy(graph), brief)        # your transform
br = ok(c.post("/branches", json={"entityType": "workflow", "entityId": wf["id"],
                                   "name": f"builder {int(time.time())}", "config": g2}))
mg = ok(c.post(f"/branches/{br['branchId']}/rebase", json={"publish": True, "force": True}))
assert mg.get("published"), mg
```

---

## 8. Open questions (each needs one approved write probe)
1. Does `POST /versions/workflow/{id}` publish immediately (`isPublished`)? What body does `PUT /versions/.../{versionId}` accept to publish/activate?
2. Does `POST /branches` accept `config` = the full graph for `entityType:"workflow"` (same shape as `configSnapshot`)? Does it need `workspaceId`?
3. Can `POST /agents` create a workflow-type agent with undocumented fields, i.e. is there any way to *create* (not overwrite) a workflow via the public API?
4. What are the valid `node_type`, `on_enter.type`, `operator`, `mode`, `truncation_strategy` and `max_turn_transition` values, and the `global_nodes` and `pre_actions` shapes? 422 messages from `POST /versions` are the cheapest oracle.
5. For a workflow, which is used at runtime when both are set: `agent_config.llmConfig.firstMessage` or the initial node's `on_enter`? The base keeps them identical; do the same.
6. Exact `{{now}}` format and timezone; the full list of system variables.
7. Does the mock's `close_call` accept outcome `paid` (hinted by `pre_process_code`)? What does the `get_account` response look like (currency, `product_id`, amounts)?
8. Are graders' simulations voice or chat mode? This decides whether the Turkish `tr` STT/TTS default in the base `agent_config` is fatal or irrelevant.

---

## Appendix A — POST /agents request body, flattened from openapi_staging.json (exact)

Format: `json.path: type [constraints] // description`. `[]` = array item, `{*}` = map value.

```
CONTENT application/json
$: object required=['name']
$.name: string minLength=1 maxLength=100
$.language: string maxLength=10
$.config: object required=['llmConfig', 'ttsConfig', 'sttConfig', 'complianceConfig']
$.config.llmConfig: object required=['provider', 'model', 'systemPrompt']
$.config.llmConfig.provider: string minLength=1 maxLength=50
$.config.llmConfig.model: string minLength=1 maxLength=100
$.config.llmConfig.baseUrl: string|null maxLength=2048 format="uri"
$.config.llmConfig.apiKey: string|null maxLength=500
$.config.llmConfig.clientType: string|null enum=["freya", "openai", null]
$.config.llmConfig.systemPrompt: string minLength=1 maxLength=200000
$.config.llmConfig.postHistoryPrompt: string|null maxLength=200000
$.config.llmConfig.firstMessage: string|null default=null maxLength=2000
$.config.llmConfig.firstMessageMode: string|null enum=["assistant-speaks-first-with-model-generated-message", "assistant-speaks-first", "assistant-waits-for-user", null] default="assistant-waits-for-user"
$.config.llmConfig.disableInterruptionsDuringFirstMessage: boolean default=false
$.config.llmConfig.maxTokens: integer default=1000 minimum=1 maximum=4000
$.config.llmConfig.maxContextWindow: integer|null minimum=1024 maximum=10000000
$.config.llmConfig.temperature: number default=0.7 minimum=0 maximum=2
$.config.llmConfig.tools: array default=[]
$.config.llmConfig.tools[]: object required=['type', 'function']
$.config.llmConfig.tools[].type: string enum=["function"]
$.config.llmConfig.tools[].function: object required=['name', 'handler', 'description', 'parameters']
$.config.llmConfig.tools[].function.name: string
$.config.llmConfig.tools[].function.handler: string
$.config.llmConfig.tools[].function.description: string
$.config.llmConfig.tools[].function.parameters: object required=['type']
$.config.llmConfig.tools[].function.parameters.type: string
$.config.llmConfig.tools[].function.parameters.properties: object
$.config.llmConfig.tools[].function.parameters.required: array
$.config.llmConfig.tools[].function.parameters.required[]: string
$.config.llmConfig.tools[].function.parameters.items: 
$.config.llmConfig.tools[].function.transition_to: string
$.config.llmConfig.tools[].config: object
$.config.llmConfig.tools[].config.maxPerNTurns: integer exclusiveMinimum=0
$.config.llmConfig.tools[].config.alwaysRunsAt: array
$.config.llmConfig.tools[].config.alwaysRunsAt[]: string enum=["each_turn", "start_of_call", "end_of_call"]
$.config.llmConfig.tools[].config.timeoutMs: integer exclusiveMinimum=0
$.config.llmConfig.tools[].config.maxRetries: integer minimum=0 maximum=5
$.config.llmConfig.tools[].config.preSpeech: object required=['mode']
$.config.llmConfig.tools[].config.preSpeech.mode: string enum=["static", "auto"]
$.config.llmConfig.tools[].config.preSpeech.message: string
$.config.llmConfig.tools[].config.preSpeech.interruptible: boolean
$.config.llmConfig.tools[].config.preSpeech.audio_asset_id: string
$.config.llmConfig.tools[].config.preSpeech.audio_loop: boolean
$.config.llmConfig.tools[].config.preSpeech.audio_volume: number
$.config.llmConfig.tools[].config.interruptible: boolean
$.config.llmConfig.tools[].response_mapping: array
$.config.llmConfig.tools[].response_mapping[]: object required=['path', 'variable']
$.config.llmConfig.tools[].response_mapping[].path: string
$.config.llmConfig.tools[].response_mapping[].variable: string
$.config.llmConfig.tools[].response_mapping[].description: string default=""
$.config.llmConfig.tools[].extraction_schema: array
$.config.llmConfig.tools[].extraction_schema[]: object required=['name', 'type']
$.config.llmConfig.tools[].extraction_schema[].name: string
$.config.llmConfig.tools[].extraction_schema[].type: string enum=["string", "integer", "float", "boolean", "date", "datetime", "enum"]
$.config.llmConfig.tools[].extraction_schema[].description: string
$.config.llmConfig.tools[].extraction_schema[].extraction_prompt: string
$.config.llmConfig.tools[].extraction_schema[].validation_pattern: string
$.config.llmConfig.tools[].extraction_schema[].max_retries: integer minimum=0
$.config.llmConfig.tools[].extraction_schema[].required: boolean
$.config.llmConfig.tools[].extraction_schema[].overridable: boolean
$.config.llmConfig.tools[].pre_process_code: string|null
$.config.llmConfig.tools[].post_process_code: string|null
$.config.llmConfig.additionalSettings: object|null default={}
$.config.llmConfig.retryExhaustionMessage: string|null maxLength=2000
$.config.llmConfig.farewellMessage: string|null maxLength=2000
$.config.ttsConfig: object required=['provider', 'voiceId']
$.config.ttsConfig.provider: string minLength=1 maxLength=50
$.config.ttsConfig.model: string|null default=null maxLength=100
$.config.ttsConfig.voiceId: string minLength=1 maxLength=100
$.config.ttsConfig.language: string|null default="multi" minLength=1
$.config.ttsConfig.baseUrl: string|null maxLength=2048 format="uri"
$.config.ttsConfig.apiKey: string|null maxLength=500
$.config.ttsConfig.clientType: string|null enum=["freya", "openai", null]
$.config.ttsConfig.additionalSettings: object|null default={}
$.config.ttsConfig.startSpeakingPlan: object|null
$.config.ttsConfig.startSpeakingPlan.waitSeconds: number default=0.4 minimum=0 maximum=5
$.config.ttsConfig.startSpeakingPlan.backgroundNoisePhrases: array|null maxItems=200
$.config.ttsConfig.startSpeakingPlan.backgroundNoisePhrases[]: string minLength=1 maxLength=100
$.config.ttsConfig.stopSpeakingPlan: object|null
$.config.ttsConfig.stopSpeakingPlan.numberOfWords: integer default=1 minimum=0 maximum=10
$.config.ttsConfig.stopSpeakingPlan.voiceSeconds: number default=0.4 minimum=0 maximum=0.5
$.config.ttsConfig.stopSpeakingPlan.backOffSeconds: number default=0.4 minimum=0 maximum=3
$.config.ttsConfig.stopSpeakingPlan.acknowledgementPhrases: array|null maxItems=200
$.config.ttsConfig.stopSpeakingPlan.acknowledgementPhrases[]: string minLength=1 maxLength=100
$.config.ttsConfig.stopSpeakingPlan.interruptionPhrases: array|null maxItems=200
$.config.ttsConfig.stopSpeakingPlan.interruptionPhrases[]: string minLength=1 maxLength=100
$.config.ttsConfig.textSubstitutions: array|null
$.config.ttsConfig.textSubstitutions[]: object required=['pattern', 'replacement']
$.config.ttsConfig.textSubstitutions[].pattern: string minLength=1 maxLength=500
$.config.ttsConfig.textSubstitutions[].replacement: string maxLength=500
$.config.ttsConfig.textSubstitutions[].caseInsensitive: boolean
$.config.ttsConfig.speechNormalization: object|null
$.config.ttsConfig.speechNormalization.emails: boolean default=true
$.config.ttsConfig.speechNormalization.urls: boolean default=true
$.config.ttsConfig.speechNormalization.emojis: boolean default=true
$.config.ttsConfig.speechNormalization.phoneNumbers: boolean default=false
$.config.ttsConfig.speechNormalization.identityNumbers: boolean default=false
$.config.ttsConfig.speechNormalization.generalNumbers: boolean default=false
$.config.ttsConfig.speechNormalization.names: boolean default=false
$.config.ttsConfig.speechNormalization.advanced: object|null
$.config.ttsConfig.speechNormalization.advanced.numbers: boolean default=false
$.config.ttsConfig.speechNormalization.advanced.money: boolean default=false
$.config.ttsConfig.speechNormalization.advanced.dates: boolean default=false
$.config.ttsConfig.speechNormalization.advanced.times: boolean default=false
$.config.ttsConfig.speechNormalization.advanced.percentages: boolean default=false
$.config.ttsConfig.speechNormalization.advanced.fractions: boolean default=false
$.config.ttsConfig.speechNormalization.advanced.units: boolean default=false
$.config.ttsConfig.speechNormalization.advanced.ordinals: boolean default=false
$.config.ttsConfig.speechNormalization.advanced.abbreviations: boolean default=false
$.config.ttsConfig.speechNormalization.advanced.iban: boolean default=false
$.config.ttsConfig.speechNormalization.advanced.symbols: boolean default=false
$.config.ttsConfig.normalizationPrompt: string|null maxLength=8000
$.config.sttConfig: object required=['provider']
$.config.sttConfig.provider: string minLength=1 maxLength=50
$.config.sttConfig.model: string|null default=null maxLength=100
$.config.sttConfig.language: string|null default="multi" minLength=1
$.config.sttConfig.baseUrl: string|null maxLength=2048 format="uri"
$.config.sttConfig.apiKey: string|null maxLength=500
$.config.sttConfig.clientType: string|null enum=["freya", "openai", null]
$.config.sttConfig.additionalSettings: object|null default={}
$.config.sttConfig.confidenceReask: object|null
$.config.sttConfig.confidenceReask.enabled: boolean default=false
$.config.sttConfig.confidenceReask.threshold: number default=0.3 minimum=0 maximum=1
$.config.sttConfig.confidenceReask.reduction: string enum=["min"] default="min"
$.config.sttConfig.confidenceReask.disableOnDigitNodes: boolean default=true
$.config.sttConfig.confidenceReask.reaskMode: string enum=["phrase", "llm"] default="phrase"
$.config.sttConfig.confidenceReask.reaskPhrases: array|null default=null maxItems=20
$.config.sttConfig.confidenceReask.reaskPhrases[]: string maxLength=2000
$.config.sttConfig.confidenceReask.maxConsecutiveReasks: integer default=2 minimum=0 maximum=5
$.config.complianceConfig: object
$.config.complianceConfig.pciDss: boolean default=false
$.config.complianceConfig.mifidIi: boolean default=false
$.config.complianceConfig.euAiAct: boolean default=false
$.config.complianceConfig.soc2: boolean default=false
$.config.callTimeoutSettings: object|null
$.config.callTimeoutSettings.silenceTimeout: integer default=0 minimum=0
$.config.callTimeoutSettings.maximumDuration: integer default=600 minimum=0 maximum=14400
$.config.callTimeoutSettings.checkInMessage: string|null default=null maxLength=2000
$.config.callTimeoutSettings.closureMessage: string|null default=null maxLength=2000
$.config.callTimeoutSettings.maxRetries: integer default=2 minimum=0 maximum=10
$.config.callTimeoutSettings.silenceTimeoutPrompt: string|null default=null maxLength=2000
$.config.callTimeoutSettings.maxDurationEndCall: object|null default=null
$.config.callTimeoutSettings.maxDurationEndCall.messageMode: string enum=["llm", "custom", "none"] default="llm"
$.config.callTimeoutSettings.maxDurationEndCall.spokenMessage: string|null default=null maxLength=2000
$.config.callTimeoutSettings.maxDurationEndCall.llmPrompt: string|null default=null maxLength=2000
$.config.callTimeoutSettings.maxDurationEndCall.interruptible: boolean default=false
$.config.callTimeoutSettings.maxDurationEndCall.triggerTimeout: number|null default=null minimum=0
$.config.callTimeoutSettings.dynamicWait: object|null default=null
$.config.callTimeoutSettings.dynamicWait.enabled: boolean default=false
$.config.callTimeoutSettings.dynamicWait.maxWaitSeconds: integer default=300 minimum=10 maximum=3600
$.config.callTimeoutSettings.dynamicWait.maxRetries: integer default=1 minimum=0 maximum=10
$.config.callTimeoutSettings.dynamicWait.checkInMessage: string|null default=null maxLength=2000
$.config.callTimeoutSettings.dynamicWait.checkInPrompt: string|null default=null maxLength=2000
$.config.callTimeoutSettings.dynamicWait.vadConfidence: number|null default=null minimum=0 maximum=1
$.config.callTimeoutSettings.dynamicWait.vadMinVolume: number|null default=null minimum=0 maximum=1
$.config.callTimeoutSettings.dynamicWait.vadStartSecs: number|null default=null minimum=0 maximum=5
$.config.callTimeoutSettings.dynamicWait.vadStopSecs: number|null default=null minimum=0 maximum=10
$.config.voicemailDetection: object|null
$.config.voicemailDetection.enabled: boolean default=false
$.config.voicemailDetection.messageMode: string enum=["llm", "custom", "none", "detect_only"] default="detect_only"
$.config.voicemailDetection.staticMessage: string|null default=null maxLength=2000
$.config.voicemailDetection.voicemailResponseDelay: number default=2 minimum=0 maximum=30
$.config.dtmfConfig: object|null
$.config.dtmfConfig.enabled: boolean default=false
$.config.dtmfConfig.timeout: number default=2 minimum=0 maximum=30
$.config.dtmfConfig.terminationDigit: string enum=["0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "#", "*"] default="#"
$.config.dtmfConfig.prefix: string default="DTMF: " maxLength=100
$.config.postConversationAnalysis: object|null
$.config.postConversationAnalysis.insights: array maxItems=200
$.config.postConversationAnalysis.insights[]: object required=['name']
$.config.postConversationAnalysis.insights[].id: string pattern="^\\d+$"
$.config.postConversationAnalysis.insights[].name: string minLength=1 maxLength=100
$.config.postConversationAnalysis.summaryPrompt: string|null default=null maxLength=10000
$.config.postConversationAnalysis.autoDetectProblems: boolean default=false
$.config.postConversationAnalysis.problemDetectionPrompt: string|null default=null maxLength=10000
$.config.postConversationAnalysis.problemDetectionInstructions: string|null default=null maxLength=10000
$.config.postConversationAnalysis.analysisModel: string|null default=null maxLength=100
$.config.privacyConfig: object|null
$.config.privacyConfig.enableRecording: boolean default=false
$.config.privacyConfig.getRecordingConsent: boolean default=false
$.config.privacyConfig.recordingConsentText: string|null default=null maxLength=500
$.config.privacyConfig.stopSecs: number minimum=0
$.config.privacyConfig.preSpeechMs: number minimum=0
$.config.privacyConfig.maxDurationSecs: number minimum=0
$.config.turnAnalyzerConfig: object
$.config.turnAnalyzerConfig.provider: string enum=["local", "fal"]
$.config.turnAnalyzerConfig.url: string|null format="uri"
$.config.turnAnalyzerConfig.modelPath: string|null
$.config.guardrailsConfig: object|null
$.config.guardrailsConfig.guardrails_enabled: boolean default=false
$.config.guardrailsConfig.custom_rules_enabled: boolean default=false
$.config.guardrailsConfig.rules: array default=[]
$.config.guardrailsConfig.rules[]: object
$.config.guardrailsConfig.rules[].id: string|null
$.config.guardrailsConfig.rules[].type: string enum=["exact", "regex", "dictionary", "structured", "classifier"]
$.config.guardrailsConfig.rules[].pattern: string
$.config.guardrailsConfig.rules[].action: string enum=["refuse", "regenerate", "log_only"]
$.config.guardrailsConfig.rules[].enabled: boolean default=true
$.config.guardrailsConfig.rules[].description: string|null
$.config.guardrailsConfig.rules[].case_sensitive: boolean default=false
$.config.guardrailsConfig.rules[].classifier_threshold: number minimum=0 maximum=1
$.config.guardrailsConfig.refuse_message: string default="I'm sorry, I can't help with that."
$.config.guardrailsConfig.max_regenerate_retries: integer default=2 minimum=0 maximum=5
$.config.guardrailsConfig.regenerate_wait_message: string default="One moment, please."
$.config.guardrailsConfig.classifier_enabled: boolean default=false
$.config.guardrailsConfig.classifier_provider: string
$.config.guardrailsConfig.classifier_model: string
$.config.guardrailsConfig.input_config: object
$.config.guardrailsConfig.input_config.enabled: boolean default=false
$.config.guardrailsConfig.input_config.normalize: boolean default=true
$.config.guardrailsConfig.input_config.detect_injection: boolean default=true
$.config.guardrailsConfig.input_config.detect_weird_input: boolean default=true
$.config.guardrailsConfig.input_config.run_classifier: boolean default=true
$.config.guardrailsConfig.input_config.refuse_message: string default="I'm sorry, I can't help with that."
$.config.guardrailsConfig.input_config.max_input_chars: integer default=8000 minimum=1 maximum=100000
$.config.piiRedactionConfig: object|null
$.config.piiRedactionConfig.enabled: boolean default=false
$.config.piiRedactionConfig.maskEmail: boolean default=true
$.config.piiRedactionConfig.maskPhone: boolean default=true
$.config.piiRedactionConfig.maskPaymentCard: boolean default=true
$.config.piiRedactionConfig.maskNationalId: boolean default=true
$.config.piiRedactionConfig.maskIban: boolean default=true
$.config.piiRedactionConfig.customRules: array default=[]
$.config.piiRedactionConfig.customRules[]: object required=['label', 'pattern']
$.config.piiRedactionConfig.customRules[].label: string minLength=1 maxLength=40 pattern="^[A-Z0-9_]+$"
$.config.piiRedactionConfig.customRules[].pattern: string minLength=1 maxLength=500
$.config.piiRedactionConfig.customRules[].enabled: boolean default=true
$.config.routingLlmConfig: object|null required=['provider', 'model']
$.config.routingLlmConfig.provider: string minLength=1 maxLength=50
$.config.routingLlmConfig.model: string minLength=1 maxLength=100
$.config.routingLlmConfig.baseUrl: string|null maxLength=2048 format="uri"
$.config.routingLlmConfig.apiKey: string|null maxLength=500
$.config.extractionLlmConfig: object|null required=['provider', 'model']
$.config.extractionLlmConfig.provider: string minLength=1 maxLength=50
$.config.extractionLlmConfig.model: string minLength=1 maxLength=100
$.config.extractionLlmConfig.baseUrl: string|null maxLength=2048 format="uri"
$.config.extractionLlmConfig.apiKey: string|null maxLength=500
$.config.backgroundSoundUrl: string|null default=null maxLength=200
$.config.backgroundSoundVolume: number|null default=null minimum=0 maximum=2
$.config.syncSource: object|null required=['url', 'apiKey']
$.config.syncSource.url: string maxLength=2048 format="uri"
$.config.syncSource.apiKey: string maxLength=500
$.config.syncSource.syncedAt: string|null format="date-time"
$.config.testVariables: object
$.config.testVariables{*}: string
```

## Appendix B — Version / branch request bodies (exact)

```
### post /versions/{entityType}/{entityId}
$: object required=['entityId', 'workspaceId', 'configSnapshot']
$.entityId: string minLength=1
$.workspaceId: string format="uuid"
$.configSnapshot: object
$.parentVersionId: string format="uuid"
$.description: string maxLength=5000
### post /branches
$: object required=['entityType', 'entityId', 'name']
$.entityType: string enum=["agent", "workflow", "persona", "kb_document"]
$.entityId: string format="uuid"
$.name: string minLength=1 maxLength=200
$.config: object
$.patch: array minItems=1
$.patch[]: object required=['op', 'path']
$.patch[].op: string enum=["add", "remove", "replace", "test"]
$.patch[].path: string
$.patch[].value: 
$.description: string maxLength=5000
$.baseVersionId: string format="uuid"
$.closesFeedbackIds: array maxItems=500
$.closesFeedbackIds[]: string format="uuid"
### post /branches/{branchId}/commit
$: object
$.config: object
$.patch: array minItems=1
$.patch[]: object required=['op', 'path']
$.patch[].op: string enum=["add", "remove", "replace", "test"]
$.patch[].path: string
$.patch[].value: 
$.name: string maxLength=200
$.description: string maxLength=5000
$.expectedHeadVersionId: string format="uuid"
### post /branches/{branchId}/rebase
$: object
$.publish: boolean
$.force: boolean
$.resolutions: array
$.resolutions[]: object required=['path']
$.resolutions[].path: string
$.resolutions[].editedValue: 
### put /custom/abbreviations
$: object required=['abbreviations']
$.id: string format="uuid"
$.name: string minLength=1 maxLength=200
$.abbreviations: array
$.abbreviations[]: object required=['pattern', 'replacement']
$.abbreviations[].pattern: string minLength=1 maxLength=500
$.abbreviations[].replacement: string maxLength=500
$.abbreviations[].caseInsensitive: boolean
### post /chat/message
$: object required=['message']
$.callId: string format="uuid"
$.agentId: string format="uuid"
$.workflowId: string format="uuid"
$.message: string minLength=1 maxLength=10000
$.customerVariables: object
$.versionId: string format="uuid"
$.fromMessageId: string
```

## Appendix C — Live base workflow tool definitions (verbatim, for copy/paste)

```json
{
 "identity.tools": [
  {
   "type": "function",
   "config": {
    "timeoutMs": 12000,
    "maxRetries": 0
   },
   "function": {
    "name": "verify_identity",
    "handler": "__function__:api_call",
    "parameters": {
     "type": "object",
     "properties": {
      "url": {
       "type": "string",
       "default": "https://freya-bank-mock.freyavoice.workers.dev/v1/verify_identity"
      },
      "body": {
       "type": "object",
       "required": [
        "field",
        "answer"
       ],
       "properties": {
        "field": {
         "enum": [
          "date_of_birth",
          "phone_last4"
         ],
         "type": "string",
         "default": "{{field}}",
         "description": "Which answer this is."
        },
        "answer": {
         "type": "string",
         "default": "{{answer}}",
         "description": "date_of_birth as YYYY-MM-DD, phone_last4 as four digits."
        },
        "customer_id": {
         "type": "string",
         "default": "{{customer_id}}"
        }
       }
      },
      "method": {
       "type": "string",
       "default": "POST"
      },
      "headers": {
       "type": "object",
       "properties": {
        "Content-Type": {
         "type": "string",
         "default": "application/json"
        }
       }
      }
     }
    },
    "description": "Check one security answer. Call it once for each answer the customer gives."
   }
  },
  {
   "type": "function",
   "config": {
    "timeoutMs": 10000
   },
   "function": {
    "name": "end_call",
    "handler": "__function__:end_call",
    "parameters": {
     "type": "object",
     "properties": {
      "message_mode": {
       "type": "string",
       "default": "none"
      },
      "interruptible": {
       "type": "boolean",
       "default": true
      },
      "trigger_pattern": {
       "type": "string",
       "default": "Have a good day|have a good day"
      },
      "trigger_timeout": {
       "type": "number",
       "default": 3
      },
      "trigger_pattern_both": {
       "type": "string",
       "default": "goodbye|bye|have a good day|you too|thank you|thanks|take care"
      }
     }
    },
    "description": "Ends the call. You normally do NOT call this: the phrase \"Have a good day.\" ends the call by itself. Call it without any text only if the customer answers after your goodbye."
   }
  }
 ],
 "collection.tools": [
  {
   "type": "function",
   "config": {
    "timeoutMs": 12000,
    "maxRetries": 0
   },
   "function": {
    "name": "get_account",
    "handler": "__function__:api_call",
    "parameters": {
     "type": "object",
     "properties": {
      "url": {
       "type": "string",
       "default": "https://freya-bank-mock.freyavoice.workers.dev/v1/get_account"
      },
      "body": {
       "type": "object",
       "required": [],
       "properties": {
        "customer_id": {
         "type": "string",
         "default": "{{customer_id}}"
        }
       }
      },
      "method": {
       "type": "string",
       "default": "POST"
      },
      "headers": {
       "type": "object",
       "properties": {
        "Content-Type": {
         "type": "string",
         "default": "application/json"
        }
       }
      }
     }
    },
    "description": "The customer's late products. Call it once, before you say anything about the account."
   }
  },
  {
   "type": "function",
   "config": {
    "timeoutMs": 12000,
    "maxRetries": 0
   },
   "function": {
    "name": "create_promise",
    "handler": "__function__:api_call",
    "parameters": {
     "type": "object",
     "properties": {
      "url": {
       "type": "string",
       "default": "https://freya-bank-mock.freyavoice.workers.dev/v1/create_promise"
      },
      "body": {
       "type": "object",
       "required": [
        "product_id",
        "amount",
        "date"
       ],
       "properties": {
        "date": {
         "type": "string",
         "default": "{{date}}",
         "description": "Payment date as YYYY-MM-DD."
        },
        "amount": {
         "type": "number",
         "default": "{{amount}}",
         "description": "Amount the customer will pay."
        },
        "product_id": {
         "type": "string",
         "default": "{{product_id}}",
         "description": "product_id from the account result, for example p1."
        },
        "customer_id": {
         "type": "string",
         "default": "{{customer_id}}"
        }
       }
      },
      "method": {
       "type": "string",
       "default": "POST"
      },
      "headers": {
       "type": "object",
       "properties": {
        "Content-Type": {
         "type": "string",
         "default": "application/json"
        }
       }
      }
     }
    },
    "description": "Record a payment promise. Call it only after the customer said yes to the read-back of amount and date."
   }
  },
  {
   "type": "function",
   "config": {
    "timeoutMs": 10000
   },
   "function": {
    "name": "end_call",
    "handler": "__function__:end_call",
    "parameters": {
     "type": "object",
     "properties": {
      "message_mode": {
       "type": "string",
       "default": "none"
      },
      "interruptible": {
       "type": "boolean",
       "default": true
      },
      "trigger_pattern": {
       "type": "string",
       "default": "Have a good day|have a good day"
      },
      "trigger_timeout": {
       "type": "number",
       "default": 3
      },
      "trigger_pattern_both": {
       "type": "string",
       "default": "goodbye|bye|have a good day|you too|thank you|thanks|take care"
      }
     }
    },
    "description": "Ends the call. You normally do NOT call this: the phrase \"Have a good day.\" ends the call by itself. Call it without any text only if the customer answers after your goodbye."
   }
  }
 ],
 "workflow.tools": [
  {
   "type": "function",
   "config": {
    "timeoutMs": 12000,
    "maxRetries": 0,
    "alwaysRunsAt": [
     "end_of_call"
    ]
   },
   "function": {
    "name": "close_call",
    "handler": "__function__:api_call",
    "parameters": {
     "type": "object",
     "properties": {
      "url": {
       "type": "string",
       "default": "https://freya-bank-mock.freyavoice.workers.dev/v1/close_call"
      },
      "body": {
       "type": "object",
       "required": [
        "outcome"
       ],
       "properties": {
        "reason": {
         "type": "string",
         "default": "{{reason}}",
         "description": "Leave out for promise. no_promise: refused. not_verified: failed (wrong answers) or declined (would not answer). not_reached: wrong_number or unclear."
        },
        "outcome": {
         "enum": [
          "promise",
          "no_promise",
          "not_verified",
          "not_reached"
         ],
         "type": "string",
         "default": "{{outcome}}",
         "description": "promise: a payment promise was recorded. no_promise: verified customer, no promise. not_verified: the identity check did not pass. not_reached: the right person was not on the line."
        },
        "customer_id": {
         "type": "string",
         "default": "{{customer_id}}"
        }
       }
      },
      "method": {
       "type": "string",
       "default": "POST"
      },
      "headers": {
       "type": "object",
       "properties": {
        "Content-Type": {
         "type": "string",
         "default": "application/json"
        }
       }
      }
     }
    },
    "description": "Record how the call ended, from the conversation."
   },
   "pre_process_code": "try:\n    b = config.get('body')\n    if isinstance(b, dict) and b.get('outcome') in ('promise', 'paid'):\n        b.pop('reason', None)\nexcept Exception:\n    pass\n"
  }
 ]
}
```
