"""Tests for ClickDataProvider soft PLC adapter."""

from __future__ import annotations

import pytest
from pyclickplc.server import MemoryDataProvider

from pyrung.click import ClickDataProvider, TagMap, c, ds, txt, x, y
from pyrung.core import Block, PLCRunner, Program, Rung, SystemState, Tag, TagType, out


def _write_slot_word(provider: ClickDataProvider, bank: str, start: int, word: int) -> None:
    for bit_index in range(16):
        provider.write(f"{bank}{start + bit_index:03d}", bool((word >> bit_index) & 0x1))


def _assert_slot_bits(
    provider: ClickDataProvider,
    bank: str,
    start: int,
    word: int,
) -> None:
    for bit_index in range(16):
        expected = bool((word >> bit_index) & 0x1)
        assert provider.read(f"{bank}{start + bit_index:03d}") is expected


def test_read_mapped_standalone_tag_returns_runner_state_value():
    valve = Tag("Valve", TagType.BOOL)
    mapping = TagMap({valve: c[1]})
    runner = PLCRunner(logic=[], initial_state=SystemState().with_tags({"Valve": True}))
    provider = ClickDataProvider(runner, mapping)

    assert provider.read("C1") is True


def test_read_mapped_block_slot_falls_back_to_logical_default_when_absent():
    alarms = Block("Alarm", TagType.BOOL, 1, 2)
    mapping = TagMap({alarms: c.select(101, 102)})
    runner = PLCRunner(logic=[])
    provider = ClickDataProvider(runner, mapping)

    assert provider.read("C101") is False


def test_read_mapped_block_slot_uses_first_class_slot_default_when_absent():
    alarms = Block("AlarmCfg", TagType.BOOL, 1, 2)
    alarms.configure_slot(1, default=True)
    mapping = TagMap({alarms: c.select(111, 112)})
    runner = PLCRunner(logic=[])
    provider = ClickDataProvider(runner, mapping)

    assert provider.read("C111") is True


def test_write_mapped_tag_is_deferred_until_next_scan():
    valve = Tag("Valve", TagType.BOOL)
    mapping = TagMap({valve: c[1]})
    runner = PLCRunner(logic=[], initial_state=SystemState().with_tags({"Valve": False}))
    provider = ClickDataProvider(runner, mapping)

    provider.write("C1", True)
    assert runner.current_state.tags["Valve"] is False

    runner.step()
    assert runner.current_state.tags["Valve"] is True


def test_pending_mapped_writes_last_value_wins_on_commit():
    valve = Tag("Valve", TagType.BOOL)
    mapping = TagMap({valve: c[1]})
    runner = PLCRunner(logic=[])
    provider = ClickDataProvider(runner, mapping)

    provider.write("C1", False)
    provider.write("c1", True)
    runner.step()

    assert runner.current_state.tags["Valve"] is True


def test_block_slot_mapping_reads_and_writes_use_correct_logical_slot_name():
    alarms = Block("Alarm", TagType.BOOL, 1, 2)
    mapping = TagMap({alarms: c.select(101, 102)})
    runner = PLCRunner(logic=[])
    provider = ClickDataProvider(runner, mapping)

    provider.write("C102", True)
    runner.step()

    assert runner.current_state.tags["Alarm2"] is True
    assert provider.read("C102") is True


def test_unmapped_addresses_delegate_to_fallback_provider():
    valve = Tag("Valve", TagType.BOOL)
    fallback = MemoryDataProvider()
    provider = ClickDataProvider(PLCRunner(logic=[]), TagMap({valve: c[1]}), fallback=fallback)

    provider.write("DS1", 42)

    assert provider.read("DS1") == 42
    assert fallback.read("DS1") == 42


def test_xd0_reflects_x001_to_x016_bit_image():
    provider = ClickDataProvider(PLCRunner(logic=[]), TagMap())
    _write_slot_word(provider, "X", 1, 0xA55A)
    assert provider.read("XD0") == 0xA55A


def test_xd0u_reflects_x021_to_x036_bit_image():
    provider = ClickDataProvider(PLCRunner(logic=[]), TagMap())
    _write_slot_word(provider, "X", 21, 0x0F0F)
    assert provider.read("XD0u") == 0x0F0F


def test_xd1_reflects_x101_to_x116_bit_image():
    provider = ClickDataProvider(PLCRunner(logic=[]), TagMap())
    _write_slot_word(provider, "X", 101, 0xC33C)
    assert provider.read("XD1") == 0xC33C


def test_yd0_reflects_y001_to_y016_bit_image():
    provider = ClickDataProvider(PLCRunner(logic=[]), TagMap())
    _write_slot_word(provider, "Y", 1, 0x55AA)
    assert provider.read("YD0") == 0x55AA


def test_write_yd0_updates_y001_to_y016_on_next_scan():
    outputs = Block("Out", TagType.BOOL, 1, 16)
    mapping = TagMap({outputs: y.select(1, 16)})
    runner = PLCRunner(logic=[])
    provider = ClickDataProvider(runner, mapping)

    provider.write("YD0", 0xA55A)
    assert provider.read("YD0") == 0

    runner.step()
    _assert_slot_bits(provider, "Y", 1, 0xA55A)
    assert provider.read("YD0") == 0xA55A


def test_write_yd0u_updates_y021_to_y036_on_next_scan():
    outputs = Block("UpperOut", TagType.BOOL, 1, 16)
    mapping = TagMap({outputs: y.select(21, 36)})
    runner = PLCRunner(logic=[])
    provider = ClickDataProvider(runner, mapping)

    provider.write("YD0u", 0x5AA5)
    assert provider.read("YD0u") == 0

    runner.step()
    _assert_slot_bits(provider, "Y", 21, 0x5AA5)
    assert provider.read("YD0u") == 0x5AA5


def test_write_xd0_raises_value_error():
    provider = ClickDataProvider(PLCRunner(logic=[]), TagMap())
    with pytest.raises(ValueError):
        provider.write("XD0", 0x1234)


def test_yd_write_can_be_overwritten_by_rung_outputs_in_same_scan():
    drive = Tag("Drive", TagType.BOOL)
    mapping = TagMap({drive: y[1]})

    with Program() as logic:
        with Rung():
            out(drive)

    runner = PLCRunner(logic=logic, initial_state=SystemState().with_tags({"Drive": False}))
    provider = ClickDataProvider(runner, mapping)
    provider.write("YD0", 0)

    runner.step()
    assert provider.read("Y001") is True
    assert provider.read("YD0") == 0x0001


def test_yd_read_normalizes_case_and_format():
    provider = ClickDataProvider(PLCRunner(logic=[]), TagMap())
    provider.write("y1", True)
    provider.write("Y021", True)
    provider.write("Y036", True)

    assert provider.read("yd0") == 0x0001
    assert provider.read("YD0") == 0x0001
    assert provider.read("yd0u") == 0x8001


def test_non_xy_banks_preserve_existing_runtime_behavior():
    valve = Tag("Valve", TagType.BOOL)
    letter = Tag("Letter", TagType.CHAR)
    mapping = TagMap({valve: c[1], letter: txt[1]})
    fallback = MemoryDataProvider()
    runner = PLCRunner(logic=[])
    provider = ClickDataProvider(runner, mapping, fallback=fallback)

    provider.write("C1", True)
    provider.write("DS1", 42)
    provider.write("TXT1", "A")

    assert provider.read("C1") is False
    assert provider.read("DS1") == 42
    assert provider.read("TXT1") == ""

    runner.step()
    assert provider.read("C1") is True
    assert provider.read("DS1") == 42
    assert provider.read("TXT1") == "A"


def test_mapped_txt_write_is_visible_after_next_scan():
    letter = Tag("Letter", TagType.CHAR)
    mapping = TagMap({letter: txt[1]})
    runner = PLCRunner(logic=[])
    provider = ClickDataProvider(runner, mapping)

    provider.write("TXT1", "A")
    assert provider.read("TXT1") == ""

    runner.step()
    assert provider.read("TXT1") == "A"
    assert runner.current_state.tags["Letter"] == "A"


def test_address_normalization_supports_case_and_zero_padding():
    valve = Tag("Valve", TagType.BOOL)
    input_1 = Tag("Input1", TagType.BOOL)
    mapping = TagMap({valve: c[1], input_1: x[1]})
    runner = PLCRunner(logic=[])
    provider = ClickDataProvider(runner, mapping)

    provider.write("c1", True)
    provider.write("x1", True)
    runner.step()

    assert provider.read("C1") is True
    assert provider.read("X001") is True


def test_mapped_read_uses_override_default_then_tag_default_precedence():
    count = Tag("Count", TagType.INT, default=5)
    total = Tag("Total", TagType.INT, default=4)
    mapping = TagMap({count: ds[1], total: ds[2]})
    mapping.override(count, default=9)
    provider = ClickDataProvider(PLCRunner(logic=[]), mapping)

    assert provider.read("DS1") == 9
    assert provider.read("DS2") == 4


def test_mapped_runtime_validation_matches_fallback_behavior():
    count = Tag("Count", TagType.INT)
    mapping = TagMap({count: ds[1]})
    provider = ClickDataProvider(PLCRunner(logic=[]), mapping)

    baseline = MemoryDataProvider()
    with pytest.raises(ValueError) as expected_error:
        baseline.write("DS1", True)

    with pytest.raises(ValueError) as mapped_error:
        provider.write("DS1", True)

    assert str(mapped_error.value) == str(expected_error.value)
