"""Tests for pilot verify — gate pipeline for trial acceptance.

Coverage targets:
- verify_gates: the full gate sequence (avoid → target → spin → cycle → dead-end → outcome)
- _gate_spin: state-key change detection, excursion retry
- _gate_cycle: visited-key rejection, influence override
- _gate_dead_end: empty frontier, lateral detection, channel override
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from pyrung import Bool, Program, Rung, out
from pyrung.core.analysis.pilot._ops import (
    PilotRung,
    _pilot_world_key,
    _set_rungs,
    _StateKeyConfig,
)
from pyrung.core.analysis.pilot.gauge import Gauge, GaugeComponent
from pyrung.core.analysis.pilot.investigate import ExcursionResult, correction_identity
from pyrung.core.analysis.pilot.navigation import BearingObjective, TargetSpec
from pyrung.core.analysis.pilot.types import (
    MotionKind,
    _AttemptIntent,
    _ConfirmedCorrection,
    _ExecutedAttempt,
    _PulseState,
)
from pyrung.core.analysis.pilot.verify import (
    _gate_cycle,
    _gate_spin,
    _owned_bearing_stop_reason,
    verify_gates,
)
from pyrung.core.condition import CompareEq
from pyrung.core.runner import PLC

# ---------------------------------------------------------------------------
# Gate pipeline
# ---------------------------------------------------------------------------


def test_outer_owner_rebases_inner_departure_receipt_to_reached():
    trial = SimpleNamespace(
        snap={"State": 6},
        coast_receipt=SimpleNamespace(stop_reason="departed"),
    )

    assert _owned_bearing_stop_reason(trial, "State", 6) == "reached"


def test_relational_owner_retains_its_crossing_receipt_after_overshoot():
    trial = SimpleNamespace(
        snap={"Acc": 5},
        coast_receipt=SimpleNamespace(stop_reason="reached"),
    )

    assert _owned_bearing_stop_reason(trial, "Acc", 4) == "reached"


def test_wrong_outer_landing_retains_departure_receipt():
    trial = SimpleNamespace(
        snap={"State": 8},
        coast_receipt=SimpleNamespace(stop_reason="departed"),
    )

    assert _owned_bearing_stop_reason(trial, "State", 6) == "departed"


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
        result = _gate_spin(
            trial,
            (("SpinSource", False),),
            SimpleNamespace(key=key, snap=snap),
            SimpleNamespace(key_config=object(), gauge=None, work=plc, rungs=[]),
            SimpleNamespace(),
            nogood_pair=("SpinSource", False),
            gate_events=gates,
            collected_nogoods=[],
            avoid_names=[],
        )
        assert result is None
        assert gates[-1].event == "spin"
        assert gates[-1].evidence == {
            "frame_key": key,
            "trial_key": key,
            "post_pulse_key": key,
            "pending_effects": False,
            "ordinal_advanced": False,
            "actions": (("SpinSource", False),),
        }

    def test_excursion_retried_with_exact_correction(self, monkeypatch):
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
        guard = CompareEq(dest, True)
        rung = PilotRung(source.name, False, guard)
        correction = _ConfirmedCorrection(
            identity=correction_identity((rung,)),
            rungs=(rung,),
            sources=(dest.name, source.name),
            justification="excursion replay",
        )
        retry = plc.fork()
        _set_rungs(retry, correction.rungs)
        retry.step()
        monkeypatch.setattr(
            "pyrung.core.analysis.pilot.verify.investigate_excursion",
            lambda *_args, **_kwargs: ExcursionResult(
                reverted=[dest.name],
                correction=correction,
                retry_fork=retry,
            ),
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
        result = _gate_spin(
            trial,
            ((source.name, True),),
            SimpleNamespace(key=frame_key, snap=snap),
            SimpleNamespace(key_config=cfg, gauge=None, work=plc, rungs=[]),
            SimpleNamespace(
                steerable=frozenset((source.name,)),
                resting={source.name: False},
                edge_tags=set(),
                max_scans=50,
                pdg=None,
                program=program,
                avoid_pred=None,
            ),
            nogood_pair=(source.name, True),
            gate_events=[],
            collected_nogoods=[],
            avoid_names=[],
        )

        assert result is not None
        assert result.fork is retry
        assert result.confirmed_correction is correction
        assert result.key == _pilot_world_key(result.snap, cfg, correction.rungs)

    def test_excursion_retry_is_rechecked_against_avoid(self, monkeypatch):
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
        monkeypatch.setattr(
            "pyrung.core.analysis.pilot.verify.investigate_excursion",
            lambda *_args, **_kwargs: ExcursionResult(
                reverted=[hazard.name],
                correction=correction,
                retry_fork=retry,
            ),
        )
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
        avoid_names: list[str] = []

        result = _gate_spin(
            trial,
            ((source.name, True),),
            SimpleNamespace(key=frame_key, snap=snap),
            SimpleNamespace(key_config=cfg, gauge=None, work=plc, rungs=[]),
            SimpleNamespace(
                steerable=frozenset((source.name,)),
                resting={source.name: False},
                edge_tags=set(),
                max_scans=50,
                pdg=None,
                program=program,
                avoid_pred=lambda state: bool(state.get(hazard.name)),
            ),
            nogood_pair=(source.name, True),
            gate_events=[],
            collected_nogoods=[],
            avoid_names=avoid_names,
        )

        assert result is None
        assert avoid_names == ["avoided condition"]

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
            SimpleNamespace(seen_keys={key}, gauge=None),
            pending=False,
            influence_prescribed=False,
            nogood_pair=("Cmd", True),
            gate_events=gates,
            collected_nogoods=[],
        )
        assert accepted is False
        assert gates[-1].event == "cycle"
        assert gates[-1].evidence["trial_key"] == key
        assert gates[-1].evidence["seen"] is True
        assert gates[-1].evidence["influence_prescribed"] is False

    @pytest.mark.skip(reason="stub")
    def test_influence_prescribed_overrides_cycle(self): ...


class TestGateDeadEnd:
    """Dead-end gate — frontier must be non-empty or async pending."""

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

    def test_target_reached_preserves_executed_attempt(self):
        source = Bool("VerifySource", external=True)
        target = Bool("VerifyTarget")
        with Program() as program:
            with Rung(source):
                out(target)
        plc = PLC(program, dt=0.010)
        before = dict(plc.state.tags)
        after = {**before, target.name: True}
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
            timeline=("recorded-event",),
        )
        objective = BearingObjective(
            TargetSpec(target.name, True),
            (("CompletionState", 17),),
        )
        intent = _AttemptIntent(
            bearing_objective=objective,
            action_pairs=((source.name, True),),
            applied=((source.name, True), ("VerifyCoaction", False)),
            target_observe_label="bearing-target",
            route_prescribed=True,
            regression_nogoods=frozenset({(source.name, True)}),
            chase_regression_causes=False,
            channel_tag=target.name,
            channel_target=True,
            motion=MotionKind.COAST_TO_BEARING,
        )

        result = verify_gates(
            _ExecutedAttempt(pulse=pulse, intent=intent),
            SimpleNamespace(snap=before),
            SimpleNamespace(),
            SimpleNamespace(
                avoid_pred=None,
                target_tag=target.name,
                target_value=True,
                target_predicate=None,
            ),
        )

        assert result.trial is not None
        assert result.trial.fork is pulse.fork
        assert result.trial.candidate == dict(intent.action_pairs)
        assert result.trial.applied == intent.applied
        assert result.trial.observe_label == intent.target_observe_label
        assert result.trial.bearing_objective is objective
        assert result.trial.route_prescribed is True
        assert result.trial.regression_nogoods == intent.regression_nogoods
        assert result.trial.chase_regression_causes is False
        assert result.trial.zoom_channel_tag == intent.channel_tag
        assert result.trial.zoom_target_value is True
        assert result.trial.motion is MotionKind.COAST_TO_BEARING
        assert result.trial.timeline == pulse.timeline

    def test_intervention_cannot_erase_banked_gauge_work(self):
        before = {"Step": 3, "Target": False}
        after = {"Step": 0, "Target": False}
        pulse = SimpleNamespace(
            snap=after,
            coast_receipt=None,
            action_snap=after,
            wait_snaps=(),
            post_pulse_snap=after,
        )
        intent = _AttemptIntent(
            bearing_objective=BearingObjective(TargetSpec("Target", True)),
            action_pairs=(("Reset", True),),
            applied=(("Reset", True),),
            nogood_pair=("Reset", True),
        )

        result = verify_gates(
            _ExecutedAttempt(pulse=pulse, intent=intent),
            SimpleNamespace(snap=before),
            SimpleNamespace(gauge=Gauge((GaugeComponent("Step", "stepper", 1),))),
            SimpleNamespace(
                avoid_pred=None,
                target_tag="Target",
                target_value=True,
                target_predicate=None,
            ),
        )

        assert result.trial is None
        assert result.nogood_pairs == frozenset({("Reset", True)})
        assert result.gate_events[-1].event == "banked-work"
        assert result.gate_events[-1].evidence["effect"] == "behind"

    @pytest.mark.skip(reason="stub")
    def test_avoid_predicate_rejects(self): ...

    @pytest.mark.skip(reason="stub")
    def test_zoom_result_routes_through_gates(self): ...
