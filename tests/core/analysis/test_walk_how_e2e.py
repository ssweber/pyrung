"""End-to-end how() behavior on small sequential programs.

Moved (bodies unchanged) from the legacy waypoint-planner test file when
``prove/waypoints.py`` was deleted — these tests assert ``how()`` behavior
and replay validity, which the corridor walker now provides.
"""

from __future__ import annotations

import pytest

from pyrung import Bool, Int, Program, Rung, copy, latch
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


class TestHowEndToEnd:
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
    def test_how_with_callable_predicate(self):
        """Opaque callable predicates can't be decomposed."""
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

    def test_how_with_independent_and_dependent_latches(self):
        """C depends on A (through cone), B is independent."""
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
        """Int tags with copy-based step sequencing."""
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
