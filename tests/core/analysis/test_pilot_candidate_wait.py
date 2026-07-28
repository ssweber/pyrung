from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

from pyrung.core.analysis.pilot.charts import (
    StaticTransitionGraph,
    _best_static_path,
    _build_action_lookup,
    _edges_from_routes,
)
from pyrung.core.analysis.pilot.compass import (
    ActionNogoodObservation,
    Compass,
    CompassObservation,
    NavigationCatalog,
)
from pyrung.core.analysis.pilot.currents import Producer
from pyrung.core.analysis.pilot.evidence import PipelineRoles, TransitionRoute
from pyrung.core.analysis.pilot.navigation import (
    ChannelHeading,
    NavigationConstraints,
    OrientationWorld,
    TargetSpec,
    pulse_identity,
)
from pyrung.core.analysis.pilot.navigation_evidence import (
    NavigationEvidence,
    Reachable,
    StaticEdgeExclusionReason,
)
from pyrung.core.analysis.pilot.options import (
    WaitPrescription,
    WaitRead,
    _admit_trace_details,
    _admit_wait_read,
    _AdmittedWait,
    _build_candidates,
    _compass_route_actions,
    _compass_route_plan,
    _TraceAdmission,
)
from pyrung.core.analysis.pilot.program_step import (
    ProgramInputHandoff,
    ProgramStep,
    ProgramStepStatus,
)
from pyrung.core.analysis.pilot.trace import TraceAction, TraceNode
from pyrung.core.crossing import Eq


def _route(from_value: int, to_value: int) -> TransitionRoute:
    return TransitionRoute(
        destination_tag="State",
        destination_value=to_value,
        request_tag=None,
        request_value=None,
        source_constraints=(("State", from_value),),
        enablers=(),
        action_tags=frozenset(),
        writer_node=0,
        writer_subroutine=None,
        call_site_gates=(),
        from_values=(from_value,),
    )


def _action_route(from_value: int, to_value: int, action: str) -> TransitionRoute:
    return TransitionRoute(
        destination_tag="State",
        destination_value=to_value,
        request_tag=None,
        request_value=None,
        source_constraints=(("State", from_value),),
        enablers=((action, True),),
        action_tags=frozenset({action}),
        writer_node=0,
        writer_subroutine=None,
        call_site_gates=(),
        from_values=(from_value,),
    )


def _wildcard_action_route(to_value: int, action: str) -> TransitionRoute:
    return TransitionRoute(
        destination_tag="State",
        destination_value=to_value,
        request_tag=None,
        request_value=None,
        source_constraints=(),
        enablers=((action, True),),
        action_tags=frozenset({action}),
        writer_node=0,
        writer_subroutine=None,
        call_site_gates=(),
    )


def _convergence_action_route(action: str) -> TransitionRoute:
    return TransitionRoute(
        destination_tag="CtrlCmd",
        destination_value=7,
        request_tag=None,
        request_value=None,
        source_constraints=(),
        enablers=((action, True),),
        action_tags=frozenset({action}),
        writer_node=0,
        writer_subroutine=None,
        call_site_gates=(),
    )


def _convergence_consumer_route(*, direct_action: str | None = None) -> TransitionRoute:
    enablers = (("CtrlCmd", 7),)
    action_tags: frozenset[str] = frozenset()
    if direct_action is not None:
        enablers = ((direct_action, True), *enablers)
        action_tags = frozenset({direct_action})
    return TransitionRoute(
        destination_tag="State",
        destination_value=2,
        request_tag=None,
        request_value=None,
        source_constraints=(("State", 0),),
        enablers=enablers,
        action_tags=action_tags,
        writer_node=0,
        writer_subroutine=None,
        call_site_gates=(),
        from_values=(0,),
    )


def test_convergent_actions_remain_ordered_independent_edges(monkeypatch) -> None:
    """Commands reaching one intermediate value are alternatives, not a batch."""
    first = _convergence_action_route("First")
    second = _convergence_action_route("Second")

    def routes_for_ctrl_cmd(*_args, **_kwargs):
        return [first, second]

    monkeypatch.setattr(
        "pyrung.core.analysis.pilot.charts.expand_routes",
        routes_for_ctrl_cmd,
    )
    consumer = _convergence_consumer_route()
    lookup = _build_action_lookup(
        (consumer,),
        SimpleNamespace(tags={"CtrlCmd": object()}),
        object(),
        frozenset({"First", "Second"}),
        frozenset(),
        None,
    )

    assert lookup[("CtrlCmd", repr(7))] == (("First", True), ("Second", True))

    graph = StaticTransitionGraph(PipelineRoles("State"), (consumer,), lookup)
    assert [edge.action for edge in graph.edges] == [
        ("First", True),
        ("Second", True),
    ]
    assert all(not edge.co_actions for edge in graph.edges)
    preferred = graph.find_path(0, (2,), edge_allowed=lambda _edge: True)
    compass = Compass(NavigationCatalog(graphs=(graph,)))
    frame = SimpleNamespace(
        key=("world",),
        snap={"State": 0, "First": False, "Second": False},
        tree=TraceNode("State", 2, satisfied=False),
    )
    ctx = SimpleNamespace(
        compass=compass,
        opaque_loop=frozenset({"State"}),
        target=TargetSpec("State", 2),
        blocked_actions=frozenset(),
        avoid_pred=None,
    )
    fallback = _compass_route_plan(frame, ctx, {("First", True)})

    assert preferred is not None
    assert preferred.first_edge.action == ("First", True)
    assert fallback is not None
    assert fallback.first_edge.action == ("Second", True)


def test_direct_and_convergent_action_is_fanned_out_once() -> None:
    consumer = _convergence_consumer_route(direct_action="First")
    graph = StaticTransitionGraph(
        PipelineRoles("State"),
        (consumer,),
        {("CtrlCmd", repr(7)): (("First", True), ("Second", True))},
    )

    assert [edge.action for edge in graph.edges] == [
        ("First", True),
        ("Second", True),
    ]


def test_rejected_joint_route_falls_back_to_same_action_with_other_gate() -> None:
    """A failed command recipe excludes that edge, not the command itself."""
    primary = ("Start", True)
    gate_a = ("GateA", True)
    gate_b = ("GateB", True)
    first = replace(
        _action_route(0, 2, primary[0]),
        edge_gates=(gate_a,),
    )
    second = replace(
        _action_route(0, 2, primary[0]),
        edge_gates=(gate_b,),
    )
    graph = StaticTransitionGraph(PipelineRoles("State"), (first, second))
    world = ("world",)
    compass, changed = Compass(NavigationCatalog(graphs=(graph,))).apply(
        (
            ActionNogoodObservation(
                world,
                pulse_identity((primary, gate_a)),
            ),
        )
    )
    frame = SimpleNamespace(
        key=world,
        snap={"State": 0, "Start": False, "GateA": False, "GateB": False},
        tree=TraceNode("State", 2, satisfied=False),
    )
    ctx = SimpleNamespace(
        compass=compass,
        opaque_loop=frozenset({"State"}),
        target=TargetSpec("State", 2),
        blocked_actions=frozenset(),
        avoid_pred=None,
    )

    plan = _compass_route_plan(
        frame,
        ctx,
        set(compass.knowledge.nogood_pairs(world)),
    )

    assert changed
    assert primary not in compass.knowledge.nogood_pairs(world)
    assert plan is not None
    assert plan.first_edge.action == primary
    assert plan.first_edge.co_actions == (gate_b,)

    target = TargetSpec("State", 2)
    constraints = NavigationConstraints()
    view = OrientationWorld(
        world_key=world,
        snapshot=frame.snap,
        frame=frame,
        state=SimpleNamespace(),
        context=ctx,
    )
    assert isinstance(
        NavigationEvidence.frontier_status(
            view,
            target,
            constraints,
            compass.knowledge,
        ),
        Reachable,
    )

    rejected_only_graph = StaticTransitionGraph(PipelineRoles("State"), (first,))
    rejected_only, _ = Compass(NavigationCatalog(graphs=(rejected_only_graph,))).apply(
        (
            ActionNogoodObservation(
                world,
                pulse_identity((primary, gate_a)),
            ),
        )
    )
    rejected_only_ctx = SimpleNamespace(**vars(ctx))
    rejected_only_ctx.compass = rejected_only
    rejected_only_view = OrientationWorld(
        world_key=world,
        snapshot=frame.snap,
        frame=frame,
        state=SimpleNamespace(),
        context=rejected_only_ctx,
    )
    assert not isinstance(
        NavigationEvidence.frontier_status(
            rejected_only_view,
            target,
            constraints,
            rejected_only.knowledge,
        ),
        Reachable,
    )


def test_static_edge_admission_names_full_artifact_exclusions() -> None:
    """The shared receipt explains exclusions without flattening to a Boolean."""
    primary = ("Start", True)
    gate = ("Gate", True)
    route = replace(_action_route(0, 2, primary[0]), edge_gates=(gate,))
    graph = StaticTransitionGraph(PipelineRoles("State"), (route,))
    edge = graph.edges[0]
    world = ("world",)
    compass, _ = Compass(NavigationCatalog(graphs=(graph,))).apply(
        (
            CompassObservation(
                "no_change",
                "State",
                primary,
                0,
                world_key=None,
                applied=(primary, gate),
            ),
            ActionNogoodObservation(world, ("pair", primary)),
            ActionNogoodObservation(world, pulse_identity((primary, gate))),
        )
    )
    ctx = SimpleNamespace(
        avoid_pred=lambda snap: snap.get("Gate") is True,
    )

    admission = NavigationEvidence.static_edge_admission(
        edge,
        world_key=world,
        snapshot={"State": 0, "Start": False, "Gate": False},
        knowledge=compass.knowledge,
        blocked_actions=frozenset({gate}),
        context=ctx,
    )

    assert not admission.allowed
    assert {exclusion.reason for exclusion in admission.exclusions} == {
        StaticEdgeExclusionReason.STATIC_STATUS,
        StaticEdgeExclusionReason.PAIR_NOGOOD,
        StaticEdgeExclusionReason.PULSE_NOGOOD,
        StaticEdgeExclusionReason.ROUTE_BLOCKED,
        StaticEdgeExclusionReason.AVOID_FORCED,
    }
    assert any(exclusion.evidence == (gate,) for exclusion in admission.exclusions)


def test_static_evidence_rejects_a_blocked_required_co_action() -> None:
    primary = ("Start", True)
    forbidden = ("ForbiddenGate", True)
    route = replace(_action_route(0, 2, primary[0]), edge_gates=(forbidden,))
    graph = StaticTransitionGraph(PipelineRoles("State"), (route,))
    compass = Compass(NavigationCatalog(graphs=(graph,)))
    frame = SimpleNamespace(
        key=("world",),
        snap={"State": 0, "Start": False, "ForbiddenGate": False},
        tree=TraceNode("State", 2, satisfied=False),
    )
    ctx = SimpleNamespace(
        compass=compass,
        opaque_loop=frozenset({"State"}),
        target=TargetSpec("State", 2),
        blocked_actions=frozenset({forbidden}),
        avoid_pred=None,
    )

    assert _compass_route_plan(frame, ctx) is None

    view = OrientationWorld(
        world_key=frame.key,
        snapshot=frame.snap,
        frame=frame,
        state=SimpleNamespace(),
        context=ctx,
    )
    assert not isinstance(
        NavigationEvidence.frontier_status(
            view,
            TargetSpec("State", 2),
            NavigationConstraints(blocked_actions=frozenset({forbidden})),
            compass.knowledge,
        ),
        Reachable,
    )

    ctx.blocked_actions = frozenset()
    ctx.avoid_pred = lambda snap: snap.get("ForbiddenGate") is True
    assert _compass_route_plan(frame, ctx) is None
    assert not isinstance(
        NavigationEvidence.frontier_status(
            view,
            TargetSpec("State", 2),
            NavigationConstraints(avoid_predicate=ctx.avoid_pred),
            compass.knowledge,
        ),
        Reachable,
    )


def test_static_path_prefers_exact_edge_over_wildcard_match() -> None:
    role = PipelineRoles("State")
    graph = StaticTransitionGraph(
        role,
        (
            _wildcard_action_route(2, "Fallback"),
            _action_route(0, 2, "Exact"),
        ),
    )

    plan = graph.find_path(0, (2,), edge_allowed=lambda _edge: True)

    assert plan is not None
    assert plan.first_edge.action == ("Exact", True)


def test_static_path_uses_wildcard_when_exact_edge_is_contextually_rejected() -> None:
    """Same-destination BFS visitation must not erase the surviving fallback."""
    role = PipelineRoles("State")
    graph = StaticTransitionGraph(
        role,
        (
            _wildcard_action_route(2, "Fallback"),
            _action_route(0, 2, "Exact"),
        ),
    )
    compass = Compass(NavigationCatalog(graphs=(graph,)))
    frame = SimpleNamespace(
        key=("world",),
        snap={"State": 0, "Exact": False, "Fallback": False},
        tree=TraceNode("State", 2, satisfied=False),
    )
    ctx = SimpleNamespace(
        compass=compass,
        opaque_loop=frozenset({"State"}),
        target=TargetSpec("State", 2),
        blocked_actions=frozenset(),
        avoid_pred=None,
    )

    plan = _compass_route_plan(frame, ctx, {("Exact", True)})

    assert plan is not None
    assert plan.first_edge.action == ("Fallback", True)


def test_live_route_query_filters_avoided_suffix_edges() -> None:
    """A legal first edge cannot smuggle an avoided later command into a plan."""
    role = PipelineRoles("State")
    graph = StaticTransitionGraph(
        role,
        (
            _action_route(0, 1, "SafeFirst"),
            _action_route(1, 2, "AvoidLater"),
        ),
    )

    allowed = _best_static_path(
        "State",
        2,
        {"State": 0},
        (graph,),
        edge_allowed=lambda _edge: True,
    )
    blocked = _best_static_path(
        "State",
        2,
        {"State": 0},
        (graph,),
        edge_allowed=lambda edge: edge.action != ("AvoidLater", True),
    )

    assert allowed is not None
    assert tuple(edge.action for edge in allowed.edges) == (
        ("SafeFirst", True),
        ("AvoidLater", True),
    )
    assert blocked is None


def test_orient_removes_live_avoid_edges_before_route_selection() -> None:
    role = PipelineRoles("State")
    graph = StaticTransitionGraph(role, (_action_route(0, 2, "Forbidden"),))
    compass = Compass(NavigationCatalog(graphs=(graph,)))
    frame = SimpleNamespace(
        snap={"State": 0, "Forbidden": False},
        tree=TraceNode("State", 2, satisfied=False),
    )
    ctx = SimpleNamespace(
        compass=compass,
        opaque_loop=frozenset({"State"}),
        target=TargetSpec("State", 2),
        blocked_actions=frozenset(),
        avoid_pred=lambda snap: snap.get("Forbidden") is True,
    )

    assert _compass_route_plan(frame, ctx) is None


def test_runtime_no_change_overlays_static_edge_without_mutating_catalog() -> None:
    role = PipelineRoles("State")
    graph = StaticTransitionGraph(role, (_action_route(0, 2, "Start"),))
    compass = Compass(NavigationCatalog(graphs=(graph,)))
    edge = graph.edges[0]

    observed, changed = compass.apply(
        (CompassObservation("no_change", "State", ("Start", True), 0),)
    )

    assert changed
    assert compass.knowledge.static_overlays.get(edge.identity) is None
    assert observed.knowledge.static_overlays[edge.identity] == "no_change"
    assert observed.catalog is compass.catalog


def test_runtime_transitions_remain_distinct_in_exact_observed_worlds() -> None:
    """The same channel/action can have different destinations by recipe."""
    action = ("Start", True)
    # Recipe is an external selector omitted by the projected Pilot world key.
    # The full pre-action context must still keep the receipts distinct.
    world = ("projected-world", 6)
    snap_a = {"State": 6, "Recipe": "A", "Start": False}
    snap_b = {"State": 6, "Recipe": "B", "Start": False}

    compass, _ = Compass().apply(
        (
            CompassObservation(
                "edge",
                "State",
                action,
                6,
                8,
                world,
                tuple(sorted(snap_a.items())),
                (action,),
            ),
            CompassObservation(
                "edge",
                "State",
                action,
                6,
                10,
                world,
                tuple(sorted(snap_b.items())),
                (action,),
            ),
        )
    )

    assert (
        compass.knowledge.transition_dest("State", 6, action, world_key=world, snapshot=snap_a) == 8
    )
    assert (
        compass.knowledge.transition_dest("State", 6, action, world_key=world, snapshot=snap_b)
        == 10
    )
    assert (
        compass.knowledge.transition_dest(
            "State", 6, action, world_key=world, snapshot={**snap_b, "Recipe": "C"}
        )
        is None
    )
    assert compass.knowledge.has_transitions("State", world_key=world, snapshot=snap_a)
    assert not compass.knowledge.has_transitions(
        "State", world_key=world, snapshot={**snap_b, "Recipe": "C"}
    )


def test_exact_world_and_applied_artifact_scope_negative_evidence() -> None:
    action = ("Start", True)
    gate = ("Gate", True)
    world_a = ("world", "a")
    world_b = ("world", "b")
    snap = {"State": 6, "Start": False, "Gate": False}
    context = tuple(sorted(snap.items()))

    compass, _ = Compass().apply(
        (
            CompassObservation("edge", "State", action, 6, 8, world_a, context, (action, gate)),
            CompassObservation("edge", "State", action, 6, 10, world_b, context, (action, gate)),
        )
    )
    # Trying the primary button without its gate disproves only that exact
    # artifact; it cannot tombstone the co-action edge in the same world.
    compass, _ = compass.apply(
        (CompassObservation("contradict", "State", action, 6, None, world_a, context, (action,)),)
    )
    assert (
        compass.knowledge.transition_dest("State", 6, action, world_key=world_a, snapshot=snap) == 8
    )
    assert (
        compass.knowledge.transition_dest("State", 6, action, world_key=world_b, snapshot=snap)
        == 10
    )

    # The matching artifact is local: it removes A without touching B.
    compass, _ = compass.apply(
        (
            CompassObservation(
                "contradict",
                "State",
                action,
                6,
                None,
                world_a,
                context,
                (action, gate),
            ),
        )
    )
    assert (
        compass.knowledge.transition_dest("State", 6, action, world_key=world_a, snapshot=snap)
        is None
    )
    assert (
        compass.knowledge.transition_dest("State", 6, action, world_key=world_b, snapshot=snap)
        == 10
    )


def test_probe_marks_are_scoped_to_the_prospective_applied_context() -> None:
    probe = ("Start", True)
    context_a = (("RecipeA", True),)
    context_b = (("RecipeB", True),)
    world = ("projected-world", 6)
    snap = {"State": 6, "Start": False}
    before = tuple(sorted(snap.items()))

    compass, _ = Compass().apply(
        (
            CompassObservation(
                "no_change",
                "State",
                probe,
                6,
                None,
                world,
                before,
                (*context_a, probe),
            ),
        )
    )

    assert (
        compass.knowledge.unprobed_actions(
            "State",
            6,
            {probe},
            world_key=world,
            snapshot=snap,
            applied_context=context_a,
        )
        == []
    )
    assert compass.knowledge.unprobed_actions(
        "State",
        6,
        {probe},
        world_key=world,
        snapshot=snap,
        applied_context=context_b,
    ) == [probe]


def test_exact_world_tombstone_overrides_global_seed_only_locally() -> None:
    action = ("Start", True)
    world = ("world", 6)
    other_world = ("other-world", 6)
    snap = {"State": 6, "Start": False}
    context = tuple(sorted(snap.items()))
    compass, _ = Compass().apply((CompassObservation("edge", "State", action, 6, 8),))
    compass, _ = compass.apply(
        (CompassObservation("contradict", "State", action, 6, None, world, context, (action,)),)
    )

    assert (
        compass.knowledge.transition_dest("State", 6, action, world_key=world, snapshot=snap)
        is None
    )
    assert (
        compass.knowledge.transition_dest("State", 6, action, world_key=other_world, snapshot=snap)
        == 8
    )


def test_static_edge_negative_overlay_requires_exact_context_and_actions() -> None:
    role = PipelineRoles("State")
    graph = StaticTransitionGraph(role, (_action_route(6, 8, "Start"),))
    edge = replace(graph.edges[0], co_actions=(("Gate", True),))
    graph.edges = (edge,)
    compass = Compass(NavigationCatalog(graphs=(graph,)))
    world = ("world", "recipe-a")
    other_world = ("world", "recipe-b")
    snap = {"State": 6, "Start": False, "Gate": False}
    context = tuple(sorted(snap.items()))

    # Missing the co-action did not exercise the guarded catalog edge.
    unchanged, _ = compass.apply(
        (
            CompassObservation(
                "contradict",
                "State",
                ("Start", True),
                6,
                None,
                world,
                context,
                (("Start", True),),
            ),
        )
    )
    assert unchanged.knowledge.static_edge_status(edge, world, snap) is None

    # Extra interference is a different artifact too.
    interfered, _ = unchanged.apply(
        (
            CompassObservation(
                "contradict",
                "State",
                ("Start", True),
                6,
                None,
                world,
                context,
                (("Start", True), ("Gate", True), ("Override", True)),
            ),
        )
    )
    assert interfered.knowledge.static_edge_status(edge, world, snap) is None

    scoped, _ = interfered.apply(
        (
            CompassObservation(
                "contradict",
                "State",
                ("Start", True),
                6,
                None,
                world,
                context,
                (("Start", True), ("Gate", True)),
            ),
        )
    )
    assert scoped.knowledge.static_edge_status(edge, world, snap) == "contradicted"
    assert scoped.knowledge.static_edge_status(edge, other_world, snap) is None


def test_wait_nogood_walks_around_the_sterile_completion_edge() -> None:
    """A rejected wait is remembered at its world key; the next ORIENT's route
    query excludes the sterile automatic edge and falls to the surviving
    operator route (the Unhold shape at a held state)."""
    from pyrung.core.analysis.pilot._ops import wait_edge_nogood

    role = PipelineRoles("State")
    graph = StaticTransitionGraph(
        role,
        (
            _route(11, 16),  # automatic completion — recipe-gated, sterile here
            _route(16, 17),
            _action_route(11, 12, "Unhold"),
            _route(12, 6),
            _route(6, 16),
        ),
    )
    compass = Compass(NavigationCatalog(graphs=(graph,)))
    frame = SimpleNamespace(
        snap={"State": 11},
        tree=TraceNode("State", 17, satisfied=False),
    )
    ctx = SimpleNamespace(
        compass=compass,
        opaque_loop=frozenset({"State"}),
        target=TargetSpec("State", 17),
        blocked_actions=frozenset(),
        avoid_pred=None,
    )

    fresh = _compass_route_plan(frame, ctx)
    assert fresh is not None
    assert fresh.first_edge.action is None  # the short automatic path wins first

    nogoods = {wait_edge_nogood("State", 11, 16)}
    rerouted = _compass_route_plan(frame, ctx, nogoods)
    assert rerouted is not None
    assert rerouted.first_edge.action == ("Unhold", True)


def test_program_owned_sibling_preserves_an_automatic_edge() -> None:
    role = PipelineRoles("State")
    route = _action_route(6, 16, "Complete")
    producer = Producer(
        rung_index=7,
        kind="program",
        guard_tags=frozenset({"FinishDone"}),
        co_writes=frozenset(),
        command_tag="Complete",
        command_value=True,
    )
    edges = _edges_from_routes(
        role,
        (route,),
        {},
        {("Complete", repr(True)): (producer,)},
    )

    assert {edge.action for edge in edges} == {("Complete", True), None}
    by_action = {edge.action: edge for edge in edges}
    assert by_action[None].program_producers == (producer,)
    assert by_action[("Complete", True)].program_producers == ()


def test_completion_edge_records_its_bearing_action_edge_does_not() -> None:
    """The recorded wait bearing is the route's charted gate pairs — minus the
    channel from-value and the operator's own button (pressing it is the
    alternative to waiting) — and rides only the completion (``action is
    None``) edge; the action-bearing command edge for the same route keeps
    ``()``."""
    role = PipelineRoles("State")
    route = replace(
        _action_route(6, 16, "Complete"),
        enablers=(("Complete", True), ("Tmr_Done", True)),
    )
    producer = Producer(
        rung_index=7,
        kind="program",
        guard_tags=frozenset({"Tmr_Done"}),
        co_writes=frozenset(),
        command_tag="Complete",
        command_value=True,
    )
    edges = _edges_from_routes(
        role,
        (route,),
        {},
        {("Complete", repr(True)): (producer,)},
    )

    by_action = {edge.action: edge for edge in edges}
    assert by_action[None].completion == (("Tmr_Done", True),)
    assert by_action[("Complete", True)].completion == ()


def test_completion_defaults_empty_without_a_recorded_bearing() -> None:
    """No ``completion_by_route`` entry → the completion edge records ``()`` and
    behaves exactly as before (chart evidence, never invented)."""
    role = PipelineRoles("State")
    edges = _edges_from_routes(role, (_route(6, 16),), {})

    assert [edge.completion for edge in edges] == [()]


def test_prescribed_wait_suppresses_stuck_reason():
    role = PipelineRoles("State")
    graph = StaticTransitionGraph(role, (_route(6, 16), _route(16, 17)))
    compass = Compass(NavigationCatalog(graphs=(graph,)))

    tree = TraceNode(
        "State",
        17,
        children=[TraceNode("UnreadableGuard", 1, satisfied=False, is_steerable=False)],
    )
    frame = SimpleNamespace(
        key=("state", 6),
        snap={"State": 6, "UnreadableGuard": 0},
        tree=tree,
        raw_trace_actions=(),
        raw_trace_action_details=(),
    )
    state = SimpleNamespace(rungs=[])
    ctx = SimpleNamespace(
        compass=compass,
        blocked_actions=frozenset(),
        edge_tags=set(),
        clear_only=frozenset(),
        steerable=frozenset(),
        pdg=SimpleNamespace(writers_of={}),
        program=object(),
        opaque_loop=frozenset(),
        target=TargetSpec("State", 17),
    )

    candidates = _build_candidates(frame, state, ctx)

    assert candidates.route is not None
    assert candidates.route.plan.first_edge.from_value == 6
    assert candidates.wait is not None
    assert candidates.wait.prescription is not None
    assert candidates.wait.prescription.heading is not None
    assert candidates.wait.prescription.heading.route is not None
    assert candidates.wait.prescription.heading.route.channel_tag == "State"
    assert candidates.trace.actions == ()
    assert candidates.options == ()
    assert candidates.wait.prescription is not None
    assert candidates.wait.reason == "let-run State: 6->16"
    assert candidates.diagnosis is None


def test_supplemental_wait_details_use_ordinary_trace_admission() -> None:
    """A narrow completion read adds evidence, never a privileged candidate."""

    lifetime = object()
    outer = TraceAction("Keep", True)
    supplemental = (
        TraceAction("Keep", True, until=lifetime),
        TraceAction("Nogood", True),
    )
    frame = SimpleNamespace(
        snap={"Keep": True, "Nogood": False},
    )
    state = SimpleNamespace(rungs=())
    ctx = SimpleNamespace(blocked_actions=frozenset(), edge_tags=frozenset())

    admitted = _admit_trace_details(
        (outer, *supplemental),
        frame,
        state,
        ctx,
        {("Nogood", True)},
    )

    # The supplemental read may enrich the already-admitted action's lifetime.
    assert admitted.detail_by_pair[("Keep", True)].until is lifetime
    # The same current-world and empirical filters apply to every source.
    assert admitted.active_actions == (("Keep", True), ("Nogood", True))
    assert admitted.actions == (("Keep", True),)
    assert tuple(detail.pair for detail in admitted.details) == (("Keep", True),)


def test_program_step_derives_only_a_uniform_shared_input_lifetime() -> None:
    """Partial handoffs stay actions; one proved boundary makes every input a hold."""

    first = TraceAction("First", True)
    second = TraceAction("Second", True)
    shared_boundary = Eq("Acc", frozenset((5,)))
    mixed_step = ProgramStep(
        ProgramStepStatus.NEEDS_INPUT,
        producer=object(),
        boundary=None,
        channel=None,
        required_inputs=(first, second),
        # One available handoff must not enrich only part of the operation.
        input_handoffs=(ProgramInputHandoff(first.pair, shared_boundary, "Acc"),),
    )

    assert mixed_step.uniform_handoff_boundary is None
    assert mixed_step.inputs_with_lifetime == (first, second)

    shared_step = ProgramStep(
        ProgramStepStatus.NEEDS_INPUT,
        producer=object(),
        boundary=None,
        channel=None,
        required_inputs=(first, second),
        input_handoffs=(
            ProgramInputHandoff(first.pair, shared_boundary, "Acc"),
            ProgramInputHandoff(second.pair, shared_boundary, "Acc"),
        ),
    )

    assert shared_step.uniform_handoff_boundary is shared_boundary
    assert tuple(detail.pair for detail in shared_step.inputs_with_lifetime) == (
        first.pair,
        second.pair,
    )
    assert all(detail.until is shared_boundary for detail in shared_step.inputs_with_lifetime)


def test_program_step_owns_observable_motion_and_route_preference() -> None:
    step = ProgramStep(
        ProgramStepStatus.INTERRUPTED,
        producer=object(),
        boundary=None,
        channel="Inner",
        projected_changes=(
            ("Outer", 6, 7),
            ("Inner", 10, 11),
            ("Unrelated", False, True),
        ),
        preserve_channels=("Outer", "Inner"),
    )

    assert tuple(
        (motion.channel_tag, motion.before_value, motion.target_value)
        for motion in step.observable_motions
    ) == (("Inner", 10, 11), ("Outer", 6, 7))
    inner = step.observable_motion()
    outer = step.observable_motion("Outer")
    assert inner is not None and inner.channel_tag == "Inner"
    assert outer is not None and outer.channel_tag == "Outer"


def test_prescribed_wait_requires_every_program_input_to_survive_admission() -> None:
    """A rejected required input cannot authorize coasting the producer."""

    required = (TraceAction("First", True), TraceAction("Blocked", True))
    step = ProgramStep(
        ProgramStepStatus.NEEDS_INPUT,
        producer=object(),
        boundary=None,
        channel=None,
        required_inputs=required,
    )
    read = WaitRead(
        WaitPrescription(
            ChannelHeading("Advance", True, object()),
            "advance the owned boundary",
            frontier=(("Advance", True),),
        ),
        required,
        program_step=step,
    )
    frame = SimpleNamespace(snap={"First": False, "Blocked": False})
    state = SimpleNamespace(rungs=())
    admitted = _admit_wait_read(
        read,
        (),
        frame,
        state,
        SimpleNamespace(
            blocked_actions=frozenset(),
            edge_tags=frozenset(),
        ),
        {("Blocked", True)},
    )

    assert admitted.viable is False
    assert admitted.prescription is None
    demoted = admitted.candidate_read
    assert demoted.prescription is None
    assert demoted.reason == "advance the owned boundary"
    assert demoted.frontier == (("Advance", True),)
    assert demoted.program_step is step
    assert demoted.details == required

    fully_admitted = _admit_wait_read(
        read,
        (),
        frame,
        state,
        SimpleNamespace(blocked_actions=frozenset(), edge_tags=frozenset()),
        set(),
    )
    assert fully_admitted.viable is True
    assert fully_admitted.prescription is read.prescription


def test_establish_suppression_retains_declined_wait_evidence() -> None:
    detail = TraceAction("Establish", True, establish=True)
    step = ProgramStep(
        ProgramStepStatus.KEEP_RUNNING,
        producer=object(),
        boundary=None,
        channel="Advance",
    )
    read = WaitRead(
        WaitPrescription(
            ChannelHeading("Advance", True, object()),
            "wait behind the establish gate",
            frontier=(("Advance", True),),
        ),
        (detail,),
        program_step=step,
    )
    establish_pending = _AdmittedWait(
        read,
        _TraceAdmission(
            active_actions=(detail.pair,),
            actions=(detail.pair,),
            details=(detail,),
            detail_by_pair={detail.pair: detail},
            managed_boolean_rungs=(),
            establish_pending=True,
        ),
    )
    assert establish_pending.viable is True
    suppressed = establish_pending.candidate_read
    assert suppressed.prescription is None
    assert suppressed.reason == "wait behind the establish gate"
    assert suppressed.frontier == (("Advance", True),)
    assert suppressed.program_step is step
    assert suppressed.details == (detail,)


def test_grounded_action_plan_always_materializes_its_first_action() -> None:
    """A selected action edge cannot fall through to a synthetic wait."""

    graph = StaticTransitionGraph(
        PipelineRoles("State"),
        (_action_route(6, 16, "Complete"),),
    )
    frame = SimpleNamespace(
        key=("state", 6),
        snap={"State": 6, "Complete": False},
        tree=TraceNode(
            "State",
            16,
            children=[TraceNode("UnreadableGuard", 1, satisfied=False, is_steerable=False)],
        ),
    )
    ctx = SimpleNamespace(
        compass=Compass(NavigationCatalog(graphs=(graph,))),
        blocked_actions=frozenset(),
        avoid_pred=None,
        opaque_loop=frozenset(),
        target=TargetSpec("State", 16),
    )

    plan = _compass_route_plan(frame, ctx, set())

    assert plan is not None
    assert plan.first_edge.action == ("Complete", True)
    assert plan.first_edge.completion == ()
    assert plan.first_edge.program_producers == ()
    assert _compass_route_actions(plan, frame, ctx, set()) == (("Complete", True),)


def test_apply_reports_changed_and_returns_self_when_nothing_new():
    """Compass.apply's no-new-knowledge contract: (compass, changed).

    Novel observations return ``changed=True`` and a new compass value; applying
    the *same* observations again returns ``changed=False`` and ``self`` — the
    identity guarantee the skiff relies on, established from the table ops rather
    than a whole-table equality scan.
    """
    obs = CompassObservation("edge", "State", ("Cmd", True), 6, 8)
    base = Compass()

    learned, changed = base.apply((obs,))
    assert changed is True
    assert learned is not base

    again, changed_again = learned.apply((obs,))
    assert changed_again is False
    assert again is learned

    # A probe mark is knowledge too: a fresh no-change tombstone counts as changed.
    probe = CompassObservation("no_change", "State", ("Other", True), 6, None)
    with_probe, probe_changed = learned.apply((probe,))
    assert probe_changed is True
    assert with_probe is not learned
    # …but re-applying it does not.
    _, probe_again = with_probe.apply((probe,))
    assert probe_again is False


def test_equivalent_applied_order_is_one_compass_receipt() -> None:
    action = ("Start", True)
    gate = ("Gate", True)
    world = ("world", 6)
    snap = {"State": 6}
    context = tuple(snap.items())
    first = CompassObservation("edge", "State", action, 6, 8, world, context, (gate, action))
    reordered = CompassObservation("edge", "State", action, 6, 8, world, context, (action, gate))

    compass, changed = Compass().apply((first,))
    assert changed
    same, changed = compass.apply((reordered,))
    assert same is compass
    assert not changed
    entry = tuple(compass.knowledge.tag_entries("State"))[0][2]
    assert entry.applied == (gate, action)


def test_duplicate_probe_evidence_requires_explicit_exhaustion_observation():
    from pyrung.core.analysis.pilot.compass import ProbeExhaustedObservation

    obs = CompassObservation("edge", "State", ("Cmd", True), 6, 8)
    compass, _ = Compass().apply((obs,))
    same, changed = compass.apply((obs,))
    assert same is compass
    assert changed is False

    key = ("state", 6)
    exhausted, changed = compass.apply((ProbeExhaustedObservation(key),))
    assert changed is True
    assert exhausted.knowledge.probe_count(key) == 1


def test_probe_budget_is_durable_world_scoped_knowledge():
    from pyrung.core.analysis.pilot.compass import ProbeExhaustedObservation

    key = ("state", 6)
    compass = Compass()
    for _ in range(2):
        compass, changed = compass.apply((ProbeExhaustedObservation(key),))
        assert changed
    assert compass.knowledge.probe_count(key) == 2
    assert compass.knowledge.probe_count(("other",)) == 0
