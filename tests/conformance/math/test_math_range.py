"""Check what a Math formula does with values that the readings of a CLICK have not shown
inside a formula. This is the ``math_range`` group, in part ``follow5_reads``. Both cases are
probes, and each expects what pyrung gives.

``double_overflow`` asks what a CLICK does with a step past the largest double, or below the
smallest normal one. A CLICK computes a formula's steps in doubles
(``faults/real_intermediate_range``). 10 ^ 300 is inside a double, and 10 ^ 300 * 1.0E+30
passes the largest one. A CLICK might carry Inf to the store, hold the step at the largest
double, set the step to 0 as it does a division by zero, or skip the write. Each destination
is a DF preset to 99.0, so a skipped write reads 99.0. pyrung carries Inf.

- Result1 classes ``( DF 10 ^ DF 300 ) * DF 1.0E+30 + DF 5``. It is 1 for Inf or a value
  from 3.0E+38 up, 2 for 5.0 (the step set to 0) and 3 for 99.0.
- Result2 classes ``( DF 10 ^ DF 300 ) * DF 1.0E+30`` the same way. It is 1 from 3.0E+38 up,
  2 for 0.0 and 3 for 99.0.
- Result3 classes ``( DF 10 ^ DF 300 ) * DF 1.0E+30 / ( DF 10 ^ DF 300 )``. It is 1 from
  3.0E+38 up (Inf carried), and 2 for 1.7E+8 to 1.9E+8 (the step held at the largest double,
  about 1.8E+308). It is 3 for 0.0 and 4 for 99.0.
- Result4 is 1 when ``( DF 10 ^ DF 300 ) / ( DF 10 ^ DF 299 )`` is about 10. That shows a
  CLICK computes 10 ^ 300 in a double.
- ErrorCode adds 1 for SC43 after the first formula, and 2 after the second.
- Scratch classes ``( DF 10 ^ DF -300 ) / DF 1.0E+10 * DF 1.0E+10 * ( DF 10 ^ DF 300 )``.
  The division gives about 1.0E-310, which is below the smallest normal double. It is 1 for
  0.99 to 1.01, which a CPU gives when it keeps such a value. It is 2 for 0.0, which a CPU
  gives when it sets such a value to 0.

``minus_sign`` asks what a minus sign gives before Inf, NaN and 0. It types each formula
without brackets (``formulas=``). It stages Inf as 3.0E+38 * 10 and NaN as Inf - Inf, as
``faults/inf_and_nan_arithmetic`` does. Copy Binary moves the bits of each 0 into a DD, and
a DD below 0 shows the sign bit. A minus sign might flip the sign bit, which gives -0.0
before 0.0. It might compute 0 - x instead, which gives +0.0.

- Result1 is 1 when ``- DF Inf * DF 2`` and ``- DF Inf / DF 2`` are both -Inf.
- Result2 adds 1 for the sign of ``- DF 0.0`` and 2 for ``- DF 0.0 * DF -5.0``.
- Result3 adds 1 for the sign of ``- DF 0.0 / DF -5.0`` and 2 for ``- DF 5.0 MOD DF 5.0``.
- ErrorCode adds 1 for the sign of ``- DS 0`` into a DF and 2 for ``- DS 0 * DS -5`` into
  a DF.
- Result4 is 1 when ``- DF NaN * DF 2``, ``- DF NaN / DF 2``, ``- DF Inf * DF 0.0`` and
  ``- DF Inf / DF Inf`` are all NaN.

pyrung flips the sign of a Real and negates a whole number exactly. It reads Result1 = 1,
Result2 = 1, Result3 = 2, ErrorCode = 0 and Result4 = 1.

What the readings settle for the pairs of a minus sign with ``*``, ``/`` and MOD:

- No value of Inf or NaN tells the two orders apart. A CLICK's Math stores one NaN, whatever
  its sign (``real_bits/non_finite_raw_bits``), and a minus sign before Inf gives -Inf in
  either order. If Result1 and Result4 read 1, the model of a CLICK (``formula_values.py``)
  can read a step on Inf or NaN.
- If Result2 bit 1 and ErrorCode bit 1 are set, a minus sign flips the sign of every 0, and
  both orders give the same 0. The model can then read a minus sign before 0, and the pairs
  over DS and DD registers can join ``formula.SAME_VALUE_ON_PLAIN_OPERANDS``.
- If a minus sign computes 0 - x, the orders differ. A CLICK that applies the minus sign
  first sets Result2 bit 2, Result3 bit 1 and ErrorCode bit 2. Applied last, it sets none of
  them. That reading settles the pairs in ``formula.CONFIRMED_PAIRS``.
- The pairs over a DF also need ``double_overflow``. Its Result3 must read 1 or 2, and its
  Scratch 1 or 2. With 2, the model must also say which sign the 0 keeps.

The cases own what ``memory.OWNED_APART`` lists under this group.
"""

from __future__ import annotations

from typing import Any

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import copy, fill, math, rung, system, to_binary
from tests.conformance.memory import dd, df, ds
from tests.conformance.probe_kit import finish, flag_bit, once

SC43 = system.fault.out_of_range


def _reading(slot: Any, into: Any, *classes: tuple[Any, ...]) -> None:
    """Set *into* to the number of the class whose conditions all hold, or leave it 0."""
    for number, *conditions in classes:
        with rung(*once(slot), *conditions):
            copy(number, into)


def prove_double_overflow(slot: Any) -> None:
    ten, three_hundred, big, five, two_ninety_nine = (df[n] for n in range(176, 181))
    inside, last, ratio, control = (df[n] for n in range(181, 185))
    minus_three_hundred, ten_billion, small = df[185], df[186], df[187]
    power = ten**three_hundred
    with rung(*once(slot)):
        math(power * big + five, inside)
    flag_bit(slot, SC43, slot.ErrorCode, 1)
    with rung(*once(slot)):
        math(power * big, last)
    flag_bit(slot, SC43, slot.ErrorCode, 2)
    with rung(*once(slot)):
        math(power * big / power, ratio)
        math(power / ten**two_ninety_nine, control)
        math(ten**minus_three_hundred / ten_billion * ten_billion * power, small)
    _reading(slot, slot.Result1, (1, inside >= 3.0e38), (2, inside == 5.0), (3, inside == 99.0))
    _reading(slot, slot.Result2, (1, last >= 3.0e38), (2, last == 0.0), (3, last == 99.0))
    _reading(
        slot,
        slot.Result3,
        (1, ratio >= 3.0e38),
        (2, ratio >= 1.7e8, ratio <= 1.9e8),
        (3, ratio == 0.0),
        (4, ratio == 99.0),
    )
    _reading(slot, slot.Result4, (1, control >= 9.99, control <= 10.01))
    _reading(slot, slot.Scratch, (1, small >= 0.99, small <= 1.01), (2, small == 0.0))
    finish(slot)


def init_double_overflow(slot: Any) -> None:
    copy(10.0, df[176])
    copy(300.0, df[177])
    copy(1.0e30, df[178])
    copy(5.0, df[179])
    copy(299.0, df[180])
    fill(99.0, df.select(181, 184))
    copy(-300.0, df[185])
    copy(1.0e10, df[186])
    copy(99.0, df[187])


#: The formulas of ``minus_sign``, one for each ``math()`` in rung order, typed without
#: brackets.
MINUS_FORMULAS = (
    "DF188 * DF189",
    "DF190 - DF190",
    "- DF190 * DF192",
    "- DF190 / DF192",
    "- DF193",
    "- DF193 * DF194",
    "- DF193 / DF194",
    "- DF195 MOD DF195",
    "- DS4471",
    "- DS4471 * DS4472",
    "- DF191 * DF192",
    "- DF191 / DF192",
    "- DF190 * DF193",
    "- DF190 / DF190",
)


def prove_minus_sign(slot: Any) -> None:
    big, ten, inf, nan, two, zero, minus_five, five = (df[n] for n in range(188, 196))
    signs = (df[198], df[199], df[200], df[201], df[202], df[203])
    nans = (df[204], df[205], df[206], df[207])
    with rung(*once(slot)):
        math(big * ten, inf)
        math(inf - inf, nan)
        math(-inf * two, df[196])
        math(-inf / two, df[197])
        math(-zero, signs[0])
        math(-zero * minus_five, signs[1])
        math(-zero / minus_five, signs[2])
        math(-five % five, signs[3])
        math(-ds[4471], signs[4])
        math(-ds[4471] * ds[4472], signs[5])
        math(-nan * two, nans[0])
        math(-nan / two, nans[1])
        math(-inf * zero, nans[2])
        math(-inf / inf, nans[3])
    with rung(*once(slot)):
        for n, sign in enumerate(signs):
            copy(sign, dd[461 + n], convert=to_binary)
    _reading(slot, slot.Result1, (1, df[196] <= -3.0e38, df[197] <= -3.0e38))
    # Each field holds the sign bits of two zeros: 1 for the first and 2 for the second.
    for n, into in enumerate((slot.Result2, slot.Result3, slot.ErrorCode)):
        first, second = dd[461 + 2 * n], dd[462 + 2 * n]
        _reading(
            slot,
            into,
            (1, first < 0, second >= 0),
            (2, first >= 0, second < 0),
            (3, first < 0, second < 0),
        )
    _reading(slot, slot.Result4, (1, *(value != value for value in nans)))
    finish(slot)


def init_minus_sign(slot: Any) -> None:
    copy(3.0e38, df[188])
    copy(10.0, df[189])
    copy(2.0, df[192])
    copy(0.0, df[193])
    copy(-5.0, df[194])
    copy(5.0, df[195])
    copy(0.0, df[190])
    copy(0.0, df[191])
    fill(99.0, df.select(196, 207))
    fill(0, dd.select(461, 466))
    copy(0, ds[4471])
    copy(-5, ds[4472])


group = Group(
    "math_range",
    [
        case(
            "pyrung expects a Math to carry Inf from a step that passes the largest double. "
            "( 10 ^ 300 ) * 1.0E+30 + 5 is Inf (Result1 = 1), and so is ( 10 ^ 300 ) * 1.0E+30 "
            "(Result2 = 1). ( 10 ^ 300 ) * 1.0E+30 / ( 10 ^ 300 ) is Inf (Result3 = 1), and "
            "( 10 ^ 300 ) / ( 10 ^ 299 ) is 10 (Result4 = 1). Both stores of Inf raise SC43 "
            "(ErrorCode = 3). ( 10 ^ -300 ) / 1.0E+10 * 1.0E+10 * ( 10 ^ 300 ) keeps the value "
            "below the smallest normal double, and is about 1.0 (Scratch = 1)",
            ladder=prove_double_overflow,
            init=init_double_overflow,
            id="double_overflow",
            expect={
                "Result1": 1,
                "Result2": 1,
                "Result3": 1,
                "Result4": 1,
                "ErrorCode": 3,
                "Scratch": 1,
            },
            probe="what a CLICK does with a step past the largest double, or below the smallest "
            "normal double, inside a formula",
        ),
        case(
            "pyrung expects a minus sign before Inf to give -Inf. - Inf * 2 and - Inf / 2 "
            "are -Inf (Result1 = 1). Result2, Result3 and ErrorCode hold the sign bits of the "
            "zeros that a minus sign makes. pyrung gives -0.0 for - DF 0.0 (Result2 = 1) and "
            "for - DF 5.0 MOD DF 5.0 (Result3 = 2), and +0.0 for the rest (ErrorCode = 0). "
            "Each formula on NaN, Inf * 0 and Inf / Inf is NaN (Result4 = 1)",
            ladder=prove_minus_sign,
            init=init_minus_sign,
            id="minus_sign",
            expect={"Result1": 1, "Result2": 1, "Result3": 2, "ErrorCode": 0, "Result4": 1},
            formulas=MINUS_FORMULAS,
            probe="what a minus sign gives before 0, Inf and NaN, and which order a CLICK "
            "applies to it with * and /",
        ),
    ],
)


def test_math_range():
    assert_all_passed(run(group))
