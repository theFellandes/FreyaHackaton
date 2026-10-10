"""Static lint rules for Freya workflow JSON (rules L001-L029).

Every rule encodes a platform behaviour documented in the kit (``docs/workflow-format.md``,
``docs/tools-and-code.md``) or measured on the reference agent. A violation usually makes the platform *silently* drop
something (a ``null`` is rejected, ``tools`` on a code node are stripped, an unresolved ``{{variable}}`` is spoken aloud,
a passthrough node without an ``always`` transition deadlocks), so the checks run before anything is deployed.

:func:`lint_workflow` works on the plain workflow dict, so it checks builder output, hand-written reference agents and
exports alike. It needs the tool API (for URLs, fields and enums) and the names of the call variables. Findings have a
severity: ``error`` blocks deployment, ``warning`` is reported in the trace only.

======  =========  =========================================================================================
Rule    Severity   Check
======  =========  =========================================================================================
L001    error      No ``null`` anywhere.
L002    error      ``tools`` only on conversation nodes (workflow-level tools need ``alwaysRunsAt``).
L003    error      ``pre_actions`` only on api_request / code nodes.
L004    error      Passthrough nodes: default transition set, last transition ``always``; api/code nodes passthrough.
L005    error      Every node reachable from ``initial_node`` (global nodes count as roots).
L006    error      Every node can reach an ``end_call`` node.
L007    error      Transition / default / max-turn / initial targets exist; ``__previous__`` only in global nodes.
L008    error      Code: no ``{{`` / ``}}``, compiles; warning for imports beyond json/re/datetime or no ``try``.
L009    error      ``response_mapping``: ``data.`` paths, node level for pre-actions, tool level for tools.
L010    error      ``equals`` on a boolean tests both spellings (``true`` / ``True``) in one OR group.
L011    error      ``api_call`` parameters: ``default`` (never ``const``), body fields are leaves with defaults.
L012    error      Tool names, URLs, methods, fields and enums match the tool API.
L013    error      ``close_call`` is reachable before every ``end_call`` and never twice on a path.
L014    error      Every ``{{variable}}`` and condition variable can be resolved.
L015    error      Extraction fields set ``min_confidence``.
L016    error      ``end_call`` nodes: silent ``on_enter``, no transitions, inactivity monitor disabled.
L017    error      ``max_node_turns`` comes with ``max_turn_transition``.
L018    warning    ``task_message`` / ``system_prompt`` longer than 6000 characters.
L019    error      No ``agent_config``.
L020    warning    Model-called tools have a description that says when to CALL them.
L021    error      Top-level condition groups use ``or``; warning for bare variable conditions and for
                   ``matches_intent`` listed before a deterministic transition.
L022    warning    ``{name}`` placeholder (IR style) left in spoken text.
L023    error      ``is_tool_call`` only on transitions of conversation nodes.
L024    warning    Node ``name`` equals its key.
L025    error      ``matches_intent`` conditions have a description; transitions have a ``target_node``.
L026    warning    Duplicate tool names in one node.
L027    error      ``tts`` entry text is not empty.
L028    error      No two transitions of a node share a ``target_node`` (the engine merges them into one OR condition).
L029    error      Post-hooks, conditions and prompts read only response fields the tool's contract returns.
======  =========  =========================================================================================
"""

from __future__ import annotations

import ast
import re
from collections import deque
from collections.abc import Iterator
from typing import Any, Literal

from pydantic import BaseModel, Field

from builder.checks.code_sandbox import extract_code_snippets
from builder.ingest.tool_api import FieldSpec, ToolApi, ToolContract

__all__ = ["RULES", "Finding", "LintReport", "lint_workflow"]

Severity = Literal["error", "warning"]

RULES: dict[str, str] = {
    "L001": "no null values",
    "L002": "tools only on conversation nodes",
    "L003": "pre_actions only on api_request/code nodes",
    "L004": "passthrough nodes route (default transition, final always)",
    "L005": "all nodes reachable from the initial node",
    "L006": "every node can reach an end_call node",
    "L007": "transition targets exist",
    "L008": "code compiles and has no double braces",
    "L009": "response_mapping placement and paths",
    "L010": "boolean equals tests both spellings",
    "L011": "api_call parameter shape (default, leaf body fields)",
    "L012": "tools match the tool API (names, URLs, fields, enums)",
    "L013": "close_call before every end_call, never twice",
    "L014": "variables resolvable",
    "L015": "extraction min_confidence set",
    "L016": "end_call node shape",
    "L017": "max_node_turns needs max_turn_transition",
    "L018": "prompt length",
    "L019": "no agent_config",
    "L020": "tool descriptions say when to call",
    "L021": "condition shape and order",
    "L022": "IR placeholders left in spoken text",
    "L023": "is_tool_call only on conversation nodes",
    "L024": "node name equals key",
    "L025": "matches_intent description, transition target",
    "L026": "duplicate tool names in a node",
    "L027": "tts text not empty",
    "L028": "one transition per target node",
    "L029": "response fields read exist in the tool contract",
}

END_CALL = "end_call"
PREVIOUS = "__previous__"
API_CALL = "__function__:api_call"
CODE_EXEC = "__function__:code_exec"
END_HANDLER = "__function__:end_call"
MAX_PROMPT_CHARS = 6000
_REF = re.compile(r"\{\{\s*([^{}]*?)\s*\}\}")
_SINGLE_BRACE = re.compile(r"(?<!\{)\{([A-Za-z_][A-Za-z0-9_]*)\}(?!\})")
_RESPONSE_REF = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)_response\.data\.([^.\s]+)")
_VARS_ASSIGN = re.compile(r"""vars\s*\[\s*['"]([^'"]+)['"]\s*\]\s*(?:=|\+=)""")
_VARS_DYNAMIC = re.compile(r"""vars\s*\[[^\]\n]*?\+\s*['"]([^'"]+)['"]\s*\]\s*(?:=|\+=)""")
_IMPORT = re.compile(r"^\s*(?:import|from)\s+([A-Za-z_][\w.]*)", re.MULTILINE)
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SYSTEM_PREFIXES = ("system.", "call.", "date.", "time.", "extractor.")
_ALLOWED_IMPORTS = {"json", "re", "datetime"}
_API_PARAMS = {"url", "method", "headers", "body", "connect_retries"}


class Finding(BaseModel):
    """One lint finding: the rule id, severity, where in the workflow (a JSON path) and what is wrong."""

    rule: str
    severity: Severity
    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.rule} {self.severity} {self.path}: {self.message}"


class LintReport(BaseModel):
    """All findings of one lint run."""

    findings: list[Finding] = Field(default_factory=list)

    @property
    def errors(self) -> list[Finding]:
        """Findings that block deployment."""
        return [f for f in self.findings if f.severity == "error"]

    @property
    def warnings(self) -> list[Finding]:
        """Findings that are reported but do not block."""
        return [f for f in self.findings if f.severity == "warning"]

    @property
    def ok(self) -> bool:
        """True when there is no error (warnings are allowed)."""
        return not self.errors

    def by_rule(self, rule: str) -> list[Finding]:
        """Findings of one rule."""
        return [f for f in self.findings if f.rule == rule]

    def summary(self) -> str:
        """One line: counts of errors and warnings with the rules involved."""
        rules = sorted({f.rule for f in self.findings})
        return f"{len(self.errors)} errors, {len(self.warnings)} warnings" + (f" ({', '.join(rules)})" if rules else "")

    def format(self, limit: int = 50) -> str:
        """Human-readable listing (errors first), truncated to ``limit`` lines."""
        ordered = sorted(self.findings, key=lambda f: (f.severity != "error", f.rule, f.path))
        lines = [str(f) for f in ordered[:limit]]
        if len(ordered) > limit:
            lines.append(f"... {len(ordered) - limit} more")
        return "\n".join(lines)


# --------------------------------------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------------------------------------


def _walk(obj: Any, path: str = "") -> Iterator[tuple[str, Any]]:
    """Yield ``(json path, value)`` for every node of a JSON structure, containers included."""
    yield path, obj
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield from _walk(value, f"{path}.{key}" if path else str(key))
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            yield from _walk(value, f"{path}[{index}]")


def _is_dict(value: Any) -> bool:
    return isinstance(value, dict)


def _func(tool: Any) -> dict[str, Any]:
    return tool.get("function") if _is_dict(tool) and _is_dict(tool.get("function")) else {}


def _leaf_default(leaf: Any) -> Any:
    return leaf.get("default") if _is_dict(leaf) else None


def _targets(node: dict[str, Any]) -> list[tuple[str, str, str]]:
    """``(kind, target, path suffix)`` of every outgoing edge of a node."""
    out: list[tuple[str, str, str]] = []
    for index, transition in enumerate(node.get("transitions") or []):
        if _is_dict(transition) and isinstance(transition.get("target_node"), str):
            out.append(("transition", transition["target_node"], f".transitions[{index}].target_node"))
    for key in ("passthrough_default_transition", "max_turn_transition"):
        if isinstance(node.get(key), str) and node[key]:
            out.append((key, node[key], f".{key}"))
    return out


class _Linter:
    def __init__(self, wf: dict[str, Any], tool_api: ToolApi, call_variables: set[str]) -> None:
        self.wf = wf
        self.api = tool_api
        self.call_variables = set(call_variables)
        self.findings: list[Finding] = []
        raw_nodes = wf.get("nodes") if _is_dict(wf.get("nodes")) else {}
        raw_globals = wf.get("global_nodes") if _is_dict(wf.get("global_nodes")) else {}
        self.nodes: dict[str, dict[str, Any]] = {k: v for k, v in raw_nodes.items() if _is_dict(v)}
        self.globals: dict[str, dict[str, Any]] = {k: v for k, v in raw_globals.items() if _is_dict(v)}
        self.all_nodes: dict[str, dict[str, Any]] = {**self.nodes, **self.globals}
        self.codes = extract_code_snippets(wf)

    # ---- plumbing --------------------------------------------------------------------------------------------------

    def add(self, rule: str, severity: Severity, path: str, message: str) -> None:
        self.findings.append(Finding(rule=rule, severity=severity, path=path, message=message))

    def err(self, rule: str, path: str, message: str) -> None:
        self.add(rule, "error", path, message)

    def warn(self, rule: str, path: str, message: str) -> None:
        self.add(rule, "warning", path, message)

    def node_path(self, name: str) -> str:
        return f"global_nodes.{name}" if name in self.globals and name not in self.nodes else f"nodes.{name}"

    def tool_entries(self, node: dict[str, Any]) -> Iterator[tuple[str, int, dict[str, Any]]]:
        for kind in ("tools", "pre_actions"):
            for index, tool in enumerate(node.get(kind) or []):
                if _is_dict(tool):
                    yield kind, index, tool

    def run(self) -> LintReport:
        for check in (
            self.l001, self.l002, self.l003, self.l004, self.l005_l006_l007, self.l008, self.l009, self.l010,
            self.l011, self.l012, self.l013, self.l014, self.l015, self.l016, self.l017, self.l018, self.l019,
            self.l020, self.l021, self.l022, self.l023, self.l024, self.l025, self.l026, self.l027, self.l028,
            self.l029,
        ):
            check()
        return LintReport(findings=self.findings)

    # ---- L001-L004 -------------------------------------------------------------------------------------------------

    def l001(self) -> None:
        for path, value in _walk(self.wf):
            if value is None:
                self.err("L001", path or "<root>", "null value (the platform schema rejects null; omit the key)")

    def l002(self) -> None:
        for name, node in self.all_nodes.items():
            if node.get("node_type") != "conversation" and node.get("tools"):
                self.err("L002", f"{self.node_path(name)}.tools",
                         f"'tools' on a {node.get('node_type')} node are stripped by the platform; use pre_actions "
                         "on an api_request/code node or move the tool to a conversation node")
        for index, tool in enumerate(self.wf.get("tools") or []):
            alwayss = ((tool.get("config") or {}) if _is_dict(tool) else {}).get("alwaysRunsAt")
            if not alwayss:
                self.warn("L002", f"tools[{index}]", "workflow-level tool without config.alwaysRunsAt never runs")

    def l003(self) -> None:
        for name, node in self.all_nodes.items():
            if node.get("node_type") in ("conversation", END_CALL) and node.get("pre_actions"):
                self.err("L003", f"{self.node_path(name)}.pre_actions",
                         f"'pre_actions' on a {node.get('node_type')} node are stripped; use an api_request/code node")

    def l004(self) -> None:
        for name, node in self.all_nodes.items():
            path = self.node_path(name)
            kind = node.get("node_type")
            if kind in ("api_request", "code") and not node.get("passthrough"):
                self.err("L004", path, f"{kind} node is not a passthrough node (it would wait for the user)")
            if not node.get("passthrough"):
                continue
            if not node.get("passthrough_default_transition"):
                self.err("L004", path, "passthrough node without passthrough_default_transition")
            transitions = node.get("transitions") or []
            last = transitions[-1] if transitions and _is_dict(transitions[-1]) else None
            condition = (last or {}).get("condition")
            if not (_is_dict(condition) and condition.get("operator") == "always"):
                self.err("L004", f"{path}.transitions", "a passthrough node must end with an 'always' transition")

    # ---- L005-L007 graph -------------------------------------------------------------------------------------------

    def l005_l006_l007(self) -> None:
        initial = self.wf.get("initial_node")
        if initial not in self.nodes:
            self.err("L007", "initial_node", f"initial_node {initial!r} is not a node")
        for name, node in self.all_nodes.items():
            is_global = name in self.globals and name not in self.nodes
            for kind, target, suffix in _targets(node):
                if target == PREVIOUS:
                    if not is_global:
                        self.err("L007", f"{self.node_path(name)}{suffix}", "'__previous__' is only valid in global nodes")
                elif target not in self.nodes:
                    self.err("L007", f"{self.node_path(name)}{suffix}", f"{kind} target {target!r} is not a node")
            for excluded in node.get("excluded_nodes") or []:
                if excluded not in self.nodes:
                    self.warn("L007", f"{self.node_path(name)}.excluded_nodes", f"excluded node {excluded!r} does not exist")
        edges: dict[str, set[str]] = {
            name: {t for _, t, _ in _targets(node) if t in self.nodes} for name, node in self.all_nodes.items()
        }
        roots = ([initial] if initial in self.nodes else []) + list(self.globals)
        seen: set[str] = set()
        queue: deque[str] = deque(roots)
        while queue:
            current = queue.popleft()
            if current in seen:
                continue
            seen.add(current)
            queue.extend(edges.get(current, ()))
        for name in self.nodes:
            if name not in seen:
                self.err("L005", self.node_path(name), "node is not reachable from initial_node")
        ends = {n for n, node in self.all_nodes.items() if node.get("node_type") == END_CALL}
        if not ends:
            self.err("L006", "nodes", "the workflow has no end_call node")
            return
        reverse: dict[str, set[str]] = {}
        for src, targets in edges.items():
            for target in targets:
                reverse.setdefault(target, set()).add(src)
        can_end: set[str] = set()
        queue = deque(ends)
        while queue:
            current = queue.popleft()
            if current in can_end:
                continue
            can_end.add(current)
            queue.extend(reverse.get(current, ()))
        for name in self.nodes:
            if name not in can_end:
                self.err("L006", self.node_path(name), "no path from this node to an end_call node")

    # ---- L008 code -------------------------------------------------------------------------------------------------

    def l008(self) -> None:
        for name, node in self.all_nodes.items():
            if node.get("node_type") == "code" and name not in self.codes:
                self.err("L008", self.node_path(name), "code node without a code_exec snippet")
        for key, code in self.codes.items():
            if "{{" in code or "}}" in code:
                self.err("L008", f"code[{key}]", "code contains '{{' or '}}' (the engine may treat it as a placeholder)")
            try:
                ast.parse(code)
            except SyntaxError as exc:
                self.err("L008", f"code[{key}]", f"code does not compile: {exc.msg} (line {exc.lineno})")
                continue
            for module in {m.split(".")[0] for m in _IMPORT.findall(code)}:
                if module not in _ALLOWED_IMPORTS:
                    self.warn("L008", f"code[{key}]", f"import of '{module}' (only json, re and datetime are documented)")
            is_hook = "." in key
            long_enough = len([line for line in code.splitlines() if line.strip()]) > 3
            if not is_hook and long_enough and "\ntry:" not in "\n" + code:
                self.warn("L008", f"code[{key}]", "code node snippet has no top-level try/except (it must never raise)")

    # ---- L009 response mapping -------------------------------------------------------------------------------------

    def l009(self) -> None:
        def check_list(path: str, mappings: Any) -> None:
            if not isinstance(mappings, list):
                self.err("L009", path, "response_mapping must be a list")
                return
            for index, mapping in enumerate(mappings):
                where = f"{path}[{index}]"
                if not _is_dict(mapping) or not isinstance(mapping.get("path"), str) or not isinstance(
                    mapping.get("variable"), str
                ):
                    self.err("L009", where, "mapping needs string 'path' and 'variable'")
                    continue
                if not mapping["path"].startswith("data."):
                    self.err("L009", where, f"mapping path {mapping['path']!r} must start with 'data.' (the result envelope)")
                if not _IDENT.match(mapping["variable"]):
                    self.err("L009", where, f"mapped variable name {mapping['variable']!r} is not an identifier")

        for name, node in self.all_nodes.items():
            base = self.node_path(name)
            if "response_mapping" in node:
                if node.get("node_type") == "conversation" and node["response_mapping"]:
                    self.warn("L009", f"{base}.response_mapping", "node-level response_mapping has no effect on a conversation node; map on the tool")
                check_list(f"{base}.response_mapping", node["response_mapping"])
            for kind, index, tool in self.tool_entries(node):
                if "response_mapping" in tool:
                    where = f"{base}.{kind}[{index}].response_mapping"
                    if kind == "pre_actions":
                        self.err("L009", where, "response_mapping inside a pre_actions entry is ignored; put it on the node")
                    check_list(where, tool["response_mapping"])

    # ---- L010 / L021 conditions ------------------------------------------------------------------------------------

    def conditions(self) -> Iterator[tuple[str, dict[str, Any], bool]]:
        """``(path, condition, top_level)`` for every condition of transitions and global nodes."""
        for name, node in self.all_nodes.items():
            base = self.node_path(name)
            for index, transition in enumerate(node.get("transitions") or []):
                if _is_dict(transition) and _is_dict(transition.get("condition")):
                    yield f"{base}.transitions[{index}].condition", transition["condition"], True
            if _is_dict(node.get("condition")):
                yield f"{base}.condition", node["condition"], True

    def l010(self) -> None:
        def visit(path: str, cond: dict[str, Any], in_group: bool) -> None:
            if isinstance(cond.get("conditions"), list):
                group = [c for c in cond["conditions"] if _is_dict(c)]
                spelled: dict[str, set[str]] = {}
                for item in group:
                    if item.get("operator") == "equals" and str(item.get("operand", "")).lower() in ("true", "false"):
                        spelled.setdefault(str(item.get("variable")), set()).add(str(item.get("operand")))
                for variable, values in spelled.items():
                    for flag in {v.lower() for v in values}:
                        if not {flag, flag.capitalize()} <= values:
                            self.err("L010", path, f"equals on boolean variable {variable!r} must test both "
                                     f"{flag!r} and {flag.capitalize()!r} in the same OR group")
                for index, item in enumerate(group):
                    visit(f"{path}.conditions[{index}]", item, True)
            elif not in_group and cond.get("operator") == "equals" and str(cond.get("operand", "")).lower() in (
                "true", "false"
            ):
                self.err("L010", path, f"bare equals on boolean variable {cond.get('variable')!r}; use an OR group "
                         "with both spellings")

        for path, cond, _ in self.conditions():
            visit(path, cond, False)

    # ---- L011 / L012 api_call shape and tool API ------------------------------------------------------------------

    def api_tools(self) -> Iterator[tuple[str, str, dict[str, Any], dict[str, Any]]]:
        """``(path, kind, tool, function)`` for each api_call function of nodes and the workflow."""
        for name, node in self.all_nodes.items():
            base = self.node_path(name)
            for kind, index, tool in self.tool_entries(node):
                function = _func(tool)
                if function.get("handler") == API_CALL:
                    yield f"{base}.{kind}[{index}]", kind, tool, function
        for index, tool in enumerate(self.wf.get("tools") or []):
            function = _func(tool)
            if function.get("handler") == API_CALL:
                yield f"tools[{index}]", "workflow", tool, function

    def l011(self) -> None:
        for path, kind, _tool, function in self.api_tools():
            params = function.get("parameters")
            if not _is_dict(params) or not _is_dict(params.get("properties")):
                self.err("L011", f"{path}.function.parameters", "api_call needs parameters.properties")
                continue
            props = params["properties"]
            for key in props:
                if key not in _API_PARAMS:
                    self.err("L011", f"{path}.function.parameters.properties.{key}",
                             f"'{key}' next to url/method/body: body fields must be leaves under body.properties")
            for key in ("url", "method"):
                if _leaf_default(props.get(key)) in (None, ""):
                    self.err("L011", f"{path}.function.parameters.properties.{key}", f"'{key}' needs a literal 'default'")
            for sub_path, value in _walk(params, f"{path}.function.parameters"):
                if _is_dict(value) and "const" in value:
                    self.err("L011", sub_path, "'const' does not bake a value (only 'default' does)")
            body = props.get("body")
            if body is None:
                continue
            if not _is_dict(body) or not _is_dict(body.get("properties")):
                self.err("L011", f"{path}.function.parameters.properties.body", "body needs 'properties' (one leaf per field)")
                continue
            if "default" in body:
                self.err("L011", f"{path}.function.parameters.properties.body",
                         "body 'default' template object: declare each field as a leaf under body.properties")
            for field, leaf in body["properties"].items():
                where = f"{path}.function.parameters.properties.body.properties.{field}"
                if not _is_dict(leaf):
                    self.err("L011", where, "body field must be an object (a leaf)")
                    continue
                if leaf.get("type") in ("object", "array") or "properties" in leaf:
                    self.err("L011", where, "body field is not a leaf (nested object/array)")
                if "default" not in leaf:
                    (self.err if kind != "tools" else self.warn)("L011", where, "body field without 'default'")
            for required in body.get("required") or []:
                if required not in body["properties"]:
                    self.err("L011", f"{path}.function.parameters.properties.body.required",
                             f"required field {required!r} is not a body property")

    def contract_for(self, function: dict[str, Any]) -> ToolContract | None:
        return self.api.tools.get(str(function.get("name")))

    def l012(self) -> None:
        for path, kind, _tool, function in self.api_tools():
            name = str(function.get("name"))
            contract = self.contract_for(function)
            if contract is None:
                self.err("L012", f"{path}.function.name", f"tool {name!r} is not in the tool API ({', '.join(self.api.tools)})")
                continue
            params = function.get("parameters") if _is_dict(function.get("parameters")) else {}
            props = params.get("properties") if _is_dict(params.get("properties")) else {}
            url = _leaf_default(props.get("url"))
            if url != contract.url:
                self.err("L012", f"{path}.function.parameters.properties.url", f"url {url!r} differs from the contract {contract.url!r}")
            method = _leaf_default(props.get("method"))
            if str(method or "").upper() != contract.method.upper():
                self.err("L012", f"{path}.function.parameters.properties.method", f"method {method!r} differs from the contract {contract.method!r}")
            body = props.get("body") if _is_dict(props.get("body")) else {}
            leaves = body.get("properties") if _is_dict(body.get("properties")) else {}
            for field, leaf in leaves.items():
                where = f"{path}.function.parameters.properties.body.properties.{field}"
                spec = contract.get_field(field, "request")
                if spec is None:
                    self.err("L012", where, f"field {field!r} is not a request field of {name}")
                    continue
                self.check_enum(where, spec, leaf, kind)
            for required in contract.required_fields:
                if required not in leaves:
                    self.err("L012", f"{path}.function.parameters.properties.body", f"required field {required!r} of {name} is missing")

    def check_enum(self, where: str, spec: FieldSpec, leaf: Any, kind: str) -> None:
        if not spec.enum or not _is_dict(leaf):
            return
        allowed = set(spec.enum)
        declared = leaf.get("enum")
        if isinstance(declared, list):
            extra = [v for v in declared if v not in allowed]
            if extra:
                self.err("L012", where, f"enum values {extra} are not in the contract's {sorted(allowed)}")
        elif kind == "tools" and not (isinstance(leaf.get("default"), str) and "{{" not in leaf["default"]):
            self.warn("L012", where, f"model-filled field has no enum; the contract allows {sorted(allowed)}")
        default = leaf.get("default")
        if isinstance(default, str) and "{{" not in default and default not in allowed:
            self.err("L012", where, f"baked value {default!r} is not in the contract's {sorted(allowed)}")

    # ---- L013 close_call --------------------------------------------------------------------------------------------

    def l013(self) -> None:
        if "close_call" not in self.api.tools:
            return
        hard: set[str] = set()
        soft: set[str] = set()
        for name, node in self.nodes.items():
            names = {_func(t).get("name") for _, _, t in self.tool_entries(node)}
            if "close_call" in names:
                (soft if node.get("node_type") == "conversation" else hard).add(name)
        lifecycle = [
            t for t in self.wf.get("tools") or []
            if _func(t).get("name") == "close_call" and "end_of_call" in ((t.get("config") or {}).get("alwaysRunsAt") or [])
        ]
        if lifecycle:
            return  # a lifecycle tool closes the call after it ended, on every path
        if not hard and not soft:
            self.err("L013", "nodes", "no node calls close_call")
            return
        initial = self.wf.get("initial_node")
        if initial not in self.nodes:
            return
        start = (initial, 0)
        seen = {start}
        queue: deque[tuple[str, int]] = deque([start])
        reported: set[str] = set()
        while queue:
            name, closed = queue.popleft()
            node = self.nodes[name]
            if name in hard:
                closed = min(2, closed + 1)
                if closed >= 2 and name not in reported:
                    reported.add(name)
                    self.err("L013", self.node_path(name), "close_call can run twice on a path through this node")
            elif name in soft:
                closed = max(closed, 1)
            if node.get("node_type") == END_CALL and closed == 0 and name not in reported:
                reported.add(name)
                self.err("L013", self.node_path(name), "a path reaches this end_call node without close_call")
            for _, target, _ in _targets(node):
                if target in self.nodes and (target, closed) not in seen:
                    seen.add((target, closed))
                    queue.append((target, closed))

    # ---- L014 variables ---------------------------------------------------------------------------------------------

    def known_variables(self) -> tuple[set[str], set[str], set[str], set[str]]:
        """``(exact names, dynamic suffixes, tool/node prefixes, tool names)`` that can be resolved."""
        exact = set(self.call_variables) | set(self.wf.get("test_variables") or {})
        suffixes: set[str] = set()
        prefixes: set[str] = set()
        tool_names: set[str] = set()
        for code in self.codes.values():
            exact.update(_VARS_ASSIGN.findall(code))
            suffixes.update(_VARS_DYNAMIC.findall(code))
        for name, node in self.all_nodes.items():
            prefixes.add(name)
            for field in node.get("extraction_schema") or []:
                if _is_dict(field) and isinstance(field.get("name"), str):
                    exact.add(field["name"])
            for mapping in node.get("response_mapping") or []:
                if _is_dict(mapping) and isinstance(mapping.get("variable"), str):
                    exact.add(mapping["variable"])
            for _, _, tool in self.tool_entries(node):
                tool_names.add(str(_func(tool).get("name")))
                for mapping in tool.get("response_mapping") or []:
                    if _is_dict(mapping) and isinstance(mapping.get("variable"), str):
                        exact.add(mapping["variable"])
        for tool in self.wf.get("tools") or []:
            tool_names.add(str(_func(tool).get("name")))
        return exact, suffixes, prefixes, tool_names

    def resolvable(self, ref: str, known: tuple[set[str], set[str], set[str], set[str]]) -> bool:
        exact, suffixes, prefixes, tool_names = known
        ref = ref.split(":")[0].strip()
        if not ref:
            return True
        if ref in exact or ref.startswith(_SYSTEM_PREFIXES):
            return True
        if any(ref.endswith(suffix) for suffix in suffixes):
            return True
        root = ref.split(".")[0]
        for tool in tool_names | prefixes:
            if root in (f"{tool}_response", f"{tool}_args") or ref in (
                f"{tool}_success_count", f"{tool}_failure_count", f"{tool}_status_code"
            ):
                return True
            if ref in (f"{tool}.current_turns", f"{tool}.total_turns"):
                return True
        return False

    def text_fields(self) -> Iterator[tuple[str, str, bool]]:
        """``(path, text, spoken)`` for every prompt / spoken string of the workflow."""
        if isinstance(self.wf.get("system_prompt"), str):
            yield "system_prompt", self.wf["system_prompt"], False
        for name, node in self.all_nodes.items():
            base = self.node_path(name)
            for key in ("task_message", "role_message", "loop_prompt", "routing_prompt", "loop_condition"):
                if isinstance(node.get(key), str):
                    yield f"{base}.{key}", node[key], False
            on_enter = node.get("on_enter")
            if _is_dict(on_enter):
                if isinstance(on_enter.get("text"), str):
                    yield f"{base}.on_enter.text", on_enter["text"], True
                if isinstance(on_enter.get("prompt"), str):
                    yield f"{base}.on_enter.prompt", on_enter["prompt"], False
            for index, transition in enumerate(node.get("transitions") or []):
                if _is_dict(transition):
                    if isinstance(transition.get("description"), str):
                        yield f"{base}.transitions[{index}].description", transition["description"], False
                    cond = transition.get("condition")
                    for sub_path, value in _walk(cond, f"{base}.transitions[{index}].condition"):
                        if _is_dict(value) and isinstance(value.get("description"), str):
                            yield f"{sub_path}.description", value["description"], False
            terminal = node.get("terminal")
            if _is_dict(terminal):
                spoken = ((((_func(terminal).get("parameters") or {}).get("properties") or {}).get("spoken_message")) or {})
                if isinstance(spoken.get("default"), str):
                    yield f"{base}.terminal.spoken_message", spoken["default"], True

    def l014(self) -> None:
        known = self.known_variables()
        for path, text, _spoken in self.text_fields():
            for ref in _REF.findall(text):
                if not self.resolvable(ref, known):
                    self.err("L014", path, f"{{{{{ref}}}}} cannot be resolved (not a call variable, mapped, extracted or written by code)")
        for _path, kind, _tool, function in self.api_tools():
            props = ((function.get("parameters") or {}).get("properties") or {})
            leaves = dict((props.get("body") or {}).get("properties") or {})
            for key in ("url", "method"):
                leaves[key] = props.get(key)
            for header, spec in (((props.get("headers") or {}).get("properties")) or {}).items():
                leaves[f"header:{header}"] = spec
            for field, leaf in leaves.items():
                default = _leaf_default(leaf)
                if not isinstance(default, str):
                    continue
                for ref in _REF.findall(default):
                    model_filled = kind in ("tools", "workflow") and ref == field
                    if not model_filled and not self.resolvable(ref, known):
                        self.err("L014", f"{_path}.{field}", f"{{{{{ref}}}}} in a baked default cannot be resolved")
        for cond_path, cond, _ in self.conditions():
            for sub_path, value in _walk(cond, cond_path):
                if _is_dict(value) and isinstance(value.get("variable"), str) and not self.resolvable(value["variable"], known):
                    self.err("L014", sub_path, f"condition variable {value['variable']!r} cannot be resolved")

    # ---- L015-L019 --------------------------------------------------------------------------------------------------

    def l015(self) -> None:
        for name, node in self.all_nodes.items():
            for index, field in enumerate(node.get("extraction_schema") or []):
                if _is_dict(field) and field.get("min_confidence") is None:
                    self.err("L015", f"{self.node_path(name)}.extraction_schema[{index}]",
                             f"extraction field {field.get('name')!r} has no min_confidence (default 0.95 discards most values)")

    def l016(self) -> None:
        for name, node in self.all_nodes.items():
            if node.get("node_type") != END_CALL:
                continue
            base = self.node_path(name)
            on_enter = node.get("on_enter")
            if not (_is_dict(on_enter) and on_enter.get("type") == "silent"):
                self.err("L016", f"{base}.on_enter", "end_call on_enter must be {'type': 'silent'} (the farewell comes from the terminal)")
            if node.get("transitions"):
                self.err("L016", f"{base}.transitions", "end_call nodes have no transitions")
            if node.get("disable_inactivity_monitor") is not True:
                self.err("L016", f"{base}.disable_inactivity_monitor", "set disable_inactivity_monitor to true")
            terminal = node.get("terminal")
            function = _func(terminal) if _is_dict(terminal) else {}
            if function.get("handler") != END_HANDLER:
                self.err("L016", f"{base}.terminal", f"terminal handler must be {END_HANDLER}")

    def l017(self) -> None:
        for name, node in self.all_nodes.items():
            if node.get("max_node_turns") is not None and not node.get("max_turn_transition"):
                self.err("L017", self.node_path(name), "max_node_turns without max_turn_transition (the cap changes nothing)")

    def l018(self) -> None:
        for path, text, _ in self.text_fields():
            if (path.endswith(".task_message") or path == "system_prompt") and len(text) > MAX_PROMPT_CHARS:
                self.warn("L018", path, f"{len(text)} characters (> {MAX_PROMPT_CHARS}); long prompts dilute the rules")

    def l019(self) -> None:
        if "agent_config" in self.wf:
            self.err("L019", "agent_config", "omit agent_config (the platform fills in defaults)")

    # ---- L020-L027 --------------------------------------------------------------------------------------------------

    def l020(self) -> None:
        for name, node in self.all_nodes.items():
            if node.get("node_type") != "conversation":
                continue
            for index, tool in enumerate(node.get("tools") or []):
                function = _func(tool)
                if function.get("handler") == END_HANDLER:
                    continue
                description = str(function.get("description") or "")
                if "call" not in description.lower():
                    self.warn("L020", f"{self.node_path(name)}.tools[{index}]",
                              f"tool {function.get('name')!r}: the description should say when to CALL it")

    def l021(self) -> None:
        for path, cond, top in self.conditions():
            if not top:
                continue
            if "logic" in cond:
                if cond.get("logic") != "or":
                    self.err("L021", path, "the top-level group of a condition must use logic 'or'")
            elif "variable" in cond:
                self.warn("L021", path, "bare variable condition; the kit's builder wraps it in an OR group")
        for name, node in self.all_nodes.items():
            seen_intent = False
            for index, transition in enumerate(node.get("transitions") or []):
                cond = transition.get("condition") if _is_dict(transition) else None
                if not _is_dict(cond):
                    continue
                semantic = cond.get("operator") == "matches_intent" and not transition.get("is_tool_call")
                if semantic:
                    seen_intent = True
                elif seen_intent and not transition.get("is_tool_call") and cond.get("operator") != "always":
                    self.warn("L021", f"{self.node_path(name)}.transitions[{index}]",
                              "deterministic transition listed after a matches_intent one (put deterministic first)")

    def l022(self) -> None:
        for path, text, spoken in self.text_fields():
            if spoken:
                for name in _SINGLE_BRACE.findall(text):
                    self.warn("L022", path, f"{{{name}}} looks like an unresolved IR placeholder in spoken text")

    def l023(self) -> None:
        for name, node in self.all_nodes.items():
            if node.get("node_type") == "conversation":
                continue
            for index, transition in enumerate(node.get("transitions") or []):
                if _is_dict(transition) and transition.get("is_tool_call"):
                    self.err("L023", f"{self.node_path(name)}.transitions[{index}]", "is_tool_call on a non-conversation node never fires")

    def l024(self) -> None:
        for name, node in self.all_nodes.items():
            if node.get("name") is not None and node.get("name") != name:
                self.warn("L024", self.node_path(name), f"node name {node.get('name')!r} differs from its key")

    def l025(self) -> None:
        for name, node in self.all_nodes.items():
            base = self.node_path(name)
            for index, transition in enumerate(node.get("transitions") or []):
                if not _is_dict(transition) or not transition.get("target_node"):
                    self.err("L025", f"{base}.transitions[{index}]", "transition without target_node")
                    continue
                for sub_path, value in _walk(transition.get("condition"), f"{base}.transitions[{index}].condition"):
                    if _is_dict(value) and value.get("operator") == "matches_intent" and not str(value.get("description") or "").strip():
                        self.err("L025", sub_path, "matches_intent needs a description (the routing model reads it)")

    def l026(self) -> None:
        for name, node in self.all_nodes.items():
            names = [str(_func(t).get("name")) for t in node.get("tools") or []]
            for duplicate in sorted({n for n in names if names.count(n) > 1}):
                self.warn("L026", f"{self.node_path(name)}.tools", f"tool {duplicate!r} listed twice")

    def l027(self) -> None:
        for name, node in self.all_nodes.items():
            on_enter = node.get("on_enter")
            if _is_dict(on_enter) and on_enter.get("type") == "tts" and not str(on_enter.get("text") or "").strip():
                self.err("L027", f"{self.node_path(name)}.on_enter", "tts entry without text")


    # ---- L028-L029 ---------------------------------------------------------------------------------------------------

    def l028(self) -> None:
        for name, node in self.all_nodes.items():
            first: dict[str, int] = {}
            for index, transition in enumerate(node.get("transitions") or []):
                target = transition.get("target_node") if _is_dict(transition) else None
                if not isinstance(target, str):
                    continue
                if target in first:
                    self.err("L028", f"{self.node_path(name)}.transitions",
                             f"transitions {first[target]} and {index} both lead to {target!r}: the engine merges them into "
                             "one transition (the OR of their conditions), so a tool-call exit or a condition is lost; "
                             "give the second one a node of its own")
                else:
                    first[target] = index

    def response_problem(self, ref: str) -> str | None:
        """Why ``<tool>_response.data.<field>`` cannot work (the tool does not return that field), else ``None``."""
        match = _RESPONSE_REF.match(ref.split(":")[0].strip())
        if match is None:
            return None
        contract = self.api.tools.get(match.group(1))
        if contract is None or not contract.response_fields or contract.get_field(match.group(2), "response") is not None:
            return None
        known = ", ".join(spec.name for spec in contract.response_fields)
        return f"tool {match.group(1)!r} returns no field {match.group(2)!r} (it returns: {known})"

    def l029(self) -> None:
        for key, code in self.codes.items():
            if not key.endswith(".post_process_code") or "/" not in key:
                continue
            contract = self.api.tools.get(key.split("/", 1)[1][: -len(".post_process_code")])
            if contract is None or not contract.response_fields:
                continue
            try:
                tree = ast.parse(code)
            except SyntaxError:
                continue
            read = {name for name in _hook_reads(tree) if name != "what_to_do_next"}
            for name in sorted(read):
                if contract.get_field(name, "response") is None:
                    known = ", ".join(spec.name for spec in contract.response_fields)
                    self.err("L029", f"code[{key}]",
                             f"the post-hook reads response field {name!r} that tool {contract.name!r} does not return "
                             f"(it returns: {known}); the rule can never match")
        for cond_path, cond, _ in self.conditions():
            for sub_path, value in _walk(cond, cond_path):
                if _is_dict(value) and isinstance(value.get("variable"), str):
                    problem = self.response_problem(value["variable"])
                    if problem:
                        self.err("L029", sub_path, f"condition variable {value['variable']!r}: {problem}")
        for path, text, _spoken in self.text_fields():
            for ref in _REF.findall(text):
                problem = self.response_problem(ref)
                if problem:
                    self.err("L029", path, f"{{{{{ref}}}}}: {problem}")


def _hook_reads(tree: ast.AST) -> set[str]:
    """Names a post-hook reads from the response data ``d`` (``d.get('x')`` and ``d['x']``)."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get"
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "d" and node.args
                and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
            found.add(node.args[0].value)
        elif (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id == "d"
              and isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str)):
            found.add(node.slice.value)
    return found


def lint_workflow(wf: dict[str, Any], tool_api: ToolApi, call_variables: set[str]) -> LintReport:
    """Run every rule on a workflow dict.

    Args:
        wf: The workflow (the value of the REST ``workflow`` key).
        tool_api: Contracts of the bank's tools (URLs, request fields, enums).
        call_variables: Names of the variables every call starts with (``customer_id``, ``now`` ...).

    Returns:
        The :class:`LintReport`; ``report.ok`` is True when there is no error.
    """
    return _Linter(wf, tool_api, call_variables).run()
