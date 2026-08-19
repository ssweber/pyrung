from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

import pyrung.core.analysis.pilot.options as options_module
from pyrung import PLC, Bool, Int, Program, copy, rung
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.pilot.availability import _WriterAvailability
from pyrung.core.analysis.pilot.awaited_actions import AwaitedAction, Producer
from pyrung.core.analysis.pilot.candidate_admission import (
    _admit_trace_details,
    _admit_wait_read,
    _effect_operation_batches,
    _separate_prerequisites,
)
from pyrung.core.analysis.pilot.candidate_read import (
    CandidateRead,
    PrerequisiteRead,
    RouteRead,
    WaitPrescription,
    WaitRead,
    _AdmittedWait,
    _Candidate,
    _LearnedAction,
    _LearnedWait,
    _PrerequisiteSeparation,
    _RouteAndCompletionRead,
    _TraceAdmission,
)
from pyrung.core.analysis.pilot.compass import (
    ActionNogoodObservation,
    Compass,
    CompassObservation,
    EvidenceScope,
    NavigationCatalog,
)
from pyrung.core.analysis.pilot.constrained_reachability import (
    NavigationEvidence,
    Reachable,
    StaticEdgeExclusionReason,
)
from pyrung.core.analysis.pilot.effects import (
    EffectPathStep,
    expectation_from_writer,
)
from pyrung.core.analysis.pilot.evidence import PipelineRoles, TransitionRoute
from pyrung.core.analysis.pilot.navigation_contracts import (
    ActSource,
    ChannelHeading,
    CrossingFidelity,
    LandingReceiptAuthority,
    NavigationConstraints,
    OrientationWorld,
    RouteEdgeContext,
    TargetSpec,
    pulse_identity,
)
from pyrung.core.analysis.pilot.options import (
    _assemble_candidate_read,
    _build_candidates,
    _candidate_applied,
    _read_learned_fallback,
    _read_route_and_wait,
    _select_wait,
    _unique_learned_expectation,
)
from pyrung.core.analysis.pilot.overlay import PilotRung
from pyrung.core.analysis.pilot.pipeline_graph import (
    StaticPath,
    StaticTransitionEdge,
    StaticTransitionGraph,
    _best_static_path,
    _build_action_lookup,
    _edges_from_routes,
)
from pyrung.core.analysis.pilot.program_step import (
    ProgramInputHandoff,
    ProgramStep,
    ProgramStepStatus,
)
from pyrung.core.analysis.pilot.route_options import (
    _compass_route_actions,
    _compass_route_plan,
    _general_chart_completion_plan,
)
from pyrung.core.analysis.pilot.trace import trace_back
from pyrung.core.analysis.pilot.trace_tree import (
    TraceAction,
    TraceCrossingBranch,
    TraceNode,
)
from pyrung.core.analysis.pilot.wait_options import _prescribe_wait
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
        "pyrung.core.analysis.pilot.pipeline_graph.expand_routes",
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
        evidence_scope=EvidenceScope.capture(
            world,
            {"State": 0, "Start": False, "Gate": False}.items(),
        ),
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


def test_static_path_can_exclude_an_edge_only_at_the_current_source() -> None:
    """A failed first Bearing may remain valid after another state transition."""

    role = PipelineRoles("State")
    graph = StaticTransitionGraph(
        role,
        (
            _wildcard_action_route(2, "Shortcut"),
            _action_route(0, 1, "Reset"),
            _route(2, 3),
        ),
    )

    preferred = graph.find_path(0, (3,), edge_allowed=lambda _edge: True)
    assert preferred is not None
    assert preferred.first_edge.action == ("Shortcut", True)

    shortcut = preferred.first_edge
    fallback = graph.find_path(
        0,
        (3,),
        edge_allowed=lambda _edge: True,
        first_edge_allowed=lambda edge: edge.identity != shortcut.identity,
    )

    assert fallback is not None
    assert fallback.first_edge.action == ("Reset", True)
    assert any(edge.identity == shortcut.identity for edge in fallback.edges[1:])


def test_theory_view_does_not_bypass_static_route_selection() -> None:
    """Theory evidence reaches navigation through Compass, not route mutation."""

    graph = StaticTransitionGraph(
        PipelineRoles("State"),
        (
            _wildcard_action_route(2, "Shortcut"),
            _action_route(0, 1, "Reset"),
            _route(2, 3),
        ),
    )
    frame = SimpleNamespace(
        key=("world",),
        snap={"State": 0, "Shortcut": False, "Reset": False},
        tree=TraceNode("State", 3, satisfied=False),
    )
    excluded = ("chart-edge", "shortcut")
    theory_view = SimpleNamespace(
        claim=SimpleNamespace(
            objective=SimpleNamespace(target_tag="State", target_value=3, frontier=()),
            obligations=(),
        ),
        requirements=(),
        excludes_first_edge=lambda artifact: artifact == excluded,
    )
    ctx = SimpleNamespace(
        compass=Compass(NavigationCatalog(graphs=(graph,))),
        opaque_loop=frozenset(),
        target=TargetSpec("State", 3),
        blocked_actions=frozenset(),
        avoid_pred=None,
        theory_view=theory_view,
    )

    unmapped = _compass_route_plan(frame, ctx)
    assert unmapped is None


def test_direct_target_chart_is_not_promoted_without_an_opaque_trace_boundary() -> None:
    graph = StaticTransitionGraph(
        PipelineRoles("State"),
        (_action_route(0, 2, "Advance"),),
    )
    frame = SimpleNamespace(
        key=("world",),
        snap={"State": 0, "Advance": False},
        tree=TraceNode("State", 2, satisfied=False),
    )
    ctx = SimpleNamespace(
        compass=Compass(NavigationCatalog(graphs=(graph,))),
        opaque_loop=frozenset(),
        target=TargetSpec("State", 2),
        blocked_actions=frozenset(),
        avoid_pred=None,
    )

    plan = _compass_route_plan(frame, ctx)

    assert plan is None


def test_general_chart_does_not_promote_an_unowned_action() -> None:
    graph = StaticTransitionGraph(
        PipelineRoles("State"),
        (
            _action_route(0, 2, "FirstAdvance"),
            _action_route(0, 2, "SecondAdvance"),
        ),
    )
    compass = Compass(NavigationCatalog(chart_graphs=(graph,)))
    frame = SimpleNamespace(
        key=("world",),
        snap={"State": 0, "FirstAdvance": False, "SecondAdvance": False},
        tree=TraceNode("State", 2, satisfied=False),
        raw_trace_action_details=(),
    )
    state = SimpleNamespace(pilot_rungs=(), earned_work=None, pending_departure=None)
    ctx = SimpleNamespace(
        compass=compass,
        opaque_loop=frozenset(),
        target=TargetSpec("State", 2),
        blocked_actions=frozenset(),
        avoid_pred=None,
        edge_tags=frozenset(),
        pdg=SimpleNamespace(downstream_slice=lambda *_args, **_kwargs: ()),
    )

    first = _read_route_and_wait(frame, state, ctx, set())
    assert first.route is None
    assert compass.knowledge.nogood_identities(frame.key) == frozenset()


def test_general_chart_bounds_a_trace_admitted_writer_without_minting_its_input() -> None:
    advance = Bool("ChartOwnedAdvance", external=True)
    state_tag = Int("ChartOwnedState")
    with Program() as program:
        with rung(state_tag == 0, advance):
            copy(2, state_tag)

    pdg = build_program_graph(program)
    route = TransitionRoute(
        destination_tag=state_tag.name,
        destination_value=2,
        request_tag=None,
        request_value=None,
        source_constraints=((state_tag.name, 0),),
        enablers=((advance.name, True),),
        action_tags=frozenset((advance.name,)),
        writer_node=0,
        writer_subroutine=None,
        call_site_gates=(),
        from_values=(0,),
    )
    graph = StaticTransitionGraph(PipelineRoles(state_tag.name), (route,))
    frame = SimpleNamespace(
        key=("world",),
        snap={state_tag.name: 0, advance.name: False},
        tree=TraceNode(
            state_tag.name,
            2,
            writer_rung=0,
            writer_availability=_WriterAvailability.AVAILABLE_NOW,
        ),
    )
    ctx = SimpleNamespace(
        compass=Compass(NavigationCatalog(chart_graphs=(graph,))),
        target=TargetSpec(state_tag.name, 2),
        blocked_actions=frozenset(),
        avoid_pred=None,
        pdg=pdg,
        program=program,
        steerable=frozenset((advance.name,)),
        opaque_loop=frozenset(),
    )
    state = SimpleNamespace(pilot_rungs=())

    plan = _general_chart_completion_plan(frame, ctx, set(), state=state)

    assert plan is not None
    assert plan.first_edge.action is None
    assert plan.first_edge.from_value == 0
    assert plan.first_edge.to_value == 2
    assert len(plan.first_edge.program_producers) == 1


def test_general_chart_only_nominates_a_writer_unavailable_from_this_channel_state() -> None:
    advance = Bool("ChartUnavailableAdvance", external=True)
    phase = Int("ChartUnavailablePhase")
    state_tag = Int("ChartUnavailableState")
    with Program() as program:
        with rung(state_tag == 0, phase == 1, advance):
            copy(2, state_tag)

    pdg = build_program_graph(program)
    route = TransitionRoute(
        destination_tag=state_tag.name,
        destination_value=2,
        request_tag=None,
        request_value=None,
        source_constraints=((state_tag.name, 0),),
        enablers=((phase.name, 1), (advance.name, True)),
        action_tags=frozenset((advance.name,)),
        writer_node=0,
        writer_subroutine=None,
        call_site_gates=(),
        from_values=(0,),
    )
    graph = StaticTransitionGraph(PipelineRoles(state_tag.name), (route,))
    frame = SimpleNamespace(
        key=("world",),
        snap={state_tag.name: 0, phase.name: 0, advance.name: True},
        tree=TraceNode(
            state_tag.name,
            2,
            writer_rung=0,
            writer_availability=_WriterAvailability.UNAVAILABLE_FROM_HERE,
        ),
    )
    ctx = SimpleNamespace(
        compass=Compass(NavigationCatalog(chart_graphs=(graph,))),
        target=TargetSpec(state_tag.name, 2),
        blocked_actions=frozenset(),
        avoid_pred=None,
        pdg=pdg,
        program=program,
        steerable=frozenset((advance.name,)),
        opaque_loop=frozenset((phase.name,)),
    )

    conservative = _general_chart_completion_plan(
        frame,
        ctx,
        set(),
        state=SimpleNamespace(pilot_rungs=()),
    )
    plan = _general_chart_completion_plan(
        frame,
        ctx,
        set(),
        state=SimpleNamespace(pilot_rungs=()),
        allow_conservative_nomination=True,
    )

    assert conservative is None
    assert plan is not None
    assert plan.first_edge.action is None
    assert len(plan.first_edge.program_producers) == 1


@pytest.mark.parametrize(
    ("general_destination", "prescribed", "expected_destination"),
    ((1, True, 1), (2, True, 2), (2, False, 1)),
)
def test_general_chart_only_competes_with_a_distinct_first_transition(
    monkeypatch,
    general_destination: int,
    prescribed: bool,
    expected_destination: int,
) -> None:
    """Same-edge geometry cannot replace its action; a distinct live carrier may."""

    role = PipelineRoles("State")
    static_edge = StaticTransitionGraph(
        role,
        (_action_route(0, 1, "ExactAction"),),
    ).edges[0]
    general_edge = replace(
        static_edge,
        to_value=general_destination,
        action=None,
        program_producers=(Producer(0, "program", frozenset(), "Effect", 1),),
    )
    static = StaticPath("State", 9, role, 9, (static_edge,))
    general = StaticPath("State", 9, role, 9, (general_edge,))
    required = TraceAction(
        "CurrentInput",
        True,
        availability=_WriterAvailability.AFTER_PREREQ,
    )
    step = ProgramStep(
        ProgramStepStatus.NEEDS_INPUT,
        producer=general_edge.program_producers[0],
        boundary=None,
        channel=None,
        required_inputs=(required,),
    )

    monkeypatch.setattr(options_module, "_compass_route_plan", lambda *_a, **_k: static)
    monkeypatch.setattr(
        options_module,
        "_general_chart_completion_plan",
        lambda *_a, **_k: general,
    )
    monkeypatch.setattr(options_module, "_live_chart_completion_edge", lambda *_a, **_k: None)
    monkeypatch.setattr(
        options_module,
        "_prescribe_wait",
        lambda *_a, **_k: WaitRead(
            (
                WaitPrescription(ChannelHeading("Boundary", 1), "exact current handoff")
                if prescribed
                else None
            ),
            (required,),
            program_step=step,
        ),
    )
    frame = SimpleNamespace(
        key=("world",),
        snap={"State": 0, "ExactAction": False, "CurrentInput": False},
        tree=TraceNode("Target", True, satisfied=False),
        raw_trace_action_details=(),
    )
    state = SimpleNamespace(
        pilot_rungs=(),
        earned_work=None,
        pending_departure=None,
    )
    ctx = SimpleNamespace(
        blocked_actions=frozenset(),
        edge_tags=frozenset(),
        compass=SimpleNamespace(),
    )

    read = _read_route_and_wait(frame, state, ctx, set())

    assert read.route is not None
    assert read.route.plan.first_edge.to_value == expected_destination
    assert (read.route.plan.first_edge.action is None) is (general_destination != 1 and prescribed)


def test_unbanked_broad_trace_keeps_ownership_over_a_shadow_chart() -> None:
    """A shadow chart cannot steal production ownership from the live trace."""

    graph = StaticTransitionGraph(
        PipelineRoles("State"),
        (_action_route(0, 2, "Advance"),),
    )
    frame = SimpleNamespace(
        key=("world",),
        snap={
            "State": 0,
            "Advance": False,
            "Broad": False,
            "NarrowA": False,
            "NarrowB": False,
        },
        tree=TraceNode("State", 2, satisfied=False),
        raw_trace_action_details=(
            TraceAction("Broad", True),
            TraceAction(
                "NarrowA",
                True,
                availability=_WriterAvailability.UNKNOWN,
            ),
            TraceAction(
                "NarrowB",
                True,
                availability=_WriterAvailability.UNKNOWN,
            ),
        ),
    )
    state = SimpleNamespace(
        pilot_rungs=(),
        earned_work=None,
        pending_departure=None,
    )
    ctx = SimpleNamespace(
        compass=Compass(NavigationCatalog(chart_graphs=(graph,))),
        opaque_loop=frozenset(),
        target=TargetSpec("State", 2),
        blocked_actions=frozenset(),
        avoid_pred=None,
        edge_tags=frozenset(),
        pdg=SimpleNamespace(
            downstream_slice=lambda tag, **_kwargs: range(100 if tag == "Broad" else 1),
        ),
    )

    read = _read_route_and_wait(frame, state, ctx, set())

    assert read.trace.actions == (
        ("Broad", True),
        ("NarrowA", True),
        ("NarrowB", True),
    )
    assert read.route is None


def test_current_non_broad_trace_keeps_ownership_over_a_chart() -> None:
    graph = StaticTransitionGraph(
        PipelineRoles("State"),
        (_action_route(0, 2, "Advance"),),
    )
    frame = SimpleNamespace(
        key=("world",),
        snap={"State": 0, "Advance": False, "Focused": False},
        tree=TraceNode("State", 2, satisfied=False),
        raw_trace_action_details=(TraceAction("Focused", True),),
    )
    state = SimpleNamespace(pilot_rungs=(), earned_work=None, pending_departure=None)
    ctx = SimpleNamespace(
        compass=Compass(NavigationCatalog(chart_graphs=(graph,))),
        opaque_loop=frozenset(),
        target=TargetSpec("State", 2),
        blocked_actions=frozenset(),
        avoid_pred=None,
        edge_tags=frozenset(),
        pdg=SimpleNamespace(downstream_slice=lambda *_args, **_kwargs: ()),
    )

    read = _read_route_and_wait(frame, state, ctx, set())

    assert read.trace.actions == (("Focused", True),)
    assert read.route is None


@pytest.mark.parametrize("relevance", ["frontier", "obligation", "requirement"])
def test_theory_channel_relevance_does_not_route_production(relevance: str) -> None:
    theory_target = Bool("TheoryRelevanceTarget", external=True)
    theory_sink = Int("TheoryRelevanceSink")
    with Program() as target_program:
        with rung(theory_target):
            copy(1, theory_sink)
    pdg = build_program_graph(target_program)
    snapshot = {
        theory_target.name: False,
        theory_sink.name: 0,
        "State": 0,
        "Advance": False,
    }
    graph = StaticTransitionGraph(
        PipelineRoles("State"),
        (_action_route(0, 2, "Advance"),),
    )
    frame = SimpleNamespace(
        key=("world",),
        snap=snapshot,
        tree=trace_back(
            theory_target.name,
            True,
            snapshot,
            pdg,
            target_program,
            frozenset((theory_target.name,)),
        ),
    )
    frontier = (("State", 2),) if relevance == "frontier" else ()
    obligations = (
        (SimpleNamespace(tag="State", value=2, required_shape=(), boundary=None),)
        if relevance == "obligation"
        else ()
    )
    requirements = (
        (
            SimpleNamespace(
                demanding_occurrence=("read", "State", 1),
                deadline_occurrence=("read", "Guard", 0),
                condition_identity=(
                    "pyrung.core.crossing",
                    "Eq",
                    (("tag", "State"), ("values", (2,))),
                ),
            ),
        )
        if relevance == "requirement"
        else ()
    )
    theory_view = SimpleNamespace(
        claim=SimpleNamespace(
            objective=SimpleNamespace(
                target_tag=theory_target.name,
                target_value=True,
                frontier=frontier,
            ),
            obligations=obligations,
        ),
        requirements=requirements,
        excludes_first_edge=lambda _artifact: False,
    )
    ctx = SimpleNamespace(
        compass=Compass(NavigationCatalog(graphs=(graph,))),
        opaque_loop=frozenset(),
        target=TargetSpec(theory_target.name, True),
        blocked_actions=frozenset(),
        avoid_pred=None,
        theory_view=theory_view,
    )

    plan = _compass_route_plan(frame, ctx)

    assert plan is None


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
        (
            CompassObservation(
                "no_change",
                "State",
                ("Start", True),
                0,
                applied=(("Start", True),),
            ),
        )
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
    compass, _ = Compass().apply(
        (CompassObservation("edge", "State", action, 6, 8, applied=(action,)),)
    )
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
    scope = EvidenceScope.capture(world, snap.items())
    assert unchanged.knowledge.static_edge_status(edge, evidence_scope=scope) is None

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
    assert interfered.knowledge.static_edge_status(edge, evidence_scope=scope) is None

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
    assert scoped.knowledge.static_edge_status(edge, evidence_scope=scope) == "contradicted"
    assert (
        scoped.knowledge.static_edge_status(
            edge,
            evidence_scope=EvidenceScope.capture(other_world, snap.items()),
        )
        is None
    )


def test_wait_nogood_walks_around_the_sterile_completion_edge() -> None:
    """A rejected wait is remembered at its world key; the next ORIENT's route
    query excludes the sterile automatic edge and falls to the surviving
    operator route (the Unhold shape at a held state)."""
    from pyrung.core.analysis.pilot.world_key import wait_edge_nogood

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


def test_compass_route_plan_builds_evidence_scope_once(monkeypatch) -> None:
    """Every edge in one orientation shares one exact-world scope key."""

    role = PipelineRoles("State")
    graph = StaticTransitionGraph(
        role,
        (
            _route(0, 1),
            _route(1, 2),
            _action_route(0, 2, "Skip"),
        ),
    )
    frame = SimpleNamespace(
        key=("world", "recipe"),
        snap={"State": 0, "Skip": False, "Timer_Acc": 1234},
        tree=TraceNode("State", 2, satisfied=False),
    )
    ctx = SimpleNamespace(
        compass=Compass(NavigationCatalog(graphs=(graph,))),
        opaque_loop=frozenset({"State"}),
        target=TargetSpec("State", 2),
        blocked_actions=frozenset(),
        avoid_pred=None,
    )
    builds = 0

    original_capture = EvidenceScope.capture

    def counted_capture(cls, world_key, context=None):
        nonlocal builds
        builds += 1
        return original_capture(world_key, context)

    monkeypatch.setattr(EvidenceScope, "capture", classmethod(counted_capture))

    plan = _compass_route_plan(frame, ctx)

    assert plan is not None
    assert builds == 1


def test_program_owned_sibling_preserves_an_automatic_edge() -> None:
    role = PipelineRoles("State")
    route = _action_route(6, 16, "Complete")
    producer = Producer(
        rung_index=7,
        kind="program",
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
    never invents chart evidence."""
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
    state = SimpleNamespace(pilot_rungs=[])
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


def test_wait_selection_keeps_its_three_evidence_sources_explicit() -> None:
    route = RouteEdgeContext("State", 6, 16)
    charted = WaitRead(
        WaitPrescription(
            ChannelHeading("State", 16, route=route),
            "charted completion",
        )
    )
    boundary = ChannelHeading("Accumulated", 5, boundary=object())
    learned = _LearnedWait(WaitRead(WaitPrescription(None, "learned transition")))

    selected = _select_wait(
        charted_completion=charted,
        instruction_boundary=boundary,
        learned=learned,
        has_candidates=False,
    )
    assert selected is learned.read

    charted_selected = _select_wait(
        charted_completion=charted,
        instruction_boundary=boundary,
        learned=None,
        has_candidates=True,
    )
    assert charted_selected is not None
    assert charted_selected.reason == "charted completion"
    assert charted_selected.prescription is not None
    assert charted_selected.prescription.heading is not None
    assert charted_selected.prescription.heading.channel_tag == "Accumulated"
    assert charted_selected.prescription.heading.route is route

    declined_learned = _LearnedWait(
        WaitRead(None, declined_reason="learned transition is not coastable")
    )
    assert (
        _select_wait(
            charted_completion=charted,
            instruction_boundary=None,
            learned=declined_learned,
            has_candidates=False,
        )
        is charted
    )

    boundary_selected = _select_wait(
        charted_completion=None,
        instruction_boundary=boundary,
        learned=None,
        has_candidates=False,
    )
    assert boundary_selected is not None
    assert boundary_selected.reason == "advance Accumulated to its next boundary 5"
    assert boundary_selected.prescription is not None
    assert boundary_selected.prescription.heading is boundary
    assert (
        _select_wait(
            charted_completion=None,
            instruction_boundary=boundary,
            learned=None,
            has_candidates=True,
        )
        is None
    )


def test_candidate_assembly_consumes_awaited_action_without_rereading(
    monkeypatch,
) -> None:
    import pyrung.core.analysis.pilot.options as options

    admission = _TraceAdmission(
        active_actions=(),
        actions=(),
        details=(),
        detail_by_pair={},
        managed_boolean_rungs=(),
        establish_pending=False,
    )
    route_and_wait = _RouteAndCompletionRead(admission, None, None)
    separated = _PrerequisiteSeparation(
        admission,
        PrerequisiteRead(),
        None,
    )
    awaited_action = AwaitedAction(
        action=("Acknowledge", True),
        command_tag="Command",
        command_value=True,
        command_writes=(("Command", True),),
        from_state=11,
        to_state=12,
        note="program awaits Acknowledge",
    )
    frame = SimpleNamespace(snap={"Acknowledge": False})
    ctx = SimpleNamespace(
        blocked_actions=frozenset(),
        edge_tags=frozenset(),
        pdg=SimpleNamespace(
            downstream_slice=lambda *_args, **_kwargs: (),
            writers_of={},
        ),
        target=TargetSpec("State", 17),
    )
    monkeypatch.setattr(
        options,
        "_awaited_action_bearing",
        lambda *_args: (_ for _ in ()).throw(AssertionError("unexpected awaited-action re-read")),
    )

    read = _assemble_candidate_read(
        route_and_wait,
        separated,
        None,
        awaited_action,
        frame,
        ctx,
        set(),
    )

    assert tuple(option.pair for option in read.options) == (("Acknowledge", True),)
    assert read.options[0].awaited_action_prescribed
    assert read.options[0].awaited_action_note == "program awaits Acknowledge"


def test_awaited_required_shape_uses_exact_live_multivalue_guard(monkeypatch) -> None:
    import pyrung.core.analysis.pilot.awaited_actions as awaited

    transition = awaited._Transition(
        to_value=12,
        command_guards={"Written": frozenset({1}), "Live": frozenset({3, 5})},
        target_tag="Request",
        writer_node=7,
    )
    world = SimpleNamespace(
        snapshot={"Channel": 11, "Live": 5, "Push": False},
        steerable=frozenset({"Push"}),
    )
    monkeypatch.setattr(awaited, "_state_transitions", lambda *_args: [transition])
    monkeypatch.setattr(awaited, "_button_writes", lambda *_args: {"Written": 1})

    readings = awaited.awaited_actions(world, "Channel", ())

    assert len(readings) == 1
    assert readings[0].required_shape == (("Written", 1), ("Live", 5))

    missing = SimpleNamespace(
        snapshot={"Channel": 11, "Push": False},
        steerable=world.steerable,
    )
    assert awaited.awaited_actions(missing, "Channel", ()) == ()


def test_awaited_same_action_multiple_writers_is_order_independent_ambiguity(
    monkeypatch,
) -> None:
    import pyrung.core.analysis.pilot.awaited_actions as awaited
    from pyrung.core.analysis.pilot.compass import unique_legal_awaited_action

    first = awaited._Transition(12, {"Command": frozenset({1})}, "RequestA", 7)
    second = awaited._Transition(13, {"Command": frozenset({1})}, "RequestB", 3)
    world = SimpleNamespace(
        snapshot={"Channel": 11, "Push": False},
        steerable=frozenset({"Push"}),
    )
    monkeypatch.setattr(awaited, "_button_writes", lambda *_args: {"Command": 1})

    for transitions in ((first, second), (second, first)):
        monkeypatch.setattr(
            awaited,
            "_state_transitions",
            lambda *_args, _transitions=transitions: list(_transitions),
        )
        assert (
            unique_legal_awaited_action(
                world,
                "Channel",
                (),
                action_avoided=lambda _action: False,
            )
            is None
        )


def _effect_collision_fixture():
    action = Bool("ReceiptCollisionAction", external=True)
    route_effect = Int("ReceiptRouteRequest")
    trace_effect = Int("ReceiptTraceEffect")
    program_effect = Int("ReceiptProgramEffect")
    with Program() as program:
        with rung(action):
            copy(7, route_effect)
        with rung(action):
            copy(9, trace_effect)
        with rung(action):
            copy(11, program_effect)
    return action, route_effect, trace_effect, program_effect, program, build_program_graph(program)


def _collision_context(program, pdg):
    return SimpleNamespace(
        blocked_actions=frozenset(),
        edge_tags=frozenset(),
        pdg=pdg,
        program=program,
        target=TargetSpec("State", 17),
    )


def test_route_request_candidate_owns_route_writer_not_same_pair_trace() -> None:
    action, request, trace_effect, _program_effect, program, pdg = _effect_collision_fixture()
    pair = (action.name, True)
    trace_detail = TraceAction(
        *pair,
        effect_path=(EffectPathStep(1, trace_effect.name, 9),),
    )
    admission = _TraceAdmission(
        active_actions=(pair,),
        actions=(pair,),
        details=(trace_detail,),
        detail_by_pair={pair: trace_detail},
        managed_boolean_rungs=(),
        establish_pending=False,
    )
    role = PipelineRoles("State", request_tags=frozenset({request.name}))
    route = TransitionRoute(
        destination_tag="State",
        destination_value=17,
        request_tag=request.name,
        request_value=7,
        source_constraints=(("State", 6),),
        enablers=(pair,),
        action_tags=frozenset({action.name}),
        writer_node=0,
        writer_subroutine=None,
        call_site_gates=(),
        from_values=(6,),
    )
    edge = StaticTransitionEdge(
        role=role,
        from_value=6,
        to_value=17,
        action=pair,
        request_tag=request.name,
        request_value=7,
        source_constraints=(("State", 6),),
        enablers=(pair,),
        route=route,
    )
    plan = StaticPath("State", 17, role, 17, (edge,))
    separated = _PrerequisiteSeparation(admission, PrerequisiteRead(), None)

    read = _assemble_candidate_read(
        _RouteAndCompletionRead(admission, RouteRead(plan, (pair,)), None),
        separated,
        None,
        None,
        SimpleNamespace(snap={action.name: False}, tree=TraceNode("State", 17)),
        _collision_context(program, pdg),
        set(),
    )

    obligation = read.options[0].expectation.obligations[0]
    assert obligation.tag == request.name
    assert obligation.producer == (None, 0, ())
    assert obligation.producer != (None, 1, ())


def test_writer_expectation_rejects_effect_owned_by_another_writer() -> None:
    _action, request, trace_effect, _program_effect, program, pdg = _effect_collision_fixture()

    assert (
        expectation_from_writer(
            pdg,
            program,
            writer_node=0,
            tag=trace_effect.name,
            value=9,
        )
        is None
    )
    expectation = expectation_from_writer(
        pdg,
        program,
        writer_node=0,
        tag=request.name,
        value=7,
    )
    assert expectation is not None


def test_awaited_candidate_owns_awaited_writer_not_same_pair_trace() -> None:
    action, _request, trace_effect, program_effect, program, pdg = _effect_collision_fixture()
    pair = (action.name, True)
    trace_detail = TraceAction(
        *pair,
        effect_path=(EffectPathStep(1, trace_effect.name, 9),),
    )
    admission = _TraceAdmission(
        active_actions=(),
        actions=(),
        details=(trace_detail,),
        detail_by_pair={pair: trace_detail},
        managed_boolean_rungs=(),
        establish_pending=False,
    )
    awaited = AwaitedAction(
        pair,
        "Command",
        True,
        (("Command", True),),
        6,
        17,
        "await exact writer",
        target_tag=program_effect.name,
        writer_node=2,
        required_shape=(("Command", True),),
    )

    read = _assemble_candidate_read(
        _RouteAndCompletionRead(admission, None, None),
        _PrerequisiteSeparation(admission, PrerequisiteRead(), None),
        None,
        awaited,
        SimpleNamespace(snap={action.name: False}, tree=TraceNode("State", 17)),
        _collision_context(program, pdg),
        set(),
    )

    obligation = read.options[0].expectation.obligations[0]
    assert obligation.tag == program_effect.name
    assert obligation.producer == (None, 2, ())
    assert obligation.required_shape == (("Command", True),)
    assert obligation.boundary == (program_effect.name, 17)
    assert read.options[0].bearing_channel_tag == program_effect.name


def test_program_input_candidate_owns_required_input_path_and_broad_paths_survive() -> None:
    action, request, trace_effect, program_effect, program, pdg = _effect_collision_fixture()
    pair = (action.name, True)
    program_detail = TraceAction(
        *pair,
        effect_path=(EffectPathStep(2, program_effect.name, 11),),
    )
    trace_detail = TraceAction(
        *pair,
        effect_path=(EffectPathStep(1, trace_effect.name, 9),),
    )
    step = ProgramStep(
        ProgramStepStatus.NEEDS_INPUT,
        pdg.rung_nodes[2],
        None,
        None,
        required_inputs=(program_detail,),
        input_handoffs=(
            ProgramInputHandoff(pair, Eq(program_effect.name, frozenset({11})), "State"),
        ),
    )
    admission = _TraceAdmission(
        active_actions=(pair,),
        actions=(pair,),
        details=(trace_detail,),
        detail_by_pair={pair: trace_detail},
        managed_boolean_rungs=(),
        establish_pending=False,
    )
    wait = WaitRead(None, program_step=step)
    read = _assemble_candidate_read(
        _RouteAndCompletionRead(admission, None, wait),
        _PrerequisiteSeparation(admission, PrerequisiteRead(), None),
        None,
        None,
        SimpleNamespace(snap={action.name: False}, tree=TraceNode("State", 17)),
        _collision_context(program, pdg),
        set(),
    )
    obligation = read.options[0].expectation.obligations[0]
    assert obligation.tag == program_effect.name
    assert obligation.producer == (None, 2, ())

    broad = ("BroadReceipt", True)
    low_a = ("LowReceiptA", True)
    low_b = ("LowReceiptB", True)
    first = TraceAction(*broad, effect_path=(EffectPathStep(0, request.name, 7),))
    second = TraceAction(*broad, effect_path=(EffectPathStep(1, trace_effect.name, 9),))
    broad_admission = replace(
        admission,
        active_actions=(broad, low_a, low_b),
        actions=(broad, low_a, low_b),
        details=(first, second),
        detail_by_pair={broad: first},
    )
    broad_ctx = SimpleNamespace(
        **{
            **vars(_collision_context(program, pdg)),
            "pdg": SimpleNamespace(
                rung_nodes=pdg.rung_nodes,
                downstream_slice=lambda tag, **_kwargs: (
                    tuple(range(100)) if tag == broad[0] else ()
                ),
            ),
        }
    )
    broad_read = _assemble_candidate_read(
        _RouteAndCompletionRead(broad_admission, None, None),
        _PrerequisiteSeparation(broad_admission, PrerequisiteRead(), None),
        None,
        None,
        SimpleNamespace(snap={}, tree=TraceNode("Target", True)),
        broad_ctx,
        set(),
    )
    broad_options = [option for option in broad_read.options if option.pair == broad]
    assert [option.expectation.obligations[0].producer for option in broad_options] == [
        (None, 0, ()),
        (None, 1, ()),
    ]


def test_current_program_input_precedes_unavailable_outer_trace_leaf() -> None:
    current = Bool("CurrentProgramInput", external=True)
    later = Bool("UnavailableOuterInput", external=True)
    first_effect = Int("CurrentProgramEffect")
    later_effect = Int("UnavailableOuterEffect")
    with Program() as program:
        with rung(current):
            copy(1, first_effect)
        with rung(later):
            copy(2, later_effect)
    pdg = build_program_graph(program)
    current_pair = (current.name, True)
    later_pair = (later.name, True)
    current_detail = TraceAction(
        *current_pair,
        effect_path=(EffectPathStep(0, first_effect.name, 1),),
    )
    later_detail = TraceAction(
        *later_pair,
        availability=_WriterAvailability.UNAVAILABLE_FROM_HERE,
        effect_path=(EffectPathStep(1, later_effect.name, 2),),
    )
    producer = Producer(
        0,
        "program",
        frozenset(),
        first_effect.name,
        1,
    )
    step = ProgramStep(
        ProgramStepStatus.NEEDS_INPUT,
        producer,
        None,
        first_effect.name,
        required_inputs=(current_detail,),
    )
    admission = _TraceAdmission(
        active_actions=(later_pair, current_pair),
        actions=(later_pair, current_pair),
        details=(later_detail, current_detail),
        detail_by_pair={later_pair: later_detail, current_pair: current_detail},
        managed_boolean_rungs=(),
        establish_pending=False,
    )
    read = _assemble_candidate_read(
        _RouteAndCompletionRead(
            admission,
            None,
            WaitRead(None, program_step=step),
        ),
        _PrerequisiteSeparation(admission, PrerequisiteRead(), None),
        None,
        None,
        SimpleNamespace(
            snap={current.name: False, later.name: False},
            tree=TraceNode(later_effect.name, 2),
        ),
        _collision_context(program, pdg),
        set(),
    )

    assert tuple((option.pair, option.source) for option in read.options) == (
        (current_pair, ActSource.PROGRAM),
        (later_pair, ActSource.TRACE),
    )


def test_route_artifact_does_not_fold_target_wide_trace_leaves() -> None:
    route_pair = ("RouteCommand", True)
    edge_gate = ("RouteEdgeGate", True)
    unrelated = (("LaterMode", True), ("UnavailableProcessInput", -1))
    role = PipelineRoles("RouteState")
    transition = TransitionRoute(
        destination_tag="RouteState",
        destination_value=1,
        request_tag=None,
        request_value=None,
        source_constraints=(("RouteState", 9),),
        enablers=(route_pair, edge_gate),
        action_tags=frozenset(pair[0] for pair in (route_pair, edge_gate)),
        writer_node=0,
        writer_subroutine=None,
        call_site_gates=(edge_gate,),
        from_values=(9,),
    )
    edge = StaticTransitionEdge(
        role=role,
        from_value=9,
        to_value=1,
        action=route_pair,
        request_tag=None,
        request_value=None,
        source_constraints=transition.source_constraints,
        enablers=transition.enablers,
        route=transition,
        co_actions=(edge_gate,),
    )
    route = RouteRead(
        StaticPath("RouteState", 1, role, 1, (edge,)),
        (route_pair,),
        (edge_gate,),
    )
    admission = _TraceAdmission(
        active_actions=unrelated,
        actions=unrelated,
        details=tuple(TraceAction(*pair) for pair in unrelated),
        detail_by_pair={pair: TraceAction(*pair) for pair in unrelated},
        managed_boolean_rungs=(),
        establish_pending=False,
    )
    candidates = CandidateRead(
        trace=admission,
        options=(_Candidate(*route_pair, ActSource.ROUTE),),
        downstream_reach_cap=20,
        route=route,
        prerequisites=PrerequisiteRead(
            pilot_rungs=(PilotRung("ExactSteadyPrerequisite", True, object()),)
        ),
    )
    ctx = SimpleNamespace(compass=SimpleNamespace(action_tags=frozenset({route_pair[0]})))

    assert _candidate_applied(candidates.options[0], candidates, ctx) == (
        route_pair,
        edge_gate,
        ("ExactSteadyPrerequisite", True),
    )


def test_widening_expectation_is_scoped_to_exact_artifact_primary_path() -> None:
    action_a = Bool("WidenA", external=True)
    action_b = Bool("WidenB", external=True)
    action_c = Bool("WidenC", external=True)
    effect_a = Int("WidenEffectA")
    effect_alt = Int("WidenEffectAlt")
    effect_c = Int("WidenEffectC")
    with Program() as program:
        with rung(action_a):
            copy(1, effect_a)
        with rung(action_a):
            copy(2, effect_alt)
        with rung(action_c):
            copy(3, effect_c)
    pdg = build_program_graph(program)
    active = ((action_a.name, True), (action_b.name, True), (action_c.name, True))
    detail_a = TraceAction(*active[0], effect_path=(EffectPathStep(0, effect_a.name, 1),))
    detail_alt = TraceAction(*active[0], effect_path=(EffectPathStep(1, effect_alt.name, 2),))
    detail_c = TraceAction(*active[2], effect_path=(EffectPathStep(2, effect_c.name, 3),))

    def assemble(details):
        admission = _TraceAdmission(
            active_actions=active,
            actions=active,
            details=details,
            detail_by_pair={detail.pair: detail for detail in details},
            managed_boolean_rungs=(),
            establish_pending=False,
        )
        return _assemble_candidate_read(
            _RouteAndCompletionRead(admission, None, None),
            _PrerequisiteSeparation(admission, PrerequisiteRead(), None),
            None,
            None,
            SimpleNamespace(snap={}, tree=TraceNode("Target", True)),
            _collision_context(program, pdg),
            set(),
        )

    # A later member's path cannot become the width-2 artifact's promise.
    assert assemble((detail_c,)).widening_expectations == ()
    # Same-pair producer alternatives are ambiguity, not iteration-order choice.
    assert assemble((detail_a, detail_alt, detail_c)).widening_expectations == ()

    exact = assemble((detail_a, detail_c)).widening_expectations
    assert tuple(artifact for artifact, _expectation in exact) == (active[:2], active[:3])
    assert all(
        expectation.obligations[0].producer == (None, 0, ()) for _artifact, expectation in exact
    )


def test_two_hop_learned_path_retains_first_edge_expectation() -> None:
    first_action = ("FirstHop", True)
    second_action = ("SecondHop", True)
    _action, request, trace_effect, _program_effect, program, pdg = _effect_collision_fixture()
    first_expectation = expectation_from_writer(
        pdg, program, writer_node=0, tag=request.name, value=7
    )
    second_expectation = expectation_from_writer(
        pdg, program, writer_node=1, tag=trace_effect.name, value=9
    )
    assert first_expectation is not None and second_expectation is not None
    compass = Compass()
    compass, _changed = compass.apply(
        (
            CompassObservation(
                "edge",
                "LearnedState",
                first_action,
                0,
                1,
                None,
                (),
                (first_action,),
                first_expectation,
            ),
            CompassObservation(
                "edge",
                "LearnedState",
                second_action,
                1,
                2,
                None,
                (),
                (second_action,),
                second_expectation,
            ),
        )
    )
    admission = _TraceAdmission((), (), (), {}, (), False)
    frame = SimpleNamespace(
        key=("learned",),
        snap={"LearnedState": 0},
        tree=TraceNode("LearnedState", 2),
    )
    ctx = SimpleNamespace(
        compass=compass,
        blocked_actions=frozenset(),
        avoid_pred=None,
    )

    learned = _read_learned_fallback(
        _RouteAndCompletionRead(admission, None, None),
        _PrerequisiteSeparation(admission, PrerequisiteRead(), None),
        frame,
        SimpleNamespace(),
        ctx,
        set(),
    )

    assert isinstance(learned, _LearnedAction)
    assert learned.action == first_action
    assert learned.expectation is first_expectation


def test_learned_first_edge_requires_one_semantic_expectation_regardless_of_order() -> None:
    _action, request, trace_effect, _program_effect, program, pdg = _effect_collision_fixture()
    cause = ("SharedCause", True)
    first = expectation_from_writer(
        pdg,
        program,
        writer_node=0,
        tag=request.name,
        value=7,
    )
    second = expectation_from_writer(
        pdg,
        program,
        writer_node=1,
        tag=trace_effect.name,
        value=9,
    )
    repeated = expectation_from_writer(
        pdg,
        program,
        writer_node=0,
        tag=request.name,
        value=7,
    )
    assert first is not None and second is not None and repeated is not None

    def entry(expectation):
        return (0, cause, SimpleNamespace(to_val=1, expectation=expectation))

    for entries in ((entry(first), entry(second)), (entry(second), entry(first))):
        assert (
            _unique_learned_expectation(
                entries,
                source=0,
                cause=cause,
                destination=1,
            )
            is None
        )

    retained = _unique_learned_expectation(
        (entry(first), entry(repeated)),
        source=0,
        cause=cause,
        destination=1,
    )
    assert retained is first


def test_program_owned_coasts_promise_command_producer_only_when_observed(monkeypatch) -> None:
    import pyrung.core.analysis.pilot.program_step as program_step

    automatic = Bool("ProgramCoastAutomatic", external=True)
    command = Int("ProgramCoastCommand")
    state_tag = Int("ProgramCoastState")
    first_guard = Int("ProgramCoastFirstGuard", default=1)
    last_guard = Int("ProgramCoastLastGuard", default=2)
    input_action = Bool("ProgramCoastInput", external=True)
    with Program() as program:
        with rung(automatic):
            copy(7, command)
        # Deliberately disagrees with edge category/tag order and repeats the
        # command read.  The promise must follow consumer evaluation order.
        with rung(last_guard == 2, command == 7, first_guard == 1, command == 7):
            copy(17, state_tag)
    pdg = build_program_graph(program)
    producer = Producer(0, "program", frozenset(), command.name, 7)
    role = PipelineRoles(state_tag.name)
    route = TransitionRoute(
        destination_tag=state_tag.name,
        destination_value=17,
        request_tag=None,
        request_value=None,
        source_constraints=((first_guard.name, 1), (command.name, 7)),
        enablers=((last_guard.name, 2),),
        action_tags=frozenset(),
        writer_node=1,
        writer_subroutine=None,
        call_site_gates=(),
        from_values=(6,),
    )
    edge = StaticTransitionEdge(
        role=role,
        from_value=6,
        to_value=17,
        action=None,
        request_tag=None,
        request_value=None,
        source_constraints=((first_guard.name, 1), (command.name, 7)),
        enablers=((last_guard.name, 2),),
        route=route,
        program_producers=(producer,),
    )
    boundary = Eq(state_tag.name, frozenset({17}))
    base = dict(
        producer=producer,
        boundary=boundary,
        channel=state_tag.name,
    )
    steps = (
        (ProgramStep(ProgramStepStatus.KEEP_RUNNING, **base), False),
        (
            ProgramStep(ProgramStepStatus.KEEP_RUNNING, **base, producer_observed=True),
            True,
        ),
        (
            ProgramStep(
                ProgramStepStatus.NEEDS_INPUT,
                **base,
                producer_observed=True,
                required_inputs=(TraceAction(input_action.name, True),),
                input_handoffs=(
                    ProgramInputHandoff((input_action.name, True), boundary, state_tag.name),
                ),
            ),
            True,
        ),
        (
            ProgramStep(
                ProgramStepStatus.INTERRUPTED,
                **base,
                producer_observed=True,
            ),
            True,
        ),
    )
    ctx = SimpleNamespace(
        pdg=pdg,
        program=program,
        steerable=frozenset({input_action.name}),
        opaque_loop=frozenset(),
        domain_prior=None,
        clear_only=frozenset(),
        pipeline_internal_tags=frozenset(),
        pipeline_roles=(),
        avoid_pred=None,
        resting={input_action.name: False},
    )
    frame = SimpleNamespace(
        snap={
            state_tag.name: 6,
            command.name: 0,
            first_guard.name: 1,
            last_guard.name: 2,
        }
    )
    state = SimpleNamespace(work=SimpleNamespace(), pilot_rungs=())

    for step, expected in steps:
        monkeypatch.setattr(
            program_step, "read_program_step", lambda *_args, _step=step, **_kw: _step
        )
        read = _prescribe_wait(edge, frame, state, ctx)
        assert read.prescription is not None
        assert read.prescription.landing_receipt_authority is LandingReceiptAuthority.PROGRAM_STEP
        expectation = read.prescription.expectation
        if not expected:
            assert expectation is None
            continue
        assert expectation is not None
        obligation = expectation.obligations[0]
        assert obligation.tag == command.name
        assert obligation.value == 7
        assert obligation.producer == (None, 0, ())
        assert obligation.consumer == (None, 1, ())
        assert obligation.required_shape == (
            (last_guard.name, 2),
            (command.name, 7),
            (first_guard.name, 1),
            (command.name, 7),
        )


def test_observed_route_writer_keeps_its_direct_expectation(monkeypatch) -> None:
    """The route writer is an effect owner, not its own downstream consumer."""

    import pyrung.core.analysis.pilot.program_step as program_step

    state_tag = Int("DirectRouteWriterState", default=50)
    ready = Bool("DirectRouteWriterReady", default=True)
    with Program() as program:
        with rung(state_tag == 50, ready):
            copy(60, state_tag)

    pdg = build_program_graph(program)
    producer = Producer(
        0,
        "program",
        frozenset(),
        state_tag.name,
        60,
    )
    role = PipelineRoles(state_tag.name)
    route = TransitionRoute(
        destination_tag=state_tag.name,
        destination_value=60,
        request_tag=None,
        request_value=None,
        source_constraints=((state_tag.name, 50),),
        enablers=((ready.name, True),),
        action_tags=frozenset(),
        writer_node=0,
        writer_subroutine=None,
        call_site_gates=(),
        from_values=(50,),
    )
    edge = StaticTransitionEdge(
        role=role,
        from_value=50,
        to_value=60,
        action=None,
        request_tag=None,
        request_value=None,
        source_constraints=route.source_constraints,
        enablers=route.enablers,
        route=route,
        program_producers=(producer,),
    )
    step = ProgramStep(
        ProgramStepStatus.INTERRUPTED,
        producer=producer,
        boundary=None,
        channel=state_tag.name,
        producer_observed=True,
        projected_changes=((state_tag.name, 50, 92),),
        preserve_channels=(state_tag.name,),
    )
    monkeypatch.setattr(program_step, "read_program_step", lambda *_args, **_kw: step)
    ctx = SimpleNamespace(
        pdg=pdg,
        program=program,
        steerable=frozenset(),
        opaque_loop=frozenset(),
        domain_prior=None,
        clear_only=frozenset(),
        pipeline_internal_tags=frozenset(),
        pipeline_roles=(),
        avoid_pred=None,
        resting={},
    )
    frame = SimpleNamespace(snap={state_tag.name: 50, ready.name: True})
    state = SimpleNamespace(work=SimpleNamespace(), pilot_rungs=())

    read = _prescribe_wait(edge, frame, state, ctx)

    assert read.prescription is not None
    expectation = read.prescription.expectation
    assert expectation is not None
    obligation = expectation.obligations[0]
    assert (obligation.tag, obligation.value) == (state_tag.name, 60)
    assert obligation.producer == (None, 0, ())
    assert obligation.consumer is None
    assert obligation.boundary == (state_tag.name, 60)


def test_charted_edge_receipt_ends_at_unique_automatic_successor() -> None:
    """A chart coordinate may conduct through its exact next consumer."""

    state_tag = Int("ChartedSuccessorState", default=40)
    advance = Bool("ChartedSuccessorAdvance", external=True)
    ready = Bool("ChartedSuccessorReady", default=True)
    with Program() as program:
        with rung(state_tag == 40, advance):
            copy(41, state_tag)
        with rung(state_tag == 41, ready):
            copy(50, state_tag)
        with rung(state_tag == 50):
            copy(81, state_tag)

    pdg = build_program_graph(program)
    role = PipelineRoles(state_tag.name)

    def route(
        source: int,
        destination: int,
        writer: int,
        *,
        enablers=(),
        action_tags=frozenset(),
    ) -> TransitionRoute:
        return TransitionRoute(
            destination_tag=state_tag.name,
            destination_value=destination,
            request_tag=None,
            request_value=None,
            source_constraints=((state_tag.name, source),),
            enablers=enablers,
            action_tags=action_tags,
            writer_node=writer,
            writer_subroutine=None,
            call_site_gates=(),
            from_values=(source,),
        )

    selected_route = route(
        40,
        41,
        0,
        enablers=((advance.name, True),),
        action_tags=frozenset({advance.name}),
    )
    graph = StaticTransitionGraph(
        role,
        (
            selected_route,
            route(41, 50, 1, enablers=((ready.name, True),)),
            route(50, 81, 2),
        ),
    )
    selected_edge = next(
        edge for edge in graph.edges if edge.from_value == 40 and edge.to_value == 41
    )
    ctx = SimpleNamespace(
        pdg=pdg,
        program=program,
        # Public scalar targets retain their compiled predicate as well as the
        # concrete chart coordinate.
        target=TargetSpec(state_tag.name, 81, lambda snap: snap[state_tag.name] == 81),
        compass=Compass(NavigationCatalog(chart_graphs=(graph,))),
    )

    expectation = options_module._expectation_from_route_writer(
        ctx,
        selected_edge,
        {
            state_tag.name: 40,
            advance.name: False,
            ready.name: True,
        },
    )

    assert expectation is not None
    obligation = expectation.obligations[0]
    assert obligation.producer == (None, 0, ())
    assert obligation.consumer == (None, 1, ())
    assert obligation.required_shape == (
        (state_tag.name, 41),
        (ready.name, True),
    )


def test_crossing_batch_bypasses_pair_nogood_but_honors_explicit_block() -> None:
    admission = _TraceAdmission(
        active_actions=(),
        actions=(),
        details=(),
        detail_by_pair={},
        managed_boolean_rungs=(),
        establish_pending=False,
    )
    route_and_wait = _RouteAndCompletionRead(admission, None, None)
    separated = _PrerequisiteSeparation(admission, PrerequisiteRead(), None)
    branch = TraceCrossingBranch(
        actions=(TraceAction("A", 1), TraceAction("B", 1)),
        fidelity=CrossingFidelity(
            constraints=(),
            reason="grouped",
            verify_required=True,
            exact=None,
            proposed=True,
        ),
    )
    frame = SimpleNamespace(
        snap={"A": 0, "B": 0},
        tree=TraceNode(
            "Target",
            True,
            relational=True,
            crossing_branches=(branch,),
        ),
    )
    base_ctx = SimpleNamespace(
        blocked_actions=frozenset(),
        edge_tags=frozenset(),
        pdg=SimpleNamespace(
            downstream_slice=lambda *_args, **_kwargs: (),
            writers_of={},
        ),
        target=TargetSpec("Target", True),
    )

    admitted = _assemble_candidate_read(
        route_and_wait,
        separated,
        None,
        None,
        frame,
        base_ctx,
        {("A", 1)},
    )
    blocked = _assemble_candidate_read(
        route_and_wait,
        separated,
        None,
        None,
        frame,
        SimpleNamespace(**{**vars(base_ctx), "blocked_actions": frozenset({("A", 1)})}),
        set(),
    )
    invalidating_branch = replace(
        branch,
        fidelity=replace(
            branch.fidelity,
            constraints=(Eq("A", frozenset({0})), Eq("B", frozenset({1}))),
        ),
    )
    invalidated = _assemble_candidate_read(
        route_and_wait,
        separated,
        None,
        None,
        SimpleNamespace(
            snap=frame.snap,
            tree=replace(frame.tree, crossing_branches=(invalidating_branch,)),
        ),
        base_ctx,
        set(),
    )

    assert [batch.actions for batch in admitted.crossing_batches] == [(("A", 1), ("B", 1))]
    assert blocked.crossing_batches == ()
    assert invalidated.crossing_batches == ()


def test_effect_paths_compose_only_the_exact_local_writer_conjunction() -> None:
    request = Bool("LocalOperationRequest", external=True)
    mode = Bool("LocalOperationMode", external=True)
    request_ready = Int("LocalOperationRequestReady")
    mode_value = Int("LocalOperationModeValue")
    selected = Int("LocalOperationSelected")
    unrelated = Int("LocalOperationUnrelated", external=True)
    with Program() as program:
        with rung(request):
            copy(1, request_ready)
        with rung(mode, request_ready == 1):
            copy(1, mode_value)
        with rung(request_ready == 1, mode_value == 1):
            copy(1, selected)

    common = EffectPathStep(
        2,
        selected.name,
        1,
        ((request_ready.name, 1), (mode_value.name, 1)),
    )
    request_detail = TraceAction(
        request.name,
        True,
        operation_boundary=(request_ready.name, 1),
        effect_path=(
            common,
            EffectPathStep(0, request_ready.name, 1, ((request.name, True),)),
        ),
    )
    mode_detail = TraceAction(
        mode.name,
        True,
        operation_boundary=(mode_value.name, 1),
        effect_path=(
            common,
            EffectPathStep(
                1,
                mode_value.name,
                1,
                ((mode.name, True), (request_ready.name, 1)),
            ),
        ),
    )
    unrelated_detail = TraceAction(
        unrelated.name,
        -1,
        operation_boundary=("LaterOperation", 1),
        effect_path=(EffectPathStep(1, mode_value.name, 1),),
    )

    batches = _effect_operation_batches(
        (request_detail, mode_detail, unrelated_detail),
        {
            request.name: False,
            mode.name: False,
            request_ready.name: 0,
            mode_value.name: 0,
            selected.name: 0,
        },
        build_program_graph(program),
        program,
        frozenset((request.name, mode.name, unrelated.name)),
    )

    assert tuple(batch.actions for batch in batches) == (((request.name, True), (mode.name, True)),)
    assert batches[0].expectation is not None
    assert tuple(
        (obligation.tag, obligation.value) for obligation in batches[0].expectation.obligations
    ) == ((selected.name, 1),)


def test_crossing_effect_shape_excludes_heuristic_and_relational_children() -> None:
    from pyrung.core.analysis.pilot.trace_tree import _crossing_at_node

    branch = TraceCrossingBranch(
        actions=(TraceAction("Action", True),),
        fidelity=CrossingFidelity((), "cross", True, True, False),
    )
    node = TraceNode(
        "Effect",
        1,
        writer_rung=4,
        children=[
            TraceNode("Concrete", 7),
            TraceNode("Heuristic", 8, heuristic=True),
            TraceNode("Relational", 9, relational=True),
        ],
    )

    receipt = _crossing_at_node(node, branch)

    assert receipt.effect_path[0].local_requirements == (("Concrete", 7),)


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
    state = SimpleNamespace(pilot_rungs=())
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


def test_supplemental_program_read_composes_availability_with_outer_lifetime() -> None:
    """Current executability and route lifetime are orthogonal receipts."""

    lifetime = object()
    outer = TraceAction(
        "Keep",
        True,
        until=lifetime,
        availability=_WriterAvailability.UNAVAILABLE_FROM_HERE,
        effect_path=(EffectPathStep(0, "OuterEffect", True),),
    )
    current = TraceAction(
        "Keep",
        True,
        availability=_WriterAvailability.AFTER_PREREQ,
        effect_path=(EffectPathStep(1, "CurrentEffect", True),),
    )
    admitted = _admit_trace_details(
        (outer, current),
        SimpleNamespace(snap={"Keep": False}),
        SimpleNamespace(pilot_rungs=()),
        SimpleNamespace(blocked_actions=frozenset(), edge_tags=frozenset()),
        set(),
    )

    operational = admitted.detail_by_pair[("Keep", True)]
    assert operational.availability is _WriterAvailability.AFTER_PREREQ
    assert operational.until is lifetime
    assert len(admitted.read_details) == 2


def test_trace_admission_releases_a_spent_edge_before_its_next_assertion() -> None:
    """The release scan is its own bearing, not hidden inside another pulse."""

    assertion = TraceAction("Trigger", True)
    release = TraceAction("Trigger", False)
    frame = SimpleNamespace(snap={"Trigger": True})
    state = SimpleNamespace(pilot_rungs=())
    ctx = SimpleNamespace(
        blocked_actions=frozenset(),
        edge_tags=frozenset({"Trigger"}),
        resting={"Trigger": False},
    )

    admitted = _admit_trace_details(
        (assertion, release),
        frame,
        state,
        ctx,
        set(),
    )

    assert admitted.actions == (("Trigger", False), ("Trigger", True))


def test_prerequisite_separation_retains_trace_action_evidence() -> None:
    detail = TraceAction("Enable", True)
    rung = PilotRung("Enable", True, object())
    admission = _TraceAdmission(
        active_actions=(detail.pair,),
        actions=(detail.pair,),
        details=(detail,),
        detail_by_pair={detail.pair: detail},
        managed_boolean_rungs=(rung,),
        establish_pending=False,
    )
    route_and_wait = _RouteAndCompletionRead(
        admission,
        None,
        WaitRead(WaitPrescription(None, "charted completion")),
    )
    frame = SimpleNamespace(
        snap={"Enable": False, "Target": False},
        tree=TraceNode("Target", True, satisfied=False),
    )
    state = SimpleNamespace(pilot_rungs=(), work=SimpleNamespace())
    ctx = SimpleNamespace(
        compass=SimpleNamespace(action_tags=frozenset()),
        edge_tags=frozenset(),
        clear_only=frozenset(),
        resting={},
        blocked_actions=frozenset(),
        pdg=SimpleNamespace(),
        program=object(),
    )

    separated = _separate_prerequisites(route_and_wait, frame, state, ctx)

    assert separated.trace.actions == ()
    assert separated.trace.active_actions == ()
    assert separated.prerequisites.pilot_rungs == (rung,)
    assert separated.trace.details == (detail,)


def test_program_input_lifetime_outranks_broad_action_catalog_role() -> None:
    """A proved level handoff is held even when catalogs also call it an action."""

    enable = Bool("ProgramLifetimeEnable", external=True)
    progress = Int("ProgramLifetimeProgress")
    with Program(strict=False) as logic:
        with rung(enable):
            copy(1, progress)

    plc = PLC(logic)
    boundary = Eq(progress.name, frozenset((1,)))
    detail = TraceAction(
        enable.name,
        True,
        until=boundary,
        availability=_WriterAvailability.UNAVAILABLE_FROM_HERE,
    )
    admission = _TraceAdmission(
        active_actions=(detail.pair,),
        actions=(detail.pair,),
        details=(detail,),
        detail_by_pair={detail.pair: detail},
        managed_boolean_rungs=(),
        establish_pending=False,
    )
    route_and_wait = _RouteAndCompletionRead(
        admission,
        None,
        WaitRead(WaitPrescription(None, "charted completion"), details=(detail,)),
    )
    frame = SimpleNamespace(
        snap=dict(plc.state.tags),
        tree=TraceNode(progress.name, 1, satisfied=False),
    )
    state = SimpleNamespace(pilot_rungs=(), work=plc)
    ctx = SimpleNamespace(
        compass=SimpleNamespace(action_tags=frozenset((enable.name,))),
        edge_tags=frozenset(),
        clear_only=frozenset(),
        resting={enable.name: False},
        blocked_actions=frozenset(),
        pdg=build_program_graph(logic),
        program=logic,
        avoid_pred=None,
    )

    separated = _separate_prerequisites(route_and_wait, frame, state, ctx)

    assert separated.trace.actions == ()
    assert tuple(rung.dest for rung in separated.prerequisites.pilot_rungs) == (enable.name,)


def test_unavailable_broad_lifetime_cannot_hitchhike_on_a_charted_coast() -> None:
    """The selected completion read, not target-wide relevance, grants the hold."""

    boundary = Eq("LaterEffect", frozenset((True,)))
    detail = TraceAction(
        "LaterInput",
        True,
        until=boundary,
        availability=_WriterAvailability.UNAVAILABLE_FROM_HERE,
    )
    admission = _TraceAdmission(
        active_actions=(detail.pair,),
        actions=(detail.pair,),
        details=(detail,),
        detail_by_pair={detail.pair: detail},
        managed_boolean_rungs=(),
        establish_pending=False,
    )
    route_and_wait = _RouteAndCompletionRead(
        admission,
        None,
        WaitRead(WaitPrescription(None, "unrelated charted completion")),
    )
    frame = SimpleNamespace(
        snap={"LaterInput": False, "LaterEffect": False},
        tree=TraceNode("StructuralChannel", 2, satisfied=False),
    )
    state = SimpleNamespace(pilot_rungs=(), work=SimpleNamespace())
    ctx = SimpleNamespace(
        compass=SimpleNamespace(action_tags=frozenset()),
        edge_tags=frozenset(),
        clear_only=frozenset(),
        resting={"LaterInput": False},
        blocked_actions=frozenset(),
        pdg=SimpleNamespace(),
        program=object(),
    )

    separated = _separate_prerequisites(route_and_wait, frame, state, ctx)

    assert separated.prerequisites.pilot_rungs == ()
    assert separated.trace.actions == (detail.pair,)


def test_trace_lifetime_positions_concurrent_input_for_its_instruction_coast() -> None:
    """One selected trace owns both its advancing boundary and durable sibling."""

    feedback_tag = Bool("GenericFeedback", external=True)
    accumulator_tag = Int("GenericAccumulator")
    done_tag = Bool("GenericDone")
    result_tag = Int("GenericResult")
    with Program(strict=False) as logic:
        with rung(feedback_tag):
            copy(accumulator_tag, accumulator_tag)
        with rung(done_tag):
            copy(1, result_tag)
    plc = PLC(logic)
    boundary = Eq(result_tag.name, frozenset((1,)))
    advance = SimpleNamespace(until=Eq(accumulator_tag.name, frozenset((5,))))
    detail = TraceAction(
        feedback_tag.name,
        True,
        until=boundary,
        availability=_WriterAvailability.UNAVAILABLE_FROM_HERE,
    )
    admission = _TraceAdmission(
        active_actions=(detail.pair,),
        actions=(detail.pair,),
        details=(detail,),
        detail_by_pair={detail.pair: detail},
        managed_boolean_rungs=(),
        establish_pending=False,
    )
    route_and_wait = _RouteAndCompletionRead(admission, None, None)
    feedback = TraceNode(
        detail.tag,
        detail.value,
        satisfied=False,
        is_steerable=True,
    )
    later_local = TraceNode(
        "GenericLaterLocal",
        1,
        satisfied=False,
    )
    accumulator = TraceNode(
        accumulator_tag.name,
        5,
        satisfied=False,
        advance=advance,
        owner_boundary=(done_tag.name, True),
        owner_condition=Eq(done_tag.name, frozenset((True,))),
        linear_boundary=True,
    )
    frame = SimpleNamespace(
        snap={
            detail.tag: False,
            accumulator_tag.name: 0,
            done_tag.name: False,
            result_tag.name: 0,
        },
        tree=TraceNode(
            result_tag.name,
            1,
            satisfied=False,
            writer_rung=0,
            children=[feedback, accumulator, later_local],
        ),
    )
    state = SimpleNamespace(pilot_rungs=(), work=plc)
    ctx = SimpleNamespace(
        compass=SimpleNamespace(action_tags=frozenset()),
        edge_tags=frozenset(),
        clear_only=frozenset(),
        resting={detail.tag: False},
        blocked_actions=frozenset(),
        pdg=build_program_graph(logic),
        program=logic,
        avoid_pred=None,
    )

    separated = _separate_prerequisites(route_and_wait, frame, state, ctx)

    assert separated.trace.actions == ()
    assert separated.instruction_boundary is not None
    assert separated.instruction_boundary.channel_tag == "GenericDone"
    assert tuple(rung.dest for rung in separated.prerequisites.pilot_rungs) == (detail.tag,)

    standalone_detail = replace(detail, until=None)
    standalone_admission = replace(
        admission,
        active_actions=(standalone_detail.pair,),
        actions=(standalone_detail.pair,),
        details=(standalone_detail,),
        detail_by_pair={standalone_detail.pair: standalone_detail},
    )
    standalone = _separate_prerequisites(
        _RouteAndCompletionRead(standalone_admission, None, None),
        frame,
        state,
        ctx,
    )
    assert standalone.instruction_boundary is None
    assert standalone.prerequisites.pilot_rungs == ()
    assert standalone.trace.actions == (standalone_detail.pair,)


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
    state = SimpleNamespace(pilot_rungs=())
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


def test_unavailable_program_input_cannot_keep_a_future_chart_edge_live() -> None:
    """Chart relevance cannot promote an unavailable producer supplement."""

    required = TraceAction(
        "FutureInput",
        True,
        availability=_WriterAvailability.UNAVAILABLE_FROM_HERE,
    )
    step = ProgramStep(
        ProgramStepStatus.NEEDS_INPUT,
        producer=object(),
        boundary=None,
        channel=None,
        required_inputs=(required,),
    )
    admitted = _admit_wait_read(
        WaitRead(None, (required,), program_step=step),
        (),
        SimpleNamespace(snap={"FutureInput": False}),
        SimpleNamespace(pilot_rungs=()),
        SimpleNamespace(blocked_actions=frozenset(), edge_tags=frozenset()),
        set(),
    )

    assert required.pair in admitted.admitted_pairs
    assert required.pair not in admitted.executable_pairs
    assert admitted.admitted_supplement is False
    assert admitted.viable is False


def test_exact_program_handoff_refines_conservative_trace_availability() -> None:
    """A current ProgramStep receipt may ground a chart-nominated producer."""

    boundary = Eq("CurrentBoundary", frozenset((1,)))
    required = TraceAction(
        "CurrentInput",
        True,
        availability=_WriterAvailability.UNAVAILABLE_FROM_HERE,
    )
    step = ProgramStep(
        ProgramStepStatus.NEEDS_INPUT,
        producer=object(),
        boundary=None,
        channel=None,
        required_inputs=(required,),
        input_handoffs=(ProgramInputHandoff(required.pair, boundary, boundary.tag),),
    )
    read = WaitRead(
        WaitPrescription(ChannelHeading(boundary.tag, 1, boundary), "current handoff"),
        (required,),
        program_step=step,
    )
    admitted = _admit_wait_read(
        read,
        (),
        SimpleNamespace(snap={"CurrentInput": False}),
        SimpleNamespace(pilot_rungs=()),
        SimpleNamespace(blocked_actions=frozenset(), edge_tags=frozenset()),
        set(),
    )

    assert required.pair in admitted.executable_pairs
    assert admitted.admitted_supplement is True
    assert admitted.viable is True


def test_one_executable_input_cannot_admit_an_incomplete_program_operation() -> None:
    """Every member of a ProgramStep input operation needs its own receipt."""

    current = TraceAction(
        "CurrentMember",
        True,
        availability=_WriterAvailability.AFTER_PREREQ,
    )
    future = TraceAction(
        "FutureMember",
        True,
        availability=_WriterAvailability.UNAVAILABLE_FROM_HERE,
    )
    step = ProgramStep(
        ProgramStepStatus.NEEDS_INPUT,
        producer=object(),
        boundary=None,
        channel=None,
        required_inputs=(current, future),
    )
    admitted = _admit_wait_read(
        WaitRead(None, (current, future), program_step=step),
        (),
        SimpleNamespace(snap={"CurrentMember": False, "FutureMember": False}),
        SimpleNamespace(pilot_rungs=()),
        SimpleNamespace(blocked_actions=frozenset(), edge_tags=frozenset()),
        set(),
    )

    assert current.pair in admitted.executable_pairs
    assert future.pair not in admitted.executable_pairs
    assert admitted.admitted_supplement is False


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
    obs = CompassObservation(
        "edge",
        "State",
        ("Cmd", True),
        6,
        8,
        applied=(("Cmd", True),),
    )
    base = Compass()

    learned, changed = base.apply((obs,))
    assert changed is True
    assert learned is not base

    again, changed_again = learned.apply((obs,))
    assert changed_again is False
    assert again is learned

    # A probe mark is knowledge too: a fresh no-change tombstone counts as changed.
    probe = CompassObservation(
        "no_change",
        "State",
        ("Other", True),
        6,
        None,
        applied=(("Other", True),),
    )
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

    obs = CompassObservation(
        "edge",
        "State",
        ("Cmd", True),
        6,
        8,
        applied=(("Cmd", True),),
    )
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
