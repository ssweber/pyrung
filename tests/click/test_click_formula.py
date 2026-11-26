"""Read a CLICK Math formula in CLICK's order, and import it to pyrung source that agrees.

A CLICK 4.00 on a C2-03CPU was read running each formula of :data:`READINGS` on 2026-11-25
and 2026-11-26. Each case is named after the reading round and its case number. The values
read are ground truth. ``test_click_formula_parse_*`` checks the parse
against them with an evaluator of its own. ``test_imported_formula_*`` checks pyrung's three
outputs: the imported program (in-process and on the generated P1AM code) and its CLICK export.
"""

from __future__ import annotations

import math
import re
from typing import Any

import pytest

from pyrung import LiteralExpr
from pyrung.click import ladder_to_pyrung, pyrung_to_ladder
from pyrung.click.codegen import formula as formula_module
from pyrung.click.codegen.formula import (
    _TIER_OF,
    CONFIRMED_PAIRS,
    NEGATE_ADDRESS,
    NEGATE_GROUP,
    READ_FORMULAS,
    Address,
    BinaryOp,
    Call,
    Negate,
    Number,
    Pi,
    Pointer,
    RangeCall,
    pair_kind,
    parse_click_formula,
    parse_with_unconfirmed_pairs,
)
from pyrung.click.ladder.types import LadderBundle
from pyrung.conformance._formula import _as_subtraction
from pyrung.core import PLC, Int, Program, Word, copy, rung
from pyrung.core import math as math_instruction
from tests.circuitpy.test_codegen import _on_every_backend
from tests.click.helpers import exec_with_source
from tests.conformance.math.formula_values import click_value as _click_value
from tests.conformance.math.formula_values import regroupings

#: (case, formula as CLICK displays it, hex mode, register values, value the PLC read).
#: A hex value is the 16 bits a DH holds.
READINGS: list[tuple[str, str, bool, dict[str, int], float]] = [
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
    assert _click_value(tree, hex_mode, registers) == value


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


def _settled_pairs() -> dict[frozenset[str], set[str]]:
    """Return each pair of operators that the readings settle, with the rounds of those readings.

    Two operators could apply in three orders. Either one applies first, or both apply from
    the left. Only two operators that meet have one other grouping, so only they are tested.
    A reading rules an order out when the formula, grouped in that order, gives another
    value. A pair is settled when the readings rule out each order but the parse's. So a pair
    from two levels needs a reading with the tighter operator on the right, and a pair from
    one level needs a reading of each order.
    """
    ruled_out: dict[frozenset[str], set[str]] = {}
    rounds: dict[frozenset[str], set[str]] = {}
    for case, text, hex_mode, registers, value in READINGS:
        tree = parse_with_unconfirmed_pairs(text, hex_mode=hex_mode)[0]
        for parent, child, child_is_left, regrouped in regroupings(tree):
            try:
                if _click_value(regrouped, hex_mode, registers) == value:
                    continue
            except ZeroDivisionError:
                # This model of a CLICK has no value for a division by zero.
                continue
            parent, child = pair_kind(parent), pair_kind(child)
            pair = frozenset((parent, child))
            # With the child on the right, the regrouping also stands for both from the left.
            orders = {parent} if child_is_left else {parent, "left"}
            ruled_out.setdefault(pair, set()).update(orders)
            rounds.setdefault(pair, set()).add(case.split("-")[0])
    settled = {}
    for pair, orders in ruled_out.items():
        if len(pair) == 1:
            needed = set(pair)
        elif pair & {NEGATE_ADDRESS, NEGATE_GROUP}:
            needed = set(pair - {NEGATE_ADDRESS, NEGATE_GROUP})
        else:
            first, second = sorted(pair)
            needed = {first, second, "left"}
            if _TIER_OF[first] != _TIER_OF[second]:
                needed.discard(min(pair, key=_TIER_OF.__getitem__))
            else:
                needed.discard("left")
        if needed <= orders:
            settled[pair] = rounds[pair]
    return settled


def test_confirmed_pairs_are_the_pairs_that_the_readings_settle() -> None:
    listed = {frozenset((a, b)): set(rounds.split(", ")) for a, b, rounds in CONFIRMED_PAIRS}
    assert len(listed) == len(CONFIRMED_PAIRS)
    assert listed == _settled_pairs()


def test_read_formulas_are_the_readings_whose_pairs_are_not_all_settled(monkeypatch) -> None:
    monkeypatch.setattr(formula_module, "_read_trees", list)
    unsettled = [
        (text, hex_mode)
        for _, text, hex_mode, _, _ in READINGS
        if parse_with_unconfirmed_pairs(text, hex_mode=hex_mode)[1]
    ]
    assert list(READ_FORMULAS) == unsettled


_BRACKET = "a minus sign before a bracket"


@pytest.mark.parametrize(
    ("text", "hex_mode", "pairs"),
    [
        ("DS1 - DS2 * 3", False, [("-", "*")]),
        ("DS1 * 3 - DS2", False, [("*", "-")]),
        ("DH1 - DH2 AND 3h", True, [("-", "AND")]),
        ("DH1 OR DH2 - 1h", True, [("OR", "-")]),
        ("DH1 - DH2 XOR 3h", True, [("-", "XOR")]),
        ("SQRT ( DS1 - DS2 * 3 )", False, [("-", "*")]),
        # Two operators that do not meet still count.
        ("DS1 - DS2 + DS3 * DS4", False, [("-", "+"), ("-", "*")]),
        ("DS1 + DS2 - DS3", False, [("+", "-")]),
        ("DS1 + DS2 + DS3", False, [("+", "+")]),
        ("DS1 MOD DS2 MOD 3", False, [("MOD", "MOD")]),
        ("DH1 / DH2 MOD 3h", True, [("/", "MOD")]),
        ("DH1 XOR DH2 * 3h", True, [("XOR", "*")]),
        # A minus sign pairs with each operator to its right.
        ("- ( DS1 ) ^ 2", False, [(_BRACKET, "^")]),
        ("- ( DS1 ) * DS2 + DS3", False, [(_BRACKET, "*"), (_BRACKET, "+")]),
        ("DS1 * - ( DS2 ) + DS3", False, [(_BRACKET, "+")]),
        ("- SQRT ( DS1 ) ^ 2", False, [("a minus sign before a function", "^")]),
        ("- PI ^ 2", False, [("a minus sign before PI", "^")]),
        ("- DH[DS1] AND 3h", True, [("a minus sign before a pointer", "AND")]),
        ("- - DS1 ^ 2", False, [("a minus sign before another minus sign", "^")]),
        ("- DS2 * 4", False, [(NEGATE_ADDRESS, "*")]),
        ("- DS1 / 2", False, [(NEGATE_ADDRESS, "/")]),
        ("- DS2 MOD 3", False, [(NEGATE_ADDRESS, "MOD")]),
    ],
)
def test_click_formula_parse_is_none_for_an_unconfirmed_pair(
    text: str, hex_mode: bool, pairs: list[tuple[str, str]]
) -> None:
    assert parse_click_formula(text, hex_mode=hex_mode) is None
    assert parse_with_unconfirmed_pairs(text, hex_mode=hex_mode)[1] == tuple(pairs)


def test_the_words_for_each_minus_sign_name_its_operator() -> None:
    for words in (_BRACKET, "a minus sign before PI", "a minus sign before a pointer"):
        assert pair_kind(words) == NEGATE_GROUP
    assert pair_kind(NEGATE_ADDRESS) == NEGATE_ADDRESS


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
        ("- DS1 * 3", False),
        ("- DS1 MOD 3", False),
        # Readings settled these pairs.
        ("DS1 * 2 + 3", False),
        ("DH1 AND DH2 OR DH3", True),
    ],
)
def test_click_formula_parse_confirms_an_order_that_the_grammar_or_a_reading_gives(
    text: str, hex_mode: bool
) -> None:
    assert parse_click_formula(text, hex_mode=hex_mode) is not None


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
        ("DH1 / DH2 MOD DH3", "DH4,mode=hex", "'/' and 'MOD'", "the two from the left"),
        ("DS1 - DS2 * 3", "DF1", "'-' and '*'", "'*' first"),
        ("- ( DS1 ) ^ 2", "DF1", f"{_BRACKET} and '^'", "the minus sign first"),
        ("- PI ^ 2", "DF1", "a minus sign before PI and '^'", "the minus sign first"),
        ("DS1 MOD DS2 MOD 3", "DS3", "'MOD' twice", "the two from the left"),
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


# ---------------------------------------------------------------------------
# Three outputs: the imported program, the generated P1AM code and the CLICK export
# ---------------------------------------------------------------------------

_CONSTANT = re.compile(r"(?<![\w-])-?\b[0-9][0-9A-Fa-f]*h?\b")
_REGISTER = re.compile(r"\b(DS|DH)\d+\b")


def _staged(text: str, hex_mode: bool, registers: dict[str, int], first: int):
    """Return *text* with each constant and register on a fresh register, and their values.

    Python computes an operation of two constants when it reads the source, so the
    imported program would hold only the result. A register keeps the operation.
    """
    bank = "DH" if hex_mode else "DS"
    values: dict[str, int] = {}

    def fresh(value: int) -> str:
        name = f"{bank}{first + len(values)}"
        values[name] = value
        return name

    def constant(match: re.Match[str]) -> str:
        token = match.group()
        return fresh(int(token[:-1], 16) if token.endswith("h") else int(token))

    text = _REGISTER.sub(lambda m: fresh(registers[m.group()]), text)
    return _CONSTANT.sub(constant, text), values


def _program(readings):
    """Import one CLICK program that stages and runs each formula of *readings*.

    Return the program, its tag map, and the formula and destination tag of each reading.
    """
    tokens: list[str] = []
    formulas: list[str] = []
    dests: list[str] = []
    free = {False: 101, True: 101}
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
    # The program moves each reading onto registers of its own, so the formulas of
    # READ_FORMULAS no longer match a reading's tree, and each warns once.
    with pytest.warns(UserWarning) as caught:
        program = _program(READINGS)
    assert len(caught) == len(READ_FORMULAS)
    return program


def test_imported_formula_gives_the_plc_value_on_every_backend(confirmed, monkeypatch) -> None:
    logic, _mapping, _formulas, dests = confirmed
    results = _on_every_backend(logic, monkeypatch, dests, scans=1)
    for backend, values in results.items():
        wrong = [
            (reading[0], got, reading[4])
            for reading, got in zip(READINGS, values, strict=True)
            if not _close(got, reading[4])
        ]
        assert wrong == [], backend


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
    wrong = [
        (reading[0], tags[dest.name], reading[4])
        for reading, dest in zip(READINGS, dests, strict=True)
        if not _close(tags[dest.name], reading[4])
    ]
    assert wrong == []


def test_imported_formula_with_an_unconfirmed_pair_imports_with_a_warning() -> None:
    from tests.conformance.math.test_math_order import PROBES, _registers

    readings = []
    for case_id, (hex_mode, formulas) in PROBES.items():
        prefix = "DH" if hex_mode else "DS"
        for formula, value, _ in formulas:
            typed, staged = _registers([formula], prefix, 1)
            registers = {f"{prefix}{address}": number for address, number in staged.items()}
            readings.append((case_id, typed[0], hex_mode, registers, value))
    with pytest.warns(UserWarning, match="no one has checked that order on a CLICK yet") as caught:
        logic, _mapping, _formulas, dests = _program(readings)
    assert len(caught) == len(readings)

    # The import applies the order that the warning names.
    plc = PLC(logic)
    plc.step()
    tags = plc.current_state.tags
    wrong = [
        (reading[0], tags[dest.name], reading[4])
        for reading, dest in zip(readings, dests, strict=True)
        if not _close(tags[dest.name], reading[4] & 0xFFFF if reading[2] else reading[4])
    ]
    assert wrong == []
