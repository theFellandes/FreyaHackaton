"""The compiler's single entry point: PolicyIR + ToolApi + RuleLedger (+ prose) -> a Freya workflow dict.

:func:`compile_workflow` chooses the profile (``staged``: the validated design; ``lean``: fewer nodes, the model closes the
call), builds the node graph, validates its shape with :class:`~builder.compiler.workflow_model.Workflow` and returns a
:class:`CompiledWorkflow`: the JSON-ready workflow (no ``null`` anywhere), the rule map (which mechanism enforces which
ledger rule) and notes for the trace.

The compiler is pure: no I/O, no LLM. Static checks (``builder.checks.lint``) and coverage
(``builder.checks.coverage``) run afterwards on the result.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from builder.compiler.errors import CompileError
from builder.compiler.profiles.lean import build_lean_with_notes
from builder.compiler.profiles.staged import build_staged_with_notes
from builder.compiler.prose import NodeProse, default_prose
from builder.ingest.tool_api import ToolApi
from builder.policy.ir import PolicyIR, RuleLedger

__all__ = ["PROFILES", "CompiledWorkflow", "compile_workflow"]

Profile = Literal["staged", "lean"]
PROFILES: tuple[str, ...] = ("staged", "lean")


class CompiledWorkflow(BaseModel):
    """Result of :func:`compile_workflow`.

    Attributes:
        workflow: The workflow JSON (the value of the REST ``workflow`` key); never contains ``null``.
        rule_map: Ledger rule id -> where the compiled agent enforces it (``node:negotiate/task_message``,
            ``code:account_facts``, ``tool:verify_identity/post_process_code`` ...).
        profile: The profile used.
        notes: Non-fatal findings (for example a queue that is not in the tool API's enum).
    """

    workflow: dict[str, Any]
    rule_map: dict[str, list[str]] = Field(default_factory=dict)
    profile: str = "staged"
    notes: list[str] = Field(default_factory=list)

    @property
    def node_count(self) -> int:
        """Number of nodes (global nodes included)."""
        return len(self.workflow.get("nodes", {})) + len(self.workflow.get("global_nodes", {}) or {})


def compile_workflow(
    ir: PolicyIR,
    tool_api: ToolApi,
    ledger: RuleLedger,
    prose: NodeProse | None = None,
    profile: Profile = "staged",
) -> CompiledWorkflow:
    """Compile the policy into a workflow.

    Args:
        ir: The structured policy.
        tool_api: Contracts of the bank's tools (URLs, fields, enums).
        ledger: The rule ledger (uncovered rules are placed verbatim in the prompts; the rule map is built against it).
        prose: Optional LLM-drafted node prose; ``None`` uses :func:`~builder.compiler.prose.default_prose`.
        profile: ``"staged"`` (default) or ``"lean"``.

    Raises:
        CompileError: For an unknown profile or when the tool API lacks a tool the policy needs.
    """
    node_prose = prose if prose is not None else default_prose(ir, ledger)
    if profile == "staged":
        workflow, rule_map, notes = build_staged_with_notes(ir, tool_api, ledger, node_prose)
    elif profile == "lean":
        workflow, rule_map, notes = build_lean_with_notes(ir, tool_api, ledger, node_prose)
    else:
        raise CompileError(f"unknown profile {profile!r} (use one of {', '.join(PROFILES)})")
    return CompiledWorkflow(workflow=workflow.to_json(), rule_map=rule_map, profile=profile, notes=notes)
