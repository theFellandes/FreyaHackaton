"""A small offline stand-in for the Freya engine that runs the deterministic parts of a compiled workflow.

It executes what the platform executes in code - code nodes, ``api_request`` pre-actions, tool pre-/post-hooks,
response mappings, the engine's counters and the flattened ``<tool>_response.*`` variables - and follows every
deterministic transition (``equals``, ``contains``, ``greater_than``, ``is_set`` ... inside ``or`` / ``and`` groups). It
stops at a conversation node that waits for the customer: what the dialogue model would say or decide there is given by
the test (``call_tool`` for a tool call, ``move`` for a tool-call transition).

Semantics follow ``docs/workflow-format.md`` and ``docs/tools-and-code.md`` plus the facts verified from call records:
booleans of a response arrive as lowercase strings, ``<tool>_success_count`` counts any HTTP 2xx, a post-hook does not run
on a 5xx, and a pre-hook that empties the URL blocks the request (nothing is sent, no post-hook runs).

Nothing here talks to the network: requests are recorded and answered by the test.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from builder.checks.code_sandbox import run_hook, run_snippet

Responder = Callable[[str, dict[str, Any]], dict[str, Any] | None]
_TEMPLATE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_.]*)\s*\}\}")


def engine_value(value: Any) -> Any:
    """A response value as the engine hands it to variables and hooks (booleans become lowercase strings)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, dict):
        return {k: engine_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [engine_value(v) for v in value]
    return value


def fill(template: Any, variables: Mapping[str, Any]) -> Any:
    """Replace ``{{name}}`` references the way the engine does (an unknown name becomes an empty string)."""
    if not isinstance(template, str):
        return template
    return _TEMPLATE.sub(lambda m: "" if variables.get(m.group(1)) is None else str(variables.get(m.group(1))),
                         template)


@dataclass
class Request:
    """One HTTP request the workflow sent."""

    tool: str
    node: str
    body: dict[str, Any]


@dataclass
class Engine:
    """Runs a compiled workflow dict; ``vars`` holds the call's variables."""

    workflow: dict[str, Any]
    vars: dict[str, Any]
    responder: Responder = lambda tool, body: {"ok": True}
    requests: list[Request] = field(default_factory=list)
    spoken: list[str] = field(default_factory=list)
    visited: list[str] = field(default_factory=list)
    node: str = ""

    # ---- conditions --------------------------------------------------------------------------------------------------

    def holds(self, condition: Mapping[str, Any]) -> bool | None:
        """True / False for a deterministic condition, ``None`` for one the routing model judges (``matches_intent``)."""
        if "logic" in condition:
            results = [self.holds(c) for c in condition.get("conditions") or []]
            known = [r for r in results if r is not None]
            if condition["logic"] == "and":
                return all(known) if known else None
            return any(known) if known else None
        operator = condition.get("operator")
        if operator == "always":
            return True
        if operator == "matches_intent":
            return None
        value = self.vars.get(condition.get("variable", ""))
        operand = condition.get("operand")
        if operator == "is_set":
            return value is not None and value != ""
        if operator == "is_not_set":
            return value is None or value == ""
        if value is None:
            return operator == "not_equals"
        if operator == "equals":
            return str(value) == str(operand)
        if operator == "not_equals":
            return str(value) != str(operand)
        if operator == "contains":
            return str(operand).lower() in str(value).lower()
        try:
            if operator == "greater_than":
                return float(value) > float(operand)
            if operator == "less_than":
                return float(value) < float(operand)
        except (TypeError, ValueError):
            return False
        raise AssertionError(f"unknown operator {operator!r}")

    def next_node(self, name: str, *, deterministic_only: bool = True) -> str | None:
        """The target of the first transition of ``name`` that holds (intent transitions are skipped)."""
        for transition in self.workflow["nodes"][name].get("transitions") or []:
            if self.holds(transition["condition"]) is True:
                return str(transition["target_node"])
        return None

    # ---- execution ---------------------------------------------------------------------------------------------------

    def _api(self, node: str, function: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any] | None:
        props = function["parameters"]["properties"]
        body = {k: fill(v.get("default"), self.vars) for k, v in props["body"]["properties"].items()}
        return self._send(node, function["name"], body, config)

    def _send(self, node: str, tool: str, body: dict[str, Any], _config: Any = None) -> dict[str, Any] | None:
        self.requests.append(Request(tool=tool, node=node, body=json.loads(json.dumps(body))))
        response = self.responder(tool, body)
        if response is None:  # HTTP 5xx: the failure counter goes up, nothing else
            key = f"{tool}_failure_count"
            self.vars[key] = int(self.vars.get(key) or 0) + 1
            return None
        data = engine_value(response)
        self.vars[f"{tool}_response.status"] = "success"
        self.vars[f"{tool}_response.status_code"] = "200"
        for key, value in data.items():
            if not isinstance(value, (dict, list)):
                self.vars[f"{tool}_response.data.{key}"] = value
        key = f"{tool}_success_count"
        self.vars[key] = int(self.vars.get(key) or 0) + 1
        return data

    def _pre_actions(self, name: str) -> None:
        node = self.workflow["nodes"][name]
        for action in node.get("pre_actions") or []:
            function = action["function"]
            if function.get("handler") == "__function__:code_exec":
                self.vars = run_snippet(function["parameters"]["properties"]["code"]["default"], self.vars)
            elif function.get("handler") == "__function__:api_call":
                data = self._api(name, function)
                for mapping in node.get("response_mapping") or []:
                    value: Any = {"data": data} if data is not None else None
                    for part in str(mapping["path"]).split("."):
                        value = value.get(part) if isinstance(value, dict) else None
                    if value is not None:
                        self.vars[mapping["variable"]] = value

    def go(self, name: str, limit: int = 60) -> str:
        """Enter ``name`` and follow passthrough / deterministic transitions until a node waits for the customer."""
        for _ in range(limit):
            self.node = name
            self.visited.append(name)
            node = self.workflow["nodes"][name]
            self._pre_actions(name)
            on_enter = node.get("on_enter") or {}
            if on_enter.get("type") == "tts":
                self.spoken.append(fill(on_enter.get("text", ""), self.vars))
            if node.get("terminal") or (node.get("node_type") == "conversation" and not node.get("passthrough")):
                return name  # waits for the customer (transitions are judged after the next turn)
            target = self.next_node(name) or node.get("passthrough_default_transition")
            if target is None:
                return name
            name = target
        raise AssertionError(f"no waiting node reached after {limit} steps: {self.visited[-10:]}")

    def tool(self, name: str, node: str | None = None) -> dict[str, Any]:
        """The tool ``name`` offered on ``node`` (the current node by default)."""
        tools = self.workflow["nodes"][node or self.node].get("tools") or []
        found = [t for t in tools if t["function"]["name"] == name]
        assert found, f"{node or self.node} offers no {name} (it offers {[t['function']['name'] for t in tools]})"
        return found[0]

    def baked(self, tool: dict[str, Any]) -> dict[str, Any]:
        """Body fields the workflow fixes (a literal ``default`` or a ``{{var}}`` that has a value); the model never sees
        them, so they replace whatever the model sent."""
        out: dict[str, Any] = {}
        for key, leaf in tool["function"]["parameters"]["properties"]["body"]["properties"].items():
            default = leaf.get("default")
            if default is None:
                continue
            names = _TEMPLATE.findall(str(default))
            if not names or all(self.vars.get(n) not in (None, "") for n in names):
                out[key] = fill(default, self.vars)
        return out

    def call_tool(self, name: str, body: dict[str, Any], *, route: bool = True) -> tuple[bool, str]:
        """The dialogue model calls ``name`` with ``body`` on the current node.

        Returns ``(sent, what_to_do_next)``; with ``route`` the node's deterministic transitions are followed afterwards.
        """
        tool = self.tool(name)
        url = tool["function"]["parameters"]["properties"]["url"]["default"]
        config = {"url": url, "method": "POST", "headers": {}, "body": {**dict(body), **self.baked(tool)}}
        if tool.get("pre_process_code"):
            pre = run_hook(tool["pre_process_code"], vars=self.vars, config=config)
            self.vars, config = pre["vars"], pre["config"]
        sent = bool(config.get("url"))
        data = self._send(self.node, name, dict(config.get("body") or {})) if sent else None
        advice = ""
        if data is not None and tool.get("post_process_code"):
            post = run_hook(tool["post_process_code"], vars=self.vars,
                            result={"status": "success", "header": {}, "data": json.loads(json.dumps(data))})
            self.vars = post["vars"]
            result = post["result"]
            advice = str((result.get("data") or {}).get("what_to_do_next") or result.get("what_to_do_next") or "")
        if route:
            target = self.next_node(self.node)
            if target is not None:
                self.go(target)
        return sent, advice

    def move(self, target: str) -> str:
        """The dialogue model takes the tool-call transition to ``target`` from the current node."""
        targets = [t["target_node"] for t in self.workflow["nodes"][self.node].get("transitions") or []]
        assert target in targets, f"{self.node} has no move to {target} (it has {targets})"
        return self.go(target)

    def sent(self, tool: str) -> list[dict[str, Any]]:
        """Bodies of every request of ``tool`` sent so far."""
        return [r.body for r in self.requests if r.tool == tool]
