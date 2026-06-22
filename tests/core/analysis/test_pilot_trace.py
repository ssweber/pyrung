"""Unit tests for pilot/trace.py — backward trace on toy programs."""

from __future__ import annotations

from pyrung import PLC, Bool, Int, Program, Timer, calc, call, copy, on_delay, out, rung, subroutine
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.pilot.trace import TraceNode, compute_steerable, trace_back


def _steerable_names(node: TraceNode) -> set[str]:
    return {t for t, _v in node.steerable_leaves()}


def _known(logic: Program) -> dict:
    plc = PLC(logic)
    return plc._known_tags_by_name


# -- Test 1: Boolean chain --------------------------------------------------


def test_bool_chain():
    """out chain: x_Enable -> y_Armed -> (+ x_Trigger) -> y_Target"""
    x_Enable = Bool("x_Enable", external=True)
    x_Trigger = Bool("x_Trigger", external=True)
    y_Armed = Bool("y_Armed")
    y_Target = Bool("y_Target")

    with Program() as logic:
        with rung(x_Enable):
            out(y_Armed)
        with rung(y_Armed, x_Trigger):
            out(y_Target)

    pdg = build_program_graph(logic)
    steerable = compute_steerable(pdg, _known(logic), logic)

    tree = trace_back("y_Target", True, {}, pdg, logic, steerable)
    names = _steerable_names(tree)
    assert "x_Enable" in names
    assert "x_Trigger" in names


# -- Test 2: Copy data-flow -------------------------------------------------


def test_copy_data_flow():
    """copy(src, dest): trace finds the gate AND the copy source."""
    x_Go = Bool("x_Go", external=True)
    Requested = Int("Requested", external=True)
    Current = Int("Current")

    with Program() as logic:
        with rung(x_Go):
            copy(Requested, Current)

    pdg = build_program_graph(logic)
    steerable = compute_steerable(pdg, _known(logic), logic)

    tree = trace_back("Current", 5, {}, pdg, logic, steerable)
    names = _steerable_names(tree)
    assert "x_Go" in names
    assert "Requested" in names


# -- Test 3: Calc expression -------------------------------------------------


def test_calc_affine():
    """calc(dest = src + 10): trace finds the gate and the Affine source."""
    x_Go = Bool("x_Go", external=True)
    Raw = Int("Raw", external=True)
    Scaled = Int("Scaled")

    with Program() as logic:
        with rung(x_Go):
            calc(Raw + 10, Scaled)

    pdg = build_program_graph(logic)
    steerable = compute_steerable(pdg, _known(logic), logic)

    tree = trace_back("Scaled", 42, {}, pdg, logic, steerable)
    names = _steerable_names(tree)
    assert "x_Go" in names
    assert "Raw" in names


# -- Test 4: Subroutine call gate -------------------------------------------


def test_subroutine_call_gate():
    """call gate: trace crosses the subroutine boundary."""
    x_Enable = Bool("x_Enable", external=True)
    x_Inner = Bool("x_Inner", external=True)
    y_Result = Bool("y_Result")

    with Program() as logic:
        with subroutine("do_work"):
            with rung(x_Inner):
                out(y_Result)

        with rung(x_Enable):
            call("do_work")

    pdg = build_program_graph(logic)
    steerable = compute_steerable(pdg, _known(logic), logic)

    tree = trace_back("y_Result", True, {}, pdg, logic, steerable)
    names = _steerable_names(tree)
    assert "x_Inner" in names
    assert "x_Enable" in names


# -- Test 5: Timer done bit -------------------------------------------------


def test_timer_done():
    """Timer done: trace finds the enable condition (x_Start)."""
    x_Start = Bool("x_Start", external=True)
    timer = Timer.clone("T1")
    y_Complete = Bool("y_Complete")

    with Program() as logic:
        with rung(x_Start):
            on_delay(timer, preset=100)
        with rung(timer.Done):
            out(y_Complete)

    pdg = build_program_graph(logic)
    steerable = compute_steerable(pdg, _known(logic), logic)

    tree = trace_back("y_Complete", True, {}, pdg, logic, steerable)
    # Timer.Done is written by the timer instruction — trace should find
    # x_Start as the enable condition on that writer rung
    names = _steerable_names(tree)
    assert "x_Start" in names


# -- Test 6: Mixed (copy + guard + subroutine) ------------------------------


def test_mixed():
    """Mixed: copy + guard + subroutine + two-level trace."""
    x_Cmd = Bool("x_Cmd", external=True)
    x_Permit = Bool("x_Permit", external=True)
    Setpoint = Int("Setpoint", external=True)
    Active = Int("Active")
    y_Running = Bool("y_Running")

    with Program() as logic:
        with subroutine("activate"):
            with rung(x_Permit):
                copy(Setpoint, Active)
            with rung(Active == 50):
                out(y_Running)

        with rung(x_Cmd):
            call("activate")

    pdg = build_program_graph(logic)
    steerable = compute_steerable(pdg, _known(logic), logic)

    tree = trace_back("y_Running", True, {}, pdg, logic, steerable)
    names = _steerable_names(tree)
    assert "x_Cmd" in names
    assert "x_Permit" in names
    assert "Setpoint" in names


# -- Test 7: (tag, value) visited key — same tag, different values -----------


def test_visited_key_tag_value():
    """Trace can visit the same tag at different values independently.

    StateCurrent needs to go 0→1→2 — the trace must discover both
    transitions, not stop after visiting StateCurrent once.
    """
    x_Reset = Bool("x_Reset", external=True)
    x_Start = Bool("x_Start", external=True)
    State = Int("State")
    y_Running = Bool("y_Running")

    with Program() as logic:
        # State 0 → 1 via x_Reset
        with rung(State == 0, x_Reset):
            copy(1, State)
        # State 1 → 2 via x_Start
        with rung(State == 1, x_Start):
            copy(2, State)
        # Output when State == 2
        with rung(State == 2):
            out(y_Running)

    pdg = build_program_graph(logic)
    steerable = compute_steerable(pdg, _known(logic), logic)

    # Start from State=0
    tree = trace_back("y_Running", True, {"State": 0}, pdg, logic, steerable)
    names = _steerable_names(tree)
    assert "x_Reset" in names, f"expected x_Reset, got {names}"
    assert "x_Start" in names, f"expected x_Start, got {names}"


# -- Test 8: Already-satisfied returns no actions ----------------------------


def test_already_satisfied():
    """When the target is already satisfied, no steerable leaves."""
    x_Go = Bool("x_Go", external=True)
    y_Out = Bool("y_Out")

    with Program() as logic:
        with rung(x_Go):
            out(y_Out)

    pdg = build_program_graph(logic)
    steerable = compute_steerable(pdg, _known(logic), logic)

    tree = trace_back("y_Out", True, {"y_Out": True}, pdg, logic, steerable)
    assert tree.satisfied
    assert tree.steerable_leaves() == []


# -- Test 9: TraceNode.ordered_actions preserves depth ordering ---------------


def test_ordered_actions_depth():
    """Deeper actions come before shallower ones (temporal prerequisite)."""
    x_Enable = Bool("x_Enable", external=True)
    x_Trigger = Bool("x_Trigger", external=True)
    y_Armed = Bool("y_Armed")
    y_Target = Bool("y_Target")

    with Program() as logic:
        with rung(x_Enable):
            out(y_Armed)
        with rung(y_Armed, x_Trigger):
            out(y_Target)

    pdg = build_program_graph(logic)
    steerable = compute_steerable(pdg, _known(logic), logic)

    tree = trace_back("y_Target", True, {}, pdg, logic, steerable)
    actions = tree.ordered_actions()
    tags = [t for t, _v in actions]
    # x_Enable is deeper (it's a prerequisite of y_Armed which gates y_Target)
    # so it should come before x_Trigger
    assert tags.index("x_Enable") < tags.index("x_Trigger")
