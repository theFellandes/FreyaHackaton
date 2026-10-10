"""The uuid_utils fallback for machines whose Application Control policy blocks the native extension.

Nothing here needs a blocked machine: the block is simulated with an import hook that raises the same
``ImportError`` Windows produces ("DLL load failed while importing _uuid_utils ...").
"""

from __future__ import annotations

import importlib.abc
import logging
import os
import subprocess
import sys
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from builder import _compat
from builder._compat import ensure_uuid_utils, uuid_utils_is_shimmed

REPO_ROOT = Path(__file__).resolve().parents[2]
BLOCK_MESSAGE = (
    "DLL load failed while importing _uuid_utils: An Application Control policy has blocked this file."
)


def _is_uuid_utils(name: str) -> bool:
    return name == "uuid_utils" or name.startswith("uuid_utils.")


class _Blocker(importlib.abc.MetaPathFinder):
    """Raise ``error`` when ``target`` is imported, like a loader that Windows refuses to load."""

    def __init__(self, target: str, error: Exception) -> None:
        self.target = target
        self.error = error

    def find_spec(self, fullname: str, path: object = None, target: object = None) -> None:
        if fullname == self.target:
            raise self.error
        return None


@pytest.fixture
def block_uuid_utils() -> Iterator[Callable[[str, Exception], None]]:
    """Yield a function that blocks an import; restores ``sys.modules`` and ``sys.meta_path`` afterwards."""
    saved = {name: module for name, module in sys.modules.items() if _is_uuid_utils(name)}
    for name in saved:
        del sys.modules[name]
    finders: list[_Blocker] = []

    def block(target: str, error: Exception) -> None:
        finder = _Blocker(target, error)
        finders.append(finder)
        sys.meta_path.insert(0, finder)

    try:
        yield block
    finally:
        for finder in finders:
            sys.meta_path.remove(finder)
        for name in [name for name in sys.modules if _is_uuid_utils(name)]:
            del sys.modules[name]
        sys.modules.update(saved)


# ------------------------------------------------------------------------------------------ the UUIDv7


def test_uuid7_for_an_explicit_timestamp_has_the_rfc_9562_layout() -> None:
    value = _compat.uuid7(timestamp=1_700_000_000, nanos=123_456_789)

    assert isinstance(value, uuid.UUID)
    assert value.int >> 80 == 1_700_000_000_123  # 48-bit Unix milliseconds
    assert value.version == 7
    assert value.variant == uuid.RFC_4122
    assert str(value)[14] == "7"
    assert str(value)[19] in "89ab"


@pytest.mark.parametrize(
    ("timestamp", "nanos", "milliseconds"),
    [(0, None, 0), (1, None, 1000), (1, 999_999, 1000), (1, 1_000_000, 1001), (2.5, None, 2000)],
)
def test_uuid7_timestamp_and_nanos_are_converted_to_milliseconds(
    timestamp: float, nanos: int | None, milliseconds: int
) -> None:
    assert _compat.uuid7(timestamp, nanos).int >> 80 == milliseconds


def test_uuid7_orders_by_timestamp() -> None:
    values = [_compat.uuid7(timestamp=seconds) for seconds in (1, 2, 10, 1_700_000_000)]

    assert values == sorted(values)
    assert len(set(values)) == 4


def test_uuid7_from_the_clock_is_unique_and_strictly_increasing() -> None:
    values = [_compat.uuid7() for _ in range(20_000)]

    assert values == sorted(values)
    assert len(set(values)) == len(values)
    assert all(value.version == 7 and value.variant == uuid.RFC_4122 for value in values[:100])


def test_uuid7_from_the_clock_encodes_the_current_time() -> None:
    before = _compat.time.time_ns() // 1_000_000
    milliseconds = _compat.uuid7().int >> 80
    after = _compat.time.time_ns() // 1_000_000

    assert before <= milliseconds <= after + 1


def test_uuid7_is_monotonic_within_a_millisecond_and_when_the_clock_goes_backwards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    readings = iter([1_000, 1_000, 1_000, 999, 1_001])
    monkeypatch.setattr(_compat, "time", SimpleNamespace(time_ns=lambda: next(readings) * 1_000_000))
    generator = _compat._UuidV7Generator()

    values = [generator.generate() for _ in range(5)]

    assert values == sorted(values)
    assert len(set(values)) == 5
    assert [value.int >> 80 for value in values] == [1_000, 1_000, 1_000, 1_000, 1_001]


def test_uuid7_counter_overflow_advances_the_timestamp(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_compat, "time", SimpleNamespace(time_ns=lambda: 5_000 * 1_000_000))
    generator = _compat._UuidV7Generator()
    first = generator.generate()
    generator._counter = (1 << 42) - 1

    second = generator.generate()

    assert (first.int >> 80, second.int >> 80) == (5_000, 5_001)
    assert second > first


@pytest.mark.parametrize(
    ("timestamp", "nanos"), [(-1, None), (True, None), ("1", None), (1, -1), (1, True), (1, 1.5)]
)
def test_uuid7_rejects_invalid_arguments(timestamp: object, nanos: object) -> None:
    with pytest.raises(ValueError):
        _compat.uuid7(timestamp, nanos)  # type: ignore[arg-type]


def test_shim_modules_expose_what_langchain_and_langsmith_import() -> None:
    package, compat = _compat._build_shim_modules()

    assert package.compat is compat
    assert package.__name__ == "uuid_utils"
    assert compat.__name__ == "uuid_utils.compat"
    assert compat.uuid7 is _compat.uuid7
    assert compat.uuid4().version == 4
    assert compat.UUID is uuid.UUID
    assert getattr(package, _compat.SHIM_MARKER) is True


# ------------------------------------------------------------------------- installing the shim in-process


@pytest.mark.parametrize(
    ("target", "error"),
    [
        ("uuid_utils._uuid_utils", ImportError(BLOCK_MESSAGE)),
        ("uuid_utils._uuid_utils", OSError(BLOCK_MESSAGE)),
        ("uuid_utils", ModuleNotFoundError("No module named 'uuid_utils'")),
    ],
    ids=["dll-blocked", "os-error", "not-installed"],
)
def test_a_blocked_or_missing_package_is_replaced_by_the_shim(
    block_uuid_utils: Callable[[str, Exception], None],
    caplog: pytest.LogCaptureFixture,
    target: str,
    error: Exception,
) -> None:
    block_uuid_utils(target, error)
    with pytest.raises((ImportError, OSError)):
        __import__("uuid_utils.compat")  # the failure being worked around really happens

    with caplog.at_level(logging.WARNING, logger="builder._compat"):
        assert ensure_uuid_utils() is True
        assert ensure_uuid_utils() is True  # idempotent

    assert uuid_utils_is_shimmed()
    from uuid_utils.compat import uuid7  # the exact import langchain_core.utils.uuid performs

    assert uuid7().version == 7
    assert uuid7(timestamp=1_700_000_000, nanos=123_456_789).int >> 80 == 1_700_000_000_123
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1  # one warning, not one per call
    assert "pure-Python UUIDv7" in warnings[0].getMessage()


def test_the_native_package_is_preferred_when_it_loads(caplog: pytest.LogCaptureFixture) -> None:
    if uuid_utils_is_shimmed():
        pytest.skip("the native uuid_utils cannot be loaded on this machine")

    with caplog.at_level(logging.WARNING, logger="builder._compat"):
        assert ensure_uuid_utils() is False

    assert not uuid_utils_is_shimmed()
    assert not caplog.records


# ------------------------------------------------------------------- a fresh interpreter, as in real use

_BLOCKER = f"""
import importlib.abc
import sys


class Blocker(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        if name == "uuid_utils._uuid_utils":
            raise ImportError({BLOCK_MESSAGE!r})
        return None


sys.meta_path.insert(0, Blocker())
"""


def _run(code: str) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(filter(None, [str(REPO_ROOT), environment.get("PYTHONPATH")]))
    environment["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, "-c", _BLOCKER + code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
        cwd=REPO_ROOT,
        timeout=180,
        check=False,
    )


def test_control_langchain_really_fails_to_import_when_the_extension_is_blocked() -> None:
    pytest.importorskip("langchain_core")

    result = _run(
        """
try:
    import langchain_core.utils.uuid
except ImportError as exc:
    print("blocked:", exc)
else:
    print("imported")
"""
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "blocked: " + BLOCK_MESSAGE


def test_importing_builder_makes_langchain_work_when_the_extension_is_blocked() -> None:
    pytest.importorskip("langchain_core")

    result = _run(
        """
import builder  # installs the fallback before anything imports LangChain
from langchain_core.utils.uuid import uuid7

value = uuid7()
assert value.version == 7, value
explicit = uuid7(1_700_000_000_123_456_789)
assert explicit.int >> 80 == 1_700_000_000_123, explicit

from pydantic import BaseModel
from langchain_core.utils.function_calling import convert_to_openai_function


class Person(BaseModel):
    name: str


function = convert_to_openai_function(Person)
assert function["name"] == "Person", function

import langsmith._internal._uuid as langsmith_uuid
assert langsmith_uuid.uuid7().version == 7
print("ok")
"""
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
    assert result.stderr.count("pure-Python UUIDv7") == 1  # one warning for the whole process


def test_langchain_anthropic_chat_models_import_when_the_extension_is_blocked() -> None:
    pytest.importorskip("langchain_anthropic")

    result = _run(
        """
import builder
from langchain_anthropic import chat_models

assert hasattr(chat_models, "AnthropicRateLimitError")
print("ok")
"""
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
