"""Tests for @udt and shared structured utilities."""

from __future__ import annotations

from typing import Any, cast

import pytest

from pyrung.core import Bool, Field, Int, TagType, auto, udt


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
        id: Int = auto(start=10, step=5)  # type: ignore[invalid-assignment]
        val: Int = 7  # type: ignore[invalid-assignment]
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
            flag: Bool = auto()  # type: ignore[invalid-assignment]


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
