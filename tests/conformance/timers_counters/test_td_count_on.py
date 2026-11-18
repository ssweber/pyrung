"""Check that a TON counts on from a TD the ladder writes mid-count: the ``td_count_on`` group
(part ``follow4_reads``).

A CLICK C2-03CPU on firmware 4.00 ran a TON of 1000 ms and had its TD written 500 over Modbus
about 300 ms in.  The TD counted on from 500 at one a ms.  The ms it had counted before the
write were gone.  This case asks the same with a ladder write, and has not run on a CLICK yet.

``ton_counts_on_from_written_td``: TON A has a preset of 1000 ms.  Each step's rungs come below
the timers in reverse order, so each new step starts on the next scan.

- Step 0: A times from 0.  On the scan its TD reads 300 or more, the ladder writes 500 into it.
- Step 1: a clock (a TON) times from the scan after the write.  On the scan A's Done comes on,
  Result1 is the clock's TD and Result2 is A's TD.  The case fires on the next scan.

A TON that counts on from 500 is Done about 500 ms after the write, so Result1 is 450..550
for any scan up to 50 ms.  A TON that kept the ms it had counted would be Done about 200 ms
after the write.  One that ignored the write would be Done about 700 ms after it, and one that
restarted from 0 about 1000 ms after it.  Result2 is the control: A's TD is 1000..1050 on the
scan its Done comes on.

Step 0 ends on A's TD and step 1 on A's Done.  T323 times the case out at 3000 ms
(``scans.deadline``).  The case owns what ``memory.OWNED_APART`` lists under this group.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.conformance import _runner as runner_module
from pyrung.core import Rung, copy, on_delay
from tests.conformance.memory import ClickTimer
from tests.conformance.scans import deadline

TIMER, CLOCK, DEADLINE = ClickTimer(321), ClickTimer(322), ClickTimer(323)
PRESET = 1000
#: Step 0 writes once A's TD reads this.
WRITE_AT = 300
WRITTEN = 500
#: The longest scan the part assumes, in ms.
SCAN_MS = 50
DEADLINE_MS = 3000


def _step(slot: Any, n: int) -> tuple[Any, Any]:
    return (slot.Cmd != 0, slot.Scratch == n)


def prove_ton_counts_on_from_written_td(slot: Any) -> None:
    with Rung(slot.Cmd != 0):
        on_delay(TIMER, PRESET)
    with Rung(slot.Cmd != 0, slot.Scratch >= 1):
        on_delay(CLOCK, 5000)
    with Rung(*_step(slot, 2)):
        copy(1, slot.Fired)
    with Rung(*_step(slot, 1), TIMER.Done):
        copy(CLOCK.Acc, slot.Result1)
        copy(TIMER.Acc, slot.Result2)
        copy(2, slot.Scratch)
    with Rung(*_step(slot, 0), TIMER.Acc >= WRITE_AT):
        copy(WRITTEN, TIMER.Acc)
        copy(1, slot.Scratch)
    deadline(slot, DEADLINE, DEADLINE_MS)


group = Group(
    "td_count_on",
    [
        case(
            "A TON of 1000 ms whose TD the ladder writes 500 at 300 counts on from 500. It is "
            "Done about 500 ms after the write (Result1 = 450..550), with its TD at the preset "
            "or just past it (Result2 = 1000..1050)",
            ladder=prove_ton_counts_on_from_written_td,
            expect={
                "Result1": range(WRITTEN - SCAN_MS, WRITTEN + SCAN_MS + 1),
                "Result2": range(PRESET, PRESET + SCAN_MS + 1),
            },
            scans=150,
        ),
    ],
)


def test_td_count_on():
    assert_all_passed(run(group))


@pytest.mark.parametrize("dt", [0.001, 0.010, 0.025, 0.040, 0.050])
def test_td_count_on_at_any_scan(monkeypatch, dt):
    """The CLICK scans in about 1 ms and the soft PLC in 10..40 ms."""
    monkeypatch.setattr(runner_module, "DT", dt)
    cases = [replace(c, scans=int(1.5 / dt)) for c in group.cases]
    assert_all_passed(run(Group("td_count_on", cases)))
