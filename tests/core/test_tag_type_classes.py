"""Tests for class-based tag type constructors and decorator type resolution."""

from __future__ import annotations

from enum import IntEnum
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
        (Char, TagType.CHAR, True, "\x00"),
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


@pytest.mark.parametrize(
    ("factory", "custom_default"),
    [
        (Bool, True),
        (Int, 7),
        (Dint, 11),
        (Real, 1.5),
        (Word, 0xBEEF),
        (Char, "A"),
    ],
)
def test_tag_type_class_allows_default_override(factory, custom_default):
    tag = factory("X", default=custom_default)
    assert tag.default == custom_default


def test_tag_type_class_normalizes_intenum_choices_and_readonly():
    class Mode(IntEnum):
        IDLE = 0
        RUN = 1

    tag = Int("Mode", choices=Mode, readonly=True)

    assert tag.choices == {0: "IDLE", 1: "RUN"}
    assert tag.readonly is True


def test_tag_type_class_allows_mapping_choices():
    tag = Int("Mode", choices={0: "IDLE", 1: "RUN"})
    assert tag.choices == {0: "IDLE", 1: "RUN"}


def test_tag_type_class_allows_struct_runtime_choices():
    @udt(readonly=True)
    class SortState:
        IDLE: Int = 0  # ty: ignore[invalid-assignment]
        DETECTING: Int = 1  # ty: ignore[invalid-assignment]

    tag = Int("Mode", choices=cast(Any, SortState))

    assert tag.choices == {0: "IDLE", 1: "DETECTING"}


def test_tag_type_class_rejects_multi_instance_struct_choices():
    @udt(count=2)
    class SortState:
        IDLE: Int = 0  # ty: ignore[invalid-assignment]

    with pytest.raises(TypeError, match="count=1"):
        Int("Mode", choices=cast(Any, SortState))


def test_bool_tag_rejects_choices():
    with pytest.raises(TypeError, match="BOOL"):
        Bool("Flag", choices={0: "Off", 1: "On"})


def test_tag_type_class_rejects_bool_choice_keys():
    with pytest.raises(TypeError, match="keys must be int, float, or str"):
        Int("Mode", choices={True: "On"})


@pytest.mark.parametrize("factory", [Bool, Int, Dint, Real, Word, Char])
def test_unnamed_tag_type_class_outside_namespace_raises(factory):
    from pyrung.core._naming import PyrungNameError

    with pytest.raises(PyrungNameError):
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


@pytest.mark.parametrize(
    ("op", "expected_operand"),
    [
        ("eq", "\x00"),
        ("ne", "\x00"),
        ("lt", "\x00"),
        ("le", "\x00"),
        ("gt", "\x00"),
        ("ge", "\x00"),
    ],
)
def test_char_comparison_normalizes_empty_string(op, expected_operand):
    """Char == "" should behave like Char == '\\x00' (the hardware default)."""
    from pyrung.core.analysis.simplified import _condition_to_expr

    tag = Char("Mode")
    ops = {
        "eq": tag.__eq__,
        "ne": tag.__ne__,
        "lt": tag.__lt__,
        "le": tag.__le__,
        "gt": tag.__gt__,
        "ge": tag.__ge__,
    }
    cond = ops[op]("")
    atom = _condition_to_expr(cond)
    assert atom.operand == expected_operand
    assert atom.form == op


def test_named_array_rejects_invalid_base_type():
    with pytest.raises(TypeError, match="not supported"):

        @named_array(list[int], count=1)
        class Bad:
            value = 0
