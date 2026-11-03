"""A commit publishes what was written, exactly, and an equal value is still no change.

``False`` over ``0`` and ``-0.0`` over ``0.0`` replace the object the state holds, so a
state never depends on when it was last published.  Nothing that asks whether a tag
changed may see them: they compare equal, and these tests say so one reader at a time.
"""

from __future__ import annotations

import math
from typing import Any

from pyrung import PLC, Bool, Int, Program, Real, Rung, calc, fall, latch, out, rise
from pyrung.click import ClickBlocks, ClickDataProvider, TagMap
from pyrung.core import compiled_run, scan_trace

Run = Bool("LwRun", external=True)
Coiled = Int("LwCoiled")
Bounded = Int("LwBounded", min=0, max=5)
Gained = Real("LwGained")
Source = Real("LwSource", external=True)
Rose, Fell = Bool("LwRose"), Bool("LwFell")


def _program() -> Program:
    with Program() as program:
        with Rung(Run):
            out(Coiled)
        with Rung(Run):
            out(Bounded)
        with Rung():
            calc(Source * -1.0, Gained)
        with Rung(rise(Coiled)):
            latch(Rose)
        with Rung(fall(Coiled)):
            latch(Fell)
    return program


def _is_negative_zero(value: Any) -> bool:
    return value == 0.0 and math.copysign(1.0, value) == -1.0


def _stepped(scans: int = 5) -> PLC:
    plc = PLC(_program(), dt=0.01)
    for _ in range(scans):
        plc.step()
    return plc


def test_the_write_is_published_from_the_first_scan() -> None:
    plc = _stepped(1)
    assert plc.state.tags[Coiled.name] is False
    assert plc.state.tags[Bounded.name] is False
    assert _is_negative_zero(plc.state.tags[Gained.name])


def test_a_monitor_does_not_fire() -> None:
    plc = PLC(_program(), dt=0.01)
    fired: list[tuple[Any, Any]] = []
    plc.monitor(Coiled, lambda now, before: fired.append((now, before)))
    plc.monitor(Gained, lambda now, before: fired.append((now, before)))
    for _ in range(5):
        plc.step()
    assert plc.state.tags[Coiled.name] is False
    assert fired == []


def test_a_breakpoint_does_not_fire() -> None:
    plc = PLC(_program(), dt=0.01)
    plc.when(Coiled != 0).pause()
    plc.when(Gained != 0.0).pause()
    plc.run(5)
    assert plc.state.scan_id == 5


def test_an_edge_does_not_fire() -> None:
    plc = _stepped()
    assert plc.state.tags[Rose.name] is False
    assert plc.state.tags[Fell.name] is False


def test_a_diff_sees_no_change() -> None:
    plc = _stepped()
    assert plc.diff(0, 5).keys().isdisjoint({Coiled.name, Bounded.name, Gained.name})


def test_the_causal_records_see_no_transition() -> None:
    plc = _stepped()
    for tag in (Coiled, Gained):
        assert tag.name not in plc._committed_tag_timelines._fired_ranges
        assert plc.cause(tag) is None


def test_the_bounds_stay_quiet() -> None:
    plc = _stepped()
    assert plc.state.tags[Bounded.name] is False
    assert not plc.bounds_violations


def test_a_real_transition_still_records_and_fires() -> None:
    plc = PLC(_program(), dt=0.01)
    fired: list[tuple[Any, Any]] = []
    plc.monitor(Coiled, lambda now, before: fired.append((now, before)))
    plc.step()
    plc.patch({Run.name: True})
    plc.step()
    plc.patch({Run.name: False})
    plc.step()
    assert fired == [(True, False), (False, True)]
    assert plc.state.tags[Rose.name] is True
    assert plc.state.tags[Fell.name] is True
    assert Coiled.name in plc._committed_tag_timelines._fired_ranges
    assert plc.cause(Coiled) is not None


def test_modbus_serves_the_bits_written() -> None:
    """The first ``-0.0`` serves ``0x80000000``, as it did after any nonzero value."""
    from pyclickplc.banks import DataType
    from pyclickplc.server import pack_value

    blocks = ClickBlocks()
    plc = _stepped(1)
    provider = ClickDataProvider(plc, TagMap({Gained: blocks.df[1], Coiled: blocks.ds[1]}))
    assert pack_value(provider.read("DF1"), DataType.FLOAT) == [0, 0x8000]
    assert pack_value(provider.read("DS1"), DataType.INT) == [0]


def test_a_kernel_run_publishes_what_stepping_does(monkeypatch: Any) -> None:
    monkeypatch.setattr(compiled_run, "INTERPRETED_PREFIX", 1)
    monkeypatch.setattr(scan_trace, "MIN_RUN_SCANS", 1)
    before = scan_trace.TOTALS["loop scans"] + scan_trace.TOTALS["real scans"]
    ran = PLC(_program(), dt=0.01)
    ran.run_for(0.5)
    stepped = _stepped(ran.state.scan_id)
    assert scan_trace.TOTALS["loop scans"] + scan_trace.TOTALS["real scans"] > before
    for tag in (Coiled, Bounded):
        assert ran.state.tags[tag.name] is stepped.state.tags[tag.name] is False
    assert _is_negative_zero(ran.state.tags[Gained.name])
    assert _is_negative_zero(stepped.state.tags[Gained.name])
