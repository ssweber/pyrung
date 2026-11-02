"""Nothing but the program has to keep a block alive for it to generate.

``Block._all_instances`` is weak, so a block lives only while something reachable
holds it.  A program holds its blocks through what its rungs name: a slot's tag, a
range, a pointer.  These build the program in a helper, so the block's own variable
is gone by the time anything is generated, and compare with the same program whose
block is held on purpose.
"""

from __future__ import annotations

import gc
import weakref
from collections.abc import Callable
from typing import cast

import pytest

from pyrung.circuitpy import P1AM, generate_circuitpy
from pyrung.core import PLC, Block, Int, Program, Rung, TagType, copy, fill
from tests.circuitpy.test_codegen import _context_for_program, _run_single_scan_source
from tests.core.test_fault_flags import _StubBase

Shape = Callable[[Block], None]


def _slot(block: Block) -> None:
    copy(7, block[2])


def _range(block: Block) -> None:
    fill(7, block.select(2, 3))


def _pointer(block: Block) -> None:
    copy(7, block[Int("LifeAt", default=2)])


def _pointer_expression(block: Block) -> None:
    at = Int("LifeAt", default=1)
    copy(7, block[at + 1])


def _window(block: Block) -> None:
    fill(7, block.select(Int("LifeFrom", default=2), Int("LifeTo", default=3)))


SHAPES = [
    pytest.param(_slot, id="slot"),
    pytest.param(_range, id="range"),
    pytest.param(_pointer, id="pointer"),
    pytest.param(_pointer_expression, id="pointer-expression"),
    pytest.param(_window, id="window"),
]


def _program(shape: Shape, held: list[Block] | None = None) -> tuple[Program, weakref.ref[Block]]:
    block = Block("Life", TagType.INT, 1, 4)
    if held is not None:
        held.append(block)
    with Program(strict=False) as program:
        with Rung():
            shape(block)
    return program, weakref.ref(block)


def _hardware() -> P1AM:
    hw = P1AM()
    hw.slot(1, "P1-08SIM")
    return hw


def test_a_block_nothing_holds_is_let_go() -> None:
    gone = weakref.ref(Block("Gone", TagType.INT, 1, 4))
    gc.collect()
    assert gone() is None


@pytest.mark.parametrize("shape", SHAPES)
def test_a_program_alone_generates_what_it_does_with_its_block_held(
    shape: Shape, monkeypatch: pytest.MonkeyPatch
) -> None:
    program, block_ref = _program(shape)
    gc.collect()
    assert block_ref() is not None, "the program is what holds its block"

    hw = _hardware()
    result = generate_circuitpy(program, hw, target_scan_ms=10.0, runstop=None)
    held: list[Block] = []
    twin, _ = _program(shape, held)
    assert (
        result.code == generate_circuitpy(twin, _hardware(), target_scan_ms=10.0, runstop=None).code
    )

    # What it does, not just what it says: the slot reads as stepping reads it.
    namespace = _run_single_scan_source(
        result.code, monkeypatch, _StubBase(), runtime_source=result.runtime
    )
    plc = PLC(program, dt=0.010)
    plc.step()
    assert plc.current_state.tags["Life2"] == 7

    slot = cast(Block, block_ref())[2]
    ctx = _context_for_program(program, hw)
    ctx.ensure_tag_referenced(slot)
    name, bracket, index = ctx.symbol_for_tag(slot).partition("[")
    assert (namespace[name][int(index[:-1])] if bracket else namespace[name]) == 7


def test_a_name_mapped_onto_a_block_only_the_map_held() -> None:
    """A tag's ``map_to`` target is no part of what the program reads or writes.

    The block goes once the mapping is dropped, and the name is a register of its own
    either way: what generates does not depend on the block having stayed.
    """

    def build(held: list[Block] | None = None) -> tuple[Program, weakref.ref[Block]]:
        slots = Block("LifeMap", TagType.INT, 1, 4)
        if held is not None:
            held.append(slots)
        name = Int("LifeName")
        name.map_to(slots[3])
        with Program(strict=False) as program:
            with Rung():
                copy(7, name)
        return program, weakref.ref(slots)

    program, slots_ref = build()
    gc.collect()
    assert slots_ref() is None

    held: list[Block] = []
    twin, _ = build(held)
    assert (
        generate_circuitpy(program, _hardware(), target_scan_ms=10.0, runstop=None).code
        == generate_circuitpy(twin, _hardware(), target_scan_ms=10.0, runstop=None).code
    )
