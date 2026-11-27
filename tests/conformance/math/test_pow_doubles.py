"""Test what a decimal Math's power stores into a DS, a DD and a DF: the ``pow_doubles`` group.

The group is in part ``follow6_reads`` (``suite.APART_PARTS``), on memory of its own.  Each
case runs once (``probe_kit``).  ErrorCode adds 1 for SC43 after the case's first Math, 2
after the second and 4 after the third.  Each destination starts at 99, so a Math that
writes nothing reads 99.

A CLICK stored each expectation in reading rounds b26 and b27 (2026-11-27), each formula in a
program of its own.  The rounds showed that a CLICK computes ``^`` as a double.  A DS or a DD
keeps the low 16 or 32 bits of that double, and an infinite power stores 0.  Each store
past the destination's range set SC43.

- ``low_bits_into_dd``: ``3 ^ 21`` is 10460353203.  Into a DD (Result1) its low 32 bits are
  1870418611, which reads 1.  A store that saturates gives 2147483647, which reads 2.  Into
  a DS (Result2) its low 16 bits are 21171.  A store that saturates gives 32767.
- ``double_low_bits``: ``3 ^ 35`` is 50031545098999707, past 2 ** 53.  The nearest double
  is 50031545098999712.  Into a DD (Result1) the double's low 32 bits are -651126888, which
  reads 1.  The exact power's low 32 bits are -651126885, which reads 2.  Into a DS
  (Result2) the double gives -26728 and the exact power -26725.  code.py computes the
  exact power and then rounds it to a float.  On a desktop that float is a double, so the
  ``circuitpy`` target stores what a CLICK stores.  The P1AM-200's float has about 22 bits,
  so the board stores 0 into the DD.
- ``infinite_power``: ``5 ^ DD`` with DD = 2147483647 is past the double range.  Into a DD
  (Result1) it stores 0.  The exact power's low 32 bits would be -858993459.  Into a DF
  (Result2) it stores +Inf, which reads 1.  Into a DS (Result3) it stores 0.

The cases own what ``memory.OWNED_APART`` lists under this group.
"""

from __future__ import annotations

from typing import Any

from pyrung import LiteralExpr
from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import copy, fill, math, rung, system
from tests.conformance.memory import dd, df, ds
from tests.conformance.probe_kit import finish, flag_bit, once

SC43 = system.fault.out_of_range

#: Each destination starts here, so a Math that writes nothing reads 99.
UNWRITTEN = 99


def _reading(slot: Any, into: Any, *classes: tuple[Any, ...]) -> None:
    """Set *into* to the number of the class whose condition holds, or leave it 0."""
    for number, condition in classes:
        with rung(*once(slot), condition):
            copy(number, into)


def prove_low_bits_into_dd(slot: Any) -> None:
    with rung(*once(slot)):
        copy(UNWRITTEN, dd[501])
        copy(UNWRITTEN, ds[4481])
        math(LiteralExpr(3) ** 21, dd[501])
    flag_bit(slot, SC43, slot.ErrorCode, 1)
    with rung(*once(slot)):
        math(LiteralExpr(3) ** 21, ds[4481])
    flag_bit(slot, SC43, slot.ErrorCode, 2)
    _reading(slot, slot.Result1, (1, dd[501] == 1870418611), (2, dd[501] == 2147483647))
    with rung(*once(slot)):
        copy(ds[4481], slot.Result2)
    finish(slot)


def init_low_bits_into_dd(slot: Any) -> None:
    copy(0, dd[501])
    copy(0, ds[4481])


def prove_double_low_bits(slot: Any) -> None:
    with rung(*once(slot)):
        copy(UNWRITTEN, dd[502])
        copy(UNWRITTEN, ds[4482])
        math(LiteralExpr(3) ** 35, dd[502])
    flag_bit(slot, SC43, slot.ErrorCode, 1)
    with rung(*once(slot)):
        math(LiteralExpr(3) ** 35, ds[4482])
    flag_bit(slot, SC43, slot.ErrorCode, 2)
    _reading(slot, slot.Result1, (1, dd[502] == -651126888), (2, dd[502] == -651126885))
    with rung(*once(slot)):
        copy(ds[4482], slot.Result2)
    finish(slot)


def init_double_low_bits(slot: Any) -> None:
    copy(0, dd[502])
    copy(0, ds[4482])


def prove_infinite_power(slot: Any) -> None:
    exponent = dd[503]
    with rung(*once(slot)):
        copy(2147483647, exponent)
        copy(UNWRITTEN, dd[504])
        copy(float(UNWRITTEN), df[2])
        copy(UNWRITTEN, ds[4483])
        math(LiteralExpr(5) ** exponent, dd[504])
    flag_bit(slot, SC43, slot.ErrorCode, 1)
    with rung(*once(slot)):
        math(LiteralExpr(5) ** exponent, df[2])
    flag_bit(slot, SC43, slot.ErrorCode, 2)
    with rung(*once(slot)):
        math(LiteralExpr(5) ** exponent, ds[4483])
    flag_bit(slot, SC43, slot.ErrorCode, 4)
    _reading(slot, slot.Result2, (1, df[2] >= 3.0e38))
    with rung(*once(slot)):
        copy(dd[504], slot.Result1)
        copy(ds[4483], slot.Result3)
    finish(slot)


def init_infinite_power(slot: Any) -> None:
    fill(0, dd.select(503, 504))
    copy(0.0, df[2])
    copy(0, ds[4483])


group = Group(
    "pow_doubles",
    [
        case(
            "A Math computes 3 ^ 21 as a double. A DD keeps its low 32 bits, 1870418611 "
            "(Result1 = 1). A DS keeps its low 16 bits (Result2 = 21171). Each store sets "
            "SC43 (ErrorCode = 3). A CLICK read these in round b26.",
            ladder=prove_low_bits_into_dd,
            init=init_low_bits_into_dd,
            expect={"Result1": 1, "Result2": 21171, "ErrorCode": 3},
        ),
        case(
            "A Math computes 3 ^ 35 as a double, 50031545098999712. A DD keeps the double's "
            "low 32 bits, -651126888 (Result1 = 1). A DS keeps its low 16 bits "
            "(Result2 = -26728). Each store sets SC43 (ErrorCode = 3). A CLICK read these in "
            "round b27.",
            ladder=prove_double_low_bits,
            init=init_double_low_bits,
            expect={"Result1": 1, "Result2": -26728, "ErrorCode": 3},
        ),
        case(
            "5 ^ DD with DD = 2147483647 is past the double range. A DD stores 0 "
            "(Result1 = 0). A DF stores +Inf (Result2 = 1). A DS stores 0 (Result3 = 0). "
            "Each store sets SC43 (ErrorCode = 7). A CLICK read these in round b26.",
            ladder=prove_infinite_power,
            init=init_infinite_power,
            expect={"Result1": 0, "Result2": 1, "Result3": 0, "ErrorCode": 7},
        ),
    ],
)


def test_pow_doubles():
    assert_all_passed(run(group))
