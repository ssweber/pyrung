"""Numerical handling: the ``numeric`` group (subroutine ``Conf_Numeric``).

Wrap and clamp at each integer width, what Math does with a Real, division by
zero, calc mode (decimal or hex), and how wide Math's intermediates are.  The
cases stage operands in raw registers the group owns (``memory.OWNED``):
DD101..DD200, DF101..DF200 and DH201..DH300, ten per case.  Each case's
``init`` fills them back to zero.

Every case fires once (``Cmd != 0, Fired == 0``): a rung stages its operands,
the next computes, and later rungs read the answer back into the slot.  A
value that does not fit an Int slot is read back exactly, by a division that
lands on a whole number or by a compare (Result = 1 when equal).  A result a
model could leave unwritten is preset first, so "the instruction skipped the
write" does not read as 0.

A fault flag is read by a contact on the rung after the faulting one, on the
same scan.  A flag that is on shows only that some instruction on that scan
set it, so each flag is read by the first case to fault on its scan:
``int_calc_wraps`` is the group's first case and sets SC43; ``div_by_zero``
is the first to set SC40 on the firing scan; ``real_div_by_zero`` and
``real_overflow_stays_finite`` count their own scans (``scans.py``) and fault
on their third, after the others have fired (one sets SC40, the other SC43).  That holds in-process, where every case fires on the same scan; over
the wire the cases fire when their ``Cmd`` lands, and another group's fault
on the same scan would also read as on.

The expectations are pyrung's answers, unconfirmed on a CLICK.  Where a CLICK
could plausibly differ, these cases tell the models apart:

- ``int_calc_wraps``, ``dint_calc_wraps``, ``word_calc_wraps``: Math wraps at
  each width.  Saturating Math reads Result1/Result2 = 32767/32766 (Int),
  32767/0 (Dint), and something other than 16 (0xFFFF copied into an Int)/0
  (Word); Math that skips an overflowing write reads 0/-1 (Int).  Math that
  wraps without the out-of-range flag reads Result3 = 0 (Int).
- ``copy_clamps_calc_wraps``: Copy clamps where Math wraps.  A Copy that
  wraps (or keeps the low word) reads -25536 and 25536 for Result1/Result3.
- ``real_to_int_truncates``: Real to Int truncates toward zero.  Rounding
  reads 3/-3, floor reads 2/-3.
- ``int_division_truncates``: integer division truncates toward zero.  Floor
  division reads -4 for -7 / 2; rounding reads 4/-4.
- ``word_math_unsigned``, ``hex_literal_is_decimal``: a WORD-only Math runs
  in hex mode, unsigned, and pyrung reads a constant in it as a decimal
  number.  Signed WORD reads Result1 = 0 (-1 / 2); 16-bit hex intermediates
  read Result2 = 0 (0x8000 * 2 wraps to 0).  CLICK reads digits in hex-mode
  Math as hex, so the exporter writes pyrung's ``10`` as ``Ah``; an export
  that wrote a bare ``10`` would read Result1 = 17 (10h + 1).
- ``intermediate_fits_32_bits``: an Int-only Math whose product needs 17 bits.
  16-bit intermediates read 244.
- ``intermediate_exceeds_32_bits``: 100000 * 100000 / 1000000 into an Int.
  pyrung's Python integers read 10000; 32-bit wrapping intermediates read 1410.
  A float intermediate reads 10000 too (1E+10 is exact even in a float32).
- ``real_is_64_bit_in_pyrung``: 16777216 + 1 in a Real.  pyrung keeps a REAL
  as a Python float (64-bit) and reads 1 twice; a float32 Real (CLICK's)
  reads 0 twice.  This one is expected to fail on a CLICK: pyrung keeps
  64-bit precision on purpose (a documented deviation).
- ``real_overflow_stays_finite``: 3.0E+38 * 10 and * -10 in a Real.  pyrung
  keeps every REAL inside CLICK's range: the result saturates at
  +/-3.4028235E+38 with SC43 on, reading 1/1/1/1.  A Real past the range
  (64-bit, unbounded) reads Result4 = 10 and Result1..3 = 0; one that
  overflows to +/-Inf reads Result1/Result2 = 0, and Result4 is whatever its
  Inf / 3.0E+38 -> Int gives; one that saturates without the flag reads
  Result3 = 0.
- ``div_by_zero``, ``real_div_by_zero``: division by zero writes 0 and sets
  the division-error flag (SC40).  A Math that skips the write reads the
  preset (99, or Result1..3 = 0 for the Real compares); an IEEE Real division
  stores +Inf, -Inf and NaN, none equal to 0, so Result1..3 read 0.

CLICK validation also reports ``CLK_CALC_MODE_MIXED`` for a Math that mixes
WORD and non-WORD operands; that is a static finding, not a ladder case.
"""

from __future__ import annotations

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import Rung, calc, copy, fill, system
from tests.conformance.memory import dd, df, dh
from tests.conformance.scans import count_scans, done, scans_for


def prove_int_calc_wraps(slot):
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        copy(32767, slot.Scratch)
        calc(slot.Scratch + 1, slot.Result1)
        calc(slot.Result1 - 1, slot.Result2)
    with Rung(slot.Cmd != 0, slot.Fired == 0, system.fault.out_of_range):
        copy(1, slot.Result3)
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        copy(1, slot.Fired)


def prove_dint_calc_wraps(slot):
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        copy(2147483647, dd[101])
        copy(-2147483648, dd[103])
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        calc(dd[101] + 1, dd[102])
        calc(dd[103] - 1, dd[104])
        calc(dd[102] / 65536, slot.Result1)
    with Rung(slot.Cmd != 0, slot.Fired == 0, dd[104] == 2147483647):
        copy(1, slot.Result2)
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        copy(1, slot.Fired)


def prove_word_calc_wraps(slot):
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        copy(0xFFF0, dh[201])
        copy(0x0020, dh[202])
        copy(0x0001, dh[204])
        copy(0x0002, dh[205])
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        calc(dh[201] + dh[202], dh[203])
        calc(dh[204] - dh[205], dh[206])
        copy(dh[203], slot.Result1)
    with Rung(slot.Cmd != 0, slot.Fired == 0, dh[206] == 0xFFFF):
        copy(1, slot.Result2)
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        copy(1, slot.Fired)


def prove_copy_clamps_calc_wraps(slot):
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        copy(40000, dd[111])
        copy(-40000, dd[112])
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        copy(dd[111], slot.Result1)
        calc(dd[111], slot.Result2)
        copy(dd[112], slot.Result3)
        calc(dd[112], slot.Result4)
        copy(1, slot.Fired)


def prove_copy_source_to_dest(slot):
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        copy(11, slot.Result1)
        copy(22, slot.Result2)
        copy(slot.Result1, slot.Result2)
        copy(1, slot.Fired)


def prove_real_to_int_truncates(slot):
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        copy(2.7, df[101])
        copy(-2.7, df[102])
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        copy(df[101], slot.Result1)
        copy(df[102], slot.Result2)
        calc(df[101], slot.Result3)
        calc(df[102], slot.Result4)
        copy(1, slot.Fired)


def prove_int_division_truncates(slot):
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        copy(7, slot.Scratch)
        copy(2, slot.Result4)
        calc(slot.Scratch / slot.Result4, slot.Result1)
        calc(0 - slot.Scratch, slot.Scratch)
        calc(slot.Scratch / slot.Result4, slot.Result2)
        copy(1, slot.Fired)


def prove_word_math_unsigned(slot):
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        copy(0xFFFF, dh[211])
        copy(0x0002, dh[212])
        copy(0x8000, dh[214])
        copy(0x0004, dh[215])
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        calc(dh[211] / dh[212], dh[213])
        calc(dh[214] * dh[212] / dh[215], dh[216])
        copy(dh[213], slot.Result1)
        copy(dh[216], slot.Result2)
        copy(1, slot.Fired)


def prove_hex_literal_is_decimal(slot):
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        copy(0x0001, dh[221])
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        calc(dh[221] + 10, dh[222])
        copy(dh[222], slot.Result1)
        copy(1, slot.Fired)


def prove_intermediate_fits_32_bits(slot):
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        copy(300, slot.Scratch)
        copy(100, slot.Result4)
        calc(slot.Scratch * slot.Scratch / slot.Result4, slot.Result1)
        copy(1, slot.Fired)


def prove_intermediate_exceeds_32_bits(slot):
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        copy(100000, dd[131])
        copy(1000000, dd[132])
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        calc(dd[131] * dd[131] / dd[132], slot.Result1)
        copy(1, slot.Fired)


def prove_real_is_64_bit_in_pyrung(slot):
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        copy(16777216.0, df[111])
        copy(16777217, dd[121])
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        calc(df[111] + 1, df[112])
        copy(dd[121], df[113])
        calc(df[112] - df[111], slot.Result1)
        calc(df[113] - df[111], slot.Result2)
        copy(1, slot.Fired)


#: ``real_overflow_stays_finite`` overflows on its own scan 3, after every
#: other case has fired, so the out-of-range flag it reads is its own (in-process).
REAL_OVERFLOW_WINDOW = 3


def prove_real_overflow_stays_finite(slot):
    # DF121 = 3.0E+38; on scan 3, 3.0E+38 * 10 goes into DF122 and * -10 into
    # DF123, both preset to 1.0 so a skipped write does not read as saturated.
    count_scans(slot, REAL_OVERFLOW_WINDOW)
    on_scan = slot.Scratch == REAL_OVERFLOW_WINDOW
    with Rung(slot.Cmd != 0, on_scan):
        copy(3.0e38, df[121])
        copy(1.0, df[122])
        copy(1.0, df[123])
    with Rung(slot.Cmd != 0, on_scan):
        calc(df[121] * 10, df[122])
        calc(df[121] * -10, df[123])
    with Rung(slot.Cmd != 0, on_scan, system.fault.out_of_range):
        copy(1, slot.Result3)
    # CLICK keeps 7 significant digits of a constant, so the REAL maximum
    # itself can't be written; only a saturated value reaches past 3.402823E+38.
    with Rung(slot.Cmd != 0, on_scan, df[122] >= 3.402823e38):
        copy(1, slot.Result1)
    with Rung(slot.Cmd != 0, on_scan, df[123] <= -3.402823e38):
        copy(1, slot.Result2)
    with Rung(slot.Cmd != 0, on_scan):
        calc(df[122] / df[121], slot.Result4)
    done(slot, REAL_OVERFLOW_WINDOW)


def prove_div_by_zero(slot):
    # Scratch stays 0: it is the divisor.
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        copy(99, slot.Result1)
        calc(100 / slot.Scratch, slot.Result1)
    with Rung(slot.Cmd != 0, slot.Fired == 0, system.fault.division_error):
        copy(1, slot.Result2)
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        copy(1, slot.Fired)


#: ``real_div_by_zero`` divides on its own scan 3, after every other case has
#: fired, so the division-error flag it reads is its own (in-process).
REAL_DIV_WINDOW = 3


def prove_real_div_by_zero(slot):
    # DF133 (the dividend of 0 / 0) and DF134 (the divisor) stay 0.0.
    count_scans(slot, REAL_DIV_WINDOW)
    on_scan = slot.Scratch == REAL_DIV_WINDOW
    with Rung(slot.Cmd != 0, on_scan):
        copy(2.5, df[131])
        copy(-2.5, df[132])
        copy(5.5, df[135])
        copy(5.5, df[136])
        copy(5.5, df[137])
    with Rung(slot.Cmd != 0, on_scan):
        calc(df[131] / df[134], df[135])
        calc(df[132] / df[134], df[136])
        calc(df[133] / df[134], df[137])
    with Rung(slot.Cmd != 0, on_scan, system.fault.division_error):
        copy(1, slot.Result4)
    with Rung(slot.Cmd != 0, on_scan, df[135] == 0):
        copy(1, slot.Result1)
    with Rung(slot.Cmd != 0, on_scan, df[136] == 0):
        copy(1, slot.Result2)
    with Rung(slot.Cmd != 0, on_scan, df[137] == 0):
        copy(1, slot.Result3)
    done(slot, REAL_DIV_WINDOW)


def init_dint_calc_wraps(slot):
    fill(0, dd.select(101, 104))


def init_word_calc_wraps(slot):
    fill(0, dh.select(201, 206))


def init_copy_clamps_calc_wraps(slot):
    fill(0, dd.select(111, 112))


def init_real_to_int_truncates(slot):
    fill(0, df.select(101, 102))


def init_word_math_unsigned(slot):
    fill(0, dh.select(211, 216))


def init_hex_literal_is_decimal(slot):
    fill(0, dh.select(221, 222))


def init_intermediate_exceeds_32_bits(slot):
    fill(0, dd.select(131, 132))


def init_real_is_64_bit_in_pyrung(slot):
    fill(0, df.select(111, 113))
    copy(0, dd[121])


def init_real_overflow_stays_finite(slot):
    fill(0, df.select(121, 123))


def init_real_div_by_zero(slot):
    fill(0, df.select(131, 137))


group = Group(
    "numeric",
    [
        case(
            "Math into an Int wraps at 32767: 32767 + 1 is -32768 (Result1), -32768 - 1 is "
            "32767 (Result2), and the out-of-range flag SC43 is on after it (Result3 = 1)",
            ladder=prove_int_calc_wraps,
            expect={"Result1": -32768, "Result2": 32767, "Result3": 1},
        ),
        case(
            "Math into a Dint wraps at 2^31: 2147483647 + 1 is -2147483648 (read back / 65536 "
            "as Result1 = -32768) and -2147483648 - 1 is 2147483647 (Result2 = 1)",
            ladder=prove_dint_calc_wraps,
            init=init_dint_calc_wraps,
            expect={"Result1": -32768, "Result2": 1},
        ),
        case(
            "Math on WORDs wraps at 65535: 0xFFF0 + 0x0020 is 0x0010 (Result1 = 16) and "
            "0x0001 - 0x0002 is 0xFFFF (Result2 = 1)",
            ladder=prove_word_calc_wraps,
            init=init_word_calc_wraps,
            expect={"Result1": 16, "Result2": 1},
        ),
        case(
            "Copy clamps where Math wraps: a Dint 40000 copies into an Int as 32767 (Result1) "
            "and calcs as -25536 (Result2); -40000 copies as -32768 (Result3) and calcs as "
            "25536 (Result4)",
            ladder=prove_copy_clamps_calc_wraps,
            init=init_copy_clamps_calc_wraps,
            expect={"Result1": 32767, "Result2": -25536, "Result3": -32768, "Result4": 25536},
        ),
        case(
            "Copy writes its first argument into its second: with Result1 = 11 and "
            "Result2 = 22, copy(Result1, Result2) leaves both 11",
            ladder=prove_copy_source_to_dest,
            expect={"Result1": 11, "Result2": 11},
        ),
        case(
            "A Real goes into an Int truncated toward zero: 2.7 and -2.7 copy as 2 (Result1) "
            "and -2 (Result2), and calc as 2 (Result3) and -2 (Result4)",
            ladder=prove_real_to_int_truncates,
            init=init_real_to_int_truncates,
            expect={"Result1": 2, "Result2": -2, "Result3": 2, "Result4": -2},
        ),
        case(
            "Integer division truncates toward zero: 7 / 2 is 3 (Result1) and -7 / 2 is -3 "
            "(Result2)",
            ladder=prove_int_division_truncates,
            expect={"Result1": 3, "Result2": -3},
        ),
        case(
            "A Math on WORDs only runs in hex mode, unsigned: 0xFFFF / 2 is 0x7FFF "
            "(Result1 = 32767), and 0x8000 * 2 / 4 is 0x4000 (Result2 = 16384), its "
            "intermediate past 16 bits",
            ladder=prove_word_math_unsigned,
            init=init_word_math_unsigned,
            expect={"Result1": 32767, "Result2": 16384},
        ),
        case(
            "A constant in a WORD-only Math is a decimal number: 0x0001 + 10 is 11 (Result1)",
            ladder=prove_hex_literal_is_decimal,
            init=init_hex_literal_is_decimal,
            expect={"Result1": 11},
        ),
        case(
            "An Int-only Math keeps an intermediate past 16 bits: 300 * 300 / 100 is 900 (Result1)",
            ladder=prove_intermediate_fits_32_bits,
            expect={"Result1": 900},
        ),
        case(
            "pyrung keeps a Math intermediate past 32 bits: 100000 * 100000 / 1000000 is "
            "10000 (Result1); 32-bit intermediates would wrap to 1410",
            ladder=prove_intermediate_exceeds_32_bits,
            init=init_intermediate_exceeds_32_bits,
            expect={"Result1": 10000},
        ),
        case(
            "pyrung keeps a Real at 64 bits: 16777216 + 1 in a Real is 16777217, 1 above "
            "16777216 (Result1 = 1), and so is a Dint 16777217 copied into a Real "
            "(Result2 = 1); a 32-bit Real reads 0 for both",
            ladder=prove_real_is_64_bit_in_pyrung,
            init=init_real_is_64_bit_in_pyrung,
            expect={"Result1": 1, "Result2": 1},
        ),
        case(
            "A Real that overflows stays finite: 3.0E+38 * 10 saturates at 3.4028235E+38 "
            "(Result1 = 1), 3.0E+38 * -10 at -3.4028235E+38 (Result2 = 1), the "
            "out-of-range flag SC43 is on (Result3 = 1), and the result reads 1 times "
            "3.0E+38, not 10 (Result4 = 1)",
            ladder=prove_real_overflow_stays_finite,
            init=init_real_overflow_stays_finite,
            expect={"Result1": 1, "Result2": 1, "Result3": 1, "Result4": 1},
            scans=scans_for(REAL_OVERFLOW_WINDOW),
        ),
        case(
            "Integer division by zero writes 0 over the destination's 99 (Result1) and sets "
            "the division-error flag SC40 (Result2 = 1)",
            ladder=prove_div_by_zero,
            expect={"Result1": 0, "Result2": 1},
        ),
        case(
            "Real division by zero writes 0, not +Inf, -Inf or NaN: 2.5 / 0, -2.5 / 0 and "
            "0 / 0 each leave 0 over 5.5 (Result1..3 = 1), and SC40 is on (Result4 = 1)",
            ladder=prove_real_div_by_zero,
            init=init_real_div_by_zero,
            expect={"Result1": 1, "Result2": 1, "Result3": 1, "Result4": 1},
            scans=scans_for(REAL_DIV_WINDOW),
        ),
    ],
)


def test_numeric():
    assert_all_passed(run(group))
