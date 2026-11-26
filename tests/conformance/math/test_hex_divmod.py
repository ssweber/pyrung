"""Test what a hex Math's ``/`` and MOD store: the ``hex_divmod`` group.

The group is the whole of part ``follow6_reads`` (``suite.APART_PARTS``), on memory of
its own.  Every tag in it is a DH, so each Math runs in hex mode.  The expectations are
what a CLICK stored on 2026-11-26, each in a program of its own.  Each case runs once
(``probe_kit``).  A Copy of a DH into a slot keeps its 16 bits, so FFFDh reads -3.  Each
DH result starts at BEEFh, so a Math that writes nothing reads -16657.

- ``division_truncates``: ``DH / DH * DH`` with 7, 2 and 2 (Result1) is 6 if each ``/``
  truncates, and 7 if the formula keeps the half.  The same with ``+ 0`` at the end
  (Result2) is 6 or 7.  With 1, 2 and 4 (Result3) it is 0, or 2 for a kept half.  A CLICK
  reads 6, 6 and 0.
- ``mod_truncates``: ``- DH MOD DH``.  With F0h and 3h (Result1) every reading is 0, and a
  16-bit cut of the negated DH reads 1.  With F1h and 7h (Result2) a MOD that truncates
  reads -3 (FFFDh).  A MOD that floors reads 4, and a 16-bit cut reads 6.  ErrorCode bit 1
  is SC43 after the first Math, and bit 2 after the second.  A CLICK reads 0, -3 and 2.
- ``product_not_cut``: ``DH * DH MOD DH``.  With 100h, 101h and 7h (Result1) the whole
  product reads 6, and a product cut to 16 bits reads 4.  With FF38h, FE70h and 7h
  (Result2) unsigned operands read 5.  A cut product reads 2, a signed 32-bit product
  -6 (FFFAh), and signed operands 4.  ErrorCode bits 1 and 2 are SC43 after each.  A CLICK
  reads 6, 5 and 0.

The cases own DH141..DH167.
"""

from __future__ import annotations

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import copy, fill, math, rung, system
from tests.conformance.memory import dh
from tests.conformance.probe_kit import finish, flag_bit, once

SC43 = system.fault.out_of_range

#: Each DH result starts here.  As an Int it reads -16657.
UNWRITTEN = 0xBEEF


def prove_division_truncates(slot):
    with rung(*once(slot)):
        copy(7, dh[141])
        copy(2, dh[142])
        copy(1, dh[143])
        copy(4, dh[144])
        fill(UNWRITTEN, dh.select(145, 147))
        math(dh[141] / dh[142] * dh[142], dh[145])
        math(dh[141] / dh[142] * dh[142] + 0, dh[146])
        math(dh[143] / dh[142] * dh[144], dh[147])
        copy(dh[145], slot.Result1)
        copy(dh[146], slot.Result2)
        copy(dh[147], slot.Result3)
    finish(slot)


def init_division_truncates(slot):
    fill(0, dh.select(141, 147))


def prove_mod_truncates(slot):
    with rung(*once(slot)):
        copy(0xF0, dh[151])
        copy(3, dh[152])
        copy(0xF1, dh[153])
        copy(7, dh[154])
        fill(UNWRITTEN, dh.select(155, 156))
        math(-dh[151] % dh[152], dh[155])
    flag_bit(slot, SC43, slot.ErrorCode, 1)
    with rung(*once(slot)):
        math(-dh[153] % dh[154], dh[156])
    flag_bit(slot, SC43, slot.ErrorCode, 2)
    with rung(*once(slot)):
        copy(dh[155], slot.Result1)
        copy(dh[156], slot.Result2)
    finish(slot)


def init_mod_truncates(slot):
    fill(0, dh.select(151, 156))


def prove_product_not_cut(slot):
    with rung(*once(slot)):
        copy(0x100, dh[161])
        copy(0x101, dh[162])
        copy(7, dh[163])
        copy(0xFF38, dh[164])
        copy(0xFE70, dh[165])
        fill(UNWRITTEN, dh.select(166, 167))
        math(dh[161] * dh[162] % dh[163], dh[166])
    flag_bit(slot, SC43, slot.ErrorCode, 1)
    with rung(*once(slot)):
        math(dh[164] * dh[165] % dh[163], dh[167])
    flag_bit(slot, SC43, slot.ErrorCode, 2)
    with rung(*once(slot)):
        copy(dh[166], slot.Result1)
        copy(dh[167], slot.Result2)
    finish(slot)


def init_product_not_cut(slot):
    fill(0, dh.select(161, 167))


group = Group(
    "hex_divmod",
    [
        case(
            "A hex Math truncates each division. DH / DH * DH with 7, 2 and 2 is 6 (Result1), "
            "and 6 with + 0 at the end (Result2). With 1, 2 and 4 it is 0 (Result3).",
            ladder=prove_division_truncates,
            init=init_division_truncates,
            expect={"Result1": 6, "Result2": 6, "Result3": 0},
        ),
        case(
            "A hex MOD truncates and does not cut the negated DH to 16 bits. - DH MOD DH with "
            "F0h and 3h is 0 (Result1). With F1h and 7h it is FFFDh (Result2 = -3) and sets "
            "SC43 (ErrorCode = 2).",
            ladder=prove_mod_truncates,
            init=init_mod_truncates,
            expect={"Result1": 0, "Result2": -3, "ErrorCode": 2},
        ),
        case(
            "A hex Math reads each DH unsigned and does not cut a product to 16 bits before a "
            "MOD. DH * DH MOD DH with 100h, 101h and 7h is 6 (Result1). With FF38h, FE70h and "
            "7h it is 5 (Result2). Neither sets SC43 (ErrorCode = 0).",
            ladder=prove_product_not_cut,
            init=init_product_not_cut,
            expect={"Result1": 6, "Result2": 5, "ErrorCode": 0},
        ),
    ],
)


def test_hex_divmod():
    assert_all_passed(run(group))
