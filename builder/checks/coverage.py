"""Rule coverage: does every active rule of the ledger have a place in the compiled workflow that is meant to apply it?

The compiler returns a ``rule_map`` (``rule id -> list of mechanism locations``, for example ``node:negotiate/task_message``,
``code:account_facts`` or ``tool:create_promise/post_process_code``). :func:`coverage_report` compares it with the ledger:

* **covered** - active rules with at least one location (with ``workflow`` given: a location that exists in it);
* **uncovered** - active rules no location is mapped to (a gap in the compiler or in the IR);
* **superseded** - rules replaced by a later document; they must not be applied and are reported separately;
* **dangling** - mapped locations that name no node, code step, tool or hook of the workflow (they do not count).

A rule map is the compiler's *declaration* of where a rule is applied, not evidence that the workflow behaves that
way (audit A9 / F09): the summary therefore says "mapped", never "enforced". Behavioural evidence comes from the
offline invariants, the walkthrough audit and the practice calls.

The report is written to the trace and shown in ``docs/COMPILER.md``'s coverage section of a build; the graph treats a
non-empty ``uncovered`` list as a warning (a reason for a repair round), never as a crash.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel, Field

from builder.policy.ir import RuleLedger

__all__ = ["CoverageReport", "coverage_report", "location_exists"]


class CoverageReport(BaseModel):
    """Result of :func:`coverage_report`."""

    covered: dict[str, list[str]] = Field(default_factory=dict, description="Active rule id -> where it is mapped.")
    uncovered: list[str] = Field(default_factory=list, description="Active rule ids without any mapped location.")
    superseded: list[str] = Field(default_factory=list, description="Ids replaced by a later rule (not required).")
    enforced_superseded: list[str] = Field(
        default_factory=list, description="Superseded ids the rule map nevertheless maps to a location (a defect)."
    )
    unknown: list[str] = Field(default_factory=list, description="Rule-map ids that are not in the ledger.")
    dangling: dict[str, list[str]] = Field(
        default_factory=dict, description="Rule id -> mapped locations that do not exist in the workflow (not counted)."
    )
    locations_checked: bool = Field(default=False, description="True when the locations were checked against a workflow.")

    @property
    def ratio(self) -> float:
        """Share of active rules that are mapped (1.0 for an empty ledger)."""
        total = len(self.covered) + len(self.uncovered)
        return 1.0 if total == 0 else len(self.covered) / total

    @property
    def ok(self) -> bool:
        """True when every active rule is mapped and no superseded rule is."""
        return not self.uncovered and not self.enforced_superseded

    def summary(self) -> str:
        """One line for the trace. "Mapped" = the compiler declares a location; it is not proof of behaviour."""
        where = "existing workflow locations" if self.locations_checked else "workflow locations (not checked)"
        dangling = sum(len(places) for places in self.dangling.values())
        return (
            f"{len(self.covered)} of {len(self.covered) + len(self.uncovered)} active rules mapped to {where} "
            f"({self.ratio:.0%}; a declaration, not behavioural evidence); {len(self.superseded)} superseded; "
            + (f"{dangling} dangling location(s); " if self.locations_checked else "")
            + f"uncovered: {', '.join(self.uncovered) or 'none'}"
        )


def _nodes(workflow: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    nodes: dict[str, Mapping[str, Any]] = {}
    for key in ("nodes", "global_nodes"):
        for name, node in (workflow.get(key) or {}).items():
            if isinstance(node, Mapping):
                nodes[str(name)] = node
    return nodes


def location_exists(workflow: Mapping[str, Any], place: str) -> bool:
    """True when a rule-map location names something the workflow has.

    Forms: ``system_prompt``; ``node:<name>`` / ``node:<name>/<field>`` (the field non-empty); ``code:<name>`` (a node of
    that name); ``tool:<function>`` / ``tool:<function>/<hook>`` (a node offers the tool; the hook is non-empty). Any
    other form is not recognised and counts as missing.
    """
    nodes = _nodes(workflow)
    kind, _, rest = place.partition(":")
    if place == "system_prompt":
        return bool(str(workflow.get("system_prompt") or "").strip())
    name, _, part = rest.partition("/")
    if kind in ("node", "code"):
        node = nodes.get(name)
        if node is None:
            return False
        return not part or bool(node.get(part))
    if kind == "tool":
        for node in nodes.values():
            for tool in node.get("tools") or []:
                function = tool.get("function") if isinstance(tool, Mapping) else None
                if isinstance(function, Mapping) and function.get("name") == name and (not part or tool.get(part)):
                    return True
        return False
    return False


def coverage_report(
    ledger: RuleLedger, rule_map: Mapping[str, Sequence[str]], workflow: Mapping[str, Any] | None = None
) -> CoverageReport:
    """Compare the ledger's active rules with the compiler's ``rule_map``.

    Rules replaced by a later document (``superseded_by`` or another rule's ``supersedes``) are listed under
    ``superseded`` and never counted as uncovered. With ``workflow``, a location that does not exist in it is reported
    under ``dangling`` and does not cover its rule.
    """
    superseded = ledger.superseded_ids()
    known = set(ledger.ids())
    covered: dict[str, list[str]] = {}
    uncovered: list[str] = []
    dangling: dict[str, list[str]] = {}
    for rule in ledger.rules:
        if rule.id in superseded:
            continue
        places = list(dict.fromkeys(place for place in rule_map.get(rule.id, ()) if place))
        if workflow is not None:
            missing = [place for place in places if not location_exists(workflow, place)]
            if missing:
                dangling.setdefault(rule.id, missing)
            places = [place for place in places if place not in missing]
        if places:
            covered.setdefault(rule.id, places)
        elif rule.id not in uncovered:
            uncovered.append(rule.id)
    return CoverageReport(
        covered=covered,
        uncovered=uncovered,
        superseded=[rule_id for rule_id in ledger.ids() if rule_id in superseded],
        enforced_superseded=[rule_id for rule_id in rule_map if rule_id in superseded and rule_map[rule_id]],
        unknown=[rule_id for rule_id in rule_map if rule_id not in known],
        dangling=dangling,
        locations_checked=workflow is not None,
    )
