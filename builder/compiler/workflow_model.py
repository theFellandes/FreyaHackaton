"""Typed mirror of the Freya workflow JSON (see the kit's ``docs/workflow-format.md`` and ``docs/tools-and-code.md``).

The compiler builds workflows as plain dicts (they are easy to read and diff), then passes them through
:class:`Workflow` so that their shape is validated once, in one place, before they leave the compiler. The models are
*tolerant*: every model keeps unknown keys (``extra="allow"``) because exported workflows carry platform fields such as
``position`` or ``assertions`` that the builder does not use. Every optional field defaults to ``None`` and
:meth:`Workflow.to_json` drops ``None`` values, because the platform schema is strict and rejects ``null``.

Shapes (all keys are the platform's):

* A **condition** is one of :class:`Always`, :class:`MatchesIntent` (the routing model decides), :class:`VarCondition`
  (deterministic check of a variable) or :class:`Group` (``logic`` ``or``/``and`` over conditions; the top level of a
  transition always uses ``or``).
* A **node** has a ``node_type``: ``conversation`` (dialogue + ``tools``), ``api_request`` (``pre_actions`` calls made
  by the engine), ``code`` (a ``code_exec`` pre-action) or ``end_call`` (``terminal``). Other platform node types parse
  into :class:`OtherNode` unchanged.
* A **tool function** (:class:`ToolFunction`) is the same object in ``tools``, ``pre_actions`` and the workflow-level
  ``tools``; hooks and ``response_mapping`` are fields of it.

:meth:`Workflow.from_json` / :meth:`Workflow.to_json` round-trip a workflow, so the model can also read the platform's
exports (``examples/base-agent.json``) and the static checks in :mod:`builder.checks.lint` can be run on either form.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Discriminator, Tag

__all__ = [
    "Always",
    "ApiRequestNode",
    "CodeNode",
    "Condition",
    "ConversationNode",
    "EndCallNode",
    "ExtractionField",
    "FunctionSpec",
    "GlobalNode",
    "Group",
    "MatchesIntent",
    "Node",
    "OnEnter",
    "OtherNode",
    "ResponseMapping",
    "Terminal",
    "ToolFunction",
    "Transition",
    "VarCondition",
    "Workflow",
    "strip_none",
]


class _Loose(BaseModel):
    """Base for every workflow model: unknown keys are kept, so exports round-trip unchanged."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)


# --------------------------------------------------------------------------------------------------------------------
# Conditions
# --------------------------------------------------------------------------------------------------------------------


class Always(_Loose):
    """The unconditional condition ``{"operator": "always"}``."""

    operator: Literal["always"] = "always"


class MatchesIntent(_Loose):
    """Semantic condition judged by the routing model from ``description``."""

    operator: Literal["matches_intent"] = "matches_intent"
    description: str


class VarCondition(_Loose):
    """Deterministic check of a variable (``equals``, ``greater_than``, ``is_set`` ...). ``operand`` is optional."""

    variable: str
    operator: str
    operand: Any = None


class Group(_Loose):
    """``logic`` (``or`` / ``and``) over conditions. A transition's top-level group is always ``or``."""

    logic: Literal["or", "and"]
    conditions: list[Condition]


def _condition_tag(value: Any) -> str:
    """Pick the condition model from the raw dict (or the already-built instance)."""
    if isinstance(value, Group):
        return "group"
    if isinstance(value, Always):
        return "always"
    if isinstance(value, MatchesIntent):
        return "intent"
    if isinstance(value, VarCondition):
        return "var"
    if isinstance(value, dict):
        if "logic" in value:
            return "group"
        operator = value.get("operator")
        if operator == "always":
            return "always"
        if operator == "matches_intent":
            return "intent"
    return "var"


Condition = Annotated[
    Annotated[Group, Tag("group")]
    | Annotated[Always, Tag("always")]
    | Annotated[MatchesIntent, Tag("intent")]
    | Annotated[VarCondition, Tag("var")],
    Discriminator(_condition_tag),
]
"""Any condition shape; the right model is chosen by :func:`_condition_tag`."""

Group.model_rebuild()


class Transition(_Loose):
    """An outgoing edge. ``is_tool_call`` makes the dialogue model trigger it by calling ``transition_to_<target>``."""

    target_node: str
    description: str | None = None
    condition: Condition | None = None
    is_tool_call: bool | None = None


# --------------------------------------------------------------------------------------------------------------------
# Tools, mappings, extraction
# --------------------------------------------------------------------------------------------------------------------


class ResponseMapping(_Loose):
    """Copies ``path`` (starting with ``data.``) of a call result into the variable ``variable``."""

    path: str
    variable: str


class FunctionSpec(_Loose):
    """The ``function`` object of a tool: name, runtime ``handler`` and JSON-schema ``parameters``."""

    name: str
    handler: str | None = None
    description: str | None = None
    parameters: dict[str, Any] | None = None


class ToolFunction(_Loose):
    """A tool function as used in ``tools``, ``pre_actions`` and the workflow-level ``tools`` array."""

    type: str = "function"
    config: dict[str, Any] | None = None
    function: FunctionSpec
    response_mapping: list[ResponseMapping] | None = None
    pre_process_code: str | None = None
    post_process_code: str | None = None


class ExtractionField(_Loose):
    """One variable the extraction model fills from user speech."""

    name: str
    type: str | None = None
    description: str | None = None
    enum_values: list[str] | None = None
    extraction_prompt: str | None = None
    validation_pattern: str | None = None
    min_confidence: float | None = None
    max_retries: int | None = None


class OnEnter(_Loose):
    """Entry behaviour: ``llm`` (model writes the line; optional ``prompt``), ``tts`` (fixed ``text``) or ``silent``."""

    type: str
    text: str | None = None
    prompt: str | None = None
    asset_id: str | None = None


class Terminal(_Loose):
    """The ``terminal`` of an ``end_call`` node: an ``end_call``-handler function."""

    type: str = "function"
    function: FunctionSpec


# --------------------------------------------------------------------------------------------------------------------
# Nodes
# --------------------------------------------------------------------------------------------------------------------


class _NodeBase(_Loose):
    """Fields common to all node types."""

    name: str | None = None
    node_type: str
    task_message: str | None = None
    role_message: str | None = None
    on_enter: OnEnter | None = None
    on_enter_first_entry_only: bool | None = None
    transitions: list[Transition] | None = None
    routing_prompt: str | None = None
    loop_condition: str | None = None
    loop_prompt: str | None = None
    extraction_schema: list[ExtractionField] | None = None
    tools: list[ToolFunction] | None = None
    pre_actions: list[ToolFunction] | None = None
    response_mapping: list[ResponseMapping] | None = None
    passthrough: bool | None = None
    passthrough_default_transition: str | None = None
    max_node_turns: int | None = None
    max_turn_transition: str | None = None
    terminal: Terminal | None = None
    disable_inactivity_monitor: bool | None = None
    suppress_tool_results: bool | None = None
    truncation_strategy: str | None = None
    truncation_window_size: int | None = None
    reasoning_override: str | None = None


class ConversationNode(_NodeBase):
    """A dialogue step; the only node type that may carry ``tools``."""

    node_type: Literal["conversation"] = "conversation"


class ApiRequestNode(_NodeBase):
    """A node whose ``pre_actions`` HTTP calls the engine makes on entry."""

    node_type: Literal["api_request"] = "api_request"


class CodeNode(_NodeBase):
    """A node whose ``pre_actions`` hold one ``code_exec`` function (the snippet is its ``code`` default)."""

    node_type: Literal["code"] = "code"


class EndCallNode(_NodeBase):
    """Hangs up; carries a ``terminal`` and no transitions."""

    node_type: Literal["end_call"] = "end_call"


class OtherNode(_NodeBase):
    """Any other platform node type (transfer_call, integration ...): kept as is."""


class GlobalNode(ConversationNode):
    """An interrupt handler that can fire from any node; a conversation node plus activation fields."""

    condition: Condition | None = None
    description: str | None = None
    excluded_nodes: list[str] | None = None
    is_tool_call: bool | None = None


def _node_tag(value: Any) -> str:
    """Pick the node model from ``node_type``."""
    kind = value.get("node_type") if isinstance(value, dict) else getattr(value, "node_type", None)
    return kind if kind in ("conversation", "api_request", "code", "end_call") else "other"


Node = Annotated[
    Annotated[ConversationNode, Tag("conversation")]
    | Annotated[ApiRequestNode, Tag("api_request")]
    | Annotated[CodeNode, Tag("code")]
    | Annotated[EndCallNode, Tag("end_call")]
    | Annotated[OtherNode, Tag("other")],
    Discriminator(_node_tag),
]
"""Any node; chosen by ``node_type``."""


# --------------------------------------------------------------------------------------------------------------------
# Workflow
# --------------------------------------------------------------------------------------------------------------------


class Workflow(_Loose):
    """A whole workflow (the value of the ``workflow`` key in the REST requests)."""

    initial_node: str
    system_prompt: str | None = None
    nodes: dict[str, Node]
    global_nodes: dict[str, GlobalNode] | None = None
    test_variables: dict[str, Any] | None = None
    context_variables: dict[str, Any] | None = None
    tools: list[ToolFunction] | None = None
    agent_config: dict[str, Any] | None = None

    # ---- (de)serialisation -----------------------------------------------------------------------------------------

    def to_json(self) -> dict[str, Any]:
        """JSON-ready dict without ``None`` values (the platform rejects ``null``)."""
        return strip_none(self.model_dump(mode="json", exclude_none=True))

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Workflow:
        """Tolerant parse of a workflow dict (builder output or a platform export)."""
        return cls.model_validate(data)

    # ---- graph helpers ---------------------------------------------------------------------------------------------

    def node_names(self) -> list[str]:
        """Names of all nodes, global nodes included, in definition order."""
        return [*self.nodes, *(self.global_nodes or {})]

    def get(self, name: str) -> _NodeBase | None:
        """The node (or global node) called ``name``."""
        if name in self.nodes:
            return self.nodes[name]
        return (self.global_nodes or {}).get(name)

    def iter_nodes(self) -> Iterator[tuple[str, _NodeBase]]:
        """Yield ``(name, node)`` for every node, global nodes last."""
        yield from self.nodes.items()
        yield from (self.global_nodes or {}).items()

    def edges(self) -> list[tuple[str, str, str]]:
        """All edges as ``(source, target, kind)``; kind is ``transition``, ``tool_call``, ``passthrough`` or ``max_turn``."""
        out: list[tuple[str, str, str]] = []
        for name, node in self.iter_nodes():
            for transition in node.transitions or []:
                out.append((name, transition.target_node, "tool_call" if transition.is_tool_call else "transition"))
            if node.passthrough_default_transition:
                out.append((name, node.passthrough_default_transition, "passthrough"))
            if node.max_turn_transition:
                out.append((name, node.max_turn_transition, "max_turn"))
        return out

    def tool_names(self) -> set[str]:
        """Names of every tool function used anywhere (node tools, pre-actions, workflow-level tools)."""
        names: set[str] = set()
        for _, node in self.iter_nodes():
            for tool in [*(node.tools or []), *(node.pre_actions or [])]:
                names.add(tool.function.name)
        names.update(tool.function.name for tool in self.tools or [])
        return names


def strip_none(value: Any) -> Any:
    """Return ``value`` with every ``None`` removed from nested dicts and lists (``None`` list items are dropped)."""
    if isinstance(value, dict):
        return {key: strip_none(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [strip_none(item) for item in value if item is not None]
    return value
