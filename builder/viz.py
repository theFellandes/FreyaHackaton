"""Mermaid diagrams of the two graphs in this project: the builder (LangGraph) and a generated phone agent (Freya workflow).

Two plain-text outputs, both ``flowchart`` sources that GitHub, VS Code and the Mermaid live editor render:

* :func:`builder_mermaid` - the pipeline that ``builder/build.py`` runs. The edges come from the REAL compiled graph
  (``build_graph().get_graph().draw_mermaid()``), so the picture cannot drift from the code. The post-processing is
  cosmetic: node roles get colours, a few edges get a label, and the failure exits are either drawn pale (view ``full``)
  or folded into one note (view ``compact``).
* :func:`workflow_mermaid` - a Freya workflow JSON (a compiled one, a hand-written one or an export) as a flowchart:
  one shape per ``node_type``, one label per transition (deterministic condition, tool call, always, intent). View
  ``compact`` folds families of sibling nodes (the many ``end_*`` markers) into one box so the picture stays readable;
  view ``full`` draws every node and edge.

Command line (output goes to stdout, or to ``--out``)::

    python -m builder.viz builder [--view compact|full] [--out docs/graphs/builder_graph.mmd]
    python -m builder.viz agent out/brief-a.workflow.json [--view compact|full] [--out docs/graphs/agent_brief_a.mmd]
    python -m builder.viz sync [--check]      # copy docs/graphs/*.mmd into the marked blocks of the docs

Pure functions, no network and no model call. See ``docs/graphs/README.md``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# ------------------------------------------------------------------------------------------------ shared helpers

#: Words Mermaid treats as syntax; a node id equal to one of them breaks the diagram.
_RESERVED_IDS = frozenset(
    {"end", "graph", "flowchart", "subgraph", "style", "class", "classdef", "click", "linkstyle", "default", "direction"}
)
DIRECTIONS = ("TD", "LR", "TB", "BT", "RL")
VIEWS = ("compact", "full")


def _escape(text: str) -> str:
    """Make ``text`` safe inside a double-quoted Mermaid label (``&``, quotes and ``<`` become entities)."""
    return (
        text.replace("&", "#amp;")
        .replace('"', "#quot;")
        .replace("<", "#lt;")
        .replace("\r", " ")
        .replace("\n", " ")
    )


def _clip(text: str, limit: int) -> str:
    """Shorten ``text`` to ``limit`` characters at a word boundary when that loses little, marking the cut with dots."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[: max(1, limit - 3)]
    space = cut.rfind(" ")
    if space >= limit // 2:
        cut = cut[:space]
    return cut.rstrip(" ,;:") + "..."


def _check_options(view: str, direction: str) -> None:
    if view not in VIEWS:
        raise ValueError(f"view must be one of {VIEWS}, not {view!r}")
    if direction not in DIRECTIONS:
        raise ValueError(f"direction must be one of {DIRECTIONS}, not {direction!r}")


class _Ids:
    """Maps arbitrary node keys to unique, Mermaid-safe identifiers."""

    def __init__(self) -> None:
        self._by_key: dict[str, str] = {}
        self._used: set[str] = set()

    def __call__(self, key: str, preferred: str | None = None) -> str:
        if key in self._by_key:
            return self._by_key[key]
        base = re.sub(r"[^A-Za-z0-9_]", "_", preferred or key) or "node"
        if base.lower() in _RESERVED_IDS or base[0].isdigit():
            base = f"n_{base}"
        candidate, counter = base, 2
        while candidate in self._used:
            candidate, counter = f"{base}_{counter}", counter + 1
        self._used.add(candidate)
        self._by_key[key] = candidate
        return candidate


# ------------------------------------------------------------------------------------------------ builder graph

#: Role of every node of the builder graph: it only picks the colour (see ``docs/graphs/README.md``).
NODE_ROLES: dict[str, str] = {
    "load_inputs": "plain",
    "guard_inputs": "check",
    "understand_brief": "llm",
    "extract_policy": "llm",
    "validate_policy": "check",
    "repair_policy": "repair",
    "guard_policy": "check",
    "draft_prose": "llm",
    "compile_workflow": "build",
    "static_checks": "check",
    "repair_compile": "repair",
    "guard_workflow": "check",
    "remote_validate": "platform",
    "deploy": "platform",
    "self_test": "platform",
    "decide": "platform",
    "agentic_build": "fallback",
    "finalize": "plain",
}

#: Short edge labels, keyed by ``(source, target)``. Cosmetic: an edge missing here is still drawn, unlabelled. A test
#: checks that every key is an edge of the real graph, so a renamed or removed edge cannot leave a stale label behind.
EDGE_LABELS: dict[tuple[str, str], str] = {
    ("guard_inputs", "understand_brief"): "ok",
    ("guard_inputs", "agentic_build"): "track agentic",
    ("validate_policy", "repair_policy"): "errors",
    ("validate_policy", "guard_policy"): "no errors",
    ("repair_policy", "validate_policy"): "re-validate",
    ("guard_policy", "draft_prose"): "ok",
    ("guard_policy", "repair_policy"): "blocked",
    ("compile_workflow", "static_checks"): "ok",
    ("compile_workflow", "repair_compile"): "compile error",
    ("static_checks", "guard_workflow"): "lint clean",
    ("static_checks", "repair_compile"): "lint errors",
    ("repair_compile", "compile_workflow"): "recompile",
    ("guard_workflow", "remote_validate"): "ok",
    ("guard_workflow", "repair_compile"): "blocked",
    ("guard_workflow", "finalize"): "dry run",
    ("remote_validate", "deploy"): "accepted",
    ("remote_validate", "repair_compile"): "rejected",
    ("deploy", "self_test"): "deployed",
    ("deploy", "repair_compile"): "content rejected",
    ("self_test", "decide"): "results",
    ("decide", "repair_policy"): "failing audit",
    ("decide", "finalize"): "done",
    ("agentic_build", "static_checks"): "workflow written",
    ("agentic_build", "agentic_build"): "retry",
}

#: Edges that leave the pipeline but stay in the ``compact`` view (every other edge into the exit targets is folded away).
_KEPT_EXITS: frozenset[tuple[str, str]] = frozenset(
    {("guard_inputs", "agentic_build"), ("guard_workflow", "finalize"), ("decide", "finalize")}
)
#: Targets of the failure exits: a failing or blocked step ends in ``finalize``, compiled-track failures try ``agentic_build``.
_EXIT_TARGETS = ("finalize", "agentic_build")

_BUILDER_CLASSES = (
    ("llm", "fill:#e8f1ff,stroke:#2f6fdb,color:#10264d"),
    ("check", "fill:#fff4d6,stroke:#b7791f,color:#4a3200"),
    ("repair", "fill:#fde8e8,stroke:#c53030,color:#4a1010"),
    ("build", "fill:#e6f6ec,stroke:#2f855a,color:#12351f"),
    ("platform", "fill:#efe8ff,stroke:#6b46c1,color:#2a1a52"),
    ("fallback", "fill:#fff,stroke:#6b7280,stroke-dasharray:4 3,color:#1f2937"),
    ("plain", "fill:#f1f2f4,stroke:#6b7280,color:#1f2937"),
)

_NODE_LINE = re.compile(r"^\t([A-Za-z_][A-Za-z0-9_]*)[\(\[]")
_EDGE_LINE = re.compile(r"^\t([A-Za-z_][A-Za-z0-9_]*) (-->|-\.->) ([A-Za-z_][A-Za-z0-9_]*);$")


def parse_langgraph_mermaid(raw: str) -> tuple[list[str], list[tuple[str, str, bool]]]:
    """Read the nodes and edges out of ``draw_mermaid()`` text.

    Returns:
        ``(nodes, edges)``: the node ids in declaration order, and ``(source, target, conditional)`` per edge, where
        ``conditional`` is True for the dotted arrows LangGraph uses for conditional edges.

    Raises:
        ValueError: when no node or no edge can be read (LangGraph changed its output format).
    """
    nodes: list[str] = []
    edges: list[tuple[str, str, bool]] = []
    for line in raw.splitlines():
        edge = _EDGE_LINE.match(line)
        if edge:
            edges.append((edge.group(1), edge.group(3), edge.group(2) == "-.->"))
            continue
        node = _NODE_LINE.match(line)
        if node and node.group(1) not in nodes:
            nodes.append(node.group(1))
    if not nodes or not edges:
        raise ValueError("could not read nodes and edges from the LangGraph Mermaid output")
    return nodes, edges


def _raw_builder_mermaid(graph: Any = None) -> str:
    if graph is None:
        # imported late: drawing a workflow file needs no LangGraph
        from builder.graph.build_graph import build_graph

        graph = build_graph()
    return graph.get_graph().draw_mermaid()


def builder_edges(graph: Any = None) -> list[tuple[str, str, bool]]:
    """Edges of the real compiled builder graph, read from LangGraph's own Mermaid output."""
    return parse_langgraph_mermaid(_raw_builder_mermaid(graph))[1]


def builder_mermaid(view: str = "compact", *, direction: str = "TD", graph: Any = None) -> str:
    """Mermaid source of the builder pipeline, built from the real compiled LangGraph.

    Solid arrows are forward progress; dotted arrows are repair loops, loops back, the agentic fallback and the failure
    exits. Colours show what a node does (see :data:`NODE_ROLES`).

    Args:
        view: ``full`` draws every edge of the graph (the failure exits to ``finalize`` and ``agentic_build`` pale);
            ``compact`` leaves those exits out and adds one note node that stands for them.
        direction: Mermaid flow direction (``TD`` top-down by default, ``LR`` left-right).
        graph: A compiled graph to draw instead of ``build_graph()`` (tests).
    """
    _check_options(view, direction)
    nodes, edges = parse_langgraph_mermaid(_raw_builder_mermaid(graph))
    order = {name: index for index, name in enumerate(nodes)}
    edges = sorted(edges, key=lambda e: (order[e[0]], order[e[1]]))

    def folded_exit(source: str, target: str) -> bool:
        return target in _EXIT_TARGETS and source != target and (source, target) not in _KEPT_EXITS

    if view == "compact":
        edges = [e for e in edges if not folded_exit(e[0], e[1])]

    # nodes are declared by their first edge (a plain box); only the shapes that differ are declared up front
    lines = [f"flowchart {direction}", '    __start__(["START"])', '    __end__(["END"])']
    if view == "compact":
        lines.append('    exits[/"any step that fails or is blocked"/]')

    pale: list[int] = []
    for number, (source, target, _conditional) in enumerate(edges):
        loops_back = order[target] <= order[source] or NODE_ROLES.get(target) in ("repair", "fallback")
        label = EDGE_LABELS.get((source, target))
        text = "-.->" if loops_back or folded_exit(source, target) else "-->"
        text += f'|"{_escape(label)}"|' if label else ""
        lines.append(f"    {source} {text} {target}")
        if view == "full" and folded_exit(source, target):
            pale.append(number)
    if view == "compact":
        lines.append('    exits -.->|"finalize"| finalize')
        lines.append('    exits -.->|"compiled track: fallback"| agentic_build')
    if pale:
        lines.append(f"    linkStyle {','.join(map(str, pale))} stroke:#b4b8c0,stroke-width:1px")
    members: dict[str, list[str]] = {role: [] for role, _style in _BUILDER_CLASSES}
    for name in nodes:
        members[NODE_ROLES.get(name, "plain")].append(name)  # __start__ / __end__ are not roles: plain
    if view == "compact":
        members["fallback"].append("exits")
    for role, style in _BUILDER_CLASSES:
        if members[role]:
            lines.append(f"    classDef {role} {style}")
            lines.append(f"    class {','.join(members[role])} {role}")
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------------------------------------ Freya workflow

_OPERATORS = {
    "equals": "=",
    "not_equals": "!=",
    "greater_than": ">",
    "less_than": "<",
    "greater_than_or_equal": ">=",
    "less_than_or_equal": "<=",
    "contains": "contains",
    "is_set": "is set",
    "is_not_set": "is not set",
}
_AGENT_CLASSES = (
    ("conv", "fill:#e8f1ff,stroke:#2f6fdb,color:#10264d"),
    ("say", "fill:#e8f1ff,stroke:#2f6fdb,stroke-dasharray:4 3,color:#10264d"),
    ("code", "fill:#fff4d6,stroke:#b7791f,color:#4a3200"),
    ("api", "fill:#e6f6ec,stroke:#2f855a,color:#12351f"),
    ("term", "fill:#fde8e8,stroke:#c53030,color:#4a1010"),
    ("glob", "fill:#efe8ff,stroke:#6b46c1,color:#2a1a52"),
    ("plain", "fill:#f1f2f4,stroke:#6b7280,color:#1f2937"),
)
_LABEL_LIMIT = 64
#: Sibling nodes are folded into one box in the ``compact`` view when at least this many share a name prefix and a shape.
MIN_GROUP_SIZE = 3


def unwrap_workflow(data: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return the workflow definition from either the bare definition or a record that holds it under ``workflow``."""
    if "nodes" not in data and isinstance(data.get("workflow"), Mapping):
        return data["workflow"]
    return data


def condition_label(condition: Mapping[str, Any] | None) -> str:
    """Short text for a transition condition: ``always``, ``a = b``, ``a = b or c > 0``, ``intent: ...``."""
    if not isinstance(condition, Mapping) or not condition:
        return ""
    operator = str(condition.get("operator", ""))
    if operator == "always":
        return "always"
    if operator == "matches_intent":
        return "intent: " + str(condition.get("description", "")).strip()
    if "conditions" in condition:
        joiner = f" {str(condition.get('logic', 'or')).lower()} "
        parts: list[str] = []
        for child in condition.get("conditions") or []:
            part = condition_label(child) if isinstance(child, Mapping) else ""
            if part and part.lower() not in (p.lower() for p in parts):  # 'true' / 'True' pairs read as one test
                parts.append(part)
        return joiner.join(f"({p})" if (" or " in p or " and " in p) and len(parts) > 1 else p for p in parts)
    variable = str(condition.get("variable", "")).strip()
    symbol = _OPERATORS.get(operator, operator)
    if "operand" in condition and operator not in ("is_set", "is_not_set"):
        return f"{variable} {symbol} {condition['operand']}"
    return f"{variable} {symbol}".strip()


def _function_names(items: Any, *, skip: Sequence[str] = ()) -> list[str]:
    """Function names of a ``tools`` or ``pre_actions`` list (entries are ``{"function": {"name": ...}}``)."""
    names: list[str] = []
    for item in items or []:
        function = item.get("function") if isinstance(item, Mapping) else None
        name = function.get("name") if isinstance(function, Mapping) else None
        if isinstance(name, str) and name not in skip and name not in names:
            names.append(name)
    return names


def _node_text(key: str, node: Mapping[str, Any], *, count: int = 0) -> str:
    """Label of a node: its key, plus the tools it can call (conversation) or the API it calls (api_request)."""
    kind = node.get("node_type")
    if kind == "conversation":
        extra = _function_names(node.get("tools"))
    elif kind == "api_request":
        extra = _function_names(node.get("pre_actions"), skip=("code_exec",))
    else:
        extra = []
    text = _escape(key)
    if count:
        text += f"<br/><i>{count} nodes</i>"
    elif extra:
        text += "<br/><i>" + _escape(_clip(", ".join(extra), 44)) + "</i>"
    return text


def _node_shape(ident: str, text: str, node: Mapping[str, Any]) -> tuple[str, str]:
    """Mermaid declaration and class name for one workflow node (one shape per ``node_type``)."""
    kind = node.get("node_type")
    if kind == "conversation":
        return f'{ident}("{text}")', ("say" if node.get("passthrough") else "conv")
    if kind == "code":
        return f'{ident}["{text}"]', "code"
    if kind == "api_request":
        return f'{ident}{{{{"{text}"}}}}', "api"
    if kind == "end_call":
        return f'{ident}(["{text}"])', "term"
    return f'{ident}["{text}"]', "plain"


@dataclass(frozen=True)
class _Edge:
    """One drawn arrow: ``dashed`` = passthrough / max-turn, ``tool`` = a tool-call transition."""

    source: str
    target: str
    label: str
    dashed: bool = False
    tool: bool = False


def _node_edges(key: str, node: Mapping[str, Any]) -> list[_Edge]:
    """Every arrow that leaves ``key``: its transitions, its passthrough default and its max-turn target."""
    passthrough = bool(node.get("passthrough"))
    default = node.get("passthrough_default_transition")
    covered = False
    out: list[_Edge] = []
    for transition in node.get("transitions") or []:
        target = transition.get("target_node")
        if not isinstance(target, str):
            continue
        if transition.get("is_tool_call"):
            out.append(_Edge(key, target, f"tool:transition_to_{target}", tool=True))
            continue
        label = condition_label(transition.get("condition"))
        covered = covered or target == default
        out.append(_Edge(key, target, label, dashed=passthrough and label == "always"))
    if passthrough and isinstance(default, str) and default and not covered:
        out.append(_Edge(key, default, "passthrough default", dashed=True))
    max_turn = node.get("max_turn_transition")
    if isinstance(max_turn, str) and max_turn:
        out.append(_Edge(key, max_turn, "max turns", dashed=True))
    return out


def _sibling_groups(nodes: Mapping[str, Mapping[str, Any]], initial: str) -> dict[str, str]:
    """Fold families of look-alike nodes: ``{member key: group label}`` (``end_*`` for ``end_a``, ``end_b``, ...).

    Members share the part of the key before the first underscore, the ``node_type`` (code or api_request) and the exact
    set of nodes they lead to; at least :data:`MIN_GROUP_SIZE` of them are needed. The initial node never joins a group.
    """
    buckets: dict[tuple[str, str, frozenset[str]], list[str]] = {}
    for key, node in nodes.items():
        kind = node.get("node_type")
        if key == initial or kind not in ("code", "api_request") or "_" not in key:
            continue
        targets = frozenset(edge.target for edge in _node_edges(key, node))
        if targets:
            buckets.setdefault((key.split("_", 1)[0], str(kind), targets), []).append(key)
    return {
        key: f"{prefix}_*"
        for (prefix, _kind, _targets), keys in buckets.items()
        if len(keys) >= MIN_GROUP_SIZE
        for key in keys
    }


def _merge_edges(edges: list[_Edge]) -> list[_Edge]:
    """Merge arrows that now join the same two boxes (after folding) into one arrow with a combined label."""
    buckets: dict[tuple[str, str], list[_Edge]] = {}
    for edge in edges:
        buckets.setdefault((edge.source, edge.target), []).append(edge)
    merged: list[_Edge] = []
    for (source, target), bucket in buckets.items():
        tools = [e for e in bucket if e.tool]
        others: list[str] = []
        for e in bucket:
            if not e.tool and e.label and e.label not in others:
                others.append(e.label)
        parts = []
        if len(tools) > 1:
            parts.append(f"tool:transition_to_{target} x{len(tools)}")  # e.g. tool:transition_to_end_* x6
        elif tools:
            parts.append(tools[0].label)
        parts += others
        merged.append(
            _Edge(
                source,
                target,
                "; ".join(parts),
                dashed=all(e.dashed for e in bucket),
                tool=bool(tools) and not others,
            )
        )
    return merged


def workflow_mermaid(workflow: Mapping[str, Any], *, direction: str = "TD", view: str = "compact") -> str:
    """Mermaid flowchart of a Freya workflow.

    Shapes: conversation = rounded box (dashed outline when it only speaks a fixed line and moves on), code =
    rectangle, api_request = hexagon, end_call = stadium. Edge labels: the deterministic condition (``var = value``),
    ``tool:transition_to_<node>`` for a tool-call transition (blue), ``always``, or ``intent: ...`` for a model-judged
    one. Dashed edges are the passthrough edges (the ``always`` / ``passthrough_default_transition`` of a node that routes
    at once) and the ``max_turn_transition`` edges.

    Args:
        workflow: The workflow definition, or a record holding it under ``workflow`` (an export).
        direction: Mermaid flow direction.
        view: ``full`` draws every node and edge. ``compact`` folds each family of sibling code or api_request nodes
            (see :func:`_sibling_groups`) into one box and merges the arrows that lead into it.

    Raises:
        ValueError: when the workflow has no nodes, or ``view`` / ``direction`` is unknown.
    """
    _check_options(view, direction)
    wf = unwrap_workflow(workflow)
    nodes: Mapping[str, Mapping[str, Any]] = wf.get("nodes") or {}
    if not nodes:
        raise ValueError("the workflow has no nodes")
    global_nodes: Mapping[str, Mapping[str, Any]] = wf.get("global_nodes") or {}
    initial = wf.get("initial_node") if wf.get("initial_node") in nodes else next(iter(nodes))
    groups = _sibling_groups(nodes, initial) if view == "compact" else {}

    ids = _Ids()
    start_id, anywhere_id = ids("\0start"), ids("\0anywhere")
    for label in sorted(set(groups.values())):
        ids(label, f"grp_{label[:-2]}")  # the folded family ``end_*`` is drawn with the id ``grp_end``

    def box(key: str) -> str:
        return groups.get(key, key)

    sizes: dict[str, int] = {}
    for label in groups.values():
        sizes[label] = sizes.get(label, 0) + 1
    declarations: list[tuple[str, str, str]] = []  # (id, Mermaid declaration, label text)
    classes: dict[str, list[str]] = {}
    declared: set[str] = set()
    for key, node in nodes.items():
        drawn = box(key)
        if drawn in declared:  # the other members of a folded family
            continue
        declared.add(drawn)
        text = _node_text(drawn, node, count=sizes.get(drawn, 0))
        shape, css = _node_shape(ids(drawn), text, node)
        declarations.append((ids(drawn), shape, text))
        classes.setdefault(css, []).append(ids(drawn))

    edges = [_Edge("\0start", initial, "")]
    for key, node in nodes.items():
        edges += _node_edges(key, node)
    if groups:
        edges = _merge_edges(
            [_Edge(box(e.source), box(e.target), e.label, e.dashed, e.tool) for e in edges]
        )

    subgraph: list[str] = []
    if global_nodes:
        subgraph.append('    subgraph global_nodes ["Global nodes: can fire from any node"]')
        for key, node in global_nodes.items():
            shape, _css = _node_shape(ids(key), _node_text(key, node), node)
            subgraph.append(f"        {shape}")
            classes.setdefault("glob", []).append(ids(key))
        subgraph.append("    end")
        for key, node in global_nodes.items():
            edges.append(_Edge("\0anywhere", key, condition_label(node.get("condition")) or "global", dashed=True))
            edges += _node_edges(key, node)

    known = {box(k) for k in nodes} | set(global_nodes) | {"\0start", "\0anywhere"}
    missing: dict[str, str] = {}  # __previous__ or a typo: draw it, so the picture shows what the workflow points at
    for edge in edges:
        if edge.target not in known and edge.target not in missing:
            missing[edge.target] = ids(f"missing:{edge.target}")

    arrows: list[str] = []
    tool_links: list[int] = []
    for number, edge in enumerate(edges):
        arrow = "-.->" if edge.dashed else "-->"
        label = f'|"{_escape(_clip(edge.label, _LABEL_LIMIT))}"|' if edge.label else ""
        arrows.append(f"    {ids(edge.source)} {arrow}{label} {missing.get(edge.target) or ids(edge.target)}")
        if edge.tool:
            tool_links.append(number)
    # a plain rectangle that an arrow names needs no declaration of its own (its class line colours it)
    named = {ident for edge in edges for ident in (ids(edge.source), missing.get(edge.target) or ids(edge.target))}

    lines = [f"flowchart {direction}", f'    {start_id}(["START"]):::plain']
    if global_nodes:
        lines.append(f'    {anywhere_id}(["any node"]):::plain')
    lines += [
        f"    {shape}"
        for ident, shape, text in declarations
        if not (shape == f'{ident}["{ident}"]' and text == ident and ident in named)
    ]
    lines += subgraph
    lines += [f'    {ident}(["{_escape(target)}"]):::plain' for target, ident in missing.items()]
    lines += arrows
    if tool_links:  # indexes are positions in ``arrows``: every arrow line is one Mermaid link, in order
        lines.append(f"    linkStyle {','.join(map(str, tool_links))} stroke:#2f6fdb,color:#2f6fdb")
    used = {*classes, "plain"}
    lines += [f"    classDef {css} {style}" for css, style in _AGENT_CLASSES if css in used]
    lines += [f"    class {','.join(members)} {css}" for css, members in classes.items()]
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------------------------------------ embedded copies

#: A marked diagram block in a Markdown file: ``<!-- mmd: name.mmd -->`` right above an (empty or stale) mermaid fence.
_EMBED = re.compile(r"(?P<head><!-- mmd: (?P<name>[\w.-]+) -->\n```mermaid\n)(?P<body>.*?)(?P<tail>```)", re.DOTALL)
#: Documents that embed diagrams of ``docs/graphs/`` (relative to the repository root).
EMBEDDING_DOCS = ("docs/graphs/README.md", "docs/HOW_IT_WORKS.md", "builder/graph/README.md")


def sync_embedded(document: Path, graphs_dir: Path, *, write: bool = True) -> bool:
    """Make every marked mermaid block of ``document`` equal to its ``.mmd`` file in ``graphs_dir``.

    A block is marked by a line ``<!-- mmd: builder_graph.mmd -->`` directly above the opening fence. Nothing else in the
    document is touched.

    Args:
        document: The Markdown file.
        graphs_dir: The folder that holds the ``.mmd`` files.
        write: When False the document is only compared, never changed.

    Returns:
        True when the document was already current.

    Raises:
        ValueError: when a marker names a file that does not exist in ``graphs_dir``.
    """
    text = document.read_text(encoding="utf-8")

    def fill(match: re.Match[str]) -> str:
        source = graphs_dir / match.group("name")
        if not source.is_file():
            raise ValueError(f"{document.name}: the marker names {match.group('name')}, which is not in {graphs_dir}")
        return match.group("head") + source.read_text(encoding="utf-8") + match.group("tail")

    updated = _EMBED.sub(fill, text)
    if updated != text and write:
        document.write_text(updated, encoding="utf-8", newline="\n")
    return updated == text


# ------------------------------------------------------------------------------------------------ command line


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m builder.viz",
        description="Mermaid diagram of the builder graph or of a Freya workflow JSON file.",
    )
    sub = parser.add_subparsers(dest="what", required=True)
    builder = sub.add_parser("builder", help="the builder's LangGraph pipeline, drawn from the real compiled graph")
    agent = sub.add_parser("agent", help="a Freya workflow JSON file (definition or export)")
    agent.add_argument("workflow", type=Path, help="path of the workflow JSON file")
    sync = sub.add_parser("sync", help="copy docs/graphs/*.mmd into the marked diagram blocks of the documentation")
    sync.add_argument("--check", action="store_true", help="change nothing; exit 1 when a block is out of date")
    for p in (builder, agent):
        p.add_argument("--view", choices=VIEWS, default="compact", help="compact: readable overview; full: every edge")
        p.add_argument("--direction", choices=DIRECTIONS, default="TD", help="flow direction (default TD, top-down)")
        p.add_argument("--out", type=Path, help="write the diagram to this file instead of stdout")
    return parser


def _sync(*, check: bool) -> int:
    from builder.paths import REPO_ROOT

    stale = 0
    for name in EMBEDDING_DOCS:
        try:
            current = sync_embedded(REPO_ROOT / name, REPO_ROOT / "docs" / "graphs", write=not check)
        except (OSError, ValueError) as exc:
            print(f"builder.viz: {exc}", file=sys.stderr)
            return 2
        print(f"{name}: {'current' if current else ('STALE' if check else 'updated')}", file=sys.stderr)
        stale += not current
    return 1 if (check and stale) else 0


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point of ``python -m builder.viz``; returns the process exit code."""
    args = _build_parser().parse_args(argv)
    if args.what == "sync":
        return _sync(check=args.check)
    try:
        if args.what == "builder":
            text = builder_mermaid(args.view, direction=args.direction)
        else:
            data = json.loads(args.workflow.read_text(encoding="utf-8-sig"))
            text = workflow_mermaid(data, direction=args.direction, view=args.view)
    except (OSError, ValueError) as exc:
        print(f"builder.viz: {exc}", file=sys.stderr)
        return 2
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8", newline="\n")
        print(f"wrote {args.out}", file=sys.stderr)
    else:
        sys.stdout.write(text)
    return 0


__all__ = [
    "DIRECTIONS",
    "EDGE_LABELS",
    "EMBEDDING_DOCS",
    "NODE_ROLES",
    "VIEWS",
    "builder_edges",
    "builder_mermaid",
    "condition_label",
    "main",
    "parse_langgraph_mermaid",
    "sync_embedded",
    "unwrap_workflow",
    "workflow_mermaid",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
