"""Test a Copy into a Word from a value below an Int and from NaN: the ``word_floor`` group.

The group is part of ``follow_timing``, where there is room.

On a CLICK, DD -1 copied into a DH writes 65535, which is the wrap (``word_store/word_below_zero``).
This case asks what a value below -32768 writes. A store could wrap all 16 bits, or it could
first floor the value at the least Int. The case also asks what a NaN writes.

``below_int_and_nan`` stages the values on scan 1. Making the NaN raises SC43, and scan 2 starts
without it. The case stores the values on scan 2, each into a DH preset to 0x1234 (4660). A store
that is refused then reads differently from one that writes. The results are:

- Result1 is for DD -40000. It is 1 for 25536 (all 16 bits wrap), 2 for 32768 (floored at -32768,
  then wrapped), 3 for 0 (clamped at 0), 4 for 4660 (no store), and 0 for any other value.
- Result2 is for DD -70000. It is 1 for 61072 (all 16 bits wrap), 2 for 32768 (floored), 3 for 0,
  4 for 4660, and 0 for any other value.
- Result3 is for a DF that holds NaN. The DF is +Inf - +Inf, as ``store_probe/inf_and_nan_to_text``
  made one. Result3 is 1 for 0, 2 for 4660 (no store), 3 for 65535, 4 for 32768, 5 for 32767, and
  0 for any other value.
- Result4 is a set of bits. Bit 1 is set where the DF is a NaN, meaning it is neither >= 0 nor
  < 0, so Result3 does ask about a NaN. This assumes that a compare with a NaN is false, as IEEE's
  unordered compares are. A 1 shows a NaN, and a 0 does not rule one out. Bit 16 is SC43 on just
  before the first store. It is the control that shows no raise carried over from the stage scan.
  Bits 2, 4 and 8 are SC43 on the rung after each store, as ``word_store`` reads it. If a Copy
  does not settle SC43, a raise from an earlier store also reads in a later bit. The compares
  ``DF32>=0`` and ``DF32<0`` use forms that were pasted before (``DF21<0`` in ``real_bits``,
  ``DF14>=3.0E+38``).
- ErrorCode is the size of a Result1 of 0, or else of a Result2 of 0. The case copies that DH
  into a DD and halves it. ErrorCode is 0 when both are classed. The DH into a DD may read
  unsigned or signed (``word_store/word_into_signed`` asks). A DH past 32767 therefore reads
  16384..32767 or -16384..-1.

A CLICK reads 2, 2, 1, 1 and 0. It floors each value at -32768, which a Word holds as 32768. A
NaN writes 0. No store raises SC43.

The cases own what ``memory.OWNED_APART`` lists under this group.
"""

from __future__ import annotations

from typing import Any

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import copy, fill, math, rung, system
from tests.conformance.memory import dd, df, dh
from tests.conformance.scans import count_scans, done, scans_for

SC43 = system.fault.out_of_range
SENTINEL = 0x1234
WINDOW = 2
STAGE_SCAN, STORE_SCAN = 1, 2


def _class_of(slot: Any, value: Any, readings: list[int], into: Any) -> None:
    """Set ``into`` to 1 if *value* equals ``readings[0]``, 2 if it equals the next, and so on.

    The rungs run after the stores.
    """
    for number, reading in enumerate(readings, start=1):
        with rung(slot.Scratch > WINDOW, value == reading):
            copy(number, into)


def prove_below_int_and_nan(slot: Any) -> None:
    minus_40000, minus_70000, size = dd[91], dd[92], dd[93]
    inf, nan = df[31], df[32]
    words = [dh[n] for n in (21, 22, 23)]
    count_scans(slot, WINDOW)
    with rung(slot.Scratch == STAGE_SCAN):
        copy(-40000, minus_40000)
        copy(-70000, minus_70000)
        copy(3.0e38, inf)
        math(inf * 10, inf)
        math(inf - inf, nan)
        fill(SENTINEL, dh.select(21, 23))
    with rung(slot.Scratch == STORE_SCAN):
        copy(1, slot.Result4)
    with rung(slot.Scratch == STORE_SCAN, nan >= 0):
        copy(0, slot.Result4)
    with rung(slot.Scratch == STORE_SCAN, nan < 0):
        copy(0, slot.Result4)
    with rung(slot.Scratch == STORE_SCAN, SC43):
        math(slot.Result4 + 16, slot.Result4)
    for source, word, bit in zip((minus_40000, minus_70000, nan), words, (2, 4, 8), strict=True):
        with rung(slot.Scratch == STORE_SCAN):
            copy(source, word)
        with rung(slot.Scratch == STORE_SCAN, SC43):
            math(slot.Result4 + bit, slot.Result4)
    _class_of(slot, words[0], [25536, 32768, 0, SENTINEL], slot.Result1)
    _class_of(slot, words[1], [61072, 32768, 0, SENTINEL], slot.Result2)
    _class_of(slot, words[2], [0, SENTINEL, 65535, 32768, 32767], slot.Result3)
    with rung(slot.Scratch > WINDOW, slot.Fired == 0, slot.Result1 == 0):
        copy(words[0], size)
        math(size / 2, slot.ErrorCode)
    with rung(slot.Scratch > WINDOW, slot.Fired == 0, slot.Result1 != 0, slot.Result2 == 0):
        copy(words[1], size)
        math(size / 2, slot.ErrorCode)
    done(slot, WINDOW)


def init_below_int_and_nan(slot: Any) -> None:
    fill(0, dd.select(91, 93))
    fill(0.0, df.select(31, 32))
    fill(0, dh.select(21, 23))


group = Group(
    "word_floor",
    [
        case(
            "A Copy into a DH preset 4660 floors a value below -32768 at -32768, which a Word "
            "holds as 32768.  DD -40000 writes 32768 (Result1 = 2, 1 for 25536 all 16 bits "
            "wrapped, 3 for 0, 4 for none).  DD -70000 writes 32768 too (Result2 = 2, 1 for "
            "61072 all 16 bits wrapped, 3 for 0, 4 for none).  A DF NaN writes 0 (Result3 = 1, "
            "2 none, 3 65535, 4 32768, 5 32767).  Result4 = 1 is bit 1 alone.  Bit 1 says the DF "
            "is a NaN, since it is neither >= 0 nor < 0.  Bits 2, 4, 8 and 16 stay off, so SC43 "
            "is off before the first store and after each one.  Result1 and Result2 each read a "
            "listed value, so ErrorCode stays 0",
            ladder=prove_below_int_and_nan,
            init=init_below_int_and_nan,
            expect={"Result1": 2, "Result2": 2, "Result3": 1, "Result4": 1, "ErrorCode": 0},
            scans=scans_for(WINDOW),
        ),
    ],
)


def test_word_floor():
    assert_all_passed(run(group))
