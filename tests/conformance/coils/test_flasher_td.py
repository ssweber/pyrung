"""A flasher's frozen off TD when a lowered preset ends its on phase: the ``flasher_td`` group
(part ``text_flasher``).

A false rung freezes a flasher's TDs and the restart is a fresh on phase (``out``, on hardware).
An on phase that times out starts its off phase from 0.  This case reads one cut short by a lowered
preset with an off TD left mid-phase.

``frozen_off_td_at_lowered_preset``: on from DS3903, off 800 ms, in steps that ``Scratch`` holds.
Each step ends on a TD, so no reading depends on how long a scan takes.  A step's rungs come below
the flasher in reverse order, so each new step starts on the next scan.  T41 times each wait that
the flasher's TDs cannot time.

- Step 0: the on preset is 100.  The step ends once the off TD reads 400, and DS3904 keeps that TD.
- Step 1: the rung is false while T41 times 100 ms, and the DS goes to 5000.
- Step 2: the rung is true again.  On the scan the on TD reads 100, Result1 is 1 if the off TD
  still equals DS3904 and 2 if it is 0 (cleared by the restart).
- Step 3: the step ends once the on TD reads 400, and the DS drops to 300 below it.
- Step 4: T41 times from the drop.  At 100 ms Result2 is 1 if the coil is off (the lowered preset
  ended the on phase) and 2 if it is on.  Result3 is 1 if the off TD is above DS3904 (run on from
  it), 2 if below it (from 0) and 0 if it equals it.
- Step 5: at 600 ms on T41 Result4 is 1 if the coil is on (the off phase ran the 400 ms it had
  left) and 0 if it is off (a whole 800 ms from 0).

Running on from the frozen TD reads 1, 1, 1, 1.  A fresh off phase reads 1, 1, 2, 0.  A CLICK read
2, 1, 2, 0 (hardware round of 2026-11-05, twice) on an earlier ladder that took the same steps at
fixed times.  The restart clears the off TD, so the off phase runs a whole 800 ms from 0.

The cases own what ``memory.OWNED_APART`` lists under this group.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.conformance import _runner as runner_module
from pyrung.core import Or, Rung, copy, fill, flasher, on_delay
from tests.conformance.memory import ClickTimer, c, ds, td


def prove_frozen_off_td_at_lowered_preset(slot: Any) -> None:
    on_td, off_td, wait, coil = td[42], td[43], td[41], c[11]
    on_preset, frozen = ds[3903], ds[3904]

    def step(n: int) -> tuple[Any, Any]:
        return (slot.Cmd != 0, slot.Scratch == n)

    with Rung(*step(0)):
        copy(100, on_preset)
    with Rung(slot.Cmd != 0, slot.Scratch >= 1, slot.Scratch <= 3):
        copy(5000, on_preset)
    with Rung(slot.Cmd != 0, slot.Scratch >= 4):
        copy(300, on_preset)
    with Rung(slot.Cmd != 0, slot.Scratch != 1):
        flasher(coil, on=(ClickTimer(42), on_preset), off=(ClickTimer(43), 800))
    with Rung(slot.Cmd != 0, Or(slot.Scratch == 1, slot.Scratch >= 4)):
        on_delay(ClickTimer(41), 5000)
    with Rung(*step(6)):
        copy(1, slot.Fired)
    with Rung(*step(5), wait >= 600, coil):
        copy(1, slot.Result4)
    with Rung(*step(5), wait >= 600):
        copy(6, slot.Scratch)
    lowered = (*step(4), wait >= 100)
    with Rung(*lowered, ~coil):
        copy(1, slot.Result2)
    with Rung(*lowered, coil):
        copy(2, slot.Result2)
    with Rung(*lowered, off_td > frozen):
        copy(1, slot.Result3)
    with Rung(*lowered, off_td < frozen):
        copy(2, slot.Result3)
    with Rung(*lowered):
        copy(5, slot.Scratch)
    with Rung(*step(3), on_td >= 400):
        copy(4, slot.Scratch)
    restarted = (*step(2), on_td >= 100)
    with Rung(*restarted, off_td == frozen):
        copy(1, slot.Result1)
    with Rung(*restarted, off_td == 0):
        copy(2, slot.Result1)
    with Rung(*restarted):
        copy(3, slot.Scratch)
    with Rung(*step(1), wait >= 100):
        copy(2, slot.Scratch)
    with Rung(*step(0), off_td >= 400):
        copy(off_td, frozen)
        copy(1, slot.Scratch)


def init_frozen_off_td_at_lowered_preset(slot: Any) -> None:
    fill(0, ds.select(3903, 3904))
    copy(0, td[42])
    copy(0, td[43])


group = Group(
    "flasher_td",
    [
        case(
            "A flasher restarted with its off TD frozen mid-phase clears it (Result1 = 2); its on "
            "preset lowered below the on TD ends the on phase (Result2 = 1) and the off phase runs "
            "from 0 (Result3 = 2), a whole off time (Result4 = 0)",
            ladder=prove_frozen_off_td_at_lowered_preset,
            init=init_frozen_off_td_at_lowered_preset,
            scans=250,
            expect={"Result1": 2, "Result2": 1, "Result3": 2, "Result4": 0},
        ),
    ],
)


def test_flasher_td():
    assert_all_passed(run(group))


@pytest.mark.parametrize("dt", [0.001, 0.010, 0.025, 0.040])
def test_flasher_td_at_any_scan(monkeypatch, dt):
    """The CLICK scans in about 1 ms, the soft PLC in 10..40 ms."""
    monkeypatch.setattr(runner_module, "DT", dt)
    assert_all_passed(run(Group("flasher_td", [replace(group.cases[0], scans=int(2.0 / dt))])))
