"""Tests for the waypoint planner (Phase 1 of explore-less how())."""

from __future__ import annotations

from pyrung import Bool, Int, Program, Rung, copy, latch, out
from pyrung.core.analysis.simplified import And, Atom, Const, Or
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.prove import _compile_property
from pyrung.core.analysis.prove.waypoints import (
    _discover_waypoints,
    _extract_condition_values,
    _extract_required_values,
    _find_backjump_target,
    _order_waypoints,
    _value_aware_cone,
    _Waypoint,
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
        assert _extract_required_values(Const(True), {}) == []


# ---------------------------------------------------------------------------
# _extract_condition_values tests
# ---------------------------------------------------------------------------


class TestExtractConditionValues:
    def test_xic_atom(self):
        assert _extract_condition_values(Atom("X", "xic")) == {"X": frozenset([True])}

    def test_eq_atom(self):
        assert _extract_condition_values(Atom("State", "eq", 5)) == {"State": frozenset([5])}

    def test_and_collects_all(self):
        expr = And((Atom("A", "xic"), Atom("State", "eq", 3)))
        assert _extract_condition_values(expr) == {"A": frozenset([True]), "State": frozenset([3])}

    def test_rise_omitted(self):
        assert _extract_condition_values(Atom("X", "rise")) == {}

    def test_or_same_tag_unions_values(self):
        expr = Or((Atom("A", "eq", 1), Atom("A", "eq", 2)))
        assert _extract_condition_values(expr) == {"A": frozenset([1, 2])}

    def test_or_disjoint_tags_returns_empty(self):
        expr = Or((Atom("A", "xic"), Atom("B", "xic")))
        assert _extract_condition_values(expr) == {}

    def test_or_with_uninvertible_branch_returns_empty(self):
        expr = Or((Atom("A", "xic"), Atom("B", "rise")))
        assert _extract_condition_values(expr) == {}

    def test_and_with_uninvertible_partial(self):
        """And with one invertible and one rise term: extracts the invertible one."""
        expr = And((Atom("A", "xic"), Atom("B", "rise")))
        assert _extract_condition_values(expr) == {"A": frozenset([True])}

    def test_const(self):
        assert _extract_condition_values(Const(True)) == {}


# ---------------------------------------------------------------------------
# _value_aware_cone condition-value propagation tests
# ---------------------------------------------------------------------------


class TestValueAwareConeConditionPropagation:
    def test_cone_shrinks_with_eq_condition(self):
        """Writers guarded by StateCurrent == X should only pull in
        writers of StateCurrent that produce X, not all writers."""
        StateCurrent = Int("StateCurrent")
        StateRequested = Int("StateRequested")
        Cmd1 = Bool("Cmd1", external=True)
        Cmd2 = Bool("Cmd2", external=True)
        Extra = Bool("Extra", external=True)
        with Program() as prog:
            with Rung(Cmd1):
                copy(10, StateCurrent)
            with Rung(Cmd2):
                copy(20, StateCurrent)
            with Rung(Extra):
                copy(99, StateCurrent)
            with Rung(StateCurrent == 10):
                copy(1, StateRequested)
        pdg = build_program_graph(prog)
        cone = _value_aware_cone("StateRequested", 1, pdg, prog)
        assert "Cmd1" in cone
        assert "Extra" not in cone

    def test_cone_propagates_bool_condition(self):
        """Condition reads of Bool tags (xic/xio) get value-aware treatment."""
        from pyrung import reset

        Enable = Bool("Enable")
        CmdEnable = Bool("CmdEnable", external=True)
        CmdDisable = Bool("CmdDisable", external=True)
        Output = Bool("Output")
        with Program() as prog:
            with Rung(CmdEnable):
                latch(Enable)
            with Rung(CmdDisable):
                reset(Enable)
            with Rung(Enable):
                latch(Output)
        pdg = build_program_graph(prog)
        cone = _value_aware_cone("Output", True, pdg, prog)
        assert "CmdEnable" in cone
        assert "CmdDisable" not in cone


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

    def test_literal_barrier_stops_copy_chain(self):
        """When an intermediate tag has a literal writer for the required value,
        back-propagation stops — upstream copy sources are NOT waypoints."""
        Cmd = Bool("Cmd", external=True)
        A = Int("A")
        B = Int("B")
        C = Int("C")
        with Program() as prog:
            # C ← B ← A copy chain
            with Rung(Cmd):
                copy(5, A)  # literal writer for A=5
            with Rung():
                copy(A, B)  # B gets A's value
            with Rung():
                copy(B, C)  # C gets B's value
            # B also has a literal writer for 5
            with Rung(Cmd):
                copy(5, B)
        pdg = build_program_graph(prog)
        snapshot = {"Cmd": False, "A": 0, "B": 0, "C": 0}
        _, _, expr = _compile_property(C == 5)

        waypoints = _discover_waypoints(snapshot, expr, pdg, prog)
        assert waypoints is not None
        wp_tags = {wp.tag_name for wp in waypoints}
        assert "C" in wp_tags
        assert "B" in wp_tags
        # A should NOT be a waypoint — B has a literal writer for 5,
        # so the copy chain A→B is unnecessary
        assert "A" not in wp_tags


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

    def test_fail_first_among_independent_waypoints(self):
        """Independent waypoints are ordered smallest-cone-first."""
        from pyrung.core.analysis.prove.waypoints import _Waypoint

        wp_a = _Waypoint("A", True, frozenset({"X", "Y", "Z"}))
        wp_b = _Waypoint("B", True, frozenset({"X"}))
        wp_c = _Waypoint("C", True, frozenset({"X", "Y"}))
        # No inter-waypoint dependencies → all are topo-equivalent
        prog, _, _ = _simple_latch()
        pdg = build_program_graph(prog)
        ordered = _order_waypoints([wp_a, wp_b, wp_c], pdg)
        assert ordered is not None
        tags = [wp.tag_name for wp in ordered]
        assert tags == ["B", "C", "A"]

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

    def test_cycle_merge_produces_valid_ordering(self):
        """Cyclic waypoints (A ↔ B) are merged into one mega-waypoint."""
        wp_a = _Waypoint("A", True, frozenset({"X"}))
        wp_b = _Waypoint("B", True, frozenset({"Y"}))
        wp_c = _Waypoint("C", True, frozenset({"Z"}))

        # Create a program where A and B form a cycle:
        # A's writer reads B (condition), B's writer reads A (condition)
        CmdA = Bool("CmdA", external=True)
        CmdB = Bool("CmdB", external=True)
        CmdC = Bool("CmdC", external=True)
        A = Bool("A")
        B = Bool("B")
        C = Bool("C")
        with Program() as prog:
            with Rung(B, CmdA):
                latch(A)
            with Rung(A, CmdB):
                latch(B)
            with Rung(CmdC):
                latch(C)

        pdg = build_program_graph(prog)
        ordered = _order_waypoints([wp_a, wp_b, wp_c], pdg)
        assert ordered is not None
        # C has no cycle dependency → appears separately
        # A and B form a cycle → merged into one waypoint
        assert len(ordered) == 2
        tags = [wp.tag_name for wp in ordered]
        assert "C" in tags
        # The merged waypoint has the combined cone
        merged = [wp for wp in ordered if wp.tag_name != "C"][0]
        assert "X" in merged.cone or "Y" in merged.cone


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

    def test_how_with_backjump_opportunity(self):
        """Waypoint C depends on A (through cone), B is independent."""
        CmdA = Bool("CmdA", external=True)
        CmdB = Bool("CmdB", external=True)
        CmdC = Bool("CmdC", external=True)
        A = Bool("A")
        B = Bool("B")
        C = Bool("C")
        with Program() as prog:
            with Rung(CmdA):
                latch(A)
            with Rung(CmdB):
                latch(B)
            with Rung(A, CmdC):
                latch(C)
        plc = PLC(prog, dt=0.010)
        path = plc.how(C)
        assert path.reachable

        replay = PLC(prog, dt=0.010)
        for step in path.steps:
            replay.patch(step.action)
            for _ in range(step.scans):
                replay.step()
        assert replay.state.tags["C"] is True

    def test_how_with_int_step_counter(self):
        """Int tags with calc writes need trace-based domain seeding.

        Uses a copy-based step counter to avoid a pre-existing soundness
        bug in threshold absorption for oneshot calc accumulators.
        """
        Go = Bool("Go", external=True)
        Step = Int("Step")
        Active = Bool("Active")
        with Program() as prog:
            with Rung(Go, Step == 0):
                copy(1, Step)
            with Rung(Step == 1):
                copy(2, Step)
            with Rung(Step == 2):
                latch(Active)
        plc = PLC(prog, dt=0.010)
        path = plc.how(Active)
        assert path.reachable

        replay = PLC(prog, dt=0.010)
        for step in path.steps:
            replay.patch(step.action)
            for _ in range(step.scans):
                replay.step()
        assert replay.state.tags["Active"] is True


# ---------------------------------------------------------------------------
# _find_backjump_target tests
# ---------------------------------------------------------------------------


class TestBackjumping:
    def _wp(self, tag: str, cone_tags: set[str]) -> _Waypoint:
        return _Waypoint(tag_name=tag, required_value=True, cone=frozenset(cone_tags))

    def test_returns_latest_cone_match(self):
        wps = [
            self._wp("A", set()),
            self._wp("B", set()),
            self._wp("C", {"A"}),
            self._wp("D", {"A", "C"}),
        ]
        stub_gens = [(None, None, 0)] * 3
        target = _find_backjump_target(wps[3].cone, wps, stub_gens)
        assert target == 2

    def test_no_match_returns_none(self):
        wps = [
            self._wp("A", set()),
            self._wp("B", set()),
            self._wp("D", {"X", "Y"}),
        ]
        stub_gens = [(None, None, 0)] * 2
        target = _find_backjump_target(wps[2].cone, wps, stub_gens)
        assert target is None

    def test_empty_generators(self):
        wp = self._wp("D", {"A", "B"})
        target = _find_backjump_target(wp.cone, [], [])
        assert target is None

    def test_single_match_earliest(self):
        wps = [
            self._wp("A", set()),
            self._wp("B", set()),
            self._wp("C", set()),
            self._wp("D", {"A"}),
        ]
        stub_gens = [(None, None, 0)] * 3
        target = _find_backjump_target(wps[3].cone, wps, stub_gens)
        assert target == 0

    def test_merged_conflict_set_finds_deeper_target(self):
        """After merging an exhausted waypoint's cone, the next lookup
        should find targets that weren't in the original cone."""
        wps = [
            self._wp("A", set()),
            self._wp("B", set()),
            self._wp("C", {"A"}),
            self._wp("D", {"C"}),
        ]
        stub_gens = [(None, None, 0)] * 3
        # D's cone is {"C"} → initial target is index 2 (C)
        target = _find_backjump_target(wps[3].cone, wps, stub_gens)
        assert target == 2
        # Simulate C exhausted: merge C's cone ({"A"}) into conflict set
        merged = wps[3].cone | wps[2].cone
        assert merged == frozenset({"C", "A"})
        remaining_gens = [(None, None, 0)] * 2  # only A, B left
        target2 = _find_backjump_target(merged, wps, remaining_gens)
        # wps[1].tag="B" not in {"C","A"}, wps[0].tag="A" IS → target 0
        assert target2 == 0
