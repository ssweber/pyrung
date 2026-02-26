"""Feature-complete tests for CircuitPython code generation."""

from __future__ import annotations

import math
import sys
import types
from typing import cast

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
    Dint,
    InputBlock,
    Int,
    OutputBlock,
    Program,
    Real,
    Rung,
    TagType,
    Tms,
    all_of,
    as_ascii,
    as_binary,
    as_text,
    as_value,
    blockcopy,
    branch,
    calc,
    call,
    copy,
    count_down,
    count_up,
    fill,
    forloop,
    lro,
    off_delay,
    on_delay,
    out,
    pack_bits,
    pack_text,
    pack_words,
    return_early,
    run_enabled_function,
    run_function,
    search,
    shift,
    sqrt,
    subroutine,
    unpack_to_bits,
    unpack_to_words,
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
from pyrung.core.instruction import Instruction
from pyrung.core.system_points import system


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


def _basic_hw() -> tuple[P1AM, InputBlock, OutputBlock]:
    hw = P1AM()
    di = hw.slot(1, "P1-08SIM")
    do = hw.slot(2, "P1-08TRS")
    return hw, di, do


def _stub_module(name: str, **attrs: object) -> types.ModuleType:
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    return module


def _namespace_list(namespace: dict[str, object], symbol: str) -> list[object]:
    value = namespace[symbol]
    assert isinstance(value, list)
    return cast(list[object], value)


def _run_single_scan_source(source: str, monkeypatch, stub_base: object) -> dict[str, object]:
    single_scan_source = source.replace("while True:", "for __scan_once in range(1):", 1)

    board_mod = _stub_module(
        "board",
        SD_SCK=object(),
        SD_MOSI=object(),
        SD_MISO=object(),
        SD_CS=object(),
    )
    busio_mod = _stub_module("busio", SPI=lambda *args, **kwargs: object())
    sdcardio_mod = _stub_module("sdcardio", SDCard=lambda *args, **kwargs: object())
    storage_mod = _stub_module(
        "storage",
        VfsFat=lambda *_args, **_kwargs: object(),
        mount=lambda *_args, **_kwargs: None,
    )
    p1am_mod = _stub_module("P1AM", Base=lambda: stub_base)
    microcontroller_mod = _stub_module("microcontroller", nvm=bytearray(1))

    monkeypatch.setitem(sys.modules, "board", board_mod)
    monkeypatch.setitem(sys.modules, "busio", busio_mod)
    monkeypatch.setitem(sys.modules, "sdcardio", sdcardio_mod)
    monkeypatch.setitem(sys.modules, "storage", storage_mod)
    monkeypatch.setitem(sys.modules, "P1AM", p1am_mod)
    monkeypatch.setitem(sys.modules, "microcontroller", microcontroller_mod)

    namespace: dict[str, object] = {}
    exec(compile(single_scan_source, "code.py", "exec"), namespace, namespace)
    return namespace


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

    def test_function_call_requires_inspectable_source(self):
        hw = P1AM()
        hw.slot(1, "P1-08SIM")

        with Program(strict=False) as prog:
            with Rung(Bool("Enable")):
                run_function(abs)

        with pytest.raises(ValueError, match="inspect"):
            generate_circuitpy(prog, hw, target_scan_ms=10.0)

    def test_strict_validation_blocks_io_untracked_findings(self):
        hw = P1AM()
        outputs = hw.slot(1, "P1-08TRS")
        light = outputs[1]
        external = InputBlock("External", TagType.BOOL, 1, 8)[1]

        with Program(strict=False) as prog:
            with Rung(external):
                out(light)

        with pytest.raises(ValueError, match="CPY_IO_BLOCK_UNTRACKED"):
            generate_circuitpy(prog, hw, target_scan_ms=10.0)

    def test_strict_validation_keeps_advisories_non_blocking(self):
        hw = P1AM()
        hw.slot(1, "P1-08SIM")
        done = Bool("Done")
        acc = Int("Acc")
        dest = Int("Dest")

        def fn():
            return {"result": 1}

        with Program(strict=False) as prog:
            with Rung(Bool("Enable")):
                on_delay(done, acc, preset=5, unit=Tms)
                run_function(fn, outs={"result": dest})

        source_code = generate_circuitpy(prog, hw, target_scan_ms=10.0)
        assert "def _run_main_rungs():" in source_code


class TestDeterministicOutput:
    def test_same_inputs_generate_identical_output(self):
        hw, di, do = _basic_hw()
        with Program(strict=False) as prog:
            with Rung(di[1]):
                out(do[1])

        s1 = generate_circuitpy(prog, hw, target_scan_ms=10.0, watchdog_ms=1000)
        s2 = generate_circuitpy(prog, hw, target_scan_ms=10.0, watchdog_ms=1000)
        assert s1 == s2


class TestPrevSnapshotScope:
    def test_no_edge_conditions_emit_no_prev_snapshots(self):
        hw = P1AM()
        hw.slot(1, "P1-08SIM")
        with Program(strict=False) as prog:
            with Rung(Bool("Enable")):
                out(Bool("Light"))

        source_code = generate_circuitpy(prog, hw, target_scan_ms=10.0)
        assert '_prev["' not in source_code

    def test_prev_snapshots_only_include_edge_tags(self):
        hw = P1AM()
        hw.slot(1, "P1-08SIM")
        sensor = Bool("Car_Sensor")
        log_enable = Bool("Car_LogEnable")
        aux = Bool("Aux")
        light = Bool("Light")

        with Program(strict=False) as prog:
            with Rung(RisingEdgeCondition(sensor)):
                out(light)
            with Rung(BitCondition(log_enable)):
                out(aux)
            with Rung(FallingEdgeCondition(log_enable)):
                out(light)

        source_code = generate_circuitpy(prog, hw, target_scan_ms=10.0)
        assert '_prev["Car_Sensor"]' in source_code
        assert '_prev["Car_LogEnable"]' in source_code
        assert source_code.count('_prev["') == 2
        assert '_prev["Aux"]' not in source_code
        assert '_prev["Light"]' not in source_code


class TestConditionAndExpressionCompiler:
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


class TestInstructionCoverage:
    def test_timers_counters_copy_calc_and_block_ops_emit(self):
        hw = P1AM()
        hw.slot(1, "P1-08SIM")
        done_ton = Bool("DoneTON")
        acc_ton = Int("AccTON")
        done_tof = Bool("DoneTOF")
        acc_tof = Int("AccTOF")
        done_up = Bool("DoneUp")
        acc_up = Dint("AccUp")
        done_dn = Bool("DoneDn")
        acc_dn = Dint("AccDn")
        source = Int("Source")
        calc_out = Int("CalcOut")
        reset_tag = Bool("Reset")
        ds = Block("DS", TagType.INT, 1, 10)
        dd = Block("DD", TagType.INT, 1, 10)
        idx = Int("Idx")

        with Program(strict=False) as prog:
            with Rung(Bool("Enable")):
                on_delay(done_ton, acc_ton, preset=5).reset(reset_tag)
            with Rung(Bool("Enable")):
                off_delay(done_tof, acc_tof, preset=5)
            with Rung(Bool("Enable")):
                count_up(done_up, acc_up, preset=2).reset(reset_tag)
            with Rung(Bool("Enable")):
                count_down(done_dn, acc_dn, preset=2).reset(reset_tag)
            with Rung(Bool("Enable")):
                copy(40000, source)
                calc(source + 1, calc_out, mode="decimal")
                blockcopy(ds.select(1, 3), dd.select(1, 3))
                fill(7, dd.select(idx, idx + 2))

        source_code = generate_circuitpy(prog, hw, target_scan_ms=10.0)
        assert "_frac:" in source_code
        assert "_dt_units" in source_code
        assert "_delta" in source_code
        assert "_store_copy_value_to_type(" in source_code
        assert "_wrap_int(" in source_code
        assert "BlockCopy length mismatch" in source_code
        assert "for _src_idx, _dst_idx in zip(" in source_code
        assert "Indirect range start must be <= end" in source_code

    def test_search_shift_pack_unpack_and_forloop_emit(self):
        hw = P1AM()
        hw.slot(1, "P1-08SIM")
        ds = Block("DS", TagType.INT, 1, 10)
        txt = Block("TXT", TagType.CHAR, 1, 8)
        bits = Block("Bits", TagType.BOOL, 1, 32)
        words = Block("Words", TagType.INT, 1, 2)
        found = Bool("Found")
        result = Int("Result")
        word = Int("Word")
        dword = Real("DWord")
        loop_count = Int("LoopCount")
        target = Int("Target")
        clock = Bool("Clock")
        reset_tag = Bool("Reset")

        with Program(strict=False) as prog:
            with Rung(Bool("Enable")):
                search(">=", 5, ds.select(1, 10), result=result, found=found, continuous=True)
                search("==", "AB", txt.select(1, 8), result=result, found=found)
            with Rung(Bool("Enable")):
                shift(bits.select(1, 8)).clock(clock).reset(reset_tag)
            with Rung(Bool("Enable")):
                pack_bits(bits.select(1, 16), word)
                pack_words(words.select(1, 2), dword)
                pack_text(txt.select(1, 8), dword, allow_whitespace=True)
                unpack_to_bits(dword, bits.select(1, 32))
                unpack_to_words(dword, words.select(1, 2))
                with forloop(loop_count, oneshot=True) as lp:
                    copy(lp.idx + 1, target)

        source_code = generate_circuitpy(prog, hw, target_scan_ms=10.0)
        assert "_cursor_index" in source_code
        assert "_shift_prev_clock:" in source_code
        assert "_int_to_float_bits(" in source_code
        assert "_float_to_int_bits(" in source_code
        assert "_parse_pack_text_value(" in source_code
        assert "_for_i" in source_code
        assert "_search_1_rng_1_indices = range(0, 10)" in source_code
        assert "_search_1_rng_1_addrs = range(1, 11)" in source_code
        assert "_shift_1_rng_1_indices = range(0, 8)" in source_code
        assert "\"Text search only supports '==' and '!=' conditions\"" not in source_code
        assert '"shift bit_range resolved to an empty range"' not in source_code
        assert '"pack_bits destination width is 16 bits but block has' not in source_code
        assert '"pack_words requires exactly 2 source tags; got' not in source_code
        assert '"unpack_to_bits source width is 32 bits but block has' not in source_code
        assert '"unpack_to_words requires exactly 2 destination tags; got' not in source_code
        assert "import hashlib" not in source_code
        assert "if True:" not in source_code
        assert "if False:" not in source_code
        key_lines = [
            line
            for line in source_code.splitlines()
            if ("_oneshot:" in line) or ("_shift_prev_clock:" in line)
        ]
        assert key_lines
        for line in key_lines:
            assert ":i" in line
            assert ".py:" not in line

    def test_function_call_subroutine_and_return_emit(self):
        hw = P1AM()
        hw.slot(1, "P1-08SIM")
        source = Int("Source")
        dest = Int("Dest")

        def plus_one(value):
            return {"result": value + 1}

        def gated(enabled, value):
            return {"result": value + (1 if enabled else 0)}

        with Program(strict=False) as prog:
            with Rung(Bool("Enable")):
                run_function(plus_one, ins={"value": source}, outs={"result": dest})
                run_enabled_function(gated, ins={"value": source}, outs={"result": dest})
                call("worker")

            with subroutine("worker"):
                with Rung(Bool("Stop")):
                    return_early()
                with Rung(Bool("Enable")):
                    out(Bool("Light"))

        source_code = generate_circuitpy(prog, hw, target_scan_ms=10.0)
        assert "def _sub_worker():" in source_code
        assert "return" in source_code
        assert "_fn_plus_one" in source_code
        assert "_fn_gated" in source_code
        assert "run_enabled_function" in source_code

    def test_unknown_instruction_type_raises(self):
        hw = P1AM()
        hw.slot(1, "P1-08SIM")

        class UnknownInstruction(Instruction):
            def execute(self, ctx, enabled):  # pragma: no cover - generation-only
                return

        with Program(strict=False) as prog:
            with Rung(Bool("Enable")):
                out(Bool("Light"))

        prog.rungs[0].add_instruction(UnknownInstruction())
        with pytest.raises(NotImplementedError, match="UnknownInstruction"):
            generate_circuitpy(prog, hw, target_scan_ms=10.0)


class TestPersistenceWatchdogAndDiagnostics:
    def test_sd_persistence_and_status_system_points_emit(self):
        hw = P1AM()
        hw.slot(1, "P1-08SIM")
        step = Int("Step")

        with Program(strict=False) as prog:
            with Rung(all_of(Bool("Enable"), system.storage.sd.ready)):
                copy(5, step)
                out(system.storage.sd.save_cmd)
                out(system.storage.sd.eject_cmd)
                out(system.storage.sd.delete_all_cmd)

        source_code = generate_circuitpy(prog, hw, target_scan_ms=10.0)
        assert "busio.SPI(board.SD_SCK, board.SD_MOSI, board.SD_MISO)" in source_code
        assert "_MEMORY_TMP_PATH" in source_code
        assert "os.replace(_MEMORY_TMP_PATH, _MEMORY_PATH)" in source_code
        assert "for _path in (_MEMORY_PATH, _MEMORY_TMP_PATH):" in source_code
        assert "os.remove(_path)" in source_code
        assert 'payload = {"schema": _RET_SCHEMA, "values": values}' in source_code
        assert "_sd_error_code = 1" in source_code
        assert "_sd_error_code = 2" in source_code
        assert "_sd_error_code = 3" in source_code
        assert "_service_sd_commands()" in source_code
        assert "save_memory()" in source_code
        assert "_t_storage_sd_save_cmd" in source_code
        assert "_t_storage_sd_eject_cmd" in source_code
        assert 'storage.umount("/sd")' in source_code
        assert "_sd_available = False" in source_code
        assert (
            "while True:\n"
            "    scan_start = time.monotonic()\n"
            "    _sd_write_status = False\n" in source_code
        )
        helper_section = source_code.split("def _service_sd_commands():", 1)[1].split("\ndef _", 1)[
            0
        ]
        assert helper_section.count("_sd_write_status = True") == 2
        assert (
            "# SC69 pulses for this serviced-command scan; reset occurs at next scan start."
            in helper_section
        )
        assert "_t_storage_sd_copy_system_cmd" not in source_code

    def test_watchdog_and_scan_diagnostics_emit(self):
        hw, di, do = _basic_hw()
        with Program(strict=False) as prog:
            with Rung(di[1]):
                out(do[1])

        source_code = generate_circuitpy(prog, hw, target_scan_ms=10.0, watchdog_ms=1000)
        assert 'getattr(base, "config_watchdog", None)' in source_code
        assert (
            'raise RuntimeError("P1AM snake_case watchdog API not found on Base() instance")'
            in source_code
        )
        assert "\n_wd_config(WATCHDOG_MS)\n" in source_code
        assert "\n_wd_start()\n" in source_code
        assert "if WATCHDOG_MS is not None:" not in source_code
        assert "    _wd_pet()" in source_code
        assert "_scan_overrun_count += 1" in source_code
        assert "PRINT_SCAN_OVERRUNS" in source_code


class TestIOMappingAndBranching:
    def test_discrete_analog_temperature_and_combo_mapping(self):
        hw = P1AM()
        di = hw.slot(1, "P1-08SIM")
        do = hw.slot(2, "P1-08TRS")
        hw.slot(3, "P1-04RTD")
        hw.slot(4, "P1-04DAL-1")
        combo_in, combo_out = hw.slot(5, "P1-16CDR")

        with Program(strict=False) as prog:
            with Rung(di[1]):
                out(do[1])
            with Rung(combo_in[1]):
                out(combo_out[1])

        source_code = generate_circuitpy(prog, hw, target_scan_ms=10.0)
        assert "base.readDiscrete(1)" in source_code
        assert "base.writeDiscrete(" in source_code
        assert "base.readTemperature(3, 1)" in source_code
        assert "base.writeAnalog(" in source_code
        assert "base.readDiscrete(5)" in source_code

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


class TestCopyModifierCodegen:
    def test_generation_unblocks_copy_blockcopy_fill_modifiers(self):
        hw = P1AM()
        hw.slot(1, "P1-08SIM")
        ch = Block("CH", TagType.CHAR, 1, 12)
        ds = Block("DS", TagType.INT, 1, 12)

        with Program(strict=False) as prog:
            with Rung(Bool("Enable")):
                copy(as_value(ch[1]), ds[1])
                blockcopy(as_ascii(ch.select(1, 3)), ds.select(1, 3))
                fill(as_binary(ds[4]), ch.select(4, 6))
                fill(as_text(ds[5], suppress_zero=False, pad=4), ch.select(7, 12))

        source = generate_circuitpy(prog, hw, target_scan_ms=10.0)
        assert "CopyModifier-based copy() codegen is not implemented" not in source
        assert "CopyModifier-based blockcopy() codegen is not implemented" not in source
        assert "CopyModifier-based fill() codegen is not implemented" not in source
        assert "_store_numeric_text_digit(" in source
        assert "_render_text_from_numeric(" in source
        assert "_termination_char(" in source

    def test_copy_modifier_runtime_modes_smoke(self, monkeypatch):
        hw = P1AM()
        hw.slot(1, "P1-08SIM")
        ch = Block("CH", TagType.CHAR, 1, 12)
        ds = Block("DS", TagType.INT, 1, 12)
        enable = Bool("Enable", default=True)

        with Program(strict=False) as prog:
            with Rung(enable):
                fill("", ch.select(1, 12))
                fill(0, ds.select(1, 12))
                copy("5", ch[1])
                copy("A", ch[2])
                copy(7, ds[3])
                copy(123, ds[4])
                copy(as_value(ch[1]), ds[1])
                copy(as_ascii(ch[2]), ds[2])
                copy(as_text(ds[3], suppress_zero=False, pad=5, termination_code=13), ch[4])
                copy(as_binary(ds[4]), ch[10])

        ctx = _context_for_program(prog, hw)
        ch_symbol = ctx.symbol_for_block(ch)
        ds_symbol = ctx.symbol_for_block(ds)

        source = generate_circuitpy(prog, hw, target_scan_ms=10.0, watchdog_ms=None)

        class StubBase:
            def rollCall(self, modules):
                return None

            def readDiscrete(self, slot):
                return 0

            def writeDiscrete(self, value, slot):
                return None

            def readAnalog(self, slot, ch_num):
                return 0

            def writeAnalog(self, value, slot, ch_num):
                return None

            def readTemperature(self, slot, ch_num):
                return 0.0

        namespace = _run_single_scan_source(source, monkeypatch, StubBase())
        ch_values = _namespace_list(namespace, ch_symbol)
        ds_values = _namespace_list(namespace, ds_symbol)

        assert ds_values[0] == 5
        assert ds_values[1] == 65
        assert ch_values[3] == "0"
        assert ch_values[4] == "0"
        assert ch_values[5] == "0"
        assert ch_values[6] == "0"
        assert ch_values[7] == "7"
        ch_terminator = ch_values[8]
        assert isinstance(ch_terminator, str)
        assert ord(ch_terminator) == 13
        assert ch_values[9] == "{"

    def test_blockcopy_modifier_failure_sets_fault_and_avoids_partial_write(self, monkeypatch):
        hw = P1AM()
        hw.slot(1, "P1-08SIM")
        ch = Block("CH", TagType.CHAR, 1, 10)
        ds = Block("DS", TagType.INT, 1, 10)
        fault_seen = Bool("FaultSeen")
        enable = Bool("Enable", default=True)

        with Program(strict=False) as prog:
            with Rung(enable):
                copy("1", ch[1])
                copy("A", ch[2])
                copy("3", ch[3])
                fill(9, ds.select(1, 3))
                blockcopy(as_value(ch.select(1, 3)), ds.select(1, 3))
            with Rung(system.fault.out_of_range):
                out(fault_seen)

        ctx = _context_for_program(prog, hw)
        ds_symbol = ctx.symbol_for_block(ds)
        fault_seen_symbol = ctx.symbol_for_tag(fault_seen)

        source = generate_circuitpy(prog, hw, target_scan_ms=10.0, watchdog_ms=None)

        class StubBase:
            def rollCall(self, modules):
                return None

            def readDiscrete(self, slot):
                return 0

            def writeDiscrete(self, value, slot):
                return None

            def readAnalog(self, slot, ch_num):
                return 0

            def writeAnalog(self, value, slot, ch_num):
                return None

            def readTemperature(self, slot, ch_num):
                return 0.0

        namespace = _run_single_scan_source(source, monkeypatch, StubBase())
        ds_values = _namespace_list(namespace, ds_symbol)
        assert ds_values[0] == 9
        assert ds_values[1] == 9
        assert ds_values[2] == 9
        assert namespace[fault_seen_symbol] is True

    def test_fill_modifier_text_renders_and_blank_fills_tail(self, monkeypatch):
        hw = P1AM()
        hw.slot(1, "P1-08SIM")
        txt = Block("TXT", TagType.CHAR, 1, 8)
        src = Int("Src", default=12)
        enable = Bool("Enable", default=True)

        with Program(strict=False) as prog:
            with Rung(enable):
                fill(
                    as_text(src, suppress_zero=False, pad=4, termination_code=13), txt.select(1, 8)
                )

        ctx = _context_for_program(prog, hw)
        txt_symbol = ctx.symbol_for_block(txt)
        source = generate_circuitpy(prog, hw, target_scan_ms=10.0, watchdog_ms=None)

        class StubBase:
            def rollCall(self, modules):
                return None

            def readDiscrete(self, slot):
                return 0

            def writeDiscrete(self, value, slot):
                return None

            def readAnalog(self, slot, ch_num):
                return 0

            def writeAnalog(self, value, slot, ch_num):
                return None

            def readTemperature(self, slot, ch_num):
                return 0.0

        namespace = _run_single_scan_source(source, monkeypatch, StubBase())
        txt_values = _namespace_list(namespace, txt_symbol)
        assert txt_values[0] == "0"
        assert txt_values[1] == "0"
        assert txt_values[2] == "1"
        assert txt_values[3] == "2"
        txt_terminator = txt_values[4]
        assert isinstance(txt_terminator, str)
        assert ord(txt_terminator) == 13
        assert txt_values[5] == ""
        assert txt_values[6] == ""
        assert txt_values[7] == ""

    def test_scalar_multi_char_copy_modifier_faults_and_skips_write(self, monkeypatch):
        hw = P1AM()
        hw.slot(1, "P1-08SIM")
        source_value = Int("SourceValue", default=123)
        target = Int("Target", default=9)
        fault_seen = Bool("FaultSeen")
        enable = Bool("Enable", default=True)

        with Program(strict=False) as prog:
            with Rung(enable):
                copy(as_text(source_value, suppress_zero=False), target)
            with Rung(system.fault.out_of_range):
                out(fault_seen)

        ctx = _context_for_program(prog, hw)
        target_symbol = ctx.symbol_for_tag(target)
        fault_seen_symbol = ctx.symbol_for_tag(fault_seen)
        source = generate_circuitpy(prog, hw, target_scan_ms=10.0, watchdog_ms=None)

        class StubBase:
            def rollCall(self, modules):
                return None

            def readDiscrete(self, slot):
                return 0

            def writeDiscrete(self, value, slot):
                return None

            def readAnalog(self, slot, ch_num):
                return 0

            def writeAnalog(self, value, slot, ch_num):
                return None

            def readTemperature(self, slot, ch_num):
                return 0.0

        namespace = _run_single_scan_source(source, monkeypatch, StubBase())
        assert namespace[target_symbol] == 9
        assert namespace[fault_seen_symbol] is True


class TestGeneratedSourceSmoke:
    def test_generated_source_compile_and_execute_single_scan(self, monkeypatch):
        hw, di, do = _basic_hw()
        with Program(strict=False) as prog:
            with Rung(di[1]):
                out(do[1])

        source = generate_circuitpy(prog, hw, target_scan_ms=10.0, watchdog_ms=None)
        assert 'getattr(base, "config_watchdog", None)' not in source
        assert "_wd_pet()" not in source
        compile(source, "code.py", "exec")
        single_scan_source = source.replace("while True:", "for __scan_once in range(1):", 1)

        class StubBase:
            def __init__(self):
                self.roll_called = None
                self.discrete_reads = {1: 0b00000001}
                self.discrete_writes = []

            def rollCall(self, modules):
                self.roll_called = list(modules)

            def readDiscrete(self, slot):
                return self.discrete_reads.get(slot, 0)

            def writeDiscrete(self, value, slot):
                self.discrete_writes.append((slot, value))

            def readAnalog(self, slot, ch):
                return 0

            def writeAnalog(self, value, slot, ch):
                return None

            def readTemperature(self, slot, ch):
                return 0.0

        stub_base = StubBase()

        board_mod = _stub_module(
            "board",
            SD_SCK=object(),
            SD_MOSI=object(),
            SD_MISO=object(),
            SD_CS=object(),
        )
        busio_mod = _stub_module("busio", SPI=lambda *args, **kwargs: object())
        sdcardio_mod = _stub_module("sdcardio", SDCard=lambda *args, **kwargs: object())
        storage_mod = _stub_module(
            "storage",
            VfsFat=lambda *_args, **_kwargs: object(),
            mount=lambda *_args, **_kwargs: None,
        )
        p1am_mod = _stub_module("P1AM", Base=lambda: stub_base)
        microcontroller_mod = _stub_module("microcontroller", nvm=bytearray(1))

        monkeypatch.setitem(sys.modules, "board", board_mod)
        monkeypatch.setitem(sys.modules, "busio", busio_mod)
        monkeypatch.setitem(sys.modules, "sdcardio", sdcardio_mod)
        monkeypatch.setitem(sys.modules, "storage", storage_mod)
        monkeypatch.setitem(sys.modules, "P1AM", p1am_mod)
        monkeypatch.setitem(sys.modules, "microcontroller", microcontroller_mod)

        namespace: dict[str, object] = {}
        exec(compile(single_scan_source, "code.py", "exec"), namespace, namespace)

        assert stub_base.roll_called == ["P1-08SIM", "P1-08TRS"]
        assert stub_base.discrete_writes[-1] == (2, 1)

    def test_pack_text_parse_error_sets_fault_and_scan_continues(self, monkeypatch):
        hw = P1AM()
        hw.slot(1, "P1-08SIM")
        txt = Block("TXT", TagType.CHAR, 1, 1)
        parsed = Int("Parsed")
        fault_seen = Bool("FaultSeen")
        enable = Bool("Enable", default=True)

        with Program(strict=False) as prog:
            with Rung(enable):
                # Empty CHAR slot parses as empty text and should fault (not crash scan).
                pack_text(txt.select(1, 1), parsed, allow_whitespace=True)
            with Rung(system.fault.out_of_range):
                out(fault_seen)

        ctx = _context_for_program(prog, hw)
        fault_symbol = ctx.symbol_for_tag(system.fault.out_of_range)
        parsed_symbol = ctx.symbol_for_tag(parsed)
        fault_seen_symbol = ctx.symbol_for_tag(fault_seen)

        source = generate_circuitpy(prog, hw, target_scan_ms=10.0, watchdog_ms=None)
        single_scan_source = source.replace("while True:", "for __scan_once in range(1):", 1)

        class StubBase:
            def __init__(self):
                self.discrete_reads = {1: 0}

            def rollCall(self, modules):
                return None

            def readDiscrete(self, slot):
                return self.discrete_reads.get(slot, 0)

            def writeDiscrete(self, value, slot):
                return None

            def readAnalog(self, slot, ch):
                return 0

            def writeAnalog(self, value, slot, ch):
                return None

            def readTemperature(self, slot, ch):
                return 0.0

        stub_base = StubBase()

        board_mod = _stub_module(
            "board",
            SD_SCK=object(),
            SD_MOSI=object(),
            SD_MISO=object(),
            SD_CS=object(),
        )
        busio_mod = _stub_module("busio", SPI=lambda *args, **kwargs: object())
        sdcardio_mod = _stub_module("sdcardio", SDCard=lambda *args, **kwargs: object())
        storage_mod = _stub_module(
            "storage",
            VfsFat=lambda *_args, **_kwargs: object(),
            mount=lambda *_args, **_kwargs: None,
        )
        p1am_mod = _stub_module("P1AM", Base=lambda: stub_base)
        microcontroller_mod = _stub_module("microcontroller", nvm=bytearray(1))

        monkeypatch.setitem(sys.modules, "board", board_mod)
        monkeypatch.setitem(sys.modules, "busio", busio_mod)
        monkeypatch.setitem(sys.modules, "sdcardio", sdcardio_mod)
        monkeypatch.setitem(sys.modules, "storage", storage_mod)
        monkeypatch.setitem(sys.modules, "P1AM", p1am_mod)
        monkeypatch.setitem(sys.modules, "microcontroller", microcontroller_mod)

        namespace: dict[str, object] = {}
        exec(compile(single_scan_source, "code.py", "exec"), namespace, namespace)

        assert namespace[fault_symbol] is True
        assert namespace[fault_seen_symbol] is True
        assert namespace[parsed_symbol] == 0
