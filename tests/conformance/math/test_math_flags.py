"""Test which fault flags a Math sets: the ``math_flags`` group.

The group is in part ``follow6_reads`` (``suite.APART_PARTS``), on memory of its own.  The
expectations are what a CLICK did on 2026-11-26, each formula in a program of its own.
Each case runs once (``probe_kit``).  Every Math clears SC40, SC43, SC44 and SC46 as it
starts, even on a false rung.  So a case reads a flag on the rung right after its Math,
with ``flag_bit``, or reads several flags with coils.

- ``minus_sets_sc43``: ``- DH``, ``0h - DH`` and ``DH 1h - DH 2h``, with DH = 2h.  Each
  stores FFFEh, FFFEh and FFFFh (Results1..3 read -2, -2 and -1), and each sets SC43
  (ErrorCode adds 1, 2 and 4, so it reads 7).
- ``last_op_sets_sc43``: ``( 0h - DH )`` followed by ``+ 0h``, ``* 1h``, ``/ 1h`` and
  ``MOD 7h``, with DH = 2h.  Each stores FFFEh (Results1..4 read -2) and sets SC43
  (ErrorCode = 15).
- ``bitwise_unsigned``: ``( 0h - DH )`` followed by ``AND FFFFh``, ``OR 0h``, ``XOR 0h``
  and ``OR 1h``, with DH = 2h.  These store FFFEh, FFFEh, FFFEh and FFFFh (Results1..4
  read -2, -2, -2 and -1), and none sets SC43 (ErrorCode = 0).  A model that keeps the OR
  or XOR result signed reads ErrorCode = 14.
- ``mod_by_zero``: ``( DS 5 MOD DS 0 ) + DS 1`` into a DS preset to 99 (Result1), and a hex
  ``DH 5h MOD DH 0h`` into a DH preset to 63h (Result2).  The MOD gives 0 and the rest of
  the formula runs, so Result1 reads 1 and Result2 reads 0.  If the whole formula were 0,
  Result1 would read 0.  If the Math skipped its write, the Results would read 99 and 99.
  ErrorCode adds 1 and 2 for SC46 after each.  A CLICK reads 1, 0 and 3.  A model that
  sets no flag for a MOD by 0, or sets SC40 as for a division by 0, reads ErrorCode = 0.
- ``or_result_unsigned``: ``( ( 0h - DH ) OR 0h ) + 1h`` with DH = 2h and 1h, and
  ``( ( 0h - DH ) OR 0h ) - 1h`` with DH = 2h.  The step after the OR reads its 16 unsigned
  bits.  So these store FFFFh, 0000h and FFFDh (Results1..3 read -1, 0 and -3).  Only the
  second ends past FFFFh, at 65536, so only it sets SC43 (ErrorCode = 2).  A model that
  keeps the OR's result signed ends at -1, 0 and -3, and reads ErrorCode = 5.
- ``hex_by_zero_goes_on``: ``( DH / DH ) + 1h`` and ``( DH MOD DH ) + 1h`` with 5h and 0h.
  Each step by 0 gives 0 and the formula goes on, so both store 0001h (Results1..2 read 1).
  Coils read SC40 and SC46 after each.  ErrorCode adds 1 and 2 for SC40 and SC46 after the
  division, and 4 and 8 for SC40 and SC46 after the MOD.  A CLICK reads 9.  A model that
  treats a MOD by 0 as a division by 0 reads 5.
- ``false_rung_clears_sc46``: ``DS 5 MOD DS 0`` sets SC46.  A coil reads it right after
  (ErrorCode adds 1), and again after a rung that holds only that coil (adds 2).  Then a
  ``math(0, DS)`` on a false rung clears it, and a third coil reads it (adds 4).  A CLICK
  reads ErrorCode = 3.  A model where a false-rung Math leaves the flag reads 7.  The
  false-rung Math writes nothing, so its DS keeps its 99 (Result1).

The export writes ``- DH`` inside a longer formula as ``( 0h - DH )``.  The CLICK was read
with ``- DH`` there, and ``( 0h - DH ) OR 0h`` gave the same result as ``- DH OR 0h``.

The cases own what ``memory.OWNED_APART`` lists under this group.
"""

from __future__ import annotations

from typing import Any

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import copy, fill, math, out, reset, rung, system
from tests.conformance.memory import c, dh, ds
from tests.conformance.probe_kit import finish, flag_bit, once

SC40 = system.fault.division_error
SC43 = system.fault.out_of_range
SC46 = system.fault.math_operation_error

#: A ``Scratch`` value that no case writes, for a rung that is never true.
NEVER = 99


def _hex_steps(slot: Any, sources: dict[Any, int], formulas: list[tuple[Any, Any]]) -> None:
    """Stage *sources*, run each Math on a rung of its own, and read SC43 after each.

    Result1.. hold the stores in order, and ErrorCode adds 1, 2, 4 and 8 for SC43.
    """
    with rung(*once(slot)):
        for tag, value in sources.items():
            copy(value, tag)
    results = (slot.Result1, slot.Result2, slot.Result3, slot.Result4)
    for bit, (formula, dest) in enumerate(formulas):
        with rung(*once(slot)):
            copy(0x63, dest)
            math(formula, dest)
        flag_bit(slot, SC43, slot.ErrorCode, 1 << bit)
    with rung(*once(slot)):
        for (_formula, dest), result in zip(formulas, results, strict=False):
            copy(dest, result)
    finish(slot)


def prove_minus_sets_sc43(slot: Any) -> None:
    two, one = dh[171], dh[172]
    _hex_steps(
        slot,
        {two: 2, one: 1},
        [(-two, dh[173]), (0 - two, dh[174]), (one - two, dh[175])],
    )


def init_minus_sets_sc43(slot: Any) -> None:
    fill(0, dh.select(171, 175))


def prove_last_op_sets_sc43(slot: Any) -> None:
    two = dh[176]
    _hex_steps(
        slot,
        {two: 2},
        [(-two + 0, dh[177]), (-two * 1, dh[178]), (-two / 1, dh[179]), (-two % 7, dh[180])],
    )


def init_last_op_sets_sc43(slot: Any) -> None:
    fill(0, dh.select(176, 180))


def prove_bitwise_unsigned(slot: Any) -> None:
    two = dh[181]
    _hex_steps(
        slot,
        {two: 2},
        [
            (-two & 0xFFFF, dh[182]),
            (-two | 0, dh[183]),
            (-two ^ 0, dh[184]),
            (-two | 1, dh[185]),
        ],
    )


def init_bitwise_unsigned(slot: Any) -> None:
    fill(0, dh.select(181, 185))


def prove_or_result_unsigned(slot: Any) -> None:
    two, one = dh[186], dh[187]
    _hex_steps(
        slot,
        {two: 2, one: 1},
        [
            (((0 - two) | 0) + 1, dh[188]),
            (((0 - one) | 0) + 1, dh[189]),
            (((0 - two) | 0) - 1, dh[190]),
        ],
    )


def init_or_result_unsigned(slot: Any) -> None:
    fill(0, dh.select(186, 190))


def _read_with_coils(slot: Any, reads: list[tuple[Any, Any]]) -> None:
    """Copy each flag into its coil, on rungs of their own, so that no Math clears it."""
    for flag, coil in reads:
        with rung(*once(slot), flag):
            out(coil)


def _record_coils(slot: Any, coils: list[Any]) -> None:
    """Add 1, 2, 4 and so on to ErrorCode for each coil that is on."""
    for number, coil in enumerate(coils):
        with rung(*once(slot), coil):
            math(slot.ErrorCode + (1 << number), slot.ErrorCode)


def prove_hex_by_zero_goes_on(slot: Any) -> None:
    five, zero = dh[191], dh[192]
    divided, modded = dh[193], dh[194]
    coils = [c[744], c[745], c[746], c[747]]
    with rung(*once(slot)):
        copy(5, five)
        copy(0, zero)
        copy(0x63, divided)
        copy(0x63, modded)
        math((five / zero) + 1, divided)
    _read_with_coils(slot, [(SC40, coils[0]), (SC46, coils[1])])
    with rung(*once(slot)):
        math((five % zero) + 1, modded)
    _read_with_coils(slot, [(SC40, coils[2]), (SC46, coils[3])])
    _record_coils(slot, coils)
    with rung(*once(slot)):
        copy(divided, slot.Result1)
        copy(modded, slot.Result2)
    finish(slot)


def init_hex_by_zero_goes_on(slot: Any) -> None:
    fill(0, dh.select(191, 194))
    reset(c.select(744, 747))


def prove_false_rung_clears_sc46(slot: Any) -> None:
    five, zero, result, sink = ds[4304], ds[4305], ds[4306], ds[4307]
    coils = [c[741], c[742], c[743]]
    with rung(*once(slot)):
        copy(5, five)
        copy(0, zero)
        copy(99, sink)
        math(five % zero, result)
    _read_with_coils(slot, [(SC46, coils[0]), (SC46, coils[1])])
    with rung(slot.Scratch == NEVER):
        math(0, sink)
    _read_with_coils(slot, [(SC46, coils[2])])
    _record_coils(slot, coils)
    with rung(*once(slot)):
        copy(sink, slot.Result1)
    finish(slot)


def init_false_rung_clears_sc46(slot: Any) -> None:
    fill(0, ds.select(4304, 4307))
    reset(c.select(741, 743))


def prove_mod_by_zero(slot: Any) -> None:
    five, zero, one = ds[4301], ds[4302], ds[4303]
    hex_five, hex_zero, hex_result = dh[168], dh[169], dh[170]
    with rung(*once(slot)):
        copy(5, five)
        copy(0, zero)
        copy(1, one)
        copy(99, slot.Result1)
        copy(5, hex_five)
        copy(0, hex_zero)
        copy(0x63, hex_result)
        math((five % zero) + one, slot.Result1)
    flag_bit(slot, SC46, slot.ErrorCode, 1)
    with rung(*once(slot)):
        math(hex_five % hex_zero, hex_result)
    flag_bit(slot, SC46, slot.ErrorCode, 2)
    with rung(*once(slot)):
        copy(hex_result, slot.Result2)
    finish(slot)


def init_mod_by_zero(slot: Any) -> None:
    fill(0, ds.select(4301, 4303))
    fill(0, dh.select(168, 170))


group = Group(
    "math_flags",
    [
        case(
            "In hex mode a minus that gives a negative result sets SC43. - 2h stores FFFEh, "
            "0h - 2h stores FFFEh and 1h - 2h stores FFFFh, and each sets SC43 "
            "(ErrorCode = 7).",
            ladder=prove_minus_sets_sc43,
            init=init_minus_sets_sc43,
            expect={"Result1": -2, "Result2": -2, "Result3": -1, "ErrorCode": 7},
        ),
        case(
            "In hex mode a +, *, / or MOD after 0h - 2h stores FFFEh and sets SC43 "
            "(ErrorCode = 15).",
            ladder=prove_last_op_sets_sc43,
            init=init_last_op_sets_sc43,
            expect={"Result1": -2, "Result2": -2, "Result3": -2, "Result4": -2, "ErrorCode": 15},
        ),
        case(
            "In hex mode AND, OR and XOR give 16 unsigned bits. After 0h - 2h, AND FFFFh, "
            "OR 0h and XOR 0h store FFFEh and OR 1h stores FFFFh. None sets SC43 "
            "(ErrorCode = 0).",
            ladder=prove_bitwise_unsigned,
            init=init_bitwise_unsigned,
            expect={"Result1": -2, "Result2": -2, "Result3": -2, "Result4": -1, "ErrorCode": 0},
        ),
        case(
            "A MOD by 0 gives 0 and sets SC46, and the rest of the formula runs. "
            "( 5 MOD 0 ) + 1 stores 1 (Result1). A hex 5h MOD 0h stores 0h (Result2). Both "
            "set SC46 (ErrorCode = 3).",
            ladder=prove_mod_by_zero,
            init=init_mod_by_zero,
            expect={"Result1": 1, "Result2": 0, "ErrorCode": 3},
        ),
        case(
            "In hex mode the step after an OR reads its 16 unsigned bits. With 2h, "
            "( ( 0h - DH ) OR 0h ) + 1h stores FFFFh and - 1h stores FFFDh, with no SC43. "
            "With 1h, + 1h ends at 65536, so it stores 0000h and sets SC43 (ErrorCode = 2).",
            ladder=prove_or_result_unsigned,
            init=init_or_result_unsigned,
            expect={"Result1": -1, "Result2": 0, "Result3": -3, "ErrorCode": 2},
        ),
        case(
            "In hex mode a division or MOD by 0 gives 0 and the formula goes on. "
            "( 5h / 0h ) + 1h and ( 5h MOD 0h ) + 1h both store 0001h. The division sets "
            "SC40 only, and the MOD sets SC46 only (ErrorCode = 9).",
            ladder=prove_hex_by_zero_goes_on,
            init=init_hex_by_zero_goes_on,
            expect={"Result1": 1, "Result2": 1, "ErrorCode": 9},
        ),
        case(
            "A Math on a false rung clears SC46 and writes nothing. A rung with only a coil "
            "leaves SC46 on (ErrorCode = 3). The false-rung Math's DS keeps 99 (Result1).",
            ladder=prove_false_rung_clears_sc46,
            init=init_false_rung_clears_sc46,
            expect={"Result1": 99, "ErrorCode": 3},
        ),
    ],
)


def test_math_flags():
    assert_all_passed(run(group))
