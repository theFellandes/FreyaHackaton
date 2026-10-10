"""Builders for the tool functions of a workflow, driven by the bank's tool contracts.

Three kinds of call exist (``docs/tools-and-code.md``):

* **Model-called tools** (:func:`model_tool`) live in a conversation node's ``tools``. ``customer_id`` and any other
  field passed in ``baked`` get a ``default`` that the engine resolves when the node is entered (the model never sees
  them); every other field is a leaf under ``body.properties`` whose ``default`` is its own ``{{name}}`` placeholder,
  which has no value, so the model fills it. The leaf carries the contract's ``type``, ``enum`` and description.
* **Engine calls** (:func:`engine_call`) live in an ``api_request`` node's ``pre_actions``: every field is baked.
* **Code** (:func:`code_exec`) is the ``code_exec`` pre-action of a ``code`` node.

Nothing here knows a brief: URLs, fields and enums come from :class:`~builder.ingest.tool_api.ToolContract`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from builder.compiler.pycode import assert_safe_code
from builder.ingest.tool_api import FieldSpec, ToolContract

__all__ = [
    "api_parameters",
    "code_exec",
    "end_terminal",
    "engine_call",
    "field_leaf",
    "model_tool",
]

API_CALL_HANDLER = "__function__:api_call"
CODE_HANDLER = "__function__:code_exec"
END_CALL_HANDLER = "__function__:end_call"
_JSON_LEAF_TYPES = {"string", "number", "integer", "boolean"}


def _placeholder(name: str) -> str:
    return "{{" + name + "}}"


def field_leaf(spec: FieldSpec, *, default: str | None = None, note: str = "") -> dict[str, Any]:
    """One body leaf for a request field: contract type, enum and description, plus ``default`` (own placeholder)."""
    leaf: dict[str, Any] = {"type": spec.type if spec.type in _JSON_LEAF_TYPES else "string"}
    text = " ".join(part for part in (spec.description.strip(), note.strip()) if part)
    if spec.format == "date" and "YYYY-MM-DD" not in text:
        text = f"{text} Format YYYY-MM-DD.".strip()
    if text:
        leaf["description"] = text
    if spec.enum:
        leaf["enum"] = list(spec.enum)
    leaf["default"] = default if default is not None else _placeholder(spec.name)
    return leaf


def api_parameters(contract: ToolContract, body: Mapping[str, Any], required: Sequence[str] = ()) -> dict[str, Any]:
    """The ``parameters`` object of an ``api_call`` function: baked ``url``/``method``/``headers`` and the ``body``."""
    return {
        "type": "object",
        "properties": {
            "url": {"type": "string", "default": contract.url},
            "method": {"type": "string", "default": contract.method or "POST"},
            "headers": {
                "type": "object",
                "properties": {"Content-Type": {"type": "string", "default": "application/json"}},
            },
            "body": {"type": "object", "properties": dict(body), "required": list(required)},
        },
    }


def model_tool(
    contract: ToolContract,
    *,
    baked: Mapping[str, str] | None = None,
    description: str,
    include_optional: Sequence[str] = (),
    field_notes: Mapping[str, str] | None = None,
    response_mapping: Sequence[tuple[str, str]] = (),
    timeout_ms: int = 12000,
    max_retries: int = 0,
    pre_process_code: str = "",
    post_process_code: str = "",
    customer_field: str = "customer_id",
    customer_ref: str = "{{customer_id}}",
) -> dict[str, Any]:
    """A tool the dialogue model calls, for ``contract``.

    Args:
        contract: The tool's contract (URL, method, request fields).
        baked: Fields fixed by the workflow, ``name -> value``. The customer-id field (``customer_field``, default
            ``customer_id``) is always baked as ``customer_ref`` (default ``{{customer_id}}``) when the contract has it,
            unless given here. A baked value may be a literal or a ``{{var}}`` reference.
        description: Instruction for the model that says when to CALL the tool (the only text it reads about it).
        include_optional: Optional request fields (not required by the contract) the model may also fill.
        field_notes: Extra description text per field, appended to the contract's own.
        response_mapping: ``(path, variable)`` pairs; tool-level, paths start with ``data.``.
        timeout_ms / max_retries: ``config`` of the call. Writes use ``max_retries=0``.
        pre_process_code / post_process_code: Hooks (see :mod:`builder.compiler.hooks`); empty = none.
    """
    baked_values = dict(baked or {})
    if contract.get_field(customer_field, "request") is not None:
        baked_values.setdefault(customer_field, customer_ref)
    notes = dict(field_notes or {})
    body: dict[str, Any] = {}
    required: list[str] = []
    for spec in contract.request_fields:
        wanted = spec.name in baked_values or spec.required or spec.name in include_optional
        if not wanted:
            continue
        if spec.name in baked_values:
            body[spec.name] = {"type": spec.type if spec.type in _JSON_LEAF_TYPES else "string",
                               "default": baked_values[spec.name]}
        else:
            body[spec.name] = field_leaf(spec, note=notes.get(spec.name, ""))
            if spec.required:
                required.append(spec.name)
    for name, value in baked_values.items():  # baked fields the contract does not list (kept, lint will flag them)
        body.setdefault(name, {"type": "string", "default": value})

    tool: dict[str, Any] = {
        "type": "function",
        "config": {"timeoutMs": timeout_ms, "maxRetries": max_retries},
        "function": {
            "name": contract.name,
            "handler": API_CALL_HANDLER,
            "description": description,
            "parameters": api_parameters(contract, body, required),
        },
    }
    if response_mapping:
        tool["response_mapping"] = [{"path": path, "variable": variable} for path, variable in response_mapping]
    if pre_process_code.strip():
        tool["pre_process_code"] = assert_safe_code(pre_process_code, f"{contract.name}.pre_process_code")
    if post_process_code.strip():
        tool["post_process_code"] = assert_safe_code(post_process_code, f"{contract.name}.post_process_code")
    return tool


def engine_call(
    contract: ToolContract,
    *,
    baked: Mapping[str, str],
    name: str | None = None,
    timeout_ms: int = 12000,
    max_retries: int = 0,
) -> dict[str, Any]:
    """A ``pre_actions`` entry for an ``api_request`` node: the engine calls ``contract`` with every field baked.

    ``baked`` maps request field names to literal values or ``{{var}}`` references. ``name`` overrides the function
    name (default: the tool name, which also names the ``<tool>_response`` variables).
    """
    body: dict[str, Any] = {}
    for field_name, value in baked.items():
        spec = contract.get_field(field_name, "request")
        kind = spec.type if spec is not None and spec.type in _JSON_LEAF_TYPES else "string"
        body[field_name] = {"type": kind, "default": value}
    return {
        "type": "function",
        "config": {"timeoutMs": timeout_ms, "maxRetries": max_retries},
        "function": {
            "name": name or contract.name,
            "handler": API_CALL_HANDLER,
            "description": contract.summary or contract.name,
            "parameters": api_parameters(contract, body, []),
        },
    }


def code_exec(code: str, timeout_ms: int = 10000) -> dict[str, Any]:
    """A ``pre_actions`` entry that runs ``code`` (``vars`` in, ``vars`` out); checked for double braces and syntax."""
    return {
        "type": "function",
        "function": {
            "name": "code_exec",
            "handler": CODE_HANDLER,
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "default": assert_safe_code(code, "code_exec")},
                    "timeout_ms": {"type": "number", "default": timeout_ms},
                },
            },
        },
    }


def end_terminal(function_name: str, message_mode: str = "none", spoken_message: str = "") -> dict[str, Any]:
    """The ``terminal`` of an ``end_call`` node. The call's ``endedReason`` is ``function_name``.

    ``message_mode`` is ``none`` (hang up silently; the goodbye was said in the previous node), ``custom`` (say
    ``spoken_message``) or ``llm``.
    """
    properties: dict[str, Any] = {"message_mode": {"type": "string", "default": message_mode}}
    if message_mode == "custom":
        properties["spoken_message"] = {"type": "string", "default": spoken_message}
    return {
        "type": "function",
        "function": {
            "name": function_name,
            "handler": END_CALL_HANDLER,
            "parameters": {"type": "object", "properties": properties},
        },
    }
