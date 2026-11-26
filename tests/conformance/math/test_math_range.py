"""Check what a Math formula does with a step past the largest double, or below the smallest
normal one. This is the ``math_range`` group, in part ``follow5_reads``.

A CLICK computes a formula's steps in doubles (``faults/real_intermediate_range``). 10 ^ 300
is inside a double, and 10 ^ 300 * 1.0E+30 passes the largest one. No reading shows what a
CLICK does then. It might carry Inf to the store, hold the step at the largest double, set
the step to 0 as it does a division by zero, or skip the write.

``double_overflow`` is a probe. It expects what pyrung gives, which is Inf carried to the
store. Each destination is a DF preset to 99.0, so a skipped write reads 99.0.

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

The pairs ``- DS1 * DS2`` and ``- DS1 / DS2`` (and their bracket forms) give the same value
in either order while each step stays inside a double. A longer formula in place of DS2 can
take a step past the largest double. One order takes that step last, and the other takes it
before the minus sign. The model of a CLICK (``formula_values.py``) has no reading of such a
step, so the importer warns on these pairs. Three readings would let them join
``formula.SAME_VALUE_PAIRS`` again:

- Result3 reads 1 or 2. Then a minus sign gives the same value in both orders past the
  largest double.
- Scratch reads 1 or 2. Then a minus sign gives the same value in both orders below the
  smallest normal double. With 2, the model must also say which sign the 0 keeps.
- A reading shows what a minus sign before a DF that holds Inf or NaN gives.

The case owns what ``memory.OWNED_APART`` lists under this group.
"""

from __future__ import annotations

from typing import Any

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import copy, fill, math, rung, system
from tests.conformance.memory import df
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
    ],
)


def test_math_range():
    assert_all_passed(run(group))
