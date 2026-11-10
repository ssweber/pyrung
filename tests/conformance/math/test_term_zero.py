"""A quiet Math error's 0 in a larger formula: the ``term_zero`` group (part ``follow2_values``).

``math_terms/error_terms`` read on a CLICK that a division by 0 and a SQRT of a negative each zero
their own term, not the formula (5, 5, 5 for ``... + 5``).  A CLICK zeroes the term of a MOD by 0,
of ``0 ^ 0`` and of ABS of the least Int too (hardware, 2026-11-10).  0 to a negative power is no
error there.  It is +Inf.  Each formula here runs once, on a scan whose first rung stages its
operands, into a register preset to 99 (no store reads 99).

``quiet_terms`` stores each ``... + 5`` into a DS.  Every operand is a DS, since pyrung folds no
DS and the editor is not asked to take a constant divisor of 0.

- Result1 ``MOD(DS 7, DS 0) + 5`` reads 5, the MOD's term zeroed (0 would be the formula zeroed).
- Result2 ``DS 0 ^ DS 0 + 5`` reads 5, the power's term zeroed (6 would be ``0 ^ 0`` taken as 1).
- Result3 ``DS 0 ^ DS -1 + 5`` reads 0.  The power is +Inf, so the formula is +Inf, and a Math
  stores +Inf into a DS as 0.
- ErrorCode adds 1, 2 and 4 for SC40 after each, read by a coil right after it.  It reads 0, so
  none raises SC40.

``abs_int_min``:

- Result1 ``ABS(DS -32768) + 1`` into a DS reads 1, the ABS's term zeroed (0 would be the formula
  zeroed, and -32767 a 32768 + 1 wrapped).
- Result2 is ``ABS(DS -32768)`` into a DD, as a class.  It reads 2 for 0 (1 would be 32768, 3
  -32768 and 4 no store).
- ErrorCode adds 1 and 2 for SC43 after each.  It reads 3, so both raise SC43.

``real_quiet_terms`` stores ``DF 7.5 MOD DF 0.0`` (Result1) and ``DF 0.0 ^ DF -1.0`` (Result2)
into DFs, each read as a class:

- 1 is +Inf (>= 3.0E+38) and 2 is -Inf (<= -3.0E+38).
- 3 is NaN (not equal to itself) and 4 is 0.0.
- 5 is 99.0 (no store) and 0 is any other value.

The MOD stores 0.0 (4) and the power +Inf (1).  Result3 and Result4 size each (DF * 10 into an
Int).  They read 0 and 0, since a Math stores +Inf into an Int as 0.

The cases own what ``memory.OWNED_APART`` lists under this group.
"""

from __future__ import annotations

from typing import Any

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import Rung, copy, fill, math, out, reset, system
from tests.conformance.memory import c, dd, df, ds
from tests.conformance.probe_kit import class_of, finish, once

SC40 = system.fault.division_error
SC43 = system.fault.out_of_range


def _coil_bits(slot: Any, coils: list[Any], into: Any) -> None:
    """*into* += 1, 2, 4, ... for each coil on: below every op."""
    for k, coil in enumerate(coils):
        with Rung(*once(slot), coil):
            math(into + (1 << k), into)


def prove_quiet_terms(slot: Any) -> None:
    seven, zero, minus = ds[4041], ds[4042], ds[4043]
    coils = [c[n] for n in range(681, 684)]
    with Rung(*once(slot)):
        copy(7, seven)
        copy(0, zero)
        copy(-1, minus)
        copy(99, slot.Result1)
        copy(99, slot.Result2)
        copy(99, slot.Result3)
    formulas = [
        (seven % zero + 5, slot.Result1),
        (zero**zero + 5, slot.Result2),
        (zero**minus + 5, slot.Result3),
    ]
    for (formula, field), coil in zip(formulas, coils, strict=True):
        with Rung(*once(slot)):
            math(formula, field)
        with Rung(*once(slot), SC40):
            out(coil)
    _coil_bits(slot, coils, slot.ErrorCode)
    finish(slot)


def init_quiet_terms(slot: Any) -> None:
    reset(c.select(681, 683))
    fill(0, ds.select(4041, 4043))


def prove_abs_int_min(slot: Any) -> None:
    least, into = ds[4044], dd[131]
    coils = [c[n] for n in range(684, 686)]
    with Rung(*once(slot)):
        copy(-32768, least)
        copy(99, slot.Result1)
        copy(99, into)
        math(abs(least) + 1, slot.Result1)
    with Rung(*once(slot), SC43):
        out(coils[0])
    with Rung(*once(slot)):
        math(abs(least), into)
    with Rung(*once(slot), SC43):
        out(coils[1])
    _coil_bits(slot, coils, slot.ErrorCode)
    class_of(slot, into, [32768, 0, -32768, 99], slot.Result2)
    finish(slot)


def init_abs_int_min(slot: Any) -> None:
    reset(c.select(684, 685))
    copy(0, ds[4044])
    copy(0, dd[131])


#: What a quiet error into a DF may leave, in the class's order after +/-Inf and NaN.
REAL_READINGS = (0.0, 99.0)


def prove_real_quiet_terms(slot: Any) -> None:
    seven_half, zero, minus = df[71], df[72], df[73]
    results = (df[74], df[75])
    with Rung(*once(slot)):
        copy(7.5, seven_half)
        copy(0.0, zero)
        copy(-1.0, minus)
        for result in results:
            copy(99.0, result)
        math(seven_half % zero, results[0])
        math(zero**minus, results[1])
    for result, field in zip(results, (slot.Result1, slot.Result2), strict=True):
        with Rung(*once(slot), result >= 3.0e38):
            copy(1, field)
        with Rung(*once(slot), result <= -3.0e38):
            copy(2, field)
        with Rung(*once(slot), result != result):
            copy(3, field)
        for number, reading in enumerate(REAL_READINGS, start=4):
            with Rung(*once(slot), result == reading):
                copy(number, field)
    with Rung(*once(slot)):
        math(results[0] * 10, slot.Result3)
        math(results[1] * 10, slot.Result4)
    finish(slot)


def init_real_quiet_terms(slot: Any) -> None:
    fill(0.0, df.select(71, 75))


group = Group(
    "term_zero",
    [
        case(
            "A MOD by 0 and 0 ^ 0 zero their own term, so MOD(7, 0) + 5 (Result1) and 0 ^ 0 + 5 "
            "(Result2) store 5. 0 ^ -1 is +Inf, so 0 ^ -1 + 5 into an Int stores 0 (Result3). "
            "None raises SC40 (ErrorCode = 0)",
            ladder=prove_quiet_terms,
            init=init_quiet_terms,
            expect={"Result1": 5, "Result2": 5, "Result3": 0, "ErrorCode": 0},
        ),
        case(
            "ABS(-32768) zeroes its own term, so ABS(-32768) + 1 into an Int stores 1 (Result1). "
            "ABS(-32768) into a DD stores 0 (Result2 = 2). Both raise SC43 (ErrorCode = 3)",
            ladder=prove_abs_int_min,
            init=init_abs_int_min,
            expect={"Result1": 1, "Result2": 2, "ErrorCode": 3},
        ),
        case(
            "7.5 MOD 0.0 into a DF stores 0.0 (Result1 = 4) and 0.0 ^ -1.0 stores +Inf "
            "(Result2 = 1). Each * 10 into an Int stores 0 (Result3, Result4)",
            ladder=prove_real_quiet_terms,
            init=init_real_quiet_terms,
            expect={"Result1": 4, "Result2": 1, "Result3": 0, "Result4": 0},
        ),
    ],
)


def test_term_zero():
    assert_all_passed(run(group))
