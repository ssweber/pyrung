"""A debounce's off TD while its coil is off: the ``deb_off`` group (part ``follow3_reset``).

``off_td_with_coil_off``: debounce A has 300 ms on and 1000 ms off, and its coil never comes on.
Debounce B is the control.  It has 0 ms on and 1000 ms off, so its coil comes on with the input.
One C bit is the input of both.  Each step's rungs come below the debounces in reverse order, so
each new step starts on the next scan.

- Step 0 (one scan): the input is off.  The ladder writes 50 into A's off TD.
- Step 1: the input stays off until a TON clock reads 200 ms.  ErrorCode is A's off TD then.  The
  same rung writes 50 into it again.
- Step 2 (one scan): the input comes on.  Result1 is A's off TD on that scan.
- Step 3: the input stays on until A's on TD reads 100, under its 300 ms preset.  The rung that
  ends the step writes 50 into A's off TD again.
- Step 4 (one scan): the input goes off.  Result2 is A's off TD on that scan.
- Step 5: the input stays off until a second TON clock reads 200 ms.  Result3 is A's off TD then,
  and Result4 is B's.  The case fires on the next scan.

A CLICK read 50, 0, 50, 50 and a Result4 of 201 (2026-11-12).  So a debounce's off TD counts only
while its coil is on and its input is off.  With the coil off, the off TD holds its value,
including one the ladder wrote.  The input's rise clears it.  pyrung reads the same.

Result4 is the control.  B's coil came on with the input.  B's off TD counts from the fall, as a
CLICK read (``deb_fall/off_td_at_fall``, ``coil_timing/off_td_held``).  So Result4 is the clock's
200 ms plus the fall scan, 200..300.  A reading under 200 means B's off side did not count, and
then the other readings say nothing.

Steps 1 and 5 end on a clock.  Steps 0, 2 and 4 last one scan.  Step 3 ends on A's on TD, which
the init writes to 0.  So no step waits on the off TD this case asks about.  T317 times the case out
at 2000 ms (``scans.deadline``).  The case owns what ``memory.OWNED_APART`` lists under this group.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.conformance import _runner as runner_module
from pyrung.core import Or, Rung, copy, debounce, on_delay, out
from tests.conformance.memory import ClickTimer, c
from tests.conformance.scans import deadline

#: C711 the input, C712 debounce A, C713 debounce B.
INPUT, DEB_A, DEB_B = c[711], c[712], c[713]
A_ON, A_OFF, B_ON, B_OFF = (ClickTimer(n) for n in range(311, 315))
#: The clocks of steps 1 and 5, and the case's deadline.
BEFORE, AFTER, DEADLINE = ClickTimer(315), ClickTimer(316), ClickTimer(317)

#: Written into A's off TD.  It is not 0, so a TD the rise clears reads apart from one it keeps.
KEPT = 50
#: Step 3 ends once A's on TD reads this, under A's 300 ms on preset.
PULSE = 100
#: Steps 1 and 5 end once their clock reads this, in ms.
WAIT = 200
#: The longest scan the part assumes, in ms.
SCAN_MS = 50
DEADLINE_MS = 2000


def _step(slot: Any, n: int) -> tuple[Any, Any]:
    return (slot.Cmd != 0, slot.Scratch == n)


def prove_off_td_with_coil_off(slot: Any) -> None:
    with Rung(slot.Cmd != 0, Or(slot.Scratch == 2, slot.Scratch == 3)):
        out(INPUT)
    with Rung(INPUT):
        debounce(DEB_A, on=(A_ON, 300), off=(A_OFF, 1000))
    with Rung(INPUT):
        debounce(DEB_B, on=(B_ON, 0), off=(B_OFF, 1000))
    with Rung(*_step(slot, 1)):
        on_delay(BEFORE, 3000)
    with Rung(*_step(slot, 5)):
        on_delay(AFTER, 3000)
    with Rung(*_step(slot, 6)):
        copy(1, slot.Fired)
    with Rung(*_step(slot, 5), AFTER.Acc >= WAIT):
        copy(A_OFF.Acc, slot.Result3)
        copy(B_OFF.Acc, slot.Result4)
        copy(6, slot.Scratch)
    with Rung(*_step(slot, 4)):
        copy(A_OFF.Acc, slot.Result2)
        copy(5, slot.Scratch)
    with Rung(*_step(slot, 3), A_ON.Acc >= PULSE):
        copy(KEPT, A_OFF.Acc)
        copy(4, slot.Scratch)
    with Rung(*_step(slot, 2)):
        copy(A_OFF.Acc, slot.Result1)
        copy(3, slot.Scratch)
    with Rung(*_step(slot, 1), BEFORE.Acc >= WAIT):
        copy(A_OFF.Acc, slot.ErrorCode)
        copy(KEPT, A_OFF.Acc)
        copy(2, slot.Scratch)
    with Rung(*_step(slot, 0)):
        copy(KEPT, A_OFF.Acc)
        copy(1, slot.Scratch)
    deadline(slot, DEADLINE, DEADLINE_MS)


def init_off_td_with_coil_off(slot: Any) -> None:
    # Step 3 ends on A's on TD, so it starts from 0 whether or not the rise clears it.
    copy(0, A_ON.Acc)
    copy(0, B_ON.Acc)


group = Group(
    "deb_off",
    [
        case(
            "A debounce's off TD holds while its coil is off. The 50 the ladder writes stands "
            "200 ms with the input off (ErrorCode = 50). The input's rise clears it (Result1 = 0). "
            "A 50 written again stands on the fall (Result2 = 50) and 200 ms after (Result3 = 50). "
            "A debounce of 0 ms on beside it counts from the fall (Result4 = 200..300)",
            ladder=prove_off_td_with_coil_off,
            init=init_off_td_with_coil_off,
            ready=lambda slot: (~DEB_A, ~DEB_B),
            # A's off TD holds the 50 the ladder wrote.  B's counts to its preset in the reset.
            keep=(A_OFF.Acc, B_OFF.Acc),
            expect={
                "ErrorCode": KEPT,
                "Result1": 0,
                "Result2": KEPT,
                "Result3": KEPT,
                "Result4": range(WAIT, WAIT + 2 * SCAN_MS + 1),
            },
            scans=100,
        ),
    ],
)


def test_deb_off():
    assert_all_passed(run(group))


@pytest.mark.parametrize("dt", [0.001, 0.010, 0.025, 0.040, 0.050])
def test_deb_off_at_any_scan(monkeypatch, dt):
    """The CLICK scans in about 1 ms, the soft PLC in 10..40 ms."""
    monkeypatch.setattr(runner_module, "DT", dt)
    cases = [replace(c, scans=int(1.0 / dt)) for c in group.cases]
    assert_all_passed(run(Group("deb_off", cases)))
