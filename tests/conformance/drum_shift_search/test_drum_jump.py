"""S6 drum probes, jog, jump and pause: the ``drum_jump`` group (subroutine ``Conf_DrumJump``).

Part ``drum1`` (``suite.PARTS``).  The snapshots are those of
``test_drum_rule`` (two digits a drum, ``S`` then ``4 * flag + 2 * B + A``,
the time drum's above the time & event drum's), taken on a time drum and a
time & event drum side by side.  The expectations are what pyrung reads, the
S5 drum rule extended to the two kinds (docs/instructions/drum-shift-search.md);
a snapshot that reads otherwise on a CLICK is an answer.

- ``paused_jog_cases`` (answered 2026-11-02): scenario A, a jog on the
  last step while the rung is false (scans 3..5) completes the drum at once with its
  outputs off, 2424 on scan 4 (Result2) and on scan 6 when the rung returns (Result3).
  Scenario B, a jog rising on a bad step (5, written on scan 3) while the rung is
  false (scans 3..6): the jog is dropped, so when the rung returns on scan 7 the
  drum corrects to step 1 and writes its row, 1111 (Result4), and stays, 1111
  (ErrorCode).  A jog that is kept moves it on, reading 2222 or 2424.
- ``jump_with_jog_and_to_own_step`` (probe): scenario A, a jog on the last step
  with a jump to step 1 on the same scan (3): the jump wins and the drum is not
  completed, 1111 on scan 3 (Result2) and 1111 on scan 4 (Result3); a drum the
  jog completes first reads 1414.  Scenario B, a jump to the step the drum is
  already on (2, with B reset above the drums on scan 4): it counts as a move
  and writes the row, 2222 (Result4, ErrorCode); a drum that ignores it reads
  2020.
- ``jump_after_completion`` (answered 2026-11-02): scenario A, a 2-step
  drum completed by a jog on scan 3, then a jump to step 1 on scan 5: the step moves
  but the outputs stay off, flag on, 1414 (Result2), and a jog with a jump on scan 7
  leaves it, 1414 (Result3), the jump beating the jog.  Scenario B, after the same jump
  (1414, Result4), step 1's own end (the time drum's Acc staged past its 2 s preset, the
  other's event) does not move it either, 1414 (ErrorCode).
  ``drum_settle/after_jump_jog_alone`` asks what a jog alone does there.
- ``completed_drum_fires_again`` (probe): both drums complete on scan 4 (the time
  drum by Acc staged past its 2 s preset, the other by its level event, on from scan
  4; it reaches step 2 on scan 2, see ``test_drum_rule``), 2424 (Result2).  B turned on
  from outside on scan 5 stays on while the drums fire again (their time and event
  still true) and rewrite only the flag: 2626 (Result3, Result4, ErrorCode).  A drum
  that writes every output off again reads 2424 throughout.
- ``acc_through_jog_completion`` (answered 2026-11-02): a time drum with
  Acc 900 staged on scan 2 is jogged on its last step on scan 3: the completion keeps
  the accumulator, Acc / 100 = 9 (Result2), and it then holds, moving on none of scans
  4..8 (Result3 = 0).  A second drum is given step 5 and a
  jog (the hold scan) on scan 3 with Acc 900 staged above it: the hold scan does not
  tick the accumulator, so Acc - 900 = 0 (Result4), and the next scan's correction
  keeps it, 900..999 (ErrorCode, 910 on the CLICK).

The cases own C1110..C1200 and C1401..C1470, T251..T270, DD661..DD680 and the DS
the group's rigs name (``memory.OWNED``).
"""

from __future__ import annotations

from typing import Any

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import Rung, copy, latch, math, out, reset
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

RES = Resources(
    bits=pool_of(range(1110, 1201), range(1401, 1471)),
    raw=[*range(3993, 4001), 4202, *range(4395, 4399), 4500],
    raw_counts=(3, 3, 4, 1, 3),
    tds=list(range(251, 271)),
    states=list(range(661, 681)),
)


def _pause_on(slot: Any, p: Pair, first: int, last: int) -> None:
    with Rung(slot.Scratch >= first, slot.Scratch <= last):
        out(p.pause)


def prove_paused_jog_cases(slot):
    r = RES.rig(slot, 0)
    count_scans(slot)

    def a_above(p: Pair) -> None:
        on_scans(slot, p.jog, 2, 4)
        _pause_on(slot, p, 3, 5)

    a = r.pair(0, jog=True, te_jog=True, pausable=True, above=a_above)
    track_both(slot, a)

    def b_above(p: Pair) -> None:
        on_scans(slot, p.jog, 1, 4)
        _pause_on(slot, p, 3, 6)
        put(slot, 3, 5, p.time.step)
        put(slot, 3, 5, p.event.step)

    b = r.pair(1, jog=True, te_jog=True, pausable=True, above=b_above)
    track_both(slot, b)
    snap(slot, a, 4, slot.Result2)
    snap(slot, a, 6, slot.Result3)
    snap(slot, b, 7, slot.Result4)
    snap(slot, b, 8, slot.ErrorCode)
    done(slot)


def prove_jump_with_jog_and_to_own_step(slot):
    r = RES.rig(slot, 1)
    count_scans(slot)
    # The jump step of both scenarios is ErrorCode (a drum's jump step is a DS
    # address): 1 for scenario A's jump on scan 3, 2 for B's on scan 4.
    put(slot, 1, 1, slot.ErrorCode)
    put(slot, 4, 2, slot.ErrorCode)

    def a_above(p: Pair) -> None:
        on_scans(slot, p.jog, 1, 3)
        on_scans(slot, p.jump, 3)

    a = r.pair(0, jog=True, te_jog=True, jump=True, jump_step=slot.ErrorCode, above=a_above)
    track_both(slot, a)

    def b_above(p: Pair) -> None:
        on_scans(slot, p.jog, 1)
        on_scans(slot, p.jump, 4)
        with Rung(slot.Scratch == 4):
            reset(p.time.b)
            reset(p.event.b)

    b = r.pair(1, jog=True, te_jog=True, jump=True, jump_step=slot.ErrorCode, above=b_above)
    track_both(slot, b)
    snap(slot, a, 3, slot.Result2)
    snap(slot, a, 4, slot.Result3)
    snap(slot, b, 4, slot.Result4)
    snap(slot, b, 5, slot.ErrorCode)
    done(slot)


def prove_jump_after_completion(slot):
    r = RES.rig(slot, 2)
    count_scans(slot)
    # Result1 holds the jump step, 1 (a DS address), so the drums step in raw DS.
    put(slot, 1, 1, slot.Result1)

    def a_above(p: Pair) -> None:
        on_scans(slot, p.jog, 1, 3, 7)
        on_scans(slot, p.jump, 5, 7)

    a = r.pair(
        0,
        own_steps=(r.raw[0], r.raw[1]),
        jog=True,
        te_jog=True,
        jump=True,
        jump_step=slot.Result1,
        above=a_above,
    )
    track_both(slot, a)

    def b_above(p: Pair) -> None:
        on_scans(slot, p.jog, 1, 3)
        on_scans(slot, p.jump, 5)
        on_scans(slot, p.events[0], 6)

    b = r.pair(
        1,
        own_steps=(r.raw[2], r.raw[3]),
        jog=True,
        te_jog=True,
        jump=True,
        jump_step=slot.Result1,
        presets=[2000, 30000],
        above=b_above,
    )
    track_both(slot, b)
    put(slot, 5, 2500, b.time.acc)
    snap(slot, a, 5, slot.Result2)
    snap(slot, a, 7, slot.Result3)
    snap(slot, b, 5, slot.Result4)
    snap(slot, b, 6, slot.ErrorCode)
    done(slot)


def prove_completed_drum_fires_again(slot):
    r = RES.rig(slot, 3)
    count_scans(slot)

    def above(p: Pair) -> None:
        on_scans(slot, p.jog, 1)
        on_scans(slot, p.events[0], 2)
        # The last step's event is a level, on from scan 4.
        with Rung(slot.Scratch >= 4):
            out(p.events[1])
        # An output turned on from outside after the completion.
        with Rung(slot.Scratch == 5):
            latch(p.time.b)
            latch(p.event.b)

    p = r.pair(0, jog=True, presets=[30000, 2000], above=above)
    track_both(slot, p)
    put(slot, 3, 2500, p.time.acc)
    snap(slot, p, 4, slot.Result2)
    snap(slot, p, 5, slot.Result3)
    snap(slot, p, 6, slot.Result4)
    snap(slot, p, 7, slot.ErrorCode)
    done(slot)


def prove_acc_through_jog_completion(slot):
    r = RES.rig(slot, 4)
    count_scans(slot)

    def a_above(p: Pair) -> None:
        on_scans(slot, p.jog, 1, 3)

    a = r.pair(0, jog=True, above=a_above)
    # Acc 900 staged on scan 2, below the drum; the jog on scan 3 completes it.
    put(slot, 2, 900, a.time.acc)
    prev = a.time.state
    with Rung(slot.Scratch == 3):
        math(a.time.acc / 100, slot.Result2)
    with Rung(slot.Scratch >= 4, slot.Scratch <= 8, a.time.acc > prev):
        math(slot.Result3 + 1, slot.Result3)
    with Rung():
        copy(a.time.acc, prev)

    def b_above(p: Pair) -> None:
        on_scans(slot, p.jog, 1, 3)
        put(slot, 3, 5, p.time.step)
        put(slot, 3, 5, p.event.step)
        put(slot, 3, 900, p.time.acc)

    b = r.pair(1, jog=True, above=b_above)
    with Rung(slot.Scratch == 3):
        math(b.time.acc - 900, slot.Result4)
    with Rung(slot.Scratch == 4):
        copy(b.time.acc, slot.ErrorCode)
    done(slot)


group = Group(
    "drum_jump",
    [
        case(
            "A paused drum takes a jog on its last step and drops one on a bad step: a "
            "jog on the last step while paused completes it at once with every output off "
            "(2424, Result2, Result3); a jog on step 5 while paused is dropped, so the "
            "returning rung corrects to step 1 and writes its row (1111, Result4, ErrorCode)",
            ladder=prove_paused_jog_cases,
            expect={"Result2": 2424, "Result3": 2424, "Result4": 1111, "ErrorCode": 1111},
            scans=SCANS,
        ),
        case(
            "A jump beats a jog on the last step and counts when it lands on the step "
            "the drum is on: a jog with a jump to step 1 on step 2 of 2 leaves step 1 "
            "with its row, not completed (1111, Result2, Result3); a jump to step 2 on "
            "step 2, with B reset on that scan, writes the row again (2222, Result4, "
            "ErrorCode)",
            ladder=prove_jump_with_jog_and_to_own_step,
            expect={"Result2": 1111, "Result3": 1111, "Result4": 2222, "ErrorCode": 2222},
            scans=SCANS,
            probe="time and time & event drums: jog on the last step with a jump, jump to own step",
        ),
        case(
            "A jump leaves a completed drum all-off, and neither a jog with a jump nor the "
            "drum's own time or event ends that: completed, then a jump to step 1 reads "
            "1414 (Result2), a jog with a jump 1414 (Result3); another pair after the same "
            "jump reads 1414 (Result4) and 1414 with step 1's time up or its event on "
            "(ErrorCode)",
            ladder=prove_jump_after_completion,
            expect={"Result2": 1414, "Result3": 1414, "Result4": 1414, "ErrorCode": 1414},
            scans=SCANS,
        ),
        case(
            "A completed drum that fires again rewrites only the flag: both complete on "
            "scan 4 (2424, Result2), B turned on from outside on scan 5 stays on while "
            "the time and the level event stay true (2626, Result3, Result4, ErrorCode)",
            ladder=prove_completed_drum_fires_again,
            expect={"Result2": 2424, "Result3": 2626, "Result4": 2626, "ErrorCode": 2626},
            scans=SCANS,
            probe="time and time & event drums: a completed drum firing again",
        ),
        case(
            "A time drum's accumulator holds through a jog completion, and a jog-hold scan "
            "does not tick it nor its correction clear it: Acc 900 staged, then jogged on "
            "the last step, reads 9 hundreds (Result2) and moves on no scan after (Result3 "
            "= 0); a jog on a bad step with Acc 900 staged leaves it 900 (Result4 = 0 over "
            "900) and the next scan's correction keeps it (ErrorCode in 900..999)",
            ladder=prove_acc_through_jog_completion,
            expect={"Result2": 9, "Result3": 0, "Result4": 0, "ErrorCode": range(900, 1000)},
            scans=SCANS,
        ),
    ],
)


def test_drum_jump():
    assert_all_passed(run(group))
