"""The long delays of ``test_fixed_scan.py``, counted: the ``fixed_minute`` group (subroutine
``Conf_FixedMinute``, part O, run with CLICK's fixed scan on).  A group of its own for its
wait: leave it out with ``GROUP=fixed_scan``.

At a scan of ``dt``, the 1 min and 60 s timers are Done on ``ceil(60 / dt)`` (600 at 100 ms,
6000 at 10 ms, 240 at 250 ms), or a scan more for a late first scan or a slip.  10 ms and
250 ms are where a count kept as float fractions of its unit lost a minute timer a scan and
not a seconds timer, and 100 ms the reverse, so these are the times asked (``AGENTS.md``,
"Fixed scan parts").  A CLICK's 16-bit accumulator holds no 60000 ms preset, so the
milliseconds side is the pair 30 s and 30000 ms.

Each case latches the scan it was counted under as ``test_fixed_scan.py`` says: ``Result3``
SD13, ``Result4`` SD12 (above SD13, a scan overran in the window), ``ErrorCode`` SC202.

Off fixed scan these counts vary with the wall clock, the ranges pass, and they say nothing.
"""

from __future__ import annotations

from typing import Any

from pyrung.conformance import Group, assert_all_passed, case, run
from tests.conformance.test_fixed_scan import IN_PROCESS_SETTING, READING, count_two_timers


def _prove_one_minute(slot: Any) -> None:
    count_two_timers(slot, (41, 60, "Ts"), (42, 1, "Tm"))


def _prove_half_minute(slot: Any) -> None:
    count_two_timers(slot, (43, 30, "Ts"), (44, 30000, "Tms"))


group = Group(
    "fixed_minute",
    [
        case(
            "On-delays of 60 s and 1 min: the scan each is Done on, and the scan setting",
            ladder=_prove_one_minute,
            expect=READING,
            scans=6010,
            fires_within=75.0,
            probe="ceil(delay/dt), or a scan more",
        ),
        case(
            "On-delays of 30 s and 30000 ms: the scan each is Done on, and the scan setting",
            ladder=_prove_half_minute,
            expect=READING,
            scans=3010,
            fires_within=45.0,
            probe="ceil(delay/dt), or a scan more",
        ),
    ],
)


def test_fixed_minute() -> None:
    assert_all_passed(run(group))


def test_at_the_in_process_fixed_scan_each_count_is_the_ceil() -> None:
    results = {r.case.id: r.actual for r in run(group)}
    assert results["one_minute"] == {"Result1": 6000, "Result2": 6000, **IN_PROCESS_SETTING}
    assert results["half_minute"] == {"Result1": 3000, "Result2": 3000, **IN_PROCESS_SETTING}
