"""For/Next: the ``loop`` group (subroutine ``Conf_Loop``).

The cases own C1701..C1800 (``BITS_C``), timers T201..T300 / TD201..TD300 and
counters CT201..CT300 / CTD201..CTD300, which no other group uses.  CLICK has no
loop index, so a case counts iterations in a slot register it increments in the
body.  Each loop rung is gated on ``Fired == 0`` so the loop runs on one scan.

The expectations are pyrung's answers, unconfirmed on a CLICK: the probe
matrix F1-F14 in ``scratchpad/click-v4/RESEARCH.md`` section 4.  F2 (does a
false For rung drive its body coils off?), F6 (edges inside a loop), F7 (timers
inside a loop) and F14 (one-shots inside a loop) are where pyrung's model is
most likely to differ.
"""

from __future__ import annotations

from dataclasses import dataclass

from pyrung.click import ClickBlocks
from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import (
    Rung,
    Tag,
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

_blocks = ClickBlocks()
c, t, td, ct, ctd = _blocks.c, _blocks.t, _blocks.td, _blocks.ct, _blocks.ctd

#: Raw C bits, timers and counters the loop cases own.
BITS_C = range(1701, 1801)
TIMERS = range(201, 301)
COUNTERS = range(201, 301)


@dataclass(frozen=True)
class _ClickTimer:
    n: int

    @property
    def Done(self) -> Tag:
        return t[self.n]

    @property
    def Acc(self) -> Tag:
        return td[self.n]


@dataclass(frozen=True)
class _ClickCounter:
    n: int

    @property
    def Done(self) -> Tag:
        return ct[self.n]

    @property
    def Acc(self) -> Tag:
        return ctd[self.n]


def _fire(slot) -> None:
    with Rung(slot.Cmd != 0):
        copy(1, slot.Fired, oneshot=True)


def prove_loop_runs_n_times(slot):
    # F1
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        with forloop(5):
            calc(slot.Result1 + 1, slot.Result1)
    _fire(slot)


def prove_false_for_rung_turns_body_outs_off(slot):
    # F2: C1701 is on when the loop's rung is false; its body out() runs unpowered.
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        latch(c[1701])
    with Rung(slot.Cmd != 0, slot.Scratch != 0):
        with forloop(2):
            out(c[1701])
    with Rung(slot.Cmd != 0, slot.Fired == 0, c[1701]):
        copy(1, slot.Result1)
    with Rung(slot.Cmd != 0, slot.Fired == 0, ~c[1701]):
        copy(2, slot.Result1)
    _fire(slot)


def prove_zero_or_negative_count_runs_once(slot):
    # F3: a count register of 0 (Scratch) or -1 (Result3).
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        copy(-1, slot.Result3)
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        with forloop(slot.Scratch):
            calc(slot.Result1 + 1, slot.Result1)
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
    # F6: C1721 goes on in iterations 1 and 3 of 4, off in 2 and 4.
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        with forloop(4):
            calc(slot.Scratch + 1, slot.Scratch)
            with Rung(slot.Scratch == 1):
                latch(c[1721])
            with Rung(slot.Scratch == 2):
                reset(c[1721])
            with Rung(slot.Scratch == 3):
                latch(c[1721])
            with Rung(slot.Scratch == 4):
                reset(c[1721])
            with Rung(rise(c[1721])):
                calc(slot.Result1 + 1, slot.Result1)
    _fire(slot)


def prove_timer_inside_loop(slot):
    # F7: a TON inside For 10 against the same TON outside; once the outside
    # one passes 100 ms, Result1 = 1 if the loop's is ahead by more than 5x.
    loop_timer, plain_timer = _ClickTimer(201), _ClickTimer(202)
    with Rung(slot.Cmd != 0):
        with forloop(10):
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
    # F8
    counter = _ClickCounter(201)
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        with forloop(5):
            count_up(counter, 100).reset(c[1741])
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
    # F14
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        with forloop(3):
            calc(slot.Result1 + 1, slot.Result1, oneshot=True)
            calc(slot.Result2 + 1, slot.Result2)
    _fire(slot)


group = Group(
    "loop",
    [
        case(
            "For 5 runs its body five times on one scan (Result1 = 5)",
            ladder=prove_loop_runs_n_times,
            expect={"Result1": 5},
        ),
        case(
            "A For rung that is false runs its body unpowered: an out() in the body "
            "turns off (Result1 = 2), not left as it was (1)",
            ladder=prove_false_for_rung_turns_body_outs_off,
            expect={"Result1": 2},
        ),
        case(
            "A loop count of 0 or -1 runs the body once (Result1 = 1, Result2 = 1)",
            ladder=prove_zero_or_negative_count_runs_once,
            expect={"Result1": 1, "Result2": 1},
        ),
        case(
            "The count register is read once at For: lowering it in the body still "
            "runs 5 iterations (Result1 = 5)",
            ladder=prove_count_read_once,
            expect={"Result1": 5},
        ),
        case(
            "A body rung evaluates its contact on every iteration: true on iteration 2 "
            "only, its out() ends off (Result1 = 2), and a later body rung counts two "
            "of three iterations (Result2 = 2)",
            ladder=prove_body_rung_contact_per_iteration,
            expect={"Result1": 2, "Result2": 2},
        ),
        case(
            "An edge contact inside a loop compares with the previous scan: a bit that "
            "comes on in iterations 1 and 3 counts two rises (Result1 = 2)",
            ladder=prove_edge_contact_inside_loop,
            expect={"Result1": 2},
        ),
        case(
            "A timer inside For 10 accumulates on every iteration: it runs more than "
            "five times ahead of the same timer outside the loop (Result1 = 1)",
            ladder=prove_timer_inside_loop,
            expect={"Result1": 1},
            scans=30,
        ),
        case(
            "A counter inside For 5 counts every iteration it is enabled (Result1 = 5)",
            ladder=prove_counter_inside_loop,
            expect={"Result1": 5},
        ),
        case(
            "Break leaves only the innermost loop: the inner loop breaks on its second "
            "iteration each time (Result1 = 6) and the outer loop runs on (Result2 = 3)",
            ladder=prove_break_leaves_innermost_loop,
            expect={"Result1": 6, "Result2": 3},
        ),
        case(
            "Rungs before Break run on the breaking iteration, rungs after do not "
            "(Result1 = 3, Result2 = 2)",
            ladder=prove_break_runs_earlier_body_rungs,
            expect={"Result1": 3, "Result2": 2},
        ),
        case(
            "Continue on iteration 2 skips only the rest of that iteration "
            "(Result1 = 5, Result2 = 4)",
            ladder=prove_continue_skips_rest_of_iteration,
            expect={"Result1": 5, "Result2": 4},
        ),
        case(
            "Continue on the last iteration ends the loop (Result1 = 5, Result2 = 4)",
            ladder=prove_continue_on_last_iteration,
            expect={"Result1": 5, "Result2": 4},
        ),
        case(
            "A Break contact written by an earlier body rung in the same iteration "
            "breaks on that iteration (Result1 = 2, Result2 = 3)",
            ladder=prove_break_on_same_iteration_write,
            expect={"Result1": 2, "Result2": 3},
        ),
        case(
            "A one-shot inside a loop fires on the first iteration of the scan only "
            "(Result1 = 1, Result2 = 3)",
            ladder=prove_oneshot_inside_loop,
            expect={"Result1": 1, "Result2": 3},
        ),
    ],
)


def test_loop():
    assert_all_passed(run(group))
