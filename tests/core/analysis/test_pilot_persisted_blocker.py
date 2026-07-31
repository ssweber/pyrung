"""Observed retained departures enter ordinary PILOT recovery."""

from __future__ import annotations

from pyrung import PLC, Bool, Program, latch, rung, system
from pyrung.core.analysis.pilot import pilot_how


def _delayed_startup_program() -> tuple[Program, Bool, Bool, Bool, Bool, Bool]:
    """A cheap startup cut loses the only arm; an independent guard preserves it."""

    enable = Bool("EnableCommand", external=True, default=True)
    guard = Bool("StartupGuard", external=True)
    start = Bool("StartCommand", external=True)
    trip = Bool("TripLatched")
    armed = Bool("ReadyLatched")
    target = Bool("TargetReached")
    with Program() as program:
        with rung(system.sys.first_scan, enable, ~guard):
            latch(trip)
        with rung(system.sys.first_scan, enable, ~trip):
            latch(armed)
        with rung(armed, start):
            latch(target)
    return program, enable, guard, start, trip, target


def _composed_startup_program() -> tuple[Program, Bool, Bool, Bool, Bool, Bool, Bool]:
    """Two independent cold faults require two successive floor replays."""

    guard_a = Bool("StartupGuardA", external=True)
    guard_b = Bool("StartupGuardB", external=True)
    start = Bool("ComposedStart", external=True)
    fault_a = Bool("StartupFaultA")
    fault_b = Bool("StartupFaultB")
    target = Bool("ComposedTarget")
    with Program() as program:
        with rung(system.sys.first_scan, ~guard_a):
            latch(fault_a)
        with rung(system.sys.first_scan, ~guard_b):
            latch(fault_b)
        with rung(start, ~fault_a, ~fault_b):
            latch(target)
    return program, guard_a, guard_b, start, fault_a, fault_b, target


def test_observed_seed_departure_replays_full_ordinary_drive() -> None:
    program, enable, guard, start, trip, target = _delayed_startup_program()

    plan = pilot_how(PLC(program), target, max_scans=40)

    assert plan.reachable, plan.reason
    assert plan.tags[target.name] is True
    assert plan.tags[trip.name] is False
    assert plan.tags[enable.name] is True
    assert plan.tags[guard.name] is True
    assert plan.tags[start.name] is True
    replay = plan.replay()
    assert replay.state.tags[target.name] is True
    assert replay.state.tags[trip.name] is False


def test_observed_departures_compose_through_successive_outer_loop_rebases() -> None:
    program, guard_a, guard_b, start, fault_a, fault_b, target = (
        _composed_startup_program()
    )
    plan = pilot_how(PLC(program), target, max_scans=60)

    assert plan.reachable, plan.reason
    assert plan.tags[fault_a.name] is False
    assert plan.tags[fault_b.name] is False
    assert plan.tags[guard_a.name] is True
    assert plan.tags[guard_b.name] is True
    assert plan.tags[start.name] is True
    assert plan.replay().state.tags[target.name] is True


def test_post_startup_invocation_uses_earliest_retained_floor() -> None:
    program, _enable, guard, start, trip, target = _delayed_startup_program()
    plc = PLC(program)
    for _ in range(6):
        plc.step()
    assert plc.state.tags[trip.name] is True

    plan = pilot_how(plc, target, max_scans=40)

    assert plan.reachable, plan.reason
    assert plan.tags[target.name] is True
    assert plan.tags[guard.name] is True
    assert plan.tags[start.name] is True
    assert plan.replay().state.tags[target.name] is True


def test_trimmed_predecessor_does_not_invent_initialization() -> None:
    program, _enable, _guard, _start, trip, target = _delayed_startup_program()
    source = PLC(program)
    for _ in range(6):
        source.step()
    plc = source.fork(source.state.scan_id, inherit_log=False)
    assert plc.history.oldest_scan_id > 0
    assert plc.state.tags[trip.name] is True

    plan = pilot_how(plc, target, max_scans=20)

    assert not plan.reachable
