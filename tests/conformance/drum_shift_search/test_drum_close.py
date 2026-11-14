"""S6 drum probes, where the rules stop: ``drum_close`` (``Conf_DrumClose``).

Part ``drum2`` (``suite.PARTS``).  ``test_drum_settle`` found that a drum takes
no event or time end on the scan its rung returns, that a completed drum's own time and
event do nothing, and that its accumulator holds.  These asked where those rules stop, on a
CLICK the same day (2026-11-02).  Snapshots as in ``test_drum_settle``, the first drum named
above the other.

- ``return_scan_ticks``: paused on scans 3..4 with Acc 1000 staged on a time drum and a timed
  time & event drum: Acc - 1000 is 0 on the return scan (Result2, Result3), which times
  nothing, and above 0 on the next (Result4, ErrorCode; 9 on the CLICK).
- ``flag_cleared_from_outside``: completed by a jog, 2424, the flag reset by a rung on scan 5,
  2020, and the step's own end on scan 6 completes it again, 2424: the flag is what stops
  the drum, not a memory of its own.
- ``flag_set_from_outside``: a drum on step 1 with its flag set by a rung on scan 3, 1515, and
  step 1's own end on scan 4 does nothing, 1515 on scans 4 and 5; a jog on scan 6 moves it,
  2626.  The flag again.
- ``return_after_jog_back``: completed, jumped and jogged back, 2626, then paused on scans 6..7
  with B reset from outside, 2424: the rung's return writes nothing with the flag on, 2424.
- ``return_after_jump``: completed and jumped, 1414, then paused on scans 5..6 with B latched
  from outside, 1616: the return writes nothing, 1616.
- ``return_scan_moves``: a jog and a jump on a return scan act, 2222 and 1111 (Result2,
  Result3).  A time and a time & event drum on their last step, paused with Acc past the
  preset and a level event from the return scan: 2222 on it (Result4), not done, and 2424
  on the next (ErrorCode).
- ``te_acc_by_step_kind``: a timed time & event drum's correction keeps the 9000 staged in
  its Acc, 9 in thousands (Result1), and it counts on (Result2 above 0).  The CLICK read 9 in
  hundreds of 900 staged, which a scan of 100 ms pushes to 10.  An event-only one with Acc
  500 staged does not move (Result3 = 0).  One with an event-only step 1 and a timed step 2,
  jogged to it, does (Result4 above 0).  A timed one completed by a jog holds (ErrorCode = 0).

The cases own C702..C750, C969..C1000, C414..C450, C27..C50, T271..T298, T377..T400, DD754..DD809 and
DS4332..DS4342, DS4352..DS4362, DS4372..DS4382 (``memory.OWNED``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.conformance import _runner as runner_module
from pyrung.core import Rung, latch, math, out, reset
from tests.conformance.drum_rig import LONG, Pool, Side, on_scans, pool_of, put, track
from tests.conformance.drum_shift_search.test_drum_settle import (
    acc_thousands,
    moves,
    pause_on,
    snap_two,
    te_side,
    time_side,
)
from tests.conformance.memory import dd, ds, td
from tests.conformance.scans import count_scans, done, scans_for

SCANS = scans_for()

_BITS = pool_of(range(702, 751), range(969, 1001), range(414, 451), range(27, 51))
_BITS_PER_CASE = 18
_STEPS = [*range(4332, 4343), *range(4352, 4363), *range(4372, 4383)]
_TDS = [*range(271, 299), *range(377, 401)]
_DDS = range(754, 810)


@dataclass(frozen=True)
class Rig:
    """The tags of a case: up to four drums (:meth:`side`), each with its step, Acc and state."""

    slot: Any
    index: int
    take: Pool

    def side(self, which: int) -> Side:
        a, b, flag = self.take(3)
        i = 4 * self.index + which
        return Side(ds[_STEPS[i]], td[_TDS[i]], a, b, flag, dd[_DDS[8 * self.index + which]])

    def spare_dd(self, which: int) -> Any:
        """A DD of the case's own, for :func:`moves`."""
        return dd[_DDS[8 * self.index + 4 + which]]


def rig(slot: Any, index: int) -> Rig:
    return Rig(slot, index, _BITS(index, _BITS_PER_CASE))


def _over(slot: Any, s: Side, scan: int, into: Any) -> None:
    """*into* = Acc - 1000 on scan *scan*.  Below the drum."""
    with Rung(slot.Scratch == scan):
        math(s.acc - 1000, into)


def prove_return_scan_ticks(slot):
    r = rig(slot, 0)
    tm, te = r.side(0), r.side(1)
    pause, e1, e2 = r.take(3)
    count_scans(slot)
    pause_on(slot, pause, 3, 4)
    put(slot, 4, 1000, tm.acc)
    put(slot, 4, 1000, te.acc)
    time_side(slot, tm, pause=pause)
    te_side(slot, te, [e1, e2], presets=[LONG, LONG], pause=pause)
    _over(slot, tm, 5, slot.Result2)
    _over(slot, te, 5, slot.Result3)
    _over(slot, tm, 6, slot.Result4)
    _over(slot, te, 6, slot.ErrorCode)
    done(slot)


def prove_flag_cleared_from_outside(slot):
    r = rig(slot, 1)
    tm, te = r.side(0), r.side(1)
    jog, e1, e2 = r.take(3)
    count_scans(slot)
    on_scans(slot, jog, 1, 3)
    with Rung(slot.Scratch == 5):
        reset(tm.flag)
        reset(te.flag)
    put(slot, 6, 2500, tm.acc)
    with Rung(slot.Scratch >= 6):
        out(e2)
    time_side(slot, tm, (LONG, 2000), jog=jog)
    te_side(slot, te, [e1, e2], jog=jog)
    track(slot, tm)
    track(slot, te)
    snap_two(slot, tm, te, 3, slot.Result2)
    snap_two(slot, tm, te, 5, slot.Result3)
    snap_two(slot, tm, te, 6, slot.Result4)
    snap_two(slot, tm, te, 7, slot.ErrorCode)
    done(slot)


def prove_flag_set_from_outside(slot):
    r = rig(slot, 2)
    tm, te = r.side(0), r.side(1)
    jog, e1, e2 = r.take(3)
    count_scans(slot)
    with Rung(slot.Scratch == 3):
        latch(tm.flag)
        latch(te.flag)
    put(slot, 4, 2500, tm.acc)
    with Rung(slot.Scratch >= 4):
        out(e1)
    on_scans(slot, jog, 6)
    time_side(slot, tm, (2000, LONG), jog=jog)
    te_side(slot, te, [e1, e2], jog=jog)
    track(slot, tm)
    track(slot, te)
    snap_two(slot, tm, te, 3, slot.Result2)
    snap_two(slot, tm, te, 4, slot.Result3)
    snap_two(slot, tm, te, 5, slot.Result4)
    snap_two(slot, tm, te, 6, slot.ErrorCode)
    done(slot)


def prove_return_after_jog_back(slot):
    r = rig(slot, 3)
    tm, te = r.side(0), r.side(1)
    jog, jump, pause, e1, e2 = r.take(5)
    count_scans(slot)
    # Result1 holds the jump step, 1 (a DS address).
    put(slot, 1, 1, slot.Result1)
    on_scans(slot, jog, 1, 3, 5)
    on_scans(slot, jump, 4)
    pause_on(slot, pause, 6, 7)
    with Rung(slot.Scratch == 7):
        reset(tm.b)
        reset(te.b)
    time_side(slot, tm, pause=pause, jog=jog, jump=jump, jump_step=slot.Result1)
    te_side(slot, te, [e1, e2], pause=pause, jog=jog, jump=jump, jump_step=slot.Result1)
    track(slot, tm)
    track(slot, te)
    snap_two(slot, tm, te, 5, slot.Result2)
    snap_two(slot, tm, te, 7, slot.Result3)
    snap_two(slot, tm, te, 8, slot.Result4)
    done(slot)


def prove_return_after_jump(slot):
    r = rig(slot, 4)
    tm, te = r.side(0), r.side(1)
    jog, jump, pause, e1, e2 = r.take(5)
    count_scans(slot)
    # Result1 holds the jump step, 1 (a DS address).
    put(slot, 1, 1, slot.Result1)
    on_scans(slot, jog, 1, 3)
    on_scans(slot, jump, 4)
    pause_on(slot, pause, 5, 6)
    with Rung(slot.Scratch == 6):
        latch(tm.b)
        latch(te.b)
    time_side(slot, tm, pause=pause, jog=jog, jump=jump, jump_step=slot.Result1)
    te_side(slot, te, [e1, e2], pause=pause, jog=jog, jump=jump, jump_step=slot.Result1)
    track(slot, tm)
    track(slot, te)
    snap_two(slot, tm, te, 4, slot.Result2)
    snap_two(slot, tm, te, 6, slot.Result3)
    snap_two(slot, tm, te, 7, slot.Result4)
    snap_two(slot, tm, te, 8, slot.ErrorCode)
    done(slot)


def prove_return_scan_moves(slot):
    r = rig(slot, 5)
    jog_tm, jog_te, last_tm, last_te = r.side(0), r.side(1), r.side(2), r.side(3)
    jog, jog_last, pause, pause_last, jump, e1 = r.take(6)
    count_scans(slot)
    # Result1 holds the jump step, 1 (a DS address).
    put(slot, 1, 1, slot.Result1)
    # Paused on scans 3..4 and 6..7, by one rung: a second Out on the bit would turn it off.
    with Rung(slot.Scratch >= 3, slot.Scratch <= 7, slot.Scratch != 5):
        out(pause)
    pause_on(slot, pause_last, 3, 4)
    on_scans(slot, jog, 5)
    on_scans(slot, jog_last, 1)
    on_scans(slot, jump, 8)
    put(slot, 4, 2500, last_tm.acc)
    with Rung(slot.Scratch >= 5):
        out(e1)
    time_side(slot, jog_tm, pause=pause, jog=jog, jump=jump, jump_step=slot.Result1)
    te_side(
        slot,
        jog_te,
        [None, None],
        presets=[LONG, LONG],
        pause=pause,
        jog=jog,
        jump=jump,
        jump_step=slot.Result1,
    )
    time_side(slot, last_tm, (LONG, 2000), pause=pause_last, jog=jog_last)
    te_side(slot, last_te, [None, e1], presets=[LONG, None], pause=pause_last, jog=jog_last)
    track(slot, jog_tm)
    track(slot, jog_te)
    track(slot, last_tm)
    track(slot, last_te)
    snap_two(slot, jog_tm, jog_te, 5, slot.Result2)
    snap_two(slot, jog_tm, jog_te, 8, slot.Result3)
    snap_two(slot, last_tm, last_te, 5, slot.Result4)
    snap_two(slot, last_tm, last_te, 6, slot.ErrorCode)
    done(slot)


def prove_te_acc_by_step_kind(slot):
    r = rig(slot, 6)
    corrected, event_only, mixed, completed = r.side(0), r.side(1), r.side(2), r.side(3)
    jog, jog2, e1 = r.take(3)
    count_scans(slot)
    put(slot, 3, 5, corrected.step)
    put(slot, 3, 9000, corrected.acc)
    put(slot, 2, 500, event_only.acc)
    on_scans(slot, jog, 3)
    on_scans(slot, jog2, 1, 3)
    te_side(slot, corrected, [e1, e1], presets=[LONG, LONG])
    te_side(slot, event_only, [e1, e1])
    te_side(slot, mixed, [e1, None], presets=[None, LONG], jog=jog)
    te_side(slot, completed, [e1, e1], presets=[LONG, LONG], jog=jog2)
    acc_thousands(slot, corrected, 3, slot.Result1)
    moves(slot, corrected.acc, r.spare_dd(0), 4, 8, slot.Result2)
    moves(slot, event_only.acc, r.spare_dd(1), 3, 8, slot.Result3)
    moves(slot, mixed.acc, r.spare_dd(2), 5, 8, slot.Result4)
    moves(slot, completed.acc, r.spare_dd(3), 4, 8, slot.ErrorCode)
    done(slot)


group = Group(
    "drum_close",
    [
        case(
            "The scan a rung returns does not time: paused on scans 3..4 with Acc 1000 staged "
            "on a time drum and a timed time & event drum, Acc has not moved on the return "
            "scan (Result2, Result3) and has on the next (Result4, ErrorCode)",
            ladder=prove_return_scan_ticks,
            expect={
                "Result2": 0,
                "Result3": 0,
                "Result4": range(1, 1000),
                "ErrorCode": range(1, 1000),
            },
            scans=SCANS,
        ),
        case(
            "The flag is what stops a completed drum: completed by a jog (2424, Result2), the "
            "flag reset from outside (2020, Result3), and the step's own end completes it "
            "again (2424, Result4, ErrorCode)",
            ladder=prove_flag_cleared_from_outside,
            expect={"Result2": 2424, "Result3": 2020, "Result4": 2424, "ErrorCode": 2424},
            scans=SCANS,
        ),
        case(
            "The flag is what stops a drum, however it came on: set from outside on step 1 "
            "(1515, Result2), step 1's own end does nothing (1515, Result3, Result4), and a "
            "jog moves it (2626, ErrorCode)",
            ladder=prove_flag_set_from_outside,
            expect={"Result2": 1515, "Result3": 1515, "Result4": 1515, "ErrorCode": 2626},
            scans=SCANS,
        ),
        case(
            "A rung's return writes nothing with the flag on: completed, jumped and jogged back "
            "(2626, Result2), paused with B reset from outside (2424, Result3), and the return "
            "leaves it (2424, Result4)",
            ladder=prove_return_after_jog_back,
            expect={"Result2": 2626, "Result3": 2424, "Result4": 2424},
            scans=SCANS,
        ),
        case(
            "A rung's return writes nothing on a completed drum: completed and jumped (1414, "
            "Result2), paused with B latched from outside (1616, Result3), and the return "
            "leaves it (1616, Result4, ErrorCode)",
            ladder=prove_return_after_jump,
            expect={"Result2": 1414, "Result3": 1616, "Result4": 1616, "ErrorCode": 1616},
            scans=SCANS,
        ),
        case(
            "A jog and a jump act on a return scan (2222, 1111, Result2, Result3), and the last "
            "step's own end waits for the next scan: 2222 on the return scan (Result4), 2424 "
            "on the next (ErrorCode)",
            ladder=prove_return_scan_moves,
            expect={"Result2": 2222, "Result3": 1111, "Result4": 2222, "ErrorCode": 2424},
            scans=SCANS,
            probe="time and time & event drums: a return scan's jog, jump and last step",
        ),
        case(
            "A time & event drum times only a step with a time, and not once completed: a timed "
            "one's correction keeps 9 thousands (Result1) and counts on (Result2), an event-only "
            "one does not move (Result3), a step 2 with a time does (Result4), and a completed "
            "one holds (ErrorCode)",
            ladder=prove_te_acc_by_step_kind,
            expect={
                "Result1": 9,
                "Result2": range(1, 6),
                "Result3": 0,
                "Result4": range(1, 5),
                "ErrorCode": 0,
            },
            scans=SCANS,
        ),
    ],
)


def test_drum_close():
    assert_all_passed(run(group))


@pytest.mark.parametrize("dt", [0.001, 0.010, 0.025, 0.040, 0.050])
def test_te_acc_by_step_kind_at_any_scan(monkeypatch, dt):
    """A CLICK scans in about 1 ms, and the soft PLC in 10 to 40 ms."""
    monkeypatch.setattr(runner_module, "DT", dt)
    cases = [c for c in group.cases if c.id == "te_acc_by_step_kind"]
    assert len(cases) == 1
    assert_all_passed(run(Group("drum_close", cases)))
