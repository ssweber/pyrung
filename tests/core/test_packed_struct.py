"""Tests for PackedStruct."""

from __future__ import annotations

from typing import cast

import pytest

from pyrung.core import Block, BlockRange, Field, PackedStruct, Tag, TagType, auto
from pyrung.core.tag import MappingEntry


def test_packed_struct_pad_generates_empty_fields():
    alarms = PackedStruct(
        "Alarm",
        TagType.INT,
        count=2,
        pad=2,
        id=Field(default=auto()),
        val=Field(default=0),
    )

    assert alarms.width == 4
    assert alarms.field_names == ("id", "val", "empty1", "empty2")
    assert alarms[1].empty1.name == "Alarm1_empty1"
    assert alarms[2].empty2.name == "Alarm2_empty2"


def test_packed_struct_range_length_validation():
    alarms = PackedStruct(
        "Alarm",
        TagType.INT,
        count=2,
        pad=1,
        id=Field(),
        val=Field(),
    )
    hardware = Block("HW", TagType.INT, 1, 20)

    with pytest.raises(ValueError, match="expects"):
        alarms.map_to(hardware.select(1, 5))


def test_packed_struct_interleaved_addresses_are_correct():
    alarms = PackedStruct(
        "Alarm",
        TagType.INT,
        count=2,
        pad=1,
        id=Field(default=auto()),
        val=Field(default=0),
    )
    hardware = Block("HW", TagType.INT, 1, 20)

    entries = alarms.map_to(hardware.select(1, 6))

    assert len(entries) == 6
    assert [entry.source.name for entry in entries] == [
        "Alarm1_id",
        "Alarm1_val",
        "Alarm1_empty1",
        "Alarm2_id",
        "Alarm2_val",
        "Alarm2_empty1",
    ]
    assert all(isinstance(entry.source, Tag) for entry in entries)
    assert all(isinstance(entry.target, Tag) for entry in entries)
    targets = [cast(Tag, entry.target) for entry in entries]
    assert [target.name for target in targets] == ["HW1", "HW2", "HW3", "HW4", "HW5", "HW6"]


def test_packed_struct_width_one_emits_block_mapping():
    singles = PackedStruct("Single", TagType.INT, count=3, value=Field(default=auto()))
    hardware = Block("HW", TagType.INT, 1, 20)

    entries = singles.map_to(hardware.select(10, 12))

    assert len(entries) == 1
    mapping = entries[0]
    assert isinstance(mapping, MappingEntry)
    assert isinstance(mapping.source, Block)
    assert isinstance(mapping.target, BlockRange)
    assert mapping.source is singles.value
    assert mapping.target.start == 10
    assert mapping.target.end == 12


def test_packed_struct_width_greater_than_one_emits_tag_mappings():
    alarms = PackedStruct("Alarm", TagType.INT, count=2, id=Field(), val=Field())
    hardware = Block("HW", TagType.INT, 1, 20)

    entries = alarms.map_to(hardware.select(1, 4))

    assert len(entries) == 4
    assert all(isinstance(entry.source, Tag) for entry in entries)
    assert all(isinstance(entry.target, Tag) for entry in entries)


def test_packed_struct_auto_default_restricted_by_base_type():
    with pytest.raises(ValueError, match="not numeric"):
        PackedStruct("Alarm", TagType.BOOL, count=2, id=Field(default=auto()))
