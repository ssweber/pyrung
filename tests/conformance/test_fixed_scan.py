"""The scan a timer is Done on, counted: the ``fixed_scan`` group (subroutine
``Conf_FixedScan``, part O, run with CLICK's fixed scan on; ``AGENTS.md``, "Fixed scan parts").

With SC202 on and a scan time set, a CLICK scan is a known time, so a count of scans is a
number to compare, and the program counts its own: ``Scratch`` is the scan in progress from
the scan its rung goes true (1), and each timer's Done latches the count into a Result.

Every expectation is a range holding every reading, because the reading is the answer.
At scan time ``dt`` a timer of delay ``d`` is Done on scan:

- ``ceil(d / dt)``: every scan adds its time, and the rung's first scan is a whole scan of
  time.  What pyrung does at a fixed dt.  1 s at 100 ms is scan 10.
- ``ceil(d / dt) + 1``: the first scan adds no time (as pyrung's generated code, whose
  first scan after boot has no clock behind it), or the timer loses a scan to a slip.  1 s
  at 100 ms is scan 11.
- Anything else, in a run that says fixed scan was on: a clock unlike all of these, which
  goes to the user, and pyrung matches it.

A pair in two units reading differently says the units do not come off one clock.

Off fixed scan (a CLICK without the setting, the local soft PLC, ``TARGET=circuitpy``) a scan
is as long as it takes, and the counts vary with the wall clock: the ranges pass, and
``scan_setup`` shows which mode a recording came from.  The soft PLC reports SC202 off and
SD13 0 and scans on its own clock, so it cannot answer this.  Nothing here makes it fixed:
in process ``DT`` already is (SC202 on, SD13 10), and that is the run this file pins.
"""

from __future__ import annotations

from typing import Any

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import Rung, calc, copy, off_delay, on_delay, time_drum
from pyrung.core.system_points import system
from tests.conformance.memory import ClickTimer, c, ds, td

#: Any reading a count or a setting can leave in a slot's Int.
ANY_COUNT = range(0, 32768)


def count_scans(slot: Any) -> None:
    """``Scratch`` = the scan in progress, holding at the top of its Int: the first rung."""
    with Rung(slot.Cmd != 0, slot.Scratch < 32767):
        calc(slot.Scratch + 1, slot.Scratch)


def latch_count(slot: Any, done: Any, into: Any) -> None:
    """*into* = the scan *done* first holds on."""
    with Rung(slot.Cmd != 0, done, into == 0):
        copy(slot.Scratch, into)


def count_timers(slot: Any, timers: list[tuple[int, int, str]], results: list[Any]) -> None:
    """Each on-delay of *timers* (number, preset, unit) latches its Done scan into its Result."""
    count_scans(slot)
    with Rung(slot.Cmd != 0):
        for number, preset, unit in timers:
            on_delay(ClickTimer(number), preset=preset, unit=unit)
    for (number, _preset, _unit), result in zip(timers, results, strict=True):
        latch_count(slot, ClickTimer(number).Done, result)
    fire_when_all_read(slot, results)


def fire_when_all_read(slot: Any, results: list[Any]) -> None:
    """``Fired`` once every Result has its count: nothing moves after it."""
    with Rung(slot.Cmd != 0, *[result != 0 for result in results]):
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
    count_timers(
        slot,
        [(21, 1, "Ts"), (22, 3, "Ts"), (23, 1000, "Tms"), (24, 3000, "Tms")],
        [slot.Result1, slot.Result2, slot.Result3, slot.Result4],
    )


#: Scans the off-delay's rung is true before it falls.
_RUNG_ON = 2


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


def _prove_off_delay_and_drum(slot: Any) -> None:
    """Off-delays: counted from the scan the rung falls.  Drum steps: from the rung's first."""
    seconds, millis = ClickTimer(31), ClickTimer(32)
    count_scans(slot)
    with Rung(slot.Cmd != 0, slot.Scratch <= _RUNG_ON):
        off_delay(seconds, preset=1, unit="Ts")
        off_delay(millis, preset=1000, unit="Tms")
    for timer, result in ((seconds, slot.Result1), (millis, slot.Result2)):
        with Rung(slot.Cmd != 0, slot.Scratch > _RUNG_ON, ~timer.Done, result == 0):
            calc(slot.Scratch - _RUNG_ON, result)
    _time_drum_of_one_step(slot, 3, 1, "Ts", ds[3901])
    _time_drum_of_one_step(slot, 6, 1000, "Tms", ds[3902])
    latch_count(slot, ds[3901] == 2, slot.Result3)
    latch_count(slot, ds[3902] == 2, slot.Result4)
    fire_when_all_read(slot, [slot.Result1, slot.Result2, slot.Result3, slot.Result4])


def _init_drum_steps(slot: Any) -> None:
    copy(0, ds[3901])
    copy(0, ds[3902])


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
            "On-delays of 1 s, 3 s, 1000 ms and 3000 ms: the scan each is Done on",
            ladder=_prove_seconds_and_ms,
            expect={
                "Result1": ANY_COUNT,
                "Result2": ANY_COUNT,
                "Result3": ANY_COUNT,
                "Result4": ANY_COUNT,
            },
            scans=310,
            fires_within=10.0,
            probe="ceil(delay/dt), or a scan more",
        ),
        case(
            "Off-delays and a time drum's step of 1 s and 1000 ms: the scan each ends on",
            ladder=_prove_off_delay_and_drum,
            init=_init_drum_steps,
            keep=[ClickTimer(31).Done, ClickTimer(31).Acc, ClickTimer(32).Done, ClickTimer(32).Acc],
            expect={
                "Result1": ANY_COUNT,
                "Result2": ANY_COUNT,
                "Result3": ANY_COUNT,
                "Result4": ANY_COUNT,
            },
            scans=110,
            fires_within=10.0,
            probe="ceil(delay/dt), or a scan more",
        ),
    ],
)


def test_fixed_scan() -> None:
    assert_all_passed(run(group))


def test_at_the_in_process_fixed_scan_each_count_is_the_ceil() -> None:
    """What pyrung reads, and so that a count can tell ``ceil`` from a scan more."""
    results = {r.case.id: r.actual for r in run(group)}
    assert results["scan_setup"]["Result1"] == 1
    assert results["scan_setup"]["Result2"] == 10
    assert results["seconds_and_ms"] == {
        "Result1": 100,
        "Result2": 300,
        "Result3": 100,
        "Result4": 300,
    }
    assert results["off_delay_and_drum"] == {
        "Result1": 100,
        "Result2": 100,
        "Result3": 100,
        "Result4": 100,
    }
