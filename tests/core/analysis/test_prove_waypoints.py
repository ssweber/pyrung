"""Tests for the waypoint planner (Phase 1 of explore-less how())."""

from __future__ import annotations

import pytest

from pyrung import Bool, Int, Program, Rung, Timer, calc, copy, latch, on_delay, out
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.prove import _compile_property
from pyrung.core.analysis.prove.waypoints import (
    _analyze_frontier_value_gap,
    _build_value_transitions,
    _discover_waypoints,
    _domain_value_path,
    _extract_condition_values,
    _extract_required_values,
    _find_backjump_target,
    _frontier_has_progress,
    _get_domain,
    _order_waypoints,
    _search_relevant_cone_size,
    _try_decompose_scc,
    _value_aware_cone,
    _Waypoint,
)
from pyrung.core.analysis.simplified import And, Atom, Const, Or
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


def _unwrap_discovery(result):
    """Unwrap _discover_waypoints tuple to just the waypoint list."""
    if result is None:
        return None
    waypoints, _orderings, _actions, _first_achievers = result
    return waypoints


class TestDiscoverWaypoints:
    def test_simple_latch_one_waypoint(self):
        prog, Start, Running = _simple_latch()
        pdg = build_program_graph(prog)
        snapshot = {"Start": False, "Running": False}
        _, _, expr = _compile_property(Running)

        waypoints = _unwrap_discovery(_discover_waypoints(snapshot, expr, pdg, prog))
        assert waypoints is not None
        assert len(waypoints) >= 1
        wp_tags = {wp.tag_name for wp in waypoints}
        assert "Running" in wp_tags

    def test_two_step_discovers_intermediate(self):
        prog, Start, Confirm, Ready, Done = _two_step_latch()
        pdg = build_program_graph(prog)
        snapshot = {"Start": False, "Confirm": False, "Ready": False, "Done": False}
        _, _, expr = _compile_property(Done)

        waypoints = _unwrap_discovery(_discover_waypoints(snapshot, expr, pdg, prog))
        assert waypoints is not None
        wp_tags = {wp.tag_name for wp in waypoints}
        assert "Done" in wp_tags
        assert "Ready" in wp_tags

    def test_already_satisfied_returns_empty(self):
        prog, Start, Running = _simple_latch()
        pdg = build_program_graph(prog)
        snapshot = {"Start": True, "Running": True}
        _, _, expr = _compile_property(Running)

        waypoints = _unwrap_discovery(_discover_waypoints(snapshot, expr, pdg, prog))
        assert waypoints is not None
        assert len(waypoints) == 0

    def test_external_input_not_a_waypoint(self):
        prog, Start, Running = _simple_latch()
        pdg = build_program_graph(prog)
        snapshot = {"Start": False, "Running": False}
        _, _, expr = _compile_property(Running)

        waypoints = _unwrap_discovery(_discover_waypoints(snapshot, expr, pdg, prog))
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

        waypoints = _unwrap_discovery(_discover_waypoints(snapshot, expr, pdg, prog))
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

        waypoints = _unwrap_discovery(_discover_waypoints(snapshot, expr, pdg, prog))
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

        waypoints = _unwrap_discovery(_discover_waypoints(snapshot, expr, pdg, prog))
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
# Value-transition graph and SCC sub-decomposition
# ---------------------------------------------------------------------------


def _step_counter_program():
    """Step counter 0→1→2→3 with direct condition guards.

    ``with rung(Step == N): copy(N+1, Step)`` — the simplest stepping pattern.
    """
    Enable = Bool("Enable", external=True)
    Step = Int("Step", choices={0: "S0", 1: "S1", 2: "S2", 3: "S3"})
    Output = Bool("Output")
    with Program() as prog:
        with Rung(Enable, Step == 0):
            copy(1, Step)
        with Rung(Step == 1):
            copy(2, Step)
        with Rung(Step == 2):
            copy(3, Step)
        with Rung(Step == 3):
            latch(Output)
    return prog, Enable, Step, Output


def _timer_gated_step_program():
    """Step counter where transitions are gated by timers (one-hop pattern).

    ``with rung(Step == N): on_delay(T, 1)``
    ``with rung(T.done): copy(N+1, Step)``

    The copy rung's condition is T.done, not Step==N — requires one-hop
    chase through the timer's writer to discover the from-value.
    """
    Enable = Bool("Enable", external=True)
    Step = Int("Step", choices={0: "S0", 1: "S1", 2: "S2", 3: "S3"})
    T1 = Timer.clone("T1")
    T2 = Timer.clone("T2")
    Trans = Bool("Trans")
    Output = Bool("Output")
    with Program() as prog:
        with Rung(Enable, Step == 0):
            on_delay(T1, 1)
        with Rung(T1.Done):
            copy(1, Step)
        with Rung(Step == 1):
            on_delay(T2, 1)
        with Rung(T2.Done):
            copy(2, Step)
        with Rung(Step == 2):
            latch(Trans)
        with Rung(Trans):
            copy(3, Step)
        with Rung(Step == 3):
            latch(Output)
    return prog, Enable, Step, Trans, T1, T2, Output


def _two_hop_gated_step_program():
    """Step counter whose advance is gated TWO hops away from Step.

    ``with rung(Step == N): out(PhaseN)``    — dispatcher: step selects phase
    ``with rung(PhaseN): on_delay(TN, 1)``   — phase enables the timer
    ``with rung(TN.Done): copy(N+1, Step)``  — timer-done advances the step

    The advance rung's condition is ``TN.Done``; the timer is gated by
    ``PhaseN``; the phase by ``Step == N``.  A one-hop chase stops at the
    phase and never reaches Step, so the from-value is undiscoverable; the
    recursive enabler chase (``TN.Done -> PhaseN -> Step``) finds it.  This
    mirrors the real CLICK fill sequencer (step -> sub-state -> timer-done).
    """
    Enable = Bool("Enable", external=True)
    Step = Int("Step", choices={0: "S0", 1: "S1", 2: "S2"})
    Ph0 = Bool("Ph0")
    Ph1 = Bool("Ph1")
    T0 = Timer.clone("T0")
    T1 = Timer.clone("T1")
    Output = Bool("Output")
    with Program() as prog:
        with Rung(Enable, Step == 0):
            out(Ph0)
        with Rung(Step == 1):
            out(Ph1)
        with Rung(Ph0):
            on_delay(T0, 1)
        with Rung(Ph1):
            on_delay(T1, 1)
        with Rung(T0.Done):
            copy(1, Step)
        with Rung(T1.Done):
            copy(2, Step)
        with Rung(Step == 2):
            latch(Output)
    return prog, Enable, Step, Ph0, Ph1, T0, T1, Output


class TestBuildValueTransitions:
    def test_direct_literal_guards(self):
        """Direct pattern: copy(N+1, Step) guarded by Step==N."""
        prog, Enable, Step, Output = _step_counter_program()
        pdg = build_program_graph(prog)
        transitions = _build_value_transitions("Step", pdg, prog)
        assert 0 in transitions and 1 in transitions[0]
        assert 1 in transitions and 2 in transitions[1]
        assert 2 in transitions and 3 in transitions[2]

    def test_one_hop_through_timer(self):
        """One-hop: Timer.done gates copy, Timer enabled by Step==N."""
        prog, Enable, Step, Trans, T1, T2, Output = _timer_gated_step_program()
        pdg = build_program_graph(prog)
        transitions = _build_value_transitions("Step", pdg, prog)
        # 0→1 via T1 (one-hop), 1→2 via T2 (one-hop), 2→3 via Trans (direct)
        assert 1 in transitions.get(0, set()), f"missing 0→1, got {transitions}"
        assert 2 in transitions.get(1, set()), f"missing 1→2, got {transitions}"
        assert 3 in transitions.get(2, set()), f"missing 2→3, got {transitions}"

    def test_two_hop_through_phase_and_timer(self):
        """Two-hop: Timer.done gates copy, Timer by Phase, Phase by Step==N.

        Requires the recursive enabler chase — a single-hop chase stops at
        the phase and never reaches Step.
        """
        prog, Enable, Step, Ph0, Ph1, T0, T1, Output = _two_hop_gated_step_program()
        pdg = build_program_graph(prog)
        transitions = _build_value_transitions("Step", pdg, prog)
        assert 1 in transitions.get(0, set()), f"missing 0→1, got {transitions}"
        assert 2 in transitions.get(1, set()), f"missing 1→2, got {transitions}"

    def test_non_sequential_values(self):
        """Arbitrary value jumps: 0→10→5→8."""
        Step = Int("Step")
        Enable = Bool("Enable", external=True)
        with Program() as prog:
            with Rung(Enable, Step == 0):
                copy(10, Step)
            with Rung(Step == 10):
                copy(5, Step)
            with Rung(Step == 5):
                copy(8, Step)
        pdg = build_program_graph(prog)
        transitions = _build_value_transitions("Step", pdg, prog)
        assert transitions == {0: {10}, 10: {5}, 5: {8}}

    def test_no_literal_writers_returns_empty(self):
        """Tag-to-tag copy (no literal) yields no transitions."""
        Src = Int("Src", external=True)
        Dst = Int("Dst")
        with Program() as prog:
            with Rung():
                copy(Src, Dst)
        pdg = build_program_graph(prog)
        transitions = _build_value_transitions("Dst", pdg, prog)
        assert transitions == {}


class TestTryDecomposeScc:
    def test_decomposes_direct_step_counter(self):
        """SCC with a step counter is sub-decomposed into per-step waypoints."""
        prog, Enable, Step, Output = _step_counter_program()
        pdg = build_program_graph(prog)
        snapshot = {"Step": 0, "Enable": False, "Output": False}

        wp_step = _Waypoint("Step", 3, pdg.upstream_slice("Step"))
        all_wp_tags = frozenset(["Step", "Output"])

        sub = _try_decompose_scc(
            ["Step"],
            {"Step": wp_step},
            snapshot,
            pdg,
            prog,
            all_wp_tags,
        )
        # Single-member SCC with 3 intermediate steps → 3 sub-waypoints
        assert sub is not None
        assert len(sub) == 3
        assert [wp.required_value for wp in sub] == [1, 2, 3]

    def test_decomposes_timer_gated_step_counter(self):
        """SCC with timer-gated step counter decomposes via one-hop."""
        prog, Enable, Step, Trans, T1, T2, Output = _timer_gated_step_program()
        pdg = build_program_graph(prog)
        snapshot = {"Step": 0, "Enable": False, "Trans": False, "Output": False}

        wp_step = _Waypoint("Step", 3, pdg.upstream_slice("Step"))
        wp_trans = _Waypoint("Trans", True, pdg.upstream_slice("Trans"))
        all_wp_tags = frozenset(["Step", "Trans", "Output"])

        sub = _try_decompose_scc(
            ["Step", "Trans"],
            {"Step": wp_step, "Trans": wp_trans},
            snapshot,
            pdg,
            prog,
            all_wp_tags,
        )
        assert sub is not None
        assert len(sub) == 3
        assert [wp.required_value for wp in sub] == [1, 2, 3]
        # Trans should be in each sub-waypoint's cone (SCC extra)
        for wp in sub:
            assert "Trans" in wp.cone

    def test_decomposes_two_hop_gated_step_counter(self):
        """SCC with a two-hop (phase + timer) gated step counter decomposes.

        Exercises the recursive enabler chase end-to-end: the from-values are
        two hops from Step, so the pre-recursion one-hop chase found no
        transitions and decomposition failed.
        """
        prog, Enable, Step, Ph0, Ph1, T0, T1, Output = _two_hop_gated_step_program()
        pdg = build_program_graph(prog)
        snapshot = {
            "Step": 0,
            "Enable": False,
            "Ph0": False,
            "Ph1": False,
            "Output": False,
        }
        wp_step = _Waypoint("Step", 2, pdg.upstream_slice("Step"))
        all_wp_tags = frozenset(["Step", "Output"])
        sub = _try_decompose_scc(
            ["Step"],
            {"Step": wp_step},
            snapshot,
            pdg,
            prog,
            all_wp_tags,
        )
        assert sub is not None
        assert [wp.required_value for wp in sub] == [1, 2]

    def test_no_decomposition_for_tag_copies(self):
        """Tag-to-tag copies (no literal writes) → no decomposition."""
        Src = Int("Src", external=True)
        Dst = Int("Dst")
        with Program() as prog:
            with Rung():
                copy(Src, Dst)
        pdg = build_program_graph(prog)
        snapshot = {"Src": 0, "Dst": 0}
        wp = _Waypoint("Dst", 5, pdg.upstream_slice("Dst"))
        sub = _try_decompose_scc(
            ["Dst"],
            {"Dst": wp},
            snapshot,
            pdg,
            prog,
            frozenset(["Dst"]),
        )
        assert sub is None

    def test_sub_decomposition_integrates_with_ordering(self):
        """_order_waypoints sub-decomposes when snapshot and program are given."""
        prog, Enable, Step, Output = _step_counter_program()
        pdg = build_program_graph(prog)
        snapshot = {"Step": 0, "Enable": False, "Output": False}

        _, _, expr = _compile_property(Step == 3)
        waypoints = _unwrap_discovery(_discover_waypoints(snapshot, expr, pdg, prog))
        assert waypoints is not None

        # Without snapshot/program → no decomposition, may merge
        ordered_no_snap = _order_waypoints(waypoints, pdg)
        # With snapshot/program → sub-decomposition
        ordered_with = _order_waypoints(waypoints, pdg, snapshot=snapshot, program=prog)

        assert ordered_no_snap is not None
        assert ordered_with is not None
        # Sub-decomposition should produce at least as many waypoints
        assert len(ordered_with) >= len(ordered_no_snap)


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

    @pytest.mark.xfail(reason="walker: opaque callable predicates need expr decomposition")
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


# ---------------------------------------------------------------------------
# RPG / landmark extraction tests
# ---------------------------------------------------------------------------


class TestRPGAndLandmarks:
    def test_rpg_reaches_literal_goal(self):
        """RPG with literal writes reaches the goal."""
        from pyrung.core.analysis.prove.waypoints import (
            _build_actions,
            _build_rpg,
        )

        prog, Enable, Step, Output = _step_counter_program()
        pdg = build_program_graph(prog)
        snapshot = {"Step": 0, "Enable": False, "Output": False}

        actions = _build_actions(pdg, prog)
        initial = frozenset((t, v) for t, v in snapshot.items())
        goal = frozenset({("Output", True)})

        _, reachable = _build_rpg(actions, initial, goal)
        assert reachable

    def test_landmarks_include_intermediate(self):
        """Landmark extraction finds intermediate waypoints."""
        from pyrung.core.analysis.prove.waypoints import (
            _build_actions,
            _build_rpg,
            _extract_landmarks,
        )

        prog, Start, Confirm, Ready, Done = _two_step_latch()
        pdg = build_program_graph(prog)
        snapshot = {"Start": False, "Confirm": False, "Ready": False, "Done": False}

        actions = _build_actions(pdg, prog)
        initial = frozenset((t, v) for t, v in snapshot.items())
        goal = frozenset({("Done", True)})

        first_achievers, reachable = _build_rpg(actions, initial, goal)
        assert reachable

        landmarks, orderings = _extract_landmarks(actions, first_achievers, goal, initial)
        landmark_tags = {t for t, v in landmarks}
        assert "Done" in landmark_tags
        assert "Ready" in landmark_tags

    def test_landmark_orderings_correct(self):
        """Greedy-necessary orderings: Ready must come before Done."""
        from pyrung.core.analysis.prove.waypoints import (
            _build_actions,
            _build_rpg,
            _extract_landmarks,
        )

        prog, Start, Confirm, Ready, Done = _two_step_latch()
        pdg = build_program_graph(prog)
        snapshot = {"Start": False, "Confirm": False, "Ready": False, "Done": False}

        actions = _build_actions(pdg, prog)
        initial = frozenset((t, v) for t, v in snapshot.items())
        goal = frozenset({("Done", True)})

        first_achievers, _ = _build_rpg(actions, initial, goal)
        landmarks, orderings = _extract_landmarks(actions, first_achievers, goal, initial)

        done_fact = ("Done", True)
        ready_fact = ("Ready", True)
        assert done_fact in orderings
        assert ready_fact in orderings[done_fact]


# ---------------------------------------------------------------------------
# Arithmetic pattern recognition tests (Phase 4)
# ---------------------------------------------------------------------------


class TestArithmeticPatterns:
    def test_calc_increment_detected(self):
        """calc(Step + 1, Step) returns ('increment', 1)."""
        from pyrung import calc

        Step = Int("Step")
        Enable = Bool("Enable", external=True)
        with Program() as prog:
            with Rung(Enable, Step == 0):
                calc(Step + 1, Step)
        rung = prog.rungs[0]

        from pyrung.core.analysis.prove.waypoints import _written_value_for_tag

        wv = _written_value_for_tag(rung, "Step")
        assert wv is not None
        assert wv[0] == "increment"
        assert wv[1] == 1

    def test_calc_decrement_detected(self):
        """calc(Step - 1, Step) returns ('decrement', 1)."""
        from pyrung import calc

        Step = Int("Step")
        Enable = Bool("Enable", external=True)
        with Program() as prog:
            with Rung(Enable, Step == 5):
                calc(Step - 1, Step)
        rung = prog.rungs[0]

        from pyrung.core.analysis.prove.waypoints import _written_value_for_tag

        wv = _written_value_for_tag(rung, "Step")
        assert wv is not None
        assert wv[0] == "decrement"
        assert wv[1] == 1

    def test_calc_increment_in_value_transitions(self):
        """Increment pattern produces correct transitions in the graph."""
        from pyrung import calc

        Step = Int("Step", choices={0: "S0", 1: "S1", 2: "S2", 3: "S3"})
        Enable = Bool("Enable", external=True)
        with Program() as prog:
            with Rung(Enable, Step == 0):
                calc(Step + 1, Step)
            with Rung(Step == 1):
                calc(Step + 1, Step)
            with Rung(Step == 2):
                calc(Step + 1, Step)
        pdg = build_program_graph(prog)
        transitions = _build_value_transitions("Step", pdg, prog)
        assert 1 in transitions.get(0, set()), f"missing 0→1, got {transitions}"
        assert 2 in transitions.get(1, set()), f"missing 1→2, got {transitions}"
        assert 3 in transitions.get(2, set()), f"missing 2→3, got {transitions}"

    def test_how_with_calc_step_counter(self):
        """End-to-end: how() works with calc-based step counter."""
        from pyrung import calc

        Step = Int("Step")
        Done = Bool("Done")
        Go = Bool("Go", external=True)
        with Program() as prog:
            with Rung(Go, Step == 0):
                calc(Step + 1, Step)
            with Rung(Step == 1):
                calc(Step + 1, Step)
            with Rung(Step == 2):
                latch(Done)
        plc = PLC(prog, dt=0.010)
        path = plc.how(Done)
        assert path.reachable

        replay = PLC(prog, dt=0.010)
        for step in path.steps:
            replay.patch(step.action)
            for _ in range(step.scans):
                replay.step()
        assert replay.state.tags["Done"] is True


# ---------------------------------------------------------------------------
# Reasonable orderings tests (Phase 2)
# ---------------------------------------------------------------------------


class TestReasonableOrderings:
    def test_reasonable_ordering_detected(self):
        """Reasonable ordering: q requires p's tag at a different value."""
        from pyrung.core.analysis.prove.waypoints import (
            _Action,
            _compute_reasonable_orderings,
        )

        p_fact = ("StateCurrent", 5)
        q_fact = ("StateRequested", 10)
        landmarks = {p_fact, q_fact}

        actions = [
            _Action(0, frozenset({("StateCurrent", 3)}), frozenset({q_fact})),
        ]
        first_achievers = {q_fact: {0}}

        reasonable = _compute_reasonable_orderings(landmarks, actions, first_achievers)
        assert q_fact in reasonable
        assert p_fact in reasonable[q_fact]


# ---------------------------------------------------------------------------
# Frontier-based refinement tests (Phase 3)
# ---------------------------------------------------------------------------


class TestFrontierRefinement:
    def test_frontier_collector_populated(self):
        """BFS with tight depth budget populates frontier_collector."""
        from pyrung.core.analysis.prove import _build_explore_context
        from pyrung.core.analysis.prove.bfs import _bfs_explore_gen

        Step = Int("Step", choices={0: "S0", 1: "S1", 2: "S2", 3: "S3", 4: "S4", 5: "S5"})
        Go = Bool("Go", external=True)
        with Program() as prog:
            with Rung(Go):
                calc(Step + 1, Step)

        snapshot = {"Step": 0, "Go": False}
        context = _build_explore_context(
            prog,
            scope=["Step", "Go"],
            project=("Step",),
            initial_state=snapshot,
        )

        frontier: list[dict] = []
        gen = _bfs_explore_gen(
            context,
            predicates=[lambda s: s.get("Step") != 5],
            depth_budget=3,
            max_states=10_000,
            initial_state=snapshot,
            frontier_collector=frontier,
        )
        next(gen, None)
        assert len(frontier) > 0
        frontier_step_values = {s.get("Step") for s in frontier}
        assert any(v is not None and v > 0 for v in frontier_step_values)

    def test_value_gap_creates_sub_waypoints(self):
        """Value-gap strategy discovers intermediate values from frontier."""
        from pyrung.core.analysis.prove.waypoints import _analyze_frontier_value_gap

        Step = Int("Step", choices={0: "S0", 1: "S1", 2: "S2", 3: "S3"})
        Go = Bool("Go", external=True)
        with Program() as prog:
            with Rung(Go, Step == 0):
                copy(1, Step)
            with Rung(Step == 1):
                copy(2, Step)
            with Rung(Step == 2):
                copy(3, Step)
        pdg = build_program_graph(prog)

        wp = _Waypoint("Step", 3, pdg.upstream_slice("Step"))
        snapshot = {"Step": 0, "Go": False}
        frontier = [
            {"Step": 1, "Go": True},
            {"Step": 2, "Go": False},
        ]

        result = _analyze_frontier_value_gap(
            frontier,
            wp,
            snapshot,
            pdg,
            prog,
            frozenset(),
        )
        assert result is not None
        assert result.strategy == "value_gap"
        assert len(result.waypoints) >= 2
        values = [w.required_value for w in result.waypoints]
        assert values[-1] == 3

    def test_condition_blocking_finds_prerequisite(self):
        """Condition-blocking strategy finds tags that block writer conditions."""
        from pyrung.core.analysis.prove.waypoints import (
            _analyze_frontier_condition_blocking,
        )

        A = Bool("A", external=True)
        B = Bool("B")
        C = Bool("C")
        with Program() as prog:
            with Rung(A):
                latch(B)
            with Rung(B):
                latch(C)
        pdg = build_program_graph(prog)

        wp = _Waypoint("C", True, pdg.upstream_slice("C"))
        snapshot = {"A": False, "B": False, "C": False}
        frontier = [
            {"A": True, "B": False, "C": False},
            {"A": False, "B": False, "C": False},
        ]

        result = _analyze_frontier_condition_blocking(
            frontier,
            wp,
            snapshot,
            pdg,
            prog,
            frozenset(),
        )
        assert result is not None
        assert result.strategy == "condition_blocking"
        sub_tags = [w.tag_name for w in result.waypoints]
        assert "B" in sub_tags
        assert sub_tags[-1] == "C"

    def test_refinement_cap_prevents_runaway(self):
        """_MAX_REFINEMENTS limits refinement attempts."""
        from pyrung.core.analysis.prove.waypoints import (
            _MAX_REFINEMENTS,
            _refine_waypoint,
        )

        Step = Int("Step", choices={0: "S0", 1: "S1", 2: "S2", 3: "S3"})
        Go = Bool("Go", external=True)
        with Program() as prog:
            with Rung(Go, Step == 0):
                copy(1, Step)
            with Rung(Step == 1):
                copy(2, Step)
            with Rung(Step == 2):
                copy(3, Step)
        pdg = build_program_graph(prog)

        wp = _Waypoint("Step", 3, pdg.upstream_slice("Step"))
        snapshot = {"Step": 0, "Go": False}
        frontier = [{"Step": 1, "Go": True}]

        tried: set[str] = set()
        count = 0
        for _ in range(_MAX_REFINEMENTS + 2):
            result = _refine_waypoint(
                frontier,
                wp,
                snapshot,
                pdg,
                prog,
                frozenset(),
                skip=tried,
            )
            if result is None:
                break
            tried.add(result.strategy)
            count += 1
        assert count <= _MAX_REFINEMENTS

    def test_how_with_refinement_end_to_end(self):
        """End-to-end: how() succeeds on a program that benefits from refinement."""
        Step = Int("Step")
        Done = Bool("Done")
        Go = Bool("Go", external=True)
        with Program() as prog:
            with Rung(Go, Step == 0):
                copy(1, Step)
            with Rung(Step == 1):
                copy(2, Step)
            with Rung(Step == 2):
                copy(3, Step)
            with Rung(Step == 3):
                latch(Done)
        plc = PLC(prog, dt=0.010)
        path = plc.how(Done)
        assert path.reachable

        replay = PLC(prog, dt=0.010)
        for step in path.steps:
            replay.patch(step.action)
            for _ in range(step.scans):
                replay.step()
        assert replay.state.tags["Done"] is True

    def test_no_frontier_skips_refinement(self):
        """When BFS completes without depth truncation, no frontier is collected."""
        from pyrung.core.analysis.prove import _build_explore_context
        from pyrung.core.analysis.prove.bfs import _bfs_explore_gen

        X = Bool("X")
        A = Bool("A", external=True)
        with Program() as prog:
            with Rung(A):
                latch(X)

        snapshot = {"X": False, "A": False}
        context = _build_explore_context(prog, initial_state=snapshot)

        frontier: list[dict] = []
        gen = _bfs_explore_gen(
            context,
            predicates=[lambda s: not s.get("X")],
            depth_budget=50,
            max_states=10_000,
            initial_state=snapshot,
            frontier_collector=frontier,
        )
        next(gen, None)
        assert len(frontier) == 0


# ---------------------------------------------------------------------------
# Range-fill arithmetic writers (seeding)
# ---------------------------------------------------------------------------


class TestRangeFillArithmeticWriters:
    def test_increment_fills_domain_range(self):
        """calc(tag + 1, tag) fills the domain between min and max observed values."""
        from pyrung.core.analysis.prove.seeding import _range_fill_arithmetic_writers

        Step = Int("Step")
        Go = Bool("Go", external=True)
        with Program() as prog:
            with Rung(Go):
                calc(Step + 1, Step)

        pdg = build_program_graph(prog)
        discovered = {"Step": (0, 5)}
        _range_fill_arithmetic_writers(discovered, prog, pdg)
        assert discovered["Step"] == (0, 1, 2, 3, 4, 5)

    def test_increment_stride_2_fills_even_steps(self):
        """calc(tag + 2, tag) fills at stride 2."""
        from pyrung.core.analysis.prove.seeding import _range_fill_arithmetic_writers

        Step = Int("Step")
        Go = Bool("Go", external=True)
        with Program() as prog:
            with Rung(Go):
                calc(Step + 2, Step)

        pdg = build_program_graph(prog)
        discovered = {"Step": (0, 6)}
        _range_fill_arithmetic_writers(discovered, prog, pdg)
        assert discovered["Step"] == (0, 2, 4, 6)

    def test_literal_only_writers_left_unchanged(self):
        """Tags with only literal writers don't get range-filled."""
        from pyrung.core.analysis.prove.seeding import _range_fill_arithmetic_writers

        Step = Int("Step")
        Go1 = Bool("Go1", external=True)
        Go2 = Bool("Go2", external=True)
        with Program() as prog:
            with Rung(Go1):
                copy(0, Step)
            with Rung(Go2):
                copy(10, Step)

        pdg = build_program_graph(prog)
        discovered = {"Step": (0, 10)}
        _range_fill_arithmetic_writers(discovered, prog, pdg)
        assert discovered["Step"] == (0, 10)

    def test_single_value_domain_skipped(self):
        """Single-value domain isn't range-filled (nothing to fill between)."""
        from pyrung.core.analysis.prove.seeding import _range_fill_arithmetic_writers

        Step = Int("Step")
        Go = Bool("Go", external=True)
        with Program() as prog:
            with Rung(Go):
                calc(Step + 1, Step)

        pdg = build_program_graph(prog)
        discovered = {"Step": (3,)}
        _range_fill_arithmetic_writers(discovered, prog, pdg)
        assert discovered["Step"] == (3,)


# ---------------------------------------------------------------------------
# Domain-threaded frontier value-gap
# ---------------------------------------------------------------------------


class TestDomainThreadedValueGap:
    def test_domain_value_path_ascending(self):
        """_domain_value_path builds ascending path from domain values."""
        path = _domain_value_path(0, 5, (0, 1, 2, 3, 4, 5))
        assert path == [1, 2, 3, 4, 5]

    def test_domain_value_path_descending(self):
        """_domain_value_path builds descending path when target < initial."""
        path = _domain_value_path(5, 0, (0, 1, 2, 3, 4, 5))
        assert path == [4, 3, 2, 1, 0]

    def test_domain_value_path_no_intermediates(self):
        """Returns None when domain has no values between initial and target."""
        path = _domain_value_path(0, 1, (0, 1))
        assert path is None

    def test_domain_value_path_non_numeric(self):
        """Returns None for non-numeric values."""
        path = _domain_value_path("a", "b", ("a", "b", "c"))
        assert path is None

    def test_frontier_value_gap_uses_domain_fallback(self):
        """When static transitions fail, domain from pipeline_cache is used."""
        Step = Int("Step")
        Go = Bool("Go", external=True)
        with Program() as prog:
            # calc doesn't produce parseable static transitions for
            # _build_value_transitions (no condition on Step guards the write)
            with Rung(Go):
                calc(Step + 1, Step)

        pdg = build_program_graph(prog)
        wp = _Waypoint("Step", 4, pdg.upstream_slice("Step"))
        snapshot = {"Step": 0, "Go": False}
        frontier = [{"Step": 2, "Go": True}]

        class FakeCache:
            stateful_dims = {"Step": (0, 1, 2, 3, 4)}
            nondeterministic_dims = {}

        result = _analyze_frontier_value_gap(
            frontier,
            wp,
            snapshot,
            pdg,
            prog,
            frozenset(),
            pipeline_cache=FakeCache(),
        )
        assert result is not None
        assert result.strategy == "value_gap"
        values = [w.required_value for w in result.waypoints]
        assert values == [1, 2, 3, 4]

    def test_frontier_value_gap_static_preferred_over_domain(self):
        """Static transition path is preferred when available."""
        Step = Int("Step", choices={0: "S0", 1: "S1", 2: "S2", 3: "S3"})
        Go = Bool("Go", external=True)
        with Program() as prog:
            with Rung(Go, Step == 0):
                copy(1, Step)
            with Rung(Step == 1):
                copy(2, Step)
            with Rung(Step == 2):
                copy(3, Step)

        pdg = build_program_graph(prog)
        wp = _Waypoint("Step", 3, pdg.upstream_slice("Step"))
        snapshot = {"Step": 0, "Go": False}
        frontier = [{"Step": 1, "Go": True}, {"Step": 2, "Go": False}]

        class FakeCache:
            stateful_dims = {"Step": (0, 1, 2, 3, 10, 20)}
            nondeterministic_dims = {}

        result = _analyze_frontier_value_gap(
            frontier,
            wp,
            snapshot,
            pdg,
            prog,
            frozenset(),
            pipeline_cache=FakeCache(),
        )
        assert result is not None
        values = [w.required_value for w in result.waypoints]
        # Static path gives 1→2→3, not the wider domain set
        assert values == [1, 2, 3]

    def test_frontier_value_gap_no_domain_no_transitions(self):
        """Returns None when neither static transitions nor domain are available."""
        Step = Int("Step")
        Go = Bool("Go", external=True)
        with Program() as prog:
            with Rung(Go):
                calc(Step + 1, Step)

        pdg = build_program_graph(prog)
        wp = _Waypoint("Step", 4, pdg.upstream_slice("Step"))
        snapshot = {"Step": 0, "Go": False}
        frontier = [{"Step": 2, "Go": True}]

        result = _analyze_frontier_value_gap(
            frontier,
            wp,
            snapshot,
            pdg,
            prog,
            frozenset(),
            pipeline_cache=None,
        )
        assert result is None


# ---------------------------------------------------------------------------
# _frontier_has_progress unit tests
# ---------------------------------------------------------------------------


class TestFrontierHasProgress:
    def test_progress_detected(self):
        frontier = [{"Step": 1}, {"Step": 2}]
        assert _frontier_has_progress(frontier, "Step", 0) is True

    def test_no_progress(self):
        frontier = [{"Step": 0}, {"Step": 0}]
        assert _frontier_has_progress(frontier, "Step", 0) is False

    def test_empty_frontier(self):
        assert _frontier_has_progress([], "Step", 0) is False

    def test_tag_absent_from_frontier(self):
        frontier = [{"Other": 5}]
        assert _frontier_has_progress(frontier, "Step", 0) is False

    def test_mixed_progress(self):
        frontier = [{"Step": 0}, {"Step": 3}]
        assert _frontier_has_progress(frontier, "Step", 0) is True


# ---------------------------------------------------------------------------
# _get_domain unit tests
# ---------------------------------------------------------------------------


class TestGetDomain:
    def test_from_stateful_dims(self):
        class FakeCache:
            stateful_dims = {"Step": (0, 1, 2, 3)}
            nondeterministic_dims = {}

        assert _get_domain("Step", FakeCache()) == (0, 1, 2, 3)

    def test_from_nondeterministic_dims(self):
        class FakeCache:
            stateful_dims = {}
            nondeterministic_dims = {"Cmd": (0, 1, 2)}

        assert _get_domain("Cmd", FakeCache()) == (0, 1, 2)

    def test_stateful_preferred_over_nondeterministic(self):
        class FakeCache:
            stateful_dims = {"Tag": (10, 20)}
            nondeterministic_dims = {"Tag": (1, 2, 3)}

        assert _get_domain("Tag", FakeCache()) == (10, 20)

    def test_none_cache(self):
        assert _get_domain("Step", None) is None

    def test_tag_not_in_cache(self):
        class FakeCache:
            stateful_dims = {}
            nondeterministic_dims = {}

        assert _get_domain("Missing", FakeCache()) is None


# ---------------------------------------------------------------------------
# _search_relevant_cone_size unit tests (L2 mega-cone gate metric)
# ---------------------------------------------------------------------------


class TestSearchRelevantConeSize:
    """The L2 gate measures search-relevant width, not raw len(cone)."""

    def test_excludes_combinational_and_size1_constants(self):
        # Mirrors the real how(fill_solv_nc) cone: 11 search-relevant tags
        # (6 stateful + 5 wide-ND) buried in a 20-tag cone padded with
        # combinational Bools and size-1 ND constants.
        wp = _Waypoint(
            "fill_solv_nc",
            True,
            frozenset(
                {
                    # 6 stateful
                    "fill_stepNumber",
                    "msg_error",
                    "sub_fillFilling",
                    "sub_fillOff",
                    "t_fillSlow_Done",
                    "t_fillTimeout_Done",
                    # 5 nondeterministic inputs with a domain wider than one value
                    "HMI_fill",
                    "HMI_resetError",
                    "sv_levelHtMax",
                    "sv_levelHtMin",
                    "systemLevel_opt2011",
                    # 2 size-1 ND constants — no branching
                    "tsv_fillSlow_ss",
                    "tsv_fillTimeout_ss",
                    # 7 combinationally-derived Bools — pure functions, no branching
                    "alarm",
                    "alarm_fillTimeout",
                    "alarm_levelMaxHt",
                    "fill",
                    "pv_LevelHt",
                    "warn_fillSlow",
                    "warn_levelMinHt",
                }
            ),
        )
        nd_dims = {
            "HMI_fill": (False, True),
            "HMI_resetError": (False, True),
            "sv_levelHtMax": (0.0, 1.0, 2.0, 3.0),
            "sv_levelHtMin": (0.0, 1.0, 2.0, 3.0),
            "systemLevel_opt2011": (0.0, 50.0, 100.0),
            "tsv_fillSlow_ss": (5,),
            "tsv_fillTimeout_ss": (10,),
        }
        sd_dims = {
            "fill_stepNumber": (0, 1, 3, 5),
            "msg_error": (False, True),
            "sub_fillFilling": (False, True),
            "sub_fillOff": (False, True),
            "t_fillSlow_Done": (False, True),
            "t_fillTimeout_Done": (False, True),
        }
        assert len(wp.cone) == 20
        # 6 stateful + 5 wide-ND, excluding the 2 size-1 constants and 7 combinational
        assert _search_relevant_cone_size(wp, nd_dims, sd_dims) == 11

    def test_falls_back_to_len_when_no_dims(self):
        wp = _Waypoint("T", True, frozenset({"A", "B", "C"}))
        assert _search_relevant_cone_size(wp, {}, {}) == 3

    def test_stateful_dim_counts_even_if_also_size1(self):
        wp = _Waypoint("T", True, frozenset({"S"}))
        assert _search_relevant_cone_size(wp, {}, {"S": (7,)}) == 1


# ---------------------------------------------------------------------------
# _run_single_wp recursive decomposition tests
# ---------------------------------------------------------------------------


class TestRunSingleWpDecomposition:
    def test_recursive_decomposition_on_deep_counter(self):
        """A 7-step counter with budget=3 requires recursive decomposition."""
        Go = Bool("Go", external=True)
        Step = Int("Step")
        with Program() as prog:
            with Rung(Go, Step == 0):
                copy(1, Step)
            with Rung(Step == 1):
                copy(2, Step)
            with Rung(Step == 2):
                copy(3, Step)
            with Rung(Step == 3):
                copy(4, Step)
            with Rung(Step == 4):
                copy(5, Step)
            with Rung(Step == 5):
                copy(6, Step)

        plc = PLC(prog, dt=0.010)
        path = plc.how(Step == 6, max_steps=3)
        assert path.reachable

        replay = PLC(prog, dt=0.010)
        for step in path.steps:
            replay.patch(step.action)
            for _ in range(step.scans):
                replay.step()
        assert replay.state.tags["Step"] == 6

    def test_how_e2e_recursive_only_path(self):
        """End-to-end: program where recursive decomposition is the only path.

        The 8-step counter needs budget > 8 for flat BFS, but with budget=3
        the waypoint planner must recursively subdivide to succeed.
        """
        Go = Bool("Go", external=True)
        Step = Int("Step")
        Done = Bool("Done")
        with Program() as prog:
            with Rung(Go, Step == 0):
                copy(1, Step)
            with Rung(Step == 1):
                copy(2, Step)
            with Rung(Step == 2):
                copy(3, Step)
            with Rung(Step == 3):
                copy(4, Step)
            with Rung(Step == 4):
                copy(5, Step)
            with Rung(Step == 5):
                copy(6, Step)
            with Rung(Step == 6):
                copy(7, Step)
            with Rung(Step == 7):
                latch(Done)

        plc = PLC(prog, dt=0.010)
        path = plc.how(Done, max_steps=3)
        assert path.reachable

        replay = PLC(prog, dt=0.010)
        for step in path.steps:
            replay.patch(step.action)
            for _ in range(step.scans):
                replay.step()
        assert replay.state.tags["Done"] is True


class TestValueGapFallbackWithoutPipelineCache:
    def test_static_fallback_still_works(self):
        """_analyze_frontier_value_gap with no pipeline_cache uses static transitions."""
        Step = Int("Step", choices={0: "S0", 1: "S1", 2: "S2", 3: "S3"})
        Go = Bool("Go", external=True)
        with Program() as prog:
            with Rung(Go, Step == 0):
                copy(1, Step)
            with Rung(Step == 1):
                copy(2, Step)
            with Rung(Step == 2):
                copy(3, Step)

        pdg = build_program_graph(prog)
        wp = _Waypoint("Step", 3, pdg.upstream_slice("Step"))
        snapshot = {"Step": 0, "Go": False}
        frontier = [{"Step": 1, "Go": True}]

        result = _analyze_frontier_value_gap(
            frontier,
            wp,
            snapshot,
            pdg,
            prog,
            frozenset(),
            pipeline_cache=None,
        )
        assert result is not None
        assert result.strategy == "value_gap"
        values = [w.required_value for w in result.waypoints]
        assert values == [1, 2, 3]


# ---------------------------------------------------------------------------
# Kernel-probed cone expansion
# ---------------------------------------------------------------------------


def _make_indirect_program():
    """Program where an indirect lookup hides a dependency from the PDG.

    Idx (external Int) selects a slot in a Block.  ``copy(blk[Idx], Result)``
    writes Result, and ``copy(Result, Output)`` propagates to Output.
    The PDG cannot trace Idx -> Result through the indirect access, so
    ``_value_aware_cone("Output", ...)`` will NOT include Idx.
    """
    from pyrung.core import Block, TagType

    Idx = Int("Idx", external=True, default=0, min=0, max=3)
    Result = Int("Result")
    Output = Int("Output")
    blk = Block("LUT", TagType.INT, 0, 3, default_factory=lambda addr: (addr + 1) * 10)

    with Program() as prog:
        with Rung():
            copy(blk[Idx], Result)
        with Rung():
            copy(Result, Output)
    return prog, Idx, Result, Output


class TestProbeConeExpansion:
    def test_discovers_indirect_dependency(self):
        """Kernel probing finds Idx as affecting Result through indirect access."""
        from pyrung.circuitpy.codegen import compile_kernel
        from pyrung.core.analysis.prove.waypoints import _probe_cone_expansion

        prog, Idx, Result, Output = _make_indirect_program()
        compiled = compile_kernel(prog, blockless=True)
        pdg = build_program_graph(prog)
        cone = _value_aware_cone("Output", 20, pdg, prog)

        assert "Idx" not in cone, "PDG should not trace through indirect access"
        assert "Result" in cone

        kernel = compiled.create_kernel()
        snapshot = dict(kernel.tags)
        expanded = _probe_cone_expansion(cone, "Output", compiled, snapshot, None)
        assert "Idx" in expanded, "kernel probing should discover Idx"

    def test_non_affecting_excluded(self):
        """Tags that don't affect cone tags are NOT added."""
        from pyrung.circuitpy.codegen import compile_kernel
        from pyrung.core import Block, TagType
        from pyrung.core.analysis.prove.waypoints import _probe_cone_expansion

        Unrelated = Bool("Unrelated", external=True)
        Idx = Int("Idx", external=True, default=0, min=0, max=3)
        Result = Int("Result")
        Output = Int("Output")
        Sink = Bool("Sink")
        blk = Block("LUT", TagType.INT, 0, 3, default_factory=lambda a: (a + 1) * 10)

        with Program() as prog:
            with Rung():
                copy(blk[Idx], Result)
            with Rung():
                copy(Result, Output)
            with Rung(Unrelated):
                out(Sink)

        compiled = compile_kernel(prog, blockless=True)
        pdg = build_program_graph(prog)
        cone = _value_aware_cone("Output", 20, pdg, prog)
        kernel = compiled.create_kernel()
        snapshot = dict(kernel.tags)
        expanded = _probe_cone_expansion(cone, "Output", compiled, snapshot, None)

        assert "Idx" in expanded
        assert "Unrelated" not in expanded
        assert "Sink" not in expanded

    def test_no_expansion_when_complete(self):
        """Returns original cone when probing finds nothing new."""
        from pyrung.circuitpy.codegen import compile_kernel
        from pyrung.core.analysis.prove.waypoints import _probe_cone_expansion

        A = Bool("A", external=True)
        B = Bool("B")
        with Program() as prog:
            with Rung(A):
                out(B)

        compiled = compile_kernel(prog, blockless=True)
        pdg = build_program_graph(prog)
        cone = _value_aware_cone("B", True, pdg, prog)
        assert "A" in cone

        kernel = compiled.create_kernel()
        snapshot = dict(kernel.tags)
        expanded = _probe_cone_expansion(cone, "B", compiled, snapshot, None)
        assert expanded == cone

    def test_iterative_discovers_transitive_chain(self):
        """A→B→Result chain through two indirect lookups: both discovered."""
        from pyrung.circuitpy.codegen import compile_kernel
        from pyrung.core import Block, TagType
        from pyrung.core.analysis.prove.waypoints import _probe_cone_expansion

        A = Int("A", external=True, default=0, min=0, max=1)
        B = Int("B", default=0)
        Result = Int("Result")
        lut1 = Block("LUT1", TagType.INT, 0, 1, default_factory=lambda a: a)
        lut2 = Block("LUT2", TagType.INT, 0, 1, default_factory=lambda a: (a + 1) * 100)

        with Program() as prog:
            with Rung():
                copy(lut1[A], B)
            with Rung():
                copy(lut2[B], Result)

        compiled = compile_kernel(prog, blockless=True)
        pdg = build_program_graph(prog)
        cone = _value_aware_cone("Result", 200, pdg, prog)

        assert "A" not in cone
        assert "B" not in cone

        kernel = compiled.create_kernel()
        snapshot = dict(kernel.tags)
        expanded = _probe_cone_expansion(cone, "Result", compiled, snapshot, None, max_iterations=2)
        assert "B" in expanded, "first iteration should discover B"
        assert "A" in expanded, "second iteration should discover A"


class TestRunSingleWpConeExpansion:
    def test_empty_frontier_triggers_expansion_and_succeeds(self):
        """Waypoint with narrow cone fails BFS, expansion widens, retry succeeds."""
        prog, Idx, Result, Output = _make_indirect_program()
        plc = PLC(prog, dt=0.010)
        path = plc.how(Output == 20, max_steps=20)
        assert path.reachable
        for step in path.steps:
            plc.patch(step.action)
            for _ in range(step.scans):
                plc.step()
        assert plc.state.tags["Output"] == 20
