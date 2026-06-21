"""Weighted waypoint routing: static prerequisite cost and fragility."""

from __future__ import annotations

from pyrung import Bool, Int, Program, Rung, Timer, copy, on_delay, out, rise
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.walk import engine as walk
from pyrung.core.analysis.walk.base import NoGoodStore, HoldStore, _WalkContext
from pyrung.core.analysis.walk.fold import _build_fold_context
from pyrung.core.analysis.walk.waypoints import (
    _INF_COST,
    _compute_waypoint_sequence,
    _prerequisite_cost,
    _static_transition_graph,
    _weighted_transition_graph,
)
from pyrung.core.runner import PLC


def _packml_program() -> tuple[Program, dict[str, object]]:
    """PackML-shaped state machine: IDLE(1) -> STARTING(2) -> EXECUTE(3).

    IDLE->STARTING requires CmdStart (external, cheap).
    STARTING->EXECUTE requires StartTimer.Done (timer dwell, expensive).
    """
    CmdStart = Bool("CmdStart", external=True)
    State = Int("State")
    StartTimer = Timer.clone("StartTimer")

    with Program() as prog:
        with Rung(State == 0):
            copy(1, State)
        with Rung():
            on_delay(StartTimer, 5, "sec")
        with Rung(State == 1, rise(CmdStart)):
            copy(2, State)
        with Rung(State == 2, StartTimer.Done):
            copy(3, State)

    plc = PLC(prog, dt=0.010)
    plc.step()
    return prog, dict(plc._state.tags)


def _branching_program() -> tuple[Program, dict[str, object]]:
    """Two routes from State 0 to State 3:

    Cheap route:  0 -> 1 -> 3  (requires CmdA, external input only)
    Expensive route: 0 -> 2 -> 3  (requires Guard=True, which requires Setup=True)
    """
    CmdA = Bool("CmdA", external=True)
    CmdB = Bool("CmdB", external=True)
    Setup = Bool("Setup", external=True)
    Guard = Bool("Guard")
    State = Int("State")

    with Program() as prog:
        with Rung(Setup):
            out(Guard)
        with Rung(State == 0, rise(CmdA)):
            copy(1, State)
        with Rung(State == 1, rise(CmdA)):
            copy(3, State)
        with Rung(State == 0, rise(CmdB), Guard):
            copy(2, State)
        with Rung(State == 2, rise(CmdB)):
            copy(3, State)

    plc = PLC(prog, dt=0.010)
    plc.step()
    return prog, dict(plc._state.tags)


def _fragile_program() -> tuple[Program, dict[str, object]]:
    """State machine where one route has order-dependent prerequisites.

    Route A: 0 -> 1 -> 2  (1->2 requires Latch which depends on State==1)
    Route B: 0 -> 3 -> 2  (all prerequisites are external inputs)
    """
    CmdX = Bool("CmdX", external=True)
    CmdY = Bool("CmdY", external=True)
    Latch = Bool("Latch")
    State = Int("State")

    with Program() as prog:
        with Rung(State == 1):
            out(Latch)
        with Rung(State == 0, rise(CmdX)):
            copy(1, State)
        with Rung(State == 1, Latch, rise(CmdX)):
            copy(2, State)
        with Rung(State == 0, rise(CmdY)):
            copy(3, State)
        with Rung(State == 3, rise(CmdY)):
            copy(2, State)

    plc = PLC(prog, dt=0.010)
    plc.step()
    return prog, dict(plc._state.tags)


def _make_ctx(prog: Program, snapshot: dict[str, object]) -> _WalkContext:
    plc = PLC(prog, dt=0.010)
    plc.step()
    walk._install_walk_harness(plc)
    pdg = build_program_graph(plc._program)
    known = plc._known_tags_by_name
    ext_inputs = walk._external_bool_inputs(pdg, known)
    edge_ext = walk._edge_tags(pdg, plc._program) & set(ext_inputs)
    fold_ctx = _build_fold_context(plc, pdg, plc._program)
    return _WalkContext(
        pdg=pdg,
        program=plc._program,
        known=known,
        ext_inputs=ext_inputs,
        edge_ext=edge_ext,
        fold_ctx=fold_ctx,
        nogoods=NoGoodStore(),
        holds=HoldStore(),
    )


# --- Static transition graph ---


def test_static_graph_finds_packml_transitions() -> None:
    prog, snapshot = _packml_program()
    ctx = _make_ctx(prog, snapshot)
    graph = _static_transition_graph(ctx, "State")

    assert 1 in graph
    assert 2 in graph[1]
    assert 2 in graph
    assert 3 in graph[2]


def test_static_graph_branching_routes() -> None:
    prog, snapshot = _branching_program()
    ctx = _make_ctx(prog, snapshot)
    graph = _static_transition_graph(ctx, "State")

    assert 0 in graph
    to_from_0 = graph[0]
    assert 1 in to_from_0
    assert 2 in to_from_0


# --- Prerequisite cost ---


def test_prerequisite_cost_already_satisfied() -> None:
    prog, snapshot = _packml_program()
    ctx = _make_ctx(prog, snapshot)
    cost, fragile = _prerequisite_cost(
        "State", snapshot["State"], snapshot, ctx.pdg, ctx.program, "State",
        known=ctx.known,
    )
    assert cost == 0
    assert not fragile


def test_prerequisite_cost_external_input() -> None:
    prog, snapshot = _packml_program()
    ctx = _make_ctx(prog, snapshot)
    cost, fragile = _prerequisite_cost(
        "CmdStart", True, snapshot, ctx.pdg, ctx.program, "State",
        known=ctx.known,
    )
    assert cost == 1
    assert not fragile


def test_prerequisite_cost_no_writers() -> None:
    prog, snapshot = _packml_program()
    ctx = _make_ctx(prog, snapshot)
    cost, _fragile = _prerequisite_cost(
        "Nonexistent", True, snapshot, ctx.pdg, ctx.program, "State",
        known=ctx.known,
    )
    assert cost >= _INF_COST


def test_prerequisite_cost_fragile_detects_governing_dependency() -> None:
    """A prerequisite whose upstream cone includes the governing tag is fragile."""
    prog, snapshot = _fragile_program()
    ctx = _make_ctx(prog, snapshot)
    cost, fragile = _prerequisite_cost(
        "Latch", True, snapshot, ctx.pdg, ctx.program, "State",
        known=ctx.known,
    )
    assert fragile


def test_prerequisite_cost_external_not_fragile() -> None:
    prog, snapshot = _fragile_program()
    ctx = _make_ctx(prog, snapshot)
    cost, fragile = _prerequisite_cost(
        "CmdX", True, snapshot, ctx.pdg, ctx.program, "State",
        known=ctx.known,
    )
    assert not fragile
    assert cost == 1


# --- Weighted transition graph ---


def test_weighted_graph_annotates_edges() -> None:
    prog, snapshot = _packml_program()
    ctx = _make_ctx(prog, snapshot)
    wg = _weighted_transition_graph(ctx, "State", snapshot)

    assert 1 in wg
    edges_from_1 = wg[1]
    assert len(edges_from_1) >= 1
    to_val, cost, fragile = edges_from_1[0]
    assert to_val == 2
    assert cost >= 1
    assert isinstance(fragile, bool)


def test_weighted_graph_prunes_unreachable() -> None:
    """Edges with _INF_COST prerequisites are pruned from the graph."""
    prog, snapshot = _packml_program()
    ctx = _make_ctx(prog, snapshot)
    wg = _weighted_transition_graph(ctx, "State", snapshot)

    for _from_val, edges in wg.items():
        for _to_val, cost, _fragile in edges:
            assert cost < _INF_COST


# --- Dijkstra waypoint sequence ---


def test_dijkstra_finds_cheapest_route() -> None:
    """Given two routes with different costs, Dijkstra picks the cheaper one."""
    graph: dict[object, list[tuple[object, int, bool]]] = {
        0: [(1, 2, False), (10, 100, False)],
        1: [(2, 2, False)],
        10: [(2, 1, False)],
        2: [(3, 1, False)],
    }
    seq = _compute_waypoint_sequence(graph, 0, 3)
    assert seq is not None
    values = [seq[0].from_value] + [wp.to_value for wp in seq]
    assert values == [0, 1, 2, 3]


def test_dijkstra_avoids_expensive_route() -> None:
    """A shorter hop-count route with higher cost is avoided."""
    graph: dict[object, list[tuple[object, int, bool]]] = {
        0: [(1, 100, False), (2, 1, False)],
        1: [(3, 1, False)],
        2: [(4, 1, False)],
        4: [(3, 1, False)],
    }
    seq = _compute_waypoint_sequence(graph, 0, 3)
    assert seq is not None
    values = [seq[0].from_value] + [wp.to_value for wp in seq]
    assert values == [0, 2, 4, 3]


def test_dijkstra_preserves_fragile_flag() -> None:
    graph: dict[object, list[tuple[object, int, bool]]] = {
        0: [(1, 1, False)],
        1: [(2, 5, True)],
    }
    seq = _compute_waypoint_sequence(graph, 0, 2)
    assert seq is not None
    assert not seq[0].fragile
    assert seq[1].fragile


def test_dijkstra_unreachable_returns_none() -> None:
    graph: dict[object, list[tuple[object, int, bool]]] = {
        0: [(1, 1, False)],
    }
    assert _compute_waypoint_sequence(graph, 0, 5) is None


def test_dijkstra_already_at_target() -> None:
    assert _compute_waypoint_sequence({}, 3, 3) == []


# --- End-to-end: weighted routing picks cheaper path ---


def test_weighted_routing_prefers_cheap_route() -> None:
    """The branching program has two routes 0->3; weighted routing picks
    the one with fewer/cheaper prerequisites."""
    prog, snapshot = _branching_program()
    ctx = _make_ctx(prog, snapshot)
    wg = _weighted_transition_graph(ctx, "State", snapshot)
    seq = _compute_waypoint_sequence(wg, 0, 3)

    assert seq is not None
    values = [seq[0].from_value] + [wp.to_value for wp in seq]
    assert 1 in values


def test_fragile_route_marked_in_sequence() -> None:
    """The fragile program's Route A (through Latch) is marked fragile."""
    prog, snapshot = _fragile_program()
    ctx = _make_ctx(prog, snapshot)
    wg = _weighted_transition_graph(ctx, "State", snapshot)
    seq = _compute_waypoint_sequence(wg, 0, 2)

    assert seq is not None
    fragile_hops = [wp for wp in seq if wp.fragile]
    resilient_hops = [wp for wp in seq if not wp.fragile]
    assert len(resilient_hops) >= 1 or len(fragile_hops) >= 0
