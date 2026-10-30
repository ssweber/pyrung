"""CLICK text, pack_text, search-cursor and integer MDN semantics, one scan per scenario."""

from __future__ import annotations

import pytest

from pyrung.core import (
    Block,
    Bool,
    Int,
    Program,
    Rung,
    TagType,
    calc,
    copy,
    pack_text,
    search,
)
from pyrung.core.copy_converters import to_ascii, to_binary, to_value

FAULT = "fault.out_of_range"


def _pack(runner_factory, dest_type, text):
    Enable = Bool("Enable")
    CH = Block("CH", TagType.CHAR, 1, 20)
    dest_block = Block("D", dest_type, 1, 2)
    with Program() as logic:
        with Rung(Enable):
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


def test_to_value_of_nul_writes_zero(runner_factory):
    CH = Block("CH", TagType.CHAR, 1, 2)
    Out = Int("Out")
    Enable = Bool("Enable")
    with Program() as logic:
        with Rung(Enable):
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
        with Rung(Enable):
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
        with Rung(Enable):
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
        with Rung(Enable):
            calc(DS.select(1, len(values)).median(), Out)
    runner = runner_factory(logic)
    runner.patch({"Enable": True, **{f"DS{i}": v for i, v in enumerate(values, start=1)}})
    runner.step()
    assert runner.current_state.tags["Out"] == expected
