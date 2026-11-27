"""Read a CLICK Math formula in CLICK's order, and import it to pyrung source that agrees.

A CLICK 4.00 on a C2-03CPU was read running each formula of :data:`READINGS` on 2026-11-25
and 2026-11-26. Each case is named after the reading round and its case number. The values
read are ground truth. ``test_click_formula_parse_*`` checks the parse
against them with an evaluator of its own. ``test_imported_formula_*`` checks pyrung's three
outputs: the imported program (in-process and on the generated P1AM code) and its CLICK export.
"""

from __future__ import annotations

import itertools
import math
import re
import struct
import warnings
from collections.abc import Iterator
from typing import Any

import pytest

from pyrung import LiteralExpr
from pyrung.click import ladder_to_pyrung, pyrung_to_ladder
from pyrung.click.codegen import formula as formula_module
from pyrung.click.codegen.formula import (
    _CLICK_FUNC_TO_PYTHON,
    _RANGE_AGG_TO_METHOD,
    _TIER_OF,
    CONFIRMED_ORDERS,
    CONFIRMED_PAIRS,
    NEGATE_ADDRESS,
    NEGATE_BRACKET,
    NEGATE_CONSTANT,
    NEGATE_FUNCTION,
    NEGATE_MINUS,
    NEGATE_PI,
    NEGATE_PLUS,
    NEGATE_POINTER,
    PLUS_SIGN,
    READ_FORMULAS,
    SAME_VALUE_ON_PLAIN_OPERANDS,
    SAME_VALUE_PAIRS,
    Address,
    BinaryOp,
    Call,
    Negate,
    Number,
    Pi,
    Pointer,
    RangeCall,
    _scope_steps,
    parse_click_formula,
    parse_with_unconfirmed_pairs,
)
from pyrung.click.ladder.types import LadderBundle
from pyrung.conformance._formula import _as_subtraction
from pyrung.core import PLC, Dint, Int, Program, Real, Word, copy, rung, system
from pyrung.core import math as math_instruction
from pyrung.core.context import ScanContext
from pyrung.core.instruction.calc import CalcInstruction
from pyrung.core.state import SystemState
from tests.circuitpy.test_codegen import _on_every_backend
from tests.click.helpers import exec_with_source
from tests.conformance.math import formula_values
from tests.conformance.math.formula_values import (
    PENDING_ZERO_SIGNS,
    Unread,
    click_steps,
    click_store,
    float32,
    pyrung_expression,
    regroupings,
    unread_zero_signs,
)
from tests.conformance.math.formula_values import click_value as _click_value
from tests.conftest import execute

#: (case, formula as CLICK displays it, hex mode, register values, value the PLC read).
#: A hex value is the 16 bits a DH holds. A DF holds a float32. The b16 cases are cases of
#: pyrung's conformance groups, named after the case, and each reads one formula of it.
READINGS: list[tuple[str, str, bool, dict[str, float], float]] = [
    ("b10-1d", "2 + 3 * 4", False, {}, 14),
    ("b10-1h", "2h + 3h * 4h", True, {}, 0xE),
    ("b10-2d", "7 MOD 3 * 2", False, {}, 2),
    ("b10-2h", "7h MOD 3h * 2h", True, {}, 2),
    ("b10-3", "2 ^ 3 ^ 2", False, {}, 64),
    ("b10-4", "-2 ^ 2", False, {}, 4),
    ("b10-5", "6h AND 3h OR 8h", True, {}, 0xA),
    ("b10-6", "6h OR 3h AND 8h", True, {}, 0),
    ("b10-7", "5h XOR 3h AND 1h", True, {}, 0),
    ("b10-8d", "10 - 4 - 3", False, {}, 3),
    ("b10-8h", "10h - 4h - 3h", True, {}, 9),
    ("b10-9d", "100 / 10 / 5", False, {}, 2),
    ("b10-9h", "100h / 10h / 5h", True, {}, 3),
    ("b11-1", "2 * 3 ^ 2", False, {}, 18),
    ("b11-1x", "3 ^ 2 * 2", False, {}, 18),
    ("b11-2", "4h AND 4h XOR 1h", True, {}, 5),
    ("b11-3", "1h OR 1h XOR 1h", True, {}, 0),
    ("b11-3x", "1h XOR 1h OR 1h", True, {}, 1),
    ("b11-4", "2h AND 3h + 1h", True, {}, 3),
    ("b11-4x", "1h + 2h AND 2h", True, {}, 3),
    ("b11-5", "- DS1 ^ 2", False, {"DS1": 2}, 4),
    ("b11-6", "- DS1 ^ 2", False, {"DS1": -2}, 4),
    ("b11-5x", "- DS1 ^ 3", False, {"DS1": 2}, -8),
    ("b11-P1", "3h AND 3h * 2h", True, {}, 6),
    ("b11-P2", "2h * 3h AND 3h", True, {}, 2),
    ("b11-P3", "1h + 2h OR 1h", True, {}, 4),
    ("b11-P4", "1h + 3h XOR 1h", True, {}, 3),
    ("b11-P5", "3 * 7 MOD 4", False, {}, 1),
    ("b11-P6", "12 / 2 * 3", False, {}, 18),
    ("b11-P7", "18 / 3 ^ 2", False, {}, 2),
    ("b11-P8", "5 - 3 + 1", False, {}, 3),
    ("b12-1h", "2h * 3h / 4h", True, {}, 1),
    ("b12-1d", "12 / 4 * 3", False, {}, 9),
    ("b12-2a", "1 + 6 / 2", False, {}, 4),
    ("b12-2ar", "6 / 2 + 1", False, {}, 4),
    ("b12-2b", "1 + 7 MOD 4", False, {}, 4),
    ("b12-2br", "7 MOD 4 + 1", False, {}, 4),
    ("b12-2c", "10 - 7 MOD 4", False, {}, 7),
    ("b12-2cr", "7 MOD 4 - 1", False, {}, 2),
    ("b12-2d", "10 - 6 / 2", False, {}, 7),
    ("b12-2dr", "6 / 2 - 1", False, {}, 2),
    ("b12-3a", "2 ^ 3 MOD 3", False, {}, 2),
    ("b12-3b", "7 MOD 3 ^ 2", False, {}, 7),
    ("b12-3c", "1 + 2 ^ 2", False, {}, 5),
    ("b12-3d", "10 - 2 ^ 2", False, {}, 6),
    ("b12-3e", "2 ^ 2 + 1", False, {}, 5),
    ("b12-3fr", "2 ^ 2 - 1", False, {}, 3),
    ("b12-4a", "- DS1 * 3", False, {"DS1": 2}, -6),
    ("b12-4b", "- DS1 MOD 3", False, {"DS1": 2}, -2),
    ("b12-4m", "-7 MOD 3", False, {}, -1),
    ("b12-4c", "- DS1 + 5", False, {"DS1": 2}, 3),
    ("b12-4e", "- DS1 - 1", False, {"DS1": 2}, -3),
    ("b12-5h", "- DH100 AND 3h", True, {"DH100": 2}, 2),
    ("b12-5hb", "- DH100 AND 3h", True, {"DH100": 1}, 3),
    ("b14-1", "- DH100 OR 1h", True, {"DH100": 2}, 0xFFFF),
    ("b14-2", "- DH100 XOR 1h", True, {"DH100": 2}, 0xFFFF),
    ("b14-2b", "- DH100 XOR 1h", True, {"DH100": 3}, 0xFFFC),
    ("b14-3", "7h MOD 5h AND 3h", True, {}, 2),
    ("b14-3r", "3h AND 7h MOD 5h", True, {}, 3),
    ("b14-4", "6h / 2h OR 1h", True, {}, 3),
    ("b14-4r", "4h OR 6h / 2h", True, {}, 3),
    # b15 also read t3, m1 and m1b, which give the same value in either order. It read u6
    # too, which adds nothing to u5 and leaves open what a minus sign before LSH applies to.
    ("b15-t1", "7h / 2h * 2h", True, {}, 6),
    ("b15-t2", "7h / 2h * 2h + 0h", True, {}, 6),
    ("b15-p1", "10 - 2 * 3", False, {}, 4),
    ("b15-p1r", "2 * 3 - 1", False, {}, 5),
    ("b15-p2", "7h - 1h AND 3h", True, {}, 6),
    ("b15-p2r", "Ah AND 7h - 1h", True, {}, 1),
    ("b15-p3", "5h - 1h OR 2h", True, {}, 2),
    ("b15-p3r", "1h OR 5h - 1h", True, {}, 4),
    ("b15-p4", "5h - 1h XOR 3h", True, {}, 3),
    ("b15-p4r", "3h XOR 5h - 1h", True, {}, 5),
    ("b15-u1", "- ( DS1 ) ^ 2", False, {"DS1": 4}, 16),
    ("b15-u1x", "- ( DS1 ) ^ 3", False, {"DS1": 4}, -64),
    ("b15-u2", "- SQRT ( DS1 ) ^ 2", False, {"DS1": 4}, 4),
    ("b15-u2x", "- SQRT ( DS1 ) ^ 3", False, {"DS1": 4}, -8),
    ("b15-u3", "- ( DS1 ) + 2", False, {"DS1": 4}, -2),
    ("b15-u4", "- SQRT ( DS1 ) + 2", False, {"DS1": 4}, 0),
    ("b15-u5", "- ( DH100 ) AND 7h", True, {"DH100": 2}, 6),
    ("b15-m2", "DH103 * DH105 MOD DH104", True, {"DH103": 0x100, "DH105": 0x101, "DH104": 7}, 6),
    (
        "b15-m2b",
        "DH103 * DH105 MOD DH104",
        True,
        {"DH103": 0xFF38, "DH105": 0xFE70, "DH104": 7},
        5,
    ),
    # math_order's probes, typed with each constant on a register of its own.
    ("b16-div_mod", "DH271 / DH272 MOD DH273", True, {"DH271": 8, "DH272": 2, "DH273": 3}, 1),
    ("b16-div_modr", "DH274 MOD DH275 / DH272", True, {"DH274": 9, "DH275": 6, "DH272": 2}, 1),
    ("b16-div_and", "DH281 / DH282 AND DH283", True, {"DH281": 8, "DH282": 2, "DH283": 3}, 0),
    ("b16-div_andr", "DH284 AND DH285 / DH282", True, {"DH284": 6, "DH285": 7, "DH282": 2}, 3),
    ("b16-div_xor", "DH291 / DH292 XOR DH293", True, {"DH291": 8, "DH292": 2, "DH293": 1}, 5),
    ("b16-div_xorr", "DH293 XOR DH294 / DH292", True, {"DH293": 1, "DH294": 6, "DH292": 2}, 3),
    ("b16-mod_or", "DH301 MOD DH302 OR DH303", True, {"DH301": 7, "DH302": 4, "DH303": 1}, 3),
    ("b16-mod_orr", "DH302 OR DH301 MOD DH304", True, {"DH302": 4, "DH301": 7, "DH304": 5}, 2),
    ("b16-mod_xor", "DH311 MOD DH312 XOR DH313", True, {"DH311": 8, "DH312": 5, "DH313": 1}, 2),
    ("b16-mod_xorr", "DH313 XOR DH314 MOD DH312", True, {"DH313": 1, "DH314": 7, "DH312": 5}, 1),
    ("b16-mul_or", "DH321 * DH322 OR DH323", True, {"DH321": 2, "DH322": 3, "DH323": 1}, 7),
    ("b16-mul_orr", "DH323 OR DH321 * DH322", True, {"DH323": 1, "DH321": 2, "DH322": 3}, 9),
    ("b16-mul_xor", "DH331 * DH332 XOR DH333", True, {"DH331": 2, "DH332": 3, "DH333": 1}, 7),
    ("b16-mul_xorr", "DH333 XOR DH331 * DH332", True, {"DH333": 1, "DH331": 2, "DH332": 3}, 9),
    (
        "b16-mod_mod",
        "DS4241 MOD DS4242 MOD DS4243",
        False,
        {"DS4241": 7, "DS4242": 5, "DS4243": 3},
        2,
    ),
    ("b16-negb_sub", "- ( DS4271 ) - DS4272", False, {"DS4271": 2, "DS4272": 1}, -3),
    ("b16-negb_or", "- ( DH381 ) OR DH382", True, {"DH381": 2, "DH382": 1}, 0xFFFF),
    ("b16-negb_xor", "- ( DH391 ) XOR DH392", True, {"DH391": 3, "DH392": 1}, 0xFFFC),
    # math_order/doubles. A double drops the 1.0 from 1.0 + 1.0E+17, and rounds the product.
    (
        "b16-doubles1",
        "DF170 + DF171 - DF172",
        False,
        {"DF170": 1.0, "DF171": float32(1.0e17), "DF172": float32(1.0e17)},
        0,
    ),
    (
        "b16-doubles2",
        "( DD178 * DD179 * DD180 ) MOD DD185",
        False,
        {"DD178": 164999119, "DD179": 114975250, "DD180": 58, "DD185": 1000000},
        675584,
    ),
    (
        "b16-doubles3",
        "DF173 + DF174 + DF175",
        False,
        {"DF173": 1.0, "DF174": float32(1.0e17), "DF175": float32(-1.0e17)},
        0,
    ),
    # math_range/by_zero. Each stores +0.0 into a DF, so the minus sign applies first.
    ("b16-by_zero1", "- DF210 / DF212", False, {"DF210": 5.0, "DF212": 0.0}, 0.0),
    ("b16-by_zero2", "- DF210 MOD DF212", False, {"DF210": 5.0, "DF212": 0.0}, 0.0),
    # Round b20 typed each constant into the formula. CLICK stored the minus signs of Part A as
    # tokens of their own, such as -#UH2#AND#UH3.
    ("b20-a_and", "- 2h AND 3h", True, {}, 0x0002),
    ("b20-a_or", "- 2h OR 1h", True, {}, 0xFFFF),
    ("b20-a_xor", "- 3h XOR 1h", True, {}, 0xFFFC),
    ("b20-a_add", "- 5h + 1h", True, {}, 0xFFFC),
    ("b20-a_sub", "- 5h - 1h", True, {}, 0xFFFA),
    # Each stores +0.0 into a DF, so the minus sign applies first.
    ("b20-b_div", "- ( DF121 ) / DF122", False, {"DF121": 5.0, "DF122": 0.0}, 0.0),
    ("b20-b_mod", "- ( DF121 ) MOD DF122", False, {"DF121": 5.0, "DF122": 0.0}, 0.0),
    # CLICK stored the decimal plus in the constant, H+5, and the hex plus as a token of its own.
    ("b20-d_dec", "+ 5 * DS401", False, {"DS401": 3}, 15),
    ("b20-d_hex", "+ 5h * DH401", True, {"DH401": 3}, 0x000F),
    # Round b21. Each hex plus stored what the formula stores without it.
    ("b21-p1", "+ ( 0h - DH421 ) + 1h", True, {"DH421": 1}, 0x0000),
    ("b21-p2", "+ 0h - DH421 + 1h", True, {"DH421": 1}, 0x0000),
    ("b21-p3", "+ ( 0h - DH421 ) * 2h", True, {"DH421": 1}, 0xFFFE),
    ("b21-p2b", "+ 0h - DH422 + 1h", True, {"DH422": 2}, 0xFFFF),
    ("b21-p3b", "+ ( 0h - DH421 ) * 1h", True, {"DH421": 1}, 0xFFFF),
    # Round b22. Each formula with a plus stored the value of the formula without it.
    ("b22-b_ds", "+ DS401 * 2", False, {"DS401": 5}, 10),
    ("b22-b_br", "+ ( DS401 - 3 ) * 2", False, {"DS401": 5}, 4),
    ("b22-b_hex", "+ DH401 * 2h", True, {"DH401": 3}, 0x0006),
    # A hex / rounds down, and a hex MOD truncates. A product stays whole past 32 bits.
    ("b22-c_div2", "- 5h / 2h", True, {}, 0xFFFD),
    ("b22-c_div3", "- 5h / 3h", True, {}, 0xFFFE),
    ("b22-c_mulk", "- FFFFh * FF00h", True, {}, 0xFF00),
    ("b22-c_mulr", "- DH431 * DH432", True, {"DH431": 0xFFFF, "DH432": 0xFF00}, 0xFF00),
    ("b22-c_mulk7", "- FFFFh * FF00h MOD 7h", True, {}, 0xFFFB),
    ("b22-c_mulr7", "- DH431 * DH432 MOD 7h", True, {"DH431": 0xFFFF, "DH432": 0xFF00}, 0xFFFB),
    ("b22-c_div", "( 0h - DH421 ) / 3h", True, {"DH421": 1}, 0xFFFF),
    ("b22-c_mod", "( 0h - DH421 ) MOD 3h", True, {"DH421": 1}, 0xFFFF),
    # Round b23 divided by a value below 0.
    ("b23-d1", "5h / ( 0h - 3h )", True, {}, 0xFFFE),
    ("b23-d2", "( 0h - 7h ) / ( 0h - 2h )", True, {}, 0x0003),
    ("b23-m1", "5h MOD ( 0h - 3h )", True, {}, 0x0002),
    ("b23-m2", "( 0h - 7h ) MOD ( 0h - 2h )", True, {}, 0xFFFF),
    # Round b24. A product of two DHs stays whole, and a plus keeps the sign of -0.0.
    ("b24-w1", "DH1 * DH2 MOD 7h", True, {"DH1": 0xFFFF, "DH2": 0xFFFF}, 0x0001),
    ("b24-w2", "- DH1 * DH2 MOD 7h", True, {"DH1": 0xFFFF, "DH2": 0xFFFF}, 0xFFFF),
    ("b24-w3", "DH1 * DH2 * DH1 MOD FBh", True, {"DH1": 0xFFFF, "DH2": 0xFFFF}, 0x0013),
    ("b24-z1", "+ DF110", False, {"DF110": -0.0}, -0.0),
    ("b20-e_hex", "DH411 MOD DH412 MOD DH413", True, {"DH411": 7, "DH412": 5, "DH413": 3}, 2),
    ("b20-e_dec", "DS411 / DS412 MOD 3", False, {"DS411": 7, "DS412": 2}, 0.5),
]


# ---------------------------------------------------------------------------
# The parse
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "hex_mode", "registers", "value"),
    [reading[1:] for reading in READINGS],
    ids=[reading[0] for reading in READINGS],
)
def test_click_formula_parse_gives_what_the_plc_read(
    text: str, hex_mode: bool, registers: dict[str, int], value: float
) -> None:
    tree = parse_click_formula(text, hex_mode=hex_mode)
    assert tree is not None
    assert _same_value(_click_value(tree, hex_mode, registers), value)


def test_click_formula_parse_tree_shapes() -> None:
    ds1 = Address("DS", 1)
    assert parse_click_formula("6h OR 3h AND 8h", hex_mode=True) == BinaryOp(
        "AND", BinaryOp("OR", Number(6), Number(3)), Number(8)
    )
    assert parse_click_formula("2 ^ 3 ^ 2") == BinaryOp(
        "^", BinaryOp("^", Number(2), Number(3)), Number(2)
    )
    assert parse_click_formula("- DS1 ^ 2") == BinaryOp("^", Negate(ds1), Number(2))
    assert parse_click_formula("-2 ^ 2") == BinaryOp("^", Number(-2), Number(2))
    assert parse_click_formula("- 2 ^ 2") == BinaryOp("^", Number(-2), Number(2))
    assert parse_click_formula("DS1 + 2 * DS2") == BinaryOp(
        "+", ds1, BinaryOp("*", Number(2), Address("DS", 2))
    )
    assert parse_click_formula("- ( DS1 + 2 )") == Negate(BinaryOp("+", ds1, Number(2)))


def test_click_formula_parse_reads_functions_pointers_and_pi() -> None:
    assert parse_click_formula("SQRT ( DS1 ) + SUM ( DS1 : DS4 )") == BinaryOp(
        "+",
        Call("SQRT", (Address("DS", 1),)),
        RangeCall("SUM", Address("DS", 1), Address("DS", 4)),
    )
    assert parse_click_formula("LSH ( DH1 , 2 )", hex_mode=True) == Call(
        "LSH", (Address("DH", 1), Number(2))
    )
    assert parse_click_formula("DH[DS1] + 1") == BinaryOp(
        "+", Pointer("DH", Address("DS", 1)), Number(1)
    )
    assert parse_click_formula("SIN ( PI )") == Call("SIN", (Pi(),))
    assert parse_click_formula("RAND ( 1 , 100 )") == Call("RAND", (Number(1), Number(100)))
    assert parse_click_formula("ABS(DS50 - 5) * 2") == parse_click_formula("ABS ( DS50 - 5 ) * 2")


def test_click_formula_equality_ignores_brackets_and_spelling() -> None:
    bare = parse_click_formula("DS1 * 2 + 3")
    bracketed = parse_click_formula("( DS1 * 2 ) + 3")
    assert bare == bracketed
    assert hash(bare) == hash(bracketed)
    assert parse_click_formula("DS1 * ( 2 + 3 )") != bare
    assert parse_click_formula("X001 + 1") == parse_click_formula("X1 + 1")
    assert parse_click_formula("DH1 AND 00FFh", hex_mode=True) == parse_click_formula(
        "DH1 AND FFh", hex_mode=True
    )
    # CLICK's hex dialog reads typed digits as hex and adds the h.
    assert parse_click_formula("DH1 + 255", hex_mode=True) == parse_click_formula(
        "DH1 + 255h", hex_mode=True
    )
    assert parse_click_formula("DH1 + 255") != parse_click_formula("DH1 + 255h")
    # A hex constant ends in a lowercase h, so C1h is a constant, not an address.
    assert parse_click_formula("DH1 + C1h", hex_mode=True) == BinaryOp(
        "+", Address("DH", 1), Number(0xC1)
    )


@pytest.mark.parametrize("text", ["DS1 +", "len([])", "DS1.x", "FOO1 + 1", "DS1 # 2", "( DS1"])
def test_click_formula_parse_refuses_what_is_not_a_formula(text: str) -> None:
    with pytest.raises(ValueError, match="Cannot read the Math formula"):
        parse_click_formula(text)


@pytest.mark.parametrize("text", ["DH1 ^ 2h", "DH1 AND DH2 ^ 2h", "( DH1 ^ 2h ) + 1h"])
def test_click_formula_parse_refuses_a_power_in_hex_mode(text: str) -> None:
    # CLICK's dialog refuses it (OBJ-1006), so no pair with it needs a reading.
    with pytest.raises(ValueError, match=r"no '\^' in hex mode"):
        parse_with_unconfirmed_pairs(text, hex_mode=True)
    # The same formula reads in decimal mode.
    parse_with_unconfirmed_pairs(text.replace("h", ""))


_SIGNS = {
    NEGATE_ADDRESS,
    NEGATE_BRACKET,
    NEGATE_CONSTANT,
    NEGATE_FUNCTION,
    NEGATE_POINTER,
    NEGATE_PI,
    NEGATE_MINUS,
    NEGATE_PLUS,
    PLUS_SIGN,
}


def _written_orders(pair: frozenset[str]) -> list[tuple[str, str]]:
    """Return each order in which a formula can write the two operators of *pair*.

    A minus sign or a plus sign pairs only with an operator to its right.
    """
    if len(pair) == 1:
        [op] = pair
        return [(op, op)]
    if pair & _SIGNS:
        [sign] = pair & _SIGNS
        [other] = pair - _SIGNS
        return [(sign, other)]
    first, second = sorted(pair)
    return [(first, second), (second, first)]


def _settles(ruled_out: set[str], first: str, second: str) -> bool:
    """Return whether ruling out the orders *ruled_out* settles *first* written before *second*.

    Two operators could apply in three orders. Either one applies first, or both apply from
    the left. When the formula writes one operator twice, the order named after that operator
    applies the right one first.
    The parse groups the formula in one of these orders. The order is settled when the readings
    rule out each order that groups the formula another way.
    """
    if first == second or first in _SIGNS:
        return second in ruled_out
    if _TIER_OF[second] < _TIER_OF[first]:
        # The parse applies the second first. Applying the first first, or both from the left,
        # groups it another way.
        return {first, "left"} <= ruled_out
    return second in ruled_out


def _settled_orders() -> dict[tuple[str, str], set[str]]:
    """Return each order of two operators that the readings settle, with the rounds of those
    readings.

    Only two operators that meet have one other grouping, so only they are tested. A reading
    rules an order out when the formula, grouped in that order, gives another value.
    """
    ruled_out: dict[frozenset[str], set[str]] = {}
    rounds: dict[frozenset[str], set[str]] = {}
    for case, text, hex_mode, registers, value in READINGS:
        tree = parse_with_unconfirmed_pairs(text, hex_mode=hex_mode)[0]
        for parent, child, child_is_left, regrouped in regroupings(tree):
            # A grouping that gives the other 0 in a DF counts as another value.
            if _same_value(_click_value(regrouped, hex_mode, registers), value):
                continue
            pair = frozenset((parent, child))
            # With the child on the right, the regrouping also stands for both from the left.
            orders = {parent} if child_is_left else {parent, "left"}
            ruled_out.setdefault(pair, set()).update(orders)
            rounds.setdefault(pair, set()).add(case.split("-")[0])
    return {
        (first, second): rounds[pair]
        for pair, orders in ruled_out.items()
        for first, second in _written_orders(pair)
        if _settles(orders, first, second)
    }


def test_confirmed_pairs_are_the_pairs_that_the_readings_settle() -> None:
    settled = _settled_orders()
    both = {
        pair: settled[_written_orders(pair)[0]]
        for pair in {frozenset(order) for order in settled}
        if all(order in settled for order in _written_orders(pair))
    }
    listed = {frozenset((a, b)): set(rounds.split(", ")) for a, b, rounds in CONFIRMED_PAIRS}
    assert len(listed) == len(CONFIRMED_PAIRS)
    assert listed == both
    one_way = {order: rounds for order, rounds in settled.items() if frozenset(order) not in both}
    assert {(a, b): set(rounds.split(", ")) for a, b, rounds in CONFIRMED_ORDERS} == one_way


def test_read_formulas_are_the_readings_whose_pairs_are_not_all_settled(monkeypatch) -> None:
    monkeypatch.setattr(formula_module, "_read_trees", list)
    unsettled = [
        (text, hex_mode)
        for _, text, hex_mode, _, _ in READINGS
        if parse_with_unconfirmed_pairs(text, hex_mode=hex_mode)[1]
    ]
    assert list(READ_FORMULAS) == unsettled


# The grid holds a stand-in for each class of value that an operand of a CLICK formula can take.
# Beside each class is how the model of a CLICK (``formula_values.click_steps``) treats it.
#
# In decimal mode:
#
# - An integer register at 0, 1 and -1, at odd and even values, and at the ends of an Int and a
#   Dint (_WHOLE_GRID). The model reads each.
# - A DF at 0, at fractions and at the ends of a float32 (3.4028235E+38 and 1.4E-45), with
#   both signs (_REAL_GRID). The model reads each as a double.
# - A DF at -0.0 (_REAL_GRID). The model reads it as a double. A DF holds -0.0 from
#   0.0 * -1.0 (``real_bits/minus_zero_math``).
# - A minus sign before any operand that is 0. A minus sign before +0.0 gives -0.0
#   (``math_range/minus_sign``), and one before -0.0 gives +0.0 (b20 c13). The model reads
#   both.
# - A 0 that a step makes. The model reads the zeros in ``formula_values.ZEROS_READ``, each at
#   its exact operands. It leaves every other 0 of a step unread, with the sign of a ^ or SQRT
#   of 0.
# - A division or MOD by zero, and SQRT of a negative (the stand-in SQRT ( DS4 ) at -1). Each
#   gives 0 with SC40 or SC46, and the rest of the formula runs (``math_terms/error_terms``).
#   A division or MOD by zero of 5.0, -5.0, 0.0 or -0.0 gives +0.0 (``math_range/by_zero``),
#   and the model reads those four dividends only. SQRT ( -5.0 ) gives +0.0 (b20 c14). No
#   reading shows the sign of the 0 of SQRT at another negative value, or of SQRT of 0.
# - A pointer. A Copy through one past its bank raises SC44 (``faults/pointer_past_bank``), and
#   no reading shows one in a Math. The model leaves each pointer unread.
# - Reals that cancel: 1.0 with 1.0E+17 and 1.0E+30, with both signs (_REAL_GRID). The model
#   reads them in doubles, and a double drops the 1.0 from 1.0 + 1.0E+17.
# - A DF at Inf, -Inf or NaN (_REAL_GRID). The model leaves each step on one unread.
# - A constant that no DF holds, 0.1 (a stand-in). The model reads it as a double.
# - A whole-number constant, 2147483647 (a stand-in). The model reads it as a double.
# - A longer formula past 2^53, and a longer formula of Reals up to 1.0E+120 (the product). The
#   model reads both in doubles.
# - A longer formula from 1.0E-300 to 1.0E+300 (the power). With the other operand of the pair,
#   a step passes the largest double or falls below the smallest. The model leaves such a step
#   unread.
# - Inf or NaN from a longer formula. A step that overflows gives one, or a step on a DF that
#   holds one. The model leaves that step unread, so the pair never sees the value.
#
# In hex mode:
#
# - A DH at 0, at 1, at mixed bits and at the ends of each half (_HEX_GRID and _PLAIN_HEX_GRID).
#   The model reads each as unsigned.
# - A constant from 0h to FFFFh (the stand-in FFFFh). The model reads it as unsigned.
# - A longer formula past FFFFh, past FDA93880h and past 2^53 (the product). The model reads it
#   up to FDA93880h. It leaves a larger value unread, and an AND, OR or XOR of one past FFFFh.
# - A longer formula below 0, down to -FFFFh (the difference). The model reads it in each
#   step, except an AND, OR or XOR of a value below -FFFFh.
# - A longer formula below -FFFFh, down to -40000000h (the stand-in ( 0h - DH4 ) * DH5). The
#   model reads it, except in AND, OR or XOR. It leaves a value below -FEFF0100h unread.
# - Every hex step works in whole numbers, so no hex value is a fraction, -0, Inf or NaN.
#
# A function, PI or a pointer gives a value from one of these classes, or an error that raises
# the same flag in both groupings. The model reads SQRT only, and in decimal mode only.

#: The whole numbers reach the ends of the Int and Dint ranges. 164999119 * 114975250 * 58
#: passes 2^53, so a double rounds its product, and the two groupings round it to different
#: values.
_WHOLE_GRID = (
    *(0, 1, -1, 2, -2, 3, 7, 32767, -32768, 2147483647, -2147483648),
    *(164999119, 114975250, 58),
)

#: Each Real is a float32, as a DF holds it. In one grouping, 1.0 + 1.0E+17 - 1.0E+17 loses
#: the 1.0 before the large Reals cancel.
_REAL_GRID = tuple(
    float32(value)
    for value in (
        *_WHOLE_GRID,
        *(1.5, -2.5, 0.25, 2.0**40, -(2.0**40)),
        *(1.0e17, -1.0e17, 1.0e30, -1.0e30),
        *(-0.0, 3.4028234663852886e38, -3.4028234663852886e38, 2.0**-149, -(2.0**-149)),
        *(math.inf, -math.inf, math.nan),
    )
)
_HEX_GRID = (0, 1, 2, 3, 7, 0x7FFF, 0x8000, 0xFFFE, 0xFFFF)

#: The plain operands that a hex pair of SAME_VALUE_ON_PLAIN_OPERANDS runs over. They are 0, 1,
#: the ends of each half of a DH, and mixed bits. A plain operand is never past FFFFh, and these
#: steps compute whole numbers far below the largest value read. So the ends stand for the
#: values between them.
_PLAIN_HEX_GRID = (0, 1, 2, 3, 7, 0x00FF, 0x5555, 0x7FFF, 0x8000, 0xAAAA, 0xFF00, 0xFFFE, 0xFFFF)

#: The plain operands that a decimal pair of SAME_VALUE_ON_PLAIN_OPERANDS runs over. They are 0,
#: small values of both signs, and the ends of an Int and a Dint with their neighbours. A DS or
#: DD holds a whole number, never Inf, NaN or -0.0. 164999119 * 114975250 * 58 passes 2^53, so
#: a product of three can round.
_PLAIN_WHOLE_GRID = (
    *(0, 1, -1, 2, -2, 3, 7, 58),
    *(32767, -32767, 32768, -32768),
    *(2147483647, -2147483647, -2147483648, 164999119, 114975250),
)

#: The destinations that the plain operands of each mode run into.
_PLAIN_DESTINATIONS: dict[bool, tuple[tuple[str, Any, Any, tuple[float, ...]], ...]] = {
    False: (
        ("Int", Int, Dint, _PLAIN_WHOLE_GRID),
        ("Dint", Dint, Dint, _PLAIN_WHOLE_GRID),
        ("Real", Real, Dint, _PLAIN_WHOLE_GRID),
    ),
    True: (("Word", Word, Word, _PLAIN_HEX_GRID),),
}

#: Each destination of a mode, with its type, the type of the operands and their values.
_DESTINATIONS: dict[bool, tuple[tuple[str, Any, Any, tuple[float, ...]], ...]] = {
    False: (
        ("Int", Int, Dint, _WHOLE_GRID),
        ("Dint", Dint, Dint, _WHOLE_GRID),
        ("Real", Real, Real, _REAL_GRID),
        ("Int", Int, Real, _REAL_GRID),
        ("Dint", Dint, Real, _REAL_GRID),
    ),
    True: (("Word", Word, Word, _HEX_GRID),),
}

#: The values of a stand-in's registers, for each type of operand.
_StandInValues = dict[Any, tuple[tuple[float, ...], ...]]

#: Each stand-in that takes the place of one register of a pair's formula, with the values of
#: its own registers. The stand-ins in brackets settle their own order, so they add no pair.
#: The product's lists reach past 2^53. In hex, 8000h four times is 2^60, and the last product
#: needs 55 bits.
_STAND_INS: dict[bool, tuple[tuple[str, _StandInValues], ...]] = {
    False: (
        (
            "( ( ( {bank}4 * {bank}5 ) * {bank}6 ) * {bank}7 )",
            {
                Dint: (
                    (1, 1, 1, 1),
                    (-2, 3, 7, 1),
                    (164999119, 114975250, 58, 1),
                    (2147483647, 2147483647, 2147483647, -2147483648),
                ),
                Real: tuple(
                    tuple(float32(value) for value in values)
                    for values in (
                        (1, 1, 1, 1),
                        (1.5, -2.5, 0.25, 1),
                        (1.0e17, 1.0e17, 1, 1),
                        (1.0e30,) * 4,
                    )
                ),
            },
        ),
        ("( {bank}4 ^ {bank}5 )", {Dint: ((10, 300), (10, -300)), Real: ((10, 300), (10, -300))}),
        ("0.1", {Dint: ((),), Real: ((),)}),
        ("2147483647", {Dint: ((),), Real: ((),)}),
        ("SQRT ( {bank}4 )", {Dint: ((-1,), (4,)), Real: ((-1.0,), (2.25,))}),
    ),
    True: (
        (
            "( ( ( {bank}4 * {bank}5 ) * {bank}6 ) * {bank}7 )",
            {
                Word: (
                    (1, 1, 1, 1),
                    (0x100, 0x101, 1, 1),
                    (0x8000, 0x8000, 0x8000, 0x8000),
                    (0x3031, 0x3AFD, 0x24FD, 0x2B86),
                )
            },
        ),
        ("( {bank}4 - {bank}5 )", {Word: ((0, 1), (0, 0xFFFF))}),
        ("( ( 0h - {bank}4 ) * {bank}5 )", {Word: ((0x8000, 0x8000), (0x100, 0x100))}),
        ("FFFFh", {Word: ((),)}),
    ),
}


def _with_a_stand_in(text: str, hex_mode: bool) -> list[tuple[str, _StandInValues]]:
    """Return *text* with each stand-in in place of each register in turn, with its values.

    A register right after the leading minus sign keeps its place, because a stand-in there
    makes another kind of minus sign. A range function takes registers only.
    """
    if ":" in text:
        return []
    bank = "DH" if hex_mode else "DS"
    return [
        (re.sub(rf"\b{bank}{index}\b", stand_in.format(bank=bank), text), values)
        for stand_in, values in _STAND_INS[hex_mode]
        for index in _registers(text, bank)
        if not text.startswith(f"- {bank}{index} ")
    ]


def _registers(text: str, bank: str) -> list[int]:
    """Return the number of each register of *text*, in order."""
    return sorted({int(index) for index in re.findall(rf"\b{bank}(\d+)\b", text)})


#: The binary operators of each mode. CLICK has no ``^`` in hex mode.
_MODE_OPERATORS = {
    False: ("+", "-", "*", "/", "^", "MOD"),
    True: ("+", "-", "*", "/", "MOD", "AND", "OR", "XOR"),
}


#: The functions that take two arguments. Every other function in the parse takes one.
_TWO_ARGUMENTS = ("LSH", "RSH", "LRO", "RRO", "RAND")


def _function_calls(bank: str) -> list[str]:
    """Return a formula with each function that the parse reads, with ``{}`` after the call."""
    calls = [
        f"{name} ( {bank}1 , {bank}2 ) {{}} {bank}3"
        if name in _TWO_ARGUMENTS
        else f"{name} ( {bank}1 ) {{}} {bank}2"
        for name in _CLICK_FUNC_TO_PYTHON
    ]
    calls += [f"{name} ( {bank}1 : {bank}2 ) {{}} {bank}3" for name in _RANGE_AGG_TO_METHOD]
    return calls


def _unconfirmed_pairs() -> list[tuple[str, str, bool, str]]:
    """Return each pair of operators that no reading settled, with its mode and a formula.

    A pair can come with several formulas. A minus sign before a function comes with one for
    each function.
    """
    confirmed = {frozenset((a, b)) for a, b, _ in CONFIRMED_PAIRS}
    confirmed_orders = {(a, b) for a, b, _ in CONFIRMED_ORDERS}
    pairs = []
    for hex_mode, ops in _MODE_OPERATORS.items():
        bank = "DH" if hex_mode else "DS"
        texts = [f"{bank}1 {a} {bank}2 {b} {bank}3" for a in ops for b in ops]
        texts += [f"- {bank}1 {b} {bank}2" for b in ops]
        texts += [f"- ( {bank}1 ) {b} {bank}2" for b in ops]
        if not hex_mode:
            # A decimal plus before an address, a constant or a bracket has no node. A plus
            # before a function keeps one.
            texts += [f"+ SQRT ( {bank}1 ) {b} {bank}2" for b in ops]
        if hex_mode:
            # The pair holds for every constant, so it comes with small and large ones.
            texts += [f"- {c} {b} {bank}2" for c in ("0h", "5h", "8000h", "FFFFh") for b in ops]
        texts += [f"- {call.format(b)}" for call in _function_calls(bank) for b in ops]
        for text in texts:
            tree = parse_with_unconfirmed_pairs(text, hex_mode=hex_mode)[0]
            [(first, _), (second, _)] = next(formula_module._scopes(tree))
            if frozenset((first, second)) in confirmed or (first, second) in confirmed_orders:
                continue
            pairs.append((first, second, hex_mode, text))
    return pairs


class _PyrungRun:
    """One formula as a pyrung ``math()``, built once and run for many values."""

    def __init__(
        self, tree: Any, bank: str, indexes: list[int], operand_type: Any, dest_type: Any
    ) -> None:
        self.names = [f"{bank}{index}" for index in indexes]
        tags = {index: operand_type(f"{bank}{index}") for index in indexes}
        self.instruction = CalcInstruction(pyrung_expression(tree, tags), dest_type("Dest"))
        execute(self.instruction, SystemState())

    def __call__(self, context: ScanContext, values: tuple[float, ...]) -> Any:
        for name, value in zip(self.names, values, strict=False):
            context.set_tag(name, value)
        self.instruction.execute(context, True)
        return context.get_tag("Dest")


def _same_value(a: float, b: float) -> bool:
    """Return whether two values are the same bit for bit, so that -0.0 differs from 0.0."""
    if isinstance(a, int) and isinstance(b, int):
        return a == b
    return struct.pack("<d", a) == struct.pack("<d", b)


def _same_bits(first: tuple[float, frozenset[str]], second: tuple[float, frozenset[str]]) -> bool:
    """Return whether two values and their flags are the same bit for bit."""
    (a, a_flags), (b, b_flags) = first, second
    return a_flags == b_flags and _same_value(a, b)


def _how_the_groupings_differ(text: str, hex_mode: bool) -> str | None:
    """Return how the two groupings of *text* differ, or None where they agree.

    At each point of the grid, the model of a CLICK computes both groupings up to the store.
    Where the two values and their flags are the same bit for bit, a CLICK can't tell the
    groupings apart, so the model leaves out the store. Otherwise both stores must give the
    same value and flags, and pyrung's ``math()`` must store that value. A point where the model
    has no reading counts as a difference. pyrung's two groupings must store the same value
    everywhere. The grid runs *text*, and *text* with each stand-in in place of each register.

    pyrung's flags are not compared, because a flag that pyrung gets wrong under both groupings
    is a flag bug, not a question of order (ruled 2026-11-26). A store that pyrung gets wrong
    under both groupings is a store bug in the same way (ruled 2026-11-26).
    """
    for formula, values in _grid_formulas(text, hex_mode, plain=False):
        how = _how_one_formula_differs(formula, hex_mode, values)
        if how is not None:
            return f"{formula}: {how}"
    return None


def _grid_formulas(
    text: str, hex_mode: bool, *, plain: bool
) -> list[tuple[str, _StandInValues | None]]:
    """Return *text*, and *text* with each stand-in in turn, each with its stand-in's values.

    With *plain*, return only the formulas whose pair has plain operands. A constant is a plain
    operand too, so its stand-in runs on the grid of plain operands.
    """
    formulas = [(text, None), *_with_a_stand_in(text, hex_mode)]
    if not plain:
        return formulas
    return [
        (formula, values)
        for formula, values in formulas
        if formula_module.plain_operands(
            *next(_scope_steps(parse_with_unconfirmed_pairs(formula, hex_mode=hex_mode)[0])),
            hex_mode=hex_mode,
        )
    ]


def _grid_points(
    text: str, hex_mode: bool, stand_in: _StandInValues | None, operand_type: Any, grid: Any
) -> Iterator[tuple[tuple[float, ...], dict[str, float]]]:
    """Yield the values of the registers of *text* at each point of *grid*, and by name.

    The registers DS1 to DS3 (or DH1 to DH3) take each value of *grid*. A stand-in's own
    registers, from DS4 (or DH4) on, take the values that *stand_in* lists for *operand_type*.
    """
    bank = "DH" if hex_mode else "DS"
    indexes = _registers(text, bank)
    outer = [index for index in indexes if index < 4]
    inner = stand_in[operand_type] if stand_in else [()]
    for point in itertools.product(itertools.product(grid, repeat=len(outer)), inner):
        values: tuple[float, ...] = (*point[0], *point[1])
        yield values, dict(zip((f"{bank}{index}" for index in indexes), values, strict=True))


def _how_one_formula_differs(
    text: str,
    hex_mode: bool,
    stand_in: _StandInValues | None = None,
    destinations: tuple[tuple[str, Any, Any, tuple[float, ...]], ...] | None = None,
) -> str | None:
    """Return how the two groupings of *text* differ, or None where they agree.

    *stand_in* holds the values of the stand-in's registers, DS4 to DS7 or DH4 to DH7.
    """
    bank = "DH" if hex_mode else "DS"
    tree = parse_with_unconfirmed_pairs(text, hex_mode=hex_mode)[0]
    [regrouped] = [regrouped for *_, regrouped in regroupings(tree)]
    indexes = _registers(text, bank)
    for dest, dest_type, operand_type, grid in destinations or _DESTINATIONS[hex_mode]:
        # pyrung_expression builds no range function, so the runs wait for a point that the
        # model reads.
        runs: list[_PyrungRun] = []
        context = ScanContext(SystemState())
        for values, registers in _grid_points(text, hex_mode, stand_in, operand_type, grid):
            where = f"for {values} into a {dest}"
            try:
                steps = [click_steps(t, hex_mode, registers) for t in (tree, regrouped)]
                click = None if _same_bits(*steps) else [click_store(*s, dest) for s in steps]
            except Unread as unread:
                return f"no reading shows {unread} {where}"
            if click is not None and not _same_bits(*click):
                return f"the model of a CLICK stores {click} {where}"
            if not runs:
                runs = [
                    _PyrungRun(t, bank, indexes, operand_type, dest_type) for t in (tree, regrouped)
                ]
            pyrung = [run(context, values) for run in runs]
            if not _same_value(*pyrung) or (
                click is not None and not _same_value(pyrung[0], click[0][0])
            ):
                stored = "" if click is None else f", a CLICK {click[0][0]}"
                return f"pyrung stores {pyrung} {where}{stored}"
    return None


def test_same_value_pairs_are_the_pairs_whose_groupings_agree() -> None:
    differ: dict[tuple[str, str, bool], list[str]] = {}
    for first, second, hex_mode, text in _unconfirmed_pairs():
        how = _how_the_groupings_differ(text, hex_mode)
        differ.setdefault((first, second, hex_mode), []).extend([] if how is None else [how])
    agree = {pair for pair, hows in differ.items() if not hows}
    assert len(set(SAME_VALUE_PAIRS)) == len(SAME_VALUE_PAIRS)
    assert set(SAME_VALUE_PAIRS) == agree, differ


def _how_pairs_differ_on_plain_operands() -> dict[tuple[str, str, bool], list[str]]:
    """Return how the groupings of each pair differ on plain operands, empty where they agree."""
    differ: dict[tuple[str, str, bool], list[str]] = {}
    for first, second, hex_mode, text in _unconfirmed_pairs():
        for formula, values in _grid_formulas(text, hex_mode, plain=True):
            how = _how_one_formula_differs(formula, hex_mode, values, _PLAIN_DESTINATIONS[hex_mode])
            differ.setdefault((first, second, hex_mode), []).extend([] if how is None else [how])
    return differ


def test_same_value_on_plain_operands_is_the_pairs_whose_groupings_agree_there() -> None:
    differ = _how_pairs_differ_on_plain_operands()
    agree = {pair for pair, hows in differ.items() if not hows}
    assert len(set(SAME_VALUE_ON_PLAIN_OPERANDS)) == len(SAME_VALUE_ON_PLAIN_OPERANDS)
    assert set(SAME_VALUE_ON_PLAIN_OPERANDS) == agree, differ


def _unread_zero_signs_on_grid(text: str, *, plain: bool) -> set[str]:
    """Return the row of each zero sign that the decimal formula *text* meets and no reading shows.

    The rows come from both groupings of *text*, and of *text* with each stand-in, at each point
    of the grid that the list's derivation runs over.
    """
    destinations = (_PLAIN_DESTINATIONS if plain else _DESTINATIONS)[False]
    grids = {(operand_type, grid) for _, _, operand_type, grid in destinations}
    rows: set[str] = set()
    for formula, values in _grid_formulas(text, False, plain=plain):
        tree = parse_with_unconfirmed_pairs(formula, hex_mode=False)[0]
        [regrouped] = [regrouped for *_, regrouped in regroupings(tree)]
        for operand_type, grid in grids:
            for _, registers in _grid_points(formula, False, values, operand_type, grid):
                rows |= unread_zero_signs(tree, registers) | unread_zero_signs(regrouped, registers)
    return rows


@pytest.mark.parametrize("listed", list(PENDING_ZERO_SIGNS))
def test_pending_zero_signs_are_the_unread_rows_that_each_pair_meets(listed: str) -> None:
    # Each list takes the pairs that only zero signs keep out on plain operands. The next test
    # checks those.
    pairs = set(PENDING_ZERO_SIGNS["SAME_VALUE_ON_PLAIN_OPERANDS"])
    met: dict[tuple[str, str], set[str]] = {pair: set() for pair in pairs}
    for first, second, hex_mode, text in _unconfirmed_pairs():
        if not hex_mode and (first, second) in pairs:
            rows = _unread_zero_signs_on_grid(text, plain=listed == "SAME_VALUE_ON_PLAIN_OPERANDS")
            met[(first, second)].update(rows)
    pending = PENDING_ZERO_SIGNS[listed]
    assert {pair: set(rows) for pair, rows in pending.items()} == met


def test_pending_zero_signs_hold_each_pair_that_only_zero_signs_keep_out(monkeypatch) -> None:
    # With unread_zero doing nothing, the model reads each zero sign as the rejoin rules suppose.
    monkeypatch.setattr(formula_values._Guard, "unread_zero", lambda self, op, *operands: None)
    differ = _how_pairs_differ_on_plain_operands()
    listed = set(SAME_VALUE_ON_PLAIN_OPERANDS)
    agree = {
        (first, second)
        for (first, second, hex_mode), hows in differ.items()
        if not hex_mode and not hows and (first, second, hex_mode) not in listed
    }
    assert agree <= set(PENDING_ZERO_SIGNS["SAME_VALUE_ON_PLAIN_OPERANDS"]), agree


@pytest.mark.parametrize(
    ("text", "hex_mode", "pairs"),
    [
        ("SQRT ( - ( DF1 ) * DS2 )", False, [(NEGATE_BRACKET, "*")]),
        # Two operators that do not meet still count.
        ("- ( DF1 ) + DS2 * DS3", False, [(NEGATE_BRACKET, "*")]),
        # A minus sign pairs with each operator to its right.
        ("- ( DF1 ) * DS2 - DS3", False, [(NEGATE_BRACKET, "*")]),
        ("- ( DF1 ) - DS2 * DS3", False, [(NEGATE_BRACKET, "*")]),
        ("- ( DS1 ) / DS2 * DS3", False, [(NEGATE_BRACKET, "*")]),
        ("- PI * 2 / 3", False, [("a minus sign before PI", "*"), ("a minus sign before PI", "/")]),
        ("- PI * 2", False, [("a minus sign before PI", "*")]),
        # No reading shows most functions inside a formula, so the grid settles no pair with a
        # minus sign before a function.
        ("- SQRT ( DS1 ) * 2", False, [(NEGATE_FUNCTION, "*")]),
        ("- ABS ( DS1 ) / 2", False, [(NEGATE_FUNCTION, "/")]),
        ("- SUM ( DS1 : DS3 ) MOD 2", False, [(NEGATE_FUNCTION, "MOD")]),
        ("- LSH ( DH1 , 2h ) * DH2", True, [(NEGATE_FUNCTION, "*")]),
        ("- DH[DS1] OR 3h", True, [("a minus sign before a pointer", "OR")]),
        ("- - DH1 XOR 3h", True, [("a minus sign before another minus sign", "XOR")]),
        # The readings that settled a minus sign before a bracket settle none of these.
        ("- PI ^ 2", False, [(NEGATE_PI, "^")]),
        ("- DS[DS1] ^ 2", False, [(NEGATE_POINTER, "^")]),
        ("- - DS1 ^ 2", False, [(NEGATE_MINUS, "^")]),
        ("- - DS1 + 2", False, [(NEGATE_MINUS, "+")]),
        # In hex mode a minus sign before a constant is an operator. Rounds b20 and b22 read its
        # order with AND, OR, XOR, +, - and /. With * the two groupings give the same value, so
        # no reading settles it. A product past FDA93880h is unread.
        ("- 5h * DH1", True, [(NEGATE_CONSTANT, "*")]),
        # No reading puts a decimal plus before a function, a pointer or PI.
        ("+ SQRT ( DF1 ) * DF2", False, [(PLUS_SIGN, "*")]),
        ("+ PI * DF2", False, [(PLUS_SIGN, "*")]),
        ("+ DS[DS1] * DS2", False, [(PLUS_SIGN, "*")]),
        ("- + PI * 2", False, [(NEGATE_PLUS, "*"), (PLUS_SIGN, "*")]),
        # FFFFh * FFFFh passes FDA93880h, the largest hex value read.
        ("- DH1 * DH2", True, [(NEGATE_ADDRESS, "*")]),
        ("- ( DH1 ) * DH2", True, [(NEGATE_BRACKET, "*")]),
        # A product, a bracket or a function hands the pair a value past FFFFh.
        ("( DH1 * DH2 ) AND DH3 AND DH4", True, [("AND", "AND")]),
        ("DH1 OR DH2 OR LSH ( DH3 , 2h )", True, [("OR", "OR")]),
        # A DF can hold Inf or NaN, and the model reads no step on either.
        ("- DF2 * 4", False, [(NEGATE_ADDRESS, "*")]),
        ("- ( DF1 ) * DS2", False, [(NEGATE_BRACKET, "*")]),
        ("- DS1 * DF2", False, [(NEGATE_ADDRESS, "*")]),
        # With 1.0E+30, 10 and 300, one grouping overflows a double at its last step, and the
        # other inside the formula. No reading shows either.
        ("- DF1 * DF2 ^ DF3", False, [(NEGATE_ADDRESS, "*")]),
        ("- ( DF1 ) * DF2 ^ DF3", False, [(NEGATE_BRACKET, "*")]),
    ],
)
def test_click_formula_parse_is_none_for_an_unconfirmed_pair(
    text: str, hex_mode: bool, pairs: list[tuple[str, str]]
) -> None:
    assert parse_click_formula(text, hex_mode=hex_mode) is None
    assert parse_with_unconfirmed_pairs(text, hex_mode=hex_mode)[1] == tuple(pairs)


@pytest.mark.parametrize(
    ("text", "hex_mode"),
    [
        # Brackets give the order.
        ("( DS1 - DS2 ) * 3", False),
        ("DS1 - ( DS2 * 3 )", False),
        ("( - DS1 ) ^ 2", False),
        ("- ( DS1 ^ 2 )", False),
        ("SQRT ( DS1 - DS2 ) * 3", False),
        # A minus sign applies before each operator to its left.
        ("- - DS1", False),
        ("DS1 / - DS2", False),
        ("DS1 MOD - DS2", False),
        ("DS1 + - ( DS2 + 1 )", False),
        ("DS1 ^ - ( DS2 )", False),
        ("DS1 * - ( DS2 + 1 )", False),
        # A CLICK was read running these.
        ("5 - 3 + 1", False),
        ("DS1 - DS2 + DS3", False),
        ("- DS1 * 3", False),
        ("- DS1 MOD 3", False),
        # Readings settled these pairs.
        ("DS1 * 2 + 3", False),
        ("DH1 AND DH2 OR DH3", True),
        ("DS1 - DS2 * 3", False),
        ("DH1 - DH2 XOR 3h", True),
        ("- ( DS1 ) ^ 2", False),
        ("- SQRT ( DS1 ) + 2", False),
        # Both groupings give the same value and flags on plain operands
        # (SAME_VALUE_ON_PLAIN_OPERANDS).
        ("DH1 AND DH2 AND DH3", True),
        ("DH1 OR DH2 OR DH3", True),
        ("DH1 XOR DH2 XOR 0Fh", True),
        # Round b16 read each of these pairs in one mode, and the lists apply each pair to both.
        # It read / MOD, / AND, / XOR, MOD OR, MOD XOR, * OR and * XOR in hex. It read * *, + +,
        # + -, MOD MOD, a minus sign before a bracket with -, and a minus sign before an address
        # with / and MOD in decimal. Round b20 read MOD MOD in hex and / MOD in decimal.
        ("SD20 * 10000 + SD21 * 100 + SD22", False),
        ("DS1 * DS2 * DS3", False),
        ("DH1 * DH2 * DH3 * DH4 * DH5", True),
        ("DF1 + DF2 + DF3", False),
        ("DF1 + DF2 - DF3", False),
        ("DH1 + DH2 + DH3", True),
        ("DH1 + DH2 - DH3", True),
        ("FFFFh + DH1 + DH2", True),
        ("DS1 / DS2 MOD 3", False),
        ("DS1 MOD DS2 MOD 3", False),
        ("DH1 / DH2 AND 3h", True),
        ("DH1 MOD DH2 OR 1h", True),
        ("DH1 XOR DH2 * 3h", True),
        ("- ( DS1 ) - DS2", False),
        ("- DS1 / DS2", False),
        ("- DH1 MOD DH2", True),
        # Round b20 read these pairs.
        ("- 2h AND 3h", True),
        ("- 2h OR DH1", True),
        ("- 3h XOR 1h", True),
        ("- 5h + DH1", True),
        ("- 5h - DH1", True),
        ("- ( DS1 ) / DS2", False),
        ("- ( DF1 ) MOD DF2", False),
        ("- ( DH1 ) MOD 7h", True),
        ("- ( DH1 * DH2 ) MOD DH3", True),
        ("DH1 MOD DH2 MOD DH3", True),
        ("- ( DF1 ) / DF2 ^ DF3", False),
        # Round b22 read these.
        ("- 5h / DH1", True),
        ("- 5 / DH1", True),
        # A decimal plus before an address or a bracket changes nothing, even on -0.0 (rounds
        # b22 and b24).
        ("+ DS1 * DS2", False),
        ("+ DF1 * DF2", False),
        ("+ ( DF1 ) * DF2", False),
        ("- + DS1 * 2", False),
        # A decimal CLICK stores a plus before a constant in the constant. A hex plus does not
        # cut its operand (round b21). It changes nothing.
        ("+ 5 * DS1", False),
        ("+ 5h * DH1", True),
        # A bracket gives the order. In decimal mode the minus sign is part of the constant.
        ("( - 5h ) / DH1", True),
        ("- 5 / DS1", False),
        # Both groupings give the same value and flags on plain operands.
        ("- 5h MOD DH1", True),
        ("- DS1 * DS2", False),
        ("- DS2 * 4", False),
        ("- ( DS1 ) * DS2", False),
        ("SQRT ( - ( DS1 ) * DS2 )", False),
    ],
)
def test_click_formula_parse_confirms_an_order_that_the_grammar_or_a_reading_gives(
    text: str, hex_mode: bool
) -> None:
    assert parse_click_formula(text, hex_mode=hex_mode) is not None


def test_pyrung_click_exports_the_formula_parser() -> None:
    import pyrung.click

    assert pyrung.click.parse_click_formula is parse_click_formula
    assert pyrung.click.parse_click_formula("- DH1 * DH2", hex_mode=True) is None
    with pytest.raises(ValueError):
        pyrung.click.parse_click_formula("2 ^ 3", hex_mode=True)


def test_click_formula_parse_keeps_a_hex_minus_sign_before_a_constant_as_an_operator() -> None:
    # CLICK stores - 5h / DH1 as -#UH5#/#DH1 and - 5 / DS1 as #H-5#/#DS1.
    negate = parse_with_unconfirmed_pairs("- 5h / DH1", hex_mode=True)[0].left
    assert negate == formula_module.Negate(Number(5))
    folded = parse_with_unconfirmed_pairs("- 5 / DS1")[0].left
    assert folded == Number(-5)


@pytest.mark.parametrize(
    ("text", "without"),
    [
        # Round b21 read each formula with 1h in DH1, and each stored what it stores without
        # the plus.
        ("+ ( 0h - DH1 ) + 1h", "( 0h - DH1 ) + 1h"),
        ("+ 0h - DH1 + 1h", "0h - DH1 + 1h"),
        ("+ ( 0h - DH1 ) * 2h", "( 0h - DH1 ) * 2h"),
        # Round b20 read it before a constant.
        ("+ 5h * DH1", "5h * DH1"),
        # A plus alone, on the right of an operator, alone in a bracket and in a function.
        ("+ ( 0h - DH1 )", "( 0h - DH1 )"),
        ("1h + + ( 0h - DH1 )", "1h + ( 0h - DH1 )"),
        ("( + ( 0h - DH1 ) ) + 1h", "( ( 0h - DH1 ) ) + 1h"),
        ("LSH ( + ( 0h - DH1 ) , 1h )", "LSH ( ( 0h - DH1 ) , 1h )"),
        ("- + 5h * DH1", "- 5h * DH1"),
    ],
)
def test_click_formula_parse_drops_a_hex_plus_sign(text: str, without: str) -> None:
    # In hex mode CLICK stores the plus as a token of its own (+#(#UH0#-#DH421#)#+#UH1), and it
    # changes no value or flag.
    assert parse_with_unconfirmed_pairs(text, hex_mode=True) == parse_with_unconfirmed_pairs(
        without, hex_mode=True
    )


@pytest.mark.parametrize(
    ("text", "without"),
    [
        # CLICK stores the plus in the constant, H+5 (round b20).
        ("+ 5 * DS1", "5 * DS1"),
        # With 5 in DS401, + DS401 * 2 and + ( DS401 - 3 ) * 2 stored 10 and 4 (round b22).
        ("+ DS1 * 2", "DS1 * 2"),
        ("+ ( DS1 - 3 ) * 2", "( DS1 - 3 ) * 2"),
        # With -0.0 in DF110, + DF110 stored -0.0 (round b24 z1).
        ("+ DF1", "DF1"),
        ("- + DS1 * 2", "- DS1 * 2"),
    ],
)
def test_click_formula_parse_drops_a_decimal_plus_sign(text: str, without: str) -> None:
    assert parse_with_unconfirmed_pairs(text) == parse_with_unconfirmed_pairs(without)


def test_click_formula_parse_keeps_a_decimal_plus_sign_before_a_function() -> None:
    # No reading puts a decimal plus before a function, a pointer or PI.
    tree = parse_with_unconfirmed_pairs("+ SQRT ( DF1 ) * 2")[0]
    assert isinstance(tree, BinaryOp)
    assert isinstance(tree.left, formula_module.Plus)
    assert parse_click_formula("+ PI * 2") is None


# ---------------------------------------------------------------------------
# The importer
# ---------------------------------------------------------------------------

_HEADER = tuple(["R"] + [str(i) for i in range(1, 32)] + ["AF"])


def _rung(token: str) -> tuple[str, ...]:
    return tuple(["R", "-"] + ["-"] * 30 + [token])


def _bundle(*tokens: str) -> LadderBundle:
    return LadderBundle(main_rows=(_HEADER, *(_rung(t) for t in tokens)), subroutine_rows=())


def _math_line(code: str) -> str:
    return next(line.strip() for line in code.splitlines() if line.strip().startswith("math("))


@pytest.mark.parametrize(
    ("token", "line"),
    [
        ("math(+ SQRT ( DF1 ) * 2,DF2)", "math(+sqrt(DF1) * 2, DF2)"),
    ],
)
def test_imported_formula_keeps_a_decimal_plus_sign(token: str, line: str) -> None:
    # No reading shows what a decimal plus before a function does. So the import keeps the
    # plus as a step of the scan, and warns.
    with pytest.warns(UserWarning, match=PLUS_SIGN):
        source = ladder_to_pyrung(_bundle(token))
    assert _math_line(source) == line
    # The export, imported again, gives the same line.
    ns = exec_with_source(source)
    exported = pyrung_to_ladder(ns["logic"], ns["mapping"])
    with pytest.warns(UserWarning, match=PLUS_SIGN):
        again = ladder_to_pyrung(exported)
    assert _math_line(again) == line


def test_imported_formula_names_a_minus_sign_before_a_plus_sign() -> None:
    with pytest.warns(UserWarning) as caught:
        source = ladder_to_pyrung(_bundle("math(- + PI * 2,DF2)"))
    assert any(f"uses {NEGATE_PLUS} and '*'" in str(w.message) for w in caught)
    assert _math_line(source) == "math(-+PI * 2, DF2)"


@pytest.mark.parametrize(
    ("token", "line"),
    [
        (
            "math(6h OR 3h AND 8h,DH1,mode=hex)",
            "math((LiteralExpr(0x6) | 0x3) & 0x8, DH1)",
        ),
        ("math(6h AND 3h OR 8h,DH1,mode=hex)", "math(LiteralExpr(0x6) & 0x3 | 0x8, DH1)"),
        (
            "math(1h + 2h AND 2h,DH1,mode=hex)",
            "math(0x1 + (LiteralExpr(0x2) & 0x2), DH1)",
        ),
        ("math(DH1 AND DH2 * 3h,DH3,mode=hex)", "math((DH1 & DH2) * 0x3, DH3)"),
        ("math(2 ^ 3 ^ 2,DF1)", "math((LiteralExpr(2)**3)**2, DF1)"),
        ("math(DS1 ^ 2,DS3)", "math(DS1**2, DS3)"),
        ("math(- DS1 ^ 2,DF1)", "math((-DS1)**2, DF1)"),
        ("math(-2 ^ 2,DF1)", "math(LiteralExpr(-2)**2, DF1)"),
        ("math(DS1 * - ( DS2 + 1 ),DS3)", "math(DS1 * -(DS2 + 1), DS3)"),
        ("math(DS1 - ( DS2 - DS3 ),DS4)", "math(DS1 - (DS2 - DS3), DS4)"),
        ("math(DH1 + 255,DH2,mode=hex)", "math(DH1 + 0x255, DH2)"),
        ("math(DH001 AND 00FFh,DH002,mode=hex)", "math(DH001 & 0x00FF, DH002)"),
        # Python would compute -7 % 3 as 2 when it loads the file. CLICK gives -1.
        ("math(-7 MOD 3,DF1)", "math(LiteralExpr(-7) % 3, DF1)"),
        ("math(5 / 0 + DS1,DF1)", "math(LiteralExpr(5) / 0 + DS1, DF1)"),
        ("math(ABS ( 2 - 3 ),DS1)", "math(abs(LiteralExpr(2) - 3), DS1)"),
        ("math(ABS ( -3 ) / 0,DS1)", "math(LiteralExpr(abs(-3)) / 0, DS1)"),
        ("math(DS1 MOD 3,DS2)", "math(DS1 % 3, DS2)"),
    ],
)
def test_imported_formula_brackets_what_click_groups(token: str, line: str) -> None:
    assert _math_line(ladder_to_pyrung(_bundle(token))) == line


@pytest.mark.parametrize(
    ("token", "line"),
    [
        # A decimal CLICK stores + 5 as the constant H+5.
        ("math(+ 5 * DS1,DS2)", "math(5 * DS1, DS2)"),
        # A decimal plus before an address or a bracket changes nothing (rounds b22 and b24).
        ("math(+ DS1 * 2,DS2)", "math(DS1 * 2, DS2)"),
        ("math(+ ( DS1 - 3 ) * 2,DS2)", "math((DS1 - 3) * 2, DS2)"),
        ("math(+ DF1,DF2)", "math(DF1, DF2)"),
        # A hex plus does not cut its operand (round b21). It changes nothing.
        ("math(+ 5h * DH1,DH2,mode=hex)", "math(0x5 * DH1, DH2)"),
        ("math(+ ( 0h - DH1 ) + 1h,DH2,mode=hex)", "math((0x0 - DH1) + 0x1, DH2)"),
    ],
)
def test_imported_formula_drops_a_plus_sign(token: str, line: str) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert _math_line(ladder_to_pyrung(_bundle(token))) == line


@pytest.mark.parametrize("operand", ['DS1 XOR "a"', "DS1..DS2 MOD 2", "DS1 ^ none"])
def test_click_operators_outside_a_formula_are_refused(operand: str) -> None:
    """Python would read ^ as XOR, so a CLICK operator outside a Math formula stops the import."""
    with pytest.raises(ValueError, match="Unsupported CSV operand expression"):
        ladder_to_pyrung(_bundle(f"copy({operand},DS10)"))


_FAULTS = ("fault.division_error", "fault.out_of_range", "fault.math_operation_error")


def _hand_written(build, dest_type, operand_type) -> Program:
    dest = dest_type("Dest")
    operand = operand_type("Operand")
    with Program() as logic:
        with rung():
            copy(1, operand)
            math_instruction(build(operand), dest)
    return logic


@pytest.mark.parametrize(
    ("token", "dest", "build", "dest_type", "operand_type", "value"),
    [
        ("math(5 / 0,DS2)", "DS2", lambda _: LiteralExpr(5) / 0, Int, Int, 0),
        ("math(0 ^ 0,DS2)", "DS2", lambda _: LiteralExpr(0) ** 0, Int, Int, 0),
        ("math(( 2 - 9 ) MOD 3,DS2)", "DS2", lambda _: (LiteralExpr(2) - 9) % 3, Int, Int, -1),
        (
            "math(( FFFFh + 2h ) * DH1 / 2h,DH2,mode=hex)",
            "DH2",
            lambda dh1: (LiteralExpr(0xFFFF) + 0x2) * dh1 / 0x2,
            Word,
            Word,
            0x8000,
        ),
        ("math(ABS ( -7 ) MOD 0,DS2)", "DS2", lambda _: LiteralExpr(7) % 0, Int, Int, 0),
        ("math(ABS ( -3 ) / 0,DS2)", "DS2", lambda _: LiteralExpr(3) / 0, Int, Int, 0),
        ("math(ABS ( 0 ) ^ 0,DS2)", "DS2", lambda _: LiteralExpr(0) ** 0, Int, Int, 0),
        ("math(ABS ( -7 ) MOD -3,DS2)", "DS2", lambda _: LiteralExpr(7) % -3, Int, Int, 1),
    ],
    ids=[
        "divide-by-0",
        "0-to-the-0",
        "mod-of-a-difference",
        "hex-wraps-at-the-scan",
        "abs-mod-0",
        "abs-divide-by-0",
        "abs-0-to-the-0",
        "abs-mod-negative",
    ],
)
def test_imported_constants_compute_what_math_computes(
    token, dest, build, dest_type, operand_type, value, runner_factory, monkeypatch
) -> None:
    """Python must not compute an operation between two constants when it loads the file."""
    operand = "DH1" if dest == "DH2" else "DS1"
    ns = exec_with_source(ladder_to_pyrung(_bundle(f"copy(1,{operand})", token)))
    results = _on_every_backend(ns["logic"], monkeypatch, [ns[dest]], scans=1)
    assert set(results.values()) == {(value,)}
    exported = pyrung_to_ladder(ns["logic"], ns["mapping"])
    again = exec_with_source(ladder_to_pyrung(exported))
    plc = PLC(again["logic"])
    plc.step()
    assert plc.current_state.tags[ns[dest].name] == value
    imported = runner_factory(ns["logic"])
    imported.step()
    hand = runner_factory(_hand_written(build, dest_type, operand_type))
    hand.step()
    got = imported.current_state.tags
    want = hand.current_state.tags
    assert got[ns[dest].name] == want["Dest"] == value
    assert [got[f] for f in _FAULTS] == [want[f] for f in _FAULTS]
    assert got["fault.division_error"] is ("/ 0," in token)


@pytest.mark.parametrize(
    ("text", "dest", "uses", "guess"),
    [
        ("( DH1 * DH2 ) AND DH3 AND DH4", "DH5,mode=hex", "'AND' twice", "the two from the left"),
        ("- ( DH1 ) * DH2", "DH3,mode=hex", f"{NEGATE_BRACKET} and '*'", "the minus sign first"),
        ("- PI * 2", "DF1", "a minus sign before PI and '*'", "the minus sign first"),
        ("- DF2 * 4", "DF3", f"{NEGATE_ADDRESS} and '*'", "the minus sign first"),
        ("- ( DF1 ) * DF2", "DF3", f"{NEGATE_BRACKET} and '*'", "the minus sign first"),
        ("+ PI * 2", "DF1", f"{PLUS_SIGN} and '*'", "the plus sign first"),
    ],
)
def test_imported_formula_with_an_unconfirmed_pair_warns(
    text: str, dest: str, uses: str, guess: str
) -> None:
    with pytest.warns(UserWarning) as caught:
        ladder_to_pyrung(_bundle(f"math({text},{dest})"))
    messages = [str(w.message) for w in caught]
    assert messages == [
        f"The Math formula {text!r} uses {uses} with no brackets between the two. pyrung "
        f"applies {guess}, but no one has checked that order on a CLICK yet. Add brackets to "
        "the formula in CLICK to set the order."
    ]
    assert all(m.isascii() for m in messages)


#: Part B of reading round b16: (case, formula as CLICK displays it, hex mode, register values,
#: destination, value the PLC stored, whether SC43 was on after the Math). The model of a CLICK
#: computes no function but SQRT, so these are not in READINGS, and they settle no pair.
#:
#: - b1a and b1b: a minus sign before ABS and SUM binds to the function's result first.
#: - b2 and b2x: a minus sign before RSH applies to the shift's result. A minus sign on the
#:   register, then a shift by 1 that rounds toward 0, stores the same value for every
#:   register value, so these two cases do not tell those two apart.
#: - b3a to b3c: the step after AND, XOR and OR reads the result as 16 unsigned bits. b3ax ends
#:   at 65536, so it stores 0000h and sets SC43.
#: - b3d: the OR cuts the product 10000h to 16 bits, with no flag. ``hex_divmod/product_not_cut``
#:   shows that a hex Math does not cut a product before a MOD, so the cut comes from the OR.
#:   b3d alone cannot tell an OR that cuts from a product cut before the OR.
PART_B_READINGS: list[tuple[str, str, bool, dict[str, int], str, int, bool]] = [
    ("b16-b1a", "- ABS ( DS101 ) + 2", False, {"DS101": 4}, "DS1", -2, False),
    ("b16-b1b", "- SUM ( DS111 : DS112 ) ^ 2", False, {"DS111": 4, "DS112": 5}, "DS2", 81, False),
    ("b16-b2", "- RSH ( DH100 , 1h ) AND FFFFh", True, {"DH100": 8}, "DH3", 0xFFFC, False),
    ("b16-b2x", "- RSH ( DH131 , 1h ) AND FFFFh", True, {"DH131": 9}, "DH4", 0xFFFC, False),
    ("b16-b3a", "( ( 0h - DH141 ) AND FFFFh ) + 1h", True, {"DH141": 2}, "DH5", 0xFFFF, False),
    ("b16-b3ax", "( ( 0h - DH151 ) AND FFFFh ) + 1h", True, {"DH151": 1}, "DH6", 0x0000, True),
    ("b16-b3b", "( ( 0h - DH161 ) XOR 0h ) - 1h", True, {"DH161": 2}, "DH7", 0xFFFD, False),
    (
        "b16-b3c",
        "( ( 0h - DH171 ) OR 0h ) / DH172",
        True,
        {"DH171": 2, "DH172": 3},
        "DH8",
        0x5554,
        False,
    ),
    (
        "b16-b3d",
        "( ( DH181 * DH182 ) OR 0h ) / 2h",
        True,
        {"DH181": 0x100, "DH182": 0x100},
        "DH9",
        0x0000,
        False,
    ),
]


@pytest.mark.parametrize(
    ("text", "hex_mode", "registers", "dest", "stored", "out_of_range"),
    [reading[1:] for reading in PART_B_READINGS],
    ids=[reading[0] for reading in PART_B_READINGS],
)
def test_imported_formula_stores_what_round_b16_part_b_read(
    text: str,
    hex_mode: bool,
    registers: dict[str, int],
    dest: str,
    stored: int,
    out_of_range: bool,
    monkeypatch,
) -> None:
    copies = [f"copy({v:X}h,{k})" if hex_mode else f"copy({v},{k})" for k, v in registers.items()]
    mode = ",mode=hex" if hex_mode else ""
    with warnings.catch_warnings():
        # b2 pairs a minus sign before a function with AND, which no reading in the model
        # settles, so its import warns.
        warnings.simplefilter("ignore")
        ns = exec_with_source(ladder_to_pyrung(_bundle(*copies, f"math({text},{dest}{mode})")))
    fault = system.fault.out_of_range
    results = _on_every_backend(ns["logic"], monkeypatch, [ns[dest], fault], scans=1)
    assert set(results.values()) == {(stored, out_of_range)}, results


_SC40, _SC43, _SC46 = "division_error", "out_of_range", "math_operation_error"

#: The registers of round b20's Part C: +0.0, the -1.0 that stages -0.0 in DF110, 5.0 and -5.0.
_B20_ZEROS = {"DF111": 0.0, "DF112": -1.0, "DF110": -0.0, "DF113": 5.0, "DF114": -5.0}

#: Round b20: (case, formula as CLICK displays it, hex mode, register values, destination,
#: value the PLC stored, the flags of SC40, SC43 and SC46 that were on after the Math). The
#: round staged -0.0 in DF110 as ``DF111 * DF112``, with 0.0 and -1.0. Each destination held a
#: sentinel value before the Math, such as -12345.0 in a DF.
ROUND_B20_READINGS: list[tuple[str, str, bool, dict[str, float], str, float, set[str]]] = [
    # Part A. A hex minus sign before a typed constant applies first.
    ("b20-a_and", "- 2h AND 3h", True, {}, "DH1", 0x0002, set()),
    ("b20-a_or", "- 2h OR 1h", True, {}, "DH2", 0xFFFF, set()),
    ("b20-a_xor", "- 3h XOR 1h", True, {}, "DH3", 0xFFFC, set()),
    ("b20-a_add", "- 5h + 1h", True, {}, "DH4", 0xFFFC, {_SC43}),
    ("b20-a_sub", "- 5h - 1h", True, {}, "DH5", 0xFFFA, {_SC43}),
    # The staging check, and Part B. A minus sign before a bracket applies first.
    ("b20-s0", "DF111 * DF112", False, {"DF111": 0.0, "DF112": -1.0}, "DF6", -0.0, set()),
    ("b20-b_div", "- ( DF121 ) / DF122", False, {"DF121": 5.0, "DF122": 0.0}, "DF7", 0.0, {_SC40}),
    (
        "b20-b_mod",
        "- ( DF121 ) MOD DF122",
        False,
        {"DF121": 5.0, "DF122": 0.0},
        "DF8",
        0.0,
        {_SC46},
    ),
    # Part C. Each product of a zero has the sign that a double gives.
    *(
        (f"b20-c{n:02}", f"{left} * {right}", False, _B20_ZEROS, f"DF{8 + n}", stored, set())
        for n, (left, right, stored) in enumerate(
            [
                ("DF111", "DF111", 0.0),
                ("DF111", "DF110", -0.0),
                ("DF110", "DF111", -0.0),
                ("DF110", "DF110", 0.0),
                ("DF111", "DF113", 0.0),
                ("DF113", "DF111", 0.0),
                ("DF110", "DF113", -0.0),
                ("DF113", "DF110", -0.0),
                ("DF111", "DF114", -0.0),
                ("DF114", "DF111", -0.0),
                ("DF110", "DF114", 0.0),
                ("DF114", "DF110", 0.0),
            ],
            start=1,
        )
    ),
    ("b20-c13", "- DF110", False, _B20_ZEROS, "DF21", 0.0, set()),
    ("b20-c14", "SQRT ( DF114 )", False, {"DF114": -5.0}, "DF22", 0.0, {_SC46}),
    # Part D. Each plus stores 5 times the register.
    ("b20-d_dec", "+ 5 * DS401", False, {"DS401": 3}, "DS23", 15, set()),
    ("b20-d_hex", "+ 5h * DH401", True, {"DH401": 3}, "DH24", 0x000F, set()),
    # Part E. Two operators of one tier apply from the left in each mode.
    (
        "b20-e_hex",
        "DH411 MOD DH412 MOD DH413",
        True,
        {"DH411": 7, "DH412": 5, "DH413": 3},
        "DH25",
        0x0002,
        set(),
    ),
    ("b20-e_dec", "DS411 / DS412 MOD 3", False, {"DS411": 7, "DS412": 2}, "DF26", 0.5, set()),
]


def _b20_rungs(text: str, hex_mode: bool, registers: dict[str, float], dest: str) -> list[str]:
    """Return the rungs that stage *registers* and a sentinel in *dest*, then run *text*.

    A -0.0 in DF110 comes from ``DF111 * DF112``, as the round staged it.
    """
    tokens = []
    for name, value in registers.items():
        if name == "DF110":
            tokens.append("math(DF111 * DF112,DF110)")
        elif hex_mode:
            tokens.append(f"copy({int(value):X}h,{name})")
        else:
            tokens.append(f"copy({value},{name})")
    sentinel = {"DS": "-12345", "DH": "BEEFh", "DF": "-12345.0"}[dest[:2]]
    mode = ",mode=hex" if hex_mode else ""
    return [*tokens, f"copy({sentinel},{dest})", f"math({text},{dest}{mode})"]


#: The registers of round b22's Part A: +0.0, the -1.0 that stages -0.0 in DF110, and 0.5,
#: 3.0 and 1.0E+30 with each sign. A DF holds 1.0E+30 as a float32.
_B22_ZEROS = {
    "DF111": 0.0,
    "DF112": -1.0,
    "DF110": -0.0,
    "DF113": 0.5,
    "DF114": -0.5,
    "DF115": 3.0,
    "DF116": -3.0,
    "DF117": float32(1.0e30),
    "DF118": float32(-1.0e30),
}

#: Round b22, on the same CLICK on 2026-11-27, in the shape of :data:`ROUND_B20_READINGS`.
#: Part A multiplies each zero by each value of DF113 to DF118, with the zero on each side. Each
#: product stored the sign that a double gives.
ROUND_B22_READINGS: list[tuple[str, str, bool, dict[str, float], str, float, set[str]]] = [
    ("b22-s0", "DF111 * DF112", False, _B22_ZEROS, "DF1", -0.0, set()),
    *(
        (
            f"b22-a{4 * n + k + 1:02}",
            f"{left} * {right}",
            False,
            _B22_ZEROS,
            f"DF{4 * n + k + 2}",
            math.copysign(0.0, _B22_ZEROS[left] * _B22_ZEROS[right]),
            set(),
        )
        for n, x in enumerate(("DF113", "DF114", "DF115", "DF116", "DF117", "DF118"))
        for k, (left, right) in enumerate([("DF111", x), (x, "DF111"), ("DF110", x), (x, "DF110")])
    ),
    ("b22-q1", "SQRT ( DF114 )", False, _B22_ZEROS, "DF26", 0.0, {_SC46}),
    ("b22-q2", "SQRT ( DF118 )", False, _B22_ZEROS, "DF27", 0.0, {_SC46}),
    ("b22-q3", "SQRT ( DF111 )", False, _B22_ZEROS, "DF28", 0.0, set()),
    ("b22-q4", "SQRT ( DF110 )", False, _B22_ZEROS, "DF29", -0.0, set()),
    # Part B. A plus changes nothing.
    ("b22-b_ds", "+ DS401 * 2", False, {"DS401": 5}, "DS30", 10, set()),
    ("b22-b_br", "+ ( DS401 - 3 ) * 2", False, {"DS401": 5}, "DS31", 4, set()),
    ("b22-b_hex", "+ DH401 * 2h", True, {"DH401": 3}, "DH32", 0x0006, set()),
    # Part C. A hex / rounds down, and a hex MOD truncates. A product stays whole past 32 bits.
    ("b22-c_div2", "- 5h / 2h", True, {}, "DH33", 0xFFFD, {_SC43}),
    ("b22-c_div3", "- 5h / 3h", True, {}, "DH34", 0xFFFE, {_SC43}),
    ("b22-c_mulk", "- FFFFh * FF00h", True, {}, "DH35", 0xFF00, {_SC43}),
    (
        "b22-c_mulr",
        "- DH431 * DH432",
        True,
        {"DH431": 0xFFFF, "DH432": 0xFF00},
        "DH36",
        0xFF00,
        {_SC43},
    ),
    ("b22-c_mulk7", "- FFFFh * FF00h MOD 7h", True, {}, "DH37", 0xFFFB, {_SC43}),
    (
        "b22-c_mulr7",
        "- DH431 * DH432 MOD 7h",
        True,
        {"DH431": 0xFFFF, "DH432": 0xFF00},
        "DH38",
        0xFFFB,
        {_SC43},
    ),
    ("b22-c_div", "( 0h - DH421 ) / 3h", True, {"DH421": 1}, "DH39", 0xFFFF, {_SC43}),
    ("b22-c_mod", "( 0h - DH421 ) MOD 3h", True, {"DH421": 1}, "DH40", 0xFFFF, {_SC43}),
]

_B24_DH = {"DH1": 0xFFFF, "DH2": 0xFFFF}

#: Round b24, on the same CLICK on 2026-11-27, in the shape of :data:`ROUND_B20_READINGS`. The
#: w cases multiply FFFFh by itself two to five times. A CLICK rounds w4's and w5's products to
#: a double. z1 puts a plus before -0.0.
ROUND_B24_READINGS: list[tuple[str, str, bool, dict[str, float], str, float, set[str]]] = [
    ("b24-w1", "DH1 * DH2 MOD 7h", True, _B24_DH, "DH4", 0x0001, set()),
    ("b24-w2", "- DH1 * DH2 MOD 7h", True, _B24_DH, "DH5", 0xFFFF, {_SC43}),
    ("b24-w3", "DH1 * DH2 * DH1 MOD FBh", True, _B24_DH, "DH6", 0x0013, set()),
    ("b24-w4", "DH1 * DH2 * DH1 * DH2 MOD FBh", True, _B24_DH, "DH7", 0x00CC, set()),
    ("b24-w5", "DH1 * DH2 * DH1 * DH2 * DH1 MOD FBh", True, _B24_DH, "DH8", 0x001B, set()),
    ("b24-z1", "+ DF110", False, {"DF111": 0.0, "DF112": -1.0, "DF110": -0.0}, "DF2", -0.0, set()),
]

#: Round b23, on the same CLICK on 2026-11-27, in the shape of :data:`ROUND_B20_READINGS`. Each
#: divides by a value below 0.
ROUND_B23_READINGS: list[tuple[str, str, bool, dict[str, float], str, float, set[str]]] = [
    ("b23-d1", "5h / ( 0h - 3h )", True, {}, "DH1", 0xFFFE, {_SC43}),
    ("b23-d2", "( 0h - 7h ) / ( 0h - 2h )", True, {}, "DH2", 0x0003, set()),
    ("b23-m1", "5h MOD ( 0h - 3h )", True, {}, "DH3", 0x0002, set()),
    ("b23-m2", "( 0h - 7h ) MOD ( 0h - 2h )", True, {}, "DH4", 0xFFFF, {_SC43}),
]


@pytest.mark.parametrize(
    ("text", "hex_mode", "registers", "dest", "stored", "flags"),
    [reading[1:] for reading in ROUND_B20_READINGS],
    ids=[reading[0] for reading in ROUND_B20_READINGS],
)
def test_imported_formula_stores_what_round_b20_read(
    text: str,
    hex_mode: bool,
    registers: dict[str, float],
    dest: str,
    stored: float,
    flags: set[str],
    monkeypatch,
) -> None:
    _check_round_reading(text, hex_mode, registers, dest, stored, flags, monkeypatch)


@pytest.mark.parametrize(
    ("text", "hex_mode", "registers", "dest", "stored", "flags"),
    [reading[1:] for reading in ROUND_B22_READINGS],
    ids=[reading[0] for reading in ROUND_B22_READINGS],
)
def test_imported_formula_stores_what_round_b22_read(
    text: str,
    hex_mode: bool,
    registers: dict[str, float],
    dest: str,
    stored: float,
    flags: set[str],
    monkeypatch,
) -> None:
    _check_round_reading(text, hex_mode, registers, dest, stored, flags, monkeypatch)


@pytest.mark.parametrize(
    ("text", "hex_mode", "registers", "dest", "stored", "flags"),
    [reading[1:] for reading in ROUND_B23_READINGS],
    ids=[reading[0] for reading in ROUND_B23_READINGS],
)
def test_imported_formula_stores_what_round_b23_read(
    text: str,
    hex_mode: bool,
    registers: dict[str, float],
    dest: str,
    stored: float,
    flags: set[str],
    monkeypatch,
) -> None:
    _check_round_reading(text, hex_mode, registers, dest, stored, flags, monkeypatch)


@pytest.mark.parametrize(
    ("text", "hex_mode", "registers", "dest", "stored", "flags"),
    [reading[1:] for reading in ROUND_B24_READINGS],
    ids=[reading[0] for reading in ROUND_B24_READINGS],
)
def test_imported_formula_stores_what_round_b24_read(
    text: str,
    hex_mode: bool,
    registers: dict[str, float],
    dest: str,
    stored: float,
    flags: set[str],
    monkeypatch,
) -> None:
    _check_round_reading(text, hex_mode, registers, dest, stored, flags, monkeypatch)


def _check_round_reading(
    text: str,
    hex_mode: bool,
    registers: dict[str, float],
    dest: str,
    stored: float,
    flags: set[str],
    monkeypatch,
) -> None:
    """Import *text* as :func:`_b20_rungs` stages it, and check each backend against a CLICK."""
    with warnings.catch_warnings():
        # The readings settle the order of each formula, so none warns.
        warnings.simplefilter("error")
        source = ladder_to_pyrung(_bundle(*_b20_rungs(text, hex_mode, registers, dest)))
    ns = exec_with_source(source)
    faults = [getattr(system.fault, name) for name in (_SC40, _SC43, _SC46)]
    results = _on_every_backend(ns["logic"], monkeypatch, [ns[dest], *faults], scans=1)
    want = (stored, *(name in flags for name in (_SC40, _SC43, _SC46)))
    for backend, got in results.items():
        assert got == want, backend
        # == cannot tell -0.0 from +0.0, so compare the sign too.
        assert math.copysign(1, got[0]) == math.copysign(1, stored), backend


#: Reading round b21, on the same CLICK on 2026-11-26: (case, formula as CLICK displays it,
#: register values, destination, value the PLC stored, whether SC43 was on after the Math).
#: SC40 and SC46 stayed off. Each n case is its p case without the plus.
ROUND_B21_READINGS: list[tuple[str, str, dict[str, int], str, int, bool]] = [
    ("b21-p1", "+ ( 0h - DH421 ) + 1h", {"DH421": 1}, "DH1", 0x0000, False),
    ("b21-p2", "+ 0h - DH421 + 1h", {"DH421": 1}, "DH2", 0x0000, False),
    ("b21-p3", "+ ( 0h - DH421 ) * 2h", {"DH421": 1}, "DH3", 0xFFFE, True),
    ("b21-p2b", "+ 0h - DH422 + 1h", {"DH422": 2}, "DH4", 0xFFFF, True),
    ("b21-p3b", "+ ( 0h - DH421 ) * 1h", {"DH421": 1}, "DH5", 0xFFFF, True),
    ("b21-p4", "+ ( 0h - DH421 ) / 2h", {"DH421": 1}, "DH6", 0xFFFF, True),
    ("b21-n1", "( 0h - DH421 ) + 1h", {"DH421": 1}, "DH7", 0x0000, False),
    ("b21-n2", "0h - DH421 + 1h", {"DH421": 1}, "DH8", 0x0000, False),
    ("b21-n3", "( 0h - DH421 ) * 2h", {"DH421": 1}, "DH9", 0xFFFE, True),
    ("b21-n2b", "0h - DH422 + 1h", {"DH422": 2}, "DH10", 0xFFFF, True),
    ("b21-n3b", "( 0h - DH421 ) * 1h", {"DH421": 1}, "DH11", 0xFFFF, True),
    ("b21-n4", "( 0h - DH421 ) / 2h", {"DH421": 1}, "DH12", 0xFFFF, True),
]


@pytest.mark.parametrize(
    ("text", "registers", "dest", "stored", "out_of_range"),
    [reading[1:] for reading in ROUND_B21_READINGS],
    ids=[reading[0] for reading in ROUND_B21_READINGS],
)
def test_imported_formula_stores_what_round_b21_read(
    text: str, registers: dict[str, int], dest: str, stored: int, out_of_range: bool, monkeypatch
) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        source = ladder_to_pyrung(_bundle(*_b20_rungs(text, True, registers, dest)))
    ns = exec_with_source(source)
    faults = [getattr(system.fault, name) for name in (_SC40, _SC43, _SC46)]
    results = _on_every_backend(ns["logic"], monkeypatch, [ns[dest], *faults], scans=1)
    for backend, got in results.items():
        assert got == (stored, False, out_of_range, False), backend


@pytest.mark.parametrize(
    ("formula", "stored"),
    [
        # math_flags read - DH with DH = 2h, which stores FFFEh and sets SC43. Round b20 read
        # - 5h + 1h, which stores FFFCh and sets SC43. So a minus sign before a typed constant
        # gives a value below 0, as one before a DH does.
        ("- 2h", 0xFFFE),
        # The same value as - DH3 MOD DH1 with DH3 = 5h. A constant of FFFBh would give 2h.
        ("- 5h MOD DH1", 0xFFFE),
    ],
)
def test_imported_hex_minus_sign_before_a_constant_runs_in_the_scan(
    formula: str, stored: int, runner_factory
) -> None:
    source = ladder_to_pyrung(_bundle("copy(3h,DH1)", f"math({formula},DH2,mode=hex)"))
    assert "LiteralExpr(0x" in _math_line(source)
    plc = runner_factory(exec_with_source(source)["logic"])
    plc.step()
    tags = plc.current_state.tags
    assert tags["DH2"] & 0xFFFF == stored
    assert tags["fault.out_of_range"] is True


# ---------------------------------------------------------------------------
# Three outputs: the imported program, the generated P1AM code and the CLICK export
# ---------------------------------------------------------------------------

_CONSTANT = re.compile(r"(?<![\w-])-?\b[0-9][0-9A-Fa-f]*h?\b")
_REGISTER = re.compile(r"\b(DS|DH|DF|DD)\d+\b")


def _staged(text: str, hex_mode: bool, registers: dict[str, float], first: int):
    """Return *text* with each constant and register on a fresh register, and their values.

    Python computes an operation of two constants when it reads the source, so the
    imported program would hold only the result. A register keeps the operation. Each
    register moves to a fresh register of its own bank.
    """
    values: dict[str, float] = {}

    def fresh(value: float, bank: str = "DH" if hex_mode else "DS") -> str:
        name = f"{bank}{first + len(values)}"
        values[name] = value
        return name

    def constant(match: re.Match[str]) -> str:
        token = match.group()
        return fresh(int(token[:-1], 16) if token.endswith("h") else int(token))

    text = _REGISTER.sub(lambda m: fresh(registers[m.group()], m.group(1)), text)
    return _CONSTANT.sub(constant, text), values


def _program(readings):
    """Import one CLICK program that stages and runs each formula of *readings*.

    Return the program, its tag map, and the formula and destination tag of each reading.
    """
    tokens: list[str] = []
    formulas: list[str] = []
    dests: list[str] = []
    # The staged DHs end below DH301, where the results start.
    free = {False: 1, True: 1}
    for n, (_, text, hex_mode, registers, _) in enumerate(readings):
        staged, values = _staged(text, hex_mode, registers, free[hex_mode])
        free[hex_mode] += len(values)
        tokens.extend(f"copy({value},{name})" for name, value in values.items())
        dest = f"DH{301 + n}" if hex_mode else f"DF{1 + n}"
        mode = ",mode=hex" if hex_mode else ""
        tokens.append(f"math({staged},{dest}{mode})")
        formulas.append(staged)
        dests.append(dest)
    ns = exec_with_source(ladder_to_pyrung(_bundle(*tokens)))
    return ns["logic"], ns["mapping"], formulas, [ns[dest] for dest in dests]


def _close(got: Any, want: float) -> bool:
    return math.isclose(float(got), want, abs_tol=1e-9)


@pytest.fixture(scope="module")
def confirmed() -> tuple[Any, ...]:
    # The program moves the registers and the decimal-digit constants of each reading onto
    # registers of its own. A formula of READ_FORMULAS that moves no longer matches a reading's
    # tree, so it warns. No other formula warns.
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        program = _program(READINGS)
    formulas = program[2]
    moved = {
        formulas[n]
        for n, (_, text, hex_mode, _, _) in enumerate(READINGS)
        if (text, hex_mode) in READ_FORMULAS and formulas[n] != text
    }
    assert {str(w.message).split("'")[1] for w in caught} == moved
    return program


def test_imported_formula_gives_the_plc_value_on_every_backend(confirmed, monkeypatch) -> None:
    logic, _mapping, _formulas, dests = confirmed
    results = _on_every_backend(logic, monkeypatch, dests, scans=1)
    for backend, values in results.items():
        wrong = {
            reading[0]
            for reading, got in zip(READINGS, values, strict=True)
            if not _close(got, reading[4])
        }
        assert not wrong, backend


def test_imported_formula_exports_what_click_reads_the_same(confirmed) -> None:
    logic, mapping, formulas, dests = confirmed
    exported = pyrung_to_ladder(logic, mapping)
    tokens = [row[-1] for row in exported.main_rows if row[-1].startswith("math(")]
    assert len(tokens) == len(formulas)
    for reading, token, formula in zip(READINGS, tokens, formulas, strict=True):
        hex_mode = reading[2]
        written = token[len("math(") : -1].rsplit(",", 2)[0]
        want = parse_with_unconfirmed_pairs(formula, hex_mode=hex_mode)[0]
        got = parse_with_unconfirmed_pairs(written, hex_mode=hex_mode)[0]
        assert _as_subtraction(got) == _as_subtraction(want), reading[0]

    # The export, imported again, still computes what the PLC read.
    ns = exec_with_source(ladder_to_pyrung(exported))
    plc = PLC(ns["logic"])
    plc.step()
    tags = plc.current_state.tags
    wrong = {
        reading[0]
        for reading, dest in zip(READINGS, dests, strict=True)
        if not _close(tags[dest.name], reading[4])
    }
    assert not wrong


def test_imported_formula_with_an_unconfirmed_pair_imports_with_a_warning() -> None:
    from tests.conformance.math.test_math_order import PROBES, _registers

    readings = []
    for case_id, (hex_mode, formulas) in PROBES.items():
        prefix = "DH" if hex_mode else "DS"
        for formula, value, _ in formulas:
            typed, staged = _registers([formula], prefix, 1)
            registers = {f"{prefix}{address}": number for address, number in staged.items()}
            readings.append((case_id, typed[0], hex_mode, registers, value))
    unconfirmed = [
        text
        for _, text, hex_mode, _, _ in readings
        if parse_click_formula(text, hex_mode=hex_mode) is None
    ]
    with pytest.warns(UserWarning, match="no one has checked that order on a CLICK yet") as caught:
        logic, _mapping, _formulas, dests = _program(readings)
    assert len(caught) == len(unconfirmed)

    # The import applies the order that the warning names. A probe stores into an Int, which
    # drops the fraction.
    plc = PLC(logic)
    plc.step()
    tags = plc.current_state.tags
    wrong = [
        (reading[0], tags[dest.name], reading[4])
        for reading, dest in zip(readings, dests, strict=True)
        if not _close(
            math.trunc(tags[dest.name]), reading[4] & 0xFFFF if reading[2] else reading[4]
        )
    ]
    assert wrong == []
