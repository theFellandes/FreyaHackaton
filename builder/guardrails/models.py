"""Result types shared by every guard: a violation, and the report of one guard stage.

A guard never raises and never edits what it checks. It returns a :class:`GuardReport`; the caller (a graph node)
decides: ``blocked`` reports stop or repair the build, warnings only go to the trace.

Severity:

* ``block`` - the artifact must not move on (an invented rule, a foreign URL in the workflow, a leaked key ...);
* ``warn``  - suspicious but not provably wrong (a prompt-injection looking sentence in a brief, a huge input ...).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["GuardReport", "GuardSeverity", "GuardStage", "GuardViolation"]

GuardSeverity = Literal["block", "warn"]
GuardStage = Literal["inputs", "ledger", "policy", "prose", "workflow"]


class GuardViolation(BaseModel):
    """One finding of a guard.

    Attributes:
        guard: Short stable id of the check that fired (``injection``, ``foreign_url``, ``secret`` ...); tests and
            repair prompts key on it.
        severity: ``block`` or ``warn``.
        message: One readable sentence. Never contains a secret value.
        location: Where the finding is (``brief-a.md:12``, ``bands[0].deadline.n``, ``nodes.identity.on_enter.text``).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    guard: str
    severity: GuardSeverity
    message: str
    location: str = ""

    def __str__(self) -> str:
        where = f" [{self.location}]" if self.location else ""
        return f"{self.severity.upper()} {self.guard}{where}: {self.message}"


class GuardReport(BaseModel):
    """All findings of one guard stage."""

    model_config = ConfigDict(extra="forbid")

    stage: GuardStage
    violations: list[GuardViolation] = Field(default_factory=list)

    # ---- building ---------------------------------------------------------------------------------------------

    def add(self, guard: str, severity: GuardSeverity, message: str, location: str = "") -> GuardViolation:
        """Append a violation and return it."""
        violation = GuardViolation(guard=guard, severity=severity, message=message, location=location)
        self.violations.append(violation)
        return violation

    def block(self, guard: str, message: str, location: str = "") -> GuardViolation:
        """Append a ``block`` violation."""
        return self.add(guard, "block", message, location)

    def warn(self, guard: str, message: str, location: str = "") -> GuardViolation:
        """Append a ``warn`` violation."""
        return self.add(guard, "warn", message, location)

    def extend(self, violations: Iterable[GuardViolation]) -> None:
        """Append already built violations."""
        self.violations.extend(violations)

    # ---- reading ----------------------------------------------------------------------------------------------

    @property
    def blocking(self) -> list[GuardViolation]:
        """The ``block`` violations."""
        return [v for v in self.violations if v.severity == "block"]

    @property
    def warnings(self) -> list[GuardViolation]:
        """The ``warn`` violations."""
        return [v for v in self.violations if v.severity == "warn"]

    @property
    def blocked(self) -> bool:
        """True when at least one violation blocks."""
        return any(v.severity == "block" for v in self.violations)

    def trace_message(self) -> str:
        """The body of the trace event: ``<stage> | ok|blocked | <n> violations`` (warnings count).

        ``Trace.event("guard", report.trace_message())`` renders as ``<ts> | GUARD  | inputs | ok | 0 violations``.
        """
        count = len(self.violations)
        noun = "violation" if count == 1 else "violations"
        return f"{self.stage} | {'blocked' if self.blocked else 'ok'} | {count} {noun}"

    def summary(self) -> str:
        """The whole trace line of this stage: ``GUARD | <stage> | ok|blocked | <n> violations``."""
        return f"GUARD | {self.trace_message()}"

    def details(self, *, limit: int = 40) -> str:
        """The summary line followed by one line per violation (blocking ones first), capped at ``limit``."""
        ordered = [*self.blocking, *self.warnings]
        lines = [self.summary(), *(f"  {violation}" for violation in ordered[:limit])]
        if len(ordered) > limit:
            lines.append(f"  ... and {len(ordered) - limit} more")
        return "\n".join(lines)

    def blocking_messages(self) -> list[str]:
        """Blocking findings as ``[location] message`` strings - the feedback a repair prompt receives."""
        return [f"[{v.location}] {v.message}" if v.location else v.message for v in self.blocking]
