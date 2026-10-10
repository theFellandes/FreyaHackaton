"""Failure triage for practice runs (dev tool, not part of the builder).

Reads a `tools/run_cases.py --out` JSON file, fetches the call of every failed attempt from Freya and prints a compact
timeline: what the customer said, what the agent said, node entries, tool calls with arguments, tool results, route
decisions and engine errors. This is the fastest way to see *why* the judge or the ledger failed a case.

A results file holds every repetition (`all_rows`), the last one again as `rows` and the verdict per case as `final`.
The hidden benchmark counts a case only when it passes in *every* repetition, so the cases worth reading are those
that failed in some repetition - not only in the last one. The triage therefore walks all repetitions and fetches the
call of each failing attempt, labelled with its repetition number.

    python evals/triage.py results/ref_a_v1.json              # every failing attempt of every failing case
    python evals/triage.py results/ref_a_v1.json --all        # every case, every repetition
    python evals/triage.py results/ref_a_v1.json --only PUB-04,PUB-07 --vars   # all repetitions of those cases
    python evals/triage.py results/ref_a_v1.json --rep 2      # one repetition (1-based; `last` for the final one)
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from collections.abc import Iterator

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _short(value: object, limit: int = 220) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def render_call(call: dict, show_vars: bool = False) -> list[str]:
    """Turn a Freya call record into readable timeline lines."""
    lines: list[str] = []
    for m in call.get("messages", []):
        role = m.get("role")
        t = m.get("secondsFromStart")
        stamp = f"{t:6.1f}s" if isinstance(t, (int, float)) else "      "
        if role in ("assistant", "user"):
            who = "AGENT " if role == "assistant" else "CUSTOMER"
            lines.append(f"{stamp} {who}: {_short(m.get('message', ''), 400)}")
            continue
        if role != "system":
            continue
        try:
            ev = json.loads(m.get("message") or "{}")
        except (TypeError, ValueError):
            lines.append(f"{stamp} SYSTEM {_short(m.get('message'))}")
            continue
        kind = ev.get("event") or ev.get("type")
        if kind == "node_enter":
            lines.append(
                f"{stamp}   > node {ev.get('node')} (from {ev.get('from_node')}; {_short(ev.get('reason', ''), 80)})"
            )
        elif kind == "function_call_start":
            args = ev.get("args") or {}
            body = args.get("body") if isinstance(args, dict) else None
            lines.append(
                f"{stamp}   * call {ev.get('function')} {_short(body if body is not None else args)}"
            )
        elif kind == "function_call_complete":
            res = ev.get("result")
            if isinstance(res, dict) and "data" in res:
                res = res.get("data")
            lines.append(f"{stamp}   = result {ev.get('function')} {_short(res)}")
        elif kind == "route_evaluated":
            lines.append(
                f"{stamp}   ~ route {ev.get('from_node')} -> {ev.get('target')} matched={ev.get('matched')} {_short(ev.get('reason', ''), 120)}"
            )
            if show_vars and ev.get("variable_table"):
                lines.append(f"            vars {_short(ev.get('variable_table'), 1500)}")
        elif kind == "error":
            lines.append(
                f"{stamp}   ! ERROR {_short(ev.get('message'))} {_short(ev.get('details', ''), 300)}"
            )
        else:
            lines.append(f"{stamp}   . {kind} {_short(ev, 160)}")
    lines.append(f"        ended: {call.get('endedReason')}  duration: {call.get('duration')}")
    return lines


def numbered_repetitions(data: dict) -> tuple[int, list[tuple[int, list[dict]]]]:
    """`(number of repetitions, [(1-based number, rows), ...])` of a results file, oldest repetition first.

    Files written by `tools/run_cases.py` carry `all_rows`, one list of rows per repetition. An older or hand-made file
    may only have `rows` (the last repetition); that one is then numbered as the last of `runs` repetitions.
    """
    all_rows = data.get("all_rows")
    if isinstance(all_rows, list) and all_rows and all(isinstance(rep, list) for rep in all_rows):
        return len(all_rows), list(enumerate(all_rows, start=1))
    runs = max(int(data.get("runs") or 1), 1)
    return runs, [(runs, data.get("rows") or [])]


def select_attempts(
    data: dict, *, show_all: bool = False, only: set[str] | None = None, rep: int | None = None
) -> Iterator[tuple[int, int, dict]]:
    """Yield `(rep_number, rep_count, row)` for the attempts to triage, grouped by case, repetitions in order.

    By default only failing attempts are yielded. `only` (case ids) and `show_all` also yield the passing attempts
    of the chosen cases, which is what you want when comparing a failing repetition with a passing one. `rep` (1-based)
    restricts the walk to one repetition.
    """
    count, numbered = numbered_repetitions(data)
    order: list[str] = []
    for _, rows in numbered:
        for row in rows:
            if row.get("id") not in order:
                order.append(row.get("id"))
    for case_id in order:
        if only and case_id not in only:
            continue
        for number, rows in numbered:
            if rep is not None and number != rep:
                continue
            for row in rows:
                if row.get("id") == case_id and (only or show_all or not row.get("pass")):
                    yield number, count, row


def summarize_final(data: dict) -> list[str]:
    """One line per case whose verdict over all repetitions is a failure (empty when `final` is missing)."""
    return [
        f"{f['id']} passed {f.get('passes')}/{f.get('runs')} repetitions"
        for f in data.get("final") or []
        if not f.get("pass")
    ]


def parse_rep(value: str, parser: argparse.ArgumentParser) -> int | str:
    """`--rep` takes a 1-based repetition number or `last`."""
    if value == "last":
        return value
    try:
        number = int(value)
    except ValueError:
        parser.error("--rep takes a repetition number (1-based) or 'last'")
    if number < 1:
        parser.error("--rep is 1-based")
    return number


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        description="Print the Freya call timeline of every failing attempt of a results file."
    )
    ap.add_argument("results")
    ap.add_argument(
        "--all", action="store_true", help="every case, every repetition (not only the failing attempts)"
    )
    ap.add_argument("--only", help="comma-separated case ids; shows all their repetitions, passing or not")
    ap.add_argument("--rep", help="only this repetition (1-based, or `last`)")
    ap.add_argument("--vars", action="store_true", help="print the variable table of route events")
    a = ap.parse_args(argv)
    rep = parse_rep(a.rep, ap) if a.rep else None
    import freya  # kit client; needs FREYA_API_KEY in the environment

    data = json.loads(pathlib.Path(a.results).read_text(encoding="utf-8"))
    if rep == "last":
        rep = numbered_repetitions(data)[0]
    only = set(a.only.split(",")) if a.only else None
    lost = summarize_final(data)
    if lost:
        print("cases lost over all repetitions: " + "; ".join(lost))
    shown = 0
    for number, total, row in select_attempts(data, show_all=a.all, only=only, rep=rep):
        shown += 1
        print("=" * 110)
        print(
            f"{row['id']} {row.get('family')} rep {number}/{total} pass={row.get('pass')} "
            f"tools={' > '.join(row.get('tools') or [])}"
        )
        for f in row.get("failed") or []:
            print(f"  FAIL: {_short(f, 600)}")
        if not row.get("call_id"):
            print("  (no call id)")
            continue
        try:
            call = freya.get_call(row["call_id"])
        except Exception as exc:  # dev tool: print and continue with the next attempt
            print(f"  could not fetch call: {exc}")
            continue
        for line in render_call(call, a.vars):
            print("  " + line)
    if not shown:
        print(
            "no failing attempts to show"
            + (" (pass --all or --only to see passing ones)" if not lost else "")
        )


if __name__ == "__main__":
    main()
