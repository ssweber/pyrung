"""Compound how() goals: And-of-Compare conjunctions of comparisons.

The target decomposition has always split And-of-eq into (tag, value)
goals; these tests pin the must-stay semantics on top of it: a committed
conjunct broken by a later goal's walk is detected on the work fork
(goal-regressed), and plan_walk's reorder loop retries with the
clobbering goal first.  Genuinely conflicting conjunctions terminate
honestly with a diagnosis naming the failing conjunct.
"""

from __future__ import annotations

from pyrung import Bool, Int, Program, Rung, copy
from pyrung.core.runner import PLC


def _replay(prog, path):
    replay = PLC(prog, dt=0.010)
    for step in path.steps:
        replay.patch(step.action)
        for _ in range(step.scans):
            replay.step()
    return replay


def _mode_resets_step():
    """Mode change resets the step sequencer — goal order matters."""
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
            copy(0, Step)  # mode change resets the sequencer
    return prog, Mode, Step


class TestCompoundCompareGoals:
    def test_independent_compare_conjuncts(self):
        """Two non-interfering Int goals solve as one compound walk."""
        GoA = Bool("GoA", external=True)
        GoB = Bool("GoB", external=True)
        X = Int("X")
        Y = Int("Y")
        with Program() as prog:
            with Rung(GoA, X == 0):
                copy(3, X)
            with Rung(GoB, Y == 0):
                copy(5, Y)
        plc = PLC(prog, dt=0.010)
        path = plc.how(X == 3, Y == 5)
        assert path.reachable
        replay = _replay(prog, path)
        assert replay.state.tags["X"] == 3
        assert replay.state.tags["Y"] == 5

    def test_clobbering_order_reorders_and_solves(self):
        """(Step, Mode): Mode's walk resets Step.  The must-stay check
        catches the regression on the work fork and the reorder retry
        solves mode-first."""
        prog, Mode, Step = _mode_resets_step()
        plc = PLC(prog, dt=0.010)
        path = plc.how(Step == 2, Mode == 2)
        assert path.reachable
        replay = _replay(prog, path)
        assert replay.state.tags["Step"] == 2
        assert replay.state.tags["Mode"] == 2

    def test_safe_order_solves_directly(self):
        """(Mode, Step) needs no reorder — mode-first preserves both."""
        prog, Mode, Step = _mode_resets_step()
        plc = PLC(prog, dt=0.010)
        path = plc.how(Mode == 2, Step == 2)
        assert path.reachable
        replay = _replay(prog, path)
        assert replay.state.tags["Step"] == 2
        assert replay.state.tags["Mode"] == 2

    def test_pinned_conjunct_unsolvable_names_conjunct(self):
        """Mode==2 pins Step at 0: the conjunction fails after the reorder
        retry, and the diagnosis names the failing conjunct plus the
        must-stay reorder note."""
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
            with Rung(Mode == 2):
                copy(0, Step)  # production mode pins the sequencer
        plc = PLC(prog, dt=0.010)
        path = plc.how(Step == 2, Mode == 2)
        assert not path.reachable
        assert path.diagnosis is not None
        assert path.diagnosis.failing_goal == ("Step", 2)
        assert any("must-stay" in note for note in path.diagnosis.notes)

    def test_mutual_clobber_terminates_honestly(self):
        """A==1 zeros B and B==1 zeros A: no order works; the reorder loop
        terminates (tried-set / failed goal) with an honest diagnosis."""
        ABtn = Bool("ABtn", external=True)
        BBtn = Bool("BBtn", external=True)
        A = Int("A")
        B = Int("B")
        with Program() as prog:
            with Rung(ABtn):
                copy(1, A)
                copy(0, B)
            with Rung(BBtn):
                copy(1, B)
                copy(0, A)
        plc = PLC(prog, dt=0.010)
        path = plc.how(A == 1, B == 1)
        assert not path.reachable
        assert path.diagnosis is not None
        assert path.diagnosis.failing_goal is not None
