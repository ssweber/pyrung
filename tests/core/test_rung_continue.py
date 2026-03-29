"""Tests for Rung.continued() — condition snapshot reuse across rungs."""

import pytest

from pyrung.core import Bool, Int, PLCRunner, Program, Rung, SystemState, branch, copy, out
from tests.conftest import evaluate_program


class TestContinueBasic:
    """Basic continued() behavior: snapshot reuse across rungs."""

    def test_continue_sees_pre_instruction_state(self):
        """Second rung with continued() evaluates conditions against snapshot
        taken before the first rung's instructions executed."""
        A = Bool("A")
        X = Bool("X")
        Y = Bool("Y")

        with Program() as logic:
            with Rung(A):
                # This sets X = True during rung 1's execution
                out(X)
            with Rung(X).continued():
                # X was False at snapshot time (before rung 1 ran),
                # so this rung's condition should be False
                out(Y)

        state = SystemState().with_tags({"A": True, "B": False, "X": False, "Y": False})
        result = evaluate_program(logic, state)

        assert result.tags["X"] is True  # rung 1 instruction executed
        assert result.tags["Y"] is False  # continue rung saw X=False in snapshot

    def test_continue_without_mutation_both_execute(self):
        """Two rungs sharing a snapshot both execute when conditions hold."""
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

    def test_continue_chain_all_share_original_snapshot(self):
        """Multiple consecutive continued() rungs all share the same snapshot."""
        A = Bool("A")
        X = Bool("X")
        Y = Bool("Y")
        Z = Bool("Z")
        Counter = Int("Counter")

        with Program() as logic:
            with Rung(A):
                copy(10, Counter)
            with Rung(Counter == 10).continued():
                # Counter was 0 at snapshot time, not 10
                out(X)
            with Rung(Counter == 0).continued():
                # Counter was 0 at snapshot time
                out(Y)
            with Rung(A).continued():
                # A was True at snapshot time
                out(Z)

        state = SystemState().with_tags(
            {"A": True, "Counter": 0, "X": False, "Y": False, "Z": False}
        )
        result = evaluate_program(logic, state)

        assert result.tags["Counter"] == 10
        assert result.tags["X"] is False  # Counter was 0, not 10
        assert result.tags["Y"] is True  # Counter was 0
        assert result.tags["Z"] is True  # A was True

    def test_normal_rung_after_continue_takes_fresh_snapshot(self):
        """A normal (non-continue) rung after a continue chain takes a fresh snapshot."""
        A = Bool("A")
        X = Bool("X")
        Y = Bool("Y")
        Counter = Int("Counter")

        with Program() as logic:
            with Rung(A):
                copy(10, Counter)
            with Rung(Counter == 10).continued():
                out(X)  # False: Counter was 0 at snapshot
            with Rung(Counter == 10):
                out(Y)  # True: fresh snapshot sees Counter=10

        state = SystemState().with_tags({"A": True, "Counter": 0, "X": False, "Y": False})
        result = evaluate_program(logic, state)

        assert result.tags["X"] is False
        assert result.tags["Y"] is True


class TestContinueWithBranch:
    """continued() combined with branch() inside the continue rung."""

    def test_continue_rung_with_branch(self):
        """Branches within a continue rung share the same snapshot."""
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
                    # X was False in the snapshot, so branch is unpowered
                    out(Z)

        state = SystemState().with_tags({"A": True, "X": False, "Y": False, "Z": False})
        result = evaluate_program(logic, state)

        assert result.tags["X"] is True  # rung 1 set it
        assert result.tags["Y"] is True  # A was True in snapshot
        assert result.tags["Z"] is False  # X was False in snapshot


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


class TestContinueRuntimeValidation:
    """Runtime errors for snapshot misuse (belt-and-suspenders with static checks)."""

    def test_continue_with_no_prior_snapshot_raises_at_runtime(self):
        """continued() rung evaluated without any prior snapshot raises at runtime."""
        from pyrung.core.context import ScanContext
        from pyrung.core.rung import Rung as RungLogic

        A = Bool("A")

        rung = RungLogic(A)
        rung._use_prior_snapshot = True

        state = SystemState().with_tags({"A": True})
        ctx = ScanContext(state)

        with pytest.raises(RuntimeError, match="no prior condition snapshot exists"):
            rung.evaluate(ctx)


class TestContinueSubroutineBoundary:
    """Subroutine boundaries fence the condition snapshot."""

    def test_subroutine_does_not_leak_snapshot(self):
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

        runner = PLCRunner(logic)
        runner.patch({"A": True, "X": False, "Y": False})
        runner.step()

        assert runner.current_state.tags["X"] is True
        assert runner.current_state.tags["Y"] is True

    def test_continue_in_subroutine_uses_subroutine_snapshot(self):
        """continued() in a subroutine reuses the subroutine's own prior rung snapshot."""
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
                    # Counter was 0 at the subroutine's first rung snapshot
                    out(X)
                with Rung(Counter == 0).continued():
                    out(Y)

        runner = PLCRunner(logic)
        runner.patch({"A": True, "Counter": 0, "X": False, "Y": False})
        runner.step()

        assert runner.current_state.tags["Counter"] == 10
        assert runner.current_state.tags["X"] is False  # saw Counter=0
        assert runner.current_state.tags["Y"] is True  # saw Counter=0


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

    def test_continued_rung_cannot_set_comment(self):
        """Setting a comment on a continued() rung raises."""
        A = Bool("A")
        rung = Rung(A).continued()
        with pytest.raises(RuntimeError, match="continued\\(\\) rung cannot have its own comment"):
            rung.comment = "not allowed"

    def test_continued_rung_allows_none_comment(self):
        """Setting comment to None on a continued() rung is fine (no-op)."""
        A = Bool("A")
        rung = Rung(A).continued()
        rung.comment = None  # should not raise
