"""Tests for @named_array.

Note: ``cast(Any, ...)`` is used throughout because ty does not yet infer
the return type of class decorators.  @named_array replaces the class with a
``_NamedArrayRuntime`` instance, but ty still sees the original class.
Tracking issue: https://github.com/astral-sh/ty/issues/143
"""

from __future__ import annotations

from typing import Any, ClassVar, cast

import pytest

from pyrung.core import Block, BlockRange, Field, Int, Tag, TagType, auto, named_array
from pyrung.core.tag import LiveTag, MappingEntry


def test_named_array_stride_creates_unmapped_gaps():
    @named_array(Int, count=2, stride=4)
    class Alarm:
        id = auto()
        val = 0

    alarms = cast(Any, Alarm)
    assert alarms.stride == 4
    assert alarms.field_names == ("id", "val")
    assert alarms[1].id.name == "Alarm1_id"
    assert alarms[2].val.name == "Alarm2_val"


def test_named_array_range_length_validation():
    @named_array(Int, count=2, stride=3)
    class Alarm:
        id = Field()
        val = Field()

    alarms = cast(Any, Alarm)
    hardware = Block("HW", TagType.INT, 1, 20)

    with pytest.raises(ValueError, match="expects"):
        alarms.map_to(hardware.select(1, 5))


def test_named_array_interleaved_addresses_are_correct():
    @named_array(Int, count=2, stride=3)
    class Alarm:
        id = auto()
        val = 0

    alarms = cast(Any, Alarm)
    hardware = Block("HW", TagType.INT, 1, 20)
    entries = alarms.map_to(hardware.select(1, 6))

    assert len(entries) == 4
    assert [entry.source.name for entry in entries] == [
        "Alarm1_id",
        "Alarm1_val",
        "Alarm2_id",
        "Alarm2_val",
    ]
    assert all(isinstance(entry.source, Tag) for entry in entries)
    assert all(isinstance(entry.target, Tag) for entry in entries)
    targets = [cast(Tag, entry.target) for entry in entries]
    assert [target.name for target in targets] == ["HW1", "HW2", "HW4", "HW5"]


def test_named_array_width_one_emits_block_mapping():
    @named_array(Int, count=3, stride=1)
    class Single:
        value = auto()

    singles = cast(Any, Single)
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


def test_named_array_width_greater_than_one_emits_tag_mappings():
    @named_array(Int, count=2, stride=2)
    class Alarm:
        id = Field()
        val = Field()

    alarms = cast(Any, Alarm)
    hardware = Block("HW", TagType.INT, 1, 20)
    entries = alarms.map_to(hardware.select(1, 4))

    assert len(entries) == 4
    assert all(isinstance(entry.source, Tag) for entry in entries)
    assert all(isinstance(entry.target, Tag) for entry in entries)


def test_named_array_auto_default_restricted_by_base_type():
    with pytest.raises(ValueError, match="not numeric"):

        @named_array("BOOL", count=2)
        class _Alarm:
            id = auto()


def test_named_array_allows_underscored_field_names():
    @named_array(Int, count=1, stride=2)
    class Alarm:
        _x = 0
        val = 0

    alarms = cast(Any, Alarm)
    assert alarms.field_names == ("_x", "val")
    assert alarms[1]._x.name == "Alarm__x"
    assert alarms[1].val.name == "Alarm_val"


def test_named_array_clone_produces_independent_copy_with_new_name():
    @named_array(Int, count=2, stride=3)
    class Task:
        call = auto()
        init = 0
        reset = 0

    Pump = cast(Any, Task).clone("Pump")
    Valve = cast(Any, Task).clone("Valve")

    assert Pump.name == "Pump"
    assert Valve.name == "Valve"
    assert Pump.count == 2
    assert Pump.stride == 3
    assert Pump.type == TagType.INT
    assert Pump.field_names == ("call", "init", "reset")

    assert Pump[1].call.name == "Pump1_call"
    assert Valve[2].init.name == "Valve2_init"
    assert Pump[1].call.default == 1
    assert Pump[2].call.default == 2

    # Original is unaffected
    task = cast(Any, Task)
    assert task[1].call.name == "Task1_call"


def test_named_array_skips_classvar_fields():
    @named_array(Int, count=1, stride=1)
    class Alarm:
        _meta: ClassVar[int] = 123
        val = 0

    alarms = cast(Any, Alarm)
    assert alarms.field_names == ("val",)
    with pytest.raises(AttributeError):
        _ = alarms._meta


def test_named_array_count_one_returns_livetag_from_getattr():
    @named_array(Int)
    class Alarm:
        id = auto()

    alarm = cast(Any, Alarm)
    assert isinstance(alarm.id, LiveTag)
    assert alarm.id.name == "Alarm_id"
    assert alarm.id.default == 1


def test_named_array_count_one_map_to_succeeds():
    @named_array(Int, stride=2)
    class Alarm:
        id = 0
        val = 0

    alarm = cast(Any, Alarm)
    hardware = Block("HW", TagType.INT, 1, 20)
    entries = alarm.map_to(hardware.select(1, 2))

    assert len(entries) == 2
    assert [entry.source.name for entry in entries] == ["Alarm_id", "Alarm_val"]
    assert [cast(Tag, entry.target).name for entry in entries] == ["HW1", "HW2"]
