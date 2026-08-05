"""Historical cold blockers are not executable retained action prefixes."""

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


def test_unselected_seed_departure_does_not_start_independent_blocker_search() -> None:
    """A missing bootstrap designation is not retroactively made a promise."""

    program, _enable, _guard, _start, _trip, target = _delayed_startup_program()

    plan = pilot_how(PLC(program), target, max_scans=40)

    assert not plan.reachable
    assert not plan.ordered_steps
    assert plan.reason is not None
    assert "No productive next action" in plan.reason


def test_unselected_cold_faults_do_not_compose_as_retained_rebases() -> None:
    """Fresh Orientation never executes a synthesized historical suffix."""

    program, _guard_a, _guard_b, _start, _fault_a, _fault_b, target = _composed_startup_program()
    plan = pilot_how(PLC(program), target, max_scans=60)

    assert not plan.reachable
    assert not plan.ordered_steps
    assert plan.reason is not None
    assert "No productive next action" in plan.reason


def test_post_startup_invocation_does_not_rewind_to_earliest_retained_floor() -> None:
    """Only a matched expectation receipt authorizes causal checkpoint restore."""

    program, _enable, _guard, _start, trip, target = _delayed_startup_program()
    plc = PLC(program)
    for _ in range(6):
        plc.step()
    assert plc.state.tags[trip.name] is True

    plan = pilot_how(plc, target, max_scans=40)

    assert not plan.reachable
    assert not plan.ordered_steps
    assert plc.state.tags[trip.name] is True


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
