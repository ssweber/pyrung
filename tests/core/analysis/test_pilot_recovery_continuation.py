"""Focused contracts for repaired program continuation certification."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

from pyrung import PLC, Int, Program, copy, rung
from pyrung.core.analysis.pilot import pilot as pilot_module
from pyrung.core.analysis.pilot import recovery_continuation
from pyrung.core.analysis.pilot.effects import EffectExpectation
from pyrung.core.analysis.pilot.execution import MotionKind
from pyrung.core.analysis.pilot.navigation_contracts import (
    ActPolicy,
    ActSource,
    Bearing,
    BearingObjective,
    Coast,
    ExpectationExemption,
    NavigationConstraints,
    OrientationRead,
    OrientationWorld,
    Pulse,
    TargetSpec,
)
from pyrung.core.analysis.pilot.program_step import ProgramStep, ProgramStepStatus
from pyrung.core.analysis.pilot.types import (
    _AttemptResult,
    _ContinuationCheckpoint,
    _ExecutedAttempt,
    _PulseState,
    _RecoveryContinuation,
)
from pyrung.core.runner import EpochRef


@dataclass(frozen=True)
class _ContextStub:
    """Minimal immutable context honoring ``_transition_once``'s route seam."""

    configured_inputs: frozenset[str] = frozenset()
    route: object | None = None


def test_multi_scan_recovery_coast_adds_one_checkpoint_at_exact_last_landing(
    monkeypatch,
) -> None:
    channel = Int("ContinuationMultiScanChannel")
    command = Int("ContinuationMultiScanCommand", external=True)
    with Program() as program:
        with rung(command == 1):
            copy(1, channel)
        with rung(command == 2):
            copy(3, channel)
        with rung(command == 2):
            copy(3, channel)

    plc = PLC(program)
    for value in (1, 0, 2):
        plc.patch({command.name: value})
        plc.step()
    assert all(plc._replay_rung_write_projection_at(scan_id) is not None for scan_id in (1, 2, 3))

    epoch_ref = EpochRef(10_001)
    epoch = SimpleNamespace(reference=epoch_ref)
    owner = object()
    source_key = ("repaired-source",)
    continuation = _RecoveryContinuation(
        checkpoint_owner=object(),
        source_world_key=source_key,
        checkpoints=(
            _ContinuationCheckpoint(
                scan_id=0,
                world_key=source_key,
                kind="local_repair",
                execution_ref=epoch_ref,
            ),
        ),
    )
    pulse = _PulseState(
        fork=plc,
        scan_before=0,
        action_scan=0,
        action_snap={channel.name: 0},
        wait_snaps=(),
        post_pulse_snap={channel.name: 0},
        post_pulse_key=("post",),
        snap=dict(plc.state.tags),
        key=("landing",),
        kernel_scan_ids=(1, 2, 3),
        coast_receipt=None,
    )
    trial = SimpleNamespace(
        attempt=SimpleNamespace(
            pulse=pulse,
            bearing=SimpleNamespace(act=Coast("bearing", None)),
        ),
        execution=SimpleNamespace(
            channel_motion=SimpleNamespace(channel_tag=channel.name),
            before_snap={channel.name: 0},
            after_snap={channel.name: 3},
        ),
    )
    state = SimpleNamespace(
        recovery_continuation=continuation,
        work=plc,
        key_config=object(),
        pilot_rungs=(),
        active_requirements=[],
    )
    step = ProgramStep(
        ProgramStepStatus.KEEP_RUNNING,
        producer=object(),
        boundary=None,
        channel=channel.name,
        projected_changes=((channel.name, 0, 3),),
    )
    monkeypatch.setattr(
        recovery_continuation,
        "execution_epoch_owner",
        lambda work, scan_id: (epoch, owner),
    )
    monkeypatch.setattr(
        recovery_continuation,
        "_pilot_world_key",
        lambda tags, key_config, pilot_rungs, active_requirements: ("landing",),
    )

    certified = recovery_continuation.advance_recovery_continuation(
        trial,
        SimpleNamespace(key=source_key),
        state,
        SimpleNamespace(),
        step,
    )

    assert certified is True
    assert len(state.recovery_continuation.checkpoints) == 2
    landing = state.recovery_continuation.tip
    assert landing.scan_id == 3
    assert landing.world_key == ("landing",)
    assert landing.landing_occurrence is not None
    assert landing.landing_occurrence.scan_id == 3
    assert landing.landing_occurrence.rung == (None, 2)


def test_multi_scan_recovery_window_retains_exact_causal_source(monkeypatch) -> None:
    epoch_ref = EpochRef(10_002)
    epoch = SimpleNamespace(reference=epoch_ref)
    owner = object()
    checkpoint_owner = object()
    source_checkpoint = object()
    source_key = ("source",)
    continuation = _RecoveryContinuation(
        checkpoint_owner=checkpoint_owner,
        source_world_key=source_key,
        checkpoints=(
            _ContinuationCheckpoint(
                scan_id=0,
                world_key=source_key,
                kind="unchanged_coast",
                execution_ref=epoch_ref,
            ),
        ),
    )
    state = SimpleNamespace(
        recovery_continuation=continuation,
        work=SimpleNamespace(state=SimpleNamespace(tags={})),
        key_config=object(),
        pilot_rungs=(),
        active_requirements=[
            SimpleNamespace(
                checkpoint_owner=checkpoint_owner,
                source_world_key=source_key,
                source_checkpoint=source_checkpoint,
            )
        ],
    )
    pulse = SimpleNamespace(
        scan_before=0,
        kernel_scan_ids=(1, 2, 3),
        fork=SimpleNamespace(state=SimpleNamespace(scan_id=3)),
        coast_receipt=None,
        projection_at=lambda scan_id: object(),
    )
    monkeypatch.setattr(
        recovery_continuation,
        "_pilot_world_key",
        lambda tags, key_config, pilot_rungs, active_requirements: source_key,
    )
    monkeypatch.setattr(
        recovery_continuation,
        "execution_epoch_owner",
        lambda work, scan_id: (epoch, owner),
    )

    assert recovery_continuation.adjacent_continuation_source(state, pulse) is source_checkpoint

    pulse.projection_at = lambda scan_id: None if scan_id == 2 else object()
    assert recovery_continuation.adjacent_continuation_source(state, pulse) is None


def test_recovery_program_coast_keeps_the_full_execution_window(monkeypatch) -> None:
    """ProgramStep selects the coast; it does not narrow its evidence window."""

    frame = SimpleNamespace(key=("source",), snap={})
    target = TargetSpec("RecoveryWindowTarget", True)
    objective = BearingObjective(target)
    orientation_world = OrientationWorld(
        world_key=frame.key,
        snapshot=frame.snap,
        frame=frame,
        state=None,
        context=None,
    )
    orientation = OrientationRead(
        world_key=frame.key,
        world=orientation_world,
        candidates=SimpleNamespace(),
    )
    policy = ActPolicy(
        source=ActSource.PROGRAM,
        motion=MotionKind.COAST_TO_BEARING,
        expectation_exemption=ExpectationExemption.UNRESOLVED_EFFECT,
    )
    bearing = Bearing(
        frame.key,
        Coast("bearing", policy),
        objective,
        orientation=orientation,
    )
    pulse = SimpleNamespace(kernel_scan_ids=(1, 2, 3))
    attempt = _AttemptResult(
        trial=None,
        executed=_ExecutedAttempt(pulse=pulse, bearing=bearing),
        proof_rejection=True,
    )
    work = SimpleNamespace(
        state=SimpleNamespace(tags={}),
        _input_overrides=None,
    )
    source_world = SimpleNamespace(work=work, pilot_rungs=())
    state = SimpleNamespace(
        active_requirements=(),
        key_config=None,
        pilot_rungs=(),
        proof_rejected_acts=set(),
        work=work,
        snapshot_world=lambda: source_world,
    )
    context = _ContextStub()
    program_step = object()

    monkeypatch.setattr(pilot_module, "assert_recovery_disposable_state", lambda *args: None)
    monkeypatch.setattr(pilot_module, "_prepare_oriented_result", lambda *args: None)
    monkeypatch.setattr(
        recovery_continuation,
        "preempt_recovery_action_with_program_coast",
        lambda *args: (bearing, program_step),
    )
    monkeypatch.setattr(
        pilot_module,
        "_selected_terminal_target_expectation",
        lambda *args: None,
    )
    monkeypatch.setattr(pilot_module, "execute", lambda *args, **kwargs: attempt)
    monkeypatch.setattr(pilot_module, "_record_attempt", lambda *args: None)

    transition = pilot_module._transition_once(
        state,
        context,
        target,
        NavigationConstraints(),
        oriented=bearing,
        resolve_excursion=False,
        derive_requirements=False,
    )

    assert transition.attempt is attempt
    assert pulse.kernel_scan_ids == (1, 2, 3)


def test_recovery_program_coast_carries_the_selected_target_expectation(
    monkeypatch,
) -> None:
    """The autonomous hop remains one expectation-bearing local act."""

    frame = SimpleNamespace(key=("source",), snap={})
    target = TargetSpec("RecoveryExpectedTarget", 81)
    objective = BearingObjective(target)
    orientation_world = OrientationWorld(
        world_key=frame.key,
        snapshot=frame.snap,
        frame=frame,
        state=None,
        context=None,
    )
    orientation = OrientationRead(
        world_key=frame.key,
        world=orientation_world,
        candidates=SimpleNamespace(),
    )
    original = Bearing(
        frame.key,
        Pulse(
            ActPolicy(
                source=ActSource.TRACE,
                motion=MotionKind.INTERVENTION,
                expectation_exemption=ExpectationExemption.UNRESOLVED_EFFECT,
            ),
        ),
        objective,
        orientation=orientation,
    )
    expectation = EffectExpectation((object(),))
    motion = SimpleNamespace(channel_tag="RecoveryChannel", target_value=61)
    step = SimpleNamespace(
        reason="continue exact recovery producer",
        boundary=("RecoveryChannel", 61),
        observable_motion=lambda: motion,
    )
    monkeypatch.setattr(
        recovery_continuation,
        "recovery_anchor_program_step",
        lambda *args: step,
    )
    monkeypatch.setattr(
        recovery_continuation,
        "_selected_terminal_target_expectation",
        lambda *args: expectation,
    )

    result, selected = recovery_continuation.preempt_recovery_action_with_program_coast(
        original,
        frame,
        SimpleNamespace(),
        SimpleNamespace(),
        target,
    )

    assert selected is step
    assert isinstance(result, Bearing)
    assert isinstance(result.act, Coast)
    assert result.act.policy.expectation is expectation
    assert result.act.policy.expectation_exemption is None
    assert result.act.policy.source is ActSource.PROGRAM
    assert result.act.policy.applied == ()
