"""Interactive development playground. Run: uv run --extra ui streamlit run streamlit_app.py."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from dataclasses import asdict
from datetime import date, timedelta
from typing import Any
from uuid import uuid4

import streamlit as st
from pydantic import ValidationError

from builder import paths
from builder.diagnostics import redact_data
from builder.freya.client import FreyaClient
from builder.ingest.tool_api import load_tool_api
from builder.monitor_views import render_monitor, render_robustness
from builder.playground import ChatSession, PracticeBank, json_object, public_error, saved_workflows
from builder.playground_views import render_inputs, render_results
from builder.policy.calendar import (
    BusinessCalendar,
    allowed_callback_days,
    check_callback,
    check_promise_date,
    deadline_for,
    parse_now,
    resolve_proposal_date,
)
from builder.policy.ir import PolicyIR
from builder.policy.money import check_promise_amount, effective_floor, fmt_amount
from builder.policy.outcomes import CallSituation, decide_outcome
from builder.settings import get_settings, secret_values
from builder.trace import TraceWriter


def public_cases() -> list[dict[str, Any]]:
    return json.loads((paths.REPO_ROOT / "public-test-set.json").read_text(encoding="utf-8"))


def case_label(case: dict[str, Any]) -> str:
    return f"{case['id']} · {case['title']}"


def show_case(case: dict[str, Any]) -> None:
    with st.expander("Customer script and expected behavior"):
        st.write(case["script"])
        st.write("Security answers", case["identity"])
        st.json({"products": case["products"], "expected": case["expected"], "checks": case["checks"]})


def render_chat() -> None:
    st.subheader("Chat with a deployed agent")
    st.caption("Send customer replies, inspect tool activity, and check the mock bank's records.")
    try:
        settings = get_settings()
    except ValidationError:
        st.error("The .env configuration is invalid. Check its values, then restart Streamlit.")
        return
    if not settings.freya_api_key:
        st.info("Set FREYA_API_KEY (or FREYA_KEY) in .env, then restart Streamlit to enable live chat.")
    st.caption("Live messages use Freya credits and can invoke the workflow's bank tools.")
    session: ChatSession | None = st.session_state.get("conversation")
    active = session is not None and not session.ended
    workflows = saved_workflows(settings.freya_state_file)
    # The kit and reference evaluations may use their own state file.
    workflows = {**saved_workflows(paths.REPO_ROOT / ".freya-agents.json"), **workflows}
    with st.expander("Conversation setup", expanded=session is None):
        options = [*workflows, "Enter workflow ID"]
        slot = st.selectbox("Saved workflow", options, disabled=active, key="chat_slot")
        workflow_id = st.text_input(
            "Workflow ID", value=workflows.get(slot, ""), disabled=active, key=f"workflow_{slot}"
        ).strip()
        version_id = st.text_input("Version ID (optional)", disabled=active, key="chat_version").strip()
        mode = st.radio(
            "Customer source",
            ["Public practice case", "Custom variables"],
            disabled=active,
            horizontal=True,
            key="chat_source",
        )
        case = None
        if mode == "Public practice case":
            cases = public_cases()
            case = st.selectbox(
                "Practice case", cases, format_func=case_label, disabled=active, key="chat_case"
            )
            show_case(case)
            st.caption(
                "Preparing creates a fresh mock-bank session with this case's customer and account data."
            )
            variables_text = ""
        else:
            defaults = {"customer_id": "", **public_cases()[0]["variables"]}
            variables_text = st.text_area(
                "Call variables (JSON)",
                json.dumps(defaults, indent=2),
                height=190,
                disabled=active,
                key="chat_variables",
            )
        if st.button("Prepare conversation", type="primary", disabled=active or not settings.freya_api_key):
            trace = None
            try:
                if not workflow_id:
                    raise ValueError("Select a saved workflow or enter its workflow ID.")
                variables = json_object(variables_text) if case is None else {}
                if case is None and not variables.get("customer_id"):
                    raise ValueError("Provide a customer_id for the workflow's bank tools.")
                trace = TraceWriter(uuid4().hex, "playground", secrets=secret_values(settings))
                bank = None
                if case is not None:
                    api = load_tool_api(paths.KIT_TOOLS_DIR / "openapi.json")
                    bank = PracticeBank(api.base_url, settings, trace)
                    with st.spinner("Preparing mock-bank customer…"):
                        variables = bank.create_session(case["id"])
                previous_trace = st.session_state.get("chat_trace")
                if previous_trace is not None:
                    previous_trace.close()
                st.session_state.conversation = ChatSession(
                    FreyaClient(trace), workflow_id, variables, version_id or None, case=case, bank=bank
                )
                st.session_state.chat_trace = trace
                st.session_state.pop("call_details", None)
                st.session_state.pop("ledger_details", None)
                st.rerun()
            except (OSError, ValueError, RuntimeError) as exc:
                if trace is not None:
                    trace.close()
                st.error(public_error(exc, settings))
    if session is None:
        st.info("Prepare a conversation, then type the customer's first reply below.")
        return
    st.caption(f"Workflow {session.workflow_id} · Call {session.call_id or 'waiting for first message'}")
    with st.expander("Current call variables", expanded=False):
        st.json(session.variables)
    for message in session.messages:
        with st.chat_message(message["role"]):
            st.write(message["content"])
    if session.ended:
        st.success("Conversation ended. You can inspect its records or prepare another conversation.")
    if message := st.chat_input("Speak as the customer…", disabled=session.ended):
        try:
            with st.spinner("Waiting for the agent…"):
                turn = session.send(message)
            if turn.warning:
                st.session_state.chat_warning = turn.warning
            st.session_state.pop("call_details", None)
            st.session_state.pop("ledger_details", None)
            st.rerun()
        except (OSError, ValueError, RuntimeError) as exc:
            st.error(public_error(exc, settings))
            st.caption(
                "The request was not retried. Inspect the call before resending after a network error."
            )
    if warning := st.session_state.pop("chat_warning", None):
        st.warning(warning)
    end, inspect, ledger = st.columns(3)
    if end.button("End conversation", disabled=session.ended):
        try:
            session.end()
            st.rerun()
        except (OSError, ValueError, RuntimeError) as exc:
            st.error(public_error(exc, settings))
    if inspect.button("Refresh call details", disabled=session.call_id is None):
        try:
            st.session_state.call_details = session.client.get_call(session.call_id)
        except (OSError, ValueError, RuntimeError) as exc:
            st.error(public_error(exc, settings))
    if ledger.button("Refresh bank ledger", disabled=session.bank is None):
        try:
            st.session_state.ledger_details = session.bank.ledger(str(session.variables["customer_id"]))
        except (OSError, ValueError, RuntimeError) as exc:
            st.error(public_error(exc, settings))
    if "call_details" in st.session_state:
        with st.expander("Transcript and engine events", expanded=True):
            st.json(redact_data(st.session_state.call_details, secret_values(settings)))
    if "ledger_details" in st.session_state:
        ledger_data = st.session_state.ledger_details
        with st.expander("Bank ledger", expanded=True):
            st.json(redact_data(ledger_data, secret_values(settings)))
            if session.case is not None:
                # Reuse the unmodified official ledger scorer; the transcript judge is a separate check.
                spec = importlib.util.spec_from_file_location(
                    "playground_score", paths.KIT_TOOLS_DIR / "score.py"
                )
                scorer = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(scorer)
                try:
                    result = scorer.score_ledger(session.case, ledger_data)
                    if result["pass"]:
                        st.success(
                            "Official ledger checks passed. Conversation checks require the practice judge."
                        )
                    else:
                        st.warning("Ledger checks have not passed.")
                        st.write(result["failed"])
                except (KeyError, TypeError, ValueError):
                    st.info("The ledger is not complete enough to score yet.")
    trace = st.session_state.get("chat_trace")
    if trace and trace.jsonl_path:
        st.caption(f"Trace: {trace.jsonl_path.name}")
    st.download_button(
        "Download conversation",
        json.dumps(
            {
                "workflow_id": session.workflow_id,
                "call_id": session.call_id,
                "variables": session.variables,
                "ended": session.ended,
                "messages": session.messages,
            },
            indent=2,
        ),
        "conversation.json",
        "application/json",
    )


def render_policy() -> None:
    st.subheader("Policy lab")
    st.caption(
        "Exercise the project's real deterministic helpers against an audited policy or your own JSON."
    )
    source = st.selectbox("Policy", ["Brief A", "Brief A + B", "Upload policy JSON"], key="policy_source")
    if source == "Upload policy JSON":
        upload = st.file_uploader("PolicyIR JSON", type=["json"], key="policy_upload")
        if upload is not None:
            raw = upload.getvalue()
            st.session_state.uploaded_policy = {
                "name": upload.name,
                "text": raw.decode("utf-8-sig", errors="replace"),
                "digest": hashlib.sha256(raw).hexdigest(),
            }
        stored_upload = st.session_state.get("uploaded_policy")
        if stored_upload is None:
            st.info("Upload a PolicyIR document to test its rules.")
            return
        original = stored_upload["text"]
        editor_key = f"policy_editor_{stored_upload['digest']}"
        st.caption(f"Uploaded policy: {stored_upload['name']}")
    else:
        name = "brief_a_ir.json" if source == "Brief A" else "brief_ab_ir.json"
        original = (paths.REPO_ROOT / "tests" / "fixtures" / "policy" / name).read_text(encoding="utf-8")
        editor_key = f"policy_editor_{name}"
    documents = st.session_state.setdefault("policy_documents", {})
    if editor_key not in st.session_state:
        st.session_state[editor_key] = documents.get(editor_key, original)
    with st.expander("Edit policy JSON"):
        edited = st.text_area("Policy document", height=300, key=editor_key)
        documents[editor_key] = edited
        st.session_state.last_policy_document = edited
        st.caption("Edits stay in this browser session. Fixture files are never overwritten.")
    try:
        ir = PolicyIR.model_validate_json(edited)
        cal = BusinessCalendar.from_ir(ir.calendar)
    except (ValueError, ValidationError) as exc:
        st.error(f"Invalid policy: {exc}")
        return
    cases = [case for case in public_cases() if source != "Brief A" or case["brief"] == "A"]
    case = st.selectbox("Scenario preset", cases, format_func=case_label, key="policy_case")
    key = f"{source}_{case['id']}"
    show_case(case)
    now_text = st.text_input("Call time (now)", case["variables"]["now"], key=f"now_{key}")
    st.caption("Dates use the call's local time, as the project helpers do.")
    try:
        now = parse_now(now_text)
    except (ValueError, OverflowError) as exc:
        st.error(str(exc))
        return
    payment, callback, outcome, calendar = st.tabs(
        ["Payment promise", "Callback", "Call outcome", "Calendar"]
    )
    with payment:
        render_payment(ir, cal, case, now.date(), key)
    with callback:
        rule = ir.third_party.callback
        if rule is None:
            st.info("This policy does not define a callback rule.")
        else:
            days = allowed_callback_days(now, cal, rule)
            st.caption(f"Rule: {rule.allowed_days} · {rule.window_start} to {rule.window_end}")
            if days:
                st.write("Allowed dates", [day.isoformat() for day in days])
            cday = st.date_input("Callback date", value=days[-1] if days else now.date(), key=f"cb_day_{key}")
            ctime = st.text_input("Callback time (HH:MM)", rule.window_start, key=f"cb_time_{key}")
            try:
                check = check_callback(cday, ctime, now, cal, rule)
                (st.success if check.ok else st.warning)(f"Callback status: {check.status}")
            except (ValueError, ArithmeticError) as exc:
                st.error(str(exc))
    with outcome:
        render_outcome(ir, key)
    with calendar:
        st.dataframe(
            [
                {
                    "Date": (now.date() + timedelta(days=i)).isoformat(),
                    "Weekday": (now.date() + timedelta(days=i)).strftime("%A"),
                    "Business day": cal.is_business_day(now.date() + timedelta(days=i)),
                }
                for i in range(min(35, (date.max - now.date()).days + 1))
            ],
            hide_index=True,
            width="stretch",
        )
        st.write("Policy holidays", [day.isoformat() for day in ir.calendar.holidays])
    st.download_button(
        "Download current policy", ir.model_dump_json(indent=2), "policy.json", "application/json"
    )


def render_payment(ir: PolicyIR, cal: BusinessCalendar, case: dict[str, Any], today: date, key: str) -> None:
    products = case["products"]
    index = st.selectbox(
        "Account product",
        range(len(products)),
        format_func=lambda i: f"Product {i + 1} · {products[i]['type']}",
        key=f"product_{key}",
    )
    product = products[index]
    product_key = f"{key}_{index}"
    columns = st.columns(3)
    full = columns[0].text_input("Past due amount", str(product["past_due"]), key=f"full_{product_key}")
    floor_field = ir.amounts.floor_field(product["type"])
    floor = columns[1].text_input(
        "Minimum acceptable amount (blank = no floor)",
        str(product.get(floor_field, "")),
        key=f"floor_{product_key}",
    )
    days_late = columns[2].number_input(
        "Days late", min_value=0, value=product["days_late"], key=f"late_{product_key}"
    )
    band = ir.band_for(days_late)
    if band is None:
        st.warning("No policy band covers this number of days late.")
        return
    try:
        deadline = deadline_for(band.deadline, today, cal)
        lowest = effective_floor(full, floor or None)
        st.write(
            f"Band: **{band.name}** · Deadline: **{deadline.isoformat()}** · "
            f"Effective floor: **{fmt_amount(lowest) if lowest is not None else 'full amount only'}**"
        )
        amount = st.text_input(
            "Proposed payment amount", str(product["past_due"]), key=f"amount_{product_key}"
        )
        proposed = st.date_input("Proposed payment date", today, key=f"date_{product_key}")
        amount_status = check_promise_amount(amount, full, floor or None, ir.amounts)
        date_check = check_promise_date(proposed, today, cal, ir.promise_dates, deadline)
        (st.success if amount_status == "ok" and date_check.ok else st.warning)(
            f"Amount: {amount_status} · Date: {date_check.status}"
        )
        if date_check.suggestion:
            st.write("Suggested date", date_check.suggestion.isoformat())
        steps = ir.persuasion.steps_for(band.name, band.persuasion_attempts)
        if steps:
            st.write("Persuasion sequence")
            st.dataframe(
                [
                    {
                        "Attempt": step.attempt,
                        "Amount": step.amount,
                        "Date": str(
                            resolve_proposal_date(step.date, today, cal, deadline) or "customer supplies date"
                        ),
                        "Wording": step.text,
                    }
                    for step in steps
                ],
                hide_index=True,
                width="stretch",
            )
    except (ValueError, ArithmeticError) as exc:
        st.error(str(exc))


def render_outcome(ir: PolicyIR, key: str) -> None:
    st.caption(
        "Enter observed facts and recorded tool results. The outcome engine does not infer them from a script."
    )
    left, right = st.columns(2)
    party = left.selectbox(
        "Who answered",
        ["confirmed", "third_party", "wrong_number", "deceased", "unclear"],
        key=f"party_{key}",
    )
    verified = left.checkbox("Identity verified", value=True, key=f"verified_{key}")
    identity_failed = left.checkbox("Identity failed", key=f"identity_failed_{key}")
    declined = left.checkbox("Identity declined", key=f"declined_{key}")
    error = right.checkbox("Recording failed after retries", key=f"record_error_{key}")
    callback_booked = right.checkbox("Callback booked", key=f"callback_booked_{key}")
    callback_failed = right.checkbox("Callback failed", key=f"callback_failed_{key}")
    keys = sorted({intent.key for intent in ir.intents} | {row.situation for row in ir.closing.rows})
    situations = st.multiselect("Detected situations", keys, key=f"situations_{key}")
    records_key = f"records_{key}"
    saved_records = st.session_state.setdefault("call_records", {})
    if records_key not in st.session_state:
        st.session_state[records_key] = saved_records.get(
            records_key,
            json.dumps({"promises": [], "notes": [], "transfers": [], "product_order": []}, indent=2),
        )
    records = st.text_area("Recorded tools (JSON)", height=170, key=records_key)
    saved_records[records_key] = records
    try:
        data = json_object(records)
        for collection in ("promises", "notes", "transfers"):
            rows = data.get(collection, [])
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise ValueError(f"{collection} must be a list of objects.")
        order = data.get("product_order", [])
        if not isinstance(order, list) or any(not isinstance(item, str) for item in order):
            raise ValueError("product_order must be a list of product IDs.")
        situation = CallSituation.from_records(
            promises=data.get("promises", []),
            notes=data.get("notes", []),
            transfers=data.get("transfers", []),
            product_order=data.get("product_order", []),
            party=party,
            verified=verified,
            identity_failed=identity_failed,
            identity_declined=declined,
            situations=set(situations),
            service_error=error,
            callback_booked=callback_booked,
            callback_failed=callback_failed,
        )
        decision = decide_outcome(ir, situation)
        st.json({**asdict(decision), "outcome": decision.outcome.model_dump(mode="json")})
    except (ValueError, TypeError, KeyError) as exc:
        st.error(f"Invalid call records: {exc}")


def main() -> None:
    st.set_page_config(page_title="Freya · Behavior playground", page_icon="☎", layout="wide")
    with st.sidebar:
        st.title("Freya")
        st.caption("BEHAVIOR PLAYGROUND")
        page = st.radio(
            "Explore",
            [
                "Chat with agent",
                "Policy lab",
                "Robustness checks",
                "Request monitor",
                "Inspect inputs",
                "Practice results",
            ],
            index=1,
            key="navigation",
        )
        st.divider()
        st.caption(
            "Policy, input, and result views run locally. Live chat starts only after you prepare a conversation."
        )
    st.title("Behavior playground")
    renderers = {
        "Chat with agent": render_chat,
        "Policy lab": render_policy,
        "Robustness checks": render_robustness,
        "Request monitor": render_monitor,
        "Inspect inputs": render_inputs,
        "Practice results": render_results,
    }
    try:
        renderers[page]()
    except (OSError, ValueError, KeyError, ArithmeticError) as exc:
        st.error(f"Could not load this view: {exc}")


if __name__ == "__main__":
    main()
