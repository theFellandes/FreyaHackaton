"""The agentic runner (Claude Agent SDK) with a fake ``query`` function: credential order, dead-credential handling, the trace.

No SDK session is ever started: ``query_fn`` replaces ``claude_agent_sdk.query``. The fake messages are dataclasses named like
the SDK's (the runner recognises messages by class name), so the same code path as in a real session is exercised.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from builder.agentic import runner
from builder.agentic.runner import (
    agentic_available,
    agentic_unavailable_reason,
    build_prompt,
    candidate_credentials,
    run_agentic_build,
)
from builder.budget import Budget
from builder.llm import keys as keys_module
from builder.llm.keys import CLAUDE_CODE, ApiKey, HealthStore
from builder.trace import NullTrace, TraceWriter

# The SDK is an optional extra: every test here that needs it patches availability instead of relying on the install
# (tests/conftest.py ``claude_sdk_installed``), so the module passes on a machine without ``claude_agent_sdk``.
pytestmark = pytest.mark.usefixtures("claude_sdk_installed")

KEY_A, KEY_B = "sk-ant-test-aaaaaaaaaaaaaaaaaa", "sk-ant-test-bbbbbbbbbbbbbbbbbb"


@dataclass
class TextBlock:
    text: str


@dataclass
class ToolUseBlock:
    name: str
    input: dict[str, Any] = field(default_factory=dict)


@dataclass
class AssistantMessage:
    content: list[Any]
    model: str = "claude-sonnet-5-5"
    error: Any = None
    usage: dict[str, int] | None = None


@dataclass
class ResultMessage:
    num_turns: int = 3
    total_cost_usd: float | None = 0.12
    duration_ms: int = 1500
    is_error: bool = False
    result: str | None = None
    subtype: str = "success"


WORKFLOW = {"initial_node": "start", "nodes": {"start": {"name": "start", "node_type": "end_call"}}}


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    """Two Anthropic keys, and a config dir without a Claude login."""
    return {
        "ANTHROPIC_API_KEY": KEY_A,
        "ANTHROPIC_API_KEY_2": KEY_B,
        "CLAUDE_CONFIG_DIR": str(tmp_path / "noclaude"),
    }


@pytest.fixture
def store(tmp_path: Path) -> HealthStore:
    return HealthStore(tmp_path / "health.json", 3600)


@pytest.fixture
def agent_settings(make_settings: Callable[..., Any], tmp_path: Path) -> Any:
    return make_settings(llm_key_health_file=tmp_path / "health.json")


def writer(slot: str = "s", workflow: Any = WORKFLOW) -> Callable[[], Path]:
    def write() -> Path:
        from builder import paths

        target = paths.OUT_DIR / f"{slot}.agentic.workflow.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(workflow), encoding="utf-8")
        return target

    return write


async def run(
    query_fn: Any, settings: Any, env: dict[str, str], store: HealthStore, trace: Any, *, budget_s: float = 1680,
    feedback: str = "",
) -> dict[str, Any]:  # fmt: skip
    return await run_agentic_build(
        ["briefs/brief-a.md"], "tools/openapi.json", "s", settings, trace, Budget(budget_s),
        feedback=feedback, env=env, query_fn=query_fn, store=store,
    )  # fmt: skip


# ------------------------------------------------------------------------------------------------ credentials


def test_credentials_are_anthropic_keys_first_then_the_login(
    agent_settings: Any, env: dict[str, str], store: HealthStore
) -> None:
    keys = candidate_credentials(agent_settings, env, store)
    assert [k.secret for k, _ in keys] == [KEY_A, KEY_B]
    assert [e["ANTHROPIC_API_KEY"] for _, e in keys] == [KEY_A, KEY_B]
    with_login = candidate_credentials(
        agent_settings, {**env, "CLAUDE_CODE_OAUTH_TOKEN": "tok-" + "x" * 30}, store
    )
    assert [k.provider for k, _ in with_login] == ["anthropic", "anthropic", "claude_code"]
    assert with_login[-1][1] == {
        "ANTHROPIC_API_KEY": "",  # a stale key in the parent environment must not leak in ...
        "CLAUDE_CODE_OAUTH_TOKEN": "tok-" + "x" * 30,  # ... and the token the login runs on must get there
    }


def test_dead_credentials_are_left_out(agent_settings: Any, env: dict[str, str], store: HealthStore) -> None:
    first = candidate_credentials(agent_settings, env, store)[0][0]
    store.mark_dead(first.provider, first.fingerprint, "quota: no credits", first.label)
    assert [k.secret for k, _ in candidate_credentials(agent_settings, env, store)] == [KEY_B]


def test_availability_needs_the_sdk_and_a_credential(
    agent_settings: Any, env: dict[str, str], store: HealthStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert agentic_available(agent_settings, env, store)
    assert not agentic_available(agent_settings, {"CLAUDE_CONFIG_DIR": env["CLAUDE_CONFIG_DIR"]}, store)
    monkeypatch.setattr(keys_module, "_sdk_installed", lambda: False)
    assert not agentic_available(agent_settings, env, store)


def test_the_prompt_forbids_saving_and_carries_feedback() -> None:
    prompt = build_prompt(
        ["briefs/brief-a.md"], "tools/openapi.json", Path("out/s.agentic.workflow.json"), "L019: x"
    )
    assert "briefs/brief-a.md" in prompt and "tools/openapi.json" in prompt
    assert "out/s.agentic.workflow.json" in prompt
    assert "Do NOT save the workflow to Freya" in prompt and "run_cases" in prompt
    assert "previous attempt was rejected" in prompt and "L019: x" in prompt
    assert "previous attempt" not in build_prompt([], "t.json", Path("o.json"))


# ------------------------------------------------------------------------------------------------ sessions


async def test_a_session_that_writes_a_valid_file_succeeds(
    agent_settings: Any, env: dict[str, str], store: HealthStore, tmp_dirs: SimpleNamespace
) -> None:
    trace = TraceWriter("run1", "s", directory=tmp_dirs.traces)
    seen: dict[str, Any] = {}
    write = writer()

    async def query_fn(*, prompt: str, options: Any) -> AsyncIterator[Any]:
        seen.update(prompt=prompt, options=options)
        yield AssistantMessage([TextBlock("I will read the brief."), ToolUseBlock("Read", {"file_path": "briefs/brief-a.md"})],
                               usage={"input_tokens": 1200, "output_tokens": 80})  # fmt: skip
        write()
        yield AssistantMessage([ToolUseBlock("Write", {"file_path": "out/s.agentic.workflow.json"})])
        yield ResultMessage(num_turns=7)

    result = await run(query_fn, agent_settings, env, store, trace, feedback="L001: null")
    trace.close()
    assert result["ok"] and result["workflow"] == WORKFLOW and result["turns"] == 7
    assert result["credential"].startswith("anthropic#1") and result["models"] == ["claude-sonnet-5-5"]
    assert seen["options"]["env"]["ANTHROPIC_API_KEY"] == KEY_A and "L001: null" in seen["prompt"]
    text = trace.log_path.read_text(encoding="utf-8")  # type: ignore[union-attr]
    assert (
        text.count(" | MODEL  | ") == 2 and "agentic build step 1: Read" in text and "step 2: Write" in text
    )
    assert "[Read]" in text and "agentic session done: 7 turns" in text
    assert KEY_A not in text
    session_log = trace.log_path.with_suffix(".agentic.jsonl")  # type: ignore[union-attr]
    records = [json.loads(line) for line in session_log.read_text(encoding="utf-8").splitlines()]
    assert [r["type"] for r in records] == ["AssistantMessage", "AssistantMessage", "ResultMessage"]


async def test_a_dead_first_key_is_marked_and_the_next_one_is_used(
    agent_settings: Any,
    env: dict[str, str],
    store: HealthStore,
    tmp_dirs: SimpleNamespace,
    null_trace: NullTrace,
) -> None:
    write = writer()

    async def query_fn(*, prompt: str, options: Any) -> AsyncIterator[Any]:
        if options["env"]["ANTHROPIC_API_KEY"] == KEY_A:
            yield AssistantMessage([TextBlock("hi")])
            raise RuntimeError("Not logged in - Please run /login")
        write()
        yield ResultMessage()

    result = await run(query_fn, agent_settings, env, store, null_trace)
    assert result["ok"] and result["credential"].startswith("anthropic#2")
    dead = store.dead_entries()
    assert len(dead) == 1 and next(iter(dead.values())).reason.startswith("auth:")
    assert any("marked dead" in e["message"] for e in null_trace.of_kind("agentic"))


async def test_quota_errors_also_rotate_the_credential(
    agent_settings: Any,
    env: dict[str, str],
    store: HealthStore,
    tmp_dirs: SimpleNamespace,
    null_trace: NullTrace,
) -> None:
    write = writer()

    async def query_fn(*, prompt: str, options: Any) -> AsyncIterator[Any]:
        if options["env"]["ANTHROPIC_API_KEY"] == KEY_A:
            yield ResultMessage(is_error=True, result="Your credit balance is too low to access the API")
            return
        write()
        yield ResultMessage()

    result = await run(query_fn, agent_settings, env, store, null_trace)
    assert result["ok"]
    assert next(iter(store.dead_entries().values())).reason.startswith("quota:")


async def test_a_session_without_a_file_stops_and_does_not_burn_the_other_keys(
    agent_settings: Any,
    env: dict[str, str],
    store: HealthStore,
    tmp_dirs: SimpleNamespace,
    null_trace: NullTrace,
) -> None:
    attempts: list[str] = []

    async def query_fn(*, prompt: str, options: Any) -> AsyncIterator[Any]:
        attempts.append(options["env"]["ANTHROPIC_API_KEY"])
        yield AssistantMessage([TextBlock("I could not do it")])
        yield ResultMessage()

    result = await run(query_fn, agent_settings, env, store, null_trace)
    assert not result["ok"] and "without writing a valid workflow file" in result["error"]
    assert attempts == [KEY_A] and store.dead_entries() == {}


async def test_an_invalid_file_is_not_accepted(
    agent_settings: Any,
    env: dict[str, str],
    store: HealthStore,
    tmp_dirs: SimpleNamespace,
    null_trace: NullTrace,
) -> None:
    write = writer(workflow={"nodes": {}, "note": "no initial node"})

    async def query_fn(*, prompt: str, options: Any) -> AsyncIterator[Any]:
        write()
        yield ResultMessage()

    result = await run(query_fn, agent_settings, env, store, null_trace)
    assert not result["ok"] and result["workflow"] is None


async def test_an_exported_file_is_unwrapped(
    agent_settings: Any,
    env: dict[str, str],
    store: HealthStore,
    tmp_dirs: SimpleNamespace,
    null_trace: NullTrace,
) -> None:
    write = writer(workflow={"workflow_id": "wf", "workflow": WORKFLOW})

    async def query_fn(*, prompt: str, options: Any) -> AsyncIterator[Any]:
        write()
        yield ResultMessage()

    result = await run(query_fn, agent_settings, env, store, null_trace)
    assert result["ok"] and result["workflow"] == WORKFLOW


async def test_no_credential_and_no_time_are_reported_without_a_session(
    agent_settings: Any,
    env: dict[str, str],
    store: HealthStore,
    tmp_dirs: SimpleNamespace,
    null_trace: NullTrace,
) -> None:
    async def never(*, prompt: str, options: Any) -> AsyncIterator[Any]:
        raise AssertionError("no session must start")
        yield  # pragma: no cover

    none = await run(
        never, agent_settings, {"CLAUDE_CONFIG_DIR": env["CLAUDE_CONFIG_DIR"]}, store, null_trace
    )
    assert not none["ok"] and "no usable credential" in none["error"]
    late = await run(never, agent_settings, env, store, null_trace, budget_s=100)
    assert not late["ok"] and "not enough time" in late["error"]


async def test_a_missing_sdk_is_reported(
    agent_settings: Any,
    env: dict[str, str],
    store: HealthStore,
    null_trace: NullTrace,
    claude_sdk_missing: None,
) -> None:
    result = await run_agentic_build(
        ["b.md"], "t.json", "s", agent_settings, null_trace, Budget(1680), env=env, store=store
    )
    assert not result["ok"] and "not installed" in result["error"]


# ------------------------------------------------------------------------------------------------ login evidence

LOGIN = ApiKey(CLAUDE_CODE, 0, "login")


@pytest.fixture
def login_file_env(tmp_path: Path) -> dict[str, str]:
    """No API key, a credentials file in the config dir: the login "exists" but nothing proves that it works."""
    config_dir = tmp_path / "claude_cfg"
    config_dir.mkdir()
    (config_dir / ".credentials.json").write_text("{}", encoding="utf-8")
    return {"CLAUDE_CONFIG_DIR": str(config_dir)}


def test_a_credentials_file_alone_is_not_a_candidate(
    agent_settings: Any, login_file_env: dict[str, str], store: HealthStore
) -> None:
    assert candidate_credentials(agent_settings, login_file_env, store) == []
    assert not agentic_available(agent_settings, login_file_env, store)
    reason = agentic_unavailable_reason(agent_settings, login_file_env, store)
    assert "no usable credential" in reason and "Not logged in" in reason


def test_a_cached_successful_login_makes_it_a_candidate_until_it_is_marked_dead(
    agent_settings: Any, login_file_env: dict[str, str], store: HealthStore
) -> None:
    store.mark_healthy(CLAUDE_CODE, LOGIN.fingerprint, LOGIN.label)
    ((key, child_env),) = candidate_credentials(agent_settings, login_file_env, store)
    assert key.is_login and child_env == {"ANTHROPIC_API_KEY": ""}
    assert agentic_available(agent_settings, login_file_env, store)
    assert agentic_unavailable_reason(agent_settings, login_file_env, store) == ""
    store.mark_dead(CLAUDE_CODE, LOGIN.fingerprint, "auth: Not logged in", LOGIN.label)
    assert candidate_credentials(agent_settings, login_file_env, store) == []


def test_an_oauth_token_that_is_marked_dead_is_still_left_out(
    agent_settings: Any, store: HealthStore
) -> None:
    env = {"CLAUDE_CODE_OAUTH_TOKEN": "tok-" + "x" * 30}
    assert [k.provider for k, _ in candidate_credentials(agent_settings, env, store)] == [CLAUDE_CODE]
    store.mark_dead(CLAUDE_CODE, LOGIN.fingerprint, "auth: revoked", LOGIN.label)
    assert candidate_credentials(agent_settings, env, store) == []


async def test_no_session_starts_on_an_unproven_login_and_the_trace_says_why(
    agent_settings: Any,
    login_file_env: dict[str, str],
    store: HealthStore,
    null_trace: NullTrace,
) -> None:
    started: list[str] = []

    async def query_fn(*, prompt: str, options: Any) -> AsyncIterator[Any]:
        started.append(prompt)
        yield ResultMessage()

    result = await run(query_fn, agent_settings, login_file_env, store, null_trace)
    assert not result["ok"] and "no usable credential" in result["error"] and started == []
    assert any("session not started" in e["message"] for e in null_trace.of_kind("agentic"))
    assert store.dead_entries() == {}  # nothing was tried, so nothing is marked dead


async def test_a_login_session_that_writes_a_file_is_remembered_as_working(
    agent_settings: Any, store: HealthStore, tmp_dirs: SimpleNamespace, null_trace: NullTrace,
) -> None:  # fmt: skip
    env = {"CLAUDE_CODE_OAUTH_TOKEN": "tok-" + "x" * 30}
    write = writer()

    async def query_fn(*, prompt: str, options: Any) -> AsyncIterator[Any]:
        write()
        yield ResultMessage()

    assert not store.is_healthy(CLAUDE_CODE, LOGIN.fingerprint)
    result = await run(query_fn, agent_settings, env, store, null_trace)
    assert result["ok"] and result["credential"] == "claude_code#0(login)"
    assert store.is_healthy(CLAUDE_CODE, LOGIN.fingerprint)  # the next run needs no token any more


async def test_a_failed_login_session_is_not_remembered_as_working(
    agent_settings: Any, store: HealthStore, tmp_dirs: SimpleNamespace, null_trace: NullTrace,
) -> None:  # fmt: skip
    env = {"CLAUDE_CODE_OAUTH_TOKEN": "tok-" + "x" * 30}

    async def query_fn(*, prompt: str, options: Any) -> AsyncIterator[Any]:
        yield ResultMessage(is_error=True, result="Not logged in - Please run /login")

    result = await run(query_fn, agent_settings, env, store, null_trace)
    assert not result["ok"]
    assert not store.is_healthy(CLAUDE_CODE, LOGIN.fingerprint)
    assert store.lookup(CLAUDE_CODE, LOGIN.fingerprint) is not None  # and it is marked dead instead


async def test_a_login_session_gets_the_oauth_token_that_only_the_env_file_knows(
    agent_settings: Any, store: HealthStore, tmp_dirs: SimpleNamespace, null_trace: NullTrace
) -> None:
    """REPO-04: the token is known to key discovery through ``.env``, so the child process must be given it.

    The SDK builds the child environment from ``os.environ`` plus ``options.env`` only; a token that exists only in
    ``.env`` never reaches the CLI otherwise, which answers "Not logged in" and gets the login marked dead.
    """
    token = "tok-" + "y" * 30
    write = writer()
    seen: list[dict[str, str]] = []

    async def query_fn(*, prompt: str, options: Any) -> AsyncIterator[Any]:
        seen.append(dict(options["env"]))
        write()
        yield ResultMessage()

    result = await run(query_fn, agent_settings, {"CLAUDE_CODE_OAUTH_TOKEN": token}, store, null_trace)
    assert result["ok"] and result["credential"] == "claude_code#0(login)"
    assert seen == [{"ANTHROPIC_API_KEY": "", "CLAUDE_CODE_OAUTH_TOKEN": token}]


def test_a_login_without_a_token_forwards_none(
    agent_settings: Any, login_file_env: dict[str, str], store: HealthStore
) -> None:
    """A login proven by an earlier success (no token anywhere) must not get an empty or invented token."""
    store.mark_healthy(CLAUDE_CODE, LOGIN.fingerprint, LOGIN.label)
    ((_, child_env),) = candidate_credentials(agent_settings, login_file_env, store)
    assert child_env == {"ANTHROPIC_API_KEY": ""}
    placeholder = {**login_file_env, "CLAUDE_CODE_OAUTH_TOKEN": "changeme"}
    ((_, child_env),) = candidate_credentials(agent_settings, placeholder, store)
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in child_env


def test_the_session_options_built_for_the_real_sdk_carry_the_credential_environment() -> None:
    """``_options`` is the only place the credential's environment meets ``ClaudeAgentOptions``; run it for real."""
    pytest.importorskip("claude_agent_sdk")
    token = "tok-" + "z" * 30
    options = runner._options(
        "claude-sonnet-5-5", {"ANTHROPIC_API_KEY": "", "CLAUDE_CODE_OAUTH_TOKEN": token}
    )
    assert options.env["CLAUDE_CODE_OAUTH_TOKEN"] == token and options.env["ANTHROPIC_API_KEY"] == ""
    assert options.env["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"] == "1"
    assert options.model == "claude-sonnet-5-5" and options.permission_mode == "dontAsk"
    assert list(options.skills) == list(runner.SKILLS) and list(options.allowed_tools) == list(
        runner.ALLOWED_TOOLS
    )


def test_the_sdk_check_has_one_source_of_truth(monkeypatch: pytest.MonkeyPatch) -> None:
    """REPO-01: the runner and the key changer must never disagree about whether the SDK is installed."""
    monkeypatch.setattr(keys_module, "_sdk_installed", lambda: False)
    assert not runner.sdk_installed()
    monkeypatch.setattr(keys_module, "_sdk_installed", lambda: True)
    assert runner.sdk_installed()
