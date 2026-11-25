"""Feature-complete tests for CircuitPython code generation."""

from __future__ import annotations

import enum
import math as pymath
import re
import sys
import types
from typing import cast

import pytest

from pyrung.circuitpy import P1AM, ModbusServerConfig, RunStopConfig, board, generate_circuitpy
from pyrung.circuitpy.codegen import (
    CodegenContext,
    compile_condition,
    compile_expression,
    compile_kernel,
    compile_rung,
)
from pyrung.click import ClickBlocks, TagMap
from pyrung.core import (
    PLC,
    And,
    Block,
    Bool,
    Char,
    CompiledPLC,
    Counter,
    Dint,
    InputBlock,
    Int,
    OutputBlock,
    Program,
    Real,
    TagType,
    Timer,
    Word,
    atan,
    blockcopy,
    branch,
    call,
    copy,
    count_down,
    count_up,
    debounce,
    event_drum,
    fill,
    flasher,
    forloop,
    lro,
    lsh,
    math,
    named_array,
    off_delay,
    on_delay,
    out,
    pack_bits,
    pack_text,
    pack_words,
    reset,
    return_early,
    rro,
    rsh,
    run_enabled_function,
    run_function,
    rung,
    search,
    shift,
    sin,
    sqrt,
    subroutine,
    time_drum,
    time_event_drum,
    timed,
    to_ascii,
    to_binary,
    to_text,
    to_value,
    toggle,
    udt,
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
from pyrung.core.expression import Expression, log
from pyrung.core.instruction import Instruction
from pyrung.core.system_points import system

Counter2 = Counter.clone("Counter2")
Timer2 = Timer.clone("Timer2")
Timer4 = Timer.clone("Timer4")


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


class _Mode(enum.IntEnum):
    IDLE = 0
    RUN = 1


class _Name(enum.StrEnum):
    SEVEN = "7"


@udt()
class _Motor:
    speed: Int
    run: Bool


@named_array(Int, count=2, stride=2)
class _Pair:
    a = 0
    b = 0


class _IdleBase:
    """A P1AM base whose inputs read 0 and whose writes go nowhere."""

    def rollCall(self, modules):
        return None

    def readDiscrete(self, slot):
        return 0

    def writeDiscrete(self, value, slot):
        return None

    def readAnalog(self, slot, ch):
        return 0

    def writeAnalog(self, value, slot, ch):
        return None

    def readTemperature(self, slot, ch):
        return 0.0


def _on_every_backend(program: Program, monkeypatch, watch, *, scans: int) -> dict[str, tuple]:
    """Return *watch*'s values after *scans* scans on each backend, keyed by backend.

    The backends are the interpreter, the compiled kernel and the generated CircuitPython.
    """
    results: dict[str, tuple] = {}
    for name, cls in (("interpreted", PLC), ("compiled", CompiledPLC)):
        plc = cls(program)
        for _ in range(scans):
            plc.step()
        tags = plc.current_state.tags
        results[name] = tuple(tags[tag.name] for tag in watch)
    hw = P1AM()
    hw.slot(1, "P1-08SIM")
    result = generate_circuitpy(
        program,
        hw,
        target_scan_ms=10.0,
        runstop=RunStopConfig(debounce_ms=0, run_when_high=False),
    )
    namespace = _run_single_scan_source(
        result.code, monkeypatch, _IdleBase(), runtime_source=result.runtime, scans=scans
    )
    ctx = _context_for_program(program, hw)
    results["circuitpy"] = tuple(namespace[ctx.symbol_for_tag(tag)] for tag in watch)
    return results


def _run_single_scan_source(
    source: str,
    monkeypatch,
    stub_base: object,
    *,
    socket_factory=None,
    runtime_source: str = "",
    scans: int = 1,
) -> dict[str, object]:
    single_scan_source = source.replace("while True:", f"for __scan_once in range({scans}):", 1)

    class _StubDigitalInOut:
        def __init__(self, pin: object):
            self.pin = pin
            self.direction = None
            self.pull = None
            self.value = False

    class _StubNeoPixel:
        def __init__(self, pin: object, count: int, auto_write: bool = True):
            self.pin = pin
            self.count = count
            self.auto_write = auto_write
            self._pixels = [(0, 0, 0)] * count

        def __setitem__(self, index: int, value: tuple[int, int, int]) -> None:
            self._pixels[index] = value

        def __getitem__(self, index: int) -> tuple[int, int, int]:
            return self._pixels[index]

    class _StubWiznet5k:
        def __init__(self, spi: object, cs: object, *args, **kwargs):
            self.spi = spi
            self.cs = cs
            self.args = args
            self.kwargs = kwargs
            self.ifconfig = None

    class _StubSocket:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs
            self.timeout = None
            self.bound = None
            self.listen_backlog = None
            self.sent_packets: list[bytes] = []
            self.connected_to = None

        def bind(self, address):
            self.bound = address

        def listen(self, backlog: int):
            self.listen_backlog = backlog

        def settimeout(self, timeout):
            self.timeout = timeout

        def connect(self, address):
            self.connected_to = address
            return None

        def accept(self):
            raise OSError("would block")

        def recv_into(self, buf):
            return 0

        def send(self, payload):
            self.sent_packets.append(bytes(payload))
            return len(payload)

        def close(self):
            return None

    board_mod = _stub_module(
        "board",
        SD_SCK=object(),
        SD_MOSI=object(),
        SD_MISO=object(),
        SD_CS=object(),
        SCK=object(),
        MOSI=object(),
        MISO=object(),
        SWITCH=object(),
        LED=object(),
        NEOPIXEL=object(),
        D5=object(),
    )
    busio_mod = _stub_module("busio", SPI=lambda *args, **kwargs: object())
    sdcardio_mod = _stub_module("sdcardio", SDCard=lambda *args, **kwargs: object())
    digitalio_mod = _stub_module(
        "digitalio",
        DigitalInOut=_StubDigitalInOut,
        Direction=types.SimpleNamespace(INPUT=object(), OUTPUT=object()),
        Pull=types.SimpleNamespace(UP=object(), DOWN=object()),
    )
    neopixel_mod = _stub_module("neopixel", NeoPixel=_StubNeoPixel)
    adafruit_wiznet5k_mod = _stub_module("adafruit_wiznet5k")
    adafruit_wiznet5k_core_mod = _stub_module(
        "adafruit_wiznet5k.adafruit_wiznet5k", WIZNET5K=_StubWiznet5k
    )
    _socket_fn = (
        socket_factory
        if socket_factory is not None
        else (lambda *args, **kwargs: _StubSocket(*args, **kwargs))
    )

    class _StubSocketPool:
        def __init__(self, *_args, **_kwargs):
            pass

        def socket(self, *_args, **_kwargs):
            return _socket_fn(*_args, **_kwargs)

    adafruit_wiznet5k_socketpool_mod = _stub_module(
        "adafruit_wiznet5k.adafruit_wiznet5k_socketpool",
        SocketPool=_StubSocketPool,
    )
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
    monkeypatch.setitem(sys.modules, "digitalio", digitalio_mod)
    monkeypatch.setitem(sys.modules, "neopixel", neopixel_mod)
    monkeypatch.setitem(sys.modules, "adafruit_wiznet5k", adafruit_wiznet5k_mod)
    monkeypatch.setitem(
        sys.modules, "adafruit_wiznet5k.adafruit_wiznet5k", adafruit_wiznet5k_core_mod
    )
    monkeypatch.setitem(
        sys.modules,
        "adafruit_wiznet5k.adafruit_wiznet5k_socketpool",
        adafruit_wiznet5k_socketpool_mod,
    )
    monkeypatch.setitem(sys.modules, "storage", storage_mod)
    monkeypatch.setitem(sys.modules, "P1AM", p1am_mod)
    monkeypatch.setitem(sys.modules, "microcontroller", microcontroller_mod)

    rt_module = None
    if runtime_source:
        rt_module = types.ModuleType("pyrung_rt")
        exec(compile(runtime_source, "pyrung_rt.py", "exec"), rt_module.__dict__)
        monkeypatch.setitem(sys.modules, "pyrung_rt", rt_module)

    namespace: dict[str, object] = {}
    exec(compile(single_scan_source, "code.py", "exec"), namespace, namespace)

    # Expose runtime-resident names so tests can call them directly.
    if rt_module is not None:
        for key, value in rt_module.__dict__.items():
            if not key.startswith("__"):
                namespace.setdefault(key, value)

    return namespace


class TestGenerateCircuitPyAPI:
    def test_rejects_bad_argument_types_and_values(self):
        hw = P1AM()
        hw.slot(1, "P1-08SIM")
        prog = Program(strict=False)

        with pytest.raises(TypeError, match="program"):
            generate_circuitpy("nope", hw, target_scan_ms=10.0)
        with pytest.raises(TypeError, match="hw"):
            generate_circuitpy(prog, object(), target_scan_ms=10.0)
        with pytest.raises(ValueError, match="target_scan_ms"):
            generate_circuitpy(prog, hw, target_scan_ms=0.0)
        with pytest.raises(ValueError, match="target_scan_ms"):
            generate_circuitpy(prog, hw, target_scan_ms=pymath.inf)
        with pytest.raises(TypeError, match="watchdog_ms"):
            generate_circuitpy(prog, hw, target_scan_ms=10.0, watchdog_ms=1.5)
        with pytest.raises(TypeError, match="runstop"):
            generate_circuitpy(prog, hw, target_scan_ms=10.0, runstop="nope")
        with pytest.raises(TypeError, match="modbus_server"):
            generate_circuitpy(prog, hw, target_scan_ms=10.0, modbus_server="nope")
        with pytest.raises(TypeError, match="tag_map"):
            generate_circuitpy(prog, hw, target_scan_ms=10.0, tag_map="nope")

    @pytest.mark.parametrize(
        ("value", "shown"),
        [
            (lambda: Timer[1], "Timer[1]"),
            (lambda: Timer, "Timer"),
            (lambda: _Motor, "_Motor"),
            (lambda: _Motor[1], "_Motor[1]"),
            (lambda: _Pair, "_Pair"),
            (lambda: Block("R", TagType.INT, 1, 2), "the whole block R"),
            (lambda: Block("R", TagType.INT, 1, 2).select(1, 2), "a range from .select()"),
            (lambda: Block("R", TagType.INT, 1, 2).slot(1), "a .slot() view"),
            (lambda: Block("R", TagType.INT, 1, 2).slot(1, 2), "a .slot() view"),
            (lambda: float("inf"), "inf"),
            (lambda: float("nan"), "nan"),
            (lambda: b"7", "b'7'"),
        ],
        ids=[
            "structure",
            "timer",
            "udt",
            "udt-instance",
            "named-array",
            "block",
            "range",
            "slot",
            "slots",
            "inf",
            "nan",
            "bytes",
        ],
    )
    def test_refuses_a_value_that_generated_code_cannot_hold(self, value, shown):
        hw = P1AM()
        hw.slot(1, "P1-08SIM")
        with Program(strict=False) as prog:
            with rung():
                # copy() refuses a range when the program is built, so a range goes to math().
                (math if shown.startswith("a range") else copy)(value(), Char("Text"))
        line = prog.rungs[0]._instructions[0].source_line

        message = rf"\(\) at .*test_codegen\.py:{line}\. It reads {re.escape(shown)} as a value\."
        with pytest.raises(NotImplementedError, match=message):
            generate_circuitpy(prog, hw, target_scan_ms=10.0)

    def test_refuses_a_value_in_a_contact_and_names_the_rung(self):
        hw = P1AM()
        hw.slot(1, "P1-08SIM")
        # Stepping keeps a StrEnum member as the member, so the code.py cannot hold it.
        with Program(strict=False) as prog:
            with rung(Char("Text") == _Name.SEVEN):
                out(Bool("Running"))
        line = prog.rungs[0].source_line

        message = (
            rf"the rung at .*test_codegen\.py:{line}\. It reads _Name\.SEVEN as a value\. Pass"
        )
        with pytest.raises(NotImplementedError, match=message):
            generate_circuitpy(prog, hw, target_scan_ms=10.0)

    def test_modbus_requires_tag_map(self):
        hw = P1AM()
        hw.slot(1, "P1-08SIM")
        prog = Program(strict=False)

        with pytest.raises(ValueError, match="tag_map is required"):
            generate_circuitpy(
                prog,
                hw,
                target_scan_ms=10.0,
                modbus_server=ModbusServerConfig(ip="192.168.1.200"),
            )

    def test_accepts_tag_map_without_modbus(self):
        hw = P1AM()
        hw.slot(1, "P1-08SIM")
        prog = Program(strict=False)
        c = ClickBlocks().c
        mapping = TagMap({Bool("Mapped"): c[1]})

        source = generate_circuitpy(prog, hw, target_scan_ms=10.0, tag_map=mapping).code
        assert "def _run_main_rungs():" in source

    def test_server_serves_mapped_tags_no_rung_uses(self):
        hw = P1AM()
        hw.slot(1, "P1-08SIM")
        prog = Program(strict=False)
        c = ClickBlocks().c
        mapping = TagMap({Bool("Mapped"): c[1]}, include_system=False)

        source = generate_circuitpy(
            prog,
            hw,
            target_scan_ms=10.0,
            modbus_server=ModbusServerConfig(ip="192.168.1.200"),
            tag_map=mapping,
        ).code
        assert 'if bank == "C" and index == 1:' in source

    def test_server_serves_raw_registers_a_rung_uses(self):
        hw = P1AM()
        hw.slot(1, "P1-08SIM")
        blocks = ClickBlocks()
        with Program(strict=False) as prog:
            with rung():
                copy(5, blocks.ds[3901])
        mapping = TagMap({Bool("Mapped"): blocks.c[1]}, include_system=False)

        source = generate_circuitpy(
            prog,
            hw,
            target_scan_ms=10.0,
            modbus_server=ModbusServerConfig(ip="192.168.1.200"),
            tag_map=mapping,
        ).code
        assert 'if bank == "DS" and index == 3901:' in source
        assert 'if bank == "DS" and index == 3902:' not in source

    def test_rejects_empty_and_non_contiguous_slots(self):
        empty_hw = P1AM()
        prog = Program(strict=False)
        with pytest.raises(ValueError, match="at least one configured slot"):
            generate_circuitpy(prog, empty_hw, target_scan_ms=10.0, runstop=None)

        hw = P1AM()
        hw.slot(1, "P1-08SIM")
        hw.slot(3, "P1-08TRS")
        with pytest.raises(ValueError, match="contiguous"):
            generate_circuitpy(prog, hw, target_scan_ms=10.0, runstop=None)

    def test_allows_zero_slot_codegen_when_board_tags_are_used(self):
        hw = P1AM()
        with Program(strict=False) as prog:
            with rung(board.switch):
                out(board.led)
        source_code = generate_circuitpy(prog, hw, target_scan_ms=10.0).code
        assert "base.rollCall" not in source_code
        assert "P1AM.Base()" not in source_code
        assert "_SLOT_MODULES = []" in source_code
        assert "import digitalio" in source_code

    def test_function_call_requires_inspectable_source(self):
        hw = P1AM()
        hw.slot(1, "P1-08SIM")

        with Program(strict=False) as prog:
            with rung(Bool("Enable")):
                run_function(abs)

        with pytest.raises(ValueError, match="cannot read the source of"):
            generate_circuitpy(prog, hw, target_scan_ms=10.0)

    def test_strict_validation_blocks_io_untracked_findings(self):
        hw = P1AM()
        outputs = hw.slot(1, "P1-08TRS")
        light = outputs[1]
        external = InputBlock("External", TagType.BOOL, 1, 8)[1]

        with Program(strict=False) as prog:
            with rung(external):
                out(light)

        with pytest.raises(ValueError, match="CPY_IO_BLOCK_UNTRACKED"):
            generate_circuitpy(prog, hw, target_scan_ms=10.0)

    def test_strict_validation_keeps_advisories_non_blocking(self):
        hw = P1AM()
        hw.slot(1, "P1-08SIM")
        dest = Int("Dest")

        def fn():
            return {"result": 1}

        with Program(strict=False) as prog:
            with rung(Bool("Enable")):
                on_delay(Timer[1], preset=5, unit="Tms")
                run_function(fn, outs={"result": dest})

        source_code = generate_circuitpy(prog, hw, target_scan_ms=10.0).code
        assert "def _run_main_rungs():" in source_code


class TestDeterministicOutput:
    def test_same_inputs_generate_identical_output(self):
        hw, di, do = _basic_hw()
        with Program(strict=False) as prog:
            with rung(di[1]):
                out(do[1])

        s1 = generate_circuitpy(prog, hw, target_scan_ms=10.0, watchdog_ms=1000).code
        s2 = generate_circuitpy(prog, hw, target_scan_ms=10.0, watchdog_ms=1000).code
        assert s1 == s2


class TestEdgeSiteMemory:
    def test_no_edge_conditions_emit_no_edge_memory(self):
        hw = P1AM()
        hw.slot(1, "P1-08SIM")
        with Program(strict=False) as prog:
            with rung(Bool("Enable")):
                out(Bool("Light"))

        source_code = generate_circuitpy(prog, hw, target_scan_ms=10.0).code
        assert "_edge:" not in source_code
        assert "_prev" not in source_code

    def test_each_edge_contact_keeps_its_own_memory(self):
        hw = P1AM()
        hw.slot(1, "P1-08SIM")
        sensor = Bool("Car_Sensor")
        log_enable = Bool("Car_LogEnable")
        aux = Bool("Aux")
        light = Bool("Light")

        with Program(strict=False) as prog:
            with rung(RisingEdgeCondition(sensor)):
                out(light)
            with rung(BitCondition(log_enable)):
                out(aux)
            with rung(FallingEdgeCondition(log_enable)):
                out(light)

        source_code = generate_circuitpy(prog, hw, target_scan_ms=10.0).code
        # One memory per contact, stored when its row is scanned.
        assert source_code.count("    _mem['_edge:e1'] = ") == 1
        assert source_code.count("    _mem['_edge:e2'] = ") == 1
        assert "_edge:e3" not in source_code
        assert "_prev" not in source_code

    @pytest.mark.parametrize("edge", [False, True], ids=["level_leg", "edge_leg"])
    def test_bare_body_helper_leg_reads_its_own_row_on_every_backend(self, monkeypatch, edge):
        """A bare For-body instruction is a row of its own.

        In CLICK it is a wire-only rung between For and Next. The reset leg reads Y as the body
        row above it latched in the same pass, whether or not the leg has an edge contact.
        """
        from pyrung.core import Or, latch, rise

        y = Bool("Y")
        z = Bool("Z")
        cnt = Counter.clone("Cnt")
        leg = Or(y, rise(z)) if edge else y
        with Program(strict=False) as prog:
            with rung():
                with forloop(2):
                    latch(y)
                    count_up(cnt, 100).reset(leg)

        results = _on_every_backend(prog, monkeypatch, (y, cnt.Acc), scans=1)
        assert results == {name: (True, 0) for name in results}


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
            (RisingEdgeCondition(Bool("A")), "_now and not "),
            (FallingEdgeCondition(Bool("A")), "_was and not "),
        ],
    )
    def test_condition_mappings(self, condition, needle):
        light = Bool("Light")
        hw = P1AM()
        hw.slot(1, "P1-08SIM")
        with Program(strict=False) as prog:
            with rung(condition):
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
        # A domain error gives 0, so sqrt goes through _permissive_math. In a Math,
        # _permissive_math appends SC46 to _calc_flags. A function with no domain error uses
        # _math_func. Outside a Math, a division by a variable goes through _permissive_op.
        # Inside a Math (calc_form), the division tests the divisor inline.
        from pyrung.core.instruction.calc import calc_form

        assert "_math_func(math.sin, " in compile_expression(sin(a), ctx, strict=True)
        assert "_permissive_math(math.sqrt, _t_A)" in compile_expression(sqrt(a), ctx)
        assert "_permissive_math(math.sqrt, _t_A, _calc_flags)" in compile_expression(
            sqrt(a), ctx, strict=True
        )
        assert "_permissive_op('/', " in compile_expression(a / b, ctx)
        divided = compile_expression(calc_form(a / b, "decimal"), ctx, strict=True)
        assert divided == "((_t_A / _t_B) if _t_B else _term_fail(_calc_flags, 40))"
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
        source = Int("Source")
        calc_out = Int("CalcOut")
        reset_tag = Bool("Reset")
        ds = Block("DS", TagType.INT, 1, 10)
        dd = Block("DD", TagType.INT, 1, 10)
        idx = Int("Idx")

        with Program(strict=False) as prog:
            with rung(Bool("Enable")):
                on_delay(Timer[1], preset=5).reset(reset_tag)
            with rung(Bool("Enable")):
                off_delay(Timer2, preset=5)
            with rung(Bool("Enable")):
                count_up(Counter[1], preset=2).reset(reset_tag)
            with rung(Bool("Enable")):
                count_down(Counter2, preset=2).reset(reset_tag)
            with rung(Bool("Enable")):
                copy(40000, source)
                math(source + 1, calc_out)
                blockcopy(ds.select(1, 3), dd.select(1, 3))
                fill(7, dd.select(idx, idx + 2))

        source_code = generate_circuitpy(prog, hw, target_scan_ms=10.0).code
        assert "_frac:" in source_code
        assert "_int_units" in source_code
        assert "_delta" in source_code
        assert "_store_int(" in source_code
        assert "_wrap_int(" in source_code
        assert "blockcopy() needs ranges of the same length" in source_code
        assert "_blockcopy_values = []" in source_code
        assert "for _raw, _dst_idx in zip(_blockcopy_values," in source_code
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
            with rung(Bool("Enable")):
                search(ds.select(1, 10) >= 5, result=result, found=found, continuous=True)
                search(txt.select(1, 8) == "AB", result=result, found=found)
            with rung(Bool("Enable")):
                shift(bits.select(1, 8)).clock(clock).reset(reset_tag)
            with rung(Bool("Enable")):
                pack_bits(bits.select(1, 16), word)
                pack_words(words.select(1, 2), dword)
                pack_text(txt.select(1, 8), dword, allow_whitespace=True)
                unpack_to_bits(dword, bits.select(1, 32))
                unpack_to_words(dword, words.select(1, 2))
                with forloop(loop_count, oneshot=True) as lp:
                    copy(lp.idx + 1, target)

        source_code = generate_circuitpy(prog, hw, target_scan_ms=10.0).code
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

    def test_time_event_drum_codegen_compiles(self):
        hw = P1AM()
        hw.slot(1, "P1-08SIM")
        with Program(strict=False) as prog:
            with rung(Bool("Enable", default=True)):
                time_event_drum(
                    outputs=[Bool("Out1"), Bool("Out2")],
                    presets=[100, None],
                    events=[None, Bool("Event2")],
                    pattern=[[1, 0], [0, 1]],
                    current_step=Int("Step"),
                    accumulator=Int("Acc"),
                    completion_flag=Bool("Done"),
                ).reset(Bool("ResetCmd"))

        result = generate_circuitpy(prog, hw, target_scan_ms=10.0, watchdog_ms=None, runstop=None)

        compile(result.code, "code.py", "exec")
        assert "_end_met" in result.code
        assert "_frac:" in result.code

    def test_off_delay_codegen_keeps_timing_at_done_false_acc_zero(self):
        """The TOF rests at Done False, Acc 0 only until it is first enabled.

        The generated code tracks this with ``_tof_armed:<Acc>``, as the interpreter does.
        """
        hw = P1AM()
        hw.slot(1, "P1-08SIM")
        with Program(strict=False) as prog:
            with rung(Bool("Enable")):
                off_delay(Timer2, preset=0)

        result = generate_circuitpy(prog, hw, target_scan_ms=10.0, watchdog_ms=None, runstop=None)

        compile(result.code, "code.py", "exec")
        assert '_mem.get("_tof_armed:Timer2_Acc", False)' in result.code
        assert "if _tof_armed or " in result.code
        assert '_mem["_tof_armed:Timer2_Acc"] = True' in result.code

    def test_out_coil_variants_codegen_compiles(self):
        hw = P1AM()
        hw.slot(1, "P1-08SIM")
        timers = Timer.clone("OutT", count=5)
        with Program(strict=False) as prog:
            with rung(Bool("Enable", default=True)):
                toggle(Bool("Lamp"))
                debounce(Bool("Valve"), on=(timers[1], 100), off=(timers[2], 50))
                flasher(Bool("Beacon"), on=(timers[3], 500), off=(timers[4], 500), unit="sec")
                timed(Bool("Horn"), timers[5], 1000, maintained=True)

        result = generate_circuitpy(prog, hw, target_scan_ms=10.0, watchdog_ms=None, runstop=None)

        compile(result.code, "code.py", "exec")
        assert "_toggle:" in result.code
        assert "_frac:OutT4_Acc" in result.code

    def test_event_and_time_drum_codegen_keys_and_precedence_smoke(self, monkeypatch):
        hw = P1AM()
        hw.slot(1, "P1-08SIM")
        enable = Bool("Enable", default=True)
        reset_cmd = Bool("ResetCmd", default=True)
        jump_cmd = Bool("JumpCmd", default=True)
        jog_cmd = Bool("JogCmd", default=True)
        step = Int("Step")
        acc = Int("Acc")
        done = Bool("Done")
        event1 = Bool("Event1")
        event2 = Bool("Event2")
        out1 = Bool("Out1")
        out2 = Bool("Out2")
        out3 = Bool("Out3")
        out4 = Bool("Out4")

        with Program(strict=False) as prog:
            with rung(enable):
                event_drum(
                    outputs=[out1, out2],
                    events=[event1, event2],
                    pattern=[[1, 0], [0, 1]],
                    current_step=step,
                    completion_flag=done,
                ).reset(reset_cmd).jump(jump_cmd, step=step).jog(jog_cmd)
            with rung(enable):
                time_drum(
                    outputs=[out1, out2, out3, out4],
                    presets=[0, 0, 0, 0],
                    pattern=[
                        [1, 0, 0, 0],
                        [0, 1, 0, 0],
                        [0, 0, 1, 0],
                        [0, 0, 0, 1],
                    ],
                    current_step=step,
                    accumulator=acc,
                    completion_flag=done,
                ).reset(reset_cmd).jump(jump_cmd, step=3).jog(jog_cmd)

        ctx = _context_for_program(prog, hw)
        step_symbol = ctx.symbol_for_tag(step)
        acc_symbol = ctx.symbol_for_tag(acc)
        out4_symbol = ctx.symbol_for_tag(out4)
        result = generate_circuitpy(prog, hw, target_scan_ms=10.0, watchdog_ms=None, runstop=None)
        source_code = result.code

        assert "_drum_event_prev:i" in source_code
        assert "_frac:" in source_code
        assert "_drum_jump_prev:i" in source_code
        assert "_drum_jog_prev:i" in source_code
        assert "_drum_rung_prev:i" in source_code

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

        namespace = _run_single_scan_source(
            source_code, monkeypatch, StubBase(), runtime_source=result.runtime
        )
        # Reset (on, with jump and jog) wins: step 1, every output off.
        assert namespace[step_symbol] == 1
        assert namespace[acc_symbol] == 0
        assert namespace[out4_symbol] is False

    def test_bank_mapped_block_shares_bank_storage(self):
        """A block mapped onto a bank is a window into it, not a second array.

        The block's registers and the bank's registers are the same memory, so an
        indirect ``dh[idx]`` read must see the block's configured defaults.
        """
        blocks = ClickBlocks()
        dh_bank = blocks.dh
        cfg = Block("ModeCfg", TagType.WORD, 1, 3)
        cfg.slot(1, name="Cfg_Prod", default=0x0000)
        cfg.slot(2, name="Cfg_Maint", default=0x1BE4)
        cfg.slot(3, name="Cfg_Manual", default=0x1FFF)
        idx = Int("Idx", default=3)
        picked = Int("Picked")
        clear = Bool("Clear")

        TagMap({cfg: dh_bank.select(201, 203)}, include_system=False)

        with Program(strict=False) as prog:
            with rung():
                copy(dh_bank[idx + 200], picked)  # indirect, through the bank
            with rung(clear):
                fill(0, cfg.select(1, 3))  # range, through the block

        ctx = CodegenContext.for_kernel(prog)

        # ModeCfg folds into the DH array — one storage, not two.
        assert id(cfg) in ctx.block_alias
        emitted = {b.logical_name for b in ctx.emitted_bindings()}
        assert "ModeCfg" not in emitted
        assert "DH" in emitted

        # Both names resolve to the same cell of the same array.
        assert ctx.symbol_for_tag(cfg[2]) == ctx.symbol_for_tag(dh_bank[202])

    def test_bank_mapped_block_indirect_through_both_sides_is_rejected(self):
        """Indirect through the block *and* its bank would split one memory in two."""
        blocks = ClickBlocks()
        ds_bank = blocks.ds
        cfg = Block("Cfg", TagType.INT, 1, 8)
        idx = Int("Idx", default=1)
        a = Int("A")
        b = Int("B")

        TagMap({cfg: ds_bank.select(100, 107)}, include_system=False)

        with Program(strict=False) as prog:
            with rung():
                copy(cfg[idx], a)
                copy(ds_bank[idx], b)

        with pytest.raises(ValueError, match="indirectly and also uses"):
            CodegenContext.for_kernel(prog)

    def test_compile_kernel_accepts_named_array_symbol_default(self):
        @named_array(Int, stride=2, readonly=True)
        class SortState:
            IDLE = 0
            RUNNING = 1

        state = Int("State", choices=SortState, default=SortState.IDLE)
        out_flag = Bool("Out")

        with Program(strict=False) as prog:
            with rung(state == SortState.IDLE):
                out(out_flag)

        compiled = compile_kernel(prog)
        assert compiled.referenced_tags["State"].default == 0

    def test_compile_kernel_blockless_static_block_access_uses_tags(self):
        ds = Block("DS", TagType.INT, 1, 4)
        out_tag = Int("Out")

        with Program(strict=False) as prog:
            with rung():
                copy(ds[3], out_tag)
                copy(7, ds[4])

        compiled = compile_kernel(prog, blockless=True)

        assert compiled.blockless is True
        assert "tags.get('DS3', 0)" in compiled.source
        assert "tags['DS4'] = " in compiled.source
        assert " = blocks[" not in compiled.source

    def test_compile_kernel_blockless_indirect_access_uses_name_tuples(self):
        ds = Block("DS", TagType.INT, 1, 4)
        idx = Int("Idx", external=True, min=1, max=4)
        out_tag = Int("Out")

        with Program(strict=False) as prog:
            with rung():
                copy(ds[idx], out_tag)
                copy(9, ds[idx])

        compiled = compile_kernel(prog, blockless=True)
        block_symbol = next(iter(compiled.indirect_block_info))
        helper_name = f"_resolve_index_{block_symbol.lstrip('_') or block_symbol}"
        read_helper_name = f"_read_index_{block_symbol.lstrip('_') or block_symbol}"
        names_symbol = f"{block_symbol}_names"

        assert compiled.blockless is True
        assert f"{names_symbol} = (" in compiled.source
        assert f"_cond_tag_elem(tags, {names_symbol}, {read_helper_name}(int(" in compiled.source
        assert f"tags[{names_symbol}[{helper_name}(int(" in compiled.source
        assert " = blocks[" not in compiled.source

    def test_compile_kernel_blockless_range_ops_use_tag_name_sequences(self):
        ds = Block("DS", TagType.INT, 1, 4)
        bits = Block("C", TagType.BOOL, 1, 4)
        result = Int("Result")
        found = Bool("Found")
        clock = Bool("Clock")
        reset_cmd = Bool("Reset")

        with Program(strict=False) as prog:
            with rung():
                fill(3, ds.select(1, 3))
                search(ds.select(1, 3) >= 3, result=result, found=found)
            with rung():
                shift(bits.select(1, 3)).clock(clock).reset(reset_cmd)

        compiled = compile_kernel(prog, blockless=True)

        assert compiled.blockless is True
        assert "('DS1', 'DS2', 'DS3')" in compiled.source
        assert "for _dst_idx in _fill" in compiled.source
        assert "tags[_dst_idx] =" in compiled.source
        assert "tags.get(_search" in compiled.source
        assert "_prev_values = [bool(tags.get(_idx, False)) for _idx in _shift" in compiled.source
        assert " = blocks[" not in compiled.source

    def test_compile_kernel_blockless_forloop_with_empty_disabled_body_compiles(self):
        enable = Bool("Enable", external=True)
        counter = Int("Counter", retentive=True, min=0, max=1)

        with Program(strict=False) as prog:
            with rung(enable):
                with forloop(counter):
                    copy(counter, counter)
            with rung(enable):
                copy(counter, counter)

        compiled = compile_kernel(prog, blockless=True)

        assert compiled.blockless is True
        assert "\n    else:\n    _cond_snap_" not in compiled.source

    def test_helper_legs_bind_at_their_box(self):
        """A box's helper legs bind just before its code, as the box starts.

        The legs read live values, so they see what the wires above the box wrote. A bare
        For-body box binds its legs the same way on each pass.
        """
        a, b, d, e = Bool("A"), Bool("B"), Bool("D"), Bool("E")
        c1, c2, c3 = Counter.clone("C1"), Counter.clone("C2"), Counter.clone("C3")
        with Program(strict=False) as prog:
            with rung(a):
                out(b)
            with rung(a, e).continued():
                count_up(c1, 3).reset(d)
            with rung(a).continued():
                count_up(c2, 3).reset(b)
            with rung(a):
                with forloop(2):
                    out(d)
                    count_up(c3, 3).reset(d)

        lines = [line.strip() for line in compile_kernel(prog).source.splitlines()]

        def at(prefix: str) -> int:
            return next(i for i, line in enumerate(lines) if line.startswith(prefix))

        legs = [i for i, line in enumerate(lines) if re.match(r"_box_\d+_snap_0 = ", line)]
        assert len(legs) == 3
        leg_c1, leg_c2, leg_c3 = legs
        assert at("_t_B = True") < leg_c1 < at("_t_C1_Acc = _acc") < leg_c2 < at("_t_C2_Acc = _acc")
        # The leg on B reads the B that the out() above wrote, because the box reads its pins
        # as it starts.
        assert lines[leg_c2].endswith(" = bool(_t_B)")
        assert at("_t_D = True") < leg_c3 < at("_t_C3_Acc = _acc")
        assert lines[leg_c3].endswith(" = bool(_t_D)")

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
            with rung(Bool("Enable")):
                run_function(plus_one, ins={"value": source}, outs={"result": dest})
                run_enabled_function(gated, ins={"value": source}, outs={"result": dest})
                call("worker")

            with subroutine("worker"):
                with rung(Bool("Stop")):
                    return_early()
                with rung(Bool("Enable")):
                    out(Bool("Light"))

        source_code = generate_circuitpy(prog, hw, target_scan_ms=10.0).code
        assert "def _sub_worker():" in source_code
        assert "return" in source_code
        assert "_fn_plus_one" in source_code
        assert "_fn_gated" in source_code
        assert "run_enabled_function" in source_code

    def test_a_function_reading_the_math_module_by_another_name_runs(self, monkeypatch):
        """code.py binds ``pymath`` for a function that reads the math module by that name.

        This file imports ``math as pymath`` because pyrung's math() hides the name ``math``.
        """
        source, dest = Int("Source"), Int("Dest")

        def floors_half(value):
            return {"result": pymath.floor(value / 2)}

        with Program(strict=False) as prog:
            with rung():
                copy(7, source)
                run_function(floors_half, ins={"value": source}, outs={"result": dest})

        results = _on_every_backend(prog, monkeypatch, (dest,), scans=1)
        assert results == {name: (3,) for name in results}

    @pytest.mark.parametrize("value", [14, 0x1E, 255, 0xFFFF])
    def test_a_constant_copied_into_dh_stores_the_same_on_every_backend(self, monkeypatch, value):
        """A constant whose hex spelling starts with E (14, 30) stores its number."""
        dh = ClickBlocks().dh
        with Program(strict=False) as prog:
            with rung():
                copy(value, dh[1])

        results = _on_every_backend(prog, monkeypatch, (dh[1],), scans=1)
        assert results == {name: (value,) for name in results}

    def test_unknown_instruction_type_raises(self):
        hw = P1AM()
        hw.slot(1, "P1-08SIM")

        class UnknownInstruction(Instruction):
            def execute(self, ctx, enabled):  # pragma: no cover - generation-only
                return

        with Program(strict=False) as prog:
            with rung(Bool("Enable")):
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
            with rung(And(Bool("Enable"), system.storage.sd.ready)):
                copy(5, step)
                out(board.save_memory_cmd)
                out(system.storage.sd.eject_cmd)
                out(system.storage.sd.delete_all_cmd)

        source_code = generate_circuitpy(prog, hw, target_scan_ms=10.0).code
        assert "busio.SPI(board.SD_SCK, board.SD_MOSI, board.SD_MISO)" in source_code
        assert "_MEMORY_TMP_PATH" in source_code
        assert "_MEMORY_BAK_PATH" in source_code
        assert "os.rename(_MEMORY_TMP_PATH, _MEMORY_PATH)" in source_code
        assert "os.rename(_MEMORY_PATH, _MEMORY_BAK_PATH)" in source_code
        assert "for _path in (_MEMORY_PATH, _MEMORY_TMP_PATH, _MEMORY_BAK_PATH):" in source_code
        assert "os.remove(_path)" in source_code
        assert 'payload = {"schema": _RET_SCHEMA, "values": values}' in source_code
        assert "_sd_error_code = 1" in source_code
        assert "_sd_error_code = 2" in source_code
        assert "_sd_error_code = 3" in source_code
        assert "_service_sd_commands()" in source_code
        assert "save_memory()" in source_code
        assert "storage.sd.save_cmd" not in source_code
        assert "_t_board_save_memory_cmd" in source_code
        assert "_t_storage_sd_eject_cmd" in source_code
        assert 'storage.umount("/sd")' in source_code
        assert "_sd_available = False" in source_code
        assert (
            "while True:\n"
            "    scan_start_ns = time.monotonic_ns()\n"
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

    def test_bak_recovery_in_load_memory(self):
        hw = P1AM()
        hw.slot(1, "P1-08SIM")
        step = Int("Step")

        with Program(strict=False) as prog:
            with rung():
                copy(1, step)

        source_code = generate_circuitpy(prog, hw, target_scan_ms=10.0).code
        # The NVM dirty flag path tries .bak.
        assert '_MEMORY_BAK_PATH = "/sd/memory.json.bak"' in source_code
        assert "open(_MEMORY_BAK_PATH" in source_code
        assert 'print("Retentive memory recovered from backup")' in source_code
        assert (
            'print("Retentive load skipped: interrupted save, no backup available")' in source_code
        )
        # The normal path falls back to .bak when the primary file fails.
        assert "Retentive memory loaded from backup" in source_code

    def test_auto_save_globals_emitted(self):
        hw = P1AM()
        hw.slot(1, "P1-08SIM")
        count = Int("Count")

        with Program(strict=False) as prog:
            with rung():
                copy(1, count)

        source_code = generate_circuitpy(prog, hw, target_scan_ms=10.0).code
        assert "_ret_snapshot = {}" in source_code
        assert "_ret_last_save_ns = 0" in source_code
        assert "_RET_AUTO_SAVE_NS = 30000000000" in source_code

    def test_auto_save_dirty_check_in_scan_loop(self):
        hw = P1AM()
        hw.slot(1, "P1-08SIM")
        count = Int("Count")

        with Program(strict=False) as prog:
            with rung():
                copy(1, count)

        source_code = generate_circuitpy(prog, hw, target_scan_ms=10.0).code
        assert "(scan_start_ns - _ret_last_save_ns) >= _RET_AUTO_SAVE_NS" in source_code
        assert '_ret_snapshot.get("Count")' in source_code
        assert "save_memory()" in source_code

    def test_save_memory_writes_an_inf_real_as_json(self):
        """A REAL holding Inf or NaN saves as its str, which is valid JSON.

        The load's float() parses that text.
        """
        import json

        hw = P1AM()
        hw.slot(1, "P1-08SIM")
        level = Real("Level")

        with Program(strict=False) as prog:
            with rung():
                copy(1.5, level)

        result = generate_circuitpy(prog, hw, target_scan_ms=10.0)
        ctx = _context_for_program(prog, hw)
        symbol = ctx.symbol_for_tag(level)
        save_section = result.code.split("def save_memory():", 1)[1].split("\ndef ", 1)[0]
        line = next(ln for ln in save_section.splitlines() if 'values["Level"] =' in ln)
        entry_expr = line.split("=", 1)[1].strip()
        for value in (1.5, pymath.inf, -pymath.inf, pymath.nan):
            entry = eval(entry_expr, {"str": str}, {symbol: value})  # noqa: S307
            loaded = float(json.loads(json.dumps(entry, allow_nan=False))["value"])
            assert loaded == value or (pymath.isnan(value) and pymath.isnan(loaded))

    def test_auto_save_dirty_check_treats_a_saved_nan_real_as_clean(self):
        """A REAL saved as NaN that is still NaN must not resave every interval.

        The check needs care because NaN != NaN.
        """
        hw = P1AM()
        hw.slot(1, "P1-08SIM")
        level = Real("Level")

        with Program(strict=False) as prog:
            with rung():
                copy(1.5, level)

        source_code = generate_circuitpy(prog, hw, target_scan_ms=10.0).code
        symbol = _context_for_program(prog, hw).symbol_for_tag(level)
        lines = source_code.splitlines()
        at = next(i for i, ln in enumerate(lines) if "_RET_AUTO_SAVE_NS:" in ln)
        dirty_line = lines[at + 1].strip()
        assert dirty_line.startswith("if ") and dirty_line.endswith(":")
        dirty_expr = dirty_line[3:-1]

        def dirty(now: float, saved: float | None) -> bool:
            return eval(dirty_expr, {}, {symbol: now, "_ret_snapshot": {"Level": saved}})  # noqa: S307

        nan = pymath.nan
        assert not dirty(nan, nan)
        assert not dirty(pymath.inf, pymath.inf)
        assert not dirty(1.5, 1.5)
        assert dirty(nan, 1.5)
        assert dirty(1.5, nan)
        assert dirty(nan, None)
        assert dirty(2.5, 1.5)

    def test_save_memory_updates_snapshot(self):
        hw = P1AM()
        hw.slot(1, "P1-08SIM")
        count = Int("Count")

        with Program(strict=False) as prog:
            with rung():
                copy(1, count)

        source_code = generate_circuitpy(prog, hw, target_scan_ms=10.0).code
        save_section = source_code.split("def save_memory():", 1)[1].split("\ndef ", 1)[0]
        assert "_ret_snapshot" in save_section
        assert "_ret_last_save_ns = time.monotonic_ns()" in save_section

    def test_runstop_save_on_stop_transition(self):
        hw = P1AM()
        hw.slot(1, "P1-08SIM")

        with Program(strict=False) as prog:
            pass

        source_code = generate_circuitpy(prog, hw, target_scan_ms=10.0).code
        # save_memory() must appear before print("Mode: STOP").
        stop_idx = source_code.index('print("Mode: STOP")')
        save_before_stop = source_code.rfind("save_memory()", 0, stop_idx)
        assert save_before_stop != -1

    def test_runstop_default(self):
        hw = P1AM()
        hw.slot(1, "P1-08SIM")

        with Program(strict=False) as prog:
            pass

        # The default includes the RunStopConfig behavior.
        source_code = generate_circuitpy(prog, hw, target_scan_ms=10.0).code
        assert "_mode_run" in source_code
        assert "_runstop_debounced" in source_code
        assert 'print("Mode: RUN")' in source_code
        assert 'print("Mode: STOP")' in source_code

        # An explicit runstop=None leaves it out.
        source_no_rs = generate_circuitpy(prog, hw, target_scan_ms=10.0, runstop=None).code
        assert "_runstop_debounced" not in source_no_rs

    def test_watchdog_and_scan_diagnostics_emit(self):
        hw, di, do = _basic_hw()
        with Program(strict=False) as prog:
            with rung(di[1]):
                out(do[1])

        source_code = generate_circuitpy(prog, hw, target_scan_ms=10.0, watchdog_ms=1000).code
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
            with rung(di[1]):
                out(do[1])
            with rung(combo_in[1]):
                out(combo_out[1])

        source_code = generate_circuitpy(prog, hw, target_scan_ms=10.0).code
        assert "base.readDiscrete(1)" in source_code
        assert "base.writeDiscrete(" in source_code
        assert "base.readTemperature(3, 1)" in source_code
        assert "base.writeAnalog(" in source_code
        assert "base.readDiscrete(5)" in source_code

    def test_branch_solved_where_it_sits_in_source_order(self):
        enable = Bool("Enable")
        branch_enable = Bool("BranchEnable")
        a = Bool("A")
        b = Bool("B")
        c = Bool("C")
        hw = P1AM()
        hw.slot(1, "P1-08SIM")
        with Program(strict=False) as prog:
            with rung(enable):
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
        # The solve reaches the branch after the row above it, and solves the branch then.
        assert a_idx < branch_idx < b_idx < c_idx

    @pytest.mark.parametrize(
        ("target", "expected"),
        [
            (Bool("ResetBool", default=True), "False"),
            (Int("ResetInt", default=7), "0"),
            (Real("ResetReal", default=2.5), "0.0"),
            (Char("ResetChar", default="A"), repr("\x00")),
        ],
    )
    def test_reset_emits_type_zero_independent_of_default(self, target, expected):
        enable = Bool("Enable")
        hw = P1AM()
        hw.slot(1, "P1-08SIM")
        with Program(strict=False) as prog:
            with rung(enable):
                reset(target)

        ctx = _context_for_program(prog, hw)
        lines = compile_rung(prog.rungs[0], "_run_main_rungs", ctx, indent=0)
        target_sym = ctx.symbol_for_tag(target)
        assert any(f"{target_sym} = {expected}" in line for line in lines)


class TestBoardPeripheralsAndRunStop:
    def test_board_peripheral_codegen_emits_wiring_and_neopixel_clamp(self):
        hw = P1AM()
        with Program(strict=False) as prog:
            with rung(board.switch):
                out(board.led)
            with rung(Bool("Enable", default=True)):
                copy(999, board.neopixel.r)
                copy(-3, board.neopixel.g)
                copy(128, board.neopixel.b)

        source_code = generate_circuitpy(prog, hw, target_scan_ms=10.0).code
        assert "import digitalio" in source_code
        assert "_board_switch_io = digitalio.DigitalInOut(board.SWITCH)" in source_code
        assert "_board_led_io = digitalio.DigitalInOut(board.LED)" in source_code
        assert "import neopixel" in source_code
        assert "_board_pixel = neopixel.NeoPixel(board.NEOPIXEL, 1, auto_write=True)" in source_code
        assert "_board_pixel[0] = (_pixel_r, _pixel_g, _pixel_b)" in source_code
        assert "max(0, min(255, int(" in source_code

    def test_runstop_emits_mapping_and_mode_tag_support(self):
        hw, di, do = _basic_hw()
        with Program(strict=False) as prog:
            with rung(di[1]):
                out(do[1])

        source_code = generate_circuitpy(
            prog,
            hw,
            target_scan_ms=10.0,
            runstop=RunStopConfig(debounce_ms=15),
        ).code
        assert "_runstop_initialized = False" in source_code
        assert "_runstop_debounced" in source_code
        assert "_reset_for_run_transition()" in source_code
        assert "_force_outputs_off()" in source_code
        assert "_t_sys_mode_run" in source_code
        assert "_t_sys_cmd_mode_stop" in source_code
        assert "_stop_cmd = _t_sys_cmd_mode_stop" in source_code

    def test_runstop_stop_mode_forces_outputs_off(self, monkeypatch):
        hw = P1AM()
        hw.slot(1, "P1-08SIM")
        outputs = hw.slot(2, "P1-08TRS")
        enable = Bool("Enable", default=True)

        with Program(strict=False) as prog:
            with rung(enable):
                out(outputs[1])

        result = generate_circuitpy(
            prog,
            hw,
            target_scan_ms=10.0,
            runstop=RunStopConfig(debounce_ms=0, run_when_high=True),
        )

        class StubBase:
            def __init__(self):
                self.discrete_writes: list[tuple[int, int]] = []

            def rollCall(self, modules):
                return None

            def readDiscrete(self, slot):
                return 0

            def writeDiscrete(self, value, slot):
                self.discrete_writes.append((slot, value))

            def readAnalog(self, slot, ch):
                return 0

            def writeAnalog(self, value, slot, ch):
                return None

            def readTemperature(self, slot, ch):
                return 0.0

        stub_base = StubBase()
        namespace = _run_single_scan_source(
            result.code, monkeypatch, stub_base, runtime_source=result.runtime
        )
        assert stub_base.discrete_writes[-1] == (2, 0)
        out_block_symbol = _context_for_program(prog, hw).symbol_for_block(outputs)
        out_values = _namespace_list(namespace, out_block_symbol)
        assert out_values[0] is False

    def test_runstop_run_when_high_false_keeps_runtime_in_run(self, monkeypatch):
        hw = P1AM()
        hw.slot(1, "P1-08SIM")
        outputs = hw.slot(2, "P1-08TRS")
        enable = Bool("Enable", default=True)

        with Program(strict=False) as prog:
            with rung(enable):
                out(outputs[1])

        result = generate_circuitpy(
            prog,
            hw,
            target_scan_ms=10.0,
            runstop=RunStopConfig(debounce_ms=0, run_when_high=False),
        )

        class StubBase:
            def __init__(self):
                self.discrete_writes: list[tuple[int, int]] = []

            def rollCall(self, modules):
                return None

            def readDiscrete(self, slot):
                return 0

            def writeDiscrete(self, value, slot):
                self.discrete_writes.append((slot, value))

            def readAnalog(self, slot, ch):
                return 0

            def writeAnalog(self, value, slot, ch):
                return None

            def readTemperature(self, slot, ch):
                return 0.0

        stub_base = StubBase()
        _run_single_scan_source(result.code, monkeypatch, stub_base, runtime_source=result.runtime)
        assert stub_base.discrete_writes[-1] == (2, 1)

    def test_real_overflow_stores_inf(self, monkeypatch):
        hw = P1AM()
        hw.slot(1, "P1-08SIM")
        big = Real("Big", default=3.0e38)
        dest = Real("Dest")
        low = Real("Low")
        dint = Dint("Huge", default=2147483647)
        copied = Real("Copied")
        square = Real("Square")
        cube = Real("Cube")
        neg = Real("Neg", default=-3.0e38)
        with Program(strict=False) as prog:
            with rung():
                math(big * 10, dest)
                math(big * -10, low)
                copy(dint, copied)
                math(big**2, square)
                math(neg**3, cube)

        result = generate_circuitpy(
            prog,
            hw,
            target_scan_ms=10.0,
            runstop=RunStopConfig(debounce_ms=0, run_when_high=False),
        )

        class StubBase:
            def rollCall(self, modules):
                return None

            def readDiscrete(self, slot):
                return 0

            def writeDiscrete(self, value, slot):
                return None

            def readAnalog(self, slot, ch):
                return 0

            def writeAnalog(self, value, slot, ch):
                return None

            def readTemperature(self, slot, ch):
                return 0.0

        namespace = _run_single_scan_source(
            result.code, monkeypatch, StubBase(), runtime_source=result.runtime
        )
        ctx = _context_for_program(prog, hw)
        assert namespace[ctx.symbol_for_tag(dest)] == pymath.inf
        assert namespace[ctx.symbol_for_tag(low)] == -pymath.inf
        assert namespace[ctx.symbol_for_tag(copied)] == 2147483647.0
        assert namespace[ctx.symbol_for_tag(square)] == pymath.inf
        assert namespace[ctx.symbol_for_tag(cube)] == -pymath.inf
        assert namespace[ctx.symbol_for_tag(system.fault.out_of_range)] is True

    @pytest.mark.parametrize(
        ("label", "make"),
        [
            ("and", lambda r: r & 1),
            ("or", lambda r: 1 | r),
            ("xor", lambda r: r ^ 3),
            ("lshift", lambda r: r << 1),
            ("rshift", lambda r: 8 >> r),
            ("invert", lambda r: ~r),
            ("lsh", lambda r: lsh(r, 1)),
            ("rsh", lambda r: rsh(4, r)),
            ("lro", lambda r: lro(r, 1)),
            ("rro", lambda r: rro(1, r)),
            ("pow_then_and", lambda r: (r**2) & 1),
        ],
    )
    @pytest.mark.parametrize("operand", ["nan", "inf"])
    def test_non_finite_bitwise_operand_is_zero_and_sc43_on_every_backend(
        self, monkeypatch, label, make, operand
    ):
        """A NaN or Inf REAL in a bitwise or shift op stores 0 and sets SC43.

        The scan runs on. This holds on the core interpreter, the compiled kernel and the
        generated CircuitPython.
        """
        big = Real("Big", default=3.0e38)
        inf = Real("InfR")
        odd = Real("OddR")
        dest = Int("Dest", default=7)
        after = Bool("After")
        with Program(strict=False) as prog:
            # Scan 0 builds the operand and sets SC43. Scan 1 starts with SC43 off,
            # so the flag on scan 1 comes from the bitwise op.
            with rung(system.sys.first_scan):
                math(big * 10, inf)
                math(inf - inf if operand == "nan" else inf + 0, odd)
            with rung(~system.sys.first_scan):
                math(make(odd), dest)
                out(after)

        results = _on_every_backend(
            prog, monkeypatch, (dest, system.fault.out_of_range, after), scans=2
        )
        assert results == {name: (0, True, True) for name in results}, label

    @staticmethod
    def _non_finite_real(operand: str) -> tuple[Real, list]:
        """Return a Real that scan 0 sets to +Inf, -Inf or NaN, and the Math instructions that set it.

        SC43 is set on scan 0 only. A REAL literal cannot be Inf or NaN, so Math instructions build it.
        """
        big = Real("Big", default=3.0e38)
        inf = Real("InfR")
        r = Real("R", default=3.0)
        made = {"inf": inf + 0, "-inf": 0 - inf, "nan": inf - inf}[operand]
        return r, [(big * 10, inf), (made, r)]

    @pytest.mark.parametrize("operand,flagged", [("nan", False), ("inf", True), ("-inf", True)])
    @pytest.mark.parametrize("dest_tag", [Int, Dint, Word])
    def test_math_non_finite_into_an_integer_is_zero_on_every_backend(
        self, monkeypatch, operand, flagged, dest_tag
    ):
        """A Math of +/-Inf into an integer stores 0 and sets SC43.

        A Math of NaN stores 0 and sets no flag.
        """
        r, setup = self._non_finite_real(operand)
        dest = dest_tag("Dest", default=7)
        with Program(strict=False) as prog:
            with rung(system.sys.first_scan):
                for expr, target in setup:
                    math(expr, target)
            with rung(~system.sys.first_scan):
                math(r * 2, dest)

        results = _on_every_backend(prog, monkeypatch, (dest, system.fault.out_of_range), scans=2)
        assert results == {name: (0, flagged) for name in results}

    @pytest.mark.parametrize("operand,flagged", [("nan", False), ("inf", True), ("-inf", True)])
    def test_math_non_finite_into_a_real_is_kept_on_every_backend(
        self, monkeypatch, operand, flagged
    ):
        """A Math of +/-Inf into a Real keeps the Real and sets SC43.

        A Math of NaN keeps the Real and sets no flag.
        """
        r, setup = self._non_finite_real(operand)
        dest = Real("Dest", default=7.0)
        with Program(strict=False) as prog:
            with rung(system.sys.first_scan):
                for expr, target in setup:
                    math(expr, target)
            with rung(~system.sys.first_scan):
                math(r * 2, dest)

        results = _on_every_backend(prog, monkeypatch, (dest, system.fault.out_of_range), scans=2)
        for stored, flag in results.values():
            assert repr(stored) == repr(
                {"nan": pymath.nan, "inf": pymath.inf, "-inf": -pymath.inf}[operand]
            )
            assert flag is flagged

    @pytest.mark.parametrize(
        "source,stored",
        [
            ("1.5", 1.5),
            ("1e40", pymath.inf),
            ("-1e40", -pymath.inf),
            ("nan", pymath.nan),
            (Char("Digit", default="7"), 7.0),
            (Char("Seven", default="7") + Char("Two", default="2"), 72.0),
            (Char("Seven", default="7") * 2, 77.0),
        ],
        ids=["1.5", "1e40", "-1e40", "nan", "Char tag", "Char plus Char", "Char times 2"],
    )
    def test_math_of_text_into_a_real_is_its_number_on_every_backend(
        self, monkeypatch, source, stored
    ):
        """A Math of text into a Real stores what float() makes of the text, and sets no flag.

        Past the REAL range the text stores +/-Inf.
        """
        dest = Real("Dest", default=3.0)
        with Program(strict=False) as prog:
            with rung():
                math(source, dest)

        results = _on_every_backend(prog, monkeypatch, (dest, system.fault.out_of_range), scans=1)
        for value, flag in results.values():
            assert repr(value) == repr(stored)
            assert flag is False

    @pytest.mark.parametrize(
        "value,dest_tag,stored",
        [(1.5, Dint, 1), (-1.5, Dint, -1), (40000, Real, 40000.0), (-40000, Real, -40000.0)],
    )
    def test_to_binary_of_a_constant_into_a_number_stores_its_value_on_every_backend(
        self, monkeypatch, value, dest_tag, stored
    ):
        """A CLICK was read storing 1 for 1.5 into a DD, and 40000.0 for 40000 into a DF."""
        dest = dest_tag("Dest", default=9)
        with Program(strict=False) as prog:
            with rung():
                copy(value, dest, convert=to_binary)

        results = _on_every_backend(prog, monkeypatch, (dest, system.fault.out_of_range), scans=1)
        assert {name: (repr(v), flag) for name, (v, flag) in results.items()} == {
            name: (repr(stored), False) for name in results
        }

    @pytest.mark.parametrize("dest_tag", [Dint, Real], ids=["dint", "real"])
    def test_to_binary_of_55_into_a_number_is_refused(self, dest_tag):
        """CLICK's Copy dialog offers no Copy Binary from 55 into a DD or a DF."""
        with pytest.raises(
            ValueError, match=r"^copy\(\) got convert=to_binary with the constant 55"
        ):
            with Program(strict=False):
                with rung():
                    copy(55, dest_tag("Dest"), convert=to_binary)

    @pytest.mark.parametrize(
        "build,lo,hi,at,flagged",
        [
            (lambda b, w, d, lo, hi, at: pack_bits(b.select(lo, hi), d[at]), 0, 2, 5, False),
            (lambda b, w, d, lo, hi, at: pack_bits(b.select(lo, hi), d[at]), 3, 1, 0, True),
            (lambda b, w, d, lo, hi, at: unpack_to_bits(d[at], b.select(lo, hi)), 3, 1, 0, True),
            (lambda b, w, d, lo, hi, at: unpack_to_words(d[at], w.select(lo, hi)), 1, 3, 0, True),
            (
                lambda b, w, d, lo, hi, at: blockcopy(w.select(lo, hi), d.select(at, hi)),
                3,
                1,
                0,
                True,
            ),
        ],
        ids=[
            "pack_bits into past the end",
            "pack_bits of a backwards range into below",
            "unpack_to_bits from below into a backwards range",
            "unpack_to_words from below into three words",
            "blockcopy of a backwards range into below",
        ],
    )
    def test_a_pointer_outside_its_bank_is_checked_first_on_every_backend(
        self, monkeypatch, build, lo, hi, at, flagged
    ):
        """Each backend checks the pointers in stepping's order.

        The first pointer outside its bank decides the result.
        """
        bits = Block("Bit", TagType.BOOL, 1, 4)
        words = Block("Word", TagType.INT, 1, 4)
        dints = Block("Dint", TagType.DINT, 1, 4)
        pointers = (Int("Lo", default=lo), Int("Hi", default=hi), Int("At", default=at))
        with Program(strict=False) as prog:
            with rung():
                build(bits, words, dints, *pointers)

        results = _on_every_backend(prog, monkeypatch, (system.fault.address_error,), scans=1)
        assert results == {name: (flagged,) for name in results}

    def test_an_int_enum_member_is_its_number_on_every_backend(self, monkeypatch):
        mode = Int("Mode", default=1)
        running = Bool("Running")
        copied = Int("Copied")
        total = Int("Total")
        with Program(strict=False) as prog:
            with rung(mode == _Mode.RUN):
                out(running)
                copy(_Mode.RUN, copied)
                math(mode + _Mode.RUN, total)

        results = _on_every_backend(prog, monkeypatch, (running, copied, total), scans=1)
        assert results == {name: (True, 1, 2) for name in results}

    @pytest.mark.parametrize(
        "operand,dest_tag,stored,flagged",
        [
            ("inf", Int, 32767, True),
            ("-inf", Int, -32768, True),
            ("nan", Int, 0, False),
            ("inf", Dint, 2147483647, False),
            ("-inf", Dint, -2147483648, False),
            ("nan", Dint, -2147483648, False),
            ("inf", Word, 65535, False),
            ("-inf", Word, 32768, False),
            ("nan", Word, 0, False),
        ],
    )
    def test_copy_non_finite_on_every_backend(
        self, monkeypatch, operand, dest_tag, stored, flagged
    ):
        """A Copy of +/-Inf clamps and sets SC43 on an INT.

        It clamps with no flag on a DINT, as it does for a Real past the DINT range
        (``flag_probe/bulk_ops_clear_flags``). It also clamps with no flag on a WORD
        (CLICK: 65535 and 32768). A Copy of NaN sets no flag. It stores -2147483648 in a
        DINT and 0 in an INT or a WORD.
        """
        r, setup = self._non_finite_real(operand)
        dest = dest_tag("Dest", default=7)
        with Program(strict=False) as prog:
            with rung(system.sys.first_scan):
                for expr, target in setup:
                    math(expr, target)
            with rung(~system.sys.first_scan):
                copy(r, dest)

        results = _on_every_backend(prog, monkeypatch, (dest, system.fault.out_of_range), scans=2)
        assert results == {name: (stored, flagged) for name in results}

    @pytest.mark.parametrize(
        "source_tag,value,dest_tag,stored,flagged",
        [
            (Dint, 40000, Int, 32767, True),
            (Dint, -40000, Int, -32768, True),
            (Dint, 32767, Int, 32767, False),
            (Real, 32767.5, Int, 32767, False),
            (Real, -32768.5, Int, -32768, False),
            (Real, 32768.0, Int, 32767, True),
            (Real, -32769.0, Int, -32768, True),
            (Real, 1.0e10, Dint, 2147483647, False),
            (Dint, 70000, Word, 65535, False),
            (Dint, -1, Word, 65535, False),
            (Word, 65535, Int, -1, False),
        ],
    )
    def test_copy_clamp_flag_on_every_backend(
        self, monkeypatch, source_tag, value, dest_tag, stored, flagged
    ):
        """A Copy sets SC43 when the store stops at a limit.

        It sets no flag on a truncation, a wrap, a WORD's clamp, or a REAL's clamp into a DINT
        (CLICK: DF 1.0E+10 into a DD).
        """
        source = source_tag("Source", default=value)
        dest = dest_tag("Dest", default=7)
        with Program(strict=False) as prog:
            with rung():
                copy(source, dest)

        results = _on_every_backend(prog, monkeypatch, (dest, system.fault.out_of_range), scans=1)
        assert results == {name: (stored, flagged) for name in results}

    @pytest.mark.parametrize("literal,stored,flagged", [(40000, 32767, True), (5, 5, False)])
    def test_copy_literal_clamp_flag_on_every_backend(self, monkeypatch, literal, stored, flagged):
        """A literal source clamps and sets the flag, as a tag source does."""
        dest = Int("Dest", default=7)
        with Program(strict=False) as prog:
            with rung():
                copy(literal, dest)

        results = _on_every_backend(prog, monkeypatch, (dest, system.fault.out_of_range), scans=1)
        assert results == {name: (stored, flagged) for name in results}

    @pytest.mark.parametrize(
        "values,flagged", [((40000, 5), True), ((-40000, 5), True), ((1, 5), False)]
    )
    def test_blockcopy_clamp_flag_on_every_backend(self, monkeypatch, values, flagged):
        """A Block Copy raises SC43 if any element clamps, and stores every element."""
        source = Block("SrcB", TagType.DINT, 1, 2)
        dest = Block("DstB", TagType.INT, 1, 2)
        first, second = Int("First"), Int("Second")
        seen = Bool("Seen")
        with Program(strict=False) as prog:
            with rung(system.sys.first_scan):
                copy(values[0], source[1])
                copy(values[1], source[2])
            with rung(~system.sys.first_scan):
                blockcopy(source.select(1, 2), dest.select(1, 2))
            with rung(system.fault.out_of_range):
                out(seen)
            with rung(~system.sys.first_scan):
                copy(dest[1], first)
                copy(dest[2], second)

        watch = (first, second, seen)
        results = _on_every_backend(prog, monkeypatch, watch, scans=2)
        expected = (max(-32768, min(32767, values[0])), values[1], flagged)
        assert results == {name: expected for name in results}

    def test_blockcopy_real_into_dint_clamps_with_no_flag_on_every_backend(self, monkeypatch):
        """A Real clamps into a Dint with no SC43, as a single Copy does (CLICK)."""
        source = Block("SrcR", TagType.REAL, 1, 2)
        dest = Block("DstD", TagType.DINT, 1, 2)
        first, second = Dint("First"), Dint("Second")
        seen = Bool("Seen")
        with Program(strict=False) as prog:
            with rung(system.sys.first_scan):
                copy(1.0e10, source[1])
                copy(-1.0e10, source[2])
            with rung(~system.sys.first_scan):
                blockcopy(source.select(1, 2), dest.select(1, 2))
            with rung(system.fault.out_of_range):
                out(seen)
            with rung(~system.sys.first_scan):
                copy(dest[1], first)
                copy(dest[2], second)

        results = _on_every_backend(prog, monkeypatch, (first, second, seen), scans=2)
        assert results == {name: (2147483647, -2147483648, False) for name in results}

    def test_fill_clamps_and_raises_the_flag_on_every_backend(self, monkeypatch):
        """A Fill stops at the limit and sets SC43, as the clamp of a single Copy does."""
        source = Dint("Source", default=40000)
        dest = Block("DstB", TagType.INT, 1, 2)
        first, second = Int("First"), Int("Second")
        seen = Bool("Seen")
        with Program(strict=False) as prog:
            with rung():
                fill(source, dest.select(1, 2))
            with rung(system.fault.out_of_range):
                out(seen)
            with rung():
                copy(dest[1], first)
                copy(dest[2], second)

        results = _on_every_backend(prog, monkeypatch, (first, second, seen), scans=1)
        assert results == {name: (32767, 32767, True) for name in results}

    @staticmethod
    def _fill_into_text(value) -> tuple[Program, tuple]:
        """Return a program that fills *value* into TXT 1..3 of four, and the tags to watch.

        Scan 0 writes "Z" into each TXT. Scan 1 runs the Fill. The watched codes are the
        ASCII codes of the four TXT, then whether the Fill set the out-of-range flag.
        """
        chars = Block("Txt", TagType.CHAR, 1, 4)
        codes = [Int(f"Code{i}") for i in range(1, 5)]
        seen = Bool("Seen")
        with Program(strict=False) as prog:
            with rung(system.sys.first_scan):
                fill("Z", chars.select(1, 4))
            with rung(~system.sys.first_scan):
                fill(value, chars.select(1, 3))
            with rung(system.fault.out_of_range):
                out(seen)
            with rung(~system.sys.first_scan):
                for i, code in enumerate(codes, start=1):
                    copy(chars[i], code, convert=to_ascii)
        return prog, (*codes, seen)

    @pytest.mark.parametrize(
        "value,digit",
        [
            pytest.param(5, "5", id="5"),
            pytest.param(0, "0", id="0"),
            pytest.param(True, "1", id="True"),
            pytest.param(False, "0", id="False"),
        ],
    )
    def test_fill_of_a_digit_into_txt_writes_its_character_on_every_backend(
        self, monkeypatch, value, digit
    ):
        """A CLICK was read filling 5 into TXT1..TXT3 as the character "5", with no flag.

        The export writes True and False as 1 and 0.
        """
        prog, watch = self._fill_into_text(value)
        results = _on_every_backend(prog, monkeypatch, watch, scans=2)
        assert results == {name: (*[ord(digit)] * 3, ord("Z"), False) for name in results}

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param(12, id="12"),
            pytest.param(-5, id="-5"),
        ],
    )
    def test_fill_of_text_longer_than_a_character_flags_on_every_backend(self, monkeypatch, value):
        """Each TXT holds one character, so the Fill writes nothing and sets the flag.

        A CLICK's editor refuses a Fill of 12 or -5 into a TXT.
        """
        prog, watch = self._fill_into_text(value)
        results = _on_every_backend(prog, monkeypatch, watch, scans=2)
        assert results == {name: (*[ord("Z")] * 4, True) for name in results}

    @pytest.mark.parametrize(
        "seed,source_type,target_type,stored",
        [
            (2.5, Real, Dint, 1075838976),
            (-2.5, Real, Dint, -1071644672),
            (1075838976, Dint, Real, 2.5),
            (-1071644672, Dint, Real, -2.5),
        ],
    )
    def test_copy_binary_between_a_real_and_a_dint_moves_the_bits_on_every_backend(
        self, monkeypatch, seed, source_type, target_type, stored
    ):
        """A CLICK was read moving DF 2.5 into DD as 1075838976 and back, with no flag."""
        source = source_type("Source")
        target = target_type("Target")
        seen = Bool("Seen")
        with Program(strict=False) as prog:
            with rung(system.sys.first_scan):
                copy(seed, source)
            with rung(~system.sys.first_scan):
                copy(source, target, convert=to_binary)
            with rung(system.fault.out_of_range):
                out(seen)

        results = _on_every_backend(prog, monkeypatch, (target, seen), scans=2)
        assert results == {name: (stored, False) for name in results}

    @pytest.mark.parametrize(
        "seed,source_type,target_type,stored",
        [
            (2.5, TagType.REAL, TagType.DINT, 1075838976),
            (-1071644672, TagType.DINT, TagType.REAL, -2.5),
        ],
        ids=["df-to-dd", "dd-to-df"],
    )
    def test_copy_binary_between_pointers_moves_the_bits_on_every_backend(
        self, monkeypatch, seed, source_type, target_type, stored
    ):
        """CLICK's dialog offers Copy Binary for DF[DS1] -> DD[DS1], so a pointer form builds."""
        sources = Block("Sources", source_type, 1, 3)
        targets = Block("Targets", target_type, 1, 3)
        ptr = Int("Ptr", default=2)
        stored_tag = (Dint if target_type is TagType.DINT else Real)("Stored")
        seen = Bool("Seen")
        with Program(strict=False) as prog:
            with rung(system.sys.first_scan):
                copy(seed, sources[2])
            with rung(~system.sys.first_scan):
                copy(sources[ptr], targets[ptr], convert=to_binary)
            with rung(system.fault.out_of_range):
                out(seen)
            with rung(~system.sys.first_scan):
                copy(targets[2], stored_tag)

        results = _on_every_backend(prog, monkeypatch, (stored_tag, seen), scans=2)
        assert results == {name: (stored, False) for name in results}

    @pytest.mark.parametrize("exponential", [False, True])
    @pytest.mark.parametrize("operand,text", [("inf", "inf"), ("-inf", "-inf"), ("nan", "nan")])
    def test_copy_non_finite_to_text_on_every_backend(
        self, monkeypatch, operand, text, exponential
    ):
        """A Copy of a non-finite Real to text writes it lowercase, with no flag."""
        r, setup = self._non_finite_real(operand)
        chars = Block("Chars", TagType.CHAR, 1, 5)
        codes = [Int(f"Code{i}") for i in range(1, 6)]
        with Program(strict=False) as prog:
            with rung(system.sys.first_scan):
                for expr, target in setup:
                    math(expr, target)
                copy("Z", chars[1])
            with rung(~system.sys.first_scan):
                copy(r, chars[1], convert=to_text(exponential=exponential))
                for i, code in enumerate(codes, start=1):
                    copy(chars[i], code, convert=to_ascii)

        results = _on_every_backend(prog, monkeypatch, (*codes, system.fault.out_of_range), scans=2)
        wanted = [ord(ch) for ch in text] + [0] * (5 - len(text))
        assert results == {name: (*wanted, False) for name in results}

    @pytest.mark.parametrize("operand", ["nan", "inf", "-inf"])
    @pytest.mark.parametrize(
        "store",
        [
            pytest.param(lambda r, chars, reals: copy(r, chars[1]), id="copy"),
            pytest.param(
                lambda r, chars, reals: blockcopy(reals.select(1, 2), chars.select(1, 2)),
                id="blockcopy",
            ),
        ],
    )
    def test_a_non_finite_real_into_a_char_is_zero_on_every_backend(
        self, monkeypatch, store, operand
    ):
        """The store sets no flag and raises no error, and the rung runs on."""
        r, setup = self._non_finite_real(operand)
        chars = Block("Txt", TagType.CHAR, 1, 2)
        reals = Block("Vals", TagType.REAL, 1, 2)
        code = Int("Code", default=7)
        after = Int("After")
        with Program(strict=False) as prog:
            with rung(system.sys.first_scan):
                for expr, target in setup:
                    math(expr, target)
                copy(r, reals[1])
                copy(r, reals[2])
            with rung(~system.sys.first_scan):
                store(r, chars, reals)
                copy(5, after)
            with rung(~system.sys.first_scan, chars[1] == "A"):
                copy(1, code)

        results = _on_every_backend(prog, monkeypatch, (code, *self._FAULTS, after), scans=2)
        assert results == {name: (7, False, False, False, 5) for name in results}

    @pytest.mark.parametrize("operand", ["nan", "inf", "-inf"])
    def test_a_copy_of_a_non_finite_real_into_a_lone_char_keeps_it_on_every_backend(
        self, monkeypatch, operand
    ):
        """A plain Copy of a Real into text writes the Real's text, which a CHAR cannot hold."""
        r, setup = self._non_finite_real(operand)
        ch = Char("Ch", default="A")
        with Program(strict=False) as prog:
            with rung(system.sys.first_scan):
                for expr, target in setup:
                    math(expr, target)
            with rung(~system.sys.first_scan):
                copy(r, ch)

        results = _on_every_backend(prog, monkeypatch, (ch, *self._FAULTS), scans=2)
        assert results == {name: ("A", False, True, False) for name in results}

    def test_a_copy_of_a_finite_real_into_a_lone_char_faults_on_every_backend(self, monkeypatch):
        r = Real("R", default=66.5)
        ch = Char("Ch", default="A")
        with Program(strict=False) as prog:
            with rung():
                copy(r, ch)

        results = _on_every_backend(prog, monkeypatch, (ch, *self._FAULTS), scans=1)
        assert results == {name: ("A", False, True, False) for name in results}

    def test_a_copy_of_a_real_into_a_txt_writes_its_digits_on_every_backend(self, monkeypatch):
        """CLICK's plain Copy of a DF into a TXT is ``to_text()``.

        Its plain Copy of a TXT into a number is ``to_value``.
        """
        r = Real("R", default=66.5)
        chars = Block("Txt", TagType.CHAR, 1, 12)
        digit = Int("Digit", default=99)
        point = Bool("Point")
        with Program(strict=False) as prog:
            with rung():
                copy(r, chars[1])
                copy(chars[1], digit)
            with rung(chars[3] == "."):
                out(point)

        watched = (chars[1], digit, point, *self._FAULTS)
        results = _on_every_backend(prog, monkeypatch, watched, scans=1)
        assert results == {name: ("6", 6, True, False, False, False) for name in results}

    def test_a_copy_of_a_whole_number_into_a_txt_writes_its_digits_on_every_backend(
        self, monkeypatch
    ):
        """CLICK's dialog stores a Copy of a DS into a TXT as Copy Value with Suppress zero.

        That encoding is ``to_text()`` (clicknick copy-encodings, the DS source rows).
        """
        n = Int("N", default=-42)
        h = Word("H", default=0xAB)
        chars = Block("Txt", TagType.CHAR, 1, 12)
        int_text = Bool("IntText")
        word_text = Bool("WordText")
        with Program(strict=False) as prog:
            with rung():
                copy(n, chars[1])
                copy(h, chars[5])
            with rung(chars[1] == "-", chars[2] == "4", chars[3] == "2"):
                out(int_text)
            with rung(chars[5] == "A", chars[6] == "B"):
                out(word_text)

        results = _on_every_backend(prog, monkeypatch, (int_text, word_text), scans=1)
        assert results == {name: (True, True) for name in results}

    def test_a_copy_of_a_number_constant_into_a_txt_writes_its_digits_on_every_backend(
        self, monkeypatch
    ):
        """A CLICK stores the digits of a number constant copied into a TXT.

        An int is written as ``to_text()`` writes it, and a real with seven decimals.
        """
        chars = Block("Txt", TagType.CHAR, 1, 20)
        int_text = Bool("IntText")
        real_text = Bool("RealText")
        with Program(strict=False) as prog:
            with rung():
                copy(-5, chars[1])
                copy(2.5, chars[5])
            with rung(chars[1] == "-", chars[2] == "5", chars[3] == ""):
                out(int_text)
            with rung(*(chars[5 + i] == ch for i, ch in enumerate("2.5000000"))):
                out(real_text)

        results = _on_every_backend(prog, monkeypatch, (int_text, real_text), scans=1)
        assert results == {name: (True, True) for name in results}

    @pytest.mark.parametrize("dest_type", [TagType.INT, TagType.REAL], ids=["int", "real"])
    def test_a_plain_block_copy_of_txt_into_numbers_reads_digits_on_every_backend(
        self, monkeypatch, dest_type
    ):
        """A plain blockcopy() of TXT into numbers runs ``to_value``, as a plain copy() does.

        A letter sets the out-of-range flag and leaves its own element alone. A blank TXT
        reads as 0.
        """
        chars = Block("Txt", TagType.CHAR, 1, 3)
        nums = Block("Num", dest_type, 1, 3)
        digits = Bool("Digits")
        with Program(strict=False) as prog:
            with rung():
                copy("1A", chars[1])
                fill(99, nums.select(1, 3))
                blockcopy(chars.select(1, 3), nums.select(1, 3))
            with rung(nums[1] == 1, nums[2] == 99, nums[3] == 0):
                out(digits)

        watched = (digits, system.fault.out_of_range)
        results = _on_every_backend(prog, monkeypatch, watched, scans=1)
        assert results == {name: (True, True) for name in results}

    def test_a_block_copy_into_one_tag_writes_one_element_per_source_on_every_backend(
        self, monkeypatch
    ):
        """CLICK's Block Copy takes the first destination address and counts on from it."""
        src = Block("Src", TagType.INT, 1, 3)
        nums = Block("Num", TagType.INT, 1, 5)
        written = Bool("Written")
        with Program(strict=False) as prog:
            with rung():
                fill(7, src.select(1, 3))
                blockcopy(src.select(1, 3), nums[2])
            with rung(nums[1] == 0, nums[2] == 7, nums[3] == 7, nums[4] == 7, nums[5] == 0):
                out(written)

        results = _on_every_backend(prog, monkeypatch, (written,), scans=1)
        assert results == {name: (True,) for name in results}

    def test_a_text_copy_into_a_txt_range_writes_from_its_first_txt_on_every_backend(
        self, monkeypatch
    ):
        """A copy into a TXT range writes from its first TXT, as the exported Copy does.

        A text longer than the range writes on past its end.
        """
        chars = Block("Txt", TagType.CHAR, 1, 10)
        written = Bool("Written")
        with Program(strict=False) as prog:
            with rung():
                copy("ABC", chars.select(5, 6))
            with rung(chars[4] == "", chars[5] == "A", chars[6] == "B", chars[7] == "C"):
                out(written)

        results = _on_every_backend(prog, monkeypatch, (written,), scans=1)
        assert results == {name: (True,) for name in results}

    @staticmethod
    def _sensor():
        # Each instance holds two fields in a window of three addresses, so a gap follows them.
        @named_array(Int, count=2, stride=3)
        class Sensor:
            raw = 11
            scaled = 22

        return Sensor

    def test_a_block_copy_of_a_named_array_instance_copies_its_fields_on_every_backend(
        self, monkeypatch
    ):
        """An instance copies its two fields and skips the gap after them."""
        sensor = self._sensor()
        working = Block("Working", TagType.INT, 1, 3)
        written = Bool("Written")
        with Program(strict=False) as prog:
            with rung():
                blockcopy(sensor.instance(2), working.select(1, 2))
            with rung(working[1] == 11, working[2] == 22, working[3] == 0):
                out(written)

        results = _on_every_backend(prog, monkeypatch, (written,), scans=1)
        assert results == {name: (True,) for name in results}

    def test_a_block_copy_of_a_named_array_instance_into_one_tag_on_every_backend(
        self, monkeypatch
    ):
        """A single tag as the destination gets one element for each field, not for the gap."""
        sensor = self._sensor()
        nums = Block("Num", TagType.INT, 1, 20)
        written = Bool("Written")
        with Program(strict=False) as prog:
            with rung():
                blockcopy(sensor.instance(2), nums[10])
            with rung(nums[10] == 11, nums[11] == 22, nums[12] == 0):
                out(written)

        results = _on_every_backend(prog, monkeypatch, (written,), scans=1)
        assert results == {name: (True,) for name in results}

    def test_a_block_copy_of_a_named_array_instance_counts_its_fields_when_built(self):
        """Three destinations for two fields is refused when the program is built."""
        sensor = self._sensor()
        working = Block("Working", TagType.INT, 1, 3)
        with pytest.raises(ValueError, match="The source has 2 tags, and the destination has 3"):
            with Program(strict=False):
                with rung():
                    blockcopy(sensor.instance(2), working.select(1, 3))

    def test_a_real_to_binary_into_a_char_is_refused_at_build(self):
        """CLICK's Copy dialog hides Copy Binary from a DF into a TXT."""
        with pytest.raises(
            ValueError, match=r"^copy\(\) got convert=to_binary with the Real tag R"
        ):
            with Program(strict=False):
                with rung():
                    copy(Real("R"), Char("Ch"), convert=to_binary)

    @pytest.mark.parametrize("operand", ["nan", "inf", "-inf"])
    def test_non_finite_randint_bound_reads_zero_on_every_backend(self, monkeypatch, operand):
        """A randint() of a Real reads 0 and sets no flag, whatever the Real holds."""
        from pyrung.core import randint

        r, setup = self._non_finite_real(operand)
        dest = Dint("Dest", default=7)
        after = Int("After")
        with Program(strict=False) as prog:
            with rung(system.sys.first_scan):
                for expr, target in setup:
                    math(expr, target)
            with rung(~system.sys.first_scan):
                math(randint(0, r) + 5, dest)
                copy(5, after)

        results = _on_every_backend(prog, monkeypatch, (dest, *self._FAULTS, after), scans=2)
        assert results == {name: (5, False, False, False, 5) for name in results}

    def test_finite_real_randint_bound_reads_zero_on_every_backend(self, monkeypatch):
        from pyrung.core import randint

        low, high = Real("Low", default=3.0), Real("High", default=5.0)
        dest = Int("Dest", default=7)
        with Program(strict=False) as prog:
            with rung():
                math(randint(low, high), dest)

        results = _on_every_backend(prog, monkeypatch, (dest, *self._FAULTS), scans=2)
        assert results == {name: (0, False, False, False) for name in results}

    @pytest.mark.parametrize(
        "bounds,dest_tag", [((7, 7), Int), ((70000, 70000), Int), ((70000, 70000), Dint)]
    )
    def test_equal_randint_constants_are_a_math_error_on_every_backend(
        self, monkeypatch, bounds, dest_tag
    ):
        """Equal bounds store 0 and set SC46 only. The rung runs on."""
        from pyrung.core import randint

        dest = dest_tag("Dest", default=7)
        after = Int("After")
        with Program(strict=False) as prog:
            with rung():
                math(randint(*bounds) / 2, dest)
                copy(5, after)

        results = _on_every_backend(prog, monkeypatch, (dest, *self._FAULTS, after), scans=1)
        faults = tuple(flag.name.endswith("math_operation_error") for flag in self._FAULTS)
        assert results == {name: (0, *faults, 5) for name in results}

    def test_equal_randint_constants_outside_calc_are_zero_on_every_backend(self, monkeypatch):
        from pyrung.core import randint

        copied = Int("Copied", default=7)
        with Program(strict=False) as prog:
            with rung():
                copy(randint(7, 7), copied)

        results = _on_every_backend(prog, monkeypatch, (copied, *self._FAULTS), scans=1)
        assert results == {name: (0, False, False, False) for name in results}

    def test_randint_constants_stay_whole_into_an_int_on_every_backend(self, monkeypatch):
        """The draw is 70000..70002. Into an Int it wraps to 4464..4466 and sets SC43."""
        from pyrung.core import randint

        dest = Int("Dest", default=7)
        with Program(strict=False) as prog:
            with rung():
                math(randint(70000, 70002), dest)

        results = _on_every_backend(prog, monkeypatch, (dest, *self._FAULTS), scans=3)
        for value, *faults in results.values():
            assert 4464 <= value <= 4466
            assert faults == [False, True, False]

    @pytest.mark.parametrize("narrow_first", [True, False], ids=["narrow_first", "wide_first"])
    def test_one_randint_into_both_widths_on_every_backend(self, monkeypatch, narrow_first):
        """The Int wraps the draw and sets SC43. The Dint Math clears SC43 when it runs last."""
        from pyrung.core import randint

        roll = randint(70000, 70002)
        narrow, wide = Int("Narrow", default=7), Dint("Wide", default=7)
        with Program(strict=False) as prog:
            with rung():
                for dest in (narrow, wide) if narrow_first else (wide, narrow):
                    math(roll, dest)

        results = _on_every_backend(prog, monkeypatch, (narrow, wide, *self._FAULTS), scans=3)
        for low, high, *faults in results.values():
            assert 4464 <= low <= 4466
            assert 70000 <= high <= 70002
            assert faults == [False, not narrow_first, False]

    @pytest.mark.parametrize(
        "make, low, high",
        [
            pytest.param(lambda n: n / 2, 8, 9, id="division"),
            pytest.param(lambda n: n**2, 256, 257, id="power"),
            pytest.param(lambda n: sqrt(n), 4, 5, id="sqrt"),
        ],
    )
    def test_a_computed_randint_bound_draws_on_every_backend(self, monkeypatch, make, low, high):
        from pyrung.core import randint

        n = Int("N", default=16)
        dest = Int("Dest")
        with Program(strict=False) as prog:
            with rung():
                math(randint(make(n), make(n) + 1), dest)

        results = _on_every_backend(prog, monkeypatch, (dest, *self._FAULTS), scans=3)
        for value, *faults in results.values():
            assert low <= value <= high
            assert faults == [False, False, False]

    def test_a_real_inside_a_computed_randint_bound_draws_on_every_backend(self, monkeypatch):
        from pyrung.core import randint

        r = Real("R", default=3.5)
        dest = Int("Dest")
        with Program(strict=False) as prog:
            with rung():
                math(randint(r + 0, 5), dest)

        results = _on_every_backend(prog, monkeypatch, (dest, *self._FAULTS), scans=3)
        for value, *faults in results.values():
            assert 3 <= value <= 5
            assert faults == [False, False, False]

    @pytest.mark.parametrize(
        "bounds, low, high, equal",
        [((2.5, 4.5), 2, 4, False), ((2.5, 2.5), 0, 0, True)],
        ids=["different", "equal"],
    )
    def test_float_literal_randint_bounds_draw_on_every_backend(
        self, monkeypatch, bounds, low, high, equal
    ):
        """Each bound is read as an integer.

        Equal bounds are a math error that stores 0 and sets SC46.
        """
        from pyrung.core import randint

        dest = Int("Dest", default=9)
        with Program(strict=False) as prog:
            with rung():
                math(randint(*bounds), dest)

        results = _on_every_backend(prog, monkeypatch, (dest, *self._FAULTS), scans=3)
        for value, *faults in results.values():
            assert low <= value <= high
            assert faults == [False, False, equal]

    @pytest.mark.parametrize(
        "make, tag_type, held",
        [
            pytest.param(lambda r, n: r(0, sqrt(n)), Int, -1, id="sqrt"),
            pytest.param(lambda r, n: r(sqrt(n), 0), Int, -1, id="sqrt-low"),
            pytest.param(lambda r, n: r(0, log(n)), Int, 0, id="log"),
            pytest.param(lambda r, n: r(0, n**0.5), Real, -8.0, id="fractional-power"),
        ],
    )
    @pytest.mark.parametrize("dest", [Dint, Int], ids=["into-dint", "into-int"])
    def test_a_domain_error_in_a_randint_bound_on_every_backend(
        self, monkeypatch, make, tag_type, held, dest
    ):
        """Inside a Math the bound is read too. The result is 0 with SC46, and the scan runs on."""
        from pyrung.core import randint

        n = tag_type("N", default=held)
        d = dest("D", default=-1)
        after = Bool("After")
        with Program(strict=False) as prog:
            with rung():
                math(make(randint, n), d)
            with rung():
                out(after)

        results = _on_every_backend(
            prog, monkeypatch, (d, system.fault.math_operation_error, after), scans=1
        )
        assert results == {name: (0, True, True) for name in results}

    def test_a_domain_error_in_a_randint_bound_outside_calc_on_every_backend(self, monkeypatch):
        """Outside a Math the same bound reads 0 and sets no flag."""
        from pyrung.core import randint

        n = Int("N", default=-1)
        d = Dint("D", default=-1)
        with Program(strict=False) as prog:
            with rung():
                copy(randint(0, sqrt(n)), d)

        results = _on_every_backend(
            prog, monkeypatch, (d, system.fault.math_operation_error), scans=1
        )
        assert results == {name: (0, False) for name in results}

    def test_a_char_randint_bound_draws_on_every_backend(self, monkeypatch):
        from pyrung.core import randint

        lo = Char("Lo", default="3")
        dest = Int("Dest")
        with Program(strict=False) as prog:
            with rung():
                math(randint(lo, 5), dest)

        results = _on_every_backend(prog, monkeypatch, (dest, *self._FAULTS), scans=3)
        for value, *faults in results.values():
            assert 3 <= value <= 5
            assert faults == [False, False, False]

    @pytest.mark.parametrize("operand", ["nan", "inf", "-inf"])
    def test_a_non_finite_computed_randint_bound_on_every_backend(self, monkeypatch, operand):
        """pyrung's choice: the site is 0.

        In a Math it also sets SC43, and the rest of the formula runs, as for any randint()
        math error. Outside a Math it sets no flag.
        """
        from pyrung.core import randint

        r, setup = self._non_finite_real(operand)
        dest = Dint("Dest", default=7)
        copied = Dint("Copied", default=7)
        after = Bool("After")
        with Program(strict=False) as prog:
            with rung(system.sys.first_scan):
                for expr, target in setup:
                    math(expr, target)
            with rung(~system.sys.first_scan):
                copy(randint(0, r + 0), copied)
            with rung(~system.sys.first_scan):
                math(randint(0, r + 0) + 5, dest)
                out(after)

        results = _on_every_backend(
            prog, monkeypatch, (dest, copied, *self._FAULTS, after), scans=2
        )
        faults = tuple(flag.name.endswith("out_of_range") for flag in self._FAULTS)
        assert results == {name: (5, 0, *faults, True) for name in results}

    def test_randint_constants_stay_wide_into_a_dint_on_every_backend(self, monkeypatch):
        from pyrung.core import randint

        dest = Dint("Dest", default=7)
        with Program(strict=False) as prog:
            with rung():
                math(randint(70000, 70002), dest)

        results = _on_every_backend(prog, monkeypatch, (dest, *self._FAULTS), scans=3)
        for value, *faults in results.values():
            assert 70000 <= value <= 70002
            assert faults == [False, False, False]

    @pytest.mark.parametrize("operand", ["nan", "inf", "-inf"])
    def test_non_finite_forloop_count_runs_none_and_sc43_on_every_backend(
        self, monkeypatch, operand
    ):
        """pyrung's choice: a forloop() whose REAL count holds Inf or NaN runs no iteration.

        It sets SC43 and the scan runs on. This holds on all three backends.
        """
        r, setup = self._non_finite_real(operand)
        hits = Int("Hits")
        after = Bool("After")
        with Program(strict=False) as prog:
            with rung(system.sys.first_scan):
                for expr, target in setup:
                    math(expr, target)
            with rung(~system.sys.first_scan):
                with forloop(r):
                    math(hits + 1, hits)
            with rung(~system.sys.first_scan):
                out(after)

        results = _on_every_backend(
            prog, monkeypatch, (hits, system.fault.out_of_range, after), scans=2
        )
        assert results == {name: (0, True, True) for name in results}

    def test_finite_real_forloop_count_still_iterates_on_every_backend(self, monkeypatch):
        r = Real("R", default=3.0)
        hits = Int("Hits")
        with Program(strict=False) as prog:
            with rung():
                with forloop(r):
                    math(hits + 1, hits)

        results = _on_every_backend(prog, monkeypatch, (hits, system.fault.out_of_range), scans=2)
        assert results == {name: (6, False) for name in results}

    def test_forloop_count_is_read_as_unsigned_of_its_width_on_every_backend(self, monkeypatch):
        """A forloop() reads a DD count whole and a DS count as unsigned 16-bit.

        A DD of 65537 runs 65537 times (hardware 2026-11-05). A DS of -32768 runs 32768 times.
        This holds on all three backends.
        """
        wide = Dint("Wide", default=65537)
        narrow = Int("Narrow", default=-32768)
        wide_hits = Dint("WideHits")
        narrow_hits = Dint("NarrowHits")
        with Program(strict=False) as prog:
            with rung(system.sys.first_scan):
                with forloop(wide):
                    math(wide_hits + 1, wide_hits)
            with rung(system.sys.first_scan):
                with forloop(narrow):
                    math(narrow_hits + 1, narrow_hits)

        results = _on_every_backend(prog, monkeypatch, (wide_hits, narrow_hits), scans=1)
        assert results == {name: (65537, 32768) for name in results}

    def test_forloop_dint_count_of_zero_or_below_runs_none_on_every_backend(self, monkeypatch):
        """A forloop() reads a DD count as signed.

        A count of -1 runs no iteration (hardware 2026-11-06). The most negative Dint and a DD
        element of -1 also run none. A count of 3 still runs 3 times. This holds on all three
        backends.
        """
        minus_one = Dint("MinusOne", default=-1)
        lowest = Dint("Lowest", default=-2147483648)
        longs = Block("Longs", TagType.DINT, 1, 2)
        pointer = Int("Pointer", default=1)
        three = Dint("Three", default=3)
        hits = Dint("Hits")
        with Program(strict=False) as prog:
            with rung(system.sys.first_scan):
                copy(-1, longs[1])
            for count in (minus_one, lowest, longs[pointer], three):
                with rung(system.sys.first_scan):
                    with forloop(count):
                        math(hits + 1, hits)

        results = _on_every_backend(prog, monkeypatch, (hits,), scans=1)
        assert results == {name: (3,) for name in results}

    def test_forloop_constant_and_negative_dint_counts_on_every_backend(self, monkeypatch):
        """A forloop() reads a constant count whole and a DD count as signed.

        A count of 70000 runs 70000 times, not 4464. A DD of -65535 or -2 runs none, not its
        low word. A CLICK reads both counts the same way (hardware 2026-11-10). This holds on
        all three backends.
        """
        low = Dint("Low", default=-65535)
        minus_two = Dint("MinusTwo", default=-2)
        whole, signed = Dint("Whole"), Dint("Signed")
        with Program(strict=False) as prog:
            with rung(system.sys.first_scan):
                with forloop(70000):
                    math(whole + 1, whole)
            for count in (low, minus_two):
                with rung(system.sys.first_scan):
                    with forloop(count):
                        math(signed + 1, signed)

        results = _on_every_backend(prog, monkeypatch, (whole, signed), scans=1)
        assert results == {name: (70000, 0) for name in results}

    _FAULTS = (
        system.fault.division_error,
        system.fault.out_of_range,
        system.fault.math_operation_error,
    )

    @pytest.mark.parametrize(
        ("label", "make"),
        [
            ("div", lambda i, z, r: i / z),
            ("floordiv", lambda i, z, r: i // z),
            ("mod", lambda i, z, r: i % z),
            ("pow", lambda i, z, r: z ** (0 - i)),
            ("div_then_add", lambda i, z, r: (i / z) + 5),
            ("bit_and", lambda i, z, r: r & 3),
            ("invert", lambda i, z, r: ~r),
            ("lshift_negative", lambda i, z, r: i << (z - 1)),
            ("lsh", lambda i, z, r: lsh(r, 1)),
            ("rro", lambda i, z, r: rro(i, r)),
            ("sqrt", lambda i, z, r: sqrt(z - i)),
        ],
    )
    def test_a_math_error_outside_calc_is_zero_with_no_flag_on_every_backend(
        self, monkeypatch, label, make
    ):
        """pyrung's choice: outside a Math an expression is permissive.

        The operation that hits a math error gives 0. Nothing raises and no flag is set. This
        holds in a rung condition, a copy source and a forloop count alike.
        """
        i = Int("I", default=4)
        z = Int("Z")
        big = Real("Big", default=3.0e38)
        r = Real("R")
        expr = make(i, z, r)
        hit = Bool("Hit")
        copied = Dint("Copied", default=7)
        hits = Int("Hits")
        after = Int("After")
        with Program(strict=False) as prog:
            with rung(system.sys.first_scan):
                math(big * 10, r)  # +Inf, with SC43 on scan 0 only
            with rung(~system.sys.first_scan, expr == (5 if label == "div_then_add" else 0)):
                out(hit)
            with rung(~system.sys.first_scan):
                copy(expr, copied)
                with forloop(expr):
                    math(hits + 1, hits)
            with rung(~system.sys.first_scan):
                copy(5, after)

        results = _on_every_backend(
            prog, monkeypatch, (hit, copied, hits, *self._FAULTS, after), scans=2
        )
        expected_copy = 5 if label == "div_then_add" else 0
        expected = (True, expected_copy, expected_copy, False, False, False, 5)
        assert results == {name: expected for name in results}, label

    def test_calc_still_flags_the_math_errors_a_condition_ignores_on_every_backend(
        self, monkeypatch
    ):
        i = Int("I", default=4)
        z = Int("Z")
        dest = Int("Dest", default=7)
        hit = Bool("Hit")
        with Program(strict=False) as prog:
            with rung((i / z) == 0):
                out(hit)
            with rung():
                math(i / z, dest)

        results = _on_every_backend(prog, monkeypatch, (hit, dest, *self._FAULTS), scans=1)
        assert results == {name: (True, 0, True, False, False) for name in results}

    def test_a_float_overflow_in_a_condition_stays_inf_on_every_backend(self, monkeypatch):
        """Plain IEEE results pass through. Only an operation that raises gives 0."""
        big = Real("Big", default=3.0e38)
        hit = Bool("Hit")
        with Program(strict=False) as prog:
            with rung((big * 10) > 1.0e38):
                out(hit)

        results = _on_every_backend(prog, monkeypatch, (hit, *self._FAULTS), scans=1)
        assert results == {name: (True, False, False, False) for name in results}

    @pytest.mark.parametrize("operand", ["nan", "inf"])
    def test_a_non_finite_randint_bound_outside_calc_is_zero_on_every_backend(
        self, monkeypatch, operand
    ):
        from pyrung.core import randint

        r, setup = self._non_finite_real(operand)
        copied = Dint("Copied", default=7)
        hit = Bool("Hit")
        with Program(strict=False) as prog:
            with rung(system.sys.first_scan):
                for expr, target in setup:
                    math(expr, target)
            with rung(~system.sys.first_scan):
                copy(randint(0, r), copied)
            with rung(~system.sys.first_scan, randint(0, r) == 0):
                out(hit)

        results = _on_every_backend(prog, monkeypatch, (copied, hit, *self._FAULTS), scans=2)
        assert results == {name: (0, True, False, False, False) for name in results}

    def test_a_math_error_in_an_index_expression_is_index_zero_on_every_backend(self, monkeypatch):
        ds = Block("DS", TagType.INT, 0, 3)
        i = Int("I", default=4)
        z = Int("Z")
        got = Int("Got")
        with Program(strict=False) as prog:
            with rung():
                copy(11, ds[0])
                copy(22, ds[1])
            with rung():
                copy(ds[i // z], got)

        results = _on_every_backend(prog, monkeypatch, (got, *self._FAULTS), scans=1)
        assert results == {name: (11, False, False, False) for name in results}

    _HUGE = [
        ("pow_tag_exponent_times_float", lambda e, bd: (2**e) * 0.5),
        ("pow_times_float", lambda e, bd: (bd**40) * 1.5),
        ("pow_divided", lambda e, bd: (bd**40) / 2),
        ("pow_plus_float", lambda e, bd: (bd**40) + 0.5),
        ("sin_of_pow", lambda e, bd: sin(bd**40)),
        ("sqrt_of_pow", lambda e, bd: sqrt(bd**40)),
        ("atan_of_pow", lambda e, bd: atan(bd**40)),
        ("lsh_tag_count_times_float", lambda e, bd: lsh(1, e) * 0.5),
    ]

    @pytest.mark.parametrize(("label", "make"), _HUGE, ids=[label for label, _ in _HUGE])
    def test_an_integer_too_large_for_a_float_is_zero_outside_calc_on_every_backend(
        self, monkeypatch, label, make
    ):
        """An exact integer power is fine until it meets a float.

        Outside a Math, the operation that meets a float gives 0 and sets no flag.
        """
        e = Int("E", default=1100)
        bd = Dint("BD", default=2_000_000_000)
        expr = make(e, bd)
        hit = Bool("Hit")
        copied = Real("Copied", default=7.0)
        with Program(strict=False) as prog:
            with rung(expr == 0):
                out(hit)
            with rung():
                copy(expr, copied)

        results = _on_every_backend(prog, monkeypatch, (hit, copied, *self._FAULTS), scans=1)
        assert results == {name: (True, 0.0, False, False, False) for name in results}, label

    #: What each _HUGE Math stores into a Real. A Math step is a double, so the integer
    #: past the double range is Inf (SC43 into a Real). sin(Inf) is NaN with no flag.
    #: atan(Inf) is pi / 2.
    _HUGE_IN_CALC = {
        "sin_of_pow": (pymath.nan, False),
        "atan_of_pow": (pymath.atan(pymath.inf), False),
    }

    @pytest.mark.parametrize(("label", "make"), _HUGE, ids=[label for label, _ in _HUGE])
    def test_an_integer_too_large_for_a_float_in_calc_is_a_double_on_every_backend(
        self, monkeypatch, label, make
    ):
        e = Int("E", default=1100)
        bd = Dint("BD", default=2_000_000_000)
        dest = Real("Dest", default=7.0)
        with Program(strict=False) as prog:
            with rung():
                math(make(e, bd), dest)

        results = _on_every_backend(prog, monkeypatch, (dest, *self._FAULTS), scans=1)
        value, flagged = self._HUGE_IN_CALC.get(label, (pymath.inf, True))
        for stored, *faults in results.values():
            assert stored == value or (pymath.isnan(value) and pymath.isnan(stored)), label
            assert faults == [False, flagged, False], label

    #: Each row is (label, formula of (n, d, z, w, r), destination type, stored, flags as
    #: (SC40, SC43, SC46)), with n = -7, d = 2147483647, z = 0, w = 0x8001, r = -7.5.
    #: The rows are what a CLICK's Math does (hardware 2026-11-05, the wide constants
    #: 2026-11-11). A constant past a Dint is cut to 32 bits, as a pasted one ran.
    _CLICK_MATH = [
        ("double_steps", lambda n, d, z, w, r: d * d - (d * d - 1), Dint, 0, (0, 0, 0)),
        ("mod_int", lambda n, d, z, w, r: n % 2, Int, -1, (0, 0, 0)),
        ("mod_negative_divisor", lambda n, d, z, w, r: 7 % (n + 5), Int, 1, (0, 0, 0)),
        ("mod_real", lambda n, d, z, w, r: r % 2.0 * 10, Int, -15, (0, 0, 0)),
        ("mod_word_by_minus_two", lambda n, d, z, w, r: w % -2, Dint, 1, (0, 0, 0)),
        ("mod_by_zero", lambda n, d, z, w, r: n % z, Int, 0, (0, 0, 0)),
        ("mod_by_zero_real", lambda n, d, z, w, r: r % (z + 0.0), Real, 0.0, (0, 0, 0)),
        ("zero_to_zero", lambda n, d, z, w, r: z**z, Int, 0, (0, 0, 0)),
        # A CLICK read both values on 2026-11-10.  Their SC43 is pyrung's, as for any Inf result.
        ("zero_to_minus_one", lambda n, d, z, w, r: (z + 0.0) ** -1.0, Real, pymath.inf, (0, 1, 0)),
        ("zero_to_minus_one_int", lambda n, d, z, w, r: z ** (z - 1) + 5, Int, 0, (0, 1, 0)),
        ("zero_to_literal_zero", lambda n, d, z, w, r: z**0, Int, 0, (0, 0, 0)),
        (
            "wide_constant_into_dint",
            lambda n, d, z, w, r: (d - d + 131072) / 65536,
            Dint,
            2,
            (0, 0, 0),
        ),
        ("wide_constant_times", lambda n, d, z, w, r: (n - n + 2) * 40000, Dint, 80000, (0, 0, 0)),
        (
            "constant_past_a_dint",
            lambda n, d, z, w, r: (d - d + 3000000000) / 1000,
            Dint,
            -1294967,
            (0, 0, 0),
        ),
        ("hex_lsh_drops_the_bit", lambda n, d, z, w, r: lsh(w, 1), Word, 2, (0, 0, 0)),
    ]

    @pytest.mark.parametrize(
        ("label", "make", "dest_type", "stored", "flags"),
        _CLICK_MATH,
        ids=[case[0] for case in _CLICK_MATH],
    )
    def test_click_math_rules_on_every_backend(
        self, monkeypatch, label, make, dest_type, stored, flags
    ):
        n, d, z = Int("N", default=-7), Dint("D", default=2147483647), Int("Z")
        w, r = Word("W", default=0x8001), Real("R", default=-7.5)
        dest = dest_type("Dest", default=99)
        with Program(strict=False) as prog:
            with rung():
                math(make(n, d, z, w, r), dest)

        results = _on_every_backend(prog, monkeypatch, (dest, *self._FAULTS), scans=1)
        expected = (stored, *(bool(flag) for flag in flags))
        assert results == {name: expected for name in results}, label

    def test_mod_floors_and_zero_to_zero_is_one_outside_calc_on_every_backend(self, monkeypatch):
        """Only math() uses CLICK's MOD and CLICK's 0 ^ 0.

        Anywhere else ``%`` and ``**`` are Python's.  So the wrap-around index
        ``(P - 1) % 10 + 1`` still works.
        """
        n, z, p = Int("N", default=-3), Int("Z"), Int("P")
        odd, zero_power = Bool("Odd"), Bool("ZeroPower")
        copied, indexed = Int("Copied", default=99), Int("Indexed", default=99)
        ds = Block("DS", TagType.INT, 1, 10)
        with Program(strict=False) as prog:
            with rung():
                copy(42, ds[10])
            with rung(n % 2 == 1):
                out(odd)
            with rung(z**z == 1):
                out(zero_power)
            with rung():
                copy(n % 2, copied)
                copy(ds[(p - 1) % 10 + 1], indexed)

        watch = (odd, zero_power, copied, indexed, *self._FAULTS)
        results = _on_every_backend(prog, monkeypatch, watch, scans=1)
        assert results == {name: (True, True, 1, 42, False, False, False) for name in results}

    @pytest.mark.parametrize("default", [300000007, 10**17 + 1])
    def test_a_real_given_an_int_holds_a_float_on_every_backend(self, monkeypatch, default):
        """An int default or patch of a Real is stored as a float.

        So the Real's steps are doubles everywhere, and no backend needs to step it as one.
        """
        r = Real("R", default=default)
        dest = Dint("Dest", default=99)
        assert type(r.default) is float
        with Program(strict=False) as prog:
            with rung():
                math(r * r + 1 - r * r, dest)

        results = _on_every_backend(prog, monkeypatch, (dest, *self._FAULTS), scans=1)
        assert results == {name: (0, False, False, False) for name in results}
        for cls in (PLC, CompiledPLC):
            plc = cls(prog)
            plc.patch({"R": default + 2})
            plc.step()
            assert type(plc.current_state.tags["R"]) is float
            assert plc.current_state.tags["Dest"] == 0

    def test_a_huge_real_power_outside_calc_is_inf_on_every_backend(self, monkeypatch):
        """A Real given 10**30 holds 1e30, so its tenth power is Inf.

        It is not an int too large for a float, which a division would turn into an
        OverflowError.
        """
        r = Real("R", default=10**30)
        copied = Real("Copied", default=7.0)
        hit = Bool("Hit")

        def big():
            return r * r * r * r * r * r * r * r * r * r / 3

        with Program(strict=False) as prog:
            with rung(big() > 0):
                out(hit)
            with rung():
                copy(big(), copied)

        results = _on_every_backend(prog, monkeypatch, (hit, copied), scans=1)
        assert results == {name: (True, pymath.inf) for name in results}

    def test_a_real_calc_step_is_not_stepped_as_a_double(self):
        r1, r2, r3 = Real("R1"), Real("R2"), Real("R3")
        with Program() as prog:
            with rung():
                math(r1 + r2 * r1 - r2, r3)
        hw = P1AM()
        hw.slot(1, "P1-08SIM")
        code = generate_circuitpy(prog, hw, target_scan_ms=10.0).code
        assert "_dbl(" not in code

    @pytest.mark.parametrize("dest_type, floor", [(Int, -32768), (Dint, -2147483648)])
    def test_abs_of_the_bottom_of_a_range_is_zero_and_sc43_on_every_backend(
        self, monkeypatch, dest_type, floor
    ):
        source = dest_type("Source", default=floor)
        dest = dest_type("Dest", default=99)
        with Program(strict=False) as prog:
            with rung():
                math(abs(source), dest)

        results = _on_every_backend(prog, monkeypatch, (dest, *self._FAULTS), scans=1)
        assert results == {name: (0, False, True, False) for name in results}

    _COMPLEX = [
        ("literal_exponent", lambda n, h: n**0.5),
        ("tag_exponent", lambda n, h: n**h),
        ("int_base", lambda n, h: (0 - 8) ** h),
    ]

    @pytest.mark.parametrize(("label", "make"), _COMPLEX, ids=[label for label, _ in _COMPLEX])
    def test_a_complex_power_is_a_math_error_on_every_backend(self, monkeypatch, label, make):
        """pyrung's choice: a negative base to a fractional power is a math error.

        It has no real value, and neither does sqrt(-1). The result is 0 with no flag outside a
        Math, and 0 with math_operation_error (SC46) in a Math.
        """
        n = Real("N", default=-8.0)
        h = Real("H", default=0.5)
        hit = Bool("Hit")
        copied = Real("Copied", default=7.0)
        dest = Real("Dest", default=7.0)
        with Program(strict=False) as prog:
            with rung(make(n, h) == 0):
                out(hit)
            with rung():
                copy(make(n, h), copied)
            with rung():
                math(make(n, h), dest)

        results = _on_every_backend(prog, monkeypatch, (hit, copied, dest, *self._FAULTS), scans=1)
        assert results == {name: (True, 0.0, 0.0, False, False, True) for name in results}, label

    _NEGATIVE_SHIFTS = [
        ("lsh", lambda w, c: lsh(w, c)),
        ("rsh", lambda w, c: rsh(w, c)),
        ("left_shift", lambda w, c: w << c),
        ("right_shift", lambda w, c: w >> c),
    ]

    @pytest.mark.parametrize(
        ("label", "make"), _NEGATIVE_SHIFTS, ids=[label for label, _ in _NEGATIVE_SHIFTS]
    )
    def test_a_negative_shift_count_in_calc_is_zero_and_sc43_on_every_backend(
        self, monkeypatch, label, make
    ):
        """pyrung's choice: a shift by a negative count stores 0 and sets out_of_range (SC43).

        A bitwise operation on Inf does the same.
        """
        w = Word("W", default=0x00F0)
        c = Int("C", default=-1)
        dest = Word("Dest", default=7)
        hit = Bool("Hit")
        with Program(strict=False) as prog:
            with rung(make(w, c) == 0):
                out(hit)
            with rung():
                math(make(w, c), dest)

        results = _on_every_backend(prog, monkeypatch, (hit, dest, *self._FAULTS), scans=1)
        assert results == {name: (True, 0, False, True, False) for name in results}, label

    def test_text_literal_copy_fans_out_one_char_per_register(self, monkeypatch):
        hw = P1AM()
        hw.slot(1, "P1-08SIM")
        txt = ClickBlocks().txt
        with Program(strict=False) as prog:
            with rung():
                copy("ZZ", txt[1])
                copy("ABC", txt[1])
                copy(txt[2], txt[8])
                copy(txt[3], txt[9])
            with rung():
                copy("XY", txt[10])

        result = generate_circuitpy(prog, hw, target_scan_ms=10.0, runstop=None)

        class StubBase:
            def rollCall(self, modules):
                return None

            def readDiscrete(self, slot):
                return 0

        namespace = _run_single_scan_source(result.code, monkeypatch, StubBase())
        ctx = _context_for_program(prog, hw)

        def char(n: int) -> object:
            symbol = ctx.symbol_for_tag(txt[n])
            name, _, index = symbol.partition("[")
            value = namespace[name]
            return value[int(index.rstrip("]"))] if index else value  # type: ignore[index]

        assert [char(n) for n in (1, 2, 3, 8, 9, 10)] == ["A", "B", "C", "B", "C", "X"]

    def test_math_domain_error_writes_zero_and_keeps_running(self, monkeypatch):
        hw = P1AM()
        hw.slot(1, "P1-08SIM")
        outputs = hw.slot(2, "P1-08TRS")
        x = Real("X", default=-4.0)
        dest = Real("Dest", default=9.0)
        with Program(strict=False) as prog:
            with rung():
                math(sqrt(x), dest)
                out(outputs[1])

        result = generate_circuitpy(
            prog,
            hw,
            target_scan_ms=10.0,
            runstop=RunStopConfig(debounce_ms=0, run_when_high=False),
        )
        assert "_permissive_math(math.sqrt, " in result.code
        assert "if 46 in _calc_flags:" in result.code

        class StubBase:
            def __init__(self):
                self.discrete_writes: list[tuple[int, int]] = []

            def rollCall(self, modules):
                return None

            def readDiscrete(self, slot):
                return 0

            def writeDiscrete(self, value, slot):
                self.discrete_writes.append((slot, value))

            def readAnalog(self, slot, ch):
                return 0

            def writeAnalog(self, value, slot, ch):
                return None

            def readTemperature(self, slot, ch):
                return 0.0

        stub_base = StubBase()
        namespace = _run_single_scan_source(
            result.code, monkeypatch, stub_base, runtime_source=result.runtime, scans=2
        )
        ctx = _context_for_program(prog, hw)
        # Every scan runs the logic. The root is 0 and sets SC46 again, because SC46 clears at
        # each scan start. The output stays on.
        assert namespace[ctx.symbol_for_tag(dest)] == 0.0
        assert namespace[ctx.symbol_for_tag(system.fault.math_operation_error)] is True
        # No scan is in STOP, so the outputs are never forced off.
        assert (2, 1) in stub_base.discrete_writes
        assert stub_base.discrete_writes[-1] == (2, 1)
        assert (2, 0) not in stub_base.discrete_writes


class TestCopyConverterCodegen:
    def test_generation_unblocks_copy_blockcopy_converters(self):
        hw = P1AM()
        hw.slot(1, "P1-08SIM")
        ch = Block("CH", TagType.CHAR, 1, 12)
        ds = Block("DS", TagType.INT, 1, 12)

        with Program(strict=False) as prog:
            with rung(Bool("Enable")):
                copy(ch[1], ds[1], convert=to_value)
                blockcopy(ch.select(1, 3), ds.select(1, 3), convert=to_ascii)

        source = generate_circuitpy(prog, hw, target_scan_ms=10.0).code
        assert "_store_numeric_text_digit(" in source

    def test_copy_converter_runtime_modes_smoke(self, monkeypatch):
        hw = P1AM()
        hw.slot(1, "P1-08SIM")
        ch = Block("CH", TagType.CHAR, 1, 12)
        ds = Block("DS", TagType.INT, 1, 12)
        enable = Bool("Enable", default=True)

        with Program(strict=False) as prog:
            with rung(enable):
                fill("", ch.select(1, 12))
                fill(0, ds.select(1, 12))
                copy("5", ch[1])
                copy("A", ch[2])
                copy(7, ds[3])
                copy(123, ds[4])
                copy(ch[1], ds[1], convert=to_value)
                copy(ch[2], ds[2], convert=to_ascii)
                copy(ds[3], ch[4], convert=to_text(suppress_zero=False, termination_code=13))
                copy(ds[4], ch[10], convert=to_binary)

        ctx = _context_for_program(prog, hw)
        ch_symbol = ctx.symbol_for_block(ch)
        ds_symbol = ctx.symbol_for_block(ds)

        result = generate_circuitpy(prog, hw, target_scan_ms=10.0, watchdog_ms=None, runstop=None)

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

        namespace = _run_single_scan_source(
            result.code, monkeypatch, StubBase(), runtime_source=result.runtime
        )
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

    def test_blockcopy_converter_failure_sets_fault_and_skips_the_bad_character(self, monkeypatch):
        # CLICK writes the other characters and leaves the bad character's slot alone.
        # "1A3" over 9s gives 1, 9, 3.
        hw = P1AM()
        hw.slot(1, "P1-08SIM")
        ch = Block("CH", TagType.CHAR, 1, 10)
        ds = Block("DS", TagType.INT, 1, 10)
        fault_seen = Bool("FaultSeen")
        enable = Bool("Enable", default=True)

        with Program(strict=False) as prog:
            with rung(enable):
                copy("1", ch[1])
                copy("A", ch[2])
                copy("3", ch[3])
                fill(9, ds.select(1, 3))
                blockcopy(ch.select(1, 3), ds.select(1, 3), convert=to_value)
            with rung(system.fault.out_of_range):
                out(fault_seen)

        ctx = _context_for_program(prog, hw)
        ds_symbol = ctx.symbol_for_block(ds)
        fault_seen_symbol = ctx.symbol_for_tag(fault_seen)

        result = generate_circuitpy(prog, hw, target_scan_ms=10.0, watchdog_ms=None, runstop=None)

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

        namespace = _run_single_scan_source(
            result.code, monkeypatch, StubBase(), runtime_source=result.runtime
        )
        ds_values = _namespace_list(namespace, ds_symbol)
        assert ds_values[0] == 1
        assert ds_values[1] == 9
        assert ds_values[2] == 3
        assert namespace[fault_seen_symbol] is True

    def test_ascii_code_of_unwritten_char_writes_zero(self, monkeypatch):
        # A TXT that nothing wrote holds NUL, as in the core engine. Its ASCII code is 0,
        # and it overwrites the destination instead of leaving it alone.
        hw = P1AM()
        hw.slot(1, "P1-08SIM")
        ch = Block("CH", TagType.CHAR, 1, 12)
        ds = Block("DS", TagType.INT, 1, 12)
        enable = Bool("Enable", default=True)

        with Program(strict=False) as prog:
            with rung(enable):
                copy(77, ds[1])
                copy(ch[11], ds[1], convert=to_ascii)
                fill(55, ds.select(2, 3))
                blockcopy(ch.select(8, 9), ds.select(2, 3), convert=to_ascii)

        ctx = _context_for_program(prog, hw)
        ch_symbol = ctx.symbol_for_block(ch)
        ds_symbol = ctx.symbol_for_block(ds)
        result = generate_circuitpy(prog, hw, target_scan_ms=10.0, watchdog_ms=None, runstop=None)

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

        namespace = _run_single_scan_source(
            result.code, monkeypatch, StubBase(), runtime_source=result.runtime
        )
        ds_values = _namespace_list(namespace, ds_symbol)
        ch_values = _namespace_list(namespace, ch_symbol)
        assert ds_values[0:3] == [0, 0, 0]
        assert ch_values[10] == "\x00"

    def test_ascii_code_of_blanked_char_writes_zero(self, monkeypatch):
        # A TXT blanked by fill("") or reset() holds NUL, as in the core engine. Its ASCII
        # code is 0, and it overwrites the destination.
        hw = P1AM()
        hw.slot(1, "P1-08SIM")
        ch = Block("CH", TagType.CHAR, 1, 12)
        ds = Block("DS", TagType.INT, 1, 12)
        enable = Bool("Enable", default=True)

        with Program(strict=False) as prog:
            with rung(enable):
                copy("ZZZZ", ch[1])
                fill("", ch.select(1, 2))
                reset(ch[3])
                reset(ch.select(4, 4))
                fill(77, ds.select(1, 6))
                copy(ch[1], ds[1], convert=to_ascii)
                copy(ch[3], ds[2], convert=to_ascii)
                copy(ch[4], ds[3], convert=to_ascii)
                blockcopy(ch.select(1, 2), ds.select(4, 5), convert=to_ascii)

        ctx = _context_for_program(prog, hw)
        ch_symbol = ctx.symbol_for_block(ch)
        ds_symbol = ctx.symbol_for_block(ds)
        result = generate_circuitpy(prog, hw, target_scan_ms=10.0, watchdog_ms=None, runstop=None)

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

        namespace = _run_single_scan_source(
            result.code, monkeypatch, StubBase(), runtime_source=result.runtime
        )
        ds_values = _namespace_list(namespace, ds_symbol)
        ch_values = _namespace_list(namespace, ch_symbol)
        assert ch_values[0:4] == ["\x00"] * 4
        assert ds_values[0:6] == [0, 0, 0, 0, 0, 77]

    @pytest.mark.parametrize(
        ("saved", "expected"), [("", "\x00"), (5, "\x00"), ("A", "A"), ("\x00", "\x00")]
    )
    def test_retentive_load_of_blank_char_is_nul(self, saved, expected):
        # A memory.json from an older build may hold "" (or junk) for a TXT. The load
        # turns that into NUL, which is the only blank a TXT has.
        from pyrung.circuitpy.codegen.compile._primitives import _load_cast_expr

        assert eval(_load_cast_expr("saved", "CHAR"), {"saved": saved}) == expected

    def test_scalar_copy_binary_from_an_int_into_a_number_is_refused(self):
        """CLICK's Copy dialog offers no Copy Binary from a DS into a DD or a DF."""
        for target in (Dint("Target"), Real("Target")):
            with pytest.raises(ValueError, match=r"^copy\(\) got convert=to_binary with the Int"):
                with Program(strict=False):
                    with rung():
                        copy(Int("SourceValue"), target, convert=to_binary)


class TestGeneratedSourceSmoke:
    def test_generated_source_compile_and_execute_single_scan(self, monkeypatch):
        hw, di, do = _basic_hw()
        with Program(strict=False) as prog:
            with rung(di[1]):
                out(do[1])

        source = generate_circuitpy(
            prog, hw, target_scan_ms=10.0, watchdog_ms=None, runstop=None
        ).code
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
            with rung(enable):
                # An empty CHAR slot parses as empty text. It must set the fault, not crash.
                pack_text(txt.select(1, 1), parsed, allow_whitespace=True)
            with rung(system.fault.out_of_range):
                out(fault_seen)

        ctx = _context_for_program(prog, hw)
        fault_symbol = ctx.symbol_for_tag(system.fault.out_of_range)
        parsed_symbol = ctx.symbol_for_tag(parsed)
        fault_seen_symbol = ctx.symbol_for_tag(fault_seen)

        source = generate_circuitpy(
            prog, hw, target_scan_ms=10.0, watchdog_ms=None, runstop=None
        ).code
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


# ---------------------------------------------------------------------------
# Runtime split validation
# ---------------------------------------------------------------------------


class TestRuntimeSplit:
    """Checks that the pyrung_rt / code.py split produces correct output shapes."""

    def test_modbus_program_produces_nonempty_runtime(self):
        """A traffic-light-style Modbus program has a non-empty runtime.

        Its code.py is significantly smaller than the single-file baseline.
        """
        from pyrung.circuitpy import ModbusClientConfig, ModbusServerConfig
        from pyrung.click import ClickBlocks, ModbusTcpTarget, TagMap, receive
        from pyrung.core.instruction.send_receive import ModbusAddress, RegisterType

        blocks = ClickBlocks()
        c, ds, t, td, txt = blocks.c, blocks.ds, blocks.t, blocks.td, blocks.txt

        State = Char("State", default="r")
        RedTimer = Timer.clone("RedTimer")
        GreenTimer = Timer.clone("GreenTimer")
        YellowTimer = Timer.clone("YellowTimer")
        WalkRequest = Bool("WalkRequest")
        RxBusy, RxOk, RxErr = Bool("RxBusy"), Bool("RxOk"), Bool("RxErr")
        RxExCode = Int("RxExCode")

        with Program(strict=False) as prog:
            with rung(State == "r"):
                on_delay(RedTimer, preset=5000, unit="Tms")
            with rung(RedTimer.Done):
                copy("g", State)
            with rung(State == "g"):
                on_delay(GreenTimer, preset=4000, unit="Tms")
            with rung(GreenTimer.Done):
                copy("y", State)
            with rung(State == "y"):
                on_delay(YellowTimer, preset=1500, unit="Tms")
            with rung(YellowTimer.Done):
                copy("r", State)
            with rung():
                receive(
                    target="panel",
                    remote_start=ModbusAddress(0, RegisterType.COIL),
                    dest=WalkRequest,
                    receiving=RxBusy,
                    success=RxOk,
                    error=RxErr,
                    exception_response=RxExCode,
                )

        hw = P1AM()
        hw.slot(1, "P1-08SIM")
        hw.slot(2, "P1-15TD2")
        mapping = TagMap(
            {
                State: txt[1],
                RedTimer.Done: t[1],
                RedTimer.Acc: td[1],
                GreenTimer.Done: t[2],
                GreenTimer.Acc: td[2],
                YellowTimer.Done: t[3],
                YellowTimer.Acc: td[3],
                WalkRequest: c[1],
                RxBusy: c[2],
                RxOk: c[3],
                RxErr: c[4],
                RxExCode: ds[1],
            }
        )

        result = generate_circuitpy(
            prog,
            hw,
            target_scan_ms=10.0,
            modbus_server=ModbusServerConfig(ip="192.168.1.200"),
            modbus_client=ModbusClientConfig(
                targets=(ModbusTcpTarget(name="panel", ip="192.168.1.50"),)
            ),
            tag_map=mapping,
        )

        assert result.runtime, "Modbus program must produce non-empty runtime"
        assert "service_modbus_server" in result.runtime
        assert "service_modbus_client" in result.runtime

        code_lines = result.code.strip().splitlines()
        runtime_lines = result.runtime.strip().splitlines()
        # code.py must be smaller than a single-file version. The runtime absorbs the helpers
        # and the Modbus infrastructure, so code.py is under 67% of the total. The TagMap
        # serves every system point, so code.py also derives SD9, the clocks and the RTC
        # each scan.
        total = len(code_lines) + len(runtime_lines)
        assert len(code_lines) < total * 0.67, (
            f"code.py should be <67% of total ({len(code_lines)}/{total})"
        )

    def test_non_modbus_program_produces_empty_runtime(self):
        """A Neopixel-style program with no Modbus and no slots has an empty runtime."""
        State = Char("State", default="r")

        with Program(strict=False) as prog:
            with rung(State == "r"):
                on_delay(Timer4, preset=3000, unit="Tms")
            with rung(Timer4.Done):
                copy("g", State)
            with rung(State == "g"):
                copy(0, board.neopixel.r)
                copy(255, board.neopixel.g)

        result = generate_circuitpy(prog, P1AM(), target_scan_ms=10.0)
        assert result.runtime == "", "Non-Modbus program must produce empty runtime"
        assert "import pyrung_rt" not in result.code

    def test_both_generated_files_compile(self):
        """Both code.py and pyrung_rt.py are valid Python."""
        from pyrung.circuitpy import ModbusServerConfig
        from pyrung.click import ClickBlocks, TagMap

        c = ClickBlocks().c
        flag = Bool("Flag", default=True)
        with Program(strict=False) as prog:
            with rung(flag):
                out(flag)

        hw = P1AM()
        hw.slot(1, "P1-08SIM")
        result = generate_circuitpy(
            prog,
            hw,
            target_scan_ms=10.0,
            modbus_server=ModbusServerConfig(ip="192.168.1.200"),
            tag_map=TagMap({flag: c[1]}),
        )

        # generate_circuitpy already compiles internally. Compile here too to check it.
        compile(result.code, "code.py", "exec")
        compile(result.runtime, "pyrung_rt.py", "exec")


class TestRandIntCodegen:
    def test_randint_draws_on_the_board(self):
        """The board has no replay, so randint calls random.randint and imports random."""
        from pyrung.core import randint

        hw = P1AM()
        hw.slot(1, "P1-08SIM")
        lo = Int("Lo")
        with Program(strict=False) as prog:
            with rung(Bool("Enable")):
                math(randint(lo, 6), Int("Out"))

        source_code = generate_circuitpy(prog, hw, target_scan_ms=10.0).code
        compile(source_code, "code.py", "exec")
        assert "\nimport random\n" in source_code
        assert "_rand_draw(_calc_flags, " in source_code
        assert "random.randint(min(lo, hi), max(lo, hi))" in source_code

    def test_no_random_import_without_randint(self):
        hw = P1AM()
        hw.slot(1, "P1-08SIM")
        with Program(strict=False) as prog:
            with rung(Bool("Enable")):
                math(Int("A") + 1, Int("Out"))
        assert "import random" not in generate_circuitpy(prog, hw, target_scan_ms=10.0).code


class TestRangeAggregateCodegen:
    def test_aggregates_emit_inline_without_statistics(self):
        """CircuitPython has no statistics module, so max, min, median and mean are inline."""
        hw = P1AM()
        hw.slot(1, "P1-08SIM")
        vals = Block("Vals", TagType.INT, 1, 4)
        outs = [Int(f"Out{i}") for i in range(5)]
        with Program(strict=False) as prog:
            with rung(Bool("Enable")):
                math(vals.select(1, 4).max(), outs[0])
                math(vals.select(1, 4).min(), outs[1])
                math(vals.select(1, 4).median(), outs[2])
                math(vals.select(1, 3).median(), outs[3])
                math(vals.select(1, 4).mean() + abs(outs[0]), outs[4])

        source_code = generate_circuitpy(prog, hw, target_scan_ms=10.0).code
        compile(source_code, "code.py", "exec")
        assert "statistics" not in source_code
        assert "max((" in source_code
        assert "min((" in source_code
        assert "sorted((" in source_code
        assert ") / 4)" in source_code


class TestCharAgainstChar:
    """A Char tag compares with another Char tag (or slot) the same on every backend."""

    _OPS = {
        "==": lambda a, b: a == b,
        "!=": lambda a, b: a != b,
        "<": lambda a, b: a < b,
        "<=": lambda a, b: a <= b,
        ">": lambda a, b: a > b,
        ">=": lambda a, b: a >= b,
    }

    @pytest.mark.parametrize("op", list(_OPS))
    @pytest.mark.parametrize("left,right", [("A", "A"), ("A", "B"), ("B", "A"), ("a", "A")])
    def test_tag_against_tag_on_every_backend(self, monkeypatch, op, left, right):
        a = Char("A", default=left)
        b = Char("B", default=right)
        hit = Bool("Hit")
        compare = self._OPS[op]
        with Program(strict=False) as prog:
            with rung(compare(a, b)):
                out(hit)

        results = _on_every_backend(prog, monkeypatch, (hit,), scans=1)
        expected = (bool(compare(left, right)),)
        assert results == {name: expected for name in results}

    @pytest.mark.parametrize("op", list(_OPS))
    def test_block_slot_against_block_slot_on_every_backend(self, monkeypatch, op):
        slots = Block("Slots", TagType.CHAR, 1, 3)
        hit = Bool("Hit")
        compare = self._OPS[op]
        with Program(strict=False) as prog:
            with rung(system.sys.first_scan):
                copy("A", slots[1])
                copy("B", slots[2])
            with rung(~system.sys.first_scan, compare(slots[1], slots[2])):
                out(hit)

        results = _on_every_backend(prog, monkeypatch, (hit,), scans=2)
        expected = (bool(compare("A", "B")),)
        assert results == {name: expected for name in results}

    def test_tag_with_choices_against_tag_builds_conditions(self):
        from pyrung.core.condition import CompareEq, CompareGt

        a = Char("A", default="x", choices={"x": "Ex", "y": "Why"})
        b = Char("B", default="x")
        assert isinstance(a == b, CompareEq)
        assert isinstance(a > b, CompareGt)
        assert isinstance(b == a, CompareEq)
