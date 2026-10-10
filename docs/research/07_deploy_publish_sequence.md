# 07 / Gap 1: creating, updating and publishing a workflow agent (the `deploy_to_freya` sequence)

Status: **partly resolved.** GET-only evidence settles the routes, the auth model, what "sync" means and the save/publish semantics. Three questions still need a write call to answer: (1) the exact publish call, (2) whether POST /branches accepts the full graph, and (3) how to create a brand-new workflow. Section 5 gives a tiered experiment for those, ordered from the safest probe up.

Tags: **[SPEC]** = openapi_staging.json / openapi_prod.json (identical path sets, 96 paths). **[OLD-SPEC]** = docs/openapi.json (58 paths, older v2). **[LIVE]** = GET on staging today (2026-10-10). **[UI]** = dashboard i18n strings (research/tools/i18n.json, from webapp/js/0awkrbce61are.js). **[INFERRED]** = my conclusion, not stated anywhere.

Artifacts produced for this gap: `scratchpad/research/gap07/` (scripts/g.sh is a GET-only curl prober that never prints the key; out/route_table.txt, out/i18n_publish.txt, out/i18n_editor.txt; docs/*.md are fetched doc pages).

---

## 1. TL;DR for the builder

1. **Do not use `/workflow/{id}/sync` or `/agents/{id}/sync` to publish.** The UI strings show that "sync" belongs to the *Linked Source* feature. It pulls updates from an upstream `syncSource {url, apiKey}`, and is not a publish switch (§3.3). This corrects the earlier hypothesis.
2. **Calls run the *published* version, and saving does not publish.** Both are [UI]/[SPEC] facts (§3.4). Every deploy must end with an explicit publish step plus a GET that verifies it.
3. **Primary deploy path. It overwrites the seeded workflow, and every step except the publish call is documented:**
   1. `POST /versions/workflow/{workflowId}` with `{entityId, workspaceId, configSnapshot: <full graph>, parentVersionId: <current head>, description}`. Per [SPEC] this is "applied to the working copy" and returns `result.version.isPublished`.
   2. If `isPublished == false`: `PUT /versions/workflow/{workflowId}/{versionId}?entityId={workflowId}&versionId={versionId}&workspaceId={ws}` with **no body**. [INFERRED, strong] This is the successor of the old "Activate agent version" PUT, and it takes no body (§3.5).
   3. Verify with `GET /versions/workflow/{workflowId}?entityId=…&workspaceId=…`: the new version has `isPublished:true`.
4. **Fallback path. It uses the only *documented* publish switch:** `POST /branches` `{entityType:"workflow", entityId, name, baseVersionId, config:<full graph>}`, then `POST /branches/{branchId}/rebase` `{"publish": true, "force": true}`, then check `result.published == true` and verify with GET as above.
5. **Creating a new workflow** is possible only through the *undocumented* dashboard route `POST /api/v2/workflow`. Internal routes do accept API-key auth (§3.2), but this route's existence and body could not be confirmed GET-only. [INFERRED] **Recommend: find the seeded "Freya Bank Collections Base (2 nodes)" workflow by name and overwrite it via 3/4.** Make "create new" an optional, probe-gated feature.

---

## 2. What is settled vs. open

| Question | Answer | Confidence |
|---|---|---|
| Is there a documented create/update route for workflows? | No. The spec has only `GET /workflow`, `GET /workflow/{workflowId}` and `GET /workflow/templates`. | [SPEC] certain |
| Documented way to write a workflow graph | `POST /versions/{entityType}/{entityId}` (entityType `workflow`) or `POST /branches` + `/commit` | [SPEC] certain |
| Shape of `configSnapshot` for a workflow | Exactly the `workflow.workflow` object of `GET /workflow/{id}`: keys `nodes, tools, agent_config, global_nodes, initial_node, system_prompt, test_variables` | [LIVE] certain: v1.configSnapshot == working copy graph (python `==` → True) |
| Do calls/sims use the working copy or the published version? | Published version | [UI] "The version live calls use"; [SPEC] insights PUT says "Calls use the new set once that version is published" |
| Does saving publish? | No. Save and publish are separate steps. | [UI] "Save Changes" vs "Save & Publish"; "Saved successfully, but publishing failed" |
| Does `/workflow/{id}/sync` publish? | Almost certainly not; it is the upstream pull | [UI] strong; permission label conflicts (§3.3) |
| Exact publish call for an existing version | `PUT /versions/{entityType}/{entityId}/{versionId}`, no body | [INFERRED] strong, unverified |
| Does `POST /versions` auto-publish? | Unknown. Read `result.version.isPublished`. | open |
| Does `POST /branches` accept `config` = full graph? | Spec: `config: object (additionalProperties)`, "Forks the edited config off a base mainline version" | [SPEC] says yes in shape; behaviour unverified |
| Can a NEW workflow be created by API? | Only via the undocumented `POST /api/v2/workflow` (dashboard route constant `WORKFLOW_POST`) | open; route existence unproven |
| Does the API key work on non-public routes? | Yes, for GET on internal routes | [LIVE] certain (§3.2) |

---

## 3. Evidence

### 3.1 Spec (exact)

- `POST /versions/{entityType}/{entityId}` (operationId `createVersion`). entityType enum `["agent","workflow"]`. Body (required: `entityId`, `workspaceId`, `configSnapshot`): `entityId` string minLength 1, `workspaceId` uuid, `configSnapshot` object, `parentVersionId` uuid, `description` string ≤5000. **No publish flag.** Description: *"Creates a new version of an entity with the provided configuration snapshot. For entities with a live working copy (agent, workflow) the snapshot is validated against that entity's config schema and applied to the working copy, so an invalid snapshot is rejected with 422."* The 200 result is `version{id, entityType, entityId, workspaceId, versionNumber, parentVersionId, branchId, isPublished, publishedAt, publishedBy, configSnapshot, configDiff, name, description, sourceRunId, createdBy, createdAt}`.
- `PUT /versions/{entityType}/{entityId}/{versionId}` (operationId `updateVersion`). Summary "Update version", description *"Updates version metadata."* **No `requestBody` at all.** Parameters: path `entityType`, `entityId`, `versionId`; query `entityId` (required, string), `versionId` (required, uuid), `workspaceId` (required, uuid). Responses: 200 "Version updated" with the same `version` object; 400/401/403; 404 "Entity or version not found"; 500.
- `POST /branches` (201 "Branch created"). Body required: `entityType` ∈ `agent|workflow|persona|kb_document`, `entityId` uuid, `name` (1..200). Optional: `config` object, `patch` [{`op` ∈ add|remove|replace|test, `path`, `value`}] (minItems 1), `description` ≤5000, `baseVersionId` uuid, `closesFeedbackIds` uuid[] ≤500. The body has no workspaceId and there are no query params. Result: `{success, branchId, branchName, headVersionId, headVersionNumber, skippedFeedbackIds[], attached[], skipped[]}`.
- `POST /branches/{branchId}/rebase` "Merge branch". Body: `publish` bool, `force` bool, `resolutions` [{`path`, `editedValue`}]. Description: *"3-way merges the branch onto mainline, minting a mainline draft (Apply) or publishing it (Apply & Publish). Returns 409 with per-path conflicts when mainline drifted, and 409 with `checksState` and `checks` when the head's checks are failing, running, or not run unless `force` is true."* Result: `{success, newVersionId|null, newVersionNumber|null, published, resolvedFeedbackIds[], conflicts[{path, liveValue, baseValue, branchValue, operation: set|text_diff}], checksState: pass|fail|running|notRun, checks, error}`.
- `GET /branches` requires `workspaceId` (query, required:true). [LIVE] confirmed: without it → 400 `{"field":"workspaceId","message":"Invalid input: expected string, received undefined"}`. With it → 200, `branches: []`. (Research file 01 said "/branches* take no workspaceId at all"; that is wrong for the list endpoint.) `GET /branches/{id}` and `/activity` take no workspaceId: a random id → 404 "Branch not found or access denied".
- `GET /simulations/{simulationId}` returns `publishedVersionId`. `POST /simulations` takes `agentId` (required) plus optional `agentVersionId`. [INFERRED] Simulations default to the published version, and the UI says so: "Publish a new agent version to rerun this simulation".
- `POST /call` `entityVersionId`: "Version ID of the entity (agent/workflow) active during the call". `POST /chat/message` and `/chat/fork` accept `versionId`. So a draft can be pinned for testing.
- Permission labels (live `/permissions`): `versions:publish` = "Publish versions: **Promote a version to current.**". `workflows:sync` = "Publish workflows: Push workflow updates to the runtime." `agents:sync` = "Publish agents: Push agent configuration changes to the runtime." `workflows:write` = "Create and edit workflows."

### 3.2 Live GET probes (scripts/g.sh, curl, bearer key; random id R = 00000000-0000-4000-8000-000000000000)

Python urllib gets Cloudflare **1010 "browser_signature_banned"**. Use curl or httpx with a normal UA. **Builder note: set an explicit User-Agent.**

| GET | Status | Meaning |
|---|---|---|
| `/api/v2/workflow/R/sync?workspaceId=WS` | **405** (empty body) | Route exists, non-GET method only (Next.js framework 405; no handler ran) |
| `/api/v2/agents/R/sync?workspaceId=WS` | **405** | Route exists |
| `/api/v2/branches/R/rebase`, `/branches/R/commit` | **405** | Routes exist (POST) |
| `/api/v2/workflow/test-api-request` | **405** | Exists |
| `/api/v2/totally-not-a-route` | 404 HTML page | Baseline for "no such route" |
| `/api/v2/workflow/R/publish`, `/workflow/R/versions`, `/workflow/R/duplicate`, `/workflow/R/copy`, `/versions/workflow/R/R/publish`, `/agents/R/publish`, `/agents/R/versions`, `/agents/R/versions/rollback`, `/workflows` | 404 HTML | **No hidden publish/duplicate routes.** The old-spec `/agents/{id}/versions*` routes are gone. |
| `/api/v2/workflow/R?workspaceId=WS` | 404 JSON "Workflow not found" | |
| `/api/v2/versions/workflow/R/rollback?...` | 400 `versionId: Invalid UUID` | Matches the `[versionId]` segment. Query/path validation runs **before** the entity lookup. |
| **Internal-only (not in public spec):** `/api/v2/models`, `/api/v2/audio-assets`, `/api/v2/banners`, `/api/v2/knowledgebase/overview` | **200** with API key | **The API key is accepted on dashboard-internal routes**, so the dashboard and the public API share one auth layer [LIVE] |
| `/api/v2/library/schemas`, `/api/v2/simulation-runs` (in the route table) | 404 HTML | **The route table contains stale constants**, so `WORKFLOW_POST` / `WORKFLOW_PUT` existence is *not* proven by the table alone |
| `/api/v2/workflow?workspaceId=WS` without auth | 401 "Missing authorization header. Include 'Authorization: Bearer <token>' header" | |
| Dashboard pages `/workflow`, `/workflow/<id>`, `/docs`, `/shared-call` | 307 → `/login?next=…` | The editor's JS chunks cannot be discovered without a session (§4) |
| Current state: `GET /versions/workflow/252ee2df…` | 1 version: v1 `171f4844…`, `isPublished:true`, `publishedAt:null`, `publishedBy:null`, `branchId:null`; `openBranches:[]` | Unchanged since seeding |

Dashboard route table (`webapp/js/38t45boimpwp0.js`, 258 constants, saved to gap07/out/route_table.txt): `WORKFLOW_GET/POST/PUT/DELETE: "/api/v2/workflow"`, `WORKFLOW_BY_ID_GET/PUT/DELETE: "/api/v2/workflow/[workflowId]"`, `WORKFLOW_SYNC: "/api/v2/workflow/[workflowId]/sync"`, `AGENT_SYNC: "/api/v2/agents/[agentId]/sync"`, `BRANCHES*`, `AGENTS_POST`, `AGENT_BY_ID_PUT`. The table has **no** `/api/v2/versions…` constant, so the editor builds version URLs elsewhere, in chunks we cannot reach.

### 3.3 "sync" = pull from a linked upstream source, not publish

[UI] keys under `saveSplitButton.*` (the editor's Save split-button):
- `checkForUpdates` "Check for Updates", `pullUpdates` "Pull Updates", `pullLatestUpdate` "Pull Latest Update", `updateAvailable` "Update available from upstream", `upToDate` "Already up to date", **`syncFailed` "Sync check failed"**, `pullSuccess` "Updates pulled successfully, new version created", `linkedAgent` "Linked to source."
- `syncSourceModal.description`: "Link this to an upstream source to check for and pull updates. Provide the API URL and an API key from the source workspace." `urlPlaceholder`: "https://example.com/api/v2/agents/uuid". `apiKeyPlaceholder`: "fak_...".
- This matches the config field `agent_config.syncSource {url (uri ≤2048), apiKey (≤500), syncedAt}`. It is `null` in the base workflow [LIVE]/[SPEC].

[INFERRED] `POST /api/v2/{agents|workflow}/{id}/sync` checks or pulls `syncSource` and mints a new version. The permission label "Publish workflows / Push workflow updates to the runtime" is contrary evidence. It may be a legacy name from before versions existed. Either way the request body is unknown and it is not needed. **Do not use it.**

### 3.4 Save vs publish semantics [UI]

- `saveSplitButton.saveChanges` "Save Changes" / `saveAndPublish` "Save & Publish". `publishDialog.description`: "Publishing will make the current draft the active version served to customers."
- `workflows.builder.publishAfterSaveFailed` / `agents.publish.saveSuccessPublishFailed`: "Saved successfully, but publishing failed. Try publishing again". So **Save & Publish is two server calls: save (mints a version), then publish (promotes it).**
- `agents.versionHistory.publishConfirmDescription`: "Publishing v{version} will make it the active version served to customers." `publishedLive` "Version {version} is now live". `active` "Active". `versionsCreatedAutomatically` "Versions are created automatically when you save changes". So **any existing version can be published by id**, and that is the job of a version-level endpoint (`PUT /versions/.../{versionId}`, permission `versions:publish` "Promote a version to current").
- `versionSelector.publishedEntryHint` "The version live calls use". `latestEntryHint` "Follows the newest saved version". `saveToCreate` "Save to create a new version!".
- `workflows.builder.autosave` "Autosaving...". [INFERRED] The working copy (`GET /workflow/{id}` → `workflow.workflow`) can be ahead of the published version, so **never verify a deploy by reading `GET /workflow/{id}`. Verify via the versions list.**
- `branchUi.apply` "Apply" / `applyAndPublish` "Apply & Publish". `mergeGate.summaryPublish` "This will squash-merge the branch into mainline and publish the result." `mergeGate.notRunSummary` "Checks have not run on this branch head. You are merging unverified changes." The last one is why `force:true` is needed.
- `workflows.nodeForm.saveAsInsightTooltip`: "The insight definition is created when you save the workflow." [INFERRED] The dashboard save route may run side effects such as insight creation that `POST /versions` might not. This only matters if the builder uses `saveAsInsight` on extracted variables.
- `workflows.createAWorkflowDescription`: "Create a new workflow from scratch, or generate one from audio, json, or a use case". So the dashboard can create a workflow from JSON, presumably via `POST /api/v2/workflow` [INFERRED].

### 3.5 Why `PUT /versions/.../{versionId}` with no body is most likely "publish"

- [OLD-SPEC] had `PUT /agents/{agentId}/versions/{versionId}`: summary **"Activate agent version"**, description "Sets the specified version as the active version for the agent." It had **no requestBody**, query `workspaceId`, and response `version{…, isActive, …}`.
- [SPEC] replaced it with `PUT /versions/{entityType}/{entityId}/{versionId}`. It has the same structure (path + query ids, **no requestBody**) and returns the same `version` object, with `isPublished` replacing `isActive`. The old "rollback" POST is gone, and live probes confirm no `/publish` route exists anywhere.
- The UI has a per-version "Publish" button (§3.4), and there is a dedicated permission `versions:publish` ("Promote a version to current"). No other spec endpoint can publish an arbitrary *existing* version: rebase publishes only a branch merge.
- The spec is generated from zod route schemas (the duplicated `entityId`/`versionId` query params show the generator reading the route's query schema). So "no requestBody" most likely means the handler ignores the body. [INFERRED]
- Counter-evidence: the summary "Updates version metadata" could mean name/description edits. **Sending `{"isPublished": true}` is harmless if ignored**, so the builder should send that body anyway and judge success only by the follow-up GET.

### 3.6 Data facts relevant to "create"

- The base workflow and its agents-table row share `createdAt` 09:53:27.056Z (one transaction). The workflow's `updatedAt` is .064Z and v1 `createdAt` is .127Z, with v1 `isPublished:true` but `publishedAt:null`. [INFERRED] The create path inserts the workflow + agent row and then writes v1 as published directly, without the publish endpoint. So a newly created workflow is probably live immediately.
- `GET /workflow/{id}` schema [SPEC] fields: `id, name (1..255), description|null, version (1..50), tags[] (default []), workspaceId, template (bool, default false), canvasViewport {x,y,zoom}|null, nodePositions {<nodeId>:{x,y}} (default {}), workflow (any), previewImage|null, isImported, executionCount, createdAt, updatedAt, createdBy, updatedBy, agentId|null`. [INFERRED] The likely `POST /api/v2/workflow` body is `{workspaceId, name, description, version:"2.0.0", tags:[], workflow:<graph>, nodePositions, canvasViewport}`.
- `POST /agents` [SPEC] body is only `{name, language, config}`. It has no `entityType`/`workflowId`, so it creates a plain prompt agent, not a workflow. `GET /workflow/templates` → `templates: []` [LIVE].

---

## 4. Why the editor's JS bodies could not be read (GET-only)

- The dashboard is Next.js App Router + Turbopack with hashed chunk names. `/_next/static/<buildId>/_buildManifest.js` (buildId `2bf9c97b29e919af78e14acda30451a49ae8f96f`) lists only `/_app` and `/_error`, with no app-route→chunk map. `_clientMiddlewareManifest.js` shows that middleware runs on every page except `_next/static`, `_next/image`, `favicon.ico`, `freya-client.js`, `monaco/` and `t/`.
- Every page that would reference the editor chunks (`/workflow`, `/workflow/[id]`, `/workflow/[id]/history`) returns 307 to `/login`. The login page references only 30 chunks: the 27 already downloaded plus 3 locale bundles (`1jswhtwk88y2t.js`, `1lq5s9mq8i6o5.js`, `3q5qkmmers1ep.js`, now in gap07/dl/chunks; they contain translations only).
- Source maps are not public (`.js.map` → 404 on the app, 403 on the CloudFront CDN `d3uhk9wxbz8vlu.cloudfront.net`). S3 listing is denied.
- `freya-client.js` (public) is the WebRTC widget SDK. It contains no workflow or API code.
- **The editor request bodies (`WORKFLOW_POST`, `WORKFLOW_BY_ID_PUT`, `WORKFLOW_SYNC`, version publish) can only be read from a logged-in browser session**, which is out of bounds here, or recovered through the validation-error probes in §5 Tier 0.

---

## 5. Minimal safe experiment (needs explicit user approval: these are non-GET calls)

Each tier is run only if the previous one left the question open. Costs: no call or LLM credits are spent (nothing is dialled or simulated). The only lasting side effect is in Tier 1/2: extra entries in the base workflow's version history. The spec has no DELETE for versions, so these cannot be removed. They are content-identical or trivially different, so behaviour does not change.

Common: `BASE=https://staging-app.freyavoice.ai/api/v2`, `WS=01e07b69-486a-4070-a7fd-3a2fc3120bf2`, `WF=252ee2df-4066-4b87-8fe6-bfecbab03505`, `V1=171f4844-dd70-4dc0-8924-5a8fd0ff2d2b`, `R=00000000-0000-4000-8000-000000000000` (non-existent). Use curl or httpx with a browser-like User-Agent, and read the key from .env inside the command.

### Tier 0: method/schema oracles that cannot change state

0a. `OPTIONS` on `/api/v2/workflow`, `/api/v2/workflow/{WF}`, `/api/v2/workflow/{WF}/sync`, `/api/v2/versions/workflow/{WF}/{V1}`. In Next.js App Router, unless a route defines OPTIONS itself, the framework answers 204 with an `Allow:` header listing the exported methods, and **no handler code runs**. This proves whether `POST /api/v2/workflow` and `PUT /api/v2/workflow/[id]` exist, which the stale route table cannot.

0b. `PUT $BASE/versions/workflow/$R/$R?entityId=$R&versionId=$R&workspaceId=$WS` with body `{"isPublished":"not-a-bool"}`. The ids do not exist, so nothing can change. A **400 with `details`** naming body fields means the body is validated, and the error shows its schema. A **404 "Workflow not found"** means the body is ignored, or validated after the lookup.

0c. `POST $BASE/workflow?workspaceId=$R` with body `{"name":123}`, using a random workspace that is not ours. **400 `details`** reveals the create schema (validation runs before lookup, as observed for GET). 403 means a workspace check ran first, with no information gained. 404/405 means no create route. Nothing can be created in a workspace we do not own. (Do not send `{}` to our own workspace: if all fields have defaults, that could create an "Untitled" workflow.)

0d. Optional, same idea: `POST $BASE/workflow/$R/sync` with `{}`. This would confirm the sync body/semantics if anyone still cares.

### Tier 1: content-identical publish round-trip on the base workflow (answers "how to publish")

1. `POST $BASE/versions/workflow/$WF` with `{"entityId":"$WF","workspaceId":"$WS","configSnapshot":<v1 configSnapshot byte-identical, from live/version_base_v1.json>,"parentVersionId":"$V1","description":"probe: no-op republish"}`. Record `result.version.{id,versionNumber,isPublished}`. If the server rejects a no-change snapshot, that answer matters too: the builder must then skip identical deploys.
2. If `isPublished` is false: `PUT $BASE/versions/workflow/$WF/<v2>?entityId=$WF&versionId=<v2>&workspaceId=$WS` with body `{"isPublished":true}` (or no body).
3. `GET $BASE/versions/workflow/$WF?entityId=$WF&workspaceId=$WS`. Expect v2 `isPublished:true`, `publishedAt` set, `publishedBy` set, and **v1 `isPublished:false`**. Also `GET $BASE/versions/workflow/$WF/graph?entityId=$WF&workspaceId=$WS`.
4. Revert: not needed, because the content is identical. To leave v1 as the live one, `PUT …/$V1?entityId=$WF&versionId=$V1&workspaceId=$WS`, which also proves re-publishing an older version works.

### Tier 2: branch flow, run only if Tier 1's PUT does not publish

1. `POST $BASE/branches?workspaceId=$WS` with `{"entityType":"workflow","entityId":"$WF","name":"probe-<ts>","baseVersionId":"<current head>","config":<head configSnapshot>}`. The query workspaceId is harmless if ignored; the body schema has no workspaceId field. If the server answers "no changes", repeat with a trivial edit, e.g. append `"\n"` to `config.system_prompt`.
2. `GET $BASE/branches/<branchId>`: the merge view with a dry-run conflict preview.
3. `POST $BASE/branches/<branchId>/rebase` with `{"publish":true,"force":true}`. Expect `published:true` and `newVersionId`. A 409 carries `conflicts` / `checksState`.
4. Verify with the same GET as Tier 1.3. Also `GET $BASE/branches?entityType=workflow&entityId=$WF&workspaceId=$WS&status=merged` → `mergedVersionPublished:true`.

### Tier 3: only if a separate workflow is wanted (decide after 0a/0c)

`POST $BASE/workflow?workspaceId=$WS` with the schema learned in 0c, e.g. `{"workspaceId":…,"name":"probe-collections-<ts>","description":"…","version":"2.0.0","tags":[],"workflow":<graph>,"nodePositions":{…},"canvasViewport":null}`. Check `result.workflow.{id,agentId}`, then `GET /versions/workflow/{newId}` (v1 published?) and `GET /agents?entityType=workflow`. Clean up with `DELETE` only after the user confirms. The dashboard constant `WORKFLOW_BY_ID_DELETE` exists; the spec documents only `DELETE /agents?agentId=…`.

**Ask the organizers:** (a) Is the builder expected to overwrite the seeded "Freya Bank Collections Base (2 nodes)" workflow, or to create a new one? (b) Does the grader run simulations against the *published* version of a given agentId, and how does it learn which agent to use (stdout, a file, or the only workflow agent in the workspace)? (c) Is it acceptable to add versions to the seeded workflow's history in the dev workspace?

---

## 6. Recommended `deploy_to_freya` (implementation sketch)

```python
import httpx, time
UA = {"User-Agent": "freya-builder/1.0 (+httpx)"}  # Python-urllib UA is blocked by Cloudflare (1010)

def ok(r):
    j = r.json()
    if r.status_code >= 400 or not j.get("success", False):
        raise RuntimeError(f"{r.request.method} {r.request.url.path} -> {r.status_code}: {j.get('data',{}).get('message')} {j.get('data',{}).get('result')}")
    return j["data"]["result"]

def deploy_to_freya(c: httpx.Client, ws: str, graph: dict, name_hint="Freya Bank Collections Base"):
    # 1. discover (never hard-code ids)
    wfs = ok(c.get("/workflow", params={"workspaceId": ws, "search": name_hint}))
    wf = wfs["workflows"][0]   # [LIVE] list key is "workflows"; rows carry id, agentId, latestVersionNumber
    wf_id = wf["id"]
    vers = ok(c.get(f"/versions/workflow/{wf_id}", params={"entityId": wf_id, "workspaceId": ws, "limit": 1}))
    head = vers["versions"][0]                               # newest first (verify ordering on first run)
    # 2. idempotency: skip if the published head already equals the graph
    cur = ok(c.get(f"/versions/workflow/{wf_id}/{head['id']}",
                   params={"entityId": wf_id, "versionId": head["id"], "workspaceId": ws}))["version"]
    if cur["configSnapshot"] == graph and head["isPublished"]:
        return {"workflowId": wf_id, "versionId": head["id"], "skipped": True}
    # 3. write (documented): applied to the working copy, 422 on invalid snapshot
    v = ok(c.post(f"/versions/workflow/{wf_id}", json={
        "entityId": wf_id, "workspaceId": ws, "configSnapshot": graph,
        "parentVersionId": head["id"], "description": f"builder deploy {time.strftime('%Y-%m-%dT%H:%M:%SZ')}"}))["version"]
    # 4. publish (inferred primary), then the documented fallback
    if not v["isPublished"]:
        try:
            ok(c.put(f"/versions/workflow/{wf_id}/{v['id']}",
                     params={"entityId": wf_id, "versionId": v["id"], "workspaceId": ws},
                     json={"isPublished": True}))           # body likely ignored; harmless
        except RuntimeError:
            pass
    if not _is_published(c, ws, wf_id, v["id"]):
        br = ok(c.post("/branches", params={"workspaceId": ws}, json={
            "entityType": "workflow", "entityId": wf_id, "name": f"builder-{int(time.time())}",
            "baseVersionId": v["id"], "config": graph}))
        mg = ok(c.post(f"/branches/{br['branchId']}/rebase", json={"publish": True, "force": True}))
        if not mg.get("published"):
            raise RuntimeError(f"publish failed: {mg}")
        v = {"id": mg["newVersionId"]}
    # 5. verify from the versions list (NOT from GET /workflow/{id}: that is the working copy)
    assert _is_published(c, ws, wf_id, v["id"]), "deployed version is not the published one"
    return {"workflowId": wf_id, "agentId": wf.get("agentId"), "versionId": v["id"], "skipped": False}

def _is_published(c, ws, wf_id, vid):
    vs = ok(c.get(f"/versions/workflow/{wf_id}", params={"entityId": wf_id, "workspaceId": ws, "limit": 100}))["versions"]
    return any(x["id"] == vid and x["isPublished"] for x in vs)
```

Notes:
- The branch fallback in step 4 forks from the new unpublished head `v`, so the 3-way merge is trivial (base == mainline head) and conflicts are not expected. [INFERRED] If the server says "no changes" because `config` equals the base, fork from the last *published* version instead, using `baseVersionId` = the published id.
- Run the agent-row `PUT /agents/{agentId}` only if agent-level settings outside the graph must change. Workflow-type agents run `graph.agent_config` (see 01 §2.1).
- Smoke test after deploy: one `POST /chat/message {"workflowId": wf_id, "message": "Hello?", "customerVariables": {...}}` without `versionId`. If the reply reflects the new prompts, the runtime is serving the published version. This costs a small amount of LLM credit.
