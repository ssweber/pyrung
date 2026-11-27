"""Test what a hex Math's ``/`` stores for a dividend below 0: the ``hex_div_down`` group.

The group is in part ``follow6_reads`` (``suite.APART_PARTS``), on memory of its own.  Every
tag in it is a DH, so each Math runs in hex mode.  The expectations are what a CLICK stored
on 2026-11-27, each formula in a program of its own.  The case runs once (``probe_kit``).  A
Copy of a DH into a slot keeps its 16 bits, so FFFFh reads -1.  Each DH result starts at
BEEFh, so a Math that writes nothing reads -16657.

- ``negative_quotient``: ``( 0h - DH ) / 3h`` with DH = 1h (Result1).  A division that rounds
  down gives -1 (FFFFh) and sets SC43.  A division that truncates toward 0 gives 0 with no
  SC43.  A division on 32 unsigned bits gives 5555h (21845) and sets SC43.  Then
  ``( 0h - DH ) MOD 3h`` (Result2).  A MOD that truncates gives -1 (FFFFh) and sets SC43.  The
  MOD that matches a division that rounds down gives 2 with no SC43.  ErrorCode adds 1 for
  SC43 after the division and 2 for SC43 after the MOD.  A CLICK reads -1, -1 and 3.

The CLICK did not divide by a value below 0.

The cases own what ``memory.OWNED_APART`` lists under this group.
"""

from __future__ import annotations

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import copy, fill, math, rung, system
from tests.conformance.memory import dh
from tests.conformance.probe_kit import finish, flag_bit, once

SC43 = system.fault.out_of_range

#: Each DH result starts here.  As an Int it reads -16657.
UNWRITTEN = 0xBEEF


def prove_negative_quotient(slot):
    with rung(*once(slot)):
        copy(1, dh[195])
        fill(UNWRITTEN, dh.select(196, 197))
        math((0 - dh[195]) / 3, dh[196])
    flag_bit(slot, SC43, slot.ErrorCode, 1)
    with rung(*once(slot)):
        math((0 - dh[195]) % 3, dh[197])
    flag_bit(slot, SC43, slot.ErrorCode, 2)
    with rung(*once(slot)):
        copy(dh[196], slot.Result1)
        copy(dh[197], slot.Result2)
    finish(slot)


def init_negative_quotient(slot):
    fill(0, dh.select(195, 197))


group = Group(
    "hex_div_down",
    [
        case(
            "A hex Math rounds a quotient below 0 down, and its MOD truncates. ( 0h - DH ) / 3h "
            "with 1h is FFFFh (Result1 = -1). ( 0h - DH ) MOD 3h is FFFFh too (Result2 = -1). "
            "Each sets SC43 (ErrorCode = 3).",
            ladder=prove_negative_quotient,
            init=init_negative_quotient,
            expect={"Result1": -1, "Result2": -1, "ErrorCode": 3},
        ),
    ],
)


def test_hex_div_down():
    assert_all_passed(run(group))
