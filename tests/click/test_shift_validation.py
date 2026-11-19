"""CLICK validation of shift(), which laddercodec's Shift checks.

These tests cover how a refusal prints: the call, the caret under the range, and the hint.
shift() takes its clock and reset as chained calls, so a refusal of the pin rows marks the
reset's condition.
"""

from __future__ import annotations

import dataclasses

import pytest
from laddercodec import Code, Shift

from pyrung.click import ClickBlocks, TagMap, pyrung_to_ladder
from pyrung.click.ladder import LadderExportError
from pyrung.click.ladder._exporter import _LadderExporter
from pyrung.click.validation import CLK_BANK_UNRESOLVED, families, validate_click_program
from pyrung.click.validation.fields import evaluate_family, refusals_of
from pyrung.click.validation.resolve import _ResolutionCache
from pyrung.core import Block, Bool, Or, TagType, shift
from pyrung.core.program import Program, rung
from pyrung.core.validation.walker import ProgramLocation

x, y, c, t, ct, sc, ds, dd, dh, df, xd, yd, xd0u, yd0u, td, ctd, sd, txt = ClickBlocks()


def _program(write) -> Program:
    prog = Program(strict=False)
    with prog:
        with rung(x[1]):
            write()
    return prog


def _errors(write, mappings=()):
    tag_map = TagMap(list(mappings), include_system=False)
    return validate_click_program(_program(write), tag_map, mode="strict").errors


def _marked(finding) -> str:
    (frame,) = finding.display.frames
    _line, start, width = frame.caret
    return frame.lines[0][start : start + width]


def test_a_refused_range_marks_the_range():
    first, last = _errors(lambda: shift(y.select(1, 8)).clock(x[2]).reset(x[3]))
    for finding in (first, last):
        assert finding.code == Code.BANK_NOT_ALLOWED
        (frame,) = finding.display.frames
        assert (frame.lines, frame.caret) == (("shift(Y1..Y8).clock(X002).reset(X003)",), (0, 6, 6))
        assert "OBJ-004" in finding.display.hint
        assert finding.message.isascii()
    # One finding for each end, and the label gives the refused address.
    assert [f.display.frames[0].caret_label for f in (first, last)] == [
        "Y1 is not allowed",
        "Y8 is not allowed",
    ]
    assert first.location == "main.rung[1].instruction[0](ShiftInstruction).instruction"


def test_a_range_of_one_address_marks_the_range():
    (finding,) = _errors(lambda: shift(c.select(1, 1)).clock(x[2]).reset(x[3]))
    assert finding.code == Code.DUPLICATE_ADDRESS
    assert _marked(finding) == "C1..C1"
    assert "OBJ-012" in finding.display.hint


_SCATTERED = Block("Scattered", TagType.BOOL, 1, 3)
_MIXED = Block("Mixed", TagType.BOOL, 1, 3)
_DESCENDING = Block("Descending", TagType.BOOL, 1, 3)
_MAPPED = {
    "descending": [
        _DESCENDING[1].map_to(c[43]),
        _DESCENDING[2].map_to(c[42]),
        _DESCENDING[3].map_to(c[41]),
    ],
    "scattered": [
        _SCATTERED[1].map_to(c[31]),
        _SCATTERED[2].map_to(c[35]),
        _SCATTERED[3].map_to(c[39]),
    ],
    "mixed": [_MIXED[1].map_to(c[21]), _MIXED[2].map_to(y[22]), _MIXED[3].map_to(c[23])],
}


@pytest.mark.parametrize(
    ("block", "reverse"),
    [(_SCATTERED, False), (_SCATTERED, True), (_MIXED, False)],
    ids=["scattered", "scattered-reversed", "mixed-banks"],
)
def test_a_shift_over_addresses_that_are_not_one_run_is_refused(block, reverse):
    mappings = _MAPPED["mixed" if block is _MIXED else "scattered"]
    bits = block.select(1, 3).reverse() if reverse else block.select(1, 3)
    write = lambda: shift(bits).clock(x[2]).reset(x[3])  # noqa: E731
    (finding,) = _errors(write, mappings)
    assert finding.code == "CLK_COPY_RANGE_NOT_CONSECUTIVE"
    assert finding.message.startswith("CLICK refuses shift(")
    with pytest.raises(LadderExportError, match="CLK_COPY_RANGE_NOT_CONSECUTIVE"):
        pyrung_to_ladder(_program(write), TagMap(mappings, include_system=False))


@pytest.mark.parametrize("reverse", [False, True], ids=["plain", "reversed"])
def test_a_shift_over_a_block_mapped_to_descending_addresses_passes(reverse):
    bits = _DESCENDING.select(1, 3).reverse() if reverse else _DESCENDING.select(1, 3)
    write = lambda: shift(bits).clock(x[2]).reset(x[3])  # noqa: E731
    mappings = _MAPPED["descending"]
    assert _errors(write, mappings) == ()
    bundle = pyrung_to_ladder(_program(write), TagMap(mappings, include_system=False))
    assert bundle is not None


def test_the_export_refuses_a_scattered_shift_even_under_allow():
    mappings = _MAPPED["scattered"]
    write = lambda: shift(_SCATTERED.select(1, 3)).clock(x[2]).reset(x[3])  # noqa: E731
    tag_map = TagMap(mappings, include_system=False)
    with pytest.raises(LadderExportError, match="CLK_COPY_RANGE_NOT_CONSECUTIVE"):
        pyrung_to_ladder(_program(write), tag_map, allow=("CLK_COPY_RANGE_NOT_CONSECUTIVE",))


@pytest.mark.parametrize(
    "write",
    [
        lambda: shift(c.select(1, 8)).clock(x[2]).reset(x[3]),
        lambda: shift(c.select(1011, 1018).reverse()).clock(c[2]).reset(c[3]),
        lambda: shift(c.select(1, 4).reverse()).clock(x[2]).reset(x[3]),
        lambda: shift(c.select(1, 2)).clock(~x[2]).reset(Or(x[3], x[4])),
    ],
    ids=["C", "reversed", "reversed-low", "conditions"],
)
def test_a_shift_that_click_takes_passes(write):
    assert _errors(write) == ()


def test_an_unmapped_range_names_the_range():
    bits = Block("Bits", TagType.BOOL, 1, 8)
    (finding,) = _errors(lambda: shift(bits.select(1, 8)).clock(x[2]).reset(x[3]))
    assert finding.code == CLK_BANK_UNRESOLVED
    assert finding.message.startswith("pyrung cannot find one CLICK address for the range")


@pytest.mark.parametrize("pin", ["clock", "reset"])
def test_an_unmapped_tag_in_a_chained_call_names_the_call(pin):
    pin_tag = Bool("Pin")
    other = x[3] if pin == "clock" else x[2]

    def write():
        call = shift(c.select(1, 8))
        if pin == "clock":
            call.clock(pin_tag).reset(other)
        else:
            call.clock(other).reset(pin_tag)

    (finding,) = _errors(write)
    assert finding.code == CLK_BANK_UNRESOLVED
    assert finding.message.startswith(f"pyrung cannot find one CLICK address for the {pin}")
    assert finding.location.endswith(f"instruction.{pin}_condition")


def test_the_export_refuses_a_shift_that_click_refuses():
    write = lambda: shift(y.select(1, 8)).clock(x[2]).reset(x[3])  # noqa: E731
    with pytest.raises(LadderExportError, match=Code.BANK_NOT_ALLOWED):
        pyrung_to_ladder(_program(write), TagMap())


def test_allow_with_the_new_code_lets_the_finding_through():
    write = lambda: shift(y.select(1, 8)).clock(x[2]).reset(x[3])  # noqa: E731
    pyrung_to_ladder(_program(write), TagMap(), allow=(Code.BANK_NOT_ALLOWED,))


def test_allow_with_the_old_code_no_longer_lets_the_finding_through():
    # CLK_BANK_NOT_WRITABLE is still a code of other checks, so allow= takes it. shift() no
    # longer reports it.
    write = lambda: shift(x.select(1, 8)).clock(x[2]).reset(x[3])  # noqa: E731
    with pytest.raises(LadderExportError, match=Code.BANK_NOT_ALLOWED):
        pyrung_to_ladder(_program(write), TagMap(), allow=("CLK_BANK_NOT_WRITABLE",))


# A refusal of the pin rows names both the clock and the reset. The family marks the reset.
# pyrung's export draws every rung within CLICK's height, so no shift() the export writes
# has pin rows that laddercodec refuses. This test hands the family's real build a Shift
# with rows that laddercodec refuses, and checks where the caret goes.


@pytest.fixture
def refused_pin_rows(monkeypatch):
    family = next(f for f in families.FAMILIES if f.name == "shift")
    refusals = refusals_of(lambda: Shift(start_bit="C1", end_bit="C8", pin_rows=(2, 1)))
    swapped = dataclasses.replace(family, build=lambda instruction, exporter: refusals)
    others = [f for f in families.FAMILIES if f is not family]
    monkeypatch.setattr(families, "FAMILIES", [*others, swapped])
    families._by_class.cache_clear()
    yield refusals
    families._by_class.cache_clear()


def test_a_refusal_of_the_pin_rows_marks_the_reset(refused_pin_rows):
    (refusal,) = refused_pin_rows
    assert refusal.code == Code.INVALID_INPUT_ROWS
    instruction = _program(lambda: shift(c.select(1, 8)).clock(x[2]).reset(x[3])).rungs[0]
    instruction = instruction._instructions[0]
    location = ProgramLocation("main", None, 0, (), 0, "ShiftInstruction", "instruction")
    tag_map = TagMap([], include_system=False)
    exporter = _LadderExporter(tag_map=tag_map, program=_program(lambda: None))
    resolver = _ResolutionCache(tag_map)
    (finding,) = evaluate_family(instruction, location, exporter, "strict", resolver, {}).findings()
    (frame,) = finding.display.frames
    assert frame.lines == ("shift(C1..C8).clock(X002).reset(X003)",)
    assert frame.lines[0][frame.caret[1] : frame.caret[1] + frame.caret[2]] == "X003"
