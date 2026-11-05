"""A For whose count is a DD holding -1: the ``for_dd_minus`` group (part ``follow_values``).

**Run it alone and last, with the watchdog raised: on a CLICK it may stop the PLC.**  A DS count of
-1 runs 65535 times (``loop/negative_count_reads_unsigned``, about 1.1 s a scan), and a DD count
is read whole (``for_wide/count_forms`` ran 65537 and 65536).  Whole and unsigned, -1 is
4294967295 runs, about 20 hours, so the scan watchdog stops the PLC, as ``for_count``'s Real count
did (error 0107, hardware 2026-11-01).  The DD count fix (``boot-click``) waits on this.

The loop runs only on a scan that C402 is on, and C402 is latched from C401, which is set by hand:
as its ``Cmd`` comes up the case latches C402, writes -1 into the count DD81 and turns C401 off.
With C402 off a rung of its own writes 0 into DD81 on every scan, so pasting the part and running
its other groups is harmless, and so is a run of this group with C401 off (Result1 = 0, which
answers nothing).  C402 is not retentive (a new project's default), so a STOP -> RUN after a
watchdog stop clears it and the loop cannot run again, though ``Cmd`` stays up.  In-process and on
the local targets nothing sets C401, so the loop never runs: the case stays bounded whatever count
pyrung reads from a DD.

To ask it:

1. Run every other group of the part first.
2. A new project's watchdog is 200 ms, which 65535 runs (about 1.1 s) would trip as well.  Set
   the watchdog to 10000 ms in Setup before arming C401, and write the project.  Then 65535 runs
   finish, and only a whole unsigned count (about 73000 s at about 17 us a run) trips it.
3. Set C401 on in the Data View, and run ``make test-conformance PLC=... GROUP=for_dd_minus``.
4. Set the watchdog back to 200 ms after.

What each reading means:

- Result1 = 1: C401 was on, the loop ran.  0: it was not armed, and the rest answers nothing.
- Result2 = 1 (DD82 = 0): a DD count of -1 runs none, as a signed count.
- Result2 = 2: it runs 65535, the low word, unsigned (about 1.1 s, under the 10 s watchdog).
- Result2 = 3: it runs once.
- Result2 = 0: another count, finished under 10 s: Result3 is DD82 / 1000.
- The PLC stops on the watchdog, and the run times out after 15 s (``fires_within``): the count is
  read whole and unsigned, 4294967295, as pyrung reads it with ``boot-click``.  Read by hand the
  error the CLICK software shows (0107 for the watchdog), SD12 (the longest scan) and DD82, the runs
  made in the 10 s before the stop, and write them down: no case records them.  To recover, clear
  the error and put the PLC back in RUN (or cycle its power): C402 is off again, so the loop does
  not run and the rung under it writes 0 into DD81.  The next run's reset clears DD82 and the slot.

The cases own what ``memory.OWNED_APART`` lists under this group.
"""

from __future__ import annotations

from typing import Any

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import Rung, calc, copy, fill, forloop, latch, reset
from tests.conformance.memory import c, dd
from tests.conformance.probe_kit import between, class_of, finish, once

ENABLE, ARMED = c[401], c[402]
COUNT, RUNS = dd[81], dd[82]


def prove_minus_one_count(slot: Any) -> None:
    with Rung(~ARMED):
        copy(0, COUNT)
    with Rung(*once(slot), ENABLE):
        latch(ARMED)
        copy(-1, COUNT)
        copy(1, slot.Result1)
        reset(ENABLE)
    with Rung(*once(slot), ARMED):
        with forloop(COUNT):
            calc(RUNS + 1, RUNS)
    with Rung(ARMED):
        reset(ARMED)
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
            "RUN ALONE, LAST, WITH THE WATCHDOG AT 10000 MS AND C401 SET BY HAND: it may stop the PLC.  "
            "A For over a DD count of -1 (Result1 = 1: armed) runs 4294967295 and stops the PLC "
            "(read the error, SD12 and DD82 by hand), none (Result2 = 1), the low word 65535 (2), "
            "or once (3)",
            ladder=prove_minus_one_count,
            init=init_minus_one_count,
            expect={
                "Result1": between(0, 1),
                "Result2": between(0, 3),
                "Result3": between(0, 32767),
            },
            probe="a For over a DD of -1: the watchdog, none, or the low word (boot-click)",
            # Past a 1.1 s scan of 65535 runs, and past the 10000 ms watchdog it is run with.
            fires_within=15.0,
        ),
    ],
)


def test_for_dd_minus():
    assert_all_passed(run(group))
