from __future__ import annotations

from dataclasses import dataclass, replace
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
    Dwell,
    NavigationConstraints,
    NeedProbe,
    OrientationWorld,
    Pulse,
    Stuck,
    TargetSpec,
    act_identity,
    pulse_identity,
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
    pdg: object = None
    program: object = None
    steerable: frozenset = frozenset()
    clear_only: frozenset = frozenset()
    opaque_loop: frozenset = frozenset()
    pipeline_internal_tags: frozenset = frozenset()
    domain_prior: object = None
    via_pred: object = None


def _candidate(tag: str) -> SimpleNamespace:
    return SimpleNamespace(
        tag=tag,
        value=True,
        pair=(tag, True),
        current_note="",
        route_prescribed=False,
        influence_prescribed=False,
        current_prescribed=False,
        program_prescribed=False,
        program_note="",
        bearing_channel_tag=None,
        bearing_channel_value=None,
    )


def _options(*candidates, stuck_reason=None):
    return SimpleNamespace(
        completion_frontier=(),
        route_plan=None,
        candidates=tuple(candidates),
        continuation_evidence=(),
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
        state=SimpleNamespace(key_config=None, rungs=(), work=SimpleNamespace()),
        context=context,
    )


def test_inferred_root_routes_are_read_together_without_commitment(monkeypatch) -> None:
    import pyrung.core.analysis.pilot.orientation as orientation
    from pyrung.core.analysis.pilot.trace import TraceChoice

    route_a = TraceChoice(id="route-a", label="A", route=("A",))
    route_b = TraceChoice(id="route-b", label="B", route=("B",))
    tree_a = object()
    tree_b = object()
    monkeypatch.setattr(
        orientation,
        "_route_rejected_actions",
        lambda _tree, _world, _exclusions: None,
    )
    monkeypatch.setattr(
        orientation,
        "rank_trace_choices",
        lambda *_args, **_kwargs: (
            (route_a, route_b),
            ((route_a, tree_a), (route_b, tree_b)),
        ),
    )

    routes = orientation._read_route_trees(
        _world(Compass()),
        TargetSpec("Target", True),
        NavigationConstraints(),
    )

    assert routes == ((route_a, tree_a), (route_b, tree_b))


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


def test_live_operation_owns_its_successor_residual_after_boundary_crosses() -> None:
    from pyrung.core.analysis.pilot.options import _current_work_evidence
    from pyrung.core.analysis.pilot.trace import TraceNode

    frame = SimpleNamespace(
        snap={"Heat_tmr_Acc": 2, "Heat_CurStep": 2},
        tree=TraceNode(
            "Target",
            True,
            children=[
                TraceNode(
                    "Heat_CurStep",
                    3,
                    satisfied=False,
                    children=[
                        TraceNode(
                            "ContinueHeat",
                            True,
                            satisfied=False,
                            is_steerable=True,
                        )
                    ],
                )
            ],
        ),
    )
    state = SimpleNamespace(
        rungs=(),
        pending_departure=None,
        committed_acts=(
            SimpleNamespace(
                context=SimpleNamespace(
                    before_snap={"Heat_tmr_Acc": 0, "Heat_CurStep": 1},
                    after_snap={"Heat_tmr_Acc": 2, "Heat_CurStep": 2},
                    frontier_tags=("Heat_tmr_Acc",),
                    channel_tag="Heat_tmr_Acc",
                    motion=SimpleNamespace(
                        value="coast-to-bearing",
                        is_coast=True,
                    ),
                )
            ),
        ),
        gauge=None,
    )

    assert _current_work_evidence(frame, state, None) == ("operation:Heat_CurStep",)


def test_open_operation_maintenance_owns_before_a_sibling_intervention(
    monkeypatch,
) -> None:
    """Keeping live work running is a continuation, not an actionless fallback."""
    import pyrung.core.analysis.pilot.orientation as orientation

    compass = Compass()
    target = TargetSpec("Target", True)
    first = SimpleNamespace(name="live")
    second = SimpleNamespace(name="sibling")
    maintain = Bearing(
        ("world",),
        Dwell(),
        BearingObjective(target),
    )
    destroy = Bearing(
        ("world",),
        Pulse(
            ("Destroy", True),
            (("Destroy", True),),
            _candidate("Destroy"),
        ),
        BearingObjective(target),
    )
    monkeypatch.setattr(
        orientation,
        "_orient_read",
        lambda _compass, world, _target, _constraints: (
            maintain if world is first else destroy
        ),
    )

    selected, results = orientation._read_group(
        compass,
        (first, second),
        target,
        NavigationConstraints(),
        maintenance_owns=True,
    )

    assert selected is maintain
    assert results == (maintain,)


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


def test_orient_returns_stuck_after_budget_with_route_receipt(monkeypatch) -> None:
    import pyrung.core.analysis.pilot.orientation as orientation
    from pyrung.core.analysis.pilot.trace import TraceChoice

    active = TraceChoice(id="route-a", label="A", route=("A",))
    compass = Compass()
    for _ in range(2):
        compass, _ = compass.apply((ProbeExhaustedObservation(("world",)),))
    monkeypatch.setattr(
        orientation,
        "_build_candidates",
        lambda *_args: _options(stuck_reason="trace_opaque"),
    )
    world = replace(_world(compass), root_route=active)

    result = compass.orient(
        world,
        TargetSpec("Target", True),
        NavigationConstraints(),
    )

    assert isinstance(result, Stuck)
    assert result.reason_code == "trace_opaque"
    assert result.evidence == ("probe budget 2",)


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


def test_joint_pulse_nogood_does_not_reject_its_primary_pair() -> None:
    primary = ("Start", True)
    gate_a = ("GateA", True)
    gate_b = ("GateB", True)
    world = ("world",)
    rejected = pulse_identity((primary, gate_a))

    compass, changed = Compass().apply((ActionNogoodObservation(world, rejected),))

    assert changed
    assert compass.knowledge.act_is_nogood(world, rejected)
    assert not compass.knowledge.act_is_nogood(
        world,
        pulse_identity((primary, gate_b)),
    )
    assert primary not in compass.knowledge.nogood_pairs(world)
    assert not compass.knowledge.act_is_nogood(("other-world",), rejected)

    singleton_world = ("singleton-world",)
    legacy_world = ("legacy-world",)
    compass, _ = compass.apply(
        (
            ActionNogoodObservation(
                singleton_world,
                pulse_identity((primary,)),
            ),
            ActionNogoodObservation(legacy_world, ("pair", primary)),
        )
    )
    assert primary in compass.knowledge.nogood_pairs(singleton_world)
    assert primary in compass.knowledge.nogood_pairs(legacy_world)


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
