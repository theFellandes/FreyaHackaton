"""Structured-output helpers shared by every provider adapter.

Three jobs:

``schema_for_llm(model)``
    Turn a Pydantic model into a JSON schema that the providers' native structured-output modes accept:
    ``$defs`` are inlined (only genuinely recursive definitions stay as ``$ref``), every object with declared
    properties gets ``additionalProperties: false``, single-value ``const`` becomes ``enum``, and keywords that
    Anthropic structured outputs reject (``minimum``, ``maximum``, ``exclusiveMinimum``, ``exclusiveMaximum``,
    ``multipleOf``, ``minLength``, ``maxLength``, ``minItems`` > 1, ``maxItems``, ``pattern``) are dropped,
    together with noise the models do not need (nested ``title``, ``default``, OpenAPI ``discriminator``).
    The Pydantic model itself still validates the answer, so dropped constraints are enforced after parsing.

    Free-form maps (``dict[str, X]``, rendered as an object whose ``additionalProperties`` is a schema) are kept
    as they are, because forcing ``additionalProperties: false`` on them would only allow ``{}``.
    ``has_free_form_objects(schema)`` tells adapters when a provider's constrained decoding cannot express the
    schema (Anthropic) so they fall back to prompt-instructed JSON.

``extract_json(text)``
    Pull the JSON payload out of a model reply: plain JSON, a fenced ```json block, or the first balanced
    ``{...}`` / ``[...]`` after some leading prose.

``parse_json_model(text, model)``
    ``extract_json`` + ``json.loads`` + ``model.model_validate``; every failure becomes a
    :class:`builder.llm.errors.ParseError`, which the gateway classifies as ``PARSE`` (one repair retry, then
    the next provider).
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Callable, Iterator
from typing import Any, Literal, TypeVar

from pydantic import BaseModel, ValidationError

from builder.llm.errors import ParseError

__all__ = [
    "UNSUPPORTED_KEYWORDS",
    "ParseError",
    "extract_json",
    "has_free_form_objects",
    "json_instruction",
    "parse_json_model",
    "repair_instruction",
    "schema_for_llm",
    "summarize_validation_error",
]

ModelT = TypeVar("ModelT", bound=BaseModel)

UNSUPPORTED_KEYWORDS: frozenset[str] = frozenset(
    {
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minLength",
        "maxLength",
        "maxItems",
        "pattern",
        # not rejected, but noise for the model (Pydantic still applies defaults) or dangling after inlining
        "default",
        "discriminator",
    }
)
"""Schema keywords removed by :func:`schema_for_llm` (``minItems`` is handled separately: 0/1 are kept)."""

_SCHEMA_LIST_KEYS = ("anyOf", "oneOf", "allOf", "prefixItems")
_SCHEMA_SINGLE_KEYS = ("items", "not", "additionalProperties")
_SCHEMA_MAP_KEYS = ("properties", "$defs", "definitions", "patternProperties")
_REF_PREFIX = "#/$defs/"
_NAME_SAFE = re.compile(r"[^A-Za-z0-9_-]+")
_FENCE = re.compile(r"```[ \t]*(?:json|JSON|json5)?[ \t]*\r?\n?(.*?)```", re.DOTALL)
_MAX_CANDIDATES = 50


# --------------------------------------------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------------------------------------------


def schema_for_llm(model: type[BaseModel]) -> dict[str, Any]:
    """Build the provider-friendly JSON schema of ``model`` (see the module docstring for the rules).

    Args:
        model: The Pydantic model the answer must validate against.

    Returns:
        A fresh JSON-schema dict with a top-level ``title`` safe to use as a function / schema name.
    """
    raw = copy.deepcopy(model.model_json_schema())
    defs: dict[str, Any] = raw.pop("$defs", {}) or {}
    root_title = raw.get("title") or model.__name__
    raw = _expand_root_ref(raw, defs)
    recursive = _recursive_defs(defs)
    schema = _inline_refs(raw, defs, recursive)
    if recursive:
        schema["$defs"] = {name: _inline_refs(defs[name], defs, recursive) for name in sorted(recursive)}
    cleaned = _clean(schema)
    cleaned["title"] = _NAME_SAFE.sub("_", str(root_title))[:64] or "Output"
    return cleaned


def _expand_root_ref(raw: dict[str, Any], defs: dict[str, Any]) -> dict[str, Any]:
    """A self-recursive model's schema is a bare root ``$ref``; give the root its own ``properties``.

    Providers (and LangChain's function conversion) need the root object spelled out; the recursive inner
    references keep pointing at ``$defs``.
    """
    ref = raw.get("$ref")
    if not (isinstance(ref, str) and ref.startswith(_REF_PREFIX)):
        return raw
    target = defs.get(ref[len(_REF_PREFIX) :])
    if target is None:
        return raw
    expanded = copy.deepcopy(target)
    expanded.update({k: v for k, v in raw.items() if k != "$ref"})
    return expanded


def has_free_form_objects(schema: dict[str, Any]) -> bool:
    """True when ``schema`` contains an object whose keys are not all declared (a ``dict[str, X]`` map).

    Anthropic's constrained decoding forces ``additionalProperties: false`` on every object, so such schemas
    must use prompt-instructed JSON there instead.
    """
    found = False

    def visit(node: dict[str, Any]) -> dict[str, Any]:
        nonlocal found
        is_object = node.get("type") == "object" or "properties" in node
        extra = node.get("additionalProperties")
        if is_object and (
            isinstance(extra, dict) or extra is True or ("properties" not in node and extra is None)
        ):
            found = True
        return node

    _walk(schema, visit)
    return found


def json_instruction(schema: dict[str, Any]) -> str:
    """Prompt suffix asking for a bare JSON answer that matches ``schema`` (used by prompt-based JSON mode)."""
    return (
        "Return ONLY JSON matching this JSON schema - no prose before or after it, no markdown fences, "
        "no comments:\n" + json.dumps(schema, indent=1, ensure_ascii=False)
    )


def repair_instruction(error: str) -> str:
    """User-message suffix for the one PARSE repair retry, quoting what was wrong with the previous reply."""
    return (
        "\n\nIMPORTANT - your previous reply could not be used because: "
        f"{error[:600]}\nReply again with ONLY one JSON object that matches the required schema exactly: "
        "every required field present, correct types, no extra keys, no prose, no markdown fences."
    )


def _recursive_defs(defs: dict[str, Any]) -> set[str]:
    """Names of definitions that can reach themselves through ``$ref`` (they cannot be inlined)."""
    edges = {name: _refs_in(body) for name, body in defs.items()}
    recursive: set[str] = set()
    for start in defs:
        stack, seen = list(edges.get(start, ())), set()
        while stack:
            current = stack.pop()
            if current == start:
                recursive.add(start)
                break
            if current in seen:
                continue
            seen.add(current)
            stack.extend(edges.get(current, ()))
    return recursive


def _refs_in(node: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith(_REF_PREFIX):
            found.add(ref[len(_REF_PREFIX) :])
        for value in node.values():
            found |= _refs_in(value)
    elif isinstance(node, list):
        for value in node:
            found |= _refs_in(value)
    return found


def _inline_refs(node: Any, defs: dict[str, Any], recursive: set[str]) -> Any:
    def visit(schema: dict[str, Any]) -> dict[str, Any]:
        ref = schema.get("$ref")
        if not (isinstance(ref, str) and ref.startswith(_REF_PREFIX)):
            return schema
        name = ref[len(_REF_PREFIX) :]
        if name in recursive or name not in defs:
            return schema
        siblings = {k: v for k, v in schema.items() if k != "$ref"}
        resolved = _inline_refs(copy.deepcopy(defs[name]), defs, recursive)
        resolved.update(siblings)  # a field's own description wins over the class docstring
        return resolved

    return _walk(node, visit)


def _clean(schema: dict[str, Any]) -> dict[str, Any]:
    """Drop unsupported keywords and titles, close declared objects, turn ``const`` into ``enum``."""

    def visit(node: dict[str, Any]) -> dict[str, Any]:
        out = {k: v for k, v in node.items() if k not in UNSUPPORTED_KEYWORDS and k != "title"}
        min_items = out.get("minItems")
        if isinstance(min_items, int) and min_items > 1:
            out.pop("minItems")
        if "const" in out:
            const = out.pop("const")
            out.setdefault("enum", [const])
        if "properties" in out and not isinstance(out.get("additionalProperties"), dict):
            out["additionalProperties"] = False
        return out

    return _walk(schema, visit)


def _walk(node: Any, visit: Callable[[dict[str, Any]], dict[str, Any]]) -> Any:
    """Apply ``visit`` to every *schema* node, bottom-up, without mistaking property names for keywords."""
    if not isinstance(node, dict):
        return node
    out = dict(node)
    for key in _SCHEMA_LIST_KEYS:
        if isinstance(out.get(key), list):
            out[key] = [_walk(item, visit) for item in out[key]]
    for key in _SCHEMA_SINGLE_KEYS:
        if isinstance(out.get(key), dict):
            out[key] = _walk(out[key], visit)
    for key in _SCHEMA_MAP_KEYS:
        if isinstance(out.get(key), dict):
            out[key] = {name: _walk(sub, visit) for name, sub in out[key].items()}
    return visit(out)


# --------------------------------------------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------------------------------------------


def extract_json(text: str, *, prefer: Literal["object", "array", "any"] = "any") -> str:
    """Return the JSON document embedded in a model reply.

    Tried in order: the whole (stripped) text, each fenced code block, then every balanced ``{...}`` /
    ``[...]`` span in order of appearance (string-aware, so braces inside strings do not confuse it). With
    ``prefer="object"`` (or ``"array"``) the first candidate of that shape wins; otherwise the first
    candidate that parses at all.

    Raises:
        ParseError: No parseable JSON in ``text``.
    """
    if not text or not text.strip():
        raise ParseError("empty reply: no JSON found", raw_text=text or "")
    fallback: str | None = None
    for candidate in _candidates(text):
        value = _try_load(candidate)
        if value is _INVALID:
            continue
        if _shape_ok(value, prefer):
            return candidate
        if fallback is None:
            fallback = candidate
    if fallback is not None:
        return fallback
    raise ParseError("no parseable JSON object found in the reply", raw_text=text)


def parse_json_model(text: str, model: type[ModelT]) -> ModelT:
    """Parse a model reply into an instance of ``model``.

    Raises:
        ParseError: No JSON, invalid JSON, or the JSON does not validate (message lists up to 8 errors).
    """
    payload = extract_json(text, prefer="object")
    try:
        data = json.loads(payload)
    except ValueError as exc:  # pragma: no cover - extract_json only returns loadable payloads
        raise ParseError(f"invalid JSON: {exc}", raw_text=text) from exc
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise ParseError(
            f"JSON does not match {model.__name__}: {summarize_validation_error(exc)}", raw_text=text
        ) from exc


def summarize_validation_error(exc: ValidationError, limit: int = 8) -> str:
    """Compact ``loc: msg`` list of a Pydantic validation error (used in repair prompts and traces)."""
    items = []
    for err in exc.errors()[:limit]:
        loc = ".".join(str(p) for p in err.get("loc", ())) or "<root>"
        items.append(f"{loc}: {err.get('msg', 'invalid')}")
    more = exc.error_count() - len(items)
    return "; ".join(items) + (f"; (+{more} more)" if more > 0 else "")


_INVALID = object()


def _try_load(candidate: str) -> Any:
    if not candidate or candidate[0] not in "{[":
        return _INVALID
    try:
        return json.loads(candidate)
    except ValueError:
        return _INVALID


def _shape_ok(value: Any, prefer: str) -> bool:
    if prefer == "object":
        return isinstance(value, dict)
    if prefer == "array":
        return isinstance(value, list)
    return True


def _candidates(text: str) -> Iterator[str]:
    """Lazily yield JSON candidates: whole text, fenced blocks, then balanced spans."""
    yield text.strip()
    for match in _FENCE.finditer(text):
        yield match.group(1).strip()
    yield from _balanced_spans(text)


def _balanced_spans(text: str) -> Iterator[str]:
    """Balanced ``{...}`` / ``[...]`` spans in order of their opening bracket (bounded work)."""
    starts = 0
    for start, char in enumerate(text):
        if char not in "{[":
            continue
        starts += 1
        if starts > _MAX_CANDIDATES:
            return
        end = _matching_close(text, start)
        if end is not None:
            yield text[start : end + 1]


def _matching_close(text: str, start: int) -> int | None:
    pairs = {"{": "}", "[": "]"}
    stack: list[str] = []
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in pairs:
            stack.append(pairs[char])
        elif char in "}]":
            if not stack or stack.pop() != char:
                return None
            if not stack:
                return index
    return None
