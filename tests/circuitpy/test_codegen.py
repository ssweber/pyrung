"""Foundation tests for CircuitPython code generation."""

from __future__ import annotations

import math

import pytest

from pyrung.circuitpy import P1AM, generate_circuitpy
from pyrung.circuitpy.codegen import (
    CodegenContext,
    compile_condition,
    compile_expression,
    compile_rung,
)
from pyrung.core import (
    Block,
    Bool,
    Int,
    Program,
    Rung,
    TagType,
    branch,
    lro,
    out,
    run_function,
    sqrt,
)
from pyrung.core.condition import (
    AllCondition,
    AnyCondition,
    BitCondition,
    CompareEq,
    CompareGe,
    CompareGt,
    CompareLe,
    CompareLt,
    CompareNe,
    FallingEdgeCondition,
    IntTruthyCondition,
    NormallyClosedCondition,
    RisingEdgeCondition,
)
from pyrung.core.expression import Expression


def _context_for_program(program: Program, hw: P1AM) -> CodegenContext:
    ctx = CodegenContext(program=program, hw=hw, target_scan_ms=10.0, watchdog_ms=None)
    ctx.collect_hw_bindings()
    ctx.collect_program_references()
    ctx.collect_retentive_tags()
    ctx.assign_symbols()
    return ctx


def _manual_context(*tags) -> CodegenContext:
    hw = P1AM()
    hw.slot(1, "P1-08SIM")
    prog = Program(strict=False)
    ctx = CodegenContext(program=prog, hw=hw, target_scan_ms=10.0, watchdog_ms=None)
    ctx.collect_hw_bindings()
    ctx.referenced_tags = {tag.name: tag for tag in tags}
    ctx.collect_retentive_tags()
    ctx.assign_symbols()
    return ctx


class TestGenerateCircuitPyAPI:
    def test_rejects_bad_argument_types_and_values(self):
        hw = P1AM()
        hw.slot(1, "P1-08SIM")
        prog = Program(strict=False)

        with pytest.raises(TypeError, match="program"):
            generate_circuitpy("nope", hw, target_scan_ms=10.0)  # type: ignore[arg-type]
        with pytest.raises(TypeError, match="hw"):
            generate_circuitpy(prog, object(), target_scan_ms=10.0)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="target_scan_ms"):
            generate_circuitpy(prog, hw, target_scan_ms=0.0)
        with pytest.raises(ValueError, match="target_scan_ms"):
            generate_circuitpy(prog, hw, target_scan_ms=math.inf)
        with pytest.raises(TypeError, match="watchdog_ms"):
            generate_circuitpy(prog, hw, target_scan_ms=10.0, watchdog_ms=1.5)  # type: ignore[arg-type]

    def test_rejects_empty_and_non_contiguous_slots(self):
        empty_hw = P1AM()
        prog = Program(strict=False)
        with pytest.raises(ValueError, match="at least one configured slot"):
            generate_circuitpy(prog, empty_hw, target_scan_ms=10.0)

        hw = P1AM()
        hw.slot(1, "P1-08SIM")
        hw.slot(3, "P1-08TRS")
        with pytest.raises(ValueError, match="contiguous"):
            generate_circuitpy(prog, hw, target_scan_ms=10.0)

    def test_validation_gate_fails_on_strict_findings(self):
        hw = P1AM()
        hw.slot(1, "P1-08SIM")

        source = Int("Source")
        dest = Int("Dest")

        def fn(value):
            return {"result": value}

        with Program(strict=False) as prog:
            with Rung():
                run_function(fn, ins={"value": source}, outs={"result": dest})

        with pytest.raises(ValueError, match="CPY_FUNCTION_CALL_VERIFY"):
            generate_circuitpy(prog, hw, target_scan_ms=10.0)

    def test_returns_str_for_valid_program(self):
        hw = P1AM()
        di = hw.slot(1, "P1-08SIM")
        do = hw.slot(2, "P1-08TRS")

        with Program(strict=False) as prog:
            with Rung(di[1]):
                out(do[1])

        source = generate_circuitpy(prog, hw, target_scan_ms=10.0, watchdog_ms=None)
        assert isinstance(source, str)
        assert "def _run_main_rungs():" in source


class TestDeterministicOutput:
    def test_same_inputs_generate_identical_output(self):
        hw = P1AM()
        di = hw.slot(1, "P1-08SIM")
        do = hw.slot(2, "P1-08TRS")

        with Program(strict=False) as prog:
            with Rung(di[1]):
                out(do[1])

        s1 = generate_circuitpy(prog, hw, target_scan_ms=10.0, watchdog_ms=1000)
        s2 = generate_circuitpy(prog, hw, target_scan_ms=10.0, watchdog_ms=1000)
        assert s1 == s2


class TestConditionCompiler:
    @pytest.mark.parametrize(
        ("condition", "needle"),
        [
            (BitCondition(Bool("A")), "bool("),
            (NormallyClosedCondition(Bool("A")), "not bool"),
            (IntTruthyCondition(Int("N")), "!= 0"),
            (CompareEq(Int("N"), 1), "=="),
            (CompareNe(Int("N"), 1), "!="),
            (CompareLt(Int("N"), 1), "<"),
            (CompareLe(Int("N"), 1), "<="),
            (CompareGt(Int("N"), 1), ">"),
            (CompareGe(Int("N"), 1), ">="),
            (AllCondition(Bool("A"), Bool("B")), " and "),
            (AnyCondition(Bool("A"), Bool("B")), " or "),
            (RisingEdgeCondition(Bool("A")), "_rise("),
            (FallingEdgeCondition(Bool("A")), "_fall("),
        ],
    )
    def test_condition_mappings(self, condition, needle):
        light = Bool("Light")
        hw = P1AM()
        hw.slot(1, "P1-08SIM")
        with Program(strict=False) as prog:
            with Rung(condition):
                out(light)
        ctx = _context_for_program(prog, hw)
        compiled = compile_condition(condition, ctx)
        assert needle in compiled

    def test_indirect_compare_uses_resolver_helper(self):
        idx = Int("Idx")
        ds = Block("DS", TagType.INT, 1, 3)
        light = Bool("Light")
        cond = ds[idx] >= 1
        hw = P1AM()
        hw.slot(1, "P1-08SIM")
        with Program(strict=False) as prog:
            with Rung(cond):
                out(light)
        ctx = _context_for_program(prog, hw)
        compiled = compile_condition(cond, ctx)
        assert "_resolve_index_" in compiled
        assert ">=" in compiled


class TestExpressionCompiler:
    def test_expression_nodes_compile(self):
        a = Int("A")
        b = Int("B")
        ctx = _manual_context(a, b)

        assert " + " in compile_expression(a + 1, ctx)
        assert " - " in compile_expression(a - b, ctx)
        assert " * " in compile_expression(a * 2, ctx)
        assert " / " in compile_expression(a / 2, ctx)
        assert " // " in compile_expression(a // 2, ctx)
        assert " % " in compile_expression(a % 2, ctx)
        assert " ** " in compile_expression(a**2, ctx)
        assert "abs(" in compile_expression(abs(a), ctx)
        assert "&" in compile_expression(a & b, ctx)
        assert "|" in compile_expression(a | b, ctx)
        assert "^" in compile_expression(a ^ b, ctx)
        assert "<<" in compile_expression(a << 1, ctx)
        assert ">>" in compile_expression(a >> 1, ctx)
        assert "~int" in compile_expression(~a, ctx)
        assert "math.sqrt(" in compile_expression(sqrt(a), ctx)
        assert "0xFFFF" in compile_expression(lro(a, 1), ctx)

    def test_unknown_expression_type_raises(self):
        a = Int("A")
        ctx = _manual_context(a)

        class UnknownExpr(Expression):
            def evaluate(self, ctx):  # pragma: no cover - not executed
                return 0

        with pytest.raises(TypeError, match="Unsupported expression type"):
            compile_expression(UnknownExpr(), ctx)


class TestCoilAndBranchCompilation:
    def test_out_latch_reset_and_oneshot_shape(self):
        enable = Bool("Enable")
        light = Bool("Light")
        latched = Bool("Latched")
        reset_me = Bool("ResetMe")
        hw = P1AM()
        hw.slot(1, "P1-08SIM")
        with Program(strict=False) as prog:
            with Rung(enable):
                out(light, oneshot=True)
                out(reset_me)
                out(latched)
        source = generate_circuitpy(prog, hw, target_scan_ms=10.0)
        assert "_oneshot:" in source
        assert "_mem.get(" in source
        assert "= True" in source
        assert "= False" in source

    def test_branch_precompute_and_source_order(self):
        enable = Bool("Enable")
        branch_enable = Bool("BranchEnable")
        a = Bool("A")
        b = Bool("B")
        c = Bool("C")
        hw = P1AM()
        hw.slot(1, "P1-08SIM")
        with Program(strict=False) as prog:
            with Rung(enable):
                out(a)
                with branch(branch_enable):
                    out(b)
                out(c)

        ctx = _context_for_program(prog, hw)
        lines = compile_rung(prog.rungs[0], "_run_main_rungs", ctx, indent=0)
        a_sym = ctx.symbol_for_tag(a)
        b_sym = ctx.symbol_for_tag(b)
        c_sym = ctx.symbol_for_tag(c)

        branch_idx = next(i for i, line in enumerate(lines) if "_branch_" in line)
        a_idx = next(i for i, line in enumerate(lines) if f"{a_sym} = True" in line)
        b_idx = next(i for i, line in enumerate(lines) if f"{b_sym} = True" in line)
        c_idx = next(i for i, line in enumerate(lines) if f"{c_sym} = True" in line)

        assert branch_idx < a_idx < b_idx < c_idx


class TestIOMapping:
    def test_discrete_mapping_emitted(self):
        hw = P1AM()
        di = hw.slot(1, "P1-08SIM")
        do = hw.slot(2, "P1-08TRS")
        with Program(strict=False) as prog:
            with Rung(di[1]):
                out(do[1])
        source = generate_circuitpy(prog, hw, target_scan_ms=10.0)
        assert "base.readDiscrete(1)" in source
        assert "base.writeDiscrete(" in source
        assert "bool((" in source

    def test_analog_and_temperature_mapping_emitted(self):
        hw = P1AM()
        hw.slot(1, "P1-04RTD")
        hw.slot(2, "P1-04DAL-1")
        with Program(strict=False) as prog:
            with Rung(Bool("Enable")):
                out(Bool("Light"))
        source = generate_circuitpy(prog, hw, target_scan_ms=10.0)
        assert "base.readTemperature(1, 1)" in source
        assert "base.writeAnalog(" in source

    def test_combo_module_reads_and_writes_same_slot(self):
        hw = P1AM()
        inp, out_block = hw.slot(1, "P1-16CDR")
        with Program(strict=False) as prog:
            with Rung(inp[1]):
                out(out_block[1])
        source = generate_circuitpy(prog, hw, target_scan_ms=10.0)
        assert "base.readDiscrete(1)" in source
        assert "base.writeDiscrete(" in source


class TestIndirectAddressingAndSmoke:
    def test_indirect_helper_and_bounds_message(self):
        hw = P1AM()
        hw.slot(1, "P1-08SIM")
        idx = Int("Idx")
        ds = Block("DS", TagType.INT, 1, 3)
        with Program(strict=False) as prog:
            with Rung(ds[idx] > 0):
                out(Bool("Light"))
        source = generate_circuitpy(prog, hw, target_scan_ms=10.0)
        assert "def _resolve_index_" in source
        assert "Address {addr} out of range for DS (1-3)" in source

    def test_generated_source_compile_smoke(self):
        hw = P1AM()
        di = hw.slot(1, "P1-08SIM")
        do = hw.slot(2, "P1-08TRS")

        with Program(strict=False) as prog:
            with Rung(di[1]):
                out(do[1])

        source = generate_circuitpy(prog, hw, target_scan_ms=10.0, watchdog_ms=None)
        compile(source, "code.py", "exec")
