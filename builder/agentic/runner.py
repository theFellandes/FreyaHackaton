"""The agentic fallback track: Claude (through the Claude Agent SDK) writes the workflow file itself.

The compiled track (brief -> ledger -> IR -> compiler) is deterministic and is the default. When it cannot produce a
deployable workflow (the models fail to extract a usable policy, the checks keep failing, ...) the graph falls back to
this track, modelled on the kit's ``examples/agent_builder.py``: a Claude session with the project skills
``build-phone-agent`` (the kit's) and ``collections-workflow-patterns`` (ours, generic patterns that were proven on the
reference agent) reads the brief and the tool API and writes ``out/<slot>.agentic.workflow.json``.

Differences from the kit example, all on purpose:

* the session is told **not** to save the workflow to Freya and **not** to run the practice cases - the graph's own
  nodes lint, guard, validate, deploy and test the file afterwards (same gates as the compiled track);
* the **key changer** applies: the session is tried with every discovered ``ANTHROPIC_API_KEY*`` (through the child
  process environment), then with the Claude Code login. A key or login that answers "not logged in", "invalid key"
  or "no credits" is recorded as dead in the shared key-health file and the next credential is tried;
* every SDK message is summarised into the trace (tool uses, texts, one ``MODEL`` line per assistant message) and the
  whole session is written to ``traces/<slot>-<time>.agentic.jsonl`` with secrets redacted.

The function never raises: it returns a dict with ``ok`` and, on failure, an ``error`` the graph writes to the trace.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import re
import time
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from builder import paths
from builder.llm import keys as llm_keys
from builder.llm.keys import (
    CLAUDE_CODE,
    LOGIN_SECRET,
    ApiKey,
    HealthStore,
    claude_code_oauth_token,
    claude_code_verified,
    default_env,
    discover_keys,
)

logger = logging.getLogger(__name__)

#: Skills the session may use (``.claude/skills/<name>/SKILL.md``).
SKILLS: tuple[str, ...] = ("build-phone-agent", "collections-workflow-patterns")
#: Tools the session may use; everything else is refused (``permission_mode="dontAsk"``).
ALLOWED_TOOLS: tuple[str, ...] = ("Read", "Write", "Edit", "Glob", "Grep", "Bash")
#: Hard cap on the agent's turns.
MAX_TURNS = 120
#: Seconds kept free at the end of the budget for the graph's own checks, deploy and finalize.
RESERVE_S = 150.0
#: Text of an auth failure ("Not logged in", invalid key ...): the credential is dead, try the next one.
AUTH_RE = re.compile(
    r"not logged in|please run /login|invalid api key|invalid x-api-key|authentication|unauthori[sz]ed|\b40[13]\b|"
    r"permission_denied|oauth token",
    re.IGNORECASE,
)
#: Text of a quota failure ("credit balance is too low" ...): the credential is dead, try the next one.
QUOTA_RE = re.compile(
    r"credit balance|insufficient[_ ]quota|exceeded your current quota|billing|usage limit|\b402\b|out of credits",
    re.IGNORECASE,
)

#: ``(prompt, options) -> async iterator of SDK messages`` (default: ``claude_agent_sdk.query``).
QueryFn = Callable[..., AsyncIterator[Any]]


def sdk_installed() -> bool:
    """True when the ``claude_agent_sdk`` package can be imported (the ``agentic`` extra).

    Delegates to the key changer's check, so this module and :func:`~builder.llm.keys.claude_code_verified` can never
    disagree about whether the SDK is there (tests patch ``builder.llm.keys._sdk_installed`` once).
    """
    return llm_keys.sdk_installed()


def candidate_credentials(
    settings: Any, env: Mapping[str, str] | None = None, store: HealthStore | None = None
) -> list[tuple[ApiKey, dict[str, str]]]:
    """The credentials a session may use, in key-changer order, with the child-process environment each needs.

    Anthropic API keys come first (``ANTHROPIC_API_KEY`` set in the child environment), then the Claude Code login
    (``ANTHROPIC_API_KEY`` blanked so a stray dead key in the parent environment is not picked up, and
    ``CLAUDE_CODE_OAUTH_TOKEN`` passed on when ``env`` has one: the SDK starts the CLI with ``os.environ`` plus the
    options' ``env``, so a token known only through ``.env`` would otherwise never reach it). Credentials the health
    store lists as dead are left out.

    The login is a candidate only with *evidence* that it works (:func:`~builder.llm.keys.claude_code_verified`:
    ``CLAUDE_CODE_OAUTH_TOKEN``, or a successful call recorded in the key-health file). A credentials file alone is
    not enough: an expired login answers "Not logged in", and an agentic session that starts on it is doomed.
    """
    env = default_env() if env is None else env
    if store is None:
        store = HealthStore(settings.llm_key_health_file, settings.llm_dead_key_ttl_s)
    dead = store.dead_entries()
    found: list[tuple[ApiKey, dict[str, str]]] = []
    for key in discover_keys("anthropic", env):
        if (key.provider, key.fingerprint) not in dead:
            found.append((key, {"ANTHROPIC_API_KEY": key.secret}))
    login = ApiKey(CLAUDE_CODE, 0, LOGIN_SECRET)
    if claude_code_verified(env, store) and (login.provider, login.fingerprint) not in dead:
        login_env = {"ANTHROPIC_API_KEY": ""}
        token = claude_code_oauth_token(env)
        if token:
            login_env["CLAUDE_CODE_OAUTH_TOKEN"] = token
        found.append((login, login_env))
    return found


def agentic_available(
    settings: Any, env: Mapping[str, str] | None = None, store: HealthStore | None = None
) -> bool:
    """Whether the agentic track can run at all: the SDK is installed and some credential has evidence and is alive."""
    return sdk_installed() and bool(candidate_credentials(settings, env, store))


def agentic_unavailable_reason(
    settings: Any, env: Mapping[str, str] | None = None, store: HealthStore | None = None
) -> str:
    """Why the agentic track cannot run (one sentence for the trace), or ``""`` when it can."""
    if not sdk_installed():
        return "the claude_agent_sdk package is not installed (install the 'agentic' extra)"
    if candidate_credentials(settings, env, store):
        return ""
    return (
        "no usable credential: no live ANTHROPIC_API_KEY, no CLAUDE_CODE_OAUTH_TOKEN and no Claude Code login "
        "that worked before (a credentials file alone is no proof: an expired login answers 'Not logged in')"
    )


def build_prompt(
    brief_paths: Sequence[Path | str], tool_api_path: Path | str, out_file: Path, feedback: str = ""
) -> str:
    """The task given to the session (paths relative to the repository root where possible)."""
    briefs = "\n".join(f"  - {_rel(p)}" for p in brief_paths)
    lines = [
        "Use the build-phone-agent skill and the collections-workflow-patterns skill to build a phone agent "
        "workflow for these inputs.",
        "Brief files (read ALL of them in full; later files may amend earlier ones):",
        briefs,
        f"Tool API file: {_rel(tool_api_path)}",
        "",
        f"Write the finished workflow JSON (the value of the platform's `workflow` key) to {_rel(out_file)}.",
        "Important differences from the build-phone-agent skill:",
        "  - Do the steps up to writing the file. Do NOT save the workflow to Freya, do NOT call freya.py, do NOT "
        "run tools/run_cases.py and do NOT start any simulation: the builder checks, deploys and tests the file itself.",
        "  - After writing, check the file with Python: it must be valid JSON without any null value, every node "
        "must be reachable from initial_node and every path must end in an end_call node.",
        "  - Use only rules, numbers, dates, names and tool values that appear in the brief files or the tool API. "
        "Do not copy anything from other briefs or from the examples.",
        "  - The phone agent may call only the tools of the tool API file.",
        "Finish with one line: the path of the file you wrote.",
    ]
    if feedback.strip():
        lines += [
            "",
            "A previous attempt was rejected by the builder's checks. Fix these problems in the new file:",
            feedback.strip(),
        ]
    return "\n".join(lines)


async def run_agentic_build(
    bundle_paths: Sequence[Path | str],
    tool_api_path: Path | str,
    slot: str,
    settings: Any,
    trace: Any,
    budget: Any,
    *,
    feedback: str = "",
    env: Mapping[str, str] | None = None,
    query_fn: QueryFn | None = None,
    store: HealthStore | None = None,
) -> dict[str, Any]:
    """Let a Claude Agent SDK session write ``out/<slot>.agentic.workflow.json`` (see the module docstring).

    Args:
        bundle_paths: The brief files.
        tool_api_path: The tool API file.
        slot: The build slot (names the output file).
        settings: ``builder.settings.Settings``.
        trace: The run trace (``TraceWriter``-like: ``event``, ``model_call``, ``error``, ``redact``).
        budget: The run's :class:`~builder.budget.Budget`; the session is cut off ``RESERVE_S`` before it ends.
        feedback: Findings of the builder's checks on a previous attempt, to be fixed.
        env: Environment the credentials are read from (default ``.env`` + ``os.environ``).
        query_fn: Replacement for ``claude_agent_sdk.query`` (tests).
        store: Key-health store (default: from ``settings``).

    Returns:
        ``{"ok", "workflow_path", "workflow", "error", "turns", "cost_usd", "credential", "models"}``. On success
        ``workflow`` holds the parsed JSON; on failure ``error`` says why.
    """
    out_file = paths.OUT_DIR / f"{slot}.agentic.workflow.json"
    result: dict[str, Any] = {
        "ok": False,
        "workflow_path": None,
        "workflow": None,
        "error": "",
        "turns": 0,
        "cost_usd": None,
        "credential": None,
        "models": [],
    }
    if query_fn is None and not sdk_installed():
        result["error"] = "claude_agent_sdk is not installed (install the 'agentic' extra)"
        return result
    credentials = candidate_credentials(settings, env, store)
    if not credentials:
        result["error"] = agentic_unavailable_reason(settings, env, store)
        _event(trace, f"agentic session not started: {result['error']}")
        return result
    store = store or HealthStore(settings.llm_key_health_file, settings.llm_dead_key_ttl_s)
    prompt = build_prompt(bundle_paths, tool_api_path, out_file, feedback)
    errors: list[str] = []
    for key, child_env in credentials:
        left = budget.remaining() - RESERVE_S
        if left < 60:
            errors.append("not enough time left for another agentic attempt")
            break
        _remove(out_file)
        attempt = await _attempt(prompt, key, child_env, settings, trace, left, query_fn, slot)
        result["credential"] = key.label
        result["turns"] += attempt["turns"]
        result["cost_usd"] = (result["cost_usd"] or 0.0) + (attempt["cost_usd"] or 0.0)
        result["models"] = sorted({*result["models"], *attempt["models"]})
        workflow = _read_workflow(out_file)
        if workflow is not None:
            result.update(ok=True, workflow=workflow, workflow_path=str(out_file), error="")
            if key.is_login:  # proof for the next run: the login works, so the agentic track may rely on it
                store.mark_healthy(key.provider, key.fingerprint, key.label)
            return result
        text = attempt["error"] or "the session ended without writing a valid workflow file"
        errors.append(f"{key.label}: {text}")
        if QUOTA_RE.search(text) or AUTH_RE.search(text):
            reason = ("quota: " if QUOTA_RE.search(text) else "auth: ") + text[:200]
            store.mark_dead(key.provider, key.fingerprint, reason, key.label)
            _event(trace, f"credential {key.label} marked dead ({reason[:80]}); trying the next one")
            continue
        break  # the session ran but produced nothing usable: another credential would do the same
    result["error"] = "; ".join(errors)[:600]
    return result


# --------------------------------------------------------------------------------------------------- one session


async def _attempt(
    prompt: str,
    key: ApiKey,
    child_env: Mapping[str, str],
    settings: Any,
    trace: Any,
    seconds: float,
    query_fn: QueryFn | None,
    slot: str,
) -> dict[str, Any]:
    """Run one SDK session; returns ``{"error", "turns", "cost_usd", "models"}`` (never raises)."""
    summary: dict[str, Any] = {"error": "", "turns": 0, "cost_usd": None, "models": []}
    provider = "claude_code" if key.is_login else "anthropic"
    model = settings.claude_code_strong_model if key.is_login else settings.anthropic_strong_model
    _event(trace, f"agentic session starting with {key.label} ({model}); time limit {seconds:.0f} s")
    log_path = _session_log_path(trace, slot)
    started = time.monotonic()
    try:
        runner = query_fn or _sdk_query()
        options = _options(model, child_env) if query_fn is None else {"model": model, "env": dict(child_env)}
        async with asyncio.timeout(seconds):
            with _open_log(log_path) as log:
                async for message in runner(prompt=prompt, options=options):
                    _handle_message(message, trace, log, provider, model, key, summary, started)
    except TimeoutError:
        summary["error"] = summary["error"] or f"the session hit the time limit ({seconds:.0f} s)"
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        summary["error"] = summary["error"] or f"{type(exc).__name__}: {exc}"[:400]
        logger.warning("agentic session failed: %s", type(exc).__name__, exc_info=True)
    if summary["error"]:
        _event(trace, f"agentic session ended with an error: {summary['error'][:200]}")
    return summary


def _handle_message(
    message: Any,
    trace: Any,
    log: Any,
    provider: str,
    model: str,
    key: ApiKey,
    summary: dict[str, Any],
    started: float,
) -> None:
    """Summarise one SDK message into the trace and the session log."""
    kind = type(message).__name__
    _log_message(log, message, trace)
    if kind == "AssistantMessage":
        used = str(getattr(message, "model", "") or model)
        if used not in summary["models"]:
            summary["models"].append(used)
        usage = getattr(message, "usage", None) or {}
        names: list[str] = []
        for block in getattr(message, "content", None) or []:
            block_kind = type(block).__name__
            if block_kind == "ToolUseBlock":
                names.append(str(getattr(block, "name", "tool")))
                _event(trace, f"  [{names[-1]}] {_clip(getattr(block, 'input', ''), 120)}")
            elif block_kind == "TextBlock" and getattr(block, "text", "").strip():
                _event(trace, f"  agent: {_clip(block.text, 160)}")
        error = getattr(message, "error", None)
        if error and not summary["error"]:
            summary["error"] = _clip(error, 300)
        _model_line(
            trace, provider, used, key, names, usage, getattr(message, "error", None), summary, started
        )
    elif kind == "ResultMessage":
        summary["turns"] = int(getattr(message, "num_turns", 0) or 0)
        summary["cost_usd"] = getattr(message, "total_cost_usd", None)
        if getattr(message, "is_error", False):
            summary["error"] = _clip(
                getattr(message, "result", None) or getattr(message, "subtype", "error"), 300
            )
        _event(
            trace,
            f"agentic session done: {summary['turns']} turns, "
            f"{(getattr(message, 'duration_ms', 0) or 0) / 1000:.0f} s, cost {summary['cost_usd']}",
        )


def _model_line(
    trace: Any,
    provider: str,
    model: str,
    key: ApiKey,
    tools: list[str],
    usage: Mapping[str, Any],
    error: Any,
    summary: dict[str, Any],
    started: float,
) -> None:
    """One ``MODEL`` trace line per assistant message of the session (the hackathon trace lists every model call)."""
    summary["steps"] = summary.get("steps", 0) + 1
    purpose = f"agentic build step {summary['steps']}: " + (", ".join(tools[:3]) if tools else "reply")
    try:
        trace.model_call(
            provider,
            model,
            key.label,
            purpose,
            error is None,
            round(time.monotonic() - started, 2),
            input_tokens=_int(usage.get("input_tokens")) if isinstance(usage, Mapping) else None,
            output_tokens=_int(usage.get("output_tokens")) if isinstance(usage, Mapping) else None,
            error_kind=None if error is None else "unknown",
            error=None if error is None else _clip(error, 120),
        )
    except Exception:  # the trace must never stop a session
        logger.debug("could not write a model_call trace line", exc_info=True)


def _options(model: str, child_env: Mapping[str, str]) -> Any:
    """``ClaudeAgentOptions`` as the kit's example builds them, plus the credential's environment."""
    from claude_agent_sdk import ClaudeAgentOptions  # lazy: optional extra

    return ClaudeAgentOptions(
        cwd=paths.REPO_ROOT,
        model=model,
        skills=list(SKILLS),
        setting_sources=["project"],
        allowed_tools=list(ALLOWED_TOOLS),
        permission_mode="dontAsk",
        max_turns=MAX_TURNS,
        env={
            "BASH_DEFAULT_TIMEOUT_MS": "900000",
            "BASH_MAX_TIMEOUT_MS": "1200000",
            "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1",
            **dict(child_env),
        },
    )


def _sdk_query() -> QueryFn:
    from claude_agent_sdk import query  # lazy: optional extra

    return query


# --------------------------------------------------------------------------------------------------- small helpers


def _remove(path: Path) -> None:
    """Remove a stale output file so an old result is never mistaken for the new one."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.debug("could not remove %s", path)


def _read_workflow(path: Path) -> dict[str, Any] | None:
    """The workflow dict in ``path`` when it is a JSON object with ``nodes`` and ``initial_node``; else ``None``."""
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    if isinstance(data, dict) and isinstance(data.get("workflow"), dict) and "nodes" not in data:
        data = data["workflow"]  # an exported file: unwrap the definition
    if isinstance(data, dict) and isinstance(data.get("nodes"), dict) and data.get("initial_node"):
        return data
    return None


def _rel(path: Path | str) -> str:
    p = Path(path)
    try:
        return p.resolve().relative_to(paths.REPO_ROOT).as_posix()
    except (ValueError, OSError):
        return p.as_posix()


def _clip(value: Any, limit: int) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _event(trace: Any, message: str) -> None:
    try:
        trace.event("agentic", message)
    except Exception:
        logger.debug("could not write an agentic trace event", exc_info=True)


def _session_log_path(trace: Any, slot: str) -> Path | None:
    """``traces/<trace stem>.agentic.jsonl`` next to the run trace, or ``None`` for an in-memory trace."""
    log_path = getattr(trace, "log_path", None)
    if not log_path:
        return None
    return Path(log_path).with_suffix(".agentic.jsonl")


class _open_log:
    """Open the session log for writing (or do nothing when there is no path / the file cannot be opened)."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self.handle: Any = None

    def __enter__(self) -> Any:
        if self.path is not None:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.handle = self.path.open("a", encoding="utf-8", newline="\n")
            except OSError:
                self.handle = None
        return self.handle

    def __exit__(self, *exc: object) -> None:
        if self.handle is not None:
            self.handle.close()


def _log_message(log: Any, message: Any, trace: Any) -> None:
    """Append one SDK message to the session log, secrets redacted."""
    if log is None:
        return
    try:
        body = dataclasses.asdict(message) if dataclasses.is_dataclass(message) else {"repr": repr(message)}
        line = json.dumps({"type": type(message).__name__, **body}, default=str, ensure_ascii=False)
        redact = getattr(trace, "redact", None)
        log.write((redact(line) if callable(redact) else line) + "\n")
        log.flush()
    except Exception:
        logger.debug("could not write the agentic session log", exc_info=True)


__all__ = [
    "ALLOWED_TOOLS",
    "AUTH_RE",
    "MAX_TURNS",
    "QUOTA_RE",
    "SKILLS",
    "agentic_available",
    "agentic_unavailable_reason",
    "build_prompt",
    "candidate_credentials",
    "run_agentic_build",
    "sdk_installed",
]
