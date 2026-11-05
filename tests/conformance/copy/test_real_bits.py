"""A Real's raw bits, as Modbus reads them: the ``real_bits`` group.

Part ``copy`` (``suite.PARTS``).  The harness reads slots, so the raw IEEE bits a CLICK
keeps in a DF, which is what two Modbus registers would show, come through an
Unpack Copy into two DH and a compare on each.  Expectations are what pyrung reads
today (the float64 it keeps packed to a float32), so a CLICK that reads otherwise
is an answer.

- ``minus_zero_math``: DF 0.0 * -1.0.  IEEE keeps the sign: high word 0x8000
  (Result1 = 1; 2 is 0x0000, any other 0), low word 0 (ErrorCode = 1).  A store that
  normalises it reads Result1 = 2.  It compares equal to 0 (Result2 = 1), not below
  it (Result3 = 0), and a Copy keeps the bits (Result4 = 1; 2 if a Copy normalises).
  The guarded loop's ruling (a commit publishes what was written) rests on this.
- ``minus_zero_shapes``: the other ways to reach it.  -1.0 * DF (Result1) and DF / -1.0
  (Result2) keep the sign (1), DF - DF is +0.0 (Result3 = 2), ABS ( -0.0 ) is +0.0
  (Result4 = 2).  A text copy reads its first character (ErrorCode): '-' (45) for
  pyrung's ``-0.0000000``, '0' (48) for a text path that drops the sign.
- ``non_finite_raw_bits``: +Inf is 0x7F80 (Result1 = 1), -Inf 0xFF80 (Result2 = 1) with
  a zero low word (ErrorCode = 1 for the pair), and +Inf - +Inf is a NaN whose high
  word tells which NaN (Result3): 2 for 0xFFC0 (x86's default, what Python's
  subtraction leaves), 1 for 0x7FC0 (ARM's), 3 for 0xFFFF, 0 for any other; Result4 is
  its low word, 1 for zero, 2 for 0xFFFF.  A CLICK's NaN reads 0xFFFF:0xFFFF, and so
  does pyrung's, whichever NaN it holds.  The word is compared after an XOR with 0x0080, each bit kept.  The checklist's "DF holding Inf or NaN over Modbus" reads these
  registers; a client (ClickNick, the sim) shows what these bits decode to.
- ``underflow``: pyrung keeps a Real at 64 bits, so 1.0E-20 * 1.0E-20 and 1.0E-30 *
  1.0E-30 are not 0 in a compare (Result1, Result2 = 0).  A float32 CLICK reads 1E-40
  as a denormal (Result1 = 0) or flushes it (Result1 = 1), and 1E-60 is 0 either way
  (Result2 = 1).  Result3 is SC43 after the second, off in pyrung; the raw low word
  of the first (Result4): 1 when nonzero (a denormal, 0x16C2), 0 when flushed.
- ``float32_equality``: DF 0.1 + DF 0.2 compared with 0.3 (Result1).  pyrung reads 0 at
  64 bits; a float32 store reads 1.  Result2 is DD 16777217 compared with DF
  16777216.0: 0 in pyrung, 1 where a Dint is compared as a float32.  Controls: DF 0.1
  == 0.1 (Result3 = 1), DF 0.5 + DF 0.25 == 0.75 (Result4 = 1), exact in both widths.

The cases own DF20..DF66, DH470..DH491, DD850 and TXT546..TXT557.
"""

from __future__ import annotations

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import (
    Rung,
    calc,
    copy,
    fill,
    system,
    to_ascii,
    to_text,
    unpack_to_words,
)
from tests.conformance.memory import dd, df, dh, txt
from tests.conformance.probe_kit import class_of, finish, flag_bit, once

SC43 = system.fault.out_of_range


def _raw(slot, source, first):
    with Rung(*once(slot)):
        unpack_to_words(source, dh.select(first, first + 1))


def prove_minus_zero_math(slot):
    with Rung(*once(slot)):
        copy(0.0, df[20])
        calc(df[20] * -1.0, df[21])
        copy(df[21], df[22])
    _raw(slot, df[21], 470)
    _raw(slot, df[22], 472)
    class_of(slot, dh[471], [0x8000, 0], slot.Result1)
    class_of(slot, dh[470], [0], slot.ErrorCode)
    with Rung(*once(slot), df[21] == 0):
        copy(1, slot.Result2)
    with Rung(*once(slot), df[21] < 0):
        copy(1, slot.Result3)
    class_of(slot, dh[473], [0x8000, 0], slot.Result4)
    finish(slot)


def init_minus_zero_math(slot):
    fill(0.0, df.select(20, 22))
    fill(0, dh.select(470, 473))


def prove_minus_zero_shapes(slot):
    with Rung(*once(slot)):
        copy(0.0, df[30])
        calc(-1.0 * df[30], df[31])
        calc(df[30] / -1.0, df[32])
        calc(df[31] - df[31], df[33])
        calc(abs(df[31]), df[34])
        copy("Z", txt[546])
        copy(df[31], txt[546], convert=to_text())
    _raw(slot, df[31], 474)
    _raw(slot, df[32], 476)
    _raw(slot, df[33], 478)
    _raw(slot, df[34], 480)
    class_of(slot, dh[475], [0x8000, 0], slot.Result1)
    class_of(slot, dh[477], [0x8000, 0], slot.Result2)
    class_of(slot, dh[479], [0x8000, 0], slot.Result3)
    class_of(slot, dh[481], [0x8000, 0], slot.Result4)
    with Rung(*once(slot)):
        copy(txt[546], slot.ErrorCode, convert=to_ascii)
    finish(slot)


def init_minus_zero_shapes(slot):
    fill(0.0, df.select(30, 34))
    fill(0, dh.select(474, 481))
    fill("\x00", txt.select(546, 557))


def prove_non_finite_raw_bits(slot):
    with Rung(*once(slot)):
        copy(3.0e38, df[40])
        calc(df[40] * 10, df[41])
        calc(df[40] * -10, df[42])
        calc(df[41] - df[41], df[43])
    _raw(slot, df[41], 482)
    _raw(slot, df[42], 484)
    _raw(slot, df[43], 486)
    class_of(slot, dh[483], [0x7F80], slot.Result1)
    class_of(slot, dh[485], [0xFF80], slot.Result2)
    # By XOR 0x0080: a constant that reads C0 stops the workspace round trip (DH490).
    with Rung(*once(slot)):
        copy(0x80, dh[491])
        calc(dh[487] ^ dh[491], dh[490])
    class_of(slot, dh[490], [0x7F40, 0xFF40, 0xFF7F], slot.Result3)
    class_of(slot, dh[486], [0, 0xFFFF], slot.Result4)
    with Rung(*once(slot), dh[482] == 0, dh[484] == 0):
        copy(1, slot.ErrorCode)
    finish(slot)


def init_non_finite_raw_bits(slot):
    fill(0.0, df.select(40, 43))
    fill(0, dh.select(482, 487))
    fill(0, dh.select(490, 491))


def prove_underflow(slot):
    with Rung(*once(slot)):
        copy(1.0e-20, df[50])
        copy(1.0e-30, df[51])
        calc(df[50] * df[50], df[52])
    _raw(slot, df[52], 488)
    with Rung(*once(slot)):
        calc(df[51] * df[51], df[53])
    flag_bit(slot, SC43, slot.Result3, 1)
    with Rung(*once(slot), df[52] == 0):
        copy(1, slot.Result1)
    with Rung(*once(slot), df[53] == 0):
        copy(1, slot.Result2)
    with Rung(*once(slot), dh[488] != 0):
        copy(1, slot.Result4)
    finish(slot)


def init_underflow(slot):
    fill(0.0, df.select(50, 53))
    fill(0, dh.select(488, 489))


def prove_float32_equality(slot):
    with Rung(*once(slot)):
        copy(0.1, df[60])
        copy(0.2, df[61])
        calc(df[60] + df[61], df[62])
        copy(16777217, dd[850])
        copy(16777216.0, df[63])
        copy(0.5, df[64])
        copy(0.25, df[65])
        calc(df[64] + df[65], df[66])
    with Rung(*once(slot), df[62] == 0.3):
        copy(1, slot.Result1)
    with Rung(*once(slot), dd[850] == df[63]):
        copy(1, slot.Result2)
    with Rung(*once(slot), df[60] == 0.1):
        copy(1, slot.Result3)
    with Rung(*once(slot), df[66] == 0.75):
        copy(1, slot.Result4)
    finish(slot)


def init_float32_equality(slot):
    fill(0.0, df.select(60, 66))
    copy(0, dd[850])


group = Group(
    "real_bits",
    [
        case(
            "DF 0.0 * -1.0 stores -0.0, raw high word 0x8000 (Result1 = 1) and low word 0 "
            "(ErrorCode = 1); it equals 0 (Result2 = 1), is not below it (Result3 = 0), and a "
            "Copy keeps its bits (Result4 = 1)",
            ladder=prove_minus_zero_math,
            init=init_minus_zero_math,
            expect={"Result1": 1, "Result2": 1, "Result3": 0, "Result4": 1, "ErrorCode": 1},
            probe="does a Real Math store -0.0 with its sign, and does a Copy keep it",
        ),
        case(
            "-1.0 * DF and DF / -1.0 keep the sign of zero (Result1, Result2 = 1), DF - DF and "
            "ABS ( -0.0 ) are +0.0 (Result3, Result4 = 2), and -0.0 as text starts with a minus "
            "(ErrorCode = 45)",
            ladder=prove_minus_zero_shapes,
            init=init_minus_zero_shapes,
            expect={"Result1": 1, "Result2": 1, "Result3": 2, "Result4": 2, "ErrorCode": 45},
            probe="which shapes reach -0.0; what its text reads",
        ),
        case(
            "The raw bits of the non-finite Reals: +Inf high word 0x7F80 (Result1 = 1), -Inf "
            "0xFF80 (Result2 = 1), both low words 0 (ErrorCode = 1); +Inf - +Inf is a NaN, "
            "0xFFFF (Result3 = 3) with low word 0xFFFF (Result4 = 2)",
            ladder=prove_non_finite_raw_bits,
            init=init_non_finite_raw_bits,
            expect={"Result1": 1, "Result2": 1, "Result3": 3, "Result4": 2, "ErrorCode": 1},
            probe="what a DF holding Inf or NaN reads over Modbus, which NaN",
        ),
        case(
            "A Real product past float32's small end is not 0 in pyrung: 1.0E-20 ^ 2 and "
            "1.0E-30 ^ 2 compare unequal to 0 (Result1, Result2 = 0), no SC43 (Result3 = 0), "
            "and the first's raw low word is nonzero (Result4 = 1)",
            ladder=prove_underflow,
            init=init_underflow,
            expect={"Result1": 0, "Result2": 0, "Result3": 0, "Result4": 1},
            probe="denormal or flushed; is underflow SC43",
        ),
        case(
            "A Real compares at pyrung's 64 bits: DF 0.1 + DF 0.2 does not equal 0.3 (Result1 "
            "= 0), and DD 16777217 does not equal DF 16777216.0 (Result2 = 0); DF 0.1 == 0.1 "
            "and 0.5 + 0.25 == 0.75 hold in any width (Result3, Result4 = 1)",
            ladder=prove_float32_equality,
            init=init_float32_equality,
            expect={"Result1": 0, "Result2": 0, "Result3": 1, "Result4": 1},
            probe="a compare at float32 where pyrung compares at 64 bits; a Dint against a Real",
        ),
    ],
)


def test_real_bits():
    assert_all_passed(run(group))
