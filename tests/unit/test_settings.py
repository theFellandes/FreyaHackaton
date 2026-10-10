"""Tests for builder.settings: sources and precedence, validation, kit env export and secret collection."""

from __future__ import annotations

import sys
import types
from collections.abc import Callable
from pathlib import Path

import pytest
from pydantic import ValidationError

from builder import paths
from builder.paths import DATA_DIR, REPO_ROOT
from builder.settings import (
    DEFAULT_FREYA_API_URL,
    MIN_SECRET_LENGTH,
    Settings,
    apply_kit_env,
    effective_env,
    get_settings,
    secret_values,
)

MakeSettings = Callable[..., Settings]


# --- defaults and sources ---------------------------------------------------------------------------------------------


def test_defaults_match_the_contract(settings: Settings) -> None:
    assert settings.freya_api_key is None
    assert settings.freya_bank_team_key is None
    assert settings.freya_workspace_id is None
    assert settings.freya_api_url == DEFAULT_FREYA_API_URL == "https://staging-app.freyavoice.ai"
    assert settings.llm_providers == "anthropic,openai,claude_code"
    assert settings.llm_provider_order == ["anthropic", "openai", "claude_code"]
    # Cheaper tiers on purpose (user request: save tokens).
    assert (settings.anthropic_strong_model, settings.anthropic_fast_model) == (
        "claude-sonnet-5-5",
        "claude-haiku-5-5",
    )
    assert (settings.openai_strong_model, settings.openai_fast_model) == ("gpt-5.6-terra", "gpt-5.6-luna")
    assert (settings.claude_code_strong_model, settings.claude_code_fast_model) == (
        "claude-sonnet-5-5",
        "claude-haiku-5-5",
    )
    assert settings.llm_timeout_s == 300.0
    assert settings.llm_max_tokens == 16000
    assert settings.llm_retries_per_key == 2
    assert settings.llm_rate_limit_cooldown_s == 30.0
    assert settings.llm_max_cooldown_wait_s == 45.0
    assert settings.llm_key_health_file == DATA_DIR / "key_health.json"
    assert settings.llm_dead_key_ttl_s == 21600
    assert settings.builder_track == "auto"
    assert settings.agent_profile == "staged"
    assert settings.build_time_budget_s == 1680
    assert settings.self_test == "auto"
    assert settings.build_slot is None
    assert settings.dry_run is False
    assert (settings.max_policy_repairs, settings.max_compile_repairs, settings.max_selftest_repairs) == (
        2,
        2,
        1,
    )
    assert settings.freya_state_file == REPO_ROOT / ".freya-agents.json"


def test_environment_variables_override_defaults(
    monkeypatch: pytest.MonkeyPatch, make_settings: MakeSettings
) -> None:
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("BUILDER_TRACK", "agentic")
    monkeypatch.setenv("llm_timeout_s", "12.5")  # names are case-insensitive
    monkeypatch.setenv("FREYA_API_URL", "https://example.invalid/")

    s = make_settings()

    assert s.dry_run is True
    assert s.builder_track == "agentic"
    assert s.llm_timeout_s == 12.5
    assert s.freya_api_url == "https://example.invalid"  # trailing slash dropped


def test_freya_key_alias_is_accepted(monkeypatch: pytest.MonkeyPatch, make_settings: MakeSettings) -> None:
    monkeypatch.setenv("FREYA_KEY", "fak_from_legacy_alias")
    s = make_settings()
    assert s.freya_api_key is not None
    assert s.freya_api_key.get_secret_value() == "fak_from_legacy_alias"


def test_freya_api_key_wins_over_alias_and_blank_primary_falls_back(
    monkeypatch: pytest.MonkeyPatch, make_settings: MakeSettings
) -> None:
    monkeypatch.setenv("FREYA_KEY", "fak_alias_value")
    monkeypatch.setenv("FREYA_API_KEY", "fak_primary_value")
    assert make_settings().freya_api_key.get_secret_value() == "fak_primary_value"  # type: ignore[union-attr]

    monkeypatch.setenv("FREYA_API_KEY", "")
    assert make_settings().freya_api_key.get_secret_value() == "fak_alias_value"  # type: ignore[union-attr]


def test_blank_values_fall_back_to_defaults(
    monkeypatch: pytest.MonkeyPatch, make_settings: MakeSettings
) -> None:
    for name in ("DRY_RUN", "BUILD_SLOT", "LLM_TIMEOUT_S", "FREYA_API_URL", "FREYA_WORKSPACE_ID"):
        monkeypatch.setenv(name, "")
    s = make_settings()
    assert s.dry_run is False
    assert s.build_slot is None
    assert s.llm_timeout_s == 300.0
    assert s.freya_api_url == DEFAULT_FREYA_API_URL
    assert s.freya_workspace_id is None


def test_blank_secrets_and_strings_given_directly_are_unset(make_settings: MakeSettings) -> None:
    s = make_settings(freya_api_key="   ", freya_bank_team_key="", build_slot="  ", freya_workspace_id="")
    assert s.freya_api_key is None
    assert s.freya_bank_team_key is None
    assert s.build_slot is None
    assert s.freya_workspace_id is None


def test_dotenv_file_is_read_and_environment_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "FREYA_API_KEY=fak_from_dotenv_file\nBUILD_SLOT=dotenv-slot\nSELF_TEST=smoke\nUNRELATED_SETTING=1\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SELF_TEST", "none")

    s = Settings(_env_file=env_file)

    assert s.freya_api_key.get_secret_value() == "fak_from_dotenv_file"  # type: ignore[union-attr]
    assert s.build_slot == "dotenv-slot"
    assert s.self_test == "none"  # process environment overrides .env


def test_hermetic_fixture_hides_the_repository_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    # Settings() without arguments uses model_config["env_file"], which the autouse fixture disables.
    assert Settings.model_config["env_file"] is None
    assert Settings().freya_api_key is None


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("BUILDER_TRACK", "hybrid"),
        ("AGENT_PROFILE", "fat"),
        ("SELF_TEST", "always"),
        ("LLM_TIMEOUT_S", "0"),
        ("LLM_MAX_TOKENS", "-5"),
        ("LLM_RETRIES_PER_KEY", "-1"),
        ("BUILD_TIME_BUDGET_S", "0"),
        ("MAX_POLICY_REPAIRS", "-1"),
        ("FREYA_API_URL", "staging-app.freyavoice.ai"),
        ("LLM_PROVIDERS", " , ;"),
        ("DRY_RUN", "maybe"),
    ],
)
def test_invalid_values_are_rejected(
    monkeypatch: pytest.MonkeyPatch, make_settings: MakeSettings, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ValidationError):
        make_settings()


def test_provider_order_is_normalised(make_settings: MakeSettings) -> None:
    s = make_settings(llm_providers=" OpenAI ; claude-code,openai\nanthropic ")
    assert s.llm_provider_order == ["openai", "claude_code", "anthropic"]


def test_relative_paths_are_anchored_at_the_repo_root(make_settings: MakeSettings, tmp_path: Path) -> None:
    s = make_settings(llm_key_health_file="data/kh.json", freya_state_file=tmp_path / "state.json")
    assert s.llm_key_health_file == REPO_ROOT / "data" / "kh.json"
    assert s.freya_state_file == tmp_path / "state.json"


def test_secrets_never_appear_in_repr_or_public_summary(make_settings: MakeSettings) -> None:
    s = make_settings(freya_api_key="fak_top_secret_value_1", freya_bank_team_key="team-secret-of-16-chars")
    for text in (repr(s), str(s), str(s.public_summary()), s.model_dump_json()):
        assert "fak_top_secret_value_1" not in text
        assert "team-secret-of-16-chars" not in text
    assert s.public_summary()["freya_api_key"] == "set"
    assert make_settings().public_summary()["freya_api_key"] == "not set"


def test_get_settings_is_cached_until_cleared(monkeypatch: pytest.MonkeyPatch) -> None:
    first = get_settings()
    assert get_settings() is first
    monkeypatch.setenv("BUILD_SLOT", "after-change")
    assert get_settings().build_slot is None  # still the cached instance
    get_settings.cache_clear()
    assert get_settings().build_slot == "after-change"


# --- apply_kit_env ----------------------------------------------------------------------------------------------------


def test_apply_kit_env_exports_known_values_only(make_settings: MakeSettings) -> None:
    environ: dict[str, str] = {"FREYA_AGENT_PROVIDER": "organiser", "FREYA_AGENT_MODEL": "organiser-model"}
    s = make_settings(freya_api_key="fak_abcdefghijkl", freya_workspace_id="ws_42")

    exported = apply_kit_env(s, environ)

    assert exported == ["FREYA_API_KEY", "FREYA_API_URL", "FREYA_WORKSPACE_ID"]
    assert environ["FREYA_API_KEY"] == "fak_abcdefghijkl"
    assert environ["FREYA_API_URL"] == DEFAULT_FREYA_API_URL
    assert environ["FREYA_WORKSPACE_ID"] == "ws_42"
    assert "FREYA_BANK_TEAM_KEY" not in environ  # unknown -> not touched
    assert environ["FREYA_AGENT_PROVIDER"] == "organiser"  # reserved for the organisers
    assert environ["FREYA_AGENT_MODEL"] == "organiser-model"


def test_apply_kit_env_defaults_to_os_environ(
    monkeypatch: pytest.MonkeyPatch, make_settings: MakeSettings
) -> None:
    import os

    monkeypatch.delenv("FREYA_AGENT_PROVIDER", raising=False)
    s = make_settings(freya_bank_team_key="team-secret-of-16-chars")
    exported = apply_kit_env(s)
    try:
        assert os.environ["FREYA_BANK_TEAM_KEY"] == "team-secret-of-16-chars"
        assert "FREYA_AGENT_PROVIDER" not in os.environ
        assert "FREYA_BANK_TEAM_KEY" in exported
    finally:
        for name in exported:
            monkeypatch.delenv(name, raising=False)


# --- effective_env and secret_values ----------------------------------------------------------------------------------


def test_effective_env_overlays_process_environment_on_dotenv(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("ANTHROPIC_API_KEY_2=from-file-key-123\nSHARED=file\nEMPTY=\n", encoding="utf-8")

    merged = effective_env(env_file, {"SHARED": "process", "OTHER": "x", "BLANK": ""})

    assert merged == {"ANTHROPIC_API_KEY_2": "from-file-key-123", "SHARED": "process", "OTHER": "x"}
    assert effective_env(tmp_path / "missing.env", {"A": "1"}) == {"A": "1"}
    assert effective_env(env_file, {"A": "1"}, read_dotenv=False) == {"A": "1"}


def test_effective_env_defaults_to_the_repository_dotenv_looked_up_at_call_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / "repo.env"
    env_file.write_text("FROM_REPO_DOTENV=yes\n", encoding="utf-8")
    monkeypatch.setattr(paths, "ENV_FILE", env_file)
    assert effective_env(environ={}) == {"FROM_REPO_DOTENV": "yes"}


def _install_fake_keys_module(monkeypatch: pytest.MonkeyPatch, discover: Callable[..., object]) -> None:
    module = types.ModuleType("builder.llm.keys")
    module.discover_all_keys = discover  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "builder.llm.keys", module)


def test_secret_values_collects_freya_llm_and_credential_like_values(
    monkeypatch: pytest.MonkeyPatch, make_settings: MakeSettings
) -> None:
    seen_env: list[dict[str, str]] = []

    def discover_all_keys(env: dict[str, str]) -> dict[str, list[object]]:
        seen_env.append(env)
        return {
            "anthropic": [
                types.SimpleNamespace(provider="anthropic", index=1, secret="sk-ant-discovered-key-1")
            ],
            "openai": [types.SimpleNamespace(provider="openai", index=1, secret="sk-openai-discovered-2")],
        }

    _install_fake_keys_module(monkeypatch, discover_all_keys)
    s = make_settings(freya_api_key="fak_freya_secret_key", freya_bank_team_key="team-secret-of-16-chars")
    env = {
        "MY_SERVICE_TOKEN": "token-value-123456",
        "SOME_PASSWORD": "hunter2hunter2",
        "OTHER_API_KEY": "short",  # below MIN_SECRET_LENGTH -> not redacted
        "PATH": "C:/not/a/secret/at/all",
        "CLAUDE_CODE_OAUTH_TOKEN": "oauth-token-abcdefgh",
    }

    secrets = secret_values(s, env)

    assert seen_env == [env]
    for expected in (
        "fak_freya_secret_key",
        "team-secret-of-16-chars",
        "sk-ant-discovered-key-1",
        "sk-openai-discovered-2",
        "token-value-123456",
        "hunter2hunter2",
        "oauth-token-abcdefgh",
    ):
        assert expected in secrets
    assert "short" not in secrets
    assert "C:/not/a/secret/at/all" not in secrets
    assert all(len(secret) >= MIN_SECRET_LENGTH for secret in secrets)
    assert len(secrets) == len(set(secrets))
    assert [len(x) for x in secrets] == sorted((len(x) for x in secrets), reverse=True)


def test_secret_values_skips_the_login_pseudo_key(
    monkeypatch: pytest.MonkeyPatch, make_settings: MakeSettings
) -> None:
    _install_fake_keys_module(
        monkeypatch, lambda env: {"claude_code": [types.SimpleNamespace(secret="login")], "openai": []}
    )
    assert "login" not in secret_values(make_settings(), {})


def test_secret_values_without_llm_keys_module(
    monkeypatch: pytest.MonkeyPatch, make_settings: MakeSettings
) -> None:
    monkeypatch.setitem(sys.modules, "builder.llm.keys", None)  # makes the lazy import raise ImportError
    secrets = secret_values(
        make_settings(freya_api_key="fak_only_freya_key"), {"ANTHROPIC_API_KEY": "sk-ant-by-name-1"}
    )
    assert "fak_only_freya_key" in secrets
    assert "sk-ant-by-name-1" in secrets  # still caught by the variable-name heuristic


def test_secret_values_survives_a_failing_discovery(
    monkeypatch: pytest.MonkeyPatch, make_settings: MakeSettings, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(env: dict[str, str]) -> object:
        raise RuntimeError("boom sk-should-not-be-logged")

    _install_fake_keys_module(monkeypatch, broken)
    secrets = secret_values(make_settings(freya_api_key="fak_still_collected"), {})
    assert secrets == ["fak_still_collected"]
    assert "sk-should-not-be-logged" not in caplog.text


def test_secret_values_reads_dotenv_by_default(
    monkeypatch: pytest.MonkeyPatch, make_settings: MakeSettings, tmp_path: Path
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("OPENAI_API_KEY_3=sk-only-in-dotenv-file\n", encoding="utf-8")
    monkeypatch.setattr(paths, "ENV_FILE", env_file)
    monkeypatch.setitem(sys.modules, "builder.llm.keys", None)  # rely on the variable-name heuristic only
    assert "sk-only-in-dotenv-file" in secret_values(make_settings())


def test_secret_values_uses_the_real_key_discovery_when_available(make_settings: MakeSettings) -> None:
    keys = pytest.importorskip("builder.llm.keys")
    assert callable(keys.discover_all_keys)
    env = {
        "ANTHROPIC_API_KEYS": "list-key-one-12345;list-key-two-67890",
        "OPENAI_API_KEY_7": "sk-numbered-7-abcdef",
    }
    secrets = secret_values(make_settings(), env)
    # Individual keys of a list variable can only come from discovery (the name heuristic sees the whole list).
    assert "list-key-one-12345" in secrets
    assert "list-key-two-67890" in secrets
    assert "sk-numbered-7-abcdef" in secrets
