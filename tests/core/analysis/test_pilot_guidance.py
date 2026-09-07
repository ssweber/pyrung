"""The assignment survey learns current-scan effects without retaining a route."""

from dataclasses import replace

import pytest
from pyrsistent import pvector

from pyrung import PLC, Bool, Program, Rung, latch, rise
from pyrung.core.analysis.pilot.drive_setup import prepare_drive, prepare_target_context
from pyrung.core.analysis.pilot.guidance import survey_current_inputs
from pyrung.core.analysis.pilot.navigation_contracts import NavigationConstraints, OrientationWorld
from pyrung.core.analysis.pilot.types import _PilotState
from pyrung.core.analysis.pilot.world import _World


def _world(*, spent_edge=False, limit=100):
    A, B = Bool("SurveyA", external=True), Bool("SurveyB", external=True)
    Done = Bool("SurveyDone")
    with Program() as program:
        with Rung(rise(A), rise(B)):
            latch(Done)
    plc = PLC(program)
    if spent_edge:
        plc.patch({A: True})
        plc.step()
    setup = prepare_drive(plc, unlink=None)
    ctx, _ = prepare_target_context(setup, Done.name, True, None, max_scans=limit, avoid_pred=None)
    state = _PilotState(
        world=_World(
            work=setup.work,
            committed_acts=pvector(),
            best_trend=None,
            pilot_rungs=pvector(),
            dwell_scans=0,
        ),
        key_config=ctx.key_config,
        seen_keys=set(),
        checkpoints=[],
        watch_tags=[],
    )
    result = ctx.compass.orient(
        OrientationWorld((), dict(state.work.state.tags), None, state, ctx),
        ctx.target,
        NavigationConstraints(),
    )
    assert result.orientation is not None
    return plc, result.orientation.world


def test_survey_confirms_simultaneous_edges_and_returns_only_observations():
    plc, world = _world()
    result = survey_current_inputs(world)
    assert result.observations
    assert all(obs.applied == (("SurveyA", True), ("SurveyB", True)) for obs in result.observations)
    assert result.evaluations == 4  # control, A, B, then the simultaneous pair
    assert result.confirmations == 2
    assert world.state.budget.spent == 6
    assert plc.state.scan_id == world.state.work.state.scan_id == 0
    assert (
        "SurveyDone" not in world.state.work.state.tags
        or not world.state.work.state.tags["SurveyDone"]
    )
    compass, changed = world.context.compass.apply(result.observations)
    assert changed
    # Applying evidence alone never changes the executable World.
    assert world.state.work.state.scan_id == 0
    assert compass.knowledge.has_transitions(
        "SurveyDone", world_key=world.world_key, snapshot=world.snapshot
    )


def test_survey_preserves_edge_memory_and_does_not_chain_successor_scans():
    plc, world = _world(spent_edge=True)
    result = survey_current_inputs(world)
    assert not result.observations
    assert plc.state.scan_id == world.state.work.state.scan_id == 1
    assert world.state.work.state.tags["SurveyA"] is True


def test_survey_exhaustion_is_bounded_and_proves_no_unreachability():
    plc, world = _world(limit=3)
    result = survey_current_inputs(world)
    assert result.evaluations == world.state.budget.spent == 3
    assert not result.observations
    assert result.reason == "survey budget exhausted"
    assert plc.state.scan_id == 0


def test_compiled_prediction_requires_matching_ordinary_confirmation(monkeypatch):
    import pyrung.core.analysis.pilot.guidance as guidance

    _, world = _world()
    original = guidance.run_pinned_scan

    def disagree(*args, **kwargs):
        result = original(*args, **kwargs)
        return replace(result, after={**result.after, "SurveyDone": False})

    monkeypatch.setattr(guidance, "run_pinned_scan", disagree)
    result = survey_current_inputs(world)
    assert not result.observations


@pytest.mark.parametrize("force_guidance", [False, True])
def test_public_guidance_survey_returns_to_compass_before_execution(monkeypatch, force_guidance):
    from pyrung.core.analysis.pilot import pilot_how
    from pyrung.core.analysis.pilot.compass import Compass
    from pyrung.core.analysis.pilot.navigation_contracts import GuidanceRequest

    plc, _ = _world()
    original = Compass.orient

    def request_survey(compass, world, target, constraints):
        result = original(compass, world, target, constraints)
        if not compass.knowledge.has_transitions("SurveyDone"):
            assert result.orientation is not None
            return GuidanceRequest(
                result.world_key, (), (), "test guidance boundary", result.orientation
            )
        return result

    if force_guidance:
        monkeypatch.setattr(Compass, "orient", request_survey)
    events = []
    path = pilot_how(
        plc, plc._known_tags_by_name["SurveyDone"], max_scans=100, on_event=events.append
    )
    assert path.reachable, path.reason
    assert path.replay().state.tags["SurveyDone"] is True
    assert plc.state.scan_id == 0
    survey_index = next(i for i, e in enumerate(events) if e.kind == "guidance_surveyed")
    if force_guidance:
        assert not any(e.kind == "trial_committed" for e in events[: survey_index + 1])
    assert any(e.kind == "iteration" for e in events[survey_index + 1 :])


def test_survey_preserves_live_forces():
    _, world = _world()
    world.state.work.force("SurveyB", False)
    fresh = world.context.compass.orient(
        replace(world, frame=None, read_identity=None),
        world.context.target,
        NavigationConstraints(),
    )
    assert fresh.orientation is not None
    world = fresh.orientation.world
    result = survey_current_inputs(world)
    assert not result.observations
    assert world.state.work.forces["SurveyB"] is False


def test_survey_cannot_use_a_stale_source():
    from pyrung.core.analysis.pilot.steer import StaleBearingError

    _, world = _world()
    world.state.work.patch({"SurveyA": True})
    with pytest.raises(StaleBearingError):
        survey_current_inputs(world)
    assert world.state.budget.spent == 0
