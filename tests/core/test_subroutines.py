"""Compiled parity tests for subroutine/call instructions."""

from __future__ import annotations

import pytest

from pyrung.core import (
    Bool,
    Int,
    Program,
    call,
    copy,
    out,
    return_early,
    rung,
    subroutine,
)


class TestSubroutineCall:
    def test_subroutine_executes_when_called(self, runner_factory):
        Button = Bool("Button")
        Light = Bool("Light")
        SubLight = Bool("SubLight")

        with Program() as logic:
            with rung(Button):
                out(Light)
                call("my_sub")

            with subroutine("my_sub"):
                with rung():
                    out(SubLight)

        runner = runner_factory(logic)
        runner.patch({"Button": True})
        runner.step()
        assert runner.current_state.tags["Light"] is True
        assert runner.current_state.tags["SubLight"] is True

    def test_subroutine_not_called_when_rung_false(self, runner_factory):
        Button = Bool("Button")
        SubLight = Bool("SubLight")

        with Program() as logic:
            with rung(Button):
                call("my_sub")

            with subroutine("my_sub"):
                with rung():
                    out(SubLight)

        runner = runner_factory(logic)
        runner.patch({"Button": False})
        runner.step()
        assert runner.current_state.tags["SubLight"] is False

    def test_decorator_form(self, runner_factory):
        Button = Bool("Button")
        SubLight = Bool("SubLight")

        @subroutine("init")
        def init_sequence():
            with rung():
                out(SubLight)

        with Program() as logic:
            with rung(Button):
                call(init_sequence)

        runner = runner_factory(logic)
        runner.patch({"Button": True})
        runner.step()
        assert runner.current_state.tags["SubLight"] is True

    def test_subroutine_shares_state(self, runner_factory):
        Enable = Bool("Enable")
        Counter = Int("Counter")

        with Program() as logic:
            with rung(Enable):
                copy(Counter + 1, Counter)
                call("add_more")

            with subroutine("add_more"):
                with rung():
                    copy(Counter + 10, Counter)

        runner = runner_factory(logic)
        runner.patch({"Enable": True, "Counter": 0})
        runner.step()
        assert runner.current_state.tags["Counter"] == 11

    def test_multiple_subroutines(self, runner_factory):
        Enable = Bool("Enable")
        A = Bool("A")
        B = Bool("B")

        with Program() as logic:
            with rung(Enable):
                call("sub_a")
                call("sub_b")

            with subroutine("sub_a"):
                with rung():
                    out(A)

            with subroutine("sub_b"):
                with rung():
                    out(B)

        runner = runner_factory(logic)
        runner.patch({"Enable": True})
        runner.step()
        assert runner.current_state.tags["A"] is True
        assert runner.current_state.tags["B"] is True

    def test_nested_call(self, runner_factory):
        Enable = Bool("Enable")
        Deep = Bool("Deep")

        with Program() as logic:
            with rung(Enable):
                call("outer")

            with subroutine("outer"):
                with rung():
                    call("inner")

            with subroutine("inner"):
                with rung():
                    out(Deep)

        runner = runner_factory(logic)
        runner.patch({"Enable": True})
        runner.step()
        assert runner.current_state.tags["Deep"] is True


class TestReturnEarlyPlacement:
    def test_return_early_in_the_main_program_says_what_to_do(self):
        Button = Bool("Button")

        with pytest.raises(RuntimeError, match="ends a subroutine.*main program ends by itself"):
            with Program():
                with rung(Button):
                    return_early()
