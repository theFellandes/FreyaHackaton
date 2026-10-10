"""Local input and saved-evaluation views for the Streamlit playground."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import streamlit as st

from builder import paths
from builder.freya.practice import PracticeError, PracticeResult, parse_practice_results
from builder.ingest.brief_loader import load_briefs
from builder.ingest.tool_api import load_tool_api
from builder.inputs import InputError, classify_inputs


def _inspect_call(call_id: str) -> None:
    st.session_state.navigation = "Request monitor"
    st.session_state.pending_monitor_call_id = call_id


def _stage_upload(upload: Any, directory: Path, index: int) -> Path:
    """Keep filenames within a unique temporary directory, including on Windows."""
    basename = str(upload.name).replace("\\", "/").rsplit("/", 1)[-1]
    basename = re.sub(r"[^A-Za-z0-9._-]", "_", basename).strip(". ") or "upload.txt"
    destination = directory / str(index) / f"file-{basename[:150]}"
    destination.parent.mkdir()
    destination.write_bytes(upload.getvalue())
    return destination


def render_inputs() -> None:
    """Inspect bundled or uploaded brief material and OpenAPI contracts."""
    st.subheader("Inspect project inputs")
    st.caption("Read the same normalized brief text and tool contracts that the builder uses.")
    source = st.radio(
        "Input source", ["Brief A", "Brief A + B", "Upload files"], horizontal=True, key="inputs_source"
    )
    if source == "Upload files":
        briefs = st.file_uploader(
            "Brief files",
            accept_multiple_files=True,
            key="inputs_briefs",
            help="Markdown, text, email, HTML, JSON, CSV, DOCX, or PDF (requires the ingest extra).",
        )
        tool_api = st.file_uploader("OpenAPI / Swagger file", type=["json", "yaml", "yml"], key="inputs_api")
        if not briefs or tool_api is None:
            st.info("Add at least one brief and one OpenAPI / Swagger file to inspect them.")
            return
        try:
            with TemporaryDirectory(prefix="freya-playground-inputs-") as temporary:
                directory = Path(temporary)
                uploads = [*briefs, tool_api]
                input_paths = [_stage_upload(upload, directory, i) for i, upload in enumerate(uploads)]
                _render_input_paths(input_paths)
        except (InputError, OSError, ValueError) as exc:
            st.error(f"Could not inspect these inputs: {exc}")
        return

    input_paths = [paths.REPO_ROOT / "briefs" / "brief-a.md"]
    if source == "Brief A + B":
        input_paths.append(paths.REPO_ROOT / "briefs" / "brief-b.md")
    input_paths.append(paths.KIT_TOOLS_DIR / "openapi.json")
    try:
        _render_input_paths(input_paths)
    except (InputError, OSError, ValueError) as exc:
        st.error(f"Could not inspect these inputs: {exc}")


def _render_input_paths(input_paths: list[Path]) -> None:
    inputs = classify_inputs(input_paths, slot="playground")
    bundle = load_briefs(inputs.brief_paths)
    api = load_tool_api(inputs.tool_api_path)
    first, second, third = st.columns(3)
    first.metric("Brief documents", len(bundle.documents))
    second.metric("Tool contracts", len(api.tools))
    third.metric("Normalized characters", sum(len(document.text) for document in bundle.documents))
    st.caption(f"Brief bundle SHA-256: {bundle.sha256}")

    for index, document in enumerate(bundle.documents):
        with st.expander(f"{index + 1}. {document.name} ({document.kind})"):
            st.code(document.text, language=None, wrap_lines=True)
            st.caption(f"Raw file SHA-256: {document.sha256}")
    st.download_button(
        "Download normalized brief text",
        bundle.combined_text(),
        file_name="normalized-briefs.txt",
        mime="text/plain",
        key="inputs_download",
    )

    st.write(f"Tool API: {api.title or 'Untitled'} · version {api.version or 'unspecified'}")
    if api.missing_tools():
        st.warning("Missing expected tools: " + ", ".join(api.missing_tools()))
    if api.extra_tools():
        st.info("Additional tools: " + ", ".join(api.extra_tools()))
    if api.tool_names:
        selected = st.selectbox("Inspect tool", api.tool_names, key="inputs_tool")
        st.code(api.require(selected).describe(), language=None, wrap_lines=True)
    with st.expander("Full parsed contracts"):
        st.json(api.model_dump(mode="json"))


def render_results() -> None:
    """Inspect saved or uploaded practice reports without running simulations."""
    st.subheader("Inspect practice results")
    st.caption("These are saved evaluations. A case passes only when every repetition passes.")
    source = st.radio("Report source", ["Saved run", "Upload JSON"], horizontal=True, key="results_source")
    if source == "Upload JSON":
        upload = st.file_uploader("Practice results JSON", type=["json"], key="results_upload")
        if upload is None:
            st.info("Upload a report produced by tools/run_cases.py to inspect it.")
            return
        try:
            with TemporaryDirectory(prefix="freya-playground-results-") as temporary:
                artifact = _stage_upload(upload, Path(temporary), 0)
                _render_report(artifact)
        except (PracticeError, OSError, ValueError, TypeError) as exc:
            st.error(f"Could not read this practice report: {exc}")
        return

    artifacts = sorted(
        path for path in paths.RESULTS_DIR.glob("*.json") if path.name != "base_agent_ids.json"
    )
    if not artifacts:
        st.info("No practice reports are available in results/. Upload a report to inspect it.")
        return
    default = next((i for i, path in enumerate(artifacts) if path.name == "ref_a_v4_reps3.json"), 0)
    artifact = st.selectbox(
        "Saved practice report",
        artifacts,
        index=default,
        format_func=lambda path: path.name,
        key="results_artifact",
    )
    try:
        _render_report(artifact)
    except (PracticeError, OSError, ValueError, TypeError) as exc:
        st.error(f"Could not read this practice report: {exc}")


def _read_report(artifact: Path) -> tuple[PracticeResult, dict[str, Any]]:
    report = parse_practice_results(artifact)
    raw = json.loads(artifact.read_text(encoding="utf-8"))
    if any(isinstance(raw.get(key), bool) for key in ("passed", "total", "runs")):
        raise ValueError("passed, total, and runs must be counts, not booleans")
    if not 0 <= report.passed <= report.total or report.runs < 1:
        raise ValueError("counts must satisfy 0 <= passed <= total and runs >= 1")
    for key in ("rows", "final"):
        value = raw.get(key, [])
        if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
            raise ValueError(f"{key} must be a list of case objects")
        _validate_rows(value)
    all_rows = raw.get("all_rows", [])
    if not isinstance(all_rows, list):
        raise ValueError("all_rows must be a list of repetitions")
    for repetition in all_rows:
        if not isinstance(repetition, list) or any(not isinstance(row, dict) for row in repetition):
            raise ValueError("each all_rows repetition must be a list of case objects")
        _validate_rows(repetition)
    return report, raw


def _validate_rows(rows: list[dict[str, Any]]) -> None:
    for row in rows:
        if not isinstance(row.get("pass"), bool):
            raise ValueError("each case row must contain a boolean pass verdict")
        if not isinstance(row.get("id"), str):
            raise ValueError("each case row must contain a string id")


def _render_report(artifact: Path) -> None:
    report, raw = _read_report(artifact)
    first, second, third, fourth = st.columns(4)
    first.metric("Cases passed", f"{report.passed} / {report.total}")
    second.metric("Pass rate", f"{report.pass_rate:.0%}")
    third.metric("Repetitions", report.runs)
    fourth.metric("Elapsed seconds", report.seconds if report.seconds is not None else "—")
    failed_ids = report.failed_ids
    if failed_ids:
        st.warning("Failed across repetitions: " + ", ".join(failed_ids))
    elif report.total:
        st.success("Every evaluated case passed.")

    verdicts = report.final or report.rows
    families: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in verdicts:
        families[str(row.get("family") or "Unspecified")].append(row)
    if families:
        st.dataframe(
            [
                {"Family": family, "Passed": sum(row["pass"] for row in rows), "Cases": len(rows)}
                for family, rows in sorted(families.items())
            ],
            hide_index=True,
            width="stretch",
        )
    if verdicts:
        st.dataframe(
            [
                {
                    "Case": row["id"],
                    "Family": str(row.get("family", "")),
                    "Passed": row["pass"],
                    "Successful repetitions": row.get("passes"),
                    "Repetitions": row.get("runs", report.runs),
                }
                for row in verdicts
            ],
            hide_index=True,
            width="stretch",
        )

    repetitions = raw.get("all_rows") or [report.rows]
    repetition = st.selectbox(
        "Inspect repetition",
        range(len(repetitions)),
        format_func=lambda index: (
            f"Repetition {index + 1}" if raw.get("all_rows") else "Last saved repetition"
        ),
        key="results_repetition",
    )
    rows = repetitions[repetition]
    failures_only = st.checkbox("Show only failed cases", key="results_failures_only")
    visible = [row for row in rows if not failures_only or not row["pass"]]
    if not visible:
        st.info("No case details match this filter.")
    for row in visible:
        label = f"{row['id']} · {'PASS' if row['pass'] else 'FAIL'} · {row.get('family', 'Unspecified')}"
        with st.expander(label, expanded=not row["pass"]):
            if row.get("call_id"):
                st.button(
                    "Inspect call requests",
                    key=f"inspect_call_{repetition}_{row['id']}",
                    on_click=_inspect_call,
                    args=(str(row["call_id"]),),
                )
            failures = row.get("failed") or []
            if isinstance(failures, str):
                failures = [failures]
            for failure in failures:
                st.code(str(failure), language=None, wrap_lines=True)
            tools = row.get("tools")
            if isinstance(tools, list):
                st.write("Tool sequence: " + " → ".join(str(tool) for tool in tools))
            st.json(row)
    with st.expander("Report metadata"):
        st.json({key: value for key, value in raw.items() if key not in {"rows", "final", "all_rows"}})
