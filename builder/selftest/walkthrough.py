"""Walkthrough self-test: a cheap model audits the compiled workflow against every active ledger rule.

One FAST-role call (:data:`builder.prompts` template ``audit/walkthrough.v1.j2``) receives the active rules (id, stages,
statement, short quote) and a condensed text of the workflow - the system prompt, each node's task message, fixed
spoken lines, transitions, tools and the variables each code step computes - and reports the rules that look
``missing``, ``contradicted`` or ``unclear``. It is a second pair of eyes on top of the static checks: it needs no
platform call, so it can run before or after deployment, and a failure of the model only skips the test.

``missing`` and ``contradicted`` findings make the test fail and feed the policy repair (they name a rule id and a
stage); ``unclear`` findings are reported as warnings.

An audit only counts as evidence if the model says it looked at (nearly) all the rules it was sent: a report that
examined none, or only a few (:data:`MIN_EXAMINED_SHARE`), is *inconclusive* - the test fails with
``details["inconclusive"]`` instead of passing on an empty list of findings, and the summary says how many rules were
submitted and how many the model reports as examined. Findings about rule ids that are not active rules are written to
the trace (``details["unknown_rule_ids"]``), not dropped silently.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from builder.llm.errors import AllProvidersFailed
from builder.llm.providers import LLMRole
from builder.policy.ir import RuleLedger
from builder.prompts import render_prompt_parts

#: Longest condensed workflow text sent to the model.
MAX_WORKFLOW_CHARS = 48_000
#: Longest quote per rule in the prompt.
MAX_QUOTE_CHARS = 160
#: Share of the submitted rules the model must report as examined for the audit to count as a pass.
MIN_EXAMINED_SHARE = 0.9
_WRITES = re.compile(r"vars\[\s*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]\s*\]\s*=")
_COMMENT = re.compile(r"^\s*#\s*(.+)$", re.MULTILINE)


class WalkIssue(BaseModel):
    """One rule the audit doubts."""

    model_config = ConfigDict(extra="ignore")

    rule_id: str
    verdict: Literal["missing", "contradicted", "unclear"]
    where: str = ""
    explanation: str = ""


class WalkthroughReport(BaseModel):
    """The audit's answer (the JSON shape the prompt asks for)."""

    model_config = ConfigDict(extra="ignore")

    issues: list[WalkIssue] = Field(default_factory=list)
    checked: int = 0


def rules_text(ledger: RuleLedger) -> str:
    """``id | stages | statement | quote`` for every active rule."""
    rows = []
    for rule in ledger.active_rules():
        quote = " ".join(rule.quote.split())[:MAX_QUOTE_CHARS]
        rows.append(f"{rule.id} | {','.join(rule.scope)} | {' '.join(rule.statement.split())} | {quote}")
    return "\n".join(rows)


def rule_map_text(rule_map: Mapping[str, list[str]]) -> str:
    """``rule id -> places`` lines of the compiler's rule map, introduced as the compiler's unverified claims.

    The map is what the compiler *says* it did; the auditor must check it against the workflow text instead of
    believing it, so the block starts with a line saying so. Empty when the map names no place.
    """
    lines = [f"{rule_id} -> {', '.join(places)}" for rule_id, places in rule_map.items() if places]
    if not lines:
        return ""
    return "\n".join(
        ["(the compiler's own claims - unverified; confirm each against the workflow text below)", *lines]
    )


def _function_name(item: Mapping[str, Any]) -> str:
    function = item.get("function")
    return str(function.get("name", "")) if isinstance(function, Mapping) else ""


def _code_of(item: Mapping[str, Any]) -> str:
    function = item.get("function")
    if not isinstance(function, Mapping):
        return ""
    props = (function.get("parameters") or {}).get("properties") or {}
    code = props.get("code")
    return str(code.get("default", "")) if isinstance(code, Mapping) else ""


def _describe_code(code: str, limit: int) -> str:
    """What a code step computes: the variables it writes and its comment lines (the data tables are left out)."""
    writes = list(dict.fromkeys(_WRITES.findall(code)))
    comments = [c.strip() for c in _COMMENT.findall(code)][:12]
    text = "code step writes: " + (", ".join(writes) if writes else "(nothing recognisable)")
    if comments:
        text += "; notes: " + " / ".join(comments)
    return text[:limit]


def _node_text(name: str, node: Mapping[str, Any], code_chars: int) -> str:
    lines = [f"## node {name} ({node.get('node_type', '?')})"]
    if node.get("task_message"):
        lines.append("TASK MESSAGE:\n" + str(node["task_message"]))
    on_enter = node.get("on_enter")
    if isinstance(on_enter, Mapping) and on_enter.get("type") not in (None, "silent"):
        lines.append(
            f"ON ENTER ({on_enter.get('type')}): {on_enter.get('text') or on_enter.get('prompt') or ''}"
        )
    for transition in node.get("transitions") or []:
        if isinstance(transition, Mapping):
            tool = " [tool-call exit]" if transition.get("is_tool_call") else ""
            lines.append(f"-> {transition.get('target_node')}{tool}: {transition.get('description', '')}")
    for tool in node.get("tools") or []:
        if isinstance(tool, Mapping):
            function = tool.get("function") or {}
            lines.append(f"TOOL {_function_name(tool)}: {function.get('description', '')}")
    for action in node.get("pre_actions") or []:
        if isinstance(action, Mapping):
            code = _code_of(action)
            lines.append(_describe_code(code, code_chars) if code else f"PRE-ACTION {_function_name(action)}")
    return "\n".join(lines)


def workflow_text(workflow: Mapping[str, Any], max_chars: int = MAX_WORKFLOW_CHARS) -> str:
    """A condensed, model-readable rendering of a workflow (see the module docstring)."""
    for code_chars in (700, 250):
        parts = ["SYSTEM PROMPT:\n" + str(workflow.get("system_prompt", ""))]
        for name, node in (workflow.get("nodes") or {}).items():
            parts.append(_node_text(name, node, code_chars))
        for name, node in (workflow.get("global_nodes") or {}).items():
            parts.append(_node_text(f"{name} (global)", node, code_chars))
        text = "\n\n".join(parts)
        if len(text) <= max_chars:
            return text
    return text[:max_chars]


async def run_walkthrough(
    llm: Any,
    ledger: RuleLedger,
    workflow: Mapping[str, Any],
    rule_map: Mapping[str, list[str]],
    call_variables: list[str],
    trace: Any,
    *,
    deadline: float | None = None,
) -> dict[str, Any]:
    """Audit ``workflow`` against ``ledger`` and return ``{"name", "passed", "summary", "details"}``.

    ``details["issues"]`` lists the doubtful rules; ``passed`` is false when any is ``missing`` or ``contradicted``.
    A failing model call yields ``passed = True`` with ``details["skipped"]`` set: an unavailable auditor must not
    block a build.
    """
    prompt = render_prompt_parts(
        "audit/walkthrough.v1.j2",
        rules_text=rules_text(ledger),
        workflow_text=workflow_text(workflow),
        rule_map_text=rule_map_text(rule_map),
        call_variables=", ".join(call_variables),
    )
    try:
        result = await llm.structured(
            purpose="walkthrough audit of the compiled workflow",
            schema=WalkthroughReport,
            system=prompt.system,
            user=prompt.user,
            role=LLMRole.FAST,
            max_tokens=6000,
            deadline=deadline,
        )
    except AllProvidersFailed as exc:
        trace.error("walkthrough audit skipped: the model call failed", error=str(exc)[:200])
        return {
            "name": "walkthrough",
            "passed": True,
            "summary": "skipped: the audit model was not available",
            "details": {"skipped": True, "issues": []},
        }
    report: WalkthroughReport = result.value
    active = {rule.id for rule in ledger.active_rules()}
    issues = [i for i in report.issues if i.rule_id in active]
    unknown = list(dict.fromkeys(i.rule_id for i in report.issues if i.rule_id not in active))
    if unknown:
        trace.event(
            "test",
            f"walkthrough: the model named rule id(s) that are not active rules: {', '.join(unknown[:8])}",
            rule_ids=unknown[:20],
        )
    hard = [i for i in issues if i.verdict != "unclear"]
    submitted = len(active)
    examined = max(0, int(report.checked))
    inconclusive = submitted > 0 and examined < MIN_EXAMINED_SHARE * submitted
    summary = (
        f"{submitted} rules submitted, model reports {examined} examined, "
        f"{len(hard)} missing/contradicted, {len(issues) - len(hard)} unclear"
        + (": " + ", ".join(f"{i.rule_id} ({i.verdict})" for i in hard[:8]) if hard else "")
    )
    if inconclusive:
        summary = f"inconclusive (fewer than {MIN_EXAMINED_SHARE:.0%} of the rules examined): {summary}"
    return {
        "name": "walkthrough",
        "passed": not hard and not inconclusive,
        "summary": summary,
        "details": {
            "skipped": False,
            "inconclusive": inconclusive,
            "issues": [i.model_dump() for i in issues],
            "checked": examined,
            "submitted": submitted,
            "unknown_rule_ids": unknown,
        },
    }


__all__ = [
    "MAX_WORKFLOW_CHARS",
    "MIN_EXAMINED_SHARE",
    "WalkIssue",
    "WalkthroughReport",
    "rule_map_text",
    "rules_text",
    "run_walkthrough",
    "workflow_text",
]
