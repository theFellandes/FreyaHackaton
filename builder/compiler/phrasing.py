"""Prescribed sentences that must reach the agent, and placeholders that must never be spoken.

Two compile-time safety nets, both generic (no brief vocabulary; every sentence comes from the IR or the ledger):

* :func:`unspoken_phrases` (audit AUD-09): a sentence the brief prescribes (a ``Phrases`` field, any ``phrases.extra``
  entry, or a quoted sentence in a rule the phrases cite) that appears nowhere in the compiled workflow is found here,
  so the profile can place it word for word in the step its rule is scoped to. The templates read only a few fixed
  ``extra`` keys; the extraction stage may file a sentence under any other descriptive key, and it used to be dropped
  silently while the coverage report still counted its rule as covered.
* :func:`speakable` (audit AUD-12): IR placeholders written ``{name}`` become Freya references ``{{name}}`` when ``name``
  is a call variable, and in text the engine speaks verbatim (``tts``) any other single-brace token is removed, so the
  caller never hears braces.

"Appears in the compiled workflow" is a token test: the content words of the sentence (lower case, placeholders and a
few function words removed) must all occur in one sentence-sized segment of some compiled string (prompt, spoken line or
code). It errs towards "present": a paraphrase that uses the same words counts, so nothing is added twice.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any

from builder.policy.ir import PolicyIR, RuleEntry, RuleLedger

__all__ = [
    "UnspokenPhrase",
    "content_tokens",
    "fill_values_note",
    "fill_values_prompt",
    "intent_fixed_sentence",
    "is_covered",
    "quoted_sentences",
    "runtime_placeholders",
    "segments_of",
    "speakable",
    "strings_of",
    "unspoken_phrases",
]

_STOPWORDS = frozenset({
    "a", "an", "the", "i", "i'm", "im", "you", "your", "yours", "we", "our", "us", "me", "my", "it", "its", "this",
    "that", "these", "those", "to", "of", "in", "on", "at", "by", "for", "with", "as", "and", "or", "is", "are", "am",
    "be", "been", "was", "were", "will", "would", "shall", "should", "can", "could", "may", "might", "do", "does", "did",
    "so", "then", "than", "there", "here", "s", "t", "please",
})
_PLACEHOLDER = re.compile(r"\{\{?\s*[A-Za-z_][\w.]*\s*\}?\}")
_QUOTED = re.compile(r'"([^"\n]{6,}?)"|\u201c([^\u201d\n]{6,}?)\u201d')
_SEGMENT_SPLIT = re.compile(r"[.!?\n\"\u201c\u201d]+")
#: a single-brace token in spoken text; the name may be in any script (``{tutar}``, ``{ödeme_tarihi}``)
_SPEECH_TOKEN = re.compile(r"(?<!\{)\{[^\W\d]\w*\}(?!\})")
#: a value a prescribed sentence leaves to run time: ``{name}`` (one brace), ``[name]`` or ``<name>``. The name starts
#: with a letter of any script and has no digits, so rule-id tags such as ``[D1-20a]`` are never taken for one.
_RUNTIME_TOKEN = re.compile(
    r"(?<!\{)\{(?P<curly>[^\W\d][\w.]*)\}(?!\})"
    r"|\[(?P<square>[^\W\d][^\W\d.]*(?: [^\W\d]+){0,3})\]"
    r"|<(?P<angle>[^\W\d][^\W\d.]*(?: [^\W\d]+){0,3})>"
)
#: ``Phrases`` fields and the step their sentence belongs to when no ledger rule tells.
_PHRASE_FIELDS: dict[str, str] = {
    "opening_sentence": "opening",
    "right_party_question": "opening",
    "robot_answer": "global",
    "scam_answer": "global",
    "confirm_question": "confirmation",
    "today_question": "negotiation",
    "recorded_sentence": "recording",
    "identity_failed_sentence": "identity",
    "deceased_sentence": "third_party",
    "paid_sentence": "negotiation",
    "goodbye_sentence": "closing",
}
#: Intents whose prescribed sentence also has a fixed ``Phrases`` field.
_INTENT_PHRASE = {"already_paid": "paid_sentence", "deceased": "deceased_sentence"}


def content_tokens(text: str) -> set[str]:
    """The content words of ``text``: lower case, placeholders and common function words removed."""
    plain = _PLACEHOLDER.sub(" ", text or "").lower().replace("\u2019", "'")
    return {word for word in re.findall(r"[a-z0-9]+(?:'[a-z]+)?", plain) if word not in _STOPWORDS}


def quoted_sentences(text: str) -> list[str]:
    """Sentences quoted in ``text`` (straight or curly double quotes, at least six characters and two content words)."""
    found = [(a or b).strip() for a, b in _QUOTED.findall(text or "")]
    return [s for s in found if len(content_tokens(s)) >= 2]


def strings_of(obj: Any) -> Iterator[str]:
    """Every string inside a JSON-like value (dict values, list items), depth first."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, Mapping):
        for value in obj.values():
            yield from strings_of(value)
    elif isinstance(obj, list | tuple):
        for item in obj:
            yield from strings_of(item)


def segments_of(texts: Iterable[str]) -> list[set[str]]:
    """Sentence-sized token sets of ``texts`` (split at sentence ends, quotes and line breaks)."""
    out: list[set[str]] = []
    for text in texts:
        # code strings embed data with escaped braces and line breaks (``builder.compiler.pycode.py_literal``)
        plain = text.replace("\\x7b", "{").replace("\\x7d", "}").replace("\\n", "\n")
        for part in _SEGMENT_SPLIT.split(plain):
            tokens = content_tokens(part)
            if tokens:
                out.append(tokens)
    return out


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", " ".join((text or "").split())) if s.strip()]


def is_covered(text: str, segments: list[set[str]]) -> bool:
    """Every sentence of ``text`` has all its content words inside one segment.

    A recording notice counts in the active form the compiler speaks it in ("We record this call", see
    :func:`builder.compiler.context.soften_recording_notice`).
    """
    from builder.compiler.context import soften_recording_notice  # context imports this module

    return _covered(text, segments) or _covered(soften_recording_notice(text), segments)


def _covered(text: str, segments: list[set[str]]) -> bool:
    for sentence in _sentences(text):
        tokens = content_tokens(sentence)
        if tokens and not any(tokens <= segment for segment in segments):
            return False
    return True


def intent_fixed_sentence(ir: PolicyIR, key: str, response: str) -> str:
    """``response`` with the brief's fixed sentence for the intent in front when the response does not already hold it.

    The already-paid and deceased sentences have a field of their own (``phrases.paid_sentence`` /
    ``phrases.deceased_sentence``); the extraction stage usually also copies them into the intent's response, but when it
    does not, the bullet must still say them (AUD-09).
    """
    field = _INTENT_PHRASE.get(key)
    fixed = getattr(ir.phrases, field, "").strip() if field else ""
    response = (response or "").strip()
    if not fixed or content_tokens(fixed) <= content_tokens(response):
        return response
    return f'Say "{fixed}" {response}'.strip()


@dataclass(frozen=True)
class UnspokenPhrase:
    """A prescribed sentence the compiled workflow does not contain.

    Attributes:
        rule_id: The ledger rule that prescribes it (``""`` when none could be found).
        scopes: The stages it belongs to (the rule's scope, else a default for the field; ``global`` = every step).
        line: The prompt line to add: the rule verbatim, or ``say exactly "<sentence>"`` with the situation.
        source: Where the sentence came from (``rule``, ``phrases.<field>`` or ``phrases.extra.<key>``), for the notes.
    """

    rule_id: str
    scopes: tuple[str, ...]
    line: str
    source: str


def _clip(text: str, max_chars: int = 420) -> str:
    text = " ".join(text.split())
    return text if len(text) <= max_chars else text[:max_chars].rsplit(" ", 1)[0] + " ..."


def _linked_rule(ledger: RuleLedger, sentence: str, preferred: list[str]) -> RuleEntry | None:
    """The active rule whose quote holds ``sentence`` (rules the phrases cite first)."""
    tokens = content_tokens(sentence)
    if not tokens:
        return None
    active = {rule.id: rule for rule in reversed(ledger.active_rules())}
    order = [active[i] for i in preferred if i in active] + [r for r in ledger.active_rules() if r.id not in preferred]
    return next((rule for rule in order if tokens <= content_tokens(rule.quote)), None)


def unspoken_phrases(
    ir: PolicyIR, ledger: RuleLedger, texts: Iterable[str], variables: Iterable[str] = (),
) -> list[UnspokenPhrase]:
    """Prescribed sentences that no string of ``texts`` (the compiled workflow) contains, in a stable order.

    1. A rule the phrases cite (``phrases.rule_ids``) whose quote holds a quoted sentence that is missing: the rule is
       placed verbatim (it carries both the trigger and the sentence).
    2. A ``Phrases`` field or ``phrases.extra`` entry that is still missing: ``say exactly "<sentence>"`` with its key as
       the situation, scoped like the rule that quotes it (else like the field; an unknown ``extra`` key is global).

    A sentence with placeholders that no call variable (``variables``) fills keeps them and gets the instruction to say
    the real values in their place (:func:`fill_values_note`), so it is never said with a placeholder or without its value.
    """
    names = list(variables)
    segments = segments_of(texts)
    out: list[UnspokenPhrase] = []
    cited = list(dict.fromkeys(ir.phrases.rule_ids))
    for rule_id in cited:
        rule = ledger.get(rule_id)
        if rule is None or rule_id in ledger.superseded_ids():
            continue
        missing = [s for s in quoted_sentences(rule.quote) if not is_covered(s, segments)]
        if missing:
            line = _clip(rule.quote)
            note = fill_values_note(list(dict.fromkeys(p for s in missing for p in runtime_placeholders(s, names))))
            line = f"{line} {note}" if note else line
            out.append(UnspokenPhrase(rule.id, tuple(rule.scope) or ("global",), line, "rule"))
            segments.extend(segments_of([rule.quote]))
    candidates = [(f"phrases.{name}", name, str(getattr(ir.phrases, name, "") or ""), _PHRASE_FIELDS[name])
                  for name in _PHRASE_FIELDS]
    candidates += [(f"phrases.extra.{key}", key, str(value or ""), "global") for key, value in ir.phrases.extra.items()]
    for source, key, sentence, default_scope in candidates:
        sentence = " ".join(sentence.split())
        if not sentence or is_covered(sentence, segments):
            continue
        rule = _linked_rule(ledger, sentence, cited)
        scopes = tuple(rule.scope) if rule is not None and rule.scope else (default_scope,)
        label = key.replace("_", " ")
        line = f'Fixed sentence ({label}): when this applies, say exactly "{sentence}"'
        note = fill_values_note(runtime_placeholders(sentence, names))
        out.append(UnspokenPhrase(rule.id if rule else "", scopes, f"{line} {note}" if note else line, source))
        segments.extend(segments_of([sentence]))
    return out


def speakable(text: str, variables: Iterable[str], *, spoken: bool) -> tuple[str, list[str]]:
    """``text`` with ``{name}`` turned into ``{{name}}`` for call variables; for spoken text other tokens are removed.

    Returns the text and the single-brace tokens that were removed (empty for prompt text, which keeps them: a prompt may
    name a computed value such as ``{floor_amount}`` that the model reads from the facts).
    """
    from builder.compiler.context import to_freya  # context imports this module

    text = to_freya(text, variables)
    if not spoken:
        return text, []
    removed = _SPEECH_TOKEN.findall(text)
    if not removed:
        return text, []
    text = _SPEECH_TOKEN.sub("", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    return text.strip(), removed


def runtime_placeholders(text: str, variables: Iterable[str] = ()) -> list[str]:
    """The placeholders of a prescribed sentence that no call variable fills, in order and without repeats.

    ``{name}`` (one brace), ``[name]`` and ``<name>`` count, with a name in any language (``{tutar}``, ``[tarih]``,
    ``<amount>``); a ``{name}`` that is a call variable (it becomes ``{{name}}``), a ``{{reference}}`` and a tag with digits
    (``[D1-20a]``) do not. Such a placeholder stands for a value of the call (the recorded amount and date of a promise),
    which the compiler cannot write into the sentence; see :func:`fill_values_note`.
    """
    known = set(variables)
    found: list[str] = []
    for match in _RUNTIME_TOKEN.finditer(text or ""):
        if match.group("curly") is not None and match.group("curly") in known:
            continue
        if match.group(0) not in found:
            found.append(match.group(0))
    return found


def _listed(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def fill_values_note(placeholders: list[str]) -> str:
    """The instruction that goes with a sentence holding ``placeholders``: say the real values, never a placeholder.

    A sentence the brief prescribes is never dropped or emptied because it holds a value only the call knows (the
    promise confirmation "Your promise of {amount} on {date} is recorded."): it is said with the value in place of each
    placeholder. ``""`` for no placeholders.
    """
    if not placeholders:
        return ""
    names = _listed(placeholders)
    verb = "stands" if len(placeholders) == 1 else "stand"
    return (f"In that text {names} {verb} for values of this call: say the real value in place of each (an amount or "
            "a date exactly as agreed with the customer and, once recorded, as recorded; any other value as the facts or "
            "the customer gave it). Never say a placeholder itself, its brackets or its name instead of the value.")


def fill_values_prompt(text: str, placeholders: list[str], language: str = "") -> str:
    """The ``on_enter`` prompt that says the fixed line ``text`` with the call's values in place of ``placeholders``.

    ``language`` (a human name such as ``Turkish``; empty = English) is the language of a call that is not in English:
    the line is then said in that language like every other fixed line (:func:`builder.compiler.language.say_in_prompt`).
    """
    from builder.compiler.language import say_in_prompt  # language imports context, which imports this module

    note = fill_values_note(placeholders)
    if language:
        return f"{say_in_prompt(text, language)} {note}".strip()
    return ("Say this to the customer word for word, as one natural group of sentences, with the real values in place "
            "of its placeholders. Add nothing of your own: no extra greeting, explanation or question (ask a question "
            f'only when the text has one). The text: "{text}" {note}').strip()
