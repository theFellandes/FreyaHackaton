"""Tests for builder.observability: the JSONL log, the Langfuse observer (fake client), the composite and the factory.

No network. Langfuse is replaced by ``FakeLangfuse``, which records the observation tree the observer builds; one
test checks that the real installed SDK still has the API surface the observer relies on (skipped when the optional
``obs`` extra is not installed). Secrets used here are made-up strings.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel, SecretStr

from builder.observability import (
    CompositeObserver,
    JsonlObserver,
    NullObserver,
    Redactor,
    build_observer,
    current_span_id,
    llm_log_path_for,
    observed_span,
    read_llm_log,
    to_text,
)
from builder.observability.langfuse_observer import LangfuseObserver
from builder.trace import NullTrace, TraceWriter

REPO_ROOT = Path(__file__).resolve().parents[2]
SECRET = "sk-test-SECRET-0123456789abcdef"
FAK = "fak_0123456789abcdefghijkl"

GENERATION: dict[str, Any] = {
    "purpose": "extract the policy",
    "provider": "openai",
    "model": "gpt-test",
    "key_label": "openai#1(…ab12)",
    "system": "You are a careful extractor.",
    "user": "Brief text: pay by Friday.",
    "output": '{"rules": ["pay by Friday"]}',
    "input_tokens": 120,
    "output_tokens": 30,
    "latency_s": 1.2345,
    "ok": True,
    "attempt": 1,
}


def generation(**overrides: Any) -> dict[str, Any]:
    return {**GENERATION, **overrides}


# ------------------------------------------------------------------------------------------------ fakes


class FakeObservation:
    def __init__(
        self, tree: FakeLangfuse, parent: FakeObservation | None, name: str, kind: str, fields: dict[str, Any]
    ):
        self.tree, self.parent, self.name, self.kind = tree, parent, name, kind
        self.fields = dict(fields)
        self.updates: list[dict[str, Any]] = []
        self.ended = False

    def start_observation(self, *, name: str, as_type: str = "span", **fields: Any) -> FakeObservation:
        return self.tree.new(self, name, as_type, fields)

    def update(self, **fields: Any) -> FakeObservation:
        self.updates.append(fields)
        self.fields.update(fields)
        return self

    def end(self) -> FakeObservation:
        self.ended = True
        return self


class FakeLangfuse:
    """Records the observation tree built through ``start_observation`` (Langfuse SDK v3.x / v4 API)."""

    def __init__(self) -> None:
        self.observations: list[FakeObservation] = []
        self.flushes = 0

    def new(
        self, parent: FakeObservation | None, name: str, kind: str, fields: dict[str, Any]
    ) -> FakeObservation:
        observation = FakeObservation(self, parent, name, kind, fields)
        self.observations.append(observation)
        return observation

    def start_observation(self, *, name: str, as_type: str = "span", **fields: Any) -> FakeObservation:
        return self.new(None, name, as_type, fields)

    def flush(self) -> None:
        self.flushes += 1

    def named(self, name: str) -> FakeObservation:
        (found,) = [o for o in self.observations if o.name == name]
        return found

    def of_kind(self, kind: str) -> list[FakeObservation]:
        return [o for o in self.observations if o.kind == kind]


class OldSdkObservation(FakeObservation):
    """SDK v3.0-style objects: ``start_span`` / ``start_generation`` instead of ``start_observation``."""

    start_observation = None  # type: ignore[assignment]

    def start_span(self, *, name: str, **fields: Any) -> OldSdkObservation:
        return self.tree.new(self, name, "span", fields)  # type: ignore[return-value]

    def start_generation(self, *, name: str, **fields: Any) -> OldSdkObservation:
        return self.tree.new(self, name, "generation", fields)  # type: ignore[return-value]


class OldSdkLangfuse(FakeLangfuse):
    start_observation = None  # type: ignore[assignment]

    def new(self, parent: Any, name: str, kind: str, fields: dict[str, Any]) -> FakeObservation:
        observation = OldSdkObservation(self, parent, name, kind, fields)
        self.observations.append(observation)
        return observation

    def start_span(self, *, name: str, **fields: Any) -> OldSdkObservation:
        return self.new(None, name, "span", fields)  # type: ignore[return-value]

    def start_generation(self, *, name: str, **fields: Any) -> OldSdkObservation:
        return self.new(None, name, "generation", fields)  # type: ignore[return-value]


class ExplodingClient:
    """A Langfuse client whose every call fails."""

    def start_observation(self, **kwargs: Any) -> Any:
        raise RuntimeError("langfuse is down")

    def flush(self) -> None:
        raise RuntimeError("langfuse is down")


class Recorder:
    """An observer that keeps every call; ``span_prefix`` makes its span ids distinguishable."""

    def __init__(self, span_prefix: str = "r") -> None:
        self.span_prefix = span_prefix
        self.calls: list[tuple[str, Any]] = []
        self.secrets: list[str] = []
        self._n = 0

    def start_run(self, run_id: str, slot: str, metadata: Any = None) -> None:
        self.calls.append(("start_run", (run_id, slot, metadata)))

    def start_span(self, name: str, metadata: Any = None) -> str:
        self._n += 1
        span_id = f"{self.span_prefix}{self._n}"
        self.calls.append(("start_span", (name, span_id)))
        return span_id

    def end_span(self, span_id: str | None, output_summary: Any = None, error: str | None = None) -> None:
        self.calls.append(("end_span", (span_id, output_summary, error)))

    def log_generation(self, **fields: Any) -> None:
        self.calls.append(("log_generation", fields))

    def end_run(self, status: str, summary: Any = None) -> None:
        self.calls.append(("end_run", (status, summary)))

    def flush(self) -> None:
        self.calls.append(("flush", None))

    def add_secrets(self, *secrets: str) -> None:
        self.secrets.extend(secrets)

    def kinds(self) -> list[str]:
        return [name for name, _ in self.calls]


class Exploding(Recorder):
    def start_run(self, *a: Any, **k: Any) -> None:
        raise RuntimeError("boom")

    def start_span(self, *a: Any, **k: Any) -> str:
        raise RuntimeError("boom")

    def end_span(self, *a: Any, **k: Any) -> None:
        raise RuntimeError("boom")

    def log_generation(self, **fields: Any) -> None:
        raise RuntimeError("boom")

    def end_run(self, *a: Any, **k: Any) -> None:
        raise RuntimeError("boom")

    def flush(self) -> None:
        raise RuntimeError("boom")

    def add_secrets(self, *secrets: str) -> None:
        raise RuntimeError("boom")


def langfuse(**kwargs: Any) -> tuple[LangfuseObserver, FakeLangfuse]:
    client = kwargs.pop("client", None) or FakeLangfuse()
    observer = LangfuseObserver(
        "pk-lf-test-public", "sk-lf-test-secret", "http://langfuse.local", client=client, **kwargs
    )
    return observer, client


# ------------------------------------------------------------------------------------------------ JSONL log


def test_jsonl_writes_one_full_line_per_generation(tmp_path: Path) -> None:
    path = tmp_path / "traces" / "slot-1.llm.jsonl"
    observer = JsonlObserver(path)
    observer.start_run("run-7", "brief-a", {"track": "compiled"})
    span = observer.start_span("extract_policy")
    long_prompt = "Why? " * 20_000  # 100 000 characters: nothing is cut

    observer.log_generation(
        **generation(system="S" * 5000, user=long_prompt, output="é ü 中文 " * 300, span_id=span)
    )
    observer.log_generation(
        **generation(attempt=2, ok=False, error_kind="overloaded", error="Overloaded", output=None)
    )

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    first, second = (json.loads(line) for line in lines)
    assert first["type"] == "generation"
    assert (first["seq"], second["seq"]) == (1, 2)
    assert first["run_id"] == "run-7" and first["slot"] == "brief-a"
    assert first["span"] == "extract_policy"
    assert first["purpose"] == "extract the policy"
    assert (first["provider"], first["model"], first["key_label"]) == (
        "openai",
        "gpt-test",
        "openai#1(…ab12)",
    )
    assert first["attempt"] == 1 and first["ok"] is True and first["error"] is None
    assert (first["input_tokens"], first["output_tokens"], first["latency_s"]) == (120, 30, 1.234)
    assert first["system"] == "S" * 5000
    assert first["user"] == long_prompt
    assert first["output"] == "é ü 中文 " * 300
    assert second["ok"] is False and second["error_kind"] == "overloaded" and second["error"] == "Overloaded"
    assert second["output"] == "" and second["span"] is None


def test_jsonl_masks_secrets_and_key_shapes_in_every_field(tmp_path: Path) -> None:
    path = tmp_path / "x.llm.jsonl"
    observer = JsonlObserver(
        path, redact=lambda text: text.replace("TRACE-SECRET-VALUE", "***"), secrets=[SECRET]
    )
    observer.add_secrets("late-registered-secret")

    observer.log_generation(
        **generation(
            purpose=f"purpose {SECRET}",
            system=f"key={SECRET} and TRACE-SECRET-VALUE",
            user=f"call with {FAK} and late-registered-secret",
            output=f'{{"echo": "{SECRET}", "bearer": "Bearer abcdefghijklmnopqrstuvwxyz0123"}}',
            ok=False,
            error_kind="auth",
            error=f"Incorrect API key provided: {SECRET}",
        )
    )

    text = path.read_text(encoding="utf-8")
    for leaked in (
        SECRET,
        FAK,
        "TRACE-SECRET-VALUE",
        "late-registered-secret",
        "abcdefghijklmnopqrstuvwxyz0123",
    ):
        assert leaked not in text
    record = json.loads(text)
    assert record["system"] == "key=*** and ***"
    assert record["user"] == "call with *** and ***"
    assert record["error"] == "Incorrect API key provided: ***"
    assert record["purpose"] == "purpose ***"


def test_jsonl_redacts_before_json_escaping(tmp_path: Path) -> None:
    """A secret with quotes or backslashes would no longer match once json.dumps had escaped it."""
    tricky = 'pa"ss\\word-123456'
    path = tmp_path / "x.llm.jsonl"
    observer = JsonlObserver(path, secrets=[tricky])

    observer.log_generation(**generation(user=f"password is {tricky}"))

    assert json.loads(path.read_text(encoding="utf-8"))["user"] == "password is ***"


def test_jsonl_renders_structured_outputs_as_json(tmp_path: Path) -> None:
    class Answer(BaseModel):
        answer: str

    observer = JsonlObserver()
    observer.log_generation(**generation(output=Answer(answer="42")))
    observer.log_generation(**generation(output={"a": [1, 2]}))

    assert [r["output"] for r in observer.records] == ['{"answer":"42"}', '{"a": [1, 2]}']
    assert to_text(None) == "" and to_text(b"bytes") == "bytes" and to_text(3) == "3"


def test_jsonl_metadata_mode_writes_no_prompt_or_reply_text(tmp_path: Path) -> None:
    """Logging privacy: ``content="metadata"`` keeps ids, timings, tokens and lengths, never the texts."""
    path = tmp_path / "x.llm.jsonl"
    observer = JsonlObserver(path, content="metadata")
    observer.add_secrets(SECRET)

    observer.log_generation(
        **generation(system="Sys prompt text", user="Customer Alice owes 500 euro", output="reply text 42")
    )
    observer.log_generation(
        **generation(
            ok=False, attempt=2, error_kind="parse", error="rules.0: Field required", output="raw {junk}"
        )
    )

    text = path.read_text(encoding="utf-8")
    for hidden in ("Sys prompt text", "Alice", "500 euro", "reply text 42", "raw {junk}"):
        assert hidden not in text
    ok, failed = read_llm_log(path)
    assert ok["content"] == "metadata" and failed["content"] == "metadata"
    assert (ok["system"], ok["user"], ok["output"]) == (None, None, None)
    assert (ok["system_chars"], ok["user_chars"], ok["output_chars"]) == (15, 28, 13)
    assert failed["output_chars"] == 10 and failed["output"] is None
    assert (ok["provider"], ok["model"], ok["purpose"]) == ("openai", "gpt-test", GENERATION["purpose"])
    assert (ok["input_tokens"], ok["output_tokens"], ok["latency_s"]) == (120, 30, 1.234)
    assert (failed["ok"], failed["error_kind"]) == (False, "parse")
    assert failed["error"] == "rules.0: Field required"


def test_jsonl_full_mode_is_the_default_and_marks_its_records(tmp_path: Path) -> None:
    path = tmp_path / "x.llm.jsonl"
    observer = JsonlObserver(path)

    observer.log_generation(**generation(system="abc", user="defgh", output="ij"))

    (record,) = read_llm_log(path)
    assert record["content"] == "full"
    assert (record["system"], record["user"], record["output"]) == ("abc", "defgh", "ij")
    assert (record["system_chars"], record["user_chars"], record["output_chars"]) == (3, 5, 2)


def test_jsonl_metadata_mode_also_applies_to_the_in_memory_log() -> None:
    observer = JsonlObserver(None, content="metadata")

    observer.log_generation(**generation())

    (record,) = observer.records
    assert record["user"] is None and record["user_chars"] == len(GENERATION["user"])


def test_jsonl_rejects_an_unknown_content_mode() -> None:
    with pytest.raises(ValueError, match="content"):
        JsonlObserver(None, content="everything")  # type: ignore[arg-type]


def test_jsonl_without_a_path_keeps_records_in_memory_and_writes_no_file(tmp_path: Path) -> None:
    observer = JsonlObserver(None)

    observer.log_generation(**generation())

    assert len(observer.records) == 1
    assert list(tmp_path.iterdir()) == []


def test_jsonl_never_raises_when_the_file_cannot_be_written(tmp_path: Path) -> None:
    observer = JsonlObserver(tmp_path)  # a directory: opening it for appending fails

    observer.log_generation(**generation())  # must not raise


def test_read_llm_log_skips_blank_and_broken_lines(tmp_path: Path) -> None:
    path = tmp_path / "x.llm.jsonl"
    path.write_text('{"a": 1}\n\nnot json\n{"b": 2}\n', encoding="utf-8")

    assert read_llm_log(path) == [{"a": 1}, {"b": 2}]


def test_llm_log_sits_next_to_the_trace_and_shares_its_stem(tmp_path: Path) -> None:
    with TraceWriter("run-1", "brief-a", directory=tmp_path) as trace:
        path = llm_log_path_for(trace)

    assert trace.log_path is not None
    assert path == trace.log_path.parent / (trace.log_path.stem + ".llm.jsonl")
    assert path is not None and path.name.startswith("brief-a-") and path.name.endswith(".llm.jsonl")
    assert llm_log_path_for(NullTrace()) is None


# ------------------------------------------------------------------------------------------------ redactor


def test_redactor_masks_nested_values_and_keys() -> None:
    redact = Redactor(secrets=[SECRET])

    cleaned = redact.value({"k": [f"a {SECRET}", {"x": 1, SECRET: None}], "n": 5, "t": ("z", FAK)})

    assert cleaned == {"k": ["a ***", {"x": 1, "***": None}], "n": 5, "t": ["z", "***"]}


def test_redactor_survives_a_failing_trace_redactor() -> None:
    def broken(text: str) -> str:
        raise RuntimeError("redactor down")

    assert Redactor(broken, [SECRET])(f"x {SECRET} y") == "x *** y"


# ------------------------------------------------------------------------------------------------ composite


def test_composite_gives_every_child_its_own_span_id() -> None:
    a, b = Recorder("a"), Recorder("b")
    composite = CompositeObserver([a, b])
    composite.start_run("run", "slot", {"k": 1})

    span = composite.start_span("node")
    composite.log_generation(**generation(), span_id=span)
    composite.end_span(span, "done", None)
    composite.log_generation(**generation(), span_id=span)  # after the span ended: no parent any more
    composite.end_run("deployed", {"n": 1})
    composite.flush()

    assert span not in ("a1", "b1")
    for child, own_id in ((a, "a1"), (b, "b1")):
        assert child.kinds() == [
            "start_run",
            "start_span",
            "log_generation",
            "end_span",
            "log_generation",
            "end_run",
            "flush",
        ]
        assert child.calls[2][1]["span_id"] == own_id
        assert child.calls[3][1] == (own_id, "done", None)
        assert child.calls[4][1]["span_id"] is None
        assert child.calls[5][1] == ("deployed", {"n": 1})


def test_composite_swallows_exceptions_and_still_calls_the_others() -> None:
    good = Recorder()
    composite = CompositeObserver([Exploding(), good, Exploding()])

    composite.start_run("run", "slot")
    span = composite.start_span("node")
    composite.log_generation(**generation(), span_id=span)
    composite.end_span(span)
    composite.end_run("failed")
    composite.flush()
    composite.add_secrets("x")

    assert good.kinds() == ["start_run", "start_span", "log_generation", "end_span", "end_run", "flush"]
    assert good.secrets == ["x"]


def test_composite_ignores_unknown_and_missing_span_ids() -> None:
    recorder = Recorder()
    composite = CompositeObserver([recorder])

    composite.end_span("nope")
    composite.end_span(None)
    composite.log_generation(**generation(), span_id="nope")

    assert recorder.kinds() == ["log_generation"]
    assert recorder.calls[0][1]["span_id"] is None


def test_null_observer_accepts_everything() -> None:
    observer = NullObserver()

    observer.start_run("r", "s")
    assert observer.start_span("n") is None
    observer.log_generation(**generation())
    observer.end_span(None)
    observer.end_run("ok")
    observer.flush()


# ------------------------------------------------------------------------------------------------ observed_span


def test_observed_span_publishes_the_current_span_and_ends_it_with_the_summary() -> None:
    recorder = Recorder()
    assert current_span_id() is None

    with observed_span(recorder, "extract", {"attempt": 1}) as span:
        assert current_span_id() == "r1" == span.span_id
        span.set_output("12 rules")

    assert current_span_id() is None
    assert recorder.calls == [("start_span", ("extract", "r1")), ("end_span", ("r1", "12 rules", None))]


def test_observed_span_reports_the_error_and_reraises() -> None:
    recorder = Recorder()
    failure = ValueError("bad ledger")

    with pytest.raises(ValueError) as caught, observed_span(recorder, "validate"):
        raise failure

    assert caught.value is failure
    assert recorder.calls[-1] == ("end_span", ("r1", None, "ValueError: bad ledger"))
    assert current_span_id() is None


def test_nested_spans_restore_the_outer_one() -> None:
    recorder = Recorder()

    with observed_span(recorder, "outer"):
        with observed_span(recorder, "inner"):
            assert current_span_id() == "r2"
        assert current_span_id() == "r1"


def test_observed_span_works_without_an_observer_and_with_a_broken_one() -> None:
    with observed_span(None, "x") as span:
        assert span.span_id is None and current_span_id() is None

    with observed_span(Exploding(), "x") as span:
        assert span.span_id is None
    assert current_span_id() is None


async def test_each_task_sees_its_own_current_span() -> None:
    recorder = Recorder()
    seen: dict[str, str | None] = {}

    async def node(name: str) -> None:
        with observed_span(recorder, name):
            await asyncio.sleep(0.005)
            seen[name] = current_span_id()

    await asyncio.gather(node("a"), node("b"))

    assert seen["a"] != seen["b"] and None not in seen.values()
    assert current_span_id() is None


# ------------------------------------------------------------------------------------------------ Langfuse observer


def test_a_run_becomes_one_root_observation_with_tags_and_session() -> None:
    propagated: list[dict[str, Any]] = []

    def propagate(**attributes: Any) -> contextlib.AbstractContextManager[None]:
        propagated.append(attributes)
        return contextlib.nullcontext()

    observer, client = langfuse(propagate=propagate)

    observer.start_run("run-9", "brief-a", {"track": "compiled", "profile": "staged", "brief_sha": "abc123"})

    root = client.named("build:brief-a")
    assert root.kind == "span" and root.parent is None and root.ended is False
    assert root.fields["metadata"]["brief_sha"] == "abc123"
    assert root.fields["input"]["run_id"] == "run-9"
    assert propagated[0] == {
        "trace_name": "build:brief-a",
        "session_id": "run-9",
        "tags": ["slot:brief-a", "track:compiled", "profile:staged"],
        "metadata": {"track": "compiled", "profile": "staged", "brief_sha": "abc123", "slot": "brief-a"},
    }


def test_spans_are_children_of_the_root_and_carry_outcome_and_error() -> None:
    observer, client = langfuse()
    observer.start_run("r", "s")
    ok_span = observer.start_span("extract_policy", {"attempt": 1})
    bad_span = observer.start_span("compile")

    observer.end_span(ok_span, {"rules": 12})
    observer.end_span(bad_span, None, "CompileError: no node")

    root = client.named("build:s")
    ok, bad = client.named("extract_policy"), client.named("compile")
    assert ok.parent is root and bad.parent is root
    assert ok.fields["metadata"] == {"attempt": 1}
    assert ok.updates == [{"output": {"rules": 12}}] and ok.ended
    assert bad.updates == [{"level": "ERROR", "status_message": "CompileError: no node"}] and bad.ended


def test_a_generation_hangs_under_its_span_with_messages_model_usage_and_metadata() -> None:
    observer, client = langfuse()
    observer.start_run("r", "s")
    span = observer.start_span("extract_policy")

    observer.log_generation(**generation(span_id=span))

    (gen,) = client.of_kind("generation")
    assert gen.parent is client.named("extract_policy")
    assert gen.name == "extract the policy"
    assert gen.ended is True
    assert gen.fields["model"] == "gpt-test"
    assert gen.fields["input"] == [
        {"role": "system", "content": "You are a careful extractor."},
        {"role": "user", "content": "Brief text: pay by Friday."},
    ]
    assert gen.fields["output"] == '{"rules": ["pay by Friday"]}'
    assert gen.fields["usage_details"] == {"input": 120, "output": 30}
    assert gen.fields["metadata"] == {
        "provider": "openai",
        "key": "openai#1(…ab12)",
        "attempt": 1,
        "latency_s": 1.234,
        "ok": True,
        "prompt_logged": True,
    }
    assert "level" not in gen.fields


def test_a_failed_attempt_is_an_error_level_generation() -> None:
    observer, client = langfuse()
    observer.start_run("r", "s")

    observer.log_generation(
        **generation(
            ok=False,
            error_kind="rate_limit",
            error="429 slow down",
            output=None,
            input_tokens=None,
            output_tokens=None,
        )
    )

    (gen,) = client.of_kind("generation")
    assert gen.parent is client.named("build:s")  # no span id: attached to the root
    assert gen.fields["level"] == "ERROR"
    assert gen.fields["status_message"] == "rate_limit: 429 slow down"
    assert gen.fields["output"] is None
    assert "usage_details" not in gen.fields
    assert gen.fields["metadata"]["error_kind"] == "rate_limit" and gen.fields["metadata"]["ok"] is False


def test_prompts_can_be_kept_out_of_langfuse() -> None:
    observer, client = langfuse(log_prompts=False)
    observer.start_run("r", "s")

    observer.log_generation(**generation(system="S" * 10, user="U" * 25, output="O" * 7))

    (gen,) = client.of_kind("generation")
    assert gen.fields["input"] == [
        {"role": "omitted", "content": "prompt hidden (system 10 chars, user 25 chars)"}
    ]
    assert gen.fields["output"] == "reply hidden (7 chars)"
    assert gen.fields["metadata"]["prompt_logged"] is False
    assert gen.fields["usage_details"] == {"input": 120, "output": 30}  # metadata still flows


def test_secrets_never_reach_langfuse() -> None:
    observer, client = langfuse(secrets=[SECRET])
    observer.start_run("run", "slot", {"note": f"token {SECRET}"})
    span = observer.start_span("node", {"debug": FAK})

    observer.log_generation(
        **generation(
            system=f"sys {SECRET}",
            user=f"user {FAK}",
            output=f"out {SECRET}",
            ok=False,
            error_kind="auth",
            error=f"bad key {SECRET}",
            span_id=span,
        )
    )
    observer.end_span(span, f"done {SECRET}", f"failed {FAK}")
    observer.end_run("failed", {"detail": SECRET})

    everything = json.dumps([(o.name, o.fields, o.updates) for o in client.observations], default=str)
    for leaked in (SECRET, FAK, "sk-lf-test-secret"):
        assert leaked not in everything
    assert "sys ***" in everything and "bad key ***" in everything


def test_langfuse_failures_never_propagate() -> None:
    observer = LangfuseObserver("pk", "sk", "http://x", client=ExplodingClient())

    observer.start_run("r", "s", {"a": 1})
    span = observer.start_span("node")
    observer.log_generation(**generation(span_id=span))
    observer.end_span(span, "x", "y")
    observer.end_run("failed")
    observer.flush()

    assert span is None  # the span could not be created; nothing raised


def test_end_run_marks_the_root_closes_leaked_spans_and_flushes() -> None:
    observer, client = langfuse()
    observer.start_run("r", "s")
    observer.start_span("never_ended")

    observer.end_run("blocked", {"violations": 2})
    observer.flush()

    root = client.named("build:s")
    assert root.ended is True
    assert root.updates[-1]["level"] == "WARNING"
    assert root.updates[-1]["output"] == {"status": "blocked", "summary": {"violations": 2}}
    leaked = client.named("never_ended")
    assert leaked.ended is True and leaked.updates[-1]["level"] == "WARNING"
    assert client.flushes == 1


def test_a_deployed_run_has_no_error_level() -> None:
    observer, client = langfuse()
    observer.start_run("r", "s")

    observer.end_run("deployed", "ok")

    assert "level" not in client.named("build:s").updates[-1]


def test_old_sdk_without_start_observation_is_still_supported() -> None:
    client = OldSdkLangfuse()
    observer = LangfuseObserver("pk", "sk", "http://x", client=client)

    observer.start_run("r", "s")
    span = observer.start_span("node")
    observer.log_generation(**generation(span_id=span))
    observer.end_span(span, "ok")
    observer.end_run("deployed")

    assert [(o.kind, o.name) for o in client.observations] == [
        ("span", "build:s"),
        ("span", "node"),
        ("generation", "extract the policy"),
    ]
    assert client.observations[2].parent is client.observations[1]


def test_a_generation_before_any_run_goes_straight_to_the_client() -> None:
    observer, client = langfuse()

    observer.log_generation(**generation())

    (gen,) = client.of_kind("generation")
    assert gen.parent is None and gen.ended


def test_missing_langfuse_package_is_an_import_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "langfuse", None)  # `import langfuse` now raises ImportError

    with pytest.raises(ImportError):
        LangfuseObserver("pk", "sk", "http://x")


def test_the_installed_langfuse_sdk_still_has_the_api_the_observer_uses() -> None:
    sdk = pytest.importorskip("langfuse")
    span_module = pytest.importorskip("langfuse._client.span")

    for target in (sdk.Langfuse, span_module.LangfuseObservationWrapper):
        parameters = inspect.signature(target.start_observation).parameters
        for name in ("name", "as_type", "input", "output", "metadata", "level", "status_message"):
            assert name in parameters, f"{target.__name__}.start_observation lost {name!r}"
    generation_parameters = inspect.signature(sdk.Langfuse.start_observation).parameters
    assert "model" in generation_parameters and "usage_details" in generation_parameters
    for method in ("update", "end"):
        assert callable(getattr(span_module.LangfuseObservationWrapper, method))
    assert callable(sdk.Langfuse.flush) and callable(sdk.propagate_attributes)
    constructor = inspect.signature(sdk.Langfuse.__init__).parameters
    assert {"public_key", "secret_key", "host"} <= set(constructor)


def test_the_observer_builds_the_expected_trace_with_the_real_sdk() -> None:
    """End to end against the installed SDK: spans go to an in-memory OpenTelemetry exporter, not to a server."""
    sdk = pytest.importorskip("langfuse")
    exporter_module = pytest.importorskip("opentelemetry.sdk.trace.export.in_memory_span_exporter")
    exporter = exporter_module.InMemorySpanExporter()
    client = sdk.Langfuse(
        public_key="pk-lf-unit-test-only",
        secret_key="sk-lf-unit-test-only",
        host="http://127.0.0.1:9",
        span_exporter=exporter,
        flush_at=1,
    )
    observer = LangfuseObserver(
        "pk-lf-unit-test-only", "sk-lf-unit-test-only", client=client, propagate=sdk.propagate_attributes
    )
    try:
        observer.start_run("run-1", "brief-a", {"track": "compiled", "profile": "staged", "brief_sha": "abc"})
        span = observer.start_span("extract_policy")
        observer.log_generation(**generation(span_id=span))
        observer.log_generation(
            **generation(
                attempt=2, ok=False, error_kind="overloaded", error="Overloaded", output=None, span_id=span
            )
        )
        observer.end_span(span, "2 rules")
        observer.end_run("deployed")
        observer.flush()
    finally:
        client.shutdown()

    finished = {(s.name, s.attributes["langfuse.observation.type"]): s for s in exporter.get_finished_spans()}
    root = finished[("build:brief-a", "span")]
    node = finished[("extract_policy", "span")]
    generations = [
        s for s in exporter.get_finished_spans() if s.attributes["langfuse.observation.type"] == "generation"
    ]
    assert len(generations) == 2
    assert root.parent is None and node.parent.span_id == root.context.span_id
    assert all(g.parent.span_id == node.context.span_id for g in generations)
    assert all(g.context.trace_id == root.context.trace_id for g in generations)
    assert root.attributes["langfuse.trace.name"] == "build:brief-a"
    assert tuple(root.attributes["langfuse.trace.tags"]) == (
        "slot:brief-a",
        "track:compiled",
        "profile:staged",
    )
    assert root.attributes["session.id"] == "run-1"
    ok, failed = sorted(generations, key=lambda g: g.attributes["langfuse.observation.metadata.attempt"])
    assert ok.attributes["langfuse.observation.model.name"] == "gpt-test"
    assert json.loads(ok.attributes["langfuse.observation.usage_details"]) == {"input": 120, "output": 30}
    assert json.loads(ok.attributes["langfuse.observation.input"])[-1] == {
        "role": "user",
        "content": "Brief text: pay by Friday.",
    }
    assert ok.attributes["langfuse.observation.output"] == '{"rules": ["pay by Friday"]}'
    assert failed.attributes["langfuse.observation.level"] == "ERROR"
    assert failed.attributes["langfuse.observation.status_message"] == "overloaded: Overloaded"


# ------------------------------------------------------------------------------------------------ factory


def factory_settings(**overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "langfuse_public_key": None,
        "langfuse_secret_key": None,
        "langfuse_host": "https://cloud.langfuse.com",
        "observability_log_prompts": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def obs_messages(trace: NullTrace) -> list[str]:
    return [event["message"] for event in trace.of_kind("obs")]


def test_without_keys_only_the_local_log_is_on(null_trace: NullTrace) -> None:
    observer = build_observer(factory_settings(), null_trace)

    assert isinstance(observer, CompositeObserver)
    assert [type(o) for o in observer.observers] == [JsonlObserver]
    assert obs_messages(null_trace) == ["langfuse disabled (no keys)"]
    assert null_trace.lines[-1].endswith("OBS    | langfuse disabled (no keys)")


def test_one_key_alone_does_not_enable_langfuse(null_trace: NullTrace) -> None:
    observer = build_observer(factory_settings(langfuse_public_key=SecretStr("pk-lf-1")), null_trace)

    assert len(observer.observers) == 1  # type: ignore[attr-defined]
    assert obs_messages(null_trace) == ["langfuse disabled (no keys)"]


def test_with_both_keys_langfuse_joins_the_local_log(null_trace: NullTrace) -> None:
    created: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    fake = Recorder()

    def fake_factory(*args: Any, **kwargs: Any) -> Recorder:
        created.append((args, kwargs))
        return fake

    settings = factory_settings(
        langfuse_public_key=SecretStr("pk-lf-1"),
        langfuse_secret_key=SecretStr("sk-lf-2"),
        langfuse_host="http://localhost:3000",
        observability_log_prompts=False,
    )

    observer = build_observer(settings, null_trace, langfuse_factory=fake_factory)

    assert [type(o) for o in observer.observers] == [JsonlObserver, Recorder]  # type: ignore[attr-defined]
    ((args, kwargs),) = created
    assert args == ("pk-lf-1", "sk-lf-2", "http://localhost:3000")
    assert kwargs["log_prompts"] is False and kwargs["redact"] == null_trace.redact
    assert obs_messages(null_trace) == ["langfuse enabled (http://localhost:3000)"]


def test_missing_sdk_or_a_broken_client_only_disables_langfuse(null_trace: NullTrace) -> None:
    settings = factory_settings(langfuse_public_key="pk", langfuse_secret_key="sk")

    def no_sdk(*a: Any, **k: Any) -> Any:
        raise ImportError("No module named 'langfuse'")

    def broken(*a: Any, **k: Any) -> Any:
        raise RuntimeError("bad host")

    for factory in (no_sdk, broken):
        observer = build_observer(settings, null_trace, langfuse_factory=factory)
        assert len(observer.observers) == 1  # type: ignore[attr-defined]

    first, second = obs_messages(null_trace)
    assert first.startswith("langfuse disabled (package not installed")
    assert second == "langfuse disabled (RuntimeError while starting)"


def test_the_real_settings_fields_feed_the_factory(make_settings: Any, null_trace: NullTrace) -> None:
    settings = make_settings(
        langfuse_public_key="pk-lf-abc",
        langfuse_secret_key="sk-lf-def",
        langfuse_host="http://localhost:3000/",
        observability_log_prompts="false",
    )
    created: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def fake_factory(*args: Any, **kwargs: Any) -> Recorder:
        created.append((args, kwargs))
        return Recorder()

    build_observer(settings, null_trace, langfuse_factory=fake_factory)

    ((args, kwargs),) = created
    assert args == ("pk-lf-abc", "sk-lf-def", "http://localhost:3000")
    assert kwargs["log_prompts"] is False


def test_the_local_log_content_setting_reaches_the_jsonl_observer_and_not_langfuse(
    make_settings: Any, null_trace: NullTrace
) -> None:
    """``LLM_LOG_CONTENT`` is the local switch; Langfuse keeps following ``OBSERVABILITY_LOG_PROMPTS`` alone."""
    settings = make_settings(
        langfuse_public_key="pk-lf-abc", langfuse_secret_key="sk-lf-def", llm_log_content="metadata"
    )
    created: list[dict[str, Any]] = []

    def fake_factory(*args: Any, **kwargs: Any) -> Recorder:
        created.append(kwargs)
        return Recorder()

    observer = build_observer(settings, null_trace, langfuse_factory=fake_factory)

    local = observer.observers[0]  # type: ignore[attr-defined]
    assert isinstance(local, JsonlObserver) and local.content == "metadata"
    assert created[0]["log_prompts"] is True  # Langfuse still gets the texts: its own setting says so


def test_the_local_log_defaults_to_full_content_and_langfuse_switch_does_not_hide_it(
    make_settings: Any, null_trace: NullTrace
) -> None:
    settings = make_settings(observability_log_prompts=False)

    local = build_observer(settings, null_trace).observers[0]  # type: ignore[attr-defined]

    assert isinstance(local, JsonlObserver) and local.content == "full"
    assert build_observer(factory_settings(), null_trace).observers[0].content == "full"  # type: ignore[attr-defined]


def test_an_unknown_content_value_on_an_ad_hoc_settings_object_fails_on_the_private_side(
    null_trace: NullTrace,
) -> None:
    local = build_observer(factory_settings(llm_log_content="everything"), null_trace).observers[0]  # type: ignore[attr-defined]

    assert isinstance(local, JsonlObserver) and local.content == "metadata"
    assert any("metadata only" in message for message in obs_messages(null_trace))


def test_metadata_content_keeps_the_texts_out_of_the_files_next_to_the_trace(
    make_settings: Any, tmp_path: Path
) -> None:
    settings = make_settings(llm_log_content="metadata")
    with TraceWriter("run-1", "brief-a", directory=tmp_path) as trace:
        observer = build_observer(settings, trace)
        observer.start_run("run-1", "brief-a")
        observer.log_generation(**generation(user="Alice owes 500 euro", output="she will pay"))

    assert trace.log_path is not None
    llm_log = trace.log_path.parent / (trace.log_path.stem + ".llm.jsonl")
    text = llm_log.read_text(encoding="utf-8")
    assert "Alice" not in text and "she will pay" not in text
    (record,) = read_llm_log(llm_log)
    assert record["content"] == "metadata" and record["user_chars"] == len("Alice owes 500 euro")


def test_the_local_log_is_written_next_to_the_trace_files(tmp_path: Path) -> None:
    with TraceWriter("run-1", "brief-a", directory=tmp_path) as trace:
        observer = build_observer(factory_settings(), trace)
        observer.start_run("run-1", "brief-a")
        observer.log_generation(**generation(user=f"secret is {SECRET}"))
        trace.add_secrets(SECRET)
        observer.log_generation(**generation(user=f"secret is {SECRET}"))

    assert trace.log_path is not None
    llm_log = trace.log_path.parent / (trace.log_path.stem + ".llm.jsonl")
    records = read_llm_log(llm_log)
    assert len(records) == 2
    assert records[1]["user"] == "secret is ***"  # the trace's redaction (it learned the secret) applies
    assert "obs" in trace.log_path.read_text(encoding="utf-8").lower()


def test_hermetic_traces_leave_no_llm_log_behind(null_trace: NullTrace, tmp_path: Path) -> None:
    observer = build_observer(factory_settings(), null_trace)

    observer.log_generation(**generation())

    (jsonl,) = observer.observers  # type: ignore[attr-defined]
    assert isinstance(jsonl, JsonlObserver) and jsonl.path is None and len(jsonl.records) == 1


@pytest.fixture(autouse=True)
def _no_stray_llm_logs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Keep any accidental default trace directory inside tmp_path."""
    monkeypatch.setattr("builder.paths.TRACES_DIR", tmp_path / "_traces")
    yield


def test_importing_the_observability_package_never_imports_the_langfuse_sdk() -> None:
    code = (
        "import sys, builder.observability, builder.llm.gateway; "
        "print(','.join(m for m in ('langfuse', 'opentelemetry') if m in sys.modules))"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True, text=True, timeout=120, check=True
    )
    assert completed.stdout.strip() == ""
