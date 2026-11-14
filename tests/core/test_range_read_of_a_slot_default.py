"""A slot read only through a range reads its declared default, on every path.

A slot's default is its power-on value.  Memory is keyed by name, and a slot nothing
names directly never reaches the runner's state: the fallback the read passes decides
what it reads.  ``SumExpr.evaluate`` passed 0, so interpreted scans summed a blank where
the kernel and generated code read the default, and a run that went on in the scan loop
gained more per scan than one stepped.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from pyrung.circuitpy import P1AM, generate_circuitpy
from pyrung.core import (
    PLC,
    Block,
    Bool,
    CompiledPLC,
    Int,
    Program,
    Real,
    Rung,
    TagType,
    copy,
    math,
    out,
)
from tests.circuitpy.test_codegen import _run_single_scan_source
from tests.core.scan_loop_counts import NEVER, ScansOutsideLoop
from tests.core.test_indirect_expr_compare import _StubBase

# Past the prefix a long run takes interpreted, so the rest of it runs in the loop.
LOOP_SCANS = 2000
STEPPED_SCANS = 6


def _factory_slots() -> Block:
    """Slots 3, 6, 9 from a factory."""
    return Block("DS", TagType.INT, 1, 3, default_factory=lambda addr: addr * 3)


def _plain_slots() -> Block:
    """Slots 0, 7, 0: one plain ``default=``."""
    slots = Block("DS", TagType.INT, 1, 3)
    slots.slot(2, default=7)
    return slots


def _bool_slots() -> Block:
    """Off, on, off: a sum over Bool slots counts the ones that are on."""
    slots = Block("C", TagType.BOOL, 1, 3)
    slots.slot(2, default=True)
    return slots


def _real_slots() -> Block:
    """Slots 0.5, 0.25, 0.75: exact in a float, so nothing rounds differently."""
    return Block(
        "DF", TagType.REAL, 1, 3, default_factory=lambda addr: {1: 0.5, 2: 0.25}.get(addr, 0.75)
    )


def _calc_on(kind: str, slots: Callable[[], Block]) -> Callable[[], Program]:
    def build() -> Program:
        block = slots()
        with Program(strict=False) as program:
            with Rung():
                math(getattr(block.select(1, 3), kind)() + 1, Int("Seen"))
        return program

    return build


def _sum_on_the_rail(slots: Callable[[], Block]) -> Callable[[], Program]:
    def build() -> Program:
        block = slots()
        with Program(strict=False) as program:
            with Rung(block.select(1, 3).sum() > 5):
                out(Bool("By"))
        return program

    return build


def _sum_as_a_copy_source(slots: Callable[[], Block]) -> Callable[[], Program]:
    def build() -> Program:
        block = slots()
        with Program(strict=False) as program:
            with Rung():
                copy(block.select(1, 3).sum(), Int("Seen"))
        return program

    return build


def _real_sum() -> Program:
    block = _real_slots()
    with Program(strict=False) as program:
        with Rung():
            math(block.select(1, 3).sum(), Real("Seen"))
    return program


def _sum_into_its_own_range() -> Program:
    """The program that showed it: DS2 is read through the range and nowhere else."""
    block = Block("DS", TagType.INT, 1, 2, default_factory=lambda _addr: 9)
    with Program(strict=False) as program:
        with Rung():
            math(block.select(1, 2).sum() + 1, block[1])
    return program


# The value is what a scan must store, worked out from the defaults by hand.
ROWS = [
    pytest.param(_sum_into_its_own_range, "DS1", 19, id="sum-into-its-own-range"),
    pytest.param(_calc_on("sum", _factory_slots), "Seen", 19, id="sum-factory"),
    pytest.param(_calc_on("sum", _plain_slots), "Seen", 8, id="sum-plain"),
    pytest.param(_calc_on("sum", _bool_slots), "Seen", 2, id="sum-bool"),
    pytest.param(_real_sum, "Seen", 1.5, id="sum-real"),
    pytest.param(_calc_on("max", _factory_slots), "Seen", 10, id="max-factory"),
    pytest.param(_calc_on("max", _plain_slots), "Seen", 8, id="max-plain"),
    pytest.param(_calc_on("min", _factory_slots), "Seen", 4, id="min-factory"),
    pytest.param(_calc_on("min", _plain_slots), "Seen", 1, id="min-plain"),
    pytest.param(_calc_on("median", _factory_slots), "Seen", 7, id="median-factory"),
    pytest.param(_calc_on("median", _plain_slots), "Seen", 1, id="median-plain"),
    pytest.param(_calc_on("mean", _factory_slots), "Seen", 7, id="mean-factory"),
    pytest.param(_calc_on("mean", _plain_slots), "Seen", 3, id="mean-plain"),
    pytest.param(_sum_on_the_rail(_factory_slots), "By", True, id="sum-on-the-rail-factory"),
    pytest.param(_sum_on_the_rail(_plain_slots), "By", True, id="sum-on-the-rail-plain"),
    pytest.param(_sum_as_a_copy_source(_factory_slots), "Seen", 18, id="sum-copy-factory"),
    pytest.param(_sum_as_a_copy_source(_plain_slots), "Seen", 7, id="sum-copy-plain"),
]


def _generated_scan(program: Program, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    hardware = P1AM()
    hardware.slot(1, "P1-08SIM")
    result = generate_circuitpy(program, hardware, target_scan_ms=10.0, runstop=None)
    return _run_single_scan_source(
        result.code, monkeypatch, _StubBase(), runtime_source=result.runtime
    )


@pytest.mark.parametrize(("build", "watched", "first_scan"), ROWS)
def test_interpreted_and_compiled_agree_from_the_first_scan(
    build: Callable[[], Program], watched: str, first_scan: Any
) -> None:
    interpreted = PLC(build(), dt=0.010)
    compiled = CompiledPLC(build(), dt=0.010)
    interpreted.step()
    compiled.step()
    assert interpreted.current_state.tags[watched] == first_scan
    assert compiled.current_state.tags[watched] == first_scan
    for scan in range(STEPPED_SCANS):
        interpreted.step()
        compiled.step()
        assert compiled.current_state.tags[watched] == interpreted.current_state.tags[watched], scan


@pytest.mark.parametrize(("build", "watched", "first_scan"), ROWS)
def test_generated_code_reads_the_same_default(
    build: Callable[[], Program],
    watched: str,
    first_scan: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = _generated_scan(build(), monkeypatch)
    # DS1 lives in the block's array; the other two are scalars of their own.
    stored = namespace["_b_DS"][0] if watched == "DS1" else namespace[f"_t_{watched}"]
    assert stored == first_scan


@pytest.mark.parametrize(("build", "watched", "first_scan"), ROWS)
def test_a_long_run_lands_where_stepping_does(
    build: Callable[[], Program], watched: str, first_scan: Any
) -> None:
    """Under ``--fold-check`` the first run is also compared with its twin, scan by scan."""
    in_the_loop = PLC(build(), dt=0.010)
    outside = ScansOutsideLoop(in_the_loop)
    in_the_loop.run_until(NEVER, max_cycles=LOOP_SCANS)
    assert outside.n < LOOP_SCANS, "the run has to reach the scan loop to say anything about it"

    stepped = PLC(build(), dt=0.010)
    stepped.run_until(NEVER, max_cycles=LOOP_SCANS, jit=False)
    compiled = CompiledPLC(build(), dt=0.010)
    for _ in range(LOOP_SCANS):
        compiled.step()

    assert in_the_loop.state.scan_id == stepped.state.scan_id
    assert dict(in_the_loop.state.tags) == dict(stepped.state.tags)
    assert compiled.current_state.tags[watched] == stepped.state.tags[watched]


def test_the_program_that_showed_it_gains_ten_per_scan() -> None:
    """One from the sum's own slot on top of DS2's 9, however the run got there."""
    for jit in (True, False):
        plc = PLC(_sum_into_its_own_range(), dt=0.010)
        plc.run_until(NEVER, max_cycles=LOOP_SCANS, jit=jit)
        assert plc.state.tags["DS1"] == 9 + 10 * LOOP_SCANS, jit


@pytest.mark.parametrize("runner", [PLC, CompiledPLC])
def test_a_written_slot_reads_what_was_written(runner: Any) -> None:
    """The default is the power-on value: it stops counting once the slot holds something."""
    plc = runner(_calc_on("sum", _factory_slots)(), dt=0.010)
    plc.step()
    assert plc.current_state.tags["Seen"] == 19
    plc.patch({"DS2": 4})
    plc.step()
    assert plc.current_state.tags["Seen"] == 3 + 4 + 9 + 1
    plc.step()
    assert plc.current_state.tags["Seen"] == 3 + 4 + 9 + 1
