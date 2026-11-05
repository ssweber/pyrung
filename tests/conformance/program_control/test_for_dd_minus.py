"""A For whose count is a DD holding -1: the ``for_dd_minus`` group (part ``follow_values``).

**Run it alone and last: on a CLICK it may stop the PLC.**  A DS count of -1 runs 65535 times
(``loop/negative_count_reads_unsigned``, about 1.1 s a scan), and a DD count is read whole
(``for_wide/count_forms`` ran 65537 and 65536).  Whole and unsigned, -1 is 4294967295 runs: about
20 hours, so the scan watchdog stops the PLC, as ``for_count``'s Real count did (error 0107 after
10 s, hardware 2026-11-01).  The DD count fix (``boot-click``) waits on this.

Its loop runs only when C401 is on as its ``Cmd`` comes up, and C401 is set by hand: the case
writes -1 into the count DD only then, and turns C401 off as it does.  With C401 off the count is
0, which runs none (``loop/zero_count``), so pasting the part and running its other groups is
harmless, and so is a run of this group with C401 off (Result1 = 0, which answers nothing).
In-process and on the local targets nothing sets C401, so the loop never runs: the case stays
bounded whatever count pyrung reads from a DD.

To ask it: run every other group of the part first.  Then set C401 on in the CLICK software's Data
View, and run ``make test-conformance PLC=... GROUP=for_dd_minus``.

- The PLC stops: the run times out after 15 s (``fires_within``).  Read by hand the error the CLICK software shows (0107 for the
  watchdog), SD12 (the longest scan) and DD82, the runs counted before the stop.  Clear the error
  and put the PLC back in RUN (or cycle its power); C401, not retentive, is off again.  Write the
  reading down: no case records it.
- It runs none: Result1 = 1, Result2 = 1 (DD82 = 0).
- It runs the low word, 65535 (about 1.1 s): Result2 = 2.
- It runs once: Result2 = 3.
- Another count: Result2 = 0, and Result3 is DD82 / 1000.

The cases own what ``memory.OWNED_APART`` lists under this group.
"""

from __future__ import annotations

from typing import Any

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import Rung, calc, copy, fill, forloop, reset
from tests.conformance.memory import c, dd
from tests.conformance.probe_kit import between, class_of, finish, once

ENABLE = c[401]
COUNT, RUNS = dd[81], dd[82]


def prove_minus_one_count(slot: Any) -> None:
    with Rung(*once(slot), ENABLE):
        copy(-1, COUNT)
        copy(1, slot.Result1)
        reset(ENABLE)
    with Rung(*once(slot)):
        with forloop(COUNT):
            calc(RUNS + 1, RUNS)
    class_of(slot, RUNS, [0, 65535, 1], slot.Result2)
    with Rung(*once(slot)):
        calc(RUNS / 1000, slot.Result3)
    finish(slot)


def init_minus_one_count(slot: Any) -> None:
    fill(0, dd.select(81, 82))


group = Group(
    "for_dd_minus",
    [
        case(
            "RUN ALONE, LAST, WITH C401 SET BY HAND: it may stop the PLC on its watchdog.  A For "
            "over a DD count of -1 (Result1 = 1: C401 was on) stops the PLC (read the error, SD12 "
            "and DD82 by hand), runs none (Result2 = 1), the low word 65535 (2), or once (3)",
            ladder=prove_minus_one_count,
            init=init_minus_one_count,
            expect={
                "Result1": between(0, 1),
                "Result2": between(0, 3),
                "Result3": between(0, 32767),
            },
            probe="a For over a DD of -1: the watchdog, none, or the low word (boot-click)",
            # Past a 1.1 s scan of 65535 runs, and past the 10 s watchdog.
            fires_within=15.0,
        ),
    ],
)


def test_for_dd_minus():
    assert_all_passed(run(group))
