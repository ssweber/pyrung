"""Tests for pilot verify — gate pipeline for trial acceptance.

Coverage targets:
- verify_gates: avoid → target → spin → dead-end → outcome → revisit
- _gate_spin: state-key change detection, excursion replay
- _gate_revisit: ordinary, earned-work, and departure revisit admission
- _gate_dead_end: empty frontier, lateral detection, channel override
"""

from __future__ import annotations

from dataclasses import replace
from types import MappingProxyType, SimpleNamespace

import pytest

from pyrung import Bool, Int, Program, Real, Rung, copy, out
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.pilot.coast import CoastReceipt, CoastTriggerEvent
from pyrung.core.analysis.pilot.compass import ActionNogoodObservation
from pyrung.core.analysis.pilot.constrained_reachability import NavigationEvidence, Unknown
from pyrung.core.analysis.pilot.earned_work import (
    EarnedWork,
    EarnedWorkComponent,
    EarnedWorkReading,
    EarnedWorkReceipt,
)
from pyrung.core.analysis.pilot.effects import (
    EffectExpectation,
    EffectObligation,
    EffectObservation,
    observe_execution_window,
)
from pyrung.core.analysis.pilot.investigate import ExcursionResult, correction_identity
from pyrung.core.analysis.pilot.navigation_contracts import (
    ActPolicy,
    ActSource,
    BatchPulse,
    Bearing,
    BearingObjective,
    ChannelHeading,
    Coast,
    Pulse,
    RouteEdgeContext,
    TargetSpec,
    act_identity,
)
from pyrung.core.analysis.pilot.outcome import (
    Agency,
    BearingEffect,
    ProgressEffect,
    TrialAssessment,
)
from pyrung.core.analysis.pilot.overlay import PilotRung
from pyrung.core.analysis.pilot.physical import install_harness
from pyrung.core.analysis.pilot.requirements import (
    OperandAuthority,
    RequirementPhase,
    RequirementStatus,
)
from pyrung.core.analysis.pilot.trace import TraceNode
from pyrung.core.analysis.pilot.types import (
    AssessedMotion,
    ChannelMotion,
    ExecutionReceipt,
    ExecutionSpan,
    MotionKind,
    RevisitCredential,
    TargetReached,
    _AttemptResult,
    _ConfirmedCorrection,
    _ExecutedAttempt,
    _IterationFrame,
    _PulseState,
    capture_execution_spans,
)
from pyrung.core.analysis.pilot.verify import (
    _accepted_trial,
    _executed_source_world_key,
    _gate_dead_end,
    _gate_revisit,
    _gate_spin,
    _owned_channel_motion,
    _replayed_channel_motion,
    _SpinVerdict,
    verify_excursion_replay,
    verify_gates,
)
from pyrung.core.analysis.pilot.world_key import _pilot_world_key, _StateKeyConfig
from pyrung.core.condition import CompareEq
from pyrung.core.crossing import Cmp
from pyrung.core.instruction.advance import constraint_holds
from pyrung.core.physical import Physical, Ramp
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
            source_world=bearing.world_key,
            decision_identity=act_identity(bearing.act),
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


def test_replay_reclassifies_stale_departure_from_corrected_landing():
    stale = ChannelMotion("Phase", 2, stop_reason="departed")

    assert _replayed_channel_motion({"Phase": 2}, {"Phase": 1}, stale).reached
    assert _replayed_channel_motion({"Phase": 1}, {"Phase": 1}, stale).stop_reason == "timeout"
    assert _replayed_channel_motion({"Phase": 3}, {"Phase": 1}, stale).departed


def test_replay_relational_overshoot_reaches_owned_boundary():
    stale = ChannelMotion(
        "Accumulator",
        4,
        boundary=Cmp("Accumulator", ">=", 4),
        stop_reason="departed",
    )

    assert _replayed_channel_motion(
        {"Accumulator": 5},
        {"Accumulator": 1},
        stale,
    ).reached


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
        assert result.confirmed_correction is None

    def test_excursion_replay_is_rechecked_against_avoid_history(self):
        source = Bool("AvoidReplaySource", external=True)
        hazard = Bool("AvoidReplayHazard")
        with Program() as program:
            with Rung(source):
                out(hazard)
        plc = PLC(program, dt=0.010)
        plc.step()
        snap = dict(plc.state.tags)
        cfg = _StateKeyConfig(
            stateful_names=(hazard.name,),
            done_specs=(),
            threshold_vector_specs=(),
            acc_indices=frozenset(),
        )
        frame_key = _pilot_world_key(snap, cfg, ())
        rung = PilotRung(source.name, True, CompareEq(hazard, False))
        correction = _ConfirmedCorrection(
            identity=correction_identity((rung,)),
            pilot_rungs=(rung,),
            sources=(hazard.name, source.name),
            justification="unsafe excursion replay",
        )
        replay = plc.fork()
        replay.patch({source.name: True})
        replay.step()
        assert replay.state.tags[hazard.name] is True
        replay.patch({source.name: False})
        replay.step()
        assert replay.state.tags[hazard.name] is False
        trial = _PulseState(
            plc.fork(),
            plc.state.scan_id,
            plc.state.scan_id,
            snap,
            (),
            {**snap, hazard.name: True},
            ("post-pulse",),
            snap,
            frame_key,
            (),
        )
        policy = ActPolicy(
            source=ActSource.TRACE,
            action_pairs=((source.name, True),),
            applied=((source.name, True),),
            nogood_pair=(source.name, True),
        )
        bearing = Bearing(
            frame_key,
            Pulse(policy),
            BearingObjective(TargetSpec(hazard.name, True)),
        )
        observation = ActionNogoodObservation(frame_key, ("pair", (source.name, True)))
        detected = _AttemptResult(
            trial=None,
            excursion_attempt=_executed(pulse=trial, bearing=bearing),
            observations=(observation,),
        )

        result = verify_excursion_replay(
            detected,
            ExcursionResult(
                reverted=[hazard.name],
                correction=correction,
                replay_fork=replay,
            ),
            SimpleNamespace(key=frame_key, snap=snap),
            SimpleNamespace(
                key_config=cfg,
                earned_work=None,
                pilot_rungs=[],
            ),
            SimpleNamespace(
                avoid_pred=lambda state: bool(state.get(hazard.name)),
                target=TargetSpec(hazard.name, True),
            ),
        )

        assert result.trial is None
        assert result.avoid_names == ("avoided condition",)
        assert result.nogood_pairs == frozenset(((source.name, True),))
        assert result.observations == (observation,)
        assert result.confirmed_correction is None

    def test_excursion_replay_rebinds_same_scan_to_replay_owned_projection(self, monkeypatch):
        command = Bool("ReplayCaptureCommand", external=True)
        effect = Int("ReplayCaptureEffect")
        with Program() as program:
            with Rung(command):
                copy(1, effect)

        original = PLC(program)
        before = dict(original.state.tags)
        original.patch({command.name: True})
        original._run_single_scan(consume_pause_request=True)

        replay = PLC(program)
        replay.patch({command.name: False})
        replay._run_single_scan(consume_pause_request=True)
        assert original.state.scan_id == replay.state.scan_id == 1

        obligation = EffectObligation(
            effect.name,
            1,
            (None, 0, ()),
            None,
            (),
            terminal_target=True,
            producer_rung=program.rungs[0],
        )
        expectation = EffectExpectation((obligation,))
        cfg = _StateKeyConfig(
            stateful_names=(effect.name,),
            done_specs=(),
            threshold_vector_specs=(),
            acc_indices=frozenset(),
        )
        frame_key = _pilot_world_key(before, cfg, ())
        pulse = _PulseState(
            fork=original,
            scan_before=0,
            action_scan=1,
            action_snap=dict(original.state.tags),
            wait_snaps=(),
            post_pulse_snap=dict(original.state.tags),
            post_pulse_key=("post",),
            snap=dict(original.state.tags),
            key=("original",),
            kernel_scan_ids=(1,),
        )
        policy = ActPolicy(
            source=ActSource.TRACE,
            action_pairs=((command.name, True),),
            applied=((command.name, True),),
            expectation=expectation,
        )
        bearing = Bearing(
            frame_key,
            Pulse(policy),
            BearingObjective(TargetSpec(effect.name, 1)),
        )
        original_observations = observe_execution_window(
            expectation,
            original,
            scan_before=0,
            action_scan=1,
            kernel_scan_ids=(1,),
            projection_at=pulse.projection_at,
        )
        assert [item.disposition for item in original_observations] == ["SURVIVED"]

        detected = _AttemptResult(
            trial=None,
            excursion_attempt=_executed(
                pulse=pulse,
                bearing=bearing,
                effect_observations=original_observations,
            ),
        )
        correction = _ConfirmedCorrection(
            identity=("replay-capture",),
            pilot_rungs=(),
            sources=(effect.name,),
            justification="test corrected replay ownership",
        )
        rebound = {}

        def _capture_rebound(attempt, *_args, **_kwargs):
            rebound["attempt"] = attempt
            return _AttemptResult(trial=None, executed=attempt)

        monkeypatch.setattr(
            "pyrung.core.analysis.pilot.verify._verify_after_spin",
            _capture_rebound,
        )
        result = verify_excursion_replay(
            detected,
            ExcursionResult(
                reverted=[effect.name],
                correction=correction,
                replay_fork=replay,
                replay_kernel_scan_ids=(1,),
            ),
            SimpleNamespace(key=frame_key, snap=before),
            SimpleNamespace(
                key_config=cfg,
                earned_work=None,
                pilot_rungs=[],
                active_requirements=(),
            ),
            SimpleNamespace(
                avoid_pred=None,
                target=TargetSpec(effect.name, 1),
            ),
        )

        replay_attempt = rebound["attempt"]
        assert result.executed is replay_attempt
        assert replay_attempt.pulse.fork is replay
        assert replay_attempt.pulse._projection_cache
        assert [item.disposition for item in replay_attempt.effect_observations] == ["ABSENT"]

    def test_pending_effects_bypass_spin(self):
        source = Bool("SpinPendingSource", external=True)
        feedback = Bool(
            "SpinPendingFeedback",
            physical=Physical("SpinPendingPlant", on_delay="200ms"),
            link=source.name,
        )
        dest = Bool("SpinPendingDest")
        with Program() as program:
            with Rung(source, feedback):
                out(dest)
        plc = PLC(program, dt=0.010)
        install_harness(plc)
        plc.patch({source.name: True})
        plc.step()
        assert plc._harness.pending_count > 0

        snap = dict(plc.state.tags)
        key = ("same",)
        trial = _PulseState(
            plc,
            plc.state.scan_id,
            plc.state.scan_id,
            snap,
            (),
            snap,
            key,
            snap,
            key,
            (),
        )

        verdict = _gate_spin(
            trial,
            SimpleNamespace(key=key, snap=snap),
            SimpleNamespace(earned_work=None),
            gate_events=[],
        )

        assert verdict is _SpinVerdict.PASS


class TestGateRevisit:
    """Revisit admission follows classified, target-relative evidence."""

    def test_visited_key_rejected(self):
        key = ("visited",)
        trial = SimpleNamespace(key=key, snap={})
        gates = []
        accepted = _gate_revisit(
            trial,
            SimpleNamespace(
                seen_keys={key},
                consumed_revisits=set(),
            ),
            earned_work_receipt=EarnedWorkReceipt(),
            earned_credential=None,
            departure_credential=None,
            nogood_pair=("Cmd", True),
            gate_events=gates,
            collected_nogoods=[],
        )
        assert accepted is False
        assert gates[-1].event == "cycle"
        assert gates[-1].evidence["trial_key"] == key
        assert gates[-1].evidence["seen"] is True

    def test_earned_work_occurrence_is_admitted_once_per_landing_mark(self):
        key = ("visited",)
        trial = SimpleNamespace(key=key, snap={})
        gates = []
        receipt = EarnedWorkReceipt((EarnedWorkReading("Phase", 1, 2, 1),))
        occurrence = RevisitCredential(
            kind="earned-work",
            source_world=("source",),
            act=("pulse", (("Advance", True),)),
            transition=((("Phase", 1),), (("Phase", 2),)),
        )
        state = SimpleNamespace(
            seen_keys={key},
            consumed_revisits=set(),
        )
        accepted = _gate_revisit(
            trial,
            state,
            earned_work_receipt=receipt,
            earned_credential=occurrence,
            departure_credential=None,
            nogood_pair=None,
            gate_events=gates,
            collected_nogoods=[],
        )

        assert accepted is True
        assert gates[-1].event == "ordinal-advance"

        state.consumed_revisits.add(occurrence)
        replay_gates = []
        assert not _gate_revisit(
            trial,
            state,
            earned_work_receipt=receipt,
            earned_credential=occurrence,
            departure_credential=None,
            nogood_pair=None,
            gate_events=replay_gates,
            collected_nogoods=[],
        )
        assert replay_gates[-1].event == "cycle"
        assert replay_gates[-1].evidence["earned_work_consumed"] is True

        changed_landing = RevisitCredential(
            kind="earned-work",
            source_world=occurrence.source_world,
            act=occurrence.act,
            transition=(occurrence.transition[0], (("Phase", 3),)),
        )
        changed_gates = []
        assert _gate_revisit(
            trial,
            state,
            earned_work_receipt=receipt,
            earned_credential=changed_landing,
            departure_credential=None,
            nogood_pair=None,
            gate_events=changed_gates,
            collected_nogoods=[],
        )
        assert changed_gates[-1].event == "ordinal-advance"

    def test_departure_occurrence_is_admitted_once(self):
        key = ("visited",)
        occurrence = RevisitCredential(
            kind="departure",
            source_world=("source",),
            act=("coast", "bearing"),
            transition=("Phase", 1, 2, 3),
        )
        state = SimpleNamespace(
            seen_keys={key},
            consumed_revisits=set(),
        )

        first_gates = []
        assert _gate_revisit(
            SimpleNamespace(key=key),
            state,
            earned_work_receipt=EarnedWorkReceipt(),
            earned_credential=None,
            departure_credential=occurrence,
            nogood_pair=None,
            gate_events=first_gates,
            collected_nogoods=[],
        )
        assert first_gates[-1].event == "departure-revisit"

        state.consumed_revisits.add(occurrence)
        repeat_gates = []
        assert not _gate_revisit(
            SimpleNamespace(key=key),
            state,
            earned_work_receipt=EarnedWorkReceipt(),
            earned_credential=None,
            departure_credential=occurrence,
            nogood_pair=None,
            gate_events=repeat_gates,
            collected_nogoods=[],
        )
        assert repeat_gates[-1].event == "cycle"
        assert repeat_gates[-1].evidence["departure_consumed"] is True

    @pytest.mark.parametrize(
        "irrelevant_knowledge",
        (
            {"pending_effects": True},
            {"learned_prescribed": True},
            {"channel_reached": True},
        ),
        ids=("unrelated-pending", "learned-provenance", "reached-channel"),
    )
    def test_non_progress_context_does_not_authorize_revisit(
        self,
        irrelevant_knowledge,
    ):
        key = ("visited",)
        gates = []
        assert not _gate_revisit(
            SimpleNamespace(key=key),
            SimpleNamespace(
                seen_keys={key},
                consumed_revisits=set(),
                **irrelevant_knowledge,
            ),
            earned_work_receipt=EarnedWorkReceipt(),
            earned_credential=None,
            departure_credential=None,
            nogood_pair=None,
            gate_events=gates,
            collected_nogoods=[],
        )
        assert gates[-1].event == "cycle"


def _empty_frontier_with_channel_motion(monkeypatch, motion):
    old_tree = TraceNode("Target", True)
    new_tree = TraceNode("Target", True)
    monkeypatch.setattr(
        "pyrung.core.analysis.pilot.verify.trace_back",
        lambda *_args, **_kwargs: new_tree,
    )
    monkeypatch.setattr(
        NavigationEvidence,
        "frontier_status",
        staticmethod(lambda *_args, **_kwargs: Unknown("no continuation")),
    )
    gates = []
    nogoods = []
    landing = motion.target_value if motion.reached else 8

    result = _gate_dead_end(
        SimpleNamespace(
            fork=SimpleNamespace(),
            snap={"State": landing},
            key=("after",),
        ),
        (),
        _IterationFrame(
            snap={"State": 3},
            tree=old_tree,
            key=("before",),
            distance_before=1,
            raw_trace_actions=(),
            raw_trace_action_details=(),
        ),
        SimpleNamespace(),
        SimpleNamespace(
            pdg=object(),
            program=object(),
            steerable=frozenset(),
            clear_only=frozenset(),
            opaque_loop=frozenset(),
            pipeline_internal_tags=frozenset(),
            route=None,
            blocked_actions=frozenset(),
            domain_prior=None,
            avoid_pred=None,
            compass=SimpleNamespace(knowledge=object()),
        ),
        target=TargetSpec("Target", True),
        earned_work_receipt=EarnedWorkReceipt(),
        nogood_pair=("Advance", True),
        gate_events=gates,
        collected_nogoods=nogoods,
        channel_motion=motion,
    )
    return result, gates, nogoods


class TestGateDeadEnd:
    """Dead-end gate — frontier must be non-empty or async pending."""

    def test_applied_co_actions_are_not_reported_as_new_frontier(self, monkeypatch):
        """The full physical act, not only its requested primary, is lateral."""
        support = ("Support", True)
        old_tree = SimpleNamespace(
            ordered_actions=lambda: (),
            unsatisfied_conditions=lambda: set(),
        )
        new_tree = SimpleNamespace(
            unsatisfied_count=lambda: 2,
            ordered_actions=lambda: (support,),
            unsatisfied_conditions=lambda: set(),
        )
        monkeypatch.setattr(
            "pyrung.core.analysis.pilot.verify.trace_back",
            lambda *_args, **_kwargs: new_tree,
        )
        monkeypatch.setattr(
            NavigationEvidence,
            "frontier_status",
            staticmethod(lambda *_args, **_kwargs: Unknown("no continuation")),
        )

        result = _gate_dead_end(
            SimpleNamespace(
                fork=SimpleNamespace(),
                snap={"Primary": True, "Support": True},
                key=("after",),
            ),
            (("Primary", True), support),
            _IterationFrame(
                snap={"Primary": False, "Support": False},
                tree=old_tree,
                key=("before",),
                distance_before=2,
                raw_trace_actions=(),
                raw_trace_action_details=(),
            ),
            SimpleNamespace(),
            SimpleNamespace(
                pdg=object(),
                program=object(),
                steerable=frozenset({"Primary", "Support"}),
                clear_only=frozenset(),
                opaque_loop=frozenset(),
                pipeline_internal_tags=frozenset(),
                route=None,
                blocked_actions=frozenset(),
                domain_prior=None,
                avoid_pred=None,
                compass=SimpleNamespace(knowledge=object()),
            ),
            target=TargetSpec("Target", True),
            earned_work_receipt=EarnedWorkReceipt(),
            nogood_pair=("Primary", True),
            gate_events=[],
            collected_nogoods=[],
            channel_motion=ChannelMotion(),
        )

        assert result is None

    def test_harness_model_is_not_post_trial_proof(self, monkeypatch):
        """VERIFY requires the executed fork's live ramp, not its planning model."""
        enable = Bool("VerifyHarness_Enable", external=True)
        temp = Real(
            "VerifyHarness_Temp",
            physical=Physical("VerifyHarness_Sensor", profile=Ramp(up=1.0, down=-0.5)),
            link=enable.name,
        )
        target = Int("VerifyHarness_Target")
        with Program() as program:
            with Rung(enable, temp >= 5.0):
                copy(1, target)

        plc = PLC(program, dt=0.010)
        install_harness(plc)
        pdg = build_program_graph(program)
        captured_reads = []

        def _post_trial_trace(*_args, constraints, **_kwargs):
            captured_reads.append(constraints)
            return TraceNode(target.name, 1)

        monkeypatch.setattr(
            "pyrung.core.analysis.pilot.verify.trace_back",
            _post_trial_trace,
        )
        monkeypatch.setattr(
            NavigationEvidence,
            "frontier_status",
            staticmethod(lambda *_args, **_kwargs: Unknown("no continuation")),
        )

        initial_snap = dict(plc.state.tags)
        frame = _IterationFrame(
            snap=initial_snap,
            tree=TraceNode(target.name, 1),
            key=("before",),
            distance_before=1,
            raw_trace_actions=(),
            raw_trace_action_details=(),
        )
        state = SimpleNamespace()
        ctx = SimpleNamespace(
            pdg=pdg,
            program=program,
            steerable=frozenset({enable.name}),
            clear_only=frozenset(),
            opaque_loop=frozenset(),
            pipeline_internal_tags=frozenset(),
            route=None,
            blocked_actions=frozenset(),
            domain_prior=None,
            avoid_pred=None,
            compass=SimpleNamespace(knowledge=object()),
        )

        def _read(fork, key):
            snap = dict(fork.state.tags)
            return _gate_dead_end(
                SimpleNamespace(fork=fork, snap=snap, key=key),
                (),
                frame,
                state,
                ctx,
                target=TargetSpec(target.name, 1),
                earned_work_receipt=EarnedWorkReceipt(),
                nogood_pair=None,
                gate_events=[],
                collected_nogoods=[],
                channel_motion=ChannelMotion(),
            )

        assert _read(plc, ("quiet",)) is None

        plc.patch({enable.name: True})
        plc.step()
        assert _read(plc, ("ramping",)) is not None
        assert len(captured_reads) == 2
        assert all(read.harness is None for read in captured_reads)

    def test_channel_reached_overrides_dead_end(self, monkeypatch):
        result, gates, nogoods = _empty_frontier_with_channel_motion(
            monkeypatch,
            ChannelMotion("State", 6, stop_reason="reached"),
        )

        assert result is not None
        assert nogoods == []
        assert len(gates) == 1
        assert gates[0].event == "channel-override-dead-end"
        assert gates[0].detail == "channel target reached"

    def test_channel_ejected_overrides_dead_end(self, monkeypatch):
        result, gates, nogoods = _empty_frontier_with_channel_motion(
            monkeypatch,
            ChannelMotion("State", 6, stop_reason="departed"),
        )

        assert result is not None
        assert nogoods == []
        assert len(gates) == 1
        assert gates[0].event == "channel-override-dead-end"
        assert gates[0].detail == "channel ejected"


class TestVerifyGates:
    """Full pipeline: target -> spin -> dead-end -> outcome -> revisit."""

    def test_verify_gates_credential_source_includes_installed_prerequisite(
        self,
        monkeypatch,
    ):
        from pyrung.core.analysis.pilot.outcome import (
            Agency,
            BearingEffect,
            ProgressEffect,
            TrialAssessment,
        )

        phase = Int("CredentialPhase", external=True)
        target = Bool("CredentialTarget")
        with Program() as program:
            with Rung(phase == -1):
                out(target)
        plc = PLC(program, dt=0.010)
        before = {**dict(plc.state.tags), phase.name: 1, target.name: False}
        after = {**before, phase.name: 3}
        cfg = _StateKeyConfig(
            stateful_names=(phase.name, target.name),
            done_specs=(),
            threshold_vector_specs=(),
            acc_indices=frozenset(),
        )
        prerequisite = PilotRung(target.name, True, CompareEq(phase, 9))
        source_key = _pilot_world_key(before, cfg, ())
        landing_key = _pilot_world_key(after, cfg, (prerequisite,))
        policy = ActPolicy(
            source=ActSource.ROUTE,
            heading=ChannelHeading(phase.name, 2),
            motion=MotionKind.COAST_TO_BEARING,
        )
        pulse = _PulseState(
            fork=plc,
            scan_before=1,
            action_scan=1,
            action_snap=before,
            wait_snaps=(),
            post_pulse_snap=before,
            post_pulse_key=source_key,
            snap=after,
            key=landing_key,
            kernel_scan_ids=(),
            channel_motion=ChannelMotion(phase.name, 2, stop_reason="departed"),
        )
        frame = _IterationFrame(
            snap=before,
            tree=TraceNode(target.name, True),
            key=source_key,
            distance_before=2,
            raw_trace_actions=(),
            raw_trace_action_details=(),
        )
        state = SimpleNamespace(
            earned_work=None,
            key_config=cfg,
            pilot_rungs=[prerequisite],
            seen_keys=set(),
            consumed_revisits=set(),
        )
        monkeypatch.setattr(
            "pyrung.core.analysis.pilot.verify._gate_spin",
            lambda *_args, **_kwargs: _SpinVerdict.PASS,
        )
        monkeypatch.setattr(
            "pyrung.core.analysis.pilot.verify._gate_dead_end",
            lambda *_args, **_kwargs: SimpleNamespace(
                trend=1,
                has_new_frontier=True,
            ),
        )
        monkeypatch.setattr(
            "pyrung.core.analysis.pilot.verify.assess_outcome",
            lambda *_args, **_kwargs: TrialAssessment(
                Agency.PROGRAM,
                BearingEffect.DEPARTED,
                ProgressEffect.UNCHANGED,
                True,
                True,
            ),
        )

        result = verify_gates(
            _executed(
                pulse,
                Bearing(
                    source_key,
                    Coast("bearing", policy),
                    BearingObjective(TargetSpec(target.name, True)),
                    prerequisites=(prerequisite,),
                ),
            ),
            frame,
            state,
            SimpleNamespace(avoid_pred=None, target=TargetSpec(target.name, True)),
        )

        assert result.trial is not None
        assert isinstance(result.trial.verification, AssessedMotion)
        credential = result.trial.verification.revisit_credentials[0]
        assert credential.source_world == _pilot_world_key(
            before,
            cfg,
            (prerequisite,),
        )
        corrected = PilotRung(target.name, False, CompareEq(phase, 9))
        corrected_credential = replace(
            credential,
            source_world=_pilot_world_key(before, cfg, (corrected,)),
        )
        state.consumed_revisits.add(credential)
        assert corrected_credential not in state.consumed_revisits

    def test_target_reached_records_bearing_target_from_owned_evidence(self):
        from pyrung.core.analysis.pilot.recording import _bearing_coast_accepted_payload

        source = Bool("VerifySource", external=True)
        target = Bool("VerifyTarget")
        with Program() as program:
            with Rung(source):
                out(target)
        plc = PLC(program, dt=0.010)
        before = dict(plc.state.tags)
        before["VerifyStep"] = 1
        after = {**before, target.name: True}
        after["VerifyStep"] = 2
        timeline = (CoastTriggerEvent("recorded", "pen", 4, ()),)
        coast_receipt = CoastReceipt(
            kind="verify",
            start_scan=3,
            end_scan=4,
            stop_reason="reached",
            fired=("target",),
            events=timeline,
            budget=1,
            advances=(("VerifyAccumulator", 9),),
        )
        pulse = _PulseState(
            fork=plc,
            scan_before=3,
            action_scan=4,
            action_snap=after,
            wait_snaps=(),
            post_pulse_snap=after,
            post_pulse_key=("post",),
            snap=after,
            key=("target",),
            kernel_scan_ids=(),
            coast_receipt=coast_receipt,
            timeline=timeline,
        )
        objective = BearingObjective(
            TargetSpec(target.name, True),
            (("CompletionState", 17),),
        )
        policy = ActPolicy(
            source=ActSource.ROUTE,
            action_pairs=((source.name, True),),
            applied=((source.name, True), ("VerifyCoaction", False)),
            heading=ChannelHeading(target.name, True),
            motion=MotionKind.COAST_TO_BEARING,
        )
        bearing = Bearing(("world",), Pulse(policy), objective)

        result = verify_gates(
            _executed(pulse=pulse, bearing=bearing),
            SimpleNamespace(snap=before),
            SimpleNamespace(
                earned_work=EarnedWork((EarnedWorkComponent("VerifyStep", "stepper", 1),))
            ),
            SimpleNamespace(
                avoid_pred=None,
                target=TargetSpec(target.name, True),
            ),
        )

        assert result.trial is not None
        assert result.trial.attempt.pulse is pulse
        assert result.trial.attempt.bearing is bearing
        assert isinstance(result.trial.verification, TargetReached)
        assert result.trial.attempt.pulse.fork is pulse.fork
        assert result.trial.attempt.bearing.objective is objective
        assert result.trial.attempt.bearing.act.policy is policy
        assert result.trial.execution.channel_motion.channel_tag == policy.heading.channel_tag
        assert result.trial.execution.channel_motion.target_value is True
        assert result.trial.execution.channel_motion.reached
        assert result.trial.execution.timeline == pulse.timeline
        assert result.trial.execution.coast_receipt is coast_receipt
        assert result.trial.execution.accelerators == (("VerifyAccumulator", 9),)
        assert (
            _bearing_coast_accepted_payload(result.trial)["observe_label"] == "bearing_coast-target"
        )
        assert result.trial.execution.before_snap is not before
        assert result.trial.execution.after_snap is not after
        assert isinstance(result.trial.execution.before_snap, MappingProxyType)
        assert isinstance(result.trial.execution.after_snap, MappingProxyType)
        assert not any(isinstance(value, PLC) for value in vars(result.trial.execution).values())
        assert result.trial.earned_work_receipt.any_forward
        assert result.trial.earned_work_receipt.source_mark == (("VerifyStep", 1),)
        assert result.trial.earned_work_receipt.landing_mark == (("VerifyStep", 2),)
        before["LateSourceMutation"] = True
        after["LateLandingMutation"] = True
        assert "LateSourceMutation" not in result.trial.execution.before_snap
        assert "LateLandingMutation" not in result.trial.execution.after_snap

    def test_assessed_motion_requires_an_accepted_assessment(self):
        from pyrung.core.analysis.pilot.outcome import (
            Agency,
            BearingEffect,
            ProgressEffect,
            TrialAssessment,
        )

        with pytest.raises(ValueError, match="requires an accepted assessment"):
            AssessedMotion(
                new_key=("rejected",),
                trend=3,
                assessment=TrialAssessment(
                    agency=Agency.PILOT,
                    bearing=BearingEffect.DEPARTED,
                    progress=ProgressEffect.BACKWARD,
                    new_frontier=False,
                    accepted=False,
                ),
            )

    def test_excursion_replay_owns_correction_timeline_and_new_earned_work_receipt(self):
        source = Bool("ReplayReceiptSource", external=True)
        target = Bool("ReplayReceiptTarget")
        step = Int("ReplayReceiptStep", external=True)
        with Program() as program:
            with Rung(step == -999):
                out(target)
            with Rung(source):
                out(target)
        plc = PLC(program, dt=0.010)
        plc.patch({step.name: 1})
        before = dict(plc.state.tags)
        pre_replay = {**before, step.name: 2}
        cfg = _StateKeyConfig(
            stateful_names=(target.name,),
            done_specs=(),
            threshold_vector_specs=(),
            acc_indices=frozenset(),
        )
        frame_key = _pilot_world_key(before, cfg, ())
        pulse = _PulseState(
            fork=plc,
            scan_before=3,
            action_scan=4,
            action_snap=pre_replay,
            wait_snaps=(),
            post_pulse_snap=pre_replay,
            post_pulse_key=("post",),
            snap=pre_replay,
            key=frame_key,
            kernel_scan_ids=(),
        )
        policy = ActPolicy(
            source=ActSource.TRACE,
            action_pairs=((source.name, True),),
            applied=((source.name, True),),
        )
        bearing = Bearing(
            frame_key,
            Pulse(policy),
            BearingObjective(TargetSpec(target.name, True)),
        )
        earned_work = EarnedWork((EarnedWorkComponent(step.name, "stepper", 1),))
        receipts = []

        class _CountingEarnedWork:
            def receipt(self, source_snap, landing_snap):
                receipt = earned_work.receipt(source_snap, landing_snap)
                receipts.append(receipt)
                return receipt

        state = SimpleNamespace(
            earned_work=_CountingEarnedWork(),
            key_config=cfg,
            pilot_rungs=[],
        )
        frame = SimpleNamespace(key=frame_key, snap=before)
        detected = _AttemptResult(
            trial=None,
            excursion_attempt=_executed(pulse=pulse, bearing=bearing),
        )

        replay = plc.fork()
        replay.patch({source.name: True, step.name: 3})
        replay.step()
        timeline = (CoastTriggerEvent("replay", "pen", 5, ()),)
        rung = PilotRung(source.name, True, CompareEq(target, True))
        correction = _ConfirmedCorrection(
            identity=correction_identity((rung,)),
            pilot_rungs=(rung,),
            sources=(target.name, source.name),
            justification="excursion replay",
        )
        result = verify_excursion_replay(
            detected,
            ExcursionResult(
                reverted=[target.name],
                correction=correction,
                replay_fork=replay,
                replay_timeline=timeline,
            ),
            frame,
            state,
            SimpleNamespace(
                avoid_pred=None,
                target=TargetSpec(target.name, True),
            ),
        )

        assert result.trial is not None
        assert result.trial.attempt.pulse.fork is replay
        assert result.trial.execution.after_snap == dict(replay.state.tags)
        assert result.trial.execution.timeline == timeline
        assert result.trial.execution.timeline != pulse.timeline
        assert result.confirmed_correction is correction
        assert result.trial.attempt.pulse.key == _pilot_world_key(
            dict(replay.state.tags),
            cfg,
            (rung,),
        )
        assert result.trial.attempt.pulse.key != _pilot_world_key(
            dict(replay.state.tags),
            cfg,
            (),
        )
        assert len(receipts) == 1
        assert result.trial.earned_work_receipt is receipts[0]
        assert result.trial.earned_work_receipt.landing_mark == ((step.name, 3),)

    def test_intervention_cannot_erase_banked_earned_work(self):
        before = {"Step": 3, "Target": False}
        # Even manufacturing the requested Target value does not pardon a
        # reset-to-floor intervention.
        after = {"Step": 0, "Target": True}
        pulse = SimpleNamespace(
            snap=after,
            coast_receipt=None,
            action_snap=after,
            wait_snaps=(),
            post_pulse_snap=after,
            confirmed_correction=None,
            channel_motion=ChannelMotion(),
        )
        objective = BearingObjective(TargetSpec("Target", True))
        policy = ActPolicy(
            source=ActSource.TRACE,
            action_pairs=(("Reset", True),),
            applied=(("Reset", True),),
            nogood_pair=("Reset", True),
        )
        bearing = Bearing(("world",), Pulse(policy), objective)

        result = verify_gates(
            _ExecutedAttempt(pulse=pulse, bearing=bearing),
            SimpleNamespace(snap=before),
            SimpleNamespace(earned_work=EarnedWork((EarnedWorkComponent("Step", "stepper", 1),))),
            SimpleNamespace(
                avoid_pred=None,
                target=TargetSpec("Target", True),
            ),
        )

        assert result.trial is None
        assert result.nogood_pairs == frozenset({("Reset", True)})
        assert result.gate_events[-1].event == "banked-work"
        assert result.gate_events[-1].evidence["effect"] == "backward"

    def test_banked_batch_rejects_exact_overlay_without_poisoning_members(self):
        before = {"Step": 3, "Target": False}
        after = {"Step": 0, "Target": True}
        pulse = SimpleNamespace(
            snap=after,
            coast_receipt=None,
            action_snap=after,
            wait_snaps=(),
            post_pulse_snap=after,
            confirmed_correction=None,
            channel_motion=ChannelMotion(),
        )
        actions = (("Reset", True), ("ResetGate", True))
        objective = BearingObjective(TargetSpec("Target", True))
        policy = ActPolicy(
            source=ActSource.WIDENING,
            action_pairs=actions,
            applied=actions,
        )
        bearing = Bearing(("world",), BatchPulse(policy), objective)

        result = verify_gates(
            _ExecutedAttempt(pulse=pulse, bearing=bearing),
            SimpleNamespace(snap=before),
            SimpleNamespace(earned_work=EarnedWork((EarnedWorkComponent("Step", "stepper", 1),))),
            SimpleNamespace(
                avoid_pred=None,
                target=TargetSpec("Target", True),
            ),
        )

        assert result.trial is None
        assert result.gate_events[-1].event == "banked-work"
        assert result.nogood_pairs == frozenset()
