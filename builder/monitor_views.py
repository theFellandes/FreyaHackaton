"""Read-only diagnostics and policy robustness views for the development playground."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import streamlit as st
from pydantic import ValidationError

from builder import paths
from builder.diagnostics import (
    events_from_call,
    needs_attention,
    read_trace,
    redact_data,
    trace_rows,
    trace_summary,
)
from builder.freya.client import FreyaClient
from builder.ingest.tool_api import load_tool_api
from builder.playground import public_error, saved_workflows
from builder.policy.ir import PolicyIR
from builder.robustness import run_robustness
from builder.settings import Settings, get_settings, secret_values
from builder.trace import TraceWriter


def _settings() -> Settings | None:
    try:
        return get_settings()
    except ValidationError:
        st.error("The .env configuration is invalid. Check its values and restart Streamlit.")
        return None


def _client(settings: Settings) -> FreyaClient:
    trace = st.session_state.get("monitor_trace")
    if trace is None or trace.closed:
        trace = TraceWriter(uuid4().hex, "monitor", secrets=secret_values(settings))
        st.session_state.monitor_trace = trace
    return FreyaClient(trace)


def render_monitor() -> None:
    st.subheader("Request monitor")
    st.caption(
        "Inspect recorded requests and Freya state. Reads happen only when you press a refresh button."
    )
    settings = _settings()
    if settings is None:
        return
    secrets = secret_values(settings)
    activity, call, dashboard = st.tabs(["Request activity", "Call engine", "Freya dashboard"])
    with activity:
        _render_activity(secrets)
    with call:
        _render_call(settings, secrets)
    with dashboard:
        _render_dashboard(settings, secrets)


def _render_activity(secrets: list[str]) -> None:
    sources = ["Saved trace", "Current conversation", "Dashboard inspection", "Upload JSONL"]
    source = st.radio("Trace source", sources, horizontal=True, key="monitor_source")
    read = None
    if source == "Saved trace":
        files = sorted(paths.TRACES_DIR.glob("*.jsonl"), key=lambda path: path.stat().st_mtime, reverse=True)
        if files:
            selected = st.selectbox(
                "Trace file", files, format_func=lambda path: path.name, key="monitor_file"
            )
            read = read_trace(selected, secrets=secrets)
        else:
            st.info("No JSONL traces yet. Prepare a conversation or refresh Freya status to record activity.")
    elif source in {"Current conversation", "Dashboard inspection"}:
        key = "chat_trace" if source == "Current conversation" else "monitor_trace"
        trace = st.session_state.get(key)
        if trace and trace.jsonl_path:
            read = read_trace(trace.jsonl_path, secrets=secrets)
        else:
            st.info("There is no trace for this source yet.")
    else:
        upload = st.file_uploader("Request or builder trace (.jsonl)", type=["jsonl"], key="monitor_upload")
        if upload is not None:
            with TemporaryDirectory(prefix="freya-monitor-") as temporary:
                target = Path(temporary) / "trace.jsonl"
                target.write_bytes(upload.getvalue())
                read = read_trace(target, secrets=secrets)
    st.button("Reload recorded activity", key="monitor_reload")
    st.caption(
        "Freya telemetry counts wrapper attempts. A save/export wrapper can contain multiple HTTP calls. "
        "Successful status codes are unknown where the kit does not expose them. Agent tool calls appear "
        "in Call engine; builder model attempts appear here when present in the selected trace. "
        "Standalone kit/harness traffic outside these wrappers is not captured by this monitor."
    )
    if read is None:
        return
    if read.partial_line:
        st.caption("The last trace line is still being written; it will appear on the next reload.")
    if read.truncated:
        st.warning("This is a bounded trace excerpt. Totals below cover only the loaded records.")
    for error in read.errors:
        st.warning(error)
    summary = trace_summary(read.records)
    first, second, third, fourth = st.columns(4)
    first.metric("Request attempts", summary["requests"])
    second.metric("Failed attempts", summary["request_failures"])
    third.metric("Builder model attempts", summary["model_calls"])
    fourth.metric("Model failures", summary["model_failures"])
    rows = trace_rows(read.records, secrets=secrets)
    if st.checkbox("Only errors and retries", key="monitor_errors_only"):
        # The same failure predicate as the "Failed attempts" counter, so the two can never disagree.
        rows = [row for row in rows if needs_attention(row)]
    if rows:
        table = [{key: value for key, value in row.items() if key != "details"} for row in rows]
        st.dataframe(table, hide_index=True, width="stretch")
        with st.expander("Attempt details"):
            st.json(rows)
    else:
        st.info(
            "No request/model attempt records match this selection. Older traces may contain action events only."
        )
    st.download_button(
        "Download redacted trace",
        "\n".join(json.dumps(record) for record in read.records),
        "requests.jsonl",
        "application/x-ndjson",
    )


def _render_call(settings: Settings, secrets: list[str]) -> None:
    conversation = st.session_state.get("conversation")
    default_id = conversation.call_id if conversation and conversation.call_id else ""
    if pending := st.session_state.pop("pending_monitor_call_id", None):
        st.session_state.monitor_call_id = pending
    call_id = st.text_input("Call ID to inspect", default_id, key="monitor_call_id").strip()
    if st.button(
        "Read call events", disabled=not settings.freya_api_key or not call_id, key="monitor_read_call"
    ):
        try:
            with st.spinner("Reading transcript and engine events…"):
                record = _client(settings).get_call(call_id)
            st.session_state.monitor_call = {"call_id": call_id, "data": redact_data(record, secrets)}
        except (OSError, ValueError, RuntimeError) as exc:
            st.error(public_error(exc, settings))
    stored = st.session_state.get("monitor_call")
    if stored is None:
        st.info("Enter a saved practice call ID or the active conversation's call ID.")
        return
    st.caption(f"Loaded call: {stored['call_id']}")
    record = stored["data"]
    rows = events_from_call(record, secrets)
    errors = [row for row in rows if row["event"] == "error"]
    st.metric("Engine errors", len(errors))
    if rows:
        st.dataframe(
            [{key: value for key, value in row.items() if key != "details"} for row in rows],
            hide_index=True,
            width="stretch",
        )
        with st.expander("Tool results, routes, and variable snapshots", expanded=bool(errors)):
            st.json(rows)
    else:
        st.info("The call has no recorded engine events yet.")
    with st.expander("Full redacted call"):
        st.json(record)


def _render_dashboard(settings: Settings, secrets: list[str]) -> None:
    st.link_button("Open Freya dashboard", settings.freya_api_url)
    st.caption(
        "This view reads the same workflow, version, simulation, and call data through documented APIs. "
        "The browser dashboard uses its own sign-in session."
    )
    workflows = {
        **saved_workflows(paths.REPO_ROOT / ".freya-agents.json"),
        **saved_workflows(settings.freya_state_file),
    }
    slot = st.selectbox("Workflow to check", [*workflows, "Enter workflow ID"], key="monitor_slot")
    workflow_id = st.text_input(
        "Workflow ID to check", workflows.get(slot, ""), key=f"monitor_wf_{slot}"
    ).strip()
    if st.button(
        "Refresh workflow status",
        disabled=not settings.freya_api_key or not workflow_id,
        key="monitor_workflow_refresh",
    ):
        try:
            with st.spinner("Reading workflow and publication status…"):
                record = _client(settings).workflow_status(workflow_id)
            st.session_state.monitor_workflow = redact_data(record, secrets)
        except (OSError, ValueError, RuntimeError) as exc:
            st.error(public_error(exc, settings))
    status = st.session_state.get("monitor_workflow")
    if status is not None:
        st.caption(f"Loaded workflow: {status['workflow_id']}")
        if status.get("newest_published") is False:
            st.warning("The newest workflow version is unpublished. Live calls may use an older version.")
        elif status.get("newest_published") is True:
            st.success("The newest workflow version is published.")
        else:
            st.info("Publication status is unknown; no usable version record was returned.")
        st.json(status)
    simulation_id = st.text_input(
        "Simulation ID (from a saved practice report)", key="monitor_sim_id"
    ).strip()
    if st.button(
        "Refresh simulation status",
        disabled=not settings.freya_api_key or not simulation_id,
        key="monitor_sim_refresh",
    ):
        try:
            st.session_state.monitor_simulation = redact_data(
                _client(settings).simulation_status(simulation_id), secrets
            )
        except (OSError, ValueError, RuntimeError) as exc:
            st.error(public_error(exc, settings))
    if "monitor_simulation" in st.session_state:
        st.json(st.session_state.monitor_simulation)
    if not settings.freya_api_key:
        st.info("Configure FREYA_API_KEY or FREYA_KEY in .env to enable live reads.")


def render_robustness() -> None:
    st.subheader("Robustness checks")
    st.caption(
        "Hidden customers are unavailable. These offline checks exercise invariants from the supplied policy "
        "and tool API across synthetic boundary inputs; they do not predict the hidden score."
    )
    source = st.selectbox(
        "Policy to check",
        ["Current policy from lab", "Brief A", "Brief A + B", "Upload policy JSON"],
        key="robustness_source",
    )
    original = ""
    if source == "Current policy from lab":
        original = st.session_state.get("last_policy_document", "")
        if not original:
            original = (paths.REPO_ROOT / "tests/fixtures/policy/brief_a_ir.json").read_text(encoding="utf-8")
    elif source == "Upload policy JSON":
        upload = st.file_uploader("PolicyIR to check", type=["json"], key="robustness_policy")
        if upload is not None:
            original = upload.getvalue().decode("utf-8-sig", errors="replace")
    else:
        name = "brief_a_ir.json" if source == "Brief A" else "brief_ab_ir.json"
        original = (paths.REPO_ROOT / "tests/fixtures/policy" / name).read_text(encoding="utf-8")
    api_source = st.radio(
        "Compatibility tool API",
        ["Bundled tool API", "Upload tool API"],
        horizontal=True,
        key="robustness_api_source",
    )
    api_upload = (
        st.file_uploader("OpenAPI / Swagger to check", type=["json", "yaml", "yml"], key="robustness_api")
        if api_source == "Upload tool API"
        else None
    )
    ready = bool(original) and (api_source != "Upload tool API" or api_upload is not None)
    fingerprint = hashlib.sha256(
        original.encode() + (api_upload.getvalue() if api_upload else b"bundled")
    ).hexdigest()
    if st.button("Run offline checks", type="primary", disabled=not ready, key="robustness_run"):
        try:
            ir = PolicyIR.model_validate_json(original)
            with (
                st.spinner("Checking policy boundaries and tool compatibility…"),
                TemporaryDirectory() as temporary,
            ):
                if api_upload is not None:
                    target = Path(temporary) / "openapi.yaml"
                    target.write_bytes(api_upload.getvalue())
                else:
                    target = paths.KIT_TOOLS_DIR / "openapi.json"
                report = run_robustness(ir, load_tool_api(target))
            st.session_state.robustness_report = {"fingerprint": fingerprint, "report": report}
        except (OSError, ValueError, ArithmeticError) as exc:
            st.error(f"Could not run policy checks: {exc}")
    stored = st.session_state.get("robustness_report")
    if stored is not None:
        if stored["fingerprint"] != fingerprint:
            st.warning("The policy or API selection changed. Run checks again to update this report.")
        report = stored["report"]
        first, second, third, fourth = st.columns(4)
        first.metric("Invariant evaluations", report["cases_tested"])
        second.metric("Passed groups", report["passed"])
        third.metric("Failed groups", report["failed"])
        fourth.metric("Unchecked groups", report["unchecked"])
        if report["failed"]:
            st.error("Some invariants failed. Inspect the counterexamples before relying on this policy.")
        elif report["unchecked"]:
            st.warning("The evaluated invariants passed; unchecked rules still need separate verification.")
        else:
            st.success(
                "All evaluated invariants passed. Conversation and hidden-customer behavior still need evaluation."
            )
        st.dataframe(
            [
                {
                    key: value
                    for key, value in check.items()
                    if key in {"name", "status", "cases_tested", "failures"}
                }
                for check in report["checks"]
            ],
            hide_index=True,
            width="stretch",
        )
        with st.expander("Counterexamples and limitations", expanded=bool(report["failed"])):
            st.json(report)
        st.download_button(
            "Download robustness report",
            json.dumps(stored["report"], indent=2),
            "robustness.json",
            "application/json",
        )
    st.divider()
    st.write(
        "Conversation evidence still requires the official practice harness: three repetitions per customer, "
        "ledger plus transcript checks, and the five supported speaking styles."
    )
    st.code(
        "uv run python tools/run_cases.py <agent_id> --reps 3 --style pieces --out results/pieces.json",
        language="sh",
    )
    st.caption(
        "Repeat with corrects, talkative, asks_back, and in_words. Run one simulation at a time. "
        "These are paid live evaluations; offline checks here do not start them. "
        "Offline checks do not execute the deployed workflow or evaluate conversation behavior."
    )
