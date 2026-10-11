"""Record explicitly supplied caller turns and platform events; never retry a chat turn."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from builder.freya.client import FreyaClient  # noqa: E402
from builder.settings import apply_kit_env, get_settings, secret_values  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workflow", required=True)
    parser.add_argument("--customer", required=True)
    parser.add_argument("--now", required=True)
    parser.add_argument("--message", action="append", required=True)
    parser.add_argument("--call-id")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("evidence output already exists")
    settings = get_settings()
    apply_kit_env(settings)
    client = FreyaClient(None)
    evidence = {"started_at_utc": datetime.now(timezone.utc).isoformat(),
                "workflow_id": args.workflow,
                "customer_variables": {"customerNumber": args.customer, "now": args.now},
                "initial_call_id": args.call_id, "turns": []}
    call_id = args.call_id
    try:
        for message in args.message:
            turn = client.chat(args.workflow, message, evidence["customer_variables"] if not call_id else None,
                               call_id=call_id)
            call_id = turn.call_id
            evidence["turns"].append({"caller": message, **turn.model_dump(mode="json")})
            print(json.dumps({"call_id": call_id, "caller": message, "response": turn.response,
                              "ended_by_agent": turn.ended_by_agent}, ensure_ascii=True), flush=True)
            if turn.ended_by_agent:
                break
        if call_id:
            evidence["platform_call"] = client.get_call(call_id)
        evidence["completed"] = True
    except Exception as exc:
        evidence["completed"] = False
        evidence["error_type"] = type(exc).__name__
        print(json.dumps({"error_type": type(exc).__name__, "call_id": call_id}), flush=True)
    evidence["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
    text = json.dumps(evidence, ensure_ascii=False, indent=2) + "\n"
    redactions = 0
    for secret in secret_values(settings):
        count = text.count(secret)
        if count:
            text = text.replace(secret, "[REDACTED_SECRET]")
            redactions += count
    evidence_path = args.output.resolve()
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    with evidence_path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    print(json.dumps({"evidence": str(evidence_path), "completed": evidence["completed"],
                      "known_secret_redactions": redactions}), flush=True)
    return 0 if evidence["completed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
