"""When a time drum reads a step's preset: the ``drum_preset`` group (part ``follow_timing``).

``drum_limits/preset_from_ds`` read on a CLICK that a step's preset from a DS is read on entering
the step, not live, and that a DS preset of 0 or -5 never ends the step.  This group asks when on
entering, and what a constant 0 does.  Each field is a drum's step, read on scan 20, so that a
1 ms step has ended even on short scans; every drum has 3 steps, step 1 and 3 30 s long.

``literal_zero``: a step preset written as the constant 0, not from a DS.  Result1, presets
0 / 30 s / 30 s: 1 where 0 never ends the step (as from a DS), 2 where it ends at once.  Result2,
the control, presets 1 ms / 30 s / 30 s: 2.  The CLICK read 2, 2 (2026-11-06): a constant 0 ends
its step at once, unlike a DS holding 0.  The editor took the constant.

``entry_or_first_timing_scan``: drums jogged from step 1 to step 2 on scan 3, step 2's preset from
a DS.
- Result1: the DS holds 1 ms up to scan 3 (the jog's scan) and 30 s from scan 4: 3 where the
  preset is read at the move, 2 where it is read on the first scan the step times (scan 4).  The
  CLICK read 2.
- Result2: the DS holds 30 s up to scan 3, 1 ms on scan 4 only, then 30 s: 2 read at the move, 3
  read on the first timing scan.  The CLICK read 2, which Result1 rules out as a read at the
  move: on a scan of under a millisecond scan 4 adds no time, so a CLICK that reads the preset
  on the first scan its step's time moves has 30 s by then.  pyrung reads on the first scan the
  step times, whatever it adds, and reads 3 in-process
  (``test_pyrung_reads_the_preset_on_the_first_timing_scan``); the expectation holds both.
- Result3: a correction.  Step 1's preset from a DS of 30 s, jogged to step 2 on scan 2; on scan
  4 the step is written 5 and the DS 1 ms from then on.  The bad step goes back to step 1: 2
  where step 1's preset is read again there (1 ms ends it), 1 where it is not.  The CLICK read
  1: a correction is no move, and the preset step 2 read stays.

The cases own what ``memory.OWNED_APART`` lists under this group.
"""

from __future__ import annotations

from typing import Any

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import Rung, copy, fill
from tests.conformance.drum_rig import LONG, Pool, Side, on_scans, put
from tests.conformance.drum_shift_search.test_drum_pins import time_box
from tests.conformance.memory import ds, td
from tests.conformance.scans import count_scans, done, scans_for

WINDOW = 22
READ_SCAN = 20
#: Each case's C bits, then its drums' step registers, TDs and DS presets.
LITERAL_BITS, TIMING_BITS = range(331, 337), range(341, 353)
LITERAL_STEPS, TIMING_STEPS = range(3923, 3925), range(3925, 3928)
LITERAL_TDS, TIMING_TDS = range(205, 207), range(207, 210)
PRESETS = range(3928, 3931)


def _sides(bits: range, steps: range, tds: range) -> list[Side]:
    take = Pool(bits)
    sides = []
    for step, acc in zip(steps, tds, strict=True):
        a, b, flag = take(3)
        sides.append(Side(ds[step], td[acc], a, b, flag, None))
    return sides


def _read(slot: Any, sides: list[Side]) -> None:
    fields = (slot.Result1, slot.Result2, slot.Result3)
    with Rung(slot.Scratch == READ_SCAN):
        for side, field in zip(sides, fields[: len(sides)], strict=True):
            copy(side.step, field)


def prove_literal_zero(slot: Any) -> None:
    zero, one = _sides(LITERAL_BITS, LITERAL_STEPS, LITERAL_TDS)
    count_scans(slot, WINDOW)
    time_box(slot, zero, [0, LONG, LONG])
    time_box(slot, one, [1, LONG, LONG])
    _read(slot, [zero, one])
    done(slot, WINDOW)


def prove_entry_or_first_timing_scan(slot: Any) -> None:
    at_move, first_scan, corrected = _sides(TIMING_BITS, TIMING_STEPS, TIMING_TDS)
    jog, jog_early = Pool(TIMING_BITS[9:])(2)
    p_move, p_first, p_back = (ds[n] for n in PRESETS)
    count_scans(slot, WINDOW)
    put(slot, 1, 1, p_move)
    put(slot, 4, LONG, p_move)
    put(slot, 1, LONG, p_first)
    put(slot, 4, 1, p_first)
    put(slot, 5, LONG, p_first)
    put(slot, 1, LONG, p_back)
    put(slot, 4, 1, p_back)
    put(slot, 4, 5, corrected.step)
    on_scans(slot, jog, 3)
    on_scans(slot, jog_early, 2)
    time_box(slot, at_move, [LONG, p_move, LONG], jog=jog)
    time_box(slot, first_scan, [LONG, p_first, LONG], jog=jog)
    time_box(slot, corrected, [p_back, LONG, LONG], jog=jog_early)
    _read(slot, [at_move, first_scan, corrected])
    done(slot, WINDOW)


def init_entry_or_first_timing_scan(slot: Any) -> None:
    # The drums' own reset pins (``Cmd == 0``) put their steps, TDs and bits back.
    fill(0, ds.select(PRESETS.start, PRESETS.stop - 1))


group = Group(
    "drum_preset",
    [
        case(
            "A time drum reads a step's DS preset on the first scan the step times, not at the "
            "jog: 1 ms only at the jog stays (Result1 = 2), 1 ms only the scan after ends on a "
            "scan that times it (Result2 in 2..3); a bad step corrected to step 1 keeps the "
            "preset it held (Result3 = 1)",
            ladder=prove_entry_or_first_timing_scan,
            init=init_entry_or_first_timing_scan,
            expect={
                "Result1": 2,
                "Result2": range(2, 4),
                "Result3": 1,
            },
            scans=scans_for(WINDOW),
        ),
        case(
            "A time drum step with the constant preset 0 ends at once, unlike one from a DS "
            "(Result1 = 2); a constant 1 ms step ends (Result2 = 2)",
            ladder=prove_literal_zero,
            expect={"Result1": 2, "Result2": 2},
            scans=scans_for(WINDOW),
        ),
    ],
)


def test_drum_preset():
    assert_all_passed(run(group))


def test_pyrung_reads_the_preset_on_the_first_timing_scan():
    """In-process every scan times 10 ms, so the 1 ms held on scan 4 alone is read (Result2)."""
    read = {r.case.id: r.actual for r in run(group)}
    assert read["entry_or_first_timing_scan"]["Result2"] == 3
