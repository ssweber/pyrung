"""What a Word holds after a store, and what comes out of one: the ``word_store`` group.

Part ``copy`` (``suite.PARTS``).  Read on a CLICK (2026-11-05): a Copy, Block copy or Fill into a
Word from a register wraps below 0 and clamps past 65535, neither with a flag; a constant keeps its
low 16 bits.  A Word copied into an Int keeps its bits, and into a DD or a DF is its value
(``core/instruction/conversions.py``).  A DH under test is preset to 0x1234, so an instruction that
refuses reads apart from one that writes.  Results are classes (``probe_kit.class_of``), 0 for any
other value; SC43 bits are read on the rung after each op.

- ``word_below_zero``: DS -1, DD -1, DF -1.6 copied into a Word.  Wrap (CLICK): 1, 1, 1 with no
  flag.  Clamp at 0 with SC43: 2, 2, 2, Result4 = 7.  A refusal that leaves the DH: 4.  A DF that
  rounds: 3 (0xFFFE) in Result3, and its raw low word (0xCCCD) reads 0.
- ``word_above_top``: DD 70000, DD 65536, DF 70000.7.  Clamp with no flag (CLICK): 2, 2, 2,
  Result4 = 0.  Wrap: 1, 1, 1 (4464, 0, 4464).  A DF that rounds reads 3 (4465).  A DF's raw low
  word reads 0.
- ``word_into_signed``: DH 0xFFFF and 0x8000 copied into an Int.  The bits read as signed (CLICK):
  -1, -32768, no flag.  Clamp, as any value past an Int does: 32767, 32767, SC43 bits 1 and 2 in
  Result4.  The same DH into a DD (Result3) and a DF (ErrorCode): 65535 is 1, -1 is 2.
- ``block_into_word``: a Block copy into Words, element by element.  DS {-1} (Result1), DD {70000}
  (Result2), DD {-1} (Result3); SC43 after each block in Result4.  CLICK: 1, 2, 1, 0, the Copy's
  rule.  Wrap: 1, 1, 1, 0.  Clamp with SC43: 2, 2, 2, 3 (a block of DS and a block of DD).
- ``block_from_word``: DH {0xFFFF, 0x8000} block copied into Ints.  The bits (CLICK): -1, -32768,
  0.  Clamp: 32767, 32767, SC43 on (Result3 = 1).
- ``fill_into_word``: a Fill from a register into Words.  DS -1, DD 70000, DF -1.6.  CLICK: 1, 2,
  1, 0, the Copy's rule.  Wrap with no flag: 1, 1, 1, 0.  Clamp: 2, 2, 2 with whichever flags.
- ``word_literals`` (last of the group: the editor may refuse a constant out of a Word's range at
  paste, and a refusal costs only this case): the constants -1 and 70000 into a Word by Copy
  (Result1, Result2) and by Fill (Result3, Result4), SC43 in ErrorCode bits 1, 2, 4, 8.  Wrap with
  no flag (CLICK): 1, 1, 1, 1, 0.  Clamp: 2, 2, 2, 2.

A Math into a Word is hex mode, in ``hex_math``.  Each case owns ten of DD810.., a few of DF434..
and of DH422..; DS4312..DS4315 are the Block copies' Ints.
"""

from __future__ import annotations

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import Rung, blockcopy, copy, fill, system
from tests.conformance.memory import dd, df, dh, ds
from tests.conformance.probe_kit import class_of, finish, flag_bit, once

SC43 = system.fault.out_of_range
PRESET = 0x1234
BELOW = [0xFFFF, 0, 0xFFFE, PRESET]  # wrap, clamp, a Real rounded, left alone


def prove_word_below_zero(slot):
    with Rung(*once(slot)):
        copy(-1, slot.Scratch)
        copy(-1, dd[810])
        copy(-1.6, df[434])
        fill(PRESET, dh.select(422, 424))
    with Rung(*once(slot)):
        copy(slot.Scratch, dh[422])
    flag_bit(slot, SC43, slot.Result4, 1)
    with Rung(*once(slot)):
        copy(dd[810], dh[423])
    flag_bit(slot, SC43, slot.Result4, 2)
    with Rung(*once(slot)):
        copy(df[434], dh[424])
    flag_bit(slot, SC43, slot.Result4, 4)
    class_of(slot, dh[422], BELOW, slot.Result1)
    class_of(slot, dh[423], BELOW, slot.Result2)
    class_of(slot, dh[424], BELOW, slot.Result3)
    finish(slot)


def init_word_below_zero(slot):
    copy(0, dd[810])
    copy(0.0, df[434])
    fill(0, dh.select(422, 424))


def prove_word_above_top(slot):
    with Rung(*once(slot)):
        copy(70000, dd[820])
        copy(65536, dd[821])
        copy(70000.7, df[440])
        fill(PRESET, dh.select(432, 434))
    with Rung(*once(slot)):
        copy(dd[820], dh[432])
    flag_bit(slot, SC43, slot.Result4, 1)
    with Rung(*once(slot)):
        copy(dd[821], dh[433])
    flag_bit(slot, SC43, slot.Result4, 2)
    with Rung(*once(slot)):
        copy(df[440], dh[434])
    flag_bit(slot, SC43, slot.Result4, 4)
    class_of(slot, dh[432], [4464, 65535, PRESET], slot.Result1)
    class_of(slot, dh[433], [0, 65535, PRESET], slot.Result2)
    class_of(slot, dh[434], [4464, 65535, 4465, PRESET], slot.Result3)
    finish(slot)


def init_word_above_top(slot):
    fill(0, dd.select(820, 821))
    copy(0.0, df[440])
    fill(0, dh.select(432, 434))


def prove_word_into_signed(slot):
    with Rung(*once(slot)):
        copy(0xFFFF, dh[442])
        copy(0x8000, dh[443])
    with Rung(*once(slot)):
        copy(dh[442], slot.Result1)
    flag_bit(slot, SC43, slot.Result4, 1)
    with Rung(*once(slot)):
        copy(dh[443], slot.Result2)
    flag_bit(slot, SC43, slot.Result4, 2)
    with Rung(*once(slot)):
        copy(dh[442], dd[830])
        copy(dh[442], df[445])
    class_of(slot, dd[830], [65535, -1], slot.Result3)
    class_of(slot, df[445], [65535.0, -1.0], slot.ErrorCode)
    finish(slot)


def init_word_into_signed(slot):
    fill(0, dh.select(442, 443))
    copy(0, dd[830])
    copy(0.0, df[445])


def prove_block_into_word(slot):
    with Rung(*once(slot)):
        copy(-1, ds[4312])
        copy(5, ds[4313])
        copy(70000, dd[835])
        copy(-1, dd[836])
        fill(PRESET, dh.select(446, 449))
    with Rung(*once(slot)):
        blockcopy(ds.select(4312, 4313), dh.select(446, 447))
    flag_bit(slot, SC43, slot.Result4, 1)
    with Rung(*once(slot)):
        blockcopy(dd.select(835, 836), dh.select(448, 449))
    flag_bit(slot, SC43, slot.Result4, 2)
    class_of(slot, dh[446], BELOW, slot.Result1)
    class_of(slot, dh[448], [4464, 65535, PRESET], slot.Result2)
    class_of(slot, dh[449], BELOW, slot.Result3)
    finish(slot)


def init_block_into_word(slot):
    fill(0, ds.select(4312, 4313))
    fill(0, dd.select(835, 836))
    fill(0, dh.select(446, 449))


def prove_block_from_word(slot):
    with Rung(*once(slot)):
        copy(0xFFFF, dh[450])
        copy(0x8000, dh[451])
    with Rung(*once(slot)):
        blockcopy(dh.select(450, 451), ds.select(4314, 4315))
    flag_bit(slot, SC43, slot.Result3, 1)
    with Rung(*once(slot)):
        copy(ds[4314], slot.Result1)
        copy(ds[4315], slot.Result2)
    finish(slot)


def init_block_from_word(slot):
    fill(0, dh.select(450, 451))
    fill(0, ds.select(4314, 4315))


def prove_fill_into_word(slot):
    with Rung(*once(slot)):
        copy(-1, slot.Scratch)
        copy(70000, dd[840])
        copy(-1.6, df[441])
        fill(PRESET, dh.select(452, 457))
    with Rung(*once(slot)):
        fill(slot.Scratch, dh.select(452, 453))
    flag_bit(slot, SC43, slot.Result4, 1)
    with Rung(*once(slot)):
        fill(dd[840], dh.select(454, 455))
    flag_bit(slot, SC43, slot.Result4, 2)
    with Rung(*once(slot)):
        fill(df[441], dh.select(456, 457))
    flag_bit(slot, SC43, slot.Result4, 4)
    class_of(slot, dh[453], BELOW, slot.Result1)
    class_of(slot, dh[455], [4464, 65535, PRESET], slot.Result2)
    class_of(slot, dh[457], BELOW, slot.Result3)
    finish(slot)


def init_fill_into_word(slot):
    copy(0, dd[840])
    copy(0.0, df[441])
    fill(0, dh.select(452, 457))


def prove_word_literals(slot):
    with Rung(*once(slot)):
        fill(PRESET, dh.select(458, 463))
    with Rung(*once(slot)):
        copy(-1, dh[458])
    flag_bit(slot, SC43, slot.ErrorCode, 1)
    with Rung(*once(slot)):
        copy(70000, dh[459])
    flag_bit(slot, SC43, slot.ErrorCode, 2)
    with Rung(*once(slot)):
        fill(-1, dh.select(460, 461))
    flag_bit(slot, SC43, slot.ErrorCode, 4)
    with Rung(*once(slot)):
        fill(70000, dh.select(462, 463))
    flag_bit(slot, SC43, slot.ErrorCode, 8)
    class_of(slot, dh[458], BELOW, slot.Result1)
    class_of(slot, dh[459], [4464, 65535, PRESET], slot.Result2)
    class_of(slot, dh[461], BELOW, slot.Result3)
    class_of(slot, dh[463], [4464, 65535, PRESET], slot.Result4)
    finish(slot)


def init_word_literals(slot):
    fill(0, dh.select(458, 463))


group = Group(
    "word_store",
    [
        case(
            "A Copy below 0 into a Word wraps with no flag: DS -1, DD -1 and DF -1.6 store "
            "0xFFFF (Result1, Result2, Result3 = 1) and none raises SC43 (Result4 = 0)",
            ladder=prove_word_below_zero,
            init=init_word_below_zero,
            expect={"Result1": 1, "Result2": 1, "Result3": 1, "Result4": 0},
            probe="does a Copy below 0 into a Word wrap or clamp, and is SC43 raised",
        ),
        case(
            "A Copy past 65535 into a Word clamps with no flag: DD 70000, DD 65536 and DF "
            "70000.7 store 65535 (Result1, Result2, Result3 = 2), and none raises SC43 "
            "(Result4 = 0)",
            ladder=prove_word_above_top,
            init=init_word_above_top,
            expect={"Result1": 2, "Result2": 2, "Result3": 2, "Result4": 0},
            probe="does a Copy past 65535 into a Word wrap or clamp, and is SC43 raised",
        ),
        case(
            "A Word copied into an Int keeps its bits: 0xFFFF and 0x8000 store -1 and -32768 "
            "(Result1, Result2) with no SC43 (Result4 = 0); into a DD (Result3 = 1) and a DF "
            "(ErrorCode = 1) 0xFFFF is 65535",
            ladder=prove_word_into_signed,
            init=init_word_into_signed,
            expect={"Result1": -1, "Result2": -32768, "Result3": 1, "Result4": 0, "ErrorCode": 1},
            probe="does a Word copy into an Int by value (clamp) or by its bits (-1)",
        ),
        case(
            "A Block copy into Words stores each element as a Copy does, with no flag: DS -1 "
            "is 0xFFFF (Result1 = 1), DD 70000 clamps to 65535 (Result2 = 2), DD -1 is 0xFFFF "
            "(Result3 = 1), SC43 off (Result4 = 0)",
            ladder=prove_block_into_word,
            init=init_block_into_word,
            expect={"Result1": 1, "Result2": 2, "Result3": 1, "Result4": 0},
            probe="does a Block copy into Words wrap or clamp, and is SC43 raised",
        ),
        case(
            "A Block copy of Words into Ints keeps the bits: 0xFFFF and 0x8000 store -1 and "
            "-32768 (Result1, Result2), SC43 off (Result3 = 0)",
            ladder=prove_block_from_word,
            init=init_block_from_word,
            expect={"Result1": -1, "Result2": -32768, "Result3": 0},
            probe="does a Block copy from Words into Ints clamp or keep the bits",
        ),
        case(
            "A Fill from a register into Words stores as a Copy does, with no flag: DS -1 is "
            "0xFFFF (Result1 = 1), DD 70000 clamps to 65535 (Result2 = 2), DF -1.6 is 0xFFFF "
            "(Result3 = 1), SC43 off (Result4 = 0)",
            ladder=prove_fill_into_word,
            init=init_fill_into_word,
            expect={"Result1": 1, "Result2": 2, "Result3": 1, "Result4": 0},
            probe="does a Fill into Words wrap or clamp, and is SC43 raised",
        ),
        case(
            "The constants -1 and 70000 into a Word wrap with no flag, by Copy (Result1, Result2 "
            "= 1) and by Fill (Result3, Result4 = 1), SC43 off (ErrorCode = 0); the editor may "
            "refuse them at paste",
            ladder=prove_word_literals,
            init=init_word_literals,
            expect={"Result1": 1, "Result2": 1, "Result3": 1, "Result4": 1, "ErrorCode": 0},
            probe="does the editor take these constants; do they wrap or clamp",
        ),
    ],
)


def test_word_store():
    assert_all_passed(run(group))
