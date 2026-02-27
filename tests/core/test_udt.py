"""Tests for @udt and shared structured utilities.

Note: ``cast(Any, ...)`` is used throughout because ty does not yet infer
the return type of class decorators.  @udt replaces the class with a
``_StructRuntime`` instance, but ty still sees the original class.
Tracking issue: https://github.com/astral-sh/ty/issues/143
"""

from __future__ import annotations

from typing import Any, ClassVar, cast

import pytest

from pyrung.core import Bool, Field, Int, TagType, auto, udt
from pyrung.core.tag import LiveTag


def test_udt_builds_blocks_with_correct_types():
    @udt(count=3)
    class Alarm:
        id: Int
        on: Bool

    alarms = cast(Any, Alarm)
    assert alarms.id.type == TagType.INT
    assert alarms.on.type == TagType.BOOL


def test_udt_uses_expected_tag_name_pattern():
    @udt(count=3)
    class Alarm:
        id: Int
        On: Bool

    alarms = cast(Any, Alarm)
    assert alarms[1].id.name == "Alarm1_id"
    assert alarms[3].On.name == "Alarm3_On"


def test_udt_literal_and_auto_defaults_resolve_per_instance():
    @udt(count=3)
    class Alarm:
        id: Int = auto(start=10, step=5)
        val: int = 7
        Time: Int

    alarms = cast(Any, Alarm)
    assert alarms[1].id.default == 10
    assert alarms[2].id.default == 15
    assert alarms[3].id.default == 20
    assert alarms[2].val.default == 7
    assert alarms[2].Time.default == 0


def test_udt_retentive_policy_inherited_by_generated_tags():
    @udt(count=2)
    class Alarm:
        id: Int = Field(retentive=True)  # type: ignore[invalid-assignment]
        On: Bool = Field(retentive=False)  # type: ignore[invalid-assignment]

    alarms = cast(Any, Alarm)
    assert alarms[1].id.retentive is True
    assert alarms[1].On.retentive is False


def test_udt_rejects_invalid_declarations():
    with pytest.raises(ValueError, match="count"):

        @udt(count=0)
        class _BadCount:
            id: Int

    with pytest.raises(ValueError, match="At least one field"):

        @udt(count=1)
        class _NoFields:
            pass

    with pytest.raises(TypeError, match="not supported"):

        @udt(count=1)
        class _BadAnnotation:
            id: list[int]


def test_udt_rejects_auto_default_for_non_numeric_type():
    with pytest.raises(ValueError, match="not numeric"):

        @udt(count=1)
        class _BadAuto:
            flag: Bool = auto()


def test_instance_view_validates_index_and_missing_attributes():
    @udt(count=2)
    class Alarm:
        id: Int

    alarms = cast(Any, Alarm)
    with pytest.raises(IndexError, match="out of range"):
        _ = alarms[0]

    with pytest.raises(IndexError, match="out of range"):
        _ = alarms[3]

    with pytest.raises(AttributeError, match="no field"):
        _ = alarms[1].missing


def test_udt_allows_underscored_fields():
    @udt(count=1)
    class Alarm:
        _x: Int = 0  # type: ignore[invalid-assignment]
        val: Int = 1  # type: ignore[invalid-assignment]

    alarms = cast(Any, Alarm)
    assert alarms.field_names == ("_x", "val")
    assert alarms[1]._x.name == "Alarm__x"
    assert alarms[1].val.name == "Alarm_val"


def test_udt_clone_produces_independent_copy_with_new_name():
    @udt(count=3)
    class Task:
        call: Bool
        init: Bool
        reset: Int = auto()

    Pump = cast(Any, Task).clone("Pump")
    Valve = cast(Any, Task).clone("Valve")

    assert Pump.name == "Pump"
    assert Valve.name == "Valve"
    assert Pump.count == 3
    assert Pump.field_names == ("call", "init", "reset")

    assert Pump[1].call.name == "Pump1_call"
    assert Valve[2].init.name == "Valve2_init"
    assert Pump[1].reset.default == 1
    assert Pump[2].reset.default == 2

    # Original is unaffected
    task = cast(Any, Task)
    assert task[1].call.name == "Task1_call"


def test_udt_skips_classvar_fields():
    @udt(count=1)
    class Alarm:
        _meta: ClassVar[int] = 123
        val: Int = 1  # type: ignore[invalid-assignment]

    alarms = cast(Any, Alarm)
    assert alarms.field_names == ("val",)
    with pytest.raises(AttributeError):
        _ = alarms._meta


def test_udt_count_one_returns_livetag_from_getattr():
    @udt()
    class Alarm:
        id: Int

    alarm = cast(Any, Alarm)
    assert isinstance(alarm.id, LiveTag)
    assert alarm.id.name == "Alarm_id"
    assert alarm.id.type == TagType.INT


def test_udt_count_one_naming_has_no_number():
    @udt()
    class Alarm:
        id: Int
        On: Bool

    alarm = cast(Any, Alarm)
    assert alarm.id.name == "Alarm_id"
    assert alarm.On.name == "Alarm_On"


def test_udt_count_one_getitem_supports_only_index_one():
    @udt()
    class Alarm:
        id: Int

    alarm = cast(Any, Alarm)
    assert alarm[1].id is alarm.id
    with pytest.raises(IndexError, match="out of range"):
        _ = alarm[0]
    with pytest.raises(IndexError, match="out of range"):
        _ = alarm[2]


def test_udt_count_one_clone_preserves_count():
    @udt()
    class Device:
        total: Int

    Pump = cast(Any, Device).clone("Pump")
    assert Pump.total.name == "Pump_total"
    assert Pump[1].total is Pump.total


def test_udt_clone_allows_count_override():
    @udt()
    class Device:
        total: Int

    Pump = cast(Any, Device).clone("Pump", count=3)
    assert Pump.count == 3
    assert Pump[1].total.name == "Pump1_total"
    assert Pump[3].total.name == "Pump3_total"

    original = cast(Any, Device)
    assert original.count == 1
    assert original.total.name == "Device_total"


def test_udt_count_one_fields_and_field_names():
    @udt()
    class Alarm:
        id: Int = 1  # type: ignore[invalid-assignment]
        On: Bool = Field(retentive=True)  # type: ignore[invalid-assignment]

    alarm = cast(Any, Alarm)
    assert alarm.field_names == ("id", "On")
    assert set(alarm.fields.keys()) == {"id", "On"}
    assert alarm.fields["id"].type == TagType.INT
    assert alarm.fields["id"].default == 1
    assert alarm.fields["On"].retentive is True


def test_udt_numbered_forces_numbered_names_for_count_one():
    @udt(numbered=True)
    class Alarm:
        id: Int
        On: Bool

    alarm = cast(Any, Alarm)
    assert alarm.id.name == "Alarm1_id"
    assert alarm.On.name == "Alarm1_On"
    assert alarm[1].id.name == "Alarm1_id"


def test_udt_numbered_has_no_effect_when_count_greater_than_one():
    @udt(count=3, numbered=True)
    class Alarm:
        id: Int

    alarms = cast(Any, Alarm)
    assert alarms[1].id.name == "Alarm1_id"
    assert alarms[3].id.name == "Alarm3_id"


def test_udt_numbered_clone_preserves_flag():
    @udt(numbered=True)
    class Device:
        total: Int

    Pump = cast(Any, Device).clone("Pump")
    assert Pump.total.name == "Pump1_total"
    assert Pump.numbered is True


def test_udt_numbered_default_is_false():
    @udt()
    class Alarm:
        id: Int

    alarm = cast(Any, Alarm)
    assert alarm.numbered is False
    assert alarm.id.name == "Alarm_id"
