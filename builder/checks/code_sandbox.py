"""Run the Python a workflow embeds, locally, the way the platform's code sandbox does.

The platform runs code-node snippets and tool hooks in its own sandbox (``json``, ``re`` and ``datetime`` are
documented as importable; nothing else is). This module mimics that so unit tests and the static checks can *execute*
the generated code:

* :func:`run_snippet` - a code-node snippet: ``vars`` in, ``vars`` out;
* :func:`run_hook` - a tool hook: ``vars``, ``config`` (pre) and ``result`` (post) in, the mutated objects out;
* :func:`extract_code_snippets` - every code string of a workflow, keyed by node or tool.

The sandbox is a *test double*, not a security boundary: it restricts builtins and imports and enforces a time limit
with a trace function (so a runaway loop raises :class:`SnippetError` instead of hanging), but it is only meant for code
the compiler generated itself.
"""

from __future__ import annotations

import builtins
import copy
import sys
import threading
import time
from typing import Any

__all__ = [
    "ALLOWED_MODULES",
    "SnippetError",
    "extract_code_snippets",
    "run_hook",
    "run_snippet",
]

ALLOWED_MODULES = frozenset({"json", "re", "datetime", "math"})

_SAFE_BUILTIN_NAMES = ["abs", "all", "any", "bool", "bytes", "chr", "dict", "divmod", "enumerate", "filter", "float", "format", "frozenset", "getattr", "hasattr", "hash", "int", "isinstance", "issubclass", "iter", "len", "list", "map", "max", "min", "next", "ord", "pow", "range", "repr", "reversed", "round", "set", "slice", "sorted", "str", "sum", "tuple", "zip", "print", "callable", "ArithmeticError", "AssertionError", "AttributeError", "BaseException", "Exception", "IndexError", "KeyError", "LookupError", "NameError", "OverflowError", "RuntimeError", "StopIteration", "TypeError", "ValueError", "ZeroDivisionError", "True", "False", "None", "NotImplemented", "Ellipsis"]


class SnippetError(RuntimeError):
    """The snippet raised, timed out, or tried to use something the sandbox forbids."""


def _restricted_import(name: str, globals_: Any = None, locals_: Any = None, fromlist: Any = (), level: int = 0) -> Any:
    root = name.split(".")[0]
    if level != 0 or root not in ALLOWED_MODULES:
        raise ImportError(f"import of {name!r} is not allowed in the sandbox (allowed: {sorted(ALLOWED_MODULES)})")
    return builtins.__import__(name, globals_, locals_, fromlist, level)


def _safe_builtins() -> dict[str, Any]:
    safe = {name: getattr(builtins, name) for name in _SAFE_BUILTIN_NAMES if hasattr(builtins, name)}
    safe["__import__"] = _restricted_import
    safe["__build_class__"] = builtins.__build_class__
    safe["__name__"] = "snippet"
    return safe


def _execute(code: str, namespace: dict[str, Any], timeout_s: float, label: str) -> None:
    """Compile and run ``code`` in ``namespace`` with a wall-clock limit enforced by a trace function."""
    try:
        compiled = compile(code, f"<{label}>", "exec")
    except SyntaxError as exc:
        raise SnippetError(f"{label}: syntax error: {exc}") from exc
    outcome: dict[str, BaseException | None] = {"error": None}
    deadline = time.monotonic() + timeout_s

    def tracer(frame: Any, event: str, arg: Any) -> Any:
        if time.monotonic() > deadline:
            raise TimeoutError(f"{label}: exceeded {timeout_s} s")
        return tracer

    def target() -> None:
        sys.settrace(tracer)
        try:
            exec(compiled, namespace)
        except BaseException as exc:
            outcome["error"] = exc
        finally:
            sys.settrace(None)

    worker = threading.Thread(target=target, daemon=True)
    worker.start()
    worker.join(timeout_s + 1.0)
    if worker.is_alive():
        raise SnippetError(f"{label}: did not finish within {timeout_s} s")
    error = outcome["error"]
    if error is not None:
        raise SnippetError(f"{label}: {type(error).__name__}: {error}") from error


def run_snippet(code: str, vars: dict[str, Any], timeout_s: float = 5.0) -> dict[str, Any]:
    """Execute a code-node snippet with a copy of ``vars`` and return the resulting variables.

    The caller's dict is not modified. Values the snippet writes are returned as they are (strings, numbers ...).

    Raises:
        SnippetError: On a syntax error, an exception inside the snippet, a forbidden import or a timeout. Generated
            snippets are written never to raise, so any error is a compiler bug.
    """
    working = copy.deepcopy(vars)
    namespace: dict[str, Any] = {"__builtins__": _safe_builtins(), "vars": working}
    _execute(code, namespace, timeout_s, "snippet")
    return working


def run_hook(
    code: str,
    *,
    vars: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
    timeout_s: float = 5.0,
) -> dict[str, Any]:
    """Execute a pre- or post-hook on copies of its inputs and return ``{"vars", "config", "result"}`` after the run.

    A pre-hook sees ``vars`` and ``config`` (``url``, ``method``, ``headers``, ``body``); a post-hook sees ``vars`` and
    ``result`` (the ``{"status", "data", ...}`` envelope). Arguments that are not given are passed as empty dicts.
    """
    working = {
        "vars": copy.deepcopy(vars or {}),
        "config": copy.deepcopy(config or {}),
        "result": copy.deepcopy(result or {}),
    }
    namespace: dict[str, Any] = {"__builtins__": _safe_builtins(), **working}
    _execute(code, namespace, timeout_s, "hook")
    return working


def extract_code_snippets(wf: dict[str, Any]) -> dict[str, str]:
    """Every Python string of a workflow dict, keyed by where it lives.

    Keys: ``<node>`` for a code node's ``code_exec`` snippet; ``<node>/<tool>.pre_process_code`` and
    ``<node>/<tool>.post_process_code`` for hooks on a conversation node's tools; ``tools/<tool>.<hook>`` for
    workflow-level tools; ``<node>/<tool>.code`` for a ``code_exec`` pre-action on a non-code node.
    """
    snippets: dict[str, str] = {}

    def add_tool(prefix: str, tool: dict[str, Any]) -> None:
        function = tool.get("function") or {}
        name = str(function.get("name", "?"))
        for hook in ("pre_process_code", "post_process_code"):
            code = tool.get(hook)
            if isinstance(code, str) and code.strip():
                snippets[f"{prefix}/{name}.{hook}"] = code
        if function.get("handler") == "__function__:code_exec":
            default = (((function.get("parameters") or {}).get("properties") or {}).get("code") or {}).get("default")
            if isinstance(default, str) and default.strip():
                snippets[prefix if name == "code_exec" else f"{prefix}/{name}.code"] = default

    nodes = {**(wf.get("nodes") or {}), **(wf.get("global_nodes") or {})}
    for node_name, node in nodes.items():
        if not isinstance(node, dict):
            continue
        for tool in node.get("pre_actions") or []:
            if isinstance(tool, dict):
                add_tool(node_name, tool)
        for tool in node.get("tools") or []:
            if isinstance(tool, dict):
                add_tool(node_name, tool)
    for tool in wf.get("tools") or []:
        if isinstance(tool, dict):
            add_tool("tools", tool)
    return snippets
