"""S6 drum probes, the rule: the ``drum_rule`` group (subroutine ``Conf_DrumRule``).

Part ``drum1`` (``suite.PARTS``).  S5 read the drum rule on event
drums (``drum_trace``); these ask the same of ``time_drum`` and
``time_event_drum``.  Each scenario runs one of each on the same inputs
(``drum_rig.Pair``) and records them together: a drum's state at the end of a
scan is two digits, ``S`` the step then ``X = 4 * flag + 2 * B + A`` (22 is
step 2 with B on, 24 step 2 complete with every output off, 10 step 1 with
both off), and a snapshot packs the time drum's state above the time & event
drum's, so 2222 is both on step 2 with B on and 2224 the time drum on step 2
with B on and the other complete.  Result2.. are the snapshots a case names,
scan by scan, so an answer names what each scan did.

The time drum's presets are 30 s (the cases that need a step to time out
stage its accumulator past a preset of 2 s instead, since over the wire a
preset cannot be timed in scans), and it moves by a jog; the time & event
drum's steps are event-only and move on their events.  The expectations are
what pyrung reads, which is the S5 drum rule (CHANGELOG: all three drums follow
it): a drum writes its outputs when its step moves or its rung comes back, not
on every scan, and a write to its step register is not a move.  A snapshot
that reads otherwise on a CLICK is an answer for that kind of drum.

The time & event drum is moved on scan 2 and the time drum on scan 1: a one-scan
event on scan 1 did not move it on a CLICK (2026-11-02), which ``drum_settle`` asks
of its own, and every scenario here starts from step 2.

- ``writes_on_move_only`` (probe): both drums on step 2 with B on from scan 2.
  Scenario A: B reset above the drums on scan 3 stays off, 2020 on scans 3 and
  4 (Result2, Result3); a drum that writes its row every scan reads 2222.
  Scenario B: step 1 written to both registers on scan 3 leaves B on, 1212
  (Result4); a drum that writes the row at once reads 1111.  The jog and the
  event on scan 5 act on the step in the register, 1, so both go to step 2
  (ErrorCode = 2222); a drum that goes on from its own step (2) completes,
  2424.
- ``writes_on_return`` (probe): the rung false on scans 3..4 with step 1 written
  over step 2 on scan 3: 1212 on scan 4 (Result2), and the rung coming back on
  scan 5 writes the step in the register, 1111 (Result3); 1212 on scan 5 says
  only a correction writes.  Then step 5 written on scan 3 of a running drum
  with the rung free and no jog corrects at once and leaves the outputs alone,
  1212 on scans 3 and 4 (Result4, ErrorCode); a drum that corrects on the next
  scan reads 5252 then 1212.
- ``bad_step_and_first_scan_jog`` (probe): step 5 written on scan 3 with a jog
  rising on that scan.  The jog scan leaves step 5 with the outputs alone
  (5252, Result2), the next scan corrects with no output written (1212,
  Result3); a drum that corrects on the jog scan reads 1212 then 1212, one that
  jogs from step 1 reads 2222.  The second pair has jog true from scan 1 with
  its step register 0 (the default: a rung after the drums writes 0 on every
  idle scan): the jog edge on step 0 is dropped and the step stands (0,
  Result4), and the next scan corrects to 1 with no output (1010, ErrorCode); a
  drum that jogs from step 0 or writes step 1's row on scan 1 reads other
  digits.  (The harness's scan 1 is the first scan of a drum whose rung has
  just come back, not the PLC's first scan.)
- ``correction_moves`` (probe): a time drum whose step 1 has a 2 s preset and
  Acc 2500, and a time & event drum whose step 1 event is on, with step 9
  written below the drums on scan 3 (so scan 4 is their first with it).  Both
  correct to step 1 on scan 4 and move on at once, writing step 2's row: 2222
  (Result3), with the time drum's Acc 0 (ErrorCode).  A correction that only
  corrects reads 1111 on scan 4 and 2222 on scan 5 (Result4).  Result2 = 1111
  is scan 3, before the write.
- ``last_step_jog`` (probe): the jog of ``drum/jog_on_last_step`` on both kinds,
  on scans 2, 4 and 6 of a 2-step drum: step 2 on scan 2 (2222, Result2), a jog
  on the last step completes it, flag on, outputs off, step 2 (2424, Result3),
  holding on scan 5 (Result4), and the further jog on scan 6 does nothing
  (2424, ErrorCode).

The cases own C101..C200 and C771..C800, T161..T180, DD641..DD660 and the DS
the group's rigs name (``memory.OWNED``).
"""

from __future__ import annotations

from typing import Any

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import Rung, copy, reset
from tests.conformance.drum_rig import (
    Pair,
    Resources,
    on_scans,
    pool_of,
    put,
    snap,
    track_both,
)
from tests.conformance.scans import count_scans, done, scans_for

SCANS = scans_for()

#: The group's raw memory (memory.OWNED): C bits 26 a case, DS step registers
#: 3 a case, TD 4 a case, DD states 4 a case.
RES = Resources(
    bits=pool_of(range(101, 201), range(771, 801)),
    raw=list(range(4191, 4202)),
    raw_counts=(3, 3, 3, 1, 1),
    tds=list(range(161, 181)),
    states=list(range(641, 661)),
)


def _up_to_step_2(slot: Any, p: Pair, *more: int) -> None:
    """Both drums are on step 2 by scan 2 (and move again on *more*): the time drum
    by its jog on scan 1, the time & event drum by its step 1 event on scan 2."""
    on_scans(slot, p.jog, 1, *more)
    on_scans(slot, p.events[0], 2, *more)


def prove_writes_on_move_only(slot):
    r = RES.rig(slot, 0)
    count_scans(slot)

    def a_above(p: Pair) -> None:
        _up_to_step_2(slot, p)
        with Rung(slot.Scratch == 3):
            reset(p.time.b)
            reset(p.event.b)

    a = r.pair(0, jog=True, above=a_above)
    track_both(slot, a)

    def b_above(p: Pair) -> None:
        _up_to_step_2(slot, p, 5)
        put(slot, 3, 1, p.time.step)
        put(slot, 3, 1, p.event.step)

    b = r.pair(1, jog=True, above=b_above)
    track_both(slot, b)
    snap(slot, a, 3, slot.Result2)
    snap(slot, a, 4, slot.Result3)
    snap(slot, b, 3, slot.Result4)
    snap(slot, b, 5, slot.ErrorCode)
    done(slot)


def prove_writes_on_return(slot):
    r = RES.rig(slot, 1)
    count_scans(slot)

    def a_above(p: Pair) -> None:
        _up_to_step_2(slot, p)
        with Rung(slot.Scratch >= 3, slot.Scratch <= 4):
            from pyrung.core import out

            out(p.pause)
        put(slot, 3, 1, p.time.step)
        put(slot, 3, 1, p.event.step)

    a = r.pair(0, jog=True, pausable=True, above=a_above)
    track_both(slot, a)

    def b_above(p: Pair) -> None:
        _up_to_step_2(slot, p)
        put(slot, 3, 5, p.time.step)
        put(slot, 3, 5, p.event.step)

    b = r.pair(1, jog=True, above=b_above)
    track_both(slot, b)
    snap(slot, a, 4, slot.Result2)
    snap(slot, a, 5, slot.Result3)
    snap(slot, b, 3, slot.Result4)
    snap(slot, b, 4, slot.ErrorCode)
    done(slot)


def prove_bad_step_and_first_scan_jog(slot):
    r = RES.rig(slot, 2)
    count_scans(slot)

    def a_above(p: Pair) -> None:
        on_scans(slot, p.jog, 1, 3)
        put(slot, 3, 5, p.time.step)
        put(slot, 3, 5, p.event.step)

    a = r.pair(0, jog=True, te_jog=True, above=a_above)
    track_both(slot, a)

    def b_above(p: Pair) -> None:
        on_scans(slot, p.jog, 1, 2, 3, 4)

    b = r.pair(1, jog=True, te_jog=True, above=b_above)
    # The step registers are 0 when the first scan comes (the drums' reset
    # writes step 1 on every idle scan, so this rung is after them).
    with Rung(slot.Cmd == 0):
        copy(0, b.time.step)
        copy(0, b.event.step)
    track_both(slot, b)
    snap(slot, a, 3, slot.Result2)
    snap(slot, a, 4, slot.Result3)
    snap(slot, b, 1, slot.Result4)
    snap(slot, b, 2, slot.ErrorCode)
    done(slot)


def prove_correction_moves(slot):
    r = RES.rig(slot, 3)
    count_scans(slot)

    def above(p: Pair) -> None:
        on_scans(slot, p.events[0], 4)

    p = r.pair(0, presets=[2000, 30000], above=above)
    track_both(slot, p)
    # Written below the drums, so scan 4 is the drums' first with step 9.
    put(slot, 3, 9, p.time.step)
    put(slot, 3, 9, p.event.step)
    put(slot, 3, 2500, p.time.acc)
    snap(slot, p, 3, slot.Result2)
    snap(slot, p, 4, slot.Result3)
    snap(slot, p, 5, slot.Result4)
    with Rung(slot.Scratch == 4):
        copy(p.time.acc, slot.ErrorCode)
    done(slot)


def prove_last_step_jog(slot):
    r = RES.rig(slot, 4)
    count_scans(slot)

    def above(p: Pair) -> None:
        on_scans(slot, p.jog, 2, 4, 6)

    p = r.pair(0, jog=True, te_jog=True, above=above)
    track_both(slot, p)
    snap(slot, p, 2, slot.Result2)
    snap(slot, p, 4, slot.Result3)
    snap(slot, p, 5, slot.Result4)
    snap(slot, p, 6, slot.ErrorCode)
    done(slot)


group = Group(
    "drum_rule",
    [
        case(
            "A time drum and a time & event drum write their outputs when the step moves, "
            "not every scan, and a write to the step register is not a move: both on step 2 "
            "with B on, B reset on scan 3 stays off (Result2, Result3 = 2020); in another "
            "pair step 1 written over step 2 on scan 3 leaves B on (Result4 = 1212), and "
            "the jog or event on scan 5 acts on step 1, to step 2 (ErrorCode = 2222)",
            ladder=prove_writes_on_move_only,
            expect={"Result2": 2020, "Result3": 2020, "Result4": 1212, "ErrorCode": 2222},
            scans=SCANS,
            probe="time and time & event drums: do they write outputs only on a move",
        ),
        case(
            "A drum whose rung comes back writes the step in its register, and a bad step "
            "in a running drum corrects at once and writes nothing: step 1 written over "
            "step 2 while paused reads 1212 on scan 4 (Result2) and 1111 on scan 5 "
            "(Result3); step 5 written on scan 3 of a free-running drum reads 1212 on "
            "scans 3 and 4 (Result4, ErrorCode)",
            ladder=prove_writes_on_return,
            expect={"Result2": 1212, "Result3": 1111, "Result4": 1212, "ErrorCode": 1212},
            scans=SCANS,
            probe="time and time & event drums: rung returning, correction with no jog",
        ),
        case(
            "A jog on a bad step leaves that scan alone and the next corrects with no "
            "output written: step 5 and a jog on scan 3 read 5252 then 1212 (Result2, "
            "Result3); and a jog on the default step 0 on a first scan is dropped, 0 "
            "(Result4), then step 1 with no output (ErrorCode = 1010)",
            ladder=prove_bad_step_and_first_scan_jog,
            expect={"Result2": 5252, "Result3": 1212, "Result4": 0, "ErrorCode": 1010},
            scans=SCANS,
            probe="time and time & event drums: jog on a bad step, jog on step 0",
        ),
        case(
            "A correction scan can move: a time drum with Acc 2500 past step 1's 2 s "
            "preset and a time & event drum whose step 1 event is on, with step 9 written "
            "below them on scan 3, correct on scan 4 and move to step 2 at once, row 2 "
            "written (Result3 = 2222), the time drum's Acc 0 (ErrorCode); step 1 with "
            "its row is scan 3 (Result2 = 1111)",
            ladder=prove_correction_moves,
            expect={"Result2": 1111, "Result3": 2222, "Result4": 2222, "ErrorCode": 0},
            scans=SCANS,
            probe="time and time & event drums: a correction scan that also moves",
        ),
        case(
            "A jog on the last step completes a time drum and a time & event drum, which "
            "stay on that step with every output off, and a further jog does nothing: "
            "jogged on scans 2, 4 and 6 of 2 steps they read 2222, 2424, 2424 and 2424 "
            "(Result2, Result3, Result4, ErrorCode)",
            ladder=prove_last_step_jog,
            expect={"Result2": 2222, "Result3": 2424, "Result4": 2424, "ErrorCode": 2424},
            scans=SCANS,
            probe="time and time & event drums: jog on the last step, and again",
        ),
    ],
)


def test_drum_rule():
    assert_all_passed(run(group))
