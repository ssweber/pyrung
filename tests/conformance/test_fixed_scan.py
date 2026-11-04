"""The scan a timer is Done on, counted: the ``fixed_scan`` group (subroutine
``Conf_FixedScan``, part O, run with CLICK's fixed scan on; ``AGENTS.md``, "Fixed scan parts").

With SC202 on and a scan time set, a CLICK scan is a known time, so a count of scans is a
number to compare, and the program counts its own: ``Scratch`` is the scan in progress from
the scan its rung goes true (1), and each timer's Done latches the count into a Result.

A count means nothing without the scan it was counted under, so every counting case latches
that too, on the scan it raises ``Fired``: ``Result3`` the set time (SD13), ``Result4`` the
longest scan so far (SD12: above the set time, a scan overran in the window), ``ErrorCode``
fixed scan mode (SC202).  ``Result1`` and ``Result2`` are the counts, a pair in two units.

Every expectation is a range holding every reading, because the reading is the answer.
At scan time ``dt`` a timer of delay ``d`` is Done on scan:

- ``ceil(d / dt)``: every scan adds its time, and the rung's first scan is a whole scan of
  time.  What pyrung does at a fixed dt.  1 s at 100 ms is scan 10.
- ``ceil(d / dt) + 1``: the first scan adds no time (as pyrung's generated code, whose
  first scan after boot has no clock behind it), or the timer loses a scan to a slip.  1 s
  at 100 ms is scan 11.
- Anything else, in a run that says fixed scan was on: a clock unlike all of these, which
  goes to the user, and pyrung matches it.

A pair reading differently says the units do not come off one clock.

Off fixed scan (a CLICK without the setting, the local soft PLC, ``TARGET=circuitpy``) a scan
is as long as it takes, and the counts vary with the wall clock: the ranges pass, and each
case's ``ErrorCode`` shows that mode made it.  The soft PLC reports SC202 off and SD13 0 and
scans on its own clock, so it cannot answer this.  Nothing here makes it fixed: in process
``DT`` already is (SC202 on, SD13 10), and that is the run this file pins.
"""

from __future__ import annotations

from typing import Any

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import Rung, calc, copy, off_delay, on_delay, time_drum
from pyrung.core.system_points import system
from tests.conformance.memory import ClickTimer, c, ds, td

#: Any reading a count or a setting can leave in a slot's Int.
ANY_COUNT = range(0, 32768)
#: Two counts, and the scan they were counted under.
READING: dict[str, range] = {
    "Result1": ANY_COUNT,
    "Result2": ANY_COUNT,
    "Result3": ANY_COUNT,
    "Result4": ANY_COUNT,
    "ErrorCode": range(0, 2),
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


def count_two_timers(slot: Any, one: tuple[int, int, str], other: tuple[int, int, str]) -> None:
    """On-delays (number, preset, unit): Result1 and Result2 the scan each is Done on."""
    count_scans(slot)
    with Rung(slot.Cmd != 0):
        for number, preset, unit in (one, other):
            on_delay(ClickTimer(number), preset=preset, unit=unit)
    latch_count(slot, ClickTimer(one[0]).Done, slot.Result1)
    latch_count(slot, ClickTimer(other[0]).Done, slot.Result2)
    fire_with_the_scan_setting(slot)


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


def _prove_one_second(slot: Any) -> None:
    count_two_timers(slot, (21, 1, "Ts"), (22, 1000, "Tms"))


def _prove_three_seconds(slot: Any) -> None:
    count_two_timers(slot, (23, 3, "Ts"), (24, 3000, "Tms"))


#: Scans the off-delay's rung is true before it falls.
_RUNG_ON = 2


def _prove_off_delay(slot: Any) -> None:
    """Counted from the scan the rung falls."""
    seconds, millis = ClickTimer(31), ClickTimer(32)
    count_scans(slot)
    with Rung(slot.Cmd != 0, slot.Scratch <= _RUNG_ON):
        off_delay(seconds, preset=1, unit="Ts")
        off_delay(millis, preset=1000, unit="Tms")
    for timer, result in ((seconds, slot.Result1), (millis, slot.Result2)):
        with Rung(slot.Cmd != 0, slot.Scratch > _RUNG_ON, ~timer.Done, result == 0):
            calc(slot.Scratch - _RUNG_ON, result)
    fire_with_the_scan_setting(slot)


def _time_drum_of_one_step(slot: Any, number: int, preset: int, unit: str, step: Any) -> None:
    with Rung(slot.Cmd != 0):
        time_drum(
            outputs=[c[number]],
            presets=[preset, 32000],
            unit=unit,
            pattern=[[1], [0]],
            current_step=step,
            accumulator=td[number + 30],
            completion_flag=c[number + 2],
        ).reset(slot.Cmd == 0)


def _prove_drum_step(slot: Any) -> None:
    """Counted from the rung's first scan to the scan the first step has ended."""
    count_scans(slot)
    _time_drum_of_one_step(slot, 3, 1, "Ts", ds[3901])
    _time_drum_of_one_step(slot, 6, 1000, "Tms", ds[3902])
    latch_count(slot, ds[3901] == 2, slot.Result1)
    latch_count(slot, ds[3902] == 2, slot.Result2)
    fire_with_the_scan_setting(slot)


group = Group(
    "fixed_scan",
    [
        case(
            "What the PLC says about its scan: fixed scan mode, the set time, the time it took",
            ladder=_prove_scan_setup,
            expect={
                "Result1": range(0, 2),
                "Result2": ANY_COUNT,
                "Result3": ANY_COUNT,
                "Result4": ANY_COUNT,
            },
            scans=8,
            probe="which mode made this recording",
        ),
        case(
            "On-delays of 1 s and 1000 ms: the scan each is Done on, and the scan setting",
            ladder=_prove_one_second,
            expect=READING,
            scans=110,
            fires_within=10.0,
            probe="ceil(delay/dt), or a scan more",
        ),
        case(
            "On-delays of 3 s and 3000 ms: the scan each is Done on, and the scan setting",
            ladder=_prove_three_seconds,
            expect=READING,
            scans=310,
            fires_within=10.0,
            probe="ceil(delay/dt), or a scan more",
        ),
        case(
            "Off-delays of 1 s and 1000 ms: the scan each ends on, and the scan setting",
            ladder=_prove_off_delay,
            keep=[ClickTimer(31).Done, ClickTimer(31).Acc, ClickTimer(32).Done, ClickTimer(32).Acc],
            expect=READING,
            scans=110,
            fires_within=10.0,
            probe="ceil(delay/dt), or a scan more",
        ),
        case(
            "A time drum's step of 1 s and of 1000 ms: the scan each ends on, and the scan setting",
            ladder=_prove_drum_step,
            expect=READING,
            scans=110,
            fires_within=10.0,
            probe="ceil(delay/dt), or a scan more",
        ),
    ],
)


def test_fixed_scan() -> None:
    assert_all_passed(run(group))


#: What the in-process fixed scan says with each count.
IN_PROCESS_SETTING = {"Result3": 10, "Result4": 10, "ErrorCode": 1}


def test_at_the_in_process_fixed_scan_each_count_is_the_ceil() -> None:
    """What pyrung reads, and so that a count can tell ``ceil`` from a scan more."""
    results = {r.case.id: r.actual for r in run(group)}
    assert results["scan_setup"]["Result1"] == 1
    assert results["scan_setup"]["Result2"] == 10
    for name, count in (
        ("one_second", 100),
        ("three_seconds", 300),
        ("off_delay", 100),
        ("drum_step", 100),
    ):
        assert results[name] == {"Result1": count, "Result2": count, **IN_PROCESS_SETTING}
