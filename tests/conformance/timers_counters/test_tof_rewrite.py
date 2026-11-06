"""A timed-out TOF whose TD the ladder writes to 0: the ``tof_rewrite`` group (part ``follow2_coils``).

``coil_timing/deb_off_against_tof`` hung in its reset on a CLICK: its init wrote 0 into a TOF's TD
on every reset scan, and its ``ready`` waited for the TOF's Done to be off, which it never was
again.  That says a CLICK TOF judges ``Acc < preset`` from its TD on every scan its rung is false,
so a TD written to 0 turns Done back on; pyrung keeps Done off once the TOF has timed out.  No case
has read it straight.

``done_after_td_zeroed`` (T1): a TOF of 20 ms on a rung true on scans 1 and 2, false after.  By
scan 30 it has timed out (Result1, Done then: 0, the control; Result4 its TD, past 20).  On scan
31 a Copy below the TOF writes its TD to 0, and on scan 32, on the rung after the TOF, Result2 is
Done (1 on) and Result3 the TD:

- 1 and about a scan's ms: Done comes back on, judged from the TD (CLICK, from the hang).
- 0 and about a scan's ms: Done stays off, the TD counting on.
- 0 and 0: Done stays off and the TD stands at 0 (pyrung reads 0, 0, 0 and 280: its TOF counts
  on past its preset until the write, and not after it).
- 0 and the TD of scan 30 or more: the write is undone.

The case's reset depends on nothing the TOF does: its rung is false through the reset, and its
Done and TD are kept (a TOF times on, and its Done may come back on, after the case).  The case owns
what ``memory.OWNED_APART`` lists under this group.
"""

from __future__ import annotations

from typing import Any

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import Rung, copy, off_delay
from tests.conformance.memory import ClickTimer
from tests.conformance.probe_kit import between
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
            "A timed-out TOF (Result1 = 0, Result4 its TD) whose TD the ladder writes to 0 turns "
            "Done back on on its next scan (Result2 = 1, 0 it stays off), the TD counting from 0 "
            "(Result3)",
            ladder=prove_done_after_td_zeroed,
            keep=(TOF.Done, TOF.Acc),
            expect={
                "Result1": 0,
                "Result2": between(0, 1),
                "Result3": between(0, 32767),
                "Result4": between(0, 32767),
            },
            scans=scans_for(WINDOW),
            probe="a TOF's TD written to 0 after it timed out: Done on again or not (5)",
        ),
    ],
)


def test_tof_rewrite():
    assert_all_passed(run(group))
