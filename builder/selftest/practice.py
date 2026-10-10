"""Practice self-test: the organisers' public practice cases against the deployed agent.

Only meaningful when the inputs are the kit's own briefs: the public cases belong to those exact briefs, so on any other
brief they would test the wrong rules (:func:`builder.freya.practice.kit_brief_letter` decides, by content hash). The run
takes minutes (one simulation per case set), so it is started only when the budget allows
(:data:`builder.budget.STAGE_ESTIMATES_S` ``["practice"]``), holds the process-wide simulation lock (the platform allows one
simulation per workspace) and is limited to the time that is left.

The harness is a blocking subprocess; it runs in an abandonable daemon worker thread
(:func:`builder.freya.background.run_blocking`), so the wall-clock guard never waits for it. Failures never raise.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from builder.budget import Budget, estimate
from builder.freya.background import CancelToken, run_blocking
from builder.freya.practice import kit_brief_letter, run_practice

#: Seconds kept free after the practice run for the decision and the finalisation.
RESERVE_S = 90.0
#: Longest practice run, whatever the budget says.
MAX_RUN_S = 1500.0


def applies_to(bundle: Any) -> str | None:
    """The kit brief letter whose public cases apply to ``bundle``, or ``None`` for any other brief."""
    return kit_brief_letter(bundle)


async def run_practice_selftest(
    freya: Any,
    agent_id: str,
    bundle: Any,
    budget: Budget,
    trace: Any,
    runner: Callable[..., Any] | None = None,
    *,
    cancel: CancelToken | None = None,
) -> dict[str, Any]:
    """Run the practice cases and return ``{"name", "passed", "summary", "details"}``.

    A result with ``details["skipped"]`` set means the test did not run (not the kit's brief, no time, no agent).
    """
    letter = applies_to(bundle)
    if letter is None:
        return _skipped("the inputs are not the kit's public briefs: the practice cases do not apply")
    if not agent_id:
        return _skipped("the deployment returned no agent id")
    if not budget.allows(estimate("practice"), RESERVE_S):
        return _skipped(f"not enough time left for the practice run ({budget.remaining():.0f} s)")
    timeout_s = max(60.0, min(MAX_RUN_S, budget.remaining() - RESERVE_S))
    call = runner or run_practice

    def work() -> Any:
        with freya.simulation_slot():
            return call(agent_id, letter, reps=1, timeout_s=timeout_s, trace=trace)

    try:
        result = await run_blocking(work, token=cancel)
    except Exception as exc:  # PracticeError, timeouts, missing credentials ...
        return {
            "name": "practice",
            "passed": False,
            "summary": f"the practice run failed: {type(exc).__name__}: {exc}"[:300],
            "details": {"skipped": False, "error": str(exc)[:500], "letter": letter, "failed_cases": []},
        }
    failed_ids = list(result.failed_ids)
    rows = [row for row in result.rows if not row.get("pass")]
    return {
        "name": "practice",
        "passed": result.passed == result.total and result.total > 0,
        "summary": f"brief {letter}: {result.passed}/{result.total} practice cases passed"
        + (f" (failed: {', '.join(failed_ids[:8])})" if failed_ids else ""),
        "details": {
            "skipped": False,
            "letter": letter,
            "passed": result.passed,
            "total": result.total,
            "failed_cases": [
                {
                    "id": str(row.get("id")),
                    "family": str(row.get("family", "")),
                    "failed": [str(item)[:240] for item in (row.get("failed") or [])][:6],
                }
                for row in rows
            ],
            "results_file": str(result.raw_path),
        },
    }


def _skipped(reason: str) -> dict[str, Any]:
    return {
        "name": "practice",
        "passed": True,
        "summary": f"skipped: {reason}",
        "details": {"skipped": True, "failed_cases": []},
    }


__all__ = ["applies_to", "run_practice_selftest"]
