"""Out, Set, Reset and Search over a range: what nothing else reads.

An expectation is pyrung's reading today; a CLICK that reads otherwise prints what it read.

``coil_range``:

- ``ranges_and_search``: nothing reads Out, Set or Reset over a range.  An Out over C1623..C1625, read
  on its true scan 3: first +100, last +10, the bit past it +1, so Result1 = 110; 100 for the first
  bit only.  After its rung falls on scan 4: Result2 = 0, each bit counted, where all follow it.  Set
  over 3 bits, Reset over the first 2: the bits read 100s, 10s, 1s, Result3 = 1; 111 where Reset
  takes one bit.  A search above 0x8000 over a DH holding 5, 0xFFFF, 7 (Result4): 73 for an
  unsigned compare, -1 for a signed one, where ``search`` read no Word table.  A one-shot search, its
  table moved after the first scan, its rung held: ErrorCode = 1 kept the first hit, 2 searched again.

The cases own what ``memory.OWNED`` lists under this group.
"""

from __future__ import annotations

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import (
    Rung,
    calc,
    copy,
    fill,
    latch,
    out,
    reset,
    search,
)
from tests.conformance.memory import c, dd, dh
from tests.conformance.scans import count_scans, done, scans_for


def prove_ranges_and_search(slot):
    count_scans(slot)
    with Rung(slot.Cmd != 0, slot.Scratch >= 2, slot.Scratch <= 3):
        out(c.select(1623, 1625))
    with Rung(slot.Cmd != 0, slot.Scratch == 3, c[1623]):
        calc(slot.Result1 + 100, slot.Result1)
    with Rung(slot.Cmd != 0, slot.Scratch == 3, c[1625]):
        calc(slot.Result1 + 10, slot.Result1)
    with Rung(slot.Cmd != 0, slot.Scratch == 3, c[1626]):
        calc(slot.Result1 + 1, slot.Result1)
    for bit in (1623, 1624, 1625):
        with Rung(slot.Cmd != 0, slot.Scratch == 4, c[bit]):
            calc(slot.Result2 + 1, slot.Result2)
    with Rung(slot.Cmd != 0, slot.Scratch == 1):
        latch(c.select(1627, 1629))
        copy(5, dh[72])
        copy(0xFFFF, dh[73])
        copy(7, dh[74])
        copy(9, dh[75])
    with Rung(slot.Cmd != 0, slot.Scratch == 2):
        reset(c.select(1627, 1628))
    with Rung(slot.Cmd != 0, slot.Scratch == 3, c[1627]):
        calc(slot.Result3 + 100, slot.Result3)
    with Rung(slot.Cmd != 0, slot.Scratch == 3, c[1628]):
        calc(slot.Result3 + 10, slot.Result3)
    with Rung(slot.Cmd != 0, slot.Scratch == 3, c[1629]):
        calc(slot.Result3 + 1, slot.Result3)
    with Rung(slot.Cmd != 0, slot.Scratch == 2):
        search(dh.select(72, 74) > 0x8000, result=slot.Result4, found=c[1630])
    # The table moves after the search's first scan, its rung still true.
    with Rung(slot.Cmd != 0, slot.Scratch >= 2):
        search(dh.select(75, 76) == 9, result=dd[77], found=c[1633], oneshot=True)
    with Rung(slot.Cmd != 0, slot.Scratch == 3):
        copy(0, dh[75])
        copy(9, dh[76])
    with Rung(slot.Cmd != 0, slot.Scratch == 6, dd[77] == 75):
        copy(1, slot.ErrorCode)
    with Rung(slot.Cmd != 0, slot.Scratch == 6, dd[77] == 76):
        copy(2, slot.ErrorCode)
    done(slot)


def init_ranges_and_search(slot):
    fill(False, c.select(1627, 1630))
    reset(c[1633])
    fill(0, dh.select(72, 76))
    copy(0, dd[77])


group = Group(
    "coil_range",
    [
        case(
            "Out, Set and Reset over a range take each bit: Result1 = 110, Result2 = 0, Result3 = "
            "1; a search over a Word table reads it unsigned (Result4 = 73); a one-shot search "
            "keeps its first hit (ErrorCode = 1)",
            ladder=prove_ranges_and_search,
            init=init_ranges_and_search,
            scans=scans_for(),
            expect={"Result1": 110, "Result2": 0, "Result3": 1, "Result4": 73, "ErrorCode": 1},
            probe="a coil over a range; a search over a Word table, and one-shot",
        ),
    ],
)


def test_coil_range():
    assert_all_passed(run(group))
