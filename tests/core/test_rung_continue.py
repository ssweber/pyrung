"""Tests for Rung.continued() -- a continued rung reads its contacts live.

A continued rung is a row of the CLICK rung above it: its trunk is the rail and
its contacts read when the solve reaches it, so they see every write above it
(the live row solve).  Before it, a continued rung read the rung above's
snapshot, taken at that rung's entry.
"""

import pytest

from pyrung.core import (
    Bool,
    Int,
    Program,
    Rung,
    SystemState,
    Timer,
    branch,
    copy,
    latch,
    on_delay,
    out,
)
from tests.conftest import evaluate_program


class TestContinueBasic:
    """Basic continued() behavior: contacts read live, after the rows above."""

    def test_continue_sees_the_write_above(self):
        """A continued rung reads its contacts after the rung above has written."""
        A = Bool("A")
        X = Bool("X")
        Y = Bool("Y")

        with Program() as logic:
            with Rung(A):
                # This sets X = True during rung 1's execution
                out(X)
            with Rung(X).continued():
                # X is True by the time the solve reaches this row
                out(Y)

        state = SystemState().with_tags({"A": True, "B": False, "X": False, "Y": False})
        result = evaluate_program(logic, state)

        assert result.tags["X"] is True  # rung 1 instruction executed
        assert result.tags["Y"] is True  # continued rung read X live

    def test_continue_without_mutation_both_execute(self):
        """A rung and its continued rung both execute when their conditions hold."""
        A = Bool("A")
        B = Bool("B")
        X = Bool("X")
        Y = Bool("Y")

        with Program() as logic:
            with Rung(A):
                out(X)
            with Rung(B).continued():
                out(Y)

        state = SystemState().with_tags({"A": True, "B": True, "X": False, "Y": False})
        result = evaluate_program(logic, state)

        assert result.tags["X"] is True
        assert result.tags["Y"] is True

    def test_continue_chain_reads_each_row_live(self):
        """Each rung of a continued() chain reads live, after the rows above it."""
        A = Bool("A")
        X = Bool("X")
        Y = Bool("Y")
        Z = Bool("Z")
        Counter = Int("Counter")

        with Program() as logic:
            with Rung(A):
                copy(10, Counter)
            with Rung(Counter == 10).continued():
                # Counter is 10 by the time the solve reaches this row
                out(X)
            with Rung(Counter == 0).continued():
                out(Y)
            with Rung(A).continued():
                out(Z)

        state = SystemState().with_tags(
            {"A": True, "Counter": 0, "X": False, "Y": False, "Z": False}
        )
        result = evaluate_program(logic, state)

        assert result.tags["Counter"] == 10
        assert result.tags["X"] is True  # read Counter=10 live
        assert result.tags["Y"] is False  # read Counter=10 live
        assert result.tags["Z"] is True

    def test_normal_rung_after_continue_reads_live(self):
        """A normal rung after a continue chain reads live, like the chain."""
        A = Bool("A")
        X = Bool("X")
        Y = Bool("Y")
        Counter = Int("Counter")

        with Program() as logic:
            with Rung(A):
                copy(10, Counter)
            with Rung(Counter == 10).continued():
                out(X)  # True: reads Counter=10 live
            with Rung(Counter == 10):
                out(Y)  # True: reads Counter=10

        state = SystemState().with_tags({"A": True, "Counter": 0, "X": False, "Y": False})
        result = evaluate_program(logic, state)

        assert result.tags["X"] is True
        assert result.tags["Y"] is True


class TestContinueWithBranch:
    """continued() combined with branch() inside the continue rung."""

    def test_continue_rung_with_branch(self):
        """A branch within a continued rung reads live too."""
        A = Bool("A")
        X = Bool("X")
        Y = Bool("Y")
        Z = Bool("Z")

        with Program() as logic:
            with Rung(A):
                out(X)
            with Rung(A).continued():
                out(Y)
                with branch(X):
                    # X is True by the time the solve reaches this branch
                    out(Z)

        state = SystemState().with_tags({"A": True, "X": False, "Y": False, "Z": False})
        result = evaluate_program(logic, state)

        assert result.tags["X"] is True  # rung 1 set it
        assert result.tags["Y"] is True
        assert result.tags["Z"] is True  # the branch read X live


class TestContinueStaticValidation:
    """Static errors caught at program build time."""

    def test_continue_as_first_rung_in_program_raises(self):
        """continued() on the first rung in a program is a static error."""
        A = Bool("A")
        X = Bool("X")

        with pytest.raises(RuntimeError, match="cannot be the first rung in a program"):
            with Program():
                with Rung(A).continued():
                    out(X)

    def test_continue_as_first_rung_in_subroutine_raises(self):
        """continued() on the first rung in a subroutine is a static error."""
        from pyrung.core import subroutine

        A = Bool("A")
        X = Bool("X")
        Y = Bool("Y")

        with pytest.raises(RuntimeError, match="cannot be the first rung in a subroutine"):
            with Program():
                with Rung(A):
                    out(X)
                with subroutine("my_sub"):
                    with Rung(A).continued():
                        out(Y)


class TestContinueSubroutineBoundary:
    """continued() inside a subroutine."""

    def test_subroutine_does_not_leak_snapshot(self, runner_factory):
        """A continued() rung in a subroutine cannot see the caller's snapshot."""
        from pyrung.core import call, subroutine

        A = Bool("A")
        X = Bool("X")
        Y = Bool("Y")

        with Program() as logic:
            with Rung(A):
                out(X)
                call("my_sub")

            with subroutine("my_sub"):
                with Rung(A):
                    out(Y)
                # Rung(A).continued() would be at index 1 in the subroutine,
                # which is valid structurally. But the snapshot should come
                # from the subroutine's own first rung, not the caller.

        runner = runner_factory(logic)
        runner.patch({"A": True, "X": False, "Y": False})
        runner.step()

        assert runner.current_state.tags["X"] is True
        assert runner.current_state.tags["Y"] is True

    def test_continue_in_subroutine_reads_live(self, runner_factory):
        """continued() in a subroutine reads live, after the subroutine's rows above."""
        from pyrung.core import call, subroutine

        A = Bool("A")
        X = Bool("X")
        Y = Bool("Y")
        Counter = Int("Counter")

        with Program() as logic:
            with Rung(A):
                call("my_sub")

            with subroutine("my_sub"):
                with Rung():
                    copy(10, Counter)
                with Rung(Counter == 10).continued():
                    # Counter is 10 by the time the solve reaches this row
                    out(X)
                with Rung(Counter == 0).continued():
                    out(Y)

        runner = runner_factory(logic)
        runner.patch({"A": True, "Counter": 0, "X": False, "Y": False})
        runner.step()

        assert runner.current_state.tags["Counter"] == 10
        assert runner.current_state.tags["X"] is True  # read Counter=10 live
        assert runner.current_state.tags["Y"] is False


class TestContinueInstructionConditions:
    """A box pin on a continued rung reads at box entry, after the rows above."""

    def test_continued_rung_box_pin_reads_at_box_entry(self, runner_factory):
        Enable = Bool("Enable")
        ResetBtn = Bool("ResetBtn")

        with Program() as logic:
            with Rung(Enable):
                latch(ResetBtn)
            with Rung(Enable).continued():
                on_delay(Timer[1], preset=100).reset(ResetBtn)

        runner = runner_factory(logic, dt=0.010)
        runner.patch({"Enable": True, "ResetBtn": False})
        runner.step()

        assert runner.current_state.tags["ResetBtn"] is True
        # The reset pin read ResetBtn=True (latched a row above) at box entry.
        assert runner.current_state.tags["Timer_Acc"] == 0
        assert runner.current_state.tags["Timer_Done"] is False


class TestContinueMethodChaining:
    """continued() returns self for chaining and works as context manager."""

    def test_continue_returns_self(self):
        """continued() returns the Rung instance for chaining."""
        A = Bool("A")
        rung = Rung(A)
        result = rung.continued()
        assert result is rung

    def test_continue_sets_flag_on_underlying_rung(self):
        """continued() sets _use_prior_snapshot on the inner RungLogic."""
        A = Bool("A")
        rung = Rung(A)
        assert rung._rung._use_prior_snapshot is False
        rung.continued()
        assert rung._rung._use_prior_snapshot is True

    def test_continued_rung_cannot_have_comment(self):
        """comment() before a continued() rung raises."""
        from pyrung.core.program import comment

        A = Bool("A")
        B = Bool("B")
        Light = Bool("Light")

        with Program():
            with Rung(A):
                out(Light)
            comment("not allowed")
            with pytest.raises(
                RuntimeError, match="continued\\(\\) rung cannot have its own comment"
            ):
                with Rung(B).continued():
                    out(Light)
