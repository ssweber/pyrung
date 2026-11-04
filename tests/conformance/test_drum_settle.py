"""S6 drum probes, what the others left open: the ``drum_settle`` group (``Conf_DrumSettle``).

Part G of the CLICK project (``suite.PARTS``).  The 2026-11-02 run of ``drum_rule``,
``drum_jump`` and ``drum_idle`` left two kinds of question.  A time & event drum
did not move on a one-scan event on the harness's scan 1, though the same pulse
moves it on later scans; and a completed drum did not take a jump, its own time or
its own event as pyrung then did.  These cases told the readings apart, on a CLICK
the same day: their expectations are what the CLICK read.

Snapshots as in ``test_drum_rule``: two digits a drum, ``S`` then ``4 * flag + 2 *
B + A``, and the drum named first above the other, so 2211 is the first on step 2
with B on and the second on step 1 with A on.  The drums here are not always one of
each, so each case names its two.  A 3-step drum's step 3 has both outputs on (33).
An event pulse names the step it is for: a pulse for step 1 leaves a drum on step
2 alone, so a control pulse of both step 1's and step 2's events says which step a
drum is on by where it goes.

The return scan.  The scan a drum's rung comes back, or a reset under a true rung
ends, takes no event and no time end, on any kind of drum:

- ``te_event_on_return_scan``: a one-scan event on the return scan moves neither a
  time & event drum nor an event drum, 1111 on scans 5 and 6; the control on scan 7
  finds both on step 1, 2222.  No reset is involved.
- ``te_event_after_reset``: the rung true all along, an event on scan 1 and again on
  the scan after a reset, 1111 each time; the control on scan 6 reads 2222.
- ``te_event_on_entry``: not the step being new, a step entered on scan 2 by an event
  or a jog takes an event on scan 3.
- ``te_level_from_scan_1``: a level from scan 1 moves a time & event drum on scan 2,
  2211, and an event drum never: its edge is spent on the return scan.
- ``te_event_with_acc_staged``: not a want of time, a time drum with Acc past its preset
  stays on the return scan and moves on the next, 1111 then 2211, and Acc staged into a
  time & event drum does not make it take the event.

A completed drum.  While the completion flag is on, the drum's own time and event do
nothing and its accumulator holds; a jog forward still writes the row:

- ``after_jump_jog_alone``: completed by a jog and jumped to step 1, 1414, a jog alone
  brings step 2's row back, 2626, and step 2's own end does not complete it again, 2626.
- ``acc_in_completed_drum``: a time drum completed by its time keeps its Acc, 25 hundreds,
  moving on none of scans 5..12, and one jumped and jogged back, 2426, moves on none of
  scans 8..12.
- ``event_drum_after_completion``: an event drum as the other two.  One jumped to step 1
  (14) does not take step 1's event, and one jogged on its last step while paused turns
  off at once: 2424, 1424, 1424, 1424.

The correction.  ``correction_with_acc_staged``: step 5 and Acc 900 staged on scan 3 with
no jog, a time drum over a time & event drum whose steps have no time.  The scan corrects
and leaves the row alone, 1111, and neither Acc clears, 9 hundreds each.  The time drum
moves on all of scans 4..8; the other on none, an event-only step not timing.

The cases own C1852..C1900 and C582..C660, T359..T376, DD718..DD753 and DS4208..DS4222,
DS4227..DS4230 (``memory.OWNED``).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import Or, Rung, calc, copy, event_drum, out, time_drum, time_event_drum
from tests.conformance.drum_rig import LONG, Pool, Side, on_scans, pattern_for, pool_of, put, track
from tests.conformance.memory import dd, ds, td
from tests.conformance.scans import count_scans, done, scans_for

SCANS = scans_for()
#: ``after_jump_jog_alone`` reads a scan past the default window.
AFTER_JUMP_WINDOW = 9
#: ``acc_in_completed_drum`` counts Acc's moves past the completion and the jog back.
LONG_WINDOW = 12

_BITS = pool_of(range(1852, 1901), range(582, 661))
_BITS_PER_CASE = 14
_STEPS = [*range(4208, 4223), *range(4227, 4231)]
_TDS = range(359, 377)
_DDS = range(718, 754)


@dataclass(frozen=True)
class Rig:
    """The tags of a case: two drums (:meth:`side`) with their step, Acc and state."""

    slot: Any
    index: int
    take: Pool

    def side(self, which: int) -> Side:
        a, b, flag = self.take(3)
        i = 2 * self.index + which
        return Side(ds[_STEPS[i]], td[_TDS[i]], a, b, flag, dd[_DDS[4 * self.index + which]])

    @property
    def spare_dd(self) -> Any:
        """A DD of the case's own, for :func:`moves`."""
        return dd[_DDS[4 * self.index + 2]]

    @property
    def second_spare_dd(self) -> Any:
        return dd[_DDS[4 * self.index + 3]]


def rig(slot: Any, index: int) -> Rig:
    return Rig(slot, index, _BITS(index, _BITS_PER_CASE))


def _gate(slot: Any, pause: Any, free: bool) -> list[Any]:
    gate = [] if free else [slot.Cmd != 0]
    if pause is not None:
        gate.append(~pause)
    return gate


def _pins(drum: Any, reset: Any, jog: Any, jump: Any, jump_step: Any) -> None:
    drum = drum.reset(reset)
    if jump is not None:
        drum = drum.jump(jump, step=jump_step)
    if jog is not None:
        drum.jog(jog)


def time_side(
    slot: Any,
    s: Side,
    presets: Sequence[int] = (LONG, LONG),
    *,
    pause: Any = None,
    jog: Any = None,
    jump: Any = None,
    jump_step: Any = None,
) -> None:
    with Rung(*_gate(slot, pause, False)):
        drum = time_drum(
            outputs=[s.a, s.b],
            presets=list(presets),
            unit="ms",
            pattern=pattern_for(len(presets)),
            current_step=s.step,
            accumulator=s.acc,
            completion_flag=s.flag,
        )
        _pins(drum, slot.Cmd == 0, jog, jump, jump_step)


def te_side(
    slot: Any,
    s: Side,
    events: Sequence[Any],
    *,
    presets: Sequence[int | None] | None = None,
    pause: Any = None,
    jog: Any = None,
    jump: Any = None,
    jump_step: Any = None,
    reset: Any = None,
    free: bool = False,
) -> None:
    with Rung(*_gate(slot, pause, free)):
        drum = time_event_drum(
            outputs=[s.a, s.b],
            presets=list(presets) if presets is not None else [None] * len(events),
            events=list(events),
            unit="ms",
            pattern=pattern_for(len(events)),
            current_step=s.step,
            accumulator=s.acc,
            completion_flag=s.flag,
        )
        _pins(drum, slot.Cmd == 0 if reset is None else reset, jog, jump, jump_step)


def event_side(
    slot: Any,
    s: Side,
    events: Sequence[Any],
    *,
    pause: Any = None,
    jog: Any = None,
    jump: Any = None,
    jump_step: Any = None,
    reset: Any = None,
    free: bool = False,
) -> None:
    with Rung(*_gate(slot, pause, free)):
        drum = event_drum(
            outputs=[s.a, s.b],
            events=list(events),
            pattern=pattern_for(len(events)),
            current_step=s.step,
            completion_flag=s.flag,
        )
        _pins(drum, slot.Cmd == 0 if reset is None else reset, jog, jump, jump_step)


def pause_on(slot: Any, bit: Any, first: int, last: int) -> None:
    with Rung(slot.Scratch >= first, slot.Scratch <= last):
        out(bit)


def snap_two(slot: Any, top: Side, under: Side, scan: int, into: Any) -> None:
    with Rung(slot.Scratch == scan):
        calc(top.state * 100 + under.state, into)


def acc_hundreds(slot: Any, s: Side, scan: int, into: Any) -> None:
    with Rung(slot.Scratch == scan):
        calc(s.acc / 100, into)


def moves(slot: Any, acc: Any, prev: Any, first: int, last: int, into: Any) -> None:
    """*into* = the scans of *first*..*last* on which *acc* moved up.  Below the drum."""
    with Rung(slot.Scratch >= first, slot.Scratch <= last, acc > prev):
        calc(into + 1, into)
    with Rung():
        copy(acc, prev)


def prove_te_event_on_return_scan(slot):
    r = rig(slot, 0)
    te, ev = r.side(0), r.side(1)
    pause, e1, e2, e3 = r.take(4)
    count_scans(slot)
    pause_on(slot, pause, 3, 4)
    on_scans(slot, e1, 5, 7)
    on_scans(slot, e2, 7)
    te_side(slot, te, [e1, e2, e3], pause=pause)
    event_side(slot, ev, [e1, e2, e3], pause=pause)
    track(slot, te)
    track(slot, ev)
    snap_two(slot, te, ev, 4, slot.Result1)
    snap_two(slot, te, ev, 5, slot.Result2)
    snap_two(slot, te, ev, 6, slot.Result3)
    snap_two(slot, te, ev, 7, slot.Result4)
    done(slot)


def prove_te_event_after_reset(slot):
    r = rig(slot, 1)
    te, ev = r.side(0), r.side(1)
    reset, e1, e2, e3 = r.take(4)
    count_scans(slot)
    with Rung(Or(slot.Cmd == 0, slot.Scratch == 3)):
        out(reset)
    on_scans(slot, e1, 1, 4, 6)
    on_scans(slot, e2, 6)
    te_side(slot, te, [e1, e2, e3], reset=reset, free=True)
    event_side(slot, ev, [e1, e2, e3], reset=reset, free=True)
    track(slot, te)
    track(slot, ev)
    snap_two(slot, te, ev, 1, slot.Result1)
    snap_two(slot, te, ev, 2, slot.Result2)
    snap_two(slot, te, ev, 4, slot.Result3)
    snap_two(slot, te, ev, 5, slot.Result4)
    snap_two(slot, te, ev, 6, slot.ErrorCode)
    done(slot)


def prove_te_event_on_entry(slot):
    r = rig(slot, 2)
    by_event, by_jog = r.side(0), r.side(1)
    jog, e1, e2, e3, f1, f2, f3 = r.take(7)
    count_scans(slot)
    on_scans(slot, e1, 2)
    on_scans(slot, jog, 2)
    on_scans(slot, e2, 3)
    on_scans(slot, f2, 3)
    te_side(slot, by_event, [e1, e2, e3])
    te_side(slot, by_jog, [f1, f2, f3], jog=jog)
    track(slot, by_event)
    track(slot, by_jog)
    snap_two(slot, by_event, by_jog, 2, slot.Result2)
    snap_two(slot, by_event, by_jog, 3, slot.Result3)
    snap_two(slot, by_event, by_jog, 4, slot.Result4)
    done(slot)


def prove_te_level_from_scan_1(slot):
    r = rig(slot, 3)
    te, ev = r.side(0), r.side(1)
    e1, e2 = r.take(2)
    count_scans(slot)
    with Rung(slot.Cmd != 0):
        out(e1)
    te_side(slot, te, [e1, e2])
    event_side(slot, ev, [e1, e2])
    track(slot, te)
    track(slot, ev)
    snap_two(slot, te, ev, 1, slot.Result2)
    snap_two(slot, te, ev, 2, slot.Result3)
    snap_two(slot, te, ev, 3, slot.Result4)
    done(slot)


def prove_te_event_with_acc_staged(slot):
    r = rig(slot, 4)
    tm, te = r.side(0), r.side(1)
    pause, e1, e2 = r.take(3)
    count_scans(slot)
    pause_on(slot, pause, 3, 4)
    on_scans(slot, e1, 5)
    put(slot, 4, 2500, tm.acc)
    put(slot, 4, 500, te.acc)
    time_side(slot, tm, (2000, LONG), pause=pause)
    te_side(slot, te, [e1, e2], pause=pause)
    track(slot, tm)
    track(slot, te)
    snap_two(slot, tm, te, 4, slot.Result2)
    snap_two(slot, tm, te, 5, slot.Result3)
    snap_two(slot, tm, te, 6, slot.Result4)
    done(slot)


def prove_after_jump_jog_alone(slot):
    r = rig(slot, 5)
    tm, te = r.side(0), r.side(1)
    jog, jump, e1, e2 = r.take(4)
    count_scans(slot, AFTER_JUMP_WINDOW)
    # Result1 holds the jump step, 1 (a DS address).
    put(slot, 1, 1, slot.Result1)
    on_scans(slot, jog, 1, 3, 7)
    on_scans(slot, jump, 5)
    on_scans(slot, e2, 8)
    put(slot, 8, 2500, tm.acc)
    time_side(slot, tm, (LONG, 2000), jog=jog, jump=jump, jump_step=slot.Result1)
    te_side(slot, te, [e1, e2], jog=jog, jump=jump, jump_step=slot.Result1)
    track(slot, tm)
    track(slot, te)
    snap_two(slot, tm, te, 5, slot.Result2)
    snap_two(slot, tm, te, 7, slot.Result3)
    snap_two(slot, tm, te, 8, slot.Result4)
    snap_two(slot, tm, te, 9, slot.ErrorCode)
    done(slot, AFTER_JUMP_WINDOW)


def prove_acc_in_completed_drum(slot):
    r = rig(slot, 6)
    by_time, by_jog = r.side(0), r.side(1)
    jog_time, jog, jump = r.take(3)
    count_scans(slot, LONG_WINDOW)
    # Result1 holds the jump step, 1 (a DS address).
    put(slot, 1, 1, slot.Result1)
    on_scans(slot, jog_time, 1)
    put(slot, 3, 2500, by_time.acc)
    on_scans(slot, jog, 1, 3, 7)
    on_scans(slot, jump, 5)
    time_side(slot, by_time, (LONG, 2000), jog=jog_time)
    time_side(slot, by_jog, jog=jog, jump=jump, jump_step=slot.Result1)
    track(slot, by_time)
    track(slot, by_jog)
    acc_hundreds(slot, by_time, 4, slot.Result2)
    snap_two(slot, by_time, by_jog, 8, slot.Result4)
    moves(slot, by_time.acc, r.spare_dd, 5, LONG_WINDOW, slot.Result3)
    moves(slot, by_jog.acc, r.second_spare_dd, 8, LONG_WINDOW, slot.ErrorCode)
    done(slot, LONG_WINDOW)


def prove_event_drum_after_completion(slot):
    r = rig(slot, 7)
    jumped, paused = r.side(0), r.side(1)
    jog, jump, e1, e2, jog2, pause, f1, f2 = r.take(8)
    count_scans(slot)
    # Result1 holds the jump step, 1 (a DS address).
    put(slot, 1, 1, slot.Result1)
    on_scans(slot, jog, 1, 3)
    on_scans(slot, jump, 5)
    on_scans(slot, e1, 6)
    on_scans(slot, jog2, 2, 4)
    pause_on(slot, pause, 3, 5)
    event_side(slot, jumped, [e1, e2], jog=jog, jump=jump, jump_step=slot.Result1)
    event_side(slot, paused, [f1, f2], pause=pause, jog=jog2)
    track(slot, jumped)
    track(slot, paused)
    snap_two(slot, jumped, paused, 4, slot.Result2)
    snap_two(slot, jumped, paused, 5, slot.Result3)
    snap_two(slot, jumped, paused, 6, slot.Result4)
    snap_two(slot, jumped, paused, 7, slot.ErrorCode)
    done(slot)


def prove_correction_with_acc_staged(slot):
    r = rig(slot, 8)
    tm, te = r.side(0), r.side(1)
    e1, e2 = r.take(2)
    count_scans(slot)
    put(slot, 3, 5, tm.step)
    put(slot, 3, 5, te.step)
    put(slot, 3, 900, tm.acc)
    put(slot, 3, 900, te.acc)
    time_side(slot, tm)
    te_side(slot, te, [e1, e2])
    track(slot, tm)
    track(slot, te)
    snap_two(slot, tm, te, 3, slot.Result2)
    acc_hundreds(slot, tm, 3, slot.Result3)
    acc_hundreds(slot, te, 3, slot.Result4)
    moves(slot, tm.acc, r.spare_dd, 4, 8, slot.ErrorCode)
    moves(slot, te.acc, r.second_spare_dd, 4, 8, slot.Result1)
    done(slot)


group = Group(
    "drum_settle",
    [
        case(
            "A drum takes no event on the scan its rung returns: paused on scans 3..4 "
            "(Result1), step 1's event on scan 5 moves neither a time & event drum nor an "
            "event drum (Result2, Result3), and the control events on scan 7 find both on "
            "step 1 and move them to step 2 (Result4)",
            ladder=prove_te_event_on_return_scan,
            expect={"Result1": 1111, "Result2": 1111, "Result3": 1111, "Result4": 2222},
            scans=SCANS,
        ),
        case(
            "A drum takes no event on the scan a reset ended, its rung true all along: "
            "step 1's event on scan 1 moves neither a time & event drum nor an event drum "
            "(Result1, Result2), a reset on scan 3 and the event on scan 4 again (Result3, "
            "Result4), and the control events on scan 6 move both to step 2 (ErrorCode)",
            ladder=prove_te_event_after_reset,
            expect={
                "Result1": 1111,
                "Result2": 1111,
                "Result3": 1111,
                "Result4": 1111,
                "ErrorCode": 2222,
            },
            scans=SCANS,
        ),
        case(
            "A time & event drum takes an event on the scan after it entered a step: "
            "entered on scan 2 by an event, and by a jog in the drum beside it (Result2), "
            "step 2's event on scan 3 moves both to step 3 (Result3, Result4)",
            ladder=prove_te_event_on_entry,
            expect={"Result2": 2222, "Result3": 3333, "Result4": 3333},
            scans=SCANS,
            probe="time & event drum: an event on the scan after a step entered",
        ),
        case(
            "A level event on from scan 1 moves a time & event drum on scan 2, not on the "
            "return scan, and an event drum beside it never, its edge spent: 1111, 2211, "
            "2211 (Result2, Result3, Result4)",
            ladder=prove_te_level_from_scan_1,
            expect={"Result2": 1111, "Result3": 2211, "Result4": 2211},
            scans=SCANS,
        ),
        case(
            "A drum whose time is up does not move on the scan its rung returns: paused on "
            "scans 3..4 with Acc staged past a time drum's preset and into a time & event "
            "drum (Result2), the return scan moves neither (Result3), the next the time "
            "drum only, the other's event spent (Result4)",
            ladder=prove_te_event_with_acc_staged,
            expect={"Result2": 1111, "Result3": 1111, "Result4": 2211},
            scans=SCANS,
        ),
        case(
            "A jog alone brings a jumped completed drum's row back and its own end does "
            "nothing: a time drum and a time & event drum completed by a jog and jumped to "
            "step 1 (Result2), jogged on scan 7 write step 2's row with the flag on (Result3), "
            "and step 2's own time and event on scan 8 leave it (Result4, ErrorCode)",
            ladder=prove_after_jump_jog_alone,
            expect={"Result2": 1414, "Result3": 2626, "Result4": 2626, "ErrorCode": 2626},
            scans=scans_for(AFTER_JUMP_WINDOW),
        ),
        case(
            "A completed time drum's Acc holds: one completed by its time keeps 25 hundreds "
            "(Result2) and moves on no scan after (Result3); one completed by a jog, jumped "
            "and jogged back (Result4) moves on none either (ErrorCode)",
            ladder=prove_acc_in_completed_drum,
            expect={"Result2": 25, "Result3": 0, "Result4": 2426, "ErrorCode": 0},
            scans=scans_for(LONG_WINDOW),
        ),
        case(
            "An event drum completed by a jog and jumped to step 1 does not take step 1's "
            "event, and one jogged on its last step while paused turns off at once: 2424, "
            "1424, 1424, 1424 (Result2, Result3, Result4, ErrorCode)",
            ladder=prove_event_drum_after_completion,
            expect={"Result2": 2424, "Result3": 1424, "Result4": 1424, "ErrorCode": 1424},
            scans=SCANS,
        ),
        case(
            "A correction keeps a drum's Acc: step 5 and Acc 900 staged on scan 3 of a time "
            "drum and an event-only time & event drum read their row (Result2), 9 hundreds "
            "each (Result3, Result4); the time drum counts on (ErrorCode), and the other, "
            "whose step has no time, does not (Result1)",
            ladder=prove_correction_with_acc_staged,
            expect={
                "Result1": 0,
                "Result2": 1111,
                "Result3": 9,
                "Result4": 9,
                "ErrorCode": range(1, 6),
            },
            scans=SCANS,
        ),
    ],
)


def test_drum_settle():
    assert_all_passed(run(group))
