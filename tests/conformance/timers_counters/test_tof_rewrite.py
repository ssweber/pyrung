"""A timed-out TOF whose TD the ladder writes to 0: the ``tof_rewrite`` group (part ``follow2_coils``).

``done_after_td_zeroed`` (T1): a TOF of 200 ms on a rung true for the first 50 ms of a TON's ms
clock (T292), false after.  By 300 ms at the latest it has timed out.  From 400 to 450 ms Result1
is its Done (0), and Result4 its TD, past 200.  From 450 to 500 ms a Copy below the TOF writes its
TD to 0 on every scan.  From 500 to 550 ms, on the rungs after the TOF, Result2 is Done (1 on) and
Result3 the TD.  Each window holds a scan at any scan up to the soft PLC's 40 ms cap.  The last
scan in a window writes the value it keeps.

A CLICK read an earlier ladder of 20 ms on scan counts 0, 1, 1..2, 33..34 (2026-11-10, twice).
Done came back on and the TD counted from 0.  So a TOF that has been enabled judges
``TD < preset`` on every scan its rung is false, and a TD written to 0 starts its delay again
(``OffDelayInstruction``).  A TOF never enabled still rests at Done off, TD 0
(``timers/tof_never_enabled``).  pyrung read 0, 0, 0, 280 before, because it kept a timed-out TOF
at rest once its TD read 0.  That ladder read the TD one scan after the write.  The soft PLC's
20 ms scan then reached the 20 ms preset and left Done off, so the windows here are in ms.

Result3 and Result4 are milliseconds, so they are ranges.  Result3 is the time from the last
write (460..500 ms) to the last read (510..550 ms), so 10..90 ms.  Result4 is the time from the last
scan of the rung (10..50 ms) to the last read (410..450 ms), so 360..440 ms.  The TOF counts its
rung's fall, as the CLICK's 33..34 read.  pyrung reads 50 and 400 at the in-process 10 ms scan.

The case's reset depends on nothing the TOF does: its rung is false through the reset, and its
Done and TD are kept (a TOF times on, and its Done may come back on, after the case).  The case owns
what ``memory.OWNED_APART`` lists under this group.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.conformance import _runner as runner_module
from pyrung.core import Rung, copy, off_delay
from tests.conformance.memory import ClickTimer
from tests.conformance.probe_kit import end_at, in_window, ms_clock

TOF = ClickTimer(291)


def prove_done_after_td_zeroed(slot: Any) -> None:
    clock = ms_clock(slot, 292)
    with Rung(slot.Cmd != 0, clock < 50):
        off_delay(TOF, 200)
    with Rung(*in_window(slot, clock, 400, 450)):
        copy(TOF.Acc, slot.Result4)
    with Rung(*in_window(slot, clock, 400, 450), TOF.Done):
        copy(1, slot.Result1)
    with Rung(*in_window(slot, clock, 500, 550)):
        copy(TOF.Acc, slot.Result3)
    with Rung(*in_window(slot, clock, 500, 550), TOF.Done):
        copy(1, slot.Result2)
    with Rung(*in_window(slot, clock, 450, 500)):
        copy(0, TOF.Acc)
    end_at(slot, clock, 600)


group = Group(
    "tof_rewrite",
    [
        case(
            "A timed-out TOF (Result1 = 0, Result4 its TD, past its 200 ms preset) whose TD the "
            "ladder writes to 0 turns Done back on (Result2 = 1), the TD counting from 0 (Result3, "
            "under the preset)",
            ladder=prove_done_after_td_zeroed,
            keep=(TOF.Done, TOF.Acc),
            expect={
                "Result1": 0,
                "Result2": 1,
                # 10..90 ms and 360..440 ms, 1 ms either side for the timers' remainders.
                "Result3": range(9, 92),
                "Result4": range(359, 442),
            },
            scans=100,
        ),
    ],
)


def test_tof_rewrite():
    assert_all_passed(run(group))


@pytest.mark.parametrize("dt", [0.001, 0.020, 0.023, 0.030, 0.040])
def test_tof_rewrite_at_any_scan(monkeypatch, dt):
    """The CLICK scans in about 1 ms, the soft PLC in 20..40 ms under load."""
    monkeypatch.setattr(runner_module, "DT", dt)
    assert_all_passed(run(Group("tof_rewrite", [replace(group.cases[0], scans=int(0.7 / dt))])))
