"""A debounce's off side: the ``coil_timing`` group (part ``follow_timing``).

Two questions the fix step ``timed-coils`` (5 in the ``conform-fix`` arc) built on with no
reading, beside ``deb_zero``'s, answered on a CLICK on 2026-11-06.

- ``off_td_held``: does a debounce's off TD keep its value while the rung is true?  A debounce of
  0 / 500 ms, in steps that ``Scratch`` holds.  The reset leaves the off TD timed out (500 ms or
  more), and the case starts with it so.  A clock (a TON on the debounce's rung) times each spell
  the rung is on, from 0.  Each step's rungs come below the debounce in reverse order, so each new
  step starts on the next scan.

  - Step 0: the rung is on until the clock reads 50.
  - Step 1: the scan the rung falls.  Result3 is 1 if the off TD is under 100, 0 if not.
  - Step 2: the rung stays off until the off TD reads 100 or more.  Result1 is 1 if the coil is
    still on then, 0 if it is off.
  - Step 3: the rung is on again.  Once the clock reads 50, Result2 is 1 if the off TD is still
    100 or more, 2 if it is 0.  ``Fired`` goes up on that scan.

  The readings (Result1, Result2, Result3):

  - 1, 2, 1: the rung's rise clears the off TD, and the fall times from 0.
  - 1, 1, 1: the off TD is held while the rung is true (as for the on side), and a fall starts it
    from 0 (pyrung from timed-coils to 2026-11-06).
  - 0, 1, 0: it is held while the rung is true and a fall resumes it.  It is past its preset at
    once, and the coil drops on the scan after the fall.

  A CLICK read an earlier ladder that timed it all on one clock from ``Cmd``.  It read 1, 2, and
  an off TD of 101 at 100 ms after the fall.  That is the first reading.  So the off TD is under
  100 on the scan of the fall, as ``deb_fall/off_td_at_fall`` read (1..2).
- ``deb_off_against_tof``: a debounce of 0 / 5 ms and a TOF of 5 ms on one rung, on scans 2 and 3.
  The scan each goes off first (Result1 the debounce, Result2 the TOF, up to 40, 0 for never) and
  ErrorCode = Result1 - Result2: 1 where the debounce's off side takes a scan more than a TOF of
  its preset, as ``zero_and_below`` read at 0, 0 where they agree.  A CLICK read 6, 5, 1: a scan
  more.  pyrung reads 5, 4, 1 at the in-process 10 ms scan (on a CLICK the scans are shorter, so
  the off comes later), so the scans are ranges and ErrorCode is exact.  The reset writes none of
  the three TDs: it wrote 0 to them every reset scan until a CLICK hung in that reset on a repeat
  run (2026-11-06), since a TOF whose Acc is zeroed while its rung is false keeps its Done on
  (``timers/tof_times_only_when_false``), so ``ready`` (the TOF's Done off) never held.  None needs
  it.  The rung's rise clears the debounce's on TD and the TOF's, and holds the debounce's off TD
  at 0.  The case lists the three TDs in ``keep`` only because they move after it fires.

pyrung's in-process readings are pinned in ``test_pyrung_reads_the_debounce_lag``.  The cases own
what ``memory.OWNED_APART`` lists under this group.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.conformance import _runner as runner_module
from pyrung.core import Or, Rung, copy, debounce, math, off_delay, on_delay, out
from tests.conformance.memory import ClickTimer, c, t, td
from tests.conformance.scans import count_scans, done, scans_for

OFF_WINDOW = 40


def prove_off_td_held(slot: Any) -> None:
    deb = c[242]
    off_td = td[116]
    rung = (slot.Cmd != 0, Or(slot.Scratch == 0, slot.Scratch >= 3))

    def step(n: int) -> tuple[Any, Any]:
        return (slot.Cmd != 0, slot.Scratch == n)

    with Rung(*rung):
        debounce(deb, on=(ClickTimer(115), 0), off=(ClickTimer(116), 500))
    # The clock times the rung's time on, from 0 at each rise.
    with Rung(*rung):
        on_delay(ClickTimer(117), 5000)
    clock = td[117]
    with Rung(*step(3), clock >= 50, off_td >= 100):
        copy(1, slot.Result2)
    with Rung(*step(3), clock >= 50, off_td == 0):
        copy(2, slot.Result2)
    with Rung(*step(3), clock >= 50):
        copy(4, slot.Scratch)
        copy(1, slot.Fired)
    with Rung(*step(2), off_td >= 100, deb):
        copy(1, slot.Result1)
    with Rung(*step(2), off_td >= 100):
        copy(3, slot.Scratch)
    with Rung(*step(1), off_td < 100):
        copy(1, slot.Result3)
    with Rung(*step(1)):
        copy(2, slot.Scratch)
    with Rung(*step(0), clock >= 50):
        copy(1, slot.Scratch)


def prove_deb_off_against_tof(slot: Any) -> None:
    rung, deb = c[245], c[246]
    tof = ClickTimer(124)
    count_scans(slot, OFF_WINDOW)
    with Rung(slot.Cmd != 0, Or(slot.Scratch == 2, slot.Scratch == 3)):
        out(rung)
    with Rung(rung):
        debounce(deb, on=(ClickTimer(122), 0), off=(ClickTimer(123), 5))
    with Rung(rung):
        off_delay(tof, 5)
    for bit, field in ((deb, slot.Result1), (tof.Done, slot.Result2)):
        with Rung(slot.Cmd != 0, slot.Scratch >= 4, slot.Scratch <= OFF_WINDOW, ~bit, field == 0):
            copy(slot.Scratch, field)
    with Rung(slot.Cmd != 0, slot.Scratch == OFF_WINDOW + 1, slot.Fired == 0):
        math(slot.Result1 - slot.Result2, slot.ErrorCode)
    done(slot, OFF_WINDOW)


group = Group(
    "coil_timing",
    [
        case(
            "A debounce's off TD goes to 0 while its rung is true (Result2 = 2, 1 if it keeps "
            "its value) and times from 0 at a fall, under 100 on the scan of the fall (Result3 "
            "= 1, 0 if it resumes), so the coil stays on through 100 ms of a 500 ms off (Result1 "
            "= 1, 0 if it drops)",
            ladder=prove_off_td_held,
            ready=lambda slot: ~c[242],
            # The off TD is timed out in the reset: what the case starts from, on every repeat.
            # The on TD is kept after the rung falls, as for any debounce; the next rise starts it from 0.
            keep=(td[115], td[116]),
            expect={"Result1": 1, "Result2": 2, "Result3": 1},
            scans=40,
            fires_within=1.0,
        ),
        case(
            "A debounce's off side goes off a scan after a TOF of its preset: the debounce "
            "(Result1), the TOF (Result2), the debounce's lag (ErrorCode = 1, 0 on one scan)",
            ladder=prove_deb_off_against_tof,
            ready=lambda slot: (~c[246], ~t[124]),
            # No init writes these TDs: the TOF's would hold its Done on (see the docstring).
            keep=(td[122], td[123], td[124]),
            expect={
                "Result1": range(5, OFF_WINDOW + 1),
                "Result2": range(4, OFF_WINDOW + 1),
                "ErrorCode": 1,
            },
            scans=scans_for(OFF_WINDOW),
        ),
    ],
)


def test_coil_timing():
    assert_all_passed(run(group))


def test_pyrung_reads_the_debounce_lag():
    """What pyrung reads at the in-process 10 ms scan."""
    read = {r.case.id: r.actual for r in run(group)}
    assert read == {
        "off_td_held": {"Result1": 1, "Result2": 2, "Result3": 1},
        "deb_off_against_tof": {"Result1": 5, "Result2": 4, "ErrorCode": 1},
    }


@pytest.mark.parametrize("dt", [0.0004, 0.001, 0.010, 0.025, 0.040])
def test_off_td_held_at_any_scan(monkeypatch, dt):
    """The CLICK scans in about 1 ms or less, the soft PLC in 10..40 ms."""
    monkeypatch.setattr(runner_module, "DT", dt)
    held = next(c for c in group.cases if c.id == "off_td_held")
    assert_all_passed(run(Group("coil_timing", [replace(held, scans=int(0.5 / dt))])))
