"""Shift registers: the ``shift`` group (subroutine ``Conf_Shift``).

A shift register is a run of C bits, and no slot field is a C bit, so each case
packs the register into a Result with ``pack_bits`` once its window is over (the
bit at the lowest address weighs 1).  The cases own C1001..C1200
(``memory.OWNED``), twenty bits per case.  Each register's reset input is
``Cmd == 0`` (or that, or the case's own reset scans), so the reset clears it.

Each case counts its own scans (``scans.py``): clock bits are driven from the
scan number, and the data bit is the rung's own condition on the scan number.

The expectations are pyrung's answers, unconfirmed on a CLICK.  Where a CLICK
could differ, and what it would read:

``forward_range_shifts_up`` and ``reversed_range_shifts_down``: data enters at
the first address of the range and leaves at the last.  Clocked on scans 3, 5, 7
with data 1, 0, 1, a forward range holds bits 1 and 3 (1 + 4 = 5) and a
reversed range bits 8 and 6 (128 + 32 = 160).  A CLICK that ignores range
order, or reads it the other way, swaps the two (160 and 5).  The reversed
range exports as ``shift(C1018..C1011)``, CLICK's ending address below its
starting one.

``clock_is_a_rising_edge``: a clock high for scans 3..5 and 7..8 shifts twice,
not five times (Result1 = 3).  A level-sensitive clock reads 31.

``data_is_the_rung_at_the_edge``: the shifted-in bit is the rung's power on the
scan of the edge, not the scan before or after.  The data is on at the first
edge but off the scan after, and off at the second but on the scan before and
after.  A register that took the previous scan's power reads Result1 = 3, one
that took the next scan's reads 1.

``bit_leaves_the_end``: a bit that reaches the last address is dropped, not fed
back to the first.  A circular register reads 1, not 0, after four shifts of a
three-bit range.

``reset_beats_clock``: a clock edge on a scan whose reset is true leaves the
register clear (Result1 = 0).  A CLICK where the clock wins over the reset
reads 1 (the data bit shifted in after the clear).

``reset_swallows_the_edge``: an edge that arrives while reset is true is spent;
the clock still high when the reset ends is not a new edge.  A CLICK that
holds the edge back until the reset drops shifts then and reads 1.
"""

from __future__ import annotations

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import Or, Rung, out, pack_bits, shift
from tests.conformance.memory import c
from tests.conformance.scans import WINDOW, count_scans, done, scans_for

SCANS = scans_for()


def clock_on(slot, bit, *scans):
    """*bit* is on, driven by the scan number, on exactly the given scans."""
    with Rung(slot.Cmd != 0, Or(*[slot.Scratch == n for n in scans])):
        out(bit)


def pack_at_end(slot, first, last, dest):
    """*dest* = the register (lowest address weighs 1), once the window is over."""
    with Rung(slot.Cmd != 0, slot.Scratch > WINDOW):
        pack_bits(c.select(first, last), dest)


def prove_forward_range_shifts_up(slot):
    # Clocks on scans 3, 5, 7; data on, off, on (off on scan 5 only).
    count_scans(slot)
    clock_on(slot, c[1009], 3, 5, 7)
    with Rung(slot.Cmd != 0, slot.Scratch != 5):
        shift(c.select(1001, 1008)).clock(c[1009]).reset(slot.Cmd == 0)
    pack_at_end(slot, 1001, 1008, slot.Result1)
    done(slot)


def prove_reversed_range_shifts_down(slot):
    # The same clocks and data, on a reversed range.
    count_scans(slot)
    clock_on(slot, c[1019], 3, 5, 7)
    with Rung(slot.Cmd != 0, slot.Scratch != 5):
        shift(c.select(1011, 1018).reverse()).clock(c[1019]).reset(slot.Cmd == 0)
    pack_at_end(slot, 1011, 1018, slot.Result1)
    done(slot)


def prove_clock_is_a_rising_edge(slot):
    # The clock is high on scans 3..5 and 7..8, data always on.
    count_scans(slot)
    clock_on(slot, c[1029], 3, 4, 5, 7, 8)
    with Rung(slot.Cmd != 0):
        shift(c.select(1021, 1028)).clock(c[1029]).reset(slot.Cmd == 0)
    pack_at_end(slot, 1021, 1028, slot.Result1)
    done(slot)


def prove_data_is_the_rung_at_the_edge(slot):
    # Clocks on scans 3 and 6.  The data rung is off on scans 4 and 6 only.
    count_scans(slot)
    clock_on(slot, c[1049], 3, 6)
    with Rung(slot.Cmd != 0, slot.Scratch != 4, slot.Scratch != 6):
        shift(c.select(1041, 1048)).clock(c[1049]).reset(slot.Cmd == 0)
    pack_at_end(slot, 1041, 1048, slot.Result1)
    done(slot)


def prove_bit_leaves_the_end(slot):
    # Three bits, clocks on scans 2, 4, 6, 8, data on at scan 2 only.  The bit is
    # at the last address after scan 6 (Result2, read on scan 7) and gone after
    # scan 8.
    count_scans(slot)
    clock_on(slot, c[1069], 2, 4, 6, 8)
    with Rung(slot.Cmd != 0, slot.Scratch == 2):
        shift(c.select(1061, 1063)).clock(c[1069]).reset(slot.Cmd == 0)
    with Rung(slot.Cmd != 0, slot.Scratch == 7):
        pack_bits(c.select(1061, 1063), slot.Result2)
    pack_at_end(slot, 1061, 1063, slot.Result1)
    done(slot)


def prove_reset_beats_clock(slot):
    # A bit is shifted in on scan 2 (clock high on 2 only, then low), so the
    # reset has something to clear.  The clock rises again on scan 5, the scan
    # reset is true, and stays high through scan 7.
    count_scans(slot)
    clock_on(slot, c[1089], 2, 5, 6, 7)
    with Rung(slot.Cmd != 0):
        shift(c.select(1081, 1088)).clock(c[1089]).reset(Or(slot.Cmd == 0, slot.Scratch == 5))
    pack_at_end(slot, 1081, 1088, slot.Result1)
    done(slot)


def prove_reset_swallows_the_edge(slot):
    # The clock is low on scans 1..4, rises on scan 5 and stays high through
    # scan 7.  Reset is true on scans 5 and 6, so it has ended on scan 7.
    count_scans(slot)
    clock_on(slot, c[1109], 5, 6, 7)
    with Rung(slot.Cmd != 0):
        shift(c.select(1101, 1108)).clock(c[1109]).reset(
            Or(slot.Cmd == 0, slot.Scratch == 5, slot.Scratch == 6)
        )
    pack_at_end(slot, 1101, 1108, slot.Result1)
    done(slot)


group = Group(
    "shift",
    [
        case(
            "A shift register's direction is set by its range order: clocked on "
            "scans 3, 5 and 7 with data on, off, on, a forward range 1..8 holds "
            "bits 1 and 3 (Result1 = 5)",
            ladder=prove_forward_range_shifts_up,
            id="forward_range_shifts_up",
            expect={"Result1": 5},
            scans=SCANS,
        ),
        case(
            "A reversed range shifts the other way: the same clocks and data on a "
            "range 8..1 hold bits 8 and 6 (Result1 = 160)",
            ladder=prove_reversed_range_shifts_down,
            id="reversed_range_shifts_down",
            expect={"Result1": 160},
            scans=SCANS,
        ),
        case(
            "A shift register shifts once per rising edge of its clock, not once "
            "per scan the clock is on: a clock high for scans 3..5 and 7..8 with "
            "data always on shifts twice (Result1 = 3)",
            ladder=prove_clock_is_a_rising_edge,
            id="clock_is_a_rising_edge",
            expect={"Result1": 3},
            scans=SCANS,
        ),
        case(
            "The bit a shift register takes in is the rung's power on the scan of "
            "the clock edge: with edges on scans 3 and 6, data on at 3 but off at "
            "4, and off at 6 but on at 5 and 7, the register holds only bit 2 "
            "(Result1 = 2)",
            ladder=prove_data_is_the_rung_at_the_edge,
            id="data_is_the_rung_at_the_edge",
            expect={"Result1": 2},
            scans=SCANS,
        ),
        case(
            "A bit that reaches the end of a shift register is dropped, not fed "
            "back: one data bit in a three-bit register is at the last bit after "
            "three shifts (Result2 = 4) and gone after the fourth (Result1 = 0)",
            ladder=prove_bit_leaves_the_end,
            id="bit_leaves_the_end",
            expect={"Result1": 0, "Result2": 4},
            scans=SCANS,
        ),
        case(
            "Reset takes precedence over the clock: a clock edge on a scan whose "
            "reset is true leaves the register clear (Result1 = 0)",
            ladder=prove_reset_beats_clock,
            id="reset_beats_clock",
            expect={"Result1": 0},
            scans=SCANS,
        ),
        case(
            "A clock edge that arrives while reset is true is spent: a clock that "
            "rose with the reset and is still high when the reset ends does not "
            "shift then (Result1 = 0)",
            ladder=prove_reset_swallows_the_edge,
            id="reset_swallows_the_edge",
            expect={"Result1": 0},
            scans=SCANS,
        ),
    ],
)


def test_shift():
    assert_all_passed(run(group))
