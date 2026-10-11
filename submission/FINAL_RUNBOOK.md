# Final submission procedure

The final on Sunday 11 October 2026 is **one Turkish credit card statement debt case**. Submit one phone agent, one matching run trace and the address of your own Cloudflare Workers bank mock. The official [Submit, Rules and FAQ](https://www.freyavoice.ai/hackathon#submit), checked on 11 October, replace the older two-brief procedure in this repository.

Preparation is incomplete. The final opened at **17:00 UTC / 20:00 Istanbul** and the deadline is **20:00 UTC / 23:00 Istanbul**. The existing version 3 ZIP in data is practice material.

At 17:21 UTC the shared [release folder](https://drive.google.com/drive/folders/1r9KOek3SXiE0mu1wBofezD0bjiAJhcYQ) contained only BRIEF.md and statement-debt-call-flow.pdf. Their unchanged copies are in `submission/case`; this is a **partial case**, missing README, the integration contract, customer fixtures and remaining sources. See `submission/evidence/final-case-acquisition.json`. Do not accept a final build or implement guessed bank endpoints from these two documents. Add the remaining original pack files here once available, preserving their bytes and provenance.

## Before release

- [ ] Provide the member email for the official members field.
- [x] Wrangler and Codex's Cloudflare MCP are authenticated. See [CLOUDFLARE_SETUP.md](CLOUDFLARE_SETUP.md) for future login checks. Keep API tokens out of chat, traces and Git.
- [x] Create an isolated supported builder runtime. submission/runtime/Scripts/python.exe uses Python 3.12.14 with the existing requirements installed; pip check and builder --help passed. The original .venv remains preserved with its missing base interpreter.
- [x] The current builder accepts a case folder and mock URL, classifies document roles and can derive OpenAPI from a prose service contract. The adaptation is still being checked by Claude; review [FINAL_AUDIT.md](FINAL_AUDIT.md) before treating it as verified.
- [ ] Confirm Markdown, Word, PDF and Excel extraction on the released inventory. Compare every derived HTTP method/path and request/response field with the original service contract before accepting the run.
- [ ] Ensure tools use the supplied mock origin and the actual contract authentication. Fixed contract header values are compiled; unspecified credentials and security schemes are not automatically filled from FREYA_BANK_TEAM_KEY. Verify the final contract's intended binding.
- [ ] Record the builder source snapshot before 17:00. Preserve existing work; never mass-stage unrelated changes.
- [x] The released BRIEF.md confirms yavuz@freyavoice.ai. Attach submission.json, the exported workflow and the trace. The user sends the prepared email themselves.

Unchecked items are readiness blockers. The existing two-brief builder.submission checker must not be used to create the new final form. Use Cloudflare Free services for the mock; this audit has not enabled a paid plan.

## At 20:00 Istanbul

1. Run tools/download_release.py using a working Python. It checks the release time, discovers a final ZIP in the official Submit section, refuses the practice v3 ZIP and preserves the download plus SHA256 hashes. If the link arrives only by email, pass --url with that final link. Each download gets a separate incoming folder.
2. Read **BRIEF.md first**, then README.md. The helper prints them in that order. Follow the actual inventory and contract; keep originals unchanged.
3. Implement the bank service from the final service contract and test customers in mock-bank. Its current bootstrap is a preparation placeholder and is not deployable as a functioning bank.
4. Implement persistent call/customer state, the actual error behavior, required authentication and evaluator-facing records. Test the contract's success and failure paths. Do not import practice endpoint names, identity limits or outcome values without checking the final contract.
5. Deploy to Cloudflare Workers. Record the deployed origin, deployment/version ID, source/fixture/contract hashes, contract test results and persistent-storage check in a deployment receipt.
6. Probe the live deployment and confirm tool calls will reach it. Deployment and successful contract checks must precede the selected builder run.

## Builder runs and selection

Run one command with the released case folder and deployed mock URL after the adaptation checks pass. The official example is:

    python3 builder/build.py {case_folder} {mock_url}

Use tools/capture_run.py to keep everything printed to stdout and stderr in a unique raw trace and record the exact command, UTC times and exit status. Its --help describes the actual arguments. The wrapper is only capture infrastructure; it is not a builder.

Use `submission/runtime/Scripts/python.exe` explicitly on this Windows machine. The exact capture, readiness and sealing commands are in [tools/README.md](tools/README.md). Preserve the downloaded originals under incoming, and run from a reviewed case folder whose exact input files can be committed; packet validation requires the case files and deployment evidence in the submitted commit.

Preserve every run. Select one successful run whose workflow export, builder result, platform IDs and trace agree. The builder already calls freya.export_workflow through its Freya client. If export did not succeed, retry through the builder and capture a new run. Do not edit the phone agent, export, policy, tool binding or prompt after the selected run.

Inspect the bank contract and policy extraction, tool URLs and authentication, publication/export evidence, and self-test failures before accepting the run. A platform error does not automatically prove that an agent is correct.

Use tools/seal_run.py to create an immutable copy of the selected trace/export/result and a hash manifest. These utilities verify local evidence, not hidden-customer success. The original raw captures remain ignored until inspected for API keys. The official instructions permit removal of API keys before commit; any necessary redaction must be explicit, must preserve the private original and must be recorded. Never silently claim a changed trace is the byte-identical raw capture.

## Commit and email

1. Inspect exports and traces for external API credentials without printing secret values. Preserve synthetic bank credentials only when the actual contract requires evaluator access.
2. Stage the selected artifacts, their evidence, the actual builder source and necessary mock source only. Protect submitted raw bytes from Git newline conversion and filters using the sealed folder attributes.
3. Commit the complete code and selected artifacts. Verify workflow and trace bytes against the Git blobs at that commit. The submitted commit must contain the source used for the selected run.
4. Use tools/prepare_submission.py with the singular final template and sealed receipt. It refuses incomplete or inconsistent packets.
5. Write submission.json only after those checks pass. It references the artifact commit; committing a JSON that contains its own resulting commit SHA would be circular.
6. Push the referenced commit and verify the organisers can access the repository and paths. Keep secrets and unreviewed raw logs out of the remote.
7. The user sends submission.json to the confirmed kit recipient before **23:00 Istanbul**, preferably by **22:30**. Record the sent-message ID/time or retain the actual sent email. The email draft is wording only until the final packet exists.
8. Keep the deployed bank Worker and persistent storage available until results are published. Do not reset, reseed, remove or replace it during evaluation without instructions from the organisers.

## Files

| Location | Purpose |
|---|---|
| final.template.json | New official single-case form with deliberately empty final values |
| draft.json | Working single-case form; final values stay empty until evidence exists |
| preparation-status.json | Dated actual readiness and blockers |
| tools/download_release.py | Guarded final-kit download and extraction |
| tools/capture_run.py | Complete builder console capture in separate runs |
| tools/seal_run.py | Copies selected artifacts and verifies hashes |
| tools/prepare_submission.py | Validates the single-case form and commit |
| mock-bank | Cloudflare configuration and implementation checklist |
| incoming | Immutable release downloads and extraction |
| runs | Private raw builder captures |
| sealed | Reviewed selected artifact copies and evidence |
| history and old verification files | Historical two-brief preparation; superseded for this final |

The release follow-up is **paused at the user's request**. Follow this runbook manually at 20:00 Istanbul. The user sends the email themselves.

No final workflow, final trace, deployed mock, artifact commit or sent email exists yet.
