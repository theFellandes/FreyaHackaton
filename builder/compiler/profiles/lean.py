"""The ``lean`` profile: fewer nodes, the model closes the call itself.

Experiments E-3 / E-4 compare it with the staged design. Same code and hook library, same prompt templates and tools; what
changes is the graph and who decides the outcome::

    setup (code) -> call -> load_account -> account_facts (code) -> negotiate -> hangup
                      \\------------------------------------------------------/
                      (verify_identity, schedule_callback, transfer_to_agent, close_call are tools of ``call``)

* ``call`` is opening, third-party handling and identity in one conversation node.
* ``negotiate`` carries ``create_promise``, the notes tool, ``transfer_to_agent`` and ``close_call``.
* There are no ``end_*`` / ``handoff_*`` / ``decide`` / ``close`` nodes: the prompt carries the outcome table, and the model
  calls ``close_call`` (once, last, after the goodbye). A deterministic transition on ``close_call_success_count`` ends the
  call, so a closed call never lingers.

What lean gives up: the deterministic outcome engine (the model picks the row of the table) and the fixed post-promise,
callback and identity-failure sentences. What it keeps: all computed facts, fixed opening and debt statement, hooks, and
deterministic routing on identity and on the close.
"""

from __future__ import annotations

from typing import Any

from builder.compiler import codegen, naming
from builder.compiler.context import LeanExits
from builder.compiler.errors import CompileError
from builder.compiler.nodes import (
    bool_true,
    code_node,
    counter_above,
    end_call_node,
    equals,
    or_group,
    tool_transition,
    transition,
)
from builder.compiler.profiles.staged import NEGOTIATION_STAGES, ProfileBuilder
from builder.compiler.prompts import render
from builder.compiler.prose import NodeProse
from builder.compiler.roles import close_roles, customer_binding, queue_field
from builder.compiler.toolset import build_tools
from builder.compiler.workflow_model import Workflow
from builder.ingest.tool_api import ToolApi
from builder.policy.ir import OutcomeRef, PolicyIR, RuleLedger

__all__ = ["build_lean", "build_lean_with_notes"]

#: Ledger scope -> the lean node whose task carries it (see ``ProfileBuilder.finalize_texts``).
LEAN_STAGE_NODES = {"global": "system_prompt", "opening": "call", "third_party": "call", "identity": "call",
                    **{scope: "negotiate" for scope in NEGOTIATION_STAGES}}


class _Lean(ProfileBuilder):
    """The lean node graph."""

    def __init__(self, ir: PolicyIR, api: ToolApi, ledger: RuleLedger, prose: NodeProse) -> None:
        super().__init__(ir, api, ledger, prose)
        self.style = LeanExits()
        self.fields = self.style

    def close_phrase(self, situation: str, ref: OutcomeRef | None) -> str:
        """``call close_call with outcome "x" and reason "y"``."""
        return self.style.end(situation, ref)

    def name_contract_fields(self) -> None:
        """Let the prompts use the contract's own field names for the outcome, the reason and the queue of the tools."""
        api = self.api
        if "close_call" not in api.tools:
            return  # build_tools reports the missing tool
        customer = customer_binding(api, self.ir.call_variable_names())
        roles = close_roles(api.tools["close_call"], customer.field)
        transfer = api.tools.get("transfer_to_agent")
        queue = queue_field(transfer, customer.field) if transfer is not None else None
        self.style = self.fields = LeanExits(roles.outcome, roles.reason or "reason", queue or "queue")

    def _row_ref(self, situation: str) -> OutcomeRef | None:
        row = next((r for r in naming.closing_rows(self.ir) if r.situation == situation), None)
        return row.ref() if row else None

    def closing_text(self) -> str:
        """The outcome table as prompt text, strongest row first, with the promise / transfer / notes special rules."""
        ir = self.ir
        order = ir.closing.resolved_precedence()
        rows = sorted(naming.closing_rows(ir), key=lambda r: order.index(r.situation) if r.situation in order else len(order))
        goodbye = self.freya(ir.phrases.goodbye_sentence).strip()
        said = f'after you said goodbye ("{goodbye}")' if goodbye else "after you said goodbye"
        lines = [
            f"Every call ends with exactly one close_call, as the LAST tool call, {said}. Choose the "
            "outcome and the reason from this table; when several rows fit, the first one in the list wins:"
        ]
        for row in rows:
            when = row.when if row.when and not row.when.startswith("(derived") else row.situation.replace("_", " ").replace(":", " to ")
            qualifier = {True: " (identity verified)", False: " (identity not verified)", None: ""}[row.requires_verified]
            lines.append(f"- {when.rstrip('.')}{qualifier}: {self.fields.text(row.ref())}")
        tap = ir.closing.transfer_after_promise_outcome
        if tap is not None:
            reason = (f' and {self.fields.reason_field} "{tap.reason}"' if tap.reason
                      else f" and the {self.fields.reason_field} of that transfer")
            lines.append(f'If a transfer follows a recorded promise, close with {self.fields.outcome_field} "{tap.outcome}"{reason}.')
        elif ir.closing.promise_wins:
            lines.append("A recorded promise decides the outcome, whatever else happened.")
        mp = ir.multi_product
        if mp.enabled and mp.closing_reason_rule == "first_non_paid_note":
            paid = f' "{mp.paid_note_reason}"' if mp.paid_note_reason else ""
            lines.append(
                "With several products and no recorded promise: close with the reason of the first product (in the "
                f"listed order) whose note is not{paid or ' a payment claim'}"
                + (f"; if every note is{paid}, use {self.fields.text(mp.all_paid_outcome)}" if mp.all_paid_outcome else "")
                + "."
            )
        lines.append("After close_call succeeded, say nothing more.")
        place = ("node:call/task_message", "node:negotiate/task_message", "tool:close_call")
        self.rm(ir.closing.rule_ids, *place)
        for row in ir.closing.rows:
            self.rm(row.rule_ids, *place)
        return "\n".join(lines)

    def build(self) -> Workflow:
        ir = self.ir
        api = self.api
        if not ir.identity.questions:
            raise CompileError("the policy has no identity question; the agent cannot verify the customer")
        self.name_contract_fields()
        style = self.style
        failed_phrase = self.close_phrase("recording_failed", ir.recording.failure_outcome)
        identity_failed = self.close_phrase("identity_failed", ir.identity.on_failed)
        self.tools = build_tools(ir, api, promise_failed_phrase=failed_phrase, lean=True,
                                 identity_failed_phrase=identity_failed,
                                 identity_done_phrase=f"call {naming.transition_tool('load_account')}")
        self.notes.extend(self.tools.notes)
        shape = self.tools.shape
        assert shape is not None
        callback = ir.third_party.callback

        # ---- the call node -------------------------------------------------------------------------------------
        opening_intents = self.stage_intents("opening")
        have = {i.key for i in opening_intents}
        opening_intents += [i for i in self.third_party_intents() if i.key not in have]
        opening = self.opening_text(
            bullets=self.bullets("opening", opening_intents, style, node="call"),
            unclear_action=self.close_phrase("unclear_party", ir.right_party.unclear_outcome),
            confirmed_action="go on with the security questions (see IDENTITY below)",
            third_party_action="follow the section IF SOMEONE ELSE ANSWERED below",
            node="call",
            extra=self.extra("opening"),
        )
        third_party = self.third_party_text(
            bullets=self.bullets("third_party", self.third_party_intents(), style, node="call"),
            not_booked_action=self.close_phrase("callback_not_booked", callback.failure if callback else None),
            booked_line=("When it is booked, thank them, say goodbye and "
                         + self.close_phrase("callback_booked", callback.success if callback else None) + "."),
            handover_action="go on with the security questions (see IDENTITY below)",
            node="call",
            extra=self.extra("third_party"),
        )
        identity = self.identity_text(
            bullets=self.bullets("identity", self.stage_intents("identity"), style, node="call"),
            declined_action=self.close_phrase("identity_declined", ir.identity.on_declined),
            counting=("the system counts them and tells you when the check has ended." if shape.counter else
                      f"count them yourself: after the last allowed wrong answer, say goodbye and {identity_failed}."),
            node="call",
            extra=self.extra("identity"),
        )
        closing = self.closing_text()
        call_task = render("lean", stage="call", opening_text=opening, third_party_text=third_party,
                           identity_text=identity, negotiate_text="", closing_text=closing, extra=self.extra("call"))
        call_tools = [t for t in (self.tools.verify, self.tools.callback, self.tools.transfer, self.tools.close) if t]
        routes: list[dict[str, Any]] = []
        if shape.all_verified:
            routes.append(transition("load_account", "All security answers verified",
                                     or_group(*bool_true(f"verify_identity_response.data.{shape.all_verified}"))))
        elif self.tools.identity_tracked:  # AUD-23: the post-hook counted every question verified
            routes.append(transition("load_account", "All security answers verified",
                                     or_group(equals("identity_all_verified", "yes"))))
        else:
            routes.append(tool_transition("load_account", "Every security answer was verified; nothing more to say."))
        routes.append(transition("hangup", "The call is closed", or_group(counter_above("close_call_success_count", 0))))
        self.nodes["setup"] = code_node("setup", codegen.setup_snippet(ir), "call")
        self.nodes["call"] = {
            "name": "call",
            "node_type": "conversation",
            "on_enter": {"type": "tts", "text": self.opening_line()},
            "on_enter_first_entry_only": True,
            "task_message": call_task,
            "tools": call_tools,
            "max_node_turns": 60,
            "max_turn_transition": "hangup",
            "transitions": routes,
        }
        self.rm(ir.calendar.rule_ids, "code:setup")

        # ---- account and negotiation ---------------------------------------------------------------------------
        self.account_nodes("negotiate")
        promise_ref = self._row_ref("promise_recorded")
        negotiate_task = self.negotiate_text(
            bullets=self.bullets("negotiate", self.stage_intents("negotiate"), style),
            stop_phrase=self.close_phrase(self.stop_situation, ir.persuasion.stop_outcome),
            promise_end_phrase=self.close_phrase("promise_recorded", promise_ref),
            failed_phrase=failed_phrase,
            extra="",
        )
        negotiate_text = render("lean", stage="negotiate", opening_text="", third_party_text="", identity_text="",
                                negotiate_text=negotiate_task, closing_text=closing, extra=self.extra("negotiate"))
        books = any(naming.intent_exit(ir, i).callback for i in self.stage_intents("negotiate"))  # AUD-08
        negotiate_tools = [t for t in (self.tools.promise, self.tools.note, self.tools.callback if books else None,
                                       self.tools.transfer, self.tools.close) if t]
        self.nodes["negotiate"] = {
            "name": "negotiate",
            "node_type": "conversation",
            "on_enter": {"type": "tts", "text": "{{debt_statement}}"},
            "on_enter_first_entry_only": True,
            "task_message": negotiate_text,
            "tools": negotiate_tools,
            "max_node_turns": 60,
            "max_turn_transition": "hangup",
            "transitions": [
                transition("hangup", "The call is closed", or_group(counter_above("close_call_success_count", 0))),
            ],
        }
        self.nodes["hangup"] = end_call_node("hangup", "call_closed")
        self.residual_to_rule_map()
        system_prompt = self.finalize_texts(self.system_prompt(), LEAN_STAGE_NODES)
        self.finish()
        return Workflow.from_json(
            {"initial_node": "setup", "system_prompt": system_prompt, "nodes": self.nodes, "global_nodes": {}})


def build_lean_with_notes(
    ir: PolicyIR, tool_api: ToolApi, ledger: RuleLedger, prose: NodeProse
) -> tuple[Workflow, dict[str, list[str]], list[str]]:
    """Like :func:`build_lean`, also returning the compiler's notes."""
    builder = _Lean(ir, tool_api, ledger, prose)
    workflow = builder.build()
    return workflow, builder.rule_map, builder.notes


def build_lean(
    ir: PolicyIR, tool_api: ToolApi, ledger: RuleLedger, prose: NodeProse
) -> tuple[Workflow, dict[str, list[str]]]:
    """Compile the lean agent; returns the workflow and the rule map (see :func:`build_staged`).

    Raises:
        CompileError: When the tool API lacks a tool the policy needs.
    """
    workflow, rule_map, _notes = build_lean_with_notes(ir, tool_api, ledger, prose)
    return workflow, rule_map
