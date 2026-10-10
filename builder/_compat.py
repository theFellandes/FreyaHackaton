"""Environment compatibility shims. This module imports only the standard library.

``ensure_uuid_utils()`` works around machines where the native extension of ``uuid_utils`` cannot be loaded.
Some Windows Application Control (WDAC / Smart App Control) policies block unsigned ``.pyd`` files, so
``import uuid_utils`` fails with ``ImportError: DLL load failed while importing _uuid_utils``. LangChain and
LangSmith import ``uuid_utils.compat.uuid7`` at import time, so that one blocked file would break every
LangChain / LangGraph import. The shim below supplies a pure-Python UUIDv7 (RFC 9562) in its place.

``builder/__init__.py`` calls ``ensure_uuid_utils()`` before anything else, so importing any ``builder`` module
(and therefore the whole builder, its tests and the Streamlit app) is enough.
"""

from __future__ import annotations

import importlib
import logging
import secrets
import sys
import threading
import time
import types
import uuid

logger = logging.getLogger(__name__)

# Marker attribute on the shim modules, so a second call (or a test) can tell the shim from the native package.
SHIM_MARKER = "__builder_shim__"

_MS_MASK = (1 << 48) - 1
_COUNTER_BITS = 42  # RFC 9562 section 6.2, method 1: a counter whose most significant bit starts at 0.


class _UuidV7Generator:
    """RFC 9562 UUIDv7: 48-bit Unix milliseconds, version 7, variant 10, a 42-bit counter and 32 random bits.

    Values generated from the clock are strictly increasing within the process, also within one millisecond and
    when the clock steps backwards. A value generated for an explicit timestamp does not touch that state.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_ms = -1
        self._counter = 0

    @staticmethod
    def _random_counter() -> int:
        return secrets.randbits(_COUNTER_BITS - 1)  # most significant bit stays 0, leaving room to increment

    @staticmethod
    def _build(milliseconds: int, counter: int) -> uuid.UUID:
        counter_hi = (counter >> 30) & 0xFFF
        counter_lo = counter & 0x3FFFFFFF
        value = (
            ((milliseconds & _MS_MASK) << 80)
            | (0x7 << 76)
            | (counter_hi << 64)
            | (0b10 << 62)
            | (counter_lo << 32)
            | secrets.randbits(32)
        )
        return uuid.UUID(int=value)

    def generate(self, timestamp: int | float | None = None, nanos: int | None = None) -> uuid.UUID:
        if timestamp is not None or nanos is not None:
            if nanos is not None and (isinstance(nanos, bool) or not isinstance(nanos, int) or nanos < 0):
                raise ValueError("nanos must be a non-negative integer")
            seconds = 0 if timestamp is None else timestamp
            if isinstance(seconds, bool) or not isinstance(seconds, int | float) or seconds < 0:
                raise ValueError("timestamp must be a non-negative number of seconds")
            milliseconds = int(seconds) * 1000 + (nanos or 0) // 1_000_000
            return self._build(milliseconds, self._random_counter())
        now_ms = time.time_ns() // 1_000_000
        with self._lock:
            if now_ms > self._last_ms:
                self._last_ms = now_ms
                self._counter = self._random_counter()
            else:  # same millisecond, or the clock went backwards: keep ordering by counting up
                self._counter += 1
                if self._counter >> _COUNTER_BITS:
                    self._last_ms += 1
                    self._counter = self._random_counter()
            return self._build(self._last_ms, self._counter)


_generator = _UuidV7Generator()


def uuid7(timestamp: int | float | None = None, nanos: int | None = None) -> uuid.UUID:
    """Same signature as ``uuid_utils.compat.uuid7``: ``timestamp`` is Unix seconds, ``nanos`` the remainder."""
    return _generator.generate(timestamp, nanos)


def _build_shim_modules() -> tuple[types.ModuleType, types.ModuleType]:
    """Create (``uuid_utils``, ``uuid_utils.compat``) stand-ins without registering them anywhere."""
    package = types.ModuleType("uuid_utils")
    compat = types.ModuleType("uuid_utils.compat")
    compat.__package__ = "uuid_utils"
    package.__path__ = []  # marks it as a package so that "uuid_utils.compat" resolves from sys.modules
    package.__package__ = "uuid_utils"
    for module in (package, compat):
        setattr(module, SHIM_MARKER, True)
        module.__version__ = "0+builder-shim"
        module.UUID = uuid.UUID
        module.NAMESPACE_DNS = uuid.NAMESPACE_DNS
        module.NAMESPACE_OID = uuid.NAMESPACE_OID
        module.NAMESPACE_URL = uuid.NAMESPACE_URL
        module.NAMESPACE_X500 = uuid.NAMESPACE_X500
        module.NIL = uuid.UUID(int=0)
        module.MAX = uuid.UUID(int=(1 << 128) - 1)
        module.getnode = uuid.getnode
        module.uuid1 = uuid.uuid1
        module.uuid3 = uuid.uuid3
        module.uuid4 = uuid.uuid4
        module.uuid5 = uuid.uuid5
        module.uuid7 = uuid7
    package.compat = compat
    return package, compat


def uuid_utils_is_shimmed() -> bool:
    """True if ``uuid_utils`` currently resolves to the pure-Python shim."""
    return bool(getattr(sys.modules.get("uuid_utils"), SHIM_MARKER, False))


def ensure_uuid_utils() -> bool:
    """Make ``import uuid_utils`` and ``uuid_utils.compat.uuid7`` work, returning True if the shim is in use.

    The native package is preferred. Only when it cannot be imported (``ImportError`` or ``OSError``, which
    covers "DLL load failed ... Application Control policy has blocked this file", and a missing package) is
    the shim installed in ``sys.modules`` and one warning logged. Calling it again is harmless.
    """
    if uuid_utils_is_shimmed():
        return True
    try:
        importlib.import_module("uuid_utils.compat")  # imports the package and its native extension too
    except (ImportError, OSError) as exc:
        for name in [name for name in sys.modules if name == "uuid_utils" or name.startswith("uuid_utils.")]:
            del sys.modules[name]
        package, compat = _build_shim_modules()
        sys.modules["uuid_utils"] = package
        sys.modules["uuid_utils.compat"] = compat
        logger.warning(
            "uuid_utils could not be loaded (%s: %s); using a pure-Python UUIDv7 fallback.",
            type(exc).__name__,
            str(exc).splitlines()[0] if str(exc) else "no details",
        )
        return True
    return False
