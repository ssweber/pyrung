"""Tests for class-based tag type constructors and decorator type resolution."""

from __future__ import annotations

from typing import Any, cast

import pytest

from pyrung.core import Bool, Char, Dint, Int, Real, TagType, Word, named_array, udt
from pyrung.core.tag import LiveTag


@pytest.mark.parametrize(
    ("factory", "expected_type", "expected_retentive", "expected_default"),
    [
        (Bool, TagType.BOOL, False, False),
        (Int, TagType.INT, True, 0),
        (Dint, TagType.DINT, True, 0),
        (Real, TagType.REAL, True, 0.0),
        (Word, TagType.WORD, True, 0),
        (Char, TagType.CHAR, True, ""),
    ],
)
def test_tag_type_class_constructor_returns_live_tag(
    factory, expected_type: TagType, expected_retentive: bool, expected_default: object
):
    tag = factory("X")
    assert isinstance(tag, LiveTag)
    assert tag.name == "X"
    assert tag.type == expected_type
    assert tag.retentive is expected_retentive
    assert tag.default == expected_default


@pytest.mark.parametrize("factory", [Bool, Int, Dint, Real, Word, Char])
def test_tag_type_class_allows_retentive_override(factory):
    tag = factory("X", retentive=False)
    assert tag.retentive is False


@pytest.mark.parametrize("factory", [Bool, Int, Dint, Real, Word, Char])
def test_unnamed_tag_type_class_outside_namespace_raises(factory):
    with pytest.raises(TypeError):
        factory()


def test_udt_resolves_primitive_and_string_annotations():
    @udt(count=1)
    class Values:
        flag: bool
        total: int
        ratio: float
        text: str
        wide: Word

    values = cast(Any, Values)
    assert values.flag.type == TagType.BOOL
    assert values.total.type == TagType.INT
    assert values.ratio.type == TagType.REAL
    assert values.text.type == TagType.CHAR
    assert values.wide.type == TagType.WORD


def test_named_array_resolves_primitive_and_string_base_types():
    @named_array(int, count=1)
    class IntData:
        value = 0

    @named_array("REAL", count=1)
    class RealData:
        value = 0.0

    int_data = cast(Any, IntData)
    real_data = cast(Any, RealData)
    assert int_data.type == TagType.INT
    assert real_data.type == TagType.REAL


def test_udt_rejects_invalid_annotation():
    with pytest.raises(TypeError, match="not supported"):

        @udt(count=1)
        class Bad:
            value: list[int]


def test_named_array_rejects_invalid_base_type():
    with pytest.raises(TypeError, match="not supported"):

        @named_array(list[int], count=1)
        class Bad:
            value = 0
