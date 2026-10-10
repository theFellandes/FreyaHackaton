"""Builder-facing Freya client: a typed, traced wrapper around the kit's reference client ``freya.py``.

Why a wrapper and not a new HTTP client
---------------------------------------
The organisers' ``freya.py`` (loaded by :func:`builder.freya.kit.load_kit`) already encodes the platform's quirks:
the browser-like ``User-Agent``, the ``{success, data: {message, result}}`` envelope, create-or-update by *slot*
with a state file, re-setting the agent model after an update, and publishing the newest version. This module
re-uses those functions and only adds what the builder needs on top:

* typed results (:class:`DeployInfo`, :class:`ChatTurn`) that can be stored in the LangGraph state;
* one error type (:class:`FreyaError`, with :class:`FreyaValidationError` for rejected workflows) instead of the
  kit's bare ``RuntimeError`` / ``urllib`` errors, carrying the HTTP status and response body;
* a trace event (``trace.freya(action, ...)``) for every write and every failure;
* timing and retry telemetry for every attempted wrapper operation, including successful reads;
* bounded retries for idempotent calls (reads and dry-run validation) -- never for writes or chat turns;
* routes the kit lacks, called through ``kit.api``: chat turns (``/api/v2/chat/message``, ``/api/v2/chat/end``),
  the version list and publish (``/api/v2/versions/workflow/{id}``), and reading a workflow back;
* a configurable state-file path (the kit defaults to ``.freya-agents.json`` in the *current directory*);
* polite pacing (:mod:`builder.freya.ratelimit`): every HTTP request of the loaded kit waits so calls start at least
  ``freya_min_interval_s`` apart, and :meth:`FreyaClient.simulation_slot` is the process-level "one practice
  simulation at a time" lock.

Deploy sequence (:meth:`FreyaClient.save`)
------------------------------------------
1. Refuse a workflow that contains ``null`` (the platform's schema rejects it) without any network call.
2. ``kit.save_workflow(slot, workflow, state_file)``: the first time a slot is seen the kit creates the workflow
   (named ``"<slot> <timestamp>"``; the platform creates the agent with it) and records ``workflow_id`` and
   ``agent_id`` in the state file; afterwards it updates the same workflow, re-sets the agent model to
   ``kit.AGENT_LLM`` and publishes. Re-using one workflow per slot matters: a workspace holds a limited number of
   workflows. If the recorded workflow no longer exists (HTTP 404), the stale slot is dropped and created again.
3. After a *create*, the kit neither checks the agent model nor publishes. The client reads the workflow back and,
   when the platform's default ``agent_config`` does not use the event model (a chat turn or real call cannot
   start then), runs ``kit.update_workflow`` once, which fixes the model and publishes.
4. Finally the client lists the versions and publishes the newest one if it is not yet published (calls and
   simulations run the *published* version only), then re-reads the list to confirm.

Typical use::

    client = FreyaClient(trace)
    client.validate("my-slot", workflow)            # dry run, raises FreyaValidationError
    info = client.save("my-slot", workflow)          # DeployInfo(workflow_id, agent_id, version, ...)
    stored = client.get_workflow(info.workflow_id)   # compare with builder.freya.diff.stripped_fields
    client.export(info.workflow_id, out_dir / "my-slot.workflow.json")

All methods are synchronous (the kit uses ``urllib``); async callers run them with
:func:`builder.freya.background.run_blocking` (an abandonable daemon thread, not ``asyncio.to_thread``). Every wrapper
operation first asks :func:`builder.freya.background.check_cancelled`, so once the build's wall-clock guard has
cancelled the thread's token the client starts no further request; :meth:`FreyaClient.deploy_snapshot` then says what
a cut-off :meth:`FreyaClient.save` had already created.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable, Iterable, Mapping
from contextlib import AbstractContextManager
from functools import partial
from pathlib import Path
from typing import Any, Protocol, TypeVar
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from builder.freya.background import PlatformCancelled, check_cancelled
from builder.freya.diff import null_paths
from builder.freya.kit import load_kit, repo_root
from builder.freya.ratelimit import PoliteLimiter, shared_limiter, simulation_slot, throttle_kit
from builder.settings import get_settings

__all__ = [
    "STATE_FILE_NAME",
    "ChatTurn",
    "DeployInfo",
    "FreyaClient",
    "FreyaError",
    "FreyaKit",
    "FreyaTrace",
    "FreyaValidationError",
    "count_nodes",
    "count_tools",
    "default_state_file",
    "tool_names",
]

logger = logging.getLogger(__name__)

T = TypeVar("T")

#: File name the kit uses for its slot -> {workflow_id, agent_id} state.
STATE_FILE_NAME = ".freya-agents.json"

# The kit's ``api`` raises RuntimeError(f"{method} {path} -> {code}: {body}") on an HTTP error status.
_HTTP_ERROR = re.compile(r"^(?P<method>[A-Z]+) (?P<path>\S+) -> (?P<status>\d{3}): (?P<body>.*)\Z", re.DOTALL)
# 4xx statuses that are about access or rate, not about the workflow content.
_NOT_A_VALIDATION_STATUS = frozenset({401, 403, 404, 405, 407, 408, 429})
_CLIP = 400


class FreyaTrace(Protocol):
    """The part of :class:`builder.trace.TraceWriter` this client uses."""

    def freya(self, action: str, **data: Any) -> None:
        """Record one Freya interaction."""


class FreyaKit(Protocol):
    """The functions of the kit's ``freya.py`` this client calls (a module or any object with them)."""

    def api(self, method: str, path: str, body: Any = None, **params: Any) -> Any:
        """Call a route and return the ``data.result`` part of the envelope."""

    def workspace_id(self) -> str:
        """Return the workspace id of the configured key."""

    def validate_workflow(self, name: str, workflow: dict[str, Any]) -> Any:
        """Dry-run ``POST /api/v2/workflow?dryRun=true``."""

    def save_workflow(self, slot: str, workflow: dict[str, Any], state_file: str = ...) -> dict[str, Any]:
        """Create the slot's workflow the first time, update and publish it afterwards."""

    def update_workflow(self, workflow_id: str, workflow: dict[str, Any]) -> int:
        """Replace the definition, fix the agent model, publish; returns the version number."""

    def export_workflow(self, workflow_id: str, path: str) -> str:
        """Write the stored workflow to ``path``."""

    def get_call(self, call_id: str) -> dict[str, Any]:
        """Return one call with its transcript."""


class FreyaError(RuntimeError):
    """A Freya API call failed.

    Attributes:
        action: The client operation that failed (``save``, ``chat``, ...).
        status: HTTP status code when the platform answered with an error status, else ``None``.
        body: The (possibly truncated) response body of an HTTP error, or ``""``.
        kind: ``http`` (error status), ``transport`` (network/timeout), ``response`` (unexpected response
            shape) or ``local`` (refused before sending, or local state problem).
        method: HTTP method of the failed request (``http`` errors only), else ``""``.
        path: Route of the failed request without query string (``http`` errors only), else ``""``.
    """

    def __init__(
        self,
        message: str,
        *,
        action: str,
        status: int | None = None,
        body: str = "",
        kind: str = "http",
        method: str = "",
        path: str = "",
    ):
        super().__init__(message)
        self.action = action
        self.status = status
        self.body = body
        self.kind = kind
        self.method = method
        self.path = path

    @property
    def retryable(self) -> bool:
        """True for failures worth repeating on an idempotent call: network errors, 429 and 5xx."""
        if self.kind == "transport":
            return True
        return self.status is not None and (self.status == 429 or self.status >= 500)


class FreyaValidationError(FreyaError):
    """Freya (or the client's own pre-send check) rejected a workflow definition.

    Attributes:
        problems: Human-readable problems extracted from the response body (or the local check), for the repair
            step's prompt.
    """

    def __init__(
        self,
        message: str,
        *,
        action: str,
        status: int | None = None,
        body: str = "",
        kind: str = "http",
        problems: list[str] | None = None,
    ):
        super().__init__(message, action=action, status=status, body=body, kind=kind)
        self.problems = list(problems or [])


class DeployInfo(BaseModel):
    """What :meth:`FreyaClient.save` created or updated on the platform.

    ``node_count`` counts ``nodes`` plus ``global_nodes``; ``tool_count`` counts unique tool function names across
    node ``tools``, ``pre_actions`` and workflow-level ``tools`` (see :func:`count_nodes`, :func:`count_tools`).
    ``published`` is ``True`` when the newest version was confirmed published, ``False`` when that could not be
    confirmed, and ``None`` when unknown (an instance built by hand).
    """

    model_config = ConfigDict(frozen=True)

    workflow_id: str
    agent_id: str | None
    version: int | None
    created: bool
    node_count: int
    tool_count: int
    slot: str = ""
    published: bool | None = None


class ChatTurn(BaseModel):
    """One turn of ``POST /api/v2/chat/message``: what the agent said and the call id to continue with."""

    model_config = ConfigDict(frozen=True)

    response: str
    call_id: str
    raw: dict[str, Any] = Field(default_factory=dict)

    @property
    def ended_by_agent(self) -> bool:
        """True when the platform reports that the agent ended the call in this turn (``endedByAgent``)."""
        return bool(self.raw.get("endedByAgent"))

    @property
    def warning(self) -> str | None:
        """The platform's ``warning`` for this turn, if any."""
        value = self.raw.get("warning")
        return str(value) if value else None


class _NoTrace:
    """Fallback trace used when the caller passes ``trace=None``."""

    def freya(self, action: str, **data: Any) -> None:
        logger.debug("freya %s %s", action, data)


def default_state_file() -> Path:
    """Return the kit state-file path: ``settings.freya_state_file``, else ``REPO_ROOT/.freya-agents.json``.

    A relative path is resolved against the repository root, so the result does not depend on the current
    directory (the kit's own default does). The default location is the kit's, so slots created by the kit's
    examples and by this builder share one state and keep updating the same workflows.
    """
    configured = getattr(get_settings(), "freya_state_file", None)
    if not configured:
        return repo_root() / STATE_FILE_NAME
    path = Path(configured)
    return path if path.is_absolute() else repo_root() / path


def count_nodes(workflow: Mapping[str, Any]) -> int:
    """Number of entries in ``nodes`` plus ``global_nodes``."""
    return sum(
        len(workflow[key]) for key in ("nodes", "global_nodes") if isinstance(workflow.get(key), Mapping)
    )


def tool_names(workflow: Mapping[str, Any]) -> set[str]:
    """Unique ``function.name`` values across node ``tools``, node ``pre_actions`` and workflow ``tools``.

    Global nodes are included. ``end_call`` terminals are not tools and are not counted.
    """
    names: set[str] = set()
    for group in ("nodes", "global_nodes"):
        nodes = workflow.get(group)
        if not isinstance(nodes, Mapping):
            continue
        for node in nodes.values():
            if isinstance(node, Mapping):
                names.update(_function_names(node.get("tools")))
                names.update(_function_names(node.get("pre_actions")))
    names.update(_function_names(workflow.get("tools")))
    return names


def count_tools(workflow: Mapping[str, Any]) -> int:
    """Number of unique tool function names (see :func:`tool_names`)."""
    return len(tool_names(workflow))


class FreyaClient:
    """Typed, traced access to the Freya API through the kit client.

    Args:
        trace: Receives ``trace.freya(action, **data)`` for every write and every failure
            (a :class:`builder.trace.TraceWriter`). ``None`` logs at debug level instead.
        kit: The kit module (or a stand-in with the same functions). Loaded lazily with
            :func:`builder.freya.kit.load_kit` on first use when omitted.
        state_file: Default state file for :meth:`save`; :func:`default_state_file` when omitted.
        read_retries: Extra attempts for idempotent calls (reads, dry-run validation, export) after a network
            error, 429 or 5xx. Writes and chat turns are never retried.
        retry_backoff_s: Linear back-off unit between those attempts.
        sleep: Sleep function (injected by tests).
        monotonic: Clock used to measure each attempted wrapper operation in request telemetry.
        limiter: Paces the platform calls (:class:`builder.freya.ratelimit.PoliteLimiter`, default 0.25 s between
            calls, ``settings.freya_min_interval_s``). ``None`` (default) paces the kit this client loads itself
            with the process-wide limiter and leaves an injected ``kit`` (tests) alone; pass a limiter to pace an
            injected kit too (once per wrapper attempt) or ``False`` to switch pacing off.
    """

    def __init__(
        self,
        trace: FreyaTrace | None,
        kit: FreyaKit | None = None,
        *,
        state_file: Path | str | None = None,
        read_retries: int = 2,
        retry_backoff_s: float = 2.0,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        limiter: PoliteLimiter | bool | None = None,
    ) -> None:
        self._trace: FreyaTrace = trace if trace is not None else _NoTrace()
        self._kit = kit
        self._state_file = Path(state_file) if state_file is not None else None
        self._read_retries = max(0, read_retries)
        self._retry_backoff_s = max(0.0, retry_backoff_s)
        self._sleep = sleep
        self._monotonic = monotonic
        self._limiter: PoliteLimiter | None = limiter if isinstance(limiter, PoliteLimiter) else None
        self._pace_loaded_kit = limiter is None  # default: pace the real kit this client loads
        self._kit_paced = False  # True once ``kit.api`` itself waits for the limiter
        self._deploys: dict[str, dict[str, Any]] = {}  # slot -> what save() did (see deploy_snapshot)

    # ------------------------------------------------------------------ basics

    @property
    def kit(self) -> FreyaKit:
        """The kit module, loaded on first access (and paced by the polite limiter)."""
        if self._kit is None:
            self._kit = load_kit()
            if self._pace_loaded_kit:
                self._limiter = shared_limiter(getattr(get_settings(), "freya_min_interval_s", None))
            if self._limiter is not None:
                self._kit_paced = throttle_kit(self._kit, self._limiter) or getattr(
                    getattr(self._kit, "api", None), "_freya_polite", False
                )
        return self._kit

    def simulation_slot(self, timeout_s: float | None = None) -> AbstractContextManager[None]:
        """Process-level lock for starting a practice simulation (the kit allows one per workspace).

        Hold it around the whole run: ``with client.simulation_slot(): run_practice(...)``. A second simulation in
        this process waits (or raises :class:`~builder.freya.ratelimit.SimulationBusy` after ``timeout_s``).
        """
        return simulation_slot(timeout_s)

    def workspace_id(self) -> str:
        """Return the workspace id of the configured key (``FREYA_WORKSPACE_ID`` or the first workspace)."""
        return str(self._call("workspace", self.kit.workspace_id, retries=self._read_retries))

    # ------------------------------------------------------------------ workflows

    def validate(self, name: str, workflow: dict[str, Any]) -> dict[str, Any]:
        """Dry-run the workflow (``POST /api/v2/workflow?dryRun=true``); nothing is stored.

        Returns:
            The dry-run result (its fields are not documented by the platform).

        Raises:
            FreyaValidationError: The workflow contains ``null``, the platform answered with a 4xx status that is
                about the content (400, 409, 422, ...), or the result explicitly reports it as invalid.
            FreyaError: Any other failure (authentication, network, server error).
        """
        self._require_no_nulls("validate", workflow)
        try:
            result = self._call(
                "validate",
                partial(self.kit.validate_workflow, name, workflow),
                retries=self._read_retries,
                method="POST",
                path="/api/v2/workflow",
            )
        except FreyaError as exc:
            if exc.kind == "http" and exc.status is not None and _is_validation_status(exc.status):
                raise FreyaValidationError(
                    str(exc),
                    action="validate",
                    status=exc.status,
                    body=exc.body,
                    problems=_problems_from_body(exc.body),
                ) from exc
            raise
        problems = _dry_run_problems(result)
        if problems:
            self._emit("validate", ok=False, name=name, problems=[_clip(p) for p in problems[:10]])
            raise FreyaValidationError(
                "the dry run reported the workflow as invalid: " + "; ".join(problems[:5]),
                action="validate",
                body=_clip(json.dumps(result, default=str), 1500),
                kind="response",
                problems=problems,
            )
        self._emit(
            "validate", ok=True, name=name, node_count=count_nodes(workflow), tool_count=count_tools(workflow)
        )
        return dict(result) if isinstance(result, Mapping) else {"result": result}

    def save(self, slot: str, workflow: dict[str, Any], state_file: Path | str | None = None) -> DeployInfo:
        """Create or update the slot's workflow, make sure the event model is set and the newest version is published.

        See the module docstring for the full sequence.

        Args:
            slot: The builder's own label for this agent (one workflow per slot is kept in the state file).
            workflow: The complete workflow definition (the platform replaces the whole definition on update).
            state_file: Overrides the client's state file for this call.

        Returns:
            :class:`DeployInfo` for the stored workflow; ``version`` is the newest version number.

        Raises:
            FreyaValidationError: The workflow contains ``null`` (nothing is sent).
            FreyaError: The platform call failed or the state file is unreadable.
        """
        if not slot or not slot.strip():
            raise ValueError("slot must be a non-empty string")
        self._require_no_nulls("save", workflow)
        path = self._resolve_state_file(state_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        previous = _read_state(path).get(slot)
        existed = previous is not None
        record: dict[str, Any] = {
            "state_file": path,
            "had_entry": existed,
            "info": None,
            "node_count": count_nodes(workflow),
            "tool_count": count_tools(workflow),
        }
        self._deploys[slot] = record
        try:
            entry = self._save_with_kit(slot, workflow, path)
        except FreyaError as exc:
            if not (existed and _is_missing_workflow(exc, previous)):
                raise
            self._forget_slot(path, slot, exc)
            existed = False
            record["had_entry"] = False
            entry = self._save_with_kit(slot, workflow, path)
        workflow_id = str(entry["workflow_id"])
        agent_id = entry.get("agent_id")
        version: int | None = _as_int(entry.get("version"))
        if not existed:
            version = self._ensure_agent_llm(workflow_id, workflow) or version
        newest, published = self.ensure_published(workflow_id)
        info = DeployInfo(
            workflow_id=workflow_id,
            agent_id=str(agent_id) if agent_id else None,
            version=newest if newest is not None else version,
            created=not existed,
            node_count=count_nodes(workflow),
            tool_count=count_tools(workflow),
            slot=slot,
            published=published,
        )
        record["info"] = info
        self._emit("save", ok=True, state_file=str(path), **info.model_dump(mode="json"))
        return info

    def ensure_published(self, workflow_id: str) -> tuple[int | None, bool]:
        """Publish the newest version if it is not yet, then re-read the version list to confirm it.

        :meth:`save` calls this once; a caller that got ``published=False`` back may call it again (the publish
        request is idempotent, and the confirmation read can lag behind the request).

        Returns:
            ``(newest version number, confirmed published)``; the number is ``None`` when the platform lists no
            versions at all.

        Raises:
            FreyaError: A request failed.
        """
        return self._ensure_published(workflow_id)

    def deploy_snapshot(self, slot: str) -> dict[str, Any] | None:
        """What this client has done for ``slot`` in this process, for a build that was cut off mid-deploy.

        After a complete :meth:`save` this is its :class:`DeployInfo` (as a dict, plus ``complete=True``). If the save
        did not finish (the wall-clock guard cancelled it) but the platform had already created or updated the
        workflow, the ids come from the kit's state file and the entry is marked ``complete=False`` and
        ``published=False``: the workflow exists, but nothing confirms that its newest version is the published one.

        Returns:
            The snapshot, or ``None`` when this client never started a save for ``slot`` or nothing is known.
        """
        record = self._deploys.get(slot)
        if record is None:
            return None
        info = record["info"]
        if isinstance(info, DeployInfo):
            return {**info.model_dump(mode="json"), "complete": True}
        try:
            entry = _read_state(record["state_file"]).get(slot)
        except FreyaError:
            return None
        if not isinstance(entry, Mapping) or not entry.get("workflow_id"):
            return None
        return {
            "workflow_id": str(entry["workflow_id"]),
            "agent_id": str(entry["agent_id"]) if entry.get("agent_id") else None,
            "version": _as_int(entry.get("version")),
            "created": not record["had_entry"],
            "node_count": record["node_count"],
            "tool_count": record["tool_count"],
            "slot": slot,
            "published": False,
            "complete": False,
        }

    def get_workflow(self, workflow_id: str) -> dict[str, Any]:
        """Return the stored workflow *definition* (the inner ``workflow`` object), as the platform kept it."""
        record = self._workflow_record(workflow_id)
        definition = record.get("workflow")
        if not isinstance(definition, Mapping):
            raise FreyaError(
                f"GET workflow {workflow_id}: the response has no workflow definition",
                action="get_workflow",
                kind="response",
            )
        return dict(definition)

    def workflow_status(self, workflow_id: str) -> dict[str, Any]:
        """Read workflow metadata and its version list, without publishing or changing anything.

        A previous published version does not mean the newest version is published. Both numbers are exposed
        separately so a monitor can identify unpublished edits without changing their state.
        """
        record = self._workflow_record(workflow_id)
        definition = record.get("workflow")
        if not isinstance(definition, Mapping):
            raise FreyaError(
                f"GET workflow {workflow_id}: the response has no workflow definition",
                action="workflow_status",
                kind="response",
            )
        versions = self._workflow_versions(workflow_id)
        numbered = [version for version in versions if _as_int(version.get("versionNumber")) is not None]
        newest = max(numbered, key=lambda version: _as_int(version.get("versionNumber")) or 0, default=None)
        published = [version for version in numbered if version.get("isPublished") is True]
        published_numbers = [_as_int(version.get("versionNumber")) for version in published]
        newest_number = _as_int(newest.get("versionNumber")) if newest else None
        published_flag = newest.get("isPublished") if newest else None
        return {
            "workflow_id": workflow_id,
            "agent_id": record.get("agentId"),
            "name": record.get("name"),
            "version": newest_number,
            "newest_version_id": newest.get("id") if newest else None,
            "newest_version_number": newest_number,
            "newest_published": published_flag if isinstance(published_flag, bool) else None,
            "published_version_number": max(
                (number for number in published_numbers if number is not None), default=None
            ),
            "updated_at": record.get("updatedAt"),
            "node_count": count_nodes(definition),
            "tool_count": count_tools(definition),
        }

    def export(self, workflow_id: str, path: Path | str) -> Path:
        """Write the workflow as stored on the platform to ``path`` (kit format; part of the submission).

        The kit writes ``{"workflow_id", "agent_id", "updated_at", "workflow"}``. Parent directories are created.
        """
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        self._call(
            "export",
            partial(self.kit.export_workflow, workflow_id, str(target)),
            retries=self._read_retries,
            method="GET",
            path=f"/api/v2/workflow/{workflow_id}",
            workflow_id=workflow_id,
        )
        self._emit("export", ok=True, workflow_id=workflow_id, path=str(target))
        return target

    # ------------------------------------------------------------------ chat and calls

    def chat(
        self,
        workflow_id: str,
        message: str,
        customer_variables: Mapping[str, Any] | None = None,
        call_id: str | None = None,
        *,
        version_id: str | None = None,
    ) -> ChatTurn:
        """Send one user turn to the published workflow (``POST /api/v2/chat/message``).

        Args:
            workflow_id: The workflow to talk to.
            message: What the simulated caller says.
            customer_variables: Call variables (first turn); ``None`` values are dropped (no ``null`` in JSON).
            call_id: The ``call_id`` of the previous turn to continue a conversation; ``None`` starts a new one.
            version_id: Optional workflow version to pin the conversation to (first turn only).

        Returns:
            :class:`ChatTurn` with the agent's reply and the call id. Chat turns are never retried: a repeated
            turn would reach the agent twice.
        """
        body: dict[str, Any] = {"workflowId": workflow_id, "message": message}
        if call_id:
            body["callId"] = call_id
        if version_id:
            body["versionId"] = version_id
        variables = {k: v for k, v in (customer_variables or {}).items() if v is not None}
        if variables:
            body["customerVariables"] = variables
        result = self._call(
            "chat",
            partial(self.kit.api, "POST", "/api/v2/chat/message", body),
            method="POST",
            path="/api/v2/chat/message",
            workflow_id=workflow_id,
            call_id=call_id,
        )
        if not isinstance(result, Mapping):
            raise FreyaError("chat: unexpected response shape", action="chat", kind="response")
        returned_id = result.get("callId") or call_id
        if not returned_id:
            raise FreyaError("chat: the response carries no callId", action="chat", kind="response")
        turn = ChatTurn(
            response=str(result.get("response") or ""), call_id=str(returned_id), raw=dict(result)
        )
        self._emit(
            "chat",
            ok=True,
            workflow_id=workflow_id,
            call_id=turn.call_id,
            message=_clip(message),
            response=_clip(turn.response),
            ended_by_agent=turn.ended_by_agent,
        )
        return turn

    def end_chat(self, call_id: str) -> dict[str, Any]:
        """End a chat conversation (``POST /api/v2/chat/end``); fires the end-of-call lifecycle tools."""
        result = self._call(
            "chat_end",
            partial(self.kit.api, "POST", "/api/v2/chat/end", {"callId": call_id}),
            method="POST",
            path="/api/v2/chat/end",
            call_id=call_id,
        )
        self._emit("chat_end", ok=True, call_id=call_id)
        return dict(result) if isinstance(result, Mapping) else {"result": result}

    def get_call(self, call_id: str) -> dict[str, Any]:
        """Return one call with its ``messages`` (transcript and engine events), via ``kit.get_call``."""
        result = self._call(
            "get_call",
            partial(self.kit.get_call, call_id),
            retries=self._read_retries,
            method="GET",
            path=f"/api/v2/call/{call_id}",
            call_id=call_id,
        )
        if not isinstance(result, Mapping):
            raise FreyaError(
                f"get_call {call_id}: unexpected response shape", action="get_call", kind="response"
            )
        return dict(result)

    def simulation_status(self, simulation_id: str) -> dict[str, Any]:
        """Fetch a simulation's current state with one read operation; do not start it or poll for completion."""
        result = self._call(
            "simulation_status",
            partial(self.kit.api, "GET", f"/api/v2/simulations/{simulation_id}"),
            retries=self._read_retries,
            method="GET",
            path=f"/api/v2/simulations/{simulation_id}",
            simulation_id=simulation_id,
        )
        simulation = _unwrap(result, "simulation")
        if not isinstance(simulation, Mapping):
            raise FreyaError(
                f"GET simulation {simulation_id}: unexpected response shape",
                action="simulation_status",
                kind="response",
            )
        return dict(simulation)

    # ------------------------------------------------------------------ internals: deploy steps

    def _save_with_kit(self, slot: str, workflow: dict[str, Any], path: Path) -> Mapping[str, Any]:
        """Run ``kit.save_workflow`` and check the shape of the state entry it returns."""
        entry = self._call("save", partial(self.kit.save_workflow, slot, workflow, state_file=str(path)))
        if not isinstance(entry, Mapping) or not entry.get("workflow_id"):
            raise FreyaError(
                f"save_workflow returned no workflow_id for slot {slot!r}", action="save", kind="response"
            )
        return entry

    def _forget_slot(self, path: Path, slot: str, cause: FreyaError) -> None:
        """Drop a slot whose workflow no longer exists on the platform from the kit state file."""
        state = _read_state(path)
        stale = state.pop(slot, None)
        path.write_text(json.dumps(state, indent=1), encoding="utf-8")
        logger.warning("workflow of slot %r is gone on the platform (%s); creating it again", slot, cause)
        self._emit("forget_slot", ok=True, slot=slot, stale=stale, status=cause.status)

    def _ensure_agent_llm(self, workflow_id: str, workflow: dict[str, Any]) -> int | None:
        """After a create: make the agent run on the event model (``kit.AGENT_LLM``) as ``update_workflow`` does.

        Returns the version number published by the fix, or ``None`` when nothing had to change.
        """
        wanted = getattr(self.kit, "AGENT_LLM", None)
        if not isinstance(wanted, Mapping):
            return None
        stored = self.get_workflow(workflow_id)
        config = stored.get("agent_config")
        llm = config.get("llmConfig") if isinstance(config, Mapping) else None
        current = (llm.get("provider"), llm.get("model")) if isinstance(llm, Mapping) else (None, None)
        if current == (wanted.get("provider"), wanted.get("model")):
            return None
        version = self._call(
            "set_agent_llm", partial(self.kit.update_workflow, workflow_id, workflow), workflow_id=workflow_id
        )
        self._emit(
            "set_agent_llm",
            ok=True,
            workflow_id=workflow_id,
            provider=wanted.get("provider"),
            model=wanted.get("model"),
            version=version,
        )
        return _as_int(version)

    def _ensure_published(self, workflow_id: str) -> tuple[int | None, bool]:
        """Publish the newest version if needed; return ``(newest version number, confirmed published)``."""
        newest = self._newest_version(workflow_id)
        if newest is None:
            self._emit("publish", ok=False, workflow_id=workflow_id, error="the platform listed no versions")
            return None, False
        number = _as_int(newest.get("versionNumber"))
        if newest.get("isPublished"):
            return number, True
        version_id = newest.get("id")
        if not version_id:
            raise FreyaError(
                f"the newest version of workflow {workflow_id} has no id", action="publish", kind="response"
            )
        self._call(
            "publish",
            partial(
                self.kit.api,
                "POST",
                f"/api/v2/versions/workflow/{workflow_id}/publish",
                {"versionId": version_id},
            ),
            method="POST",
            path=f"/api/v2/versions/workflow/{workflow_id}/publish",
            workflow_id=workflow_id,
        )
        confirmed = self._newest_version(workflow_id)
        published = bool(confirmed and confirmed.get("id") == version_id and confirmed.get("isPublished"))
        self._emit(
            "publish",
            ok=published,
            workflow_id=workflow_id,
            version=number,
            version_id=version_id,
            confirmed=published,
        )
        return number, published

    def _newest_version(self, workflow_id: str) -> Mapping[str, Any] | None:
        """Return the version with the highest ``versionNumber`` (``GET /api/v2/versions/workflow/{id}``)."""
        versions = self._workflow_versions(workflow_id)
        numbered = [version for version in versions if _as_int(version.get("versionNumber")) is not None]
        return (
            max(numbered, key=lambda version: _as_int(version.get("versionNumber")) or 0)
            if numbered
            else None
        )

    def _workflow_versions(self, workflow_id: str) -> list[Mapping[str, Any]]:
        """Read documented version metadata once, ignoring non-object entries in the returned list."""
        workspace = self.workspace_id()
        result = self._call(
            "versions",
            partial(self.kit.api, "GET", f"/api/v2/versions/workflow/{workflow_id}", workspaceId=workspace),
            retries=self._read_retries,
            method="GET",
            path=f"/api/v2/versions/workflow/{workflow_id}",
            workflow_id=workflow_id,
        )
        listed = _unwrap(result, "versions")
        if not isinstance(listed, list):
            raise FreyaError(
                f"versions of workflow {workflow_id}: unexpected response shape",
                action="versions",
                kind="response",
            )
        return [version for version in listed if isinstance(version, Mapping)]

    def _workflow_record(self, workflow_id: str) -> Mapping[str, Any]:
        """``GET /api/v2/workflow/{id}`` -> the record ``{id, agentId, updatedAt, workflow}``."""
        workspace = self.workspace_id()
        result = self._call(
            "get_workflow",
            partial(self.kit.api, "GET", f"/api/v2/workflow/{workflow_id}", workspaceId=workspace),
            retries=self._read_retries,
            method="GET",
            path=f"/api/v2/workflow/{workflow_id}",
            workflow_id=workflow_id,
        )
        record = _unwrap(result, "workflow")
        if not isinstance(record, Mapping):
            raise FreyaError(
                f"GET workflow {workflow_id}: unexpected response shape",
                action="get_workflow",
                kind="response",
            )
        return record

    # ------------------------------------------------------------------ internals: plumbing

    def _resolve_state_file(self, override: Path | str | None) -> Path:
        if override is not None:
            return Path(override)
        if self._state_file is not None:
            return self._state_file
        return default_state_file()

    def _require_no_nulls(self, action: str, workflow: Mapping[str, Any]) -> None:
        """Refuse a workflow containing ``null`` before any network call (the platform rejects it)."""
        paths = null_paths(workflow)
        if paths:
            self._emit(action, ok=False, error="workflow contains null", paths=paths[:20])
            raise FreyaValidationError(
                f"the workflow contains null at {len(paths)} path(s): {', '.join(paths[:10])}",
                action=action,
                kind="local",
                problems=[f"null value at {p}" for p in paths],
            )

    def _call(
        self,
        action: str,
        thunk: Callable[[], T],
        *,
        retries: int = 0,
        method: str | None = None,
        path: str | None = None,
        workflow_id: str | None = None,
        call_id: str | None = None,
        simulation_id: str | None = None,
    ) -> T:
        """Time every kit attempt, translate its errors, and retry only when the caller allows it.

        The kit hides successful HTTP statuses and may make multiple HTTP calls inside save/update. Request
        records therefore describe wrapper attempts; they never pretend to count each underlying HTTP request.
        Bodies, headers, and exception text are deliberately excluded from these request records.
        """
        attempt = 1
        request_id = uuid4().hex
        while True:
            check_cancelled()  # the build was cancelled / its deadline passed: start nothing new
            # An unpaced (injected) kit is paced here, once per attempt; a loaded kit paces every HTTP call itself.
            if self._limiter is not None and not self._kit_paced:
                self._limiter.wait()
            started = self._monotonic()
            try:
                result = thunk()
            except PlatformCancelled:
                raise  # not a platform failure: the paced kit refused to start a request
            except Exception as exc:
                # Already-translated errors keep their previous behavior: propagate without another retry.
                error = (
                    exc
                    if isinstance(exc, FreyaError)
                    else (
                        _translate(action, exc)
                        if isinstance(
                            exc,
                            RuntimeError
                            | OSError
                            | ValueError
                            | KeyError
                            | IndexError
                            | TypeError
                            | AttributeError,
                        )
                        else None
                    )
                )
                retry = (
                    error is not None
                    and not isinstance(exc, FreyaError)
                    and attempt <= retries
                    and error.retryable
                )
                retry_delay = self._retry_backoff_s * attempt if retry else 0.0
                self._record_request(
                    action,
                    request_id=request_id,
                    attempt=attempt,
                    max_attempts=retries + 1,
                    ok=False,
                    duration_ms=round(max(0.0, self._monotonic() - started) * 1000, 3),
                    method=error.method or method if error else method,
                    path=error.path or path if error else path,
                    status=error.status if error else None,
                    error_type=type(exc).__name__,
                    error_kind=error.kind if error else "unexpected",
                    retryable=error.retryable if error else False,
                    retry_scheduled=retry,
                    retry_delay_ms=round(retry_delay * 1000, 3),
                    workflow_id=workflow_id,
                    call_id=call_id,
                    simulation_id=simulation_id,
                )
                if retry:
                    logger.info("freya %s failed (%s); retry %d of %d", action, error, attempt, retries)
                    self._sleep(retry_delay)
                    attempt += 1
                    continue
                if isinstance(exc, FreyaError) or error is None:
                    raise
                self._emit(action, ok=False, status=error.status, kind=error.kind, error=_clip(str(error)))
                raise error from exc
            else:
                returned_call_id = (
                    result.get("callId") if action == "chat" and isinstance(result, Mapping) else None
                )
                returned_workflow_id = (
                    result.get("workflow_id") if action == "save" and isinstance(result, Mapping) else None
                )
                self._record_request(
                    action,
                    request_id=request_id,
                    attempt=attempt,
                    max_attempts=retries + 1,
                    ok=True,
                    duration_ms=round(max(0.0, self._monotonic() - started) * 1000, 3),
                    method=method,
                    path=path,
                    retryable=False,
                    retry_scheduled=False,
                    retry_delay_ms=0.0,
                    workflow_id=returned_workflow_id or workflow_id,
                    call_id=returned_call_id or call_id,
                    simulation_id=simulation_id,
                )
                return result

    def _record_request(self, operation: str, **data: Any) -> None:
        """Emit safe request metadata, while supporting older traces with only a freya method."""
        if data.get("path"):
            data["path"] = str(data["path"]).split("?", 1)[0]
        payload = {
            "service": "freya",
            "scope": "wrapper",
            "operation": operation,
            **{key: value for key, value in data.items() if value is not None},
        }
        try:
            event = getattr(self._trace, "event", None)
            if callable(event):
                event("request", "Freya wrapper attempt completed", **payload)
            else:
                self._emit("request", **payload)
        except Exception:  # telemetry must never change request behavior
            logger.exception("could not record Freya request telemetry")

    def _emit(self, action: str, **data: Any) -> None:
        """Send one trace event; a failing trace never breaks a Freya operation."""
        try:
            self._trace.freya(action, **data)
        except Exception:  # pragma: no cover - defensive: tracing is a side channel
            logger.exception("trace.freya(%s) failed", action)


# ---------------------------------------------------------------------- module helpers


def _translate(action: str, exc: BaseException) -> FreyaError:
    """Map a kit exception to a :class:`FreyaError` with status, body and kind."""
    text = str(exc)
    if isinstance(exc, RuntimeError):
        match = _HTTP_ERROR.match(text)
        if match:
            status = int(match["status"])
            body = match["body"]
            return FreyaError(
                f"{action}: HTTP {status} on {match['method']} {match['path']}: {_clip(body)}",
                action=action,
                status=status,
                body=body,
                method=match["method"],
                path=match["path"].split("?", 1)[0],
            )
        return FreyaError(f"{action}: {text}", action=action, kind="response")
    if isinstance(exc, OSError):  # urllib.error.URLError, TimeoutError, ConnectionError
        return FreyaError(f"{action}: network error: {exc!r}", action=action, kind="transport")
    return FreyaError(f"{action}: unexpected response from Freya: {exc!r}", action=action, kind="response")


def _is_validation_status(status: int) -> bool:
    return 400 <= status < 500 and status not in _NOT_A_VALIDATION_STATUS


def _is_missing_workflow(exc: FreyaError, previous: Any) -> bool:
    """True when ``exc`` says the slot's recorded workflow itself is gone (404 on ``/api/v2/workflow/{id}``).

    Deliberately narrow: a 404 from any other route (versions, publish, workspaces) must never make the client
    create a second workflow for the slot, because a workspace holds only a few workflows.
    """
    recorded = previous.get("workflow_id") if isinstance(previous, Mapping) else None
    return (
        bool(recorded)
        and exc.kind == "http"
        and exc.status == 404
        and exc.path == f"/api/v2/workflow/{recorded}"
    )


def _dry_run_problems(result: Any) -> list[str]:
    """Problems a dry-run result reports explicitly (``valid``/``isValid``/``success`` false, or ``errors``)."""
    if not isinstance(result, Mapping):
        return []
    flags = [result.get(flag) for flag in ("valid", "isValid", "success") if flag in result]
    errors = result.get("errors") or result.get("validationErrors")
    listed = (
        [_stringify(e) for e in errors]
        if isinstance(errors, list)
        else ([_stringify(errors)] if errors else [])
    )
    if any(flag is False for flag in flags):
        return listed or ["the dry run reported the workflow as invalid"]
    if listed and not any(flag is True for flag in flags):
        return listed
    return []


def _problems_from_body(body: str) -> list[str]:
    """Extract readable problems from an error body (JSON ``errors``/``details``/``message`` or the raw text)."""
    text = body.strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except ValueError:
        return [_clip(text, 1500)]
    found: list[str] = []
    _collect_messages(parsed, found)
    return found or [_clip(text, 1500)]


def _collect_messages(node: Any, found: list[str]) -> None:
    """Depth-first collection of ``errors``/``details``/``message``/``error`` values in a parsed error body."""
    if isinstance(node, Mapping):
        for key in ("errors", "details", "issues", "message", "error"):
            value = node.get(key)
            if isinstance(value, str) and value:
                found.append(value)
            elif isinstance(value, list):
                found.extend(_stringify(item) for item in value)
            elif isinstance(value, Mapping):
                _collect_messages(value, found)
        data = node.get("data")
        if isinstance(data, Mapping):
            _collect_messages(data, found)


def _read_state(path: Path) -> dict[str, Any]:
    """Read the kit state file (slot -> entry); a missing file is an empty state."""
    if not path.exists():
        return {}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise FreyaError(
            f"the Freya state file {path} is unreadable ({exc}); fix or remove it",
            action="save",
            kind="local",
        ) from exc
    if not isinstance(state, dict):
        raise FreyaError(
            f"the Freya state file {path} does not hold a JSON object", action="save", kind="local"
        )
    return state


def _function_names(items: Any) -> Iterable[str]:
    if not isinstance(items, list):
        return []
    names = []
    for item in items:
        function = item.get("function") if isinstance(item, Mapping) else None
        name = function.get("name") if isinstance(function, Mapping) else None
        if isinstance(name, str) and name:
            names.append(name)
    return names


def _unwrap(result: Any, key: str) -> Any:
    """The kit's fallback rule: ``result[key]`` when present, else the bare result."""
    if isinstance(result, Mapping) and key in result:
        return result[key]
    return result


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _stringify(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, default=str)


def _clip(text: str, limit: int = _CLIP) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"
