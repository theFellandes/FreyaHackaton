"""Fixtures for the graph tests: fixture gateways, fake Freya clients, contexts and a helper that runs the graph.

Brief values live in ``tests/fixtures/policy`` (the Brief A ledger and IR); the builder code under test contains none.
Nothing here touches the network: the LLM is a :class:`~builder.testing.FixtureGateway`, Freya a
:class:`~builder.testing.FakeFreya`, the agentic runner and the practice harness are replaced by small fakes.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from builder.budget import Budget
from builder.compiler import compile_workflow
from builder.graph import BuilderContext, build_graph, initial_state
from builder.graph.build_graph import RECURSION_LIMIT
from builder.inputs import InputBundle, classify_inputs
from builder.policy.ir import PolicyIR, RuleLedger
from builder.selftest.smoke import render_phrase
from builder.settings import Settings
from builder.testing import FakeFreya, FixtureGateway
from builder.trace import NullTrace

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "policy"


def load_ir(name: str = "brief_a") -> PolicyIR:
    """The fixture policy IR (``brief_a`` or ``brief_ab``)."""
    return PolicyIR.model_validate(json.loads((FIXTURES / f"{name}_ir.json").read_text(encoding="utf-8")))


def load_ledger(name: str = "brief_a") -> RuleLedger:
    """The fixture rule ledger."""
    return RuleLedger.model_validate(
        json.loads((FIXTURES / f"{name}_ledger.json").read_text(encoding="utf-8"))
    )


def opening_reply(ir: PolicyIR) -> str:
    """What a correct agent says first: the IR's opening sentence and right-party question (placeholders filled)."""
    variables = {"title": "Mr", "last_name": "Morgan", "first_name": "Alex"}
    return " ".join(
        render_phrase(text, variables)
        for text in (ir.phrases.opening_sentence, ir.phrases.right_party_question)
    )


@pytest.fixture(autouse=True)
def _isolated_key_health(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Dead-credential bookkeeping of a test must never reach the real ``data/key_health.json``."""
    monkeypatch.setenv("LLM_KEY_HEALTH_FILE", str(tmp_path / "key_health.json"))


@pytest.fixture(autouse=True)
def _no_real_agentic_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """A graph test must never start a real Claude Agent SDK session (a login may exist on the developer machine)."""

    def blocked() -> Any:
        raise RuntimeError("a real agentic session is blocked in tests: pass agentic_runner=<fake>")

    monkeypatch.setattr("builder.agentic.runner._sdk_query", blocked)


@pytest.fixture
def fixture_dir() -> Path:
    """The directory with the policy fixtures (``brief_a_ledger.json`` ...)."""
    return FIXTURES


@pytest.fixture
def inputs(brief_a_path: Path, kit_openapi_path: Path) -> InputBundle:
    """The classified kit inputs: Brief A and the kit's tool API."""
    return classify_inputs([brief_a_path, kit_openapi_path])


@pytest.fixture
def ir_a() -> PolicyIR:
    """The fixture IR of Brief A."""
    return load_ir("brief_a")


@pytest.fixture
def ledger_a() -> RuleLedger:
    """The fixture ledger of Brief A."""
    return load_ledger("brief_a")


@pytest.fixture
def make_gateway(null_trace: NullTrace) -> Callable[..., FixtureGateway]:
    """Factory for a fixture gateway answering with the Brief A fixtures: ``make_gateway(prose=..., fail_schemas=...)``."""

    def factory(**kwargs: Any) -> FixtureGateway:
        return FixtureGateway(load_ledger("brief_a"), load_ir("brief_a"), trace=null_trace, **kwargs)

    return factory


@pytest.fixture
def make_freya(null_trace: NullTrace, ir_a: PolicyIR) -> Callable[..., FakeFreya]:
    """Factory for a fake Freya client whose agent opens like the Brief A policy says."""

    def factory(**kwargs: Any) -> FakeFreya:
        kwargs.setdefault("opening", opening_reply(ir_a))
        return FakeFreya(null_trace, **kwargs)

    return factory


@pytest.fixture
def practice_ok() -> Callable[..., Any]:
    """A practice runner that reports all 13 cases passed."""

    def runner(agent_id: str, letter: str | None, **kwargs: Any) -> Any:
        return SimpleNamespace(
            passed=13, total=13, failed_ids=[], rows=[], raw_path=Path("results/fake.json")
        )

    return runner


@pytest.fixture
def make_ctx(
    make_settings: Callable[..., Settings], null_trace: NullTrace, tmp_dirs: SimpleNamespace
) -> Callable[..., BuilderContext]:
    """Factory for a :class:`BuilderContext`; outputs go to the temporary directories of ``tmp_dirs``.

    ``make_ctx(llm=..., freya=..., budget_s=1680, **settings_overrides)``; without ``freya`` the settings are a dry run.
    """

    def factory(
        llm: Any,
        freya: Any = None,
        *,
        budget_s: float = 1680,
        agentic_runner: Any = None,
        practice_runner: Any = None,
        **overrides: Any,
    ) -> BuilderContext:
        overrides.setdefault("dry_run", freya is None)
        overrides.setdefault("self_test", "auto")
        overrides.setdefault("builder_track", "compiled")  # the agentic fallback is opt-in per test
        settings = make_settings(**overrides)
        return BuilderContext(
            settings=settings,
            llm=llm,
            freya=None if settings.dry_run else freya,
            trace=null_trace,
            budget=Budget(budget_s),
            secrets=("sk-test-secret-value-0123456789",),
            agentic_runner=agentic_runner,
            practice_runner=practice_runner,
        )

    return factory


@pytest.fixture
def invoke(inputs: InputBundle) -> Callable[[BuilderContext], Awaitable[dict[str, Any]]]:
    """``await invoke(ctx)`` runs the whole graph on the Brief A inputs and returns the final state."""

    async def run(ctx: BuilderContext) -> dict[str, Any]:
        start = initial_state(
            run_id="test-run",
            slot=inputs.slot,
            started_at="2026-10-10T00:00:00Z",
            inputs=inputs.as_dict(),
            dry_run=bool(ctx.settings.dry_run),
        )
        return await build_graph().ainvoke(start, context=ctx, config={"recursion_limit": RECURSION_LIMIT})

    return run


@pytest.fixture
def compiled_a_workflow(ir_a: PolicyIR, ledger_a: RuleLedger, kit_tool_api: Any) -> dict[str, Any]:
    """A valid compiled workflow for Brief A (what an agentic session would have to write)."""
    return compile_workflow(ir_a, kit_tool_api, ledger_a).workflow


def stages_of(trace: NullTrace) -> list[str]:
    """Names of the stages that started, in order (from the in-memory trace)."""
    return [e["data"]["stage"] for e in trace.of_kind("stage") if e["data"].get("status") == "start"]
