from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

from pyrung.core.analysis.pilot.charts import (
    StaticTransitionGraph,
    _best_static_path,
    _edges_from_routes,
)
from pyrung.core.analysis.pilot.compass import (
    Compass,
    CompassObservation,
    NavigationCatalog,
)
from pyrung.core.analysis.pilot.currents import Producer
from pyrung.core.analysis.pilot.evidence import PipelineRoles, TransitionRoute
from pyrung.core.analysis.pilot.options import _build_candidates, _compass_route_plan
from pyrung.core.analysis.pilot.trace import TraceNode


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
        target_tag="State",
        target_value=2,
        route_allowed=lambda _pair: True,
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
        target_tag="State",
        target_value=17,
        route_allowed=lambda _pair: True,
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
        blocked_route_actions=frozenset(),
        edge_tags=set(),
        clear_only=frozenset(),
        steerable=frozenset(),
        pdg=SimpleNamespace(writers_of={}),
        program=object(),
        route_allowed=lambda _pair: True,
        opaque_loop=frozenset(),
        target_tag="State",
        target_value=17,
    )

    candidates = _build_candidates(frame, state, ctx)

    assert candidates.wait_prescribed is True
    assert candidates.wait_reason == "let-run State: 6->16"
    assert candidates.stuck_reason is None


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
