"""A TD written by the ladder into a running Out coil variant: the ``td_write`` group (part
``follow_extra``).

A CLICK's debounce and flasher keep their state in their TDs and, for a flasher, a phase of its
own: they never set T (probe O6) and their TDs freeze while the rung is false.  A TON counts on
from a TD the ladder writes (``timers/acc_clamps_at_32767``).  Does a debounce, which counts its
sides in its TDs, or a flasher move with a TD written while it runs?  A CLICK answered all three
on 2026-11-06: a written TD is counted on from, and a flasher's phase is its own, not read from
its TDs.

Each case runs in steps that ``Scratch`` holds, and its rung is on throughout.  Step 0 ends on
the scan the coil's own TD crosses a threshold, so the side or phase is timing and not on its
first scan.  That scan writes the TD once.  A clock (a TON) times from the scan after the write,
and the later steps end on it.  Each step's rungs come below the coil in reverse order, so each
new step starts on the next scan.  Each Result is a code, 0 for a reading none of the others
names.  Each case's reset writes 0 to the TD that step 0 reads, and to a flasher's other TD.
So a second run starts from 0, even on a PLC whose rise clears no TD.

``debounce_on_td_write``: a debounce of 500 ms on, 50 ms off.  The write puts its on TD at 500,
the preset, once the on TD reads 100 or more.

- Result1 is 1 if the coil comes on before the clock reads 300, and 2 if not.  A side that waits
  for its own time comes on about 400 ms after the write.
- Result2, at a clock of 50: 1 if the on TD is past 500 (it counted on from the write), 2 if it
  is 500 (held at the preset).  A side that keeps its own count, or clears at the write, reads
  under 500.
- Result3, at a clock of 600: 1 if the on TD is 1050 or more (counted on from 500 and still
  running), 2 if it is 500 (held at the preset).  A write that left no mark reads about 700.

``flasher_on_td_write``: a flasher of 500 ms on, 500 ms off.  The write puts its on TD at 600,
past the on preset, once the on TD reads 100 or more.

- Result1 is 1 if the coil goes off before the clock reads 300, and 2 if not.  An on phase that
  runs its own time ends about 400 ms after the write.
- Result2, at a clock of 50: 1 if the off TD is above 0 and under 500 (the off phase is timing),
  2 if it is 0 (the off phase has not begun).
- Result3, at a clock of 50: 1 if the on TD is 600..650 (the write and at most one scan's tick,
  standing through the off phase), 2 if it is under 500 (the write was ignored or cleared).
- ErrorCode, at a clock of 700: 1 if the coil is on, 2 if off.  The off phase ran about 500 ms
  from the write, so the coil is on again.  A flasher that ignores the write is in its off phase
  then.

``flasher_off_td_write``: a flasher of 200 ms on, 500 ms off.  The write puts its off TD at 600,
past the off preset, once the off TD reads 150 or more with the coil off.

- Result1 is 1 if the coil comes on before the clock reads 250, and 2 if not.  An off phase that
  runs its own time ends about 350 ms after the write.  pyrung before 2026-11-06 read the phase
  from the TDs.  Its on TD, held past its preset of 200 through the off phase, ended the new on
  phase on the spot and began a new off phase from 0.
- Result2, at a clock of 50: 1 if the off TD is 600..650 (the write and at most one scan's
  tick, standing), 2 if it is under 500 (a new off phase from 0, or the write ignored).
- Result3, at a clock of 50: 1 if the on TD is above 0 and under 200 (an on phase from 0 since
  the write), 2 if it is 200 or more (held from the last on phase).  0 is a cleared TD.

A CLICK read an earlier ladder that timed each write from the rung's rise and read each value at
a set time, on 2026-11-06.  The debounce came on 2 ms after its write.  Its on TD read 551 at
50 ms past the write and 1101 at 600 ms.  The on flasher went off 4 ms after its write.  At 50 ms
its off TD read 48 and its on TD 602, and the coil was on at 600 ms.  The off flasher came on
4 ms after its write.  At 70 ms its off TD read 602 and its on TD 69.  So every expectation here
is 1.  A flasher keeps its phase as hidden state (``FlasherInstruction``).  A TD ends its phase
only while that phase runs.  The 600..650 bounds hold for any scan up to 50 ms.  They start at
600 because a scan under 1 ms adds nothing to the written TD.  The cases own what
``memory.OWNED_APART`` lists under this group.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.conformance import _runner as runner_module
from pyrung.core import Rung, copy, debounce, flasher, on_delay
from tests.conformance.memory import ClickTimer, c, td


def _step(slot: Any, n: int) -> tuple[Any, Any]:
    return (slot.Cmd != 0, slot.Scratch == n)


def _zero(*tds: Any) -> Any:
    """An init that writes 0 to *tds*."""

    def init(slot: Any) -> None:
        for tag in tds:
            copy(0, tag)

    return init


def _clock(slot: Any, n: int) -> Any:
    """A TON of *n* that times from the scan after step 0's write: its TD in ms."""
    with Rung(slot.Cmd != 0, slot.Scratch >= 1):
        on_delay(ClickTimer(n), 5000)
    return td[n]


def prove_debounce_on_td_write(slot: Any) -> None:
    coil, on_td = c[261], td[150]
    with Rung(slot.Cmd != 0):
        debounce(coil, on=(ClickTimer(150), 500), off=(ClickTimer(151), 50))
    clock = _clock(slot, 152)
    with Rung(*_step(slot, 4)):
        copy(1, slot.Fired)
    with Rung(*_step(slot, 3), clock >= 600, on_td >= 1050):
        copy(1, slot.Result3)
    with Rung(*_step(slot, 3), clock >= 600, on_td == 500):
        copy(2, slot.Result3)
    with Rung(*_step(slot, 3), clock >= 600):
        copy(4, slot.Scratch)
    with Rung(*_step(slot, 2), clock >= 50, on_td > 500):
        copy(1, slot.Result2)
    with Rung(*_step(slot, 2), clock >= 50, on_td == 500):
        copy(2, slot.Result2)
    with Rung(*_step(slot, 2), clock >= 50):
        copy(3, slot.Scratch)
    with Rung(*_step(slot, 1), coil):
        copy(1, slot.Result1)
        copy(2, slot.Scratch)
    with Rung(*_step(slot, 1), clock >= 300):
        copy(2, slot.Result1)
        copy(2, slot.Scratch)
    with Rung(*_step(slot, 0), on_td >= 100):
        copy(500, on_td)
        copy(1, slot.Scratch)


def prove_flasher_on_td_write(slot: Any) -> None:
    coil, on_td, off_td = c[262], td[153], td[154]
    with Rung(slot.Cmd != 0):
        flasher(coil, on=(ClickTimer(153), 500), off=(ClickTimer(154), 500))
    clock = _clock(slot, 155)
    with Rung(*_step(slot, 4)):
        copy(1, slot.Fired)
    with Rung(*_step(slot, 3), clock >= 700, coil):
        copy(1, slot.ErrorCode)
    with Rung(*_step(slot, 3), clock >= 700, ~coil):
        copy(2, slot.ErrorCode)
    with Rung(*_step(slot, 3), clock >= 700):
        copy(4, slot.Scratch)
    with Rung(*_step(slot, 2), clock >= 50, off_td > 0, off_td < 500):
        copy(1, slot.Result2)
    with Rung(*_step(slot, 2), clock >= 50, off_td == 0):
        copy(2, slot.Result2)
    with Rung(*_step(slot, 2), clock >= 50, on_td >= 600, on_td <= 650):
        copy(1, slot.Result3)
    with Rung(*_step(slot, 2), clock >= 50, on_td < 500):
        copy(2, slot.Result3)
    with Rung(*_step(slot, 2), clock >= 50):
        copy(3, slot.Scratch)
    with Rung(*_step(slot, 1), ~coil):
        copy(1, slot.Result1)
        copy(2, slot.Scratch)
    with Rung(*_step(slot, 1), clock >= 300):
        copy(2, slot.Result1)
        copy(2, slot.Scratch)
    with Rung(*_step(slot, 0), on_td >= 100):
        copy(600, on_td)
        copy(1, slot.Scratch)


def prove_flasher_off_td_write(slot: Any) -> None:
    coil, on_td, off_td = c[263], td[156], td[157]
    with Rung(slot.Cmd != 0):
        flasher(coil, on=(ClickTimer(156), 200), off=(ClickTimer(157), 500))
    clock = _clock(slot, 158)
    with Rung(*_step(slot, 3)):
        copy(1, slot.Fired)
    with Rung(*_step(slot, 2), clock >= 50, off_td >= 600, off_td <= 650):
        copy(1, slot.Result2)
    with Rung(*_step(slot, 2), clock >= 50, off_td < 500):
        copy(2, slot.Result2)
    with Rung(*_step(slot, 2), clock >= 50, on_td > 0, on_td < 200):
        copy(1, slot.Result3)
    with Rung(*_step(slot, 2), clock >= 50, on_td >= 200):
        copy(2, slot.Result3)
    with Rung(*_step(slot, 2), clock >= 50):
        copy(3, slot.Scratch)
    with Rung(*_step(slot, 1), coil):
        copy(1, slot.Result1)
        copy(2, slot.Scratch)
    with Rung(*_step(slot, 1), clock >= 250):
        copy(2, slot.Result1)
        copy(2, slot.Scratch)
    with Rung(*_step(slot, 0), off_td >= 150, ~coil):
        copy(600, off_td)
        copy(1, slot.Scratch)


group = Group(
    "td_write",
    [
        case(
            "A debounce whose on TD the ladder writes to its preset (500) while the rung is on "
            "counts on from the write: the coil is on within 300 ms (Result1 = 1), and the on "
            "TD reads past 500 at 50 ms after the write (Result2 = 1) and 1050 or more at "
            "600 ms (Result3 = 1)",
            ladder=prove_debounce_on_td_write,
            ready=lambda slot: ~c[261],
            init=_zero(td[150]),
            # The off TD runs while the rung is off.  The reset writes 0 to the on TD, and the
            # part of a ms it had counted stays until the rise.
            keep=(td[150], td[151]),
            expect={"Result1": 1, "Result2": 1, "Result3": 1},
            scans=100,
            fires_within=1.5,
        ),
        case(
            "A flasher whose on TD the ladder writes past its preset (600 of 500) during the on "
            "phase ends the phase: the coil is off within 300 ms (Result1 = 1), and at 50 ms "
            "after the write the off TD is timing (Result2 = 1) and the on TD holds the write "
            "and at most a scan's tick (Result3 = 1), and the coil is on again at 700 ms "
            "(ErrorCode = 1, 2 off)",
            ladder=prove_flasher_on_td_write,
            ready=lambda slot: ~c[262],
            init=_zero(td[153], td[154]),
            # The part of a ms each TD had counted stays until the rise.
            keep=(td[153], td[154]),
            expect={"Result1": 1, "Result2": 1, "Result3": 1, "ErrorCode": 1},
            scans=100,
            fires_within=1.5,
        ),
        case(
            "A flasher whose off TD the ladder writes past its preset (600 of 500) during the "
            "off phase ends the phase: the coil is on within 250 ms (Result1 = 1), and at 50 ms "
            "after the write the off TD holds the write and at most a scan's tick (Result2 = 1) "
            "and the on TD times from 0 (Result3 = 1)",
            ladder=prove_flasher_off_td_write,
            ready=lambda slot: ~c[263],
            init=_zero(td[156], td[157]),
            keep=(td[156], td[157]),
            expect={"Result1": 1, "Result2": 1, "Result3": 1},
            scans=60,
            fires_within=2.0,
        ),
    ],
)


def test_td_write():
    assert_all_passed(run(group))


@pytest.mark.parametrize("dt", [0.0004, 0.001, 0.010, 0.025, 0.040])
def test_td_write_at_any_scan(monkeypatch, dt):
    """The CLICK scans in about 1 ms or less, the soft PLC in 10..40 ms."""
    monkeypatch.setattr(runner_module, "DT", dt)
    cases = [replace(c, scans=int(1.0 / dt)) for c in group.cases]
    assert_all_passed(run(Group("td_write", cases)))
