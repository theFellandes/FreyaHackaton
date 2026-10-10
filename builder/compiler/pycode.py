"""Helpers for writing the Python that the compiler embeds in a workflow (code nodes and tool hooks).

The platform's code rules (``docs/tools-and-code.md``): never ``{{`` or ``}}`` in code (the engine may treat them as
placeholders, and a nested dict literal ends in ``}}``), read every value from ``vars``, never raise. The generators in
this package therefore:

* embed *data* (holidays, naming templates, outcome tables ...) as a JSON text inside a Python string literal whose
  braces are written as ``\\x7b`` / ``\\x7d`` (:func:`json_literal`), so the source has no braces from data at all;
* avoid dict/set literals in code and build dicts with ``dict()``;
* check the finished source with :func:`assert_safe_code`.
"""

from __future__ import annotations

import ast
import json
from typing import Any

__all__ = [
    "MONEY_BODY",
    "MONEY_FUNCS",
    "assert_safe_code",
    "compact",
    "json_literal",
    "py_literal",
    "strip_code",
]


# One input contract for amounts, shared with ``builder.policy.money.to_money`` (the Decimal reference; its module docstring
# states the contract): an int is whole units, a float is read through its shortest text form; in text only
# ``0-9 , . -`` are kept (currency words, symbols and spaces drop out) and a ``-`` is a sign only as the first kept
# character; with both "," and "." the last one is the decimal mark; a single "," followed by one or two digits is a
# decimal comma, any other "," is a thousands separator; several "." are thousands separators; what is left must be
# digits with at most one "."; the result is rounded half-up to whole cents with string / integer arithmetic only (no
# float rounding: 1.005 -> 1.01, 0.125 -> 0.13). ``MONEY_BODY`` turns the value ``x`` into ``c`` (cents, or ``None``) and
# needs no ``def``; ``MONEY_FUNCS`` wraps it in ``to_cents`` / ``fmt_cents`` for the code nodes.
MONEY_BODY = """c = None
if x is not None and not isinstance(x, bool):
    if isinstance(x, int):
        c = x * 100
    else:
        s = repr(x) if isinstance(x, float) else str(x)
        if isinstance(x, float) and ('e' in s or 'E' in s):
            s = '%.6f' % x
        keep = ''.join(ch for ch in s if ch in '0123456789.,-')
        neg = keep.startswith('-')
        if neg:
            keep = keep[1:]
        if '-' not in keep and any(ch in '0123456789' for ch in keep):
            if ',' in keep and '.' in keep:
                if keep.rfind(',') > keep.rfind('.'):
                    keep = keep.replace('.', '').replace(',', '.')
                else:
                    keep = keep.replace(',', '')
            elif ',' in keep:
                tail = keep.rsplit(',', 1)[1]
                if keep.count(',') == 1 and 1 <= len(tail) <= 2:
                    keep = keep.replace(',', '.')
                else:
                    keep = keep.replace(',', '')
            elif keep.count('.') > 1:
                keep = keep.replace('.', '')
            if ',' not in keep and keep.count('.') <= 1:
                whole, dot, frac = keep.partition('.')
                c = int(whole or '0') * 100 + int((frac + '00')[:2])
                if len(frac) > 2 and frac[2] >= '5':
                    c += 1
                if neg:
                    c = -c
"""

MONEY_FUNCS = (
    "def to_cents(x):\n"
    + "".join("    " + line + "\n" if line else "\n" for line in MONEY_BODY.splitlines())
    + "    return c\n"
    + "def fmt_cents(c):\n"
    + "    return ('-' if c < 0 else '') + str(abs(c) // 100) + '.' + ('0' + str(abs(c) % 100))[-2:]\n"
)


def py_literal(text: str) -> str:
    """A Python string literal for ``text`` in which ``{`` and ``}`` are written as ``\\x7b`` and ``\\x7d``."""
    return repr(text).replace("{", "\\x7b").replace("}", "\\x7d")


def compact(value: Any) -> Any:
    """Make ``value`` free of ``None``: dict entries that are ``None`` are dropped, ``None`` list items become ``""``.

    The generated code reads optional entries with ``.get(...)`` and treats ``""`` as "none", so the workflow JSON never
    contains the text ``null`` - not even inside an embedded code string.
    """
    if isinstance(value, dict):
        return {key: compact(item) for key, item in value.items() if item is not None}
    if isinstance(value, list | tuple):
        return ["" if item is None else compact(item) for item in value]
    return value


def json_literal(value: Any) -> str:
    """A Python expression that evaluates to ``value`` (see :func:`compact`) via ``json.loads`` of a brace-free literal.

    The generated code must ``import json`` before using it.
    """
    text = json.dumps(compact(value), ensure_ascii=True, separators=(",", ":"), sort_keys=False)
    return f"json.loads({py_literal(text)})"


def strip_code(code: str) -> str:
    """Dedent-free cleanup: strip leading blank lines and trailing whitespace, end with exactly one newline."""
    lines = code.strip("\n").rstrip().splitlines()
    return "\n".join(line.rstrip() for line in lines) + "\n"


def assert_safe_code(code: str, what: str = "snippet") -> str:
    """Return ``code`` after checking that it compiles and contains no ``{{`` or ``}}``.

    Raises:
        ValueError: With ``what`` in the message when the code breaks one of the two rules.
    """
    if "{{" in code or "}}" in code:
        raise ValueError(f"{what}: generated code contains a double brace")
    try:
        ast.parse(code)
    except SyntaxError as exc:  # pragma: no cover - a generator bug, surfaced by the unit tests
        raise ValueError(f"{what}: generated code does not compile: {exc}") from exc
    return code
