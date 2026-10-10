"""Step 3 of understanding a brief: concise task messages for the conversational nodes.

The compiler builds each node prompt from three parts: fixed sentences from the IR, computed facts (dates, amounts)
and the *verbatim* text of the ledger rules scoped to the node. :func:`draft_prose` adds the glue between them - a short
``NodeProse`` per node that says what to do first, next and when to stop, with every bullet tagged with the rule ids it
comes from. The model gets only the node's own rules (plus the global ones for ``system_prompt_extra``) and the IR
section the node uses, and is told never to add numbers, limits, sentences or policies that those do not contain.

Because a model can still slip, :func:`sanitize_prose` runs afterwards: rule ids in brackets that are not active ledger
rules are removed, bullets are capped, and numbers that appear neither in the ledger nor in the IR are reported to the
trace as warnings (nothing is invented silently).

Nodes covered (the same keys for both profiles; the lean profile merges ``opening`` and ``identity`` in the compiler):
``opening``, ``third_party``, ``identity``, ``negotiate``. If the compiler package defines
:class:`builder.compiler.prose.NodeProse` it is used as the result type; otherwise a structurally identical model
defined here stands in, so the stage works on its own.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from builder.llm.providers import LLMRole
from builder.policy.ir import PolicyIR, RuleLedger
from builder.prompts import render_prompt_parts
from builder.understand._common import LLMLike, TraceLike, call_structured, trace_event

try:  # the compiler owns NodeProse; this fallback keeps the stage usable (and testable) without it
    from builder.compiler.prose import NodeProse
except ImportError:  # pragma: no cover - depends on the state of the parallel compiler package

    class NodeProse(BaseModel):  # type: ignore[no-redef]
        """Task messages per node (stand-in with the same fields as the compiler's model)."""

        model_config = ConfigDict(extra="forbid")

        system_prompt_extra: str = ""
        per_node: dict[str, str] = Field(default_factory=dict)


__all__ = [
    "NODE_SPECS",
    "NodeProse",
    "NodeSpec",
    "ProseDraft",
    "draft_prose",
    "node_specs",
    "sanitize_prose",
]

_MAX_NODE_CHARS = 3000
_MAX_EXTRA_CHARS = 1500
_BRACKET_IDS = re.compile(r"\[([^\[\]]{1,200})\]")
_NUMBER = re.compile(r"(?<![\w.])\d+(?:[.:]\d+)?(?![\w])")


@dataclass(frozen=True)
class NodeSpec:
    """A conversational node: its key, what it does, the ledger stages it covers and the IR sections it uses."""

    key: str
    title: str
    job: str
    scopes: tuple[str, ...]
    ir_sections: tuple[str, ...]


NODE_SPECS: tuple[NodeSpec, ...] = (
    NodeSpec(
        "opening", "opening and right party",
        "Say the fixed opening, find out whether the person on the line is the customer, answer the questions the "
        "rules allow, and decide who is on the line.",
        ("opening",), ("right_party", "forbidden_before_verification"),
    ),
    NodeSpec(
        "third_party", "someone else answers",
        "Handle a person who is not the customer: what may and may not be said, whether and when a callback can be "
        "booked, and the wrong-number and deceased cases.",
        ("third_party",), ("third_party",),
    ),
    NodeSpec(
        "identity", "identity verification",
        "Ask the security questions one at a time, send each answer to the verification tool, handle hints, wrong "
        "answers, refusals and the failure case.",
        ("identity",), ("identity", "forbidden_before_verification"),
    ),
    NodeSpec(
        "negotiate", "stating the debt and agreeing a payment",
        "After verification: state the debt, get a payment promise within the date and amount rules, handle what the "
        "customer says, confirm and record the promise, and finish the call.",
        ("disclosure", "negotiation", "confirmation", "recording", "closing"),
        ("disclosure", "bands", "promise_dates", "amounts", "persuasion", "intents", "confirmation", "recording",
         "multi_product", "closing"),
    ),
)


def node_specs(profile: str) -> tuple[NodeSpec, ...]:
    """The nodes that get prose for ``profile`` (``staged`` or ``lean``): both use the same four keys."""
    return NODE_SPECS


class NodeText(BaseModel):
    """The model's text for one node."""

    model_config = ConfigDict(extra="ignore")

    node: str
    text: str = ""


class ProseDraft(BaseModel):
    """The model's answer: global conduct plus one text per node (converted to :class:`NodeProse`)."""

    model_config = ConfigDict(extra="ignore")

    system_prompt_extra: str = ""
    nodes: list[NodeText] = Field(default_factory=list)


def _strip_rule_ids(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _strip_rule_ids(v) for k, v in value.items() if k != "rule_ids"}
    if isinstance(value, list):
        return [_strip_rule_ids(v) for v in value]
    return value


def _rules_text(ledger: RuleLedger, scopes: tuple[str, ...], *, include_global: bool = False) -> str:
    wanted = set(scopes) | ({"global"} if include_global else set())
    rows = [r for r in ledger.active_rules() if wanted.intersection(r.scope)]
    return "\n".join(f"{r.id} | {' '.join(r.statement.split())}" for r in rows)


async def draft_prose(
    llm: LLMLike,
    ir: PolicyIR,
    ledger: RuleLedger,
    profile: str,
    trace: TraceLike | None,
    *,
    deadline: float | None = None,
) -> NodeProse:
    """Draft the task messages of the conversational nodes (see the module docstring).

    Args:
        llm: The LLM gateway.
        ir: The validated policy IR.
        ledger: The rule ledger (only active rules reach the prompt).
        profile: ``"staged"`` or ``"lean"``; changes one instruction of the prompt.
        trace: Run trace; the sanitiser's warnings go there.
        deadline: ``time.monotonic()`` value after which the gateway starts no new attempt.

    Returns:
        A sanitised :class:`NodeProse` with one entry per node of :data:`NODE_SPECS`.

    Raises:
        builder.llm.errors.AllProvidersFailed: No provider produced a valid answer (the caller falls back to the
            compiler's template-only prose).
    """
    ir_data = ir.to_json_dict()
    nodes: list[dict[str, str]] = []
    for spec in node_specs(profile):
        section = {name: _strip_rule_ids(ir_data.get(name)) for name in spec.ir_sections if name in ir_data}
        if spec.key in ("opening", "negotiate"):
            section["phrases"] = _strip_rule_ids(ir_data.get("phrases", {}))
        nodes.append(
            {
                "key": spec.key,
                "title": spec.title,
                "job": spec.job,
                "rules": _rules_text(ledger, spec.scopes),
                "ir_json": json.dumps(section, ensure_ascii=False, separators=(",", ":")),
            }
        )
    prompt = render_prompt_parts(
        "compile/node_prose.v1.j2",
        profile=profile,
        nodes=nodes,
        global_rules=_rules_text(ledger, ("global",)),
        phrases_json=json.dumps(_strip_rule_ids(ir_data.get("phrases", {})), ensure_ascii=False, separators=(",", ":")),
        forbidden_json=json.dumps(ir.forbidden_before_verification, ensure_ascii=False),
    )
    draft = await call_structured(
        llm,
        purpose=f"draft node prose ({profile} profile)",
        schema=ProseDraft,
        system=prompt.system,
        user=prompt.user,
        role=LLMRole.FAST,
        max_tokens=8000,
        deadline=deadline,
    )
    prose = NodeProse(
        system_prompt_extra=draft.system_prompt_extra.strip(),
        per_node={item.node.strip(): item.text.strip() for item in draft.nodes if item.node.strip()},
    )
    prose = sanitize_prose(prose, ir, ledger, trace)
    trace_event(trace, "prose", f"drafted prose for {len(prose.per_node)} node(s)",
                nodes={key: len(text) for key, text in prose.per_node.items()})
    return prose


def sanitize_prose(prose: NodeProse, ir: PolicyIR, ledger: RuleLedger, trace: TraceLike | None) -> NodeProse:
    """Remove rule ids that are not active ledger rules, cap lengths, warn about numbers found nowhere in the sources.

    Returns a new :class:`NodeProse`; the input is untouched. Unknown keys of ``per_node`` are dropped (the compiler
    only reads the keys of :data:`NODE_SPECS`).
    """
    active = {rule.id for rule in ledger.active_rules()}
    allowed_numbers = _source_numbers(ir, ledger)

    def clean(text: str, label: str, limit: int) -> str:
        removed: list[str] = []

        def fix(match: re.Match[str]) -> str:
            items = [part.strip() for part in match.group(1).split(",") if part.strip()]
            if not items or not all(re.fullmatch(r"[A-Za-z0-9._\-]+", i) for i in items):
                return match.group(0)  # not an id list; leave the text alone
            kept = [i for i in items if i in active]
            removed.extend(i for i in items if i not in active)
            return f"[{', '.join(kept)}]" if kept else ""

        cleaned = _BRACKET_IDS.sub(fix, text)
        cleaned = "\n".join(line.rstrip() for line in cleaned.split("\n"))
        if len(cleaned) > limit:
            cut = cleaned[:limit].rsplit("\n", 1)[0]
            trace_event(trace, "prose", f"{label}: text cut from {len(cleaned)} to {len(cut)} characters")
            cleaned = cut
        if removed:
            trace_event(trace, "prose", f"{label}: removed rule ids that are not active ledger rules: "
                        f"{', '.join(dict.fromkeys(removed))}")
        stray = sorted({n for n in _NUMBER.findall(_BRACKET_IDS.sub("", cleaned)) if n not in allowed_numbers})
        if stray:
            trace_event(trace, "prose", f"warning: {label} mentions number(s) found in neither the rules nor the "
                        f"IR: {', '.join(stray)}")
        return cleaned.strip()

    keys = {spec.key for spec in NODE_SPECS}
    per_node = {key: clean(text, f"node {key}", _MAX_NODE_CHARS) for key, text in prose.per_node.items() if key in keys}
    for key in sorted(set(prose.per_node) - keys):
        trace_event(trace, "prose", f"unknown node key {key!r} in the drafted prose was ignored")
    return NodeProse(system_prompt_extra=clean(prose.system_prompt_extra, "system_prompt_extra", _MAX_EXTRA_CHARS),
                     per_node=per_node)


def _source_numbers(ir: PolicyIR, ledger: RuleLedger) -> set[str]:
    """Every number that occurs in a ledger quote/statement or in the IR (as text), plus small counting words."""
    blob = " ".join(f"{r.quote} {r.statement}" for r in ledger.rules)
    blob += " " + json.dumps(ir.to_json_dict(), ensure_ascii=False)
    numbers = set(_NUMBER.findall(blob))
    numbers.update({"1", "2"})  # one/two ("1." list numbering, "2 sentences") are never a policy value on their own
    return numbers
