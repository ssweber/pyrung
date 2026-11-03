"""A pointer reads the same memory a slot's name writes, whatever compiled first.

A slot of a block can carry a name of its own: ``Tag.map_to``, ``Block.map_to``,
``slot(name=...)`` or a tag of the same spelling.  Memory is keyed by name, so the
name and ``block[pointer]`` are one cell in the interpreter.  Code generation used
to learn the name from ``Block._tag_cache``, which holds only the slots something
has indexed so far: a kernel (or generated code) compiled before a scan reached the
slot through the pointer kept the name in a scalar of its own and the slot in the
block array, and read a blank through the pointer.
"""

from __future__ import annotations

from typing import Any

import pytest

from pyrung.circuitpy import P1AM, generate_circuitpy
from pyrung.circuitpy.codegen import compile_kernel
from pyrung.core import (
    PLC,
    Block,
    Bool,
    CompiledPLC,
    Int,
    Program,
    Rung,
    TagType,
    calc,
    copy,
    out,
)
from pyrung.core.analysis.prove.kernel import _step_compiled_kernel
from pyrung.core.kernel import CompiledKernel
from tests.circuitpy.test_codegen import _run_single_scan_source
from tests.core.test_indirect_expr_compare import _StubBase

SCANS = 12


def _tag_mapped(slots: Block) -> Int:
    name = Int("Cp")
    name.map_to(slots[3])
    return name


def _block_mapped(slots: Block) -> Any:
    names = Block("Sn", TagType.INT, 1, 4)
    names.map_to(slots.select(1, 4))
    return names[3]


def _slot_named(slots: Block) -> Int:
    slots.slot(3, name="Nm")
    return Int("Nm")


def _same_spelling(slots: Block) -> Int:
    return Int("Cb3")


NAMES = [
    pytest.param(_tag_mapped, id="tag-map_to"),
    pytest.param(_block_mapped, id="block-map_to"),
    pytest.param(_slot_named, id="slot-name"),
    pytest.param(_same_spelling, id="same-spelling"),
]


def _program(name_slot: Any, *, pointer: int = 3) -> Program:
    """``Seen`` reads through the pointer what a rung above wrote through the name."""
    slots = Block("Cb", TagType.INT, 1, 8)
    index = Int("Jx", default=pointer)
    count = Int("N")
    seen = Int("Seen")
    name = name_slot(slots)
    with Program(strict=False) as program:
        with Rung():
            calc(count + 1, count)
        with Rung():
            copy(count, name)
        with Rung():
            copy(slots[index], seen)
    return program


def _recording(program: Program, arrivals: dict[int, dict[str, int]] | None = None) -> list[dict]:
    """The tags as a stepped twin recorded them, by scan id."""
    plc = PLC(program, dt=0.010)
    recorded = [dict(plc.state.tags)]
    for scan in range(SCANS):
        if arrivals and scan in arrivals:
            plc.patch(arrivals[scan])
        plc.step()
        recorded.append(dict(plc.state.tags))
    return recorded


def _assert_replay_equals_stepping(plc: PLC, recorded: list[dict[str, Any]]) -> None:
    assert isinstance(plc._compiled_replay_supported_kernel(), CompiledKernel)
    assert plc._initial_scan_id == 0
    # Scan 0 ran nothing: a replay seeds the fault flags a recording only has from
    # scan 1 on.  What the recording holds it must still hold.
    assert {name: plc.replay_to(0).state.tags[name] for name in recorded[0]} == recorded[0]
    for scan_id in range(1, len(recorded)):
        assert dict(plc.replay_to(scan_id).state.tags) == recorded[scan_id], scan_id


@pytest.mark.parametrize("name_slot", NAMES)
def test_replay_compiled_before_the_first_scan_equals_stepping(name_slot: Any) -> None:
    plc = PLC(_program(name_slot), dt=0.010)
    plc.replay_to(0)
    plc.run(SCANS)

    recorded = _recording(_program(name_slot))
    assert recorded[-1]["Seen"] == SCANS
    _assert_replay_equals_stepping(plc, recorded)


@pytest.mark.parametrize("name_slot", NAMES)
def test_replay_compiled_before_the_pointer_arrives_equals_stepping(name_slot: Any) -> None:
    """Not about the first scan: the kernel outlives every slot it has not seen yet."""
    arrives = {6: {"Jx": 3}}
    plc = PLC(_program(name_slot, pointer=1), dt=0.010)
    for scan in range(SCANS):
        if scan == 3:
            plc.replay_to(2)
        if scan in arrives:
            plc.patch(arrives[scan])
        plc.step()

    recorded = _recording(_program(name_slot, pointer=1), arrives)
    assert recorded[-1]["Seen"] == SCANS
    _assert_replay_equals_stepping(plc, recorded)


@pytest.mark.parametrize("blockless", [False, True], ids=["blocks", "blockless"])
@pytest.mark.parametrize("name_slot", NAMES)
def test_compile_does_not_depend_on_what_has_been_indexed(name_slot: Any, blockless: bool) -> None:
    program = _program(name_slot)
    before = compile_kernel(program, blockless=blockless).source
    PLC(program, dt=0.010).run(SCANS)
    assert compile_kernel(program, blockless=blockless).source == before


@pytest.mark.parametrize("name_slot", NAMES)
@pytest.mark.parametrize("engine", ["interpreted", "compiled", "kernel_legacy", "kernel_blockless"])
def test_every_engine_reads_the_name_through_the_pointer(name_slot: Any, engine: str) -> None:
    program = _program(name_slot)
    if engine in ("interpreted", "compiled"):
        runner = (PLC if engine == "interpreted" else CompiledPLC)(program, dt=0.010)
        runner.run(3)
        assert runner.current_state.tags["Seen"] == 3
        return
    compiled = compile_kernel(program, blockless=engine == "kernel_blockless")
    kernel = compiled.create_kernel()
    for _ in range(3):
        _step_compiled_kernel(compiled, kernel, dt=0.010)
    assert kernel.tags["Seen"] == 3


@pytest.mark.parametrize("name_slot", NAMES)
def test_generated_circuitpy_reads_the_name_through_the_pointer(
    name_slot: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    hw = P1AM()
    hw.slot(1, "P1-08SIM")
    result = generate_circuitpy(_program(name_slot), hw, target_scan_ms=10.0, runstop=None)
    namespace = _run_single_scan_source(
        result.code, monkeypatch, _StubBase(), runtime_source=result.runtime, scans=3
    )
    assert namespace["_t_Seen"] == 3


class _InputOn(_StubBase):
    def __init__(self) -> None:
        self.discrete_writes: list[tuple[int, int]] = []

    def readDiscrete(self, slot: int) -> int:
        return 0b10 if slot == 1 else 0

    def writeDiscrete(self, value: int, slot: int) -> None:
        self.discrete_writes.append((slot, value))


def test_generated_circuitpy_reads_a_name_on_a_hardware_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A name on an input slot nothing has indexed still reads the module."""
    hw = P1AM()
    inputs = hw.slot(1, "P1-08SIM")
    outputs = hw.slot(2, "P1-08TRS")
    button = Bool("Fl")
    button.map_to(inputs[2])
    with Program(strict=False) as program:
        with Rung(button):
            out(outputs[1])

    result = generate_circuitpy(program, hw, target_scan_ms=10.0, runstop=None)
    stub = _InputOn()
    _run_single_scan_source(result.code, monkeypatch, stub, runtime_source=result.runtime)
    assert stub.discrete_writes[-1] == (2, 1)
