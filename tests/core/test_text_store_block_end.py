"""A store that writes several elements stops at the end of its block.

A text (``to_text()``, a Real copied into a TXT, a string literal) or a text's digits
(``to_value``) go into the target and the elements after it.  Past the end of the block
the rest are dropped, with no flag and no tag past the block, as a single write past the
end is.  A target below the block keeps its address error.  What a CLICK does at the end
of its TXT bank is not yet read: writing what fits is pyrung's choice.

Every case runs on the interpreter, the compiled kernel (legacy and blockless) and the
generated CircuitPython code, and they agree.
"""

from __future__ import annotations

from typing import Any

import pytest

from pyrung.circuitpy import P1AM, generate_circuitpy
from pyrung.circuitpy.codegen import compile_kernel
from pyrung.click import ClickBlocks, TagMap
from pyrung.core import (
    PLC,
    Block,
    Bool,
    Char,
    CompiledPLC,
    Int,
    Program,
    Rung,
    TagType,
    copy,
    fill,
    out,
    system,
    to_text,
    to_value,
)
from pyrung.core.analysis.prove.kernel import _step_compiled_kernel
from pyrung.core.analysis.write_sites import instruction_write_targets
from pyrung.core.storage import BLANK_CHAR
from tests.circuitpy.test_codegen import _context_for_program, _run_single_scan_source
from tests.core.test_out_of_range_index import _StubBase

_ENGINES = ["interpreted", "compiled", "kernel_legacy", "kernel_blockless", "circuitpy"]

SC43 = system.fault.out_of_range
SC44 = system.fault.address_error


def _scan(
    prog: Program, engine: str, watch: list[Any], monkeypatch
) -> tuple[dict[str, Any], set[str]]:
    """*watch*'s values after one scan of *prog* on *engine*, and every tag name it holds.

    Generated CircuitPython holds only the tags the program names, so its name set is
    empty: what it stores past the block is no tag at all.
    """
    names: set[str] = set()
    if engine in ("interpreted", "compiled"):
        runner = (PLC if engine == "interpreted" else CompiledPLC)(prog, dt=0.010)
        runner.step()
        state = runner.current_state.tags
        return {tag.name: state.get(tag.name, tag.default) for tag in watch}, set(state)
    if engine in ("kernel_legacy", "kernel_blockless"):
        compiled = compile_kernel(prog, blockless=engine == "kernel_blockless")
        kernel = compiled.create_kernel()
        _step_compiled_kernel(compiled, kernel, dt=0.010)
        # A blockless kernel holds only the tags the program names or writes.
        tags = kernel.tags
        return {tag.name: tags.get(tag.name, tag.default) for tag in watch}, set(tags)
    hw = P1AM()
    hw.slot(1, "P1-08SIM")
    result = generate_circuitpy(prog, hw, target_scan_ms=10.0, runstop=None)
    namespace = _run_single_scan_source(result.code, monkeypatch, _StubBase())
    ctx = _context_for_program(prog, hw)
    values = {tag.name: eval(ctx.symbol_for_tag(tag), namespace) for tag in watch}  # noqa: S307
    return values, names


def _watched(block: Block, first: int, last: int) -> list[Any]:
    return [block[addr] for addr in range(first, last + 1)]


def _past(names: set[str], prefix: str, end: int) -> list[str]:
    """The tags in *names* of the block *prefix* past its last address *end*."""
    found = []
    for name in names:
        suffix = name[len(prefix) :]
        if name.startswith(prefix) and suffix.isdigit() and int(suffix) > end:
            found.append(name)
    return sorted(found)


@pytest.mark.parametrize("engine", _ENGINES)
@pytest.mark.parametrize("form", ["address", "pointer", "plain"])
@pytest.mark.parametrize(
    ("start", "text"),
    [(995, "1.5000"), (998, "1.5"), (1000, "1")],
    ids=["six_fit", "three_fit", "last"],
)
def test_a_real_into_text_writes_what_fits(engine, form, start, text, monkeypatch):
    """``1.5`` is "1.5000000": the TXT bank's 1000 ends it, and nothing is written past.

    By address, by a DS pointer, and as a plain Copy (which converts a Real to text).
    """
    blocks = ClickBlocks()
    txt, df, ds = blocks.txt, blocks.df, blocks.ds
    seen = Bool("Seen")
    watch = _watched(txt, 994, 1000)

    with Program(strict=False) as prog:
        with Rung():
            copy(1.5, df[1])
            copy(start, ds[1])
            if form == "pointer":
                copy(df[1], txt[ds[1]], convert=to_text())
            elif form == "address":
                copy(df[1], txt[start], convert=to_text())
            else:
                copy(df[1], txt[start])
        with Rung(*(tag == "#" for tag in watch)):
            out(seen)

    values, names = _scan(prog, engine, [*watch, SC43, SC44], monkeypatch)
    offset = start - 994
    wanted = [BLANK_CHAR] * offset + list(text)
    assert [values[tag.name] for tag in watch] == wanted
    assert (bool(values[SC43.name]), bool(values[SC44.name])) == (False, False)
    assert _past(names, "TXT", 1000) == []


@pytest.mark.parametrize("engine", _ENGINES)
def test_a_text_literal_writes_what_fits(engine, monkeypatch):
    blocks = ClickBlocks()
    txt = blocks.txt
    watch = _watched(txt, 996, 1000)
    with Program(strict=False) as prog:
        with Rung():
            copy("ABCDEFG", txt[997])
        with Rung(*(tag == "#" for tag in watch)):
            out(Bool("Seen"))

    values, names = _scan(prog, engine, [*watch, SC43], monkeypatch)
    assert [values[tag.name] for tag in watch] == [BLANK_CHAR, "A", "B", "C", "D"]
    assert bool(values[SC43.name]) is False
    assert _past(names, "TXT", 1000) == []


@pytest.mark.parametrize("engine", _ENGINES)
def test_digits_into_numbers_write_what_fits(engine, monkeypatch):
    """``to_value`` puts one digit in each DS; DS4500 ends the bank."""
    blocks = ClickBlocks()
    ds, txt = blocks.ds, blocks.txt
    watch = _watched(ds, 4498, 4500)
    with Program(strict=False) as prog:
        with Rung():
            copy("123", txt[1])
            copy(txt[1], ds[4499], convert=to_value)
            copy("1234", ds[4499], convert=to_value)
        with Rung(*(tag == 9 for tag in watch)):
            out(Bool("Seen"))

    values, names = _scan(prog, engine, [*watch, SC43], monkeypatch)
    assert [values[tag.name] for tag in watch] == [0, 1, 2]
    assert bool(values[SC43.name]) is False
    assert _past(names, "DS", 4500) == []


@pytest.mark.parametrize("engine", _ENGINES)
def test_a_bad_digit_past_the_end_still_faults(engine, monkeypatch):
    """The text is converted whole before it is stored: a character that is no digit
    leaves every element and raises SC43, past the end too (pyrung's choice)."""
    blocks = ClickBlocks()
    ds = blocks.ds
    watch = _watched(ds, 4499, 4500)
    with Program(strict=False) as prog:
        with Rung():
            copy(7, ds[4499])
            copy("12x", ds[4499], convert=to_value)
        with Rung(*(tag == 9 for tag in watch)):
            out(Bool("Seen"))

    values, _names = _scan(prog, engine, [*watch, SC43], monkeypatch)
    assert [values[tag.name] for tag in watch] == [7, 0]
    assert bool(values[SC43.name]) is True


@pytest.mark.parametrize("engine", _ENGINES)
@pytest.mark.parametrize(("pointer", "flag"), [(0, True), (1001, False)], ids=["below", "past"])
def test_a_text_from_outside_the_block_writes_nothing(engine, pointer, flag, monkeypatch):
    """Below the bank: SC44.  Past it: dropped, no flag (unchanged)."""
    blocks = ClickBlocks()
    txt, ds = blocks.txt, blocks.ds
    watch = _watched(txt, 1, 2)
    with Program(strict=False) as prog:
        with Rung():
            copy(pointer, ds[1])
            copy("ABC", txt[ds[1]])
        with Rung(*(tag == "#" for tag in watch)):
            out(Bool("Seen"))

    values, names = _scan(prog, engine, [*watch, SC43, SC44], monkeypatch)
    assert [values[tag.name] for tag in watch] == [BLANK_CHAR, BLANK_CHAR]
    assert (bool(values[SC43.name]), bool(values[SC44.name])) == (False, flag)
    assert _past(names, "TXT", 1000) == []


@pytest.mark.parametrize("engine", _ENGINES)
def test_a_block_of_ones_own_ends_the_text(engine, monkeypatch):
    """Any block ends a text, and a range over it elsewhere keeps the store by address."""
    msg = Block("Msg", TagType.CHAR, 1, 5)
    watch = _watched(msg, 1, 5)
    with Program(strict=False) as prog:
        with Rung():
            fill("x", msg.select(1, 1))
            copy("ABCDEFG", msg[3])
        with Rung(msg[5] == "#"):
            out(Bool("Seen"))

    # Generated CircuitPython holds only the elements the program names.
    if engine == "circuitpy":
        watch = [msg[1], msg[5]]
    values, names = _scan(prog, engine, [*watch, SC43], monkeypatch)
    wanted = {"Msg1": "x", "Msg2": BLANK_CHAR, "Msg3": "A", "Msg4": "B", "Msg5": "C"}
    assert {tag.name: values[tag.name] for tag in watch} == {
        tag.name: wanted[tag.name] for tag in watch
    }
    assert bool(values[SC43.name]) is False
    assert _past(names, "Msg", 5) == []


def test_the_write_sites_of_a_text_stop_at_the_block_end():
    """Analyses see the elements a literal writes, none past its block."""
    txt = ClickBlocks().txt
    with Program(strict=False) as prog:
        with Rung():
            copy("ABCDEFG", txt[997])
    (instr,) = prog.rungs[0]._execution_items
    names = [getattr(target, "name", None) for target in instruction_write_targets(instr)]
    assert names == ["TXT997", "TXT998", "TXT999", "TXT1000"]


@pytest.mark.parametrize("engine", _ENGINES)
def test_a_text_into_a_bank_of_another_blocks_object_keeps_its_addresses(engine, monkeypatch):
    """Two ``ClickBlocks()`` are one TXT bank: a range over one still lets a text
    into the other land at its own addresses, TXT2 too, which no rung names."""
    first, second = ClickBlocks(), ClickBlocks()
    watch = [second.txt[1], first.txt[5]]
    with Program(strict=False) as prog:
        with Rung():
            fill("x", first.txt.select(5, 5))
            copy("AB", second.txt[1])
        with Rung(*(tag == "#" for tag in watch)):
            out(Bool("Seen"))

    # Generated CircuitPython holds only the elements the program names.
    if engine != "circuitpy":
        watch.insert(1, second.txt[2])
    values, _names = _scan(prog, engine, [*watch, SC43], monkeypatch)
    wanted = {"TXT1": "A", "TXT2": "B", "TXT5": "x"}
    assert {tag.name: values[tag.name] for tag in watch} == {
        tag.name: wanted[tag.name] for tag in watch
    }
    assert bool(values[SC43.name]) is False


@pytest.mark.parametrize("engine", _ENGINES)
def test_a_text_writes_the_tags_that_hold_the_addresses(engine, monkeypatch):
    """A block mapped onto the bank, and a named slot, hold their addresses: a text
    run over them writes those tags, not a tag named for the address."""
    blocks = ClickBlocks()
    txt = blocks.txt
    msg = Block("Msg", TagType.CHAR, 1, 10)
    msg.map_to(txt.select(101, 110))
    txt.slot(996, name="Unit")
    unit = txt[996]
    watch = [txt[100], msg[1], msg[2], txt[995], unit, txt[997], txt[998]]
    with Program(strict=False) as prog:
        with Rung():
            copy("ABC", txt[100])
            copy("DE", txt[995])
            copy("FG", unit)
        with Rung(*(tag == "#" for tag in watch)):
            out(Bool("Seen"))

    values, names = _scan(prog, engine, [*watch, SC43], monkeypatch)
    assert [values[tag.name] for tag in watch] == ["A", "B", "C", "D", "F", "G", BLANK_CHAR]
    assert bool(values[SC43.name]) is False
    assert not {"TXT101", "TXT102", "TXT996"} & names


@pytest.mark.parametrize("engine", _ENGINES)
@pytest.mark.parametrize("bank_read_first", [False, True], ids=["fresh", "bank_read"])
def test_a_text_into_a_mapped_block_ends_with_that_block(engine, bank_read_first, monkeypatch):
    """Msg on TXT101..110 ends a text at Msg10, though a CLICK writes on into TXT111
    (CLK_TEXT_PAST_BLOCK says so).  Reading the bank's address first, which makes
    the bank the slot's block, changes nothing."""
    blocks = ClickBlocks()
    txt = blocks.txt
    msg = Block("Msg", TagType.CHAR, 1, 10)
    msg.map_to(txt.select(101, 110))
    if bank_read_first:
        assert txt[109] is msg[9]
    watch = [msg[9], msg[10], txt[111]]
    with Program(strict=False) as prog:
        with Rung():
            copy("ABC", msg[9])
        with Rung(*(tag == "#" for tag in watch)):
            out(Bool("Seen"))

    values, names = _scan(prog, engine, [*watch, SC43], monkeypatch)
    assert [values[tag.name] for tag in watch] == ["A", "B", BLANK_CHAR]
    assert bool(values[SC43.name]) is False
    assert _past(names, "Msg", 10) == []


_BEFORE = ["fresh", "bank_read", "legacy_compiled"]


def _mapped_scalar_program(kind: str, before: str):
    """A scalar tag mapped by a TagMap onto a bank slot, then a store of several
    elements from it: a text into ``Name`` on TXT3, or digits into ``Count`` on
    DS4499.  *before* reads the bank's address, or compiles a legacy kernel (which
    reads every bank address), before the scan."""
    blocks = ClickBlocks()
    txt, ds = blocks.txt, blocks.ds
    if kind == "text":
        target, slot = Char("Name"), txt[3]
        after, store = [txt[4], txt[5]], lambda: copy("ABC", target)
        sentinel: Any = "#"
    else:
        target, slot = Int("Count"), ds[4499]
        after, store = [ds[4500]], lambda: copy("1234", target, convert=to_value)
        sentinel = 9
    TagMap({target: slot}, include_system=False)
    if before == "bank_read":
        bank = txt if kind == "text" else ds
        assert bank[slot._pyrung_block_addr] is target
    watch = [target, *after]
    with Program(strict=False) as prog:
        with Rung():
            store()
        with Rung(*(tag == sentinel for tag in watch)):
            out(Bool("Seen"))
    if before == "legacy_compiled":
        compile_kernel(prog, blockless=False)
    return prog, watch


@pytest.mark.parametrize("engine", _ENGINES)
@pytest.mark.parametrize("before", _BEFORE)
def test_a_text_into_a_tag_mapped_onto_the_bank_writes_the_next_slots(engine, before, monkeypatch):
    """``TagMap({Name: txt[3]})``: "ABC" into Name writes TXT3..TXT5, as a CLICK
    does, however the bank was read before."""
    prog, watch = _mapped_scalar_program("text", before)
    values, names = _scan(prog, engine, [*watch, SC43], monkeypatch)
    assert [values[tag.name] for tag in watch] == ["A", "B", "C"]
    assert bool(values[SC43.name]) is False
    assert "TXT3" not in names


@pytest.mark.parametrize("engine", _ENGINES)
@pytest.mark.parametrize("before", _BEFORE)
def test_digits_into_a_tag_mapped_onto_the_bank_write_what_fits(engine, before, monkeypatch):
    """``Count`` on DS4499: "1234" writes 1 there and 2 in DS4500, and drops the rest."""
    prog, watch = _mapped_scalar_program("digits", before)
    values, names = _scan(prog, engine, [*watch, SC43], monkeypatch)
    assert [values[tag.name] for tag in watch] == [1, 2]
    assert bool(values[SC43.name]) is False
    assert _past(names, "DS", 4500) == []
