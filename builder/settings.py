"""Typed builder configuration, read from the environment and the repository's ``.env`` file.

All tunables of a build live in :class:`Settings`, a ``pydantic-settings`` model. Values come from (highest priority
first): keyword arguments, process environment variables, the ``.env`` file at the repository root, and finally the
defaults below. Variable names are case-insensitive and equal to the field names in upper case (``DRY_RUN``,
``LLM_TIMEOUT_S``, ...); the Freya key also accepts the legacy name ``FREYA_KEY``. Empty variables (``NAME=``) are
ignored, so a copy of ``.env.example`` with blank values falls back to the defaults.

Besides the model this module offers:

* :func:`get_settings` - the process-wide cached instance.
* :func:`apply_kit_env` - exports the Freya values into ``os.environ`` so the kit's root ``freya.py`` (which reads its
  configuration once, at import time) sees them. It never touches ``FREYA_AGENT_PROVIDER`` / ``FREYA_AGENT_MODEL``,
  which only the organisers may set.
* :func:`effective_env` - the ``.env`` values overlaid with the real environment, for code that discovers keys by
  variable name (the LLM key changer).
* :func:`secret_values` - every secret string the trace must redact: the Freya keys, all LLM keys found by
  ``builder.llm.keys.discover_all_keys`` and any variable whose name looks like a credential.

Secrets are held as :class:`pydantic.SecretStr`, so ``repr(settings)`` and logs never show them.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Iterable, Mapping, MutableMapping
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from builder import paths
from builder.paths import DATA_DIR, ENV_FILE, REPO_ROOT

logger = logging.getLogger(__name__)

#: Default Freya platform (staging) used by the hackathon.
DEFAULT_FREYA_API_URL = "https://staging-app.freyavoice.ai"

#: Secrets shorter than this are not redacted (they would mangle ordinary words in the trace, e.g. the
#: ``claude_code`` provider's pseudo key ``"login"``).
MIN_SECRET_LENGTH = 8

#: Environment variable names whose values are treated as secrets for redaction, whatever their provider.
_SECRET_NAME_RE = re.compile(r"API[_-]?KEY|_KEYS?$|TOKEN|SECRET|PASSW(?:OR)?D|CREDENTIAL", re.IGNORECASE)

#: Variables exported for the kit's ``freya.py`` by :func:`apply_kit_env`.
KIT_ENV_VARS = ("FREYA_API_KEY", "FREYA_API_URL", "FREYA_BANK_TEAM_KEY", "FREYA_WORKSPACE_ID")

#: Kit variables reserved for the organisers; the builder never sets or changes them.
RESERVED_KIT_ENV_VARS = ("FREYA_AGENT_PROVIDER", "FREYA_AGENT_MODEL")

TrackName = Literal["auto", "compiled", "agentic"]
ProfileName = Literal["staged", "lean"]
SelfTestMode = Literal["auto", "none", "smoke", "walkthrough", "practice"]
LogContent = Literal["full", "metadata"]


class Settings(BaseSettings):
    """Every configurable value of a builder run (see the module docstring for where values come from)."""

    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        env_ignore_empty=True,
        validate_by_name=True,
        validate_by_alias=True,
    )

    # --- Freya platform -------------------------------------------------------------------------------------------
    freya_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("FREYA_API_KEY", "FREYA_KEY"),
        description="Workspace API key (starts with 'fak_'). Read from FREYA_API_KEY, or FREYA_KEY as a fallback.",
    )
    freya_api_url: str = Field(default=DEFAULT_FREYA_API_URL, description="Base URL of the Freya platform.")
    freya_bank_team_key: SecretStr | None = Field(
        default=None,
        description="Team secret (16+ characters) the kit's practice harness sends to the mock bank.",
    )
    freya_workspace_id: str | None = Field(
        default=None, description="Workspace id; discovered from the API when not set."
    )
    freya_state_file: Path = Field(
        default=REPO_ROOT / ".freya-agents.json",
        description="Where slot -> workflow/agent ids are remembered between runs (update in place, never recreate).",
    )
    freya_min_interval_s: float = Field(
        default=0.25,
        ge=0,
        description="Minimum seconds between two Freya API calls (be polite to the platform; 0 = no pacing).",
    )

    # --- LLM key changer ---------------------------------------------------------------------------------------------
    llm_providers: str = Field(
        default="anthropic,openai,claude_code",
        description="Comma-separated provider chain, tried in this order by the LLM gateway.",
    )
    anthropic_strong_model: str = "claude-sonnet-5-5"
    anthropic_fast_model: str = "claude-haiku-5-5"
    openai_strong_model: str = "gpt-5.6-terra"
    openai_fast_model: str = "gpt-5.6-luna"
    claude_code_strong_model: str = "claude-sonnet-5-5"
    claude_code_fast_model: str = "claude-haiku-5-5"
    llm_timeout_s: float = Field(default=300.0, gt=0, description="Timeout of one model call, in seconds.")
    llm_max_tokens: int = Field(
        default=16000, gt=0, description="Default output token limit of one model call."
    )
    llm_retries_per_key: int = Field(
        default=2, ge=0, description="Retries of transient errors before rotating."
    )
    llm_rate_limit_cooldown_s: float = Field(
        default=30.0,
        ge=0,
        description="Cooldown of a rate-limited key when the provider sends no retry-after.",
    )
    llm_max_cooldown_wait_s: float = Field(
        default=45.0,
        ge=0,
        description="Longest wait for a cooling key when no other key or provider is left.",
    )
    llm_key_health_file: Path = Field(
        default=DATA_DIR / "key_health.json",
        description="Where dead keys are remembered (fingerprints only, never secrets).",
    )
    llm_dead_key_ttl_s: float = Field(default=21600, ge=0, description="How long a dead key stays skipped.")

    # --- LLM rate limiting (client side, keeps the builder below the provider limits) --------------------------------
    llm_max_concurrency: int = Field(
        default=2, ge=0, description="Provider calls in flight at once over all providers (0 = unlimited)."
    )
    llm_rpm_per_key: int = Field(
        default=30, ge=0, description="Requests per minute per (provider, key); 0 = unlimited."
    )
    llm_tpm_per_key: int = Field(
        default=150_000, ge=0, description="Estimated tokens per minute per (provider, key); 0 = unlimited."
    )
    llm_min_interval_s: float = Field(
        default=0.5,
        ge=0,
        description="Minimum seconds between two call starts on the same key (0 = no pacing).",
    )
    llm_rate_overrides: str = Field(
        default="",
        description="Per-provider limits replacing the two defaults above: 'openai=60:200000,anthropic=30:100000'.",
    )

    # --- observability (Langfuse is optional; the local traces/*.llm.jsonl log is always written) --------------------
    langfuse_public_key: SecretStr | None = Field(
        default=None, description="Langfuse public key (pk-lf-...). With the secret key it turns Langfuse on."
    )
    langfuse_secret_key: SecretStr | None = Field(
        default=None, description="Langfuse secret key (sk-lf-...)."
    )
    langfuse_host: str = Field(
        default="https://cloud.langfuse.com", description="Langfuse server (cloud, or your self-hosted URL)."
    )
    observability_log_prompts: bool = Field(
        default=True,
        description=(
            "False sends only metadata (lengths, tokens, timings) to Langfuse, not prompt / reply texts. "
            "Affects Langfuse only; the local log is governed by llm_log_content."
        ),
    )
    llm_log_content: LogContent = Field(
        default="full",
        description=(
            "What the local traces/*.llm.jsonl keeps of every LLM attempt: 'full' = prompt and reply texts "
            "(default, for reading what the model said), 'metadata' = only ids, tokens, timings and text lengths. "
            "Independent of observability_log_prompts (Langfuse)."
        ),
    )

    # --- build --------------------------------------------------------------------------------------------------------
    builder_track: TrackName = Field(
        default="compiled",
        description=(
            "'compiled' (deterministic compiler; the default, and the configuration measured live), 'agentic' "
            "(Claude Agent SDK) or 'auto' (compiled with an agentic fallback). 'agentic' and 'auto' are opt-ins."
        ),
    )
    agent_profile: ProfileName = Field(
        default="staged", description="Workflow shape produced by the compiler."
    )
    build_time_budget_s: float = Field(
        default=1680,
        gt=0,
        description="Wall-clock budget of one build, self-tests included (hackathon limit 30 min).",
    )
    self_test: SelfTestMode = Field(
        default="smoke",
        description=(
            "Which self-test runs after deployment. 'smoke' (default) never redeploys a tested version; 'auto', "
            "'walkthrough' and 'practice' are opt-ins (their findings can start a policy repair round and a redeploy)."
        ),
    )
    build_slot: str | None = Field(
        default=None,
        description="Fixed slot name (workflow label); derived from the brief file names when unset.",
    )
    dry_run: bool = Field(default=False, description="Compile and check locally; make no Freya calls.")
    tool_base_url: str | None = Field(
        default=None,
        description=(
            "Base URL of the bank's services the agent calls (the team's mock, e.g. a Cloudflare Worker). Every tool "
            "request goes there, keeping each operation's path and method; unset keeps the contract's own server. "
            "A URL argument or --tool-base-url on the command line wins."
        ),
    )
    max_policy_repairs: int = Field(default=2, ge=0)
    max_compile_repairs: int = Field(default=2, ge=0)
    max_selftest_repairs: int = Field(default=1, ge=0)

    # --- validators ---------------------------------------------------------------------------------------------------
    @field_validator(
        "freya_api_key", "freya_bank_team_key", "langfuse_public_key", "langfuse_secret_key", mode="before"
    )
    @classmethod
    def _blank_secret_is_none(cls, value: Any) -> Any:
        """Treat an empty or whitespace-only secret as "not configured"."""
        if isinstance(value, SecretStr):
            value = value.get_secret_value()
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value

    @field_validator("llm_log_content", mode="before")
    @classmethod
    def _normalise_log_content(cls, value: Any) -> Any:
        """Accept ``Full`` / `` METADATA `` spellings; anything but the two modes is still rejected."""
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("freya_workspace_id", "build_slot", mode="before")
    @classmethod
    def _blank_str_is_none(cls, value: Any) -> Any:
        """Treat an empty or whitespace-only optional string as unset."""
        if isinstance(value, str):
            return value.strip() or None
        return value

    @field_validator("tool_base_url", mode="before")
    @classmethod
    def _valid_tool_base_url(cls, value: Any) -> Any:
        """Blank = unset; otherwise an http(s) URL with a host (trailing slashes dropped)."""
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        from builder.inputs import InputError, normalise_base_url

        try:
            return normalise_base_url(str(value))
        except InputError as exc:
            raise ValueError(f"tool_base_url must be an http(s) URL with a host: {exc}") from None

    @field_validator("freya_api_url")
    @classmethod
    def _normalise_url(cls, value: str) -> str:
        """Require an http(s) URL and drop trailing slashes (the kit appends paths that start with '/')."""
        value = value.strip().rstrip("/")
        if not re.match(r"^https?://\S+$", value):
            raise ValueError(f"freya_api_url must be an http(s) URL, got {value!r}")
        return value

    @field_validator("langfuse_host")
    @classmethod
    def _normalise_langfuse_host(cls, value: str) -> str:
        """Require an http(s) URL and drop trailing slashes."""
        value = value.strip().rstrip("/")
        if not re.match(r"^https?://\S+$", value):
            raise ValueError(f"langfuse_host must be an http(s) URL, got {value!r}")
        return value

    @field_validator("llm_rate_overrides")
    @classmethod
    def _valid_rate_overrides(cls, value: str) -> str:
        """Reject a malformed ``provider=rpm:tpm`` list (a typo would otherwise silently disable the override)."""
        if value and value.strip():
            from builder.llm.ratelimit import parse_rate_overrides

            parse_rate_overrides(value, strict=True)
        return value.strip() if value else ""

    @field_validator("llm_providers")
    @classmethod
    def _require_a_provider(cls, value: str) -> str:
        """Reject a provider chain that names no provider at all."""
        if not _split_providers(value):
            raise ValueError(
                "llm_providers must name at least one provider, e.g. 'anthropic,openai,claude_code'"
            )
        return value

    @field_validator("freya_state_file", "llm_key_health_file")
    @classmethod
    def _anchor_relative_path(cls, value: Path) -> Path:
        """Resolve relative paths against the repository root, so the working directory does not matter."""
        value = value.expanduser()
        return value if value.is_absolute() else REPO_ROOT / value

    # --- derived values -----------------------------------------------------------------------------------------------
    @property
    def llm_provider_order(self) -> list[str]:
        """The provider chain as a list: lower case, ``-`` normalised to ``_``, duplicates removed, order kept."""
        return _split_providers(self.llm_providers)

    def public_summary(self) -> dict[str, Any]:
        """A trace-safe description of the configuration: secrets are reported only as set / not set."""
        return {
            "freya_api_url": self.freya_api_url,
            "freya_api_key": "set" if self.freya_api_key else "not set",
            "freya_bank_team_key": "set" if self.freya_bank_team_key else "not set",
            "freya_workspace_id": self.freya_workspace_id or "auto",
            "llm_providers": self.llm_provider_order,
            "builder_track": self.builder_track,
            "agent_profile": self.agent_profile,
            "self_test": self.self_test,
            "build_time_budget_s": self.build_time_budget_s,
            "build_slot": self.build_slot or "auto",
            "dry_run": self.dry_run,
            "tool_base_url": self.tool_base_url or "contract server",
            "langfuse": "enabled" if self.langfuse_public_key and self.langfuse_secret_key else "disabled",
            "llm_log_content": self.llm_log_content,
            "langfuse_log_prompts": self.observability_log_prompts,
        }


def _split_providers(value: str) -> list[str]:
    """Split a provider chain such as ``"anthropic, openai;claude-code"`` into normalised, unique names."""
    names: list[str] = []
    for raw in re.split(r"[,;\s]+", value or ""):
        name = raw.strip().lower().replace("-", "_")
        if name and name not in names:
            names.append(name)
    return names


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide :class:`Settings`, read once from the environment and ``.env``.

    Call ``get_settings.cache_clear()`` after changing the environment (tests do this automatically).
    """
    return Settings()


def apply_kit_env(s: Settings, environ: MutableMapping[str, str] | None = None) -> list[str]:
    """Export the Freya settings as the environment variables the kit's ``freya.py`` reads at import time.

    Sets ``FREYA_API_KEY``, ``FREYA_API_URL``, ``FREYA_BANK_TEAM_KEY`` and ``FREYA_WORKSPACE_ID`` when the
    corresponding setting has a value; unknown values are left alone. ``FREYA_AGENT_PROVIDER`` and
    ``FREYA_AGENT_MODEL`` are never touched (the phone agent's model is fixed by the organisers).

    Args:
        s: The settings to export.
        environ: The mapping to update; ``os.environ`` by default (tests pass a plain dict).

    Returns:
        The names of the variables that were set (never their values).
    """
    target: MutableMapping[str, str] = os.environ if environ is None else environ
    values: dict[str, str | None] = {
        "FREYA_API_KEY": s.freya_api_key.get_secret_value() if s.freya_api_key else None,
        "FREYA_API_URL": s.freya_api_url,
        "FREYA_BANK_TEAM_KEY": s.freya_bank_team_key.get_secret_value() if s.freya_bank_team_key else None,
        "FREYA_WORKSPACE_ID": s.freya_workspace_id,
    }
    exported: list[str] = []
    for name, value in values.items():
        if value:
            target[name] = value
            exported.append(name)
    return exported


def effective_env(
    env_file: Path | None = None, environ: Mapping[str, str] | None = None, *, read_dotenv: bool = True
) -> dict[str, str]:
    """Return the ``.env`` file's variables overlaid with the process environment (the environment wins).

    ``pydantic-settings`` reads ``.env`` without exporting it, so code that discovers keys by variable name (the LLM
    key changer, :func:`secret_values`) uses this mapping to see both sources. Empty values are dropped.

    Args:
        env_file: The dotenv file to read; ``paths.ENV_FILE`` (looked up at call time) when ``None``. A missing
            file simply contributes nothing.
        environ: The environment to overlay; ``os.environ`` by default.
        read_dotenv: ``False`` ignores dotenv files altogether ("environment only").
    """
    merged: dict[str, str] = {}
    if read_dotenv:
        env_file = paths.ENV_FILE if env_file is None else Path(env_file)
    if read_dotenv and env_file is not None and env_file.is_file():
        from dotenv import dotenv_values  # dependency of pydantic-settings

        for name, value in dotenv_values(env_file, encoding="utf-8").items():
            if value:
                merged[name] = value
    for name, value in (os.environ if environ is None else environ).items():
        if value:
            merged[name] = value
    return merged


def secret_values(s: Settings, env: Mapping[str, str] | None = None) -> list[str]:
    """Collect every secret string that must never appear in a trace.

    Includes the Freya API key and bank team key, every LLM key found by ``builder.llm.keys.discover_all_keys``
    (imported lazily; skipped when that module is unavailable) and the value of every variable whose name looks like
    a credential (``*_API_KEY*``, ``*_KEY``, ``*TOKEN*``, ``*SECRET*``, ``*PASSWORD*``, ``*CREDENTIAL*``). Values
    shorter than :data:`MIN_SECRET_LENGTH` are skipped so that redaction cannot mangle ordinary words.

    Args:
        s: The settings holding the Freya secrets.
        env: The variables to scan; :func:`effective_env` (``.env`` + process environment) by default.

    Returns:
        Unique secrets, longest first (so a secret that contains another one is redacted whole).
    """
    scope = effective_env() if env is None else dict(env)
    candidates: list[str] = []
    for secret in (s.freya_api_key, s.freya_bank_team_key):
        if secret is not None:
            candidates.append(secret.get_secret_value())
    candidates.extend(_discovered_llm_secrets(scope))
    candidates.extend(value for name, value in scope.items() if _SECRET_NAME_RE.search(name))

    unique: list[str] = []
    for value in candidates:
        value = value.strip() if isinstance(value, str) else ""
        if len(value) >= MIN_SECRET_LENGTH and value not in unique:
            unique.append(value)
    return sorted(unique, key=len, reverse=True)


def _discovered_llm_secrets(env: Mapping[str, str]) -> list[str]:
    """Secrets of all LLM keys the key changer would use, or ``[]`` when ``builder.llm.keys`` is unavailable."""
    try:
        from builder.llm.keys import discover_all_keys
    except ImportError:
        logger.debug("builder.llm.keys is not importable; LLM keys are redacted by variable name only")
        return []
    try:
        found = discover_all_keys(dict(env))
    except Exception as exc:  # redaction must never break a build
        logger.warning("LLM key discovery failed during secret collection (%s)", type(exc).__name__)
        return []
    return list(_iter_secret_strings(found))


def _iter_secret_strings(found: object) -> Iterable[str]:
    """Yield secret strings from whatever shape ``discover_all_keys`` returns (mapping / list of keys / strings)."""
    if isinstance(found, str):
        yield found
        return
    if isinstance(found, SecretStr):
        yield found.get_secret_value()
        return
    if isinstance(found, Mapping):
        for value in found.values():
            yield from _iter_secret_strings(value)
        return
    secret = getattr(found, "secret", None)
    if secret is not None:
        yield from _iter_secret_strings(secret)
        return
    if isinstance(found, Iterable):
        for item in found:
            yield from _iter_secret_strings(item)


__all__ = [
    "DEFAULT_FREYA_API_URL",
    "KIT_ENV_VARS",
    "MIN_SECRET_LENGTH",
    "RESERVED_KIT_ENV_VARS",
    "LogContent",
    "ProfileName",
    "SelfTestMode",
    "Settings",
    "TrackName",
    "apply_kit_env",
    "effective_env",
    "get_settings",
    "secret_values",
]
