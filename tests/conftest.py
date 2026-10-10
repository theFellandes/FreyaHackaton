"""Shared pytest fixtures and safety nets for the whole test suite.

Safety nets (autouse, skipped for tests marked ``@pytest.mark.live``):

* **Hermetic configuration** - :class:`builder.settings.Settings` and :func:`builder.settings.effective_env` ignore
  the repository's ``.env`` file (``builder.paths.ENV_FILE`` points to a file that does not exist), every
  variable that maps to a setting (``FREYA_API_KEY``, ``DRY_RUN``, ``LLM_*`` ...) is removed from the environment, and
  so are LLM credentials (``ANTHROPIC_API_KEY*``, ``OPENAI_API_KEY*``, ``CLAUDE_CODE_OAUTH_TOKEN``). The settings
  cache is cleared before and after each test. A test that needs a value sets it with ``monkeypatch.setenv`` or
  builds settings with the ``make_settings`` fixture. Key discovery (``builder.llm.keys.default_env``) reads the
  same ``builder.paths.ENV_FILE``, so it cannot see the developer's ``.env`` either.
* **Optional packages** - ``claude_agent_sdk`` (``agentic``), ``pypdf`` (``ingest``), ``streamlit`` (``ui``) and
  ``langfuse`` (``obs``) are extras. Tests that need one skip themselves or stub its availability (the
  ``claude_sdk_installed`` / ``claude_sdk_missing`` fixtures); ``tests/unit/test_optional_extras.py`` proves it by
  running the affected modules with the package hidden (``tests/hide_optional_packages.py``).
* **No network** - connecting a socket to anything but a loopback address fails the test (best effort: it covers
  ``socket``-based clients such as ``urllib``, ``http.client`` and sync ``httpx``).

Fixtures for everyone:

* paths of the kit: ``repo_root``, ``kit_openapi_path``, ``brief_a_path``, ``brief_b_path``, ``kit_brief_paths``,
  ``public_test_set_path``;
* parsed kit data (fresh copies per test): ``kit_tool_api``, ``public_cases``;
* ``null_trace`` - an in-memory :class:`builder.trace.NullTrace`;
* ``tmp_dirs`` - temporary ``out/``, ``traces/``, ``results/``, ``data/`` with ``builder.paths`` pointed at them;
* ``make_settings`` / ``settings`` - :class:`Settings` built without any ``.env`` file;
* ``fake_clock`` - a manually advanced monotonic clock;
* ``claude_sdk_installed`` / ``claude_sdk_missing`` - pretend the optional ``claude_agent_sdk`` is (not) installed.

The kit files are read-only reference material: fixtures only read them, tests copy them before changing anything.
"""

from __future__ import annotations

import copy
import json
import os
import re
import socket
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from builder import paths
from builder.ingest.tool_api import ToolApi, load_tool_api
from builder.llm import keys as llm_keys
from builder.settings import Settings, get_settings
from builder.trace import NullTrace

_LLM_CREDENTIAL_RE = re.compile(r"^(ANTHROPIC|OPENAI)_API_KEY(S|_\d+)?$", re.IGNORECASE)
_EXTRA_CREDENTIALS = ("CLAUDE_CODE_OAUTH_TOKEN", "FREYA_KEY")


def _is_live(request: pytest.FixtureRequest) -> bool:
    return request.node.get_closest_marker("live") is not None


def _setting_env_names() -> set[str]:
    """Upper-cased environment names of every Settings field, aliases included."""
    names: set[str] = set()
    for field_name, info in Settings.model_fields.items():
        names.add(field_name.upper())
        alias = info.validation_alias
        choices = getattr(alias, "choices", None)
        if isinstance(alias, str):
            names.add(alias.upper())
        elif choices:
            names.update(str(choice).upper() for choice in choices if isinstance(choice, str))
    return names


@pytest.fixture(autouse=True)
def _hermetic_settings(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Keep unit tests independent of the developer's ``.env`` and environment (see the module docstring)."""
    get_settings.cache_clear()
    if not _is_live(request):
        monkeypatch.setitem(Settings.model_config, "env_file", None)
        monkeypatch.setattr(
            paths, "ENV_FILE", paths.REPO_ROOT / ".env.disabled-in-unit-tests"
        )  # never created
        doomed = _setting_env_names() | set(_EXTRA_CREDENTIALS)
        for name in list(os.environ):
            if name.upper() in doomed or _LLM_CREDENTIAL_RE.match(name):
                monkeypatch.delenv(name, raising=False)
    yield
    get_settings.cache_clear()


def _is_loopback(address: Any) -> bool:
    if not isinstance(address, tuple) or not address:
        return True  # AF_UNIX paths and exotic families are local
    host = address[0].decode() if isinstance(address[0], bytes) else str(address[0])
    return (
        host in {"localhost", "::1", "0.0.0.0", ""} or host.startswith("127.") or host.endswith(".localhost")
    )


@pytest.fixture(autouse=True)
def _no_network(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[list[Any]]:
    """Fail any non-live test that connects a socket to a non-loopback address."""
    attempts: list[Any] = []
    if _is_live(request):
        yield attempts
        return
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def guarded_connect(self: socket.socket, address: Any) -> None:
        if not _is_loopback(address):
            attempts.append(address)
            raise ConnectionRefusedError(f"network access is blocked in unit tests (connect to {address!r})")
        original_connect(self, address)

    def guarded_connect_ex(self: socket.socket, address: Any) -> int:
        if not _is_loopback(address):
            attempts.append(address)
            raise ConnectionRefusedError(f"network access is blocked in unit tests (connect to {address!r})")
        return original_connect_ex(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    yield attempts
    if attempts:
        pytest.fail(
            f"test tried to open network connections: {attempts!r} (mark it @pytest.mark.live if intended)"
        )


# --- kit paths ---------------------------------------------------------------------------------------------------------


@pytest.fixture(scope="session")
def repo_root() -> Path:
    """The repository root."""
    return paths.REPO_ROOT


@pytest.fixture(scope="session")
def kit_openapi_path() -> Path:
    """The kit's tool API file ``tools/openapi.json`` (read-only)."""
    return paths.KIT_TOOLS_DIR / "openapi.json"


@pytest.fixture(scope="session")
def brief_a_path() -> Path:
    """The kit's ``briefs/brief-a.md`` (read-only)."""
    return paths.REPO_ROOT / "briefs" / "brief-a.md"


@pytest.fixture(scope="session")
def brief_b_path() -> Path:
    """The kit's ``briefs/brief-b.md`` (read-only)."""
    return paths.REPO_ROOT / "briefs" / "brief-b.md"


@pytest.fixture(scope="session")
def kit_brief_paths(brief_a_path: Path, brief_b_path: Path) -> list[Path]:
    """Both kit briefs, A first (Brief B builds on A)."""
    return [brief_a_path, brief_b_path]


@pytest.fixture(scope="session")
def public_test_set_path() -> Path:
    """The kit's ``public-test-set.json`` (read-only)."""
    return paths.REPO_ROOT / "public-test-set.json"


# --- parsed kit data ---------------------------------------------------------------------------------------------------


@pytest.fixture(scope="session")
def _kit_tool_api_cached(kit_openapi_path: Path) -> ToolApi:
    return load_tool_api(kit_openapi_path)


@pytest.fixture
def kit_tool_api(_kit_tool_api_cached: ToolApi) -> ToolApi:
    """The kit's tool API parsed into a :class:`ToolApi` (a fresh deep copy per test)."""
    return _kit_tool_api_cached.model_copy(deep=True)


@pytest.fixture(scope="session")
def _public_cases_cached(public_test_set_path: Path) -> list[dict[str, Any]]:
    return json.loads(public_test_set_path.read_text(encoding="utf-8"))


@pytest.fixture
def public_cases(_public_cases_cached: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The practice cases of ``public-test-set.json`` (a fresh deep copy per test)."""
    return copy.deepcopy(_public_cases_cached)


# --- runtime helpers ---------------------------------------------------------------------------------------------------


@pytest.fixture
def null_trace() -> NullTrace:
    """An in-memory trace; inspect ``.events`` / ``.lines`` / ``.of_kind(kind)``."""
    return NullTrace(run_id="test-run", slot="test")


@pytest.fixture
def tmp_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Temporary output directories with ``builder.paths`` (OUT/TRACES/RESULTS/DATA_DIR) redirected to them."""
    dirs = SimpleNamespace(
        root=tmp_path,
        out=tmp_path / "out",
        traces=tmp_path / "traces",
        results=tmp_path / "results",
        data=tmp_path / "data",
    )
    for attribute, directory in (
        ("OUT_DIR", dirs.out),
        ("TRACES_DIR", dirs.traces),
        ("RESULTS_DIR", dirs.results),
        ("DATA_DIR", dirs.data),
    ):
        directory.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(paths, attribute, directory)
    return dirs


@pytest.fixture
def claude_sdk_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend the optional ``claude_agent_sdk`` package is installed (it belongs to the ``agentic`` extra).

    ``builder.llm.keys._sdk_installed`` is the single source of truth: ``claude_code_available``,
    ``claude_code_verified`` and ``builder.agentic.runner.sdk_installed`` all ask it. A test that expects the
    Claude Code login or the agentic track to be usable must use this fixture instead of relying on the real
    install, so the suite passes on a machine without the extra.
    """
    monkeypatch.setattr(llm_keys, "_sdk_installed", lambda: True)


@pytest.fixture
def claude_sdk_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend ``claude_agent_sdk`` is not installed (the counterpart of :func:`claude_sdk_installed`)."""
    monkeypatch.setattr(llm_keys, "_sdk_installed", lambda: False)


@pytest.fixture
def make_settings() -> Callable[..., Settings]:
    """Factory for :class:`Settings` that never reads a ``.env`` file: ``make_settings(dry_run=True)``."""

    def factory(**overrides: Any) -> Settings:
        return Settings(_env_file=None, **overrides)

    return factory


@pytest.fixture
def settings(make_settings: Callable[..., Settings]) -> Settings:
    """Default settings (environment cleaned by the hermetic fixture, no ``.env``)."""
    return make_settings()


class FakeClock:
    """A monotonic clock that only moves when told to: ``clock()`` reads it, ``clock.advance(s)`` moves it."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def fake_clock() -> FakeClock:
    """A :class:`FakeClock` starting at 1000.0 s."""
    return FakeClock()
