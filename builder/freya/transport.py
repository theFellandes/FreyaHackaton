"""Deadline-bounded HTTP for the kit client (audit P-04, P-03).

The organisers' ``freya.py`` sends every request with ``urlopen(..., timeout=120)``. That number knows nothing about the
build: a request started a few seconds before the wall-clock guard fires may still hold its thread for two minutes, and
nothing stops a retry or a poll from starting when the 30 minute limit is already behind us. The kit file must not be
edited, but its helpers (``workspace_id``, ``validate_workflow``, ``export_workflow``, ``get_call`` ...) all look ``api`` up
in the module namespace when they run - the same property :func:`builder.freya.ratelimit.throttle_kit` relies on. So the
builder installs *its own* ``api`` in the loaded kit instance (:func:`install_bounded_http`):

* same contract as the kit's: URL and headers from the kit module, ``{success, data: {message, result}}`` envelope
  unwrapped, an HTTP error status raised as ``RuntimeError("<METHOD> <path> -> <code>: <body>")`` (the text
  :class:`builder.freya.client.FreyaClient` parses into status and body);
* ``timeout = min(120, time left)`` where the time left comes from the build's cancel token or an explicit deadline
  (:func:`builder.freya.background.time_left`); with (almost) no time left the request is not started at all
  (:class:`~builder.freya.background.PlatformCancelled`), so no request outlives the deadline by more than its timeout;
* a dropped connection or a half-read body surfaces as an ``OSError``, never as an ``http.client`` exception, so the
  client classifies it as a retryable network error.
"""

from __future__ import annotations

import functools
import http.client
import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any

from builder.freya.background import PlatformCancelled, time_left

__all__ = [
    "MAX_TIMEOUT_S",
    "MIN_TIMEOUT_S",
    "install_bounded_http",
    "make_bounded_api",
    "request_timeout",
]

#: The kit's own per-request timeout; the upper bound of every request.
MAX_TIMEOUT_S = 120.0
#: A request is not worth starting with less time than this before the deadline.
MIN_TIMEOUT_S = 0.5


def request_timeout() -> float:
    """``min(120, time left before the bound deadline)``, or 120 when nothing limits the running thread.

    Raises:
        PlatformCancelled: Less than :data:`MIN_TIMEOUT_S` is left (or the build was cancelled).
    """
    left = time_left()
    if left is None:
        return MAX_TIMEOUT_S
    if left < MIN_TIMEOUT_S:
        raise PlatformCancelled("platform call not started: the build deadline has passed")
    return min(MAX_TIMEOUT_S, left)


def make_bounded_api(kit: Any) -> Callable[..., Any]:
    """Return an ``api(method, path, body=None, **params)`` function with the kit's contract and a bounded timeout.

    ``kit.URL`` and ``kit.HEADERS`` are read at call time, so a reloaded or reconfigured kit is honoured.
    """

    def api(method: str, path: str, body: Any = None, **params: Any) -> Any:
        timeout = request_timeout()
        query = "?" + urllib.parse.urlencode(params) if params else ""
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            kit.URL + path + query, data=data, headers=dict(kit.HEADERS), method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                out = json.load(response)
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", errors="replace")[:1500]
            raise RuntimeError(f"{method} {path} -> {exc.code}: {text}") from None
        except http.client.HTTPException as exc:  # IncompleteRead, BadStatusLine ... are network trouble
            raise ConnectionError(f"{method} {path}: {type(exc).__name__}: {exc}") from exc
        if not isinstance(out, dict):
            return out
        envelope = out.get("data")
        return envelope.get("result", out) if isinstance(envelope, Mapping) else out

    api._freya_bounded = True  # type: ignore[attr-defined]
    return api


def install_bounded_http(kit: Any) -> bool:
    """Make ``kit.api`` (and so every kit helper) use the bounded transport (idempotent).

    A stand-in without ``URL`` / ``HEADERS`` (a test double that brings its own ``api``) is left alone.

    Returns:
        ``True`` when the kit was patched now, ``False`` when it already was or cannot be.
    """
    current = getattr(kit, "api", None)
    if not callable(current) or getattr(current, "_freya_bounded", False):
        return False
    if not isinstance(getattr(kit, "URL", None), str) or not isinstance(getattr(kit, "HEADERS", None), Mapping):
        return False
    bounded = make_bounded_api(kit)
    functools.update_wrapper(bounded, current, assigned=("__doc__",), updated=())
    kit.api = bounded
    return True
