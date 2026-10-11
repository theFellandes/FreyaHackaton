"""Deterministic, always-safe corrections of an extracted :class:`~builder.policy.ir.PolicyIR`.

A model call is expensive and slow; many defects of a freshly extracted IR are mechanical. :func:`apply_safe_fixes`
repairs exactly those, without changing the meaning of any value:

* ``rule_ids`` lists lose duplicates and ids that are not in the ledger (they are traceability data, not behaviour);
* intent keys are normalised to the catalog spelling (``canonical_intent_key``), and every reference to a renamed key
  (``overrides``, outcome-table situations, precedence) follows;
* values of the tool API's closed vocabularies are matched case- and separator-insensitively
  (``Some_Value`` becomes ``some_value``): identity fields, outcomes, reasons, note reasons and queues;
* active ledger rules that no section cites become ``residual_rules`` (their statement and stages), so no rule of the
  documents is silently dropped. A rule that really belongs in a structured field is still honoured, just as text;
* the disclosure wording gets its format: a ``team_phrase`` that is a whole sentence moves to
  ``phrases.extra["disclosure_intro"]``, an empty one becomes the institution's name;
* ``disclosure.amount_field`` / ``days_field`` are written down when the tool API shows that the state fields alone
  would make the compiler pick the wrong product field (a band is decided by them);
* an intent that records a per-product note in every scope is split into a before-verification part (the outcome) and an
  after-verification part (the note), because the opening and identity nodes have no note tool;
* a persuasion step whose date kind contradicts the date rule its own text states unambiguously ("the next business day
  after today" written as a week kind) gets the kind the text states (:mod:`builder.understand.date_cues`: a value
  contradicted by its own text, never a guess).

Anything else (a value that is *wrong*, not merely spelled differently) is left to the validator and the LLM repair.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

from builder.ingest.tool_api import ToolApi
from builder.policy.intents import canonical_intent_key
from builder.policy.ir import Disclosure, PolicyIR, RuleLedger, looks_like_sentence
from builder.policy.outcomes import TRANSFER_PREFIX
from builder.understand.date_cues import corrected_proposal_date
from builder.understand.validate import ToolVocabulary, resolve_product_fields, tool_vocabulary

__all__ = ["apply_safe_fixes"]

_SEPARATORS = re.compile(r"[^a-z0-9]+")


def apply_safe_fixes(
    ir: PolicyIR, ledger: RuleLedger, tool_api: ToolApi | None = None
) -> tuple[PolicyIR, list[str]]:
    """Return ``(fixed IR, log)``; ``log`` has one human-readable line per kind of correction made.

    The input is not modified. ``tool_api`` enables the vocabulary corrections (without it only the id, intent-key and
    coverage fixes run).
    """
    data = ir.model_dump(mode="json")
    log: list[str] = []
    if ledger.rules:
        _clean_rule_ids(data, set(ledger.ids()), log)
    _canonical_intents(data, log)
    if tool_api is not None:
        _match_vocabulary(data, tool_vocabulary(tool_api), log)
    _disclosure_wording(data, log)
    _split_noted_intents(data, log)
    _persuasion_date_kinds(data, log)
    result = PolicyIR.model_validate(data)
    if tool_api is not None:
        result = _product_fields(result, tool_api, log)
    if ledger.rules:
        data = result.model_dump(mode="json")
        _residual_for_uncovered(data, ledger, log)
        result = PolicyIR.model_validate(data)
    return result, log


# --------------------------------------------------------------------------------------------------------------------


def _walk(node: Any) -> Iterator[dict[str, Any]]:
    """Yield every dict inside ``node`` (depth first)."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


def _clean_rule_ids(data: dict[str, Any], known: set[str], log: list[str]) -> None:
    dropped: list[str] = []
    for node in _walk(data):
        ids = node.get("rule_ids")
        if not isinstance(ids, list):
            continue
        cleaned = [i for i in dict.fromkeys(ids) if isinstance(i, str) and i in known]
        dropped.extend(str(i) for i in ids if i not in known)
        node["rule_ids"] = cleaned
    if dropped:
        shown = ", ".join(sorted(set(dropped)))
        log.append(f"dropped {len(set(dropped))} rule id(s) that are not in the ledger: {shown}")


def _canonical_intents(data: dict[str, Any], log: list[str]) -> None:
    intents = data.get("intents") or []
    renames: dict[str, str] = {}
    taken = {(i.get("key"), i.get("scope")) for i in intents}
    for intent in intents:
        key = str(intent.get("key", ""))
        canon = canonical_intent_key(key)
        if canon and canon != key and (canon, intent.get("scope")) not in taken:
            taken.discard((key, intent.get("scope")))
            taken.add((canon, intent.get("scope")))
            renames[key] = canon
            intent["key"] = canon
    if not renames:
        return
    for intent in intents:
        intent["overrides"] = [renames.get(k, k) for k in intent.get("overrides", [])]
    closing = data.get("closing") or {}
    for row in closing.get("rows", []):
        row["situation"] = renames.get(row.get("situation"), row.get("situation"))
    closing["precedence"] = [renames.get(k, k) for k in closing.get("precedence", [])]
    log.append("intent keys renamed to catalog spelling: " + ", ".join(f"{a} -> {b}" for a, b in renames.items()))


def _canon(value: str) -> str:
    return _SEPARATORS.sub("_", value.strip().lower()).strip("_")


def _match(value: Any, allowed: list[str] | None) -> str | None:
    """The allowed value ``value`` is a respelling of, or ``None`` (also when it is already exact)."""
    if not isinstance(value, str) or not allowed or value in allowed:
        return None
    wanted = _canon(value)
    hits = [a for a in allowed if _canon(a) == wanted]
    return hits[0] if len(hits) == 1 else None


def _rekey_transfer_queue(params: dict[str, Any], vocab: ToolVocabulary, changes: list[str]) -> None:
    """Move a transfer param the API does not have (``queue``, or the only unknown one) to the API's queue field.

    Only a key that is not a request field of the transfer tool is moved, so a valid param is never rewritten.
    """
    fields = vocab.request_fields.get("transfer_to_agent")
    target = vocab.queue_field
    if not fields or target not in fields or target in params:
        return
    unknown = [key for key in params if key not in fields]
    source = "queue" if "queue" in unknown else (unknown[0] if len(unknown) == 1 else None)
    if source is not None:
        params[target] = params.pop(source)
        changes.append(f"transfer_to_agent param {source!r} -> {target!r} (the tool API's queue field)")


def _match_vocabulary(data: dict[str, Any], vocab: ToolVocabulary, log: list[str]) -> None:
    changes: list[str] = []

    def fix(container: dict[str, Any], key: str, allowed: list[str] | None, label: str) -> None:
        better = _match(container.get(key), allowed)
        if better is not None:
            changes.append(f"{label}: {container[key]!r} -> {better!r}")
            container[key] = better

    for question in (data.get("identity") or {}).get("questions", []):
        fix(question, "field", vocab.identity_fields, "identity field")
    for node in _walk(data):
        if isinstance(node.get("outcome"), str):
            fix(node, "outcome", vocab.outcomes, "outcome")
            if "reason" in node:
                fix(node, "reason", vocab.reasons, "reason")
    multi = data.get("multi_product") or {}
    if isinstance(multi.get("note_reasons"), list):
        multi["note_reasons"] = [_match(r, vocab.note_reasons) or r for r in multi["note_reasons"]]
    fix(multi, "paid_note_reason", vocab.note_reasons, "paid note reason")
    for intent in data.get("intents") or []:
        fix(intent, "note_reason", vocab.note_reasons, "note reason")
        for action in intent.get("actions", []):
            params = action.get("params") or {}
            if action.get("tool") == "transfer_to_agent":
                _rekey_transfer_queue(params, vocab, changes)
                fix(params, vocab.queue_field, vocab.queues, "queue")
            elif "queue" in params:
                fix(params, "queue", vocab.queues, "queue")

    def fix_situation(key: str) -> str:
        if not key.lower().startswith(TRANSFER_PREFIX):
            return key
        queue = key[len(TRANSFER_PREFIX):]
        fixed = TRANSFER_PREFIX + (_match(queue, vocab.queues) or queue)
        if fixed != key:
            changes.append(f"situation {key!r} -> {fixed!r}")
        return fixed

    closing = data.get("closing") or {}
    for row in closing.get("rows", []):
        row["situation"] = fix_situation(str(row.get("situation", "")))
    closing["precedence"] = [fix_situation(str(k)) for k in closing.get("precedence", [])]
    if changes:
        log.append("vocabulary respelled to the tool API's values: " + "; ".join(changes))


def _intro_present(extra: dict[str, Any]) -> bool:
    return any(
        isinstance(value, str) and value.strip() and (name in ("disclosure_intro", "intro") or name.endswith("_intro"))
        for name, value in extra.items()
    )


def _disclosure_wording(data: dict[str, Any], log: list[str]) -> None:
    """``team_phrase`` completes "I am calling from ___": a whole sentence moves to ``disclosure_intro``, empty = bank name."""
    disclosure = data.get("disclosure")
    if not isinstance(disclosure, dict):
        return
    phrases = data.setdefault("phrases", {})
    extra = phrases.setdefault("extra", {})
    team = str(disclosure.get("team_phrase") or "").strip()
    if _intro_present(extra):
        return
    if team and looks_like_sentence(team, opener_only=True):
        sentence = team if team[-1] in ".!?" else team + "."
        extra["disclosure_intro"] = sentence
        disclosure["team_phrase"] = ""
        log.append(f"disclosure.team_phrase was a whole sentence; it is now phrases.extra['disclosure_intro']: {sentence!r}")
    elif not team and str(data.get("bank_name") or "").strip():
        disclosure["team_phrase"] = str(data["bank_name"]).strip()
        log.append(f"disclosure.team_phrase was empty; it is now the institution's name {disclosure['team_phrase']!r}")


def _product_fields(ir: PolicyIR, tool_api: ToolApi, log: list[str]) -> PolicyIR:
    """Write ``disclosure.amount_field`` / ``days_field`` when reading the state fields would pick the wrong product field."""
    disclosure = ir.disclosure
    amount, days = resolve_product_fields(ir, tool_api)
    guessed_amount, guessed_days = Disclosure.model_construct(
        team_phrase="", product_naming={}, state_fields=list(disclosure.state_fields), amount_field=None, days_field=None
    ).amount_and_days_fields()
    update: dict[str, str] = {}
    if amount and not (disclosure.amount_field or "").strip() and amount != guessed_amount:
        update["amount_field"] = amount
        log.append(f"disclosure.amount_field set to {amount!r} (the state fields alone gave {guessed_amount or 'nothing'!r})")
    if days and not (disclosure.days_field or "").strip() and days != guessed_days:
        update["days_field"] = days
        log.append(f"disclosure.days_field set to {days!r} (the state fields alone gave {guessed_days or 'nothing'!r})")
    if not update:
        return ir
    return ir.model_copy(update={"disclosure": disclosure.model_copy(update=update)})


def _split_noted_intents(data: dict[str, Any], log: list[str]) -> None:
    """Split an ``any``-scope intent that also records a note: the note exists only after verification.

    The opening and identity nodes have no note tool, so the compiled agent could not act on a note there. The
    before-verification half keeps the outcome (no note); the after-verification half keeps both.
    """
    intents = data.get("intents")
    if not isinstance(intents, list):
        return
    taken = {(i.get("key"), i.get("scope")) for i in intents if isinstance(i, dict)}
    out: list[Any] = []
    split: list[str] = []
    dropped: list[str] = []
    for intent in intents:
        if (
            isinstance(intent, dict)
            and intent.get("scope") == "before_verification"
            and intent.get("note_reason")
            and intent.get("outcome")
        ):  # a note cannot be taken before verification; the outcome is what the agent can do
            intent = {k: v for k, v in intent.items() if k != "note_reason"}
            dropped.append(str(intent.get("key")))
        if (
            isinstance(intent, dict)
            and intent.get("scope", "any") == "any"
            and intent.get("note_reason")
            and intent.get("outcome")
            and (intent.get("key"), "before_verification") not in taken
            and (intent.get("key"), "after_verification") not in taken
        ):
            before = {k: v for k, v in intent.items() if k != "note_reason"}
            before["scope"] = "before_verification"
            after = {**intent, "scope": "after_verification"}
            out.extend([before, after])
            split.append(str(intent.get("key")))
        else:
            out.append(intent)
    if split or dropped:
        data["intents"] = out
    if split:
        log.append("intents split into before/after verification (the note exists only after it): " + ", ".join(split))
    if dropped:
        log.append("note_reason dropped from before-verification intents (no note tool exists yet): " + ", ".join(dropped))


def _persuasion_date_kinds(data: dict[str, Any], log: list[str]) -> None:
    """AUD-18: give a persuasion step the date kind its own text states, when the text states exactly one."""
    changes: list[str] = []
    for step in (data.get("persuasion") or {}).get("steps") or []:
        if not isinstance(step, dict):
            continue
        better = corrected_proposal_date(str(step.get("date", "")), step.get("n"), str(step.get("text") or ""))
        if better is None:
            continue
        old = f"{step.get('date')}" + (f" n={step.get('n')}" if step.get("n") is not None else "")
        step["date"], step["n"] = better
        new = better[0] + (f" n={better[1]}" if better[1] is not None else "")
        changes.append(f"attempt {step.get('attempt')}: {old} -> {new}")
    if changes:
        log.append("persuasion date kinds set to the rule their own text states: " + "; ".join(changes))


def _residual_for_uncovered(data: dict[str, Any], ledger: RuleLedger, log: list[str]) -> None:
    cited: set[str] = set()
    for node in _walk(data):
        cited.update(i for i in node.get("rule_ids", []) if isinstance(i, str))
    residual = data.setdefault("residual_rules", [])
    cited.update(str(r.get("rule_id")) for r in residual)
    added: list[str] = []
    for rule in ledger.active_rules():
        if rule.id in cited:
            continue
        residual.append(
            {
                "rule_id": rule.id,
                "text": rule.statement.strip() or rule.quote.strip(),
                "scope": list(rule.scope) or ["global"],
            }
        )
        added.append(rule.id)
    if added:
        log.append(f"{len(added)} uncovered ledger rule(s) added as residual rules: {', '.join(added)}")
