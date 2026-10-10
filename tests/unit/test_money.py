"""Tests for builder.policy.money: cent-exact parsing, formatting and the reference amount check.

Amount vectors come from the practice cases (PUB-06: past due 2125.10, floor 637.50) and the edge cases of
docs/research/13_brief_rule_mechanism_map.md section 7.11 (amount above the full amount, floor above the past-due
amount, missing floor).

The input contract is ONE contract for two implementations: the Decimal helper (this module under test) and the Python the
compiler generates (``builder.compiler.pycode.MONEY_FUNCS``, which uses string and integer arithmetic only). Literal
vectors pin the contract down; a differential test then runs both on a wide grid of inputs, so neither can drift.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from builder.checks.code_sandbox import run_snippet
from builder.compiler.pycode import MONEY_FUNCS
from builder.policy.ir import AmountRules
from builder.policy.money import (
    amount_equal,
    check_promise_amount,
    effective_floor,
    fmt_amount,
    in_range,
    spoken_amount,
    to_money,
)


@pytest.mark.parametrize(("raw", "expected"), [
    (637.5, "637.50"),
    (2125.1, "2125.10"),
    (420, "420.00"),
    ("637.5", "637.50"),
    ("637.50", "637.50"),
    (Decimal("1250"), "1250.00"),
    ("$1,250", "1250.00"),
    ("1,250.75", "1250.75"),
    ("1.250,50", "1250.50"),
    ("637,50", "637.50"),
    ("637,5", "637.50"),
    ("1,250", "1250.00"),
    ("1,250,000", "1250000.00"),
    ("1.250.000", "1250000.00"),
    ("637.50 dollars", "637.50"),
    ("  420  ", "420.00"),
    ("-5", "-5.00"),
    ("0.125", "0.13"),  # half up
    (0.125, "0.13"),
    (1.005, "1.01"),  # repr-based: no binary-float artefact
    (2.675, "2.68"),
    ("1.005", "1.01"),
    ("0.125", "0.13"),
    ("2125.1", "2125.10"),
    ("1,250.00", "1250.00"),
    ("420 dollars", "420.00"),
    ("$1,250", "1250.00"),
    ("USD 1 250,50", "1250.50"),
    (".5", "0.50"),
    ("5.", "5.00"),
    ("-5,25", "-5.25"),
    ("-0", "0.00"),  # no negative zero
    ("0.004", "0.00"),
    ("0.0049999", "0.00"),
    ("0.005", "0.01"),
    ("-0.005", "-0.01"),  # half up = away from zero
    (1e-05, "0.00"),
    (5e-05, "0.00"),
    ("1,2,3", "123.00"),
    ("1.2.3", "123.00"),
])
def test_to_money(raw: object, expected: str) -> None:
    assert to_money(raw) == Decimal(expected)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", [True, False, float("nan"), float("inf"), "abc", "", "1-2", "--", Decimal("NaN"),
                                 "5-", "1,234.567,89", "\u0663\u0664\u0660", "dollars", ",", ".", "-"])
def test_to_money_rejects_non_amounts(bad: object) -> None:
    with pytest.raises(ValueError):
        to_money(bad)  # type: ignore[arg-type]


def test_formatting() -> None:
    assert fmt_amount(637.5) == "637.50"
    assert fmt_amount(2125.1) == "2125.10"
    assert fmt_amount(2125.1, thousands=True) == "2,125.10"
    assert fmt_amount(1250) == "1250.00"
    assert spoken_amount(1250) == "1,250.00"
    assert spoken_amount(637.5, "") == "637.50"
    assert spoken_amount(637.5, "euros") == "637.50 euros"
    assert spoken_amount(637.5, "  ") == "637.50"
    assert spoken_amount(2125.1, "pounds", thousands=False) == "2125.10 pounds"


def test_amount_equal_and_in_range() -> None:
    assert amount_equal(637.5, "637.50")
    assert amount_equal(2125.1, Decimal("2125.10"))
    assert not amount_equal(637.5, 637.51)
    assert in_range(126, 126, 420)
    assert in_range(420.0, 126, "420")
    assert not in_range(125.99, 126, 420)
    assert not in_range(420.01, 126, 420)
    assert not in_range(126, 126, 420, inclusive=False)
    assert in_range(200, 126, 420, inclusive=False)


def test_effective_floor() -> None:
    assert effective_floor(2125.1, 637.5) == Decimal("637.50")
    assert effective_floor(20, 25) == Decimal("20.00"), "the floor is never above the full amount"
    assert effective_floor(300, None) is None


@pytest.mark.parametrize(("amount", "full", "floor", "status"), [
    (2125.10, 2125.10, 637.50, "ok"),  # full amount
    (1000, 2125.10, 637.50, "ok"),  # inside the range
    (637.50, 2125.10, 637.50, "ok"),  # floor bound included
    (637.49, 2125.10, 637.50, "below_floor"),
    (500, 2125.10, 637.50, "below_floor"),
    (2200, 2125.10, 637.50, "above_full"),
    (2125.11, 2125.10, 637.50, "above_full"),
    (0, 2125.10, 637.50, "below_floor"),
    (-10, 2125.10, 637.50, "below_floor"),
    (20, 20, 25, "ok"),  # floor capped at the full amount
    (300, 300, None, "ok"),  # no floor: only the full amount
    (299.99, 300, None, "below_floor"),
    ("420", 420.0, 126.0, "ok"),
])
def test_check_promise_amount(amount: object, full: object, floor: object, status: str) -> None:
    assert check_promise_amount(amount, full, floor) == status  # type: ignore[arg-type]


def test_check_promise_amount_without_partial_amounts() -> None:
    rules = AmountRules(floor_field_by_type={}, accept_between_floor_and_full=False)
    assert check_promise_amount(1000, 2125.10, 637.50, rules) == "not_allowed"
    assert check_promise_amount(637.50, 2125.10, 637.50, rules) == "ok"
    assert check_promise_amount(2125.10, 2125.10, 637.50, rules) == "ok"
    assert check_promise_amount(1000, 2125.10, 637.50, AmountRules(floor_field_by_type={})) == "ok"


# --------------------------------------------------------------------------------------------------------------------
# One contract, two implementations: the Decimal helper and the generated integer-cent code
# --------------------------------------------------------------------------------------------------------------------

CONTRACT_INPUTS: list[object] = [
    # numbers
    0, 1, 420, -7, 1.005, 0.125, 2125.1, 637.5, 2.675, 1.015, 0.995, 1e-05, 5e-05, 1e22, 99.999, 0.1 + 0.2,
    # text, plain
    "1.005", "0.125", "2125.1", "2125.10", "637.5", "420", "0", "00", "1.0049", "1.0051", "0.994", "0.995", "0.999",
    # separators
    "1,250.00", "1,250", "1,250,000", "1.250.000", "1.250,50", "637,50", "637,5", "637,505", "1,2", "12,345", "1.250",
    "1 250,50", "1 250.50", "1'250.50",
    # currency words and symbols
    "$1,250", "420 dollars", "637.50 dollars", "USD 99.5", "EUR 1.234,56", "about 420.", "  420  ", "x420y",
    # signs
    "-5", "-5,25", "- 5", "-0", "-0.005", "5-", "1-2", "--", "+5",
    # not amounts
    "", "abc", ",", ".", "-", ",,", "..", "1.234567.89", "1,234.567,89", "1,23,45", "٣٤٠", "1e5", "NaN",
    "Infinity", float("nan"), float("inf"), True, False, None,
]


def _generated_cents(values: list[object]) -> list[int | None]:
    code = "import json\n" + MONEY_FUNCS + "vars['out'] = [to_cents(x) for x in vars['inputs']]\n"
    return run_snippet(code, {"inputs": values})["out"]


def _helper_cents(value: object) -> int | None:
    try:
        return int(to_money(value) * 100)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return None


def test_the_generated_money_code_agrees_with_the_helper_on_the_whole_grid() -> None:
    got = _generated_cents(CONTRACT_INPUTS)
    for value, generated in zip(CONTRACT_INPUTS, got, strict=True):
        assert generated == _helper_cents(value), repr(value)


@pytest.mark.parametrize(("raw", "cents"), [
    (1.005, 101), (0.125, 13), (2125.1, 212510), ("1,250.00", 125000), ("637,50", 63750), ("420 dollars", 42000),
    ("$1,250", 125000), ("1.005", 101), ("0.994", 99), ("0.995", 100), (-0.005, -1), ("-0", 0), ("1-2", None),
    ("1,234.567,89", None), ("1.234567.89", 12345678900), ("٣٤٠", None), (None, None), (True, None), ("lots", None),
])
def test_the_generated_money_code_gives_literal_cents(raw: object, cents: int | None) -> None:
    assert _generated_cents([raw]) == [cents]


def test_the_generated_money_code_formats_cents_with_two_decimals() -> None:
    code = "import json\n" + MONEY_FUNCS + "vars['out'] = [fmt_cents(c) for c in vars['cents']]\n"
    cents = [0, 1, 5, 10, 101, 63750, 212510, 125000, -1, -150]
    out = run_snippet(code, {"cents": cents})["out"]
    assert out == ["0.00", "0.01", "0.05", "0.10", "1.01", "637.50", "2125.10", "1250.00", "-0.01", "-1.50"]
    assert [fmt_amount(Decimal(c) / 100) for c in cents] == out


def test_the_generated_money_code_uses_no_float_arithmetic() -> None:
    """Parsing and rounding are string / integer work: no float(), round() or ``%.2f`` rounding in the shared body."""
    for needle in ("float(", "round(", "Decimal", "%.2f", "100.0"):
        assert needle not in MONEY_FUNCS, needle
    assert "'%.6f' % x" in MONEY_FUNCS, "only a float written in exponent form is turned into text first"


# Amounts around the half-cent boundary: the helper and the generated checks must give the same verdict.
BOUNDARY_AMOUNTS: list[object] = [0.994, 0.995, 1.004, 1.005, "1.0049", "1.0051", 1.01, 1.0149, 1.015, "1,005", "$1.015",
                                  "0.9949999", "1,01"]
BOUNDARY_FULLS: list[object] = [1.01, 1.005, "1.014", 1.015, "1.00"]
BOUNDARY_FLOORS: list[object | None] = [None, 1.005, 0.995, "0.994", 1.01, 1.02]


def test_floor_and_full_comparisons_at_the_half_cent_boundary_agree() -> None:
    cases = [(a, f, lo) for a in BOUNDARY_AMOUNTS for f in BOUNDARY_FULLS for lo in BOUNDARY_FLOORS]
    assert len(cases) > 300
    expected = [check_promise_amount(a, f, lo) for a, f, lo in cases]  # type: ignore[arg-type]
    code = (
        "import json\n" + MONEY_FUNCS
        + "out = []\n"
        + "for a, f, lo in vars['cases']:\n"
        + "    cents, full, floor = to_cents(a), to_cents(f), to_cents(lo)\n"
        + "    floor = min(floor, full) if floor is not None else None\n"
        + "    low = floor if floor is not None else full\n"
        + "    if cents > full:\n"
        + "        out.append('above_full')\n"
        + "    elif cents <= 0 or cents < low:\n"
        + "        out.append('below_floor')\n"
        + "    else:\n"
        + "        out.append('ok')\n"
        + "vars['out'] = out\n"
    )
    got = run_snippet(code, {"cases": [list(c) for c in cases]})["out"]
    for case, want, have in zip(cases, expected, got, strict=True):
        assert have == want, case


@pytest.mark.parametrize(("amount", "full", "floor", "status"), [
    (1.005, 1.01, 1.01, "ok"),  # the audit case: 1.005 is 1.01
    (1.004, 1.01, 1.01, "below_floor"),
    (1.0149, 1.01, 1.01, "ok"),
    (1.015, 1.01, 1.01, "above_full"),  # 1.015 is 1.02
    (0.995, 1.01, 0.995, "ok"),  # the floor 0.995 is 1.00; the amount 0.995 is 1.00
    (0.994, 1.01, 0.995, "below_floor"),
    (1.005, 1.005, None, "ok"),  # full 1.005 is 1.01, no floor: only the full amount
    (1.004, 1.005, None, "below_floor"),
])
def test_half_cent_boundary_verdicts_are_literal(amount: object, full: object, floor: object, status: str) -> None:
    assert check_promise_amount(amount, full, floor) == status  # type: ignore[arg-type]
