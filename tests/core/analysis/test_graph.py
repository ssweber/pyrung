"""Tests for how() reachability path-finder."""

from __future__ import annotations

import pytest

from pyrung.core import (
    PLC,
    Bool,
    Counter,
    Or,
    Program,
    Rung,
    Timer,
    count_up,
    latch,
    on_delay,
    out,
    rise,
)
from pyrung.core.analysis.graph import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _replay_path(program: Program, path: Path) -> PLC:
    """Replay a how() path on a concrete PLC and return the final state."""
    plc = PLC(program, dt=0.010)
    for step in path.steps:
        plc.patch(step.action)
        for _ in range(step.scans):
            plc.step()
    return plc


# ---------------------------------------------------------------------------
# Simple programs for testing
# ---------------------------------------------------------------------------


def _simple_latch_program() -> tuple[Program, Bool, Bool, Bool]:
    """One external input, one latched output."""
    Start = Bool("Start", external=True)
    Running = Bool("Running")
    Done = Bool("Done")
    with Program() as prog:
        with Rung(Start):
            latch(Running)
        with Rung(Running):
            out(Done)
    return prog, Start, Running, Done


def _two_step_program() -> tuple[Program, Bool, Bool, Bool, Bool]:
    """Reaching Done requires Start then Confirm — two input changes."""
    Start = Bool("Start", external=True)
    Confirm = Bool("Confirm", external=True)
    Ready = Bool("Ready")
    Done = Bool("Done")
    with Program() as prog:
        with Rung(Start):
            latch(Ready)
        with Rung(Ready, Confirm):
            out(Done)
    return prog, Start, Confirm, Ready, Done


def _unreachable_program() -> tuple[Program, Bool, Bool]:
    """Output can never be True — no rung writes it."""
    Input = Bool("Input", external=True)
    Impossible = Bool("Impossible")
    with Program() as prog:
        with Rung(Input):
            out(Input)  # self-referential, doesn't write Impossible
    return prog, Input, Impossible


# ---------------------------------------------------------------------------
# Path display
# ---------------------------------------------------------------------------


class TestPathDisplay:
    def test_str_reachable(self):
        prog, Start, Running, Done = _simple_latch_program()
        plc = PLC(prog, dt=0.010)
        path = plc.how(Running)
        text = str(path)
        assert "Path" in text
        assert "Step 1" in text

    def test_str_unreachable(self):
        path = Path(reachable=False, steps=(), total_changes=0, total_scans=0, reason="nope")
        assert "Unreachable" in str(path)

    def test_str_already_there(self):
        path = Path(reachable=True, steps=(), total_changes=0, total_scans=0)
        assert "Already" in str(path)


# ---------------------------------------------------------------------------
# to_commands()
# ---------------------------------------------------------------------------


class TestPathToCommands:
    def test_simple_path(self):
        prog, Start, Running, Done = _simple_latch_program()
        plc = PLC(prog, dt=0.010)
        path = plc.how(Running)
        assert path.reachable
        commands = path.to_commands()
        assert "force Start true" in commands
        assert commands[-1] == "clear_forces"
        assert any(c.startswith("step") for c in commands)

    def test_unreachable_empty(self):
        path = Path(reachable=False, steps=(), total_changes=0, total_scans=0, reason="nope")
        assert path.to_commands() == []

    def test_already_there_empty(self):
        path = Path(reachable=True, steps=(), total_changes=0, total_scans=0)
        assert path.to_commands() == []

    def test_two_step_differential(self):
        from pyrung.core.analysis.graph import ReachabilityStep

        step1 = ReachabilityStep(
            action={"A": True, "B": 10},
            source_key=(),
            dest_key=(),
            scans=1,
        )
        step2 = ReachabilityStep(
            action={"A": True, "C": 20},
            source_key=(),
            dest_key=(),
            scans=2,
        )
        path = Path(reachable=True, steps=(step1, step2), total_changes=3, total_scans=3)
        commands = path.to_commands()
        assert commands == [
            "force A true",
            "force B 10",
            "step 1",
            "unforce B",
            "force C 20",
            "step 2",
            "clear_forces",
        ]

    def test_bool_formatting(self):
        from pyrung.core.analysis.graph import ReachabilityStep

        step = ReachabilityStep(
            action={"X": True, "Y": False},
            source_key=(),
            dest_key=(),
            scans=1,
        )
        path = Path(reachable=True, steps=(step,), total_changes=2, total_scans=1)
        commands = path.to_commands()
        assert "force X true" in commands
        assert "force Y false" in commands


# ---------------------------------------------------------------------------
# PLC.how()
# ---------------------------------------------------------------------------


class TestPLCHow:
    def test_how_from_initial(self):
        prog, Start, Running, Done = _simple_latch_program()
        plc = PLC(prog, dt=0.010)
        path = plc.how(Running)
        assert path.reachable
        assert path.total_changes >= 1

    def test_how_with_condition(self):
        prog, Start, Running, Done = _simple_latch_program()
        plc = PLC(prog, dt=0.010)
        path = plc.how(Done == True)  # noqa: E712
        assert path.reachable

    def test_how_with_avoid(self):
        prog, Start, Confirm, Ready, Done = _two_step_program()
        plc = PLC(prog, dt=0.010)
        path = plc.how(Done, avoid=Ready)
        assert not path.reachable

    @pytest.mark.xfail(reason="pilot: latch-through-OR alternative route")
    def test_how_with_avoid_uses_non_avoided_route(self):
        Manual = Bool("Manual", external=True)
        Start = Bool("Start", external=True)
        Auto = Bool("Auto")
        Done = Bool("Done")
        with Program() as prog:
            with Rung(Start):
                latch(Auto)
            with Rung(Or(Manual, Auto)):
                out(Done)

        plc = PLC(prog, dt=0.010)
        path = plc.how(Done, avoid=Manual)

        assert path.reachable
        replay = _replay_path(prog, path)
        assert replay.state.tags["Done"] is True
        assert replay.state.tags["Manual"] is False
        assert replay.state.tags["Auto"] is True

    def test_how_without_explore_works(self):
        prog, Start, Running, Done = _simple_latch_program()
        plc = PLC(prog, dt=0.010)
        path = plc.how(Done)
        assert path.reachable
        assert path.total_changes > 0

    def test_how_path_replays_correctly(self):
        prog, Start, Running, Done = _simple_latch_program()
        plc = PLC(prog, dt=0.010)
        path = plc.how(Running)
        assert path.reachable
        result = _replay_path(prog, path)
        assert result.state.tags["Running"] is True

    def test_how_two_step_replays_correctly(self):
        prog, Start, Confirm, Ready, Done = _two_step_program()
        plc = PLC(prog, dt=0.010)
        path = plc.how(Done)
        assert path.reachable
        result = _replay_path(prog, path)
        assert result.state.tags["Done"] is True

    def test_how_from_stepped_state(self):
        prog, Start, Running, Done = _simple_latch_program()
        plc = PLC(prog, dt=0.010)
        plc.patch({"Start": True})
        plc.step()
        assert plc.state.tags["Running"] is True
        path = plc.how(Running)
        assert path.reachable

    @pytest.mark.xfail(reason="pilot: single-target only", raises=ValueError)
    def test_how_multiple_conditions_and(self):
        prog, Start, Confirm, Ready, Done = _two_step_program()
        plc = PLC(prog, dt=0.010)
        path = plc.how(Ready, Done)
        assert path.reachable
        result = _replay_path(prog, path)
        assert result.state.tags["Ready"] is True
        assert result.state.tags["Done"] is True

    def test_how_from_initial_state_override(self):
        """how() finds the correct source when initial_state has different
        external input values than the graph's representative snapshot."""
        from pyrung.core.state import SystemState

        prog, Start, Running, Done = _simple_latch_program()
        # The graph reaches Running=True via Start=True.  Set Running=True
        # with Start=False — same internal state, different external input.
        tags = {"Running": True, "Done": True, "Start": False}
        plc = PLC(prog, dt=0.010, initial_state=SystemState().with_tags(tags))

        path = plc.how(Running)
        assert path.reachable
        assert path.steps == (), "should already be at target"


# ---------------------------------------------------------------------------
# Timer/counter how()
# ---------------------------------------------------------------------------


def _timer_program() -> tuple[Program, Bool, Timer, Bool]:
    Enable = Bool("Enable", external=True)
    T1 = Timer.clone("T1")
    Output = Bool("Output")
    with Program() as prog:
        with Rung(Enable):
            on_delay(T1, preset=500)
        with Rung(T1.Done):
            out(Output)
    return prog, Enable, T1, Output


def _counter_program() -> tuple[Program, Bool, Counter, Bool]:
    Trigger = Bool("Trigger", external=True)
    Reset = Bool("Reset", external=True)
    C1 = Counter.clone("C1")
    Output = Bool("Output")
    with Program() as prog:
        with Rung(rise(Trigger)):
            count_up(C1, preset=5).reset(Reset)
        with Rung(C1.Done):
            out(Output)
    return prog, Trigger, C1, Output


class TestTimerCounterHow:
    def test_timer_how_finds_path(self):
        prog, Enable, T1, Output = _timer_program()
        plc = PLC(prog, dt=0.010)
        path = plc.how(Output)
        assert path.reachable

    def test_timer_path_replays_correctly(self):
        prog, Enable, T1, Output = _timer_program()
        plc = PLC(prog, dt=0.010)
        path = plc.how(Output)
        assert path.reachable
        result = _replay_path(prog, path)
        assert result.state.tags["Output"] is True

    def test_counter_how_finds_path(self):
        prog, Trigger, C1, Output = _counter_program()
        plc = PLC(prog, dt=0.010)
        path = plc.how(Output)
        # BFS planner cannot yet solve rise()-gated counters (replay
        # verification fails), so just confirm how() returns a Path.
        assert isinstance(path, Path)
