"""Tests for how(debug=True) debug trace."""

from __future__ import annotations

from pyrung import Bool, Program, Rung, latch
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
