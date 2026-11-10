"""A reset that turns on while its rung is on: the ``reset_pins`` group (part ``follow3_reset``).

No other case holds a reset on over scans where the rung stays on and the box is already
counting.  Each case here does, then drops the reset with the rung still on.

``down_reset_positive`` (CT11, preset 3) and ``down_reset_negative`` (CT12, preset -3, Acc
staged at -2 on scan 1).  The count input is on for scans 1..8 and the reset for scans 3 and 4.
Result1 is Acc at the end.  Result2 lists the scans 1..6 where Done is on, as digits (12 is
scans 1 and 2).  Result3 is Acc on scan 3, the first reset scan.  Result4 is Acc on scan 5, the
first scan after the reset.

- Reset clears Acc and Done, nothing counts the held input again, and Done is judged again once
  the reset is off.  pyrung reads Result2 = 1256 for preset 3 (0 <= 3) and 12 for preset -3
  (0 <= -3 is false), and 0 for Result1, Result3 and Result4.
- Done is not judged again until a count: Result2 = 12 for both presets.
- The reset drops, and the held input counts again as a new edge: Result1 = Result4 = -1.
- Reset loads the preset into the down counter: Result3 = 3 (preset 3) or -3 (preset -3), and
  Done is on on the reset scans (Result2 = 1234 for preset 3).

``rton_reset_held`` (T301 clock, T302 RTON) and ``rton_reset_after`` (T303 clock, T304 RTON).
The clock times from the first scan.  The RTON has 200 ms and its rung is always on.  Its reset
is on while the clock reads 300..499 ms (C701, C703).

- ``rton_reset_held``: Result1 is 1 if Done was on just before the reset (the control).
  Result2 is the first reset scan, Done as 1 plus 10 if Acc is not 0.  Result3 counts the later
  reset scans where Done is on or Acc is not 0.  Result4 is the first scan after the reset,
  read as Result2.  pyrung reads 1, 0, 0, 10: the reset scans read Acc 0 and Done off, and
  the scan after the reset already counted its time.  A reset that leaves Done on, or counts
  while it holds, reads Result2 = 1 or above, or Result3 above 0.  A timer that starts on the
  scan after the drop reads Result4 = 0.
- ``rton_reset_after``: Result1 is 1 if Acc is above 0 and under 200 between clock 520 and 600
  ms (timing started once the reset dropped).  Result2 is 1 if Done is on between clock 520 and
  640 ms (it is off while Acc is under 200).  Result3 is 1 if Done is on at clock 800 ms.
  Result4 is 1 if Acc is still 0 at clock 540 ms or later.  A reset that holds until the rung
  falls reads Result1 = 0, Result3 = 0, Result4 = 1.

The group owns what ``memory.OWNED_APART`` lists under it.  The two RTON cases wait on the
wall clock, so each passes ``Fired`` last.
"""

from __future__ import annotations

from typing import Any

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import And, Or, Rung, copy, count_down, fall, math, on_delay, out, rise
from tests.conformance.memory import ClickCounter, ClickTimer, c
from tests.conformance.scans import WINDOW, count_scans, done, scans_for

RESET_FIRST, RESET_LAST = 3, 4


def _down_reset(slot: Any, counter: ClickCounter, preset: int, staged: int | None) -> None:
    # The count input is on for every scan of the window, the reset on for scans 3 and 4.
    count_scans(slot)
    if staged is not None:
        with Rung(slot.Cmd != 0, slot.Scratch == 1):
            copy(staged, counter.Acc)
    reset = Or(slot.Cmd == 0, And(slot.Scratch >= RESET_FIRST, slot.Scratch <= RESET_LAST))
    with Rung(slot.Cmd != 0, slot.Scratch <= WINDOW):
        count_down(counter, preset).reset(reset)
    with Rung(slot.Cmd != 0, slot.Scratch <= WINDOW):
        copy(counter.Acc, slot.Result1)
    with Rung(slot.Cmd != 0, slot.Scratch <= 6, counter.Done):
        math(slot.Result2 * 10 + slot.Scratch, slot.Result2)
    with Rung(slot.Cmd != 0, slot.Scratch == RESET_FIRST):
        copy(counter.Acc, slot.Result3)
    with Rung(slot.Cmd != 0, slot.Scratch == RESET_LAST + 1):
        copy(counter.Acc, slot.Result4)
    done(slot)


def prove_down_reset_positive(slot: Any) -> None:
    _down_reset(slot, ClickCounter(11), 3, None)


def prove_down_reset_negative(slot: Any) -> None:
    _down_reset(slot, ClickCounter(12), -3, -2)


CLOCK_ON, CLOCK_OFF = 300, 500
END = 800


def _clocked_rton(slot: Any, clock: ClickTimer, rton: ClickTimer, holding: Any) -> None:
    # *holding* is on while the clock reads 300..499 ms; the RTON's rung is always on.
    with Rung(slot.Cmd != 0):
        on_delay(clock, 3000)
    with Rung(slot.Cmd != 0, clock.Acc >= CLOCK_ON, clock.Acc < CLOCK_OFF):
        out(holding)
    with Rung(slot.Cmd != 0):
        on_delay(rton, 200).reset(Or(slot.Cmd == 0, holding))


def prove_rton_reset_held(slot: Any) -> None:
    clock, rton = ClickTimer(301), ClickTimer(302)
    holding, at_drop = c[701], c[702]
    _clocked_rton(slot, clock, rton, holding)
    with Rung(slot.Cmd != 0, clock.Acc >= 250, clock.Acc < CLOCK_ON, rton.Done):
        copy(1, slot.Result1)
    with Rung(rise(holding), slot.Cmd != 0, rton.Done):
        copy(1, slot.Result2)
    with Rung(rise(holding), slot.Cmd != 0, rton.Acc != 0):
        math(slot.Result2 + 10, slot.Result2)
    with Rung(holding, slot.Cmd != 0, slot.Scratch != 0, Or(rton.Done, rton.Acc != 0)):
        math(slot.Result3 + 1, slot.Result3)
    with Rung(holding, slot.Cmd != 0):
        copy(1, slot.Scratch)
    with Rung(fall(holding), slot.Cmd != 0):
        out(at_drop)
    with Rung(at_drop, rton.Done):
        copy(1, slot.Result4)
    with Rung(at_drop, rton.Acc != 0):
        math(slot.Result4 + 10, slot.Result4)
    with Rung(slot.Cmd != 0, clock.Acc >= CLOCK_OFF + 100):
        copy(1, slot.Fired)


def prove_rton_reset_after(slot: Any) -> None:
    clock, rton = ClickTimer(303), ClickTimer(304)
    holding = c[703]
    _clocked_rton(slot, clock, rton, holding)
    with Rung(slot.Cmd != 0, clock.Acc >= 520, clock.Acc < 600, rton.Acc > 0, rton.Acc < 200):
        copy(1, slot.Result1)
    with Rung(slot.Cmd != 0, clock.Acc >= 520, clock.Acc < 640, rton.Done):
        copy(1, slot.Result2)
    with Rung(slot.Cmd != 0, clock.Acc >= END, rton.Done):
        copy(1, slot.Result3)
    with Rung(slot.Cmd != 0, clock.Acc >= 540, rton.Acc == 0):
        copy(1, slot.Result4)
    with Rung(slot.Cmd != 0, clock.Acc >= END):
        copy(1, slot.Fired)


group = Group(
    "reset_pins",
    [
        case(
            "A down counter of preset 3 counting a held input, reset on scans 3 and 4 with the "
            "input still on: Acc at the end (Result1), the scans 1..6 it is done on as digits "
            "(Result2), Acc on the first reset scan (Result3) and on scan 5 (Result4)",
            ladder=prove_down_reset_positive,
            expect={"Result1": 0, "Result2": 1256, "Result3": 0, "Result4": 0},
            scans=scans_for(),
            probe="a down counter's reset held under a held input: Acc and Done after it",
        ),
        case(
            "A down counter of preset -3 staged at Acc -2, counting a held input, reset on "
            "scans 3 and 4 with the input still on: Acc at the end (Result1), the scans 1..6 "
            "it is done on as digits (Result2), Acc on the first reset scan (Result3) and on "
            "scan 5 (Result4)",
            ladder=prove_down_reset_negative,
            expect={"Result1": 0, "Result2": 12, "Result3": 0, "Result4": 0},
            scans=scans_for(),
            probe="a negative preset down counter's reset held under a held input",
        ),
        case(
            "An RTON of 200 ms on an always true rung, reset for clock 300..499 ms: it was "
            "done before (Result1 = 1), the first reset scan reads Done and Acc (Result2), the "
            "later reset scans count Done or Acc (Result3), the first scan after it reads "
            "Done and Acc (Result4)",
            ladder=prove_rton_reset_held,
            expect={"Result1": 1, "Result2": 0, "Result3": 0, "Result4": 10},
            scans=100,
            probe="an RTON reset while its rung is on: Acc and Done on the reset and the drop",
        ),
        case(
            "An RTON of 200 ms on an always true rung, reset for clock 300..499 ms: after the "
            "reset drops it is timing (Result1 = 1), not done before 640 ms (Result2 = 0), "
            "done by 800 ms (Result3 = 1), and its Acc is not 0 after 540 ms (Result4 = 0)",
            ladder=prove_rton_reset_after,
            expect={"Result1": 1, "Result2": 0, "Result3": 1, "Result4": 0},
            scans=100,
            probe="an RTON starts timing when its reset drops under a rung that stayed on",
        ),
    ],
)


def test_reset_pins():
    assert_all_passed(run(group))
