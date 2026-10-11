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

from builder.compiler import codegen, hooks, naming
from builder.compiler.context import (
    DEFAULT_DECLINE_EXPLANATION,
    DEFAULT_OVERRIDE_REFUSAL,
    ExitStyle,
    StagedExits,
    concessions_forbidden,
    confirm_question,
    deferred_notes,
    exit_bullet,
    extra_phrase,
    failure_note_reason,
    ids_tag,
    language_name,
    ordinal,
    person_refs,
    persona_noun,
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
from builder.compiler.language import is_english, localize_entry, spoken_language
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
from builder.compiler.phrasing import intent_fixed_sentence, speakable, strings_of, unspoken_phrases
from builder.compiler.prompts import render
from builder.compiler.prose import NodeProse, clean_text
from builder.compiler.pycode import assert_safe_code, py_literal
from builder.compiler.roles import note_roles
from builder.compiler.tools import contract_fills, engine_call
from builder.compiler.toolset import ToolSet, build_tools
from builder.compiler.workflow_model import Workflow
from builder.ingest.tool_api import ToolApi
from builder.policy.intents import canonical_intent_key
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
#: A rule about the pre-verification restriction itself ("never say debt before identity is verified").
_ABOUT_DISCLOSURE = re.compile(
    r"\bbefore\s+(?:the\s+)?(?:identity|verification|verifying|the\s+customer\s+is\s+verified|it\s+is\s+verified)"
    r"|\b(?:never|not|don't|cannot|can't)\s+(?:say|mention|tell|disclose|reveal|name)\b", re.IGNORECASE)
#: The value the platform writes into ``transition_to_<node>_response`` when a transition tool was called in the same
#: message as other tool calls and the move did not happen (call records, audit AUD-28).
DROPPED_TRANSITION_MARK = "Transitioning"
RESCUE_STAGES = ("identity", "third_party", "negotiate")
#: Ledger scope -> the node whose task carries a rule of that scope (``global``: the system prompt).
STAGE_NODES = {"global": "system_prompt", "opening": "opening", "third_party": "third_party", "identity": "identity",
               **{scope: "negotiate" for scope in NEGOTIATION_STAGES}}


def rescue_snippet(targets: Sequence[str], ending: Sequence[str]) -> str:
    """Code of ``rescue_<stage>``: find the dropped transition, mark it handled, say whether it ends the call (AUD-28).

    Writes ``rescue_target`` (the first target, in transition order, whose ``transition_to_<target>_response`` holds the
    platform's "Transitioning" mark; ``""`` when none does) and ``rescue_ending`` (``yes`` for an ``end_*`` /
    ``handoff_*`` target). Every marked response is overwritten with ``handled`` so the route fires once per drop.
    """
    ends = "[" + ", ".join(py_literal(t) for t in ending) + "]"
    mark = py_literal(DROPPED_TRANSITION_MARK)
    # one block per target, each naming its variable literally (the linter resolves only literal assignments)
    checks = "".join(
        _RESCUE_CHECK.replace("__KEY__", py_literal(f"{naming.transition_tool(t)}_response"))
        .replace("__TARGET__", py_literal(t)).replace("__MARK__", mark)
        for t in targets
    )
    code = _RESCUE_CODE.replace("__CHECKS__", checks).replace("__ENDS__", ends)
    return assert_safe_code(code, "rescue node")


_RESCUE_CHECK = """    if __MARK__ in str(vars.get(__KEY__) or ''):
        picked = picked or __TARGET__
        vars[__KEY__] = 'handled'
"""
_RESCUE_CODE = """try:
    picked = ''
__CHECKS__    vars['rescue_target'] = picked
    vars['rescue_ending'] = 'yes' if picked in __ENDS__ else 'no'
except Exception as e:
    vars['rescue_target'] = ''
    vars['rescue_ending'] = 'no'
"""
_RESCUE_GO_CODE = assert_safe_code("vars['rescue_state'] = 'ending'\n", "rescue node")


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
        #: the language the agent speaks (ISO code) and its English name; ``foreign``: not English, so the compiler's
        #: English fixed lines are said through the model in that language (see :meth:`localize_speech`)
        self.language = spoken_language(ir)
        self.language_label = language_name(self.language)
        self.foreign = not is_english(self.language)

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
        clock = codegen.clock_variable(self.ir)
        if clock not in self.vars:
            self.notes.append("the policy declares no 'now' call variable; the date code falls back to the real clock")
        elif clock != "now":  # AUD-13: a renamed clock is read by every date rule, never the real clock
            self.notes.append(f"the date code reads the call variable {clock!r} as the current date and time")
        variable = self.tools.customer.variable
        if variable not in self.vars:
            self.notes.append(f"the policy declares no {variable!r} call variable; tool calls bake {{{{{variable}}}}} anyway")
        self.notes.extend(self.unplaced_actions())
        return self.rule_map

    def unplaced_actions(self) -> list[str]:
        """Notes for intent actions no mechanism carries out (AUD-08): the build must not hide a dropped tool call.

        Placed: transfers (handoff nodes / the transfer tool), callbacks (the callback tool on the intent's stages), notes
        (the notes tool, or the engine's note in front of the end node), and the tools the graph always calls.
        """
        note_tool = self.ir.multi_product.note_tool or "add_note"
        always = {naming.TRANSFER_TOOL, naming.CALLBACK_TOOL, "close_call", "verify_identity", "get_account",
                  "create_promise"}
        out: list[str] = []
        for intent in self.ir.intents:
            for action in intent.actions:
                if action.tool in always:
                    continue
                if action.tool == note_tool and naming.note_reason_of(self.ir, intent) and (
                        intent.scope != "before_verification"):
                    continue
                out.append(f"intent {intent.key!r} ({intent.scope}) asks for a {action.tool!r} call that no node or "
                           "tool of the compiled agent makes; check the brief")
        return out

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
        # AUD-11: any other pre-verification intent that ends the call and that the brief scopes to the third-party step
        # ("if the person says the customer moved away, close as ...") is an exit of that step too.
        third_party_rules = {rule.id for rule in self.ledger.active_rules() if "third_party" in rule.scope}
        have = {i.key for i in found}
        for intent in sorted(ir.intents, key=lambda i: -i.precedence):
            if (intent.scope != "after_verification" and intent.key not in have | OPENING_ONLY
                    and third_party_rules & set(intent.rule_ids)
                    and naming.intent_exit(ir, intent).kind == "end"):
                found.append(intent)
                have.add(intent.key)
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
            if info.kind == "note" and stage != "negotiate":
                info = naming.IntentExit("info")  # no product is known before verification: nothing to note yet
            if info.kind == "info":
                response = intent_fixed_sentence(self.ir, canonical_intent_key(intent.key), intent.response)
                if intent.key not in TEMPLATE_COVERED and (response or intent.description):
                    label = intent.description.rstrip(".")
                    text = f"{label}: {response}".strip(": ").strip()
                    out.append(f"{text} {ids_tag(intent.rule_ids)}".strip())
                continue
            bullet = exit_bullet(self.ir, intent, style, note_tool=self.ir.multi_product.note_tool,
                                 redirect=self.exit_redirect if stage == "negotiate" else None,
                                 concessions_forbidden=concessions_forbidden(self.ir, self.ledger))
            if bullet is None:
                continue
            out.append(bullet)
        return out

    # ---- prompt texts (shared with the lean profile) ----------------------------------------------------------------

    def deferred_global_rules(self) -> list[tuple[str, str]]:
        """Global rules that name a word forbidden before verification: they go to the negotiation task (AUD-51).

        The system prompt is in front of the model from the first word of the call, also when a relative answers; a rule
        such as "find the customer ... obtain a payment promise" there tells the model why it calls. A rule that is
        itself about the restriction ("never say X before identity is verified") stays in the system prompt.
        """
        leak = self._leak_pattern()
        if leak is None:
            return []
        return [(rule_id, text) for rule_id, text in uncovered_rules(self.ir, self.ledger, ["global"])
                if leak.search(text) and not _ABOUT_DISCLOSURE.search(text)]

    def system_prompt(self) -> str:
        """The global system prompt."""
        ir = self.ir
        deferred = {rule_id for rule_id, _ in self.deferred_global_rules()}
        rules = [rule for rule in uncovered_rules(ir, self.ledger, ["global"]) if rule[0] not in deferred]
        self.rm([r[0] for r in rules], "system_prompt")
        self.rm(ir.rule_ids, "system_prompt")
        for intent in ir.intents:
            if intent.key in ("robot_question", "scam_question", "unknown_question"):
                self.rm(intent.rule_ids, "system_prompt")
        return render(
            "system_prompt",
            bank_name=ir.bank_name,
            persona=persona_noun(ir),
            language=self.language_label,
            foreign=self.foreign,
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
            bank = self.ir.bank_name.strip() or "your bank"
            persona = persona_noun(self.ir)
            sentence = f"Hello, this is {bank}'s {persona}." if persona else f"Hello, this is {bank}."
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
            confirm=quoted(rp.confirm_examples) or quoted(["yes", "that's me", "speaking"]),
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
                         node: str = "third_party", extra: str | None = None, handover_action: str = "") -> str:
        """Task message for a person who is not the customer.

        ``handover_action``: what to do when the customer themself comes to the phone (AUD-45); empty = no such bullet.
        """
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
            handover_action=handover_action,
            question=self.right_party_question(),
            confirm=quoted(ir.right_party.confirm_examples) or quoted(["yes", "that's me", "speaking"]),
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

    def _spoken_text(self) -> str:
        """Why an answer said in words is not "partial": models withheld a complete date whose year was spoken as words."""
        if not any(q.answer_format in ("date_iso", "year") for q in self.ir.identity.questions):
            return ""
        return ("a date or a year said in words is complete when every part is there: a year said in words, such as "
                '"nineteen eighty", is a whole year, not part of one')

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
            decline_max=max(1, ident.decline_max),
            decline_nth=ordinal(max(1, ident.decline_max)),
            complete=self._complete_text(),
            partial=self._partial_text(),
            spoken=self._spoken_text(),
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

    def _product_order_text(self) -> str:
        """The brief's own order rule for several products, verbatim (``multi_product.order == "custom"``, AUD-34).

        The IR only records that the order is custom; the wording is the multi-product rule that speaks about the order
        (or every multi-product rule when none does), so a rule the switch cites is not lost from the prompt.
        """
        mp = self.ir.multi_product
        if not mp.enabled or mp.order != "custom":
            return ""
        quotes = [(rule.id, " ".join(rule.quote.split())) for rule_id in mp.rule_ids
                  if (rule := self.ledger.get(rule_id)) is not None]
        about = [q for q in quotes if re.search(r"\b(order|first|start)\b", q[1], re.IGNORECASE)]
        picked = about or quotes
        if not picked:
            self.notes.append("multi_product.order is 'custom' but no multi-product rule gives the order; the products "
                              "are taken in the order get_account returns them")
            return ""
        return "; ".join(f'"{text}" [{rule_id}]' for rule_id, text in picked[:3])

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
        have = {rule[0] for rule in rules}
        rules += [rule for rule in self.deferred_global_rules() if rule[0] not in have]  # AUD-51
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
            per_product_attempts=mp.per_product_attempts,
            order_text=self._product_order_text(),
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
        self.rm(ir.recording.rule_ids, *place, "tool:create_promise/pre_process_code",
                "tool:create_promise/post_process_code")
        self.rm(ir.disclosure.rule_ids, f"node:{node}/on_enter", "code:account_facts")
        self.rm(mp.rule_ids, *place, *(["tool:add_note/pre_process_code"] if self.tools.note is not None else []))
        self.rm([r[0] for r in rules], f"node:{node}/task_message")
        return text

    def residual_to_rule_map(self) -> None:
        """Map residual rules (no structured home) to the prompt that carries their text."""
        deferred = {rule_id for rule_id, _ in self.deferred_global_rules()}
        for rule in self.ir.residual_rules:
            places = []
            for scope in rule.scope:
                if scope == "global":
                    places.append("node:negotiate/task_message" if rule.rule_id in deferred else "system_prompt")
                elif scope in NEGOTIATION_STAGES:
                    places.append("node:negotiate/task_message")
                else:
                    places.append(f"node:{scope}/task_message")
            self.rm([rule.rule_id], *places)

    # ---- last pass over the finished texts ----------------------------------------------------------------------------

    def finalize_texts(self, system_prompt: str, stage_node: dict[str, str]) -> str:
        """Place missing prescribed sentences, then make every text speakable; returns the system prompt.

        ``stage_node`` maps a ledger scope to the node whose task carries it (``global`` -> ``system_prompt``; a scope
        that is not listed goes to the ``negotiation`` entry).
        """
        system_prompt = self.place_unspoken(system_prompt, stage_node)
        system_prompt = self.make_speakable(system_prompt)
        self.localize_speech()
        return system_prompt

    def localize_speech(self) -> None:
        """In a call that is not in English, every fixed ``tts`` line is said through the model in the call's language.

        Several fixed lines are built from the compiler's English templates (the debt statement, guard corrections,
        confirmations, rescue goodbyes); spoken verbatim they would be heard in English. Each ``tts`` entry becomes an
        ``llm`` entry whose prompt asks for the line in the call's language with the same meaning, numbers, amounts,
        dates and names, and the line itself (its ``{{vars}}`` intact); a line already in that language (the brief's own
        sentence) is said word for word. An English call keeps its ``tts`` lines unchanged.
        """
        if not self.foreign:
            return
        changed = [name for name, node in self.nodes.items() if localize_entry(node, self.language)]
        if changed:
            self.notes.append(f"the agent speaks {self.language_label}: {len(changed)} fixed line(s) are said through "
                              f"the model in {self.language_label} ({', '.join(changed)})")

    def place_unspoken(self, system_prompt: str, stage_node: dict[str, str]) -> str:
        """Add every prescribed sentence the workflow does not contain to the step its rule is scoped to (AUD-09).

        A sentence for every step goes to the system prompt, unless it names a word that is forbidden before
        verification (then to the negotiation step, like :meth:`deferred_global_rules`).
        """
        found = unspoken_phrases(self.ir, self.ledger, [system_prompt, *strings_of(self.nodes)])
        if not found:
            return system_prompt
        leak = self._leak_pattern()
        lines_for: dict[str, list[str]] = {}
        for item in found:
            text = f"[{item.rule_id}] {item.line}" if item.rule_id else item.line
            targets: list[str] = []
            for scope in item.scopes:
                target = stage_node.get(scope, stage_node["negotiation"])
                if target == "system_prompt" and leak is not None and leak.search(item.line):
                    target = stage_node["negotiation"]
                if target not in targets and (target == "system_prompt" or target in self.nodes):
                    targets.append(target)
            for target in targets:
                if text not in lines_for.setdefault(target, []):
                    lines_for[target].append(text)
                if item.rule_id:
                    self.rm([item.rule_id], "system_prompt" if target == "system_prompt" else f"node:{target}/task_message")
            self.notes.append(f"{item.source} is said by no node of the workflow; it is placed word for word in "
                              f"{', '.join(targets)}")
        for target, lines in lines_for.items():
            block = ("\n\nFixed sentences of the brief (say them word for word when they apply):\n"
                     + "\n".join(f"- {line}" for line in lines))
            if target == "system_prompt":
                system_prompt += block
            else:
                self.nodes[target]["task_message"] = str(self.nodes[target].get("task_message") or "") + block
        return system_prompt

    def make_speakable(self, system_prompt: str) -> str:
        """Call-variable placeholders become ``{{name}}`` everywhere; spoken lines lose any other ``{token}`` (AUD-12)."""
        for name, node in self.nodes.items():
            if isinstance(node.get("task_message"), str):
                node["task_message"], _ = speakable(node["task_message"], self.vars, spoken=False)
            enter = node.get("on_enter")
            if isinstance(enter, dict) and enter.get("type") in ("tts", "llm"):
                key = "text" if enter["type"] == "tts" else "prompt"
                if isinstance(enter.get(key), str):
                    enter[key], removed = speakable(enter[key], self.vars, spoken=enter["type"] == "tts")
                    if removed:
                        self.notes.append(f"the spoken line of {name!r} named {', '.join(sorted(set(removed)))}, which "
                                          "no call variable fills; it is left out so the caller never hears braces")
        text, _ = speakable(system_prompt, self.vars, spoken=False)
        return text

    def _leak_pattern(self) -> re.Pattern[str] | None:
        """Words the brief forbids before verification (descriptions such as "the product" are skipped)."""
        terms = [t.strip() for t in self.ir.forbidden_before_verification
                 if t.strip() and not re.match(r"(the|a|an|any)\s", t.strip(), re.IGNORECASE)]
        if not terms:
            return None
        return re.compile(r"\b(" + "|".join(re.escape(t) for t in terms) + r")\b", re.IGNORECASE)

    def account_nodes(self, next_node: str = "negotiate", unavailable: str | None = None) -> None:
        """``load_account`` (engine call) and ``account_facts`` (code) nodes, shared by both profiles.

        ``unavailable`` (AUD-24): the node a call goes to when the account could not be loaded (``account_state`` is
        ``empty`` or ``error: ...``: the request failed after its retries, or no product could be read). The call then
        says so in a fixed line (``account_unavailable``) and closes there, instead of negotiating without any facts.
        """
        ir = self.ir
        contract = self.api.require("get_account")
        customer, account = self.tools.customer, self.tools.account
        self.nodes["load_account"] = api_request_node(
            "load_account",
            engine_call(contract, baked={customer.field: customer.ref}, max_retries=2),
            "account_facts",
            [(f"data.{account.list_field}", "products")],
        )
        routes: list[dict[str, Any]] = []
        if unavailable is not None:
            goodbye = self.freya(ir.phrases.goodbye_sentence).strip()
            line = ("I am sorry, I cannot open your account details right now, so I cannot go through the payment in "
                    "this call. " + goodbye).strip()
            self.nodes["account_unavailable"] = tts_node(
                "account_unavailable", line, unavailable,
                "The account could not be loaded. Say nothing more. No tool call is allowed in this turn.")
            routes.append(transition("account_unavailable", "The account could not be loaded",
                                     or_group(equals("account_state", "empty"), contains("account_state", "error"))))
        self.nodes["account_facts"] = code_node(
            "account_facts",
            codegen.account_facts_snippet(ir, account, speak_in=self.language_label if self.foreign else ""),
            next_node, routes=routes)
        self.notes.extend(codegen.persuasion_ladder_gaps(ir))
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
        #: ``note_<situation>`` node -> (the ``end_*`` node it leads to, the note reason) (single-product notes, AUD-08)
        self._note_ends: dict[str, tuple[str, str]] = {}

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

    def note_end(self, situation: str, reason: str) -> str:
        """The exit target of an ``end`` exit that writes a note first (single-product brief, AUD-08).

        A brief that says "add a note with reason X and end the call" gets an engine-called notes request in front of
        ``end_<situation>`` (``note_<situation>``, created by :meth:`terminals`): the note cannot be skipped and there
        is exactly one. With one product, ``{{product_ids}}`` is that product. When the tool API has no usable notes tool
        the note is dropped with a compile note and the exit goes straight to the end node.
        """
        target = self.end(situation)
        tool = self.ir.multi_product.note_tool or "add_note"
        contract = self.api.tools.get(tool)
        roles = note_roles(contract, self.tools.customer.field, self.tools.account.id_key) if contract else None
        if roles is None or roles.product is None or roles.reason is None:
            self.notes.append(f"an intent writes a note with reason {reason!r}, but the tool API has no usable {tool!r} "
                              "tool (product id and reason fields); the note is not written")
            return target
        enum = contract.enum(roles.reason) if contract else []
        if enum and reason not in enum:
            self.notes.append(f"note reason {reason!r} is not in the {tool} enum {enum}")
        name = f"note_{naming.slug(situation)}"
        self._note_ends[name] = (target, reason)
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
                if info.reason and stage == "negotiate" and not self.ir.multi_product.enabled:
                    target = self.note_end(info.situation, info.reason)
                    self.rm(intent.rule_ids, f"node:{target}")
                said = "no callback time could be agreed; you said goodbye." if info.callback else "you said goodbye."
                add(self.guard(intent, target) if guarded else target, f"{base}; {said}")
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

    def callback_routes(self, stage: str, intents: Sequence[IntentRule]) -> list[dict[str, Any]]:
        """The route on a booked callback for the intents of ``stage`` that book one (AUD-08); empty when none does.

        The callback tool goes on the stage's node (see :meth:`stage_tools`); once the engine counted a successful
        booking the route leads to ``callback_ok_<stage>`` (fixed confirmation and goodbye) and on to the end node of the
        first such intent, like ``callback_done`` of the third-party step. The prompt bullet's tool exit is the branch
        where no time could be agreed.
        """
        booking = [i for i in intents if naming.intent_exit(self.ir, i).callback]
        if not booking or self.tools.callback is None:
            return []
        info = naming.intent_exit(self.ir, booking[0])
        if len({naming.intent_exit(self.ir, i).situation for i in booking}) > 1:
            self.notes.append(f"several intents of the {stage} step book a callback; a booking there ends the call as "
                              f"{info.situation!r}")
        say = f"callback_ok_{stage}"
        sentence = ("The callback is booked. " + self.freya(self.ir.phrases.goodbye_sentence)).strip()
        self.nodes[say] = tts_node(say, sentence, self.end(info.situation),
                                   "The callback is booked. Say nothing more. No tool call is allowed in this turn.")
        for intent in booking:
            self.rm(intent.rule_ids, f"node:{say}", f"node:{stage}/transitions", "tool:schedule_callback", "code:decide")
        return [*self.callback_fix_routes(stage, None),
                transition(say, "A callback was booked",
                           or_group(equals(hooks.acked_flag(hooks.CALLBACK_ACKS), "yes")))]

    def callback_fix_routes(self, stage: str, give_up: str | None) -> list[dict[str, Any]]:
        """The guard recovery of ``schedule_callback`` on ``stage`` (AUD-29), the twin of ``promise_fix``.

        The pre-hook blocks a slot outside the allowed callback times (nothing reaches the bank; the post-hook does not run
        on that path). The route leads to ``callback_fix_<stage>`` (code), which marks the block handled, and on to
        ``callback_fix_say_<stage>``, which speaks the allowed times and returns to ``stage``. With ``give_up`` (the
        policy asks again only so often, then books nothing) a block past that number goes to ``callback_stop_<stage>``
        (a fixed apology) and on to ``give_up``. Empty when the callback tool has no guard (a custom callback rule).
        """
        tool = self.tools.callback
        if tool is None or "callback_guard" not in str(tool.get("pre_process_code") or ""):
            return []
        fix, say = f"callback_fix_{stage}", f"callback_fix_say_{stage}"
        routes: list[dict[str, Any]] = []
        if give_up is not None:
            stop = f"callback_stop_{stage}"
            line = ("I am sorry, I cannot book a callback at that time. "
                    + self.freya(self.ir.phrases.goodbye_sentence)).strip()
            self.nodes[stop] = tts_node(stop, line, give_up,
                                        "No callback was booked. Say nothing more. No tool call is allowed in this turn.")
            routes.append(transition(stop, "The allowed times were offered and still no allowed time was given",
                                     or_group(equals("callback_guard_stop", "yes"))))
        self.nodes[fix] = code_node(fix, codegen.callback_fix_snippet(), say, routes=routes)
        self.nodes[say] = tts_node(say, "{{callback_fix_text}}", stage,
                                   "Say nothing more now. No tool call is allowed in this turn.")
        callback = self.ir.third_party.callback
        self.rm(callback.rule_ids if callback else [], "tool:schedule_callback/pre_process_code", f"node:{say}")
        return [transition(fix, "The callback guard blocked a schedule_callback call",
                           or_group(contains("callback_guard", "blocked")))]

    def callback_situations(self) -> list[str]:
        """Situations of intents that book a callback and close on a row of their own (``decide`` input, AUD-08)."""
        keys: list[str] = []
        for intent in self.ir.intents:
            info = naming.intent_exit(self.ir, intent)
            if info.callback and info.situation not in ("callback_booked", "callback_not_booked", *keys):
                keys.append(info.situation)
        return keys

    def rescue_routes(self) -> None:
        """Recover a transition the platform dropped because it was sent together with other tool calls (AUD-28).

        A transition tool called in the same message as a bank tool (``add_note`` x2 + the finishing transition, or
        ``verify_identity`` + a move) is not routed: the platform only writes "Transitioning to <node>" into
        ``transition_to_<node>_response``. Each conversation node with bank tools therefore gets one variable route on
        those responses (after its deterministic routes, before the goodbye-loop breaker) to ``rescue_<stage>`` (code):
        it marks the response handled and routes to the dropped target. A target that ends the call goes through
        ``rescue_<stage>_bye`` (the fixed goodbye; the model may not have said it) and ``rescue_<stage>_go``. Every
        target keeps its own transition from the rescue node, so the engine never merges two of them.
        """
        goodbye = self.freya(self.ir.phrases.goodbye_sentence).strip() or "Thank you for your time. Goodbye."
        for stage in RESCUE_STAGES:
            node = self.nodes.get(stage)
            if node is None or not node.get("tools"):
                continue
            targets = list(dict.fromkeys(t["target_node"] for t in node["transitions"] if t.get("is_tool_call")))
            if not targets:
                continue
            ending = [t for t in targets if t.startswith(("end_", "handoff_", "note_"))]
            name, bye, go = f"rescue_{stage}", f"rescue_{stage}_bye", f"rescue_{stage}_go"

            def route(target: str) -> dict[str, Any]:
                return transition(target, f"Recover the dropped move to {target}",
                                  or_group(equals("rescue_target", target)))

            routes = [route(t) for t in targets if t not in ending]
            if ending:
                routes.append(transition(bye, "The dropped move ends the call", or_group(equals("rescue_ending", "yes"))))
                self.nodes[bye] = tts_node(bye, goodbye, go,
                                           "You said goodbye. Say nothing more. No tool call is allowed in this turn.")
                self.nodes[go] = code_node(go, _RESCUE_GO_CODE, stage, routes=[route(t) for t in ending])
            self.nodes[name] = code_node(name, rescue_snippet(targets, ending), stage, routes=routes)
            condition = or_group(*(contains(f"{naming.transition_tool(t)}_response", DROPPED_TRANSITION_MARK)
                                   for t in targets))
            transitions = node["transitions"]
            at = 0
            while at < len(transitions) and not transitions[at].get("is_tool_call") and (
                    (transitions[at].get("condition") or {}).get("operator") != "matches_intent"):
                at += 1
            transitions.insert(at, transition(name, "A move was sent together with other tool calls and did not "
                                                    "happen", condition))

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
        callback_routes = self.callback_routes("opening", intents)
        self.nodes["opening"] = {
            "name": "opening",
            "node_type": "conversation",
            "on_enter": {"type": "tts", "text": self.opening_line()},
            "on_enter_first_entry_only": True,
            "task_message": task,
            **({"tools": [self.tools.callback]} if callback_routes else {}),
            "max_node_turns": 30,
            "max_turn_transition": unclear_node,
            "transitions": [
                *callback_routes,
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
            handover_action=f"call {naming.transition_tool('identity')} (no text needed)",
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
            transitions.extend(self.callback_fix_routes("third_party", not_booked))
            transitions.append(transition("callback_done", "The callback was booked",
                                          or_group(equals(hooks.acked_flag(hooks.CALLBACK_ACKS), "yes"))))
        transitions.append(intent_transition(loop_end, "The conversation is over", GOODBYE_LOOP))
        if callback is not None:
            transitions.append(tool_transition(booked, "The callback is booked; you said goodbye."))
        transitions.append(tool_transition(
            not_booked, "No callback inside the allowed times, or nothing to book; you said goodbye."))
        # AUD-45: the person fetched the customer, who came to the phone and confirmed who they are
        transitions.append(tool_transition(
            "identity", "The customer themself came to the phone and explicitly confirmed it is them (not a relative "
                        "speaking for them)."))
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
        elif self.tools.identity_tracked:  # AUD-23: the post-hook counted every question verified
            transitions.append(transition("load_account", "All security answers verified",
                                          or_group(equals("identity_all_verified", "yes"))))
        else:
            transitions.append(tool_transition("load_account", "Every security answer was verified; nothing more to say."))
        if counter:
            # a counter that counts down ends at 0; one that counts up ends at the policy limit, or past it (AUD-50: a
            # count-up counter may skip the exact limit when two answers are checked in one message)
            failed = (equals(f"verify_identity_response.data.{counter}", "0") if shape.left else
                      counter_above(f"verify_identity_response.data.{counter}", ident.max_wrong_total - 1))
            transitions.append(transition("identity_failed", "No wrong answers left", or_group(failed)))
        transitions.append(tool_transition(declined_node, "The customer declined the security questions; you said goodbye."))
        transitions.append(tool_transition("third_party", "The person says they are not the customer "
                                                          "(for example a relative or a colleague)."))
        transitions.extend(self.exit_transitions("identity", [*intents, *wrong_number]))
        if not counter:
            transitions.append(tool_transition(self.end("identity_failed"), "Too many wrong answers; you said goodbye."))
        callback_routes = self.callback_routes("identity", intents)
        at = max((k + 1 for k, t in enumerate(transitions[:2]) if not t.get("is_tool_call")), default=0)
        transitions[at:at] = callback_routes  # after the verified / failed routes, before the tool-call exits
        ask = self.freya(ident.questions[0].ask)
        self.nodes["identity"] = {
            "name": "identity",
            "node_type": "conversation",
            "on_enter": {"type": "llm",
                         "prompt": f'Ask exactly: "{ask}" Say nothing about the account or why you are calling.'},
            "on_enter_first_entry_only": True,
            "task_message": task,
            "tools": [self.tools.verify, *([self.tools.callback] if callback_routes else [])],
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
                       or_group(and_group(equals(hooks.acked_flag(hooks.PROMISE_ACKS), "yes"),
                                          equals("product_count", "1")))),
            *self.callback_routes("negotiate", intents),
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
            "tools": [self.tools.promise, *([self.tools.note] if self.tools.note else []),
                      *([self.tools.callback] if "callback_ok_negotiate" in self.nodes else [])],
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
        # the descriptions serve the negotiation extraction: an intent in force after verification describes it best
        for intent in sorted(self.ir.intents, key=lambda i: i.scope == "before_verification"):
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
            guarded = self.ir.intent(key, verified=True) or self.ir.intent(key)
            if guarded is not None:
                self.rm(guarded.rule_ids, f"node:{name}", f"code:{name}")

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
                name, engine_call(contract, baked={customer.field: customer.ref, field: queue}, max_retries=1),
                "decide")
        for name, (target, reason) in sorted(self._note_ends.items()):
            tool = self.ir.multi_product.note_tool or "add_note"
            contract = self.api.require(tool)
            roles = note_roles(contract, customer.field, self.tools.account.id_key)
            assert roles.product is not None and roles.reason is not None  # checked by :meth:`note_end`
            self.nodes[name] = api_request_node(
                name, engine_call(contract, baked={customer.field: customer.ref, roles.product: "{{product_ids}}",
                                                   roles.reason: reason}), target)
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
                callback_situations=self.callback_situations(),
            ),
            "close",
        )
        close = self.api.require("close_call")
        roles = self.tools.close_fields
        assert roles is not None
        baked = {customer.field: customer.ref, roles.outcome: "{{final_outcome}}"}
        if roles.reason:
            baked[roles.reason] = "{{final_reason}}"
        filled = contract_fills(close, baked, guess_enum=True)  # engine_call sends what the contract settles
        extra_required = [n for n in close.required_fields if n not in baked and n not in filled]
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
        self.notes.extend(self.tools.notes)
        system_prompt = self.system_prompt()
        self.setup()
        self.opening()
        self.third_party()
        self.identity()
        self.account_nodes("negotiate", unavailable=self.end("recording_failed"))
        self.negotiate()
        self.rescue_routes()
        self.terminals()
        self.residual_to_rule_map()
        system_prompt = self.finalize_texts(system_prompt, STAGE_NODES)
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
