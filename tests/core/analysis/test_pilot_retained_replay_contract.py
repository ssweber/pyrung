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
)
from pyrung.core.analysis.pilot.overlay import PilotRung
from pyrung.core.analysis.pilot.retained import (
    _MAX_RETAINED_COMPOSITIONS,
    _occurrence_repeated,
    _scan_projection,
    _write_address,
    _writer_occurrence,
    replay_retained_prefix,
)
from pyrung.core.condition import AnyCondition


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
    process_step = Int("ContractProcessStep")
    guard_10 = Bool("ContractGuard10", external=True)
    guard_94 = Bool("ContractGuard94", external=True)
    start = Bool("ContractOverwriteStart", external=True)
    target = Bool("ContractOverwriteTarget")
    with Program() as program:
        with rung(system.sys.first_scan):
            copy(81, process_step)
        with rung(system.sys.first_scan, ~guard_10):
            copy(10, process_step)
        with rung(system.sys.first_scan, ~guard_94):
            copy(94, process_step)
        with rung(process_step == 81, start):
            latch(target)
    return program, process_step, guard_10, guard_94, start, target


def _shifted_identical_blocker_program(
    *,
    correctable_replacement: bool = True,
) -> tuple[Program, Int, Bool, Bool | None]:
    """A correction moves one identical write instead of fixing the endpoint.

    The second write is a no-op in the recorded scan but owns the selected
    retained writer site. Suppressing it leaves the first writer to perform the
    same ``81 -> 98`` transition. When the first writer's guard is an
    external tag it supplies correction B; with only ``first_scan`` it has no
    defensible replacement Bearing.
    """

    process_step = Int("ContractShiftedProcessStep", default=81)
    guard_a = Bool("ContractShiftedGuardA", external=True)
    guard_b = Bool("ContractShiftedGuardB", external=True) if correctable_replacement else None
    with Program(strict=False) as program:
        replacement_condition = (
            (system.sys.first_scan, ~guard_b) if guard_b is not None else (system.sys.first_scan,)
        )
        with rung(*replacement_condition):
            copy(98, process_step)
        with rung(system.sys.first_scan, ~guard_a):
            copy(98, process_step)
    return program, process_step, guard_a, guard_b


def _shifted_blocker_chain_program(
    length: int,
) -> tuple[Program, Int, tuple[Bool, ...]]:
    """A long succession of latent identical retained blockers."""

    process_step = Int("ContractBoundedProcessStep", default=81)
    guards = tuple(Bool(f"ContractBoundedGuard{index}", external=True) for index in range(length))
    with Program(strict=False) as program:
        for guard in guards:
            with rung(system.sys.first_scan, ~guard):
                copy(98, process_step)
    return program, process_step, guards


def _capture_retained_bearings(monkeypatch) -> list[tuple[tuple, RetainedReplay]]:
    from pyrung.core.analysis.pilot import retained as retained_module

    retained: list[tuple[tuple, RetainedReplay]] = []
    original = retained_module.compose_retained_bearing

    def recording_compose(compass, bearing, target, constraints):
        result = original(compass, bearing, target, constraints)
        retained.append((result.world_key, result.act))
        return result

    monkeypatch.setattr(retained_module, "compose_retained_bearing", recording_compose)
    return retained


def _capture_retained_verifications(monkeypatch) -> list[tuple[tuple[str, ...], bool]]:
    """Record retained acts after their shared VERIFY and occurrence gate."""

    from pyrung.core.analysis.pilot import retained as retained_module

    verified: list[tuple[tuple[str, ...], bool]] = []
    original = retained_module.execute_retained_replay

    def recording_execute(bearing, frame, state, ctx):
        result = original(bearing, frame, state, ctx)
        # Both disposable and outer retained trials preserve the ordinary
        # execution receipt even when their final occurrence gate rejects.
        assert result.executed is not None
        assert result.executed.bearing is bearing
        verified.append(
            (
                tuple(rung.dest for rung in bearing.act.correction.pilot_rungs),
                result.trial is not None,
            )
        )
        return result

    monkeypatch.setattr(retained_module, "execute_retained_replay", recording_execute)
    return verified


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
    # The exact occurrence scope and continuation lifetime remain separate OR
    # arms. first_scan and Enable belong only to the former; unresolved Target
    # and the correction's self-continuation belong only to the latter.
    assert installed.operation is None
    assert isinstance(installed.guard, AnyCondition)
    branch_reads = {
        frozenset(_extract_reads_from_condition(branch, {}))
        for branch in installed.guard.conditions
    }
    assert branch_reads == {
        frozenset({system.sys.first_scan.name, enable.name}),
        frozenset({target.name, guard.name}),
    }

    # This is an ordinary verified landing: the accepted event reports that the
    # exact blocker moved from the harmful retained value to the needed value.
    accepted = [event for event in events if event.kind == "retained_replay_accepted"]
    assert len(accepted) == 1
    occurrence = accepted[0].data["occurrence"]
    blocker_change = next(
        change for change in accepted[0].data["changes"]["total"] if change.tag == occurrence["tag"]
    )
    assert (blocker_change.before, blocker_change.after) == (
        occurrence["to"],
        occurrence["from"],
    )

    assert plan.tags[guard.name] is True
    plan.fork.step()
    assert plan.fork.state.tags[target.name] is True
    assert plan.fork.state.tags[guard.name] is False


def test_successive_rebases_compose_one_bearing_without_fake_steps(monkeypatch) -> None:
    program, guard_a, guard_b, start, _fault_a, _fault_b, target = _composed_departure_program()
    retained = _capture_retained_bearings(monkeypatch)

    plan = pilot_how(PLC(program), target, max_scans=60)

    assert plan.reachable, plan.reason
    assert len(retained) == 1
    correction = retained[0][1].correction
    assert len(correction.pilot_rungs) == 2
    assert {rung.dest for rung in correction.pilot_rungs} == {
        guard_a.name,
        guard_b.name,
    }
    # A retained-prefix replacement is not a physical action. The only
    # committed step is the final ordinary command pulse.
    assert len(plan.journey) == 1
    assert plan.journey[0].inputs == {start.name: True}
    assert plan.journey[0].scan_before == 1
    assert plan.journey[0].scan_after > plan.journey[0].scan_before


def test_retained_inner_orientation_composes_unchanged_candidate_and_reuses_verify(
    monkeypatch,
) -> None:
    """A shifted identical blocker is solved only as one verified A+B act."""

    from pyrung.core.analysis.pilot import retained as retained_module

    composition_limits: list[int] = []
    original_compose = retained_module.compose_corrections

    def recording_compose(*args, **kwargs):
        composition_limits.append(kwargs["budget"].limit)
        return original_compose(*args, **kwargs)

    monkeypatch.setattr(retained_module, "compose_corrections", recording_compose)

    program, process_step, guard_a, guard_b = _shifted_identical_blocker_program()
    assert guard_b is not None
    retained = _capture_retained_bearings(monkeypatch)
    verified = _capture_retained_verifications(monkeypatch)
    isolation_receipts: list[tuple[object, object, tuple[object, ...], tuple[object, ...]]] = []
    original_orient = Compass.orient

    def isolation_orient(self, world, target_spec, constraints):
        state = world.state
        ctx = world.context
        before = (
            state.work,
            state.work.state.scan_id,
            dict(state.work.state.tags),
            tuple(state.pilot_rungs),
            tuple(state.correction_receipts),
            tuple(state.committed_acts),
            tuple(state.checkpoints),
            tuple(state.steps),
            tuple(state.journey),
            {key: frozenset(value) for key, value in state.correction_nogoods.items()},
        )
        compass_before = ctx.compass
        knowledge_before = ctx.compass.knowledge
        result = original_orient(self, world, target_spec, constraints)
        after = (
            state.work,
            state.work.state.scan_id,
            dict(state.work.state.tags),
            tuple(state.pilot_rungs),
            tuple(state.correction_receipts),
            tuple(state.committed_acts),
            tuple(state.checkpoints),
            tuple(state.steps),
            tuple(state.journey),
            {key: frozenset(value) for key, value in state.correction_nogoods.items()},
        )
        if isinstance(result, Bearing) and isinstance(result.act, RetainedReplay):
            isolation_receipts.append((compass_before, knowledge_before, before, after))
            assert ctx.compass is compass_before
            assert ctx.compass.knowledge is knowledge_before
        return result

    monkeypatch.setattr(Compass, "orient", isolation_orient)

    source = PLC(program)
    source.step()
    assert source.state.tags[process_step.name] == 98
    plan = pilot_how(source, process_step == 81, max_scans=40)

    assert plan.reachable, (
        plan.reason,
        verified,
        [tuple(rung.dest for rung in act.correction.pilot_rungs) for _key, act in retained],
    )
    assert plan.tags[process_step.name] == 81
    assert plan.replay().state.tags[process_step.name] == 81

    composite_dests = frozenset({guard_a.name, guard_b.name})
    # The exact ordinary verifier accepts A as a novel correction-overlay
    # world. Because its physical channel endpoint is nevertheless unchanged,
    # the local orient phase continues to B instead of leaking A as an outer
    # commit.
    assert verified[0] == ((guard_a.name,), True)
    assert any(frozenset(dests) == composite_dests and accepted for dests, accepted in verified)
    assert len(retained) == 1
    assert frozenset(rung.dest for rung in retained[0][1].correction.pilot_rungs) == composite_dests
    assert composition_limits == [_MAX_RETAINED_COMPOSITIONS + 1]

    # Inner orientation and attempts are disposable.  Their observations and
    # nogoods are returned as receipts to the local composer; they do not apply
    # Compass knowledge or mutate any outer/global execution owner.
    assert isolation_receipts
    for compass_before, knowledge_before, before, after in isolation_receipts:
        assert before == after
        assert compass_before.knowledge is knowledge_before


def test_retained_unchanged_frontier_without_replacement_does_not_claim_progress(
    monkeypatch,
) -> None:
    """An accepted singleton cannot fabricate a target or a composite."""

    program, process_step, guard_a, guard_b = _shifted_identical_blocker_program(
        correctable_replacement=False
    )
    assert guard_b is None
    retained = _capture_retained_bearings(monkeypatch)
    verified = _capture_retained_verifications(monkeypatch)

    source = PLC(program)
    source.step()
    assert source.state.tags[process_step.name] == 98
    plan = pilot_how(source, process_step == 81, max_scans=20)

    assert not plan.reachable
    assert verified
    assert all(dests == (guard_a.name,) and accepted for dests, accepted in verified)
    assert len(retained) == 1
    assert tuple(rung.dest for rung in retained[0][1].correction.pilot_rungs) == (guard_a.name,)


def test_retained_candidate_composition_is_bounded(monkeypatch) -> None:
    """A long latent-writer chain stops at the inner composition budget."""

    program, process_step, guards = _shifted_blocker_chain_program(_MAX_RETAINED_COMPOSITIONS + 4)
    retained = _capture_retained_bearings(monkeypatch)
    verified = _capture_retained_verifications(monkeypatch)

    source = PLC(program)
    source.step()
    assert source.state.tags[process_step.name] == 98
    plan = pilot_how(source, process_step == 81, max_scans=20)

    assert plan.reachable, plan.reason
    assert plan.tags[process_step.name] == 81
    # The outer loop may legitimately accept several bounded prefixes. No one
    # inner composition may exhaust the full latent chain, and the finite set of
    # outer prefixes must terminate instead of reconstructing one in a cycle.
    attempted_widths = [len(dests) for dests, _accepted in verified]
    returned_widths = [len(act.correction.pilot_rungs) for _key, act in retained]
    assert attempted_widths
    assert max(attempted_widths) < len(guards)
    assert returned_widths
    assert max(returned_widths) <= _MAX_RETAINED_COMPOSITIONS + 2
    assert max(returned_widths) < len(guards)
    assert sum(returned_widths) == len(guards)
    assert len(verified) <= len(guards) + len(retained)


def test_same_scan_overwrites_recover_exact_final_then_preceding_occurrence(
    monkeypatch,
) -> None:
    program, process_step, guard_10, guard_94, start, target = _successive_overwrite_program()
    retained = _capture_retained_bearings(monkeypatch)

    plan = pilot_how(PLC(program), target, max_scans=60)

    assert plan.reachable, plan.reason
    assert plan.tags[process_step.name] == 81
    # The scan endpoint 81 -> 94 hides two ordered writes. Recovery must first
    # suppress the exact final 10 -> 94 occurrence, accept its honest landing
    # at 10, then re-orient and suppress the preceding 81 -> 10 occurrence.
    assert [act.occurrence.from_value for _key, act in retained] == [10, 81]
    assert [act.occurrence.to_value for _key, act in retained] == [94, 10]
    assert [act.correction.pilot_rungs[0].dest for _key, act in retained] == [
        guard_94.name,
        guard_10.name,
    ]
    causal_identities = {
        (
            act.occurrence.writer,
            act.occurrence.address,
            (
                act.occurrence.tag,
                act.occurrence.from_value,
                act.occurrence.to_value,
            ),
        )
        for _key, act in retained
    }
    # Rebased replay-local ordinals may coincide. Writer site, dynamic address,
    # and exact transition together retain the two causal identities.
    assert len(causal_identities) == 2
    assert len(plan.journey) == 1
    assert plan.journey[0].inputs == {start.name: True}


def test_replay_correspondence_survives_unrelated_overlay_ordinal_shift(monkeypatch) -> None:
    program, _enable, _guard, start, trip, _target = _single_departure_program()
    source = PLC(program)
    source.step()
    aggregate = source.history.previous_transition(trip.name, to=True)
    assert aggregate is not None
    assert aggregate.occurrence_ordinal is None

    requested_depths: list[bool] = []
    original_cause = source.cause

    def recording_cause(tag, scan=None, *, deep=True, **kwargs):
        requested_depths.append(deep)
        return original_cause(tag, scan=scan, deep=deep, **kwargs)

    monkeypatch.setattr(source, "cause", recording_cause)
    resolved = _writer_occurrence(source, trip.name, True, source.state.scan_id)
    assert resolved is not None
    assert requested_depths and requested_depths == [False]
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
    assert [(write.transition.from_value, write.transition.to_value) for write in writes] == [
        (False, True),
        (False, True),
    ]

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
