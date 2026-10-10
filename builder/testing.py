"""Test doubles for the builder: an offline LLM gateway and a fake Freya client.

Both let the *whole* graph run without a model, a key or a network - in unit tests and through
``python builder/build.py ... --fake-llm DIR`` (a dry run that shows the pipeline works end to end):

* :class:`FixtureGateway` answers the structured calls of the understand / prose / audit stages from fixtures it is
  given (a rule ledger, a policy IR, optionally node prose). It plays the model the way the stages expect: the ledger
  call returns the fixture rules whose quote lies inside the requested passage, the extraction call returns the IR, the
  prose call a fixed draft, the walkthrough call a fixed list of findings. Every call is written to the trace as a
  ``MODEL`` line (provider ``fixture``), so a dry run has the same trace shape as a real build. The class holds no
  brief values: the fixtures come from the caller (tests keep them under ``tests/fixtures``).
* :class:`FakeFreya` mimics :class:`builder.freya.client.FreyaClient` in memory: validation, save (a workflow id, an
  agent id, versions), read-back, export, chat and the simulation lock. Behaviour switches (reject the first
  validations, fail the save, scripted chat replies) make the failure paths testable.

Neither class is used by a production build.
"""

from __future__ import annotations

import contextlib
import itertools
import json
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from builder.compiler.prose import NodeProse
from builder.freya.client import (
    ChatTurn,
    DeployInfo,
    FreyaError,
    FreyaValidationError,
    count_nodes,
    count_tools,
)
from builder.llm.gateway import LLMCallResult
from builder.llm.providers import LLMRole
from builder.observability.base import current_span_id
from builder.policy.ir import PolicyIR, RuleLedger
from builder.understand.grounding import normalize_text
from builder.understand.ledger import (
    LedgerPart,
    LedgerReconciliation,
    RequoteResult,
    RuleDraft,
    SupersessionLink,
)
from builder.understand.prose import NodeText, ProseDraft

_TARGET = re.compile(r"<<<TARGET_PASSAGE\n(.*?)\nTARGET_PASSAGE>>>", re.DOTALL)


class FixtureMissing(RuntimeError):
    """The fixture gateway was asked for a schema it has no answer for."""


@dataclass
class FixtureCall:
    """One call received by :class:`FixtureGateway`."""

    purpose: str
    schema: str
    role: str
    system: str
    user: str
    deadline: float | None = None


def _as_ledger(value: RuleLedger | Mapping[str, Any] | Path | str) -> RuleLedger:
    if isinstance(value, RuleLedger):
        return value
    if isinstance(value, Mapping):
        return RuleLedger.model_validate(dict(value))
    return RuleLedger.model_validate(json.loads(Path(value).read_text(encoding="utf-8")))


def _as_ir(value: PolicyIR | Mapping[str, Any] | Path | str) -> PolicyIR:
    if isinstance(value, PolicyIR):
        return value
    if isinstance(value, Mapping):
        return PolicyIR.model_validate(dict(value))
    return PolicyIR.model_validate(json.loads(Path(value).read_text(encoding="utf-8")))


class FixtureGateway:
    """An LLM gateway stand-in that answers from fixtures (see the module docstring).

    Args:
        ledger: The fixture rule ledger (model, dict or JSON file).
        ir: The fixture policy IR (model, dict or JSON file).
        prose: Node prose for the prose draft (``None`` = an empty draft, i.e. template-only prompts).
        walkthrough_issues: Findings the audit returns (dicts with ``rule_id``, ``verdict``, ``where``, ``explanation``).
        trace: Receives one ``model_call`` line per call (optional).
        observer: Receives one ``log_generation`` per call with the full prompt and the answer (optional), so an
            offline run also leaves the ``*.llm.jsonl`` log for inspecting prompts.
        responders: ``{schema class name: fn(call) -> value}`` overrides or additions.
        fail_schemas: Schema names whose calls raise :class:`FixtureMissing` (to test the failure paths).
    """

    def __init__(
        self,
        ledger: RuleLedger | Mapping[str, Any] | Path | str,
        ir: PolicyIR | Mapping[str, Any] | Path | str,
        prose: NodeProse | None = None,
        *,
        walkthrough_issues: Sequence[Mapping[str, Any]] = (),
        trace: Any = None,
        observer: Any = None,
        responders: Mapping[str, Callable[[FixtureCall], Any]] | None = None,
        fail_schemas: Iterable[str] = (),
    ) -> None:
        self.ledger = _as_ledger(ledger)
        self.ir = _as_ir(ir)
        self.prose = prose
        self.walkthrough_issues = [dict(item) for item in walkthrough_issues]
        self.trace = trace
        self.observer = observer
        self.responders = dict(responders or {})
        self.fail_schemas = set(fail_schemas)
        self.calls: list[FixtureCall] = []
        self._ambiguities_given = False

    # ------------------------------------------------------------------------------------------ gateway API

    def describe(self) -> dict[str, Any]:
        """Shape-compatible with :meth:`LLMGateway.describe`."""
        return {"order": ["fixture"], "fixture": True}

    def available_providers(self) -> list[str]:
        """Shape-compatible with :meth:`LLMGateway.available_providers`."""
        return ["fixture"]

    def purposes(self, fragment: str) -> list[FixtureCall]:
        """The received calls whose purpose contains ``fragment``."""
        return [call for call in self.calls if fragment in call.purpose]

    async def structured(
        self,
        *,
        purpose: str,
        schema: type,
        system: str,
        user: str,
        role: LLMRole = LLMRole.STRONG,
        max_tokens: int | None = None,
        deadline: float | None = None,
    ) -> LLMCallResult[Any]:
        """Answer a structured call from the fixtures (same keyword interface as the real gateway)."""
        call = FixtureCall(
            purpose, schema.__name__, str(getattr(role, "value", role)), system, user, deadline
        )
        self.calls.append(call)
        ok = call.schema not in self.fail_schemas
        self._trace_call(call, ok)
        if not ok:
            raise FixtureMissing(f"the fixture gateway was told to fail {call.schema} calls")
        value = self._answer(call)
        self._log_generation(call, value)
        return LLMCallResult(
            value=value,
            provider="fixture",
            model="fixture",
            key_label="fixture#0",
            attempts=[],
            latency_s=0.0,
            input_tokens=(len(system) + len(user)) // 4,
            output_tokens=0,
            purpose=purpose,
        )

    async def text(
        self,
        *,
        purpose: str,
        system: str,
        user: str,
        role: LLMRole = LLMRole.STRONG,
        max_tokens: int | None = None,
        deadline: float | None = None,
    ) -> LLMCallResult[str]:
        """Plain-text calls are not part of the graph; answer with an empty string."""
        call = FixtureCall(purpose, "text", str(getattr(role, "value", role)), system, user, deadline)
        self.calls.append(call)
        self._trace_call(call, True)
        return LLMCallResult(
            value="", provider="fixture", model="fixture", key_label="fixture#0", attempts=[], latency_s=0.0
        )

    # ------------------------------------------------------------------------------------------ answers

    def _trace_call(self, call: FixtureCall, ok: bool) -> None:
        if self.trace is None:
            return
        self.trace.model_call(
            "fixture",
            "fixture",
            "fixture#0",
            call.purpose,
            ok,
            0.0,
            input_tokens=(len(call.system) + len(call.user)) // 4,
            output_tokens=0,
            error_kind=None if ok else "unknown",
            error=None if ok else "fixture failure requested",
        )

    def _log_generation(self, call: FixtureCall, value: Any) -> None:
        if self.observer is None:
            return
        output = value.model_dump(mode="json") if hasattr(value, "model_dump") else value
        with contextlib.suppress(Exception):  # observability never breaks a run
            self.observer.log_generation(
                purpose=call.purpose,
                provider="fixture",
                model="fixture",
                key_label="fixture#0",
                system=call.system,
                user=call.user,
                output=output,
                input_tokens=(len(call.system) + len(call.user)) // 4,
                output_tokens=0,
                latency_s=0.0,
                ok=True,
                attempt=1,
                span_id=current_span_id(),
            )

    def _answer(self, call: FixtureCall) -> Any:
        if call.schema in self.responders:
            return self.responders[call.schema](call)
        if call.schema == "LedgerPart":
            return self._ledger_part(call)
        if call.schema == "LedgerReconciliation":
            consistent = self.ledger.with_consistent_supersessions()
            return LedgerReconciliation(
                links=[
                    SupersessionLink(new_rule=r.id, replaces=r.supersedes, note=f"{r.id} amends")
                    for r in consistent.rules
                    if r.supersedes
                ]
            )
        if call.schema == "RequoteResult":
            return RequoteResult()
        if call.schema == "PolicyIR":
            return self.ir.model_copy(deep=True)
        if call.schema == "ProseDraft":
            prose = self.prose or NodeProse()
            return ProseDraft(
                system_prompt_extra=prose.system_prompt_extra,
                nodes=[NodeText(node=name, text=text) for name, text in prose.per_node.items()],
            )
        if call.schema == "WalkthroughReport":
            from builder.selftest.walkthrough import WalkthroughReport

            return WalkthroughReport.model_validate(
                {"issues": self.walkthrough_issues, "checked": len(self.ledger.active_rules())}
            )
        raise FixtureMissing(f"the fixture gateway has no answer for a {call.schema} call ({call.purpose})")

    def _ledger_part(self, call: FixtureCall) -> LedgerPart:
        """The fixture rules quoted inside the requested passage; no rules for the completeness pass."""
        match = _TARGET.search(call.user)
        if match is None:
            return LedgerPart()
        passage = normalize_text(match.group(1))
        drafts = [
            RuleDraft(
                id=rule.id,
                quote=rule.quote,
                category=rule.category,
                statement=rule.statement,
                scope=rule.scope,
            )
            for rule in self.ledger.rules
            if normalize_text(rule.quote) in passage
        ]
        ambiguities: list[dict[str, Any]] = []
        if not self._ambiguities_given:
            self._ambiguities_given = True
            ambiguities = [a.model_dump() for a in self.ledger.ambiguities]
        return LedgerPart.model_validate(
            {"rules": [d.model_dump() for d in drafts], "ambiguities": ambiguities}
        )


def load_fixture_gateway(
    directory: Path | str, document_names: Sequence[str], trace: Any = None, observer: Any = None
) -> FixtureGateway:
    """Build a :class:`FixtureGateway` from a fixture directory (``build.py --fake-llm DIR``).

    The directory holds ``ledger.json`` and ``ir.json`` (optionally ``prose.json`` and ``walkthrough.json``), or one or
    more ``<name>_ledger.json`` / ``<name>_ir.json`` pairs. With several pairs the one whose ledger ``source_files``
    equal ``document_names`` is used.

    Raises:
        FileNotFoundError: No usable pair was found.
    """
    base = Path(directory)
    pairs: list[tuple[Path, Path, Path | None, Path | None]] = []
    if (base / "ledger.json").is_file() and (base / "ir.json").is_file():
        pairs.append((base / "ledger.json", base / "ir.json", base / "prose.json", base / "walkthrough.json"))
    for ledger_path in sorted(base.glob("*_ledger.json")):
        stem = ledger_path.name[: -len("_ledger.json")]
        ir_path = base / f"{stem}_ir.json"
        if ir_path.is_file():
            pairs.append(
                (ledger_path, ir_path, base / f"{stem}_prose.json", base / f"{stem}_walkthrough.json")
            )
    if not pairs:
        raise FileNotFoundError(
            f"no ledger.json + ir.json (or <name>_ledger.json + <name>_ir.json) in {base}"
        )
    chosen = pairs[0]
    if len(pairs) > 1:
        wanted = sorted(document_names)
        for pair in pairs:
            ledger = _as_ledger(pair[0])
            if sorted(ledger.source_files) == wanted:
                chosen = pair
                break
    ledger_path, ir_path, prose_path, walkthrough_path = chosen
    prose = None
    if prose_path is not None and prose_path.is_file():
        prose = NodeProse.model_validate(json.loads(prose_path.read_text(encoding="utf-8")))
    issues: list[Any] = []
    if walkthrough_path is not None and walkthrough_path.is_file():
        issues = json.loads(walkthrough_path.read_text(encoding="utf-8"))
    return FixtureGateway(
        ledger_path, ir_path, prose, walkthrough_issues=issues, trace=trace, observer=observer
    )


# ----------------------------------------------------------------------------------------------------- fake Freya


class FakeFreya:
    """In-memory stand-in for :class:`builder.freya.client.FreyaClient` (see the module docstring).

    Args:
        trace: Receives ``freya(...)`` lines like the real client (optional).
        reject_validations: One list of problem texts per ``validate`` call that must be rejected, in order; later
            calls succeed.
        reject_saves: The same for ``save`` (the platform rejecting on write).
        fail_save: Make ``save`` raise a transport error.
        opening: The agent's first chat reply.
        replies: Further chat replies, used in order (the last one repeats).
    """

    def __init__(
        self,
        trace: Any = None,
        *,
        reject_validations: Sequence[Sequence[str]] = (),
        reject_saves: Sequence[Sequence[str]] = (),
        fail_save: bool = False,
        opening: str = "Hello, this is a digital assistant. Am I speaking with the customer?",
        replies: Sequence[str] = ("I can only talk to the customer named on this call.",),
    ) -> None:
        self.trace = trace
        self._validation_rejections = [list(p) for p in reject_validations]
        self._save_rejections = [list(p) for p in reject_saves]
        self.fail_save = fail_save
        self.opening = opening
        self.replies = list(replies) or [""]
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.saved: dict[str, dict[str, Any]] = {}
        self.versions: dict[str, int] = {}
        self.chats: dict[str, list[tuple[str, str]]] = {}
        self.chat_variables: list[dict[str, Any] | None] = []
        self._ids = itertools.count(1)

    def _note(self, action: str, *args: Any) -> None:
        self.calls.append((action, args))
        if self.trace is not None:
            self.trace.freya(action, fake=True)

    @staticmethod
    def _reject(queue: list[list[str]], action: str) -> None:
        if queue:
            problems = queue.pop(0)
            raise FreyaValidationError(
                "the fake platform rejected the workflow", action=action, status=422, problems=problems
            )

    def workspace_id(self) -> str:
        """A fixed fake workspace id."""
        return "ws_fake"

    def validate(self, name: str, workflow: dict[str, Any]) -> dict[str, Any]:
        """Accept the workflow, or reject it with queued problems."""
        self._note("validate", name)
        self._reject(self._validation_rejections, "validate")
        return {"valid": True}

    def save(self, slot: str, workflow: dict[str, Any], state_file: Path | str | None = None) -> DeployInfo:
        """Create or update the slot's fake workflow."""
        self._note("save", slot)
        if self.fail_save:
            raise FreyaError("fake transport failure", action="save", kind="transport")
        self._reject(self._save_rejections, "save")
        created = slot not in self.saved
        if created:
            self.saved[slot] = {
                "workflow_id": f"wf_fake_{next(self._ids)}",
                "agent_id": f"ag_fake_{next(self._ids)}",
            }
        entry = self.saved[slot]
        entry["workflow"] = json.loads(json.dumps(workflow))
        self.versions[entry["workflow_id"]] = self.versions.get(entry["workflow_id"], 0) + 1
        return DeployInfo(
            workflow_id=entry["workflow_id"],
            agent_id=entry["agent_id"],
            version=self.versions[entry["workflow_id"]],
            created=created,
            node_count=count_nodes(workflow),
            tool_count=count_tools(workflow),
            slot=slot,
            published=True,
        )

    def _entry(self, workflow_id: str) -> dict[str, Any]:
        for entry in self.saved.values():
            if entry["workflow_id"] == workflow_id:
                return entry
        raise FreyaError(f"unknown fake workflow {workflow_id}", action="get_workflow", status=404)

    def get_workflow(self, workflow_id: str) -> dict[str, Any]:
        """The definition as saved (nothing is stripped)."""
        self._note("get_workflow", workflow_id)
        return json.loads(json.dumps(self._entry(workflow_id)["workflow"]))

    def export(self, workflow_id: str, path: Path | str) -> Path:
        """Write the kit's export format to ``path``."""
        self._note("export", workflow_id)
        entry = self._entry(workflow_id)
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(
                {
                    "workflow_id": entry["workflow_id"],
                    "agent_id": entry["agent_id"],
                    "updated_at": "fake",
                    "workflow": entry["workflow"],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return target

    def chat(
        self,
        workflow_id: str,
        message: str,
        customer_variables: Mapping[str, Any] | None = None,
        call_id: str | None = None,
        *,
        version_id: str | None = None,
    ) -> ChatTurn:
        """Scripted chat: the opening first, then the configured replies."""
        self._note("chat", workflow_id, message)
        self.chat_variables.append(dict(customer_variables) if customer_variables else None)
        self._entry(workflow_id)
        call_id = call_id or f"call_fake_{next(self._ids)}"
        history = self.chats.setdefault(call_id, [])
        reply = self.opening if not history else self.replies[min(len(history) - 1, len(self.replies) - 1)]
        history.append((message, reply))
        return ChatTurn(response=reply, call_id=call_id, raw={"callId": call_id, "response": reply})

    def end_chat(self, call_id: str) -> dict[str, Any]:
        """Acknowledge the end of a chat."""
        self._note("end_chat", call_id)
        return {"ended": True}

    def get_call(self, call_id: str) -> dict[str, Any]:
        """The scripted transcript of a chat."""
        self._note("get_call", call_id)
        return {"id": call_id, "messages": [{"user": u, "agent": a} for u, a in self.chats.get(call_id, [])]}

    def simulation_slot(self, timeout_s: float | None = None) -> contextlib.AbstractContextManager[None]:
        """No lock is needed in memory."""
        return contextlib.nullcontext()


__all__ = [
    "FakeFreya",
    "FixtureCall",
    "FixtureGateway",
    "FixtureMissing",
    "load_fixture_gateway",
]
