"""The compiled kernel runs a pack, an unpack, a ``blockcopy()`` and a ``fill()`` as stepping does.

Each instruction resolves a pointer on each side.  A pointer can point below its bank,
inside it, across its end, past its end or into a gap, and a pointer range can run
backwards.  Stepping checks one side before the other, and the first problem it finds
decides whether the instruction sets the address-error flag, does nothing or raises.  The
kernel must check the sides in the same order.  Otherwise it must decline the program, so
that pyrung steps its scans.  The generated code.py must do what stepping does too.

The cases below pair each instruction with each pointer position on each side.
"""

from __future__ import annotations

import time
import warnings
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from pyrung import PLC, Program, rung
from pyrung.circuitpy import P1AM, RunStopConfig, generate_circuitpy
from pyrung.core import (
    Block,
    Int,
    TagType,
    blockcopy,
    fill,
    jit_stats,
    pack_bits,
    pack_text,
    pack_words,
    to_ascii,
    to_value,
    unpack_to_bits,
    unpack_to_words,
)
from pyrung.core.compiled_plc import CompiledPLC
from pyrung.core.system_points import system
from pyrung.core.tag import immediate
from tests.circuitpy.test_codegen import _context_for_program, _IdleBase, _run_single_scan_source

# Most registers start with something other than 0, so a write or a read shows.
BIT = Block("Bit", TagType.BOOL, 1, 6, default_factory=lambda addr: addr in (1, 3, 6))
WRD = Block("Wrd", TagType.INT, 1, 6, default_factory=lambda addr: (0, 123, -7, 9, 4, -5, 6)[addr])
DNT = Block(
    "Dnt", TagType.DINT, 1, 6, default_factory=lambda addr: (0, 70000, -5, 11, 1, 2, 3)[addr]
)
TXT = Block("Txt", TagType.CHAR, 1, 6, default_factory=lambda addr: " 5A9123"[addr])
# Two banks with no registers at 4 and 5.
GAP = ((1, 3), (6, 8))
SPB = Block(
    "Spb", TagType.BOOL, 1, 8, valid_ranges=GAP, default_factory=lambda addr: addr in (1, 6)
)
SPW = Block("Spw", TagType.INT, 1, 8, valid_ranges=GAP, default_factory=lambda addr: addr * 10)

REGISTERS = tuple(
    (block, addr)
    for block in (BIT, WRD, DNT, TXT, SPB, SPW)
    for addr in range(block.start, block.end + 1)
    if block.valid_ranges is None or not 4 <= addr <= 5
)
FLAGS = (system.fault.address_error, system.fault.out_of_range)
WATCHED = (*(block[addr] for block, addr in REGISTERS), *FLAGS)

# One side of an instruction: its label, a function that builds the side from the pointer
# tags, and the pointer values for each place that the side can sit.
Side = tuple[str, Callable[[dict[str, Any]], Any], dict[str, dict[str, int]]]


def _range_positions(block: Block, span: int) -> dict[str, tuple[int, int]]:
    """Return each place a pointer range of *span* registers can sit in *block*."""
    end = block.end
    positions = {
        "below": (0, span - 1),
        "negative": (-2, span - 3),
        "in": (1, span),
        "long": (1, span + 1),
        "across the end": (end - span + 2, end + 1),
        "past": (end + 1, end + span),
        "backwards": (span, 1),
    }
    if block.valid_ranges is not None:
        positions["from the gap"] = (4, 6)
        positions["in the gap"] = (4, 5)
    return positions


def _point_positions(block: Block) -> dict[str, int]:
    """Return each place a pointer can point in *block*."""
    end = block.end
    positions = {"below": 0, "negative": -1, "in": 1, "at the end": end, "past": end + 1}
    if block.valid_ranges is not None:
        positions["in the gap"] = 4
    return positions


# A pointer expression ``X + 1 - 1`` points where the pointer ``X`` points.  The table runs
# it at these places only, to keep the table short.
EXPRESSION_PLACES = {"below", "in", "past", "backwards", "in the gap"}


def _range_sides(block: Block, lo: str, hi: str, span: int) -> Iterator[Side]:
    """Yield each kind of range of *block* that one side of an instruction can name.

    The kinds are a fixed range, the same range read with ``immediate()``, a range between
    the pointers *lo* and *hi*, and a range whose first pointer is an expression.
    """
    bank = block.name.lower()
    yield f"{bank}[1..{span}]", lambda p: block.select(1, span), {"": {}}
    yield f"immediate({bank}[1..{span}])", lambda p: immediate(block.select(1, span)), {"": {}}
    places = {
        where: {lo: first, hi: last}
        for where, (first, last) in _range_positions(block, span).items()
    }
    yield f"{bank}[{lo}..{hi}]", lambda p: block.select(p[lo], p[hi]), places
    yield (
        f"{bank}[{lo}+1-1..{hi}]",
        lambda p: block.select(p[lo] + 1 - 1, p[hi]),
        {where: ptrs for where, ptrs in places.items() if where in EXPRESSION_PLACES},
    )


def _point_sides(block: Block, pointer: str) -> Iterator[Side]:
    """Yield register 1 of *block*, a pointer into *block* and a pointer expression into it."""
    bank = block.name.lower()
    yield f"{bank}[1]", lambda p: block[1], {"": {}}
    places = {where: {pointer: addr} for where, addr in _point_positions(block).items()}
    yield f"{bank}[{pointer}]", lambda p: block[p[pointer]], places
    yield (
        f"{bank}[{pointer}+1-1]",
        lambda p: block[p[pointer] + 1 - 1],
        {where: ptrs for where, ptrs in places.items() if where in EXPRESSION_PLACES},
    )


def _value_sides() -> Iterator[Side]:
    """Yield each value that ``fill()`` writes in the table: a constant, then registers."""
    yield "5", lambda p: 5, {"": {}}
    yield from _point_sides(WRD, "P")


# One case: a function that builds the instruction from the pointer tags, the pointer
# values, and whether the instruction's types are wrong.  pyrung builds a program with the
# wrong types, and stepping raises only when the instruction reaches the type check.  The
# kernel declines such a program, and generate_circuitpy() refuses it.
Case = tuple[Callable[[dict[str, Any]], Any], dict[str, int], bool]


def _pairs(
    name: str,
    build: Callable[[Any, Any], Any],
    firsts: Iterator[Side],
    seconds: Iterator[Side],
    *,
    wrong_type: bool = False,
) -> Iterator[tuple[str, Case]]:
    second_sides = list(seconds)
    for first_label, first, first_places in firsts:
        for second_label, second, second_places in second_sides:
            for first_where, first_ptrs in first_places.items():
                for second_where, second_ptrs in second_places.items():
                    ptrs = {**first_ptrs, **second_ptrs}
                    if not ptrs and "immediate" not in first_label + second_label:
                        continue
                    left = f"{first_label} {first_where}".strip()
                    right = f"{second_label} {second_where}".strip()
                    yield (
                        f"{name}({left}, {right})",
                        (
                            lambda p, first=first, second=second: build(first(p), second(p)),
                            ptrs,
                            wrong_type,
                        ),
                    )


def _cases() -> Iterator[tuple[str, Case]]:
    for name, convert in (("blockcopy", None), ("to_value", to_value), ("to_ascii", to_ascii)):
        yield from _pairs(
            name,
            lambda src, dst, convert=convert: blockcopy(src, dst, convert=convert),
            _range_sides(WRD if convert is None else TXT, "A", "B", 3),
            _range_sides(DNT if convert is None else WRD, "Q", "R", 3),
        )
    yield from _pairs(
        "blockcopy", blockcopy, _range_sides(SPW, "A", "B", 3), _range_sides(SPB, "Q", "R", 3)
    )
    yield from _pairs("fill", fill, _value_sides(), _range_sides(DNT, "Q", "R", 3))
    yield from _pairs("fill", fill, _value_sides(), _range_sides(SPW, "Q", "R", 3))
    bits = _range_sides(BIT, "A", "B", 3)
    yield from _pairs("pack_bits", pack_bits, bits, _point_sides(DNT, "Q"))
    gap_bits = _range_sides(SPB, "A", "B", 3)
    yield from _pairs("pack_bits", pack_bits, gap_bits, _point_sides(SPW, "Q"))
    words = list(_range_sides(WRD, "A", "B", 2))
    yield from _pairs("pack_words", pack_words, iter(words), _point_sides(DNT, "Q"))
    yield from _pairs(
        "pack_words", pack_words, iter(words), _point_sides(WRD, "Q"), wrong_type=True
    )
    texts = _range_sides(TXT, "A", "B", 3)
    yield from _pairs("pack_text", pack_text, texts, _point_sides(WRD, "Q"))
    bits = _range_sides(BIT, "A", "B", 3)
    yield from _pairs("unpack_to_bits", unpack_to_bits, _point_sides(DNT, "Q"), bits)
    gap_bits = _range_sides(SPB, "A", "B", 3)
    yield from _pairs("unpack_to_bits", unpack_to_bits, _point_sides(SPW, "Q"), gap_bits)
    yield from _pairs("unpack_to_words", unpack_to_words, _point_sides(DNT, "Q"), iter(words))
    yield from _pairs(
        "unpack_to_words", unpack_to_words, _point_sides(WRD, "Q"), iter(words), wrong_type=True
    )


def _program(case: Case) -> Program | None:
    """Return the program of one rung that runs *case*, or None if pyrung refuses it."""
    build, ptrs, _ = case
    pointers = {name: Int(name, default=value) for name, value in ptrs.items()}
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            with Program(strict=False) as program:
                with rung():
                    build(pointers)
    except (TypeError, ValueError):
        return None
    return program


CASES = {label: case for label, case in _cases() if _program(case) is not None}


def _watched(tags: Any) -> dict[str, Any]:
    """Return the value of each watched tag in *tags*, with a Bool as 0 or 1.

    The kernel holds only the tags that it wrote, so a missing tag reads as its default.
    code.py may store a Bool as either form.
    """
    values = {}
    for tag in WATCHED:
        value = tags.get(tag.name, tag.default)
        values[tag.name] = int(value) if isinstance(value, bool) else value
    return values


def _outcome(program: Program, runner: type) -> tuple[Any, ...]:
    """Return what one scan of *program* does on *runner*."""
    try:
        plc = runner(program, dt=0.01)
    except NotImplementedError:
        return ("declined",)
    try:
        plc.step()
    except Exception as exc:  # noqa: BLE001 - the test compares the class that each one raises.
        return ("raised", type(exc).__name__)
    return ("stored", _watched(plc.current_state.tags))


def _code_py_outcome(program: Program, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, ...]:
    """Return what one scan of the code.py that pyrung generates for *program* does."""
    hw = P1AM()
    hw.slot(1, "P1-08SIM")
    try:
        result = generate_circuitpy(
            program,
            hw,
            target_scan_ms=10.0,
            runstop=RunStopConfig(debounce_ms=0, run_when_high=False),
        )
    except (TypeError, ValueError):
        return ("refused",)
    # The scan loop sleeps out the rest of its 10 ms, and no case reads the clock.
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    try:
        namespace = _run_single_scan_source(
            result.code, monkeypatch, _IdleBase(), runtime_source=result.runtime
        )
    except Exception as exc:  # noqa: BLE001 - the test compares the class that each one raises.
        return ("raised", type(exc).__name__)
    ctx = _context_for_program(program, hw)
    tags = {flag.name: namespace[ctx.symbol_for_tag(flag)] for flag in FLAGS}
    for block, addr in REGISTERS:
        name = block[addr].name
        if name in ctx.symbol_table:
            # code.py stores a register that the program names only alone as a variable.
            tags[name] = namespace[ctx.symbol_table[name]]
        elif id(block) in ctx.block_symbols:
            index = ctx.block_index(id(block), addr)
            tags[name] = namespace[ctx.block_symbols[id(block)]][index]
    return ("stored", _watched(tags))


@pytest.mark.parametrize("label", list(CASES))
def test_the_kernel_runs_the_scan_as_stepping_does(label: str) -> None:
    case = CASES[label]
    program = _program(case)
    assert program is not None
    compiled = _outcome(program, CompiledPLC)
    if case[2]:
        assert compiled == ("declined",)
    else:
        assert compiled == _outcome(program, PLC)


@pytest.mark.parametrize("label", list(CASES))
def test_the_code_py_runs_the_scan_as_stepping_does(
    label: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = CASES[label]
    program = _program(case)
    assert program is not None
    code_py = _code_py_outcome(program, monkeypatch)
    if case[2]:
        assert code_py == ("refused",)
    else:
        assert code_py == _outcome(program, PLC)


def _long_run(label: str) -> Any:
    """Check that run_for() of 30 scans lands where 30 steps do, and return its stats."""
    program = _program(CASES[label])
    assert program is not None
    stepped = PLC(program, dt=0.01)
    for _ in range(30):
        stepped.step()
    plc = PLC(program, dt=0.01)
    with jit_stats.collect() as stats:
        plc.run_for(0.3)
    assert plc.current_state.scan_id == 30
    assert _watched(plc.current_state.tags) == _watched(stepped.current_state.tags)
    return stats


# In each case below, a pointer outside its bank decides what the instruction does before
# the other side is read.  A kernel that checks the sides in another order declines these
# runs, or sets the address-error flag in a scan where stepping does not.
LONG_RUN_CASES = [
    "pack_bits(bit[A..B] below, dnt[Q] past)",
    "pack_bits(bit[A..B] backwards, dnt[Q] below)",
    "unpack_to_bits(dnt[Q] below, bit[A..B] backwards)",
    "unpack_to_words(dnt[Q] below, wrd[A..B] long)",
    "blockcopy(wrd[A..B] backwards, dnt[Q..R] below)",
    "to_value(txt[A..B] backwards, wrd[Q..R] below)",
]


@pytest.mark.parametrize("label", LONG_RUN_CASES)
def test_a_long_run_runs_in_the_kernel_and_lands_where_stepping_does(
    label: str, loop_from_first_scan: None
) -> None:
    stats = _long_run(label)
    # Only the first scan steps.
    assert stats.scans["interpreted"] == 1
    assert stats.scans["undone"] == 0


def test_a_long_run_of_a_wrong_type_steps_with_no_warning(loop_from_first_scan: None) -> None:
    with warnings.catch_warnings():
        # The kernel declines without the warning about a bug in pyrung.
        warnings.simplefilter("error", RuntimeWarning)
        stats = _long_run("pack_words(wrd[A..B] in, wrd[Q] past)")
    assert stats.scans["interpreted"] == 30
