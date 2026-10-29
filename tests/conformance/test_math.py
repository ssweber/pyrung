"""Range aggregates: the ``math`` group (subroutine ``Conf_Math``).

CLICK's ``AVG``/``MDN``/``MAX``/``MIN`` take a range of consecutive registers,
and a slot's fields are not one range, so each case stages its sources in raw
registers it owns (``memory.OWNED``): DS4401..DS4500 at the top of the staging
area, DD1..DD100, DF1..DF100 and DH1..DH100.

Each case stages its sources in its first rung (gated on ``Cmd``/``Fired`` like
every case), computes in the next rung, and writes what it saw into its slot.
Its ``init`` fills its sources and results back to zero.
A Real result is read back through an Int slot scaled by 10; a DD or DH result
that does not fit an Int slot is read back as a compare (Result = 1 when equal).

The cases are the probes M1 (AVG/MDN of an integer range: truncate or round),
M2 (MDN of an even count: mean of the middle two, or one of them) and M4
(MAX/MIN on DH: signed or unsigned), plus M3 (RAND with lo > hi). RAND cases
check only the range, never a value.

MDN of an integer range is an integer on a CLICK (hardware round 1,
2026-10-29: the MDN of {4, 1, 3, 10} read back as a whole number, where pyrung
keeps 3.5), so ``mdn_even_count`` is ``pending="TEXT"``.  Round 1's data could not tell the
truncated mean of the middle two from the middle value nearer zero, so the
case now uses data that does:

- ``mdn_even_count``: {9, 2, 10, 1} (middle 2 and 9) and {-9, -2, -10, -1}
  (middle -9 and -2).  The mean of the middle two, truncated toward zero, as
  an integer (adopted): 5, 50, 50, -5.  The middle value nearer zero: 2, 20,
  20, -2.  The lower middle value: 2, 20, 20, -9.  The mean floored: 5, 50,
  50, -6.  pyrung's exact mean: 5, 55, 55, -5.
- ``mdn_real_range`` (probe): MDN of a DF range.  Exact in a Real (adopted):
  {9.0, 2.0, 10.0, 1.0} is 5.5 (read back x10 as 55, Result1) and {1.5, 2.5,
  0.5} is 1.5 (15, Result2).  An integer MDN, as for DS: 50 and 10; the
  middle value nearer zero: 20.
"""

from __future__ import annotations

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import Rung, calc, copy, fill, randint
from tests.conformance.memory import dd, df, dh, ds


def prove_avg_mdn_positive(slot):
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        copy(1, ds[4401])
        copy(2, ds[4402])
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        calc(ds.select(4401, 4402).mean(), slot.Result1)
        calc(ds.select(4401, 4402).median(), slot.Result2)
        calc(ds.select(4401, 4402).mean(), df[1])
        calc(df[1] * 10, slot.Result3)
        calc(ds.select(4401, 4402).mean() * 10, slot.Result4)
        copy(1, slot.Fired)


def prove_avg_mdn_negative(slot):
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        copy(-1, ds[4411])
        copy(-2, ds[4412])
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        calc(ds.select(4411, 4412).mean(), slot.Result1)
        calc(ds.select(4411, 4412).median(), slot.Result2)
        copy(1, slot.Fired)


def prove_avg_odd_count(slot):
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        copy(1, ds[4421])
        copy(1, ds[4422])
        copy(2, ds[4423])
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        calc(ds.select(4421, 4423).mean(), slot.Result1)
        copy(1, slot.Fired)


def prove_avg_dint_no_overflow(slot):
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        copy(2147483647, dd[1])
        copy(2147483647, dd[2])
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        calc(dd.select(1, 2).mean(), dd[3])
    with Rung(slot.Cmd != 0, slot.Fired == 0, dd[3] == 2147483647):
        copy(1, slot.Result1)
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        copy(1, slot.Fired)


def prove_mdn_even_count(slot):
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        copy(9, ds[4431])
        copy(2, ds[4432])
        copy(10, ds[4433])
        copy(1, ds[4434])
        copy(-9, ds[4435])
        copy(-2, ds[4436])
        copy(-10, ds[4437])
        copy(-1, ds[4438])
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        calc(ds.select(4431, 4434).median(), slot.Result1)
        calc(ds.select(4431, 4434).median() * 10, slot.Result2)
        calc(ds.select(4431, 4434).median(), df[2])
        calc(df[2] * 10, slot.Result3)
        calc(ds.select(4435, 4438).median(), slot.Result4)
        copy(1, slot.Fired)


def prove_mdn_real_range(slot):
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        copy(9.0, df[11])
        copy(2.0, df[12])
        copy(10.0, df[13])
        copy(1.0, df[14])
        copy(1.5, df[15])
        copy(2.5, df[16])
        copy(0.5, df[17])
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        calc(df.select(11, 14).median(), df[18])
        calc(df.select(15, 17).median(), df[19])
        calc(df[18] * 10, slot.Result1)
        calc(df[19] * 10, slot.Result2)
        copy(1, slot.Fired)


def prove_max_min_word_unsigned(slot):
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        copy(0x0001, dh[1])
        copy(0xFFFF, dh[2])
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        calc(dh.select(1, 2).max(), dh[3])
        calc(dh.select(1, 2).min(), dh[4])
    with Rung(slot.Cmd != 0, slot.Fired == 0, dh[3] == 0xFFFF):
        copy(1, slot.Result1)
    with Rung(slot.Cmd != 0, slot.Fired == 0, dh[4] == 0x0001):
        copy(1, slot.Result2)
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        copy(1, slot.Fired)


def prove_rand_range(slot):
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        calc(randint(1, 6), slot.Result1)
        calc(randint(10, 12), slot.Result2)
        calc(randint(6, 1), slot.Result3)
        copy(1, slot.Fired)


def init_avg_mdn_positive(slot):
    fill(0, ds.select(4401, 4402))
    copy(0, df[1])


def init_avg_mdn_negative(slot):
    fill(0, ds.select(4411, 4412))


def init_avg_odd_count(slot):
    fill(0, ds.select(4421, 4423))


def init_avg_dint_no_overflow(slot):
    fill(0, dd.select(1, 3))


def init_mdn_even_count(slot):
    fill(0, ds.select(4431, 4438))
    copy(0, df[2])


def init_mdn_real_range(slot):
    fill(0, df.select(11, 19))


def init_max_min_word_unsigned(slot):
    fill(0, dh.select(1, 4))


group = Group(
    "math",
    [
        case(
            "AVG and MDN of the DS range {1, 2} truncate 1.5 to 1 in an Int; AVG into a Real "
            "keeps 1.5 (read back x10 as 15), and AVG * 10 into an Int is 15",
            ladder=prove_avg_mdn_positive,
            init=init_avg_mdn_positive,
            expect={"Result1": 1, "Result2": 1, "Result3": 15, "Result4": 15},
        ),
        case(
            "AVG and MDN of the DS range {-1, -2} truncate -1.5 toward zero to -1 in an Int",
            ladder=prove_avg_mdn_negative,
            init=init_avg_mdn_negative,
            expect={"Result1": -1, "Result2": -1},
        ),
        case(
            "AVG of the DS range {1, 1, 2} truncates 1.33 to 1 in an Int",
            ladder=prove_avg_odd_count,
            init=init_avg_odd_count,
            expect={"Result1": 1},
        ),
        case(
            "AVG of the DD range {2147483647, 2147483647} into a Dint is 2147483647 (the sum "
            "does not overflow); Result1 = 1 when it is",
            ladder=prove_avg_dint_no_overflow,
            init=init_avg_dint_no_overflow,
            expect={"Result1": 1},
        ),
        case(
            "MDN of a DS range with an even count is the mean of the middle two, truncated "
            "toward zero to an integer: {9, 2, 10, 1} is 5 (Result1), so MDN * 10 into an "
            "Int is 50 (Result2) and MDN into a Real is 5.0 (read back x10, Result3 = 50); "
            "{-9, -2, -10, -1} is -5 (Result4)",
            ladder=prove_mdn_even_count,
            init=init_mdn_even_count,
            expect={"Result1": 5, "Result2": 50, "Result3": 50, "Result4": -5},
            pending="TEXT",
            probe="MDN of an even count: truncated mean or middle value nearer zero",
        ),
        case(
            "MAX and MIN of the DH range {0x0001, 0xFFFF} (hex mode) treat WORD as unsigned: "
            "MAX is 0xFFFF (Result1 = 1) and MIN is 0x0001 (Result2 = 1)",
            ladder=prove_max_min_word_unsigned,
            init=init_max_min_word_unsigned,
            expect={"Result1": 1, "Result2": 1},
        ),
        case(
            "RAND ( 1 , 6 ) draws 1..6 and RAND ( 10 , 12 ) draws 10..12, both ends included; "
            "RAND ( 6 , 1 ) with its bounds reversed draws 1..6",
            ladder=prove_rand_range,
            expect={"Result1": range(1, 7), "Result2": range(10, 13), "Result3": range(1, 7)},
        ),
        case(
            "MDN of a DF range keeps its fraction: {9.0, 2.0, 10.0, 1.0} is 5.5 (read back "
            "x10, Result1 = 55) and {1.5, 2.5, 0.5} is 1.5 (Result2 = 15)",
            ladder=prove_mdn_real_range,
            init=init_mdn_real_range,
            expect={"Result1": 55, "Result2": 15},
            probe="MDN of a DF range: exact or integer",
        ),
    ],
)


def test_math():
    assert_all_passed(run(group))
