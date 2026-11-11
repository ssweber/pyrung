"""The v4 Out family, past what ``out`` reads.

Every expectation is what a CLICK read (hardware round of 2026-11-05).  The timed coils of
``toggle_and_timed``, ``timed_maintained`` and ``preset_mid_phase`` run in steps that ``Scratch``
holds.  A step ends on a TD or on a flasher's phase, so no reading depends on how long a scan
takes.  A step's rungs come below the coils in reverse order, so each new step starts on the next
scan.  Each case has a clock, a TON that starts with its coils.  It times what the coils' own TDs
cannot.  A time below is the first scan the clock reads that time or more.  The CLICK read each
case on an earlier ladder, which read the same things in windows of the clock.

``out_more``:

- ``toggle_and_timed``: ``out`` reads a toggle's first flip and a timed coil's held rung.  A toggle
  over two edges, scan 3 reads it on, scan 5 with the rung false, scan 7 past the second edge.
  Result1 = 110, by digit.  100 if the rung's fall turns it off, 111 if the second edge is
  lost.  The timed coils start on scan 9, with the clock, TD226.

  - A timed coil of 300 ms has its rung on until 100 ms on the clock and again from 150 to 250 ms.
    Result2 is the coil at 360 ms.  It is 0 if the second edge does nothing and 1 if the edge
    restarts the time.
  - Its rung comes back at 500 ms, after the time is up.  ErrorCode is the coil at 550 ms.  It is 1
    for a new pulse and 0 if the coil needs more than an edge.
  - A timed coil of 200 ms has its rung held.  At 500 ms Result3 is 1 if its TD stopped near the
    preset, 2 if it counts on as a debounce's does, and 3 if it is 0.
  - That rung falls at 550 ms.  At 700 ms Result4 is 1 if the TD is 0 and 2 if it is kept.

  A CLICK reads 110, 0, 1, 2 and ErrorCode 1.  The TD is kept.
- ``timed_maintained``: nothing has read a maintained coil.  The clock is TD229.

  - One coil's rung goes at 100 ms into a 400 ms time.  Result1 is the coil at 250 ms (0 if a
    maintained coil follows its rung throughout).  Result2 is the coil at 500 ms (1 if it holds).
  - The other's rung is held past a 200 ms time.  Result3 is the coil at 500 ms (0 if it pulses as
    the plain one does).  The rung goes at 500 ms, and Result4 is the coil at 700 ms (1 if it stays
    on).
- ``zero_and_below``: a preset of 0 or below, scan-paced, the rung true on scans 3..5.  A debounce of
  0/0: first on (Result1), first off past it (Result2); 3 and 6 are the rung's own scans, later ones
  mean a scan's lag.  A timed coil from a DS holding -5: scans on (ErrorCode), 1 as a preset of 0,
  3 (the rung's) where -5 reads as unsigned, 65531 ms.  A CLICK reads 3, 7, 0: no pulse from a
  preset of 0 or below, and -5 is signed.  Its debounce turns off a scan after the rung falls, as
  pyrung's has since 2026-11-06 (``deb_zero/off_side_lag``: the off side always takes a scan more).
  CLICK's dialog takes a debounce preset of 0 but refuses 0 for a flasher or a timed coil, so this
  case asks neither of those.
- ``preset_mid_phase``: a preset in a DS moved mid-phase, read live or at the phase's start.  The
  clock is TD238.

  - A flasher of 100/100 runs its on phase and its off phase.  Halfway through its second on
    phase, on the first scan that phase's TD reads 50 or more, Result3 is 1 if the off TD holds its
    last value and 2 if it is 0.
  - A debounce's on preset goes from 200 up to 2000 ms at 100 ms.  Result2 is its coil on the scan
    after 450 ms.  It is 0 if the debounce reads the preset live and 1 if it took the preset at the
    rise.
  - A flasher's on preset goes from 1000 down to 100 ms at 300 ms, or once Result3 is read if that
    comes later.  Result1 is its coil on the scan after 450 ms.  It is 0 if the flasher reads the
    preset live (the on phase ended) and 1 if it took the preset at the phase's start.
  - A debounce's rung goes at 100 ms.  On the scan after 450 ms, Result4 is 1 if its on TD is 0
    and 2 if the TD is kept.

  A CLICK reads 0, 0, 1, 2.  The on TD is kept.  The reset writes 0 to the first flasher's off TD
  (TD240), which a false rung only freezes.  The restart clears it too
  (``flasher_td/frozen_off_td_at_lowered_preset``), so the write is spare.

The cases own what ``memory.OWNED`` lists under this group.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.conformance import _runner as runner_module
from pyrung.core import (
    Or,
    Rung,
    copy,
    debounce,
    fill,
    flasher,
    math,
    on_delay,
    out,
    reset,
    timed,
    toggle,
)
from tests.conformance.memory import ClickTimer, c, ds, t, td
from tests.conformance.probe_kit import ms_clock
from tests.conformance.scans import count_scans, done, scans_for


def _step(slot: Any, n: int) -> tuple[Any, Any]:
    return (slot.Cmd != 0, slot.Scratch == n)


def prove_toggle_and_timed(slot):
    count_scans(slot)
    coil, rung = c[1702], c[1703]
    with Rung(slot.Cmd != 0, Or(slot.Scratch == 2, slot.Scratch == 3, slot.Scratch >= 6)):
        out(rung)
    with Rung(rung):
        toggle(coil)
    with Rung(slot.Cmd != 0, slot.Scratch == 3, coil):
        math(slot.Result1 + 100, slot.Result1)
    with Rung(slot.Cmd != 0, slot.Scratch == 5, coil):
        math(slot.Result1 + 10, slot.Result1)
    with Rung(slot.Cmd != 0, slot.Scratch == 7, coil):
        math(slot.Result1 + 1, slot.Result1)
    # The timed coils run from scan 9, where count_scans leaves Scratch, in steps 9..16.
    clock = td[226]
    with Rung(slot.Cmd != 0, slot.Scratch >= 9):
        on_delay(ClickTimer(226), 5000)
    pulse, held = c[1705], c[1706]
    with Rung(slot.Cmd != 0, Or(slot.Scratch == 9, slot.Scratch == 11, slot.Scratch == 14)):
        timed(pulse, ClickTimer(227), 300)
    with Rung(slot.Cmd != 0, slot.Scratch >= 9, slot.Scratch <= 14):
        timed(held, ClickTimer(228), 200)
    with Rung(*_step(slot, 16)):
        copy(1, slot.Fired)
    fallen = (*_step(slot, 15), clock >= 700)
    with Rung(*fallen, td[228] == 0):
        copy(1, slot.Result4)
    with Rung(*fallen, td[228] >= 200):
        copy(2, slot.Result4)
    with Rung(*fallen):
        copy(16, slot.Scratch)
    with Rung(*_step(slot, 14), clock >= 550, pulse):
        copy(1, slot.ErrorCode)
    with Rung(*_step(slot, 14), clock >= 550):
        copy(15, slot.Scratch)
    held_on = (*_step(slot, 13), clock >= 500)
    with Rung(*held_on, td[228] >= 200, td[228] < 300):
        copy(1, slot.Result3)
    with Rung(*held_on, td[228] >= 300):
        copy(2, slot.Result3)
    with Rung(*held_on, td[228] == 0):
        copy(3, slot.Result3)
    with Rung(*held_on):
        copy(14, slot.Scratch)
    with Rung(*_step(slot, 12), clock >= 360, pulse):
        copy(1, slot.Result2)
    with Rung(*_step(slot, 12), clock >= 360):
        copy(13, slot.Scratch)
    with Rung(*_step(slot, 11), clock >= 250):
        copy(12, slot.Scratch)
    with Rung(*_step(slot, 10), clock >= 150):
        copy(11, slot.Scratch)
    with Rung(*_step(slot, 9), clock >= 100):
        copy(10, slot.Scratch)


def init_toggle_and_timed(slot):
    reset(c[1702])


def prove_timed_maintained(slot):
    clock = ms_clock(slot, 229)
    dropped, held = c[1707], c[1708]
    with Rung(*_step(slot, 0)):
        timed(dropped, ClickTimer(230), 400, maintained=True)
    with Rung(slot.Cmd != 0, slot.Scratch <= 2):
        timed(held, ClickTimer(231), 200, maintained=True)
    with Rung(*_step(slot, 4)):
        copy(1, slot.Fired)
    with Rung(*_step(slot, 3), clock >= 700, held):
        copy(1, slot.Result4)
    with Rung(*_step(slot, 3), clock >= 700):
        copy(4, slot.Scratch)
    with Rung(*_step(slot, 2), clock >= 500, dropped):
        copy(1, slot.Result2)
    with Rung(*_step(slot, 2), clock >= 500, held):
        copy(1, slot.Result3)
    with Rung(*_step(slot, 2), clock >= 500):
        copy(3, slot.Scratch)
    with Rung(*_step(slot, 1), clock >= 250, dropped):
        copy(1, slot.Result1)
    with Rung(*_step(slot, 1), clock >= 250):
        copy(2, slot.Scratch)
    with Rung(*_step(slot, 0), clock >= 100):
        copy(1, slot.Scratch)


def prove_zero_and_below(slot):
    rung, deb, neg = c[1709], c[1710], c[1714]
    window = (slot.Cmd != 0, slot.Scratch >= 1, slot.Scratch <= 8)
    count_scans(slot)
    with Rung(slot.Cmd != 0, slot.Scratch == 1):
        copy(-5, ds[3926])
    with Rung(slot.Cmd != 0, slot.Scratch >= 3, slot.Scratch <= 5):
        out(rung)
    with Rung(rung):
        debounce(deb, on=(ClickTimer(232), 0), off=(ClickTimer(233), 0))
    with Rung(rung):
        timed(neg, ClickTimer(237), ds[3926])
    with Rung(*window, deb, slot.Result1 == 0):
        copy(slot.Scratch, slot.Result1)
    with Rung(slot.Cmd != 0, slot.Scratch >= 3, slot.Scratch <= 8, ~deb, slot.Result2 == 0):
        copy(slot.Scratch, slot.Result2)
    with Rung(*window, neg):
        math(slot.ErrorCode + 1, slot.ErrorCode)
    done(slot)


def init_zero_and_below(slot):
    copy(0, ds[3926])


def prove_preset_mid_phase(slot):
    clock = ms_clock(slot, 238)
    fla, deb, fla2, deb2 = c[1715], c[1716], c[1718], c[1719]
    with Rung(*_step(slot, 0)):
        copy(1000, ds[3927])
        copy(200, ds[3928])
    with Rung(slot.Cmd != 0, slot.Scratch >= 1):
        copy(2000, ds[3928])
    with Rung(slot.Cmd != 0, slot.Scratch >= 4):
        copy(100, ds[3927])
    with Rung(slot.Cmd != 0):
        flasher(fla, on=(ClickTimer(239), ds[3927]), off=(ClickTimer(240), 1000))
    with Rung(slot.Cmd != 0):
        debounce(deb, on=(ClickTimer(241), ds[3928]), off=(ClickTimer(242), 1000))
    with Rung(slot.Cmd != 0):
        flasher(fla2, on=(ClickTimer(225), 100), off=(ClickTimer(113), 100))
    with Rung(*_step(slot, 0)):
        debounce(deb2, on=(ClickTimer(114), 2000), off=(ClickTimer(115), 2000))
    with Rung(*_step(slot, 6)):
        copy(1, slot.Fired)
    with Rung(*_step(slot, 5), fla):
        copy(1, slot.Result1)
    with Rung(*_step(slot, 5), deb):
        copy(1, slot.Result2)
    with Rung(*_step(slot, 5), td[114] == 0):
        copy(1, slot.Result4)
    with Rung(*_step(slot, 5), td[114] > 0):
        copy(2, slot.Result4)
    with Rung(*_step(slot, 5)):
        copy(6, slot.Scratch)
    with Rung(*_step(slot, 4), clock >= 450):
        copy(5, slot.Scratch)
    with Rung(*_step(slot, 3), clock >= 300):
        copy(4, slot.Scratch)
    # Halfway through the second on phase, its TD reads 50 to 99.
    second_on = (*_step(slot, 2), fla2, td[225] >= 50)
    with Rung(*second_on, td[113] >= 100):
        copy(1, slot.Result3)
    with Rung(*second_on, td[113] == 0):
        copy(2, slot.Result3)
    with Rung(*second_on):
        copy(3, slot.Scratch)
    with Rung(*_step(slot, 1), ~fla2):
        copy(2, slot.Scratch)
    with Rung(*_step(slot, 0), clock >= 100):
        copy(1, slot.Scratch)


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
            "A maintained timed coil runs its time out with the rung gone: on at 250 ms "
            "(Result1 = 1), off at 500 ms (Result2 = 0); past its time it follows a held rung, on "
            "(Result3 = 1), then off when it falls (Result4 = 0)",
            ladder=prove_timed_maintained,
            scans=200,
            keep=[td[230], td[231]],
            expect={"Result1": 1, "Result2": 0, "Result3": 1, "Result4": 0},
            probe="a maintained timed coil, read by nothing yet",
        ),
        case(
            "A debounce with a preset of 0 follows its rung (first on, Result1 = 3; first off past "
            "it, Result2 = 7), and a timed coil from a DS of -5 gives no pulse (ErrorCode = 0)",
            ladder=prove_zero_and_below,
            init=init_zero_and_below,
            scans=scans_for(),
            keep=[td[232], t[233], td[233], td[237]],
            expect={
                "Result1": 3,
                "Result2": 7,
                "ErrorCode": 0,
            },
        ),
        case(
            "An Out coil's DS preset reads live: a flasher's lowered at 300 ms ends the on phase "
            "(Result1 = 0), a debounce's raised at 100 ms holds it off (Result2 = 0); a flasher's "
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


#: Each case that steps on its timers, with the time it takes in-process.
STEPPED = {"toggle_and_timed": 1.0, "timed_maintained": 1.0, "preset_mid_phase": 0.8}


@pytest.mark.parametrize("dt", [0.001, 0.010, 0.020, 0.025, 0.030, 0.035, 0.040, 0.050])
@pytest.mark.parametrize("case_id", STEPPED)
def test_out_more_at_any_scan(monkeypatch, dt, case_id):
    """The CLICK scans in about 1 ms, the soft PLC in 10..40 ms."""
    monkeypatch.setattr(runner_module, "DT", dt)
    stepped = next(c for c in group.cases if c.id == case_id)
    scans = int(STEPPED[case_id] / dt) + 9
    assert_all_passed(run(Group("out_more", [replace(stepped, scans=scans)])))


def test_debounce_of_zero_goes_off_a_scan_after_its_rung():
    """``zero_and_below`` Result2 takes 6 or 7; pyrung reads CLICK's 7."""
    zero = next(c for c in group.cases if c.id == "zero_and_below")
    (result,) = run(Group("out_more", [zero]))
    assert result.actual["Result2"] == 7
