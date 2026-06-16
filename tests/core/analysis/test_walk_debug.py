"""Tests for how(debug=True) debug trace."""

from __future__ import annotations

from pyrung import Bool, Int, Program, Rung, copy, latch
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.walk.agenda import _drive, _establish, _PlanNode, _Request
from pyrung.core.analysis.walk.base import (
    HoldStore,
    NoGoodStore,
    _DebugSink,
    _WalkBudget,
    _WalkContext,
)
from pyrung.core.analysis.walk.fold import _build_jump_context
from pyrung.core.analysis.walk.passes import run_walk_passes
from pyrung.core.runner import PLC


def _guarded_latch():
    Enable = Bool("Enable", external=True)
    Cmd = Bool("Cmd", external=True)
    Gate = Bool("Gate")
    Out = Bool("Out")
    with Program() as prog:
        with Rung(Enable):
            latch(Gate)
        with Rung(Gate, Cmd):
            latch(Out)
    return prog, Enable, Cmd, Gate, Out


def _mode_resets_step():
    Go1 = Bool("Go1", external=True)
    Go2 = Bool("Go2", external=True)
    ModeBtn = Bool("ModeBtn", external=True)
    Mode = Int("Mode")
    Step = Int("Step")
    with Program() as prog:
        with Rung(Go1, Step == 0):
            copy(1, Step)
        with Rung(Go2, Step == 1):
            copy(2, Step)
        with Rung(ModeBtn, Mode == 0):
            copy(2, Mode)
            copy(0, Step)
    return prog, Mode, Step


def _depth_refusal_context(plc: PLC, prog: Program, target: Bool) -> _WalkContext:
    pdg = build_program_graph(prog)
    advice, journal = run_walk_passes(prog, pdg)
    return _WalkContext(
        pdg=pdg,
        program=prog,
        known=plc._known_tags_by_name,
        ext_inputs=["Go"],
        edge_ext=set(),
        jump_ctx=_build_jump_context(
            plc,
            pdg,
            prog,
            target_names=frozenset({target.name}),
            advice=advice,
            journal=journal,
        ),
        nogoods=NoGoodStore(),
        holds=HoldStore(),
        budget=_WalkBudget(),
        advice=advice,
        journal=journal,
        debug_sink=_DebugSink(),
    )


def _go_target_program() -> tuple[Program, Bool]:
    go = Bool("Go", external=True)
    target = Bool("Target")
    with Program() as prog:
        with Rung(go):
            latch(target)
    return prog, target


class TestDebugTrace:
    def test_debug_false_no_trace(self):
        prog, Enable, Cmd, Gate, Out = _guarded_latch()
        plc = PLC(prog, dt=0.010)
        path = plc.how(Out)
        assert path.reachable
        assert path.debug_trace is None

    def test_debug_true_has_trace(self):
        prog, Enable, Cmd, Gate, Out = _guarded_latch()
        plc = PLC(prog, dt=0.010)
        path = plc.how(Out, debug=True)
        assert path.reachable
        assert path.debug_trace is not None
        assert len(path.debug_trace) > 0

    def test_trace_has_cone_snapshot(self):
        prog, Enable, Cmd, Gate, Out = _guarded_latch()
        plc = PLC(prog, dt=0.010)
        path = plc.how(Out, debug=True)
        assert path.reachable
        events = path.debug_trace.events
        cone_events = [e for e in events if e.kind == "cone-snapshot"]
        assert len(cone_events) >= 1
        assert cone_events[0].tag == "Out"

    def test_trace_has_goal_lifecycle(self):
        prog, Enable, Cmd, Gate, Out = _guarded_latch()
        plc = PLC(prog, dt=0.010)
        path = plc.how(Out, debug=True)
        assert path.reachable
        events = path.debug_trace.events
        kinds = {e.kind for e in events}
        assert "goal-start" in kinds
        assert "goal-resolved" in kinds

    def test_trace_str_renders(self):
        prog, Enable, Cmd, Gate, Out = _guarded_latch()
        plc = PLC(prog, dt=0.010)
        path = plc.how(Out, debug=True)
        text = str(path.debug_trace)
        assert "cone-snapshot" in text
        assert "goal-start" in text

    def test_trace_in_path_str(self):
        prog, Enable, Cmd, Gate, Out = _guarded_latch()
        plc = PLC(prog, dt=0.010)
        path = plc.how(Out, debug=True)
        text = str(path)
        assert "Debug Trace" in text

    def test_failed_walk_has_trace(self):
        x = Bool("x", external=True)
        y = Bool("y")
        z = Bool("z")
        with Program() as prog:
            with Rung(x):
                latch(y)
            with Rung(y, ~y):
                latch(z)
        plc = PLC(prog, dt=0.010)
        path = plc.how(z, debug=True)
        assert not path.reachable
        assert path.debug_trace is not None
        assert len(path.debug_trace) > 0
        text = str(path)
        assert "Debug Trace" in text

    def test_bounds_refusal_event_on_depth_limit(self):
        prog, target = _go_target_program()
        plc = PLC(prog, dt=0.010)
        ctx = _depth_refusal_context(plc, prog, target)
        req = _Request(
            runner=plc,
            goal=(target.name, True),
            depth=7,
            visited=frozenset(),
            budget=16,
            provenance="test",
        )
        node = _PlanNode(goal=req.goal, provenance=req.provenance, depth=req.depth)

        result = _drive(ctx, _establish(ctx, req, node), node, plc)

        assert result is None
        events = [e for e in ctx.debug_sink.events if e.kind == "bounds-refusal"]
        assert len(events) == 1
        assert "depth" in events[0].detail

    def test_recovery_snapshot_event_on_cross_guard(self):
        from tests.core.analysis.test_walk_nogood import _program as _cross_guard_program

        prog, target = _cross_guard_program()
        plc = PLC(prog, dt=0.010)
        path = plc.how(target, debug=True)

        assert path.reachable
        events = [e for e in path.debug_trace.events if e.kind == "recovery-snapshot"]
        assert events
        assert "iter=" in events[0].detail
        assert "target_current=" in events[0].detail
        assert "mined_goals=" in events[0].detail

    def test_progress_regression_event_on_compound_reorder(self):
        prog, mode, step = _mode_resets_step()
        plc = PLC(prog, dt=0.010)
        path = plc.how(step == 2, mode == 2, debug=True)

        assert path.reachable
        events = [e for e in path.debug_trace.events if e.kind == "progress-regression"]
        assert events
        assert any(e.tag == "Step" for e in events)
        assert any("clobbered_by=" in e.detail for e in events)

    def test_dead_end_snapshot_on_budget_exhaustion(self):
        prog, Enable, Cmd, Gate, Out = _guarded_latch()
        plc = PLC(prog, dt=0.010)
        path = plc.how(Out, walk_seconds=0.0, debug=True)

        assert not path.reachable
        events = [e for e in path.debug_trace.events if e.kind == "dead-end-snapshot"]
        assert len(events) == 1
        assert "open_goals" in events[0].detail
        assert "holds" in events[0].detail
        assert "progress_credits" in events[0].detail

    def test_no_diagnostic_events_when_debug_false(self):
        prog, Enable, Cmd, Gate, Out = _guarded_latch()
        plc = PLC(prog, dt=0.010)
        path = plc.how(Out, walk_seconds=0.0)

        assert not path.reachable
        assert path.debug_trace is None
