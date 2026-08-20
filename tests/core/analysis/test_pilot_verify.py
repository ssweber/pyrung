"""Tests for pilot verify — gate pipeline for trial acceptance.

Coverage targets:
- verify_gates: avoid → target → spin → dead-end → outcome → revisit
- _gate_spin: state-key change detection, excursion replay
- _gate_revisit: ordinary, earned-work, and departure revisit admission
- _gate_dead_end: empty frontier, lateral detection, channel override
"""

from __future__ import annotations

from types import SimpleNamespace

from pyrung import Bool, Int, Program, Rung, out
from pyrung.core.analysis.pilot.earned_work import (
    EarnedWorkReceipt,
)
from pyrung.core.analysis.pilot.effects import (
    EffectExpectation,
    EffectObligation,
    EffectObservation,
)
from pyrung.core.analysis.pilot.execution import (
    ChannelMotion,
    ExecutionReceipt,
    ExecutionSpan,
    MotionKind,
    capture_execution_spans,
)
from pyrung.core.analysis.pilot.navigation_contracts import (
    ActPolicy,
    ActSource,
    Bearing,
    BearingObjective,
    ChannelHeading,
    Coast,
    Pulse,
    RouteEdgeContext,
    TargetSpec,
)
from pyrung.core.analysis.pilot.outcome import (
    Agency,
    BearingEffect,
    ProgressEffect,
    TrialAssessment,
)
from pyrung.core.analysis.pilot.overlay import PilotRung
from pyrung.core.analysis.pilot.requirements import (
    OperandAuthority,
    RequirementPhase,
    RequirementStatus,
)
from pyrung.core.analysis.pilot.trace_tree import TraceNode
from pyrung.core.analysis.pilot.trial_gates import (
    _gate_spin,
    _SpinVerdict,
)
from pyrung.core.analysis.pilot.types import (
    AssessedMotion,
    _ExecutedAttempt,
    _PulseState,
)
from pyrung.core.analysis.pilot.verify import (
    _accepted_trial,
    _executed_source_world_key,
    _owned_channel_motion,
    verify_gates,
)
from pyrung.core.analysis.pilot.world_key import _pilot_world_key, _StateKeyConfig
from pyrung.core.condition import CompareEq
from pyrung.core.crossing import Cmp
from pyrung.core.instruction.advance import constraint_holds
from pyrung.core.runner import PLC

# ---------------------------------------------------------------------------
# Gate pipeline
# ---------------------------------------------------------------------------


def _executed(
    pulse: _PulseState,
    bearing: Bearing,
    effect_observations: tuple[EffectObservation, ...] = (),
) -> _ExecutedAttempt:
    """Build the same immutable pre-VERIFY receipt production execution requires."""

    configurations = tuple(getattr(pulse, "applied_configurations", ()))
    source_snap = getattr(pulse, "source_snap", None)
    return _ExecutedAttempt(
        pulse,
        bearing,
        effect_observations,
        execution=ExecutionReceipt(
            before_snap=source_snap or pulse.action_snap,
            after_snap=pulse.snap,
            channel_motion=getattr(pulse, "channel_motion", ChannelMotion()),
            coast_receipt=pulse.coast_receipt,
            timeline=tuple(getattr(pulse, "timeline", ())),
            effect_observations=tuple(
                observation.diagnostic_snapshot() for observation in effect_observations
            ),
            replay_motion=getattr(pulse, "replay_motion", ChannelMotion()),
            spans=capture_execution_spans(pulse.fork, tuple(pulse.kernel_scan_ids)),
            source_scan=pulse.scan_before,
            applied_configurations=configurations,
            stop=getattr(pulse, "stop_receipt", None),
        ),
    )


def _target_landing_attempt(
    *,
    source: Bool,
    target: Bool,
    before: dict[str, object],
    after: dict[str, object],
    expectation: EffectExpectation | None = None,
    observations: tuple[EffectObservation, ...] = (),
) -> tuple[_ExecutedAttempt, SimpleNamespace]:
    with Program() as program:
        with Rung(source):
            out(target)
    plc = PLC(program, dt=0.010)
    policy = ActPolicy(
        source=ActSource.TRACE,
        action_pairs=((source.name, True),),
        applied=((source.name, True),),
        expectation=expectation,
    )
    pulse = _PulseState(
        fork=plc,
        scan_before=0,
        action_scan=1,
        action_snap=dict(after),
        wait_snaps=(),
        post_pulse_snap=dict(after),
        post_pulse_key=("target-landing",),
        snap=dict(after),
        key=("target-landing",),
        kernel_scan_ids=(),
    )
    bearing = Bearing(
        ("source",),
        Pulse(policy),
        BearingObjective(TargetSpec(target.name, True)),
    )
    return (
        _executed(pulse, bearing, observations),
        SimpleNamespace(
            snap=before,
            key=("source",),
            distance_before=1,
            tree=TraceNode(target.name, True),
        ),
    )


def test_execution_span_keeps_one_detached_epoch_owner_across_the_next_fork() -> None:
    command = Bool("ReceiptCommand", external=True)
    result = Bool("ReceiptResult")
    with Program() as program:
        with Rung(command):
            out(result)

    source = PLC(program)
    execution = source.fork()
    execution.patch({command.name: True})
    execution.step()

    spans = capture_execution_spans(execution, (1,))

    assert len(spans) == 1
    span = spans[0]
    assert isinstance(span, ExecutionSpan)
    assert span.kernel_scan_ids == (1,)
    assert span.epoch.first_scan <= 1 <= span.epoch.last_scan
    assert span.owner.state_at(1).tags[result.name] is True

    continuation = execution.fork()
    assert continuation._causal_lineage.owner_at(1) is span.owner


def test_global_target_from_another_producer_does_not_pardon_selected_effect_failure() -> None:
    selected = Bool("SelectedProducerCommand", external=True)
    effect = Bool("SelectedProducerEffect")
    target = Bool("AlternateProducerTarget")
    obligation = EffectObligation(
        effect.name,
        True,
        (None, 0, ()),
        (None, 1, ()),
        ((effect.name, True),),
    )
    expectation = EffectExpectation((obligation,))
    observation = EffectObservation(obligation, "OVERWRITTEN")
    before = {selected.name: False, effect.name: False, target.name: False}
    after = {selected.name: True, effect.name: False, target.name: True}
    attempt, frame = _target_landing_attempt(
        source=selected,
        target=target,
        before=before,
        after=after,
        expectation=expectation,
        observations=(observation,),
    )

    result = verify_gates(
        attempt,
        frame,
        SimpleNamespace(earned_work=None, active_requirements=()),
        SimpleNamespace(avoid_pred=None, target=TargetSpec(target.name, True)),
    )

    assert result.trial is None
    assert result.executed is attempt
    assert result.executed_attempt is attempt
    assert result.nogood_pairs == frozenset()
    assert not any(event.event == "target" for event in result.gate_events)


def test_selected_work_cannot_break_a_satisfied_authoritative_requirement() -> None:
    selected = Bool("ConstraintPreservingCommand", external=True)
    target = Bool("ConstraintPreservingTarget")
    configured = Int("ConstraintPreservingConfigured", default=20)
    condition = Cmp(configured.name, ">", 10)
    requirement = SimpleNamespace(
        condition=condition,
        status=RequirementStatus.ACTIVE,
        phase=RequirementPhase.STEADY,
        permits_assignment=False,
        operand_authority=OperandAuthority.CONFIGURED,
    )
    before = {selected.name: False, target.name: False, configured.name: 20}
    after = {selected.name: True, target.name: True, configured.name: 0}
    attempt, frame = _target_landing_attempt(
        source=selected,
        target=target,
        before=before,
        after=after,
    )

    result = verify_gates(
        attempt,
        frame,
        SimpleNamespace(earned_work=None, active_requirements=(requirement,)),
        SimpleNamespace(avoid_pred=None, target=TargetSpec(target.name, True)),
    )

    assert result.trial is None
    assert constraint_holds(condition, before) is True
    assert constraint_holds(condition, after) is False
    assert not any(event.event == "target" for event in result.gate_events)


def test_unconsumed_live_boundary_loss_remains_a_departure() -> None:
    source = Bool("LiveBoundarySource", external=True)
    target = Bool("LiveBoundaryLaterTarget")
    effect = Int("LiveBoundaryEffect")
    obligation = EffectObligation(
        effect.name,
        1,
        (None, 0, ()),
        None,
        (),
        boundary=(effect.name, 1),
    )
    expectation = EffectExpectation((obligation,))
    before = {source.name: False, effect.name: 0, target.name: False}
    after = {source.name: True, effect.name: 0, target.name: False}
    attempt, frame = _target_landing_attempt(
        source=source,
        target=target,
        before=before,
        after=after,
        expectation=expectation,
        observations=(EffectObservation(obligation, "SURVIVED"),),
    )

    result = verify_gates(
        attempt,
        frame,
        SimpleNamespace(earned_work=None, active_requirements=()),
        SimpleNamespace(avoid_pred=None, target=TargetSpec(target.name, True)),
    )

    assert result.trial is not None
    assert result.trial.execution.channel_motion.departed
    verification = result.trial.verification
    assert isinstance(verification, AssessedMotion)
    assert verification.assessment.bearing is BearingEffect.DEPARTED


def test_outer_owner_rebases_inner_departure_receipt_to_reached():
    trial = SimpleNamespace(
        snap={"State": 6},
        coast_receipt=SimpleNamespace(stop_reason="departed"),
    )

    assert _owned_channel_motion(trial, ChannelMotion("State", 6)).reached


def test_relational_owner_retains_its_crossing_receipt_after_overshoot():
    trial = SimpleNamespace(
        snap={"Acc": 5},
        coast_receipt=SimpleNamespace(stop_reason="reached"),
    )

    assert _owned_channel_motion(trial, ChannelMotion("Acc", 4)).reached


def test_wrong_outer_landing_retains_departure_receipt():
    trial = SimpleNamespace(
        snap={"State": 8},
        coast_receipt=SimpleNamespace(stop_reason="departed"),
    )

    assert _owned_channel_motion(trial, ChannelMotion("State", 6)).departed


def test_wrong_channel_landing_cannot_mint_a_frontier_progress_receipt():
    """A prerequisite reached before an ejection does not own the coast tip."""

    with Program() as program:
        pass
    plc = PLC(program, dt=0.010)
    before = {"State": 3, "Prerequisite": False}
    after = {"State": 8, "Prerequisite": True}
    policy = ActPolicy(
        ActSource.ROUTE,
        heading=ChannelHeading("State", 6),
        motion=MotionKind.COAST_TO_BEARING,
    )
    pulse = _PulseState(
        plc,
        0,
        0,
        before,
        (),
        before,
        ("source",),
        after,
        ("landing",),
        (),
    )
    attempt = _executed(
        pulse,
        Bearing(
            ("source",),
            Coast("bearing", policy),
            BearingObjective(TargetSpec("Target", True)),
        ),
    )
    verification = AssessedMotion(
        ("landing",),
        1,
        TrialAssessment(
            Agency.PROGRAM,
            BearingEffect.DEPARTED,
            ProgressEffect.FORWARD,
            new_frontier=True,
            accepted=True,
        ),
    )

    accepted = _accepted_trial(
        attempt,
        SimpleNamespace(key=("source",), snap=before, distance_before=2),
        [],
        ChannelMotion("State", 6, stop_reason="departed"),
        EarnedWorkReceipt(),
        verification,
    )

    assert accepted.execution.scan_progress is None


def test_chart_reachability_cannot_mint_selected_producer_landing() -> None:
    """A globally recoverable side branch is geometry, not landing proof."""

    with Program() as program:
        pass
    plc = PLC(program, dt=0.010)
    before = {"State": 3}
    after = {"State": 8}
    route = RouteEdgeContext("State", 3, 6)
    policy = ActPolicy(
        ActSource.ROUTE,
        heading=ChannelHeading("State", 6, route=route),
        motion=MotionKind.COAST_TO_BEARING,
    )
    pulse = _PulseState(
        plc,
        0,
        0,
        before,
        (),
        before,
        ("source",),
        after,
        ("landing",),
        (),
    )
    attempt = _executed(
        pulse,
        Bearing(
            ("source",),
            Coast("bearing", policy),
            BearingObjective(TargetSpec("Target", True)),
            orientation=SimpleNamespace(
                world=SimpleNamespace(
                    context=SimpleNamespace(
                        target=TargetSpec("Target", True),
                        compass=SimpleNamespace(
                            chart_graphs=(
                                SimpleNamespace(
                                    role=SimpleNamespace(channel_tag="State"),
                                    # Boolean True shares an equality domain
                                    # with integer state 1. The old broad chart
                                    # proof therefore called state 8 target-
                                    # reachable despite the selected 3->6 edge.
                                    edges=(SimpleNamespace(from_value=8, to_value=1),),
                                ),
                            ),
                        ),
                    ),
                ),
            ),
        ),
    )
    verification = AssessedMotion(
        ("landing",),
        1,
        TrialAssessment(
            Agency.PROGRAM,
            BearingEffect.DEPARTED,
            ProgressEffect.UNCHANGED,
            new_frontier=False,
            accepted=True,
        ),
    )

    accepted = _accepted_trial(
        attempt,
        SimpleNamespace(key=("source",), snap=before, distance_before=2),
        [],
        ChannelMotion("State", 6, stop_reason="departed"),
        EarnedWorkReceipt(),
        verification,
    )

    assert accepted.execution.scan_progress is None


def test_executed_source_world_includes_normal_bearing_prerequisite():
    target = Bool("SourceWorldTarget")
    guard = Bool("SourceWorldGuard")
    snap = {target.name: False, guard.name: False}
    cfg = _StateKeyConfig(
        stateful_names=(target.name, guard.name),
        done_specs=(),
        threshold_vector_specs=(),
        acc_indices=frozenset(),
    )
    frame = SimpleNamespace(key=_pilot_world_key(snap, cfg, ()), snap=snap)
    prerequisite = PilotRung(target.name, True, CompareEq(guard, False))
    state = SimpleNamespace(key_config=cfg, pilot_rungs=[prerequisite])

    source_key = _executed_source_world_key(frame, state)

    assert source_key == _pilot_world_key(snap, cfg, (prerequisite,))
    assert source_key != frame.key


class TestGateSpin:
    """Spin gate — trial must change the state key."""

    def test_no_change_is_spin(self):
        Source = Bool("SpinSource", external=True)
        Dest = Bool("SpinDest")
        with Program() as prog:
            with Rung(Source):
                out(Dest)
        plc = PLC(prog, dt=0.010)
        snap = dict(plc.state.tags)
        key = ("same",)
        trial = _PulseState(plc, 0, 0, snap, (), snap, key, snap, key, ())
        gates = []
        verdict = _gate_spin(
            trial,
            SimpleNamespace(key=key, snap=snap),
            SimpleNamespace(earned_work=None),
            gate_events=gates,
        )
        assert verdict is _SpinVerdict.SPIN
        assert gates == []

    def test_verify_reports_excursion_without_investigating_or_nogood(self):
        source = Bool("ExcursionSource", external=True)
        dest = Bool("ExcursionDest")
        with Program() as program:
            with Rung(source):
                out(dest)
        plc = PLC(program, dt=0.010)
        plc.step()
        snap = dict(plc.state.tags)
        cfg = _StateKeyConfig(
            stateful_names=(dest.name,),
            done_specs=(),
            threshold_vector_specs=(),
            acc_indices=frozenset(),
        )
        frame_key = _pilot_world_key(snap, cfg, ())
        policy = ActPolicy(
            source=ActSource.TRACE,
            action_pairs=((source.name, True),),
            applied=((source.name, True),),
            nogood_pair=(source.name, True),
        )
        bearing = Bearing(
            frame_key,
            Pulse(policy),
            BearingObjective(TargetSpec(dest.name, True)),
        )
        trial = _PulseState(
            plc.fork(),
            plc.state.scan_id,
            plc.state.scan_id,
            snap,
            (),
            {**snap, dest.name: True},
            ("post-pulse",),
            snap,
            frame_key,
            (),
        )
        executed = _executed(pulse=trial, bearing=bearing)

        result = verify_gates(
            executed,
            SimpleNamespace(key=frame_key, snap=snap),
            SimpleNamespace(key_config=cfg, earned_work=None),
            SimpleNamespace(
                avoid_pred=None,
                target=TargetSpec(dest.name, True),
            ),
        )

        assert result.trial is None
        assert result.excursion_attempt is executed
        assert result.nogood_pairs == frozenset()
        assert result.correction_requirement is None
