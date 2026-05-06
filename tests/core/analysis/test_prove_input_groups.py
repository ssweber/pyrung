"""Regression tests for grouped input enumeration in prove BFS."""

from __future__ import annotations

from pyrung.core import Bool, PLC, Program, Rung, latch, rise
from pyrung.core.analysis.prove import Counterexample, Intractable, prove, reachable_states


class TestInputGroupComposition:
    def test_input_group_composes_with_free_input(self):
        a = Bool("A", external=True)
        b = Bool("B", external=True)
        free = Bool("Free", external=True)
        target = Bool("Target")

        with Program(strict=False) as logic:
            with Rung(rise(a), rise(b), free):
                latch(target)

        plc = PLC(logic, dt=0.010)
        plc.patch({"A": True, "B": True, "Free": True})
        plc.step()
        assert plc.current_state.tags["Target"] is True

        states = reachable_states(
            logic,
            project=["Target"],
            input_groups=(("A", "B"),),
        )
        assert not isinstance(states, Intractable)
        assert frozenset({("Target", True)}) in states

        result = prove(logic, ~target, input_groups=(("A", "B"),))
        assert isinstance(result, Counterexample)

    def test_multiple_input_groups_compose_with_each_other(self):
        a = Bool("A", external=True)
        b = Bool("B", external=True)
        c = Bool("C", external=True)
        d = Bool("D", external=True)
        target = Bool("Target")

        with Program(strict=False) as logic:
            with Rung(rise(a), rise(b), rise(c), rise(d)):
                latch(target)

        plc = PLC(logic, dt=0.010)
        plc.patch({"A": True, "B": True, "C": True, "D": True})
        plc.step()
        assert plc.current_state.tags["Target"] is True

        states = reachable_states(
            logic,
            project=["Target"],
            input_groups=(("A", "B"), ("C", "D")),
        )
        assert not isinstance(states, Intractable)
        assert frozenset({("Target", True)}) in states

        result = prove(logic, ~target, input_groups=(("A", "B"), ("C", "D")))
        assert isinstance(result, Counterexample)
