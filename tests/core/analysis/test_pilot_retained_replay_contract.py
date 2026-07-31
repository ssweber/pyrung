"""Internal contracts for retained-occurrence recovery.

The user-facing behavior remains specified in ``test_pilot_persisted_blocker``.
These tests pin the ordinary-Bearing seam and replay bookkeeping separately.
"""

from __future__ import annotations

from dataclasses import replace

from pyrung import (
    PLC,
    Bool,
    Int,
    Program,
    call,
    copy,
    latch,
    reset,
    rung,
    subroutine,
    system,
)
from pyrung.core.analysis.pdg import _extract_reads_from_condition
from pyrung.core.analysis.pilot import pilot_how
from pyrung.core.analysis.pilot.compass import Compass
from pyrung.core.analysis.pilot.navigation_contracts import (
    ActPolicy,
    ActSource,
    Bearing,
    Pulse,
    RetainedOccurrence,
    RetainedReplay,
    act_identity,
)
from pyrung.core.analysis.pilot.overlay import PilotRung
from pyrung.core.analysis.pilot.retained import (
    _occurrence_repeated,
    _scan_projection,
    _write_address,
    _writer_occurrence,
    replay_retained_prefix,
)
from pyrung.core.condition import AllCondition, AnyCondition


def _single_departure_program() -> tuple[Program, Bool, Bool, Bool, Bool, Bool]:
    enable = Bool("ContractEnable", external=True, default=True)
    guard = Bool("ContractGuard", external=True)
    start = Bool("ContractStart", external=True)
    trip = Bool("ContractTrip")
    ready = Bool("ContractReady")
    target = Bool("ContractTarget")
    with Program() as program:
        with rung(system.sys.first_scan, enable, ~guard):
            latch(trip)
        with rung(system.sys.first_scan, enable, ~trip):
            latch(ready)
        with rung(ready, start):
            latch(target)
    return program, enable, guard, start, trip, target


def _composed_departure_program() -> tuple[Program, Bool, Bool, Bool, Bool, Bool, Bool]:
    guard_a = Bool("ContractGuardA", external=True)
    guard_b = Bool("ContractGuardB", external=True)
    start = Bool("ContractComposedStart", external=True)
    fault_a = Bool("ContractFaultA")
    fault_b = Bool("ContractFaultB")
    target = Bool("ContractComposedTarget")
    with Program() as program:
        with rung(system.sys.first_scan, ~guard_a):
            latch(fault_a)
        with rung(system.sys.first_scan, ~guard_b):
            latch(fault_b)
        with rung(start, ~fault_a, ~fault_b):
            latch(target)
    return program, guard_a, guard_b, start, fault_a, fault_b, target


def _successive_overwrite_program() -> tuple[Program, Int, Bool, Bool, Bool, Bool]:
    heel_step = Int("ContractHeelStep")
    guard_10 = Bool("ContractGuard10", external=True)
    guard_94 = Bool("ContractGuard94", external=True)
    start = Bool("ContractOverwriteStart", external=True)
    target = Bool("ContractOverwriteTarget")
    with Program() as program:
        with rung(system.sys.first_scan):
            copy(81, heel_step)
        with rung(system.sys.first_scan, ~guard_10):
            copy(10, heel_step)
        with rung(system.sys.first_scan, ~guard_94):
            copy(94, heel_step)
        with rung(heel_step == 81, start):
            latch(target)
    return program, heel_step, guard_10, guard_94, start, target


def _capture_retained_bearings(monkeypatch) -> list[tuple[tuple, RetainedReplay]]:
    retained: list[tuple[tuple, RetainedReplay]] = []
    original = Compass.orient

    def recording_orient(self, world, target, constraints):
        result = original(self, world, target, constraints)
        if isinstance(result, Bearing) and isinstance(result.act, RetainedReplay):
            retained.append((result.world_key, result.act))
        return result

    monkeypatch.setattr(Compass, "orient", recording_orient)
    return retained


def test_retained_guard_is_the_writer_condition_minus_the_corrected_lever(monkeypatch) -> None:
    program, enable, guard, _start, _trip, target = _single_departure_program()
    retained = _capture_retained_bearings(monkeypatch)
    events = []

    plan = pilot_how(PLC(program), target, max_scans=40, on_event=events.append)

    assert plan.reachable, plan.reason
    assert len(retained) == 1
    correction = retained[0][1].correction
    assert len(correction.pilot_rungs) == 1
    installed = correction.pilot_rungs[0]
    assert installed.dest == guard.name
    # first_scan is present because this exact harmful writer read it. Enable is
    # retained for the same reason; the corrected Guard conjunct is projected.
    assert installed.operation is None
    assert isinstance(installed.guard, AllCondition)
    outer = installed.guard.conditions
    continuation = next(branch for branch in outer if isinstance(branch, AnyCondition))
    target_bound = next(branch for branch in outer if branch is not continuation)
    assert frozenset(_extract_reads_from_condition(target_bound, {})) == frozenset(
        {target.name}
    )
    continuation_reads = {
        frozenset(_extract_reads_from_condition(branch, {}))
        for branch in continuation.conditions
    }
    # The start branch is copied exactly from the harmful writer after its
    # corrected lever is projected out. first_scan appears only because that
    # writer read it. Guard may continue its own established value, but the
    # enclosing target-unresolved condition bounds that continuation.
    assert continuation_reads == {
        frozenset({system.sys.first_scan.name, enable.name}),
        frozenset({guard.name}),
    }

    # This is an ordinary verified landing: the accepted event reports that the
    # exact blocker moved from the harmful retained value to the needed value.
    accepted = [event for event in events if event.kind == "retained_replay_accepted"]
    assert len(accepted) == 1
    occurrence = accepted[0].data["occurrence"]
    blocker_change = next(
        change
        for change in accepted[0].data["changes"]["total"]
        if change.tag == occurrence["tag"]
    )
    assert (blocker_change.before, blocker_change.after) == (
        occurrence["to"],
        occurrence["from"],
    )

    assert plan.tags[guard.name] is True
    plan.fork.step()
    assert plan.fork.state.tags[target.name] is True
    assert plan.fork.state.tags[guard.name] is False


def test_successive_rebases_are_distinct_bearings_without_fake_steps(monkeypatch) -> None:
    program, guard_a, guard_b, start, _fault_a, _fault_b, target = (
        _composed_departure_program()
    )
    retained = _capture_retained_bearings(monkeypatch)

    plan = pilot_how(PLC(program), target, max_scans=60)

    assert plan.reachable, plan.reason
    assert len(retained) == 2
    assert len({world_key for world_key, _act in retained}) == 2
    assert len({act_identity(act) for _world_key, act in retained}) == 2
    assert all(len(act.correction.pilot_rungs) == 1 for _key, act in retained)
    assert {act.correction.pilot_rungs[0].dest for _key, act in retained} == {
        guard_a.name,
        guard_b.name,
    }
    # A retained-prefix replacement is not a physical action. The only
    # committed step is the final ordinary command pulse.
    assert len(plan.journey) == 1
    assert plan.journey[0].inputs == {start.name: True}
    assert plan.journey[0].scan_before == 1
    assert plan.journey[0].scan_after > plan.journey[0].scan_before


def test_same_scan_overwrites_recover_exact_final_then_preceding_occurrence(
    monkeypatch,
) -> None:
    program, heel_step, guard_10, guard_94, start, target = (
        _successive_overwrite_program()
    )
    retained = _capture_retained_bearings(monkeypatch)

    plan = pilot_how(PLC(program), target, max_scans=60)

    assert plan.reachable, plan.reason
    assert plan.tags[heel_step.name] == 81
    # The scan endpoint 81 -> 94 hides two ordered writes. Recovery must first
    # suppress the exact final 10 -> 94 occurrence, accept its honest landing
    # at 10, then re-orient and suppress the preceding 81 -> 10 occurrence.
    assert [act.occurrence.from_value for _key, act in retained] == [10, 81]
    assert [act.occurrence.to_value for _key, act in retained] == [94, 10]
    assert [
        act.correction.pilot_rungs[0].dest for _key, act in retained
    ] == [guard_94.name, guard_10.name]
    assert len({act.occurrence.ordinal for _key, act in retained}) == 2
    assert len(plan.journey) == 1
    assert plan.journey[0].inputs == {start.name: True}


def test_replay_correspondence_survives_unrelated_overlay_ordinal_shift() -> None:
    program, _enable, _guard, start, trip, _target = _single_departure_program()
    source = PLC(program)
    source.step()
    aggregate = source.history.previous_transition(trip.name, to=True)
    assert aggregate is not None
    assert aggregate.occurrence_ordinal is None

    resolved = _writer_occurrence(source, trip.name, True, source.state.scan_id)
    assert resolved is not None
    _rung, writer, exact, address = resolved
    assert writer == (None, 0)
    assert exact.occurrence_ordinal is not None
    occurrence = RetainedOccurrence(
        floor_scan=0,
        scan=1,
        ordinal=exact.occurrence_ordinal,
        tag=exact.tag_name,
        from_value=exact.from_value,
        to_value=exact.to_value,
        writer=writer,
        address=address,
    )

    replay = replay_retained_prefix(
        source,
        0,
        1,
        (PilotRung(start.name, True, system.sys.first_scan),),
    )
    replayed = replay.cause(trip, scan=1, deep=True)
    assert replayed is not None
    # A synthetic write changes the executor's absolute ordinal, but it does
    # not suppress or replace the exact program-writer occurrence.
    assert replayed.effect.occurrence_ordinal != occurrence.ordinal
    assert _occurrence_repeated(replay, occurrence)


def test_retained_rebase_replays_an_earlier_accepted_act(monkeypatch) -> None:
    program, _enable, _guard, start, _trip, target = _single_departure_program()
    original = Compass.orient
    substituted = False
    retained_after_act: list[RetainedReplay] = []

    def orient_with_one_earlier_act(self, world, target_spec, constraints):
        nonlocal substituted
        result = original(self, world, target_spec, constraints)
        if isinstance(result, Bearing) and isinstance(result.act, RetainedReplay):
            if not substituted:
                substituted = True
                pair = (start.name, True)
                return replace(
                    result,
                    act=Pulse(
                        ActPolicy(
                            source=ActSource.TRACE,
                            action_pairs=(pair,),
                            applied=(pair,),
                        )
                    ),
                    rationale="contract: accept one ordinary act before rebase",
                )
            retained_after_act.append(result.act)
        return result

    monkeypatch.setattr(Compass, "orient", orient_with_one_earlier_act)

    plan = pilot_how(PLC(program), target, max_scans=40)

    assert substituted
    assert retained_after_act
    assert plan.reachable, plan.reason
    assert plan.changes[start.name] is True
    assert plan.replay().state.tags[target.name] is True


def test_repeated_same_subroutine_writer_calls_have_distinct_addresses() -> None:
    output = Bool("ContractRepeatedSubOutput")

    @subroutine("ContractRepeatedSub")
    def shared_writer() -> None:
        with rung():
            latch(output)

    with Program(strict=False) as program:
        with rung():
            call(shared_writer)
            reset(output)
            call(shared_writer)

    plc = PLC(program)
    plc.step()
    projection = _scan_projection(plc, plc.state.scan_id)
    assert projection is not None
    writes = tuple(
        write
        for write in projection.writes
        if write.rung_id.subroutine == "ContractRepeatedSub"
        and write.transition.tag_name == output.name
    )
    assert len(writes) == 2
    assert [
        (write.transition.from_value, write.transition.to_value) for write in writes
    ] == [(False, True), (False, True)]

    addresses = tuple(_write_address(projection, write) for write in writes)
    assert addresses[0] != addresses[1]
    assert addresses[0][2:] == (0, 0)
    assert addresses[1][2:] == (1, 0)


def test_trimmed_public_floor_does_not_offer_hidden_predecessor(monkeypatch) -> None:
    program, _enable, _guard, _start, trip, target = _single_departure_program()
    source = PLC(program)
    for _ in range(6):
        source.step()
    plc = source.fork(source.state.scan_id, inherit_log=False)
    retained = _capture_retained_bearings(monkeypatch)

    plan = pilot_how(plc, target, max_scans=20)

    assert plc.history.oldest_scan_id > 0
    assert plc.state.tags[trip.name] is True
    assert not plan.reachable
    assert retained == []
