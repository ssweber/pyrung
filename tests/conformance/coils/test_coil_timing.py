"""Test a debounce's off side: the ``coil_timing`` group (part ``follow_timing``).

The group has two cases on a debounce's off TD, beside the cases in ``deb_zero``.

- ``off_td_held`` asks whether a debounce's off TD keeps its value while the rung is true. The
  debounce is 0 / 500 ms, and the steps are numbered in ``Scratch``. The reset leaves the off TD
  timed out (500 ms or more), and the case starts from that state. A clock, a TON on the
  debounce's rung, times each spell that the rung is on, from 0. The rungs for the steps come
  below the debounce in reverse order, so each step starts on the next scan.

  - Step 0: the rung is on until the clock reads 50.
  - Step 1: the rung falls on this scan. Result3 is 1 if the off TD is under 100, and 0 if not.
  - Step 2: the rung stays off until the off TD reads 100 or more. Result1 is 1 if the coil is
    still on then, and 0 if it is off.
  - Step 3: the rung is on again. Once the clock reads 50, Result2 is 1 if the off TD is still
    100 or more, and 2 if it is 0. ``Fired`` goes up on that scan.

  The readings (Result1, Result2, Result3) can be one of three:

  - 1, 2, 1: the rung's rise clears the off TD, and the fall times from 0.
  - 1, 1, 1: the off TD keeps its value while the rung is true, as the on side does, and a fall
    starts it from 0.
  - 0, 1, 0: the off TD keeps its value while the rung is true, and a fall resumes it. It is past
    its preset at once, and the coil drops on the scan after the fall.

  A CLICK reads the first of these, so the off TD is under 100 on the scan of the fall
  (``deb_fall/off_td_at_fall``, 1..2).
- ``deb_off_against_tof`` puts a debounce of 0 / 5 ms and a TOF of 5 ms on one rung, and uses
  scans 2 and 3. Result1 is the scan on which the debounce goes off, and Result2 is the scan on
  which the TOF goes off. Each is at most 40, and 0 means it never went off.
  ErrorCode = Result1 - Result2. It is 1 when the debounce's off side takes one scan longer than
  a TOF of the same preset, as ``zero_and_below`` reads at 0. It is 0 when they agree.

  A CLICK reads 6, 5, 1, so the debounce is one scan later. pyrung reads 5, 4, 1 at the
  in-process 10 ms scan. A CLICK scans faster, so its off comes later. For that reason the scans
  are ranges and ErrorCode is exact.

  The reset writes none of the three TDs. A TOF whose Acc is zeroed while its rung is false keeps
  its Done on (``timers/tof_times_only_when_false``). A reset that zeroed them would then never
  see ``ready``, which is the TOF's Done off. No case needs that write. The rung's rise clears
  the debounce's on TD and the TOF's, and holds the debounce's off TD at 0. The case lists the
  three TDs in ``keep`` only because they move after it fires.

``test_pyrung_reads_the_debounce_lag`` pins the readings that pyrung takes in-process. The cases
own what ``memory.OWNED_APART`` lists under this group.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.conformance import _runner as runner_module
from pyrung.core import Or, copy, debounce, math, off_delay, on_delay, out, rung
from tests.conformance.memory import ClickTimer, c, t, td
from tests.conformance.scans import count_scans, done, scans_for

OFF_WINDOW = 40


def prove_off_td_held(slot: Any) -> None:
    deb = c[242]
    off_td = td[116]
    rung_conds = (slot.Cmd != 0, Or(slot.Scratch == 0, slot.Scratch >= 3))

    def step(n: int) -> tuple[Any, Any]:
        return (slot.Cmd != 0, slot.Scratch == n)

    with rung(*rung_conds):
        debounce(deb, on=(ClickTimer(115), 0), off=(ClickTimer(116), 500))
    # The clock times the rung's time on, from 0 at each rise.
    with rung(*rung_conds):
        on_delay(ClickTimer(117), 5000)
    clock = td[117]
    with rung(*step(3), clock >= 50, off_td >= 100):
        copy(1, slot.Result2)
    with rung(*step(3), clock >= 50, off_td == 0):
        copy(2, slot.Result2)
    with rung(*step(3), clock >= 50):
        copy(4, slot.Scratch)
        copy(1, slot.Fired)
    with rung(*step(2), off_td >= 100, deb):
        copy(1, slot.Result1)
    with rung(*step(2), off_td >= 100):
        copy(3, slot.Scratch)
    with rung(*step(1), off_td < 100):
        copy(1, slot.Result3)
    with rung(*step(1)):
        copy(2, slot.Scratch)
    with rung(*step(0), clock >= 50):
        copy(1, slot.Scratch)


def prove_deb_off_against_tof(slot: Any) -> None:
    rung_bit, deb = c[245], c[246]
    tof = ClickTimer(124)
    count_scans(slot, OFF_WINDOW)
    with rung(slot.Cmd != 0, Or(slot.Scratch == 2, slot.Scratch == 3)):
        out(rung_bit)
    with rung(rung_bit):
        debounce(deb, on=(ClickTimer(122), 0), off=(ClickTimer(123), 5))
    with rung(rung_bit):
        off_delay(tof, 5)
    for bit, field in ((deb, slot.Result1), (tof.Done, slot.Result2)):
        with rung(slot.Cmd != 0, slot.Scratch >= 4, slot.Scratch <= OFF_WINDOW, ~bit, field == 0):
            copy(slot.Scratch, field)
    with rung(slot.Cmd != 0, slot.Scratch == OFF_WINDOW + 1, slot.Fired == 0):
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
            # The reset leaves the off TD timed out, and the case starts from that on every repeat.
            # The on TD is kept after the rung falls, as for any debounce. The next rise starts
            # it from 0.
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
            # No init writes these TDs. Writing the TOF's would hold its Done on (see the docstring).
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
    """Pin what pyrung reads at the in-process 10 ms scan."""
    read = {r.case.id: r.actual for r in run(group)}
    assert read == {
        "off_td_held": {"Result1": 1, "Result2": 2, "Result3": 1},
        "deb_off_against_tof": {"Result1": 5, "Result2": 4, "ErrorCode": 1},
    }


@pytest.mark.parametrize("dt", [0.0004, 0.001, 0.010, 0.025, 0.040])
def test_off_td_held_at_any_scan(monkeypatch, dt):
    """Check the case at scan times from 0.4 to 40 ms.

    A CLICK scans in about 1 ms or less, and the soft PLC in 10 to 40 ms.
    """
    monkeypatch.setattr(runner_module, "DT", dt)
    held = next(c for c in group.cases if c.id == "off_td_held")
    assert_all_passed(run(Group("coil_timing", [replace(held, scans=int(0.5 / dt))])))
