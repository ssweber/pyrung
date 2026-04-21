"""Compiled parity tests for calc instruction."""

from __future__ import annotations

from pyrung.core import Bool, Dint, Int, Program, Real, Rung, calc


class TestCalcInstruction:
    def test_basic_arithmetic(self, runner_factory):
        Enable = Bool("Enable")
        A = Int("A")
        B = Int("B")
        Result = Int("Result")

        with Program() as logic:
            with Rung(Enable):
                calc(A + B, Result)

        runner = runner_factory(logic)
        runner.patch({"Enable": True, "A": 100, "B": 200})
        runner.step()
        assert runner.current_state.tags["Result"] == 300

    def test_wrapping_semantics(self, runner_factory):
        """calc wraps on overflow (modular), unlike copy which clamps."""
        Enable = Bool("Enable")
        A = Int("A")
        Result = Int("Result")

        with Program() as logic:
            with Rung(Enable):
                calc(A + 1, Result)

        runner = runner_factory(logic)
        runner.patch({"Enable": True, "A": 32767})
        runner.step()
        assert runner.current_state.tags["Result"] == -32768

    def test_division_by_zero_yields_zero(self, runner_factory):
        Enable = Bool("Enable")
        A = Int("A")
        B = Int("B")
        Result = Int("Result")

        with Program() as logic:
            with Rung(Enable):
                calc(A / B, Result)

        runner = runner_factory(logic)
        runner.patch({"Enable": True, "A": 100, "B": 0})
        runner.step()
        assert runner.current_state.tags["Result"] == 0
        assert runner.current_state.tags["fault.division_error"] is True

    def test_real_result(self, runner_factory):
        Enable = Bool("Enable")
        A = Real("A")
        B = Real("B")
        Result = Real("Result")

        with Program() as logic:
            with Rung(Enable):
                calc(A * B, Result)

        runner = runner_factory(logic)
        runner.patch({"Enable": True, "A": 2.5, "B": 4.0})
        runner.step()
        assert runner.current_state.tags["Result"] == 10.0

    def test_complex_expression(self, runner_factory):
        Enable = Bool("Enable")
        A = Int("A")
        B = Int("B")
        C = Int("C")
        Result = Dint("Result")

        with Program() as logic:
            with Rung(Enable):
                calc((A + B) * C, Result)

        runner = runner_factory(logic)
        runner.patch({"Enable": True, "A": 10, "B": 20, "C": 3})
        runner.step()
        assert runner.current_state.tags["Result"] == 90

    def test_not_executed_when_rung_false(self, runner_factory):
        Enable = Bool("Enable")
        A = Int("A")
        Result = Int("Result")

        with Program() as logic:
            with Rung(Enable):
                calc(A + 1, Result)

        runner = runner_factory(logic)
        runner.patch({"Enable": False, "A": 10, "Result": 0})
        runner.step()
        assert runner.current_state.tags["Result"] == 0
