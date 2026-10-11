# Final submission preparation

Use [FINAL_RUNBOOK.md](FINAL_RUNBOOK.md) for the **one-case Turkish final** on 11 October 2026. The official [submission instructions](https://www.freyavoice.ai/hackathon#submit) now require your own Cloudflare Workers mock and the singular workflow, trace, mock_url and members fields.

The preparation folder contains guarded download, run capture, sealing and submission validation tools, a Cloudflare workspace, the current form template and a readiness record. These are preparation artifacts. The final kit opens at **20:00 Istanbul**, and submission closes at **23:00 Istanbul**.

Start with [preparation-status.json](preparation-status.json) and [FINAL_AUDIT.md](FINAL_AUDIT.md). The builder now accepts the case folder and mock URL; Claude's adaptation checks are in progress. The submission tools pass 31 isolated tests, and the mock preparation guards pass 9 tests. Wrangler and Codex's Cloudflare MCP are authenticated. Member email, final contract/tool binding and final run evidence still need resolution. The supported Python runtime is under runtime. Use [CLOUDFLARE_SETUP.md](CLOUDFLARE_SETUP.md) for login checks. The release follow-up is paused at the user's request.

Use draft.json as the working eight-field one-case form and final.template.json as its clean template. Neither is a ready packet while final values are empty. The earlier draft/readiness are preserved under history; submission.example.json, verification.json, VALIDATION.md and settled-source-snapshot.json are historical two-brief evidence. Use tools/prepare_submission.py for this final.
