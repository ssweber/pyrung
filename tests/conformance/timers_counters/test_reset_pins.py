"""The ``reset_pins`` group (part ``follow3_reset``) holds a reset on while its rung is on.

Each case holds the reset on while the rung stays on, then drops it with the rung still on.

``down_reset_positive`` (CT11, preset 3) and ``down_reset_negative`` (CT12, preset -3, Acc
staged at -2 on scan 1).  The count input is on for scans 1..8 and the reset for scans 3 and 4.
Result1 is Acc at the end.  Result2 lists the scans 3..6 where Done is off, as digits (34 is
scans 3 and 4).  Result3 is Acc on scan 3, the first reset scan.  Result4 is Acc on scan 5, the
first scan after the reset.

- Reset clears Acc and Done, nothing counts the held input again, and Done is judged again once
  the reset is off.  pyrung reads Result2 = 34 for preset 3 (0 <= 3 is true on scan 5) and 3456
  for preset -3 (0 <= -3 is false), and 0 for Result1, Result3 and Result4.
- Done is not judged again until a count: Result2 = 3456 for both presets.
- The held input counts again as a new edge once the reset drops: Result1 = Result4 = -1.
- Reset loads the preset into the down counter.  Result3 = 3 (preset 3) or -3 (preset -3).
  Done is on on every scan for preset 3 (Result2 = 0).

``rton_reset_held`` (T301 clock, T302 RTON) and ``rton_reset_after`` (T303 clock, T304 RTON).
The RTON has 200 ms and its rung is always on.  Each case runs in steps that ``Scratch`` holds.
Step 0 ends on the scan the RTON's Acc first reads its 200 ms preset or more.  Its reset
(C701, C703) is then on until the clock reads 200 ms.  The clock times from the reset's first
scan.
Each step's rungs come below the RTON in reverse order, so each new step starts on the next
scan.

- ``rton_reset_held``: Result1 is 1 if Done is on as step 0 ends (the control).  Step 1 is the
  first reset scan.  Result2 is 1 if Done is on, plus 10 if Acc is not 0, then.  Step 2 is the
  later reset scans, and Result3 counts those where Done is on or Acc is not 0.  Step 3 starts
  on the first scan after the reset.  A TON beside the RTON (T305) starts on that scan.  A
  CLICK's TON counts the time of the scan it starts on (the hardware notes in
  ``tests/core/test_timers.py``).  Step 3 ends on the scan the TON first reads 20 ms or more.
  Result4 is 1 if the RTON's Acc equals the TON's with Done off then, and 2 if it is under the
  TON's.  Result4 = 0 is an RTON ahead of the TON, or Done on.  pyrung reads 1, 0, 0, 1.  A reset
  that leaves Done on, or counts while it holds, reads Result2 = 1 or above, or Result3 above 0.
  An RTON that starts timing a scan after the drop misses that scan's time.  It reads Result4 = 2
  when the drop scan adds 1 ms or more.  A shorter drop scan can leave it level with the TON
  (Result4 = 1).
- ``rton_reset_after``: once the clock reads 300 ms, 100 ms after the drop, Result1 is 1 if Acc
  is above 0 and under 200 (timing started once the reset dropped).  Result4 is 1 if Acc is
  still 0 then.  Result2 is 1 if Done is on with Acc under 200 on any scan after the drop.
  Result3 is 1 if Done is on once the clock reads 500 ms.  A reset that holds until the rung
  falls reads Result1 = 0, Result3 = 0, Result4 = 1.

No CLICK has read these two cases.  Every expectation is pyrung's.  The group owns what
``memory.OWNED_APART`` lists under it.  The two RTON cases raise ``Fired`` in their last step.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.conformance import _runner as runner_module
from pyrung.core import And, Or, Rung, copy, count_down, math, on_delay, out
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
    with Rung(slot.Cmd != 0, slot.Scratch >= RESET_FIRST, slot.Scratch <= 6, ~counter.Done):
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


#: The RTON's Acc that ends step 0: its 200 ms preset, so it has timed out.
PRESET_REACHED = 200
#: The clock's reading when the reset drops.
DROP = 200
#: rton_reset_held compares the RTON with a TON once the TON reads this, in ms.
COMPARE_AT = 20


def _step(slot: Any, n: int) -> tuple[Any, Any]:
    return (slot.Cmd != 0, slot.Scratch == n)


def _rton(slot: Any, rton: ClickTimer, holding: Any) -> None:
    # The RTON's rung is always on.  Its reset is on while *holding* is.
    with Rung(slot.Cmd != 0):
        on_delay(rton, 200).reset(Or(slot.Cmd == 0, holding))


def prove_rton_reset_held(slot: Any) -> None:
    clock, rton = ClickTimer(301), ClickTimer(302)
    holding = c[701]
    with Rung(slot.Cmd != 0, Or(slot.Scratch == 1, slot.Scratch == 2)):
        out(holding)
    # The clock times the reset, from its first scan.
    with Rung(slot.Cmd != 0, holding):
        on_delay(clock, 3000)
    _rton(slot, rton, holding)
    # On from the first scan after the reset, beside the RTON, so both see that scan's time.
    ton = ClickTimer(305)
    with Rung(slot.Cmd != 0, slot.Scratch >= 3):
        on_delay(ton, 3000)
    with Rung(*_step(slot, 4)):
        copy(1, slot.Fired)
    ready = (*_step(slot, 3), ton.Acc >= COMPARE_AT)
    with Rung(*ready, rton.Acc == ton.Acc, ~rton.Done):
        copy(1, slot.Result4)
    with Rung(*ready, rton.Acc < ton.Acc):
        copy(2, slot.Result4)
    with Rung(*ready):
        copy(4, slot.Scratch)
    with Rung(*_step(slot, 2), Or(rton.Done, rton.Acc != 0)):
        math(slot.Result3 + 1, slot.Result3)
    with Rung(*_step(slot, 2), clock.Acc >= DROP):
        copy(3, slot.Scratch)
    with Rung(*_step(slot, 1), rton.Done):
        copy(1, slot.Result2)
    with Rung(*_step(slot, 1), rton.Acc != 0):
        math(slot.Result2 + 10, slot.Result2)
    with Rung(*_step(slot, 1)):
        copy(2, slot.Scratch)
    with Rung(*_step(slot, 0), rton.Acc >= PRESET_REACHED, rton.Done):
        copy(1, slot.Result1)
    with Rung(*_step(slot, 0), rton.Acc >= PRESET_REACHED):
        copy(1, slot.Scratch)


def prove_rton_reset_after(slot: Any) -> None:
    clock, rton = ClickTimer(303), ClickTimer(304)
    holding = c[703]
    with Rung(*_step(slot, 1)):
        out(holding)
    # The clock times from the reset's first scan, and on past the drop.
    with Rung(slot.Cmd != 0, slot.Scratch >= 1):
        on_delay(clock, 3000)
    _rton(slot, rton, holding)
    with Rung(slot.Cmd != 0, slot.Scratch >= 2, slot.Fired == 0, rton.Done, rton.Acc < 200):
        copy(1, slot.Result2)
    with Rung(*_step(slot, 4)):
        copy(1, slot.Fired)
    with Rung(*_step(slot, 3), clock.Acc >= DROP + 300, rton.Done):
        copy(1, slot.Result3)
    with Rung(*_step(slot, 3), clock.Acc >= DROP + 300):
        copy(4, slot.Scratch)
    with Rung(*_step(slot, 2), clock.Acc >= DROP + 100, rton.Acc > 0, rton.Acc < 200):
        copy(1, slot.Result1)
    with Rung(*_step(slot, 2), clock.Acc >= DROP + 100, rton.Acc == 0):
        copy(1, slot.Result4)
    with Rung(*_step(slot, 2), clock.Acc >= DROP + 100):
        copy(3, slot.Scratch)
    with Rung(*_step(slot, 1), clock.Acc >= DROP):
        copy(2, slot.Scratch)
    with Rung(*_step(slot, 0), rton.Acc >= PRESET_REACHED):
        copy(1, slot.Scratch)


group = Group(
    "reset_pins",
    [
        case(
            "A down counter of preset 3 counting a held input, reset on scans 3 and 4 with the "
            "input still on: Acc at the end (Result1), the scans 3..6 it is not done on as digits "
            "(Result2), Acc on the first reset scan (Result3) and on scan 5 (Result4)",
            ladder=prove_down_reset_positive,
            expect={"Result1": 0, "Result2": 34, "Result3": 0, "Result4": 0},
            scans=scans_for(),
            probe="a down counter's reset held under a held input: Acc and Done after it",
        ),
        case(
            "A down counter of preset -3 staged at Acc -2, counting a held input, reset on "
            "scans 3 and 4 with the input still on: Acc at the end (Result1), the scans 3..6 "
            "it is not done on as digits (Result2), Acc on the first reset scan (Result3) and on "
            "scan 5 (Result4)",
            ladder=prove_down_reset_negative,
            expect={"Result1": 0, "Result2": 3456, "Result3": 0, "Result4": 0},
            scans=scans_for(),
            probe="a negative preset down counter's reset held under a held input",
        ),
        case(
            "An RTON of 200 ms on an always true rung, reset for 200 ms once its Acc reads "
            "200: it was done before (Result1 = 1), the first reset scan reads Done and Acc "
            "(Result2), the later reset scans count Done or Acc (Result3), and after it the RTON "
            "times the same as a TON whose rung comes on then (Result4 = 1, 2 a scan behind)",
            ladder=prove_rton_reset_held,
            expect={"Result1": 1, "Result2": 0, "Result3": 0, "Result4": 1},
            scans=100,
            probe="an RTON reset while its rung is on: Acc and Done on the reset and the drop",
        ),
        case(
            "An RTON of 200 ms on an always true rung, reset for 200 ms once its Acc reads "
            "200: 100 ms after the reset drops it is timing (Result1 = 1) and its Acc is not 0 "
            "(Result4 = 0), it is not done under its preset (Result2 = 0), and it is done 300 "
            "ms after the drop (Result3 = 1)",
            ladder=prove_rton_reset_after,
            expect={"Result1": 1, "Result2": 0, "Result3": 1, "Result4": 0},
            scans=100,
            probe="an RTON starts timing when its reset drops under a rung that stayed on",
        ),
    ],
)


def test_reset_pins():
    assert_all_passed(run(group))


@pytest.mark.parametrize("dt", [0.0004, 0.001, 0.010, 0.025, 0.040])
def test_rton_resets_at_any_scan(monkeypatch, dt):
    """The CLICK scans in about 1 ms or less, the soft PLC in 10..40 ms."""
    monkeypatch.setattr(runner_module, "DT", dt)
    rtons = [replace(c, scans=int(1.0 / dt)) for c in group.cases if c.id.startswith("rton_")]
    assert_all_passed(run(Group("reset_pins", rtons)))
