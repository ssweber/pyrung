"""A timed-out TOF whose TD the ladder writes to 0: the ``tof_rewrite`` group (part ``follow2_coils``).

``done_after_td_zeroed`` (T1): a TOF of 200 ms, in steps that ``Scratch`` holds.  Each step moves on
from the TOF's own Done and TD, so no reading depends on the scan time.  A step's rungs come below
the TOF in reverse order, so each new step starts on the next scan.

- Step 0: the TOF's rung is true.  The step ends once Done is on.
- Step 1: the rung is false from here on.  The step ends on the scan the TD first reads 300 or
  more.  Result1 is 1 if Done is on then (it is off, since 300 is past the preset).
- Step 2: a Copy below the TOF writes its TD to 0, for one scan.
- Step 3: the scan after the write.  Result2 is 1 if Done is on, and Result3 is 1 if the TD is
  under the preset.
- Step 4: the step ends on the first scan Done is off again.  Result4 is 1 if the TD is then at the
  preset or past it, 2 if it is under it.

A CLICK read an earlier ladder of 20 ms that counted scans (2026-11-10, twice).  Done came back on
and the TD counted from 0.  So a TOF that has been enabled judges ``TD < preset`` on every scan its
rung is false, and a TD written to 0 starts its delay again (``OffDelayInstruction``).  A TOF never
enabled still rests at Done off, TD 0 (``timers/tof_never_enabled``).  pyrung used to keep a
timed-out TOF at rest after its TD read 0.  It would read Result2 = 0 and Result4 = 2 here.  The
earlier ladder read Done one scan after the write.  At the soft PLC's 20 ms scan, that one tick
already reached the 20 ms preset.  The preset here is 200 ms, five times the soft PLC's longest
scan.

The case's reset depends on nothing the TOF does: its rung is false through the reset, and its
Done and TD are kept (a TOF times on, and its Done may come back on, after the case).  The case owns
what ``memory.OWNED_APART`` lists under this group.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.conformance import _runner as runner_module
from pyrung.core import Rung, copy, off_delay
from tests.conformance.memory import ClickTimer

TOF = ClickTimer(291)
PRESET = 200
#: The TD that ends step 1: past the preset, so the TOF has timed out.
TIMED_OUT = 300


def prove_done_after_td_zeroed(slot: Any) -> None:
    def step(n: int) -> tuple[Any, Any]:
        return (slot.Cmd != 0, slot.Scratch == n)

    with Rung(*step(0)):
        off_delay(TOF, PRESET)
    with Rung(*step(5)):
        copy(1, slot.Fired)
    with Rung(*step(4), ~TOF.Done, TOF.Acc >= PRESET):
        copy(1, slot.Result4)
    with Rung(*step(4), ~TOF.Done, TOF.Acc < PRESET):
        copy(2, slot.Result4)
    with Rung(*step(4), ~TOF.Done):
        copy(5, slot.Scratch)
    with Rung(*step(3), TOF.Done):
        copy(1, slot.Result2)
    with Rung(*step(3), TOF.Acc < PRESET):
        copy(1, slot.Result3)
    with Rung(*step(3)):
        copy(4, slot.Scratch)
    with Rung(*step(2)):
        copy(0, TOF.Acc)
        copy(3, slot.Scratch)
    with Rung(*step(1), TOF.Acc >= TIMED_OUT, TOF.Done):
        copy(1, slot.Result1)
    with Rung(*step(1), TOF.Acc >= TIMED_OUT):
        copy(2, slot.Scratch)
    with Rung(*step(0), TOF.Done):
        copy(1, slot.Scratch)


group = Group(
    "tof_rewrite",
    [
        case(
            "A timed-out TOF (Result1 = 0, Done off at a TD of 300 on a 200 ms preset) whose TD "
            "the ladder writes to 0 has Done on again the scan after (Result2 = 1), the TD "
            "under the preset (Result3 = 1), and times out again at the preset (Result4 = 1)",
            ladder=prove_done_after_td_zeroed,
            keep=(TOF.Done, TOF.Acc),
            expect={"Result1": 0, "Result2": 1, "Result3": 1, "Result4": 1},
            scans=100,
        ),
    ],
)


def test_tof_rewrite():
    assert_all_passed(run(group))


@pytest.mark.parametrize("dt", [0.001, 0.020, 0.023, 0.030, 0.040])
def test_tof_rewrite_at_any_scan(monkeypatch, dt):
    """The CLICK scans in about 1 ms, the soft PLC in 20..40 ms under load."""
    monkeypatch.setattr(runner_module, "DT", dt)
    assert_all_passed(run(Group("tof_rewrite", [replace(group.cases[0], scans=int(0.7 / dt))])))
