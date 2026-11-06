"""A For whose count is a DD holding -1: the ``for_dd_minus`` group (part ``follow_values``).

A DS count of -1 runs 65535 times (``loop/negative_count_reads_unsigned``), and a DD count is read
whole (``for_wide/count_forms`` ran 65537 and 65536).  A DD count is read signed too: -1 runs none
(hardware 2026-11-06, read 1/1/0 when C401 armed it by hand, the PLC stayed in RUN), so the case
arms itself and needs no person or raised watchdog.

- ``minus_one_count``: the case writes -1 into the count DD81 (Result1 = 1) and runs the For over
  it once, counting into DD82.  Result2 = 1: no iteration (DD82 = 0); 2 for 65535, the low word
  unsigned; 3 for one.  Result3 is DD82 / 1000.

The cases own what ``memory.OWNED_APART`` lists under this group.
"""

from __future__ import annotations

from typing import Any

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import Rung, copy, fill, forloop, math
from tests.conformance.memory import dd
from tests.conformance.probe_kit import class_of, finish, once

COUNT, RUNS = dd[81], dd[82]


def prove_minus_one_count(slot: Any) -> None:
    with Rung(*once(slot)):
        copy(-1, COUNT)
        copy(1, slot.Result1)
    with Rung(*once(slot)):
        with forloop(COUNT):
            math(RUNS + 1, RUNS)
    class_of(slot, RUNS, [0, 65535, 1], slot.Result2)
    with Rung(*once(slot)):
        math(RUNS / 1000, slot.Result3)
    finish(slot)


def init_minus_one_count(slot: Any) -> None:
    fill(0, dd.select(81, 82))


group = Group(
    "for_dd_minus",
    [
        case(
            "A For over a DD count of -1 (Result1 = 1) runs no iteration (Result2 = 1; 2 for the "
            "low word 65535, 3 for once)",
            ladder=prove_minus_one_count,
            init=init_minus_one_count,
            expect={"Result1": 1, "Result2": 1, "Result3": 0},
        ),
    ],
)


def test_for_dd_minus():
    assert_all_passed(run(group))
