"""Timers in different units with one delay are Done on one scan: the ``unit_pairs`` group
(subroutine ``Conf_UnitPairs``, part ``units``).

Over the wire time is the wall clock, so a case cannot choose a scan's length, or count
what a scan a timer should end on.  It asks the relation instead, which holds or not
whatever a scan takes: two timers on one rung, one in seconds and one in milliseconds, get
the same time on every scan.  A rung below them latches ``Result1`` on any scan one is Done
and the other is not, so ``Result1`` says "they ever differed" and is expected off;
``Result2`` says both got there, and ``Fired`` goes up with it.

On a real CLICK this asks whether all units come off one clock, which a program can lean
on.  A CLICK that reads ``Result1 = 1`` has not failed: that is the answer, and it goes to
the user.

In process ``DT`` is 10 ms, where the seconds pairs have always agreed, so these cases pin
that they stay so; the pair that has told the units apart is in ``test_unit_minute.py``.
A timer whose count is a sum of float fractions of its unit ends a scan late wherever that
sum falls short, and a pair of them differs here for that one scan.
"""

from __future__ import annotations

from typing import Any

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import Rung, copy, on_delay
from tests.conformance.memory import ClickTimer

#: A timer pair's timers: a number each, a preset and a unit each.
Timed = tuple[int, int, str]


def pair_on_one_rung(slot: Any, one: Timed, other: Timed) -> None:
    """Result1 = the two ever differed on a scan; Result2 = both Done, and ``Fired``."""
    first, second = ClickTimer(one[0]), ClickTimer(other[0])
    with Rung(slot.Cmd != 0):
        on_delay(first, preset=one[1], unit=one[2])
        on_delay(second, preset=other[1], unit=other[2])
    with Rung(slot.Cmd != 0, first.Done, ~second.Done):
        copy(1, slot.Result1)
    with Rung(slot.Cmd != 0, ~first.Done, second.Done):
        copy(1, slot.Result1)
    with Rung(slot.Cmd != 0, first.Done, second.Done):
        copy(1, slot.Result2)
        copy(1, slot.Fired)


def _prove_one_second(slot: Any) -> None:
    pair_on_one_rung(slot, (1, 1, "Ts"), (2, 1000, "Tms"))


def _prove_three_seconds(slot: Any) -> None:
    pair_on_one_rung(slot, (3, 3, "Ts"), (4, 3000, "Tms"))


group = Group(
    "unit_pairs",
    [
        case(
            "A 1 s timer and a 1000 ms timer on one rung are Done on one scan",
            ladder=_prove_one_second,
            expect={"Result1": 0, "Result2": 1},
            scans=110,
            fires_within=10.0,
            probe="all units on one clock",
        ),
        case(
            "A 3 s timer and a 3000 ms timer on one rung are Done on one scan",
            ladder=_prove_three_seconds,
            expect={"Result1": 0, "Result2": 1},
            scans=310,
            fires_within=10.0,
            probe="all units on one clock",
        ),
    ],
)


def test_unit_pairs() -> None:
    assert_all_passed(run(group))
