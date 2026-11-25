"""Check a Fill of a number into TXT registers, and Copy Binary into a DD or a DF: the
``fill_binary`` group (part ``follow5_reads``).

A CLICK C2-03CPU on firmware 4.00 ran each of these stores in a short program written for the
question, and pyrung was changed to match.  No case here has run on a CLICK yet.  Each case
does all its work on the scan it fires (``probe_kit.once``).  A rung right after each store
reads SC43, so no later instruction can clear the flag first.

- ``fill_number_into_txt`` writes "ZZZZ" into TXT451..TXT454, then fills 5 into
  TXT451..TXT453.  Result1..Result4 hold the ASCII code of each TXT.  A CLICK writes the
  character "5" (53) into the three TXT and leaves TXT454 at "Z" (90).  ErrorCode holds SC43
  after the Fill, and a CLICK leaves it off (0).  If the Fill stored the number 5 itself, the
  three codes would read 5.
- ``copy_binary_df_dd`` copies DF451 = 2.5 into DD451 with Copy Binary, then DD452 =
  1075838976 into DF452.  Result1 classes DD451: 1 for 1075838976 (0x40200000, the bits of
  2.5), 2 for 2, 3 for 0 and 4 for 305419896 (its value before the Copy).  Result2 classes
  DF452 the same way: 1 for 2.5, 2 for 1075838976.0, 3 for 0.0 and 4 for -7.25.  A CLICK moves
  the 32 bits unchanged, so both read 1.  A Copy that converted the value would read 2 and 2.
  Result3 holds SC43 after each Copy as bits 1 and 2, and a CLICK raises neither (0).
- ``copy_binary_constants`` copies the constant 1.5 into DD453 and 40000 into DF453, both
  with Copy Binary.  Result1 classes DD453: 1 for 1, 2 for 2, 3 for 1069547520 (the bits of
  1.5) and 4 for 305419896.  Result2 classes DF453: 1 for 40000.0, 2 for 0.0 and 3 for -7.25.
  The bits of 40000 read as a Real are about 5.6E-41, which is none of these (0).  A CLICK
  stores what a plain Copy stores, so both read 1.  The case then copies the constant 2.0
  into DD454 with Copy Binary.  Result4 classes DD454: 1 for 2, 2 for 1073741824 (the bits
  of 2.0), 3 for 0 and 4 for 305419896.  Reading b9 (section B) saw a CLICK store 2, as a
  plain Copy of 2.0 does, so it reads 1.  Result3 holds SC43 after each of the three Copy
  Binary as bits 1 to 3, and a CLICK reads 0.

The cases own what ``memory.OWNED_APART`` lists under this group.
"""

from __future__ import annotations

from typing import Any

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import copy, fill, rung, system, to_ascii, to_binary
from tests.conformance.memory import dd, df, txt
from tests.conformance.probe_kit import class_of, finish, flag_bit, once

SC43 = system.fault.out_of_range
#: A DD's value before each Copy Binary into it.
DD_BEFORE = 305419896
#: A DF's value before each Copy Binary into it.
DF_BEFORE = -7.25


def prove_fill_number_into_txt(slot: Any) -> None:
    with rung(*once(slot)):
        copy("ZZZZ", txt[451])
    with rung(*once(slot)):
        fill(5, txt.select(451, 453))
    flag_bit(slot, SC43, slot.ErrorCode)
    fields = (slot.Result1, slot.Result2, slot.Result3, slot.Result4)
    for address, field in zip(range(451, 455), fields, strict=True):
        with rung(*once(slot)):
            copy(txt[address], field, convert=to_ascii)
    finish(slot)


def init_fill_number_into_txt(slot: Any) -> None:
    fill("\x00", txt.select(451, 454))


def prove_copy_binary_df_dd(slot: Any) -> None:
    with rung(*once(slot)):
        copy(2.5, df[451])
        copy(1075838976, dd[452])
        copy(DD_BEFORE, dd[451])
        copy(DF_BEFORE, df[452])
    with rung(*once(slot)):
        copy(df[451], dd[451], convert=to_binary)
    flag_bit(slot, SC43, slot.Result3, 1)
    with rung(*once(slot)):
        copy(dd[452], df[452], convert=to_binary)
    flag_bit(slot, SC43, slot.Result3, 2)
    class_of(slot, dd[451], (1075838976, 2, 0, DD_BEFORE), slot.Result1)
    class_of(slot, df[452], (2.5, 1075838976.0, 0.0, DF_BEFORE), slot.Result2)
    finish(slot)


def init_copy_binary_df_dd(slot: Any) -> None:
    fill(0, dd.select(451, 452))
    fill(0.0, df.select(451, 452))


def prove_copy_binary_constants(slot: Any) -> None:
    with rung(*once(slot)):
        copy(DD_BEFORE, dd[453])
        copy(DF_BEFORE, df[453])
        copy(DD_BEFORE, dd[454])
    with rung(*once(slot)):
        copy(1.5, dd[453], convert=to_binary)
    flag_bit(slot, SC43, slot.Result3, 1)
    with rung(*once(slot)):
        copy(40000, df[453], convert=to_binary)
    flag_bit(slot, SC43, slot.Result3, 2)
    # Reading b9 (section B): a CLICK stored 2 for 2.0, with no flag.
    with rung(*once(slot)):
        copy(2.0, dd[454], convert=to_binary)
    flag_bit(slot, SC43, slot.Result3, 3)
    class_of(slot, dd[453], (1, 2, 1069547520, DD_BEFORE), slot.Result1)
    class_of(slot, df[453], (40000.0, 0.0, DF_BEFORE), slot.Result2)
    class_of(slot, dd[454], (2, 1073741824, 0, DD_BEFORE), slot.Result4)
    finish(slot)


def init_copy_binary_constants(slot: Any) -> None:
    copy(0, dd[453])
    copy(0.0, df[453])
    copy(0, dd[454])


group = Group(
    "fill_binary",
    [
        case(
            'A Fill of 5 into TXT451..TXT453 writes the character "5" (Result1..Result3 = 53) '
            'and leaves TXT454 at "Z" (Result4 = 90), with no SC43 (ErrorCode = 0)',
            ladder=prove_fill_number_into_txt,
            init=init_fill_number_into_txt,
            expect={"Result1": 53, "Result2": 53, "Result3": 53, "Result4": 90, "ErrorCode": 0},
        ),
        case(
            "A Copy Binary of DF451 = 2.5 into DD451 stores 1075838976 (Result1 = 1), and of "
            "DD452 = 1075838976 into DF452 stores 2.5 (Result2 = 1), with no SC43 (Result3 = 0)",
            ladder=prove_copy_binary_df_dd,
            init=init_copy_binary_df_dd,
            expect={"Result1": 1, "Result2": 1, "Result3": 0},
        ),
        case(
            "A Copy Binary of the constant 1.5 into DD453 stores 1 (Result1 = 1), of 40000 "
            "into DF453 stores 40000.0 (Result2 = 1), and of 2.0 into DD454 stores 2 "
            "(Result4 = 1), with no SC43 (Result3 = 0)",
            ladder=prove_copy_binary_constants,
            init=init_copy_binary_constants,
            expect={"Result1": 1, "Result2": 1, "Result4": 1, "Result3": 0},
        ),
    ],
)


def test_fill_binary():
    assert_all_passed(run(group))
