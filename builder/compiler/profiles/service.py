"""Source-driven service planning and deterministic Freya compilation.

This path accepts APIs without collections roles. The model describes policy,
stages and call-variable bindings; it never supplies platform JSON or API schemas.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from builder.compiler.errors import CompileError
from builder.compiler.nodes import end_call_node, tool_transition
from builder.compiler.tools import model_tool
from builder.compiler.workflow_model import Workflow
from builder.ingest.brief_loader import BriefBundle
from builder.ingest.tool_api import ToolApi


class _PlanModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ServiceEvidence(_PlanModel):
    document: str
    quote: str


class ServiceVariable(_PlanModel):
    name: str
    description: str
    evidence: ServiceEvidence


class ServiceRule(_PlanModel):
    id: str
    instruction: str
    evidence: ServiceEvidence


class ServiceBinding(_PlanModel):
    field: str
    call_variable: str


class ServiceToolUse(_PlanModel):
    tool: str
    when_to_call: str
    bindings: list[ServiceBinding] = Field(default_factory=list)


class ServiceExit(_PlanModel):
    target: str
    instruction: str


class ServiceStage(_PlanModel):
    name: str
    task_message: str
    tools: list[ServiceToolUse]
    exits: list[ServiceExit]


class ServicePlan(_PlanModel):
    language: str
    system_prompt: str
    call_variables: list[ServiceVariable]
    rules: list[ServiceRule]
    initial_stage: str
    stages: list[ServiceStage]
    closing_instruction: str


SERVICE_PLANNER_SYSTEM = """You plan a phone agent from ORIGINAL bank documents and their actual Tool API.
Return a ServicePlan, not Freya JSON. Do not assume this is collections, outbound,
or unverified: infer direction, identity status, language and all business rules
from the documents. Documents can include amendments: resolve their precedence.
Preserve the source language for spoken instructions and mandatory utterances.

Extract EVERY operative rule: opening, verification, allowed/forbidden disclosures,
product selection, information requests, values to read aloud, payment options,
consent and explicit confirmation before writes, timing, unavailable/empty data,
each documented API error, escalation, and closure. Each rule needs a verbatim
quote and the exact original document name. Do not import a different bank's rules.
Use short consecutive source quotes; whitespace differences are allowed.

Extract only caller variables EXPLICITLY supplied by the source/platform, retaining
their exact names and quotes that include those names. Request field names are not
automatically call variables. Bind an API field to a caller variable only when the
source establishes that relationship. Do not ask the caller for an already-supplied
trusted identifier. Every other request field is model-selected from the actual
tool response or caller answer, with exact API type/enum. Never invent IDs, dates,
amounts, response fields, or literal test-customer data. Supplied current-time
variables must be used for date rules; missing data must not be invented.

Build a small sequence of conversation stages, each with a single purpose, needed
bank tools, and explicit exits. Tool names MUST be exact keys from ToolApi. Tools
execute and their results are read BEFORE transitioning. Tool descriptions say
when to CALL, permitted arguments, success evidence and failure behavior. Include
source-required operations in appropriate stages. All stages must be reachable
from initial_stage and eventually reach the reserved terminal target 'end_call'.
Stage names are ASCII identifiers and must not equal end_call. Exits are explicit
transition tools: the dialogue model selects one after its stated prerequisites
are satisfied. Never transition merely because a tool was attempted. A failed
write must never be described as completed. Stay in the current stage or return
to a suitable earlier stage for corrections or retry; do not retry writes blindly.
Include routes for refusal, errors, out-of-scope requests and customer-requested
ending, following the original brief. Avoid duplicate targets from one stage.

The shared system prompt and stage task messages must together implement every
source rule. Describe how to select the right returned product, obtain write
consent, resolve or ask about relative dates, read relevant values without losing
information, distinguish errors/empty lists and escalate. Refer to actual API
fields and permitted enum values, which are supplied separately. Tool responses
are automatically accessible as '<tool>_response'; arguments as '<tool>_args'.
Do not use {{placeholders}} except declared call variables or these tool-response
variables. Speak before calling a transition tool; use the closing instruction
before transition to end_call. Do not call platform end_call directly.
No URLs, API schema JSON, source filenames, submission/admin instructions or
credentials belong in spoken prompts. No hand-authored workflow JSON is allowed.
"""


def _normal(text: str) -> str:
    return " ".join(text.split())


def validate_service_plan(plan: ServicePlan, bundle: BriefBundle, api: ToolApi) -> None:
    """Reject invented evidence, bindings and graph structure before compilation."""
    documents = {doc.name: _normal(doc.text) for doc in bundle.documents}
    identifier = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")

    def evidence(item: ServiceEvidence) -> None:
        quote = _normal(item.quote)
        if not quote or quote not in documents.get(item.document, ""):
            raise CompileError(f"service evidence is absent from {item.document!r}: {quote[:100]!r}")

    if not plan.rules or not plan.stages or not plan.system_prompt.strip():
        raise CompileError("service plan needs grounded rules, stages and a system prompt")
    rule_ids = [rule.id for rule in plan.rules]
    if len(set(rule_ids)) != len(rule_ids):
        raise CompileError("service rule IDs must be unique")
    for rule in plan.rules:
        evidence(rule.evidence)
        if not rule.instruction.strip():
            raise CompileError(f"service rule {rule.id!r} has no instruction")
    variables = {var.name for var in plan.call_variables}
    if len(variables) != len(plan.call_variables):
        raise CompileError("service call variable names must be unique")
    for var in plan.call_variables:
        evidence(var.evidence)
        if not identifier.fullmatch(var.name) or var.name not in var.evidence.quote:
            raise CompileError(f"service call variable {var.name!r} is not explicitly source-grounded")
    names = {stage.name for stage in plan.stages}
    if len(names) != len(plan.stages) or "end_call" in names:
        raise CompileError("service stages need unique names; end_call is reserved")
    if plan.initial_stage not in names:
        raise CompileError("service initial stage does not exist")
    for stage in plan.stages:
        if not identifier.fullmatch(stage.name) or not stage.task_message.strip():
            raise CompileError(f"invalid service stage {stage.name!r}")
        targets = [edge.target for edge in stage.exits]
        if not targets or len(targets) != len(set(targets)):
            raise CompileError(f"service stage {stage.name!r} needs distinct exits")
        for edge in stage.exits:
            if edge.target not in names | {"end_call"} or not edge.instruction.strip():
                raise CompileError(f"invalid service exit from {stage.name!r}")
        used: set[str] = set()
        for use in stage.tools:
            if use.tool not in api.tools or use.tool in used or not use.when_to_call.strip():
                raise CompileError(f"invalid service tool {use.tool!r} in {stage.name!r}")
            used.add(use.tool)
            fields: set[str] = set()
            for binding in use.bindings:
                if binding.field in fields or binding.call_variable not in variables:
                    raise CompileError(f"invalid service binding for {use.tool}.{binding.field}")
                fields.add(binding.field)
                if api.tools[use.tool].get_field(binding.field, "request") is None:
                    raise CompileError(f"unknown service request field {use.tool}.{binding.field}")
    reached = {plan.initial_stage}
    while True:
        next_reached = reached | {
            edge.target for stage in plan.stages if stage.name in reached for edge in stage.exits
        }
        if next_reached == reached:
            break
        reached = next_reached
    if names - reached:
        raise CompileError(f"unreachable service stages: {sorted(names - reached)}")
    ending = {"end_call"}
    while True:
        next_ending = ending | {
            stage.name for stage in plan.stages if any(edge.target in ending for edge in stage.exits)
        }
        if next_ending == ending:
            break
        ending = next_ending
    if names - ending:
        raise CompileError(f"service stages cannot end: {sorted(names - ending)}")


def compile_service_workflow(plan: ServicePlan, bundle: BriefBundle, api: ToolApi) -> dict[str, Any]:
    """Compile a grounded plan using only actual ToolApi contracts and platform constructors."""
    validate_service_plan(plan, bundle, api)
    nodes: dict[str, Any] = {}
    shared = "\n".join(f"[{rule.id}] {rule.instruction}" for rule in plan.rules)
    for stage in plan.stages:
        tools = []
        for use in stage.tools:
            contract = api.tools[use.tool]
            # Freya's documented api_call body schema permits scalar leaves. Reject
            # unsupported shapes rather than silently coercing an object to text.
            if any(spec.type not in {"string", "integer", "number", "boolean"}
                   for spec in contract.request_fields):
                raise CompileError(f"service tool {use.tool!r} has a non-scalar request unsupported by Freya api_call")
            tools.append(model_tool(
                contract,
                baked={binding.field: "{{" + binding.call_variable + "}}" for binding in use.bindings},
                customer_field="",
                include_optional=[spec.name for spec in contract.request_fields if not spec.required],
                description="CALL " + use.when_to_call,
                max_retries=0,
            ))
        task = (stage.task_message + "\n\nRead each bank tool result before acting on it. "
                "Do not claim an operation succeeded without its successful response. "
                "Call an exit transition only after its prerequisites are met. "
                "Speak the appropriate reply or goodbye before the exit transition.\n"
                + plan.closing_instruction)
        nodes[stage.name] = {
            "name": stage.name,
            "node_type": "conversation",
            "on_enter": {"type": "llm"},
            "task_message": task,
            "tools": tools,
            "transitions": [tool_transition(edge.target, edge.instruction) for edge in stage.exits],
        }
    nodes["end_call"] = end_call_node("end_call", "end_call")
    workflow = Workflow.from_json({
        "system_prompt": plan.system_prompt + "\n\nBank policy:\n" + shared,
        "initial_node": plan.initial_stage,
        "nodes": nodes,
        "global_nodes": {},
        "test_variables": {},
    }).to_json()
    return {
        "workflow": workflow,
        "profile": "service",
        "rule_map": {rule.id: ["system_prompt"] for rule in plan.rules},
        "notes": ["Source-grounded service plan compiled deterministically; no collections roles required."],
    }
