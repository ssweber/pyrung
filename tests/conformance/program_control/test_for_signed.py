"""For counts read signed, by the low word, or whole: the ``for_signed`` group (part ``follow2_for``).

**Run it alone, with the watchdog at 10000 ms and C411 set by hand: on a CLICK it may stop the
PLC.**  ``for_dd_minus`` read that a DD count of -1 runs none, as a signed count would, where a DS
count of -1 runs 65535 (``loop/negative_count_reads_unsigned``) and a DD of 65536 runs 65536
(``for_wide/count_forms``).  One DD of -1 does not tell "a DD count is signed" from "a negative DD
runs none" or "its low word, 65535, unless ...", and no count of a CTD or of a constant over 65535
has been read.  pyrung reads a DS, TD or constant count through 16 bits unsigned and a DD or CTD
whole and unsigned.

One count a scan, each For on a scan of its own, so that each scan's runs stay under the
watchdog where the count is read signed or by its low word (at about 17 us a run, 65535 runs take
about 1.1 s):

- scan 3, Result1, a DS of -1, the control: 1 for 65535 runs (as read), 2 for none.
- scan 4, Result2, a constant 70000 (the editor takes it): 1 for 70000 runs, whole; 2 for 4464,
  the low 16 bits (pyrung).
- scan 5, Result3, a DD of -65535 (0xFFFF0001): 1 for none, signed; 2 for 1, the low word.
- scan 6, Result4, a DD of -2: 1 for none, signed; 2 for 65534, the low word.
- scan 7, ErrorCode, a CTD of -1 (a down counter counted once from 0 on scan 2): 1 for none,
  signed; 2 for 65535, the low word.

Each reading is 0 where the For did not run (the case was not armed, or the PLC stopped before
that scan) and 3 for another count of runs, which its DD holds (DD143..DD147, in the order above)
until the next reset.  Where a count is read whole and unsigned (pyrung's reading for the DD and
CTD counts: 4294901761, 4294967294 and 4294967295 runs), the scan trips the watchdog and the PLC
stops; the run times out after 20 s (``fires_within``).  Read by hand then the error the CLICK
software shows (0107 for the watchdog), SD12 and the DD of the count it stopped in (the runs made
before the stop), and write them down: no case records them.  The readings before the stop are
in the slot.

The loops run only on scans that C412 is on, and C412 is latched from C411, which is set by hand,
as ``for_dd_minus`` does with C401: as its ``Cmd`` comes up the case latches C412, writes the
counts and turns C411 off.  With C412 off a rung writes 0 into every count and the counter is held
in reset, so pasting the part and a run with C411 off are harmless (every reading 0).  C412 is not
retentive, so a STOP -> RUN after a watchdog stop clears it and no loop can run again.  In-process
and on the local targets nothing sets C411: the loops never run, and pyrung reads 0 for all five.

To ask it:

1. A new project's watchdog is 200 ms, which 65535 runs would trip.  Set it to 10000 ms in Setup,
   and write the project.
2. Set C411 on in the Data View, and run ``make test-conformance PLC=... GROUP=for_signed``.
3. After a watchdog stop, clear the error and put the PLC back in RUN (or cycle its power).
4. Set the watchdog back to 200 ms after.

No reading says whether the editor takes a CTD as a For count.  If it refuses ``for(CTD6)`` at
paste, delete that For, its body (the Math into DD147) and its Next, and the class rungs under it
(the rungs that write ErrorCode on scan 7): ErrorCode then reads 0, and the other four counts
still answer.

The cases own what ``memory.OWNED_APART`` lists under this group.
"""

from __future__ import annotations

from typing import Any

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import Or, Rung, copy, count_down, fill, forloop, latch, math, reset
from tests.conformance.memory import ClickCounter, c, dd, ds
from tests.conformance.probe_kit import between
from tests.conformance.scans import count_scans, done, scans_for

WINDOW = 8
ENABLE, ARMED = c[411], c[412]
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
    with Rung(~ARMED):
        copy(0, DS_COUNT)
        copy(0, DD_LOW)
        copy(0, DD_MINUS_TWO)
    with Rung(slot.Cmd != 0, slot.Scratch == 1, ENABLE):
        latch(ARMED)
        copy(-1, DS_COUNT)
        copy(-65535, DD_LOW)
        copy(-2, DD_MINUS_TWO)
        reset(ENABLE)
    with Rung(slot.Cmd != 0, slot.Scratch == COUNT_SCAN, ARMED):
        count_down(DOWN, DOWN_PRESET).reset(Or(slot.Cmd == 0, ~ARMED))
    for scan, ((count, readings), runs, field) in enumerate(
        zip(loops, RUNS, fields, strict=True), start=COUNT_SCAN + 1
    ):
        on_scan = (slot.Cmd != 0, slot.Scratch == scan, ARMED)
        with Rung(*on_scan):
            with forloop(count):
                math(runs + 1, runs)
        with Rung(*on_scan):
            copy(3, field)
        for number, reading in enumerate(readings, start=1):
            with Rung(*on_scan, runs == reading):
                copy(number, field)
    with Rung(slot.Cmd != 0, slot.Scratch == WINDOW, ARMED):
        reset(ARMED)
    done(slot, WINDOW)


def init_minus_counts(slot: Any) -> None:
    fill(0, dd.select(143, 147))
    # The down counter's preset, below the -1 it is counted to.
    copy(-5, DOWN_PRESET)


group = Group(
    "for_signed",
    [
        case(
            "RUN ALONE, WITH THE WATCHDOG AT 10000 MS AND C411 SET BY HAND: it may stop the PLC.  "
            "A For over a DS of -1 runs 65535 (Result1 = 1), over 70000 whole (Result2 = 1) or "
            "4464 (2), over a DD of -65535 (Result3), a DD of -2 (Result4) and a CTD of -1 "
            "(ErrorCode) none (1), the low word (2), or whole and the PLC stops",
            ladder=prove_minus_counts,
            init=init_minus_counts,
            expect={
                "Result1": between(0, 3),
                "Result2": between(0, 3),
                "Result3": between(0, 3),
                "Result4": between(0, 3),
                "ErrorCode": between(0, 3),
            },
            scans=scans_for(WINDOW),
            probe="For counts: signed, the low word, or whole (boot-click)",
            # Five scans of up to about 1.2 s each, or a stop on the 10000 ms watchdog.
            fires_within=20.0,
        ),
    ],
)


def test_for_signed():
    assert_all_passed(run(group))
