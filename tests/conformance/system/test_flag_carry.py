"""SC40 and SC46 across a scan boundary, with nothing between: the ``flag_carry`` group.

Part ``follow2_carry``, the group alone, so that nothing but its own rungs and main's two runs
between its op and its reads.

``flag_scan/math_flags_over_a_scan`` (part ``boot``) read SC40 and SC46 off on the scan after
their op, but the op was not the last Math of its scan and the reads were not the first rungs of
the next.  A false-rung Math sat below the division, and main called ``boot_read``, ``rtc_set``
and ``for_wide`` before the reads.  Their rungs hold Maths true and false, and a false-rung Math
clears the flags (``flag_steps``).  A true Math ran right after the root too.  So that case cannot
tell a flag cleared at the scan's start from one cleared by a later Math.

``math_flags_carry``: one Math, ``SQRT(DF) + 1 / DS`` into a DF, on scans 3 and 5 only, the last
Math rung of the program; below it only Copies.  On scan 3 the DF holds 1.0 and the DS 0, so it
raises SC40 alone; on scan 5 the DF holds -1.0 and the DS 1, so it raises SC46 alone.  The reads
are the case's first rungs, Copies above its scan count, so on the next scan only main's Copy of
the layout hash, the call and the reset rung's Fill and Copies (on a false rung) run before them.

- Result1 = 1: SC40 is still on at the top of the scan after.  0 would be a flag cleared at the
  scan's start, or by a false Copy or Fill.
- Result2 = 1: SC46 is still on at the top of the scan after.
- Result3 and Result4 are the controls, SC40 and SC46 read by Copies right after the op on its own
  scan: 1, 1.  A 0 there is an op that did not raise its flag, and the reads above it say nothing.

The CLICK read 1, 1, 1, 1 on 2026-11-10.  A flag carries into the next scan, and nothing clears it
as the scan starts.  The case owns what ``memory.OWNED_APART`` lists under this group.
"""

from __future__ import annotations

from typing import Any

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import Or, Rung, copy, math, system
from pyrung.core.expression import sqrt
from tests.conformance.memory import df, ds
from tests.conformance.scans import count_scans, done, scans_for

SC40 = system.fault.division_error
SC46 = system.fault.math_operation_error

#: The op's scans: SC40's, then SC46's.
DIVIDE, ROOT = 3, 5
WINDOW = 7


def prove_math_flags_carry(slot: Any) -> None:
    root, sink, divisor = df[61], df[62], ds[4001]
    # The reads first, above the case's own count: Scratch still holds the scan before.
    with Rung(slot.Cmd != 0, slot.Scratch == DIVIDE, SC40):
        copy(1, slot.Result1)
    with Rung(slot.Cmd != 0, slot.Scratch == ROOT, SC46):
        copy(1, slot.Result2)
    count_scans(slot, WINDOW)
    with Rung(slot.Cmd != 0, slot.Scratch == ROOT - 1):
        copy(-1.0, root)
        copy(1, divisor)
    with Rung(slot.Cmd != 0, Or(slot.Scratch == DIVIDE, slot.Scratch == ROOT)):
        math(sqrt(root) + 1 / divisor, sink)
    with Rung(slot.Cmd != 0, slot.Scratch == DIVIDE, SC40):
        copy(1, slot.Result3)
    with Rung(slot.Cmd != 0, slot.Scratch == ROOT, SC46):
        copy(1, slot.Result4)
    done(slot, WINDOW)


def init_math_flags_carry(slot: Any) -> None:
    copy(1.0, df[61])
    copy(0.0, df[62])
    copy(0, ds[4001])


group = Group(
    "flag_carry",
    [
        case(
            "SC40 (Result1) and SC46 (Result2) raised by the scan's last Math are still on at the "
            "top of the next scan (1).  Each is on right after the op (Result3 = 1, Result4 = 1)",
            ladder=prove_math_flags_carry,
            init=init_math_flags_carry,
            expect={
                "Result1": 1,
                "Result2": 1,
                "Result3": 1,
                "Result4": 1,
            },
            scans=scans_for(WINDOW),
        ),
    ],
)


def test_flag_carry():
    assert_all_passed(run(group))
