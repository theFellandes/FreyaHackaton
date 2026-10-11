# Single-case run evidence

These standard-library Python tools retain the output and evidence of one selected builder run. They never deploy a
Worker, edit the builder or a phone agent, stage files, commit, push, or send email. The final packet generator is
`prepare_submission.py`; the older two-brief `builder.submission` helper is not the current final contract.

## Capture

First deploy the mock bank and run `submission/mock-bank/tools/verify-ready.mjs` with its actual deployment receipt and
`--report` destination. Capture requires the receipt and saved verifier report. Use the health URL named by that receipt;
no banking endpoint is inferred. The receipt's contract/customer-fixture hashes must identify original files present in
the actual case folder. Adapter outputs belong in the deployed bundle and source evidence.

From the repository root, after setting these variables to the reviewed release/deployment values:

```powershell
$Python = '.venv-playground/Scripts/python.exe'
# Set $CaseFolder, $MockUrl, $ProbeUrl, $DeploymentReceipt, and $ReadinessReport from the actual kit and deployment.
& $Python -B submission/tools/capture_run.py --cwd $PWD.Path --case-folder $CaseFolder --mock-url $MockUrl --probe-url $ProbeUrl --deployment-receipt $DeploymentReceipt --readiness-report $ReadinessReport -- $Python builder/build.py $CaseFolder $MockUrl
$BuilderExit = $LASTEXITCODE
```

Everything after `--` is the exact child argument list. Use the approved builder entrypoint/arguments for the released
kit. The sample uses the documented single-case command. The wrapper does not add flags or reconstruct a shell command.
It records that argv and the working directory; keep credentials in the child environment, never in argv or URLs.

Each invocation creates `submission/runs/<UTC-time>-<UUID>/` and never overwrites a prior run. `console.raw.log` receives
stdout and stderr through one shared native file handle, without decoding, line-ending conversion, banners, or added
newlines. Exit status and UTC start/end times are saved separately in `run.json`. Capture has no terminal emulation or
live tee; the saved file can be viewed without modifying it. A terminal-only application needs a separately reviewed
capture method. The documented Python builder writes ordinary stdout/stderr.

Before launching, capture verifies receipt/report bytes, deployment input evidence, explicit live `ready`/`bank_ready`,
Cloudflare version ID and deployed input hashes. HTML or a generic HTTP 200 does not pass. It hashes builder code,
prompts, `.claude/skills`, the reference Freya client, dependency manifests, and case files before and after the child.
The raw capture also retains the final builder JSON and immediately hashes the reported platform export and builder
trace. A source or case change during the run makes that run ineligible for sealing.

Raw runs, the temporary local runtime, and incoming downloads are ignored by `submission/.gitignore`. Raw incoming
receipts may contain signed download URLs. Retain reviewed, required source/deployment evidence separately for the final
commit. An interrupted or failed launch keeps its evidence and cannot become a selected packet.

## Vet, seal, verify

Review the complete output and select a successful, complete run. Supply the actual export and corresponding build-result
file from that run; no workflow content is regenerated or reformatted. Resolve known secrets before committing. The
scanner checks recognized key shapes and any secret values supplied through repeated `--secret-env NAME` options; it
cannot identify every possible credential. Those environment values are never printed or persisted.

If the trace needs key removal, create a separate byte projection in the ignored run directory:

```powershell
& $Python -B submission/tools/seal_run.py redact --run-manifest $RunManifest --output $VettedTrace --secret-env FREYA_API_KEY
```

`redact` replaces only configured secret substrings and recognized key-shaped tokens. It refuses an existing output
file and leaves the raw capture untouched. Inspect the result. Sealing a different trace requires
`--allow-known-secret-redaction` and verifies that its only differences are exactly that projection. Arbitrary edits,
missing output, reordered lines, and newline conversion are rejected. A workflow export containing credentials is refused;
phone-agent content is never edited by these tools.

```powershell
& $Python -B submission/tools/seal_run.py seal --run-manifest $RunManifest --workflow $FreyaExport --trace $VettedTrace --build-result $BuildResult --output $FreshSealDirectory --vet-trace
& $Python -B submission/tools/seal_run.py verify $FreshSealDirectory
```

For an unchanged clean capture, pass `console.raw.log` as the vetted trace. Add the same `--secret-env` options used for
redaction and `--allow-known-secret-redaction` when required. Every seal directory must be new. It contains exact copies
of `workflow.json`, `trace.log`, `build-result.json`, `capture.json`, `deployment-receipt.json`, and
`readiness-report.json`, plus `seal.json`, `.gitattributes`, and `.gitignore`. The seal binds file sizes/SHA-256 hashes,
builder/workflow/agent/version identities, service readiness and source provenance. Verification detects changed bytes or
a changed file set. Hashes establish preservation of the captured and vetted files; trace review remains necessary.

The local `.gitattributes` disables text conversion and clean filters for sealed files; the local `.gitignore` makes
`trace.log` eligible for the explicit final commit. `prepare_submission.py` additionally checks actual raw Git blobs for
all selected artifacts, source snapshots and deployment evidence at the submitted commit. Final workflow/trace fields
must point to these sealed copies. Do not generate a final `submission.json` until a selected real run and commit exist.

## Isolated checks

```powershell
$env:PYTHONDONTWRITEBYTECODE = '1'
& $Python -B -m unittest discover -s submission/tools/tests -p test_capture_seal.py -v
```

These tests use verified task-specific temporary directories, synthetic child processes, and a loopback readiness server.
They do not run the builder, read `.env`, contact Freya/Cloudflare, or alter this repository's Git state.