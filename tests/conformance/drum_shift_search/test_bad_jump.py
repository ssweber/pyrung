"""A drum jump toward a step the drum does not have: the ``bad_jump`` group (part ``follow_timing``).

``drum_branch`` read two answers on a CLICK that one rule fits: a jump that fires takes the scan
even when its target is bad, so a jog on that scan does nothing (``jump_to_bad_step`` Result3)
and a bad step is not corrected that scan (``jump_on_bad_step`` ErrorCode, 5252).  Neither asks
whether the drum's own time and event are skipped too, or what the next scan does.  The fix step
``drum-click`` 6b (``conform-fix`` arc) waits on this.

Snapshots as in ``test_drum_pins``: two digits a drum, the step then ``4 * flag + 2 * B + A``, the
time drum above the time & event drum; on 3 steps row 1 is 11, row 2 is 22.

``bad_target_takes_the_scan``:

- On step 1, a jump to 9 on scan 3, with the time drum's Acc staged past its step's preset and
  the time & event drum's step 1 event (a level) on, on scans 3 and 4.  Result1 is scan 3 and
  Result2 scan 4.  The jump takes the scan: 1111, then 2222 as the time and the event move the
  drums on scan 4.  The jump is dropped and the drums run: 2222, 2222.  pyrung reads 2222, 2222.
- On step 2 (a jog on scan 2), step 5 staged on scan 4 with a jump to 9 on that scan, as
  ``jump_on_bad_step``'s ErrorCode, which read 5252 (Result3, scan 4).  Result4 is scan 5 and
  ErrorCode scan 6: corrected to step 1 with the row left as it was, 1212; corrected and the row
  written, 1111; not corrected, 5252.  pyrung reads 1212, 1212, 1212: it corrects on the jump's
  own scan.

The cases own what ``memory.OWNED_APART`` lists under this group.
"""

from __future__ import annotations

from typing import Any

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import copy, fill, reset
from tests.conformance.drum_rig import LONG, Pool, Side, on_scans, put, track
from tests.conformance.drum_shift_search.test_drum_pins import snap, te_box, time_box
from tests.conformance.memory import c, dd, ds, td
from tests.conformance.probe_kit import between
from tests.conformance.scans import count_scans, done, scans_for

#: The case's C bits, its step registers (the last holds the jump's target), TDs and states.
BITS = range(301, 322)
STEPS = range(3918, 3923)
TDS = range(201, 205)
STATES = range(71, 75)


def prove_bad_target_takes_the_scan(slot: Any) -> None:
    take = Pool(BITS)
    target = ds[STEPS[-1]]

    def side(i: int) -> Side:
        a, b, flag = take(3)
        return Side(ds[STEPS[i]], td[TDS[i]], a, b, flag, dd[STATES[i]])

    tm_p, te_p, tm_q, te_q = (side(i) for i in range(4))
    p1, p2, p3, q1, q2, q3 = take(6)
    jump_p, jump_q, jog_q = take(3)
    count_scans(slot)
    put(slot, 1, 9, target)
    # P: on step 1, a jump to 9 with the time up and the event on.
    on_scans(slot, jump_p, 3)
    on_scans(slot, p1, 3, 4)
    put(slot, 3, LONG + 100, tm_p.acc)
    # Q: on step 2, step 5 staged on the scan of a jump to 9.
    on_scans(slot, jog_q, 2)
    on_scans(slot, jump_q, 4)
    put(slot, 4, 5, tm_q.step)
    put(slot, 4, 5, te_q.step)
    time_box(slot, tm_p, [LONG, LONG, LONG], jump=jump_p, step=target)
    te_box(slot, te_p, [p1, p2, p3], jump=jump_p, step=target)
    time_box(slot, tm_q, [LONG, LONG, LONG], jog=jog_q, jump=jump_q, step=target)
    te_box(slot, te_q, [q1, q2, q3], jog=jog_q, jump=jump_q, step=target)
    for s in (tm_p, te_p, tm_q, te_q):
        track(slot, s)
    snap(slot, tm_p, te_p, 3, slot.Result1)
    snap(slot, tm_p, te_p, 4, slot.Result2)
    snap(slot, tm_q, te_q, 4, slot.Result3)
    snap(slot, tm_q, te_q, 5, slot.Result4)
    snap(slot, tm_q, te_q, 6, slot.ErrorCode)
    done(slot)


def init_bad_target_takes_the_scan(slot: Any) -> None:
    reset(c.select(BITS.start, BITS.stop - 1))
    fill(0, ds.select(STEPS.start, STEPS.stop - 1))
    for n in TDS:
        copy(0, td[n])
    fill(0, dd.select(STATES.start, STATES.stop - 1))


group = Group(
    "bad_jump",
    [
        case(
            "A drum jump to step 9 of 3 is dropped and the drums run on: the time up and the event "
            "on move them that scan, 2222 (Result1), 2222 (Result2); on a bad step 5 the jump's "
            "scan corrects it with no row written, 1212 (Result3), and so it stays (Result4, "
            "ErrorCode)",
            ladder=prove_bad_target_takes_the_scan,
            init=init_bad_target_takes_the_scan,
            expect={
                "Result1": between(1111, 2222),
                "Result2": between(1111, 2222),
                "Result3": between(1111, 5252),
                "Result4": between(1111, 5252),
                "ErrorCode": between(1111, 5252),
            },
            scans=scans_for(),
            probe="a jump to a bad step: does it take the scan, does the next scan correct (6b)",
        ),
    ],
)


def test_bad_jump():
    assert_all_passed(run(group))
