# Submission

Use [submission/FINAL_RUNBOOK.md](../submission/FINAL_RUNBOOK.md) for the one-case Turkish final. The current form has
exactly `team`, `members`, `repo`, `commit`, `run_command`, `workflow`, `trace`, `mock_url`.

[draft.json](../submission/draft.json) is the working form, and
[final.template.json](../submission/final.template.json) is the clean template. Final values remain empty until the
released case, deployed mock, selected export/trace and artifact commit exist. The member email still needs filling.

Use [the capture, seal and preparation tools](../submission/tools/README.md) to produce and validate the packet.
`submission/tools/prepare_submission.py` checks the one-case form against sealed artifacts and exact committed bytes.
The earlier `builder.submission` helper and plural workflow/trace form are preserved only as
[historical documentation](../submission/history/SUBMISSION.two-brief.md).

The user sends the prepared email and `submission.json` to the confirmed kit recipient before **23:00 Istanbul**.
See [EMAIL_DRAFT.md](../submission/EMAIL_DRAFT.md), [readiness](../submission/preparation-status.json) and
[final audit](../submission/FINAL_AUDIT.md). The release follow-up remains paused at the user's request.
