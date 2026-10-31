"""Compiled parity tests for calc instruction."""

from __future__ import annotations

import pytest

from pyrung.core import PLC, Bool, CompiledPLC, Dint, Int, Program, Real, Rung, calc


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

    def test_int_destination_cuts_constants_to_16_bits(self, runner_factory):
        """A Math into an Int keeps each constant in 16 bits, as a CLICK does:
        -2147483648 / 65536 divides by 0; the same 65536 read from a Dint does
        not, and a Dint destination keeps the constant whole."""
        Enable = Bool("Enable")
        Num = Dint("Num")
        Den = Dint("Den")
        ByConstant = Int("ByConstant")
        ByTag = Int("ByTag")
        IntoDint = Dint("IntoDint")
        Low = Int("Low")

        with Program() as logic:
            with Rung(Enable):
                calc(Num / 65536, ByConstant)
                calc(Num / Den, ByTag)
                calc(Num / 65536, IntoDint)
                calc(Den * 0 + 65543, Low)

        runner = runner_factory(logic)
        runner.patch({"Enable": True, "Num": -2147483648, "Den": 65536, "ByConstant": 99})
        runner.step()
        tags = runner.current_state.tags
        assert tags["ByConstant"] == 0
        assert tags["fault.division_error"] is True
        assert tags["ByTag"] == -32768
        assert tags["IntoDint"] == -32768
        assert tags["Low"] == 7

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

    @pytest.mark.parametrize("runner_cls", [PLC, CompiledPLC])
    def test_expression_overflow_sets_out_of_range_and_stores_zero(self, runner_cls):
        Enable = Bool("Enable")
        Source = Real("Source")
        Result = Real("Result")

        with Program() as logic:
            with Rung(Enable):
                # 3.0E+38 ** 10 overflows a float: OverflowError, not a value.
                calc(Source**10, Result)

        runner = runner_cls(logic)
        runner.patch({"Enable": True, "Source": 3.0e38, "Result": 123.0})
        runner.step()

        assert runner.current_state.tags["Result"] == 0.0
        assert runner.current_state.tags["fault.out_of_range"] is True

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


class TestCutIntConstants:
    def test_cuts_each_integer_constant_to_its_low_16_bits_signed(self):
        from pyrung.core.expression import LiteralExpr, sqrt
        from pyrung.core.instruction.calc import cut_int_constants

        A = Int("A")
        assert cut_int_constants(65536) == 0
        assert cut_int_constants(-65536) == 0
        assert cut_int_constants(65543) == 7
        assert cut_int_constants(40000) == -25536
        assert cut_int_constants(32767) == 32767
        assert cut_int_constants(-32768) == -32768
        assert cut_int_constants(1.5) == 1.5
        assert cut_int_constants(True) is True
        cut = cut_int_constants(sqrt(A + 65536) * 2)
        assert repr(cut) == "(sqrt((TagExpr(A) + LiteralExpr(0))) * LiteralExpr(2))"
        assert isinstance(cut.right, LiteralExpr)

    def test_returns_the_expression_itself_when_no_constant_changes(self):
        from pyrung.core.instruction.calc import CalcInstruction, cut_int_constants

        A = Int("A")
        expr = (A + 32767) * -32768
        assert cut_int_constants(expr) is expr
        instr = CalcInstruction(expr, Int("Dest"))
        assert instr.expression is expr
        assert instr.source_expression is expr

    def test_only_an_int_destination_cuts(self):
        from pyrung.core.instruction.calc import CalcInstruction

        A = Dint("A")
        expr = A / 65536
        into_int = CalcInstruction(expr, Int("I"))
        assert into_int.source_expression is expr
        assert repr(into_int.expression) == "(TagExpr(A) / LiteralExpr(0))"
        for dest in (Dint("D"), Real("R")):
            assert CalcInstruction(expr, dest).expression is expr


class TestCalcRangeAggregates:
    """max/min/median/mean through calc's destination conversion, both backends."""

    def _run(self, runner_factory, expression_for, dest, values):
        from pyrung.core import Block, TagType

        Enable = Bool("Enable")
        DS = Block("DS", TagType.INT, 1, 6)
        with Program() as logic:
            with Rung(Enable):
                calc(expression_for(DS), dest)
        runner = runner_factory(logic)
        runner.patch({"Enable": True, **{f"DS{i}": v for i, v in enumerate(values, start=1)}})
        runner.step()
        return runner.current_state.tags[dest.name]

    @pytest.mark.parametrize(
        ("method", "expected"),
        [("max", 10), ("min", -7), ("median", 3), ("mean", 2)],
    )
    def test_int_dest_truncates(self, runner_factory, method, expected):
        # values 3, -7, 10, 4: median (3 + 4) / 2 = 3.5 -> 3, mean 10 / 4 = 2.5 -> 2
        result = self._run(
            runner_factory,
            lambda ds: getattr(ds.select(1, 4), method)(),
            Int("Out"),
            [3, -7, 10, 4],
        )
        assert result == expected

    @pytest.mark.parametrize(
        ("method", "expected"),
        # An integer range's even-count MDN is already an integer (CLICK): 3.5 -> 3.
        [("median", 3.0), ("mean", 2.5)],
    )
    def test_real_dest_keeps_fraction(self, runner_factory, method, expected):
        result = self._run(
            runner_factory,
            lambda ds: getattr(ds.select(1, 4), method)(),
            Real("Out"),
            [3, -7, 10, 4],
        )
        assert result == pytest.approx(expected)

    def test_negative_mean_truncates_toward_zero(self, runner_factory):
        # -7, -2 -> -4.5 -> -4 (calc's integer store truncates toward zero)
        result = self._run(runner_factory, lambda ds: ds.select(1, 2).mean(), Int("Out"), [-7, -2])
        assert result == -4

    def test_composed_with_abs(self, runner_factory):
        result = self._run(
            runner_factory,
            lambda ds: ds.select(1, 4).max() + abs(ds[5]),
            Int("Out"),
            [3, -7, 10, 4, -20],
        )
        assert result == 30

    def test_word_range_is_unsigned_hex_mode(self, runner_factory):
        """MAX over WORD treats values as unsigned 16-bit, like the rest of hex mode."""
        from pyrung.core import Block, TagType, Word

        Enable = Bool("Enable")
        DH = Block("DH", TagType.WORD, 1, 3)
        Out = Word("Out")
        with Program() as logic:
            with Rung(Enable):
                calc(DH.select(1, 3).max(), Out)
        runner = runner_factory(logic)
        runner.patch({"Enable": True, "DH1": 0x0001, "DH2": 0xFFFF, "DH3": 0x8000})
        runner.step()
        assert runner.current_state.tags["Out"] == 0xFFFF

    def test_bool_range_sum_counts_true_bits(self, runner_factory):
        from pyrung.core import Block, TagType

        Enable = Bool("Enable")
        C = Block("C", TagType.BOOL, 1, 5)
        Count = Int("Count")
        with Program() as logic:
            with Rung(Enable):
                calc(C.select(1, 5).sum(), Count)
                calc(C.select(1, 1).sum(), Int("One"))
        runner = runner_factory(logic)
        runner.patch({"Enable": True, "C1": True, "C3": True, "C4": True})
        runner.step()
        assert runner.current_state.tags["Count"] == 3
        assert runner.current_state.tags["One"] == 1
