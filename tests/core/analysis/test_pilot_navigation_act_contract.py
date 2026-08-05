"""The navigation boundary has no historical-prefix replay act."""

from __future__ import annotations

from types import SimpleNamespace
from typing import get_args

import pytest

from pyrung.core.analysis.pilot import navigation_contracts, steer
from pyrung.core.analysis.pilot.navigation_contracts import (
    BatchPulse,
    Bearing,
    BearingObjective,
    Coast,
    Dwell,
    NavigationAct,
    OrientationWorld,
    Pulse,
    TargetSpec,
)


def test_navigation_act_cannot_name_a_historical_replay() -> None:
    assert get_args(NavigationAct) == (Pulse, BatchPulse, Coast, Dwell)
    assert not hasattr(navigation_contracts, "RetainedReplay")
    assert not hasattr(navigation_contracts, "RetainedOccurrence")


def test_execution_rejects_a_historical_replay_shaped_act(monkeypatch) -> None:
    """Execution cannot silently replay an old prefix through an unknown act."""

    class _HistoricalReplay:
        pass

    key = ("current-world",)
    state = SimpleNamespace(
        key_config=object(),
        work=SimpleNamespace(state=SimpleNamespace(tags={})),
        pilot_rungs=(),
        active_requirements=(),
    )
    world = OrientationWorld(
        world_key=key,
        snapshot={},
        frame=SimpleNamespace(),
        state=state,
        context=SimpleNamespace(),
    )
    bearing = Bearing(
        world_key=key,
        act=_HistoricalReplay(),  # type: ignore[arg-type]
        objective=BearingObjective(TargetSpec("Target", True)),
    )
    monkeypatch.setattr(steer, "_pilot_world_key", lambda *_args: key)

    with pytest.raises(TypeError, match="unsupported navigation act _HistoricalReplay"):
        steer.execute(bearing, world)
