"""Tests for the waypoint planner (Phase 1 of explore-less how())."""

from __future__ import annotations

from pyrung import Bool, Program, Rung, latch, out
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.prove import _compile_property
from pyrung.core.analysis.prove.waypoints import (
    _discover_waypoints,
    _extract_required_values,
    _order_waypoints,
)
from pyrung.core.runner import PLC

# ---------------------------------------------------------------------------
# Helper programs
# ---------------------------------------------------------------------------


def _three_step_program():
    """A → B → C: three sequential latches requiring three input changes."""
    CmdA = Bool("CmdA", external=True)
    CmdB = Bool("CmdB", external=True)
    CmdC = Bool("CmdC", external=True)
    A = Bool("A")
    B = Bool("B")
    C = Bool("C")
    with Program() as prog:
        with Rung(CmdA):
            latch(A)
        with Rung(A, CmdB):
            latch(B)
        with Rung(B, CmdC):
            latch(C)
    return prog, CmdA, CmdB, CmdC, A, B, C


def _two_step_latch():
    """Start → Ready → Done: two sequential latches."""
    Start = Bool("Start", external=True)
    Confirm = Bool("Confirm", external=True)
    Ready = Bool("Ready")
    Done = Bool("Done")
    with Program() as prog:
        with Rung(Start):
            latch(Ready)
        with Rung(Ready, Confirm):
            latch(Done)
    return prog, Start, Confirm, Ready, Done


def _simple_latch():
    """Single latch: Start → Running."""
    Start = Bool("Start", external=True)
    Running = Bool("Running")
    with Program() as prog:
        with Rung(Start):
            latch(Running)
    return prog, Start, Running


def _already_satisfied():
    """Program where target is trivially satisfied from start."""
    Start = Bool("Start", external=True)
    Output = Bool("Output")
    with Program() as prog:
        with Rung(Start):
            out(Output)
    return prog, Start, Output


# ---------------------------------------------------------------------------
# _extract_required_values tests
# ---------------------------------------------------------------------------


class TestExtractRequiredValues:
    def test_xic_atom(self):
        from pyrung.core.analysis.simplified import Atom

        pairs = _extract_required_values(Atom("Running", "xic"), {})
        assert pairs == [("Running", True)]

    def test_xio_atom(self):
        from pyrung.core.analysis.simplified import Atom

        pairs = _extract_required_values(Atom("Running", "xio"), {})
        assert pairs == [("Running", False)]

    def test_eq_atom(self):
        from pyrung.core.analysis.simplified import Atom

        pairs = _extract_required_values(Atom("State", "eq", 5), {})
        assert pairs == [("State", 5)]

    def test_and_expr(self):
        from pyrung.core.analysis.simplified import And, Atom

        expr = And((Atom("A", "xic"), Atom("B", "xio")))
        pairs = _extract_required_values(expr, {})
        assert pairs is not None
        assert ("A", True) in pairs
        assert ("B", False) in pairs

    def test_or_picks_cheapest_branch(self):
        from pyrung.core.analysis.simplified import Atom, Or

        expr = Or((Atom("A", "xic"), Atom("B", "xic")))
        snapshot = {"A": True, "B": False}
        pairs = _extract_required_values(expr, snapshot)
        assert pairs == [("A", True)]

    def test_rise_fall_returns_none(self):
        from pyrung.core.analysis.simplified import Atom

        assert _extract_required_values(Atom("X", "rise"), {}) is None
        assert _extract_required_values(Atom("X", "fall"), {}) is None

    def test_const_returns_empty(self):
        from pyrung.core.analysis.simplified import Const

        assert _extract_required_values(Const(True), {}) == []


# ---------------------------------------------------------------------------
# _discover_waypoints tests
# ---------------------------------------------------------------------------


class TestDiscoverWaypoints:
    def test_simple_latch_one_waypoint(self):
        prog, Start, Running = _simple_latch()
        pdg = build_program_graph(prog)
        snapshot = {"Start": False, "Running": False}
        _, _, expr = _compile_property(Running)

        waypoints = _discover_waypoints(snapshot, expr, pdg, prog)
        assert waypoints is not None
        assert len(waypoints) >= 1
        wp_tags = {wp.tag_name for wp in waypoints}
        assert "Running" in wp_tags

    def test_two_step_discovers_intermediate(self):
        prog, Start, Confirm, Ready, Done = _two_step_latch()
        pdg = build_program_graph(prog)
        snapshot = {"Start": False, "Confirm": False, "Ready": False, "Done": False}
        _, _, expr = _compile_property(Done)

        waypoints = _discover_waypoints(snapshot, expr, pdg, prog)
        assert waypoints is not None
        wp_tags = {wp.tag_name for wp in waypoints}
        assert "Done" in wp_tags
        assert "Ready" in wp_tags

    def test_already_satisfied_returns_empty(self):
        prog, Start, Running = _simple_latch()
        pdg = build_program_graph(prog)
        snapshot = {"Start": True, "Running": True}
        _, _, expr = _compile_property(Running)

        waypoints = _discover_waypoints(snapshot, expr, pdg, prog)
        assert waypoints is not None
        assert len(waypoints) == 0

    def test_external_input_not_a_waypoint(self):
        prog, Start, Running = _simple_latch()
        pdg = build_program_graph(prog)
        snapshot = {"Start": False, "Running": False}
        _, _, expr = _compile_property(Running)

        waypoints = _discover_waypoints(snapshot, expr, pdg, prog)
        assert waypoints is not None
        wp_tags = {wp.tag_name for wp in waypoints}
        assert "Start" not in wp_tags


# ---------------------------------------------------------------------------
# _order_waypoints tests
# ---------------------------------------------------------------------------


class TestOrderWaypoints:
    def test_ordering_respects_dependencies(self):
        prog, Start, Confirm, Ready, Done = _two_step_latch()
        pdg = build_program_graph(prog)
        snapshot = {"Start": False, "Confirm": False, "Ready": False, "Done": False}
        _, _, expr = _compile_property(Done)

        waypoints = _discover_waypoints(snapshot, expr, pdg, prog)
        assert waypoints is not None
        ordered = _order_waypoints(waypoints, pdg)
        assert ordered is not None
        tags_ordered = [wp.tag_name for wp in ordered]
        if "Ready" in tags_ordered and "Done" in tags_ordered:
            assert tags_ordered.index("Ready") < tags_ordered.index("Done")

    def test_single_waypoint_trivial(self):
        prog, Start, Running = _simple_latch()
        pdg = build_program_graph(prog)
        snapshot = {"Start": False, "Running": False}
        _, _, expr = _compile_property(Running)

        waypoints = _discover_waypoints(snapshot, expr, pdg, prog)
        assert waypoints is not None
        ordered = _order_waypoints(waypoints, pdg)
        assert ordered is not None
        assert len(ordered) == len(waypoints)


# ---------------------------------------------------------------------------
# Integration: how() with waypoint planner
# ---------------------------------------------------------------------------


class TestHowWithWaypoints:
    def test_simple_latch_how_without_explore(self):
        prog, Start, Running = _simple_latch()
        plc = PLC(prog, dt=0.010)
        path = plc.how(Running)
        assert path.reachable
        assert path.total_changes > 0

    def test_two_step_how_without_explore(self):
        prog, Start, Confirm, Ready, Done = _two_step_latch()
        plc = PLC(prog, dt=0.010)
        path = plc.how(Done)
        assert path.reachable
        assert path.total_changes > 0

    def test_three_step_how_without_explore(self):
        prog, CmdA, CmdB, CmdC, A, B, C = _three_step_program()
        plc = PLC(prog, dt=0.010)
        path = plc.how(C)
        assert path.reachable

    def test_how_replay_validates(self):
        """Every returned path must replay correctly."""
        prog, Start, Confirm, Ready, Done = _two_step_latch()
        plc = PLC(prog, dt=0.010)
        path = plc.how(Done)
        assert path.reachable

        replay_plc = PLC(prog, dt=0.010)
        for step in path.steps:
            replay_plc.patch(step.action)
            for _ in range(step.scans):
                replay_plc.step()
        assert replay_plc.state.tags["Done"] is True

    def test_already_satisfied_zero_steps(self):
        prog, Start, Running = _simple_latch()
        plc = PLC(prog, dt=0.010)
        plc.patch({"Start": True})
        plc.step()
        assert plc.state.tags["Running"] is True
        path = plc.how(Running)
        assert path.reachable
        assert path.total_changes == 0

    def test_how_with_xio_target(self):
        """how() with normally-closed (xio) target condition."""
        Enable = Bool("Enable", external=True)
        Active = Bool("Active")
        with Program() as prog:
            with Rung(Enable):
                latch(Active)
        plc = PLC(prog, dt=0.010)
        plc.patch({"Enable": True})
        plc.step()
        assert plc.state.tags["Active"] is True
        from pyrung.core.condition import NormallyClosedCondition as NCC

        path = plc.how(NCC(Active))
        assert not path.reachable

    def test_how_multiple_conditions_and(self):
        prog, Start, Confirm, Ready, Done = _two_step_latch()
        plc = PLC(prog, dt=0.010)
        path = plc.how(Ready, Done)
        assert path.reachable

        replay_plc = PLC(prog, dt=0.010)
        for step in path.steps:
            replay_plc.patch(step.action)
            for _ in range(step.scans):
                replay_plc.step()
        assert replay_plc.state.tags["Ready"] is True
        assert replay_plc.state.tags["Done"] is True

    def test_fallback_to_undecomposed_bfs_with_callable(self):
        """Opaque callable predicates can't be decomposed — falls back to BFS."""
        prog, Start, Running = _simple_latch()
        plc = PLC(prog, dt=0.010)
        path = plc.how(lambda s: s["Running"])
        assert path.reachable

    def test_how_from_stepped_state(self):
        prog, Start, Confirm, Ready, Done = _two_step_latch()
        plc = PLC(prog, dt=0.010)
        plc.patch({"Start": True})
        plc.step()
        assert plc.state.tags["Ready"] is True
        path = plc.how(Done)
        assert path.reachable
        assert path.total_changes > 0
