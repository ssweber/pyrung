"""Copy forms nothing has read: the ``copy_more`` and ``block_more`` groups.

Part ``copy`` (``suite.PARTS``).  An expectation is pyrung's reading today; a CLICK that reads
otherwise prints what it read.  Text is read as a pair of characters, 128 * first + second,
so 6197 is "15" (49, 53); a NUL reads 0.

``copy_more``:

- ``across_types``: a Copy of a constant into a C bit: 2 (Result1), 1 where any nonzero is on, 0
  where the low bit is taken.  2.7 into an Int (ErrorCode): 2 truncated, 3 rounded.  The exporter
  refuses a DS or DF into a C and a C into a DS or DF (``CLK_COPY_BANK_INCOMPATIBLE``), so those
  are not asked.
- ``pointer_limits``: ``faults/pointer_past_bank`` reads a DD past its bank.  The others, by
  pointer into a slot staged at 99: DS4501 (Result1), DH501 (Result2, by 1 for 0, 2 for
  kept), DF501 (Result3, 1 for 0.0, 2 for kept), TXT1001 (Result4).  pyrung reads 0 for
  each.  ErrorCode counts those that raised SC43 (units) or SC44 (tens).
- ``to_text_real``: a Real into text, fixed: 1.5 is "1.5000000", first pair "1." (Result1),
  pair 9-10 "0" and NUL (Result2).  1.0E10 is "10000000000.000" or "1.0000000E+10" in fixed,
  first pair (Result3) and pair 2-3 (Result4), where the second char tells.  Exponential: 1.5
  is "1.5000000E+00", pair 10-11 "E+" (ErrorCode).
- ``to_text_ints``: a DD in fixed width: "0000000042" pair 9-10 "42" (Result1), and pair 11-12
  NUL (Result2).  Negative: "-0000000042" first pair "-0" (Result3).  A DH: "00AB" pair 3-4
  "AB" (Result4, fixed), or "00171" as decimal.  Suppressed: "171" (17, ErrorCode = 6327) is pyrung, or "AB" (ErrorCode = 8386): pyrung reads a Word as hex fixed and decimal suppressed.
- ``pack_text_forms``: Pack Copy text into a DF: "12.5" (Result1, * 10 = 125), "1.5E+02" (Result2,
  by 1 for 150.0, 2 for 1.0, 3 with SC43 and kept).  "abc" into a DF: SC43 and kept (Result3 =
  1).  "00ff" into a DH, lower case: 1 for 0xFF, 2 with SC43 and kept (Result4).  "+5" into
  an Int (ErrorCode): 5, 0 with SC43 and kept at 99.

``block_more``:

- ``block_forms``: a block copy from TXT by Character Value of "1", "A", "2" into three DS at 99,
  read as 10 * first + third (Result1) and the middle (Result2, 1 for 0, 2 for 99, 3 for 'A' at
  17).  pyrung leaves the whole range on a bad character: 1089, 2.  Stopping at it: 109, 2.  Going
  past it: 12 with 1 or 3.  Fill: DF 2.7 into a DS range: 2 truncated, 3 rounded (Result3).  Fill
  of 2 into a C range: on, by any nonzero (Result4 = 1), off by the low bit.

The cases own what ``memory.OWNED`` lists under these groups.
"""

from __future__ import annotations

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import (
    Rung,
    blockcopy,
    calc,
    copy,
    fill,
    pack_text,
    reset,
    system,
    to_ascii,
    to_text,
    to_value,
)
from tests.conformance.memory import c, dd, df, dh, ds, txt
from tests.conformance.probe_kit import class_of, finish, flag_bit, once

SC43 = system.fault.out_of_range
SC44 = system.fault.address_error


def _pair(slot, first, second, into):
    with Rung(*once(slot)):
        copy(first, slot.Scratch, convert=to_ascii)
        calc(slot.Scratch * 128, into)
        copy(second, slot.Scratch, convert=to_ascii)
        calc(into + slot.Scratch, into)


def prove_across_types(slot):
    with Rung(*once(slot)):
        copy(2, c[1737])
        copy(2.7, slot.ErrorCode)
    with Rung(*once(slot), c[1737]):
        copy(1, slot.Result1)
    finish(slot)


def init_across_types(slot):
    reset(c[1737])


def prove_pointer_limits(slot):
    with Rung(*once(slot)):
        copy(4501, slot.Scratch)
        copy(99, slot.Result1)
        copy(ds[slot.Scratch], slot.Result1)
    flag_bit(slot, SC43, slot.ErrorCode, 1)
    flag_bit(slot, SC44, slot.ErrorCode, 10)
    with Rung(*once(slot)):
        copy(501, slot.Scratch)
        copy(99, dh[67])
        copy(dh[slot.Scratch], dh[67])
    flag_bit(slot, SC43, slot.ErrorCode, 1)
    flag_bit(slot, SC44, slot.ErrorCode, 10)
    with Rung(*once(slot)):
        copy(99.0, df[194])
        copy(df[slot.Scratch], df[194])
    flag_bit(slot, SC43, slot.ErrorCode, 1)
    flag_bit(slot, SC44, slot.ErrorCode, 10)
    with Rung(*once(slot)):
        copy(1001, slot.Scratch)
        copy("Z", txt[570])
        copy(txt[slot.Scratch], txt[570])
    flag_bit(slot, SC43, slot.ErrorCode, 1)
    flag_bit(slot, SC44, slot.ErrorCode, 10)
    class_of(slot, dh[67], [0, 99], slot.Result2)
    class_of(slot, df[194], [0.0, 99.0], slot.Result3)
    with Rung(*once(slot)):
        copy(txt[570], slot.Result4, convert=to_ascii)
    finish(slot)


def init_pointer_limits(slot):
    copy(0, dh[67])
    copy(0.0, df[194])
    copy("\x00", txt[570])


def prove_to_text_real(slot):
    with Rung(*once(slot)):
        copy(1.5, df[195])
        copy(1.0e10, df[196])
        copy(df[195], txt[571], convert=to_text())
        copy(df[196], txt[591], convert=to_text())
        copy(df[195], txt[611], convert=to_text(exponential=True))
    _pair(slot, txt[571], txt[572], slot.Result1)
    _pair(slot, txt[579], txt[580], slot.Result2)
    _pair(slot, txt[591], txt[592], slot.Result3)
    _pair(slot, txt[592], txt[593], slot.Result4)
    _pair(slot, txt[620], txt[621], slot.ErrorCode)
    finish(slot)


def init_to_text_real(slot):
    fill(0.0, df.select(195, 196))
    fill("\x00", txt.select(571, 590))
    fill("\x00", txt.select(591, 610))
    fill("\x00", txt.select(611, 630))


def prove_to_text_ints(slot):
    with Rung(*once(slot)):
        copy(42, dd[59])
        copy(-42, dd[60])
        copy(0x00AB, dh[68])
        copy(dd[59], txt[631], convert=to_text(suppress_zero=False))
        copy(dd[60], txt[651], convert=to_text(suppress_zero=False))
        copy(dh[68], txt[671], convert=to_text(suppress_zero=False))
        copy(dh[68], txt[691], convert=to_text())
    _pair(slot, txt[639], txt[640], slot.Result1)
    _pair(slot, txt[641], txt[642], slot.Result2)
    _pair(slot, txt[651], txt[652], slot.Result3)
    _pair(slot, txt[673], txt[674], slot.Result4)
    _pair(slot, txt[691], txt[692], slot.ErrorCode)
    finish(slot)


def init_to_text_ints(slot):
    fill(0, dd.select(59, 60))
    copy(0, dh[68])
    fill("\x00", txt.select(631, 650))
    fill("\x00", txt.select(651, 670))
    fill("\x00", txt.select(671, 690))
    fill("\x00", txt.select(691, 700))


def _stage(slot, text, first):
    """Stage *text* into TXT from *first*: one Copy per character."""
    with Rung(*once(slot)):
        for offset, char in enumerate(text):
            copy(char, txt[first + offset])


def prove_pack_text_forms(slot):
    _stage(slot, "12.5", 701)
    _stage(slot, "1.5E+02", 711)
    _stage(slot, "abc", 721)
    _stage(slot, "00ff", 731)
    _stage(slot, "+5", 741)
    with Rung(*once(slot)):
        copy(99.0, df[197])
        copy(99.0, df[198])
        copy(99.0, df[199])
        copy(99, dh[69])
        copy(99, slot.ErrorCode)
        pack_text(txt.select(701, 704), df[197])
        calc(df[197] * 10, slot.Result1)
        pack_text(txt.select(711, 717), df[198])
    flag_bit(slot, SC43, slot.Result2, 10)
    with Rung(*once(slot)):
        pack_text(txt.select(721, 723), df[199])
    with Rung(*once(slot), SC43, df[199] == 99.0):
        copy(1, slot.Result3)
    with Rung(*once(slot)):
        pack_text(txt.select(731, 734), dh[69])
        pack_text(txt.select(741, 742), slot.ErrorCode)
    class_of(slot, df[198], [150.0, 1.0, 99.0], slot.Scratch)
    with Rung(*once(slot)):
        calc(slot.Result2 + slot.Scratch, slot.Result2)
    class_of(slot, dh[69], [0xFF, 99], slot.Result4)
    finish(slot)


def init_pack_text_forms(slot):
    fill(0.0, df.select(197, 199))
    copy(0, dh[69])
    fill("\x00", txt.select(701, 704))
    fill("\x00", txt.select(711, 717))
    fill("\x00", txt.select(721, 723))
    fill("\x00", txt.select(731, 734))
    fill("\x00", txt.select(741, 742))


def prove_block_forms(slot):
    _stage(slot, "1A2", 751)
    with Rung(*once(slot)):
        fill(99, ds.select(3915, 3917))
        blockcopy(txt.select(751, 753), ds.select(3915, 3917), convert=to_value)
        calc(ds[3915] * 10 + ds[3917], slot.Result1)
        copy(2.7, df[200])
        fill(df[200], ds.select(3918, 3919))
        fill(2, c.select(1738, 1740))
    class_of(slot, ds[3916], [0, 99, 17], slot.Result2)
    with Rung(*once(slot)):
        copy(ds[3918], slot.Result3)
    with Rung(*once(slot), c[1739]):
        copy(1, slot.Result4)
    finish(slot)


def init_block_forms(slot):
    fill(0, ds.select(3915, 3919))
    copy(0.0, df[200])
    fill(False, c.select(1738, 1740))
    fill("\x00", txt.select(751, 753))


group = Group(
    "copy_more",
    [
        case(
            "A Copy across types: a constant 2 into a C bit is on (Result1), 2.7 into an Int "
            "truncates (ErrorCode = 2)",
            ladder=prove_across_types,
            init=init_across_types,
            expect={"Result1": 1, "ErrorCode": 2},
            probe="what a Copy does across bits and numbers",
        ),
        case(
            "A pointer past the bank reads 0 with no flag: DS4501 (Result1), DH501 (Result2 = 1), "
            "DF501 (Result3 = 1), TXT1001 (Result4 = 0); SC43 and SC44 raised (ErrorCode)",
            ladder=prove_pointer_limits,
            init=init_pointer_limits,
            expect={"Result1": 0, "Result2": 1, "Result3": 1, "Result4": 0, "ErrorCode": 0},
            probe="a pointer past each other bank: kept or zero, flagged or not",
        ),
        case(
            "A Real to text: 1.5 is 1.5000000 (Result1 pair 1-2, Result2 pair 9-10), 1.0E10 reads "
            "fixed (Result3, Result4), 1.5 exponential has E+ at 10-11 (ErrorCode)",
            ladder=prove_to_text_real,
            init=init_to_text_real,
            expect={
                "Result1": 6318,
                "Result2": 6144,
                "Result3": 6320,
                "Result4": 6192,
                "ErrorCode": 8875,
            },
            probe="what a Real reads as text, fixed and exponential",
        ),
        case(
            "A DD fixed: 0000000042 (Result1 = 42 pair, Result2 = NUL pair), negative -0000000042 "
            "(Result3); a DH fixed is 00AB (Result4), suppressed reads 171 in pyrung (ErrorCode)",
            ladder=prove_to_text_ints,
            init=init_to_text_ints,
            expect={
                "Result1": 6706,
                "Result2": 0,
                "Result3": 5808,
                "Result4": 8386,
                "ErrorCode": 6327,
            },
            probe="a Dint and a Word as text",
        ),
        case(
            "Pack text: DF 12.5 (Result1 = 125), 1.5E+02 is 150.0 (Result2 = 1), abc faults kept "
            "(Result3 = 1), DH 00ff is 0xFF (Result4 = 1), Int +5 reads 5 (ErrorCode)",
            ladder=prove_pack_text_forms,
            init=init_pack_text_forms,
            expect={"Result1": 125, "Result2": 1, "Result3": 1, "Result4": 1, "ErrorCode": 5},
            probe="Pack text into a Real, a lower-case Word, a sign",
        ),
    ],
)

block_group = Group(
    "block_more",
    [
        case(
            "A block copy to_value with a bad character leaves its range (Result1 = 1089, "
            "Result2 = 2); a Fill of DF 2.7 truncates, 2 (Result3); a Fill of 2 into bits is on "
            "(Result4 = 1)",
            ladder=prove_block_forms,
            init=init_block_forms,
            expect={"Result1": 1089, "Result2": 2, "Result3": 2, "Result4": 1},
            probe="a block copy's converter; a Fill across types",
        ),
    ],
)


def test_copy_more():
    assert_all_passed(run(group))


def test_block_more():
    assert_all_passed(run(block_group))
