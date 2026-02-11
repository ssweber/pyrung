"""Tests for Struct and shared struct utilities."""

from __future__ import annotations

from typing import Any, cast

import pytest

from pyrung.core import Field, Struct, TagType, auto


def test_struct_builds_blocks_with_correct_types():
    alarms = Struct(
        "Alarm",
        count=3,
        id=Field(TagType.INT),
        on=Field(TagType.BOOL),
    )

    assert alarms.id.type == TagType.INT
    assert alarms.on.type == TagType.BOOL


def test_struct_uses_expected_tag_name_pattern():
    alarms = Struct(
        "Alarm",
        count=3,
        id=Field(TagType.INT),
        On=Field(TagType.BOOL),
    )

    assert alarms[1].id.name == "Alarm1_id"
    assert alarms[3].On.name == "Alarm3_On"


def test_struct_literal_and_auto_defaults_resolve_per_instance():
    alarms = Struct(
        "Alarm",
        count=3,
        id=Field(TagType.INT, default=auto(start=10, step=5)),
        val=Field(TagType.INT, default=7),
        Time=Field(TagType.INT),
    )

    assert alarms[1].id.default == 10
    assert alarms[2].id.default == 15
    assert alarms[3].id.default == 20
    assert alarms[2].val.default == 7
    assert alarms[2].Time.default == 0


def test_struct_retentive_policy_inherited_by_generated_tags():
    alarms = Struct(
        "Alarm",
        count=2,
        id=Field(TagType.INT, retentive=True),
        On=Field(TagType.BOOL, retentive=False),
    )

    assert alarms[1].id.retentive is True
    assert alarms[1].On.retentive is False


def test_struct_rejects_invalid_declarations():
    with pytest.raises(ValueError, match="count"):
        Struct("Alarm", count=0, id=Field(TagType.INT))

    with pytest.raises(ValueError, match="requires a TagType"):
        Struct("Alarm", count=1, id=Field())

    with pytest.raises(TypeError, match="must be a Field"):
        Struct("Alarm", count=1, id=cast(Any, 123))


def test_struct_rejects_auto_default_for_non_numeric_type():
    with pytest.raises(ValueError, match="not numeric"):
        Struct("Alarm", count=1, flag=Field(TagType.BOOL, default=auto()))


def test_instance_view_validates_index_and_missing_attributes():
    alarms = Struct("Alarm", count=2, id=Field(TagType.INT))

    with pytest.raises(IndexError, match="out of range"):
        _ = alarms[0]

    with pytest.raises(IndexError, match="out of range"):
        _ = alarms[3]

    with pytest.raises(AttributeError, match="no field"):
        _ = alarms[1].missing
