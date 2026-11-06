"""A Copy into a Word from below an Int and from NaN: the ``word_floor`` group (part
``follow_timing``, where there is room).

``word_store/word_below_zero`` read on a CLICK that DD -1 copied into a DH writes 65535, the
wrap.  It does not say what a value below -32768 writes, where a store could wrap all 16 bits or
first floor the value at the least Int, nor what a NaN writes.  The fix step ``store-click`` (3 in
the ``conform-fix`` arc) guesses both and waits on this.

``below_int_and_nan``: the values are staged on scan 1 (making the NaN raises SC43, which scan 2
starts without) and stored on scan 2, each into a DH preset to 0x1234 (4660), so a store that is
refused reads apart from one that writes:

- Result1, DD -40000: 1 for 25536 (all 16 bits wrap), 2 for 32768 (floored at -32768, then
  wrapped), 3 for 0 (clamped at 0), 4 for 4660 (no store), 0 for any other.
- Result2, DD -70000: 1 for 61072 (all 16 bits wrap), 2 for 32768 (floored), 3 for 0, 4 for
  4660, 0 for any other.
- Result3, a DF holding NaN (+Inf - +Inf, as ``store_probe/inf_and_nan_to_text`` made one): 1
  for 0, 2 for 4660 (no store), 3 for 65535, 4 for 32768, 5 for 32767, 0 for any other.
- Result4: bit 1 where the DF is a NaN (neither >= 0 nor < 0), so Result3 asks about one.  It
  assumes a compare with a NaN is false, as IEEE's unordered compares are: 1 shows a NaN, 0 does
  not rule one out.  Bit 16 is SC43 on just before the first store, the control that no raise
  carried over from the stage scan; bits 2, 4 and 8 are SC43 on the rung after each store, as
  ``word_store`` reads it (a raise carried from an earlier store reads in a later bit too, if a
  Copy does not settle SC43).  The compares ``DF32>=0`` and ``DF32<0`` take forms pasted before
  (``DF21<0`` in ``real_bits``, ``DF14>=3.0E+38``).
- ErrorCode: the size of a Result1 of 0, else of a Result2 of 0: that DH copied into a DD and
  halved, 0 where both are classed.  The DH into a DD may read unsigned or signed
  (``word_store/word_into_signed`` asks), so a DH past 32767 reads 16384..32767 or -16384..-1.

pyrung on dev 42dd614 wraps with no flag and reads 1, 1, 1, 1, 0.  With ``store-click``'s floor at
-32768 it reads 2, 2, 1.

The cases own what ``memory.OWNED_APART`` lists under this group.
"""

from __future__ import annotations

from typing import Any

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import Rung, calc, copy, fill, system
from tests.conformance.memory import dd, df, dh
from tests.conformance.probe_kit import between
from tests.conformance.scans import count_scans, done, scans_for

SC43 = system.fault.out_of_range
SENTINEL = 0x1234
WINDOW = 2
STAGE_SCAN, STORE_SCAN = 1, 2


def _class_of(slot: Any, value: Any, readings: list[int], into: Any) -> None:
    """``into`` = 1 when *value* equals ``readings[0]``, 2 for the next, after the stores."""
    for number, reading in enumerate(readings, start=1):
        with Rung(slot.Scratch > WINDOW, value == reading):
            copy(number, into)


def prove_below_int_and_nan(slot: Any) -> None:
    minus_40000, minus_70000, size = dd[91], dd[92], dd[93]
    inf, nan = df[31], df[32]
    words = [dh[n] for n in (21, 22, 23)]
    count_scans(slot, WINDOW)
    with Rung(slot.Scratch == STAGE_SCAN):
        copy(-40000, minus_40000)
        copy(-70000, minus_70000)
        copy(3.0e38, inf)
        calc(inf * 10, inf)
        calc(inf - inf, nan)
        fill(SENTINEL, dh.select(21, 23))
    with Rung(slot.Scratch == STORE_SCAN):
        copy(1, slot.Result4)
    with Rung(slot.Scratch == STORE_SCAN, nan >= 0):
        copy(0, slot.Result4)
    with Rung(slot.Scratch == STORE_SCAN, nan < 0):
        copy(0, slot.Result4)
    with Rung(slot.Scratch == STORE_SCAN, SC43):
        calc(slot.Result4 + 16, slot.Result4)
    for source, word, bit in zip((minus_40000, minus_70000, nan), words, (2, 4, 8), strict=True):
        with Rung(slot.Scratch == STORE_SCAN):
            copy(source, word)
        with Rung(slot.Scratch == STORE_SCAN, SC43):
            calc(slot.Result4 + bit, slot.Result4)
    _class_of(slot, words[0], [25536, 32768, 0, SENTINEL], slot.Result1)
    _class_of(slot, words[1], [61072, 32768, 0, SENTINEL], slot.Result2)
    _class_of(slot, words[2], [0, SENTINEL, 65535, 32768, 32767], slot.Result3)
    with Rung(slot.Scratch > WINDOW, slot.Fired == 0, slot.Result1 == 0):
        copy(words[0], size)
        calc(size / 2, slot.ErrorCode)
    with Rung(slot.Scratch > WINDOW, slot.Fired == 0, slot.Result1 != 0, slot.Result2 == 0):
        copy(words[1], size)
        calc(size / 2, slot.ErrorCode)
    done(slot, WINDOW)


def init_below_int_and_nan(slot: Any) -> None:
    fill(0, dd.select(91, 93))
    fill(0.0, df.select(31, 32))
    fill(0, dh.select(21, 23))


group = Group(
    "word_floor",
    [
        case(
            "A Copy into a DH preset 4660: DD -40000 writes 25536, all 16 bits wrapped "
            "(Result1 = 1, 2 for 32768 floored at -32768, 3 for 0, 4 for none); DD -70000 "
            "61072 (Result2, as Result1); a DF NaN 0 (Result3 = 1, 2 none, 3 65535, 4 32768, "
            "5 32767); the NaN made (Result4 bit 1), SC43 after each (bits 2, 4, 8) and "
            "before the first (16); an unclassed DH halved (ErrorCode)",
            ladder=prove_below_int_and_nan,
            init=init_below_int_and_nan,
            expect={
                "Result1": between(0, 4),
                "Result2": between(0, 4),
                "Result3": between(0, 5),
                "Result4": between(0, 31),
                "ErrorCode": between(-32768, 32767),
            },
            scans=scans_for(WINDOW),
            probe="a Copy into a Word below -32768 and from NaN: wrap, floor or 0 (3)",
        ),
    ],
)


def test_word_floor():
    assert_all_passed(run(group))
