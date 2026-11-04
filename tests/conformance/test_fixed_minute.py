"""The long delays of ``test_fixed_scan.py``, counted: the ``fixed_minute`` group (subroutine
``Conf_FixedMinute``, part O, run with CLICK's fixed scan on).  A group of its own for its
wait: leave it out with ``GROUP=fixed_scan``.

At a scan of ``dt``, the 1 min and 60 s timers are Done on ``ceil(60 / dt)`` (600 at 100 ms,
6000 at 10 ms, 240 at 250 ms), or a scan more for a late first scan or a slip.  10 ms and
250 ms are where a count kept as float fractions of its unit lost a minute timer a scan and
not a seconds timer, and 100 ms the reverse, so these are the times asked (``AGENTS.md``,
"Fixed scan parts").  A CLICK's 16-bit accumulator holds no 60000 ms preset, so the
milliseconds side is the pair 30 s and 30000 ms.

Off fixed scan these counts vary with the wall clock, the ranges pass, and they say nothing.
"""

from __future__ import annotations

from typing import Any

from pyrung.conformance import Group, assert_all_passed, case, run
from tests.conformance.test_fixed_scan import ANY_COUNT, count_timers


def _prove_long_timers(slot: Any) -> None:
    count_timers(
        slot,
        [(41, 60, "Ts"), (42, 1, "Tm"), (43, 30, "Ts"), (44, 30000, "Tms")],
        [slot.Result1, slot.Result2, slot.Result3, slot.Result4],
    )


group = Group(
    "fixed_minute",
    [
        case(
            "On-delays of 60 s, 1 min, 30 s and 30000 ms: the scan each is Done on",
            ladder=_prove_long_timers,
            expect={
                "Result1": ANY_COUNT,
                "Result2": ANY_COUNT,
                "Result3": ANY_COUNT,
                "Result4": ANY_COUNT,
            },
            scans=6010,
            fires_within=75.0,
            probe="ceil(delay/dt), or a scan more",
        ),
    ],
)


def test_fixed_minute() -> None:
    assert_all_passed(run(group))


def test_at_the_in_process_fixed_scan_each_count_is_the_ceil() -> None:
    (result,) = run(group)
    assert result.actual == {"Result1": 6000, "Result2": 6000, "Result3": 3000, "Result4": 3000}
