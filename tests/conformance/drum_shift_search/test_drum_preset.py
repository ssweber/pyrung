"""When a time drum reads a step's preset: the ``drum_preset`` group (part ``follow_timing``).

A time drum reads a DS step preset on every scan the step times, as it reads a constant.  Each
field is a drum's step.  Every drum has 3 steps, step 1 and 3 30 s long.  The case's init zeroes
the DS presets while the drums are reset.

Each case reads its steps once 50 ms of timing has passed, and raises ``Fired`` then.  A 1 ms step
has ended by then at any scan length.  ``literal_zero`` waits on its control drum, 50 ms into
step 2.  ``entry_or_first_timing_scan`` takes a TON started on scan 5, its last write, as its
clock.

``literal_zero``: a step preset written as the constant 0, not from a DS.  Result1 has presets
0 / 30 s / 30 s.  It reads 2 if 0 ends the step at once, and 1 if the step never ends.  Result2,
the control, has presets 1 ms / 30 s / 30 s and reads 2.  The CLICK read 2, 2 (2026-11-06).  The
rows were pasted.  Whether the editor's dialog takes a typed 0 is unread.

``entry_or_first_timing_scan``: drums jogged from step 1 to step 2 on scan 3, step 2's preset from
a DS.
- Result1: the DS holds 1 ms up to scan 3 (the jog's scan) and 30 s from scan 4.  Step 2 first
  times on scan 4 and reads 30 s, 2.  A drum that read the preset at the move would read 3.
- Result2: the DS holds 30 s up to scan 3, 1 ms on scan 4 only, then 30 s.  Step 2 reads the 1 ms
  on scan 4 and ends, 3.
- Result3: a correction.  Step 1's preset from a DS of 30 s, jogged to step 2 on scan 2.  On scan
  4 the step is written 5 and the DS 1 ms from then on.  The bad step goes back to step 1, which
  reads the 1 ms and ends, 2.

The CLICK read 2, 3, 2, twice (2026-11-12).  The readings before 2026-11-12 (2, 2, 1) measured a
drum that laddercodec had pasted with a wrong encoding.

The cases own what ``memory.OWNED_APART`` lists under this group.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.conformance import _runner as runner_module
from pyrung.core import Rung, copy, fill, on_delay
from tests.conformance.drum_rig import LONG, Pool, Side, on_scans, put
from tests.conformance.drum_shift_search.test_drum_pins import time_box
from tests.conformance.memory import ClickTimer, ds, td
from tests.conformance.scans import count_scans

#: The ms of timing a case waits before it reads its steps.
SETTLED = 50
#: The scan of the last write ``entry_or_first_timing_scan`` makes.
LAST_WRITE = 5
SCANS = 24
#: Each case's C bits, then its drums' step registers, TDs and DS presets.
LITERAL_BITS, TIMING_BITS = range(331, 337), range(341, 353)
LITERAL_STEPS, TIMING_STEPS = range(3923, 3925), range(3925, 3928)
LITERAL_TDS, TIMING_TDS = range(205, 207), range(207, 210)
PRESETS = range(3928, 3931)
CLOCK = ClickTimer(210)


def _sides(bits: range, steps: range, tds: range) -> list[Side]:
    take = Pool(bits)
    sides = []
    for step, acc in zip(steps, tds, strict=True):
        a, b, flag = take(3)
        sides.append(Side(ds[step], td[acc], a, b, flag, None))
    return sides


def _read(slot: Any, sides: list[Side], *when: Any) -> None:
    """Each side's step into Result1.., and ``Fired``, on the first scan *when* holds."""
    fields = (slot.Result1, slot.Result2, slot.Result3)
    with Rung(slot.Cmd != 0, slot.Fired == 0, *when):
        for side, field in zip(sides, fields[: len(sides)], strict=True):
            copy(side.step, field)
        copy(1, slot.Fired)


def prove_literal_zero(slot: Any) -> None:
    zero, one = _sides(LITERAL_BITS, LITERAL_STEPS, LITERAL_TDS)
    time_box(slot, zero, [0, LONG, LONG])
    time_box(slot, one, [1, LONG, LONG])
    _read(slot, [zero, one], one.step == 2, one.acc >= SETTLED)


def prove_entry_or_first_timing_scan(slot: Any) -> None:
    at_move, first_scan, corrected = _sides(TIMING_BITS, TIMING_STEPS, TIMING_TDS)
    jog, jog_early = Pool(TIMING_BITS[9:])(2)
    p_move, p_first, p_back = (ds[n] for n in PRESETS)
    count_scans(slot, LAST_WRITE)
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
    with Rung(slot.Cmd != 0, slot.Scratch >= LAST_WRITE):
        on_delay(CLOCK, LONG)
    _read(slot, [at_move, first_scan, corrected], CLOCK.Acc >= SETTLED)


def init_entry_or_first_timing_scan(slot: Any) -> None:
    # The drums' own reset pins (``Cmd == 0``) put their steps, TDs and bits back.
    fill(0, ds.select(PRESETS.start, PRESETS.stop - 1))


group = Group(
    "drum_preset",
    [
        case(
            "A time drum reads a DS step preset every scan.  Step 2 on a DS of 1 ms only at the "
            "jog stays (Result1 = 2).  Step 2 on a DS of 1 ms only on the scan after ends "
            "(Result2 = 3).  A bad step corrected to step 1, its DS lowered to 1 ms, ends "
            "(Result3 = 2)",
            ladder=prove_entry_or_first_timing_scan,
            init=init_entry_or_first_timing_scan,
            expect={"Result1": 2, "Result2": 3, "Result3": 2},
            scans=SCANS,
        ),
        case(
            "A time drum step with the constant preset 0 ends at once (Result1 = 2).  A constant "
            "1 ms step ends (Result2 = 2)",
            ladder=prove_literal_zero,
            expect={"Result1": 2, "Result2": 2},
            scans=SCANS,
        ),
    ],
)


def test_drum_preset():
    assert_all_passed(run(group))


def _at(dt: float) -> Group:
    return Group("drum_preset", [replace(k, scans=max(SCANS, int(0.2 / dt))) for k in group.cases])


@pytest.mark.parametrize("dt", [0.001, 0.010, 0.025, 0.040])
def test_drum_preset_at_any_scan(monkeypatch, dt):
    """A CLICK scans in about 1 ms, and the soft PLC in 10 to 40 ms."""
    monkeypatch.setattr(runner_module, "DT", dt)
    assert_all_passed(run(_at(dt)))
