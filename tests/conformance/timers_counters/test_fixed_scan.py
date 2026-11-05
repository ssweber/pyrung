"""The scan a timer is Done on as a CLICK counts it: the ``fixed_scan`` group (subroutine
``Conf_FixedScan``, part ``fixed_scan``, run with CLICK's fixed scan on at 100 ms; ``AGENTS.md``, "Fixed
scan part").  For curiosity: pyrung's count is exact by rule, whatever a CLICK reads
(``tests/core/test_timer_exact.py``), and a CLICK that reads otherwise is recorded, not chased.

With SC202 on and a scan time set, a CLICK scan is a known time, so a count of scans is a
number, and the program counts its own: ``Scratch`` is the scan in progress from the scan its
rung goes true (1), and each timer's Done latches the count into a Result.

A count means nothing without the scan it was counted under, so a counting case latches that
too, on the scan it raises ``Fired``: ``Result3`` the set time (SD13), ``Result4`` the longest
scan so far (SD12: above the set time, a scan overran in the window), ``ErrorCode`` fixed scan
mode (SC202).  That leaves ``Result1`` and ``Result2`` for two counts a case.

Every expectation is a :func:`~tests.conformance.probe_kit.between`, so a pass says nothing:
what a case read goes in ``readings.toml`` with its setting, and never in ``confirmed.toml``.
1 s at 100 ms is scan 10, as in pyrung at a fixed ``dt``, where every scan adds its time and
the rung's first scan is a whole scan of it; scan 11 if the first scan adds no time, or the
timer loses a scan to a slip.

Off fixed scan (a CLICK without the setting, the local soft PLC, ``TARGET=circuitpy``) a scan
is as long as it takes, and the counts vary with the wall clock: the ranges pass, and each
case's ``ErrorCode`` shows that mode made it.  In process ``DT`` is fixed (SC202 on, SD13 10),
and that is the run this file pins.
"""

from __future__ import annotations

from typing import Any

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import Rung, calc, copy, off_delay, on_delay, time_drum
from pyrung.core.system_points import system
from tests.conformance.memory import ClickTimer, c, ds, td
from tests.conformance.probe_kit import between

#: Any reading a count or a setting can leave in a slot's Int.
ANY_COUNT = between(0, 32767)
#: Two counts, and the scan they were counted under.
READING: dict[str, range] = {
    "Result1": ANY_COUNT,
    "Result2": ANY_COUNT,
    "Result3": ANY_COUNT,
    "Result4": ANY_COUNT,
    "ErrorCode": between(0, 1),
}


def count_scans(slot: Any) -> None:
    """``Scratch`` = the scan in progress, holding at the top of its Int: the first rung."""
    with Rung(slot.Cmd != 0, slot.Scratch < 32767):
        calc(slot.Scratch + 1, slot.Scratch)


def latch_count(slot: Any, done: Any, into: Any) -> None:
    """*into* = the scan *done* first holds on."""
    with Rung(slot.Cmd != 0, done, into == 0):
        copy(slot.Scratch, into)


def fire_with_the_scan_setting(slot: Any) -> None:
    """``Fired`` once both counts are in, and on that scan what the scan was set to."""
    answered = [slot.Cmd != 0, slot.Fired == 0, slot.Result1 != 0, slot.Result2 != 0]
    with Rung(*answered, system.sys.fixed_scan_mode):
        copy(1, slot.ErrorCode)
    with Rung(*answered):
        copy(system.sys.scan_time_fixed_setup_ms, slot.Result3)
        copy(system.sys.scan_time_max_ms, slot.Result4)
        copy(1, slot.Fired)


def _prove_scan_setup(slot: Any) -> None:
    with Rung(slot.Cmd != 0, slot.Scratch < 3):
        calc(slot.Scratch + 1, slot.Scratch)
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        copy(system.sys.scan_time_fixed_setup_ms, slot.Result2)
        copy(system.sys.scan_time_current_ms, slot.Result3)
        copy(system.sys.scan_time_max_ms, slot.Result4)
    with Rung(slot.Cmd != 0, slot.Fired == 0, system.sys.fixed_scan_mode):
        copy(1, slot.Result1)
    with Rung(slot.Cmd != 0, slot.Fired == 0, slot.Scratch >= 3):
        copy(1, slot.Fired)


def _prove_seconds_and_ms(slot: Any) -> None:
    seconds, millis = ClickTimer(21), ClickTimer(22)
    count_scans(slot)
    with Rung(slot.Cmd != 0):
        on_delay(seconds, preset=1, unit="Ts")
        on_delay(millis, preset=1000, unit="Tms")
    latch_count(slot, seconds.Done, slot.Result1)
    latch_count(slot, millis.Done, slot.Result2)
    fire_with_the_scan_setting(slot)


#: Scans the off-delay's rung is true before it falls.
_RUNG_ON = 2


def _prove_off_delay_and_drum(slot: Any) -> None:
    """The off-delay counted from the scan the rung falls, the drum's step from its first."""
    timer = ClickTimer(31)
    count_scans(slot)
    with Rung(slot.Cmd != 0, slot.Scratch <= _RUNG_ON):
        off_delay(timer, preset=1, unit="Ts")
    with Rung(slot.Cmd != 0, slot.Scratch > _RUNG_ON, ~timer.Done, slot.Result1 == 0):
        calc(slot.Scratch - _RUNG_ON, slot.Result1)
    with Rung(slot.Cmd != 0):
        time_drum(
            outputs=[c[3]],
            presets=[1, 32000],
            unit="Ts",
            pattern=[[1], [0]],
            current_step=ds[3901],
            accumulator=td[33],
            completion_flag=c[5],
        ).reset(slot.Cmd == 0)
    latch_count(slot, ds[3901] == 2, slot.Result2)
    fire_with_the_scan_setting(slot)


group = Group(
    "fixed_scan",
    [
        case(
            "What the PLC says about its scan: fixed scan mode, the set time, the time it took",
            ladder=_prove_scan_setup,
            expect={
                "Result1": between(0, 1),
                "Result2": ANY_COUNT,
                "Result3": ANY_COUNT,
                "Result4": ANY_COUNT,
            },
            scans=8,
            probe="which mode made this reading",
        ),
        case(
            "On-delays of 1 s and 1000 ms: the scan each is Done on, and the scan setting",
            ladder=_prove_seconds_and_ms,
            expect=READING,
            scans=110,
            probe="the scan a CLICK counts, for curiosity",
        ),
        case(
            "An off-delay and a time drum's step of 1 s: the scan each ends on, and the scan setting",
            ladder=_prove_off_delay_and_drum,
            keep=[ClickTimer(31).Done, ClickTimer(31).Acc],
            expect=READING,
            scans=110,
            probe="the scan a CLICK counts, for curiosity",
        ),
    ],
)


def test_fixed_scan() -> None:
    assert_all_passed(run(group))


def test_at_the_in_process_fixed_scan_each_count_is_the_ceil() -> None:
    results = {r.case.id: r.actual for r in run(group)}
    assert results["scan_setup"]["Result1"] == 1
    assert results["scan_setup"]["Result2"] == 10
    setting = {"Result3": 10, "Result4": 10, "ErrorCode": 1}
    assert results["seconds_and_ms"] == {"Result1": 100, "Result2": 100, **setting}
    # A drum's step starts on a scan of its own (``tests/core/test_timer_exact.py``).
    assert results["off_delay_and_drum"] == {"Result1": 100, "Result2": 101, **setting}
