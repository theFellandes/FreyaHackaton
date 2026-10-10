"""Session-scoped adapters for the Streamlit playground; network work is always explicit."""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from builder.freya.client import FreyaClient
from builder.settings import Settings, secret_values

logger = logging.getLogger(__name__)


def json_object(text: str) -> dict[str, Any]:
    """Read an editor's JSON and require an object rather than a list or scalar."""
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("Enter a JSON object with named fields.")
    return value


def saved_workflows(path: Path) -> dict[str, str]:
    """Read slot-to-workflow IDs without making a platform request."""
    try:
        data = json_object(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {
        str(slot): str(record["workflow_id"])
        for slot, record in data.items()
        if isinstance(record, dict) and record.get("workflow_id")
    }


def public_error(exc: Exception, settings: Settings) -> str:
    """Keep credentials out of UI errors as well as traces."""
    message = str(exc)
    for secret in secret_values(settings):
        message = message.replace(secret, "[REDACTED]")
    return message


class PracticeBank:
    """The kit mock bank's session and ledger API, using the supplied OpenAPI base URL."""

    def __init__(
        self,
        base_url: str,
        settings: Settings,
        trace: Any,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not settings.freya_bank_team_key:
            raise ValueError("Set FREYA_BANK_TEAM_KEY in .env to prepare a practice case.")
        if not base_url.startswith(("https://", "http://")):
            raise ValueError("The tool API must declare an HTTP server URL.")
        self.base_url = base_url.rstrip("/")
        self.key = settings.freya_bank_team_key.get_secret_value()
        self.trace = trace
        self._monotonic = monotonic

    def _request(self, path: str, body: dict[str, Any] | None = None, *, operation: str) -> dict[str, Any]:
        request = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(body).encode("utf-8") if body is not None else None,
            headers={
                "content-type": "application/json",
                "user-agent": "freya-bank-scorer/0.1",
                "x-team-key": self.key,
            },
            method="POST" if body is not None else "GET",
        )
        # Writes are never retried: a repeated POST would create another customer session.
        started = self._monotonic()
        status = None
        metadata = {
            "service": "practice_bank",
            "scope": "http",
            "operation": operation,
            "request_id": uuid4().hex,
            "method": request.method,
            "path": path,
            "attempt": 1,
            "max_attempts": 1,
            "retry_scheduled": False,
            "retry_delay_ms": 0.0,
        }
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                code = getattr(response, "status", None)
                status = code if isinstance(code, int) else None
                result = json.load(response)
            if not isinstance(result, dict):
                raise ValueError("The mock bank returned a non-object response.")
            if result.get("error"):
                raise ValueError(f"The mock bank returned an error: {result['error']}")
        except Exception as exc:
            if isinstance(exc, urllib.error.HTTPError):
                status = exc.code
            self._record_request(
                **metadata,
                ok=False,
                status=status,
                duration_ms=round(max(0.0, self._monotonic() - started) * 1000, 3),
                error_type=type(exc).__name__,
                retryable=isinstance(exc, OSError) and (status is None or status == 429 or status >= 500),
            )
            self._emit(
                "practice_bank",
                "Mock bank request failed",
                path=path,
                error=str(exc).replace(self.key, "[REDACTED]"),
            )
            raise
        self._record_request(
            **metadata,
            ok=True,
            status=status,
            duration_ms=round(max(0.0, self._monotonic() - started) * 1000, 3),
            retryable=False,
        )
        self._emit("practice_bank", "Mock bank request completed", path=path, method=request.method)
        return result

    def _record_request(self, **metadata: Any) -> None:
        """Record transport metadata without the authenticated request or returned bank data."""
        self._emit(
            "request",
            "Mock bank HTTP attempt completed",
            **{key: value for key, value in metadata.items() if value is not None},
        )

    def _emit(self, kind: str, message: str, **metadata: Any) -> None:
        try:
            self.trace.event(kind, message, **metadata)
        except Exception:  # telemetry must never change request behavior
            logger.exception("could not record mock bank request telemetry")

    def create_session(self, case_id: str) -> dict[str, Any]:
        result = self._request("/practice/sessions", {"case": case_id}, operation="create_session")
        variables = result.get("variables")
        if not isinstance(variables, dict) or not variables.get("customer_id"):
            raise ValueError("The practice session returned no customer variables/customer_id.")
        return variables

    def ledger(self, customer_id: str) -> dict[str, Any]:
        return self._request(
            "/practice/ledger/" + urllib.parse.quote(customer_id, safe=""), operation="ledger"
        )


@dataclass
class ChatSession:
    """One conversation; freeze its workflow and variables until it ends."""

    client: FreyaClient
    workflow_id: str
    variables: dict[str, Any]
    version_id: str | None = None
    case: dict[str, Any] | None = None
    bank: PracticeBank | None = None
    call_id: str | None = None
    ended: bool = False
    messages: list[dict[str, str]] = field(default_factory=list)

    def send(self, message: str) -> Any:
        if self.ended:
            raise ValueError("This conversation has ended. Prepare a new conversation.")
        if not message.strip():
            raise ValueError("Enter a message to send.")
        turn = self.client.chat(
            self.workflow_id,
            message,
            customer_variables=self.variables if self.call_id is None else None,
            call_id=self.call_id,
            version_id=self.version_id if self.call_id is None else None,
        )
        self.call_id = turn.call_id
        self.ended = turn.ended_by_agent
        self.messages.extend(
            [
                {"role": "user", "content": message},
                {"role": "assistant", "content": turn.response},
            ]
        )
        return turn

    def end(self) -> None:
        if self.call_id and not self.ended:
            self.client.end_chat(self.call_id)
        self.ended = True
