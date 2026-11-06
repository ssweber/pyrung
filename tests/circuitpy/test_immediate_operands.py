"""An ``immediate()`` operand reads and writes its plain tag on every backend.

``immediate()`` is a transcription hint for the CLICK export; in simulation it does
nothing.  Wherever the interpreter takes the wrapped tag's value or type, the
compiled kernel and the generated CircuitPython must take the same one.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from pyrung.circuitpy import P1AM, RunStopConfig, generate_circuitpy
from pyrung.core import (
    PLC,
    Block,
    Bool,
    CompiledPLC,
    Dint,
    Int,
    Program,
    Real,
    Rung,
    Tag,
    TagType,
    Word,
    blockcopy,
    calc,
    copy,
    fill,
    pack_bits,
    pack_text,
    pack_words,
    run_function,
    search,
    unpack_to_bits,
    unpack_to_words,
)
from pyrung.core.copy_converters import to_text
from pyrung.core.system_points import system
from pyrung.core.tag import immediate
from tests.circuitpy.test_codegen import _context_for_program, _run_single_scan_source


class _InputsHighBase:
    """A P1AM base whose slot-1 inputs 1 and 2 read on, and whose writes go nowhere."""

    def rollCall(self, modules):
        return None

    def readDiscrete(self, slot):
        return 0b11 if slot == 1 else 0

    def writeDiscrete(self, value, slot):
        return None

    def readAnalog(self, slot, ch):
        return 0

    def writeAnalog(self, value, slot, ch):
        return None

    def readTemperature(self, slot, ch):
        return 0.0


def _hardware():
    hw = P1AM()
    x = hw.slot(1, "P1-08SIM")
    y = hw.slot(2, "P1-08TRS")
    return hw, x, y


def _on_every_backend(
    program: Program, hw: P1AM, inputs: tuple[Tag, ...], watch: tuple[Tag, ...], monkeypatch
) -> dict[str, tuple]:
    """*watch*'s values after two scans with *inputs* on, keyed by backend."""
    results: dict[str, tuple] = {}
    for name, cls in (("interpreted", PLC), ("compiled", CompiledPLC)):
        plc = cls(program)
        for _ in range(2):
            plc.patch({tag: True for tag in inputs})
            plc.step()
        tags = plc.current_state.tags
        results[name] = tuple(tags.get(tag.name, tag.default) for tag in watch)
    result = generate_circuitpy(
        program,
        hw,
        target_scan_ms=10.0,
        runstop=RunStopConfig(debounce_ms=0, run_when_high=False),
    )
    namespace = _run_single_scan_source(
        result.code, monkeypatch, _InputsHighBase(), runtime_source=result.runtime, scans=2
    )
    ctx = _context_for_program(program, hw)
    results["circuitpy"] = tuple(eval(ctx.symbol_for_tag(tag), namespace) for tag in watch)
    return results


Case = Callable[..., tuple[P1AM, Program, tuple[Tag, ...], tuple[Tag, ...]]]


def _copy_real_into_dint(wrap: Callable[[Any], Any] = immediate):
    hw, _x, _y = _hardware()
    src = Real("Src", default=3.7)
    big = Real("Big", default=1.0e12)
    dest = Dint("Dest")
    clamped = Dint("Clamped")
    with Program(strict=False) as prog:
        with Rung():
            copy(wrap(src), dest)
            copy(wrap(big), clamped)
    return hw, prog, (), (dest, clamped, system.fault.out_of_range)


def _copy_int_into_int(wrap: Callable[[Any], Any] = immediate):
    hw, _x, _y = _hardware()
    src = Int("Src", default=-12)
    dest = Int("Dest")
    with Program(strict=False) as prog:
        with Rung():
            copy(wrap(src), dest)
    return hw, prog, (), (dest,)


def _copy_input_into_bool(wrap: Callable[[Any], Any] = immediate):
    hw, x, _y = _hardware()
    dest = Bool("Dest")
    with Program(strict=False) as prog:
        with Rung():
            copy(wrap(x[1]), dest)
    return hw, prog, (x[1],), (dest,)


def _copy_into_output(wrap: Callable[[Any], Any] = immediate):
    hw, x, y = _hardware()
    with Program(strict=False) as prog:
        with Rung():
            copy(x[1], wrap(y[1]))
    return hw, prog, (x[1],), (y[1],)


def _copy_to_text(wrap: Callable[[Any], Any] = immediate):
    hw, _x, _y = _hardware()
    src = Int("Src", default=42)
    txt = Block("Txt", TagType.CHAR, 1, 4)
    with Program(strict=False) as prog:
        with Rung():
            copy(wrap(src), wrap(txt[1]), convert=to_text())
            copy(txt[2], txt[4])
    return hw, prog, (), (txt[1], txt[4])


def _blockcopy_inputs(wrap: Callable[[Any], Any] = immediate):
    hw, x, _y = _hardware()
    bits = Block("Bits", TagType.BOOL, 1, 4)
    with Program(strict=False) as prog:
        with Rung():
            blockcopy(wrap(x.select(1, 3)), wrap(bits.select(1, 3)))
    return hw, prog, (x[1], x[2]), tuple(bits[i] for i in range(1, 4))


def _blockcopy_registers(wrap: Callable[[Any], Any] = immediate):
    hw, _x, _y = _hardware()
    ds = Block("DS", TagType.INT, 1, 8)
    with Program(strict=False) as prog:
        with Rung():
            copy(7, ds[1])
            copy(-3, ds[2])
            blockcopy(wrap(ds.select(1, 2)), wrap(ds.select(5, 6)))
    return hw, prog, (), (ds[5], ds[6])


def _fill_registers(wrap: Callable[[Any], Any] = immediate):
    hw, _x, _y = _hardware()
    src = Real("Src", default=2.5)
    ds = Block("DS", TagType.INT, 1, 4)
    with Program(strict=False) as prog:
        with Rung():
            fill(wrap(src), wrap(ds.select(1, 3)))
    return hw, prog, (), tuple(ds[i] for i in range(1, 4))


def _fill_outputs(wrap: Callable[[Any], Any] = immediate):
    hw, x, y = _hardware()
    with Program(strict=False) as prog:
        with Rung():
            fill(wrap(x[1]), wrap(y.select(1, 3)))
    return hw, prog, (x[1],), tuple(y[i] for i in range(1, 4))


def _pack_bits_inputs(wrap: Callable[[Any], Any] = immediate):
    hw, x, _y = _hardware()
    word = Word("Packed")
    with Program(strict=False) as prog:
        with Rung():
            pack_bits(wrap(x.select(1, 8)), wrap(word))
    return hw, prog, (x[1], x[2]), (word,)


def _unpack_to_outputs(wrap: Callable[[Any], Any] = immediate):
    hw, _x, y = _hardware()
    word = Word("Source", default=0b101)
    with Program(strict=False) as prog:
        with Rung():
            unpack_to_bits(wrap(word), wrap(y.select(1, 4)))
    return hw, prog, (), tuple(y[i] for i in range(1, 5))


def _pack_and_unpack_words(wrap: Callable[[Any], Any] = immediate):
    hw, _x, _y = _hardware()
    words = Block("Words", TagType.INT, 1, 4)
    packed = Dint("Packed")
    source = Dint("Source", default=0x00030004)
    with Program(strict=False) as prog:
        with Rung():
            copy(1, words[1])
            copy(2, words[2])
            pack_words(wrap(words.select(1, 2)), wrap(packed))
            unpack_to_words(wrap(source), wrap(words.select(3, 4)))
    return hw, prog, (), (packed, words[3], words[4])


def _pack_text_digits(wrap: Callable[[Any], Any] = immediate):
    hw, _x, _y = _hardware()
    txt = Block("Txt", TagType.CHAR, 1, 3)
    dest = Int("Dest")
    with Program(strict=False) as prog:
        with Rung():
            copy("1", txt[1])
            copy("2", txt[2])
            copy("3", txt[3])
            pack_text(wrap(txt.select(1, 3)), wrap(dest))
    return hw, prog, (), (dest,)


def _calc_of_an_immediate(wrap: Callable[[Any], Any] = immediate):
    hw, _x, _y = _hardware()
    src = Int("Src", default=9)
    dest = Dint("Dest")
    with Program(strict=False) as prog:
        with Rung():
            calc(wrap(src), dest)
    return hw, prog, (), (dest,)


def _search_for_an_immediate(wrap: Callable[[Any], Any] = immediate):
    hw, _x, _y = _hardware()
    ds = Block("DS", TagType.INT, 1, 4)
    wanted = Int("Wanted", default=5)
    result = Dint("Result")
    found = Bool("Found")
    with Program(strict=False) as prog:
        with Rung():
            copy(5, ds[3])
            search(ds.select(1, 4) == wrap(wanted), result=result, found=found)
    return hw, prog, (), (result, found)


def _double(a):
    return {"o": a * 2}


def _run_function_operands(wrap: Callable[[Any], Any] = immediate):
    hw, _x, _y = _hardware()
    src = Int("Src", default=21)
    dest = Int("Dest")
    with Program(strict=False) as prog:
        with Rung():
            run_function(_double, ins={"a": wrap(src)}, outs={"o": wrap(dest)})
    return hw, prog, (), (dest,)


_CASES: dict[str, Case] = {
    "copy Real into Dint": _copy_real_into_dint,
    "copy Int into Int": _copy_int_into_int,
    "copy input into Bool": _copy_input_into_bool,
    "copy into output": _copy_into_output,
    "copy to text": _copy_to_text,
    "blockcopy inputs": _blockcopy_inputs,
    "blockcopy registers": _blockcopy_registers,
    "fill registers": _fill_registers,
    "fill outputs": _fill_outputs,
    "pack_bits inputs": _pack_bits_inputs,
    "unpack_to_bits outputs": _unpack_to_outputs,
    "pack and unpack words": _pack_and_unpack_words,
    "pack_text digits": _pack_text_digits,
    "calc of an immediate": _calc_of_an_immediate,
    "search for an immediate": _search_for_an_immediate,
    "run_function operands": _run_function_operands,
}


@pytest.mark.parametrize("case", list(_CASES), ids=list(_CASES))
def test_immediate_operand_agrees_on_every_backend(case, monkeypatch):
    hw, prog, inputs, watch = _CASES[case]()
    results = _on_every_backend(prog, hw, inputs, watch, monkeypatch)
    assert results["compiled"] == results["interpreted"], results
    assert results["circuitpy"] == results["interpreted"], results


def _interpreted(program: Program, inputs: tuple[Tag, ...], watch: tuple[Tag, ...]) -> tuple:
    plc = PLC(program)
    for _ in range(2):
        plc.patch({tag: True for tag in inputs})
        plc.step()
    tags = plc.current_state.tags
    return tuple(tags.get(tag.name, tag.default) for tag in watch)


@pytest.mark.parametrize("case", list(_CASES), ids=list(_CASES))
def test_immediate_operand_reads_as_the_plain_one(case):
    """The interpreter's answer the other backends are held to is the unwrapped program's."""
    _hw, prog, inputs, watch = _CASES[case]()
    _hw, plain, plain_inputs, plain_watch = _CASES[case](wrap=lambda value: value)
    assert _interpreted(prog, inputs, watch) == _interpreted(plain, plain_inputs, plain_watch)
