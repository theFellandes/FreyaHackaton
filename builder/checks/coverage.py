"""Rule coverage: does every active rule of the ledger have a place in the compiled workflow where it is enforced?

The compiler returns a ``rule_map`` (``rule id -> list of mechanism locations``, for example ``node:negotiate/task_message``,
``code:account_facts`` or ``tool:create_promise/post_process_code``). :func:`coverage_report` compares it with the ledger:

* **covered** - active rules with at least one location;
* **uncovered** - active rules nobody enforces (a gap in the compiler or in the IR);
* **superseded** - rules replaced by a later document; they must not be enforced and are reported separately.

The report is written to the trace and shown in ``docs/COMPILER.md``'s coverage section of a build; the graph treats a
non-empty ``uncovered`` list as a warning (a reason for a repair round), never as a crash.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from pydantic import BaseModel, Field

from builder.policy.ir import RuleLedger

__all__ = ["CoverageReport", "coverage_report"]


class CoverageReport(BaseModel):
    """Result of :func:`coverage_report`."""

    covered: dict[str, list[str]] = Field(default_factory=dict, description="Active rule id -> where it is enforced.")
    uncovered: list[str] = Field(default_factory=list, description="Active rule ids without any enforcement.")
    superseded: list[str] = Field(default_factory=list, description="Ids replaced by a later rule (not required).")
    enforced_superseded: list[str] = Field(
        default_factory=list, description="Superseded ids the rule map nevertheless enforces (a defect)."
    )
    unknown: list[str] = Field(default_factory=list, description="Rule-map ids that are not in the ledger.")

    @property
    def ratio(self) -> float:
        """Share of active rules that are covered (1.0 for an empty ledger)."""
        total = len(self.covered) + len(self.uncovered)
        return 1.0 if total == 0 else len(self.covered) / total

    @property
    def ok(self) -> bool:
        """True when every active rule is covered and no superseded rule is enforced."""
        return not self.uncovered and not self.enforced_superseded

    def summary(self) -> str:
        """One line for the trace."""
        return (
            f"{len(self.covered)} of {len(self.covered) + len(self.uncovered)} active rules enforced "
            f"({self.ratio:.0%}); {len(self.superseded)} superseded; uncovered: {', '.join(self.uncovered) or 'none'}"
        )


def coverage_report(ledger: RuleLedger, rule_map: Mapping[str, Sequence[str]]) -> CoverageReport:
    """Compare the ledger's active rules with the compiler's ``rule_map``.

    Rules replaced by a later document (``superseded_by`` or another rule's ``supersedes``) are listed under
    ``superseded`` and never counted as uncovered.
    """
    superseded = ledger.superseded_ids()
    known = set(ledger.ids())
    covered: dict[str, list[str]] = {}
    uncovered: list[str] = []
    for rule in ledger.rules:
        if rule.id in superseded:
            continue
        places = [place for place in rule_map.get(rule.id, ()) if place]
        if places:
            covered.setdefault(rule.id, list(dict.fromkeys(places)))
        elif rule.id not in uncovered:
            uncovered.append(rule.id)
    return CoverageReport(
        covered=covered,
        uncovered=uncovered,
        superseded=[rule_id for rule_id in ledger.ids() if rule_id in superseded],
        enforced_superseded=[rule_id for rule_id in rule_map if rule_id in superseded and rule_map[rule_id]],
        unknown=[rule_id for rule_id in rule_map if rule_id not in known],
    )
