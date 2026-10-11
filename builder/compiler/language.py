# ruff: noqa: RUF001  (Turkish letters such as the dotless i are meant)
"""The language the agent speaks, and how fixed lines are said in a language other than English.

The compiler writes its instructions in English (the model reads them) and builds some spoken lines from English
templates (the debt statement, guard corrections, callback and promise confirmations, goodbyes of the rescue paths). In
an English call those lines are spoken verbatim (``tts``). In a call in another language a fixed English line would be
heard in English, so :func:`localize_entry` turns every ``tts`` entry into an ``llm`` entry whose prompt asks the model
to say the line in the call's language, keeping its meaning and every number, amount, date, name and product. A line
that is the brief's own sentence is already in that language and is said word for word.

:func:`spoken_language` is the IR's ``language``; when the extraction left the default ``en`` but the brief's own fixed
sentences are clearly written in another language (a small table of very common words per language), that language
wins, so a missed language code cannot make a Turkish agent speak English.
"""

from __future__ import annotations

import re
from typing import Any

from builder.compiler.context import language_name
from builder.policy.ir import PolicyIR

__all__ = [
    "ENGLISH",
    "is_english",
    "localize_entry",
    "say_in_prompt",
    "spoken_language",
    "text_language",
]

ENGLISH = "en"

#: Very common words per language (lower case, accents kept); a sentence's language is the one with the most hits.
_COMMON_WORDS: dict[str, frozenset[str]] = {
    "en": frozenset({"the", "and", "you", "your", "is", "are", "this", "that", "with", "for", "to", "of", "we", "i",
        "am", "a", "an", "be", "can", "will", "not", "it", "on", "in", "please", "thank", "goodbye", "hello",
        "speaking", "calling"}),
    "tr": frozenset({"ve", "bir", "bu", "için", "ile", "mi", "mı", "mu", "mü", "misiniz", "musunuz", "değil", "ben",
        "biz", "siz", "sizin", "size", "evet", "hayır", "teşekkür", "ederim", "lütfen", "merhaba", "günler", "iyi",
        "olarak", "olan", "var", "yok", "ama", "çok", "da", "de", "gibi"}),
    "de": frozenset({"und", "der", "die", "das", "ist", "sie", "ich", "wir", "nicht", "mit", "für", "ein", "eine",
        "zu", "auf", "bitte", "danke", "guten", "tag", "ihr", "ihre", "spreche", "sind", "haben"}),
    "fr": frozenset({"et", "le", "la", "les", "est", "vous", "je", "nous", "pas", "avec", "pour", "un", "une", "de",
        "du", "des", "à", "merci", "bonjour", "au", "revoir", "votre", "suis", "sommes"}),
    "es": frozenset({"y", "el", "la", "los", "las", "es", "usted", "yo", "nosotros", "no", "con", "para", "un", "una",
        "de", "del", "gracias", "hola", "adiós", "su", "sus", "estoy", "hablo"}),
}
#: Letters that only one of the tabled languages uses; each one counts as a hit for that language.
_LETTERS: dict[str, str] = {"ğ": "tr", "ı": "tr", "ş": "tr", "İ": "tr", "ß": "de", "ñ": "es"}
_WORD = re.compile(r"[^\W\d_]+")


def is_english(code: str) -> bool:
    """True for English (``en``, ``en-GB`` ...) and for an empty code (the IR default)."""
    return (code or ENGLISH).strip().lower()[:2] == ENGLISH


def text_language(texts: list[str]) -> str:
    """The tabled language most of ``texts`` is written in (``""`` when nothing scores)."""
    scores = dict.fromkeys(_COMMON_WORDS, 0)
    for text in texts:
        for letter in text:
            if letter in _LETTERS:
                scores[_LETTERS[letter]] += 1
        for word in _WORD.findall(text.lower()):
            for code, common in _COMMON_WORDS.items():
                if word in common:
                    scores[code] += 1
    best = max(scores, key=lambda code: scores[code])
    return best if scores[best] > 0 else ""


def _fixed_sentences(ir: PolicyIR) -> list[str]:
    phrases = ir.phrases
    texts = [phrases.opening_sentence, phrases.right_party_question, phrases.goodbye_sentence,
             phrases.today_question, phrases.confirm_question, phrases.robot_answer, *phrases.extra.values()]
    return [re.sub(r"\{[^{}]*\}", " ", text) for text in texts if text and text.strip()]


def spoken_language(ir: PolicyIR) -> str:
    """The code of the language the agent speaks (see the module docstring)."""
    code = (ir.language or ENGLISH).strip().lower() or ENGLISH
    if not is_english(code):
        return code
    found = text_language(_fixed_sentences(ir))
    return found if found and found != ENGLISH else code


def say_in_prompt(text: str, language: str) -> str:
    """The ``on_enter`` prompt that says the fixed line ``text`` in ``language`` (a human name, e.g. ``Turkish``)."""
    return (f"Say this to the customer in {language}, as one natural group of sentences. If it is already in "
            f"{language}, say it word for word; otherwise say it in {language} with exactly its meaning, keeping every "
            f"number, amount, date, name and product exactly as given. Add nothing of your own: no extra greeting, "
            f'explanation or question (ask a question only when the text has one). The text: "{text}"')


def localize_entry(node: dict[str, Any], language_code: str) -> bool:
    """Turn the ``tts`` entry of ``node`` into an ``llm`` entry that says it in ``language_code``; True if changed.

    A passthrough node (a fixed line that moves on at once) also gets a first sentence in its task message saying that
    the entry line is its only line, because its task message goes on "Say nothing more".
    """
    entry = node.get("on_enter")
    if not isinstance(entry, dict) or entry.get("type") != "tts" or not isinstance(entry.get("text"), str):
        return False
    language = language_name(language_code)
    node["on_enter"] = {"type": "llm", "prompt": say_in_prompt(entry["text"], language)}
    if node.get("passthrough") and isinstance(node.get("task_message"), str):
        node["task_message"] = (f"Your only line here is the entry line, said in {language}. "
                                + node["task_message"])
    return True

