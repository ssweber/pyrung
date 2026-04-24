"""Compiled parity tests for subroutine/call instructions."""

from __future__ import annotations

from pyrung.core import Bool, Int, Program, Rung, call, copy, out, subroutine


class TestSubroutineCall:
    def test_subroutine_executes_when_called(self, runner_factory):
        Button = Bool("Button")
        Light = Bool("Light")
        SubLight = Bool("SubLight")

        with Program() as logic:
            with Rung(Button):
                out(Light)
                call("my_sub")

            with subroutine("my_sub"):
                with Rung():
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
            with Rung(Button):
                call("my_sub")

            with subroutine("my_sub"):
                with Rung():
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
            with Rung():
                out(SubLight)

        with Program() as logic:
            with Rung(Button):
                call(init_sequence)

        runner = runner_factory(logic)
        runner.patch({"Button": True})
        runner.step()
        assert runner.current_state.tags["SubLight"] is True

    def test_subroutine_shares_state(self, runner_factory):
        Enable = Bool("Enable")
        Counter = Int("Counter")

        with Program() as logic:
            with Rung(Enable):
                copy(Counter + 1, Counter)
                call("add_more")

            with subroutine("add_more"):
                with Rung():
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
            with Rung(Enable):
                call("sub_a")
                call("sub_b")

            with subroutine("sub_a"):
                with Rung():
                    out(A)

            with subroutine("sub_b"):
                with Rung():
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
            with Rung(Enable):
                call("outer")

            with subroutine("outer"):
                with Rung():
                    call("inner")

            with subroutine("inner"):
                with Rung():
                    out(Deep)

        runner = runner_factory(logic)
        runner.patch({"Enable": True})
        runner.step()
        assert runner.current_state.tags["Deep"] is True
