"""A quiet Math error's 0 in a larger formula: the ``term_zero`` group (part ``follow2_values``).

``math_terms/error_terms`` read on a CLICK that a division by 0 and a SQRT of a negative each zero
their own term, not the formula (5, 5, 5 for ``... + 5``), where pyrung zeroes the whole formula.
The quiet errors (``MOD`` by 0, ``0 ^ 0``, ``0 ^ -1``) and ABS of the least Int are coded at the
formula's level too, and their only readings are of a formula of one term.  Each formula here runs
once, on a scan whose first rung stages its operands, into a register preset to 99 (no store
reads 99).

- ``quiet_terms`` (M1), each ``... + 5`` into a DS: Result1 ``MOD(DS 7, DS 0)``, Result2 ``DS 0 ^
  DS 0``, Result3 ``DS 0 ^ DS -1`` (every operand a DS: pyrung folds no DS, and the editor is not
  asked to take a constant divisor of 0).  5: the term is 0; 0: the formula is; 6 for ``0 ^ 0``
  as 1; 99 no store.  ErrorCode has SC40 after each (bits 1, 2, 4), read by a coil right after it.
  pyrung reads 0, 0, 0, 0: each formula zeroed, and no SC40.
- ``abs_int_min`` (M2): Result1 ``ABS(DS -32768) + 1`` into a DS: 1 for the term zeroed, 0 for the
  formula, -32767 for 32768 + 1 wrapped (or ABS wrapping to -32768, + 1).  Result2 ``ABS(DS
  -32768)`` into a DD, as a class: 1 for 32768, 2 for 0, 3 for -32768, 4 for 99, 0 for another.
  ErrorCode has SC43 after each (bits 1, 2).  pyrung reads 0, 2, 3: ABS of the least Int is out
  of range, zeroing the formula, into a DD too, with SC43 both times.
- ``real_quiet_terms`` (M3): ``DF 7.5 MOD DF 0.0`` (Result1) and ``DF 0.0 ^ DF -1.0`` (Result2)
  into DFs, as a class: 1 for +Inf (>= 3.0E+38), 2 for -Inf (<= -3.0E+38), 3 for NaN (not equal to
  itself), 4 for 0.0, 5 for 99.0 (no store), 0 for another, which Result3 and Result4 size (each
  DF * 10 into an Int).  pyrung reads 4, 4, 0, 0: 0.0 for both.

The cases own what ``memory.OWNED_APART`` lists under this group.
"""

from __future__ import annotations

from typing import Any

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import Rung, copy, fill, math, out, reset, system
from tests.conformance.memory import c, dd, df, ds
from tests.conformance.probe_kit import between, class_of, finish, once

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
            "A quiet math error zeroes its term, not the formula: MOD(7, 0) + 5 (Result1), "
            "0 ^ 0 + 5 (Result2) and 0 ^ -1 + 5 (Result3) read 5 (0 for the formula, 99 no "
            "store); SC40 after each (ErrorCode bits 1, 2, 4)",
            ladder=prove_quiet_terms,
            init=init_quiet_terms,
            expect={
                "Result1": between(0, 99),
                "Result2": between(0, 99),
                "Result3": between(0, 99),
                "ErrorCode": between(0, 7),
            },
            probe="a quiet math error zeroes its term or its formula (2)",
        ),
        case(
            "ABS(-32768) + 1 into an Int (Result1: 1 the term zeroed, 0 the formula, -32767 "
            "wrapped) and ABS(-32768) into a DD (Result2: 1 for 32768, 2 for 0, 3 for -32768); "
            "SC43 after each (ErrorCode bits 1, 2)",
            ladder=prove_abs_int_min,
            init=init_abs_int_min,
            expect={
                "Result1": between(-32768, 99),
                "Result2": between(0, 4),
                "ErrorCode": between(0, 3),
            },
            probe="ABS of the least Int in a formula, and into a DD (2)",
        ),
        case(
            "7.5 MOD 0.0 (Result1) and 0.0 ^ -1.0 (Result2) into a DF: 1 +Inf, 2 -Inf, 3 NaN, "
            "4 0.0, 5 no store, 0 another, sized by Result3 and Result4 (DF * 10)",
            ladder=prove_real_quiet_terms,
            init=init_real_quiet_terms,
            expect={
                "Result1": between(0, 5),
                "Result2": between(0, 5),
                "Result3": between(-32768, 32767),
                "Result4": between(-32768, 32767),
            },
            probe="a Real MOD by 0 and 0 ^ -1: NaN, Inf or 0.0 (2)",
        ),
    ],
)


def test_term_zero():
    assert_all_passed(run(group))
