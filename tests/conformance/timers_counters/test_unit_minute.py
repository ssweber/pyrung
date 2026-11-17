"""The long pairs of ``test_unit_pairs.py``: the ``unit_minute`` group (subroutine
``Conf_UnitMinute``, part ``units``).  It has a group of its own because of its wait.
Run with ``GROUP=unit_pairs`` to leave it out.

``one_minute`` runs a 1 min timer against a 60 s one.  In process, at the 10 ms ``DT``, it
tells a count kept as float fractions of its unit from an exact count.  6000 scans of
10 ms sum to just under a minute.  A 1 min timer that counts in floats is therefore Done
on scan 6001 and the 60 s timer on 6000, and ``Result1`` reads 1.

A millisecond timer cannot join it.  CLICK's timer accumulator is a 16-bit Int, so its
preset stops at 32767.  A 60000 ms timer is never Done.  ``thirty_seconds`` runs 30000 ms
against 30 s, the longest round delay that milliseconds can hold.
"""

from __future__ import annotations

from typing import Any

from pyrung.conformance import Group, assert_all_passed, case, run
from tests.conformance.timers_counters.test_unit_pairs import pair_on_one_rung


def _prove_one_minute(slot: Any) -> None:
    pair_on_one_rung(slot, (11, 1, "Tm"), (12, 60, "Ts"))


def _prove_thirty_seconds(slot: Any) -> None:
    pair_on_one_rung(slot, (13, 30, "Ts"), (14, 30000, "Tms"))


group = Group(
    "unit_minute",
    [
        case(
            "A 1 min timer and a 60 s timer on one rung are Done on one scan",
            ladder=_prove_one_minute,
            expect={"Result1": 0, "Result2": 1},
            scans=6010,
            fires_within=75.0,
            probe="all units on one clock",
        ),
        case(
            "A 30 s timer and a 30000 ms timer on one rung are Done on one scan",
            ladder=_prove_thirty_seconds,
            expect={"Result1": 0, "Result2": 1},
            scans=3010,
            fires_within=45.0,
            probe="all units on one clock",
        ),
    ],
)


def test_unit_minute() -> None:
    assert_all_passed(run(group))
