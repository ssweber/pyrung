"""For/Next: the ``loop`` group (subroutine ``Conf_Loop``).

The cases own C1701..C1800, timers T201..T300 and counters CT201..CT250
(``memory.OWNED``).  CLICK has no
loop index, so a case counts iterations in a slot register it increments in the
body.  Each loop rung is gated on ``Fired == 0`` so the loop runs on one scan.

A false For rung skips its body (F2, measured on a CLICK 2026-10-29): body
coils hold, and body one-shots, timers and counters are not scanned, so they
do not see a reset either.  So the cases that reset through an instruction
inside the loop (F6 edge, F7 timer, F8 counter, F14 one-shot) keep their For
rung true and gate a body rung instead: their resets are scanned on every
reset scan.  Bits a case latches, or drives from inside a loop, are reset in
its ``init``.

The probe matrix is ``scratchpad/click-v4/RESEARCH.md`` section 4; round 1 on
a CLICK is ``scratchpad/conformance_review/FINDINGS.md`` item 3.  Which case
tells which model apart, and what the other model reads:

- ``false_for_rung_skips_body`` (F2): skipped, C1701 keeps its latch
  (Result1 = 1); a body run once with power off turns it off (2, pyrung before
  CLICK's rule).  Result2 = 0: a body calc does not run either way.  Round 1
  read Result1 = 1 on the same ladder without Result2.
- ``zero_count`` (F3): no iteration reads Result2 = 0; 65536 iterations read
  1; "at least once" (pyrung before) reads Result1 = 1, Result2 = 1.  Round 1
  read Result1 = 0, which 0 and 65536 both give; but the count -1 loop in the
  same scan alone accounts for its 1122 ms maximum scan (SD12), so 0 is likely.
- ``negative_count_reads_unsigned`` (F3): unsigned 16-bit runs 65535
  iterations (Result2 = -1, the Int wraps); "at least once" reads 1.  Round 1
  read -1.  In-process only: on a CLICK it makes a scan of about 1.1 s that
  every wall-clock case beside it would see.
- ``edge_contact_inside_loop`` (F6): C1721 latches on iteration 1 of 3 and
  stays on.  An edge judged against the previous scan (pyrung today) rises on
  all three (Result1 = 3); one with memory in the contact rises once (1, what
  CLICK's per-contact edge memory predicts, FINDINGS item 2).
- ``counter_inside_loop`` (F8): the count input is on in iterations 1, 2 and 4
  of 4.  A counter that counts every enabled execution (pyrung today) reads 3;
  one that counts edges remembered by the instruction reads 2 (CLICK's edge
  counters, FINDINGS item 1); an edge judged once per scan reads 1.
- ``oneshot_inside_loop`` (F14): the one-shot's rung is true in iterations
  1, 2 and 4 of 4.  Memory per instruction, re-armed by iteration 3, fires
  twice (Result1 = 2); once per scan reads 1; a one-shot ignored inside loops
  reads 3.
- ``body_oneshot_holds_while_for_false`` (F17): the For rung is false on scan
  2 of 4.  Skipped, the one-shot stays fired and Result1 = 1; a body run with
  power off re-arms it and it fires again on scan 3 (2).

F6 and F8 expect pyrung's answers until the per-contact edge memory and
edge-counting changes land; a CLICK is predicted to fail them as noted.
"""

from __future__ import annotations

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import (
    Rung,
    break_loop,
    calc,
    continue_loop,
    copy,
    count_up,
    forloop,
    latch,
    on_delay,
    out,
    reset,
    rise,
)
from tests.conformance.memory import ClickCounter, ClickTimer, c
from tests.conformance.scans import count_scans, done, scans_for


def _fire(slot) -> None:
    with Rung(slot.Cmd != 0):
        copy(1, slot.Fired, oneshot=True)


def prove_loop_runs_n_times(slot):
    # F1
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        with forloop(5):
            calc(slot.Result1 + 1, slot.Result1)
    _fire(slot)


def prove_false_for_rung_skips_body(slot):
    # F2: the loop's rung is false (Scratch = 0); C1701 is latched just above.
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        latch(c[1701])
    with Rung(slot.Cmd != 0, slot.Scratch != 0):
        with forloop(2):
            out(c[1701])
            calc(slot.Result2 + 1, slot.Result2)
    with Rung(slot.Cmd != 0, slot.Fired == 0, c[1701]):
        copy(1, slot.Result1)
    with Rung(slot.Cmd != 0, slot.Fired == 0, ~c[1701]):
        copy(2, slot.Result1)
    _fire(slot)


def prove_zero_count(slot):
    # F3: a count register of 0 (Scratch).
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        with forloop(slot.Scratch):
            calc(slot.Result1 + 1, slot.Result1)
            copy(1, slot.Result2)
    _fire(slot)


def prove_negative_count_reads_unsigned(slot):
    # F3: a count register of -1 (Result3).
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        copy(-1, slot.Result3)
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        with forloop(slot.Result3):
            calc(slot.Result2 + 1, slot.Result2)
    _fire(slot)


def prove_count_read_once(slot):
    # F4: the body lowers the count register from 5 to 2.
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        copy(5, slot.Result3)
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        with forloop(slot.Result3):
            calc(slot.Result1 + 1, slot.Result1)
            copy(2, slot.Result3)
    _fire(slot)


def prove_body_rung_contact_per_iteration(slot):
    # F5: the body rung's contact is true on iteration 2 of 3 only.
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        with forloop(3):
            calc(slot.Scratch + 1, slot.Scratch)
            with Rung(slot.Scratch == 2):
                out(c[1711])
            with Rung(slot.Scratch >= 2):
                calc(slot.Result2 + 1, slot.Result2)
    with Rung(slot.Cmd != 0, slot.Fired == 0, c[1711]):
        copy(1, slot.Result1)
    with Rung(slot.Cmd != 0, slot.Fired == 0, ~c[1711]):
        copy(2, slot.Result1)
    _fire(slot)


def prove_edge_contact_inside_loop(slot):
    # F6: C1721 latches on iteration 1 of 3 and stays on.
    with Rung():
        with forloop(3):
            with Rung(slot.Cmd != 0, slot.Fired == 0):
                calc(slot.Scratch + 1, slot.Scratch)
            with Rung(slot.Scratch == 1):
                latch(c[1721])
            with Rung(rise(c[1721])):
                calc(slot.Result1 + 1, slot.Result1)
    _fire(slot)


def prove_timer_inside_loop(slot):
    # F7: a TON inside For 10 against the same TON outside; once the outside
    # one passes 100 ms, Result1 = 1 if the loop's is ahead by more than 5x.
    loop_timer, plain_timer = ClickTimer(201), ClickTimer(202)
    with Rung():
        with forloop(10):
            with Rung(slot.Cmd != 0):
                on_delay(loop_timer, 30000)
    with Rung(slot.Cmd != 0):
        on_delay(plain_timer, 30000)
    with Rung(slot.Cmd != 0, plain_timer.Acc >= 100, slot.Fired == 0):
        calc(loop_timer.Acc - plain_timer.Acc * 5, slot.Result3)
    with Rung(slot.Cmd != 0, plain_timer.Acc >= 100, slot.Fired == 0, slot.Result3 > 0):
        copy(1, slot.Result1)
    with Rung(slot.Cmd != 0, plain_timer.Acc >= 100, slot.Fired == 0, slot.Result3 <= 0):
        copy(2, slot.Result1)
    with Rung(slot.Cmd != 0, plain_timer.Acc >= 100):
        copy(1, slot.Fired, oneshot=True)


def prove_counter_inside_loop(slot):
    # F8: the count input is on in iterations 1, 2 and 4 of 4.
    counter = ClickCounter(201)
    with Rung():
        with forloop(4):
            with Rung(slot.Cmd != 0, slot.Fired == 0):
                calc(slot.Scratch + 1, slot.Scratch)
            with Rung(slot.Cmd != 0, slot.Fired == 0, slot.Scratch != 3):
                count_up(counter, 100).reset(slot.Cmd == 0)
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        copy(counter.Acc, slot.Result1)
    _fire(slot)


def prove_break_leaves_innermost_loop(slot):
    # F9: the inner loop breaks on its second iteration, three times.
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        with forloop(3):
            copy(0, slot.Scratch)
            with forloop(4):
                calc(slot.Scratch + 1, slot.Scratch)
                calc(slot.Result1 + 1, slot.Result1)
                with Rung(slot.Scratch == 2):
                    break_loop()
            calc(slot.Result2 + 1, slot.Result2)
    _fire(slot)


def prove_break_runs_earlier_body_rungs(slot):
    # F10
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        with forloop(5):
            calc(slot.Scratch + 1, slot.Scratch)
            calc(slot.Result1 + 1, slot.Result1)
            with Rung(slot.Scratch == 3):
                break_loop()
            calc(slot.Result2 + 1, slot.Result2)
    _fire(slot)


def prove_continue_skips_rest_of_iteration(slot):
    # F11
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        with forloop(5):
            calc(slot.Scratch + 1, slot.Scratch)
            calc(slot.Result1 + 1, slot.Result1)
            with Rung(slot.Scratch == 2):
                continue_loop()
            calc(slot.Result2 + 1, slot.Result2)
    _fire(slot)


def prove_continue_on_last_iteration(slot):
    # F12
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        with forloop(5):
            calc(slot.Scratch + 1, slot.Scratch)
            calc(slot.Result1 + 1, slot.Result1)
            with Rung(slot.Scratch == 5):
                continue_loop()
            calc(slot.Result2 + 1, slot.Result2)
    _fire(slot)


def prove_break_on_same_iteration_write(slot):
    # F13: a body rung turns C1731 on in iteration 3; the next body rung breaks on it.
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        with forloop(5):
            calc(slot.Scratch + 1, slot.Scratch)
            with Rung(slot.Scratch == 3):
                out(c[1731])
            with Rung(c[1731]):
                break_loop()
            calc(slot.Result1 + 1, slot.Result1)
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        copy(slot.Scratch, slot.Result2)
    _fire(slot)


def prove_oneshot_inside_loop(slot):
    # F14: the one-shot's rung is true in iterations 1, 2 and 4 of 4.
    with Rung():
        with forloop(4):
            with Rung(slot.Cmd != 0, slot.Fired == 0):
                calc(slot.Scratch + 1, slot.Scratch)
            with Rung(slot.Cmd != 0, slot.Fired == 0, slot.Scratch != 3):
                calc(slot.Result1 + 1, slot.Result1, oneshot=True)
                calc(slot.Result2 + 1, slot.Result2)
    _fire(slot)


def prove_body_oneshot_holds_while_for_false(slot):
    # F17: the For rung is false on scan 2 of 4; the one-shot's own rung is
    # true on scans 1 to 3 and false on scan 4, which re-arms it.
    count_scans(slot, window=4)
    with Rung(slot.Cmd != 0, slot.Scratch != 2, slot.Scratch <= 4):
        with forloop(1):
            with Rung(slot.Scratch != 4):
                calc(slot.Result1 + 1, slot.Result1, oneshot=True)
            calc(slot.Result2 + 1, slot.Result2)
    done(slot, window=4)


def init_false_for_rung_skips_body(slot):
    reset(c[1701])


def init_body_rung_contact_per_iteration(slot):
    reset(c[1711])


def init_edge_contact_inside_loop(slot):
    reset(c[1721])


def init_break_on_same_iteration_write(slot):
    reset(c[1731])


group = Group(
    "loop",
    [
        case(
            "For 5 runs its body five times on one scan (Result1 = 5)",
            ladder=prove_loop_runs_n_times,
            expect={"Result1": 5},
            confirmed=True,
        ),
        case(
            "The count register is read once at For: lowering it in the body still "
            "runs 5 iterations (Result1 = 5)",
            ladder=prove_count_read_once,
            expect={"Result1": 5},
            confirmed=True,
        ),
        case(
            "A body rung evaluates its contact on every iteration: true on iteration 2 "
            "only, its out() ends off (Result1 = 2), and a later body rung counts two "
            "of three iterations (Result2 = 2)",
            ladder=prove_body_rung_contact_per_iteration,
            init=init_body_rung_contact_per_iteration,
            expect={"Result1": 2, "Result2": 2},
            confirmed=True,
        ),
        case(
            "An edge contact inside a loop compares with the previous scan: a bit "
            "latched on iteration 1 of 3 rises on all three (Result1 = 3), not once (1)",
            ladder=prove_edge_contact_inside_loop,
            init=init_edge_contact_inside_loop,
            expect={"Result1": 3},
        ),
        case(
            "A timer inside For 10 accumulates on every iteration: it runs more than "
            "five times ahead of the same timer outside the loop (Result1 = 1)",
            ladder=prove_timer_inside_loop,
            expect={"Result1": 1},
            scans=30,
            confirmed=True,
        ),
        case(
            "A counter inside For 4 counts every iteration its input is on: on in "
            "iterations 1, 2 and 4 counts 3 (Result1 = 3), not 2 by edges",
            ladder=prove_counter_inside_loop,
            expect={"Result1": 3},
        ),
        case(
            "Break leaves only the innermost loop: the inner loop breaks on its second "
            "iteration each time (Result1 = 6) and the outer loop runs on (Result2 = 3)",
            ladder=prove_break_leaves_innermost_loop,
            expect={"Result1": 6, "Result2": 3},
            confirmed=True,
        ),
        case(
            "Rungs before Break run on the breaking iteration, rungs after do not "
            "(Result1 = 3, Result2 = 2)",
            ladder=prove_break_runs_earlier_body_rungs,
            expect={"Result1": 3, "Result2": 2},
            confirmed=True,
        ),
        case(
            "Continue on iteration 2 skips only the rest of that iteration "
            "(Result1 = 5, Result2 = 4)",
            ladder=prove_continue_skips_rest_of_iteration,
            expect={"Result1": 5, "Result2": 4},
            confirmed=True,
        ),
        case(
            "Continue on the last iteration ends the loop (Result1 = 5, Result2 = 4)",
            ladder=prove_continue_on_last_iteration,
            expect={"Result1": 5, "Result2": 4},
            confirmed=True,
        ),
        case(
            "A Break contact written by an earlier body rung in the same iteration "
            "breaks on that iteration (Result1 = 2, Result2 = 3)",
            ladder=prove_break_on_same_iteration_write,
            init=init_break_on_same_iteration_write,
            expect={"Result1": 2, "Result2": 3},
            confirmed=True,
        ),
        case(
            "A one-shot inside a loop re-arms on an iteration its rung is false: true "
            "in iterations 1, 2 and 4 of 4, it fires twice (Result1 = 2, Result2 = 3)",
            ladder=prove_oneshot_inside_loop,
            expect={"Result1": 2, "Result2": 3},
        ),
        case(
            "A For rung that is false skips its body: an out() in the body leaves its "
            "coil on (Result1 = 1), not off (2), and a body calc does not run "
            "(Result2 = 0)",
            ladder=prove_false_for_rung_skips_body,
            init=init_false_for_rung_skips_body,
            expect={"Result1": 1, "Result2": 0},
        ),
        case(
            "A loop count of 0 runs no iteration (Result1 = 0, Result2 = 0), not one "
            "(1, 1) or 65536 (Result2 = 1)",
            ladder=prove_zero_count,
            expect={"Result1": 0, "Result2": 0},
        ),
        case(
            "A loop count of -1 is read as unsigned 16-bit: 65535 iterations wrap an "
            "Int to -1 (Result2 = -1), not one iteration (1)",
            ladder=prove_negative_count_reads_unsigned,
            expect={"Result2": -1},
            in_process_only=(
                "65535 iterations make a scan of about 1.1 s on a CLICK, which every "
                "wall-clock case run beside it would see"
            ),
            confirmed=True,
        ),
        case(
            "A one-shot in a loop holds while the For rung is false: skipped on scan 2, "
            "it does not re-arm and fires once over scans 1 and 3 (Result1 = 1, "
            "Result2 = 3), not twice (2)",
            ladder=prove_body_oneshot_holds_while_for_false,
            expect={"Result1": 1, "Result2": 3},
            scans=scans_for(4),
        ),
    ],
)


def test_loop():
    assert_all_passed(run(group))
