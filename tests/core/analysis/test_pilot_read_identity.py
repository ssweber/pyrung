"""Execution freshness must distinguish states hidden by search abstraction."""

from dataclasses import replace

import pytest
from pyrsistent import pvector

from pyrung import PLC, Int, Program, Rung, copy
from pyrung.core.analysis.pilot.compass import ProbeExhaustedObservation
from pyrung.core.analysis.pilot.drive_setup import prepare_drive, prepare_target_context
from pyrung.core.analysis.pilot.navigation_contracts import (
    Bearing,
    NavigationConstraints,
    OrientationWorld,
)
from pyrung.core.analysis.pilot.steer import StaleBearingError, execute
from pyrung.core.analysis.pilot.types import _PilotState
from pyrung.core.analysis.pilot.world import _World
from pyrung.core.analysis.pilot.world_key import _pilot_world_key, _StateKeyConfig


def _oriented_world():
    Count = Int("Count", external=True, default=1)
    Output = Int("Output")
    with Program() as program:
        with Rung():
            copy(Count, Output)
    setup = prepare_drive(PLC(program), unlink=None)
    ctx, _ = prepare_target_context(setup, "Output", 3, None, max_scans=30, avoid_pred=None)
    # An intentionally coarse, valid projection: concrete count changes do
    # not leave the search region. Freshness must not depend on this choice.
    config = _StateKeyConfig(("Count",), (), (), frozenset({0}))
    state = _PilotState(
        world=_World(
            work=setup.work,
            committed_acts=pvector(),
            best_trend=None,
            pilot_rungs=pvector(),
            dwell_scans=0,
        ),
        key_config=config,
        seen_keys=set(),
        checkpoints=[],
        watch_tags=[],
    )
    world = OrientationWorld((), dict(state.work.state.tags), None, state, ctx)
    bearing = ctx.compass.orient(world, ctx.target, NavigationConstraints())
    assert isinstance(bearing, Bearing)
    assert bearing.orientation is not None
    assert bearing.read_identity is not None
    return bearing, bearing.orientation.world


@pytest.mark.parametrize(
    "change", ["count", "memory", "patch", "force", "scan", "restore", "knowledge"]
)
def test_stale_read_cannot_execute_inside_the_same_search_region(change):
    bearing, world = _oriented_world()
    state = world.state
    work = state.work
    if change == "count":
        work._state = work.state.with_tags({"Count": 2})
    elif change == "memory":
        work._state = work.state.with_memory({"spent": True})
    elif change == "patch":
        work.patch({"Count": 2})
    elif change == "force":
        work.force("Count", 2)
    elif change == "scan":
        work.step()
    elif change == "restore":
        checkpoint = state.snapshot_world()
        state.load_world(checkpoint)
    else:
        world.context.compass, _ = world.context.compass.apply(
            (ProbeExhaustedObservation(bearing.world_key),)
        )
    assert (
        _pilot_world_key(
            dict(state.work.state.tags),
            state.key_config,
            state.pilot_rungs,
            state.active_requirements,
        )
        == bearing.world_key
    )
    with pytest.raises(StaleBearingError, match="execution state, configuration, or knowledge"):
        execute(bearing, world)


def test_fresh_bearing_executes_on_a_disposable_fork():
    bearing, world = _oriented_world()
    result = execute(bearing, world)
    assert result.executed_attempt is not None
    assert result.executed_attempt.pulse.fork.state.scan_id == 1
    assert world.state.work.state.scan_id == 0


def test_production_bearing_requires_exact_read_identity():
    bearing, world = _oriented_world()
    with pytest.raises(StaleBearingError):
        execute(replace(bearing, read_identity=None), world)
