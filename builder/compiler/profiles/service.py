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
from builder.compiler.pycode import assert_safe_code
from builder.compiler.service_context import response_context_hook, response_context_prompt
from builder.compiler.tools import model_tool
from builder.compiler.workflow_model import Workflow
from builder.ingest.brief_loader import BriefBundle
from builder.ingest.tool_api import FieldSpec, ToolApi, ToolContract


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


class ServiceOutcome(_PlanModel):
    name: str
    reason: str
    evidence: ServiceEvidence


class ServicePlan(_PlanModel):
    language: str
    system_prompt: str
    call_variables: list[ServiceVariable]
    rules: list[ServiceRule]
    initial_stage: str
    stages: list[ServiceStage]
    closing_instruction: str
    outcomes: list[ServiceOutcome] = Field(default_factory=list)


SERVICE_PLANNER_SYSTEM = """You plan a phone agent from ORIGINAL bank documents and their actual Tool API.
Return a ServicePlan, not Freya JSON. Do not assume this is collections, outbound,
or unverified: infer direction, identity status, language and all business rules
from the documents. Documents can include amendments: resolve their precedence.
Preserve the source language for spoken instructions and mandatory utterances.

Apply the provided EXECUTION CONTEXT and the original sources' explicit waivers
for that endpoint before deciding whether a tool is executable. When the sources
explicitly waive authentication, headers or call metadata in the supplied test
environment, their production-only requirements must not prevent the documented
customer flow there. Omit unavailable waived metadata; never invent a caller
phone number, credential, token or other missing integration value, and never
ask the caller for it. A derived schema or example's mandatory-looking fields do
not cancel an explicit original-source environment waiver. Do not claim that
production authentication occurred. Requirements not waived still apply exactly.
An explicitly unavailable or unresolved eligibility signal cannot become an
invented predicate, caller attestation or extra integration. Implement the
documented feasible flow and truthful limitation instead of suppressing the flow.
Do not ask a caller to certify an unavailable bank eligibility signal or treat
a statement about prior use as verified eligibility. Apply caller attestation
only when the authoritative source expressly authorizes that mechanism.

Extract EVERY operative rule: opening, verification, allowed/forbidden disclosures,
product selection, information requests, values to read aloud, payment options,
consent and explicit confirmation before writes, timing, unavailable/empty data,
each documented API error, escalation, and closure. Each rule needs a verbatim
quote and the exact original document name. Do not import a different bank's rules.
Make every source rule for numbers, currencies, speech and customer actions explicit
and actionable: name the actual fields, all source-defined conditions, what to say,
and what action or question follows. Preserve every required amount and currency,
including zero, negative, missing and partial-data cases when the source defines
them. Do not replace exact source behavior with vague directions such as 'choose
an appropriate value', 'use the correct wording', or 'handle the situation'.
The deployed phone agent cannot access the original documents. Its system prompt,
rule instructions and stage messages must be standalone: embed every operative
branch condition, actual field path and complete required utterance/template.
Naming a source case or saying 'follow the source wording/conditions' does not
implement it; include the conditions and wording themselves in the plan.
Enumerate EVERY source-defined branch with its field conditions, exact spoken
outputs and next stage/action. Do not omit required amounts, notices, thanks,
date cases, linked-product details or source-specific no-data/zero-value endings.
Build a coverage checklist from EVERY authoritative paragraph, bullet, table row
and amendment before writing the stages. For each operative branch, determine:
its exact input/response fields and comparison conditions; whether those values
are fetched on every incoming path; the full spoken output in its required order;
and its next action, question or terminal exit. Map each source-relevant API
response field to that behavior, including combinations of fields, boundaries,
absent/empty/null values and record relationships. Do not infer a speaking rule
for diagnostic fields that the sources keep internal. Encode this checklist in
the actual rules and stage instructions, not a claim that coverage is complete.
Check the whole source checklist on every repair; retaining previous correct
branches is required even when the latest feedback discusses different defects.
Before producing the plan, check every operative source branch against a concrete
runtime instruction and usable exit. Preserve source-specific exceptions BEFORE
generic completion or offer routes: different branch endings cannot be replaced
by one unconditional 'after reading, continue' route. For a requested-information
shortcut, implement its exact next action instead of applying the full-reading
path. Required notices and date-specific branches must occur in their prescribed
order, not after an ordinary reading has already contradicted them.
Evaluate source-defined temporal exceptions against the supplied current time
and source timezone BEFORE selecting the normal reading. Implement the distinct
before/after/on-boundary cases, their required total versus individual values,
and no-issued-record cases as complete standalone utterances and next actions.
Do not read a normal date/amount sentence first when the applicable exception
requires a notice followed by a different returned value. A required factual
notice remains operative when only its final editorial wording is pending: say
the documented fact concisely, without adding an amount, cause or promise.
When a caller combines requests, retain that intent across the necessary data
fetch and enter the source-required consent/action step directly. A shortcut
must skip an expressly unnecessary offer/question while fetching all needed
data. Enumerate empty/missing-field alternatives with both their required spoken
explanation and next available action; a limitation must not erase another
source-permitted option.
Write the complete source-required customer-facing sentences for errors, transfers,
privacy, refusals, confirmation and completion. 'Say the relevant error/security/
success text' is incomplete when that text is not embedded elsewhere in runtime
instructions. Evidence quotes are provenance, not runtime instructions.
If a source requires details of multiple related records, fetch and retain each
record's actual required fields through the available documented reference and
tool. Acknowledging the relationship, omitting missing details, or reusing another
record's figures/dates does not fulfill a requirement to read both records.
Preserve the original record's identifiers, values and write prerequisites before
fetching a related record; distinguish the latest response from earlier retained
records. Use the documented relationship key and each record's own returned fields.
When each related record's fields are required, fetch each record separately with
its documented identifier and retain the responses. Do not assume that related
records share dates, amounts or settings just because one relationship is shared.
Speak each record's own required identifier, name and fields before the next step.
Counted repeat requests need a reachable repeat path and the source-defined stop
condition. An unconditional success-to-terminal exit cannot also allow another
request in the same call. Preserve any explicit-consent requirement on each write.
After a repeatable successful action, provide a reachable opportunity for the
caller to request the permitted repeat and retain its counter and required input
values. Stop at the source limit and require fresh consent for every permitted
write; do not merely mention a counter beside an unconditional terminal exit.
Every evidence.quote MUST be ONE continuous verbatim span from its named document.
Copy one short literal sentence or clause, preferably from a single physical line.
NEVER add ellipses, combine separate spans, paraphrase, omit intervening words, or
attribute a quote to a different file. Keep punctuation and words exact; structural
mail quote prefixes and Markdown emphasis may be omitted. For a call variable,
copy a short source span containing its exact name, without skipping examples or
list markers between sentences. A source that has no such quote cannot supply a rule.

Extract only caller variables EXPLICITLY supplied by the source/platform, retaining
their exact names and quotes that include those names. Request field names are not
automatically call variables. Bind an API field to a caller variable only when the
source establishes that relationship. Do not ask the caller for an already-supplied
trusted identifier. Every other request field is model-selected from the actual
tool response or caller answer, with exact API type/enum. Never invent IDs, dates,
amounts, response fields, or literal test-customer data. Supplied current-time
variables must be used for date rules; missing data must not be invented.
For EVERY tool use, account for EVERY required request field in its when_to_call:
state the documented fixed value, supplied-variable binding, returned source field,
or permitted caller choice. Include source-fixed constants even when the compiler
also bakes them from the contract. bindings maps only supplied call variables;
literal constants and model-selected fields belong in the tool instructions.
For each use, write an explicit argument map in when_to_call: exact dotted request
field -> documented constant, supplied call variable, exact returned field path,
or source-permitted caller selection. Specify the actual scalar type and enum
choice where relevant. Retain the selected record identifier across stages and
reuse it exactly; a caller's digits/name select a returned record, never construct
its API identifier. Map every source-constrained argument even when optional in
the API schema. Response-derived values and caller choices are tool arguments,
not invented platform call variables or unsupported bindings. No request field
may be described merely as 'the selected value' without saying how it is obtained.

Build a small sequence of conversation stages, each with a single purpose, needed
bank tools, and explicit exits. Tool names MUST be exact keys from ToolApi. Tools
execute and their results are read BEFORE transitioning. Tool descriptions say
when to CALL, permitted arguments, success evidence and failure behavior. Include
source-required operations in appropriate stages. All stages must be reachable
from initial_stage and eventually reach the reserved terminal target 'end_call'.
Stage names are ASCII identifiers and must not equal end_call. Exits may also
target a source-grounded outcome listed in outcomes. Create distinct terminal
outcomes when the source requires returning to a phone menu, human assistance,
or ending for different reasons. Use a short ASCII identifier describing each
outcome; its terminal function name becomes the platform endedReason. This is
outcome metadata, not a bank API call or a promise of a completed physical transfer.
Each outcome needs evidence for the requested routing.
The reserved target end_call is implicit: NEVER put it in the outcomes list.
outcomes contains only additional distinct source-grounded terminal names.
Exits are explicit transition tools: the dialogue model selects one after its stated prerequisites
are satisfied. Never transition merely because a tool was attempted. A failed
write must never be described as completed. Stay in the current stage or return
to a suitable earlier stage for corrections or retry; do not retry writes blindly.
Include routes for refusal, errors, out-of-scope requests and customer-requested
ending, following the original brief. Exactly ONE exit per target per stage is
permitted. Combine multiple reasons for the same target into one instruction with
explicit OR alternatives; do not create duplicate exits to that target.
Every source-wide interruption or terminal route must be available as an explicit
exit in EVERY applicable stage, including stages that fetch or write data. Derive
these routes from the sources, for example requests for human help, out-of-scope
questions, payment requests, privacy restrictions or ending. A shared instruction
alone cannot route the call: expose the required target in that stage's exits,
preserve its source-defined prerequisites, and never require an unrelated bank
operation before honoring an immediate source-required interruption.
Apply every source-wide counter or limit in every applicable stage, with an
explicit exit for its terminal result. Include the source's exact counter scope,
threshold and prerequisite, such as unusable answers, silence or calming attempts;
do not merely describe the rule globally while omitting its usable stage exit.
Audit every stage's exits separately, including selection and final consent:
counter exhaustion must appear as an explicit alternative on its actual target
exit, with the exact source-required sentence. A success/refusal exit to that
same target does not implicitly cover exhausted attempts. Keep silent/non-answer,
unusable-answer, repeat-request and complaint counters distinct as the source does.

The shared system prompt and stage task messages must together implement every
source rule. Describe how to select the right returned product, obtain write
consent, resolve or ask about relative dates, read relevant values without losing
information, distinguish errors/empty lists and escalate. Refer to actual API
fields and permitted enum values, which are supplied separately. Tool responses
are automatically accessible as '<tool>_response'; arguments as '<tool>_args'.
Use ONLY the exact allowed response and argument names listed with this request.
The prefix is the actual ToolApi.tools key, never a stage name, abbreviated alias,
source operation spelling or invented preview/result label. Do not write an
unlisted response name even in a negated example or explanatory comparison.
The platform wraps the HTTP response: its 'data' is the COMPLETE bank JSON body.
Thus bank-body path payload.items is <tool>_response.data.payload.items; preserve
every original response envelope layer, rather than dropping a repeated data key.
Every tool response referenced by a stage, tool description or exit must have been
fetched by a tool in that stage or on EVERY incoming path to the stage. A shortcut
may skip unnecessary questions, never the data fetch needed by its destination.
Do not rely on a response from an optional branch or a tool absent from the stage.
Do not use {{placeholders}} except declared call variables or these tool-response
variables. Speak before calling a transition tool; use the closing instruction
before transition to end_call. Do not call platform end_call directly.
No URLs, API schema JSON, source filenames, submission/admin instructions or
credentials belong in spoken prompts. No hand-authored workflow JSON is allowed.
Never ask the caller for integration credentials or manufacture tokens. An auth
operation whose credentials are not supplied cannot be called by this phone agent;
the integration operator provisions them outside the caller conversation.
Describe only actions executable by an exposed tool with all its required inputs
available. Do not instruct token renewal, retries using refreshed credentials or
other integration actions when there is no usable tool and supplied credentials
to perform them. Never invent background integration capabilities.
API request objects use dotted binding paths, e.g. payload.memberReference. The
compiler reconstructs the exact nested request envelope; do not invent flat fields.
Resolve policy precedence from document dates, author authority, amendment scope
and explicit supersession. Older examples, FAQs and recorded prompts are illustrations
when newer authoritative requirements disagree; never let them override updates.
"""


def service_response_name_instructions(api: ToolApi) -> str:
    """Enumerate the actual compiler tool namespace for the current service API."""
    names = "\n".join(f"- {tool}: {tool}_response and {tool}_args" for tool in sorted(api.tools))
    return (
        "\n\nEXACT ALLOWED TOOL RESPONSE AND ARGUMENT NAMES:\n" + names
        + "\nCopy these names exactly in every stage message, tool instruction and exit. "
        "They identify the actual tool results and arguments; stage names create no response variables. "
        "The compiler supplies recorded full envelopes and history as JSON data at stage handoff. "
        "All original bank envelope layers remain present inside each platform result. "
        "A listed name does not authorize a call without source-required inputs or credentials."
    )


def _normal(text: str) -> str:
    # Mail forwarding and Markdown emphasis are source structure, not words.
    # Strip only line-start quoting and paired inline emphasis; retain numeric
    # operators, punctuation and actual wording for the verbatim evidence gate.
    text = re.sub(r"(?m)^[ \t]*(?:>[ \t]*)+", "", text)
    text = re.sub(r"(?<![\w*])\*\*(?=\S)(.+?)(?<=\S)\*\*(?![\w*])", r"\1", text)
    text = re.sub(r"(?<![\w*])\*(?!\*)(?=\S)([^*\n]+?)(?<=\S)\*(?![\w*])", r"\1", text)
    return " ".join(text.split())


def validate_service_plan(plan: ServicePlan, bundle: BriefBundle, api: ToolApi) -> None:
    """Report all evidence, binding and graph errors together without weakening grounding."""
    documents: dict[str, list[str]] = {}
    for doc in bundle.documents:
        documents.setdefault(doc.name, []).append(_normal(doc.text))
    identifier = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
    problems: list[str] = []

    def evidence(item: ServiceEvidence, location: str) -> None:
        quote = _normal(item.quote)
        if not quote or not any(quote in text for text in documents.get(item.document, [])):
            problems.append(f"{location}: service evidence is absent from {item.document!r}: {quote[:100]!r}")

    if not plan.rules or not plan.stages or not plan.system_prompt.strip():
        problems.append("service plan needs grounded rules, stages and a system prompt")
    rule_ids = [rule.id for rule in plan.rules]
    if len(set(rule_ids)) != len(rule_ids):
        problems.append("service rule IDs must be unique")
    for rule in plan.rules:
        evidence(rule.evidence, f"rule {rule.id}")
        if not rule.instruction.strip():
            problems.append(f"service rule {rule.id!r} has no instruction")
    variables = {var.name for var in plan.call_variables}
    if len(variables) != len(plan.call_variables):
        problems.append("service call variable names must be unique")
    for var in plan.call_variables:
        evidence(var.evidence, f"call variable {var.name}")
        if not identifier.fullmatch(var.name) or var.name not in var.evidence.quote:
            problems.append(f"service call variable {var.name!r} is not explicitly source-grounded")
    names = {stage.name for stage in plan.stages}
    outcomes = {outcome.name for outcome in plan.outcomes}
    if len(outcomes) != len(plan.outcomes) or outcomes & (names | {"end_call"}):
        problems.append("service outcome names must be distinct from stages and end_call")
    for outcome in plan.outcomes:
        evidence(outcome.evidence, f"outcome {outcome.name}")
        if not identifier.fullmatch(outcome.name) or not outcome.reason.strip():
            problems.append(f"invalid service terminal outcome {outcome.name!r}")
    if len(names) != len(plan.stages) or "end_call" in names:
        problems.append("service stages need unique names; end_call is reserved")
    if plan.initial_stage not in names:
        problems.append("service initial stage does not exist")
    for stage in plan.stages:
        if not identifier.fullmatch(stage.name) or not stage.task_message.strip():
            problems.append(f"invalid service stage {stage.name!r}")
        targets = [edge.target for edge in stage.exits]
        if not targets or len(targets) != len(set(targets)):
            problems.append(f"service stage {stage.name!r} needs distinct exits")
        for edge in stage.exits:
            if edge.target not in names | outcomes | {"end_call"} or not edge.instruction.strip():
                problems.append(f"invalid service exit from {stage.name!r}: {edge.target!r}")
        used: set[str] = set()
        for use in stage.tools:
            if use.tool not in api.tools or use.tool in used or not use.when_to_call.strip():
                problems.append(f"invalid service tool {use.tool!r} in {stage.name!r}")
            used.add(use.tool)
            if use.tool not in api.tools:
                continue
            fields: set[str] = set()
            for binding in use.bindings:
                if binding.field in fields or binding.call_variable not in variables:
                    problems.append(f"invalid service binding for {use.tool}.{binding.field}")
                fields.add(binding.field)
                field = api.tools[use.tool].get_field(binding.field, "request")
                if field is None:
                    problems.append(f"unknown service request field {use.tool}.{binding.field}")
                elif field.type not in {"string", "integer", "number", "boolean"}:
                    problems.append(f"service binding {use.tool}.{binding.field} must address a scalar dotted path")
    reached = {plan.initial_stage}
    while True:
        next_reached = reached | {
            edge.target for stage in plan.stages if stage.name in reached for edge in stage.exits
        }
        if next_reached == reached:
            break
        reached = next_reached
    if names - reached:
        problems.append(f"unreachable service stages: {sorted(names - reached)}")
    ending = outcomes | {"end_call"}
    while True:
        next_ending = ending | {
            stage.name for stage in plan.stages if any(edge.target in ending for edge in stage.exits)
        }
        if next_ending == ending:
            break
        ending = next_ending
    if names - ending:
        problems.append(f"service stages cannot end: {sorted(names - ending)}")
    problems.extend(_response_dependency_errors(plan, api, reached))
    if problems:
        raise CompileError("Invalid service plan:\n" + "\n".join(dict.fromkeys(problems)))


def _response_dependency_errors(plan: ServicePlan, api: ToolApi, reached: set[str]) -> list[str]:
    """Require referenced responses on every incoming path, including joins and loops.

    A tool in the current stage can supply its own response. Responses from an
    earlier stage are definite only when every reachable predecessor supplied
    them; initialize with the universe and monotonically intersect to a fixed
    point, with an empty initial-stage boundary on the first call.
    """
    stages = {stage.name: stage for stage in plan.stages}
    tools = {name: {use.tool for use in stage.tools if use.tool in api.tools}
             for name, stage in stages.items()}
    predecessors = {name: {prior.name for prior in plan.stages if prior.name in reached
                           and any(edge.target == name for edge in prior.exits)} for name in stages}
    available = {name: set(api.tools) for name in stages}
    while True:
        updated: dict[str, set[str]] = {}
        for name in stages:
            incoming = predecessors[name]
            updated[name] = (set.intersection(*(available[prior] | tools[prior] for prior in incoming))
                             if incoming and name != plan.initial_stage else set())
        if updated == available:
            break
        available = updated
    problems: list[str] = []
    response_name = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)_response\b")
    declared = {variable.name for variable in plan.call_variables}
    for name, stage in stages.items():
        text = "\n".join([stage.task_message, *(use.when_to_call for use in stage.tools),
                          *(edge.instruction for edge in stage.exits)])
        references = {match.group(1) for match in response_name.finditer(text)
                      if match.group(0) not in declared}
        for tool in sorted(references):
            if tool not in api.tools:
                problems.append(f"service stage {name!r} references unknown tool response {tool}_response")
            elif tool not in available[name] | tools[name]:
                problems.append(f"service stage {name!r} needs {tool}_response, but {tool!r} is not fetched "
                                "in that stage or on every incoming path")
    return problems


def _flat_fields(contract: ToolContract) -> tuple[list[FieldSpec], list[tuple[str, tuple[str, ...]]]]:
    """Freya scalar argument schema plus an exact path map for the request hook."""
    fields: list[FieldSpec] = []
    paths: list[tuple[str, tuple[str, ...]]] = []

    def walk(spec: FieldSpec, parent: tuple[str, ...], required: bool) -> None:
        path = (*parent, spec.name)
        if spec.type == "object":
            if not spec.items:
                raise CompileError(f"service tool {contract.name!r} has an object with no documented properties")
            for child in spec.items:
                walk(child, path, required and spec.required)
            return
        if spec.type not in {"string", "integer", "number", "boolean"}:
            raise CompileError(f"service tool {contract.name!r} has a non-scalar request unsupported by Freya api_call")
        alias = "__".join(path)
        if any(existing.name == alias for existing in fields):
            raise CompileError(f"service request flattening collision for {contract.name}.{'.'.join(path)}")
        enum: list[Any] | None = None
        if spec.enum is not None:
            enum = []
            for value in spec.enum:
                try:
                    converted = int(value) if spec.type == "integer" else float(value) if spec.type == "number" else value
                    if spec.type == "boolean":
                        if str(value).lower() not in {"true", "false"}:
                            raise ValueError("invalid boolean enum")
                        converted = str(value).lower() == "true"
                except (TypeError, ValueError) as exc:
                    raise CompileError(f"invalid typed enum for {contract.name}.{'.'.join(path)}") from exc
                enum.append(converted)
        fields.append(spec.model_copy(update={"name": alias, "required": required and spec.required,
                                             "enum": enum,
                                             "description": (spec.description + " API field " + ".".join(path)).strip()}))
        paths.append((alias, path))

    for spec in contract.request_fields:
        walk(spec, (), True)
    return fields, paths


def service_lint_api(api: ToolApi) -> ToolApi:
    """The platform's scalar projection, used only to lint compiled service tool arguments.

    Source contracts remain unchanged. The compiler verifies bindings against
    their real dotted paths and generates the reconstruction hook from those paths.
    """
    return api.model_copy(update={"tools": {
        name: contract.model_copy(update={"request_fields": _flat_fields(contract)[0]})
        for name, contract in api.tools.items()
    }})


def _request_hook(paths: list[tuple[str, tuple[str, ...]]], content_type: str = "application/json") -> str:
    media_type = content_type.split(";", 1)[0].strip().lower()
    if media_type == "application/x-www-form-urlencoded":
        if any(len(path) > 1 for _, path in paths):
            raise CompileError("nested form request encoding is not documented for Freya api_call")
        # Encode UTF-8 bytes, including reserved delimiters, without external modules.
        return assert_safe_code(
            "try:\n"
            "    original = config.get('body')\n"
            "    if isinstance(original, dict):\n"
            "        pairs = []\n"
            f"        for alias, path in {paths!r}:\n"
            "            if alias not in original:\n"
            "                continue\n"
            "            value = original[alias]\n"
            "            value = ('true' if value else 'false') if isinstance(value, bool) else str(value)\n"
            "            encoded = []\n"
            "            for part in (path[0], value):\n"
            "                encoded.append(''.join(chr(byte) if (48 <= byte <= 57 or 65 <= byte <= 90 or 97 <= byte <= 122 or byte in (45, 46, 95, 126)) else '+' if byte == 32 else '%%%02X' % byte for byte in part.encode('utf-8')))\n"
            "            pairs.append('='.join(encoded))\n"
            "        config['body'] = '&'.join(pairs)\n"
            "        config.setdefault('headers', dict())['Content-Type'] = 'application/x-www-form-urlencoded'\n"
            "except Exception:\n"
            "    pass\n", "service_form_body")
    if media_type != "application/json" and not media_type.endswith("+json"):
        raise CompileError(f"unsupported service request media type {content_type!r}")
    return assert_safe_code(
        "try:\n"
        "    original = config.get('body')\n"
        "    if isinstance(original, dict):\n"
        "        rebuilt = dict()\n"
        f"        for alias, path in {paths!r}:\n"
        "            if alias not in original:\n"
        "                continue\n"
        "            current = rebuilt\n"
        "            for part in path[:-1]:\n"
        "                if part not in current:\n"
        "                    current[part] = dict()\n"
        "                current = current[part]\n"
        "            current[path[-1]] = original[alias]\n"
        "        config['body'] = rebuilt\n"
        "except Exception:\n"
        "    pass\n", "service_request_envelope")


def compile_service_workflow(plan: ServicePlan, bundle: BriefBundle, api: ToolApi) -> dict[str, Any]:
    """Compile a grounded plan using only actual ToolApi contracts and platform constructors."""
    validate_service_plan(plan, bundle, api)
    nodes: dict[str, Any] = {}
    shared = "\n".join(f"[{rule.id}] {rule.instruction}" for rule in plan.rules)
    response_context = response_context_prompt(use.tool for stage in plan.stages for use in stage.tools)
    for stage in plan.stages:
        tools = []
        for use in stage.tools:
            contract = api.tools[use.tool]
            fields, paths = _flat_fields(contract)
            projected = contract.model_copy(update={"request_fields": fields})
            aliases = {".".join(path): alias for alias, path in paths}
            tools.append(model_tool(
                projected,
                baked={aliases[binding.field]: "{{" + binding.call_variable + "}}" for binding in use.bindings},
                customer_field="",
                include_optional=[spec.name for spec in fields if not spec.required],
                description="CALL " + use.when_to_call,
                timeout_ms=30000,
                max_retries=0,
                pre_process_code=_request_hook(paths, contract.request_content_type)
                if any(len(path) > 1 for _, path in paths) or contract.request_content_type != "application/json" else "",
                post_process_code=response_context_hook(use.tool),
            ))
        task = (stage.task_message + "\n\nRead each bank tool result before acting on it. "
                "Do not claim an operation succeeded without its successful response. "
                "Call an exit transition only after its prerequisites are met. "
                "When those prerequisites are already satisfied, call the transition tool immediately "
                "in this same turn; do not wait for an unnecessary caller acknowledgment. "
                "Do not stop after announcing that you will fetch information: perform the required "
                "fetch or transition now. Speak the appropriate reply or goodbye before the exit transition.\n"
                + "\n".join(f"transition_to_{edge.target}: {edge.instruction}" for edge in stage.exits)
                + "\n"
                + plan.closing_instruction + response_context)
        nodes[stage.name] = {
            "name": stage.name,
            "node_type": "conversation",
            "on_enter": {"type": "llm"},
            "task_message": task,
            "tools": tools,
            "transitions": [tool_transition(edge.target, edge.instruction) for edge in stage.exits],
        }
    terminals = {edge.target for stage in plan.stages for edge in stage.exits} - {stage.name for stage in plan.stages}
    for name in terminals:
        nodes[name] = end_call_node(name, name)
    caller_context = "\n".join(
        f"{variable.name} = {{{{{variable.name}}}}} ({variable.description})"
        for variable in plan.call_variables
    )
    if caller_context:
        caller_context = ("\n\nSupplied platform call context:\n" + caller_context
                          + "\nUse these supplied values for the source-defined rules. They are context data, "
                          "not text to read aloud or ask the caller to repeat unless a source rule requires it. "
                          "If a supplied value is missing or unresolved, never invent a replacement.")
    workflow = Workflow.from_json({
        "system_prompt": plan.system_prompt + "\n\nBank policy:\n" + shared + caller_context,
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
