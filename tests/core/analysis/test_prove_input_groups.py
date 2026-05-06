"""Regression tests for joint/exclusive input enumeration in prove BFS."""

from __future__ import annotations

from pyrung.core import PLC, Bool, Program, Rung, fall, latch, rise
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
            joint_inputs=(("A", "B"),),
        )
        assert not isinstance(states, Intractable)
        assert frozenset({("Target", True)}) in states

        result = prove(logic, ~target, joint_inputs=(("A", "B"),))
        assert isinstance(result, Counterexample)

    def test_multiple_joint_inputs_compose_with_each_other(self):
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
            joint_inputs=(("A", "B"), ("C", "D")),
        )
        assert not isinstance(states, Intractable)
        assert frozenset({("Target", True)}) in states

        result = prove(logic, ~target, joint_inputs=(("A", "B"), ("C", "D")))
        assert isinstance(result, Counterexample)


class TestAutoJointInputs:
    def test_auto_joint_dual_rise(self):
        a = Bool("A", external=True)
        b = Bool("B", external=True)
        target = Bool("Target")

        with Program(strict=False) as logic:
            with Rung(rise(a), rise(b)):
                latch(target)

        plc = PLC(logic, dt=0.010)
        plc.patch({"A": True, "B": True})
        plc.step()
        assert plc.current_state.tags["Target"] is True

        states = reachable_states(logic, project=["Target"])
        assert not isinstance(states, Intractable)
        assert frozenset({("Target", True)}) in states

        result = prove(logic, ~target)
        assert isinstance(result, Counterexample)

    def test_auto_joint_rise_fall_pair(self):
        a = Bool("A", external=True)
        b = Bool("B", external=True)
        target = Bool("Target")

        with Program(strict=False) as logic:
            with Rung(rise(a), fall(b)):
                latch(target)

        plc = PLC(logic, dt=0.010)
        plc.patch({"B": True})
        plc.step()
        plc.patch({"A": True, "B": False})
        plc.step()
        assert plc.current_state.tags["Target"] is True

        states = reachable_states(logic, project=["Target"])
        assert not isinstance(states, Intractable)
        assert frozenset({("Target", True)}) in states

        result = prove(logic, ~target)
        assert isinstance(result, Counterexample)


class TestExclusiveInputs:
    def test_exclusive_prunes_multi_hot(self):
        """exclusive_inputs prevents both A and B being True simultaneously."""
        a = Bool("A", external=True)
        b = Bool("B", external=True)
        target = Bool("Target")

        with Program(strict=False) as logic:
            with Rung(a, b):
                latch(target)

        states = reachable_states(logic, project=["Target"])
        assert not isinstance(states, Intractable)
        assert frozenset({("Target", True)}) in states

        states_excl = reachable_states(logic, project=["Target"], exclusive_inputs=(("A", "B"),))
        assert not isinstance(states_excl, Intractable)
        assert frozenset({("Target", True)}) not in states_excl

    def test_exclusive_composes_with_free_input(self):
        """exclusive_inputs compose with other input dimensions."""
        a = Bool("A", external=True)
        b = Bool("B", external=True)
        free = Bool("Free", external=True)
        target = Bool("Target")

        with Program(strict=False) as logic:
            with Rung(a, free):
                latch(target)

        states = reachable_states(
            logic,
            project=["Target"],
            exclusive_inputs=(("A", "B"),),
        )
        assert not isinstance(states, Intractable)
        assert frozenset({("Target", True)}) in states

    def test_exclusive_allows_one_hot(self):
        """exclusive_inputs still allows exactly one member True."""
        a = Bool("A", external=True)
        b = Bool("B", external=True)
        target = Bool("Target")

        with Program(strict=False) as logic:
            with Rung(a):
                latch(target)

        states = reachable_states(logic, project=["Target"], exclusive_inputs=(("A", "B"),))
        assert not isinstance(states, Intractable)
        assert frozenset({("Target", True)}) in states
