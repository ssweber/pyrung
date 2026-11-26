"""Check the order in which a CLICK reads a Math formula: the ``math_order`` group (part
``follow5_order``).

pyrung's exporter brackets every operation, so each case passes ``formulas=``, and the CLICK
export types each formula without brackets (``pyrung.conformance._formula``). The PLC then
reads it in its own order. In-process, the case's ``math()`` runs the order that pyrung's
importer reads from the same text.

A CLICK reads a minus sign before an address, a bracket or a function first. Then it reads
``^``, then ``*``, ``/``, MOD, AND, OR and XOR at one level, then ``+`` and ``-``. It applies
each level from the left.
Each case runs formulas that a CLICK 4.00 on a C2-03CPU was read running on 2026-11-25 and
2026-11-26. It expects the values read then. Most cases move each constant into a register of
its own, so that pyrung's source keeps the operation. ``-2 ^ 2`` and ``-7 MOD 3`` keep their
constants, because they test a minus sign written against a constant. A decimal case writes
into the Results. A hex case writes into a DH and copies the DH into a Result, so FFFFh reads
-1.

Each case of ``PROBES`` asks about one pair of operators. It expects the value of the order
that pyrung applies, which follows CLICK's levels. ``PROBES`` lists each formula with that
value and the value of the other order. ``test_each_probe_tells_the_two_orders_apart``
computes both. A case whose pair ``formula.CONFIRMED_PAIRS`` lists is an ordinary case,
because a reading settled the pair. The rest are probes (``probe=``). When a probe passes on a
CLICK, add its readings to the ``READINGS`` of ``tests/click/test_click_formula.py``, and its
pair to ``formula.CONFIRMED_PAIRS`` with the round.

No probe asks about a minus sign before ``/`` or MOD in decimal. Both orders give the same
value in pyrung and in the model of a CLICK (``formula_values.py``) for each value the model
reads, so no such value tells them apart. The model reads no step on Inf or NaN, and no step
past the largest double, so the importer warns on these pairs. ``test_math_range.py`` asks
what a CLICK does with a step past the largest double.

The ``WIDTHS`` cases each bracket their formulas, so the order is fixed and only one step is
read. They run formulas that a CLICK was read running on 2026-11-26, and expect the values read
then. ``width_int`` and ``width_word`` run reading round b17's cases b1 to b4, b1x and b2x.
``hex_by_zero`` runs reading round b18's cases d1 and d2. A CLICK does not cut an
intermediate value to the destination's size. It raises SC43 only for the value that it stores.

- ``width_int``: ``( DS 32767 + DS 1 ) - DS 1`` (Result1) and ``( DS 300 * DS 300 ) / DS 400``
  (Result2) into an Int. Result3 is 1 when ``( DD 2147483647 + DD 1 ) - DD 1`` into a DD is
  2147483647, and 2 when it is 2147483646. Result4 is 1 when ``( DD 100000 * DD 100000 ) /
  DD 100000`` is 100000, 2 for 14100 (a 32-bit wrap) and 3 for 21474 (a step held at the top
  of a DD). ErrorCode adds 1, 2, 4 and 8 for SC43 after each.
- ``width_word``: ``( DH FFFFh + DH 1h ) - DH 1h`` (Result1, FFFFh reads -1) and
  ``( DH 0h - DH 1h ) + DH 1h`` (Result2). A Word that saturates reads FFFEh and 1h. ErrorCode
  adds 1 and 2 for SC43 after each.
- ``hex_by_zero``: into DHs preset to 99h, ``( DH 5h / DH 0h ) + DH 1h`` (Result1) and
  ``( DH 5h MOD DH 0h ) + DH 1h`` (Result2). Each stores 1h. If the whole formula were 0, each
  would read 0h. If the write were skipped, each would read 99h. ErrorCode adds 1 and 2 for
  SC40 after each, and reads 1. The MOD raises SC46 on a CLICK. The case does not read SC46,
  which pyrung does not raise yet.

The ``doubles`` case is a probe. It types its formulas without brackets, as the order cases
do, and expects what pyrung gives.

- Result1 is ``DF 1.0 + DF 1.0E+17 - DF 1.0E+17`` into an Int. A double drops the 1.0 from
  1.0 + 1.0E+17, so the formula is 0 from the left and 1 with the ``-`` first.
- Result2 is 1 when ``( DD 164999119 * DD 114975250 * DD 58 ) MOD DD 1000000`` into a DD is
  675584, from the product of the doubles from the left. It is 2 for 675456, with the second
  ``*`` first, and 3 for 675500, from the whole product. The MOD keeps the stored value inside
  the values that a reading shows a DD holding.
- Result3 is 1 when ``DD 2147483647 * DD 2147483647`` into a DD is 0, 2 for 1 and 3 for
  2147483647. Result4 is the same formula into an Int. As a double the product is
  2^62 - 2^32, whose low 32 bits are 0. The low bits of the whole product are 1.
- Scratch is ``DF 1.0 + DF 1.0E+17 + DF -1.0E+17`` into an Int. It is 0 from the left and 1
  with the second ``+`` first. A slot has four Results, so the case reads this one in
  ``Scratch``, which it uses for nothing else.

When the case passes on a CLICK, add its readings to ``READINGS``, and widen the stores that
``formula_values._STORES_READ`` reads.

No probe asks about a minus sign before another minus sign. laddercodec refuses ``- - DS1``
(OBJ-1001), as CLICK's dialog refused ``5 - - DS1``. One formula that laddercodec refuses keeps
the whole group off the PLC, so ``test_typed_formulas.py`` reads the export back through
laddercodec.

The cases own what ``memory.OWNED_APART`` lists under this group.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

import pytest

from pyrung.click.codegen.formula import (
    CONFIRMED_PAIRS,
    _scopes,
    parse_with_unconfirmed_pairs,
)
from pyrung.click.codegen.utils import _guessed_order, _operator_words
from pyrung.conformance import Case, Group, assert_all_passed, case, run
from pyrung.core import copy, fill, math, rung, system
from tests.conformance.math.formula_values import (
    click_value,
    float32,
    pyrung_expression,
    regroupings,
)
from tests.conformance.memory import dd, df, dh, ds
from tests.conformance.probe_kit import class_of, finish, flag_bit, once

#: A constant of a formula. A minus sign written against it is part of it (``-7``).
_CONSTANT = re.compile(r"(?<![\w-])-?\b[0-9][0-9A-Fa-f]*h?\b")

_RESULTS = ("Result1", "Result2", "Result3", "Result4")


class _KeepConstants(str):
    """A formula whose constants stay in the typed formula."""


def _value(token: str) -> int:
    return int(token[:-1], 16) if token.endswith("h") else int(token)


def _registers(formulas: Sequence[str], prefix: str, first: int) -> tuple[list[str], dict]:
    """Return *formulas* with each constant read from a register, and each register's value."""
    addresses: dict[int, int] = {}

    def register(match: re.Match[str]) -> str:
        value = _value(match.group())
        addresses.setdefault(value, first + len(addresses))
        return f"{prefix}{addresses[value]}"

    typed = [
        formula if isinstance(formula, _KeepConstants) else _CONSTANT.sub(register, formula)
        for formula in formulas
    ]
    return typed, {address: value for value, address in addresses.items()}


def _order_case(
    sentence: str,
    case_id: str,
    readings: Sequence[tuple[str, int]],
    *,
    hex_mode: bool,
    first: int,
    probe: str | None = None,
) -> Case:
    """Return a case that runs each formula of *readings* and expects the value beside it.

    The case owns ten registers from *first*: DS in decimal mode and DH in hex mode. A hex
    case keeps its last registers for the results.
    """
    bank, prefix = (dh, "DH") if hex_mode else (ds, "DS")
    formulas = [formula for formula, _ in readings]
    typed, staged = _registers(formulas, prefix, first)
    dests = range(first + 10 - len(readings), first + 10)
    if len(staged) > (dests.start - first if hex_mode else 10):
        raise ValueError(f"Case {case_id} needs more than ten registers")
    trees = [parse_with_unconfirmed_pairs(text, hex_mode=hex_mode)[0] for text in typed]

    def ladder(slot: Any) -> None:
        with rung(*once(slot)):
            for tree, dest, field in zip(trees, dests, _RESULTS, strict=False):
                if hex_mode:
                    math(pyrung_expression(tree, bank), dh[dest])
                    copy(dh[dest], getattr(slot, field))
                else:
                    math(pyrung_expression(tree, bank), getattr(slot, field))
        finish(slot)

    def init(slot: Any) -> None:
        for address, value in staged.items():
            copy(value, bank[address])
        if hex_mode:
            fill(0, dh.select(dests.start, dests.stop - 1))

    return case(
        sentence,
        ladder=ladder,
        init=init,
        id=case_id,
        expect={field: value for field, (_, value) in zip(_RESULTS, readings, strict=False)},
        formulas=typed,
        probe=probe,
    )


def _decimal(case_id: str, sentence: str, first: int, *readings: tuple[str, int]) -> Case:
    return _order_case(sentence, case_id, readings, hex_mode=False, first=first)


def _hex(case_id: str, sentence: str, first: int, *readings: tuple[str, int]) -> Case:
    return _order_case(sentence, case_id, readings, hex_mode=True, first=first)


def _hex_text(value: int) -> str:
    return f"{value & 0xFFFF:X}h"


def _pair(typed: str, hex_mode: bool) -> tuple[str, str]:
    """Return the first and last operators of the probe formula *typed*.

    A probe of a minus sign before another minus sign has the second minus sign between them.
    """
    tree = parse_with_unconfirmed_pairs(typed, hex_mode=hex_mode)[0]
    operators, *inner = _scopes(tree)
    assert not any(inner)
    return operators[0][0], operators[-1][0]


def _probe(case_id: str, hex_mode: bool, first: int, readings: Sequence[tuple[str, int]]) -> Case:
    typed = _registers([readings[0][0]], "DH" if hex_mode else "DS", 1)[0][0]
    left, right = _pair(typed, hex_mode)
    if left == right:
        uses = f"{_operator_words(left)} twice"
    else:
        uses = f"{_operator_words(left)} and {_operator_words(right)}"
    order = _guessed_order(left, right)
    show = _hex_text if hex_mode else str
    sentence = f"For {uses}, a CLICK applies {order}. " + " and ".join(
        f"{formula} gives {show(value)}" for formula, value in readings
    )
    settled = {left, right} in [{a, b} for a, b, _ in CONFIRMED_PAIRS]
    return _order_case(
        sentence,
        case_id,
        readings,
        hex_mode=hex_mode,
        first=first,
        probe=None if settled else f"whether a CLICK applies {order} for {uses}",
    )


#: Each probe case's mode and formulas, with the value of each formula in the order that
#: pyrung applies, and in the other order.
PROBES: dict[str, tuple[bool, tuple[tuple[str, int, int], ...]]] = {
    "div_mod": (True, (("8h / 2h MOD 3h", 1, 4), ("9h MOD 6h / 2h", 1, 0))),
    "div_and": (True, (("8h / 2h AND 3h", 0, 4), ("6h AND 7h / 2h", 3, 2))),
    "div_xor": (True, (("8h / 2h XOR 1h", 5, 2), ("1h XOR 6h / 2h", 3, 2))),
    "mod_or": (True, (("7h MOD 4h OR 1h", 3, 2), ("4h OR 7h MOD 5h", 2, 6))),
    "mod_xor": (True, (("8h MOD 5h XOR 1h", 2, 0), ("1h XOR 7h MOD 5h", 1, 3))),
    "mul_or": (True, (("2h * 3h OR 1h", 7, 6), ("1h OR 2h * 3h", 9, 7))),
    "mul_xor": (True, (("2h * 3h XOR 1h", 7, 4), ("1h XOR 2h * 3h", 9, 7))),
    "sub_mul": (False, (("10 - 2 * 3", 4, 24), ("2 * 3 - 1", 5, 4))),
    "mod_mod": (False, (("7 MOD 5 MOD 3", 2, 1),)),
    "negb_power": (False, (("- ( 2 ) ^ 2", 4, -4),)),
    "negb_add": (False, (("- ( 2 ) + 5", 3, -7),)),
    "negb_sub": (False, (("- ( 2 ) - 1", -3, -1),)),
    "sub_and": (True, (("7h - 1h AND 3h", 6, 2), ("6h AND 7h - 1h", 5, 6))),
    "sub_or": (True, (("5h - 1h OR 2h", 2, 6), ("1h OR 4h - 1h", 4, 3))),
    "sub_xor": (True, (("5h - 1h XOR 3h", 3, 7), ("1h XOR 3h - 1h", 1, 3))),
    "negb_and": (True, (("- ( 2h ) AND 3h", 2, -2),)),
    "negb_or": (True, (("- ( 2h ) OR 1h", -1, -3),)),
    "negb_xor": (True, (("- ( 3h ) XOR 1h", -4, -2),)),
    "negpi_power": (False, (("- PI ^ 2", 9, -9),)),
}


#: The first register of each probe case, in the order of ``PROBES``. A decimal case takes ten
#: DS registers, and a hex case ten DH registers.
_PROBE_REGISTERS = {
    False: (*range(4231, 4281, 10), 4441),
    True: tuple(range(271, 401, 10)),
}


def _probes() -> list[Case]:
    """Return the probe cases, each on ten registers of its own after the other cases'."""
    first = {hex_mode: iter(registers) for hex_mode, registers in _PROBE_REGISTERS.items()}
    cases = []
    for case_id, (hex_mode, readings) in PROBES.items():
        values = [(formula, value) for formula, value, _ in readings]
        cases.append(_probe(case_id, hex_mode, next(first[hex_mode]), values))
    return cases


SC40 = system.fault.division_error
SC43 = system.fault.out_of_range


def prove_width_int(slot: Any) -> None:
    top, one, three_hundred, four_hundred = ds[4461], ds[4462], ds[4463], ds[4464]
    dint_top, dint_one, dint_sum, big, dint_product = (dd[n] for n in range(173, 178))
    with rung(*once(slot)):
        math((top + one) - one, slot.Result1)
    flag_bit(slot, SC43, slot.ErrorCode, 1)
    with rung(*once(slot)):
        math((three_hundred * three_hundred) / four_hundred, slot.Result2)
    flag_bit(slot, SC43, slot.ErrorCode, 2)
    with rung(*once(slot)):
        math((dint_top + dint_one) - dint_one, dint_sum)
    flag_bit(slot, SC43, slot.ErrorCode, 4)
    with rung(*once(slot)):
        math((big * big) / big, dint_product)
    flag_bit(slot, SC43, slot.ErrorCode, 8)
    class_of(slot, dint_sum, [2147483647, 2147483646], slot.Result3)
    class_of(slot, dint_product, [100000, 14100, 21474], slot.Result4)
    finish(slot)


def init_width_int(slot: Any) -> None:
    copy(32767, ds[4461])
    copy(1, ds[4462])
    copy(300, ds[4463])
    copy(400, ds[4464])
    copy(2147483647, dd[173])
    copy(1, dd[174])
    copy(100000, dd[176])
    copy(0, dd[175])
    copy(0, dd[177])


def prove_width_word(slot: Any) -> None:
    top, one, zero = dh[34], dh[35], dh[36]
    with rung(*once(slot)):
        math((top + one) - one, dh[37])
    flag_bit(slot, SC43, slot.ErrorCode, 1)
    with rung(*once(slot)):
        math((zero - one) + one, dh[38])
    flag_bit(slot, SC43, slot.ErrorCode, 2)
    with rung(*once(slot)):
        copy(dh[37], slot.Result1)
        copy(dh[38], slot.Result2)
    finish(slot)


def init_width_word(slot: Any) -> None:
    copy(0xFFFF, dh[34])
    copy(1, dh[35])
    fill(0, dh.select(36, 38))


def prove_hex_by_zero(slot: Any) -> None:
    five, zero, one = dh[39], dh[40], dh[41]
    with rung(*once(slot)):
        math((five / zero) + one, dh[42])
    flag_bit(slot, SC40, slot.ErrorCode, 1)
    with rung(*once(slot)):
        math((five % zero) + one, dh[43])
    flag_bit(slot, SC40, slot.ErrorCode, 2)
    with rung(*once(slot)):
        copy(dh[42], slot.Result1)
        copy(dh[43], slot.Result2)
    finish(slot)


def init_hex_by_zero(slot: Any) -> None:
    copy(5, dh[39])
    copy(0, dh[40])
    copy(1, dh[41])
    fill(0x99, dh.select(42, 43))


def prove_doubles(slot: Any) -> None:
    one, big, big_again = df[170], df[171], df[172]
    a, b, c, product, million = dd[178], dd[179], dd[180], dd[181], dd[185]
    top, top_again, square = dd[182], dd[183], dd[184]
    with rung(*once(slot)):
        math(one + big - big_again, slot.Result1)
        math((a * b * c) % million, product)
        math(top * top_again, square)
        math(top * top_again, slot.Result4)
        math(df[173] + df[174] + df[175], slot.Scratch)
    class_of(slot, product, DOUBLES_PRODUCTS, slot.Result2)
    class_of(slot, square, [0, 1, 2147483647], slot.Result3)
    finish(slot)


def init_doubles(slot: Any) -> None:
    copy(1.0, df[170])
    fill(1.0e17, df.select(171, 172))
    copy(164999119, dd[178])
    copy(114975250, dd[179])
    copy(58, dd[180])
    copy(1000000, dd[185])
    fill(2147483647, dd.select(182, 183))
    copy(0, dd[181])
    copy(0, dd[184])
    copy(1.0, df[173])
    copy(1.0e17, df[174])
    copy(-1.0e17, df[175])


#: The formulas of the ``doubles`` case that read an order, with their register values.
DOUBLES: tuple[tuple[str, dict[str, Any]], ...] = (
    ("DF170 + DF171 - DF172", {"DF170": 1.0, "DF171": float32(1.0e17), "DF172": float32(1.0e17)}),
    (
        "( DD178 * DD179 * DD180 ) MOD DD185",
        {"DD178": 164999119, "DD179": 114975250, "DD180": 58, "DD185": 1000000},
    ),
    ("DF173 + DF174 + DF175", {"DF173": 1.0, "DF174": float32(1.0e17), "DF175": float32(-1.0e17)}),
)

#: What a DD stores for ( 164999119 * 114975250 * 58 ) MOD 1000000, from the product of the
#: doubles from the left, from the product with the second ``*`` first, and from the whole
#: product.
DOUBLES_PRODUCTS = [675584, 675456, 675500]

#: The cases that bracket their formulas to read one step, in the order of the manifest.
WIDTHS = [
    case(
        "A Math keeps each intermediate whole and checks only the value it stores. "
        "( 32767 + 1 ) - 1 is 32767 (Result1) and ( 300 * 300 ) / 400 is 225 (Result2) "
        "into an Int. ( 2147483647 + 1 ) - 1 is 2147483647 (Result3 = 1) and "
        "( 100000 * 100000 ) / 100000 is 100000 (Result4 = 1) into a DD. None raises SC43 "
        "(ErrorCode = 0)",
        ladder=prove_width_int,
        init=init_width_int,
        id="width_int",
        expect={"Result1": 32767, "Result2": 225, "Result3": 1, "Result4": 1, "ErrorCode": 0},
    ),
    case(
        "A hex Math keeps each intermediate whole: ( FFFFh + 1h ) - 1h is FFFFh (Result1 = -1) "
        "and ( 0h - 1h ) + 1h is 0h (Result2). Neither raises SC43 (ErrorCode = 0)",
        ladder=prove_width_word,
        init=init_width_word,
        id="width_word",
        expect={"Result1": -1, "Result2": 0, "ErrorCode": 0},
    ),
    case(
        "A hex division or MOD by zero inside a formula is 0, and the rest of the formula "
        "runs: ( 5h / 0h ) + 1h is 1h (Result1) and ( 5h MOD 0h ) + 1h is 1h (Result2). Only "
        "the division raises SC40 (ErrorCode = 1)",
        ladder=prove_hex_by_zero,
        init=init_hex_by_zero,
        id="hex_by_zero",
        expect={"Result1": 1, "Result2": 1, "ErrorCode": 1},
    ),
]

#: A case that reads the order of two operators in doubles, and a store past 2^53.
DOUBLES_CASE = case(
    "pyrung expects a Math to apply + and -, + twice and * twice from the left, in doubles. "
    "1.0 + 1.0E+17 - 1.0E+17 into an Int is 0 (Result1), and 1 with the - first. "
    "( 164999119 * 114975250 * 58 ) MOD 1000000 into a DD is 675584 (Result2 = 1), and 675456 "
    "with the second * first (2). 1.0 + 1.0E+17 + -1.0E+17 into an Int is 0 (Scratch), and 1 "
    "with the second + first. pyrung stores 0 for 2147483647 * 2147483647 into a DD "
    "(Result3 = 1) and into an Int (Result4)",
    ladder=prove_doubles,
    init=init_doubles,
    id="doubles",
    expect={"Result1": 0, "Result2": 1, "Result3": 1, "Result4": 0, "Scratch": 0},
    formulas=[
        "DF170 + DF171 - DF172",
        "( DD178 * DD179 * DD180 ) MOD DD185",
        "DD182 * DD183",
        "DD182 * DD183",
        "DF173 + DF174 + DF175",
    ],
    probe="whether a CLICK applies + and -, + twice and * twice from the left where a double "
    "rounds, and what it stores for a value past 2^53",
)


group = Group(
    "math_order",
    [
        _decimal(
            "dec_tiers",
            "2 + 3 * 4 is 14, 7 MOD 3 * 2 is 2, 2 ^ 3 ^ 2 is 64 and 10 - 4 - 3 is 3",
            4101,
            ("2 + 3 * 4", 14),
            ("7 MOD 3 * 2", 2),
            ("2 ^ 3 ^ 2", 64),
            ("10 - 4 - 3", 3),
        ),
        _decimal(
            "dec_power",
            "100 / 10 / 5 is 2, -2 ^ 2 is 4, 2 * 3 ^ 2 is 18 and 3 ^ 2 * 2 is 18",
            4111,
            ("100 / 10 / 5", 2),
            (_KeepConstants("-2 ^ 2"), 4),
            ("2 * 3 ^ 2", 18),
            ("3 ^ 2 * 2", 18),
        ),
        _decimal(
            "dec_minus_power",
            "A minus sign binds tighter than ^: - DS ^ 2 is 4 for 2 and for -2, and - DS ^ 3 is "
            "-8 for 2. 3 * 7 MOD 4 is 1",
            4121,
            ("- 2 ^ 2", 4),
            ("- -2 ^ 2", 4),
            ("- 2 ^ 3", -8),
            ("3 * 7 MOD 4", 1),
        ),
        _decimal(
            "dec_div_level",
            "12 / 2 * 3 is 18, 18 / 3 ^ 2 is 2, 5 - 3 + 1 is 3 and 12 / 4 * 3 is 9",
            4131,
            ("12 / 2 * 3", 18),
            ("18 / 3 ^ 2", 2),
            ("5 - 3 + 1", 3),
            ("12 / 4 * 3", 9),
        ),
        _decimal(
            "dec_add_after",
            "+ binds looser than / and MOD: 1 + 6 / 2, 6 / 2 + 1, 1 + 7 MOD 4 and 7 MOD 4 + 1 "
            "are each 4",
            4141,
            ("1 + 6 / 2", 4),
            ("6 / 2 + 1", 4),
            ("1 + 7 MOD 4", 4),
            ("7 MOD 4 + 1", 4),
        ),
        _decimal(
            "dec_sub_after",
            "- binds looser than / and MOD: 10 - 7 MOD 4 is 7, 7 MOD 4 - 1 is 2, 10 - 6 / 2 is 7 "
            "and 6 / 2 - 1 is 2",
            4151,
            ("10 - 7 MOD 4", 7),
            ("7 MOD 4 - 1", 2),
            ("10 - 6 / 2", 7),
            ("6 / 2 - 1", 2),
        ),
        _decimal(
            "dec_power_mod",
            "^ binds tighter than MOD, + and -: 2 ^ 3 MOD 3 is 2, 7 MOD 3 ^ 2 is 7, 1 + 2 ^ 2 is "
            "5 and 10 - 2 ^ 2 is 6",
            4161,
            ("2 ^ 3 MOD 3", 2),
            ("7 MOD 3 ^ 2", 7),
            ("1 + 2 ^ 2", 5),
            ("10 - 2 ^ 2", 6),
        ),
        _decimal(
            "dec_minus_mul",
            "2 ^ 2 + 1 is 5 and 2 ^ 2 - 1 is 3. With DS = 2, - DS * 3 is -6 and - DS MOD 3 is -2",
            4171,
            ("2 ^ 2 + 1", 5),
            ("2 ^ 2 - 1", 3),
            ("- 2 * 3", -6),
            ("- 2 MOD 3", -2),
        ),
        _decimal(
            "dec_minus_add",
            "MOD keeps the sign of the dividend: -7 MOD 3 is -1. With DS = 2, - DS + 5 is 3 and "
            "- DS - 1 is -3",
            4181,
            (_KeepConstants("-7 MOD 3"), -1),
            ("- 2 + 5", 3),
            ("- 2 - 1", -3),
        ),
        _hex(
            "hex_tiers",
            "2h + 3h * 4h is Eh, 7h MOD 3h * 2h is 2h, 6h AND 3h OR 8h is Ah and 6h OR 3h AND 8h "
            "is 0",
            201,
            ("2h + 3h * 4h", 0xE),
            ("7h MOD 3h * 2h", 2),
            ("6h AND 3h OR 8h", 0xA),
            ("6h OR 3h AND 8h", 0),
        ),
        _hex(
            "hex_level",
            "5h XOR 3h AND 1h is 0, 10h - 4h - 3h is 9h, 100h / 10h / 5h is 3h and 4h AND 4h XOR "
            "1h is 5h",
            211,
            ("5h XOR 3h AND 1h", 0),
            ("10h - 4h - 3h", 9),
            ("100h / 10h / 5h", 3),
            ("4h AND 4h XOR 1h", 5),
        ),
        _hex(
            "hex_or_xor",
            "1h OR 1h XOR 1h is 0, 1h XOR 1h OR 1h is 1h, 2h AND 3h + 1h is 3h and 1h + 2h AND 2h "
            "is 3h",
            221,
            ("1h OR 1h XOR 1h", 0),
            ("1h XOR 1h OR 1h", 1),
            ("2h AND 3h + 1h", 3),
            ("1h + 2h AND 2h", 3),
        ),
        _hex(
            "hex_and_mul",
            "3h AND 3h * 2h is 6h, 2h * 3h AND 3h is 2h, 1h + 2h OR 1h is 4h and 1h + 3h XOR 1h "
            "is 3h",
            231,
            ("3h AND 3h * 2h", 6),
            ("2h * 3h AND 3h", 2),
            ("1h + 2h OR 1h", 4),
            ("1h + 3h XOR 1h", 3),
        ),
        _hex(
            "hex_minus_and",
            "2h * 3h / 4h is 1h. A minus sign binds tighter than AND: - DH AND 3h is 2h for 2h "
            "and 3h for 1h",
            241,
            ("2h * 3h / 4h", 1),
            ("- 2h AND 3h", 2),
            ("- 1h AND 3h", 3),
        ),
        _hex(
            "hex_minus_or",
            "A minus sign binds tighter than OR and XOR: - DH OR 1h is FFFFh for 2h, and - DH XOR "
            "1h is FFFFh for 2h and FFFCh for 3h. 7h MOD 5h AND 3h is 2h",
            251,
            ("- 2h OR 1h", -1),
            ("- 2h XOR 1h", -1),
            ("- 3h XOR 1h", -4),
            ("7h MOD 5h AND 3h", 2),
        ),
        _hex(
            "hex_mod_and_div_or",
            "MOD and AND sit at one level, and so do / and OR: 3h AND 7h MOD 5h, 6h / 2h OR 1h "
            "and 4h OR 6h / 2h are each 3h",
            261,
            ("3h AND 7h MOD 5h", 3),
            ("6h / 2h OR 1h", 3),
            ("4h OR 6h / 2h", 3),
        ),
        *_probes(),
        DOUBLES_CASE,
        *WIDTHS,
    ],
)


@pytest.mark.parametrize("case_id", PROBES)
def test_each_probe_tells_the_two_orders_apart(case_id: str) -> None:
    hex_mode, readings = PROBES[case_id]
    prefix = "DH" if hex_mode else "DS"
    pairs = set()
    for formula, value, other in readings:
        typed, staged = _registers([formula], prefix, 1)
        registers = {f"{prefix}{address}": number for address, number in staged.items()}
        tree = parse_with_unconfirmed_pairs(typed[0], hex_mode=hex_mode)[0]
        pair = _pair(typed[0], hex_mode)
        pairs.add(frozenset(pair))
        [regrouped] = [
            regrouped
            for parent, child, _, regrouped in regroupings(tree)
            if {parent, child} == set(pair)
        ]
        # A case stores into an Int or a DH, and the store drops the fraction.
        cut = 0xFFFF if hex_mode else -1
        assert int(click_value(tree, hex_mode, registers)) == value & cut
        assert int(click_value(regrouped, hex_mode, registers)) == other & cut
        assert value != other
    assert len(pairs) == 1


def test_math_order():
    assert_all_passed(run(group))


@pytest.mark.parametrize(("text", "registers"), DOUBLES)
def test_doubles_tells_the_two_orders_apart(text: str, registers: dict[str, Any]) -> None:
    tree = parse_with_unconfirmed_pairs(text)[0]
    [regrouped] = [regrouped for *_, regrouped in regroupings(tree)]
    left, other = (click_value(t, False, registers) for t in (tree, regrouped))
    if text.startswith("DF"):
        assert (left, other) == (0.0, 1.0)
    else:
        assert [left, other] == DOUBLES_PRODUCTS[:2]
        a, b, c, million = registers.values()
        assert a * b * c % million == DOUBLES_PRODUCTS[2]
