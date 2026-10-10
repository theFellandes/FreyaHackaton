# 07 gap 2: Which config drives a workflow call, and which English STT/TTS/LLM/temperature values are valid on staging

Tags: **[LIVE]** = observed via a GET in this session (staging, workspace `01e07b69-486a-4070-a7fd-3a2fc3120bf2`); **[SPEC]** = openapi_staging.json; **[UI]** = web-app i18n strings (`research/tools/i18n.json`); **[INFERRED]** = my reasoning, not stated anywhere.
Raw data: `research/live07_gap2/models.json` (full `GET /models` body, 241 KB) and `research/live07_gap2/models_summary.json`. (A copy is also in `research/live07/`, a folder that another agent is using too.)

---

## TL;DR

1. **`GET /api/v2/models` works with the API key** [LIVE] (HTTP 200, 241,088 bytes; permission `models:read` is in the key's role). It is the model/voice catalog. Query params `language`, `type`, `includeAll` are ignored, so the response is the same with or without `workspaceId`. `GET /admin/models` returns 403 ("Authentication method 'api_key' is not allowed").
2. **The only valid LLM is `experimental-5`** ("Freya YC LLM"). `result.chat` contains exactly one entry: `{"provider":"experimental-5","model":"experimental-5","supportsTemperature":true,"temperatureDefault":0.5,"contextWindow":65536,"recommended":true,"analysisCapable":true}` [LIVE]. **`openai/gpt-4o` (the agents-row LLM) is NOT in the catalog.** Providers `openai`, `anthropic`, `google`, `openrouter` and `experimental-2/3/4` appear in `result.providers`, but no chat model is offered for any of them.
3. **English voice stack, all present in the catalog** [LIVE]:
   - TTS `cartesia` / `sonic-3.6`: `recommended:true`, and `en` is in its `supportedLanguages`.
   - The agents-row voice `9287676d-f0cc-423f-ac03-3b3c7242f091` is in `tts_voices.cartesia` as **"Allen - Modern Voice", `language:"en"`** (male, neutral American).
   - STT `deepgram` / `nova-3`: present, not deprecated, priced at $0.0043/min.
   - Freya STT/TTS has no English option. All 5 Freya voices are tagged `language:"tr"`, `freya-tts` lists no `supportedLanguages`, and the Freya transcription API's language enum is `["tr","cs",null]` [SPEC].
4. **Which config drives a workflow call: the workflow's embedded `agent_config`**, taken from the **published workflow version's `configSnapshot`**. The agents-row `config` of a workflow-type agent is an auto-generated stub that is never versioned. Confidence is high, but no live call has confirmed it (evidence in §2). The per-node "Agent" selector is optional ("No agent" placeholder), the base nodes do not use it, and the builder should not use it.
5. **Practical answer for the builder.** Write the English stack into `workflow.agent_config`, with `llmConfig.provider/model = "experimental-5"` and `temperature` ≈ 0.3. The STT/TTS blocks should be identical to the agents row (deepgram nova-3 `en` + cartesia sonic-3.6 Allen `en`). The voice is then English whichever row the runtime reads, so a wrong guess about the driving row cannot break voice.
6. **Still unresolved without a write or credit spend:**
   - (a) whether staging's cartesia/deepgram credentials actually work at call time;
   - (b) a live confirmation of which row drives;
   - (c) whether `experimental-5` has an allowed-hours schedule. The catalog exposes no schedule field.
   - (d) Whether grading uses voice or chat mode can only be answered by the organizers.

   §6 gives one minimal experiment that settles (a) and (b) together.

---

## 1. `GET /models` catalog [LIVE]

Request: `GET https://staging-app.freyavoice.ai/api/v2/models?workspaceId=<ws>` with `Authorization: Bearer <key>`. The route is the FE constant `MODELS_GET:"/api/v2/models"` (webapp chunk `38t45boimpwp0.js`). It is not in the OpenAPI spec.
Envelope: `{success:true, data:{message:"Success", result:{chat[], audio_speech[], audio_transcription[], tts_voices{}, providers{}}}}`.

Fields per model entry (union): `id, name, provider, model, description, tags, contextWindow, recommended, deprecated, successorModelId, successorModelName, supportedLanguages, settings[], supportsCustomVoice, supportsTemperature, temperatureDefault, analysisCapable, pricing{displayPrice, audioOutput, perMinute, input, output, audioInput}`. **There is no schedule/allowed-hours field.**

### 1.1 `chat` (LLM): 1 entry
| id | provider | model | notes |
|---|---|---|---|
| `experimental-5` | `experimental-5` | `experimental-5` | name "Freya YC LLM", recommended, analysisCapable, supportsTemperature, **temperatureDefault 0.5**, contextWindow 65536 |

### 1.2 `audio_speech` (TTS): 10 entries
| id (= model) | provider | status | supportedLanguages includes `en`? |
|---|---|---|---|
| `freya-tts` | `freya` | recommended | (no list) |
| `sonic-3.6` | `cartesia` | **recommended** | yes (42 langs) |
| `sonic-3` | `cartesia` | ok | yes |
| `sonic`, `sonic-2`, `sonic-turbo` | `cartesia` | deprecated → successor `sonic-3.6` | yes |
| `eleven_flash_v2_5` | `elevenlabs` | recommended; settings speed 0.7..1.2 (1), stability 0..1 (0.5), similarity_boost 0..1 (0.75) | yes |
| `eleven_multilingual_v2` | `elevenlabs` | ok | yes |
| `eleven_turbo_v2_5` | `elevenlabs` | deprecated | yes |
| `gpt-4o-mini-tts` | `openai` | ok; setting speed 0.25..4 | (no list) |

`tts_voices` counts: cartesia 612 (347 `en`), elevenlabs 176 (69 `en`), openai 11 (`alloy, ash, ballad, coral, echo, fable, nova, onyx, sage, shimmer, verse`), freya 5.
Freya voices, all `language:"tr"`:
- `leyla`
- `6ccbf222-…` "Cemre - Professional Support"
- `6ccbf111-…` "Rhea - Multilingual Support"
- `6ccbf333-1111-48f7-b71d-91ac6298247b` "Ivory - Call Center", described as "Warm, professional American English voice" but tagged `tr`. Unverified; do not rely on it.
- `6ccbf444-…` "Meryem"

English cartesia voices that suit a collections call, if a different voice is wanted (all `language:"en"`):
- `9287676d-f0cc-423f-ac03-3b3c7242f091` Allen - Modern Voice (male; **the platform's own choice on the agents row, the safest**)
- `01eaafa9-308a-4276-a017-6ab0cf061b1f` Clara - Instructor (female, "customer support")
- `55deba52-bc73-4481-ab69-9c8831c8a7c3` Camille - Friendly Expert (female, calm)
- `1242fb95-7ddd-44ac-8a05-9e8a22a6137d` Cindy - Receptionist (female)

### 1.3 `audio_transcription` (STT): 39 entries (non-deprecated shown)
- `freya-stt` (provider `freya`) and `freya-stt-continuous` (provider `freya-continuous`). Settings: `streaming`, `keyterms`, `boostTerms`, `boostStrength` 0..3, `keytermMinAudioSec`, `keytermAntiParrot`. No English support is documented anywhere.
- `deepgram`:
  - `nova-3`, `nova-3-general`, `nova-3-medical`: setting `keyterms`, comma list, ≤100.
  - `nova-2` and `nova-2-{atc,automotive,conversationalai,drivethru,finance,general,meeting,phonecall,video,voicemail}`: setting `keywords` `word:boost`.
  - `whisper`, `whisper-{base,large,medium,small,tiny}`.
- `openai` `gpt-4o-transcribe`: setting `prompt`.
- STT entries carry **no `supportedLanguages`**. `en` for deepgram is the value the platform itself wrote on the agents row.

### 1.4 `providers` registry
Keys: `experimental-2, experimental-3, experimental-4, freya, experimental-5, openai, openrouter, experimental, anthropic, google, elevenlabs, deepgram, cartesia, freya-continuous`.
Note: catalog provider ids are **lowercase** (`freya`, `cartesia`, `deepgram`). The platform defaults and the base `agent_config` write `"provider":"Freya"` (capital F), so the runtime matches case-insensitively [INFERRED]. The builder should emit the catalog ids exactly.

---

## 2. Which config drives a workflow call

### Evidence
| # | Evidence | Source |
|---|---|---|
| E1 | The base workflow has one **published** version `171f4844-dd70-4dc0-8924-5a8fd0ff2d2b` (`isPublished:true`). Its `configSnapshot` keys are `nodes, tools, agent_config, global_nodes, initial_node, system_prompt, test_variables`, and its `agent_config` is the Turkish one (`Freya`/`leyla`/`tr`, `experimental-5`, temp 0.7). | [LIVE] `live2/version_wf_v1.json`, `live/versions_base.pretty.json` |
| E2 | The wrapper agent `2f8dcbea-…` has **zero versions** (`GET /versions/agent/2f8dcbea-…` → `versions:[]`). Its English config is therefore part of no version. | [LIVE] `live2/versions_agent.json` |
| E3 | Calls record **`entityVersionId` / `entityVersionNumber`** ("which agent/workflow version ran") plus `assistantEntityType:"workflow"` and `assistantWorkflowId`. Simulations pin `agentVersionId`, chat pins `versionId`. A run of a workflow agent can only pin a workflow version, whose snapshot holds only `agent_config`. | [SPEC] (`research/03` §call fields; `tools07/getcall_schema.txt`) |
| E4 | `PUT /custom/abbreviations` says "on an agent or on a **workflow's embedded agent config** … creates a new version". The embedded config is the versioned, editable runtime config of a workflow. | [SPEC] |
| E5 | The workflow editor has its own "Agent" settings panel: `workflows.builder.agentConfig` = "Agent", `invalidFieldsAgentSettings` = "Agent settings". Copy/paste of it says: "**Only agent settings (voice, models, turn-taking…) will be replaced. The workflow steps stay the same.**" (`workflows.paste.settingsConfirmDescription`) and "This workflow has no agent settings to copy". Voice and models therefore live in the workflow's own config. | [UI] |
| E6 | Node-level overrides talk about the "agent-level" setting. Examples: "Override the agent-level Freya STT keyterm/boost settings while this node is active. Empty fields inherit the agent default." (`sttOverrides.sectionTooltip`), and "Auto (follow agent setting)" for Smart Turn and confidence re-ask. Routing/extraction override hints say "Leave empty to use the **workflow's configured** routing model". | [UI] |
| E7 | The agents-row config is a **generic auto-generated stub**: `openai/gpt-4o` (not in the `/models` chat catalog), `firstMessage` "Hello! How can I help you today?", generic systemPrompt. It was created in the **same millisecond** as the workflow (`createdAt` 2026-10-10T09:53:27.056Z for both) and never updated. The audit log has `WORKFLOW_CREATED` (user agent `Mozilla/5.0 freya-cli`) and **no `AGENT_CREATED`/`AGENT_UPDATED`** event. The row was therefore a side effect of workflow creation. | [LIVE] `live/agent_base.json`, `live07/audit_logs.txt` (written by another agent) |
| E8 | The workflow's `agent_config` equals `GET /agents/defaults` (Turkish platform defaults) except `llmConfig.firstMessage`, which the seeder customised to "Hello, this is the digital assistant of Freya Bank. Am I speaking with {{title}} {{last_name}}?". So the seeder deliberately edited `agent_config` and left the agents row alone. | [LIVE] `live/agents_defaults.json` vs `live/workflow_base.json` |
| E9 | The runtime is Pipecat ("pipecat's resume path degrades gracefully", `/chat/fork` description). The graph format (`role_message`, `task_message`, `functions`/`tools`, `pre_actions`, `transitions`) is pipecat-flows style: one pipeline whose STT/TTS/LLM services come from one agent config. | [SPEC] + [INFERRED] |

**Conclusion [INFERRED, high confidence]:** for a workflow-type agent, the runtime voice/LLM config is `workflow.agent_config` from the published (or pinned) workflow version. The agents-row `config` is vestigial; if anything reads it, it would be legacy code. Research file 01's "vestigial" call stands, now with E1–E8 behind it.

### The per-node "Agent" selector and the "Missing agent" warning
- `workflows.nodeForm.agent` = "Agent", `agentPlaceholder` = **"No agent"**, `agentTooltip` = "Select an agent whose voice and LLM config will be used for this node." [UI]. The default is "No agent", so the field is optional. With no node agent, the node uses the workflow's Agent settings (E5/E6) [INFERRED].
- The base nodes have **no agent field at all**. The node keys are `name, tools, on_enter, position, node_type ("conversation"), assertions, passthrough, pre_actions, transitions, role_message, task_message, interruptible, max_node_turns, response_mapping, extraction_schema, max_turn_transition, truncation_strategy, suppress_tool_results, truncation_window_size, on_enter_first_entry_only, disable_inactivity_monitor, passthrough_default_transition, return_to_previous_on_interrupt` [LIVE]. The spec defines no node schema, so the JSON key the selector writes is **unknown**.
- `workflows.builder.missingAgent` ("Missing agent") sits in the "Incomplete Nodes" list next to `missingRoleMessage` ("Missing role prompt"). Both base nodes have `role_message:""`, so if these warnings fired for every conversation node, the organizers' base would show both. The warning is advisory ("Running the workflow now will **potentially** result in errors") and most likely belongs to the legacy v1 "Agent node" editor (`workflows.agent.*`: "Role Messages", "Task Messages", "Select an agent..."). The workflow format is now `version: "2.0.0"` [INFERRED].
- A separate **invoke-agent node** exists: "When this node fires, control transfers to the selected agent. The agent's voice, LLM, and prompts swap in for the duration of its execution." Single-level only; a workflow cannot invoke its own agent [UI]. It is not needed.
- **Recommendation:** do not set node agents. They would require creating extra regular agents, use an unknown key name, and add a second config source that can drift.

---

## 3. Recommended `agent_config` voice/LLM blocks (English)

The values come from the catalog and from the agents row the platform generated. All provider/model/voice ids are verified present in `GET /models` [LIVE].

```jsonc
"llmConfig": {
  "provider": "experimental-5", "model": "experimental-5",   // only catalog chat model
  "temperature": 0.3,                                         // [SPEC] number 0..2 (spec default 0.7; catalog temperatureDefault 0.5)
  "maxTokens": 1000, "firstMessageMode": "assistant-speaks-first",
  "disableInterruptionsDuringFirstMessage": true,
  "apiKey": null, "baseUrl": null, "clientType": null,
  "systemPrompt": "<non-empty>", "firstMessage": "<from brief>", "additionalSettings": {}
},
"sttConfig": {
  "provider": "deepgram", "model": "nova-3", "language": "en",
  "apiKey": null, "baseUrl": null,
  "additionalSettings": {"keyterms": "Freya", "punctuate": true, "diarize": false, "krispEnabled": true, "aicEnabled": false},
  "confidenceReask": {"enabled": false, "reaskMode": "phrase", "reduction": "min", "threshold": 0.3, "reaskPhrases": null, "disableOnDigitNodes": true, "maxConsecutiveReasks": 2}
},
"ttsConfig": {
  "provider": "cartesia", "model": "sonic-3.6", "voiceId": "9287676d-f0cc-423f-ac03-3b3c7242f091", "language": "en",
  "additionalSettings": {"speed": 1, "stability": 0.5, "similarity_boost": 0.75},   // harmless leftovers (these are ElevenLabs keys; cartesia models list no settings)
  "startSpeakingPlan": {...English, see 01 §3.2...}, "stopSpeakingPlan": {...English phrases, see 01 §3.2...},
  "textSubstitutions": [...], "speechNormalization": {...}
},
"routingLlmConfig": null, "extractionLlmConfig": null
```
Notes:
- Keep `routingLlmConfig` / `extractionLlmConfig` = `null`. [UI] `agents.configSections.routingLlmDescription`: "When disabled, the agent's LLM is used, or the platform default if that LLM is not OpenAI-compatible." The agent LLM and the platform default (`GET /agents/defaults` → `experimental-5`) are the same model, so routing and extraction run on `experimental-5` either way [INFERRED]. If you do set them, the shape is `{provider, model, baseUrl?, apiKey?}` with provider and model required [SPEC].
- `smartTurnEnabled: true` sits in the Freya STT `additionalSettings` of the base. The platform-generated deepgram block omits it. Smart Turn in Pipecat is provider-agnostic [INFERRED]. Adding it is optional; leave it out unless a voice simulation shows turn-taking problems.
- Deepgram language: use `"en"`, as observed. The spec default is `"multi"`. The catalog gives no STT language list.
- Temperature 0.3 is my recommendation [INFERRED]. The goal is consistent figures and gate compliance; 0.3 trades a little naturalness for that. Anything in 0..2 is valid per the spec.
- Bring-your-own LLM is technically possible: `llmConfig.baseUrl` ("OpenAI-compatible API endpoint" [UI]) + `apiKey` + `clientType` `"openai"|"freya"` [SPEC]. It is not recommended: it embeds a secret in the agent config and is outside the scored platform path.

---

## 4. Model choice and the time-gating risk

- [UI] `agents.unavailableModelNotice`: "{model} is outside its allowed hours ({schedule}). Calls run on the default model until it reopens." [SPEC] `POST /agents` and `PUT /agents/{id}` can return **422** "The config selects a model that is outside its allowed hours".
- The API-key catalog exposes **no schedule** for `experimental-5`. The schedules are probably in the admin catalog (`/admin/models`, 403 for API keys).
- `experimental-5` is (a) the only chat model, (b) the platform default (`GET /agents/defaults` → `llmConfig.provider/model = "experimental-5"`), and (c) what the base runs. The "default model" fallback is therefore `experimental-5` itself or an internal equivalent, and hard-coding it cannot switch you to a worse model unless the platform default changes [INFERRED].
- Builder hardening, all GET-only at build time:
  1. `GET /models` → choose the `chat` entry with `recommended:true` (else the first). Assert that the chosen TTS model has `"en"` in `supportedLanguages` and that the voiceId exists in `tts_voices[provider]` with `language:"en"`, and that the STT model is present and not `deprecated`.
  2. Fall back to `GET /agents/defaults` → `llmConfig` if `/models` is unavailable.
  3. If a write returns 422 (allowed hours / schema), retry with the default LLM.
  4. Log `iteration.agentLlm` / `runAssistantLlms.recorded` from simulations to detect silent substitution.
- Simulations can override only the LLMs (`assistantLlmOverride`, `routingLlmOverride`, `extractionLlmOverride`, persona/analysis). There is **no STT/TTS override** [SPEC]. In voice mode a grader therefore always uses our STT/TTS, and could replace our LLM via `assistantLlmOverride`.

---

## 5. Voice or chat grading: unresolved

- The simulation `mode` defaults to `"voice"` [SPEC]. The task says "phone agent". Chat mode skips STT/TTS/VAD [UI: "Choose whether to simulate voice calls or text chat conversations."].
- Configuring English STT/TTS costs nothing in chat mode. Leaving Turkish STT/TTS would very likely ruin voice runs: English speech would go through Turkish STT, and the agent would speak in a Turkish voice. **The builder should always write the English stack.**
- **Question for the organizers:** "Are graded simulations run with `mode: "voice"` or `"chat"`, and do you set `assistantLlmOverride`?" Nothing in the docs, spec or workspace answers this.

---

## 6. Minimal safe experiment (needs the user's approval: it writes to the workspace and spends credits)

Budget: 20 credits ≈ $10 (`creditUsdRate 0.5`). Deepgram costs $0.0043/min and cartesia ~$31 per 1M chars [LIVE catalog]. A 60–90 s voice run should cost well under $1 [INFERRED].

One experiment answers both "which row drives" and "do cartesia/deepgram work on staging":
1. **Write one workflow version** with the §3 English `agent_config` (via the write path chosen in the workflow-write research, e.g. `POST /branches` + `/rebase {publish:true, force:true}`, or `POST /versions/workflow/<id>`).
   - Deliberately pick a **different, female** cartesia voice from the agents row, e.g. `01eaafa9-308a-4276-a017-6ab0cf061b1f` (Clara).
   - Leave the agents row untouched: it stays openai/gpt-4o + male Allen.
2. **Create one persona**: `POST /personas {personas:[{name:"probe-en", prompt:"You are John Smith. Speak English. Confirm your name when asked, then say you must go and hang up.", successCriteria:"Agent greets in English"}]}`.
3. **Run one voice simulation**: `POST /simulations {name:"probe-voice", agentId:"2f8dcbea-fad5-4c09-ae0b-35cff67e9a14", selectedPersonaIds:[<id>], mode:"voice", runsPerPersona:1, maxConcurrentCalls:1, maxCallDurationSeconds:60, maxRetries:0, customerVariablesMap:{<id>:{customer_id:"<test id>", title:"Mr", last_name:"Smith"}}}`. Optionally pin `agentVersionId` to the new version.
4. **Read the results (GET only)**:
   - `GET /simulations/{id}/iterations`: `agentLlm` = `experimental-5` means `agent_config` drives; `openai/gpt-4o` means the agents row drives.
   - `GET /call/{callId}`: check `modality:"voice"`, `entityVersionId` = the new workflow version, clean English user transcript (deepgram works), assistant turns present, `endedReason` not a provider error.
   - `GET /call/{callId}/cost-events` (the route exists): which TTS/STT providers were billed [INFERRED field content].
   - `GET /call/{callId}/recording`: the user listens. A **female** voice means `agent_config` drives and cartesia works. Allen (male) means the agents row drives. Silence or an error means no cartesia credentials.

A cheaper partial check (text only, a few cents, no config write): `POST /chat/message {workflowId:"252ee2df-4066-4b87-8fe6-bfecbab03505", message:"Hello", customerVariables:{…}}` → `POST /chat/end` → `GET /call/{callId}`. Expect `assistantEntityType:"workflow"` and `entityVersionId:"171f4844-…"`. This confirms version pinning (E3) but tests neither STT/TTS nor provider credentials.

---

## 7. Answers to the assignment's sub-questions

| Question | Answer | Status |
|---|---|---|
| Which config drives a workflow call? | `workflow.agent_config` of the published/pinned workflow version; agents-row config is a never-versioned stub | INFERRED, high confidence (E1–E9); live confirmation needs §6 |
| Does the per-node Agent selector mean the agents row drives nodes? | No. It is an optional per-node override ("No agent" default); base nodes don't set it; "Missing agent" is advisory/legacy | INFERRED |
| Valid LLM provider/model on staging | only `experimental-5` / `experimental-5` | LIVE (`GET /models`) |
| Is `openai/gpt-4o` valid? | Not in this workspace's catalog | LIVE |
| Valid English TTS | `cartesia` `sonic-3.6` (recommended) / `sonic-3`; `elevenlabs` `eleven_flash_v2_5` / `eleven_multilingual_v2`; `openai` `gpt-4o-mini-tts`; voice `9287676d-f0cc-423f-ac03-3b3c7242f091` (Allen, en) verified | LIVE (catalog); runtime credentials unverified |
| Valid English STT | `deepgram` `nova-3` (also `nova-3-general`, `nova-2-phonecall`, …), `openai` `gpt-4o-transcribe`; language `en` | LIVE (catalog); runtime credentials unverified |
| Freya-native English? | No: all Freya voices `tr`; Freya transcription supports `tr`, `cs` | LIVE + SPEC |
| Temperature knob | `agent_config.llmConfig.temperature`, number 0..2, spec default 0.7, catalog `temperatureDefault` 0.5, `supportsTemperature:true`; recommend ~0.3 | SPEC + LIVE; value INFERRED |
| Routing/extraction LLM | leave `null` → agent LLM / platform default = `experimental-5` | UI + INFERRED |
| Time-gating | no schedule visible to API keys; `experimental-5` is also the default, so fallback ≈ same model | INFERRED |
| Voice or chat grading | unknown; ask organizers; build English voice stack regardless | OPEN |
