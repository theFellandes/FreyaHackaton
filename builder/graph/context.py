"""The run context handed to every graph node (LangGraph ``context_schema``).

State holds *data*; the context holds the *services* a run uses and that must not be serialised: the settings, the LLM
gateway (the key changer), the Freya client, the trace writer and the wall-clock budget. ``build.py`` creates one
:class:`BuilderContext` per build; tests build one with a :class:`~builder.testing.FixtureGateway` and a
:class:`~builder.testing.FakeFreya`.

Two optional hooks make the expensive collaborators replaceable without patching: ``agentic_runner`` (default:
:func:`builder.agentic.runner.run_agentic_build`) and ``practice_runner`` (default:
:func:`builder.freya.practice.run_practice`).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from builder.freya.background import CancelToken

if TYPE_CHECKING:
    from builder.budget import Budget
    from builder.freya.client import FreyaClient
    from builder.llm.gateway import LLMGateway
    from builder.observability.base import LLMObserver
    from builder.settings import Settings
    from builder.trace import TraceWriter

#: ``async (**kwargs) -> dict`` with the contract of :func:`builder.agentic.runner.run_agentic_build`.
AgenticRunner = Callable[..., Awaitable[dict[str, Any]]]
#: ``(agent_id, letter, **kwargs) -> PracticeResult`` (blocking), see :func:`builder.freya.practice.run_practice`.
PracticeRunner = Callable[..., Any]


@dataclass
class BuilderContext:
    """Services of one build run.

    Attributes:
        settings: The effective settings (CLI flags already applied).
        llm: The LLM gateway; anything with async ``structured(...)`` and ``text(...)`` methods.
        freya: The Freya client; ``None`` in a dry run.
        trace: The run trace (always finalised by ``build.py``, even after a crash).
        budget: The wall-clock budget of the build.
        observer: Optional LLM observer for graph-node spans (the gateway logs the generations itself).
        secrets: Secret values that must never reach a workflow or a trace.
        agentic_runner: Replacement for the Claude Agent SDK runner (tests).
        practice_runner: Replacement for the practice-harness subprocess (tests).
        clock: Wall-clock source (``datetime.now``); the smoke test derives its call date from it.
        progress: Key facts of the run, updated after every node, so a build cut off by the wall-clock guard can
            still report what exists (workflow id, exported file, ...).
        finalized: Set once the trace summary has been written (the summary is written exactly once).
        cancel: Token bound to every platform call the nodes run in a worker thread (``run_blocking``). ``build.py``
            gives it the absolute deadline of the wall-clock guard; the guard cancels it, so a call that is still
            in flight when the guard fires is the last one: no further request is started.
        active_node: Name of the node that is running right now (empty between nodes); after a cut-off it tells which
            node was interrupted.
    """

    settings: Settings
    llm: LLMGateway | Any
    freya: FreyaClient | Any | None
    trace: TraceWriter
    budget: Budget
    observer: LLMObserver | None = None
    secrets: tuple[str, ...] = ()
    agentic_runner: AgenticRunner | None = None
    practice_runner: PracticeRunner | None = None
    clock: Callable[[], datetime] = datetime.now
    progress: dict[str, Any] = field(default_factory=dict)
    finalized: bool = False
    cancel: CancelToken = field(default_factory=CancelToken)
    active_node: str = ""


__all__ = ["AgenticRunner", "BuilderContext", "PracticeRunner"]
