"""Detect what the Freya platform silently dropped or rewrote when it stored a workflow.

The platform validates a workflow on save and *removes* fields that do not belong to a node type without
reporting an error (for example ``tools`` on an ``api_request`` node, or ``pre_actions`` on a ``conversation``
node; see ``docs/workflow-format.md`` and ``docs/tools-and-code.md``). A workflow can therefore save cleanly and
still behave differently from what the builder compiled. The deploy step reads the stored definition back with
:meth:`builder.freya.client.FreyaClient.get_workflow` and calls :func:`stripped_fields` to list every JSON path
that was sent but is missing or different in the stored copy.

Comparison rules (deliberately one-directional, "sent ⊆ stored"):

* Only keys present in the *sent* workflow are inspected, so keys the platform adds (defaults, ids,
  ``agent_config``) never count as differences.
* The top-level keys ``agent_config`` and ``nodePositions`` are ignored: the platform owns them.
* Mappings are compared key by key, lists of mappings index by index, and lists of scalars as multisets
  (a platform that re-orders an ``enum`` or ``required`` list has not stripped anything).
* With ``ignore_empty`` (the default) an empty value that was sent (``""``, ``[]``, ``{}``) and is absent or
  ``null`` in the stored copy is not reported, because dropping it changes nothing.

Paths use dots for keys and brackets for list indices, e.g. ``nodes.identity.tools[0].function.name``. Keys that
are not plain identifiers are quoted: ``nodes["odd.key"]``.

The module is pure (no I/O) and also offers :func:`null_paths`, used by the client to refuse a workflow that
contains ``null`` before it is sent (the platform rejects ``null`` anywhere).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

__all__ = ["DEFAULT_IGNORED_TOP_LEVEL_KEYS", "join_path", "null_paths", "stripped_fields"]

#: Top-level workflow keys that belong to the platform and are never compared.
DEFAULT_IGNORED_TOP_LEVEL_KEYS: frozenset[str] = frozenset({"agent_config", "nodePositions"})

_PLAIN_KEY = re.compile(r"^[A-Za-z0-9_\-]+$")
_MISSING = object()


def join_path(parent: str, key: str | int) -> str:
    """Append one key or list index to a JSON path.

    >>> join_path("nodes", "identity")
    'nodes.identity'
    >>> join_path("nodes.identity.tools", 0)
    'nodes.identity.tools[0]'
    >>> join_path("nodes", "odd.key")
    'nodes["odd.key"]'
    """
    if isinstance(key, int):
        return f"{parent}[{key}]"
    if _PLAIN_KEY.match(key):
        return f"{parent}.{key}" if parent else key
    return f"{parent}[{json.dumps(key)}]"


def stripped_fields(
    sent: Mapping[str, Any],
    stored: Mapping[str, Any] | None,
    *,
    ignore_top_level: Iterable[str] = DEFAULT_IGNORED_TOP_LEVEL_KEYS,
    ignore_empty: bool = True,
) -> list[str]:
    """Return the JSON paths that are present in ``sent`` but missing or changed in ``stored``.

    Args:
        sent: The workflow definition the builder sent to Freya.
        stored: The definition read back from Freya (the ``workflow`` object of ``GET /api/v2/workflow/{id}``).
            ``None`` means nothing was stored, so every sent top-level key is reported.
        ignore_top_level: Top-level keys that are never compared (default: ``agent_config``, ``nodePositions``).
        ignore_empty: Do not report empty sent values (``""``, ``[]``, ``{}``) that are absent or ``null`` in
            the stored copy.

    Returns:
        Sorted-by-discovery list of paths (depth-first, in the sent document's key order). A path is reported at
        the deepest level where the difference starts: a dropped key is reported once, not once per descendant.
    """
    ignored = frozenset(ignore_top_level)
    found: list[str] = []
    stored_root: Mapping[str, Any] = stored if isinstance(stored, Mapping) else {}
    for key, value in sent.items():
        if key in ignored:
            continue
        _compare(value, stored_root.get(key, _MISSING), join_path("", key), found, ignore_empty)
    return found


def null_paths(document: Any, path: str = "") -> list[str]:
    """Return the JSON path of every ``null`` (``None``) value in ``document``.

    Freya's workflow schema is strict and rejects ``null`` anywhere, so the client checks a workflow with this
    function before sending it. The root itself is reported as ``"$"`` when it is ``None``.
    """
    if document is None:
        return [path or "$"]
    paths: list[str] = []
    if isinstance(document, Mapping):
        for key, value in document.items():
            paths.extend(null_paths(value, join_path(path, str(key))))
    elif _is_list(document):
        for index, value in enumerate(document):
            paths.extend(null_paths(value, join_path(path, index)))
    return paths


def _compare(sent: Any, stored: Any, path: str, found: list[str], ignore_empty: bool) -> None:
    """Recursive worker of :func:`stripped_fields`; appends differing paths to ``found``."""
    if stored is _MISSING or stored is None:
        if sent is None or (ignore_empty and _is_empty(sent)):
            return
        found.append(path)
        return
    if isinstance(sent, Mapping):
        if not isinstance(stored, Mapping):
            found.append(path)
            return
        for key, value in sent.items():
            _compare(value, stored.get(key, _MISSING), join_path(path, str(key)), found, ignore_empty)
        return
    if _is_list(sent):
        if not _is_list(stored):
            found.append(path)
            return
        _compare_lists(sent, stored, path, found, ignore_empty)
        return
    if not _scalar_equal(sent, stored):
        found.append(path)


def _compare_lists(
    sent: Sequence[Any], stored: Sequence[Any], path: str, found: list[str], ignore_empty: bool
) -> None:
    """Compare two lists: scalars as a multiset, anything else position by position."""
    if all(_is_scalar(item) for item in sent):
        remaining = list(stored)
        for index, item in enumerate(sent):
            match = next((i for i, other in enumerate(remaining) if _scalar_equal(item, other)), None)
            if match is None:
                found.append(join_path(path, index))
            else:
                del remaining[match]
        return
    for index, item in enumerate(sent):
        other = stored[index] if index < len(stored) else _MISSING
        _compare(item, other, join_path(path, index), found, ignore_empty)


def _scalar_equal(left: Any, right: Any) -> bool:
    """Equality that keeps ``True`` distinct from ``1`` while treating ``2`` and ``2.0`` as equal."""
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left is right
    return bool(left == right)


def _is_list(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray)


def _is_scalar(value: Any) -> bool:
    return value is None or isinstance(value, str | int | float | bool)


def _is_empty(value: Any) -> bool:
    return (isinstance(value, str) and value == "") or (
        (isinstance(value, Mapping) or _is_list(value)) and len(value) == 0
    )
