"""A TD written by the ladder into a running Out coil variant: the ``td_write`` group (part
``follow_extra``).

A CLICK's debounce and flasher keep their state in their TDs and, for a flasher, a phase of its
own: they never set T (probe O6) and their TDs freeze while the rung is false.  A TON counts on
from a TD the ladder writes (``timers/acc_clamps_at_32767``).  Does a debounce, which counts its
sides in its TDs, or a flasher move with a TD written while it runs?  A clock (a TON from ``Cmd``,
its TD in ms) times each write, once the side is timing and not on its first scan.  A CLICK
answered all three on 2026-11-06: a written TD is counted on from, and a flasher's phase is its
own, not read from its TDs.

``debounce_on_td_write``: a debounce of 500 ms on, 50 ms off, its rung on.  At 100 ms the ladder
writes its on TD to 500, the preset, once.

- Result1, the clock when the coil first reads on.  Near 100, a scan or two past the write: the
  write counted, the side is done at once.  Near 500 or past it: the coil waits for its own time.
- Result2, the on TD at 150 ms.  Near 550: the write was counted on from.  Near 150: the side
  keeps its own count.  500: clamped to the preset.  Near 50: cleared.
- Result3, the on TD at 700 ms.  About 1100: counted on from 500 and still running with the rung.
  About 700: the write left no mark.  500: held at the preset.

A CLICK read 102, 551, 1101: the write counts, the side counts on from it.

``flasher_on_td_write``: a flasher of 500 ms on, 500 ms off, its rung on.  At 100 ms the ladder
writes its on TD to 600, past the on preset, once.

- Result1, the clock when the coil first reads off (from 100 ms).  Near 100: the on phase ended at
  once.  Near 500: the coil runs its own time.
- Result2, the off TD at 150 ms.  Near 50: the off phase began from 0 at the write.  0: it has not
  begun.
- Result3, the on TD at 150 ms.  600 and a scan's tick: the written value, ticked once, stands
  through the off phase.  Near 150: the TD ignored the write.  0: cleared.
- ErrorCode, the coil at 700 ms: 1 on, 2 off.  On: the off phase ran 100..600, on again at 600.
  A flasher that ignores the write: off (off phase 500..1000).

A CLICK read 104, 48, 602, 1: the on phase ended on the scan after the write.

``flasher_off_td_write``: a flasher of 200 ms on, 500 ms off, its rung on.  Its off phase is
200..700 ms.  At 350 ms the ladder writes its off TD to 600, past the off preset, once.

- Result1, the clock when the coil first reads on again (from 350 ms).  Near 350: the off phase
  ended at once and the on phase began.  Near 700: the coil runs its own time.  Near 860 (pyrung
  before 2026-11-06, the phase read from the TDs): the on TD, held past its preset of 200 through
  the off phase, ends the new on phase on the spot and a new off phase begins from 0.
- Result2, the off TD at 420 ms.  600 and a scan's tick: the written value stands, ticked once.
  Near 60: a new off phase began from 0.  Near 220: the TD ignored the write.  0: cleared.
- Result3, the on TD at 420 ms.  Near 60: the on phase began from 0 at the write.  210: held from
  the last on phase.  0: cleared.

A CLICK read 354, 602, 69: the write ended the off phase on the next scan, a fresh on phase
counts from 0 and the off TD holds what it reached.  So a flasher keeps its phase as hidden state
(``FlasherInstruction``): a TD ends its phase only while that phase runs.

The expectations are CLICK's, as ranges where a value is wall-clock; they hold for any scan up to
40 ms.  pyrung's own readings at the in-process 10 ms scan are pinned in
``test_pyrung_follows_a_written_td``.  The cases own what ``memory.OWNED_APART`` lists under this
group.
"""

from __future__ import annotations

from typing import Any

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import Rung, copy, debounce, flasher
from tests.conformance.memory import ClickTimer, c, td
from tests.conformance.probe_kit import end_at, ms_clock, once


def prove_debounce_on_td_write(slot: Any) -> None:
    coil, on_td = c[261], td[150]
    clock = ms_clock(slot, 152)
    with Rung(slot.Cmd != 0):
        debounce(coil, on=(ClickTimer(150), 500), off=(ClickTimer(151), 50))
    with Rung(slot.Cmd != 0, clock >= 100):
        copy(500, on_td, oneshot=True)
    with Rung(*once(slot), coil):
        copy(clock, slot.Result1, oneshot=True)
    with Rung(*once(slot), clock >= 150):
        copy(on_td, slot.Result2, oneshot=True)
    with Rung(*once(slot), clock >= 700):
        copy(on_td, slot.Result3, oneshot=True)
    end_at(slot, clock, 700)


def prove_flasher_on_td_write(slot: Any) -> None:
    coil, on_td, off_td = c[262], td[153], td[154]
    clock = ms_clock(slot, 155)
    with Rung(slot.Cmd != 0):
        flasher(coil, on=(ClickTimer(153), 500), off=(ClickTimer(154), 500))
    with Rung(slot.Cmd != 0, clock >= 100):
        copy(600, on_td, oneshot=True)
    with Rung(*once(slot), clock >= 100, ~coil):
        copy(clock, slot.Result1, oneshot=True)
    with Rung(*once(slot), clock >= 150):
        copy(off_td, slot.Result2, oneshot=True)
        copy(on_td, slot.Result3, oneshot=True)
    with Rung(*once(slot), clock >= 700, coil):
        copy(1, slot.ErrorCode)
    with Rung(*once(slot), clock >= 700, ~coil):
        copy(2, slot.ErrorCode)
    end_at(slot, clock, 700)


def prove_flasher_off_td_write(slot: Any) -> None:
    coil, on_td, off_td = c[263], td[156], td[157]
    clock = ms_clock(slot, 158)
    with Rung(slot.Cmd != 0):
        flasher(coil, on=(ClickTimer(156), 200), off=(ClickTimer(157), 500))
    with Rung(slot.Cmd != 0, clock >= 350):
        copy(600, off_td, oneshot=True)
    with Rung(*once(slot), clock >= 350, coil):
        copy(clock, slot.Result1, oneshot=True)
    with Rung(*once(slot), clock >= 420):
        copy(off_td, slot.Result2, oneshot=True)
        copy(on_td, slot.Result3, oneshot=True)
    end_at(slot, clock, 1000)


group = Group(
    "td_write",
    [
        case(
            "A debounce whose on TD the ladder writes to its preset (500) at 100 ms while the "
            "rung is on counts on from the write: the coil is on a scan or two later (Result1, "
            "the clock, near 100) and the on TD reads near 550 at 150 ms (Result2) and near 1100 "
            "at 700 ms (Result3)",
            ladder=prove_debounce_on_td_write,
            ready=lambda slot: ~c[261],
            # The TDs of a debounce are kept after its rung falls; the next rise starts the on
            # side from 0.
            keep=(td[150], td[151]),
            expect={
                "Result1": range(100, 181),
                "Result2": range(501, 601),
                "Result3": range(1050, 1151),
            },
            scans=100,
            fires_within=1.5,
        ),
        case(
            "A flasher whose on TD the ladder writes past its preset (600 of 500) at 100 ms "
            "during the on phase ends the phase on the next scan: the coil is off near 100 ms "
            "(Result1, the clock), at 150 ms the off TD counts from 0 (Result2, near 50) and "
            "the on TD holds the write and a scan's tick (Result3, near 600), and the coil is "
            "on again at 700 ms (ErrorCode = 1, 2 off)",
            ladder=prove_flasher_on_td_write,
            ready=lambda slot: ~c[262],
            keep=(td[153], td[154]),
            expect={
                "Result1": range(100, 181),
                "Result2": range(91),
                "Result3": range(601, 641),
                "ErrorCode": 1,
            },
            scans=100,
            fires_within=1.5,
        ),
        case(
            "A flasher whose off TD the ladder writes past its preset (600 of 500) at 350 ms "
            "during the off phase ends the phase on the next scan: the coil is on again near "
            "350 ms (Result1, the clock), and at 420 ms the off TD holds the write and a scan's "
            "tick (Result2, near 600) and the on TD counts from 0 (Result3, near 70)",
            ladder=prove_flasher_off_td_write,
            ready=lambda slot: ~c[263],
            keep=(td[156], td[157]),
            expect={
                "Result1": range(350, 431),
                "Result2": range(601, 641),
                "Result3": range(111),
            },
            scans=130,
            fires_within=2.0,
        ),
    ],
)


def test_td_write():
    assert_all_passed(run(group))


def test_pyrung_follows_a_written_td():
    """What pyrung reads at the in-process 10 ms scan."""
    read = {r.case.id: r.actual for r in run(group)}
    assert read["debounce_on_td_write"] == {"Result1": 110, "Result2": 550, "Result3": 1100}
    assert read["flasher_on_td_write"] == {
        "Result1": 120,
        "Result2": 40,
        "Result3": 610,
        "ErrorCode": 1,
    }
    assert read["flasher_off_td_write"] == {"Result1": 370, "Result2": 610, "Result3": 60}
