# Final submission procedure

The released final is one Turkish credit card statement debt case. The deadline is **11 October 2026, 20:00 UTC / 23:00 Istanbul**. The user sends the email. The release follow-up remains paused.

## Released inputs and bank service

The complete, unchanged 18-file release is in `submission/case`. This includes BRIEF.md, README.md, the flow PDF, policy sources, service contract, examples, mock example and both customer files. `submission/evidence/final-case-acquisition.json` records their hashes and acquisition provenance. Preserved downloads are under `submission/incoming`. No further download is needed.

The live bank mock is:

    https://freya-hackathon-bank-oguzhan.freya-submission-bank-preparation.workers.dev

It implements the seven released POST/JSON operations, both canonical `/voice/v1/` paths and root aliases, and the original fixture responses and delays. All 617 response fixtures across the 34 supplied customers passed live comparisons; the complete live suite passed 668 checks. SQLite Durable Objects preserve state across requests and process restarts. `submission/mock-bank/deployment-receipt.json` binds the deployment version, compiled source, contract, fixtures and verification evidence.

Wrangler is authenticated. No paid Cloudflare upgrade was enabled. Keep this Worker and its storage available until results are published. Do not reset, reseed or replace evaluation state. See `mock-bank/CALL_COMPATIBILITY.md` for request and session behavior.

## Builder and verification

Use the healthy Windows interpreter `.venv-playground/Scripts/python.exe` (Python 3.12.14). The builder's portable final command is:

    python3 -B builder/build.py {case_folder} {mock_url} --slot final-statement --track compiled --self-test none --budget-s 1200 --max-policy-repairs 4

The supplied case folder and mock URL are the only placeholders. The generic service path reads the original policy and contract, validates source evidence and response dependencies, and requires an independent source-policy review before deployment. The deterministic compiler constructs the platform workflow and actual API requests. Preserve every failed and successful capture; never edit generated plans, prompts, workflows or exports to repair a final agent.

Collection-specific self-tests do not apply to this inbound service case. `--self-test none` therefore does **not** mean that live phone-agent verification passed. Record actual Freya conversations separately with `tools/check_phone_chat.py`. Check policy amounts and dates, card selection, registered-address consent, preview and consent before a week change, linked cards, no-data and error routes, instruction injection, and the 12-second fixture delay. A deployed export alone is insufficient for selecting a final run.

Before the selected build, commit the exact runtime source and verify its raw bytes against Git. Probe the deployed mock using `mock-bank/tools/verify-ready.mjs`. Then use `tools/capture_run.py` to capture the complete console bytes, exact argv, UTC times, source/input hashes, mock probe and builder result. Source and case files must stay unchanged throughout the captured run. See `tools/README.md` for the capture command.

Select a successful, published run only after source review and live conversations pass. Workflow IDs, published version, export, result and trace must agree. Rebuild through the builder if a generic repair is needed; do not patch the export.

## Seal, commit and prepare the email

1. Check the selected export, complete trace, result and supporting evidence for configured secrets without printing their values. Preserve private originals if explicit known-secret redaction is necessary.
2. Use `tools/seal_run.py seal` with the capture manifest, platform export, raw console trace and build result. The seal copies exact artifact bytes plus deployment/readiness evidence into a new folder under `submission/sealed`. Run `seal_run.py verify` on that folder.
3. Stage only the reviewed builder source, original case, necessary mock source/evidence, selected seal and relevant documentation. Preserve unrelated work and private diagnostic captures. Commit, then verify every selected artifact and captured runtime source against raw Git blobs. Push the referenced commit and verify the remote commit.
4. Fill the eight official fields in `submission/submission.json`: `team`, `members`, `repo`, `commit`, `run_command`, `workflow`, `trace`, `mock_url`. The workflow and trace paths point to the sealed artifacts. The commit contains those exact artifacts and source. Create this JSON after the artifact commit to avoid a circular self-reference.
5. The user completes the member contact address before sending. Do not invent one or reuse an account address. Run `tools/prepare_submission.py validate` with the configuration and selected `seal.json`; its strict validator must pass after the contact field is completed.
6. The released BRIEF.md confirms **yavuz@freyavoice.ai**. The user attaches **submission.json, the exported workflow and the matching builder trace**. `EMAIL_DRAFT.md` contains the wording. Send before **23:00 Istanbul**; target 22:30 when possible.

`preparation-status.json` records current completion and remaining blockers. `FINAL_AUDIT.md` and `submission/history` contain historical preparation evidence; they do not establish live success for the final case.
