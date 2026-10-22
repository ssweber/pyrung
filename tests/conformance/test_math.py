"""Range aggregates: the ``math`` group (subroutine ``Conf_Math``).

CLICK's ``AVG``/``MDN``/``MAX``/``MIN`` take a range of consecutive registers,
and a slot's fields are not one range, so each case stages its sources in raw
registers it owns:

- DS4401..DS4500, the top of DS (``SOURCE_DS``). No group block may reach it;
  ``test_no_group_block_reaches_the_source_area`` checks the manifest.
- DD, DF and DH, which no other group uses.

Each case stages its sources in its first rung (gated on ``Cmd``/``Fired`` like
every case), computes in the next rung, and writes what it saw into its slot.
A Real result is read back through an Int slot scaled by 10; a DD or DH result
that does not fit an Int slot is read back as a compare (Result = 1 when equal).

The expectations are pyrung's answers, unconfirmed on a CLICK. They are the
open probes M1 (AVG/MDN of an integer range: truncate or round), M2 (MDN of an
even count: mean of the middle two, or one of them) and M4 (MAX/MIN on DH:
signed or unsigned).
"""

from __future__ import annotations

from pathlib import Path

from pyrung.click import ClickBlocks
from pyrung.conformance import Group, assert_all_passed, case, load_manifest, run
from pyrung.core import Rung, calc, copy

_blocks = ClickBlocks()
ds, dd, df, dh = _blocks.ds, _blocks.dd, _blocks.df, _blocks.dh

#: Raw DS registers the math cases stage their source ranges in.
SOURCE_DS = range(4401, 4501)


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
        copy(4, ds[4431])
        copy(1, ds[4432])
        copy(3, ds[4433])
        copy(10, ds[4434])
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        calc(ds.select(4431, 4434).median(), slot.Result1)
        calc(ds.select(4431, 4434).median() * 10, slot.Result2)
        calc(ds.select(4431, 4434).median(), df[2])
        calc(df[2] * 10, slot.Result3)
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


group = Group(
    "math",
    [
        case(
            "AVG and MDN of the DS range {1, 2} truncate 1.5 to 1 in an Int; AVG into a Real "
            "keeps 1.5 (read back x10 as 15), and AVG * 10 into an Int is 15",
            ladder=prove_avg_mdn_positive,
            expect={"Result1": 1, "Result2": 1, "Result3": 15, "Result4": 15},
        ),
        case(
            "AVG and MDN of the DS range {-1, -2} truncate -1.5 toward zero to -1 in an Int",
            ladder=prove_avg_mdn_negative,
            expect={"Result1": -1, "Result2": -1},
        ),
        case(
            "AVG of the DS range {1, 1, 2} truncates 1.33 to 1 in an Int",
            ladder=prove_avg_odd_count,
            expect={"Result1": 1},
        ),
        case(
            "AVG of the DD range {2147483647, 2147483647} into a Dint is 2147483647 (the sum "
            "does not overflow); Result1 = 1 when it is",
            ladder=prove_avg_dint_no_overflow,
            expect={"Result1": 1},
        ),
        case(
            "MDN of the DS range {4, 1, 3, 10} (even count) is the mean of the middle two, "
            "3.5: 3 in an Int, 35 as MDN * 10 into an Int, and 3.5 into a Real (read back x10)",
            ladder=prove_mdn_even_count,
            expect={"Result1": 3, "Result2": 35, "Result3": 35},
        ),
        case(
            "MAX and MIN of the DH range {0x0001, 0xFFFF} (hex mode) treat WORD as unsigned: "
            "MAX is 0xFFFF (Result1 = 1) and MIN is 0x0001 (Result2 = 1)",
            ladder=prove_max_min_word_unsigned,
            expect={"Result1": 1, "Result2": 1},
        ),
    ],
)


def test_math():
    assert_all_passed(run(group))


def test_no_group_block_reaches_the_source_area():
    manifest = load_manifest(Path(__file__).with_name("manifest.toml"))
    for layout in manifest.groups.values():
        assert layout.last_register < SOURCE_DS.start, layout
