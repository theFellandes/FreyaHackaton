"""The ``staged`` profile: the validated agent design, generated from the PolicyIR, the ToolApi and the RuleLedger.

The structure copies the hand-authored reference (``evals/reference/brief_a_ref.py``, 9/10 on the official scorer) and the
lessons measured on it; only the content is generic. Node graph (``docs/COMPILER.md`` has the full table)::

    setup (code) -> opening -> identity -> load_account -> account_facts (code) -> negotiate
                       |  \\-> third_party -> callback_done            |  \\-> promise_done -> end_promise_recorded
                       |         (schedule_callback)                    \\-> end_<situation> | handoff_<queue>
                       \\-> end_<situation> | handoff_<queue>                     \\-> decide (code) -> close -> hangup

Mechanisms (each one is there because the reference runs proved it):

* **Code nodes compute facts.** ``setup`` writes the date table; ``account_facts`` writes per-product deadlines, floors,
  the exact floor-offer sentence, scripted persuasion attempts, allowed dates and the debt statement; ``decide`` mirrors the
  reference outcome engine.
* **Fixed sentences are spoken by the engine** (``on_enter`` ``tts``): the opening, the debt statement (``{{debt_statement}}``),
  the callback confirmation, the post-promise sentence and the identity-failure line.
* **Tool-call transitions** let the model say goodbye and leave a node in the same message; the finishing nodes
  (``end_<situation>``) only record ``chosen_end``; ``decide`` then applies the IR's precedence to the recorded facts, so
  the model never chooses an outcome name.
* **Deterministic routes** use the engine's counters and tool results: all answers verified, no wrong answers left, a
  callback booked, the only product's promise recorded.
* **Goodbye-loop breaker**: a ``matches_intent`` transition to ``end_auto`` (and ``max_turn_transition``) with a
  ``call_situation`` extraction as the outcome fallback.
* **Hooks** write a ``what_to_do_next`` line into tool results, normalise tool arguments and keep a notes log for
  several-product briefs.
* **Guard recovery** (reference v10): the ``create_promise`` pre-hook blocks a forbidden promise and prepares the spoken
  correction (``promise_guard_say``); ``negotiate``'s first transition (``promise_guard`` contains ``blocked``) leads to
  ``promise_fix`` (code) and ``promise_fix_say`` (tts) and back to ``negotiate``. The platform does not run the post-hook of
  a blocked call (E-9), so the engine, not the model, says the correction.
* **Escape exits** (reference v9): ``identity`` can leave to ``third_party`` and to the wrong-number end, like the opening.

Every rule of the ledger ends up in ``rule_map`` with the mechanism that enforces it. The class :class:`ProfileBuilder`
holds what the staged and the lean profile share (prompt text, intent bullets, the rule map).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from builder.compiler import codegen, naming
from builder.compiler.context import (
    DEFAULT_DECLINE_EXPLANATION,
    DEFAULT_OVERRIDE_REFUSAL,
    ExitStyle,
    StagedExits,
    confirm_question,
    deferred_notes,
    exit_bullet,
    extra_phrase,
    failure_note_reason,
    ids_tag,
    language_name,
    ordinal,
    person_refs,
    quoted,
    recording_failure_texts,
    soften_recording_notice,
    times_text,
    to_freya,
    today_question,
    uncovered_rules,
    words,
)
from builder.compiler.errors import CompileError
from builder.compiler.nodes import (
    and_group,
    api_request_node,
    bool_true,
    code_node,
    contains,
    counter_above,
    end_call_node,
    equals,
    intent_transition,
    or_group,
    tool_transition,
    transition,
    tts_node,
)
from builder.compiler.prompts import render
from builder.compiler.prose import NodeProse, clean_text
from builder.compiler.tools import engine_call
from builder.compiler.toolset import ToolSet, build_tools
from builder.compiler.workflow_model import Workflow
from builder.ingest.tool_api import ToolApi
from builder.policy.ir import IntentRule, PolicyIR, RuleLedger

__all__ = ["ProfileBuilder", "build_staged", "build_staged_with_notes"]

#: Intent keys whose behaviour the opening / identity / negotiation prompts already spell out in their fixed text.
TEMPLATE_COVERED = frozenset({
    "robot_question", "scam_question", "unknown_question", "right_party_confirmed", "third_party", "unclear_party",
    "decline_verification", "cant_afford_full", "proposes_date", "proposes_amount", "vague_period", "flat_refusal",
    "change_promise",
})
OPENING_ONLY = frozenset({"right_party_confirmed", "third_party", "unclear_party", "decline_verification"})
NEGOTIATION_STAGES = ("negotiation", "confirmation", "recording", "closing", "disclosure")
GOODBYE_LOOP = (
    "The agent already said goodbye and the customer now only says goodbye or thanks to end the call "
    "(for example 'Thank you, goodbye')."
)


class ProfileBuilder:
    """Prompt text, intent bullets and rule-map bookkeeping shared by the staged and the lean profile."""

    def __init__(self, ir: PolicyIR, api: ToolApi, ledger: RuleLedger, prose: NodeProse) -> None:
        self.ir = ir
        self.api = api
        self.ledger = ledger
        self.prose = prose
        self.vars = list(ir.call_variable_names())
        self.nodes: dict[str, dict[str, Any]] = {}
        #: intent key -> guard node its exit goes through (the prompt must name that node's tool; see ``plan_latches``)
        self.exit_redirect: dict[str, str] = {}
        self.rule_map: dict[str, list[str]] = {}
        self.notes: list[str] = []
        self.address, self.full_name = person_refs(ir)
        self.stop_situation = naming.stop_situation(ir)
        self.tools: ToolSet
        self.allowed_vars = {*self.vars, "account_facts", "date_table", "debt_statement", "callback_facts"}

    # ---- small helpers ---------------------------------------------------------------------------------------------

    def rm(self, ids: Sequence[str], *places: str) -> None:
        """Record that the rules ``ids`` are enforced at ``places``."""
        for rule_id in ids:
            known = self.rule_map.setdefault(rule_id, [])
            known.extend(place for place in places if place not in known)

    def extra(self, node: str) -> str:
        """Cleaned LLM-drafted text for ``node`` (empty when none)."""
        return clean_text(self.prose.for_node(node), self.allowed_vars)

    def freya(self, text: str) -> str:
        """IR placeholders -> Freya references."""
        return to_freya(text, self.vars)

    def merge_duplicate_targets(self) -> None:
        """Make every node's transitions point at distinct targets.

        The engine merges transitions that share a ``target_node`` into one whose condition is the OR of the originals
        (``docs/workflow-format.md``): a tool-call exit and a semantic or variable transition to the same node would lose
        one of their behaviours. Two tool-call exits to one node are the same exit and are joined; any other pair is a
        compiler bug (the second transition needs a node of its own), reported as a ``CompileError``.
        """
        for name, node in self.nodes.items():
            first_to: dict[str, dict[str, Any]] = {}
            kept: list[dict[str, Any]] = []
            for transition_ in node.get("transitions", []):
                first = first_to.get(transition_["target_node"])
                if first is None:
                    first_to[transition_["target_node"]] = transition_
                    kept.append(transition_)
                elif first.get("is_tool_call") and transition_.get("is_tool_call"):
                    joined = f"{first['description'].rstrip('.')}; or {transition_['description']}"
                    first["description"] = first["condition"]["description"] = joined
                else:
                    raise CompileError(
                        f"node {name!r} has two transitions to {transition_['target_node']!r} that are not both "
                        "tool-call exits; the engine would merge them into one")
            node["transitions"] = kept

    def finish(self) -> dict[str, list[str]]:
        """The rule map without superseded rules; also adds the notes every profile shares."""
        self.merge_duplicate_targets()
        for rule_id in self.ledger.superseded_ids():
            self.rule_map.pop(rule_id, None)
        if "now" not in self.vars:
            self.notes.append("the policy declares no 'now' call variable; the date code falls back to the real clock")
        variable = self.tools.customer.variable
        if variable not in self.vars:
            self.notes.append(f"the policy declares no {variable!r} call variable; tool calls bake {{{{{variable}}}}} anyway")
        return self.rule_map

    # ---- intents ---------------------------------------------------------------------------------------------------

    def stage_intents(self, stage: str) -> list[IntentRule]:
        """Intents that act in ``stage`` (``opening``, ``identity`` or ``negotiate``), strongest precedence first."""
        scopes = ("before_verification", "any") if stage in ("opening", "identity") else ("after_verification", "any")
        skip = set(OPENING_ONLY) if stage in ("opening", "identity") else set()
        if stage == "identity":
            skip |= {"wrong_number", "deceased"}
        picked = [i for i in self.ir.intents if i.scope in scopes and i.key not in skip]
        return sorted(picked, key=lambda i: -i.precedence)

    def third_party_intents(self) -> list[IntentRule]:
        """The wrong-number and deceased exits, from the declared intents or the IR's own outcome references."""
        ir = self.ir
        found: list[IntentRule] = []
        deceased_line = ir.phrases.deceased_sentence.strip()
        for key, ref, text, response in (
            ("wrong_number", ir.third_party.wrong_number,
             "The person says the number does not belong to the customer", ""),
            ("deceased", ir.third_party.deceased, "The person says the customer has died",
             f'Say "{deceased_line}" and ask nothing else.' if deceased_line else ""),
        ):
            declared = next((i for i in ir.intents if i.key == key and i.outcome is not None), None)
            if declared is not None:
                found.append(declared)
            elif ref is not None:
                found.append(IntentRule(key=key, description=text, response=response, outcome=ref,
                                        scope="before_verification"))
        return found

    def bullets(self, stage: str, intents: Sequence[IntentRule], style: ExitStyle, *, node: str | None = None) -> list[str]:
        """Prompt bullets for ``intents`` (exits, notes and guidance-only intents the fixed text does not cover).

        ``node`` names the node whose ``task_message`` carries the bullets (default: ``stage``); it only matters for the rule map.
        """
        out: list[str] = []
        where = f"node:{node or stage}/task_message"
        for intent in intents:
            info = naming.intent_exit(self.ir, intent)
            self.rm(intent.rule_ids, where)
            if intent.key == "flat_refusal":  # spelled out by the persuasion rule of the negotiation prompt
                continue
            if info.kind == "info":
                if intent.key not in TEMPLATE_COVERED and (intent.response or intent.description):
                    label = intent.description.rstrip(".")
                    text = f"{label}: {intent.response}".strip(": ").strip()
                    out.append(f"{text} {ids_tag(intent.rule_ids)}".strip())
                continue
            bullet = exit_bullet(self.ir, intent, style, note_tool=self.ir.multi_product.note_tool,
                                 redirect=self.exit_redirect if stage == "negotiate" else None)
            if bullet is None:
                continue
            out.append(bullet)
        return out

    # ---- prompt texts (shared with the lean profile) ----------------------------------------------------------------

    def system_prompt(self) -> str:
        """The global system prompt."""
        ir = self.ir
        rules = uncovered_rules(ir, self.ledger, ["global"])
        self.rm([r[0] for r in rules], "system_prompt")
        self.rm(ir.rule_ids, "system_prompt")
        for intent in ir.intents:
            if intent.key in ("robot_question", "scam_question", "unknown_question"):
                self.rm(intent.rule_ids, "system_prompt")
        return render(
            "system_prompt",
            bank_name=ir.bank_name,
            language=language_name(ir.language),
            forbidden=quoted(ir.forbidden_before_verification),
            robot_answer=ir.phrases.robot_answer.strip(),
            scam_answer=ir.phrases.scam_answer.strip(),
            unknown_answer=extra_phrase(ir.phrases.extra, "unknown_answer"),
            currency=ir.currency_word.strip(),
            global_rules=rules,
            extra=clean_text(self.prose.system_prompt_extra, self.allowed_vars),
        )

    def right_party_question(self) -> str:
        """The question that asks who is speaking: the brief's, else a neutral one built from the form of address."""
        return self.freya(self.ir.phrases.right_party_question).strip() or f"Am I speaking with {self.address}?"

    def opening_line(self) -> str:
        """The fixed opening: the brief's first sentence and the right-party question.

        A brief that prescribes no opening sentence gets a neutral introduction (and a compile note), so the call never
        starts with a bare question.
        """
        sentence = soften_recording_notice(self.freya(self.ir.phrases.opening_sentence)).strip()
        if not sentence:
            required = [rule.id for rule in self.ledger.active_rules()
                        if "opening" in rule.scope and re.search(r"record", rule.quote, re.IGNORECASE)]
            if required:
                raise CompileError(
                    f"the policy prescribes no opening sentence, but the rule(s) {', '.join(required)} require a recording "
                    "notice in the opening; the neutral introduction would leave it out")
            sentence = f"Hello, this is {self.ir.bank_name.strip() or 'your bank'}'s digital assistant."
            self.notes.append("the policy prescribes no opening sentence; the call starts with a neutral introduction "
                              "(check the brief for a recording notice or another required opening)")
        return " ".join(part for part in (sentence, self.right_party_question()) if part)

    def opening_text(self, *, bullets: list[str], unclear_action: str, confirmed_action: str,
                     third_party_action: str, node: str = "opening", extra: str | None = None) -> str:
        """Task message of the opening step."""
        ir = self.ir
        rp = ir.right_party
        rules = uncovered_rules(ir, self.ledger, ["opening"])
        self.rm(ir.phrases.rule_ids, f"node:{node}/on_enter")
        self.rm(rp.rule_ids, f"node:{node}/on_enter", f"node:{node}/task_message")
        self.rm([r[0] for r in rules], f"node:{node}/task_message")
        return render(
            "opening",
            question=self.right_party_question(),
            address=self.address,
            confirm=quoted(rp.confirm_examples),
            not_confirm=quoted(rp.not_confirm_examples, "and"),
            max_asks=max(1, rp.max_asks),
            ordinal=ordinal(max(1, rp.max_asks)),
            unclear_action=unclear_action,
            confirmed_action=confirmed_action,
            third_party_action=third_party_action,
            exits=bullets,
            rules=rules,
            extra=self.extra(node) if extra is None else extra,
        )

    def third_party_text(self, *, bullets: list[str], not_booked_action: str, booked_line: str,
                         node: str = "third_party", extra: str | None = None) -> str:
        """Task message for a person who is not the customer."""
        ir = self.ir
        tp = ir.third_party
        cb = tp.callback
        rules = uncovered_rules(ir, self.ledger, ["third_party"])
        place = (f"node:{node}/task_message", "tool:schedule_callback")
        self.rm(tp.rule_ids, *place)
        self.rm(cb.rule_ids if cb else [], *place, "code:setup")
        self.rm([r[0] for r in rules], f"node:{node}/task_message")
        return render(
            "third_party",
            address=self.address,
            full_name=self.full_name,
            disclose_nothing=tp.disclose_nothing,
            notes=[self.freya(n) for n in tp.notes],
            ask_when_reachable=tp.ask_when_reachable,
            callback=cb is not None,
            schedule_tool="schedule_callback",
            extra_asks=cb.max_extra_asks if cb else 0,
            extra_asks_words=words(cb.max_extra_asks) if cb else "",
            not_booked_action=not_booked_action,
            booked_line=booked_line,
            exits=bullets,
            rules=rules,
            extra=self.extra(node) if extra is None else extra,
        )

    @staticmethod
    def _how(question: Any) -> str:
        text = question.normalisation.strip()
        if text:
            return text
        return {
            "date_iso": "send the date as YYYY-MM-DD, whatever way it was said.",
            "digits": f"send exactly {question.digits or 'the'} digits.",
            "year": "send the four-digit year.",
            "postcode": "send the postcode as said.",
            "text": "send the answer as said.",
        }[question.answer_format]

    def _complete_text(self) -> str:
        """What a complete answer is, per identity question (only complete answers may be sent to the tool)."""
        parts = []
        for q in self.ir.identity.questions:
            if q.answer_format == "date_iso":
                parts.append(f"{q.field}: a date needs day, month and year")
            elif q.answer_format == "year":
                parts.append(f"{q.field}: all four digits of the year")
            elif q.answer_format == "digits":
                parts.append(f"{q.field}: " + (f"exactly {q.digits} digits" if q.digits else "all the digits"))
            else:
                parts.append(f"{q.field}: the whole answer")
        return "; ".join(parts)

    def _partial_text(self) -> str:
        """Examples of a partial answer, from the question formats."""
        examples = []
        for q in self.ir.identity.questions:
            if q.answer_format == "date_iso":
                examples.append("only the day and the month")
            elif q.answer_format in ("digits", "year"):
                total = q.digits or 4
                examples.append(f"only {max(1, total // 2)} of the {total} digits")
        return "for example " + " or ".join(dict.fromkeys(examples)) if examples else "an answer that is cut short"

    def identity_text(self, *, bullets: list[str], declined_action: str, node: str = "identity",
                      extra: str | None = None,
                      counting: str = "the system counts them and moves on by itself.") -> str:
        """Task message of the identity step."""
        ir = self.ir
        ident = ir.identity
        rules = uncovered_rules(ir, self.ledger, ["identity"])
        place = (f"node:{node}/task_message", f"node:{node}/transitions", "tool:verify_identity/post_process_code")
        self.rm(ident.rule_ids, *place)
        self.rm([r[0] for r in rules], f"node:{node}/task_message")
        hints = [
            f'If the result carries the hint "{h.value}": {h.instruction.strip()}'
            + ("" if h.counts_as_wrong else " This does not count as a wrong answer.")
            for h in ident.hints
        ]
        return render(
            "identity",
            n_questions=words(len(ident.questions)),
            one_at_a_time=ident.one_at_a_time,
            questions=[{"ask": self.freya(q.ask), "field": q.field, "how": self._how(q)} for q in ident.questions],
            verify_tool="verify_identity",
            max_wrong=words(ident.max_wrong_total).capitalize(),
            hints=hints,
            explanation=(ident.decline_explanation.strip() or extra_phrase(ir.phrases.extra, "decline_explanation")
                         or DEFAULT_DECLINE_EXPLANATION),
            complete=self._complete_text(),
            partial=self._partial_text(),
            counting=counting,
            declined_action=declined_action,
            exits=bullets,
            rules=rules,
            extra=self.extra(node) if extra is None else extra,
        )

    def _refusal_note(self) -> str:
        """Note reason for a product whose persuasion attempts are used up (several-product briefs)."""
        rule = next((i for i in self.ir.intents if i.key == "flat_refusal" and i.note_reason), None)
        return rule.note_reason if rule and rule.note_reason else (self.ir.persuasion.stop_outcome.reason or "")

    def negotiate_text(self, *, bullets: list[str], stop_phrase: str, promise_end_phrase: str, failed_phrase: str,
                       node: str = "negotiate", extra: str | None = None) -> str:
        """Task message of the negotiation step."""
        ir = self.ir
        mp = ir.multi_product
        note_tool = mp.note_tool if mp.enabled else ""
        first_ask = today_question(ir)
        parts = [f"the {f}" for f in ir.confirmation.readback_fields]
        readback = (", ".join(parts[:-1]) + " and " + parts[-1]) if len(parts) > 1 else (parts[0] if parts else "the promise")
        note_reasons = ", ".join(mp.note_reasons)
        if mp.enabled and not note_reasons and mp.note_tool in self.api.tools:
            reason_field = next((s for s in self.api.tools[mp.note_tool].request_fields if s.enum), None)
            note_reasons = ", ".join(reason_field.enum or []) if reason_field else ""
        failure_note = failure_note_reason(ir)
        failure_rule, _ = recording_failure_texts(
            ir, "create_promise", failed_phrase=failed_phrase, note_tool=mp.note_tool,
            with_note=bool(note_tool and failure_note))
        rules = uncovered_rules(ir, self.ledger, list(NEGOTIATION_STAGES))
        text = render(
            "negotiate",
            next_semantics=ir.promise_dates.next_weekday_semantics,
            ids_dates=ids_tag(ir.promise_dates.rule_ids),
            ids_amounts=ids_tag(ir.amounts.rule_ids),
            ids_persuasion=ids_tag(ir.persuasion.rule_ids),
            ids_confirm=ids_tag(ir.confirmation.rule_ids),
            ids_recording=ids_tag(ir.recording.rule_ids),
            ids_multi=ids_tag(mp.rule_ids),
            today_question=first_ask,
            today_question_all=extra_phrase(ir.phrases.extra, "today_question_all") or first_ask,
            ask_full_first=ir.amounts.ask_full_first,
            never_open=ir.amounts.never_ask_open_amount,
            allow_today=ir.promise_dates.allow_today,
            business_days_only=ir.promise_dates.business_days_only,
            not_after_deadline=ir.promise_dates.not_after_deadline,
            shift=ir.promise_dates.non_business_day_shift,
            vague=ir.promise_dates.vague_period,
            after_deadline=ir.promise_dates.after_deadline,
            refuse_deadline_refusal=ir.promise_dates.refuse_deadline_is_refusal,
            floor_offer_times=ir.amounts.floor_offer_times,
            floor_times_words=words(ir.amounts.floor_offer_times),
            accept_between=ir.amounts.accept_between_floor_and_full,
            below_floor=ir.amounts.below_floor,
            below_sentence=extra_phrase(ir.phrases.extra, "below_floor"),
            non_answer_counts=ir.persuasion.non_answer_counts,
            stop_phrase=stop_phrase,
            intent_bullets=bullets,
            multi=mp.enabled,
            single_confirmation=ir.confirmation.single_confirmation_for_all_products,
            confirm_question=confirm_question(ir),
            one_date_mode=mp.one_date_after_deadline,
            note_tool=note_tool,
            refusal_note=self._refusal_note(),
            note_reasons=note_reasons,
            notes_on_transfer=mp.notes_on_transfer,
            failure_note_reason=failure_note,
            promise_tool="create_promise",
            promise_end_phrase=promise_end_phrase,
            failure_rule=failure_rule,
            fail_times=times_text(max(0, ir.recording.retries_on_failure) + 1),
            deferred=deferred_notes(ir),
            readback=readback,
            success_sentence=(ir.recording.success_sentence or ir.phrases.recorded_sentence).strip(),
            failure_sentence=ir.recording.failure_sentence.strip(),
            immutable_sentence=ir.recording.immutable_sentence.strip(),
            rules=rules,
            extra=self.extra(node) if extra is None else extra,
        )
        place = (f"node:{node}/task_message", f"node:{node}/transitions")
        for section in (ir.promise_dates, ir.amounts, ir.persuasion, ir.confirmation):
            self.rm(section.rule_ids, *place)
        self.rm(ir.recording.rule_ids, *place, "tool:create_promise/post_process_code")
        self.rm(ir.disclosure.rule_ids, f"node:{node}/on_enter", "code:account_facts")
        self.rm(mp.rule_ids, *place, *(["tool:add_note/pre_process_code"] if self.tools.note is not None else []))
        self.rm([r[0] for r in rules], f"node:{node}/task_message")
        return text

    def residual_to_rule_map(self) -> None:
        """Map residual rules (no structured home) to the prompt that carries their text."""
        for rule in self.ir.residual_rules:
            places = []
            for scope in rule.scope:
                if scope == "global":
                    places.append("system_prompt")
                elif scope in NEGOTIATION_STAGES:
                    places.append("node:negotiate/task_message")
                else:
                    places.append(f"node:{scope}/task_message")
            self.rm([rule.rule_id], *places)

    def account_nodes(self, next_node: str = "negotiate") -> None:
        """``load_account`` (engine call) and ``account_facts`` (code) nodes, shared by both profiles."""
        ir = self.ir
        contract = self.api.require("get_account")
        customer, account = self.tools.customer, self.tools.account
        self.nodes["load_account"] = api_request_node(
            "load_account",
            engine_call(contract, baked={customer.field: customer.ref}, max_retries=2),
            "account_facts",
            [(f"data.{account.list_field}", "products")],
        )
        self.nodes["account_facts"] = code_node("account_facts", codegen.account_facts_snippet(ir, account), next_node)
        known = {s.name for array in contract.response_fields if array.name == account.list_field
                 for s in (array.items or [])}
        wanted = {*ir.disclosure.state_fields, *ir.amounts.floor_field_by_type.values()}
        if known and wanted - known:
            self.notes.append(f"the policy reads get_account item fields that the contract does not list: "
                              f"{sorted(wanted - known)}")
        for section in (ir.disclosure, ir.amounts, ir.persuasion, ir.promise_dates, ir.calendar):
            self.rm(section.rule_ids, "code:account_facts")
        for band in ir.bands:
            self.rm(band.rule_ids, "code:account_facts", "code:setup")
        self.rm(ir.calendar.rule_ids, "code:setup")


class _Staged(ProfileBuilder):
    """The staged node graph."""

    def __init__(self, ir: PolicyIR, api: ToolApi, ledger: RuleLedger, prose: NodeProse) -> None:
        super().__init__(ir, api, ledger, prose)
        self.style = StagedExits()
        self._ends: dict[str, str] = {}
        self._handoffs: dict[str, str] = {}
        #: guard node name -> (node it guards the way to, the intent whose exit it guards, the latching intents)
        self._guards: dict[str, tuple[str, str, list[IntentRule]]] = {}
        #: intent key (the overridden one) -> the intents that latch and block its exit (set by :meth:`negotiate`)
        self._latched: dict[str, list[IntentRule]] = {}

    # ---- registries ------------------------------------------------------------------------------------------------

    def end(self, situation: str) -> str:
        """Node name of ``end_<situation>``; the node is created by :meth:`terminals`."""
        name = naming.end_node(situation)
        self._ends[name] = situation
        return name

    def end_alias(self, suffix: str, situation: str) -> str:
        """A second ``end_*`` node that records ``situation``, for a transition that must not share its target.

        The engine merges the transitions of a node that have the same ``target_node``; the goodbye-loop breaker and the
        tool-call exit of one node therefore need nodes of their own, although both end the call in the same situation.
        """
        name = naming.end_node(suffix)
        self._ends[name] = situation
        return name

    def guard(self, intent: IntentRule, target: str) -> str:
        """The name of the guard node in front of ``target`` for the overridden ``intent`` (created by :meth:`terminals`)."""
        name = f"guard_{naming.slug(intent.key)}"
        self._guards[name] = (target, intent.key, self._latched[intent.key])
        return name

    def handoff(self, queue: str) -> str:
        """Node name of ``handoff_<queue>``; the node is created by :meth:`terminals`."""
        if "transfer_to_agent" not in self.api.tools:
            raise CompileError("the policy transfers calls but the tool API has no 'transfer_to_agent' tool")
        name = naming.handoff_node(queue)
        self._handoffs[name] = queue
        return name

    def exit_transitions(self, stage: str, intents: Sequence[IntentRule]) -> list[dict[str, Any]]:
        """Tool-call transitions for the end / handoff exits of ``intents``."""
        transitions: list[dict[str, Any]] = []
        seen: set[str] = set()

        def add(target: str, description: str) -> None:
            if target not in seen:
                seen.add(target)
                transitions.append(tool_transition(target, description))

        for intent in intents:
            info = naming.intent_exit(self.ir, intent)
            base = intent.description.rstrip(".")
            guarded = intent.key in self._latched
            if info.kind == "end":
                target = self.end(info.situation)
                add(self.guard(intent, target) if guarded else target, f"{base}; you said goodbye.")
                self.rm(intent.rule_ids, f"node:{naming.end_node(info.situation)}", "code:decide",
                        f"node:{stage}/transitions")
            elif info.kind == "handoff":
                target = self.handoff(info.queue)
                add(self.guard(intent, target) if guarded else target, f"{base}; you said you will transfer them.")
                self.rm(intent.rule_ids, f"node:{naming.handoff_node(info.queue)}", "code:decide",
                        f"node:{stage}/transitions")
                if info.declined is not None and info.declined.kind == "end":
                    add(self.end(info.declined.situation), f"{base}; the customer declined; you said goodbye.")
        return transitions

    # ---- nodes -----------------------------------------------------------------------------------------------------

    def setup(self) -> None:
        ir = self.ir
        self.nodes["setup"] = code_node("setup", codegen.setup_snippet(ir), "opening")
        self.rm(ir.calendar.rule_ids, "code:setup")
        self.rm(ir.third_party.callback.rule_ids if ir.third_party.callback else [], "code:setup")

    def opening(self) -> None:
        ir = self.ir
        intents = self.stage_intents("opening")
        declared = {i.key for i in intents}
        intents += [i for i in self.third_party_intents() if i.key not in declared]
        unclear_node = self.end("unclear_party")
        task = self.opening_text(
            bullets=self.bullets("opening", intents, self.style),
            unclear_action=f"call {naming.transition_tool(unclear_node)}",
            confirmed_action=f"call {naming.transition_tool('identity')} (no text needed)",
            third_party_action=f"call {naming.transition_tool('third_party')}",
        )
        self.nodes["opening"] = {
            "name": "opening",
            "node_type": "conversation",
            "on_enter": {"type": "tts", "text": self.opening_line()},
            "on_enter_first_entry_only": True,
            "task_message": task,
            "max_node_turns": 30,
            "max_turn_transition": unclear_node,
            "transitions": [
                tool_transition("identity", "The person confirmed they are the customer."),
                tool_transition("third_party", "Someone other than the customer answered."),
                tool_transition(unclear_node, "Still unclear who answered after the last ask; you said goodbye."),
                *self.exit_transitions("opening", intents),
            ],
        }
        self.rm(ir.right_party.rule_ids, "node:opening/transitions")

    def third_party(self) -> None:
        ir = self.ir
        callback = ir.third_party.callback
        intents = self.third_party_intents()
        booked = self.end("callback_booked") if callback is not None else ""
        not_booked = self.end("callback_not_booked")
        loop_end = self.end_alias("auto_third_party", "callback_not_booked")
        task = self.third_party_text(
            bullets=self.bullets("third_party", intents, self.style),
            not_booked_action=f"call {naming.transition_tool(not_booked)}",
            booked_line="The system then confirms the booking and says goodbye.",
        )
        transitions: list[dict[str, Any]] = []
        node: dict[str, Any] = {
            "name": "third_party",
            "node_type": "conversation",
            "task_message": task,
            "max_node_turns": 30,
            "max_turn_transition": not_booked,
            "transitions": transitions,
        }
        if callback is not None and self.tools.callback is not None:
            node["tools"] = [self.tools.callback]
            transitions.append(transition("callback_done", "The callback was booked",
                                          or_group(counter_above("schedule_callback_success_count", 0))))
        transitions.append(intent_transition(loop_end, "The conversation is over", GOODBYE_LOOP))
        if callback is not None:
            transitions.append(tool_transition(booked, "The callback is booked; you said goodbye."))
        transitions.append(tool_transition(
            not_booked, "No callback inside the allowed times, or nothing to book; you said goodbye."))
        transitions.extend(self.exit_transitions("third_party", intents))
        self.nodes["third_party"] = node
        self.rm(ir.third_party.rule_ids, "node:third_party/transitions")
        if callback is not None:
            self.rm(callback.rule_ids, "node:third_party/transitions", "node:callback_done", "code:decide")
            sentence = "The callback is booked. " + self.freya(ir.phrases.goodbye_sentence)
            self.nodes["callback_done"] = tts_node(
                "callback_done", sentence.strip(), booked,
                "The callback is booked. Say nothing more. No tool call is allowed in this turn.")

    def identity(self) -> None:
        ir = self.ir
        ident = ir.identity
        shape = self.tools.shape
        assert shape is not None
        declined_node = self.end("identity_declined")
        intents = self.stage_intents("identity")
        # Escape exits (reference v9): the person answering may turn out not to be the customer at any point before
        # verification, so identity can leave to ``third_party`` and to the wrong-number end like the opening can.
        wrong_number = [
            i if i.response else i.model_copy(update={"response": "Apologise for the mistake."})
            for i in self.third_party_intents() if i.key == "wrong_number"
        ]
        counter = shape.counter
        failed_end = naming.transition_tool(self.end("identity_failed")) if not counter else ""
        not_customer = (
            "If the person says they are not the customer (for example a relative or a colleague), say nothing about "
            f"the account, ask no further security question and call {naming.transition_tool('third_party')}."
        )
        task = self.identity_text(
            bullets=[*self.bullets("identity", intents, self.style), not_customer,
                     *self.bullets("identity", wrong_number, self.style)],
            declined_action=f"call {naming.transition_tool(declined_node)}",
            counting=("the system counts them and moves on by itself." if counter else
                      f"count them yourself: after the last allowed wrong answer thank them, say goodbye and call {failed_end}."),
        )
        transitions: list[dict[str, Any]] = []
        if shape.all_verified:
            transitions.append(transition(
                "load_account", "All security answers verified",
                or_group(*bool_true(f"verify_identity_response.data.{shape.all_verified}"))))
        else:
            transitions.append(tool_transition("load_account", "Every security answer was verified; nothing more to say."))
        if counter:
            limit = "0" if shape.left else str(ident.max_wrong_total)  # a counter that counts up ends at the policy limit
            transitions.append(transition(
                "identity_failed", "No wrong answers left",
                or_group(equals(f"verify_identity_response.data.{counter}", limit))))
        transitions.append(tool_transition(declined_node, "The customer declined the security questions; you said goodbye."))
        transitions.append(tool_transition("third_party", "The person says they are not the customer "
                                                          "(for example a relative or a colleague)."))
        transitions.extend(self.exit_transitions("identity", [*intents, *wrong_number]))
        if not counter:
            transitions.append(tool_transition(self.end("identity_failed"), "Too many wrong answers; you said goodbye."))
        ask = self.freya(ident.questions[0].ask)
        self.nodes["identity"] = {
            "name": "identity",
            "node_type": "conversation",
            "on_enter": {"type": "llm",
                         "prompt": f'Ask exactly: "{ask}" Say nothing about the account or why you are calling.'},
            "on_enter_first_entry_only": True,
            "task_message": task,
            "tools": [self.tools.verify],
            "max_node_turns": 60,  # generous: a talkative customer burns turns on fragments; the cap leads to ``decide``
            "max_turn_transition": self.end("auto"),
            "transitions": transitions,
        }
        if counter:
            failure = self.freya(" ".join(x for x in (ir.phrases.identity_failed_sentence.strip(),
                                                      ir.phrases.goodbye_sentence.strip()) if x))
            failure = failure or "I'm sorry, I could not verify your identity, so I cannot continue."
            self.nodes["identity_failed"] = tts_node(
                "identity_failed", failure, self.end("identity_failed"),
                "The identity check failed. Say nothing more. No tool call is allowed in this turn.")
            self.rm(ident.rule_ids, "node:identity_failed", "code:decide")

    def negotiate(self) -> None:
        ir = self.ir
        mp = ir.multi_product
        stop_node = self.end(self.stop_situation)
        promise_node = self.end("promise_recorded")
        failed_node = self.end("recording_failed")
        auto_node = self.end("auto")
        intents = self.stage_intents("negotiate")
        latching = self.plan_latches(intents)
        task = self.negotiate_text(
            bullets=self.bullets("negotiate", intents, self.style),
            stop_phrase=f"call {naming.transition_tool(stop_node)}",
            promise_end_phrase=f"call {naming.transition_tool(promise_node)}",
            failed_phrase=f"call {naming.transition_tool(failed_node)}",
        )
        transitions: list[dict[str, Any]] = [
            transition("promise_fix", "The promise guard blocked a create_promise call",
                       or_group(contains("promise_guard", "blocked"))),
            transition("promise_done", "The only product's promise is recorded",
                       or_group(and_group(counter_above("create_promise_success_count", 0),
                                          equals("product_count", "1")))),
            intent_transition(auto_node, "The conversation is over", GOODBYE_LOOP),
            tool_transition(promise_node,
                            "A promise was recorded, the confirmation sentence was said and you said goodbye."),
            tool_transition(stop_node, "The persuasion attempts are used up or the customer refuses to talk about "
                                       "paying; you said goodbye."),
            tool_transition(failed_node, "create_promise failed twice; you said you could not record it and goodbye."),
            *self.exit_transitions("negotiate", intents),
        ]
        situations = [s for s in naming.negotiation_situations(ir) if s != "promise_recorded"]
        descriptions = self._situation_descriptions()
        extraction: list[dict[str, Any]] = [{
            "name": "call_situation",
            "type": "string",
            "min_confidence": 0.6,
            "enum_values": ["none", *situations],
            "description": ("What best describes the customer's position so far: "
                            + "; ".join(f"{key} ({descriptions.get(key, key.replace('_', ' '))})" for key in situations)
                            + "; none (none of these yet)."
                            + "".join(f" Once {x.key} applies it stays the answer, whatever else they ask for later."
                                      for x in latching)),
        }]
        # The latch of an intent that takes precedence (``IntentRule.overrides``): a yes/no that stays yes once the intent
        # was expressed AT ANY POINT, because ``call_situation`` is overwritten by every later message (PUB-05: hardship,
        # then "I just need a discount" flipped it to restructuring and the call ended in the restructuring queue).
        for x in latching:
            extraction.append({
                "name": f"{naming.slug(x.key)}_seen",
                "type": "string",
                "min_confidence": 0.6,
                "enum_values": ["yes", "no"],
                "description": (
                    f"yes if at ANY point in this call the customer said something that fits: {x.description.rstrip('.')}. "
                    "It stays yes for the rest of the call, whatever they say later (a later request for a discount or a "
                    "waiver does not change it). Otherwise no."),
            })
        self.nodes["negotiate"] = {
            "name": "negotiate",
            "node_type": "conversation",
            "on_enter": {"type": "tts", "text": "{{debt_statement}}"},
            "on_enter_first_entry_only": True,
            "task_message": task,
            "tools": [self.tools.promise, *([self.tools.note] if self.tools.note else [])],
            "max_node_turns": 60,
            "max_turn_transition": auto_node,
            "extraction_schema": extraction,
            "transitions": transitions,
        }
        self.rm(ir.recording.rule_ids, "node:promise_done")
        self.rm(ir.phrases.rule_ids, "node:negotiate/on_enter", "node:promise_done", "system_prompt")
        self.rm(mp.rule_ids, "code:decide")
        text = " ".join(x for x in ("Your promise is recorded.",
                                     (ir.recording.success_sentence or ir.phrases.recorded_sentence).strip(),
                                     ir.phrases.goodbye_sentence.strip()) if x)
        self.nodes["promise_done"] = tts_node(
            "promise_done", self.freya(text), promise_node,
            "The promise is recorded. Say nothing more. No tool call is allowed in this turn.")
        # Guard recovery: a blocked create_promise call leaves the model with a generic error (the post-hook does not run
        # on that path), so the engine speaks the correction the pre-hook prepared and then resumes the conversation.
        self.nodes["promise_fix"] = code_node("promise_fix", codegen.promise_fix_snippet(), "promise_fix_say")
        self.nodes["promise_fix_say"] = tts_node(
            "promise_fix_say", "{{promise_fix_text}}", "negotiate",
            "Say nothing more now. No tool call is allowed in this turn.")
        self.rm(ir.promise_dates.rule_ids, "tool:create_promise/pre_process_code", "node:promise_fix_say")
        self.rm(ir.amounts.rule_ids, "tool:create_promise/pre_process_code", "node:promise_fix_say")

    def situations_of(self, intent: IntentRule) -> list[str]:
        """The ``call_situation`` values that mean ``intent`` happened: its key and the situations its exits end in."""
        info = naming.intent_exit(self.ir, intent)
        found = [intent.key]
        for candidate in (info, info.declined):
            if candidate is not None and candidate.kind == "end" and candidate.situation not in found:
                found.append(candidate.situation)
        return found

    def plan_latches(self, intents: Sequence[IntentRule]) -> list[IntentRule]:
        """Find the intents that must latch and register which exits their latch guards.

        An intent ``X`` with ``overrides`` ("once hardship applies it comes before the restructuring rule") is not
        enforced by prompt text alone: the model followed the prompt in two runs of three and transferred a customer in
        hardship to the restructuring team in the third. For every overridden intent ``Y`` that has an exit in this stage
        (a transfer or an end) the exit goes through a guard node (:meth:`terminals`) that blocks it once ``X`` was
        expressed at any point of the call. Returns the intents ``X`` that need the latch extraction.
        """
        self._latched = {}
        self.exit_redirect = {}
        present = {intent.key: intent for intent in intents}
        latching: list[IntentRule] = []
        for x in intents:
            guarded = False
            for key in x.overrides:
                y = present.get(key)
                if y is None or y.key == x.key or naming.intent_exit(self.ir, y).kind not in ("end", "handoff"):
                    continue
                self._latched.setdefault(key, []).append(x)
                self.exit_redirect[key] = f"guard_{naming.slug(key)}"
                guarded = True
            if guarded:
                latching.append(x)
        return latching

    def _situation_descriptions(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for intent in self.ir.intents:
            info = naming.intent_exit(self.ir, intent)
            for candidate in (info, info.declined):
                if candidate is not None and candidate.kind == "end":
                    out.setdefault(candidate.situation, intent.description.rstrip("."))
        out.setdefault(self.stop_situation, "will not pay or has no more offers to accept")
        return out

    def guard_nodes(self) -> None:
        """Create the guard nodes in front of the exits an intent with precedence blocks (see :meth:`plan_latches`).

        ``guard_<intent>`` (code) sets ``<intent>_blocked`` from the latch extraction ``<latching intent>_seen`` (and from
        ``call_situation``), then routes: blocked -> ``guard_<intent>_say`` (the engine says a fixed refusal, passthrough)
        -> ``negotiate``; otherwise on to the real exit. The same pattern as ``promise_fix``: variable transitions the
        engine evaluates, never the model.
        """
        refusal = extra_phrase(self.ir.phrases.extra, "override_refusal") or DEFAULT_OVERRIDE_REFUSAL
        for name, (target, key, latching) in sorted(self._guards.items()):
            variable = f"{naming.slug(key)}_blocked"
            say = f"{name}_say"
            self.nodes[name] = code_node(
                name, codegen.guard_snippet(variable, [(f"{naming.slug(x.key)}_seen", self.situations_of(x))
                                                       for x in latching]), target,
                routes=[transition(say, f"The customer already expressed {', '.join(x.key for x in latching)}, "
                                        f"which comes before {key}", or_group(equals(variable, "yes")))])
            self.nodes[say] = tts_node(say, self.freya(refusal), "negotiate",
                                       "Say nothing more now. No tool call is allowed in this turn.")
            for x in latching:
                self.rm(x.rule_ids, f"node:{name}", f"code:{name}", f"node:{say}")

    def terminals(self) -> None:
        """Create the ``end_*`` and ``handoff_*`` nodes the transitions refer to, then decide / close / hangup."""
        ir = self.ir
        shape = self.tools.shape
        for name, situation in sorted(self._ends.items()):
            self.nodes[name] = code_node(name, codegen.set_choice_snippet(situation), "decide")
        customer = self.tools.customer
        for name, queue in sorted(self._handoffs.items()):
            contract = self.api.require("transfer_to_agent")
            field = self.tools.queue
            if field is None:
                raise CompileError("transfer_to_agent has no field that names the team the call goes to "
                                   f"(request fields: {', '.join(s.name for s in contract.request_fields)})")
            queue_enum = contract.enum(field)
            if queue_enum and queue not in queue_enum:
                self.notes.append(f"queue {queue!r} is not in the transfer_to_agent enum {queue_enum}")
            self.nodes[name] = api_request_node(
                name, engine_call(contract, baked={customer.field: customer.ref, field: queue}), "decide")
        self.guard_nodes()
        self.nodes["decide"] = code_node(
            "decide",
            codegen.decide_outcome_snippet(
                ir,
                transfer_nodes=dict(self._handoffs),
                auto_keys=naming.negotiation_situations(ir),
                tool_names={"note": ir.multi_product.note_tool or "add_note"},
                left_variable=(f"verify_identity_response.data.{shape.left}" if shape and shape.left else ""),
                note_fields=((self.tools.note_fields.product or "product_id", self.tools.note_fields.reason or "reason")
                             if self.tools.note_fields else ("product_id", "reason")),
                wrong_variable=(f"verify_identity_response.data.{shape.wrong}" if shape and shape.wrong else ""),
                max_wrong=ir.identity.max_wrong_total,
            ),
            "close",
        )
        close = self.api.require("close_call")
        roles = self.tools.close_fields
        assert roles is not None
        baked = {customer.field: customer.ref, roles.outcome: "{{final_outcome}}"}
        if roles.reason:
            baked[roles.reason] = "{{final_reason}}"
        extra_required = [n for n in close.required_fields if n not in baked]
        if extra_required:
            raise CompileError(
                f"close_call has required fields the compiler cannot fill: {extra_required}; every call would fail to close")
        loose = [s.name for s in close.request_fields if s.name not in baked]
        if loose and roles.reason is None and any(row.reason for row in naming.closing_rows(ir)):
            raise CompileError(
                f"the outcome table has reasons, but the compiler cannot tell which close_call field holds the reason "
                f"(unused fields: {loose}); the reasons would be dropped from every close")
        self.nodes["close"] = api_request_node("close", engine_call(close, baked=baked, max_retries=2), "hangup")
        self.nodes["hangup"] = end_call_node("hangup", "call_closed")
        place = ("code:decide", "node:close")
        self.rm(ir.closing.rule_ids, *place)
        for row in ir.closing.rows:
            self.rm(row.rule_ids, *place)
        for section in (ir.persuasion, ir.identity, ir.right_party, ir.third_party):
            self.rm(section.rule_ids, "code:decide")

    def build(self) -> Workflow:
        ir = self.ir
        if not ir.identity.questions:
            raise CompileError("the policy has no identity question; the agent cannot verify the customer")
        self.tools = build_tools(
            ir, self.api, promise_failed_phrase=f"call {naming.transition_tool(naming.end_node('recording_failed'))}",
            identity_done_phrase=f"call {naming.transition_tool('load_account')}")
        system_prompt = self.system_prompt()
        self.setup()
        self.opening()
        self.third_party()
        self.identity()
        self.account_nodes("negotiate")
        self.negotiate()
        self.terminals()
        self.residual_to_rule_map()
        self.finish()
        return Workflow.from_json(
            {"initial_node": "setup", "system_prompt": system_prompt, "nodes": self.nodes, "global_nodes": {}})


def build_staged_with_notes(
    ir: PolicyIR, tool_api: ToolApi, ledger: RuleLedger, prose: NodeProse
) -> tuple[Workflow, dict[str, list[str]], list[str]]:
    """Like :func:`build_staged`, also returning the compiler's notes (non-fatal findings, for the trace)."""
    builder = _Staged(ir, tool_api, ledger, prose)
    workflow = builder.build()
    return workflow, builder.rule_map, builder.notes


def build_staged(
    ir: PolicyIR, tool_api: ToolApi, ledger: RuleLedger, prose: NodeProse
) -> tuple[Workflow, dict[str, list[str]]]:
    """Compile the staged agent.

    Returns:
        The :class:`~builder.compiler.workflow_model.Workflow` and the ``rule_map`` (rule id -> where it is enforced).

    Raises:
        CompileError: When the tool API lacks a tool the policy needs.
    """
    workflow, rule_map, _notes = build_staged_with_notes(ir, tool_api, ledger, prose)
    return workflow, rule_map
