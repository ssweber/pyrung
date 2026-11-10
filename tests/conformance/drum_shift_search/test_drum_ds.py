"""A drum step's DS preset, and a bad jump on the rung's return: ``drum_ds`` and ``jump_return``.

Part ``follow2_drums``.  A CLICK answered both on 2026-11-10, once.

``drum_ds/ds_preset_ends``: four time drums of three steps.  Each field is a drum's step: 1 for
step 1 still timing, 2 when step 1 has ended.  The case reads them, and raises ``Fired``, once the
control drum has timed 50 ms on its step 2.  A 1 ms step has ended by then at any scan length, and
so has the 5 ms step of Result3.  The run the CLICK read took them on scan 20 instead.  Every step
but the one asked about is a constant 30 s.  Each DS holds the value the case means from the
reset on, as the init writes it.

- Result1 (D1): step 1's preset a DS holding 1 ms from the reset on, never written again.
- Result2 (D2): step 1's preset a DS holding 1 ms through the reset and 30 s from scan 1 (written
  on a rung above the drum).
- Result3 (D3): step 1 a constant 5 ms, step 2 a DS holding 0, step 3 30 s.
- Result4, the control: step 1 a constant 1 ms.

A CLICK read 1, 1, 2, 2.  No DS preset ended a step, not even one that held 1 ms from the reset
on, while a constant 1 ms did.  So the drum does not read its preset while it is held in reset
(that reads 2 in Result2), and a DS of 0 entered by a move does not end at once (that reads 3 in
Result3).  No reading yet shows a DS preset ending a drum step.  pyrung reads 2, 1, 2, 2.  It reads
a DS preset when the step starts timing, so D1 ends.  The case is pending ``PRESET``.

``jump_return/bad_jump_on_return``: does a jump to a bad step on the scan the drum's rung comes
back keep the drum from writing its row?  ``bad_jump`` read on a CLICK that a jump to a bad step
takes its scan (a bad step 5 is corrected the scan after, 5252 then 1212), and the drum rule
writes a row on a move or on the rung's return.  Two pairs of a time drum and a time & event drum
(3 steps, 30 s, events never on), snapshots as in ``test_drum_pins`` (the step, then ``4 * flag +
2 * B + A``, the time drum above the other): jogged to step 2 on scan 2 (row 22), paused on scans
4 and 5, their outputs reset by hand on scan 4 (20), back on scan 6.  Pair Q has a jump to step 9
of 3 on scan 6; pair P none.

- Result1, P on scan 6: the return writes its row (2222).
- Result2, Q on scan 5, paused: a paused drum leaves its outputs (2020).
- Result3, Q on scan 6: the bad jump takes the scan, and the drum writes no row (2020).
- Result4, Q on scan 7: the row waits for a move (2020), and does not come a scan late.
- ErrorCode, P on scan 7: the row held (2222).

A CLICK read 2222, 2020, 2020, 2020, 2222, and pyrung reads the same.

The cases own what ``memory.OWNED_APART`` lists under these groups.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.conformance import _runner as runner_module
from pyrung.core import Rung, copy, fill, reset
from tests.conformance.drum_rig import LONG, Pool, Side, on_scans, put, track
from tests.conformance.drum_shift_search.test_drum_pins import snap, te_box, time_box
from tests.conformance.memory import c, dd, ds, td
from tests.conformance.scans import count_scans, done, scans_for

# --- drum_ds -------------------------------------------------------------------------

#: The ms the control drum times on its step 2 before the case reads the steps.
SETTLED = 50
#: ds_preset_ends: C611..C622, step registers DS4011..4014, presets DS4015..4017, TD261..264.
DS_BITS = range(611, 623)
DS_STEPS = range(4011, 4015)
DS_PRESETS = range(4015, 4018)
DS_TDS = range(261, 265)


def prove_ds_preset_ends(slot: Any) -> None:
    take = Pool(DS_BITS)
    sides = []
    for step, acc in zip(DS_STEPS, DS_TDS, strict=True):
        a, b, flag = take(3)
        sides.append(Side(ds[step], td[acc], a, b, flag, None))
    held, at_reset, by_move, control = sides
    p_held, p_reset, p_zero = (ds[n] for n in DS_PRESETS)
    count_scans(slot, 1)
    put(slot, 1, LONG, p_reset)
    time_box(slot, held, [p_held, LONG, LONG])
    time_box(slot, at_reset, [p_reset, LONG, LONG])
    time_box(slot, by_move, [5, p_zero, LONG])
    time_box(slot, control, [1, LONG, LONG])
    fields = (slot.Result1, slot.Result2, slot.Result3, slot.Result4)
    with Rung(slot.Cmd != 0, slot.Fired == 0, control.step == 2, control.acc >= SETTLED):
        for side, field in zip(sides, fields, strict=True):
            copy(side.step, field)
        copy(1, slot.Fired)


def init_ds_preset_ends(slot: Any) -> None:
    # The drums' own reset pins (``Cmd == 0``) put their steps, TDs and bits back; the presets
    # hold what the case means from the reset on.
    p_held, p_reset, p_zero = (ds[n] for n in DS_PRESETS)
    copy(1, p_held)
    copy(1, p_reset)
    copy(0, p_zero)


drum_ds = Group(
    "drum_ds",
    [
        case(
            "A time drum's DS preset of 1 ms held from the reset does not end its step "
            "(Result1 = 1), nor one of 1 ms through the reset and 30 s from scan 1 "
            "(Result2 = 1); a DS of 0 entered by a move never ends (Result3 = 2); a constant "
            "1 ms ends (Result4 = 2)",
            ladder=prove_ds_preset_ends,
            init=init_ds_preset_ends,
            expect={"Result1": 1, "Result2": 1, "Result3": 2, "Result4": 2},
            scans=24,
            pending="PRESET",
        ),
    ],
)


# --- jump_return ---------------------------------------------------------------------

#: bad_jump_on_return: C631..C651, step registers DS4021..4024, the target DS4025, TD265..268,
#: states DD121..124.
RET_BITS = range(631, 652)
RET_STEPS = range(4021, 4025)
RET_TARGET = 4025
RET_TDS = range(265, 269)
RET_STATES = range(121, 125)


def prove_bad_jump_on_return(slot: Any) -> None:
    take = Pool(RET_BITS)
    target = ds[RET_TARGET]

    def side(i: int) -> Side:
        a, b, flag = take(3)
        return Side(ds[RET_STEPS[i]], td[RET_TDS[i]], a, b, flag, dd[RET_STATES[i]])

    tm_p, te_p, tm_q, te_q = (side(i) for i in range(4))
    p1, p2, p3, q1, q2, q3 = take(6)
    jog, pause, jump = take(3)
    count_scans(slot)
    put(slot, 1, 9, target)
    on_scans(slot, jog, 2)
    on_scans(slot, pause, 4, 5)
    on_scans(slot, jump, 6)
    with Rung(slot.Scratch == 4):
        for s in (tm_p, te_p, tm_q, te_q):
            reset(s.a)
            reset(s.b)
    time_box(slot, tm_p, [LONG, LONG, LONG], pause=pause, jog=jog)
    te_box(slot, te_p, [p1, p2, p3], pause=pause, jog=jog)
    time_box(slot, tm_q, [LONG, LONG, LONG], pause=pause, jog=jog, jump=jump, step=target)
    te_box(slot, te_q, [q1, q2, q3], pause=pause, jog=jog, jump=jump, step=target)
    for s in (tm_p, te_p, tm_q, te_q):
        track(slot, s)
    snap(slot, tm_p, te_p, 6, slot.Result1)
    snap(slot, tm_q, te_q, 5, slot.Result2)
    snap(slot, tm_q, te_q, 6, slot.Result3)
    snap(slot, tm_q, te_q, 7, slot.Result4)
    snap(slot, tm_p, te_p, 7, slot.ErrorCode)
    done(slot)


def init_bad_jump_on_return(slot: Any) -> None:
    reset(c.select(RET_BITS.start, RET_BITS.stop - 1))
    fill(0, ds.select(RET_STEPS.start, RET_TARGET))
    for n in RET_TDS:
        copy(0, td[n])
    fill(0, dd.select(RET_STATES.start, RET_STATES.stop - 1))


jump_return = Group(
    "jump_return",
    [
        case(
            "A drum paused with its outputs reset by hand writes its row as its rung comes back "
            "(Result1 = 2222, ErrorCode = 2222 the scan after), but not with a jump to step 9 "
            "of 3 on that scan (Result3 = 2020), nor on the scan after (Result4 = 2020); paused, "
            "it leaves them off (Result2 = 2020)",
            ladder=prove_bad_jump_on_return,
            init=init_bad_jump_on_return,
            expect={
                "Result1": 2222,
                "Result2": 2020,
                "Result3": 2020,
                "Result4": 2020,
                "ErrorCode": 2222,
            },
            scans=scans_for(),
        ),
    ],
)


def test_drum_ds():
    assert_all_passed(run(drum_ds))


def test_jump_return():
    assert_all_passed(run(jump_return))


@pytest.mark.parametrize("dt", [0.001, 0.010, 0.025, 0.040])
def test_drum_ds_at_any_scan(monkeypatch, dt):
    """A CLICK scans in about 1 ms, and the soft PLC in 10 to 40 ms.  pyrung reads 2, 1, 2, 2 at
    each, so the case stays pending."""
    monkeypatch.setattr(runner_module, "DT", dt)
    (only,) = drum_ds.cases
    results = run(Group("drum_ds", [replace(only, scans=max(only.scans, int(0.2 / dt)))]))
    assert_all_passed(results)
    read = results[0].actual
    assert (read["Result1"], read["Result2"], read["Result3"], read["Result4"]) == (2, 1, 2, 2)
