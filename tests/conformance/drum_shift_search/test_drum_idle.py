"""S6 drum probes, a drum not reached: the ``drum_idle`` group (rungs in main).

Part ``drum2`` (``suite.PARTS``).  CLICK has no nested call, so
the group's rungs run in the main program (``in_main``), and the first case's
drums live in a subroutine main does not call on some scans.  A drum that is
not reached on a scan keeps its rung memory, so when it is reached again the
rung has not "come back" and writes no row.  Each case runs a time drum and a
time & event drum, moved to step 2 with B on (the time drum on scan 1, the other on
scan 2, see ``test_drum_rule``), B reset on scan 3, and not reached on scans 3..4.  Snapshots as in ``test_drum_rule`` (``S`` then
``4 * flag + 2 * B + A``, the time drum's above the time & event drum's), on
scans 2, 4, 5 and 6: 2222, 2020 and then pyrung reads 2020 (Result4,
ErrorCode) when the drums are reached again on scan 5: a reset output stays
off.  A drum that treats the unreached scans as a false rung writes the row
when it is reached again and reads 2222 on scan 5.

- ``uncalled_subroutine`` (probe): the drums are in a subroutine whose call rung
  is false on scans 3..4.
- ``drum_in_skipped_loop`` (probe): the drums are in the body of a ``For`` of 1
  whose rung is false on scans 3..4 (CLICK skips a false For's body,
  ``loop/body_oneshot_holds_while_for_false``).

A drum in a For body may be refused at paste; the group is last in the part, so
its subroutine and rungs are the last to delete.  A STOP to RUN transition
(does it clear the rung memory) is not probed: the harness cannot switch the
PLC's mode.

The cases own C1751..C1780, T351..T358, DD681..DD688 and DS4399..DS4400
(``memory.OWNED``).
"""

from __future__ import annotations

from typing import Any

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import Rung, call, forloop, out, reset, subroutine
from tests.conformance.drum_rig import (
    Pair,
    Resources,
    emit_pair,
    on_scans,
    pool_of,
    snap,
    track_both,
)
from tests.conformance.scans import count_scans, done, scans_for

SCANS = scans_for()

RES = Resources(
    bits=pool_of(range(1751, 1781)),
    raw=[4399, 4400],
    raw_counts=(1, 1),
    tds=list(range(351, 359)),
    states=list(range(681, 689)),
    bits_per_case=15,
)


def _drive(slot: Any, p: Pair, skip: Any) -> None:
    """Both drums are on step 2 by scan 2; the drums are not reached on scans 3..4,
    when B is reset (above the drums)."""
    on_scans(slot, p.jog, 1)
    on_scans(slot, p.events[0], 2)
    with Rung(slot.Scratch >= 3, slot.Scratch <= 4):
        out(skip)
    with Rung(slot.Scratch == 3):
        reset(p.time.b)
        reset(p.event.b)


def _record(slot: Any, p: Pair) -> None:
    track_both(slot, p)
    snap(slot, p, 2, slot.Result2)
    snap(slot, p, 4, slot.Result3)
    snap(slot, p, 5, slot.Result4)
    snap(slot, p, 6, slot.ErrorCode)
    done(slot)


def prove_uncalled_subroutine(slot):
    r = RES.rig(slot, 0)
    skip = r.take(1)[0]
    p = r.alloc(0, jog=True)
    count_scans(slot)
    _drive(slot, p, skip)
    with Rung(~skip):
        call("Conf_DrumIdle_Pair")
    with subroutine("Conf_DrumIdle_Pair"):
        emit_pair(slot, p, jog=True)
    _record(slot, p)


def prove_drum_in_skipped_loop(slot):
    r = RES.rig(slot, 1)
    skip = r.take(1)[0]
    p = r.alloc(0, jog=True)
    count_scans(slot)
    _drive(slot, p, skip)
    with Rung(~skip):
        with forloop(1):
            emit_pair(slot, p, jog=True)
    _record(slot, p)


group = Group(
    "drum_idle",
    [
        case(
            "A drum in a subroutine that is not called keeps its rung memory: a time drum "
            "and a time & event drum on step 2 with B on (2222, Result2), B reset and the "
            "call skipped on scans 3..4 (2020, Result3), read 2020 when called again on "
            "scan 5 (Result4, ErrorCode) -- a reset output stays off",
            ladder=prove_uncalled_subroutine,
            expect={"Result2": 2222, "Result3": 2020, "Result4": 2020, "ErrorCode": 2020},
            scans=SCANS,
            probe="time and time & event drums: rung memory across an uncalled subroutine",
        ),
        case(
            "A drum in a For body that is skipped keeps its rung memory: the same drums "
            "in a For of 1 whose rung is false on scans 3..4 read 2222, 2020, 2020 and "
            "2020 (Result2, Result3, Result4, ErrorCode)",
            ladder=prove_drum_in_skipped_loop,
            expect={"Result2": 2222, "Result3": 2020, "Result4": 2020, "ErrorCode": 2020},
            scans=SCANS,
            probe="time and time & event drums: rung memory across a skipped For body",
        ),
    ],
    in_main=True,
)


def test_drum_idle():
    assert_all_passed(run(group))
