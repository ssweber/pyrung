"""Tests for pilot verify — gate pipeline for trial acceptance.

Coverage targets:
- verify_gates: the full gate sequence (avoid → target → spin → cycle → dead-end → outcome)
- _gate_spin: state-key change detection, excursion retry
- _gate_cycle: visited-key rejection, influence override
- _gate_dead_end: empty frontier, lateral detection, channel override
"""

from __future__ import annotations

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
    EarnedWorkReceipt,
)
from pyrung.core.analysis.pilot.investigate import ExcursionResult, correction_identity
from pyrung.core.analysis.pilot.navigation_contracts import (
    ActPolicy,
    ActSource,
    BatchPulse,
    Bearing,
    BearingObjective,
    ChannelHeading,
    Pulse,
    TargetSpec,
)
from pyrung.core.analysis.pilot.overlay import PilotRung
from pyrung.core.analysis.pilot.physical import install_harness
from pyrung.core.analysis.pilot.trace import TraceNode
from pyrung.core.analysis.pilot.types import (
    AssessedMotion,
    ChannelMotion,
    MotionKind,
    TargetReached,
    _AttemptResult,
    _ConfirmedCorrection,
    _ExecutedAttempt,
    _IterationFrame,
    _PulseState,
)
from pyrung.core.analysis.pilot.verify import (
    _gate_cycle,
    _gate_dead_end,
    _gate_spin,
    _owned_channel_motion,
    _SpinVerdict,
    verify_excursion_retry,
    verify_gates,
)
from pyrung.core.analysis.pilot.world_key import _pilot_world_key, _StateKeyConfig
from pyrung.core.condition import CompareEq
from pyrung.core.physical import Physical, Ramp
from pyrung.core.runner import PLC

# ---------------------------------------------------------------------------
# Gate pipeline
# ---------------------------------------------------------------------------


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
        trial = _PulseState(plc, 0, 0, snap, (), snap, key, snap, key)
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
        )
        executed = _ExecutedAttempt(pulse=trial, bearing=bearing)

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

    def test_excursion_retry_is_rechecked_against_avoid_history(self):
        source = Bool("AvoidRetrySource", external=True)
        hazard = Bool("AvoidRetryHazard")
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
            rungs=(rung,),
            sources=(hazard.name, source.name),
            justification="unsafe excursion replay",
        )
        retry = plc.fork()
        retry.patch({source.name: True})
        retry.step()
        assert retry.state.tags[hazard.name] is True
        retry.patch({source.name: False})
        retry.step()
        assert retry.state.tags[hazard.name] is False
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
            excursion_attempt=_ExecutedAttempt(pulse=trial, bearing=bearing),
            observations=(observation,),
        )

        result = verify_excursion_retry(
            detected,
            ExcursionResult(
                reverted=[hazard.name],
                correction=correction,
                retry_fork=retry,
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

    @pytest.mark.skip(reason="stub")
    def test_pending_effects_bypass_spin(self): ...


class TestGateCycle:
    """Cycle gate — new key must not have been visited."""

    def test_visited_key_rejected(self):
        key = ("visited",)
        trial = SimpleNamespace(key=key, snap={})
        gates = []
        accepted = _gate_cycle(
            trial,
            SimpleNamespace(snap={}),
            SimpleNamespace(seen_keys={key}, earned_work=None),
            pending=False,
            earned_work_receipt=EarnedWorkReceipt(),
            learned_prescribed=False,
            nogood_pair=("Cmd", True),
            gate_events=gates,
            collected_nogoods=[],
        )
        assert accepted is False
        assert gates[-1].event == "cycle"
        assert gates[-1].evidence["trial_key"] == key
        assert gates[-1].evidence["seen"] is True
        assert gates[-1].evidence["learned_prescribed"] is False

    def test_learned_prescribed_overrides_cycle(self):
        key = ("visited",)
        trial = SimpleNamespace(key=key, snap={})
        gates = []
        collected_nogoods = []

        accepted = _gate_cycle(
            trial,
            SimpleNamespace(snap={}),
            SimpleNamespace(seen_keys={key}, earned_work=None),
            pending=False,
            earned_work_receipt=EarnedWorkReceipt(),
            learned_prescribed=True,
            nogood_pair=("Cmd", True),
            gate_events=gates,
            collected_nogoods=collected_nogoods,
        )

        assert accepted is True
        assert collected_nogoods == []
        assert len(gates) == 1
        assert gates[0].event == "learned-override-cycle"
        assert gates[0].detail == "learned-prescribed"


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
            learned_prescribed=False,
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
                learned_prescribed=False,
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

    @pytest.mark.skip(reason="stub")
    def test_empty_frontier_is_dead_end(self): ...

    @pytest.mark.skip(reason="stub")
    def test_channel_reached_overrides_dead_end(self): ...

    @pytest.mark.skip(reason="stub")
    def test_channel_ejected_overrides_dead_end(self): ...

    @pytest.mark.skip(reason="stub")
    def test_lateral_no_new_frontier_rejected(self): ...


class TestVerifyGates:
    """Full pipeline: target check -> spin -> cycle -> dead-end -> outcome."""

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
            _ExecutedAttempt(pulse=pulse, bearing=bearing),
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

    def test_excursion_retry_owns_correction_timeline_and_new_earned_work_receipt(self):
        source = Bool("RetryReceiptSource", external=True)
        target = Bool("RetryReceiptTarget")
        step = Int("RetryReceiptStep", external=True)
        with Program() as program:
            with Rung(step == -999):
                out(target)
            with Rung(source):
                out(target)
        plc = PLC(program, dt=0.010)
        plc.patch({step.name: 1})
        before = dict(plc.state.tags)
        pre_retry = {**before, step.name: 2}
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
            action_snap=pre_retry,
            wait_snaps=(),
            post_pulse_snap=pre_retry,
            post_pulse_key=("post",),
            snap=pre_retry,
            key=frame_key,
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
            excursion_attempt=_ExecutedAttempt(pulse=pulse, bearing=bearing),
        )

        retry = plc.fork()
        retry.patch({source.name: True, step.name: 3})
        retry.step()
        timeline = (CoastTriggerEvent("retry", "pen", 5, ()),)
        rung = PilotRung(source.name, True, CompareEq(target, True))
        correction = _ConfirmedCorrection(
            identity=correction_identity((rung,)),
            rungs=(rung,),
            sources=(target.name, source.name),
            justification="excursion replay",
        )
        result = verify_excursion_retry(
            detected,
            ExcursionResult(
                reverted=[target.name],
                correction=correction,
                retry_fork=retry,
                retry_timeline=timeline,
            ),
            frame,
            state,
            SimpleNamespace(
                avoid_pred=None,
                target=TargetSpec(target.name, True),
            ),
        )

        assert result.trial is not None
        assert result.trial.attempt.pulse.fork is retry
        assert result.trial.execution.after_snap == dict(retry.state.tags)
        assert result.trial.execution.timeline == timeline
        assert result.trial.execution.timeline != pulse.timeline
        assert result.confirmed_correction is correction
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

    def test_banked_batch_nogoods_every_regressive_action(self):
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
        assert result.nogood_pairs == frozenset(actions)

    @pytest.mark.skip(reason="stub")
    def test_avoid_predicate_rejects(self): ...

    @pytest.mark.skip(reason="stub")
    def test_bearing_coast_result_routes_through_gates(self): ...
