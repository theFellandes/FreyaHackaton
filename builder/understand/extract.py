"""Step 2 of understanding a brief: from the rule ledger to the structured policy IR.

:func:`extract_policy` gives a model everything it needs to fill :class:`~builder.policy.ir.PolicyIR` and nothing it
could misuse:

* the full original text (authoritative wording for fixed sentences, tables and numbers), every document wrapped as
  ``<untrusted_document>`` data (:func:`builder.guardrails.input_guard.wrap_bundle`);
* the rule ledger with ids (so every IR section cites the rules it implements), superseded rules marked as such;
* the ambiguity register with the chosen interpretations (the IR follows them);
* the tool API contracts and an explicit list of the closed vocabularies (identity fields, outcomes, reasons, note
  reasons, queues, product types and fields);
* the intent catalog and the generic situation keys of the outcome table;
* a checklist of the ``phrases`` fields the ledger prescribes (quoted or only described), which the model must fill -
  quoting the brief or composing the sentence from the elements the rule names
  (:func:`builder.understand.validate.phrase_checklist`);
* the PolicyIR JSON schema (``builder.llm.structured.schema_for_llm``) plus a generic field guide in the prompt
  (``prompts/extract/policy_ir.v1.j2``): what every field means, never which value it has.

The answer is then passed through :func:`builder.understand.fixes.apply_safe_fixes` (mechanical corrections and
coverage by residual rules) and the IR's fixed sentences are compared with the briefs
(:func:`builder.understand.grounding.check_phrase_grounding`); suspicious sentences are traced as warnings. Validation
against the tool API and repair are separate steps (:mod:`~builder.understand.validate`,
:mod:`~builder.understand.repair`).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from builder.guardrails.input_guard import wrap_bundle
from builder.llm.providers import LLMRole
from builder.llm.structured import schema_for_llm
from builder.policy.intents import describe_catalog
from builder.policy.ir import PolicyIR, RuleLedger
from builder.policy.outcomes import STRUCTURAL_ORDER
from builder.prompts import render_prompt_parts
from builder.understand._common import (
    MAX_OUTPUT_TOKENS,
    LLMLike,
    TraceLike,
    call_structured,
    trace_event,
)
from builder.understand.fixes import apply_safe_fixes
from builder.understand.grounding import check_phrase_grounding
from builder.understand.ledger import ledger_prompt_lines
from builder.understand.validate import phrase_checklist, tool_vocabulary

if TYPE_CHECKING:
    from builder.ingest.brief_loader import BriefBundle
    from builder.ingest.tool_api import ToolApi

__all__ = ["ambiguity_lines", "extract_policy", "policy_prompt_context"]


def ambiguity_lines(ledger: RuleLedger) -> str:
    """The ambiguity register as prompt lines: ``- id [rule ids]: question -> CHOSEN: reading (because ...)``."""
    return "\n".join(
        f"- {a.id} [{', '.join(a.rule_ids)}]: {a.question} -> CHOSEN: {a.chosen} (because: {a.rationale})"
        for a in ledger.ambiguities
    )


def policy_prompt_context(bundle: BriefBundle, tool_api: ToolApi | None, ledger: RuleLedger) -> dict[str, Any]:
    """The template context shared by the extraction and repair prompts (``tool_api`` None = no vocabulary lists)."""
    schema = json.dumps(schema_for_llm(PolicyIR), ensure_ascii=False, separators=(",", ":"))
    return {
        "files": bundle.names,
        "full_text": wrap_bundle(bundle, labeled=True),
        "tool_api_text": tool_api.describe() if tool_api is not None else "(no tool API available)",
        "vocabulary_text": tool_vocabulary(tool_api).describe() if tool_api is not None else "(no tool API available)",
        "intent_catalog": describe_catalog(),
        "situation_keys": [str(key) for key in STRUCTURAL_ORDER],
        "ledger_lines": ledger_prompt_lines(ledger, quote_chars=100),
        "supersession_lines": "\n".join(f"- {line}" for line in ledger.supersessions),
        "ambiguity_lines": ambiguity_lines(ledger),
        "phrase_checklist": phrase_checklist(ledger),
        "schema_json": schema,
    }


async def extract_policy(
    llm: LLMLike,
    bundle: BriefBundle,
    tool_api: ToolApi,
    ledger: RuleLedger,
    trace: TraceLike | None,
    *,
    deadline: float | None = None,
) -> PolicyIR:
    """Extract the :class:`~builder.policy.ir.PolicyIR` of the briefs (see the module docstring).

    Args:
        llm: The LLM gateway (anything with an async ``structured`` method).
        bundle: The loaded brief documents.
        tool_api: The tool contracts (vocabulary and field names the IR must use).
        ledger: The rule ledger produced by :func:`builder.understand.ledger.build_rule_ledger`.
        trace: Run trace (``None`` silences it).
        deadline: ``time.monotonic()`` value after which the gateway starts no new attempt.

    Returns:
        The IR after mechanical corrections; it may still have validator errors (the caller validates and repairs).

    Raises:
        builder.llm.errors.AllProvidersFailed: No provider produced a valid IR.
    """
    prompt = render_prompt_parts("extract/policy_ir.v1.j2", **policy_prompt_context(bundle, tool_api, ledger))
    ir = await call_structured(
        llm,
        purpose="extract policy IR from the rule ledger",
        schema=PolicyIR,
        system=prompt.system,
        user=prompt.user,
        role=LLMRole.STRONG,
        max_tokens=MAX_OUTPUT_TOKENS,
        deadline=deadline,
    )
    ir, fixes = apply_safe_fixes(ir, ledger, tool_api)
    for line in fixes:
        trace_event(trace, "extract", f"safe fix: {line}")
    for problem in check_phrase_grounding(ir, bundle):
        trace_event(trace, "grounding", f"warning: {problem}")
    trace_event(
        trace,
        "extract",
        f"policy IR extracted: {len(ir.bands)} band(s), {len(ir.intents)} intent(s), "
        f"{len(ir.closing.rows)} outcome row(s), {len(ir.residual_rules)} residual rule(s)",
        cited_rules=len(ir.referenced_rule_ids()),
    )
    return ir
