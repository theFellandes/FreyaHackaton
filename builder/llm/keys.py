"""API key discovery, per-provider key pools and the persistent key-health file.

This is the "key" half of the key changer (the "provider" half lives in ``providers.py`` and the loop that
ties both together in ``gateway.py``).

Discovery (``discover_keys`` / ``discover_all_keys``)
    For a provider ``p`` (prefix ``P = p.upper()``) the keys are read, in this order, from
    ``P_API_KEY``, ``P_API_KEY_1`` ... ``P_API_KEY_20`` and ``P_API_KEYS`` (comma / semicolon / newline
    separated). Values are stripped (also of surrounding quotes), empty values and obvious placeholders are
    skipped, duplicates are removed and the first occurrence keeps its position. Keys are numbered from 1.
    The ``claude_code`` provider has no API key: it uses the local Claude Code login, represented by the
    pseudo key ``ApiKey("claude_code", 0, "login")`` which is available when the ``claude_agent_sdk`` package
    is installed and a login exists (``~/.claude/.credentials.json`` or ``CLAUDE_CODE_OAUTH_TOKEN``).

``ApiKey``
    Immutable key handle. Its ``repr`` / ``str`` / ``label`` never show more than the last four characters,
    and ``fingerprint`` (12 hex chars of SHA-256) identifies a key in files without revealing it.

``KeyPool``
    The keys of one provider and their in-memory state: ACTIVE, COOLING (rate limited until a time) or DEAD
    (no credits / invalid). ``next_key`` picks ACTIVE keys first, then keys whose cooldown has expired, each
    group ordered by fewest consecutive failures (ties keep discovery order).

``HealthStore``
    A small JSON file (default ``data/key_health.json``) that remembers DEAD keys across runs by
    provider + fingerprint + reason + timestamp - never the secret - so a key without credits is skipped at
    the start of the next build instead of costing a failed call. Entries expire after a TTL.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import json
import logging
import os
import re
import tempfile
import threading
import time
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from builder import paths

logger = logging.getLogger(__name__)

__all__ = [
    "CLAUDE_CODE",
    "DEFAULT_KEY_PROVIDERS",
    "LOGIN_SECRET",
    "ApiKey",
    "DeadKeyEntry",
    "HealthStore",
    "HealthyKeyEntry",
    "KeyPool",
    "KeyStatus",
    "claude_code_available",
    "claude_code_oauth_token",
    "claude_code_verified",
    "default_env",
    "discover_all_keys",
    "discover_keys",
    "llm_secrets",
    "sdk_installed",
]

CLAUDE_CODE = "claude_code"
"""Name of the provider that runs through the local Claude Code login (Claude Agent SDK)."""

LOGIN_SECRET = "login"
"""Secret value of the ``claude_code`` pseudo key (it is not a secret: the real credential stays in ~/.claude)."""

DEFAULT_KEY_PROVIDERS: tuple[str, ...] = ("anthropic", "openai")
"""Providers whose real API keys ``discover_all_keys`` looks for by default."""

MAX_NUMBERED_KEYS = 20
"""Highest ``<P>_API_KEY_<n>`` suffix that is read."""

_SPLIT = re.compile(r"[,;\n\r]+")
_PLACEHOLDER = re.compile(r"^(?:<.*>|.*\.\.\.|x+|changeme|todo|none|null|your[-_ ].*)$", re.IGNORECASE)
_MIN_COOLDOWN_S = 1.0


# --------------------------------------------------------------------------------------------------------------
# ApiKey
# --------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ApiKey:
    """One credential of one provider.

    Attributes:
        provider: Provider name (``"anthropic"``, ``"openai"``, ``"claude_code"`` ...).
        index: 1-based position in discovery order (``0`` for the Claude Code login pseudo key).
        secret: The key itself. Never printed: ``repr``/``str`` show the masked ``label`` only.
    """

    provider: str
    index: int
    secret: str = field(repr=False)

    @property
    def is_login(self) -> bool:
        """True for the Claude Code login pseudo key (no API key involved)."""
        return self.provider == CLAUDE_CODE and self.secret == LOGIN_SECRET

    @property
    def label(self) -> str:
        """Masked, human-readable name used in traces, e.g. ``anthropic#1(…f3a2)``.

        Only the last four characters are shown, and only for secrets of 12+ characters.
        """
        if self.is_login:
            return f"{self.provider}#{self.index}(login)"
        tail = self.secret[-4:] if len(self.secret) >= 12 else ""
        return f"{self.provider}#{self.index}(…{tail})"

    @property
    def fingerprint(self) -> str:
        """First 12 hex characters of SHA-256(secret): a stable, non-reversible identifier for files."""
        return hashlib.sha256(self.secret.encode("utf-8")).hexdigest()[:12]

    def __repr__(self) -> str:
        return f"ApiKey({self.label})"

    def __str__(self) -> str:
        return self.label


# --------------------------------------------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------------------------------------------


def default_env(env_file: Path | str | None = None) -> dict[str, str]:
    """Return the environment the key changer reads keys from.

    The ``.env`` file (if it exists) is read first and the real process environment overrides it, which is the
    same precedence pydantic-settings uses. This matters because Settings ignores unknown variables such as
    ``ANTHROPIC_API_KEY_2``; they still have to reach key discovery. Empty values are dropped from both sources
    (pydantic-settings' ``env_ignore_empty`` and :func:`builder.settings.effective_env` do the same), so a variable
    that is defined but blank - ``docker run -e VAR=``, a CI template, ``export VAR=`` - never hides a real value
    from ``.env``.

    Args:
        env_file: Path of the dotenv file. ``None`` means ``builder.paths.ENV_FILE``, looked up at call time, so a
            test (or the hermetic ``conftest`` fixture) that redirects it also redirects key discovery.

    Returns:
        A new ``{name: value}`` dict (the file's values are never logged).
    """
    path = Path(env_file) if env_file is not None else Path(paths.ENV_FILE)
    merged: dict[str, str] = {}
    if path.is_file():
        merged.update({name: value for name, value in _read_dotenv(path).items() if value})
    merged.update({name: value for name, value in os.environ.items() if value})
    return merged


def discover_keys(
    provider: str,
    env: Mapping[str, str] | None = None,
    *,
    home: Path | None = None,
) -> list[ApiKey]:
    """Find every configured key of ``provider``.

    Args:
        provider: Provider name; its upper-cased form is the env-var prefix (``openai`` -> ``OPENAI_API_KEY``).
        env: Environment mapping to read; defaults to :func:`default_env`.
        home: Home directory used to look for the Claude Code login (tests); defaults to ``Path.home()``.

    Returns:
        Keys in priority order: ``P_API_KEY``, ``P_API_KEY_1..20``, then the ``P_API_KEYS`` list, deduplicated,
        numbered from 1. For ``claude_code`` a single login pseudo key (or nothing when unavailable).
    """
    env = default_env() if env is None else env
    if provider == CLAUDE_CODE:
        return [ApiKey(CLAUDE_CODE, 0, LOGIN_SECRET)] if claude_code_available(env, home=home) else []
    prefix = provider.upper()
    candidates: list[str] = [env.get(f"{prefix}_API_KEY", "")]
    candidates += [env.get(f"{prefix}_API_KEY_{n}", "") for n in range(1, MAX_NUMBERED_KEYS + 1)]
    candidates += _SPLIT.split(env.get(f"{prefix}_API_KEYS", ""))
    secrets: list[str] = []
    for raw in candidates:
        value = _clean_secret(raw)
        if value and value not in secrets:
            secrets.append(value)
    return [ApiKey(provider, i, s) for i, s in enumerate(secrets, start=1)]


def discover_all_keys(
    env: Mapping[str, str] | None = None,
    providers: Iterable[str] = DEFAULT_KEY_PROVIDERS,
) -> dict[str, list[ApiKey]]:
    """Discover the real API keys of several providers at once.

    The ``claude_code`` login pseudo key is deliberately *not* included (pass ``providers`` explicitly to get
    it): its "secret" is the word ``login``, which must never be redacted from traces.

    Args:
        env: Environment mapping; defaults to :func:`default_env`.
        providers: Provider names to look up.

    Returns:
        ``{provider: [ApiKey, ...]}`` (providers without keys map to an empty list).
    """
    env = default_env() if env is None else env
    return {p: discover_keys(p, env) for p in providers}


def llm_secrets(
    env: Mapping[str, str] | None = None, providers: Iterable[str] = DEFAULT_KEY_PROVIDERS
) -> list[str]:
    """Every LLM credential value found in ``env`` - for trace redaction (``Settings.secret_values``).

    Includes all discovered API keys and ``CLAUDE_CODE_OAUTH_TOKEN``; excludes the ``login`` pseudo secret.
    """
    env = default_env() if env is None else env
    values = [k.secret for keys in discover_all_keys(env, providers).values() for k in keys]
    token = claude_code_oauth_token(env)
    if token:
        values.append(token)
    return list(dict.fromkeys(values))


def claude_code_available(env: Mapping[str, str] | None = None, *, home: Path | None = None) -> bool:
    """Whether the ``claude_code`` provider can run: SDK installed *and* a login is present.

    A login is either ``CLAUDE_CODE_OAUTH_TOKEN`` in ``env`` or a credentials file at
    ``$CLAUDE_CONFIG_DIR/.credentials.json`` (default ``~/.claude/.credentials.json``). On macOS the CLI keeps
    the login in the Keychain; set ``CLAUDE_CODE_OAUTH_TOKEN`` (``claude setup-token``) there.
    """
    env = default_env() if env is None else env
    if not _sdk_installed():
        return False
    if claude_code_oauth_token(env):
        return True
    config_dir = env.get("CLAUDE_CONFIG_DIR")
    base = Path(config_dir) if config_dir else (home or Path.home()) / ".claude"
    return (base / ".credentials.json").is_file()


def claude_code_verified(env: Mapping[str, str] | None, store: HealthStore) -> bool:
    """Whether there is *evidence* that the Claude Code login works, not merely that a login file exists.

    ``claude_code_available`` only says a login may exist; an expired or revoked login still answers
    "Not logged in", and an agentic session that starts on it is doomed (it burns minutes before it fails). The
    login counts as verified when the SDK is installed and either ``CLAUDE_CODE_OAUTH_TOKEN`` is set (an explicit
    token from ``claude setup-token``) or ``store`` holds an unexpired *successful* call with the login (written
    by the key changer and by the agentic runner). An ``ANTHROPIC_API_KEY`` is deliberately no evidence here: the
    login session runs with that variable blanked, and a real key is a candidate of its own.

    Args:
        env: Environment mapping; defaults to :func:`default_env`.
        store: The key-health file that records successful calls.
    """
    env = default_env() if env is None else env
    if not _sdk_installed():
        return False
    if claude_code_oauth_token(env):
        return True
    return store.is_healthy(CLAUDE_CODE, ApiKey(CLAUDE_CODE, 0, LOGIN_SECRET).fingerprint)


def claude_code_oauth_token(env: Mapping[str, str] | None = None) -> str:
    """The ``CLAUDE_CODE_OAUTH_TOKEN`` in ``env`` (quotes stripped), or ``""`` when it is unset or a placeholder.

    The token may be known only through ``.env``. The Claude Agent SDK starts the CLI with ``os.environ`` plus the
    ``env`` of its options, so every caller that starts a login session has to pass this value on explicitly.

    Args:
        env: Environment mapping; defaults to :func:`default_env`.
    """
    env = default_env() if env is None else env
    return _clean_secret(env.get("CLAUDE_CODE_OAUTH_TOKEN", ""))


def sdk_installed() -> bool:
    """True when the optional ``claude_agent_sdk`` package (the ``agentic`` extra) can be imported.

    The one answer every module uses (the key changer, the agentic runner, the graph): tests patch
    ``builder.llm.keys._sdk_installed`` and all of them follow.
    """
    return _sdk_installed()


def _sdk_installed() -> bool:
    try:
        return importlib.util.find_spec("claude_agent_sdk") is not None
    except (ImportError, ValueError):  # pragma: no cover - broken installs
        return False


def _clean_secret(raw: str | None) -> str:
    value = (raw or "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1].strip()
    if not value or _PLACEHOLDER.match(value):
        return ""
    return value


def _read_dotenv(path: Path) -> dict[str, str]:
    """Parse a dotenv file (python-dotenv when installed, else a minimal ``KEY=VALUE`` parser)."""
    try:
        from dotenv import dotenv_values

        return {k: v for k, v in dotenv_values(path).items() if v is not None}
    except ImportError:  # pragma: no cover - python-dotenv ships with pydantic-settings
        pass
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.removeprefix("export ").partition("=")
        values[name.strip()] = value.split(" #", 1)[0].strip()
    return values


# --------------------------------------------------------------------------------------------------------------
# Health store
# --------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DeadKeyEntry:
    """One persisted DEAD key (no secret inside)."""

    provider: str
    fingerprint: str
    reason: str
    marked_at: float
    label: str = ""

    def to_json(self) -> dict[str, Any]:
        """Serialise for the health file (includes a readable UTC timestamp)."""
        return {
            "provider": self.provider,
            "fingerprint": self.fingerprint,
            "label": self.label,
            "reason": self.reason,
            "marked_at": self.marked_at,
            "marked_at_utc": datetime.fromtimestamp(self.marked_at, UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }


@dataclass(frozen=True, slots=True)
class HealthyKeyEntry:
    """One persisted successful call with a key (no secret inside): the proof that a login really works."""

    provider: str
    fingerprint: str
    marked_at: float
    label: str = ""

    def to_json(self) -> dict[str, Any]:
        """Serialise for the health file (includes a readable UTC timestamp)."""
        return {
            "provider": self.provider,
            "fingerprint": self.fingerprint,
            "label": self.label,
            "marked_at": self.marked_at,
            "marked_at_utc": datetime.fromtimestamp(self.marked_at, UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }


class HealthStore:
    """Persist DEAD keys (and keys that worked) across runs in a small JSON file.

    File format (``version`` 1)::

        {"version": 1, "entries": [{"provider": "anthropic", "fingerprint": "3f9c0a1b2c3d",
          "label": "anthropic#1(…f3a2)", "reason": "quota: ...", "marked_at": 1760000000.0,
          "marked_at_utc": "2025-10-09T08:53:20Z"}],
         "healthy": [{"provider": "claude_code", "fingerprint": "...", "label": "claude_code#0(login)",
          "marked_at": 1760000000.0, "marked_at_utc": "2025-10-09T08:53:20Z"}]}

    ``healthy`` (optional) lists keys a call succeeded with; marking a key dead removes it from there and marking it
    healthy removes it from ``entries``, so the two lists never contradict each other.
    Entries older than ``ttl_s`` are ignored and pruned on the next write. A missing, unreadable or corrupt
    file is treated as empty, and write failures (read-only volume) only log a warning: key health is an
    optimisation and must never break a build.
    """

    def __init__(self, path: Path | str, ttl_s: float, clock: Callable[[], float] = time.time) -> None:
        """Create a store backed by ``path`` whose entries live ``ttl_s`` seconds (``clock`` = epoch seconds)."""
        self.path = Path(path)
        self.ttl_s = float(ttl_s)
        self._clock = clock
        self._lock = threading.Lock()

    def dead_entries(self) -> dict[tuple[str, str], DeadKeyEntry]:
        """Unexpired entries keyed by ``(provider, fingerprint)``."""
        with self._lock:
            return self._live(self._read())

    def healthy_entries(self) -> dict[tuple[str, str], HealthyKeyEntry]:
        """Unexpired successful-call records keyed by ``(provider, fingerprint)``."""
        with self._lock:
            return self._live_healthy(self._read_healthy())

    def is_healthy(self, provider: str, fingerprint: str) -> bool:
        """True when a call with this key succeeded within the TTL and the key was not marked dead since."""
        if self.lookup(provider, fingerprint) is not None:
            return False
        return (provider, fingerprint) in self.healthy_entries()

    def mark_healthy(self, provider: str, fingerprint: str, label: str = "") -> None:
        """Record that a call with this key succeeded (also clears a dead entry of the same key)."""
        with self._lock:
            dead = self._live(self._read())
            healthy = self._live_healthy(self._read_healthy())
            dead.pop((provider, fingerprint), None)
            healthy[(provider, fingerprint)] = HealthyKeyEntry(provider, fingerprint, self._clock(), label)
            self._write(dead, healthy)

    def lookup(self, provider: str, fingerprint: str) -> DeadKeyEntry | None:
        """The unexpired entry for this key, or ``None``."""
        return self.dead_entries().get((provider, fingerprint))

    def mark_dead(self, provider: str, fingerprint: str, reason: str, label: str = "") -> None:
        """Record (or refresh) a dead key and prune expired entries."""
        with self._lock:
            entries = self._live(self._read())
            healthy = self._live_healthy(self._read_healthy())
            healthy.pop((provider, fingerprint), None)
            entries[(provider, fingerprint)] = DeadKeyEntry(
                provider=provider,
                fingerprint=fingerprint,
                reason=reason[:300],
                marked_at=self._clock(),
                label=label,
            )
            self._write(entries, healthy)

    def forget(self, provider: str, fingerprint: str) -> None:
        """Remove a key's entry (e.g. it worked again after being revived)."""
        with self._lock:
            entries = self._live(self._read())
            if entries.pop((provider, fingerprint), None) is not None:
                self._write(entries, self._live_healthy(self._read_healthy()))

    def _live(self, entries: dict[tuple[str, str], DeadKeyEntry]) -> dict[tuple[str, str], DeadKeyEntry]:
        now = self._clock()
        return {k: e for k, e in entries.items() if now - e.marked_at < self.ttl_s}

    def _live_healthy(
        self, entries: dict[tuple[str, str], HealthyKeyEntry]
    ) -> dict[tuple[str, str], HealthyKeyEntry]:
        now = self._clock()
        return {k: e for k, e in entries.items() if now - e.marked_at < self.ttl_s}

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            logger.warning("Ignoring unreadable key-health file %s: %s", self.path, exc.__class__.__name__)
            return {}
        return data if isinstance(data, dict) else {}

    def _read_healthy(self) -> dict[tuple[str, str], HealthyKeyEntry]:
        raw_entries = self._load().get("healthy", [])
        entries: dict[tuple[str, str], HealthyKeyEntry] = {}
        for raw in raw_entries if isinstance(raw_entries, list) else []:
            try:
                entry = HealthyKeyEntry(
                    provider=str(raw["provider"]),
                    fingerprint=str(raw["fingerprint"]),
                    marked_at=float(raw["marked_at"]),
                    label=str(raw.get("label", "")),
                )
            except (KeyError, TypeError, ValueError, AttributeError):
                continue
            entries[(entry.provider, entry.fingerprint)] = entry
        return entries

    def _read(self) -> dict[tuple[str, str], DeadKeyEntry]:
        data = self._load()
        entries: dict[tuple[str, str], DeadKeyEntry] = {}
        raw_entries = data.get("entries", [])
        for raw in raw_entries if isinstance(raw_entries, list) else []:
            try:
                entry = DeadKeyEntry(
                    provider=str(raw["provider"]),
                    fingerprint=str(raw["fingerprint"]),
                    reason=str(raw.get("reason", "")),
                    marked_at=float(raw["marked_at"]),
                    label=str(raw.get("label", "")),
                )
            except (KeyError, TypeError, ValueError, AttributeError):
                continue
            entries[(entry.provider, entry.fingerprint)] = entry
        return entries

    def _write(
        self,
        entries: dict[tuple[str, str], DeadKeyEntry],
        healthy: dict[tuple[str, str], HealthyKeyEntry] | None = None,
    ) -> None:
        payload: dict[str, Any] = {"version": 1, "entries": [e.to_json() for e in entries.values()]}
        if healthy:
            payload["healthy"] = [e.to_json() for e in healthy.values()]
        tmp_name: str | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(prefix=".key_health.", suffix=".tmp", dir=self.path.parent)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, indent=2, ensure_ascii=False)
                handle.write("\n")
            os.replace(tmp_name, self.path)
            tmp_name = None
        except OSError as exc:
            logger.warning("Could not write key-health file %s: %s", self.path, exc.__class__.__name__)
        finally:
            if tmp_name is not None:
                with contextlib.suppress(OSError):
                    os.unlink(tmp_name)


# --------------------------------------------------------------------------------------------------------------
# Key pool
# --------------------------------------------------------------------------------------------------------------


class KeyStatus(StrEnum):
    """Lifecycle state of a key inside a :class:`KeyPool`."""

    ACTIVE = "active"
    COOLING = "cooling"
    DEAD = "dead"


@dataclass
class _KeyState:
    status: KeyStatus = KeyStatus.ACTIVE
    failures: int = 0
    total_failures: int = 0
    successes: int = 0
    cooling_until: float = 0.0
    was_cooling: bool = False
    dead_reason: str | None = None
    persisted_dead: bool = False
    revived: bool = False
    healthy_recorded: bool = False
    last_error_kind: str | None = None


class KeyPool:
    """The keys of one provider plus their health, with rotation logic.

    The pool is purely bookkeeping: it never calls a provider. The gateway asks it for the next key, reports
    the outcome of each attempt (``mark_success`` / ``mark_failure`` / ``mark_cooling`` / ``mark_dead``) and
    reads ``soonest_cooldown_s`` to decide whether waiting is worth it.
    """

    def __init__(
        self,
        provider: str,
        keys: Iterable[ApiKey],
        store: HealthStore | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Build a pool; keys recorded as dead in ``store`` start DEAD (``persisted_dead``)."""
        self.provider = provider
        self._store = store
        self._clock = clock
        self._lock = threading.Lock()
        unique: dict[str, ApiKey] = {}
        for key in keys:
            unique.setdefault(key.fingerprint, key)
        self._keys: tuple[ApiKey, ...] = tuple(unique.values())
        self._state: dict[str, _KeyState] = {k.fingerprint: _KeyState() for k in self._keys}
        if store is not None:
            dead = store.dead_entries()
            for key in self._keys:
                entry = dead.get((provider, key.fingerprint))
                if entry is not None:
                    state = self._state[key.fingerprint]
                    state.status = KeyStatus.DEAD
                    state.persisted_dead = True
                    state.dead_reason = f"(previous run) {entry.reason}"

    @property
    def keys(self) -> tuple[ApiKey, ...]:
        """All keys of the pool in priority order (including dead ones)."""
        return self._keys

    def __len__(self) -> int:
        return len(self._keys)

    def status(self, key: ApiKey) -> KeyStatus:
        """Current status of ``key`` (cooling keys whose time is up are reported ACTIVE)."""
        with self._lock:
            state = self._state[key.fingerprint]
            self._refresh(state)
            return state.status

    def next_key(self, exclude: Collection[ApiKey] = ()) -> ApiKey | None:
        """Pick the key to use next, or ``None`` when no key is usable right now.

        ACTIVE keys come first, then keys whose cooldown has expired; within each group fewest consecutive
        failures first, ties in discovery order. DEAD keys, keys still cooling and keys in ``exclude`` are
        never returned.
        """
        excluded = {k.fingerprint for k in exclude}
        with self._lock:
            candidates: list[tuple[bool, int, int, ApiKey]] = []
            for position, key in enumerate(self._keys):
                if key.fingerprint in excluded:
                    continue
                state = self._state[key.fingerprint]
                self._refresh(state)
                if state.status is KeyStatus.ACTIVE:
                    candidates.append((state.was_cooling, state.failures, position, key))
            if not candidates:
                return None
            return min(candidates, key=lambda c: c[:3])[3]

    def soonest_cooldown_s(self, exclude: Collection[ApiKey] = ()) -> float | None:
        """Seconds until the first COOLING key becomes usable again (``None`` if no key is cooling)."""
        excluded = {k.fingerprint for k in exclude}
        now = self._clock()
        with self._lock:
            remaining = [
                max(0.0, s.cooling_until - now)
                for k in self._keys
                if k.fingerprint not in excluded
                and (s := self._state[k.fingerprint]).status is KeyStatus.COOLING
            ]
        return min(remaining) if remaining else None

    def has_live_keys(self) -> bool:
        """True when at least one key is not DEAD (it may be cooling)."""
        with self._lock:
            return any(s.status is not KeyStatus.DEAD for s in self._state.values())

    def mark_success(self, key: ApiKey) -> None:
        """Record a successful call: the key is ACTIVE with zero consecutive failures.

        The first success of the Claude Code login in a pool is also written to the health store (once per pool, not
        per call): that record is the proof the agentic track needs before it may use the login.
        """
        forget = False
        with self._lock:
            state = self._state[key.fingerprint]
            state.status = KeyStatus.ACTIVE
            state.failures = 0
            state.successes += 1
            state.was_cooling = False
            state.cooling_until = 0.0
            state.dead_reason = None
            forget = state.revived or state.persisted_dead
            state.persisted_dead = False
            state.revived = False
            record_healthy = key.is_login and not state.healthy_recorded
            state.healthy_recorded = state.healthy_recorded or record_healthy
        if self._store is not None:
            if forget:
                self._store.forget(self.provider, key.fingerprint)
            if record_healthy:
                self._store.mark_healthy(self.provider, key.fingerprint, key.label)

    def mark_failure(self, key: ApiKey, kind: str | None = None) -> None:
        """Record a failed attempt (increments the failure counters; does not change the status)."""
        with self._lock:
            state = self._state[key.fingerprint]
            state.failures += 1
            state.total_failures += 1
            state.last_error_kind = kind

    def mark_cooling(self, key: ApiKey, seconds: float) -> None:
        """Rate-limited: the key is unusable for ``seconds`` (at least 1 s). DEAD keys stay DEAD."""
        with self._lock:
            state = self._state[key.fingerprint]
            if state.status is KeyStatus.DEAD:
                return
            state.status = KeyStatus.COOLING
            state.was_cooling = True
            state.cooling_until = self._clock() + max(_MIN_COOLDOWN_S, float(seconds))

    def mark_dead(self, key: ApiKey, reason: str) -> None:
        """No credits / invalid key: unusable for the rest of the run and persisted to the health store."""
        clean_reason = reason.replace(key.secret, "***") if not key.is_login else reason
        with self._lock:
            state = self._state[key.fingerprint]
            state.status = KeyStatus.DEAD
            state.dead_reason = clean_reason
            state.revived = False
            state.healthy_recorded = False
        if self._store is not None:
            self._store.mark_dead(self.provider, key.fingerprint, clean_reason, label=key.label)

    def revive_persisted(self) -> list[ApiKey]:
        """Give keys that are DEAD *only because of a previous run* one more chance (last resort).

        Returns:
            The keys that were set back to ACTIVE. Each key can be revived at most once per pool.
        """
        revived: list[ApiKey] = []
        with self._lock:
            for key in self._keys:
                state = self._state[key.fingerprint]
                if state.status is KeyStatus.DEAD and state.persisted_dead:
                    state.status = KeyStatus.ACTIVE
                    state.persisted_dead = False
                    state.revived = True
                    state.dead_reason = None
                    revived.append(key)
        return revived

    def snapshot(self) -> list[dict[str, Any]]:
        """Secret-free view of every key's state (for ``describe()``, diagnostics and tests)."""
        now = self._clock()
        with self._lock:
            rows = []
            for key in self._keys:
                state = self._state[key.fingerprint]
                self._refresh(state)
                rows.append(
                    {
                        "label": key.label,
                        "fingerprint": key.fingerprint,
                        "status": state.status.value,
                        "failures": state.failures,
                        "total_failures": state.total_failures,
                        "successes": state.successes,
                        "cooling_remaining_s": round(max(0.0, state.cooling_until - now), 1)
                        if state.status is KeyStatus.COOLING
                        else 0.0,
                        "dead_reason": state.dead_reason,
                        "last_error_kind": state.last_error_kind,
                    }
                )
            return rows

    def _refresh(self, state: _KeyState) -> None:
        if state.status is KeyStatus.COOLING and self._clock() >= state.cooling_until:
            state.status = KeyStatus.ACTIVE
