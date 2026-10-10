"""Self-tests of a deployed agent: smoke chat, walkthrough audit and the organisers' practice cases.

* :mod:`builder.selftest.smoke` - one two-turn chat conversation (about a minute);
* :mod:`builder.selftest.walkthrough` - a cheap model audits the compiled workflow against the rule ledger;
* :mod:`builder.selftest.practice` - the kit's public practice cases, only for the kit's own briefs and only with time left.

:func:`plan_self_tests` decides which of them run for a ``settings.self_test`` mode and the time that is left. Every
test returns the same dict shape ``{"name", "passed", "summary", "details"}`` and never raises.
"""

from __future__ import annotations

from builder.budget import STAGE_ESTIMATES_S, Budget

#: The tests in the order ``auto`` runs them (cheapest first).
ORDER: tuple[str, ...] = ("smoke", "walkthrough", "practice")
#: Seconds kept free after a test.
RESERVE_S = 120.0


def plan_self_tests(mode: str, budget: Budget, *, has_ledger: bool, has_agent: bool) -> list[str]:
    """The tests to run for ``mode`` (``auto``, ``none``, or one test name) given the time left.

    ``auto`` picks every test whose pessimistic estimate still fits; the walkthrough needs the ledger (compiled track),
    the smoke and practice tests need a deployed agent. An explicit mode runs only that test, if it can run at all.
    """
    if mode == "none":
        return []
    wanted = list(ORDER) if mode == "auto" else [mode]
    chosen: list[str] = []
    for name in wanted:
        if name not in ORDER:
            continue
        if name == "walkthrough" and not has_ledger:
            continue
        if name in ("smoke", "practice") and not has_agent:
            continue
        if mode == "auto" and not budget.allows(STAGE_ESTIMATES_S[name], RESERVE_S):
            continue
        chosen.append(name)
    return chosen


__all__ = ["ORDER", "plan_self_tests"]
