"""What sets and clears a fault flag, on every engine.

SC40 (``fault.division_error``) follows the last Math: any Math that runs sets or
clears it.  SC43 (``fault.out_of_range``) and SC44 (``fault.address_error``)
follow the last Copy or Math.  Nothing else touches them, and an instruction that
does not run clears nothing.  A pointer below its block is SC44 with the
destination left alone; past it, there is no flag, a read gets the type's zero
and a write is dropped.  ``tests/conformance/test_system.py`` and
``tests/conformance/test_faults.py`` hold what a CLICK read.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from pyrung.circuitpy import P1AM, generate_circuitpy
from pyrung.circuitpy.codegen import compile_kernel
from pyrung.core import (
    PLC,
    Block,
    Bool,
    Char,
    CompiledPLC,
    Counter,
    Dint,
    Int,
    Program,
    Real,
    Rung,
    TagType,
    Timer,
    Word,
    blockcopy,
    calc,
    copy,
    count_up,
    fill,
    forloop,
    latch,
    on_delay,
    out,
    pack_bits,
    pack_text,
    pack_words,
    reset,
    search,
    system,
    to_ascii,
    to_text,
    unpack_to_bits,
    unpack_to_words,
)
from pyrung.core.analysis.prove.kernel import _step_compiled_kernel
from tests.circuitpy.test_codegen import _context_for_program, _run_single_scan_source

SC40 = system.fault.division_error
SC43 = system.fault.out_of_range
SC44 = system.fault.address_error

_ENGINES = [
    "interpreted",
    "compiled",
    "kernel_legacy",
    "kernel_blockless",
    "circuitpy",
    "circuitpy_rt",
]
#: Those that run the per-scan system points; a bare kernel step leaves them to
#: its caller (``system=`` of ``_step_compiled_kernel``).
_SCANNING_ENGINES = ["interpreted", "compiled", "circuitpy", "circuitpy_rt"]


class _StubBase:
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


def _run(prog: Program, engine: str, watch: tuple[Any, ...], monkeypatch, scans: int = 1) -> tuple:
    """*watch*'s values after *scans* scans of *prog* on *engine*."""
    if engine in ("interpreted", "compiled"):
        runner = (PLC if engine == "interpreted" else CompiledPLC)(prog, dt=0.010)
        for _ in range(scans):
            runner.step()
        state = runner.current_state.tags
        return tuple(state[tag.name] for tag in watch)
    if engine in ("kernel_legacy", "kernel_blockless"):
        compiled = compile_kernel(prog, blockless=engine == "kernel_blockless")
        kernel = compiled.create_kernel()
        for _ in range(scans):
            _step_compiled_kernel(compiled, kernel, dt=0.010)
        return tuple(kernel.tags[tag.name] for tag in watch)
    hw = P1AM()
    hw.slot(1, "P1-08SIM")
    split = engine == "circuitpy_rt"
    result = generate_circuitpy(prog, hw, target_scan_ms=10.0, runstop=None, force_runtime=split)
    namespace = _run_single_scan_source(
        result.code, monkeypatch, _StubBase(), runtime_source=result.runtime, scans=scans
    )
    ctx = _context_for_program(prog, hw)
    return tuple(namespace[ctx.symbol_for_tag(tag)] for tag in watch)


# ---- The registers the faults and the instructions under test use ------------------


_Tbl = Block("Tbl", TagType.INT, 1, 4)
_Big = Dint("Big", default=40000)
_Zero = Int("Zero")
_Sink = Int("Sink")
_Other = Int("Other")
_Hex = Word("HexIn", default=0x0F)
_HexOut = Word("HexOut")
_Bits = Block("Bits", TagType.BOOL, 1, 16)
_Words = Block("Words", TagType.INT, 1, 2)
_Packed = Dint("Packed")
_Chars = Block("Chars", TagType.CHAR, 1, 2)
_Ch = Char("Ch")
_Clk = Bool("Clk")
_Coil = Bool("Coil")
_Found = Bool("Found")
_Result = Int("SearchResult")
_Tmr = Timer.clone("FlagTmr")
_Ctr = Counter.clone("FlagCtr")


_FAULTS: dict[str, tuple[Any, Callable[[], object]]] = {
    "SC40": (SC40, lambda: calc(100 / _Zero, _Sink)),
    "SC43_math": (SC43, lambda: calc(_Big + 0, _Sink)),
    "SC43_copy": (SC43, lambda: copy(_Big, _Sink)),
    "SC44_copy": (SC44, lambda: copy(_Tbl[_Zero], _Sink)),
}

#: One Math of each kind, and every Copy tab, none of which faults here.
_MATH: dict[str, Callable[[], object]] = {
    "math": lambda: calc(_Other + 1, _Other),
    "math_hex": lambda: calc(_Hex & 3, _HexOut),
}
_COPY: dict[str, Callable[[], object]] = {
    "copy": lambda: copy(1, _Other),
    "copy_converter": lambda: copy(5, _Ch, convert=to_text()),
    "copy_to_ascii": lambda: copy(_Ch, _Other, convert=to_ascii),
    "blockcopy": lambda: blockcopy(_Tbl.select(1, 2), _Words.select(1, 2)),
    "fill": lambda: fill(1, _Words.select(1, 2)),
    "pack_bits": lambda: pack_bits(_Bits.select(1, 16), _Other),
    "pack_words": lambda: pack_words(_Words.select(1, 2), _Packed),
    "pack_text": lambda: pack_text(_Chars.select(1, 2), _Other),
    "unpack_to_bits": lambda: unpack_to_bits(_Other, _Bits.select(1, 16)),
    "unpack_to_words": lambda: unpack_to_words(_Packed, _Words.select(1, 2)),
}


def _forloop_step() -> None:
    with forloop(1):
        out(_Coil)


#: Instructions that run and leave every flag as it is.
_NEITHER: dict[str, Callable[[], object]] = {
    "out": lambda: out(_Coil),
    "latch": lambda: latch(_Coil),
    "reset": lambda: reset(_Coil),
    "on_delay": lambda: on_delay(_Tmr, preset=1000, unit="Ts"),
    "count_up": lambda: count_up(_Ctr, preset=3).reset(_Clk),
    "search": lambda: search(_Tbl.select(1, 2) == 99, result=_Result, found=_Found),
    "forloop": _forloop_step,
}
_STEPS = {**_MATH, **_COPY, **_NEITHER}


def _flag_after(fault: Callable[[], object], flag: Any, step: Callable[[], object]) -> Program:
    with Program(strict=False) as prog:
        with Rung():
            copy("12", _Chars[1])
        with Rung():
            fault()
        with Rung(flag):
            out(Bool("Raised"))
        with Rung():
            step()
        with Rung(flag):
            out(Bool("Left"))
    return prog


_FOLLOW_CASES = [
    *((f, s, False) for f in _FAULTS for s in _MATH),
    *((f, s, f == "SC40") for f in _FAULTS for s in _COPY),
    *((f, s, True) for f in _FAULTS for s in _NEITHER),
]


@pytest.mark.parametrize("engine", _ENGINES)
@pytest.mark.parametrize(("fault_name", "step_name", "stays"), _FOLLOW_CASES)
def test_what_clears_a_flag(engine, fault_name, step_name, stays, monkeypatch) -> None:
    flag, fault = _FAULTS[fault_name]
    prog = _flag_after(fault, flag, _STEPS[step_name])
    raised, left, end = _run(prog, engine, (Bool("Raised"), Bool("Left"), flag), monkeypatch)
    assert bool(raised) is True
    assert bool(left) is stays
    assert bool(end) is stays


@pytest.mark.parametrize("engine", _ENGINES)
@pytest.mark.parametrize("fault_name", list(_FAULTS))
def test_a_rung_that_does_not_run_clears_nothing(engine, fault_name, monkeypatch) -> None:
    flag, fault = _FAULTS[fault_name]
    never = Bool("Never")
    with Program(strict=False) as prog:
        with Rung():
            fault()
        with Rung(never):
            copy(1, _Other)
            calc(_Other + 1, _Other)
    assert _run(prog, engine, (flag,), monkeypatch) == (True,)


@pytest.mark.parametrize("engine", _SCANNING_ENGINES)
@pytest.mark.parametrize("fault_name", ["SC43_copy", "SC44_copy"])
def test_a_oneshot_that_does_not_fire_clears_nothing(engine, fault_name, monkeypatch) -> None:
    """On scan 1 the oneshot fires and clears the flag; on scan 2 it does not."""
    flag, fault = _FAULTS[fault_name]
    cleared, kept = Bool("Cleared"), Bool("Kept")
    with Program(strict=False) as prog:
        with Rung():
            fault()
        with Rung():
            copy(1, _Other, oneshot=True)
        with Rung(system.sys.first_scan, ~flag):
            latch(cleared)
        with Rung(~system.sys.first_scan, flag):
            latch(kept)
    assert _run(prog, engine, (cleared, kept), monkeypatch, scans=2) == (True, True)


@pytest.mark.parametrize("engine", _ENGINES)
def test_each_flag_follows_on_its_own(engine, monkeypatch) -> None:
    """A Math clears what it does not raise and raises what it does."""
    at_div, at_range = Bool("AtDiv"), Bool("AtRange")
    with Program(strict=False) as prog:
        with Rung():
            calc(100 / _Zero, _Sink)  # SC40 only
        with Rung(SC40, ~SC43):
            out(at_div)
        with Rung():
            calc(_Big + 0, _Sink)  # SC43 only
        with Rung(~SC40, SC43):
            out(at_range)
    assert _run(prog, engine, (at_div, at_range), monkeypatch) == (True, True)


@pytest.mark.parametrize("engine", _SCANNING_ENGINES)
def test_a_flag_clears_at_the_next_scan_whatever_runs(engine, monkeypatch) -> None:
    """pyrung's choice: nothing has to run on the next scan to clear a flag."""
    armed = Bool("Armed")
    at_top = Bool("AtTop")
    with Program(strict=False) as prog:
        with Rung(SC44):
            latch(at_top)
        with Rung(~armed):
            copy(_Tbl[_Zero], _Sink)
        with Rung():
            out(armed)
    assert _run(prog, engine, (at_top, SC44), monkeypatch, scans=2) == (False, False)


@pytest.mark.parametrize("engine", _ENGINES)
@pytest.mark.parametrize(
    ("flag", "fault"),
    [(SC43, lambda: copy(_Big, _Sink)), (SC44, lambda: copy(_Tbl[_Zero], _Sink))],
    ids=["SC43", "SC44"],
)
def test_a_source_reads_the_flag_as_the_instruction_found_it(
    engine, flag, fault, monkeypatch
) -> None:
    seen = Bool("SeenFlag")
    with Program(strict=False) as prog:
        with Rung():
            fault()
            copy(flag, seen)
    assert _run(prog, engine, (seen, flag), monkeypatch) == (True, False)


@pytest.mark.parametrize("engine", _ENGINES)
def test_a_calc_reading_the_flag_reads_it_as_it_found_it(engine, monkeypatch) -> None:
    number = Int("Number")
    with Program(strict=False) as prog:
        with Rung():
            calc(100 / _Zero, _Sink)
            calc(SC40 + 5, number)
    assert _run(prog, engine, (number, SC40), monkeypatch) == (6, False)


# ---- Pointers ---------------------------------------------------------------------


def _pointers() -> tuple[Int, Int, Int]:
    return Int("Past", default=50), Int("Below", default=0), Int("Inside", default=2)


_Kept = Int("Kept", default=7)


@pytest.mark.parametrize("engine", _ENGINES)
@pytest.mark.parametrize(
    ("tag_type", "dest", "zero"),
    [
        (TagType.INT, Int("IntDest", default=7), 0),
        (TagType.DINT, Dint("DintDest", default=7), 0),
        (TagType.REAL, Real("RealDest", default=7.0), 0.0),
        (TagType.WORD, Word("WordDest", default=7), 0),
        (TagType.BOOL, Bool("BoolDest", default=True), False),
        (TagType.CHAR, Char("CharDest", default="A"), "\x00"),
    ],
    ids=["int", "dint", "real", "word", "bool", "char"],
)
def test_a_read_past_the_block_is_the_types_zero_with_no_flag(
    engine, tag_type, dest, zero, monkeypatch
) -> None:
    table = Block("Typed", tag_type, 1, 4)
    past, _, _ = _pointers()
    with Program(strict=False) as prog:
        with Rung():
            copy(table[past], dest)
    value, sc43, sc44 = _run(prog, engine, (dest, SC43, SC44), monkeypatch)
    assert value == zero
    assert (bool(sc43), bool(sc44)) == (False, False)


@pytest.mark.parametrize("engine", _ENGINES)
def test_a_read_past_the_block_through_an_expression(engine, monkeypatch) -> None:
    past, _, inside = _pointers()
    for_copy, for_calc = Int("ForCopy", default=7), Int("ForCalc", default=7)
    with Program(strict=False) as prog:
        with Rung():
            copy(_Tbl[past + 1], for_copy)
            calc(_Tbl[inside + 100], for_calc)
    assert _run(prog, engine, (for_copy, for_calc, SC44), monkeypatch) == (0, 0, False)


@pytest.mark.parametrize("engine", _ENGINES)
@pytest.mark.parametrize(
    "op",
    [
        lambda elem, dest: copy(elem, dest),
        lambda elem, dest: calc(elem, dest),
    ],
    ids=["copy", "calc"],
)
def test_a_read_below_the_block_is_an_address_error_and_writes_nothing(
    engine, op, monkeypatch
) -> None:
    _, below, _ = _pointers()
    with Program(strict=False) as prog:
        with Rung():
            op(_Tbl[below], _Kept)
    assert _run(prog, engine, (_Kept, SC44, SC43), monkeypatch) == (7, True, False)


@pytest.mark.parametrize("engine", _ENGINES)
def test_a_converter_reads_past_the_block_as_the_types_zero(engine, monkeypatch) -> None:
    past, _, _ = _pointers()
    text = Block("Text", TagType.CHAR, 1, 4)
    code = Int("Code", default=7)
    digits = Block("Digits", TagType.CHAR, 1, 1)
    with Program(strict=False) as prog:
        with Rung():
            copy("9", digits[1])
            copy(text[past], code, convert=to_ascii)
            copy(_Tbl[past], digits[1], convert=to_text())
    assert _run(prog, engine, (code, digits[1], SC44, SC43), monkeypatch) == (
        0,
        "0",
        False,
        False,
    )


@pytest.mark.parametrize("engine", _ENGINES)
@pytest.mark.parametrize(
    "write",
    [
        lambda elem: copy(5, elem),
        lambda elem: copy(5, elem, oneshot=True),
    ],
    ids=["copy", "oneshot"],
)
def test_a_write_past_the_block_is_dropped_with_no_flag(engine, write, monkeypatch) -> None:
    past, _, _ = _pointers()
    table = Block("Dest", TagType.INT, 1, 2)
    first, second = Int("First"), Int("Second")
    at_sc44, at_sc43 = Bool("AtSc44"), Bool("AtSc43")
    with Program(strict=False) as prog:
        with Rung():
            write(table[past])
        with Rung(SC44):
            out(at_sc44)
        with Rung(SC43):
            out(at_sc43)
        with Rung():
            copy(table[1], first)
            copy(table[2], second)
    assert _run(prog, engine, (first, second, at_sc44, at_sc43), monkeypatch) == (
        0,
        0,
        False,
        False,
    )


@pytest.mark.parametrize("engine", _ENGINES)
def test_a_write_below_the_block_is_an_address_error(engine, monkeypatch) -> None:
    _, below, _ = _pointers()
    table = Block("Dest", TagType.INT, 1, 2)
    with Program(strict=False) as prog:
        with Rung():
            copy(5, table[below])
    assert _run(prog, engine, (SC44, SC43), monkeypatch) == (True, False)


@pytest.mark.parametrize("engine", _ENGINES)
def test_a_store_past_the_block_clears_the_flag_it_follows(engine, monkeypatch) -> None:
    """A dropped write still ran: the Copy clears what a fault before it raised."""
    past, _, _ = _pointers()
    table = Block("Dest", TagType.INT, 1, 2)
    with Program(strict=False) as prog:
        with Rung():
            copy(_Big, _Sink)
            copy(5, table[past])
    assert _run(prog, engine, (SC43,), monkeypatch) == (False,)


@pytest.mark.parametrize("engine", _ENGINES)
def test_a_converter_write_past_the_block_is_dropped(engine, monkeypatch) -> None:
    past, _, _ = _pointers()
    text = Block("Text", TagType.CHAR, 1, 4)
    with Program(strict=False) as prog:
        with Rung():
            copy(5, text[past], convert=to_text())
    assert _run(prog, engine, (SC44, SC43), monkeypatch) == (False, False)


_PACK_THROUGH_POINTER: dict[str, Callable[[Any], None]] = {
    "pack_bits": lambda elem: pack_bits(_Bits.select(1, 16), elem),
    "pack_text": lambda elem: pack_text(_Chars.select(1, 2), elem),
    "unpack_to_bits": lambda elem: unpack_to_bits(elem, _Bits.select(1, 16)),
}


@pytest.mark.parametrize("engine", _ENGINES)
@pytest.mark.parametrize("op_name", list(_PACK_THROUGH_POINTER))
def test_pack_and_unpack_through_a_pointer(engine, op_name, monkeypatch) -> None:
    past, below, _ = _pointers()
    op = _PACK_THROUGH_POINTER[op_name]
    at_past = Bool("AtPast")
    with Program(strict=False) as prog:
        with Rung():
            copy("12", _Chars[1])
        with Rung():
            op(_Tbl[past])
        with Rung(SC44):
            out(at_past)
        with Rung():
            op(_Tbl[below])
    assert _run(prog, engine, (at_past, SC44), monkeypatch) == (False, True)


@pytest.mark.parametrize("engine", _ENGINES)
def test_pack_text_refuses_text_before_it_looks_at_a_pointer(engine, monkeypatch) -> None:
    past, below, _ = _pointers()
    at_past = Bool("AtPast")
    with Program(strict=False) as prog:
        with Rung():
            pack_text(_Chars.select(1, 2), _Tbl[past])
        with Rung(SC43, ~SC44):
            out(at_past)
        with Rung():
            pack_text(_Chars.select(1, 2), _Tbl[below])
    assert _run(prog, engine, (at_past, SC43, SC44), monkeypatch) == (True, True, False)


@pytest.mark.parametrize("runner", [PLC, CompiledPLC], ids=["interpreted", "compiled"])
@pytest.mark.parametrize(
    "steps, up",
    [
        pytest.param(("copy_clamps",), "fault.out_of_range", id="sc43_up"),
        pytest.param(("copy_clamps", "plain_copy"), None, id="sc43_cleared_by_a_copy"),
        pytest.param(("copy_clamps", "plain_calc"), None, id="sc43_cleared_by_a_calc"),
        pytest.param(("copy_clamps", "coil"), "fault.out_of_range", id="sc43_left_by_a_coil"),
        pytest.param(("bad_pointer",), "fault.address_error", id="sc44_up"),
        pytest.param(("bad_pointer", "plain_copy"), None, id="sc44_cleared_by_a_copy"),
        pytest.param(("divide",), "fault.division_error", id="sc40_up"),
        pytest.param(("divide", "plain_copy"), "fault.division_error", id="sc40_left_by_a_copy"),
        pytest.param(("divide", "plain_calc"), None, id="sc40_cleared_by_a_calc"),
    ],
)
def test_a_flag_nothing_reads_settles_the_same(runner, steps, up) -> None:
    """The compiled backend settles a flag no rung reads, as the state shows it."""
    actions: dict[str, Callable[[], object]] = {
        "copy_clamps": lambda: copy(Dint("Big", default=99_999), Int("Small")),
        "bad_pointer": lambda: copy(_Tbl[Int("Below", default=0)], Int("Plain")),
        "divide": lambda: calc(Int("One", default=1) / Int("Zero"), Int("Plain")),
        "plain_copy": lambda: copy(1, Int("Plain")),
        "plain_calc": lambda: calc(Int("One", default=1) + 1, Int("Plain")),
        "coil": lambda: out(_Coil),
    }
    with Program(strict=False) as prog:
        for name in steps:
            with Rung():
                actions[name]()
    plc = runner(prog, dt=0.010)
    plc.step()
    plc.step()
    tags = plc.current_state.tags
    for flag in ("fault.division_error", "fault.out_of_range", "fault.address_error"):
        assert tags[flag] is (flag == up), flag


def test_instructions_declare_every_flag_they_follow() -> None:
    """``_fault_writes`` is what the PDG and the prover see: a clear is a write."""
    with Program(strict=False) as prog:
        with Rung():
            for step in (*_MATH.values(), *_COPY.values()):
                step()
    for instruction in prog.rungs[0]._instructions:
        assert instruction._fault_follows, type(instruction).__name__
        assert set(instruction._fault_follows) <= set(instruction._fault_writes)
