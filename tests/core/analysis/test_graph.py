"""Tests for the transition graph and how() reachability path-finder."""

from __future__ import annotations

import pytest

from pyrung.core import (
    PLC,
    Bool,
    Counter,
    Int,
    Program,
    Rung,
    Timer,
    count_up,
    latch,
    on_delay,
    out,
    rise,
)
from pyrung.core.analysis.graph import Path, TransitionGraph
from pyrung.core.analysis.prove import Intractable, explore

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
# explore() tests
# ---------------------------------------------------------------------------


class TestExplore:
    def test_simple_latch_returns_graph(self):
        prog, *_ = _simple_latch_program()
        graph = explore(prog)
        assert isinstance(graph, TransitionGraph)
        assert graph.state_count >= 2
        assert graph.edge_count >= 1

    def test_state_count(self):
        prog, Start, Running, Done = _simple_latch_program()
        graph = explore(prog)
        # At minimum: (Start=F,Running=F) and (Start=T,Running=T)
        # Plus variants with Start toggled back off after latching
        assert graph.state_count >= 2

    def test_edge_count_positive(self):
        prog, *_ = _simple_latch_program()
        graph = explore(prog)
        assert graph.edge_count > 0

    def test_initial_key_exists(self):
        prog, *_ = _simple_latch_program()
        graph = explore(prog)
        tags = graph.state_tags(graph.initial_key)
        assert isinstance(tags, dict)

    def test_intractable_on_large_space(self):
        tags = [Bool(f"I{i}", external=True) for i in range(6)]
        O = Bool("Out")
        with Program() as prog:
            # Use all inputs so none are pruned
            with Rung(tags[0], tags[1], tags[2], tags[3], tags[4], tags[5]):
                latch(O)
        result = explore(prog, max_states=5)
        assert isinstance(result, Intractable)


# ---------------------------------------------------------------------------
# TransitionGraph path-finding tests
# ---------------------------------------------------------------------------


class TestShortestPath:
    def test_one_step_path(self):
        prog, Start, Running, Done = _simple_latch_program()
        graph = explore(prog)
        path = graph.shortest_path(lambda s: s.get("Running") is True)
        assert path.reachable
        assert len(path.steps) >= 1
        assert path.total_changes >= 1

    def test_two_step_path(self):
        prog, Start, Confirm, Ready, Done = _two_step_program()
        graph = explore(prog)
        path = graph.shortest_path(lambda s: s.get("Done") is True)
        assert path.reachable
        assert len(path.steps) >= 2

    def test_unreachable(self):
        prog, Input, Impossible = _unreachable_program()
        graph = explore(prog)
        path = graph.shortest_path(lambda s: s.get("Impossible") is True)
        assert not path.reachable
        assert path.reason is not None

    def test_already_at_target(self):
        prog, *_ = _simple_latch_program()
        graph = explore(prog)
        # Initial state has Running=False — target Running=False is already met
        path = graph.shortest_path(lambda s: s.get("Running") is False)
        assert path.reachable
        assert len(path.steps) == 0

    def test_avoid_blocks_path(self):
        prog, Start, Confirm, Ready, Done = _two_step_program()
        graph = explore(prog)
        # Avoid the Ready state — should block path to Done
        path = graph.shortest_path(
            lambda s: s.get("Done") is True,
            avoid=lambda s: s.get("Ready") is True,
        )
        assert not path.reachable

    def test_minimize_steps_vs_changes(self):
        prog, Start, Confirm, Ready, Done = _two_step_program()
        graph = explore(prog)
        path_steps = graph.shortest_path(
            lambda s: s.get("Done") is True,
            minimize="steps",
        )
        path_changes = graph.shortest_path(
            lambda s: s.get("Done") is True,
            minimize="changes",
        )
        assert path_steps.reachable
        assert path_changes.reachable


# ---------------------------------------------------------------------------
# Path display
# ---------------------------------------------------------------------------


class TestPathDisplay:
    def test_str_reachable(self):
        prog, Start, Running, Done = _simple_latch_program()
        graph = explore(prog)
        path = graph.shortest_path(lambda s: s.get("Running") is True)
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
# PLC.explore() and PLC.how()
# ---------------------------------------------------------------------------


class TestPLCHow:
    def test_explore_returns_graph(self):
        prog, *_ = _simple_latch_program()
        plc = PLC(prog, dt=0.010)
        graph = plc.explore()
        assert isinstance(graph, TransitionGraph)

    def test_explore_caches(self):
        prog, *_ = _simple_latch_program()
        plc = PLC(prog, dt=0.010)
        g1 = plc.explore()
        g2 = plc.explore()
        assert g1 is g2

    def test_how_from_initial(self):
        prog, Start, Running, Done = _simple_latch_program()
        plc = PLC(prog, dt=0.010)
        plc.explore()
        path = plc.how(Running)
        assert path.reachable
        assert path.total_changes >= 1

    def test_how_with_condition(self):
        prog, Start, Running, Done = _simple_latch_program()
        plc = PLC(prog, dt=0.010)
        plc.explore()
        path = plc.how(Done == True)  # noqa: E712
        assert path.reachable

    def test_how_with_avoid(self):
        prog, Start, Confirm, Ready, Done = _two_step_program()
        plc = PLC(prog, dt=0.010)
        plc.explore()
        path = plc.how(Done, avoid=Ready)
        assert not path.reachable

    def test_how_without_explore_raises(self):
        prog, *_ = _simple_latch_program()
        plc = PLC(prog, dt=0.010)
        with pytest.raises(RuntimeError, match="explore"):
            plc.how(Bool("X"))

    def test_explore_without_program_raises(self):
        plc = PLC([])
        with pytest.raises(TypeError, match="Program"):
            plc.explore()

    def test_how_path_replays_correctly(self):
        prog, Start, Running, Done = _simple_latch_program()
        plc = PLC(prog, dt=0.010)
        plc.explore()
        path = plc.how(Running)
        assert path.reachable
        result = _replay_path(prog, path)
        assert result.state.tags["Running"] is True

    def test_how_two_step_replays_correctly(self):
        prog, Start, Confirm, Ready, Done = _two_step_program()
        plc = PLC(prog, dt=0.010)
        plc.explore()
        path = plc.how(Done)
        assert path.reachable
        result = _replay_path(prog, path)
        assert result.state.tags["Done"] is True

    def test_how_from_stepped_state(self):
        prog, Start, Running, Done = _simple_latch_program()
        plc = PLC(prog, dt=0.010)
        plc.explore()
        plc.patch({"Start": True})
        plc.step()
        assert plc.state.tags["Running"] is True
        path = plc.how(Running)
        assert path.reachable

    def test_how_multiple_conditions_and(self):
        prog, Start, Confirm, Ready, Done = _two_step_program()
        plc = PLC(prog, dt=0.010)
        plc.explore()
        path = plc.how(Ready, Done)
        assert path.reachable
        result = _replay_path(prog, path)
        assert result.state.tags["Ready"] is True
        assert result.state.tags["Done"] is True


# ---------------------------------------------------------------------------
# Timer/counter absorption in explore()
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


class TestExploreTimerAbsorption:
    def test_timer_program_small_graph(self):
        prog, Enable, T1, Output = _timer_program()
        graph = explore(prog)
        assert isinstance(graph, TransitionGraph)
        assert graph.state_count < 30

    def test_timer_how_finds_path(self):
        prog, Enable, T1, Output = _timer_program()
        plc = PLC(prog, dt=0.010)
        plc.explore()
        path = plc.how(Output)
        assert path.reachable

    def test_timer_path_replays_correctly(self):
        prog, Enable, T1, Output = _timer_program()
        plc = PLC(prog, dt=0.010)
        plc.explore()
        path = plc.how(Output)
        assert path.reachable
        result = _replay_path(prog, path)
        assert result.state.tags["Output"] is True

    def test_counter_program_small_graph(self):
        prog, Trigger, C1, Output = _counter_program()
        graph = explore(prog)
        assert isinstance(graph, TransitionGraph)
        assert graph.state_count < 30

    def test_counter_done_reachable(self):
        prog, Trigger, C1, Output = _counter_program()
        graph = explore(prog)
        has_done = any(
            graph.state_tags(key).get("C1_Done") is True
            for key in graph._state_tags
        )
        assert has_done

    def test_counter_how_finds_path(self):
        prog, Trigger, C1, Output = _counter_program()
        plc = PLC(prog, dt=0.010)
        plc.explore()
        path = plc.how(Output)
        assert path.reachable
        assert path.total_scans > 0

    def test_counter_how_destination_has_output_true(self):
        prog, Trigger, C1, Output = _counter_program()
        plc = PLC(prog, dt=0.010)
        graph = plc.explore()
        path = plc.how(Output)
        assert path.reachable
        dest_tags = graph.state_tags(path.steps[-1].dest_key)
        assert dest_tags["Output"] is True

    def test_graph_preserves_acc_in_state_tags(self):
        """State snapshots should include concrete Acc values even though
        they're absorbed from the state key."""
        prog, Enable, T1, Output = _timer_program()
        graph = explore(prog)
        initial_tags = graph.state_tags(graph.initial_key)
        assert "T1_Acc" in initial_tags
