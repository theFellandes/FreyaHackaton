"""Repairing an extracted policy IR that failed validation.

:func:`repair_policy` is the body of the graph's bounded repair loop. It is deliberately frugal with model calls:

1. :func:`~builder.understand.fixes.apply_safe_fixes` removes everything that is mechanical (unknown rule ids,
   respelled vocabulary, uncovered rules as residual rules);
2. when a tool API is available the result is validated again; if no *error* is left the repair is finished without a
   model call - unless an issue came from another stage (the policy guard's ``GUARD_*``, the self-tests'
   ``WALKTHROUGH_*`` / ``PRACTICE_*``): the validator cannot see those, so "no validator error" proves nothing and the
   model is asked;
3. otherwise the remaining issues (the outside ones included), the current IR, the documents, the ledger and the tool
   vocabulary go to the model
   (``prompts/repair/policy_repair.v1.j2``), which returns the complete corrected IR; the safe fixes run once more on
   its answer.

The caller decides how often to repair (``Settings.max_policy_repairs``) and re-validates after every round.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from builder.llm.providers import LLMRole
from builder.policy.ir import PolicyIR, RuleLedger
from builder.prompts import render_prompt_parts
from builder.understand._common import (
    MAX_OUTPUT_TOKENS,
    LLMLike,
    TraceLike,
    call_structured,
    trace_event,
)
from builder.understand.extract import policy_prompt_context
from builder.understand.fixes import apply_safe_fixes
from builder.understand.validate import (
    PolicyIssue,
    errors_of,
    format_issues,
    is_external_issue,
    validate_policy,
)

if TYPE_CHECKING:
    from builder.ingest.brief_loader import BriefBundle
    from builder.ingest.tool_api import ToolApi

__all__ = ["apply_safe_fixes", "repair_policy"]


async def repair_policy(
    llm: LLMLike,
    ir: PolicyIR,
    issues: list[PolicyIssue],
    bundle: BriefBundle,
    ledger: RuleLedger,
    trace: TraceLike | None,
    *,
    tool_api: ToolApi | None = None,
    deadline: float | None = None,
    attempt: int = 1,
) -> PolicyIR:
    """Return a repaired copy of ``ir`` that addresses ``issues`` (see the module docstring).

    Args:
        llm: The LLM gateway.
        ir: The IR that failed validation.
        issues: The validator's issues for ``ir``.
        bundle: The briefs (the model corrects values from their wording).
        ledger: The rule ledger.
        trace: Run trace.
        tool_api: The tool contracts. Strongly recommended: it enables the model-free shortcut, the allowed-values
            list in the prompt and the re-validation of the model's answer.
        deadline: ``time.monotonic()`` value after which the gateway starts no new attempt.
        attempt: Which repair round this is (shown to the model after the first).

    Raises:
        builder.llm.errors.AllProvidersFailed: The model call failed on every provider.
    """
    if not issues:
        return ir
    fixed, log = apply_safe_fixes(ir, ledger, tool_api)
    for line in log:
        trace_event(trace, "repair", f"safe fix: {line}")
    outside = [issue for issue in issues if is_external_issue(issue)]
    if tool_api is not None:
        remaining = validate_policy(fixed, ledger, tool_api)
        if not outside and not errors_of(remaining):
            trace_event(trace, "repair", "policy IR repaired without a model call", remaining_warnings=len(remaining))
            return fixed
        remaining = [*outside, *remaining]  # the validator does not know the outside findings; the model must
    else:
        remaining = [issue for issue in issues if issue.severity == "error"] or list(issues)
    context = policy_prompt_context(bundle, tool_api, ledger)
    prompt = render_prompt_parts(
        "repair/policy_repair.v1.j2",
        files=context["files"],
        full_text=context["full_text"],
        vocabulary_text=context["vocabulary_text"],
        issues_text=format_issues(remaining),
        ir_json=json.dumps(fixed.to_json_dict(), ensure_ascii=False, separators=(",", ":")),
        ledger_lines=context["ledger_lines"],
        ambiguity_lines=context["ambiguity_lines"],
        schema_json=context["schema_json"],
        attempt=attempt,
    )
    trace_event(trace, "repair", f"asking the model to fix {len(remaining)} issue(s) (round {attempt})",
                codes=sorted({issue.code for issue in remaining}))
    repaired = await call_structured(
        llm,
        purpose=f"repair policy IR (round {attempt}, {len(errors_of(remaining))} errors)",
        schema=PolicyIR,
        system=prompt.system,
        user=prompt.user,
        role=LLMRole.STRONG,
        max_tokens=MAX_OUTPUT_TOKENS,
        deadline=deadline,
    )
    repaired, log = apply_safe_fixes(repaired, ledger, tool_api)
    for line in log:
        trace_event(trace, "repair", f"safe fix: {line}")
    return repaired

