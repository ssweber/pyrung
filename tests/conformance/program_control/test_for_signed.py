"""For counts read signed, by the low word, or whole: the ``for_signed`` group (part ``follow2_for``).

**Run it alone, with the watchdog at 10000 ms: two of its scans take over a second on a CLICK.**
A CLICK reads a DS count through its low 16 bits, unsigned.  It reads a DD count, a CTD count and a
constant whole, and a DD or CTD count signed (hardware, 2026-11-10, read once, SD12 1287 ms after
the run).  So a DS of -1 runs 65535, a constant 70000 runs 70000, and a negative DD or CTD runs
none.  ``for_dd_minus`` had read a DD of -1 run none, and ``for_wide/count_forms`` a DD of 65536
run 65536.

One count a scan, each For on a scan of its own, so that no scan makes more than about 1.2 s of
runs (about 17 us a run).  Each field reads 1 for the CLICK's count and 2 for the other model's:

- scan 3, Result1, a DS of -1, the control: 1 for 65535 runs, 2 for none.
- scan 4, Result2, a constant 70000: 1 for 70000 runs, whole; 2 for 4464, the low 16 bits.
- scan 5, Result3, a DD of -65535 (0xFFFF0001): 1 for none, signed; 2 for 1, the low word.
- scan 6, Result4, a DD of -2: 1 for none, signed; 2 for 65534, the low word.
- scan 7, ErrorCode, a CTD of -1 (a down counter counted once from 0 on scan 2): 1 for none,
  signed; 2 for 65535, the low word.

Each reading is 0 where the For did not run (the PLC stopped before that scan) and 3 for another
count of runs, which its DD holds (DD143..DD147, in the order above) until the next reset.  A count
read whole and unsigned (4294901761, 4294967294 and 4294967295 runs) would trip the watchdog and
stop the PLC, and the run would time out after 20 s (``fires_within``).

To run it on a CLICK:

1. A new project's watchdog is 200 ms, which 65535 runs trip.  Set it to 10000 ms in Setup, and
   write the project.
2. Run ``make test-conformance PLC=... GROUP=for_signed``.
3. Set the watchdog back to 200 ms after.

The cases own what ``memory.OWNED_APART`` lists under this group.
"""

from __future__ import annotations

from typing import Any

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import Rung, copy, count_down, fill, forloop, math
from tests.conformance.memory import ClickCounter, dd, ds
from tests.conformance.scans import count_scans, done, scans_for

WINDOW = 8
DS_COUNT, DOWN_PRESET = ds[4051], ds[4052]
DD_LOW, DD_MINUS_TWO = dd[141], dd[142]
DOWN = ClickCounter(6)
#: The runs of each For, in the order of the scans: DD143..DD147.
RUNS = tuple(dd[n] for n in range(143, 148))
COUNT_SCAN = 2


def prove_minus_counts(slot: Any) -> None:
    fields = (slot.Result1, slot.Result2, slot.Result3, slot.Result4, slot.ErrorCode)
    loops = (
        (DS_COUNT, (65535, 0)),
        (70000, (70000, 4464)),
        (DD_LOW, (0, 1)),
        (DD_MINUS_TWO, (0, 65534)),
        (DOWN.Acc, (0, 65535)),
    )
    count_scans(slot, WINDOW)
    with Rung(slot.Cmd != 0, slot.Scratch == 1):
        copy(-1, DS_COUNT)
        copy(-65535, DD_LOW)
        copy(-2, DD_MINUS_TWO)
    with Rung(slot.Cmd != 0, slot.Scratch == COUNT_SCAN):
        count_down(DOWN, DOWN_PRESET).reset(slot.Cmd == 0)
    for scan, ((count, readings), runs, field) in enumerate(
        zip(loops, RUNS, fields, strict=True), start=COUNT_SCAN + 1
    ):
        on_scan = (slot.Cmd != 0, slot.Scratch == scan)
        with Rung(*on_scan):
            with forloop(count):
                math(runs + 1, runs)
        with Rung(*on_scan):
            copy(3, field)
        for number, reading in enumerate(readings, start=1):
            with Rung(*on_scan, runs == reading):
                copy(number, field)
    done(slot, WINDOW)


def init_minus_counts(slot: Any) -> None:
    copy(0, DS_COUNT)
    fill(0, dd.select(141, 147))
    # The down counter's preset, below the -1 it is counted to.
    copy(-5, DOWN_PRESET)


group = Group(
    "for_signed",
    [
        case(
            "RUN ALONE, WITH THE WATCHDOG AT 10000 MS. A For over a DS of -1 runs 65535 "
            "(Result1 = 1) and over a constant 70000 runs 70000 (Result2 = 1). Over a DD of "
            "-65535 (Result3), a DD of -2 (Result4) and a CTD of -1 (ErrorCode) it runs none "
            "(1). 2 is the other model's count and 3 another count",
            ladder=prove_minus_counts,
            init=init_minus_counts,
            expect={"Result1": 1, "Result2": 1, "Result3": 1, "Result4": 1, "ErrorCode": 1},
            scans=scans_for(WINDOW),
            # Two scans of about 1.2 s each.
            fires_within=20.0,
        ),
    ],
)


def test_for_signed():
    assert_all_passed(run(group))
