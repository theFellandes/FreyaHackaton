"""Tests for builder.freya.transport: the deadline-bounded HTTP the loaded kit uses (audit P-04, P-03).

The kit's ``api`` has a fixed 120 s timeout and knows nothing about the build's deadline, so a request started shortly
before the wall-clock guard could outlive it by two minutes. The transport re-implements the kit's ``api`` contract
(same URL, headers, envelope and error text) with ``timeout = min(120, time left)``; the kit file itself is untouched.
A tiny HTTP server on the loopback interface stands in for the platform, so the real ``urllib`` path is exercised.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import threading
import time
import urllib.parse
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from builder.freya.background import CancelToken, PlatformCancelled, bound_deadline, bound_token
from builder.freya.client import FreyaClient, FreyaError
from builder.freya.ratelimit import PoliteLimiter, throttle_kit
from builder.freya.transport import MAX_TIMEOUT_S, install_bounded_http, make_bounded_api, request_timeout
from builder.trace import NullTrace

REPO_ROOT = Path(__file__).resolve().parents[2]


class Stub:
    """A scripted loopback server: ``replies`` maps ``(method, path)`` to a callable returning ``(status, body)``."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.replies: dict[tuple[str, str], Callable[[], tuple[int, bytes]]] = {}
        self.block_s = 0.0
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def _serve(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                stub.requests.append(
                    {
                        "method": self.command,
                        "target": self.path,
                        "headers": dict(self.headers),
                        "body": json.loads(raw) if raw else None,
                    }
                )
                if stub.block_s:
                    time.sleep(stub.block_s)
                route = self.path.split("?", 1)[0]
                status, body = stub.replies.get((self.command, route), lambda: (404, b"nope"))()
                if status == -1:  # announce more bytes than are sent, then hang up
                    self.send_response(200)
                    self.send_header("Content-Length", "500")
                    self.end_headers()
                    self.wfile.write(b'{"data":')
                    self.wfile.flush()
                    self.close_connection = True
                    return
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                with contextlib.suppress(OSError):  # the client gave up (a timeout test)
                    self.wfile.write(body)

            do_GET = do_POST = do_PUT = _serve

            def log_message(self, *args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def stub() -> Iterator[Stub]:
    server = Stub()
    try:
        yield server
    finally:
        server.close()


def kit_for(stub: Stub) -> SimpleNamespace:
    return SimpleNamespace(
        URL=stub.url, HEADERS={"Authorization": "Bearer fak_unit", "User-Agent": "Mozilla/5.0 (unit)"}
    )


def envelope(result: Any) -> bytes:
    return json.dumps({"success": True, "data": {"message": "Success", "result": result}}).encode()


# ---------------------------------------------------------------------------------------------------- the contract


def test_it_speaks_the_kits_protocol(stub: Stub) -> None:
    stub.replies[("POST", "/api/v2/workflow")] = lambda: (200, envelope({"workflow": {"id": "wf_1"}}))
    api = make_bounded_api(kit_for(stub))

    result = api("POST", "/api/v2/workflow", {"name": "n", "workflow": {"a": 1}}, dryRun="true", x="é")

    assert result == {"workflow": {"id": "wf_1"}}  # the envelope's data.result
    sent = stub.requests[0]
    assert sent["method"] == "POST"
    assert sent["target"] == "/api/v2/workflow?dryRun=true&x=%C3%A9"
    assert sent["body"] == {"name": "n", "workflow": {"a": 1}}
    assert sent["headers"]["Authorization"] == "Bearer fak_unit"
    assert sent["headers"]["User-Agent"] == "Mozilla/5.0 (unit)"


def test_a_get_sends_no_body_and_a_bare_payload_is_returned_as_is(stub: Stub) -> None:
    stub.replies[("GET", "/api/v2/call/c1")] = lambda: (200, json.dumps({"call": {"id": "c1"}}).encode())
    api = make_bounded_api(kit_for(stub))
    assert api("GET", "/api/v2/call/c1") == {"call": {"id": "c1"}}  # no envelope: the payload itself
    assert stub.requests[0]["body"] is None


def test_an_http_error_has_the_kits_message_so_the_client_can_parse_it(stub: Stub) -> None:
    stub.replies[("PUT", "/api/v2/workflow/wf_1")] = lambda: (503, b"upstream unavailable")
    api = make_bounded_api(kit_for(stub))
    with pytest.raises(RuntimeError) as caught:
        api("PUT", "/api/v2/workflow/wf_1", {"workflow": {}})
    assert str(caught.value) == "PUT /api/v2/workflow/wf_1 -> 503: upstream unavailable"


def test_the_client_maps_the_transport_errors_like_any_kit_error(stub: Stub) -> None:
    stub.replies[("GET", "/api/v2/workspaces")] = lambda: (502, b"bad gateway")
    kit = kit_for(stub)
    kit.api = make_bounded_api(kit)
    kit.workspace_id = lambda: kit.api("GET", "/api/v2/workspaces")["workspaces"][0]["id"]
    client = FreyaClient(NullTrace(), kit=kit, limiter=False, sleep=lambda s: None, read_retries=0)
    with pytest.raises(FreyaError) as caught:
        client.workspace_id()
    assert (caught.value.status, caught.value.kind, caught.value.retryable) == (502, "http", True)


def test_a_dropped_connection_is_a_network_error_not_an_http_library_exception(stub: Stub) -> None:
    stub.replies[("GET", "/api/v2/x")] = lambda: (-1, b"")  # announces 500 bytes, sends 8
    api = make_bounded_api(kit_for(stub))
    with pytest.raises(OSError):  # http.client.IncompleteRead is not an OSError by itself
        api("GET", "/api/v2/x")


def test_a_refused_connection_is_an_oserror() -> None:
    api = make_bounded_api(SimpleNamespace(URL="http://127.0.0.1:9", HEADERS={}))
    with pytest.raises(OSError):
        api("GET", "/api/v2/x")


# ---------------------------------------------------------------------------------------------------- the deadline


def test_the_timeout_is_the_kits_120_seconds_when_nothing_limits_the_call() -> None:
    assert MAX_TIMEOUT_S == 120.0
    assert request_timeout() == 120.0


def test_the_timeout_shrinks_to_the_time_left() -> None:
    now = {"t": 50.0}
    token = CancelToken(deadline=80.0, clock=lambda: now["t"])
    with bound_token(token):
        assert request_timeout() == 30.0
        now["t"] = 79.0
        assert request_timeout() == 1.0
    with bound_token(CancelToken(deadline=time.monotonic() + 10_000.0)):
        assert request_timeout() == 120.0  # far away: the usual cap


def test_no_request_starts_when_the_deadline_has_passed(stub: Stub) -> None:
    api = make_bounded_api(kit_for(stub))
    token = CancelToken(deadline=time.monotonic() - 1.0)
    with bound_token(token), pytest.raises(PlatformCancelled):
        api("GET", "/api/v2/workspaces")
    with bound_deadline(time.monotonic() + 0.1), pytest.raises(PlatformCancelled):
        api("GET", "/api/v2/workspaces")  # less than the smallest useful timeout is left
    assert stub.requests == []


def test_a_request_in_flight_ends_at_the_deadline_not_two_minutes_later(stub: Stub) -> None:
    stub.block_s = 5.0  # the platform hangs
    stub.replies[("GET", "/api/v2/workspaces")] = lambda: (200, envelope({}))
    api = make_bounded_api(kit_for(stub))
    started = time.monotonic()
    with bound_deadline(started + 1.0), pytest.raises(OSError):  # TimeoutError / URLError
        api("GET", "/api/v2/workspaces")
    assert time.monotonic() - started < 3.0


# ---------------------------------------------------------------------------------------------------- installation


def test_installation_replaces_the_kits_api_once() -> None:
    kit = SimpleNamespace(URL="http://127.0.0.1:9", HEADERS={}, api=lambda *a, **k: "original")
    original = kit.api
    assert install_bounded_http(kit) is True
    assert kit.api is not original and getattr(kit.api, "_freya_bounded", False)
    installed = kit.api
    assert install_bounded_http(kit) is False and kit.api is installed  # idempotent


def test_a_stand_in_without_url_and_headers_keeps_its_own_api() -> None:
    kit = SimpleNamespace(api=lambda *a, **k: "fake platform")
    original = kit.api
    assert install_bounded_http(kit) is False
    assert kit.api is original
    assert install_bounded_http(SimpleNamespace()) is False  # no api at all


def test_it_composes_with_the_polite_limiter(stub: Stub) -> None:
    stub.replies[("GET", "/api/v2/workspaces")] = lambda: (200, envelope({"workspaces": [{"id": "ws"}]}))
    kit = kit_for(stub)
    kit.api = lambda *a, **k: "original"
    assert install_bounded_http(kit) is True
    limiter = PoliteLimiter(0.0)
    assert throttle_kit(kit, limiter) is True  # the pacing wrapper goes on top of the bounded transport
    assert kit.api("GET", "/api/v2/workspaces") == {"workspaces": [{"id": "ws"}]}
    assert limiter.calls == 1 and len(stub.requests) == 1
    assert install_bounded_http(kit) is False  # already bounded underneath


def test_the_real_kit_talks_to_the_stub_through_the_bounded_transport(
    stub: Stub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The kit's own helpers (workspace_id, get_call ...) get the bounded transport because they look ``api`` up late."""
    monkeypatch.setenv("FREYA_API_URL", stub.url)
    monkeypatch.setenv("FREYA_API_KEY", "fak_unit_test_placeholder")
    monkeypatch.delenv("FREYA_WORKSPACE_ID", raising=False)
    spec = importlib.util.spec_from_file_location("freya_kit_transport_test", REPO_ROOT / "freya.py")
    assert spec is not None and spec.loader is not None
    kit = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(kit)
    stub.replies[("GET", "/api/v2/workspaces")] = lambda: (200, envelope({"workspaces": [{"id": "ws_9"}]}))
    stub.replies[("GET", "/api/v2/call/c7")] = lambda: (200, envelope({"call": {"id": "c7"}}))

    assert install_bounded_http(kit) is True
    assert kit.workspace_id() == "ws_9"
    assert kit.get_call("c7") == {"id": "c7"}
    assert stub.requests[0]["headers"]["Authorization"] == "Bearer fak_unit_test_placeholder"
    stub.block_s = 5.0
    started = time.monotonic()
    with bound_deadline(started + 1.0), pytest.raises(OSError):
        kit.get_call("c7")
    assert time.monotonic() - started < 3.0


# ---------------------------------------------------------------------------------------------------- the whole stack


class PlatformServer:
    """The in-memory :class:`FakePlatform` behind a loopback HTTP server with the platform's envelope."""

    def __init__(self, platform: Any) -> None:
        self.platform = platform
        self.requests: list[tuple[str, str]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _serve(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length)) if length else None
                route, _, query = self.path.partition("?")
                outer.requests.append((self.command, route))
                params = dict(urllib.parse.parse_qsl(query))
                try:
                    result = outer.platform.api(self.command, route, body, **params)
                    status = 200
                    payload = json.dumps({"success": True, "data": {"message": "Success", "result": result}}).encode()
                except RuntimeError as exc:  # the fake raises the kit's "<METHOD> <path> -> <code>: <body>"
                    status = int(str(exc).split(" -> ")[1].split(":")[0])
                    payload = str(exc).split(": ", 1)[1].encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = do_POST = do_PUT = _serve

            def log_message(self, *args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def test_a_complete_deploy_runs_through_the_real_kit_and_the_bounded_transport(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``FreyaClient`` with no injected kit: the real ``freya.py``, the bounded ``api`` and an HTTP server in between.

    Create (with the editor layout), the kit-style update with the model re-save, publication, read-back and export
    all work over real HTTP; an update of the same slot reuses the workflow.
    """
    import sys

    import builder.freya.kit as kit_mod
    from builder.settings import get_settings
    from tests.unit.test_freya_client import EVENT_LLM, FakePlatform, sample_workflow

    platform = FakePlatform()
    server = PlatformServer(platform)
    try:
        monkeypatch.setenv("FREYA_API_URL", server.url)
        monkeypatch.setenv("FREYA_API_KEY", "fak_unit_test_placeholder")
        monkeypatch.delenv("FREYA_AGENT_PROVIDER", raising=False)
        monkeypatch.delenv("FREYA_AGENT_MODEL", raising=False)
        get_settings.cache_clear()
        monkeypatch.setattr(kit_mod, "_KIT", None)
        monkeypatch.setattr(sys, "path", list(sys.path))
        original = sys.modules.pop("freya", None)
        try:
            client = FreyaClient(NullTrace(), state_file=tmp_path / "agents.json", limiter=False)
            assert server.url == client.kit.URL and client.kit.AGENT_LLM == EVENT_LLM
            assert getattr(client.kit.api, "_freya_bounded", False) is True

            first = client.save("slot", sample_workflow())
            assert first.created and first.published and first.workflow_id == "wf_1"
            record = platform.workflows["wf_1"]
            assert record["workflow"]["agent_config"]["llmConfig"]["model"] == "experimental-5"
            assert set(record["nodePositions"]) == {"setup", "talk", "end", "help"}
            changed = sample_workflow()
            changed["system_prompt"] = "Second build."
            second = client.save("slot", changed)
            assert not second.created and second.published and second.version > first.version
            assert record["workflow"]["system_prompt"] == "Second build."
            target = client.export(first.workflow_id, tmp_path / "out" / "slot.json")
            assert json.loads(target.read_text(encoding="utf-8"))["workflow"]["system_prompt"] == "Second build."
            assert client.get_workflow(first.workflow_id)["system_prompt"] == "Second build."
            assert ("POST", "/api/v2/workflow") in server.requests and len(platform.workflows) == 1
        finally:
            sys.modules.pop("freya", None)
            if original is not None:
                sys.modules["freya"] = original
    finally:
        server.close()
        get_settings.cache_clear()
