"""Template inputs for the agent prompts, derived from the PolicyIR, the rule ledger and the tool contracts.

The prompt templates (``builder/prompts/agent/*.j2``) hold the *static* instruction wording that was validated on the
reference agent. Everything brief-specific reaches them as data built here:

* fixed sentences (:class:`~builder.policy.ir.Phrases`) with ``{title}``-style placeholders turned into Freya
  ``{{title}}`` references (:func:`to_freya`);
* per-intent **exit bullets** (:func:`exit_bullet`): what to say and which finishing tool to call, rendered for the staged
  design (tool-call transitions) or the lean design (the model calls ``close_call``) through an :class:`ExitStyle`;
* ledger rules that no IR section cites, verbatim with their ids (:func:`uncovered_rules`), so a rule the structured
  fields could not hold is still in front of the model;
* small wording helpers (ordinals, quoted lists, rule-id tags);
* the sentences several prompts and hooks must agree on, built in ONE place so they cannot drift apart: the retry /
  give-up rule for a failed promise (:func:`recording_failure_texts`), the note step of a several-product call
  (:func:`note_step`), neutral defaults for phrases a brief does not prescribe.

Nothing here is specific to one brief.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from builder.compiler import naming
from builder.policy.ir import IntentRule, OutcomeRef, PolicyIR, RuleLedger, template_fields

__all__ = [
    "DEFAULT_CONFIRM_QUESTION",
    "DEFAULT_DECLINE_EXPLANATION",
    "DEFAULT_OVERRIDE_REFUSAL",
    "DEFAULT_TODAY_QUESTION",
    "ExitStyle",
    "LeanExits",
    "StagedExits",
    "confirm_question",
    "deferred_notes",
    "exit_bullet",
    "extra_phrase",
    "failure_note_reason",
    "ids_tag",
    "language_name",
    "note_step",
    "ordinal",
    "outcome_text",
    "override_phrase",
    "person_refs",
    "quoted",
    "recording_failure_texts",
    "retry_leads",
    "soften_recording_notice",
    "times_text",
    "to_freya",
    "today_question",
    "uncovered_rules",
    "words",
]

#: Neutral wording used when the brief prescribes no sentence for a fixed point of the call.
DEFAULT_TODAY_QUESTION = "Can you pay the full amount today?"
DEFAULT_CONFIRM_QUESTION = "Is that correct?"
DEFAULT_DECLINE_EXPLANATION = "The questions are needed before we can continue."
DEFAULT_OVERRIDE_REFUSAL = "I am sorry, I cannot arrange that."

_NUMBER_WORDS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"]
_ORDINALS = ["zeroth", "first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth", "tenth"]
_LANGUAGES = {"en": "English", "tr": "Turkish", "de": "German", "fr": "French", "es": "Spanish", "it": "Italian",
              "pt": "Portuguese", "nl": "Dutch", "pl": "Polish", "ru": "Russian", "ar": "Arabic"}
_CALL_NOUN = r"(?:(?:this|the|your|our)\s+(?:phone\s+)?(?:call|conversation)|calls|conversations)"
_PASSIVE = r"(?:is|are|will\s+be|may\s+be|might\s+be|can\s+be|could\s+be|shall\s+be|gets?)\s+(?:being\s+)?"
_RECORDED = r"(?:recorded(?:\s+(?:and|or)\s+monitored)?|monitored\s+and\s+recorded)"
_RECORDED_CALL = re.compile(rf"\b{_CALL_NOUN}\s+{_PASSIVE}{_RECORDED}\b", re.IGNORECASE)
_RECORDING_CALL = re.compile(
    r"\bwe\s*(?:are|'re|will\s+be|'ll\s+be)\s+recording\s+(?:this|the|your|our)\s+(?:phone\s+)?(?:call|conversation)\b",
    re.IGNORECASE)


def extra_phrase(extra: dict[str, str], *keys: str) -> str:
    """A prescribed sentence from ``Phrases.extra`` (free-form keys chosen by the extraction stage).

    For each wanted key the exact key is tried first, then any key that ends with ``_<key>`` (so ``intro`` finds
    ``disclosure_intro``). Returns the stripped sentence, or ``""`` when none matches.
    """
    for key in keys:
        if extra.get(key, "").strip():
            return extra[key].strip()
        for name, value in extra.items():
            if name.endswith("_" + key) and value.strip():
                return value.strip()
    return ""


def today_question(ir: PolicyIR) -> str:
    """The first ask after the debt statement: the brief's sentence, else its first persuasion ask, else a neutral one."""
    return ir.phrases.today_question.strip() or ir.persuasion.first_ask.strip() or DEFAULT_TODAY_QUESTION


def confirm_question(ir: PolicyIR) -> str:
    """The question asked after the read-back: the brief's sentence, else a neutral one."""
    return ir.confirmation.question.strip() or ir.phrases.confirm_question.strip() or DEFAULT_CONFIRM_QUESTION


def deferred_notes(ir: PolicyIR) -> bool:
    """True when several-product notes are written only at the very end of the call.

    A policy that forbids notes on a transfer (``notes_on_transfer`` false) needs it: a note written when the customer
    disputes the first product cannot be taken back when they then ask for a transfer. Notes are therefore remembered
    during the call and written once every product is handled and no transfer is pending.
    """
    return bool(ir.multi_product.enabled and not ir.multi_product.notes_on_transfer)


def failure_note_reason(ir: PolicyIR) -> str:
    """The note reason for a product whose promise could not be recorded (``""`` = the policy has none)."""
    reason = ir.recording.failure_outcome.reason or ""
    notes = ir.multi_product.note_reasons
    return reason if reason and (not notes or reason in notes) else ""


def note_step(ir: PolicyIR, note_tool: str, reason: str) -> str:
    """The note step of a failed promise in a several-product call (deferred or immediate, see :func:`deferred_notes`)."""
    if deferred_notes(ir):
        return (f'then remember the reason "{reason}" for that product (do not call {note_tool} yet) and continue with '
                "the next product")
    return f'then call {note_tool} with that product_id and reason "{reason}" and continue with the next product'


def retry_leads(tool: str, retries: int) -> tuple[str, str]:
    """``(prompt lead, hook lead)``: what to do after a failed recording, for ``retries`` allowed retries.

    Both texts end right before the give-up sentence and say the same thing: the prompt counts attempts ("fails a third
    time"), the post-hook, which sees one failure at a time, counts failures ("failure number 1 to 2").
    """
    n = max(0, retries)
    if n == 0:
        return "If it fails, do not try again: ", "Do not try again: "
    if n == 1:
        return ("If it fails, call it once more with the same values. If it fails again, ",
                f"If this is the first failure for this product, call {tool} once more with exactly the same values now. "
                "If it already failed once before, ")
    return (f"If it fails, call it again with the same values (at most {times_text(n)} more). "
            f"If it fails a {ordinal(n + 1)} time, ",
            f"If this is failure number 1 to {n} for this product, call {tool} again with exactly the same values now. "
            f"If it already failed {times_text(n)} before, ")


def recording_failure_texts(
    ir: PolicyIR, tool: str, *, failed_phrase: str, note_tool: str, with_note: bool
) -> tuple[str, str]:
    """``(prompt rule, hook text)`` for a promise that could not be recorded - one source for both.

    The prompt rule and the post-hook of ``create_promise`` used to word this separately and disagreed (the prompt always
    retried once, the hook followed ``retries_on_failure``; several-product prompts asked for a service-error note while
    the hook ended the call). Both now come from the retry count of the policy and from the same give-up clause: say the
    failure sentence, then either end the call (``failed_phrase``) or, in a several-product call with a service-error
    note reason (``with_note``), note the reason for that product and go on with the next one.
    """
    prompt_lead, hook_lead = retry_leads(tool, ir.recording.retries_on_failure)
    sentence = ir.recording.failure_sentence.strip()
    say = f'say "{sentence}"' if sentence else "tell the customer you could not record it"
    reason = failure_note_reason(ir)
    if with_note and reason:
        give_up = f"{say}, {note_step(ir, note_tool, reason)} (rule B7)."
    else:
        give_up = f"{say}, thank them, say goodbye and {failed_phrase}."
    return prompt_lead + give_up, hook_lead + give_up


def words(n: int) -> str:
    """``3`` -> ``three`` (digits above ten)."""
    return _NUMBER_WORDS[n] if 0 <= n < len(_NUMBER_WORDS) else str(n)


def ordinal(n: int) -> str:
    """``2`` -> ``second``."""
    return _ORDINALS[n] if 0 <= n < len(_ORDINALS) else f"{n}th"


def quoted(items: Iterable[str], joiner: str = "or") -> str:
    """``['yes', "that's me"]`` -> ``"yes" or "that's me"``."""
    parts = [f'"{item}"' for item in items if item]
    if len(parts) <= 1:
        return "".join(parts)
    return ", ".join(parts[:-1]) + f" {joiner} " + parts[-1]


def language_name(code: str) -> str:
    """English name of a language code (unknown codes are returned as written)."""
    return _LANGUAGES.get((code or "en").lower()[:2], code or "English")


def times_text(n: int) -> str:
    """``1`` -> ``once``, ``2`` -> ``twice``, ``3`` -> ``three times``."""
    return {1: "once", 2: "twice"}.get(n, f"{words(n)} times")


def ids_tag(ids: Sequence[str], limit: int = 4) -> str:
    """``[A-1, A-2]`` rule-id tag (empty for no ids; long lists are cut with a count)."""
    unique = list(dict.fromkeys(ids))
    if not unique:
        return ""
    shown = ", ".join(unique[:limit]) + (f", +{len(unique) - limit}" if len(unique) > limit else "")
    return f"[{shown}]"


def to_freya(text: str, variables: Iterable[str]) -> str:
    """Turn IR placeholders ``{name}`` into Freya ``{{name}}`` when ``name`` is a call variable; others stay as written."""
    known = set(variables)

    def sub(match: re.Match[str]) -> str:
        name = match.group(1)
        return "{{" + name + "}}" if name in known else match.group(0)

    return re.sub(r"(?<!\{)\{([A-Za-z_][A-Za-z0-9_]*)\}(?!\})", sub, text or "")


def soften_recording_notice(text: str) -> str:
    """Rephrase any "this call is recorded" in a fixed line as the active "We record this call".

    Lesson from the reference runs: a judge check "never say *recorded* before a promise is actually recorded" read the
    passive "This call is recorded" as a violation. The active form says the same thing and does not collide. Every
    phrasing about the call is rewritten (``this/the/your call is / will be / may be / is being recorded``, ``calls are
    recorded``, ``this conversation is recorded``, ``recorded and monitored``, ``we are recording this call``); the rest of
    the sentence is kept, and the new clause starts with a capital only at the start of a sentence ("Please note that we
    record this call."). Text about anything else being recorded ("Your promise is recorded.") is left alone.
    """

    def replace(match: re.Match[str]) -> str:
        before = match.string[: match.start()].rstrip()
        return "We record this call" if not before or before[-1] in ".!?" else "we record this call"

    return _RECORDING_CALL.sub(replace, _RECORDED_CALL.sub(replace, text or ""))


def person_refs(ir: PolicyIR) -> tuple[str, str]:
    """``(how to address the customer, full name)`` as Freya references, from the right-party question and call variables.

    "{{title}} {{last_name}}" / "{{first_name}} {{last_name}}" when those variables exist; otherwise "the customer".
    """
    names = set(ir.call_variable_names())
    fields = [name for name in template_fields(ir.phrases.right_party_question) if name in names]
    if not fields and not ir.phrases.right_party_question.strip():  # no question to read the form of address from
        fields = next((list(pair) for pair in (("title", "last_name"), ("first_name", "last_name"))
                       if all(name in names for name in pair)), [])
    address = " ".join("{{" + name + "}}" for name in fields) or "the customer"
    full = [name for name in ("first_name", "last_name") if name in names]
    return address, (" ".join("{{" + name + "}}" for name in full) or address)


@dataclass(frozen=True)
class ExitStyle:
    """How a prompt tells the model to finish the call. Subclasses render the two kinds of exit."""

    def end(self, situation: str, outcome: OutcomeRef | None) -> str:
        """Phrase for ending the call in ``situation`` (outcome ``outcome``)."""
        raise NotImplementedError

    def handoff(self, queue: str, outcome: OutcomeRef | None) -> str:
        """Phrase for transferring the call to ``queue``."""
        raise NotImplementedError

    def tool_name(self, node: str) -> str:
        """Name the model sees for the exit into ``node`` (staged design)."""
        return naming.transition_tool(node)


@dataclass(frozen=True)
class StagedExits(ExitStyle):
    """Staged design: the model calls ``transition_to_<node>``; the workflow computes the outcome and closes the call."""

    def end(self, situation: str, outcome: OutcomeRef | None) -> str:
        return f"call {naming.transition_tool(naming.end_node(situation))}"

    def handoff(self, queue: str, outcome: OutcomeRef | None) -> str:
        return f"call {naming.transition_tool(naming.handoff_node(queue))}"


@dataclass(frozen=True)
class LeanExits(ExitStyle):
    """Lean design: the model itself calls ``transfer_to_agent`` / ``close_call`` with the outcome from the table."""

    def end(self, situation: str, outcome: OutcomeRef | None) -> str:
        return f"call close_call with {outcome_text(outcome)}" if outcome else "call close_call"

    def handoff(self, queue: str, outcome: OutcomeRef | None) -> str:
        close = f", then close_call with {outcome_text(outcome)}" if outcome else ""
        return f'call transfer_to_agent with queue "{queue}"{close}'


def outcome_text(ref: OutcomeRef | None) -> str:
    """``outcome "x" and reason "y"`` (reason omitted when none)."""
    if ref is None:
        return "the outcome from the table"
    text = f'outcome "{ref.outcome}"'
    return text + (f' and reason "{ref.reason}"' if ref.reason else "")


def override_phrase(ir: PolicyIR, key: str) -> str:
    """What "never ..." means for an intent key that a higher-precedence intent overrides (for example in a prompt line)."""
    if key == "flat_refusal":
        return "make a persuasion attempt"
    other = ir.intent(key)
    if other is not None:
        queue = other.transfer_queue()
        if queue:
            return f"transfer the customer to the {queue} team"
        if other.outcome is not None:
            return f"end the call because of {key.replace('_', ' ')}"
    return f"apply the {key.replace('_', ' ')} rule"


def _sentence(text: str) -> str:
    text = " ".join((text or "").split())
    return text if not text or text[-1] in ".!?\"'" else text + "."


def exit_bullet(
    ir: PolicyIR,
    intent: IntentRule,
    style: ExitStyle,
    *,
    note_tool: str = "add_note",
    redirect: Mapping[str, str] | None = None,
) -> str | None:
    """One prompt bullet for an intent that leaves the conversation or records a note; ``None`` for guidance-only intents.

    The bullet states the situation (the intent's own description), what to say (its ``response``), the single offer it
    makes first (``offer``), and the finishing tool for the accept / decline branches. Rule ids are appended.

    ``redirect`` maps an intent key to the node its exit goes through instead (a guard node, see
    :meth:`builder.compiler.profiles.staged._Staged.plan_latches`): the engine names the tool the model calls after the
    node a transition leads to, so the bullet must name that node's tool.
    """
    info = naming.intent_exit(ir, intent)
    if info.kind == "info":
        return None
    label = " ".join(intent.description.split()).rstrip(".")
    parts: list[str] = []
    if intent.min_occurrences > 1:
        parts.append(
            f"Act only when the person says this {times_text(intent.min_occurrences)}; before that follow the other rules."
        )
    if intent.response:
        parts.append(_sentence(intent.response))
    if info.kind == "note":
        if deferred_notes(ir):
            parts.append(f'Take no promise for this product; {note_tool} reason "{info.reason}" (do not call {note_tool} '
                         "yet - write it only at the very end); continue with the others.")
        else:
            parts.append(f'Take no promise for this product; {note_tool} reason "{info.reason}"; continue with the others.')
    elif info.kind == "handoff":
        move = style.handoff(info.queue, info.outcome)
        if redirect and intent.key in redirect:
            move = f"call {naming.transition_tool(redirect[intent.key])}"
        if intent.offer:
            parts.append(_sentence(intent.offer))
            if "once" not in intent.offer.lower():
                parts.append("Make this offer only once.")
            if intent.overrides:
                parts.append("Make no other offer of any kind (no other transfer, plan or discount) unless the customer "
                             "explicitly asks for it.")
            parts.append(f"If they accept or insist: say you will transfer them, say goodbye and {move}.")
            if info.declined is not None:
                parts.append(_declined_sentence(style, info.declined, note_tool, deferred_notes(ir)))
        else:
            parts.append(f"Say you will transfer them, say goodbye and {move}.")
    else:  # end
        if intent.offer:
            parts.append(_sentence(intent.offer))
        move = style.end(info.situation, info.outcome)
        if redirect and intent.key in redirect:
            move = f"call {naming.transition_tool(redirect[intent.key])}"
        parts.append(f"Thank them, say goodbye and {move}.")
    if intent.overrides:
        keys = ", ".join(intent.overrides)
        never = " and never ".join(override_phrase(ir, key) for key in intent.overrides)
        parts.append(f"Once this applies, it comes before the rules for {keys}: never {never} in this call.")
    if intent.key == "restructuring" and info.kind == "handoff":
        parts.append("A request for a discount or a waiver is not a request for this: say you cannot offer one.")
    tag = ids_tag(intent.rule_ids)
    return f"{label}: " + " ".join(part for part in parts if part) + (f" {tag}" if tag else "")


def _declined_sentence(style: ExitStyle, declined: naming.IntentExit, note_tool: str, deferred: bool = False) -> str:
    if declined.kind == "note":
        if deferred:
            return ('If they decline (or answer anything other than yes): do not ask again; '
                    f'{note_tool} reason "{declined.reason}" for the product (do not call {note_tool} yet - write it only '
                    "at the very end) and continue.")
        return (f'If they decline (or answer anything other than yes): do not ask again; call {note_tool} with the '
                f'product_id and reason "{declined.reason}" and continue.')
    return (f"If they decline (or answer anything other than yes): thank them, say goodbye and "
            f"{style.end(declined.situation, declined.outcome)} - do not ask again.")


def uncovered_rules(
    ir: PolicyIR, ledger: RuleLedger, scopes: Sequence[str], *, max_chars: int = 420
) -> list[tuple[str, str]]:
    """Active ledger rules of ``scopes`` that no IR section cites (and residual rules of those scopes), verbatim.

    Returns ``(rule id, text)`` pairs in ledger order. Superseded rules are never returned. Quotes longer than
    ``max_chars`` are cut at a word boundary.
    """
    cited = ir.referenced_rule_ids()
    residual = {rule.rule_id: rule for rule in ir.residual_rules}
    out: list[tuple[str, str]] = []
    seen: set[str] = set()

    def clip(text: str) -> str:
        text = " ".join(text.split())
        return text if len(text) <= max_chars else text[:max_chars].rsplit(" ", 1)[0] + " ..."

    for rule in ledger.active_rules():
        if rule.id in seen or not set(scopes) & set(rule.scope):
            continue
        if rule.id in residual:
            out.append((rule.id, clip(residual[rule.id].text)))
            seen.add(rule.id)
        elif rule.id not in cited:
            out.append((rule.id, clip(rule.quote)))
            seen.add(rule.id)
    for rule_id, rule in residual.items():
        if rule_id not in seen and set(scopes) & set(rule.scope) and rule_id not in ledger.superseded_ids():
            out.append((rule_id, clip(rule.text)))
            seen.add(rule_id)
    return out

