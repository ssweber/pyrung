"""Aggregates and RAND past what ``math``, ``numeric`` and ``store_probe`` read: ``agg_more``.

Part J (``suite.PARTS``).  An expectation is pyrung's reading today; a CLICK that reads
otherwise prints what it read.  ``numeric`` reads a Real's SUM and a DS AVG, ``math`` a
MEDIAN.  Unread: a SUM that wraps, a SUM of bits, and each aggregate's sign, NaN and Inf.

- ``sum_int_wrap``: DS {32767, 1} is 32768, into an Int -32768 wrapped (Result1); SC43 Result2
  bit 1.  DD {2147483647, 1} into a DD: 1 for -2147483648 (Result3), SC43 bit 2 of Result2.
  Into a DD the DS row is 32768 (Result4 = 1), 2 for -32768, where a DS sum wraps
  before it is stored.  DH {0xFFFF, 1} into a DH: 1 for 0 (ErrorCode); SC43 bit 4 of Result2.
- ``sum_bits``: the only aggregate over a bit range the exporter takes: SUM of 5 C bits, 3 on,
  is a count, 3 (Result1); into a DF (Result2, by 1); with no bit on, 0 (Result3).
  ``numeric`` reads no bit SUM.
- ``agg_signed``: MAX and MIN of DS {-5, 3}, signed, 3 and -5 (Result1, Result2).  DD {-100000,
  3}: 1 for 3, then 1 for -100000 (Result3 tens, units).  AVG of DD {-3, -4} (Result4): -3
  truncated, -4 floored or rounded.  AVG of DH {0xFFFF, 0xFFFF}: 1 for 0xFFFF, 2 for
  0x7FFF, where the sum is cut (ErrorCode).
- ``agg_nan_order``: NaN in a Real aggregate, by order.  ``max`` keeps the first it meets, so
  {NaN, 1.0} and {1.0, NaN} differ: Result1 is a class of MAX {NaN, 1.0}, 1 for 1.0, 2 for NaN;
  Result2 of MAX {1.0, NaN}; Result3 and Result4 the same for MIN.  A NaN is neither above
  nor below nor equal, so a class is 2 only when it equals itself not.
- ``agg_inf``: SUM {+Inf, -Inf} is NaN (Result1 = 2); AVG {+Inf, 1.0} is +Inf (Result2 = 1); AVG
  {3e38, 3e38}: 3e38 if it does not overflow on the way (Result3 = 1), +Inf (2) if a float32
  sum does.  SUM {3e38, 3e38}: +Inf (Result4 = 1), 2 where it stops at the Real top.  SC43
  (ErrorCode bit 1).
- ``rand_sites``: pyrung's RAND is one draw per site per scan.  Two sites over 8 scans, 1..30000:
  they differ on some scan (Result1 = equal scans, 0 for independent); a site changes
  from scan to scan (Result2 = changes of 7, above 2).  One formula with two (Result3 = scans
  they cancel to 0, 0).  A bound written above the Math on its own scan (Result4): 5 is the bound
  as the scan began, 9 is the bound live.  pyrung's engines differ here until a CLICK settles it:
  core and the kernel read 5, the generated P1AM code reads 9.  So Result4 is a reading, and the
  case records what it read until then; Result1..3 still assert.
pyrung refuses ``randint`` in a For body, so a draw per iteration is not asked here.

The cases own DD41..DD56, DS4318..DS4321, DF170..DF191, DH61..DH66, C522..C526 and C1586..C1590.
"""

from __future__ import annotations

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import Rung, calc, copy, fill, latch, reset, system
from pyrung.core.expression import randint
from tests.conformance.memory import c, dd, df, dh, ds
from tests.conformance.probe_kit import between, class_of, finish, flag_bit, once
from tests.conformance.scans import count_scans, done, scans_for

SC43 = system.fault.out_of_range


def prove_sum_int_wrap(slot):
    with Rung(*once(slot)):
        copy(32767, ds[4318])
        copy(1, ds[4319])
        calc(ds.select(4318, 4319).sum(), slot.Result1)
    flag_bit(slot, SC43, slot.Result2, 1)
    with Rung(*once(slot)):
        copy(2147483647, dd[41])
        copy(1, dd[42])
        calc(dd.select(41, 42).sum(), dd[43])
    flag_bit(slot, SC43, slot.Result2, 2)
    with Rung(*once(slot)):
        calc(ds.select(4318, 4319).sum(), dd[44])
        copy(0xFFFF, dh[61])
        copy(1, dh[62])
        calc(dh.select(61, 62).sum(), dh[63])
    flag_bit(slot, SC43, slot.Result2, 4)
    class_of(slot, dd[43], [-2147483648], slot.Result3)
    class_of(slot, dd[44], [32768, -32768], slot.Result4)
    class_of(slot, dh[63], [0], slot.ErrorCode)
    finish(slot)


def init_sum_int_wrap(slot):
    fill(0, ds.select(4318, 4319))
    fill(0, dd.select(41, 44))
    fill(0, dh.select(61, 63))


def prove_sum_bits(slot):
    with Rung(*once(slot)):
        latch(c[522])
        latch(c[524])
        latch(c[525])
        calc(c.select(522, 526).sum(), slot.Result1)
        calc(c.select(522, 526).sum(), df[170])
        calc(c.select(1586, 1590).sum(), slot.Result3)
    class_of(slot, df[170], [3.0], slot.Result2)
    finish(slot)


def init_sum_bits(slot):
    for n in (522, 524, 525):
        reset(c[n])
    copy(0.0, df[170])


def prove_agg_signed(slot):
    with Rung(*once(slot)):
        copy(-5, ds[4320])
        copy(3, ds[4321])
        calc(ds.select(4320, 4321).max(), slot.Result1)
        calc(ds.select(4320, 4321).min(), slot.Result2)
        copy(-100000, dd[45])
        copy(3, dd[46])
        calc(dd.select(45, 46).max(), dd[47])
        calc(dd.select(45, 46).min(), dd[48])
        copy(-3, dd[49])
        copy(-4, dd[50])
        calc(dd.select(49, 50).mean(), slot.Result4)
        copy(0xFFFF, dh[64])
        copy(0xFFFF, dh[65])
        calc(dh.select(64, 65).mean(), dh[66])
    with Rung(*once(slot), dd[47] == 3):
        calc(slot.Result3 + 10, slot.Result3)
    with Rung(*once(slot), dd[48] == -100000):
        calc(slot.Result3 + 1, slot.Result3)
    class_of(slot, dh[66], [0xFFFF, 0x7FFF], slot.ErrorCode)
    finish(slot)


def init_agg_signed(slot):
    fill(0, ds.select(4320, 4321))
    fill(0, dd.select(45, 50))
    fill(0, dh.select(64, 66))


def _nan_class(slot, value, into):
    with Rung(*once(slot), value == 1.0):
        copy(1, into)
    with Rung(*once(slot), value != value):
        copy(2, into)


def prove_agg_nan_order(slot):
    with Rung(*once(slot)):
        copy(3.0e38, df[171])
        calc(df[171] * 10, df[172])
        calc(df[172] - df[172], df[173])
        copy(1.0, df[174])
        copy(1.0, df[175])
        copy(df[173], df[176])
        calc(df.select(173, 174).max(), df[177])
        calc(df.select(175, 176).max(), df[178])
        calc(df.select(173, 174).min(), df[179])
        calc(df.select(175, 176).min(), df[180])
    _nan_class(slot, df[177], slot.Result1)
    _nan_class(slot, df[178], slot.Result2)
    _nan_class(slot, df[179], slot.Result3)
    _nan_class(slot, df[180], slot.Result4)
    finish(slot)


def init_agg_nan_order(slot):
    fill(0.0, df.select(171, 180))


def _inf_class(slot, value, into):
    with Rung(*once(slot), value > 3.4028235e38):
        copy(1, into)


def prove_agg_inf(slot):
    with Rung(*once(slot)):
        copy(3.0e38, df[181])
        calc(df[181] * 10, df[182])
        calc(df[181] * -10, df[183])
        calc(df.select(182, 183).sum(), df[184])
        copy(df[182], df[185])
        copy(1.0, df[186])
        calc(df.select(185, 186).mean(), df[187])
        copy(3.0e38, df[188])
        copy(3.0e38, df[189])
        calc(df.select(188, 189).mean(), df[190])
    with Rung(*once(slot), df[184] != df[184]):
        copy(2, slot.Result1)
    _inf_class(slot, df[187], slot.Result2)
    with Rung(*once(slot), df[190] == 3.0e38):
        copy(1, slot.Result3)
    with Rung(*once(slot), df[190] > 3.4028235e38):
        copy(2, slot.Result3)
    with Rung(*once(slot)):
        calc(df.select(188, 189).sum(), df[191])
    flag_bit(slot, SC43, slot.ErrorCode, 1)
    with Rung(*once(slot), df[191] > 3.4028235e38):
        copy(1, slot.Result4)
    with Rung(*once(slot), df[191] == 3.4028235e38):
        copy(2, slot.Result4)
    finish(slot)


def init_agg_inf(slot):
    fill(0.0, df.select(181, 191))


def prove_rand_sites(slot):
    count_scans(slot)
    with Rung(slot.Scratch == 1):
        copy(5, dd[51])
        copy(5, dd[52])
    with Rung(slot.Scratch <= 8, slot.Scratch != 0):
        calc(randint(1, 30000), dd[53])
        calc(randint(1, 30000), dd[54])
        calc(randint(1, 30000) - randint(1, 30000), dd[55])
    with Rung(slot.Scratch <= 8, slot.Scratch != 0, dd[53] == dd[54]):
        calc(slot.Result1 + 1, slot.Result1)
    with Rung(slot.Scratch >= 2, slot.Scratch <= 8, dd[53] != dd[56]):
        calc(slot.Result2 + 1, slot.Result2)
    with Rung(slot.Scratch <= 8, slot.Scratch != 0, dd[55] == 0):
        calc(slot.Result3 + 1, slot.Result3)
    with Rung():
        copy(dd[53], dd[56])
    with Rung(slot.Scratch == 4):
        copy(9, dd[51])
        copy(9, dd[52])
        calc(randint(dd[51], dd[52]), slot.Result4)
    done(slot)


def init_rand_sites(slot):
    fill(0, dd.select(51, 56))


group = Group(
    "agg_more",
    [
        case(
            "A SUM past the destination wraps: DS {32767, 1} is -32768 in an Int (Result1), DD "
            "{2147483647, 1} wraps (Result3 = 1), a DS row into a DD is 32768 (Result4 = 1), DH "
            "{0xFFFF, 1} is 0 (ErrorCode = 1); SC43 each (Result2 = 7)",
            ladder=prove_sum_int_wrap,
            init=init_sum_int_wrap,
            expect={"Result1": -32768, "Result2": 7, "Result3": 1, "Result4": 1, "ErrorCode": 1},
            probe="a SUM that wraps, its flag, and the width it adds at",
        ),
        case(
            "SUM of a C range counts: 3 of 5 on is 3 into an Int (Result1) and a DF (Result2 = 1), "
            "none on is 0 (Result3)",
            ladder=prove_sum_bits,
            init=init_sum_bits,
            expect={"Result1": 3, "Result2": 1, "Result3": 0},
            probe="SUM over a bit range",
        ),
        case(
            "MAX and MIN are signed: DS {-5, 3} is 3 and -5 (Result1, Result2), DD {-100000, 3} "
            "(Result3 = 11); AVG DD {-3, -4} truncates to -3 (Result4); AVG DH {0xFFFF, 0xFFFF} "
            "is 0xFFFF (ErrorCode = 1)",
            ladder=prove_agg_signed,
            init=init_agg_signed,
            expect={"Result1": 3, "Result2": -5, "Result3": 11, "Result4": -3, "ErrorCode": 1},
            probe="the sign of MAX and MIN, the rounding of AVG, Word AVG's width",
        ),
        case(
            "MAX and MIN over a NaN depend on its place: class 1 for 1.0, 2 for NaN: MAX {NaN, 1.0} "
            "(Result1), MAX {1.0, NaN} (Result2), MIN {NaN, 1.0} (Result3), MIN {1.0, NaN} (Result4)",
            ladder=prove_agg_nan_order,
            init=init_agg_nan_order,
            expect={"Result1": 2, "Result2": 1, "Result3": 2, "Result4": 1},
            probe="what an aggregate does with a NaN: which it returns, by order",
        ),
        case(
            "Infs in aggregates: SUM {+Inf, -Inf} is NaN (Result1 = 2), AVG {+Inf, 1.0} is +Inf "
            "(Result2 = 1), AVG {3e38, 3e38} is 3e38 (Result3 = 1); SUM {3e38, 3e38} (Result4, SC43 "
            "ErrorCode)",
            ladder=prove_agg_inf,
            init=init_agg_inf,
            expect={"Result1": 2, "Result2": 1, "Result3": 1, "Result4": 1, "ErrorCode": 1},
            probe="an aggregate's overflow on the way, and into a DF",
        ),
        case(
            "RAND is one draw per site and per scan: two sites differ on most scans (Result1 = "
            "equal scans), a site changes (Result2, changes of 7), a formula with two does not "
            "cancel (Result3); a bound written above the Math on its scan: 5 is the bound as the scan "
            "began, 9 live (Result4)",
            ladder=prove_rand_sites,
            init=init_rand_sites,
            scans=scans_for(),
            expect={
                "Result1": range(0, 2),
                "Result2": range(3, 8),
                "Result3": range(0, 2),
                "Result4": between(5, 9),
            },
            probe="RAND: sites independent; bounds read when",
        ),
    ],
)


def test_agg_more():
    assert_all_passed(run(group))
