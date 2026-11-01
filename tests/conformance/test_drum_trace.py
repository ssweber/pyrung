"""A drum's state scan by scan, for when it writes its outputs.

Part A's drum probes record when a bit first comes on; these record the whole
state, so a reading names what each scan did.  ``drum``'s block is full and part
A is near its step budget, so they are a group and a part (E) of their own.

Each case records the state at the end of a scan as four digits, SFBA: the step,
the completion flag, step 2's output (B) and step 1's output (A), so 5010 is
step 5 with B on.  Result2, Result3 and Result4 hold three scans named per case
and ErrorCode the last scan of the window, 8; Result1 is the step register.

Answered on hardware (2026-11-01), with step 5 of 2 staged on scan 3:

- ``tied_jog`` (scans 3, 4, 5): the ladder of
  ``drum/pins_read_before_step_fix``.  The jog scan leaves step 5 with the
  outputs off (5000), scan 4 corrects and writes no output (1000), and the rung
  is false from scan 5 (1000, 1000).
- ``free_jog`` (scans 3, 4, 5): the ladder of
  ``row_solve/correction_scan_with_free_enable``.  The jog scan leaves step 5
  with B on (5010), scan 4 corrects and leaves B on (1010), and step 1's output
  never comes on, though the rung stays true (1010, 1010).
- ``free_no_jog`` (scans 3, 4, 5): the same with the jog never on.  The scan
  corrects at once and leaves B on (1010 on every scan).
  ``drum/correction_without_jog`` and ``drum/invalid_step_corrects_to_1`` read
  step 1's output on because their rung comes back on that scan.
- ``jog_again_after_last_step`` (scans 4, 5, 6): the drum of
  ``drum/jog_on_last_step``, which a jog on its last step completes on scan 4,
  left on step 2 with its outputs off, and a third jog on scan 6, which does
  nothing (2100 throughout).

- ``output_reset_while_running`` (scans 3, 4, 5): on step 2, B reset above the
  drum on scan 3 stays off (2000).  A drum that writes a row each scan reads
  2010.
- ``step_written_while_running`` (scans 3, 4, 5): on step 2, step 1 written to
  the register on scan 3, and step 1's event on scan 5: 1010, 1010, then 2010
  through scan 8, the register being the step the event acts on.  Writing the
  row at once reads 1001; a drum that goes on from its own step completes on
  scan 5, 1100 or 2100.
- ``step_written_while_paused`` (scans 4, 5, 6): the same write with the rung
  false on scans 3..4: 1010, then 1001 from scan 5, the rung coming back writing
  the step whether or not it needed correcting.  1010 throughout says only a
  correction writes.

So a drum writes its outputs when its step moves or its rung comes back, not on
every scan, and a write to its step register is not a move.

The cases own C51..C100 (``memory.OWNED``).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import And, Or, Rung, calc, copy, event_drum, out, reset
from tests.conformance.memory import c
from tests.conformance.scans import WINDOW, count_scans, done, scans_for

SCANS = scans_for()


def record_states(slot: Any, a: Any, b: Any, flag: Any, scans: Sequence[int]) -> None:
    """ErrorCode = the state as SFBA through the window; Result2.. = it on *scans*."""
    with Rung(slot.Cmd != 0, slot.Scratch <= WINDOW):
        calc(slot.Result1 * 1000, slot.ErrorCode)
    with Rung(slot.Cmd != 0, slot.Scratch <= WINDOW, flag):
        calc(slot.ErrorCode + 100, slot.ErrorCode)
    with Rung(slot.Cmd != 0, slot.Scratch <= WINDOW, b):
        calc(slot.ErrorCode + 10, slot.ErrorCode)
    with Rung(slot.Cmd != 0, slot.Scratch <= WINDOW, a):
        calc(slot.ErrorCode + 1, slot.ErrorCode)
    for scan, into in zip(scans, (slot.Result2, slot.Result3, slot.Result4), strict=True):
        with Rung(slot.Cmd != 0, slot.Scratch == scan):
            copy(slot.ErrorCode, into)


def prove_tied_jog(slot):
    # A = C51, B = C52, events C53 and C54, flag C55, jog C56 (scan 3).
    a, b, flag, jog = c[51], c[52], c[55], c[56]
    count_scans(slot)
    with Rung(slot.Cmd != 0, slot.Scratch == 3):
        copy(5, slot.Result1)
    with Rung(slot.Cmd != 0, slot.Scratch == 3):
        out(jog)
    with Rung(slot.Result1 != 1):
        event_drum(
            outputs=[a, b],
            events=[c[53], c[54]],
            pattern=[[1, 0], [0, 1]],
            current_step=slot.Result1,
            completion_flag=flag,
        ).reset(And(slot.Result1 != 1, slot.Cmd == 0)).jog(And(slot.Result1 != 1, jog))
    record_states(slot, a, b, flag, (3, 4, 5))
    done(slot)


def _free_enable(slot, a, b, event, other_event, flag, jog_bit, *, jog):
    # The event pulses on scan 2, so the drum is on step 2 with B on when the
    # step register is staged out of range on scan 3.
    count_scans(slot)
    with Rung(slot.Cmd != 0, slot.Scratch == 1):
        copy(1, slot.Result1)
    with Rung(slot.Cmd != 0, slot.Scratch == 2):
        out(event)
    with Rung(slot.Cmd != 0, slot.Scratch == 3):
        copy(5, slot.Result1)
    if jog:
        with Rung(slot.Cmd != 0, slot.Scratch == 3):
            out(jog_bit)
    with Rung(slot.Cmd != 0):
        event_drum(
            outputs=[a, b],
            events=[event, other_event],
            pattern=[[1, 0], [0, 1]],
            current_step=slot.Result1,
            completion_flag=flag,
        ).reset(slot.Cmd == 0).jog(jog_bit)
    record_states(slot, a, b, flag, (3, 4, 5))
    done(slot)


def prove_free_jog(slot):
    _free_enable(slot, c[61], c[62], c[63], c[64], c[65], c[66], jog=True)


def prove_free_no_jog(slot):
    _free_enable(slot, c[71], c[72], c[73], c[74], c[75], c[76], jog=False)


def prove_jog_again_after_last_step(slot):
    # A = C81, B = C82, the event C83 never on, flag C84, jog C85 (scans 2, 4, 6).
    a, b, event, flag, jog = c[81], c[82], c[83], c[84], c[85]
    count_scans(slot)
    with Rung(slot.Cmd != 0, Or(slot.Scratch == 2, slot.Scratch == 4, slot.Scratch == 6)):
        out(jog)
    with Rung(slot.Cmd != 0):
        event_drum(
            outputs=[a, b],
            events=[event, event],
            pattern=[[1, 0], [0, 1]],
            current_step=slot.Result1,
            completion_flag=flag,
        ).reset(slot.Cmd == 0).jog(jog)
    record_states(slot, a, b, flag, (4, 5, 6))
    done(slot)


def _running_drum(slot, a, b, event, flag):
    with Rung(slot.Cmd != 0):
        event_drum(
            outputs=[a, b],
            events=[event, event],
            pattern=[[1, 0], [0, 1]],
            current_step=slot.Result1,
            completion_flag=flag,
        ).reset(slot.Cmd == 0)


def prove_output_reset_while_running(slot):
    # A = C86, B = C87, the event C88 (scan 2), flag C89.  B is reset on scan 3,
    # above the drum.
    a, b, event, flag = c[86], c[87], c[88], c[89]
    count_scans(slot)
    with Rung(slot.Cmd != 0, slot.Scratch == 2):
        out(event)
    with Rung(slot.Cmd != 0, slot.Scratch == 3):
        reset(b)
    _running_drum(slot, a, b, event, flag)
    record_states(slot, a, b, flag, (3, 4, 5))
    done(slot)


def prove_step_written_while_running(slot):
    # A = C91, B = C92, the event C93 (scans 2 and 5), flag C94.  Step 1 is
    # written over step 2 on scan 3, above the drum.
    a, b, event, flag = c[91], c[92], c[93], c[94]
    count_scans(slot)
    with Rung(slot.Cmd != 0, Or(slot.Scratch == 2, slot.Scratch == 5)):
        out(event)
    with Rung(slot.Cmd != 0, slot.Scratch == 3):
        copy(1, slot.Result1)
    _running_drum(slot, a, b, event, flag)
    record_states(slot, a, b, flag, (3, 4, 5))
    done(slot)


def prove_step_written_while_paused(slot):
    # A = C96, B = C97, the event C98 (scan 2), flag C99, the pause C100 (scans
    # 3..4).  Step 1 is written over step 2 on scan 3, above the drum.
    a, b, event, flag, pause = c[96], c[97], c[98], c[99], c[100]
    count_scans(slot)
    with Rung(slot.Cmd != 0, slot.Scratch == 2):
        out(event)
    with Rung(slot.Cmd != 0, slot.Scratch >= 3, slot.Scratch <= 4):
        out(pause)
    with Rung(slot.Cmd != 0, slot.Scratch == 3):
        copy(1, slot.Result1)
    with Rung(slot.Cmd != 0, ~pause):
        event_drum(
            outputs=[a, b],
            events=[event, event],
            pattern=[[1, 0], [0, 1]],
            current_step=slot.Result1,
            completion_flag=flag,
        ).reset(slot.Cmd == 0)
    record_states(slot, a, b, flag, (4, 5, 6))
    done(slot)


group = Group(
    "drum_trace",
    [
        case(
            "A jog on an out-of-range step, the rung tied to the step register, scan by "
            "scan as step, flag, B, A: step 5 staged on scan 3 stands that scan "
            "(Result2 = 5000), is corrected on scan 4 with no output written (Result3 = "
            "1000), and the rung false from then on holds it (Result4 = 1000, ErrorCode "
            "= 1000 at scan 8)",
            ladder=prove_tied_jog,
            probe="tied rung: which scan corrects a jogged bad step",
            expect={
                "Result1": 1,
                "Result2": 5000,
                "Result3": 1000,
                "Result4": 1000,
                "ErrorCode": 1000,
            },
            scans=SCANS,
        ),
        case(
            "A jog on an out-of-range step, the rung free, scan by scan as step, flag, "
            "B, A: step 5 staged on scan 3 stands with B still on (Result2 = 5010), is "
            "corrected on scan 4 with the outputs untouched (Result3 = 1010), and B "
            "stays on with step 1's output off (Result4 = 1010, ErrorCode = 1010 at "
            "scan 8)",
            ladder=prove_free_jog,
            probe="free rung: what the scans after a jogged bad step do",
            expect={
                "Result1": 1,
                "Result2": 5010,
                "Result3": 1010,
                "Result4": 1010,
                "ErrorCode": 1010,
            },
            scans=SCANS,
        ),
        case(
            "The same with no jog, scan by scan as step, flag, B, A: step 5 staged on "
            "scan 3 goes to step 1 on that scan and B stays on with step 1's output off "
            "(Result2, Result3, Result4 and ErrorCode = 1010)",
            ladder=prove_free_no_jog,
            probe="free rung: does a bad step with no jog correct on its own scan",
            expect={
                "Result1": 1,
                "Result2": 1010,
                "Result3": 1010,
                "Result4": 1010,
                "ErrorCode": 1010,
            },
            scans=SCANS,
        ),
        case(
            "A further jog on a drum a jog completed on its last step does nothing, scan "
            "by scan as step, flag, B, A: jogged on scans 2, 4 and 6, it reads step 2, "
            "flag on, outputs off on scans 4, 5, 6 and 8 (Result2, Result3, Result4 and "
            "ErrorCode = 2100)",
            ladder=prove_jog_again_after_last_step,
            probe="what a jog on a drum completed on its last step does",
            expect={
                "Result1": 2,
                "Result2": 2100,
                "Result3": 2100,
                "Result4": 2100,
                "ErrorCode": 2100,
            },
            scans=SCANS,
        ),
        case(
            "A running drum leaves an output the ladder reset, scan by scan as step, "
            "flag, B, A: on step 2, B reset on scan 3 stays off (Result2, Result3, "
            "Result4 and ErrorCode = 2000)",
            ladder=prove_output_reset_while_running,
            probe="does a running drum write its outputs every scan",
            expect={
                "Result1": 2,
                "Result2": 2000,
                "Result3": 2000,
                "Result4": 2000,
                "ErrorCode": 2000,
            },
            scans=SCANS,
        ),
        case(
            "A running drum takes a step written to its register without writing that "
            "step's outputs, scan by scan as step, flag, B, A: step 1 written on step 2 "
            "on scan 3 reads 1010 on scans 3 and 4 (Result2, Result3), and step 1's "
            "event on scan 5 moves it to step 2 (Result4 and ErrorCode = 2010)",
            ladder=prove_step_written_while_running,
            probe="what a step written to a running drum's register does",
            expect={
                "Result1": 2,
                "Result2": 1010,
                "Result3": 1010,
                "Result4": 2010,
                "ErrorCode": 2010,
            },
            scans=SCANS,
        ),
        case(
            "A drum whose rung comes back writes the step in its register, scan by scan "
            "as step, flag, B, A: step 1 written on step 2 while paused reads 1010 on "
            "scan 4 (Result2) and 1001 from scan 5, the scan the rung comes back "
            "(Result3, Result4 and ErrorCode)",
            ladder=prove_step_written_while_paused,
            probe="does a rung coming back write a valid step's outputs",
            expect={
                "Result1": 1,
                "Result2": 1010,
                "Result3": 1001,
                "Result4": 1001,
                "ErrorCode": 1001,
            },
            scans=SCANS,
        ),
    ],
)


def test_drum_trace():
    assert_all_passed(run(group))
