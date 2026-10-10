"""Money helpers: exact cent arithmetic, formatting and the reference amount check.

Amounts arrive as JSON numbers from ``get_account`` (``637.5``), as strings typed by the model, or as numbers the
customer said. Everything is converted to :class:`~decimal.Decimal` rounded to cents (half up), so ``2125.1`` and
``"2125.10"`` compare equal and no binary-float artefacts reach a prompt.

The input contract (one for this module AND for the code the compiler generates, ``builder.compiler.pycode.MONEY_BODY``;
``tests/unit/test_money.py`` and ``test_codegen.py`` run both on the same vectors):

* an ``int`` is whole currency units; a ``float`` is read through its shortest text form (``repr``), so ``1.005`` is
  the amount 1.005 and not its binary neighbour; booleans, NaN and infinity are not amounts;
* in text only the characters ``0-9 , . -`` are kept (currency words and symbols, spaces and letters are dropped);
  a ``-`` is a sign only as the first kept character, anywhere else the text is not an amount;
* with both ``,`` and ``.`` the last one is the decimal mark and the other one a thousands separator; a lone ``,``
  followed by one or two digits (and used once) is a decimal comma, any other ``,`` is a thousands separator; several
  ``.`` are thousands separators; what is left must be digits with at most one ``.``;
* the result is rounded half up to whole cents (``1.005`` -> ``1.01``, ``0.125`` -> ``0.13``); the generated code does
  this with string and integer arithmetic, here with ``ROUND_HALF_UP``.

:func:`check_promise_amount` is the reference for the compiler's amount validation snippet: the full past-due amount
first, any amount from the floor up to the full amount accepted, nothing above the full amount, the floor never
higher than the full amount. Floors come from the IR's :class:`~builder.policy.ir.AmountRules`; nothing here knows a
particular product or bank.
"""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Literal

from builder.policy.ir import AmountRules

__all__ = [
    "AmountStatus",
    "MoneyLike",
    "amount_equal",
    "check_promise_amount",
    "effective_floor",
    "fmt_amount",
    "in_range",
    "spoken_amount",
    "to_money",
]

MoneyLike = Decimal | int | float | str
"""Accepted amount inputs."""

AmountStatus = Literal["ok", "below_floor", "above_full", "not_allowed"]

_CENT = Decimal("0.01")
_NOT_NUMERIC = re.compile(r"[^0-9,.\-]")
_PLAIN_NUMBER = re.compile(r"[0-9]*\.?[0-9]*")


def to_money(value: MoneyLike) -> Decimal:
    """Convert an amount to a ``Decimal`` rounded to cents (half up), following the module's input contract.

    Strings may carry currency symbols or words and thousands separators: ``"$1,250"``, ``"1.250,50"`` and
    ``"637,50"`` are understood. With both ``,`` and ``.`` present the last one is the decimal mark; a single ``,``
    followed by one or two digits is a decimal comma; a single ``.`` is always a decimal point.
    Raises ``ValueError`` for booleans, NaN/infinity and unreadable text.
    """
    if isinstance(value, bool):
        raise ValueError("a boolean is not an amount")
    if isinstance(value, Decimal):
        number = value
    elif isinstance(value, int):
        number = Decimal(value)
    elif isinstance(value, float):
        number = Decimal(repr(value))
    else:
        number = _parse_amount_text(str(value))
    if not number.is_finite():
        raise ValueError(f"not a finite amount: {value!r}")
    cents = number.quantize(_CENT, rounding=ROUND_HALF_UP)
    return cents if cents != 0 else Decimal("0.00")  # no negative zero


def _parse_amount_text(text: str) -> Decimal:
    cleaned = _NOT_NUMERIC.sub("", text.strip())
    negative = cleaned.startswith("-")
    body = cleaned[1:] if negative else cleaned
    if "-" in body or not any(ch.isdigit() for ch in body):
        raise ValueError(f"no amount in {text!r}")
    if "," in body and "." in body:
        decimal_mark = "," if body.rfind(",") > body.rfind(".") else "."
        thousands = "." if decimal_mark == "," else ","
        body = body.replace(thousands, "").replace(decimal_mark, ".")
    elif "," in body:
        head, _, tail = body.rpartition(",")
        is_decimal_comma = body.count(",") == 1 and 1 <= len(tail) <= 2
        body = f"{head}.{tail}" if is_decimal_comma else body.replace(",", "")
    elif body.count(".") > 1:
        body = body.replace(".", "")
    if not _PLAIN_NUMBER.fullmatch(body) or "," in body:
        raise ValueError(f"cannot read an amount from {text!r}")
    try:
        return Decimal(("-" if negative else "") + body)
    except InvalidOperation as exc:  # pragma: no cover - the pattern above admits only readable numbers
        raise ValueError(f"cannot read an amount from {text!r}") from exc


def fmt_amount(value: MoneyLike, *, thousands: bool = False) -> str:
    """Format with exactly two decimals: ``637.5`` → ``'637.50'``; with ``thousands`` → ``'2,125.10'``."""
    amount = to_money(value)
    return f"{amount:,.2f}" if thousands else f"{amount:.2f}"


def spoken_amount(value: MoneyLike, currency_word: str = "", *, thousands: bool = True) -> str:
    """Amount as the agent says it: two decimals, thousands separators, then the currency word if the brief has one.

    ``spoken_amount(1250, "")`` → ``'1,250.00'``; ``spoken_amount(637.5, "euros")`` → ``'637.50 euros'``.
    """
    text = fmt_amount(value, thousands=thousands)
    word = (currency_word or "").strip()
    return f"{text} {word}" if word else text


def amount_equal(a: MoneyLike, b: MoneyLike) -> bool:
    """True when two amounts are equal to the cent."""
    return to_money(a) == to_money(b)


def in_range(value: MoneyLike, low: MoneyLike, high: MoneyLike, *, inclusive: bool = True) -> bool:
    """True when ``low <= value <= high`` (strict inequalities when ``inclusive`` is False), compared in cents."""
    amount, lo, hi = to_money(value), to_money(low), to_money(high)
    return lo <= amount <= hi if inclusive else lo < amount < hi


def effective_floor(full: MoneyLike, floor: MoneyLike | None) -> Decimal | None:
    """The floor actually usable for a product: never above the full amount; ``None`` when the product has none."""
    if floor is None:
        return None
    return min(to_money(floor), to_money(full))


def check_promise_amount(
    amount: MoneyLike,
    full: MoneyLike,
    floor: MoneyLike | None,
    rules: AmountRules | None = None,
) -> AmountStatus:
    """Check an amount the customer proposed or accepted against the product's full amount and floor.

    * ``above_full``: more than the past-due amount (offer the full amount instead).
    * ``below_floor``: below the effective floor, or not positive. Without a floor only the full amount is valid.
    * ``not_allowed``: strictly between floor and full when the rules do not accept partial amounts.
    * ``ok``: otherwise (the full amount and the floor itself are always ok).
    """
    value, total = to_money(amount), to_money(full)
    if value > total:
        return "above_full"
    lowest = effective_floor(total, floor)
    if value <= 0 or value < (lowest if lowest is not None else total):
        return "below_floor"
    if value in (total, lowest):
        return "ok"
    accept_between = True if rules is None else rules.accept_between_floor_and_full
    return "ok" if accept_between else "not_allowed"
