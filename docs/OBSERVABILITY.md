# Observability and rate limiting

How to see exactly what the builder sent to the LLMs and what came back, and how the builder keeps itself below the
provider and platform limits.

## What is logged where

| What | Where | On by default |
| --- | --- | --- |
| Run trace: stages, one `MODEL` line per LLM attempt (no prompt text), Freya calls, tests | `traces/<slot>-<time>.log` and `.jsonl` | yes |
| **Every LLM attempt with the full prompt and the full reply**, success and failure | `traces/<slot>-<time>.llm.jsonl` | yes |
| The same, as a trace per build with one span per graph node and one generation per attempt | Langfuse (cloud or self-hosted) | only with `LANGFUSE_PUBLIC_KEY` + `LANGFUSE_SECRET_KEY` |

The trace and observer redactors mask configured secret values and recognized key/token shapes with `***` before
writing or sending their records. This is credential masking, not general personal-data removal. Prompts can include
brief/API text, repair context and test evidence. The result file (`results/<slot>-<run>.build.json`), the stdout JSON
line and the CLI's log output are masked the same way (F11); conversation downloads made with other tools are not.

Two independent switches decide where prompt and reply texts end up:

| Setting | Governs | Default | Effect when changed |
| --- | --- | --- | --- |
| `LLM_LOG_CONTENT` | the local `traces/*.llm.jsonl` | `full` | `metadata`: the file keeps ids, models, tokens, timings, errors and the **length** of each text (`system_chars`, `user_chars`, `output_chars`), but `system` / `user` / `output` are `null` |
| `OBSERVABILITY_LOG_PROMPTS` | Langfuse only | `true` | `false`: Langfuse gets lengths instead of prompt / reply texts |

The default is `full` locally because the owner wants to read what the models answered. Set `LLM_LOG_CONTENT=metadata`
before a run whose prompts must not be written to disk (for example before sharing the `traces/` folder); set
`OBSERVABILITY_LOG_PROMPTS=false` as well if Langfuse is on. Neither setting touches the run trace (`.log` /
`.jsonl`), which never contains prompt text. Keep Langfuse off (no keys) if nothing may leave the machine.

## The local LLM log

One JSON object per finished attempt, appended with the file closed after each write. Completed records are usually
available after a process crash; unfinished attempts and storage failures are not covered by that guarantee:

```json
{"type": "generation", "seq": 3, "ts": "2026-10-10T13:05:41.220Z", "run_id": "...", "slot": "brief-a",
 "span": "extract_policy", "purpose": "extract policy IR", "provider": "openai", "model": "gpt-5.6-terra",
 "key_label": "openai#1(…ab12)", "attempt": 1, "ok": true, "error_kind": null, "error": null,
 "latency_s": 4.21, "input_tokens": 5120, "output_tokens": 830,
 "content": "full", "system_chars": 1800, "user_chars": 14200, "output_chars": 2650,
 "system": "...", "user": "...", "output": "{\"rules\": [...]}"}
```

A failed attempt has `ok: false`, an `error_kind` (`auth`, `model_unavailable`, `rate_limit`, `overloaded`,
`parse` ...) and, for an unparseable structured reply, the raw reply in `output`. `user` is exactly what the provider
received, repair note included. Every record also carries `content` (`full` or `metadata`) and the lengths
`system_chars`, `user_chars`, `output_chars`; with `LLM_LOG_CONTENT=metadata` the three texts are `null`. The error
text is kept in both modes (it is also on the run trace's `MODEL` line). Read the log with `builder.observability.read_llm_log(path)` or any JSONL tool, e.g.
`Get-Content traces\brief-a-*.llm.jsonl | ConvertFrom-Json | Select seq, span, provider, ok, latency_s`.

## Langfuse

Install the optional extra once: `uv sync --extra obs` (installs `langfuse>=3`; written against SDK v4, with a fallback
for the v3 `start_span` / `start_generation` calls).

**Cloud**: create a project at <https://cloud.langfuse.com> (US: <https://us.cloud.langfuse.com>), copy its keys into `.env`:

```
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_HOST=https://cloud.langfuse.com
```

**Self-hosted**: `docker compose -f docker-compose.langfuse.yml up -d`, open <http://localhost:3000> (login
`admin@example.com` / `changeme`), and use `LANGFUSE_HOST=http://localhost:3000` with the keys from the compose file
header (`pk-lf-changeme` / `sk-lf-changeme`). The placeholder passwords are for a laptop; change them (header of the
compose file) before exposing the stack. `docker compose ... down -v` deletes the stored traces.

The build trace line says what is on: `OBS | langfuse enabled (<host>)`, `langfuse disabled (no keys)` or
`langfuse disabled (package not installed ...)`.

In Langfuse each build is one trace `build:<slot>` (session = run id, tags `slot:..`, `track:..`, `profile:..`), each
graph node a span under it, each LLM attempt a generation under its node with the messages sent, the raw reply, the
model, token usage and, for a failure, level `ERROR` with the error as status message. A generation is recorded after
the attempt finished, so Langfuse shows it as (almost) instantaneous: read the real duration from the `latency_s`
metadata. Observer exceptions are swallowed so they do not fail the build. Successful remote delivery and its latency
still need verification; local trace completeness should be checked before accepting a submission run.

## LLM rate limiting

Every LLM attempt first takes a slot from `builder.llm.ratelimit.RateLimiter`: a global concurrency cap plus, per
(provider, key), a requests-per-minute bucket, an estimated-tokens-per-minute bucket and a minimum spacing between call
starts. The estimate is `(len(system) + len(user)) // 4 + max_tokens` and is corrected with the reported usage after
the call. A wait over 0.5 s is traced as `RATE | waited 3.2 s for openai#1(…ab12)`; a 429 additionally blocks that key
for `Retry-After` (or the cooldown setting); a wait that would pass the build deadline ends the call instead of sleeping.
Set a value to `0` to switch that limit off.

| Variable | Default | Meaning |
| --- | --- | --- |
| `LLM_MAX_CONCURRENCY` | 2 | provider calls in flight at once, all providers together |
| `LLM_RPM_PER_KEY` | 30 | requests per minute per (provider, key) |
| `LLM_TPM_PER_KEY` | 150000 | estimated tokens per minute per (provider, key) |
| `LLM_MIN_INTERVAL_S` | 0.5 | minimum seconds between two call starts on the same key |
| `LLM_RATE_OVERRIDES` | empty | per-provider `rpm:tpm` replacing the two limits, e.g. `openai=60:200000,anthropic=30:100000` |
| `LLM_RATE_LIMIT_COOLDOWN_S` | 30 | block time after a 429 without `Retry-After` (existing setting) |
| `FREYA_MIN_INTERVAL_S` | 0.25 | minimum seconds between two Freya API calls |

## Polite Freya client

`builder.freya.ratelimit.PoliteLimiter` spaces every HTTP request of the kit client (also the several requests inside
`save_workflow`) at least `FREYA_MIN_INTERVAL_S` apart, because the rules forbid probing or load-testing the platform.
`simulation_slot()` (also `FreyaClient.simulation_slot()`) is a process-level lock: hold it around a practice
simulation so this process never starts two at once (the kit allows one per workspace).

## Wiring (for developers)

- `observer = build_observer(settings, trace)`; `observer.start_run(run_id, slot, {"track": ..., "profile": ...,
  "brief_sha": ...})` at the start; `gateway = build_gateway(settings, trace, observer=observer)`; at the end
  `observer.end_run(status, summary)` then `observer.flush()`.
- A graph node wraps its work in `with observed_span(observer, "node_name") as span: ... span.set_output("12 rules")`;
  the gateway attaches generations to the running node through `current_span_id()`.
- A new observer implements `builder.observability.LLMObserver`; add it to the `CompositeObserver` in
  `builder/observability/factory.py`. Tests: `tests/unit/test_observability.py`, `test_llm_ratelimit.py`,
  `test_freya_ratelimit.py`, `test_llm_gateway.py`.
