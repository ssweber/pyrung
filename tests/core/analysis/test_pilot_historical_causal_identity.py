"""Recorded causal identity survives later PILOT overlay changes."""

from __future__ import annotations

from datetime import datetime

import pytest
from pyrsistent import pmap, pvector

from pyrung import PLC, Bool, Program, latch, rung, system
from pyrung.core.analysis.causal import CausalChain, EnablingCondition, Transition
from pyrung.core.analysis.pilot.overlay import PilotRung, _set_pilot_rungs
from pyrung.core.analysis.pilot.types import _PilotState, _World
from pyrung.core.condition import AnyCondition
from pyrung.core.context import RungId


def _startup_trip_program() -> tuple[Program, Bool, Bool, Bool]:
    enable = Bool("EnableCommand", external=True, default=True)
    guard = Bool("StartupGuard", external=True)
    trip = Bool("TripLatched")
    with Program() as program:
        with rung(system.sys.first_scan, enable, ~guard):
            latch(trip)
    return program, enable, guard, trip


def _synthetic_epoch_program() -> tuple[Program, Bool, Bool, Bool, Bool, Bool, Bool]:
    parent_guard = Bool("ParentGuard", external=True, default=True)
    child_guard = Bool("ChildGuard", external=True, default=True)
    continue_ = Bool("Continue", external=True)
    held = Bool("Held")
    parent_seen = Bool("ParentSeen")
    later = Bool("Later")
    inventory = Bool("GuardInventory")
    with Program() as program:
        with rung(held):
            latch(parent_seen)
        with rung(parent_seen, continue_):
            latch(later)
        # Keep both epoch guards in the program's authoritative tag inventory.
        with rung(parent_guard, child_guard):
            latch(inventory)
    return program, parent_guard, child_guard, continue_, held, parent_seen, later


def _branching_history_program() -> tuple[Program, Bool, Bool]:
    command = Bool("BranchCommand", external=True)
    seen = Bool("BranchSeen")
    with Program() as program:
        with rung(command):
            latch(seen)
    return program, command, seen


def _identity(chain: CausalChain) -> tuple[object, ...]:
    return (
        chain.effect,
        tuple(
            (
                step.transition,
                step.rung_index,
                step.subroutine,
                step.triggers,
                step.enablers,
            )
            for step in chain.steps
        ),
        tuple(chain.roots),
    )


def _pilot_state(work: PLC) -> _PilotState:
    return _PilotState(
        world=_World(
            work=work,
            committed_acts=pvector(),
            best_trend=None,
            pilot_rungs=pvector(),
            dwell_scans=0,
        ),
        key_config=None,
        seen_keys=set(),
        checkpoints=[],
        watch_tags=[],
    )


def test_recorded_startup_cause_survives_later_pilot_overlay() -> None:
    program, enable, guard, trip = _startup_trip_program()
    plc = PLC(program)
    plc.step()
    trip_scan = plc.state.scan_id

    original = plc.cause(trip, scan=trip_scan, deep=True)
    assert original is not None
    assert original.effect == Transition(trip.name, trip_scan, False, True, 17)
    assert original.steps[0].triggers == (
        Transition(system.sys.first_scan.name, trip_scan, None, True),
    )
    assert original.steps[0].enablers == (
        EnablingCondition(enable.name, True, None),
        EnablingCondition(guard.name, False, None),
    )
    assert tuple(
        (root.tag_name, root.value, root.kind, root.scan_id, root.held_since_scan)
        for root in original.roots
    ) == (
        (system.sys.first_scan.name, True, "system", trip_scan, trip_scan),
        (enable.name, True, "external", trip_scan, None),
        (guard.name, False, "external", trip_scan, None),
    )

    original_identity = _identity(original)
    state = _pilot_state(plc)
    executed = state.work
    state.pilot_rungs = (
        PilotRung(
            guard.name,
            True,
            AnyCondition(system.sys.first_scan, trip),
        ),
    )
    assert state.work is not executed
    assert state.work._causal_parent is not executed
    assert state.work._causal_parent._is_frozen_causal_owner
    assert state.work.state == executed.state

    child = state.work
    child.step()
    assert child.state.tags[guard.name] is True
    child_state = child.state

    state.pilot_rungs = (PilotRung(enable.name, False, trip),)
    assert state.work is not child
    assert state.work._causal_parent is not child
    assert state.work._causal_parent._is_frozen_causal_owner
    assert state.work.state == child.state
    state.work.step()
    assert state.work.state.tags[enable.name] is False
    assert tuple(state.work.history.scan_ids()) == (0, 1, 2, 3)
    assert state.work.history.at(1) == executed.state
    assert state.work.history.at(2) == child_state
    assert state.work.history.at(3) == state.work.state
    # Authority cannot depend on a derived replay capture remaining warm. The
    # retained prefix belongs to the runner that executed it; successive overlay
    # forks must delegate old causal reads through every boundary after eviction.
    executed._clear_retained_debug_trace_caches()
    child._clear_retained_debug_trace_caches()

    recalled = state.work.cause(trip, scan=trip_scan, deep=True)
    assert recalled is not None
    assert recalled.effect.occurrence_ordinal == original.effect.occurrence_ordinal
    assert _identity(recalled) == original_identity


def test_deep_cause_resolves_synthetic_rung_in_its_executing_overlay_epoch() -> None:
    program, parent_guard, child_guard, continue_, held, parent_seen, later = (
        _synthetic_epoch_program()
    )
    state = _pilot_state(PLC(program))
    state.pilot_rungs = (PilotRung(held.name, True, parent_guard),)
    parent = state.work
    parent.step()
    establishing_scan = parent.state.scan_id
    assert parent.state.tags[held.name] is True
    assert parent.state.tags[parent_seen.name] is True
    assert parent.state.tags[later.name] is False

    establishing_cause = parent.cause(parent_seen, scan=establishing_scan, deep=True)
    assert establishing_cause is not None
    establishing_step = next(
        step for step in establishing_cause.steps if step.transition.tag_name == held.name
    )
    assert establishing_step.transition == Transition(
        held.name,
        establishing_scan,
        False,
        True,
        16,
    )
    assert establishing_step.subroutine == "PILOT"
    assert establishing_step.rung_index == 0
    parent_synthetic_rung = parent._synthesis.holds[0]

    # Replace the parent hold with the opposite value. Its synthetic identity is
    # also PILOT:0, but it is not the rung that established ``Held`` in scan 1.
    state.pilot_rungs = (PilotRung(held.name, False, child_guard),)
    child = state.work
    assert child._causal_parent is not parent
    assert child._causal_parent._is_frozen_causal_owner
    child_synthetic_rung = child._synthesis.holds[0]
    assert child_synthetic_rung is not parent_synthetic_rung
    child.patch({continue_.name: True})
    child.step()
    assert child.state.tags[held.name] is False
    assert child.state.tags[later.name] is True

    parent._clear_retained_debug_trace_caches()
    cause = child.cause(later, scan=child.state.scan_id, deep=True)
    assert cause is not None
    inherited_step = next(step for step in cause.steps if step.transition.tag_name == held.name)
    assert (
        inherited_step.transition,
        inherited_step.rung_index,
        inherited_step.subroutine,
        inherited_step.triggers,
        inherited_step.enablers,
    ) == (
        establishing_step.transition,
        establishing_step.rung_index,
        establishing_step.subroutine,
        establishing_step.triggers,
        establishing_step.enablers,
    )
    assert child._resolve_node_rung(RungId("PILOT", 0), establishing_scan) is parent_synthetic_rung
    assert (
        child._resolve_node_rung(RungId("PILOT", 0), establishing_scan) is not child_synthetic_rung
    )


def test_raw_overlay_install_refuses_to_reinterpret_executed_scans() -> None:
    program, _enable, guard, trip = _startup_trip_program()
    plc = PLC(program)
    plc.step()
    executed_state = plc.state
    executed_synthesis = plc._synthesis

    with pytest.raises(
        RuntimeError,
        match="cannot change PILOT rungs after this runner has executed",
    ):
        _set_pilot_rungs(
            plc,
            (PilotRung(guard.name, True, AnyCondition(system.sys.first_scan, trip)),),
        )

    assert plc.state is executed_state
    assert plc._synthesis is executed_synthesis


def test_inherited_history_is_clamped_at_a_historical_fork_boundary() -> None:
    program, command, seen = _branching_history_program()
    parent = PLC(program)
    parent.step()
    boundary = parent.state
    child = parent.fork(scan_id=boundary.scan_id)

    parent.patch({command.name: True})
    parent.step()
    parent_future = parent.state
    assert parent_future.tags[seen.name] is True

    assert child.history.oldest_scan_id == 0
    assert child.history.newest_scan_id == boundary.scan_id
    assert child.history.at(boundary.scan_id) == boundary
    assert not child.history.contains(parent_future.scan_id)
    with pytest.raises(KeyError):
        child.history.at(parent_future.scan_id)

    child.step()
    assert child.state.scan_id == parent_future.scan_id
    assert child.state.tags[seen.name] is False
    assert child.history.at(child.state.scan_id) == child.state
    assert child.history.at(child.state.scan_id) != parent_future

    # A second boundary must also exclude the immediate parent's later firing
    # timeline, not merely its states. Both parent branches latch ``seen`` after
    # their child was forked; neither transition belongs to the grandchild.
    grandchild = child.fork()
    child.patch({command.name: True})
    child.step()
    assert child.state.tags[seen.name] is True

    grandchild.step()
    assert grandchild.state.tags[seen.name] is False
    assert grandchild.history.previous_transition(seen) is None
    assert grandchild.cause(seen, scan=grandchild.state.scan_id, deep=True) is None


def test_fork_without_log_inheritance_keeps_history_local() -> None:
    program, _command, _seen = _branching_history_program()
    parent = PLC(program)
    parent.step()
    parent.step()

    local = parent.fork(inherit_log=False)
    boundary_scan = local.state.scan_id
    assert local._causal_parent is None
    assert local.history.oldest_scan_id == boundary_scan
    assert local.history.newest_scan_id == boundary_scan
    assert local.history.contains(boundary_scan)
    assert not local.history.contains(boundary_scan - 1)
    with pytest.raises(KeyError):
        local.history.at(boundary_scan - 1)

    local.step()
    assert tuple(local.history.scan_ids()) == (boundary_scan, boundary_scan + 1)


@pytest.mark.parametrize("mutation", ["reboot", "trim"])
def test_child_causal_history_is_independent_of_later_parent_mutation(mutation: str) -> None:
    program, command, seen = _branching_history_program()
    parent = PLC(program)
    parent.patch({command.name: True})
    parent.step()
    parent.step()
    child = parent.fork()
    expected_states = tuple(child.history.range(0, child.state.scan_id + 1))
    expected_cause = child.cause(seen, scan=1, deep=True)
    assert expected_cause is not None

    if mutation == "reboot":
        parent.reboot()
    else:
        parent._trim_history_before(parent.state.scan_id)

    assert tuple(child.history.range(0, child.state.scan_id + 1)) == expected_states
    assert child.cause(seen, scan=1, deep=True) == expected_cause


def test_fork_from_inherited_scan_discards_descendant_suffix() -> None:
    program, command, seen = _branching_history_program()
    root = PLC(program)
    root.patch({command.name: True})
    root.step()
    root.patch({command.name: False})
    root.step()
    child = root.fork()
    child.step()

    historical = child.fork(scan_id=1)

    assert tuple(historical.history.scan_ids()) == (0, 1)
    assert historical.state == root.history.at(1)
    cause = historical.cause(seen, scan=1, deep=True)
    assert cause is not None
    assert cause.effect.scan_id == 1


def test_composite_observed_writers_checks_both_owned_alternating_parities() -> None:
    parent = PLC(logic=[])
    parent.run(3)
    timelines = parent._rung_firing_timelines
    timelines.append(7, 1, pmap({"EvenWrite": True}))
    timelines.append(7, 2, pmap({"OddWrite": True}))
    timelines.append(7, 3, pmap({"EvenWrite": True}))

    both = parent.fork(scan_id=3)
    assert both._causal_rung_firing_timelines.observed_writers_of("EvenWrite") == {7}
    assert both._causal_rung_firing_timelines.observed_writers_of("OddWrite") == {7}

    # The stored range includes both patterns, but this historical branch owns
    # only its first parity. The future odd write must not leak through payload.
    clipped = parent.fork(scan_id=1)
    assert clipped._causal_rung_firing_timelines.observed_writers_of("EvenWrite") == {7}
    assert clipped._causal_rung_firing_timelines.observed_writers_of("OddWrite") == set()


def test_historical_fork_uses_executing_epochs_rtc_base() -> None:
    root = PLC(logic=[])
    root.set_rtc(datetime(2024, 1, 2, 3, 4, 5))
    root.step()
    expected = root._system_runtime._rtc_now(root.state)
    child = root.fork()
    child.set_rtc(datetime(2035, 6, 7, 8, 9, 10))
    child.step()

    historical = child.fork(scan_id=1)

    assert historical._system_runtime._rtc_now(historical.state) == expected


def test_child_trim_and_reboot_reset_only_its_retained_floor() -> None:
    parent = PLC(logic=[])
    parent.run(2)
    child = parent.fork()
    child.run(2)

    child._trim_history_before(3)
    assert child.history.oldest_scan_id == 3
    assert tuple(child.history.scan_ids()) == (3, 4)
    assert child._causal_parent is None

    child.reboot()
    assert child.history.oldest_scan_id == 0
    assert tuple(child.history.scan_ids()) == (0,)
    assert child._causal_parent is None


def test_repeated_boundary_forks_do_not_create_executed_empty_epochs() -> None:
    root = PLC(logic=[])
    root.step()
    child = root.fork().fork().fork()

    assert [(first, last) for _owner, first, last in child._causal_epoch_intervals()] == [
        (0, 1)
    ]

    child.step()
    assert [(first, last) for _owner, first, last in child._causal_epoch_intervals()] == [
        (0, 1),
        (2, 2),
    ]
