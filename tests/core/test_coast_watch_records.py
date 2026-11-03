"""A coast under a watch leaves the records stepping leaves, on every scan the loop ran.

A watched tag's loop leaves before the tag changes, so the scans it runs hold the tag.  A path
traced on a scan that moved the tag must not run them: its records would say the tag moved on
each, with values it never had.  Two routes make a watch: a predicate that does not declare its
reads, and a monitor beside a predicate that does.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

import pytest

from pyrung import Bool, Int, Program, Rung, Timer, calc, copy, on_delay, out, system
from pyrung.core import PLC
from pyrung.core.coast_run import coast_until
from tests.fold_check import compare_records


def _timer_tick() -> Program:
    """A tag that moves once in every hundred scans, from a timer that moves on each."""
    Tmr = Timer.clone("WrTmr")
    Q = Int("WrQ")
    with Program(strict=False) as prog:
        with Rung():
            on_delay(Tmr, 30000, "ms")
        with Rung():
            calc(Tmr.Acc // 1000, Q)
        with Rung(Q == 7):
            out(Bool("WrHit"))
    return prog


def _pulse() -> Program:
    """A Bool that flips for one scan in every ten, from a timer that resets itself."""
    Tmr = Timer.clone("WrPulseTmr")
    with Program(strict=False) as prog:
        with Rung(~Tmr.Done):
            on_delay(Tmr, 100, "ms")
    return prog


def _clock_mirror(contact: bool) -> Program:
    """A copy of the clock's second, which *contact* also reads through ``clock_1s``."""
    T = Bool("WrT")
    Sec = Int("WrSec")
    Tmr = Timer.clone("WrClockTmr")
    with Program(strict=False) as prog:
        with Rung():
            copy(system.rtc.second, Sec)
        with Rung(~T):
            out(T)
        if contact:
            with Rung(system.sys.clock_1s):
                out(Bool("WrBlink"))
        with Rung():
            on_delay(Tmr, 20, "s")
    return prog


# name -> (program, the watched tag, the value that ends the coast)
_PROGRAMS: dict[str, tuple[Callable[[], Program], str, Any]] = {
    "timer_tick": (_timer_tick, "WrQ", 7),
    "pulse": (_pulse, "WrPulseTmr_Done", True),
    "clock_contact": (lambda: _clock_mirror(True), "WrSec", 55),
    "clock_no_contact": (lambda: _clock_mirror(False), "WrSec", 55),
}


def _plc(build: Callable[[], Program]) -> PLC:
    plc = PLC(build(), dt=0.010)
    plc.set_rtc(datetime(2026, 3, 1, 12, 59, 50, 250_000))
    plc.step()
    return plc


def _read(plc: PLC, tag: str, scan: int) -> tuple[Any, ...]:
    return (
        {rung: dict(writes) for rung, writes in plc.rung_firings(scan).items()},
        plc.history.previous_transition(tag, at_or_before=scan),
        str(plc.cause(tag, scan)),
    )


def _assert_records_equal(plc: PLC, ref: PLC, tag: str) -> None:
    assert plc.state.scan_id == ref.state.scan_id
    assert plc.state.tags == ref.state.tags
    compare_records("watch", plc, ref, 1)
    for scan in range(2, plc.state.scan_id + 1):
        assert _read(plc, tag, scan) == _read(ref, tag, scan), scan


@pytest.mark.parametrize("name", sorted(_PROGRAMS))
def test_a_predicate_that_does_not_declare_its_reads(name: str) -> None:
    build, tag, value = _PROGRAMS[name]

    def stop(state: Any) -> bool:
        return state.tags.get(tag) == value

    ref = _plc(build)
    for _ in range(1_500):
        ref.step()
        if stop(ref.state):
            break
    assert stop(ref.state)
    plc = _plc(build)
    stats: dict[str, int] = {}
    coast_until(plc, stop, budget=1_500, predicate_reads=None, stats=stats)
    assert stats["stretched_scans"] > 0
    _assert_records_equal(plc, ref, tag)


@pytest.mark.parametrize("name", sorted(_PROGRAMS))
def test_a_monitor(name: str) -> None:
    build, tag, _value = _PROGRAMS[name]
    ref = _plc(build)
    stepped: list[tuple[Any, Any]] = []
    ref.monitor(tag, lambda now, before: stepped.append((now, before)))
    for _ in range(800):
        ref.step()
    plc = _plc(build)
    seen: list[tuple[Any, Any]] = []
    plc.monitor(tag, lambda now, before: seen.append((now, before)))
    stats: dict[str, int] = {}
    coast_until(plc, lambda _s: False, budget=800, predicate_reads=frozenset(), stats=stats)
    assert stats["stretched_scans"] > 0
    assert seen == stepped
    _assert_records_equal(plc, ref, tag)
