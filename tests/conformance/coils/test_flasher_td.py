"""A flasher's frozen off TD when a lowered preset ends its on phase: the ``flasher_td`` group
(part ``text_flasher``).

A false rung freezes a flasher's TDs and the restart is a fresh on phase (``out``, on hardware).
An on phase that times out starts its off phase from 0.  One cut short by a lowered preset has not
been read with an off TD left mid-phase: pyrung, reading the phase from the TDs, runs on from it.

``frozen_off_td_at_lowered_preset``: on from a DS, 100 ms, off 800 ms.  The rung drops at 500 ms,
about 400 ms into the off phase, and comes back at 600 ms, the DS raised to 5000 in between.  At
1000 ms, about 400 ms into the on phase, the DS drops to 300, below the on TD.

- Result1, the off TD at 700-900 ms against what it froze at: 1 still there, 2 cleared by the
  restart.
- Result2, the coil at 1060-1140 ms: 1 off, the lowered preset ended the on phase; 2 still on.
- Result3, the off TD in the same window: 1 above what it froze at, run on from it; 2 below it, from
  0; 0 standing still.
- Result4, the coil at 1510-1650 ms: 1 on, the off phase ran the 400 ms it had left; 0 off, a whole
  800 ms from 0, which ends at 1800 ms.

pyrung reads 1, 1, 1, 1; a fresh off phase reads 1, 1, 2, 0.  Windows hold for any scan up to 50 ms.

The cases own what ``memory.OWNED_APART`` lists under this group.
"""

from __future__ import annotations

from typing import Any

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import Or, Rung, copy, flasher, on_delay
from tests.conformance.memory import ClickTimer, c, ds, td
from tests.conformance.probe_kit import once


def prove_frozen_off_td_at_lowered_preset(slot: Any) -> None:
    clock, off_td, coil, on_preset = td[41], td[43], c[11], ds[3903]
    with Rung(slot.Cmd != 0):
        on_delay(ClickTimer(41), 5000)
    with Rung(slot.Cmd != 0, clock < 520):
        copy(100, on_preset)
    with Rung(slot.Cmd != 0, clock >= 520, clock < 1000):
        copy(5000, on_preset)
    with Rung(slot.Cmd != 0, clock >= 1000):
        copy(300, on_preset)
    with Rung(slot.Cmd != 0, Or(clock < 500, clock >= 600)):
        flasher(coil, on=(ClickTimer(42), on_preset), off=(ClickTimer(43), 800))
    with Rung(*once(slot), clock >= 520, clock < 590):
        copy(off_td, slot.Scratch)
    restarted = (*once(slot), clock >= 700, clock < 900)
    with Rung(*restarted, off_td == slot.Scratch):
        copy(1, slot.Result1)
    with Rung(*restarted, off_td == 0):
        copy(2, slot.Result1)
    lowered = (*once(slot), clock >= 1060, clock < 1140)
    with Rung(*lowered, ~coil):
        copy(1, slot.Result2)
    with Rung(*lowered, coil):
        copy(2, slot.Result2)
    with Rung(*lowered, off_td > slot.Scratch):
        copy(1, slot.Result3)
    with Rung(*lowered, off_td < slot.Scratch):
        copy(2, slot.Result3)
    with Rung(*once(slot), clock >= 1510, clock < 1650, coil):
        copy(1, slot.Result4)
    with Rung(*once(slot), clock >= 1700):
        copy(1, slot.Fired)


def init_frozen_off_td_at_lowered_preset(slot: Any) -> None:
    copy(0, ds[3903])
    copy(0, td[42])
    copy(0, td[43])


group = Group(
    "flasher_td",
    [
        case(
            "A flasher restarted with its off TD frozen mid-phase keeps it (Result1 = 1); its on "
            "preset lowered below the on TD ends the on phase (Result2 = 1) and the off phase runs "
            "on from the frozen TD (Result3 = 1), its time left and no more (Result4 = 1)",
            ladder=prove_frozen_off_td_at_lowered_preset,
            init=init_frozen_off_td_at_lowered_preset,
            scans=200,
            expect={"Result1": 1, "Result2": 1, "Result3": 1, "Result4": 1},
            probe="a lowered on preset's off phase: from a frozen TD or from 0",
        ),
    ],
)


def test_flasher_td():
    assert_all_passed(run(group))
