from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from pyrung.core.analysis.pilot.compass import (
    ActionNogoodObservation,
    Compass,
    ProbeExhaustedObservation,
)
from pyrung.core.analysis.pilot.navigation import (
    Bearing,
    BearingObjective,
    Coast,
    NavigationConstraints,
    NeedProbe,
    OrientationWorld,
    Pulse,
    Stuck,
    TargetSpec,
    act_identity,
)


@dataclass
class _Context:
    compass: Compass
    target_tag: str = "Target"
    target_value: object = True
    target_predicate: object = None
    blocked_route_actions: frozenset = frozenset()
    avoid_pred: object = None
    route: object = None


def _candidate(tag: str) -> SimpleNamespace:
    return SimpleNamespace(
        tag=tag,
        value=True,
        pair=(tag, True),
        current_note="",
        route_prescribed=False,
        influence_prescribed=False,
        current_prescribed=False,
        bearing_channel_tag=None,
        bearing_channel_value=None,
    )


def _options(*candidates, stuck_reason=None):
    return SimpleNamespace(
        completion_frontier=(),
        route_plan=None,
        candidates=tuple(candidates),
        prerequisite_rungs=(),
        wait_prescribed=False,
        wait_reason=None,
        prescribed_batch=None,
        active_trace_actions=(),
        stuck_reason=stuck_reason,
    )


def _world(compass: Compass) -> OrientationWorld:
    from pyrung.core.analysis.pilot.trace import TraceNode

    context = _Context(compass)
    return OrientationWorld(
        world_key=("world",),
        snapshot={"Target": False},
        frame=SimpleNamespace(
            key=("world",),
            tree=TraceNode("Target", True, satisfied=False),
            completion_frontier=(),
        ),
        state=SimpleNamespace(key_config=None, rungs=()),
        context=context,
    )


def test_active_inferred_root_route_is_retraced_without_reranking(monkeypatch) -> None:
    """A score change cannot switch routes before an exhaustion boundary."""
    import pyrung.core.analysis.pilot.orientation as orientation
    from pyrung.core.analysis.pilot.trace import TraceChoice

    active = TraceChoice(id="route-a", label="A", route=("A",))
    committed_tree = object()
    monkeypatch.setattr(
        orientation,
        "_trace_for_route",
        lambda _world, _target, _constraints, route, _rejected: (
            committed_tree if route is active else pytest.fail("commitment changed")
        ),
    )
    monkeypatch.setattr(
        orientation,
        "_route_rejected_actions",
        lambda _tree, _world, _exclusions: None,
    )
    monkeypatch.setattr(
        orientation,
        "rank_trace_choices",
        lambda *_args, **_kwargs: pytest.fail("active route was re-ranked"),
    )

    selected, tree, exhaustion = orientation._read_committed_route(
        _world(Compass()),
        TargetSpec("Target", True),
        NavigationConstraints(active_root_route=active),
    )

    assert selected is active
    assert tree is committed_tree
    assert exhaustion is None


def test_orient_returns_one_act_without_route_suffix(monkeypatch) -> None:
    import pyrung.core.analysis.pilot.orientation as orientation

    compass = Compass()
    first = _candidate("First")
    monkeypatch.setattr(orientation, "_build_candidates", lambda *_args: _options(first))
    monkeypatch.setattr(
        orientation,
        "_candidate_applied",
        lambda option, _options, _context: (option.pair,),
    )

    world = _world(compass)
    result = compass.orient(
        world,
        TargetSpec("Target", True),
        NavigationConstraints(),
    )

    assert isinstance(result, Bearing)
    assert isinstance(result.act, Pulse)
    assert result.act.action == ("First", True)
    assert result.objective.target == TargetSpec("Target", True)
    assert result.objective.frontier == ()
    assert not hasattr(result, "path")
    assert not hasattr(result, "candidates")
    assert result.trace is not None
    assert result.trace.world.frame is world.frame
    assert result.trace.candidates.candidates == (first,)
    assert not hasattr(result.trace, "readings")


def test_bearing_preserves_downstream_channel_goal(monkeypatch) -> None:
    """A Boolean target keeps the state-register goal Orientation traced for it."""
    import pyrung.core.analysis.pilot.orientation as orientation
    from pyrung.core.analysis.pilot.trace import TraceNode

    compass = Compass()
    first = _candidate("First")
    monkeypatch.setattr(orientation, "_build_candidates", lambda *_args: _options(first))
    monkeypatch.setattr(
        orientation,
        "_candidate_applied",
        lambda option, _options, _context: (option.pair,),
    )
    state_goal = TraceNode(
        "State",
        17,
        children=[TraceNode("CompleteCommand", True, is_steerable=True)],
    )
    complete_goal = TraceNode("Complete", True, children=[state_goal])
    world = _world(compass)
    world.snapshot.update({"Complete": False, "State": 6})
    world.frame.tree = complete_goal

    result = compass.orient(
        world,
        TargetSpec("Complete", True),
        NavigationConstraints(),
    )

    assert isinstance(result, Bearing)
    assert result.objective.target == TargetSpec("Complete", True)
    assert result.objective.channel_goals("State") == (17,)


def test_coast_act_carries_only_immediate_heading() -> None:
    act = Coast(
        "bearing",
        channel_tag="State",
        target_value=2,
        route_prescribed=True,
    )

    assert act.channel_tag == "State"
    assert act.target_value == 2
    assert not hasattr(act, "option")
    assert not hasattr(act, "path")


def test_orient_returns_need_probe_then_stuck_after_budget(monkeypatch) -> None:
    import pyrung.core.analysis.pilot.orientation as orientation

    monkeypatch.setattr(
        orientation,
        "_build_candidates",
        lambda *_args: _options(stuck_reason="trace_opaque"),
    )
    compass = Compass()
    result = compass.orient(
        _world(compass),
        TargetSpec("Target", True),
        NavigationConstraints(),
    )
    assert isinstance(result, NeedProbe)

    for _ in range(2):
        compass, _ = compass.apply((ProbeExhaustedObservation(("world",)),))
    result = compass.orient(
        _world(compass),
        TargetSpec("Target", True),
        NavigationConstraints(),
    )
    assert isinstance(result, Stuck)
    assert result.reason_code == "trace_opaque"


def test_orient_does_not_mutate_world_context_or_knowledge(monkeypatch) -> None:
    import pyrung.core.analysis.pilot.orientation as orientation

    compass = Compass()
    world = _world(compass)
    before_snapshot = dict(world.snapshot)
    before_context = dict(vars(world.context))
    monkeypatch.setattr(
        orientation,
        "_build_candidates",
        lambda *_args: _options(stuck_reason="trace_empty"),
    )

    compass.orient(world, TargetSpec("Target", True), NavigationConstraints())

    assert world.snapshot == before_snapshot
    assert vars(world.context) == before_context
    assert len(compass.knowledge.entries) == 0
    assert len(compass.knowledge.act_nogoods) == 0


def test_rejected_act_knowledge_forces_fresh_next_orientation(monkeypatch) -> None:
    import pyrung.core.analysis.pilot.orientation as orientation

    first = _candidate("First")
    second = _candidate("Second")
    monkeypatch.setattr(
        orientation,
        "_build_candidates",
        lambda *_args: _options(first, second),
    )
    monkeypatch.setattr(
        orientation,
        "_candidate_applied",
        lambda option, _options, _context: (option.pair,),
    )
    compass = Compass()
    result = compass.orient(
        _world(compass),
        TargetSpec("Target", True),
        NavigationConstraints(),
    )
    assert isinstance(result, Bearing)
    assert result.act.action == first.pair

    compass, changed = compass.apply(
        (ActionNogoodObservation(("world",), act_identity(result.act)),)
    )
    assert changed
    next_result = compass.orient(
        _world(compass),
        TargetSpec("Target", True),
        NavigationConstraints(),
    )
    assert isinstance(next_result, Bearing)
    assert next_result.act.action == second.pair


def test_trace_rejections_require_exact_singleton_pulse_artifact() -> None:
    from pyrung.core.analysis.pilot.orientation import _exact_rejected_actions

    first = ("First", True)
    second = ("Second", True)
    exclusions = frozenset(
        {
            ("pulse", (first,)),
            ("pulse", (second, ("Gate", True))),
            ("pair", ("Legacy", True)),
        }
    )

    assert _exact_rejected_actions(exclusions) == frozenset({first})


def test_stale_bearing_cannot_execute() -> None:
    from pyrung.core.analysis.pilot._ops import _StateKeyConfig
    from pyrung.core.analysis.pilot.steer import StaleBearingError, execute

    state = SimpleNamespace(
        key_config=_StateKeyConfig(
            stateful_names=("X",),
            done_specs=(),
            threshold_vector_specs=(),
            acc_indices=frozenset(),
        ),
        work=SimpleNamespace(state=SimpleNamespace(tags={"X": 1})),
        rungs=(),
    )
    world = OrientationWorld(
        world_key=("stale",),
        snapshot={"X": 1},
        frame=SimpleNamespace(),
        state=state,
        context=SimpleNamespace(),
    )
    bearing = Bearing(
        world_key=("stale",),
        act=Pulse(("Cmd", True), (("Cmd", True),), _candidate("Cmd")),
        objective=BearingObjective(TargetSpec("Target", True)),
    )
    with pytest.raises(StaleBearingError):
        execute(bearing, world)


def test_driver_has_no_direct_option_builder_or_probe_policy() -> None:
    from pathlib import Path

    import pyrung.core.analysis.pilot.pilot as pilot

    source = Path(pilot.__file__).read_text(encoding="utf-8")
    assert "_build_candidates" not in source
    assert "_orient_escalate_skiff" not in source
    assert "from pyrung.core.analysis.pilot.options" not in source
