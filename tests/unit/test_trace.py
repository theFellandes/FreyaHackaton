"""Tests for builder.trace: file layout, line format, redaction, summary block, thread safety, NullTrace."""

from __future__ import annotations

import io
import json
import re
import threading
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from pathlib import Path

import pytest
from pydantic import BaseModel, SecretStr

from builder.trace import REDACTED, NullTrace, TraceWriter

ELLIPSIS = "…"
SECRET = "sk-ant-api03-SUPERSECRETVALUE0000"
FREYA_SECRET = "fak_live_freya_key_123456"
TS_RE = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z"


class StepClock:
    """Wall clock + monotonic clock pair that advance together, one tick per read of the monotonic clock."""

    def __init__(self, start: datetime) -> None:
        self.wall = start
        self.mono = 100.0

    def now(self) -> datetime:
        return self.wall

    def monotonic(self) -> float:
        return self.mono

    def advance(self, seconds: float) -> None:
        self.wall += timedelta(seconds=seconds)
        self.mono += seconds


@pytest.fixture
def clock() -> StepClock:
    return StepClock(datetime(2026, 10, 11, 17, 2, 3, 123456, tzinfo=UTC))


@pytest.fixture
def writer(tmp_path: Path, clock: StepClock) -> TraceWriter:
    trace = TraceWriter(
        "run-1",
        "brief-a",
        tmp_path,
        secrets=[SECRET, FREYA_SECRET],
        clock=clock.now,
        monotonic=clock.monotonic,
    )
    yield trace
    trace.close()


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# --- files and format -------------------------------------------------------------------------------------------------


def test_files_are_named_after_slot_and_utc_start(writer: TraceWriter, tmp_path: Path) -> None:
    assert writer.log_path == tmp_path / "brief-a-20261011T170203Z.log"
    assert writer.jsonl_path == tmp_path / "brief-a-20261011T170203Z.jsonl"
    assert writer.log_path.exists() and writer.jsonl_path.exists()


def test_existing_trace_is_never_overwritten(tmp_path: Path, clock: StepClock) -> None:
    first = TraceWriter("r1", "slot", tmp_path, clock=clock.now, monotonic=clock.monotonic)
    second = TraceWriter("r2", "slot", tmp_path, clock=clock.now, monotonic=clock.monotonic)
    first.close()
    second.close()
    assert first.log_path != second.log_path
    assert second.log_path is not None and second.log_path.name == "slot-20261011T170203Z-2.log"
    assert "r1" in first.log_path.read_text(encoding="utf-8")  # type: ignore[union-attr]


def test_unsafe_slot_characters_are_replaced_in_file_names(tmp_path: Path, clock: StepClock) -> None:
    trace = TraceWriter("r", "../evil slot/x", tmp_path, clock=clock.now, monotonic=clock.monotonic)
    trace.close()
    assert trace.log_path is not None and trace.log_path.parent == tmp_path
    assert trace.log_path.name.startswith("evil-slot-x-")


def test_default_directory_is_read_from_paths_at_call_time(tmp_dirs, clock: StepClock) -> None:  # type: ignore[no-untyped-def]
    trace = TraceWriter("r", "slot", clock=clock.now, monotonic=clock.monotonic)
    trace.close()
    assert trace.log_path is not None and trace.log_path.parent == tmp_dirs.traces


def test_start_line_and_jsonl_record(writer: TraceWriter) -> None:
    lines = writer.log_path.read_text(encoding="utf-8").splitlines()  # type: ignore[union-attr]
    assert re.fullmatch(
        r"2026-10-11T17:02:03\.123Z \| START  \| run run-1 started \| slot=brief-a \| builder .+", lines[0]
    )
    record = read_jsonl(writer.jsonl_path)[0]  # type: ignore[arg-type]
    assert record["kind"] == "start"
    assert record["seq"] == 1
    assert record["run_id"] == "run-1" and record["slot"] == "brief-a"
    assert record["data"]["started_at"] == "2026-10-11T17:02:03.123Z"


def test_model_call_line_matches_the_contract_example(writer: TraceWriter) -> None:
    writer.model_call(
        "anthropic",
        "claude-opus-5-5",
        f"anthropic#1({ELLIPSIS}f3a2)",
        "extract policy IR",
        True,
        12.3,
        12034,
        2981,
    )
    last = writer.log_path.read_text(encoding="utf-8").splitlines()[-1]  # type: ignore[union-attr]
    assert last == (
        f"2026-10-11T17:02:03.123Z | MODEL  | anthropic/claude-opus-5-5 | key=anthropic#1({ELLIPSIS}f3a2) "
        "| ok 12.3s in=12034 out=2981 | extract policy IR"
    )
    record = read_jsonl(writer.jsonl_path)[-1]  # type: ignore[arg-type]
    assert record["kind"] == "model_call"
    assert record["data"] == {
        "provider": "anthropic",
        "model": "claude-opus-5-5",
        "key_label": f"anthropic#1({ELLIPSIS}f3a2)",
        "purpose": "extract policy IR",
        "ok": True,
        "latency_s": 12.3,
        "input_tokens": 12034,
        "output_tokens": 2981,
    }


class Kind(StrEnum):
    QUOTA = "quota"


def test_failed_model_call_shows_kind_attempt_and_one_line_error(writer: TraceWriter) -> None:
    writer.model_call(
        "openai",
        "gpt-5.6-sol",
        "openai#2(...)",
        "draft node prose\n(second line)",
        False,
        0.44,
        error_kind=Kind.QUOTA,
        error="Error 429:\n You exceeded your current quota " + "x" * 600,
        attempt=2,
    )
    last = writer.log_path.read_text(encoding="utf-8").splitlines()[-1]  # type: ignore[union-attr]
    assert (
        "| openai/gpt-5.6-sol | key=openai#2(...) | FAIL quota 0.4s attempt=2 | draft node prose (second line) |"
        in last
    )
    assert "error: Error 429: You exceeded your current quota" in last
    assert len(last) < 600  # error text is shortened on the text line
    data = read_jsonl(writer.jsonl_path)[-1]["data"]  # type: ignore[arg-type]
    assert data["error_kind"] == "quota"
    assert data["attempt"] == 2
    assert data["error"].endswith("x" * 600)  # full error kept in JSONL


def test_every_event_is_exactly_one_text_line(writer: TraceWriter) -> None:
    writer.event("note", "first line\nsecond line\r\nthird", detail="a\nb")
    writer.decision("use the compiled track\nbecause the IR validated")
    writer.error("deploy failed:\n500 Internal Server Error", stage="deploy")
    lines = writer.log_path.read_text(encoding="utf-8").splitlines()  # type: ignore[union-attr]
    records = read_jsonl(writer.jsonl_path)  # type: ignore[arg-type]
    assert len(lines) == len(records) == 4
    assert all(re.match(rf"{TS_RE} \| \S+\s* \| ", line) for line in lines)
    assert lines[1].endswith("| NOTE   | first line second line third | detail=a b")
    assert "| DECIDE | use the compiled track because the IR validated" in lines[2]
    assert "| ERROR  | deploy failed: 500 Internal Server Error | stage=deploy" in lines[3]
    assert records[1]["message"] == "first line\nsecond line\r\nthird"  # JSONL keeps the original text


def test_stage_reports_duration_between_start_and_end(writer: TraceWriter, clock: StepClock) -> None:
    writer.stage("compile_workflow", "start")
    clock.advance(2.31)
    writer.stage("compile_workflow", "end", nodes=12)
    writer.stage("never_started", "failed")
    lines = writer.log_path.read_text(encoding="utf-8").splitlines()  # type: ignore[union-attr]
    assert lines[-3].endswith("| STAGE  | compile_workflow | start")
    assert lines[-2].endswith("| STAGE  | compile_workflow | end 2.3s | nodes=12")
    assert lines[-1].endswith("| STAGE  | never_started | failed")
    records = read_jsonl(writer.jsonl_path)  # type: ignore[arg-type]
    assert records[-2]["data"] == {
        "stage": "compile_workflow",
        "status": "end",
        "nodes": 12,
        "duration_s": 2.31,
    }
    assert "duration_s" not in records[-1]["data"]


def test_freya_test_and_ambiguity_lines(writer: TraceWriter) -> None:
    writer.freya("save", workflow_id="wf-1", agent_id="ag-1", version=3, node_count=11, tool_count=7)
    writer.test("smoke", False, "no opening sentence", turns=2)
    writer.ambiguity(["R3", "R43"], "Does a question count as the second ask?", "yes", "rule 3 says so")
    writer.freya("validate", ok=False, kind="schema", error="nodes.x: unknown field")
    lines = writer.log_path.read_text(encoding="utf-8").splitlines()  # type: ignore[union-attr]
    assert lines[-1].endswith("| FREYA  | validate | ok=False kind=schema error=nodes.x: unknown field")
    lines = lines[:-1]
    assert lines[-3].endswith(
        "| FREYA  | save | workflow_id=wf-1 agent_id=ag-1 version=3 node_count=11 tool_count=7"
    )
    assert lines[-2].endswith("| TEST   | FAIL smoke | no opening sentence | turns=2")
    assert lines[-1].endswith(
        "| AMBIG  | [R3, R43] Does a question count as the second ask? -> chosen: yes (because: rule 3 says so)"
    )


# --- redaction ----------------------------------------------------------------------------------------------------------


def test_secrets_are_redacted_everywhere(writer: TraceWriter) -> None:
    writer.event(
        "input",
        f"loaded key {SECRET}",
        nested={"headers": {"Authorization": f"Bearer {FREYA_SECRET}"}, "list": [SECRET, 1]},
        **{f"key_{SECRET}": "value"},
    )
    writer.model_call("anthropic", "m", "k", "p", False, 1.0, error=f"invalid x-api-key {SECRET}")
    writer.freya("save", url=f"https://x.test/?key={FREYA_SECRET}")
    writer.summary({"status": f"failed with {SECRET}"})
    writer.close()
    text = writer.log_path.read_text(encoding="utf-8")  # type: ignore[union-attr]
    raw_jsonl = writer.jsonl_path.read_text(encoding="utf-8")  # type: ignore[union-attr]
    for content in (text, raw_jsonl):
        assert SECRET not in content
        assert FREYA_SECRET not in content
        assert REDACTED in content
    for record in read_jsonl(writer.jsonl_path):  # type: ignore[arg-type]
        assert SECRET not in json.dumps(record)


def test_key_shaped_tokens_are_masked_without_configuration(tmp_path: Path) -> None:
    trace = NullTrace()
    trace.event(
        "note",
        "keys sk-ant-api03-abcdefghijklmnopqrstuv and sk-proj-abcdefghijklmnopqrstuvwxyz and fak_abcdefghijklmn",
        header="Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123",
    )
    line = trace.lines[-1]
    assert "abcdefghijklmnopqrstuv" not in line
    assert "fak_abcdefghijklmn" not in line
    assert "Bearer ***" in line
    record = json.dumps(trace.events[-1])
    assert "abcdefghijklmnopqrstuvwxyz0123" not in record
    assert "sk-ant-api03-abcdefghijklmnopqrstuv" not in record
    assert record.count(REDACTED) == 4


def test_secrets_added_later_and_short_secrets(tmp_path: Path) -> None:
    trace = NullTrace(secrets=["", "abc"])  # empty / too short secrets must not blank out everything
    trace.event("note", "abc stays visible")
    trace.add_secrets("late-secret-value")
    trace.event("note", "now late-secret-value is hidden")
    assert trace.lines[-2].endswith("abc stays visible")
    assert trace.lines[-1].endswith("now *** is hidden")
    assert trace.redact("x late-secret-value y") == "x *** y"


def test_overlapping_secrets_are_redacted_whole() -> None:
    trace = NullTrace(secrets=["abcd1234", "abcd1234efgh5678"])
    trace.event("note", "token abcd1234efgh5678 end")
    assert trace.lines[-1].endswith("token *** end")


# --- data conversion ----------------------------------------------------------------------------------------------------


class Model(BaseModel):
    name: str
    secret: SecretStr


@dataclass
class Point:
    x: int
    y: int


def test_non_json_data_is_converted(writer: TraceWriter) -> None:
    writer.event(
        "note",
        "conversions",
        path=Path("out") / "wf.json",
        when=date(2026, 10, 14),
        tags={"b", "a"},
        model=Model(name="n", secret=SecretStr("pydantic-secret-1")),
        point=Point(1, 2),
        blob=b"\x00\x01",
        nan=float("nan"),
        kind=Kind.QUOTA,  # data may use the names of the positional parameters
        message="data message",
        dropped=None,
    )
    data = read_jsonl(writer.jsonl_path)[-1]["data"]  # type: ignore[arg-type]
    assert data["path"] == "out/wf.json"
    assert data["when"] == "2026-10-14"
    assert data["tags"] == ["a", "b"]
    assert data["model"] == {"name": "n", "secret": "**********"}
    assert data["point"] == {"x": 1, "y": 2}
    assert data["blob"] == "<2 bytes>"
    assert data["nan"] == "nan"
    assert data["kind"] == "quota"
    assert data["message"] == "data message"
    assert "dropped" not in data
    record = read_jsonl(writer.jsonl_path)[-1]  # type: ignore[arg-type]
    assert (record["kind"], record["message"]) == ("note", "conversions")


# --- summary ------------------------------------------------------------------------------------------------------------


def test_summary_contains_everything_the_hackathon_requires(writer: TraceWriter, clock: StepClock) -> None:
    writer.model_call("anthropic", "claude-opus-5-5", "a#1", "build rule ledger", True, 30.0, 1000, 200)
    writer.model_call(
        "anthropic", "claude-opus-5-5", "a#2", "extract policy IR", False, 1.0, error_kind="quota"
    )
    writer.model_call("openai", "gpt-5.6-sol", "o#1", "extract policy IR", True, 40.0, 500, 100)
    writer.stage("deploy", "start")
    clock.advance(65.5)
    writer.stage("deploy", "end")
    writer.freya("save", workflow_id="wf-9", agent_id="ag-9", version=4, node_count=12, tool_count=7)
    writer.test("smoke", True, "opening ok")

    summary = writer.summary(
        {"track": "compiled", "status": "deployed", "exported": "out/brief-a.workflow.json"}
    )

    assert summary["started_at"] == "2026-10-11T17:02:03.123Z"
    assert summary["ended_at"] == "2026-10-11T17:03:08.623Z"
    assert summary["duration_s"] == 65.5
    assert summary["track"] == "compiled" and summary["status"] == "deployed" and summary["slot"] == "brief-a"
    assert summary["model_calls"]["total"] == 3
    assert summary["model_calls"]["failed"] == 1
    assert summary["model_calls"]["by_model"]["anthropic/claude-opus-5-5"] == {
        "calls": 2, "ok": 1, "failed": 1, "input_tokens": 1000, "output_tokens": 200,
    }  # fmt: skip
    assert summary["freya"] == {
        "workflow_id": "wf-9", "agent_id": "ag-9", "version": 4, "node_count": 12, "tool_count": 7,
        "exported": "out/brief-a.workflow.json",
    }  # fmt: skip
    assert summary["tests"] == [{"name": "smoke", "passed": True, "summary": "opening ok"}]
    assert summary["stages"] == [{"name": "deploy", "status": "end", "duration_s": 65.5}]

    text = writer.log_path.read_text(encoding="utf-8")  # type: ignore[union-attr]
    for expected in (
        "BUILD SUMMARY",
        "started      : 2026-10-11T17:02:03.123Z",
        "ended        : 2026-10-11T17:03:08.623Z",
        "duration     : 65.5 s (1 min 06 s)",
        "model calls  : 3 (2 ok, 1 failed)",
        "  - anthropic/claude-opus-5-5: calls=2 (1 ok, 1 failed), tokens in=1000 out=200",
        "  - workflow id: wf-9",
        "  - agent id: ag-9",
        "  - steps (nodes): 12",
        "  - tools: 7",
        "tests        : 1 (1 passed, 0 failed)",
        "  - PASS smoke: opening ok",
        "track        : compiled",
        "slot         : brief-a",
    ):
        assert expected in text
    summary_record = read_jsonl(writer.jsonl_path)[-1]  # type: ignore[arg-type]
    assert summary_record["kind"] == "summary"
    assert summary_record["data"]["freya"]["workflow_id"] == "wf-9"


def test_summary_caller_values_win_and_nothing_created(writer: TraceWriter) -> None:
    writer.test("smoke", True, "recorded by trace")
    summary = writer.summary(
        {"tests": [{"name": "walkthrough", "passed": False, "summary": "R12 missing"}], "status": "failed"}
    )
    assert summary["tests"] == [{"name": "walkthrough", "passed": False, "summary": "R12 missing"}]
    text = writer.log_path.read_text(encoding="utf-8")  # type: ignore[union-attr]
    assert "Freya        : nothing created" in text
    assert "  - FAIL walkthrough: R12 missing" in text


# --- lifecycle and robustness -------------------------------------------------------------------------------------------


def test_close_writes_end_line_and_is_idempotent(
    writer: TraceWriter, caplog: pytest.LogCaptureFixture
) -> None:
    writer.close()
    writer.close()
    assert writer.closed
    assert writer.event("note", "after close") == {}
    lines = writer.log_path.read_text(encoding="utf-8").splitlines()  # type: ignore[union-attr]
    assert "| END    | run run-1 closed after" in lines[-1]
    assert "after close" not in "\n".join(lines)
    assert "trace already closed" in caplog.text


def test_context_manager_closes(tmp_path: Path) -> None:
    with TraceWriter("r", "s", tmp_path) as trace:
        trace.decision("x")
    assert trace.closed


def test_concurrent_writers_produce_intact_lines(writer: TraceWriter) -> None:
    def work(worker: int) -> None:
        for index in range(50):
            writer.model_call("p", "m", f"k{worker}", f"call {worker}-{index}", True, 0.1, 1, 1)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    records = read_jsonl(writer.jsonl_path)  # type: ignore[arg-type]
    lines = writer.log_path.read_text(encoding="utf-8").splitlines()  # type: ignore[union-attr]
    assert len(records) == len(lines) == 1 + 8 * 50
    assert [record["seq"] for record in records] == list(range(1, 402))
    assert writer.model_stats()["p/m"]["calls"] == 400


def test_echo_stream_survives_unencodable_characters(tmp_path: Path) -> None:
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="ascii", errors="strict")
    trace = TraceWriter("r", "s", tmp_path, echo=stream)
    trace.model_call("anthropic", "m", f"anthropic#1({ELLIPSIS}abcd)", "purpose", True, 1.0)
    trace.close()
    stream.flush()
    echoed = raw.getvalue().decode("ascii")
    assert "anthropic#1(?abcd)" in echoed
    assert echoed.count("\n") == 3  # START, MODEL, END


def test_write_errors_are_logged_not_raised(writer: TraceWriter, caplog: pytest.LogCaptureFixture) -> None:
    class Broken(io.StringIO):
        def write(self, text: str) -> int:
            raise OSError("disk full")

    writer._log_file = Broken()  # simulate a failing disk
    writer.event("note", "still fine")  # must not raise
    assert "disk full" in caplog.text


# --- NullTrace ----------------------------------------------------------------------------------------------------------


def test_null_trace_keeps_events_in_memory_and_writes_no_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    trace = NullTrace("run-x", "slot-x", tmp_path, [SECRET])  # same positional signature as TraceWriter
    trace.model_call("anthropic", "m", "k", f"purpose {SECRET}", True, 1.0)
    trace.test("smoke", True, "fine")
    trace.summary({"track": "compiled"})
    trace.close()

    assert trace.log_path is None and trace.jsonl_path is None
    assert list(tmp_path.iterdir()) == []
    assert [event["kind"] for event in trace.events] == ["start", "model_call", "test", "summary", "end"]
    assert len(trace.of_kind("model_call")) == 1
    assert SECRET not in trace.text
    assert "BUILD SUMMARY" in trace.text
    assert trace.events[1]["data"]["purpose"] == "purpose ***"


def test_summary_survives_odd_caller_data(writer: TraceWriter) -> None:
    summary = writer.summary({"freya": "not a mapping", "tests": "not a list", "extra_obj": object()})
    assert summary["freya"] == {}
    assert summary["tests"] == []
    assert summary["extra"]["extra_obj"].startswith("<object object at")
    assert "BUILD SUMMARY" in writer.log_path.read_text(encoding="utf-8")  # type: ignore[union-attr]


# --- tests that ran, started or were skipped (P-09: a cut-off build must not lose them) ---------------------------------


def test_merged_tests_add_what_the_trace_recorded_and_mark_unfinished_ones(writer: TraceWriter) -> None:
    writer.test("static_checks", True, "recorded")
    writer.test("smoke", True, "2 turns")
    writer.test_started("practice")  # started, never finished: the build was cut off
    given = [{"name": "static_checks", "passed": True, "summary": "from the state"}]
    merged = writer.merged_tests(given)
    assert [t["name"] for t in merged] == ["static_checks", "smoke", "practice"]
    assert merged[0]["summary"] == "from the state"  # the caller's entry wins where both exist
    assert merged[2]["passed"] is False and merged[2]["summary"].startswith("aborted")
    assert given == [{"name": "static_checks", "passed": True, "summary": "from the state"}]  # not mutated


def test_a_repair_round_is_not_counted_twice(writer: TraceWriter) -> None:
    writer.test("smoke", False, "round 0")
    writer.test("smoke", True, "round 1")  # the node records both rounds under one name
    given = [
        {"name": "smoke", "passed": False, "summary": "round 0"},
        {"name": "smoke#2", "passed": True, "summary": "round 1"},
    ]
    assert writer.merged_tests(given) == given


def test_checks_that_rerun_after_every_repair_are_not_listed_once_per_run(writer: TraceWriter) -> None:
    writer.test("static_checks", False, "3 errors")
    writer.test("static_checks", True, "0 errors")  # after the compile repair
    given = [
        {"name": "static_checks", "passed": True, "summary": "0 errors"}
    ]  # the state keeps the latest run
    assert writer.merged_tests(given, latest_only=["static_checks"]) == given
    assert len(writer.merged_tests(given)) == 2  # without the hint the earlier run would be listed as well


def test_a_test_cut_off_in_a_repair_round_is_aborted_even_if_an_earlier_round_ran_it(
    writer: TraceWriter,
) -> None:
    writer.test("practice", False, "7/10")
    writer.test_started("practice")  # round 1, cut off
    merged = writer.merged_tests([{"name": "practice", "passed": False, "summary": "7/10"}])
    assert [t["name"] for t in merged] == ["practice", "practice"]
    assert merged[1]["summary"].startswith("aborted")


def test_finished_and_skipped_tests_are_no_longer_pending(writer: TraceWriter) -> None:
    writer.test_started("smoke")
    writer.test("smoke", True, "ok")
    writer.test_started("walkthrough")
    writer.test_skipped("walkthrough", "the auditor was not available")
    assert [t["name"] for t in writer.merged_tests([])] == ["smoke"]
    log = writer.log_path.read_text(encoding="utf-8")  # type: ignore[union-attr]
    assert any("SKIPPED walkthrough" in line for line in log.splitlines())


def test_the_summary_block_does_not_call_an_unpublished_version_published(writer: TraceWriter) -> None:
    writer.freya("save", workflow_id="wf-1", agent_id="ag-1", version=3, published=False)
    writer.summary({"status": "deployed_unpublished"})
    text = writer.log_path.read_text(encoding="utf-8")  # type: ignore[union-attr]
    assert "  - published version:" not in text
    assert "  - newest version (NOT confirmed published): 3" in text


def test_the_summary_block_keeps_the_published_label_for_a_confirmed_version(writer: TraceWriter) -> None:
    writer.freya("save", workflow_id="wf-1", agent_id="ag-1", version=3, published=True)
    writer.summary({"status": "deployed"})
    assert "  - published version: 3" in writer.log_path.read_text(encoding="utf-8")  # type: ignore[union-attr]
