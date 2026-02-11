"""Tests for ClickDataProvider soft PLC adapter."""

from __future__ import annotations

import pytest
from pyclickplc.server import MemoryDataProvider

from pyrung.click import ClickDataProvider, TagMap, c, ds, txt, x
from pyrung.core import Block, PLCRunner, SystemState, Tag, TagType


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


def test_write_mapped_tag_is_deferred_until_next_scan():
    valve = Tag("Valve", TagType.BOOL)
    mapping = TagMap({valve: c[1]})
    runner = PLCRunner(logic=[], initial_state=SystemState().with_tags({"Valve": False}))
    provider = ClickDataProvider(runner, mapping)

    provider.write("C1", True)
    assert runner.current_state.tags["Valve"] is False

    runner.step()
    assert runner.current_state.tags["Valve"] is True


def test_multiple_mapped_writes_before_scan_last_write_wins():
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


def test_xd_and_yd_addresses_are_fallback_only_even_if_mapped():
    raw_input = Tag("RawInputWord", TagType.WORD, default=7)
    mapping = TagMap({raw_input: Tag("XD0", TagType.WORD)})
    runner = PLCRunner(logic=[], initial_state=SystemState().with_tags({"RawInputWord": 1234}))
    fallback = MemoryDataProvider()
    provider = ClickDataProvider(runner, mapping, fallback=fallback)

    assert provider.read("XD0") == 0

    provider.write("XD0", 0x1234)
    provider.write("YD0", 0x2345)

    assert provider.read("XD0") == 0x1234
    assert provider.read("YD0") == 0x2345
    runner.step()
    assert runner.current_state.tags["RawInputWord"] == 1234


def test_mapped_txt_write_accepts_string_and_becomes_visible_after_next_scan():
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


def test_mapped_read_default_precedence_uses_override_then_slot_default():
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
