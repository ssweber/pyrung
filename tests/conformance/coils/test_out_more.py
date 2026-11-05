"""The v4 Out family, past what ``out`` reads.

Every expectation is what a CLICK read (hardware round of 2026-11-05), save one field a scan
count may move (``zero_and_below`` Result2).  Clock windows hold for any scan up to 50 ms, as in
``out``.

``out_more``:

- ``toggle_and_timed``: ``out`` reads a toggle's first flip and a timed coil's held rung.  A toggle
  over two edges, scan 3 reads it on, scan 5 with the rung false, scan 7 past the second edge:
  Result1 = 110, by digit.  100 if the rung's fall turns it off, 111 if the second edge is
  lost.  A timed coil of 300 ms, its rung on 0-100 ms and again 150-250 ms: Result2 at 360-440 ms
  is 0 where the second edge does nothing, 1 where it restarts the time.  Its rung back after the
  time is up, at 500-700 ms: on (ErrorCode = 1), 0 where it needs more than an edge.  TD of a held
  timed coil past its 200 ms preset, read at 500-590 ms: Result3 = 1 stopped near the preset, 2
  counting on as a debounce's does, 3 cleared.  After its rung falls, at 700-900 ms: Result4 = 1
  cleared, 2 kept.  A CLICK reads 110, 0, 1, 2 and ErrorCode 1: the TD is kept.
- ``timed_maintained``: nothing has read a maintained coil.  Its rung gone at 100 ms into a 400 ms
  time: on at 200-350 ms (Result1 = 1), 0 where a maintained coil follows its rung throughout;
  off by 500 ms (Result2 = 0), 1 where it holds.  Its rung held past a 200 ms time: on at 400-590
  ms (Result3 = 1), 0 where it pulses as the plain one; rung gone at 600 ms, off by 700 ms
  (Result4 = 0), 1 where it stays.
- ``zero_and_below``: a preset of 0, scan-paced, the rung true on scans 3..5.  A debounce of 0/0:
  first on (Result1), first off past it (Result2); 3 and 6 are the rung's own scans, later ones
  mean a scan's lag.  A flasher of 0/0: scans on of 8 (Result3): a scan each way is 2, on throughout 3,
  once 1.  A timed coil of 0: scans on (Result4), 1 a one-scan pulse, 0 none.  A timed coil from a DS
  holding -5: scans on (ErrorCode), 1 as a preset of 0, 3 (the rung's) where -5 reads as unsigned,
  65531 ms.  A CLICK reads 3, 7, 0, 0, 0: no pulse from a preset of 0 or below, and -5 is signed.
  Its debounce turns off a scan after the rung falls, where pyrung's does on the rung's scan (6):
  a wait of 0 ms that a CLICK takes a scan longer over, the timer-count rule (pyrung finishes a
  timer on the exact count), so Result2 takes either.
- ``preset_mid_phase``: a preset in a DS moved mid-phase, read live or at the phase's start.  A
  flasher's on side from 1000 down to 100 ms at 300 ms: at 420-600 ms the coil is off (Result1 =
  0) where it reads live, on (1) where it took the preset at the start.  A debounce's on side
  from 200 up to 2000 ms at 150 ms: still off at 300-600 ms (Result2 = 0) live, on (1) if taken at
  the start.  A flasher's off TD through its second on phase: Result3 = 1 holds its last value, 2
  cleared.  A debounce's on TD, its rung gone at 150 ms: at 300-500 ms Result4 = 1 cleared, 2 kept.
  A CLICK reads 0, 0, 1, 2: the on TD is kept.  The reset writes 0 to the first flasher's off TD
  (TD240), which a false rung only freezes; the restart clears it too
  (``flasher_td/frozen_off_td_at_lowered_preset``), so the write is spare.

The cases own what ``memory.OWNED`` lists under this group.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import (
    Or,
    Rung,
    calc,
    copy,
    debounce,
    fill,
    flasher,
    out,
    reset,
    timed,
    toggle,
)
from tests.conformance.memory import ClickTimer, c, ds, t, td
from tests.conformance.probe_kit import end_at, in_window, ms_clock, once
from tests.conformance.scans import count_scans, done, scans_for


def prove_toggle_and_timed(slot):
    count_scans(slot)
    coil, rung = c[1702], c[1703]
    with Rung(slot.Cmd != 0, Or(slot.Scratch == 2, slot.Scratch == 3, slot.Scratch >= 6)):
        out(rung)
    with Rung(rung):
        toggle(coil)
    with Rung(slot.Cmd != 0, slot.Scratch == 3, coil):
        calc(slot.Result1 + 100, slot.Result1)
    with Rung(slot.Cmd != 0, slot.Scratch == 5, coil):
        calc(slot.Result1 + 10, slot.Result1)
    with Rung(slot.Cmd != 0, slot.Scratch == 7, coil):
        calc(slot.Result1 + 1, slot.Result1)
    clock = ms_clock(slot, 226)
    pulse_rung, pulse, held = c[1704], c[1705], c[1706]
    with Rung(slot.Cmd != 0, Or(clock < 100, clock >= 500)):
        out(pulse_rung)
    with Rung(slot.Cmd != 0, clock >= 150, clock < 250):
        out(c[1717])
    with Rung(Or(pulse_rung, c[1717]), clock < 700):
        timed(pulse, ClickTimer(227), 300)
    with Rung(*in_window(slot, clock, 360, 440), pulse):
        copy(1, slot.Result2)
    with Rung(*in_window(slot, clock, 500, 700), pulse):
        copy(1, slot.ErrorCode)
    with Rung(slot.Cmd != 0, clock < 600):
        timed(held, ClickTimer(228), 200)
    with Rung(*in_window(slot, clock, 500, 590), td[228] >= 200, td[228] < 300):
        copy(1, slot.Result3)
    with Rung(*in_window(slot, clock, 500, 590), td[228] >= 300):
        copy(2, slot.Result3)
    with Rung(*in_window(slot, clock, 500, 590), td[228] == 0):
        copy(3, slot.Result3)
    with Rung(*in_window(slot, clock, 700, 900), td[228] == 0):
        copy(1, slot.Result4)
    with Rung(*in_window(slot, clock, 700, 900), td[228] >= 200):
        copy(2, slot.Result4)
    end_at(slot, clock, 950)


def init_toggle_and_timed(slot):
    reset(c[1702])


def prove_timed_maintained(slot):
    clock = ms_clock(slot, 229)
    dropped, held = c[1707], c[1708]
    with Rung(slot.Cmd != 0, clock < 100):
        timed(dropped, ClickTimer(230), 400, maintained=True)
    with Rung(slot.Cmd != 0, clock < 600):
        timed(held, ClickTimer(231), 200, maintained=True)
    with Rung(*in_window(slot, clock, 200, 350), dropped):
        copy(1, slot.Result1)
    with Rung(*in_window(slot, clock, 500, 590), dropped):
        copy(1, slot.Result2)
    with Rung(*in_window(slot, clock, 400, 590), held):
        copy(1, slot.Result3)
    with Rung(*in_window(slot, clock, 700, 900), held):
        copy(1, slot.Result4)
    end_at(slot, clock, 950)


def prove_zero_and_below(slot):
    rung, deb, fla, pulse, neg = c[1709], c[1710], c[1712], c[1713], c[1714]
    window = (slot.Cmd != 0, slot.Scratch >= 1, slot.Scratch <= 8)
    count_scans(slot)
    with Rung(slot.Cmd != 0, slot.Scratch == 1):
        copy(-5, ds[3926])
    with Rung(slot.Cmd != 0, slot.Scratch >= 3, slot.Scratch <= 5):
        out(rung)
    with Rung(rung):
        debounce(deb, on=(ClickTimer(232), 0), off=(ClickTimer(233), 0))
    with Rung(rung):
        flasher(fla, on=(ClickTimer(234), 0), off=(ClickTimer(235), 0))
    with Rung(rung):
        timed(pulse, ClickTimer(236), 0)
    with Rung(rung):
        timed(neg, ClickTimer(237), ds[3926])
    with Rung(*window, deb, slot.Result1 == 0):
        copy(slot.Scratch, slot.Result1)
    with Rung(slot.Cmd != 0, slot.Scratch >= 3, slot.Scratch <= 8, ~deb, slot.Result2 == 0):
        copy(slot.Scratch, slot.Result2)
    with Rung(*window, fla):
        calc(slot.Result3 + 1, slot.Result3)
    with Rung(*window, pulse):
        calc(slot.Result4 + 1, slot.Result4)
    with Rung(*window, neg):
        calc(slot.ErrorCode + 1, slot.ErrorCode)
    done(slot)


def init_zero_and_below(slot):
    copy(0, ds[3926])


def prove_preset_mid_phase(slot):
    clock = ms_clock(slot, 238)
    fla, deb, fla2, deb2 = c[1715], c[1716], c[1718], c[1719]
    with Rung(slot.Cmd != 0, clock < 150):
        copy(1000, ds[3927])
        copy(200, ds[3928])
    with Rung(slot.Cmd != 0, clock >= 150):
        copy(2000, ds[3928])
    with Rung(slot.Cmd != 0, clock >= 300):
        copy(100, ds[3927])
    with Rung(slot.Cmd != 0):
        flasher(fla, on=(ClickTimer(239), ds[3927]), off=(ClickTimer(240), 1000))
    with Rung(slot.Cmd != 0):
        debounce(deb, on=(ClickTimer(241), ds[3928]), off=(ClickTimer(242), 1000))
    with Rung(slot.Cmd != 0):
        flasher(fla2, on=(ClickTimer(225), 100), off=(ClickTimer(113), 100))
    with Rung(slot.Cmd != 0, clock < 150):
        debounce(deb2, on=(ClickTimer(114), 2000), off=(ClickTimer(115), 2000))
    with Rung(*in_window(slot, clock, 420, 600), fla):
        copy(1, slot.Result1)
    with Rung(*in_window(slot, clock, 300, 600), deb):
        copy(1, slot.Result2)
    # The coil on at 250 ms or later is the second on phase: the first ended by 150.
    with Rung(*once(slot), clock >= 250, fla2, td[113] >= 100):
        copy(1, slot.Result3)
    with Rung(*once(slot), clock >= 250, fla2, td[113] == 0):
        copy(2, slot.Result3)
    with Rung(*in_window(slot, clock, 300, 500), td[114] == 0):
        copy(1, slot.Result4)
    with Rung(*in_window(slot, clock, 300, 500), td[114] > 0):
        copy(2, slot.Result4)
    end_at(slot, clock, 650)


def init_preset_mid_phase(slot):
    fill(0, ds.select(3927, 3928))
    # A false rung freezes the flasher's TDs; the restart clears them as well.
    copy(0, td[240])


group = Group(
    "out_more",
    [
        case(
            "A toggle holds and flips on its next edge (Result1 = 110); a timed coil's second edge "
            "does not restart it (Result2 = 0), a later one gives a new pulse (ErrorCode = 1); its "
            "TD stops near the preset (Result3 = 1) and is kept after the rung falls (Result4 = 2)",
            ladder=prove_toggle_and_timed,
            init=init_toggle_and_timed,
            scans=200,
            keep=[td[227], td[228]],
            expect={"Result1": 110, "Result2": 0, "Result3": 1, "Result4": 2, "ErrorCode": 1},
        ),
        case(
            "A maintained timed coil runs its time out with the rung gone: on at 200-350 ms "
            "(Result1 = 1), off by 500 ms (Result2 = 0); past its time it follows a held rung, on "
            "(Result3 = 1), then off when it falls (Result4 = 0)",
            ladder=prove_timed_maintained,
            scans=200,
            keep=[td[230], td[231]],
            expect={"Result1": 1, "Result2": 0, "Result3": 1, "Result4": 0},
            probe="a maintained timed coil, read by nothing yet",
        ),
        case(
            "Out coils with a preset of 0 and below: a debounce follows its rung (first on, Result1 "
            "= 3; first off past it, Result2 = 7, 6 by pyrung's count); a flasher (Result3 = 0), a "
            "timed coil (Result4 = 0) and one from a DS of -5 (ErrorCode = 0) give no pulse",
            ladder=prove_zero_and_below,
            init=init_zero_and_below,
            scans=scans_for(),
            keep=[td[232], t[233], td[233], t[234], td[234], t[235], td[235], td[236], td[237]],
            expect={
                "Result1": 3,
                "Result2": range(6, 8),
                "Result3": 0,
                "Result4": 0,
                "ErrorCode": 0,
            },
        ),
        case(
            "An Out coil's DS preset reads live: a flasher's lowered at 300 ms ends the on phase "
            "(Result1 = 0), a debounce's raised at 150 ms holds it off (Result2 = 0); a flasher's "
            "off TD holds into the next on phase (Result3 = 1), a debounce's on TD is kept after "
            "its rung falls (Result4 = 2)",
            ladder=prove_preset_mid_phase,
            init=init_preset_mid_phase,
            ready=lambda slot: ~c[1716],
            scans=200,
            keep=[
                t[239],
                td[239],
                t[242],
                td[242],
                t[225],
                td[225],
                t[113],
                td[113],
                td[114],
                t[115],
                td[115],
                td[241],
            ],
            expect={"Result1": 0, "Result2": 0, "Result3": 1, "Result4": 2},
        ),
    ],
)


def test_out_more():
    assert_all_passed(run(group))


@pytest.mark.parametrize("scans", range(200, 310, 10))
def test_preset_mid_phase_holds_wherever_the_last_run_stopped(scans):
    """One flasher cycle (1100 ms) in 100 ms steps: where the reset cuts the off phase."""
    mid_phase = next(c for c in group.cases if c.id == "preset_mid_phase")
    assert_all_passed(run(Group("out_more", [replace(mid_phase, scans=scans)])))
