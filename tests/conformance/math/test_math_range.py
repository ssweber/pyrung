"""Check what a Math formula does with values that the readings of a CLICK have not shown
inside a formula. This is the ``math_range`` group, in part ``follow5_reads``. Each case is a
probe. A CLICK ran each case in reading round b16, and each case expects what that CLICK read.
pyrung gives the same.

``double_overflow`` asks what a CLICK does with a step past the largest double, or below the
smallest normal one. A CLICK computes a formula's steps in doubles
(``math_more/abs_and_precision``). 10 ^ 300 is inside a double, and 10 ^ 300 * 1.0E+30 passes
the largest one. A CLICK might carry Inf to the store, hold the step at the largest double,
set the step to 0 as it does a division by zero, or skip the write. Each destination is a DF
preset to 99.0, so a skipped write reads 99.0. pyrung carries Inf.

- Result1 shows what ``( DF 10 ^ DF 300 ) * DF 1.0E+30 + DF 5`` stored. It is 1 for Inf or a
  value from 3.0E+38 up, 2 for 5.0 (the step set to 0) and 3 for 99.0 (the write skipped).
- Result2 shows what ``( DF 10 ^ DF 300 ) * DF 1.0E+30`` stored. It is 1 from 3.0E+38 up, 2
  for 0.0 and 3 for 99.0.
- Result3 shows what ``( DF 10 ^ DF 300 ) * DF 1.0E+30 / ( DF 10 ^ DF 300 )`` stored. It is 1
  from 3.0E+38 up (Inf carried), and 2 for 1.7E+8 to 1.9E+8 (the step held at the largest
  double, about 1.8E+308). It is 3 for 0.0 and 4 for 99.0.
- Result4 is 1 when ``( DF 10 ^ DF 300 ) / ( DF 10 ^ DF 299 )`` is about 10. That shows a
  CLICK computes 10 ^ 300 in a double.
- ErrorCode adds 1 for SC43 after the first formula, and 2 after the second.
- Scratch shows what ``( DF 10 ^ DF -300 ) / DF 1.0E+10 * DF 1.0E+10 * ( DF 10 ^ DF 300 )``
  stored. The division gives about 1.0E-310, which is below the smallest normal double. It
  is 1 for 0.99 to 1.01, which a CPU gives when it keeps such a value. It is 2 for 0.0, which
  a CPU gives when it sets such a value to 0.

``minus_sign``, ``by_zero`` and ``zero_sum`` ask about the sign of a zero, and
``minus_sign`` also asks about Inf and NaN. Each types its formulas without brackets
(``formulas=``). Copy Binary moves the bits of each zero into a DD, and a DD below 0 shows
the sign bit. Each field adds 1 for its first zero with the sign bit set, 2 for the second
and 4 for the third.

``minus_sign`` stages Inf as 3.0E+38 * 10 and NaN as Inf - Inf, as
``faults/inf_and_nan_arithmetic`` does. A minus sign might flip the sign bit, which gives
-0.0 before 0.0. It might compute 0 - x instead, which gives +0.0.

- Result1 is 1 when ``- DF Inf * DF 2`` and ``- DF Inf / DF 2`` are both -Inf, and
  ``- DF NaN * DF 2``, ``- DF NaN / DF 2``, ``- DF Inf * DF 0.0`` and ``- DF Inf / DF Inf``
  are all NaN.
- Result2 holds the signs of ``- DF 0.0``, ``- DF 0.0 * DF -5.0`` and ``- DF 0.0 / DF -5.0``.
- Result3 holds the signs of ``- DF 5.0 MOD DF 5.0``, ``DF -5.0 MOD DF 5.0`` and
  ``DF 5.0 MOD DF 5.0``.
- Result4 holds the signs of ``DF 5.0 MOD DF -5.0`` and ``DF -5.0 MOD DF -5.0``.
- ErrorCode holds the signs of ``- DS 0``, ``- DS 0 * DS -5`` and ``DS 0 * DS -5``, each
  stored in a DF.

A CLICK read Result1 = 1, Result2 = 1, Result3 = 3, Result4 = 2 and ErrorCode = 5 in reading
round b16. So a minus sign flips the sign bit. A CLICK also computes a DS of 0 as the double
+0.0, not as a whole number:

- ``- DS 0`` gives -0.0.
- ``- DS 0 * DS -5`` gives +0.0.
- ``DS 0 * DS -5`` gives -0.0.

The 0 of a MOD takes the dividend's sign.

``by_zero`` asks which sign the 0 of a division or MOD by zero takes. It stages -0.0 as
0.0 * -1.0 (``real_bits/minus_zero_math``).

- Result1 holds the signs of ``DF 5.0 / DF 0.0``, ``DF -5.0 / DF 0.0`` and
  ``- DF 5.0 / DF 0.0``.
- Result2 holds the signs of ``DF 0.0 / DF 0.0`` and ``DF -0.0 / DF 0.0``.
- Result3 and Result4 hold the same for MOD.
- Scratch holds the sign of the staged -0.0.

pyrung gives +0.0 for each division and MOD by zero. It reads 0 in Result1 to Result4, and
Scratch = 1.

``zero_sum`` asks which sign a sum to 0 gives, and which sign a MOD, ``*`` or ``/`` of 0
gives. It stages -0.0 the same way. Each 0 is stored in a DF.

- Result1 holds the sign of ``DS 0 + DS 0``.
- Result2 holds the signs of ``DS 5 + DS -5`` and ``DS 5 - DS 5``.
- Result3 holds the signs of ``DF 5.0 - DF 5.0`` and ``DF -5.0 + DF 5.0``.
- Result4 holds the signs of ``DF 0.0 + DF 0.0`` and ``DF -0.0 + DF 0.0``.
- Scratch holds the signs of ``DF -0.0 MOD DF 5.0`` and ``DF 0.0 MOD DF 5.0``.
- ErrorCode holds the signs of ``DF -0.0 * DF -5.0`` and ``DF -0.0 / DF -5.0``.

A CLICK read 0 in each field but Scratch, which read 1, in reading round b16. So each sum to 0
gives +0.0, and so do ``DF -0.0 * DF -5.0`` and ``DF -0.0 / DF -5.0``. ``DF -0.0 MOD DF 5.0``
gives -0.0 and ``DF 0.0 MOD DF 5.0`` gives +0.0. So the 0 of a MOD takes the dividend's sign,
-0.0 included. The part has no rung to spare, so this case does not check the staged -0.0.
``by_zero`` checks it.

A pair joins a list again only when a reading shows every row of its entry in
``formula_values.PENDING_ZERO_SIGNS``. Each reading must agree with these rules:

- each step gives the zero sign that a double gives;
- a minus sign flips the sign bit;
- the 0 of a division or MOD by zero is +0.0;
- SQRT of a negative value gives +0.0.

``test_pending_zero_signs_are_the_unread_rows_that_each_pair_meets`` in
``tests/click/test_click_formula.py`` generates the rows that each pair meets, and checks the
table against them. Each row that is left has one value of its class read, by a field of
this group or by a case of reading round b20. A row is read only when every value of its class
is.

``by_zero`` also settles two pairs in ``formula.CONFIRMED_PAIRS``. ``- DF 5.0 / DF 0.0`` and
``- DF 5.0 MOD DF 0.0`` stored +0.0. Applied last, the minus sign would give -0.0. So a CLICK
applies a minus sign before an address first, with ``/`` and with MOD. In reading round b20,
``- ( DF 5.0 ) / DF 0.0`` and ``- ( DF 5.0 ) MOD DF 0.0`` stored +0.0 too. So a minus sign
before a bracket applies first as well. No case of this group reads those two formulas.

The pairs of a minus sign with ``*`` also depend on these:

- ``minus_sign`` read Result1 = 1, so a minus sign before Inf gave -Inf, and each step on NaN
  gave NaN. That field checks six steps at once. The model of a CLICK
  (``formula_values.py``) leaves each step on Inf or NaN unread.
- The pairs over a DF also need a step that overflows a double, and a step below the
  smallest normal double. ``double_overflow`` read Result3 = 1 and Scratch = 1, so a CLICK
  carried Inf and kept the small value. The model leaves both kinds of step unread too.

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
    "- DF191 * DF192",
    "- DF191 / DF192",
    "- DF190 * DF193",
    "- DF190 / DF190",
    "- DF193",
    "- DF193 * DF194",
    "- DF193 / DF194",
    "- DF195 MOD DF195",
    "DF194 MOD DF195",
    "- DS4471",
    "- DS4471 * DS4472",
    "DS4471 * DS4472",
    "DF195 MOD DF195",
    "DF195 MOD DF194",
    "DF194 MOD DF194",
)


def _bits(slot: Any, into: Any, *checks: tuple[Any, Any]) -> None:
    """Set *into* to the sum of ``1 << n`` over each check n that holds.

    Each check holds two conditions. The first holds when the check passes, and the second when
    it fails. Adding a bit would need a Math, and each Math of the case must be one of its typed
    formulas. So each sum gets a rung of its own.
    """
    for total in range(1, 1 << len(checks)):
        conditions = [check[0 if total >> n & 1 else 1] for n, check in enumerate(checks)]
        with rung(*once(slot), *conditions):
            copy(total, into)


def _signs(slot: Any, values: list[Any], first_dd: int) -> list[tuple[Any, Any]]:
    """Copy the bits of each of *values* into a DD from *first_dd* on, for :func:`_bits`.

    Return a check for each value that holds when its sign bit is set.
    """
    with rung(*once(slot)):
        for n, value in enumerate(values):
            copy(value, dd[first_dd + n], convert=to_binary)
    return [(dd[first_dd + n] < 0, dd[first_dd + n] >= 0) for n in range(len(values))]


def prove_minus_sign(slot: Any) -> None:
    big, ten, inf, nan, two, zero, minus_five, five = (df[n] for n in range(188, 196))
    infs, nans = (df[196], df[197]), [df[n] for n in range(198, 202)]
    zeros = [df[n] for n in (*range(202, 210), *range(225, 228))]
    zero_ds, minus_five_ds = ds[4471], ds[4472]
    with rung(*once(slot)):
        math(big * ten, inf)
        math(inf - inf, nan)
        math(-inf * two, infs[0])
        math(-inf / two, infs[1])
        math(-nan * two, nans[0])
        math(-nan / two, nans[1])
        math(-inf * zero, nans[2])
        math(-inf / inf, nans[3])
        math(-zero, zeros[0])
        math(-zero * minus_five, zeros[1])
        math(-zero / minus_five, zeros[2])
        math(-five % five, zeros[3])
        math(minus_five % five, zeros[4])
        math(-zero_ds, zeros[5])
        math(-zero_ds * minus_five_ds, zeros[6])
        math(zero_ds * minus_five_ds, zeros[7])
        math(five % five, zeros[8])
        math(five % minus_five, zeros[9])
        math(minus_five % minus_five, zeros[10])
    signs = _signs(slot, zeros[:8], 461) + _signs(slot, zeros[8:], 480)
    # One rung for both checks keeps the part under its rung limit. So a 0 does not say
    # which half failed, Inf or NaN.
    _reading(slot, slot.Result1, (1, *(x <= -3.0e38 for x in infs), *(x != x for x in nans)))
    _bits(slot, slot.Result2, *signs[0:3])
    _bits(slot, slot.Result3, *signs[3:5], signs[8])
    _bits(slot, slot.Result4, *signs[9:11])
    _bits(slot, slot.ErrorCode, *signs[5:8])
    finish(slot)


def init_minus_sign(slot: Any) -> None:
    copy(3.0e38, df[188])
    copy(10.0, df[189])
    copy(0.0, df[190])
    copy(0.0, df[191])
    copy(2.0, df[192])
    copy(0.0, df[193])
    copy(-5.0, df[194])
    copy(5.0, df[195])
    fill(99.0, df.select(196, 209))
    fill(99.0, df.select(225, 227))
    fill(0, dd.select(461, 468))
    fill(0, dd.select(480, 482))
    copy(0, ds[4471])
    copy(-5, ds[4472])


#: The formulas of ``by_zero``, one for each ``math()`` in rung order, typed without brackets.
BY_ZERO_FORMULAS = (
    "DF212 * DF213",
    "DF210 / DF212",
    "DF211 / DF212",
    "- DF210 / DF212",
    "DF212 / DF212",
    "DF214 / DF212",
    "DF210 MOD DF212",
    "DF211 MOD DF212",
    "- DF210 MOD DF212",
    "DF212 MOD DF212",
    "DF214 MOD DF212",
)


def prove_by_zero(slot: Any) -> None:
    five, minus_five, zero, minus_one, minus_zero = (df[n] for n in range(210, 215))
    zeros = [df[n] for n in range(215, 225)]
    with rung(*once(slot)):
        # 0.0 * -1.0 stores -0.0 (``real_bits/minus_zero_math``).
        math(zero * minus_one, minus_zero)
        math(five / zero, zeros[0])
        math(minus_five / zero, zeros[1])
        math(-five / zero, zeros[2])
        math(zero / zero, zeros[3])
        math(minus_zero / zero, zeros[4])
        math(five % zero, zeros[5])
        math(minus_five % zero, zeros[6])
        math(-five % zero, zeros[7])
        math(zero % zero, zeros[8])
        math(minus_zero % zero, zeros[9])
    signs = _signs(slot, [*zeros, minus_zero], 469)
    _bits(slot, slot.Result1, *signs[0:3])
    _bits(slot, slot.Result2, *signs[3:5])
    _bits(slot, slot.Result3, *signs[5:8])
    _bits(slot, slot.Result4, *signs[8:10])
    _bits(slot, slot.Scratch, signs[10])
    finish(slot)


def init_by_zero(slot: Any) -> None:
    copy(5.0, df[210])
    copy(-5.0, df[211])
    copy(0.0, df[212])
    copy(-1.0, df[213])
    fill(99.0, df.select(214, 224))
    fill(0, dd.select(469, 479))


#: The formulas of ``zero_sum``, one for each ``math()`` in rung order, typed without brackets.
ZERO_SUM_FORMULAS = (
    "DF230 * DF231",
    "DS4473 + DS4473",
    "DS4474 + DS4475",
    "DS4474 - DS4474",
    "DF228 - DF228",
    "DF229 + DF228",
    "DF230 + DF230",
    "DF232 + DF230",
    "DF232 MOD DF228",
    "DF230 MOD DF228",
    "DF232 * DF229",
    "DF232 / DF229",
)


def prove_zero_sum(slot: Any) -> None:
    five, minus_five, zero, minus_one, minus_zero = (df[n] for n in range(228, 233))
    zero_ds, five_ds, minus_five_ds = ds[4473], ds[4474], ds[4475]
    zeros = [df[n] for n in range(233, 244)]
    with rung(*once(slot)):
        # 0.0 * -1.0 stores -0.0 (``real_bits/minus_zero_math``).
        math(zero * minus_one, minus_zero)
        math(zero_ds + zero_ds, zeros[0])
        math(five_ds + minus_five_ds, zeros[1])
        math(five_ds - five_ds, zeros[2])
        math(five - five, zeros[3])
        math(minus_five + five, zeros[4])
        math(zero + zero, zeros[5])
        math(minus_zero + zero, zeros[6])
        math(minus_zero % five, zeros[7])
        math(zero % five, zeros[8])
        math(minus_zero * minus_five, zeros[9])
        math(minus_zero / minus_five, zeros[10])
    signs = _signs(slot, zeros, 483)
    _bits(slot, slot.Result1, signs[0])
    _bits(slot, slot.Result2, *signs[1:3])
    _bits(slot, slot.Result3, *signs[3:5])
    _bits(slot, slot.Result4, *signs[5:7])
    _bits(slot, slot.Scratch, *signs[7:9])
    _bits(slot, slot.ErrorCode, *signs[9:11])
    finish(slot)


def init_zero_sum(slot: Any) -> None:
    copy(5.0, df[228])
    copy(-5.0, df[229])
    copy(0.0, df[230])
    copy(-1.0, df[231])
    fill(99.0, df.select(232, 243))
    fill(0, dd.select(483, 493))
    copy(0, ds[4473])
    copy(5, ds[4474])
    copy(-5, ds[4475])


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
            "A minus sign before Inf gives -Inf, and a step on NaN gives NaN (Result1 = 1). The "
            "other fields hold the sign bits of zeros. - DF 0.0 gives -0.0 (Result2 = 1). "
            "- DF 5.0 MOD DF 5.0 and DF -5.0 MOD DF 5.0 give -0.0, and DF 5.0 MOD DF 5.0 gives "
            "+0.0 (Result3 = 3). DF 5.0 MOD DF -5.0 gives +0.0 and DF -5.0 MOD DF -5.0 gives -0.0 "
            "(Result4 = 2). A Math computes a DS of 0 as the double +0.0. So - DS 0 gives -0.0, "
            "- DS 0 * DS -5 gives +0.0 and DS 0 * DS -5 gives -0.0 (ErrorCode = 5)",
            ladder=prove_minus_sign,
            init=init_minus_sign,
            id="minus_sign",
            expect={"Result1": 1, "Result2": 1, "Result3": 3, "Result4": 2, "ErrorCode": 5},
            formulas=MINUS_FORMULAS,
            probe="what a minus sign gives before 0, Inf and NaN, and which order a CLICK "
            "applies to it with * and /",
        ),
        case(
            "pyrung expects a division or MOD by zero to give +0.0 whatever the dividend. Each "
            "field holds the sign bits of the zeros, so Result1 to Result4 read 0. 0.0 * -1.0 "
            "gives -0.0 (Scratch = 1)",
            ladder=prove_by_zero,
            init=init_by_zero,
            id="by_zero",
            expect={"Result1": 0, "Result2": 0, "Result3": 0, "Result4": 0, "Scratch": 1},
            formulas=BY_ZERO_FORMULAS,
            probe="which sign the 0 of a division or MOD by zero takes",
        ),
        case(
            "Each field holds the sign bits of zeros. Each sum to 0 gives +0.0 (Result1 to "
            "Result4 = 0). DF -0.0 MOD DF 5.0 gives -0.0 and DF 0.0 MOD DF 5.0 gives +0.0 "
            "(Scratch = 1). DF -0.0 * DF -5.0 and DF -0.0 / DF -5.0 give +0.0 (ErrorCode = 0)",
            ladder=prove_zero_sum,
            init=init_zero_sum,
            id="zero_sum",
            expect={
                "Result1": 0,
                "Result2": 0,
                "Result3": 0,
                "Result4": 0,
                "Scratch": 1,
                "ErrorCode": 0,
            },
            formulas=ZERO_SUM_FORMULAS,
            probe="which sign the 0 of a sum takes, and the 0 of a MOD, * and / of 0",
        ),
    ],
)


def test_math_range():
    assert_all_passed(run(group))
