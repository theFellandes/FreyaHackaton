"""Small constructors for workflow nodes, transitions and conditions (plain dicts, platform shapes).

These are the building blocks of the profiles. They encode the conventions of the validated reference agent:

* every non-conversation node is a *passthrough* node that ends with an ``always`` transition;
* tool-call transitions (``is_tool_call``) let the dialogue model say goodbye and move in the same message;
* variable conditions sit inside an ``or`` group, ``and`` nests inside it, and booleans test both spellings.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from builder.compiler.tools import code_exec, end_terminal

__all__ = [
    "always_transition",
    "and_group",
    "api_request_node",
    "bool_true",
    "code_node",
    "contains",
    "counter_above",
    "end_call_node",
    "equals",
    "intent_transition",
    "or_group",
    "tool_transition",
    "transition",
    "tts_node",
]


def equals(variable: str, operand: Any) -> dict[str, Any]:
    """A deterministic ``equals`` condition (string comparison on the platform)."""
    return {"variable": variable, "operator": "equals", "operand": operand}


def contains(variable: str, operand: str) -> dict[str, Any]:
    """A deterministic ``contains`` condition (the variable's text contains ``operand``)."""
    return {"variable": variable, "operator": "contains", "operand": operand}


def counter_above(variable: str, operand: int = 0) -> dict[str, Any]:
    """``variable > operand`` (numeric comparison; use for the engine's success / failure counters)."""
    return {"variable": variable, "operator": "greater_than", "operand": operand}


def bool_true(variable: str) -> list[dict[str, Any]]:
    """The two spellings of a true boolean (``true`` / ``True``) as OR-ed conditions."""
    return [equals(variable, "true"), equals(variable, "True")]


def or_group(*conditions: dict[str, Any]) -> dict[str, Any]:
    """The top-level group of a variable condition (always ``or``)."""
    return {"logic": "or", "conditions": list(conditions)}


def and_group(*conditions: dict[str, Any]) -> dict[str, Any]:
    """An ``and`` group, to be nested inside :func:`or_group`."""
    return {"logic": "and", "conditions": list(conditions)}


def transition(target: str, description: str, condition: dict[str, Any]) -> dict[str, Any]:
    """A transition to ``target`` with an explicit condition."""
    return {"target_node": target, "description": description, "condition": condition}


def always_transition(target: str, description: str = "always") -> dict[str, Any]:
    """The unconditional transition every passthrough node ends with."""
    return transition(target, description, {"operator": "always"})


def intent_transition(target: str, description: str, intent: str) -> dict[str, Any]:
    """A semantic transition judged by the routing model after each user message."""
    return transition(target, description, {"operator": "matches_intent", "description": intent})


def tool_transition(target: str, description: str) -> dict[str, Any]:
    """A tool-call transition: the dialogue model calls ``transition_to_<target>`` and moves at once."""
    return {"target_node": target, "description": description, "is_tool_call": True,
            "condition": {"operator": "matches_intent", "description": description}}


def code_node(
    name: str, code: str, nxt: str, timeout_ms: int = 10000, routes: Sequence[dict[str, Any]] = ()
) -> dict[str, Any]:
    """A passthrough ``code`` node that runs ``code`` and moves on to ``nxt``.

    ``routes`` are transitions with a variable condition that are tried first (in order); the ``always`` transition to
    ``nxt`` stays last, as the engine requires of a passthrough node.
    """
    return {
        "name": name,
        "node_type": "code",
        "passthrough": True,
        "passthrough_default_transition": nxt,
        "on_enter": {"type": "silent"},
        "disable_inactivity_monitor": True,
        "pre_actions": [code_exec(code, timeout_ms)],
        "transitions": [*routes, always_transition(nxt)],
    }


def api_request_node(
    name: str,
    pre_action: dict[str, Any],
    nxt: str,
    mapping: Sequence[tuple[str, str]] = (),
) -> dict[str, Any]:
    """A passthrough ``api_request`` node: the engine calls ``pre_action`` on entry, then moves on to ``nxt``.

    ``mapping`` is ``(path, variable)`` pairs at node level (paths start with ``data.``).
    """
    node: dict[str, Any] = {
        "name": name,
        "node_type": "api_request",
        "passthrough": True,
        "passthrough_default_transition": nxt,
        "on_enter": {"type": "silent"},
        "disable_inactivity_monitor": True,
        "pre_actions": [pre_action],
        "transitions": [always_transition(nxt)],
    }
    if mapping:
        node["response_mapping"] = [{"path": path, "variable": variable} for path, variable in mapping]
    return node


def tts_node(name: str, text: str, nxt: str, task_message: str) -> dict[str, Any]:
    """A passthrough conversation node that speaks the fixed line ``text`` and moves on to ``nxt``.

    Used for sentences the brief prescribes (callback confirmed, promise recorded, identity failed): a fixed line cannot
    be skipped or reworded by the model.
    """
    return {
        "name": name,
        "node_type": "conversation",
        "on_enter": {"type": "tts", "text": text},
        "task_message": task_message,
        "passthrough": True,
        "passthrough_default_transition": nxt,
        "transitions": [always_transition(nxt)],
    }


def end_call_node(name: str, function_name: str) -> dict[str, Any]:
    """A silent ``end_call`` node (the goodbye was said in the previous conversation node)."""
    return {
        "name": name,
        "node_type": "end_call",
        "on_enter": {"type": "silent"},
        "disable_inactivity_monitor": True,
        "transitions": [],
        "terminal": end_terminal(function_name, "none"),
    }
