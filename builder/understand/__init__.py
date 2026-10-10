"""Understanding the briefs: from raw documents to a rule ledger and a structured policy IR.

Pipeline (see ``docs/UNDERSTANDING.md``):

1. :func:`~builder.understand.ledger.build_rule_ledger` - read every file, one atomic quoted rule per entry, amendments
   and ambiguities recorded;
2. :func:`~builder.understand.grounding.check_grounding` - every quote must really be in the briefs;
3. :func:`~builder.understand.extract.extract_policy` - the ledger becomes a :class:`~builder.policy.ir.PolicyIR`;
4. :func:`~builder.understand.validate.validate_policy` and :func:`~builder.understand.repair.repair_policy` - check the
   IR against the tool API and the ledger, repair what is wrong;
5. :func:`~builder.understand.prose.draft_prose` - short task messages for the conversational nodes.

Everything here is generic: brief values enter only as data (documents, ledger, IR), never as code or constants.
"""

from __future__ import annotations

from builder.understand.extract import extract_policy
from builder.understand.fixes import apply_safe_fixes
from builder.understand.grounding import (
    check_grounding,
    check_phrase_grounding,
    snap_quotes,
    uncovered_passages,
)
from builder.understand.ledger import build_rule_ledger, ledger_prompt_lines
from builder.understand.prose import NodeProse, draft_prose, sanitize_prose
from builder.understand.repair import repair_policy
from builder.understand.validate import (
    PolicyIssue,
    errors_of,
    format_issues,
    has_errors,
    phrase_checklist,
    phrase_requirements,
    tool_vocabulary,
    validate_policy,
)

__all__ = [
    "NodeProse",
    "PolicyIssue",
    "apply_safe_fixes",
    "build_rule_ledger",
    "check_grounding",
    "check_phrase_grounding",
    "draft_prose",
    "errors_of",
    "extract_policy",
    "format_issues",
    "has_errors",
    "ledger_prompt_lines",
    "phrase_checklist",
    "phrase_requirements",
    "repair_policy",
    "sanitize_prose",
    "snap_quotes",
    "tool_vocabulary",
    "uncovered_passages",
    "validate_policy",
]
