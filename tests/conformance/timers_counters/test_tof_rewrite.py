"""A timed-out TOF whose TD the ladder writes to 0: the ``tof_rewrite`` group (part ``follow2_coils``).

``done_after_td_zeroed`` (T1): a TOF of 20 ms on a rung true on scans 1 and 2, false after.  By
scan 30 it has timed out (Result1, Done then: 0; Result4 its TD, past 20).  On scan 31 a Copy
below the TOF writes its TD to 0, and on scan 32, on the rung after the TOF, Result2 is Done (1
on) and Result3 the TD.

A CLICK read 0, 1, 1..2, 33..34 (2026-11-10, twice).  Done comes back on and the TD counts from
0.  So a TOF that has been enabled judges ``TD < preset`` on every scan its rung is false, and a
TD written to 0 starts its delay again (``OffDelayInstruction``).  A TOF never enabled still
rests at Done off, TD 0 (``timers/tof_never_enabled``).  pyrung read 0, 0, 0, 280 before: it kept
a timed-out TOF at rest once its TD read 0.

Result3 and Result4 are milliseconds, so they follow the target's scan and are ranges.  Result3
is a scan's tick from 0, under the 20 ms preset (Done on says so).  Result4 is past the preset.
pyrung reads 10 and 280 at the in-process 10 ms scan.

The case's reset depends on nothing the TOF does: its rung is false through the reset, and its
Done and TD are kept (a TOF times on, and its Done may come back on, after the case).  The case owns
what ``memory.OWNED_APART`` lists under this group.
"""

from __future__ import annotations

from typing import Any

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import Rung, copy, off_delay
from tests.conformance.memory import ClickTimer
from tests.conformance.scans import count_scans, done, scans_for

WINDOW = 34
TIMED_OUT, ZEROED, READ = 30, 31, 32
TOF = ClickTimer(291)


def prove_done_after_td_zeroed(slot: Any) -> None:
    count_scans(slot, WINDOW)
    with Rung(slot.Cmd != 0, slot.Scratch <= 2):
        off_delay(TOF, 20)
    with Rung(slot.Cmd != 0, slot.Scratch == TIMED_OUT):
        copy(TOF.Acc, slot.Result4)
    with Rung(slot.Cmd != 0, slot.Scratch == TIMED_OUT, TOF.Done):
        copy(1, slot.Result1)
    with Rung(slot.Cmd != 0, slot.Scratch == READ):
        copy(TOF.Acc, slot.Result3)
    with Rung(slot.Cmd != 0, slot.Scratch == READ, TOF.Done):
        copy(1, slot.Result2)
    with Rung(slot.Cmd != 0, slot.Scratch == ZEROED):
        copy(0, TOF.Acc)
    done(slot, WINDOW)


group = Group(
    "tof_rewrite",
    [
        case(
            "A timed-out TOF (Result1 = 0, Result4 its TD, past its 20 ms preset) whose TD the "
            "ladder writes to 0 turns Done back on on its next scan (Result2 = 1), the TD "
            "counting from 0 (Result3, a scan's ms)",
            ladder=prove_done_after_td_zeroed,
            keep=(TOF.Done, TOF.Acc),
            expect={
                "Result1": 0,
                "Result2": 1,
                "Result3": range(1, 20),
                "Result4": range(20, 32768),
            },
            scans=scans_for(WINDOW),
        ),
    ],
)


def test_tof_rewrite():
    assert_all_passed(run(group))
