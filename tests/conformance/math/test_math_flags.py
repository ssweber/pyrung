"""Test which fault flags a Math sets: the ``math_flags`` group.

The group is in part ``follow6_reads`` (``suite.APART_PARTS``), on memory of its own.  The
expectations are what a CLICK did on 2026-11-26, each formula in a program of its own.
Each case runs once (``probe_kit``).  A flag is read on the rung right after its Math,
because every Math clears SC40, SC43, SC44 and SC46 as it starts.

- ``mod_by_zero``: ``( DS 5 MOD DS 0 ) + DS 1`` into a DS preset to 99 (Result1), and a hex
  ``DH 5h MOD DH 0h`` into a DH preset to 63h (Result2).  The MOD gives 0 and the rest of
  the formula runs, so Result1 reads 1 and Result2 reads 0.  If the whole formula were 0,
  Result1 would read 0.  If the Math skipped its write, the Results would read 99 and 99.
  ErrorCode adds 1 and 2 for SC46 after each.  A CLICK reads 1, 0 and 3.  A model that
  sets no flag for a MOD by 0, or sets SC40 as for a division by 0, reads ErrorCode = 0.

The cases own what ``memory.OWNED_APART`` lists under this group.
"""

from __future__ import annotations

from typing import Any

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import copy, fill, math, rung, system
from tests.conformance.memory import dh, ds
from tests.conformance.probe_kit import finish, flag_bit, once

SC46 = system.fault.math_operation_error


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
            "A MOD by 0 gives 0 and sets SC46, and the rest of the formula runs. "
            "( 5 MOD 0 ) + 1 stores 1 (Result1). A hex 5h MOD 0h stores 0h (Result2). Both "
            "set SC46 (ErrorCode = 3).",
            ladder=prove_mod_by_zero,
            init=init_mod_by_zero,
            expect={"Result1": 1, "Result2": 0, "ErrorCode": 3},
        ),
    ],
)


def test_math_flags():
    assert_all_passed(run(group))
