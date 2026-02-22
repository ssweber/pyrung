"""Tests for AutoTag auto-naming declarations."""

import pytest

from pyrung.core import AutoTag, Block, Bool, Char, Dint, InputBlock, Int, OutputBlock, Real, Tag, TagType, Word
from pyrung.core.tag import LiveTag


@pytest.mark.parametrize(
    ("factory", "expected_type", "expected_retentive", "expected_default"),
    [
        (Bool, TagType.BOOL, False, False),
        (Int, TagType.INT, True, 0),
        (Dint, TagType.DINT, True, 0),
        (Real, TagType.REAL, True, 0.0),
        (Word, TagType.WORD, False, 0),
        (Char, TagType.CHAR, True, ""),
    ],
)
def test_auto_naming_for_all_core_constructors(
    factory, expected_type: TagType, expected_retentive: bool, expected_default: object
):
    class Tags(AutoTag):
        Auto = factory()

    assert isinstance(Tags.Auto, Tag)
    assert Tags.Auto.name == "Auto"
    assert Tags.Auto.type == expected_type
    assert Tags.Auto.retentive is expected_retentive
    assert Tags.Auto.default == expected_default


def test_auto_naming_preserves_retentive_override():
    class Tags(AutoTag):
        Latched = Bool(retentive=True)
        VolatileCounter = Int(retentive=False)

    assert Tags.Latched.retentive is True
    assert Tags.VolatileCounter.retentive is False


def test_namespace_registry_is_immutable_and_copyable():
    class Tags(AutoTag):
        Auto = Bool()
        Explicit = Int("Counter")

    assert set(Tags.__pyrung_tags__) == {"Auto", "Explicit"}
    assert Tags.__pyrung_tags__["Auto"] is Tags.Auto
    assert Tags.__pyrung_tags__["Explicit"] is Tags.Explicit

    with pytest.raises(TypeError):
        Tags.__pyrung_tags__["X"] = Bool("X")

    copy = Tags.tags()
    copy["Auto"] = Bool("Shadow")
    assert Tags.Auto.name == "Auto"


def test_export_flattens_tags_into_target_namespace():
    class Tags(AutoTag):
        Start = Bool()
        Count = Int()

    target: dict[str, object] = {}
    exported = Tags.export(target)

    assert set(exported) == {"Start", "Count"}
    assert target["Start"] is Tags.Start
    assert target["Count"] is Tags.Count


def test_export_rejects_name_conflicts_by_default():
    class Tags(AutoTag):
        Start = Bool()

    target: dict[str, object] = {"Start": object()}
    with pytest.raises(ValueError, match="conflicts with existing names"):
        Tags.export(target)


def test_export_overwrite_replaces_conflicting_names():
    class Tags(AutoTag):
        Start = Bool()

    sentinel = object()
    target: dict[str, object] = {"Start": sentinel}
    Tags.export(target, overwrite=True)

    assert target["Start"] is Tags.Start


@pytest.mark.parametrize("factory", [Bool, Int, Dint, Real, Word, Char])
def test_unnamed_constructor_outside_class_raises(factory):
    with pytest.raises(TypeError, match="AutoTag class body"):
        factory()


def test_unnamed_constructor_in_non_namespace_class_raises():
    with pytest.raises(TypeError, match="AutoTag subclass"):

        class NotNamespace:
            Auto = Bool()


def test_duplicate_names_in_same_namespace_raise():
    with pytest.raises(ValueError, match="Duplicate tag names"):

        class Bad(AutoTag):
            Auto = Bool()
            AlsoAuto = Bool("Auto")


def test_duplicate_names_across_inheritance_raise():
    class Base(AutoTag):
        Auto = Bool()

    with pytest.raises(ValueError, match="Duplicate tag names"):

        class Child(Base):
            Other = Bool("Auto")


def test_redeclaring_same_attribute_name_in_subclass_raises():
    class Base(AutoTag):
        Auto = Bool()

    with pytest.raises(ValueError, match="Duplicate tag names"):

        class Child(Base):
            Auto = Bool()


def test_explicit_tag_attributes_are_normalized_and_included():
    class Tags(AutoTag):
        Explicit = Tag("Explicit")
        Auto = Bool()

    assert isinstance(Tags.Explicit, Tag)
    assert Tags.Explicit.name == "Explicit"
    assert isinstance(Tags.Explicit, LiveTag)
    assert set(Tags.__pyrung_tags__) == {"Explicit", "Auto"}


@pytest.mark.parametrize("factory", [Block, InputBlock, OutputBlock])
def test_blocks_are_not_allowed_in_autotag_subclass(factory):
    with pytest.raises(TypeError, match="Block declarations are not allowed on AutoTag subclasses"):

        class Bad(AutoTag):
            Region = factory("DS", TagType.INT, 1, 5)
