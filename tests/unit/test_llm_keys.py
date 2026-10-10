"""Tests for builder.llm.keys: discovery, ApiKey masking, KeyPool rotation/cooldown, HealthStore persistence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from builder import paths
from builder.llm import keys as keys_module
from builder.llm.keys import (
    CLAUDE_CODE,
    ApiKey,
    HealthStore,
    KeyPool,
    KeyStatus,
    claude_code_available,
    claude_code_verified,
    default_env,
    discover_all_keys,
    discover_keys,
    llm_secrets,
)

K1 = "sk-ant-api03-AAAAAAAAAAAAAAAA1111"
K2 = "sk-ant-api03-BBBBBBBBBBBBBBBB2222"
K3 = "sk-ant-api03-CCCCCCCCCCCCCCCC3333"


class FakeClock:
    """Epoch-seconds clock that only moves when told to."""

    def __init__(self, start: float = 1_760_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_keys(*secrets: str, provider: str = "anthropic") -> list[ApiKey]:
    return [ApiKey(provider, i, s) for i, s in enumerate(secrets, start=1)]


# ------------------------------------------------------------------------------------------------ ApiKey


def test_api_key_label_repr_and_str_are_masked() -> None:
    key = ApiKey("anthropic", 1, K1)
    assert key.label == "anthropic#1(…1111)"
    assert repr(key) == "ApiKey(anthropic#1(…1111))"
    assert str(key) == key.label
    assert K1 not in repr([key]) and K1 not in f"{key}"


def test_short_secret_label_shows_no_characters() -> None:
    assert ApiKey("openai", 2, "short-key").label == "openai#2(…)"


def test_fingerprint_is_stable_short_and_not_the_secret() -> None:
    a, b = ApiKey("anthropic", 1, K1), ApiKey("anthropic", 9, K1)
    assert a.fingerprint == b.fingerprint
    assert len(a.fingerprint) == 12 and all(c in "0123456789abcdef" for c in a.fingerprint)
    assert a.fingerprint != ApiKey("anthropic", 1, K2).fingerprint
    assert a.fingerprint not in K1


def test_login_pseudo_key() -> None:
    key = ApiKey(CLAUDE_CODE, 0, "login")
    assert key.is_login
    assert key.label == "claude_code#0(login)"
    assert not ApiKey("anthropic", 1, "login").is_login


# --------------------------------------------------------------------------------------------- discovery


def test_discovery_order_dedupe_and_numbering() -> None:
    env = {
        "ANTHROPIC_API_KEY": K1,
        "ANTHROPIC_API_KEY_2": K3,
        "ANTHROPIC_API_KEY_1": K2,
        "ANTHROPIC_API_KEYS": f" k4-xxxxxxxxxxxxxxxx ;{K3}, k5-yyyyyyyyyyyyyyyy\nk6-zzzzzzzzzzzzzzzz ",
        "OPENAI_API_KEY": "sk-proj-OOOOOOOOOOOOOOOO0000",
    }
    found = discover_keys("anthropic", env)
    assert [k.secret for k in found] == [
        K1,
        K2,
        K3,
        "k4-xxxxxxxxxxxxxxxx",
        "k5-yyyyyyyyyyyyyyyy",
        "k6-zzzzzzzzzzzzzzzz",
    ]
    assert [k.index for k in found] == [1, 2, 3, 4, 5, 6]
    assert all(k.provider == "anthropic" for k in found)


def test_discovery_strips_quotes_and_skips_empty_and_placeholders() -> None:
    env = {
        "OPENAI_API_KEY": '  "sk-proj-QUOTEDQUOTEDQUOTED"  ',
        "OPENAI_API_KEY_1": "",
        "OPENAI_API_KEY_2": "   ",
        "OPENAI_API_KEY_3": "<your key>",
        "OPENAI_API_KEY_4": "sk-...",
        "OPENAI_API_KEY_5": "your_openai_key_here",
        "OPENAI_API_KEYS": ",,;",
    }
    assert [k.secret for k in discover_keys("openai", env)] == ["sk-proj-QUOTEDQUOTEDQUOTED"]


def test_discovery_reads_numbered_keys_up_to_20_only() -> None:
    env = {"ANTHROPIC_API_KEY_20": K1, "ANTHROPIC_API_KEY_21": K2}
    assert [k.secret for k in discover_keys("anthropic", env)] == [K1]


def test_discovery_without_keys_is_empty() -> None:
    assert discover_keys("anthropic", {}) == []


def test_discover_all_keys_excludes_claude_code_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(keys_module, "_sdk_installed", lambda: True)
    env = {"ANTHROPIC_API_KEY": K1, "OPENAI_API_KEY": K2, "CLAUDE_CODE_OAUTH_TOKEN": "oauth-token-123456"}
    found = discover_all_keys(env)
    assert set(found) == {"anthropic", "openai"}
    assert [k.secret for k in found["anthropic"]] == [K1]
    with_cc = discover_all_keys(env, providers=("anthropic", CLAUDE_CODE))
    assert [k.label for k in with_cc[CLAUDE_CODE]] == ["claude_code#0(login)"]


def test_llm_secrets_lists_keys_and_oauth_token_but_never_login(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(keys_module, "_sdk_installed", lambda: True)
    env = {
        "ANTHROPIC_API_KEY": K1,
        "ANTHROPIC_API_KEYS": f"{K1},{K2}",
        "CLAUDE_CODE_OAUTH_TOKEN": "oauth-tok-999999",
    }
    assert llm_secrets(env) == [K1, K2, "oauth-tok-999999"]


def test_claude_code_needs_sdk_and_login(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(keys_module, "_sdk_installed", lambda: True)
    assert discover_keys(CLAUDE_CODE, {}, home=tmp_path) == []
    credentials = tmp_path / ".claude" / ".credentials.json"
    credentials.parent.mkdir()
    credentials.write_text("{}", encoding="utf-8")
    found = discover_keys(CLAUDE_CODE, {}, home=tmp_path)
    assert found == [ApiKey(CLAUDE_CODE, 0, "login")]
    monkeypatch.setattr(keys_module, "_sdk_installed", lambda: False)
    assert discover_keys(CLAUDE_CODE, {}, home=tmp_path) == []


def test_claude_code_available_with_oauth_token_or_config_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(keys_module, "_sdk_installed", lambda: True)
    empty_home = tmp_path / "home"
    empty_home.mkdir()
    assert claude_code_available({"CLAUDE_CODE_OAUTH_TOKEN": "tok-abcdef"}, home=empty_home)
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    (config_dir / ".credentials.json").write_text("{}", encoding="utf-8")
    assert claude_code_available({"CLAUDE_CONFIG_DIR": str(config_dir)}, home=empty_home)
    assert not claude_code_available({}, home=empty_home)


def test_default_env_reads_dotenv_and_environment_wins(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "ANTHROPIC_API_KEY_2=from-file-key-2222\nOPENAI_API_KEY=from-file-openai\n", encoding="utf-8"
    )
    monkeypatch.setenv("OPENAI_API_KEY", "from-process-openai")
    monkeypatch.delenv("ANTHROPIC_API_KEY_2", raising=False)
    env = default_env(env_file)
    assert env["ANTHROPIC_API_KEY_2"] == "from-file-key-2222"
    assert env["OPENAI_API_KEY"] == "from-process-openai"


def test_default_env_without_file_is_process_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LLM_KEYS_TEST_MARKER", "1")
    env = default_env(tmp_path / "missing.env")
    assert env["LLM_KEYS_TEST_MARKER"] == "1"


def test_default_env_ignores_empty_values_like_settings_do(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """REPO-15: a defined-but-empty variable (``docker -e VAR=``, CI templates) must not hide the ``.env`` value."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "OPENAI_API_KEY=sk-from-dotenv-0000000000" + chr(10) + "EMPTY_IN_FILE=" + chr(10), encoding="utf-8"
    )
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setenv("EMPTY_IN_PROCESS", "")
    env = default_env(env_file)
    assert env.get("OPENAI_API_KEY") == "sk-from-dotenv-0000000000"
    assert "EMPTY_IN_PROCESS" not in env and "EMPTY_IN_FILE" not in env
    assert [k.secret for k in discover_keys("openai", env)] == ["sk-from-dotenv-0000000000"]


def test_default_env_reads_the_dotenv_that_paths_points_to(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """REPO-16: ``builder.paths.ENV_FILE`` (looked up at call time) is the one ``.env``; tests redirect it there."""
    env_file = tmp_path / "elsewhere.env"
    env_file.write_text("PATHS_ENV_FILE_MARKER=from-the-patched-path" + chr(10), encoding="utf-8")
    monkeypatch.setattr(paths, "ENV_FILE", env_file)
    assert default_env().get("PATHS_ENV_FILE_MARKER") == "from-the-patched-path"


def test_default_env_does_not_read_the_repository_dotenv_under_the_test_fixture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hermetic fixture points ``paths.ENV_FILE`` at a missing file, so no developer secrets reach a test."""
    opened: list[Path] = []
    real_read = keys_module._read_dotenv
    monkeypatch.setattr(keys_module, "_read_dotenv", lambda path: opened.append(path) or real_read(path))
    default_env()
    assert paths.REPO_ROOT / ".env" not in opened


def test_oauth_token_helper_cleans_the_value() -> None:
    from builder.llm.keys import claude_code_oauth_token

    assert (
        claude_code_oauth_token({"CLAUDE_CODE_OAUTH_TOKEN": '  "tok-abcdef123456"  '}) == "tok-abcdef123456"
    )
    assert claude_code_oauth_token({"CLAUDE_CODE_OAUTH_TOKEN": "changeme"}) == ""  # placeholder
    assert claude_code_oauth_token({"CLAUDE_CODE_OAUTH_TOKEN": ""}) == ""
    assert claude_code_oauth_token({}) == ""


# ---------------------------------------------------------------------------------------------- KeyPool


def test_pool_prefers_active_keys_in_discovery_order() -> None:
    k1, k2, k3 = make_keys(K1, K2, K3)
    pool = KeyPool("anthropic", [k1, k2, k3], clock=FakeClock())
    assert pool.next_key() == k1
    assert pool.next_key(exclude=[k1]) == k2
    assert pool.next_key(exclude=[k1, k2, k3]) is None


def test_pool_prefers_fewest_consecutive_failures() -> None:
    k1, k2, k3 = make_keys(K1, K2, K3)
    pool = KeyPool("anthropic", [k1, k2, k3], clock=FakeClock())
    pool.mark_failure(k1, "unknown")
    pool.mark_failure(k1, "unknown")
    pool.mark_failure(k2, "unknown")
    assert pool.next_key() == k3
    pool.mark_failure(k3, "unknown")
    assert pool.next_key() == k2  # k2 and k3 tie on 1 failure -> discovery order
    pool.mark_success(k1)
    assert pool.next_key() == k1


def test_pool_deduplicates_identical_secrets() -> None:
    pool = KeyPool("anthropic", [ApiKey("anthropic", 1, K1), ApiKey("anthropic", 2, K1)], clock=FakeClock())
    assert len(pool) == 1


def test_cooling_key_returns_after_expiry_but_after_active_keys() -> None:
    clock = FakeClock()
    k1, k2 = make_keys(K1, K2)
    pool = KeyPool("anthropic", [k1, k2], clock=clock)
    pool.mark_cooling(k1, 30)
    assert pool.status(k1) is KeyStatus.COOLING
    assert pool.next_key() == k2
    assert pool.next_key(exclude=[k2]) is None
    assert pool.soonest_cooldown_s() == pytest.approx(30)
    clock.advance(10)
    assert pool.soonest_cooldown_s() == pytest.approx(20)
    clock.advance(20)
    assert pool.status(k1) is KeyStatus.ACTIVE
    assert pool.next_key() == k2  # a key fresh from cooldown ranks after never-cooled active keys
    assert pool.next_key(exclude=[k2]) == k1
    assert pool.soonest_cooldown_s() is None


def test_mark_cooling_has_a_minimum_and_never_resurrects_dead_keys() -> None:
    clock = FakeClock()
    k1, k2 = make_keys(K1, K2)
    pool = KeyPool("anthropic", [k1, k2], clock=clock)
    pool.mark_cooling(k1, 0)
    assert pool.soonest_cooldown_s() == pytest.approx(1.0)
    pool.mark_dead(k2, "quota: no credits")
    pool.mark_cooling(k2, 5)
    assert pool.status(k2) is KeyStatus.DEAD


def test_dead_keys_are_never_returned() -> None:
    k1, k2 = make_keys(K1, K2)
    pool = KeyPool("anthropic", [k1, k2], clock=FakeClock())
    pool.mark_dead(k1, "auth: invalid key")
    assert pool.next_key() == k2
    assert pool.has_live_keys()
    pool.mark_dead(k2, "quota: no credits")
    assert pool.next_key() is None
    assert not pool.has_live_keys()


def test_mark_success_clears_failures_and_cooling_flag() -> None:
    clock = FakeClock()
    k1, k2 = make_keys(K1, K2)
    pool = KeyPool("anthropic", [k1, k2], clock=clock)
    pool.mark_failure(k1, "rate_limit")
    pool.mark_cooling(k1, 2)
    clock.advance(3)
    pool.mark_success(k1)
    row = pool.snapshot()[0]
    assert row["status"] == "active" and row["failures"] == 0 and row["successes"] == 1
    assert row["total_failures"] == 1
    assert pool.next_key() == k1


def test_snapshot_contains_no_secret() -> None:
    k1, k2 = make_keys(K1, K2)
    pool = KeyPool("anthropic", [k1, k2], clock=FakeClock())
    pool.mark_dead(k1, f"auth: bad key {K1}")
    dumped = json.dumps(pool.snapshot(), ensure_ascii=False)
    assert K1 not in dumped and K2 not in dumped
    assert "anthropic#1(…1111)" in dumped


# ------------------------------------------------------------------------------------------- HealthStore


def test_health_store_persists_dead_keys_without_secrets(tmp_path: Path) -> None:
    path = tmp_path / "data" / "key_health.json"
    clock = FakeClock()
    k1, k2 = make_keys(K1, K2)
    pool = KeyPool("anthropic", [k1, k2], store=HealthStore(path, ttl_s=3600, clock=clock), clock=clock)
    pool.mark_dead(k1, f"quota: credit balance is too low for {K1}")
    raw = path.read_text(encoding="utf-8")
    assert K1 not in raw and K2 not in raw
    data = json.loads(raw)
    assert data["version"] == 1
    (entry,) = data["entries"]
    assert entry["provider"] == "anthropic"
    assert entry["fingerprint"] == k1.fingerprint
    assert entry["label"] == "anthropic#1(…1111)"
    assert entry["reason"].startswith("quota: credit balance is too low")
    assert entry["marked_at_utc"].endswith("Z")

    # next run: a fresh pool backed by the same file starts with k1 dead
    next_pool = KeyPool("anthropic", [k1, k2], store=HealthStore(path, ttl_s=3600, clock=clock), clock=clock)
    assert next_pool.status(k1) is KeyStatus.DEAD
    assert next_pool.next_key() == k2
    assert next_pool.snapshot()[0]["dead_reason"].startswith("(previous run)")


def test_health_store_entries_expire_after_ttl(tmp_path: Path) -> None:
    path = tmp_path / "key_health.json"
    clock = FakeClock()
    store = HealthStore(path, ttl_s=100, clock=clock)
    store.mark_dead("anthropic", "abc123abc123", "quota: x")
    assert store.lookup("anthropic", "abc123abc123") is not None
    clock.advance(101)
    assert store.lookup("anthropic", "abc123abc123") is None
    store.mark_dead("openai", "def456def456", "auth: y")  # write prunes the expired entry
    entries = json.loads(path.read_text(encoding="utf-8"))["entries"]
    assert [(e["provider"], e["fingerprint"]) for e in entries] == [("openai", "def456def456")]


def test_health_store_is_per_provider(tmp_path: Path) -> None:
    clock = FakeClock()
    store = HealthStore(tmp_path / "h.json", ttl_s=3600, clock=clock)
    key = ApiKey("anthropic", 1, K1)
    store.mark_dead("openai", key.fingerprint, "auth")
    pool = KeyPool("anthropic", [key], store=store, clock=clock)
    assert pool.status(key) is KeyStatus.ACTIVE


def test_health_store_tolerates_corrupt_file(tmp_path: Path) -> None:
    path = tmp_path / "key_health.json"
    path.write_text("{not json", encoding="utf-8")
    store = HealthStore(path, ttl_s=3600, clock=FakeClock())
    assert store.dead_entries() == {}
    store.mark_dead("anthropic", "abc123abc123", "quota")
    assert len(json.loads(path.read_text(encoding="utf-8"))["entries"]) == 1


def test_health_store_ignores_malformed_entries(tmp_path: Path) -> None:
    path = tmp_path / "key_health.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "entries": [
                    {"provider": "x"},
                    "junk",
                    {"provider": "a", "fingerprint": "f", "marked_at": 1_760_000_000.0},
                ],
            }
        ),
        encoding="utf-8",
    )
    store = HealthStore(path, ttl_s=3600, clock=FakeClock())
    assert list(store.dead_entries()) == [("a", "f")]


def test_health_store_write_failure_does_not_raise(tmp_path: Path) -> None:
    store = HealthStore(tmp_path, ttl_s=3600, clock=FakeClock())  # the "file" is a directory
    store.mark_dead("anthropic", "abc123abc123", "quota")  # logs a warning, no exception
    assert store.dead_entries() == {}


def test_revive_persisted_dead_keys_and_forget_on_success(tmp_path: Path) -> None:
    path = tmp_path / "key_health.json"
    clock = FakeClock()
    k1, k2 = make_keys(K1, K2)
    store = HealthStore(path, ttl_s=3600, clock=clock)
    store.mark_dead("anthropic", k1.fingerprint, "quota", label=k1.label)
    pool = KeyPool("anthropic", [k1, k2], store=store, clock=clock)
    pool.mark_dead(k2, "auth: invalid")  # dead in *this* run: never revived
    assert pool.next_key() is None
    assert pool.revive_persisted() == [k1]
    assert pool.revive_persisted() == []  # at most once
    assert pool.next_key() == k1
    pool.mark_success(k1)
    assert store.lookup("anthropic", k1.fingerprint) is None
    assert store.lookup("anthropic", k2.fingerprint) is not None


def test_forget_removes_entry(tmp_path: Path) -> None:
    store = HealthStore(tmp_path / "h.json", ttl_s=3600, clock=FakeClock())
    store.mark_dead("anthropic", "abc123abc123", "quota")
    store.forget("anthropic", "abc123abc123")
    assert store.dead_entries() == {}


# ------------------------------------------------------------------- healthy keys and the verified login

LOGIN = ApiKey(CLAUDE_CODE, 0, "login")


def test_healthy_entries_round_trip_without_secrets_and_expire(tmp_path: Path) -> None:
    path = tmp_path / "key_health.json"
    clock = FakeClock()
    store = HealthStore(path, ttl_s=100, clock=clock)
    assert not store.is_healthy(CLAUDE_CODE, LOGIN.fingerprint)
    store.mark_healthy(CLAUDE_CODE, LOGIN.fingerprint, label=LOGIN.label)
    assert store.is_healthy(CLAUDE_CODE, LOGIN.fingerprint)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["entries"] == []
    (entry,) = data["healthy"]
    assert entry["provider"] == CLAUDE_CODE and entry["fingerprint"] == LOGIN.fingerprint
    assert entry["label"] == "claude_code#0(login)" and entry["marked_at_utc"].endswith("Z")
    # a second store on the same file (the next run) sees it; after the TTL it is gone
    assert HealthStore(path, ttl_s=100, clock=clock).is_healthy(CLAUDE_CODE, LOGIN.fingerprint)
    clock.advance(101)
    assert not store.is_healthy(CLAUDE_CODE, LOGIN.fingerprint)
    assert store.healthy_entries() == {}


def test_marking_dead_and_healthy_never_contradict_each_other(tmp_path: Path) -> None:
    store = HealthStore(tmp_path / "h.json", ttl_s=3600, clock=FakeClock())
    store.mark_healthy(CLAUDE_CODE, LOGIN.fingerprint)
    store.mark_dead(CLAUDE_CODE, LOGIN.fingerprint, "auth: Not logged in")
    assert store.healthy_entries() == {} and not store.is_healthy(CLAUDE_CODE, LOGIN.fingerprint)
    assert store.lookup(CLAUDE_CODE, LOGIN.fingerprint) is not None
    store.mark_healthy(CLAUDE_CODE, LOGIN.fingerprint)
    assert store.lookup(CLAUDE_CODE, LOGIN.fingerprint) is None
    assert store.is_healthy(CLAUDE_CODE, LOGIN.fingerprint)


def test_other_dead_keys_do_not_erase_a_healthy_record_and_forget_keeps_it(tmp_path: Path) -> None:
    store = HealthStore(tmp_path / "h.json", ttl_s=3600, clock=FakeClock())
    store.mark_healthy(CLAUDE_CODE, LOGIN.fingerprint)
    store.mark_dead("anthropic", "abc123abc123", "quota")
    store.forget("anthropic", "abc123abc123")
    assert store.is_healthy(CLAUDE_CODE, LOGIN.fingerprint)


def test_malformed_healthy_entries_are_ignored(tmp_path: Path) -> None:
    path = tmp_path / "key_health.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "entries": [],
                "healthy": [
                    {"provider": "x"},
                    "junk",
                    {"provider": "a", "fingerprint": "f", "marked_at": 1_760_000_000.0},
                ],
            }
        ),
        encoding="utf-8",
    )
    store = HealthStore(path, ttl_s=3600, clock=FakeClock())
    assert list(store.healthy_entries()) == [("a", "f")]
    path.write_text(json.dumps({"version": 1, "entries": [], "healthy": "nope"}), encoding="utf-8")
    assert store.healthy_entries() == {}


def test_a_credentials_file_alone_does_not_verify_the_login(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The machine answers "Not logged in" although ~/.claude/.credentials.json exists: that is no evidence."""
    monkeypatch.setattr(keys_module, "_sdk_installed", lambda: True)
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    (config_dir / ".credentials.json").write_text("{}", encoding="utf-8")
    env = {"CLAUDE_CONFIG_DIR": str(config_dir)}
    store = HealthStore(tmp_path / "h.json", ttl_s=3600, clock=FakeClock())
    assert claude_code_available(env)  # "may exist" (the key changer may try it once) ...
    assert not claude_code_verified(env, store)  # ... but it is not proven


def test_an_api_key_is_no_evidence_for_the_login(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The login session runs with ANTHROPIC_API_KEY blanked, so a key says nothing about the login."""
    monkeypatch.setattr(keys_module, "_sdk_installed", lambda: True)
    store = HealthStore(tmp_path / "h.json", ttl_s=3600, clock=FakeClock())
    assert not claude_code_verified({"ANTHROPIC_API_KEY": K1}, store)


def test_oauth_token_or_a_cached_success_verifies_the_login(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(keys_module, "_sdk_installed", lambda: True)
    clock = FakeClock()
    store = HealthStore(tmp_path / "h.json", ttl_s=100, clock=clock)
    assert claude_code_verified({"CLAUDE_CODE_OAUTH_TOKEN": "tok-abcdef123456"}, store)
    assert not claude_code_verified({"CLAUDE_CODE_OAUTH_TOKEN": "changeme"}, store)  # placeholder
    assert not claude_code_verified({}, store)
    store.mark_healthy(CLAUDE_CODE, LOGIN.fingerprint)
    assert claude_code_verified({}, store)
    clock.advance(101)  # the proof expires: a login can be revoked any time
    assert not claude_code_verified({}, store)


def test_a_dead_login_is_not_verified_even_after_an_earlier_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(keys_module, "_sdk_installed", lambda: True)
    store = HealthStore(tmp_path / "h.json", ttl_s=3600, clock=FakeClock())
    store.mark_healthy(CLAUDE_CODE, LOGIN.fingerprint)
    store.mark_dead(CLAUDE_CODE, LOGIN.fingerprint, "auth: Not logged in")
    assert not claude_code_verified({}, store)


def test_verification_needs_the_sdk(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(keys_module, "_sdk_installed", lambda: False)
    store = HealthStore(tmp_path / "h.json", ttl_s=3600, clock=FakeClock())
    store.mark_healthy(CLAUDE_CODE, LOGIN.fingerprint)
    assert not claude_code_verified({"CLAUDE_CODE_OAUTH_TOKEN": "tok-abcdef123456"}, store)


def test_a_successful_login_call_is_recorded_once_per_pool(tmp_path: Path) -> None:
    clock = FakeClock()
    store = HealthStore(tmp_path / "h.json", ttl_s=3600, clock=clock)
    pool = KeyPool(CLAUDE_CODE, [LOGIN], store=store, clock=clock)
    assert not store.is_healthy(CLAUDE_CODE, LOGIN.fingerprint)
    pool.mark_success(LOGIN)
    assert store.is_healthy(CLAUDE_CODE, LOGIN.fingerprint)
    stamp = store.healthy_entries()[(CLAUDE_CODE, LOGIN.fingerprint)].marked_at
    clock.advance(30)
    pool.mark_success(LOGIN)  # later calls do not rewrite the file
    assert store.healthy_entries()[(CLAUDE_CODE, LOGIN.fingerprint)].marked_at == stamp
    pool.mark_dead(LOGIN, "auth: Not logged in")
    assert not store.is_healthy(CLAUDE_CODE, LOGIN.fingerprint)


def test_real_api_keys_are_not_recorded_as_healthy(tmp_path: Path) -> None:
    store = HealthStore(tmp_path / "h.json", ttl_s=3600, clock=FakeClock())
    key = ApiKey("anthropic", 1, K1)
    KeyPool("anthropic", [key], store=store).mark_success(key)
    assert store.healthy_entries() == {} and not (tmp_path / "h.json").exists()
