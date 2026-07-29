"""Regression knowledge belongs to the world where its action was tried."""

from __future__ import annotations

from types import SimpleNamespace

from pyrung.core.analysis.pilot import progress, recording
from pyrung.core.analysis.pilot.navigation_contracts import (
    ActPolicy,
    ActSource,
    BatchPulse,
    Bearing,
    BearingObjective,
    TargetSpec,
)
from pyrung.core.analysis.pilot.outcome import (
    Agency,
    BearingEffect,
    ProgressEffect,
    TrialAssessment,
)
from pyrung.core.analysis.pilot.types import (
    AssessedMotion,
    ChannelMotion,
    _AcceptedTrial,
    _ExecutedAttempt,
    _ExecutionEvidence,
    _PulseState,
    _RecoveryOrigin,
)


class _RecordingCompass:
    def __init__(self) -> None:
        self.observations = []

    def apply(self, observations):
        self.observations.extend(observations)
        return self, bool(observations)


class _RecoveryState:
    def __init__(self, checkpoint) -> None:
        self.checkpoints = [checkpoint]
        self.pending_departure = None
        self.overlay_rules = ()
        self.best_trend = checkpoint.trend
        self.work = checkpoint.world.work

    def load_world(self, world) -> None:
        self.work = world.work
        self.overlay_rules = world.overlay_rules


def test_regression_nogood_uses_action_source_world(monkeypatch) -> None:
    """A rollback destination does not own the failed action's identity."""
    source_key = ("action-source",)
    rollback_key = ("rollback-destination",)
    owner = object()
    checkpoint_work = SimpleNamespace(state=SimpleNamespace(scan_id=10, tags={}))
    checkpoint = SimpleNamespace(
        owner=owner,
        key=rollback_key,
        world=SimpleNamespace(work=checkpoint_work, overlay_rules=()),
        trend=3,
    )
    state = _RecoveryState(checkpoint)
    compass = _RecordingCompass()
    ctx = SimpleNamespace(compass=compass)
    frame = SimpleNamespace(key=source_key)
    policy = ActPolicy(
        source=ActSource.WIDENING,
        action_pairs=(("Action", True),),
        applied=(("Action", True),),
    )
    pulse = _PulseState(
        fork=checkpoint_work,
        scan_before=10,
        action_scan=10,
        action_snap={},
        wait_snaps=(),
        post_pulse_snap={},
        post_pulse_key=("post-pulse",),
        snap={},
        key=("landing",),
    )
    trial = _AcceptedTrial(
        attempt=_ExecutedAttempt(
            pulse=pulse,
            bearing=Bearing(
                world_key=source_key,
                act=BatchPulse(policy),
                objective=BearingObjective(TargetSpec("Target", True)),
            ),
        ),
        execution=_ExecutionEvidence({}, {}, ChannelMotion(), None, ()),
        verification=AssessedMotion(
            new_key=pulse.key,
            trend=4,
            assessment=TrialAssessment(
                agency=Agency.PILOT,
                bearing=BearingEffect.DEPARTED,
                progress=ProgressEffect.BEHIND,
                new_frontier=False,
                accepted=True,
            ),
        ),
    )
    origin = _RecoveryOrigin(
        checkpoint_owner=owner,
        anchor_scan=10,
        before_snap={},
    )
    monkeypatch.setattr(recording, "_channel_transitions", lambda *_args: ())
    monkeypatch.setattr(progress, "_revoke_corrections", lambda *_args: ())

    progress._investigate_and_revert(
        trial,
        frame,
        state,
        ctx,
        origin=origin,
    )

    assert [observation.world_key for observation in compass.observations] == [source_key]
