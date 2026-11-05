"""A debounce of 0/0, scan by scan: the ``deb_zero`` group (part ``follow_timing``).

``out_more/zero_and_below`` read the first off of a 0/0 debounce at scan 7 on a CLICK, a scan after
its rung's 6, where pyrung reads 6.  One reading cannot say whether the off side always takes a
scan more (the off delay times a scan before it is done, even at 0) or that reading was a scan's
lag of the case.  The fix step ``timed-coils`` (5 in the ``conform-fix`` arc) waits on this.

``off_side_lag``: one rung, an Out on scans 3, 4, 5, 7, 8 and 11 (a one-scan dropout at 6, a
two-scan one at 9 and 10, a one-scan pulse at 11), drives a 0/0 debounce, a TON of 0 and a TOF of
0.  Each field is a mask of the scans its bit is on, read below them: scan 2 is 4096, scan 3 2048,
..., scan 14 is 1.

- ErrorCode, the rung's own coil, is the control: 3784 (scans 3, 4, 5, 7, 8, 11).
- Result1, the debounce.  Following its rung, 3784 (pyrung).  An off side a scan late: 4076
  (scans 3..9, 11, 12), the dropout at 6 filtered.
- Result2, the TON of 0: 3784 when it is done on its rung's first scan (pyrung), 1600 a scan late
  (scans 4, 5, 8).
- Result3, the TOF of 0: 3784 when it is off on its rung's first false scan (pyrung), 4076 a scan
  late.

The cases own what ``memory.OWNED_APART`` lists under this group.
"""

from __future__ import annotations

from typing import Any

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import Or, Rung, calc, copy, debounce, off_delay, on_delay, out
from tests.conformance.memory import ClickTimer, c, t, td
from tests.conformance.probe_kit import between
from tests.conformance.scans import count_scans, done, scans_for

WINDOW = 14
ON_SCANS = (3, 4, 5, 7, 8, 11)
DEB_ON, DEB_OFF, TON, TOF = (ClickTimer(n) for n in range(111, 115))


def prove_off_side_lag(slot: Any) -> None:
    rung, deb = c[221], c[222]
    count_scans(slot, WINDOW)
    with Rung(slot.Cmd != 0, Or(*[slot.Scratch == n for n in ON_SCANS])):
        out(rung)
    with Rung(rung):
        debounce(deb, on=(DEB_ON, 0), off=(DEB_OFF, 0))
    with Rung(rung):
        on_delay(TON, 0)
    with Rung(rung):
        off_delay(TOF, 0)
    fields = (slot.Result1, slot.Result2, slot.Result3, slot.ErrorCode)
    with Rung(slot.Cmd != 0, slot.Scratch >= 2, slot.Scratch <= WINDOW):
        for field in fields:
            calc(field * 2, field)
    for bit, field in zip((deb, TON.Done, TOF.Done, rung), fields, strict=True):
        with Rung(slot.Cmd != 0, slot.Scratch >= 2, slot.Scratch <= WINDOW, bit):
            calc(field + 1, field)
    done(slot, WINDOW)


def init_off_side_lag(slot: Any) -> None:
    for timer in (DEB_ON, DEB_OFF, TON, TOF):
        copy(0, timer.Acc)


group = Group(
    "deb_zero",
    [
        case(
            "A debounce of 0/0 follows its rung scan by scan, a one-scan dropout too "
            "(Result1 = 3784, 4076 for an off side a scan late), as a TON of 0 (Result2) and a "
            "TOF of 0 (Result3) do; the rung's own coil is the control (ErrorCode = 3784)",
            ladder=prove_off_side_lag,
            init=init_off_side_lag,
            ready=lambda slot: (~c[222], ~t[114]),
            expect={
                "Result1": between(0, 8191),
                "Result2": between(0, 8191),
                "Result3": between(0, 8191),
                "ErrorCode": 3784,
            },
            scans=scans_for(WINDOW),
            probe="the off side of a 0/0 debounce: a scan late or not (5)",
        ),
    ],
)


def test_deb_zero():
    assert_all_passed(run(group))
