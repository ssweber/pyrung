"""Tests for CLICK text, pack_text, search-cursor and integer MDN semantics, one scan each."""

from __future__ import annotations

import math as math_

import pytest

from pyrung.core import (
    Block,
    Bool,
    Int,
    Program,
    TagType,
    copy,
    math,
    out,
    pack_text,
    rung,
    search,
    system,
)
from pyrung.core.copy_converters import to_ascii, to_binary, to_value

FAULT = "fault.out_of_range"


def _pack(runner_factory, dest_type, text):
    Enable = Bool("Enable")
    CH = Block("CH", TagType.CHAR, 1, 20)
    dest_block = Block("D", dest_type, 1, 2)
    with Program() as logic:
        with rung(Enable):
            pack_text(CH.select(1, len(text)), dest_block[1])
    runner = runner_factory(logic)
    patch = {"Enable": True, "D1": 99}
    patch.update({f"CH{i}": ch for i, ch in enumerate(text, start=1)})
    runner.patch(patch)
    runner.step()
    tags = runner.current_state.tags
    return tags["D1"], bool(tags.get(FAULT, False))


@pytest.mark.parametrize(
    ("dest_type", "text", "value", "fault"),
    [
        (TagType.INT, "123456", 32767, True),
        (TagType.INT, "-123456", -32768, True),
        (TagType.DINT, "2147483648", 2147483647, True),
        (TagType.DINT, "-2147483649", -2147483648, True),
        (TagType.INT, "12.9", 12, False),
        (TagType.INT, "-42", -42, False),
        (TagType.DINT, "70000", 70000, False),
        (TagType.INT, "12x", 99, True),
        (TagType.DINT, "12x", 99, True),
        (TagType.INT, "x12", 99, True),
    ],
)
def test_pack_text_int_dint(runner_factory, dest_type, text, value, fault):
    assert _pack(runner_factory, dest_type, text) == (value, fault)


@pytest.mark.parametrize(
    ("text", "value", "fault"),
    [
        ("3.40282352E38", math_.inf, False),
        ("3.5E38", math_.inf, False),
        ("-3.5E38", -math_.inf, False),
        ("1E400", math_.inf, False),
        ("1.5", 1.5, False),
        ("inf", 99.0, True),
        ("\x00" * 13, 99.0, True),
    ],
    ids=["just-past", "past", "minus-past", "past-a-double", "in-range", "inf-word", "blank"],
)
def test_pack_text_real(runner_factory, text, value, fault):
    """Read on a CLICK: "3.40282352E38" stores +Inf with no flag, and a blank text raises SC43
    and writes nothing.  The other texts past the range follow the same rule."""
    assert _pack(runner_factory, TagType.REAL, text) == (value, fault)


@pytest.mark.parametrize(
    ("dest_type", "text"),
    [
        (TagType.INT, "١"),
        (TagType.DINT, "1٢"),
        (TagType.WORD, "١"),
        (TagType.REAL, "١.5"),
    ],
    ids=["int", "dint", "word", "real"],
)
def test_pack_text_refuses_a_character_above_ffh(runner_factory, dest_type, text):
    """A TXT holds one byte.  Python's int() and float() read U+0661 as the digit 1."""
    assert _pack(runner_factory, dest_type, text) == (99, True)


def test_pack_text_refuses_a_character_above_ffh_that_strip_removes(runner_factory):
    """Python's strip() removes U+3000, so the check comes before the strip."""
    CH = Block("CH", TagType.CHAR, 1, 2)
    dest = Int("Dest")
    with Program() as logic:
        with rung():
            pack_text(CH.select(1, 2), dest, allow_whitespace=True)
    runner = runner_factory(logic)
    runner.patch({"CH1": "　", "CH2": "7", "Dest": 99})
    runner.step()
    tags = runner.current_state.tags
    assert (tags["Dest"], tags[FAULT]) == (99, True)


def test_a_blank_pack_text_flag_lasts_until_a_good_pack_text(runner_factory):
    """Read on a CLICK: SC43 from a blank text is still on as the next scan starts, and a
    pack_text that parses its text turns it off."""
    CH = Block("CH", TagType.CHAR, 1, 3)
    Good = Block("G", TagType.CHAR, 1, 3)
    Blank, Parse = Bool("Blank"), Bool("Parse")
    AtStart, AfterGood = Bool("AtStart"), Bool("AfterGood")
    dest = Block("D", TagType.REAL, 1, 2)
    with Program() as logic:
        with rung(system.fault.out_of_range):
            out(AtStart)
        with rung(Blank):
            pack_text(CH.select(1, 3), dest[1])
        with rung(Parse):
            pack_text(Good.select(1, 3), dest[2])
        with rung(system.fault.out_of_range):
            out(AfterGood)
    runner = runner_factory(logic)
    runner.patch({"Blank": True, "G1": "1", "G2": ".", "G3": "5"})
    runner.step()
    runner.patch({"Blank": False})
    runner.step()
    tags = runner.current_state.tags
    assert (tags["AtStart"], tags["AfterGood"]) == (True, True)
    runner.patch({"Parse": True})
    runner.step()
    tags = runner.current_state.tags
    assert (tags["AtStart"], tags["AfterGood"], tags["D2"]) == (True, False, 1.5)
    runner.step()
    assert runner.current_state.tags["AtStart"] is False


def test_to_value_of_nul_writes_zero(runner_factory):
    CH = Block("CH", TagType.CHAR, 1, 2)
    Out = Int("Out")
    Enable = Bool("Enable")
    with Program() as logic:
        with rung(Enable):
            copy(CH[1], Out, convert=to_value)
    runner = runner_factory(logic)
    runner.patch({"Enable": True, "CH1": "\x00", "Out": 7})
    runner.step()
    assert runner.current_state.tags["Out"] == 0
    assert not runner.current_state.tags.get(FAULT, False)


@pytest.mark.parametrize("code", [0, 127, 128, 200, 255])
def test_txt_is_a_whole_byte(runner_factory, code):
    CH = Block("CH", TagType.CHAR, 1, 2)
    Out, Back = Int("Out"), Int("Back")
    Enable = Bool("Enable")
    with Program() as logic:
        with rung(Enable):
            copy(CH[1], Out, convert=to_ascii)
            copy(Back, CH[2], convert=to_binary)
    runner = runner_factory(logic)
    runner.patch({"Enable": True, "CH1": chr(code), "Back": code})
    runner.step()
    assert runner.current_state.tags["Out"] == code
    assert runner.current_state.tags["CH2"] == chr(code)


def _search_from(runner_factory, start):
    Enable = Bool("Enable")
    DS = Block("DS", TagType.INT, 1, 10)
    Result, Found = Int("Result"), Bool("Found")
    with Program() as logic:
        with rung(Enable):
            search(DS.select(3, 7) == 5, result=Result, found=Found, continuous=True)
    runner = runner_factory(logic)
    runner.patch(
        {"Enable": True, "Result": start, "Found": False, **{f"DS{i}": 5 for i in range(1, 11)}}
    )
    runner.step()
    return runner.current_state.tags["Result"]


@pytest.mark.parametrize(
    ("start", "expected"),
    [
        (0, 3),  # restart
        (4, 5),  # inside the range: resume after it
        (6, 7),
        (7, -1),  # on the last address: nothing after it
        (2, -1),  # below the range: a miss, not the first match
        (9, -1),  # above the range
    ],
)
def test_continuous_search_cursor(runner_factory, start, expected):
    assert _search_from(runner_factory, start) == expected


@pytest.mark.parametrize(
    ("values", "expected"),
    [([-9, -2], -5), ([-2, -3], -2), ([9, 2, 10, 1], 5), ([-9, -2, -10, -1], -5)],
)
def test_integer_mdn_truncates_toward_zero(runner_factory, values, expected):
    Enable = Bool("Enable")
    DS = Block("DS", TagType.INT, 1, 6)
    Out = Int("Out")
    with Program() as logic:
        with rung(Enable):
            math(DS.select(1, len(values)).median(), Out)
    runner = runner_factory(logic)
    runner.patch({"Enable": True, **{f"DS{i}": v for i, v in enumerate(values, start=1)}})
    runner.step()
    assert runner.current_state.tags["Out"] == expected
