"""Regression knowledge belongs to the world where its action was tried."""

from __future__ import annotations

from types import SimpleNamespace

from pyrung.core.analysis.pilot import progress
from pyrung.core.analysis.pilot.types import _RecoveryOrigin


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
        self.rungs = ()
        self.best_trend = checkpoint.trend
        self.work = checkpoint.world.work

    def load_world(self, world) -> None:
        self.work = world.work
        self.rungs = world.rungs


def test_regression_nogood_uses_action_source_world(monkeypatch) -> None:
    """A rollback destination does not own the failed action's identity."""
    source_key = ("action-source",)
    rollback_key = ("rollback-destination",)
    owner = object()
    checkpoint_work = SimpleNamespace(state=SimpleNamespace(scan_id=10, tags={}))
    checkpoint = SimpleNamespace(
        owner=owner,
        key=rollback_key,
        world=SimpleNamespace(work=checkpoint_work, rungs=()),
        trend=3,
    )
    state = _RecoveryState(checkpoint)
    compass = _RecordingCompass()
    ctx = SimpleNamespace(compass=compass)
    frame = SimpleNamespace(key=source_key)
    trial = SimpleNamespace(
        chase_regression_causes=False,
        regression_nogoods=frozenset({("Action", True)}),
        trend=4,
        fork_snap={},
    )
    origin = _RecoveryOrigin(
        checkpoint_owner=owner,
        anchor_scan=10,
        before_snap={},
    )
    monkeypatch.setattr(progress, "_channel_transitions", lambda *_args: ())
    monkeypatch.setattr(progress, "_revoke_corrections", lambda *_args: ())

    progress._investigate_and_revert(
        trial,
        frame,
        state,
        ctx,
        origin=origin,
    )

    assert [observation.world_key for observation in compass.observations] == [source_key]
