"""Tests for Tag and TagType.

Tags are lightweight references to values in SystemState.
They carry type metadata but no runtime state.
"""

import pytest


class TestTagType:
    """Test TagType enum (IEC 61131-3 naming)."""

    def test_bool_type(self):
        """BOOL is the IEC 61131-3 name for boolean."""
        from pyrung.core import TagType

        assert TagType.BOOL.value == "bool"

    def test_int_type(self):
        from pyrung.core import TagType

        assert TagType.INT.value == "int"

    def test_real_type(self):
        """REAL is the IEC 61131-3 name for float."""
        from pyrung.core import TagType

        assert TagType.REAL.value == "real"

    def test_deprecated_enum_aliases_removed(self):
        from pyrung.core import TagType

        for alias in ("BIT", "INT2", "FLOAT", "HEX", "TXT"):
            assert not hasattr(TagType, alias)

    @pytest.mark.parametrize("deprecated_name", ["bit", "int2", "float", "hex", "txt"])
    def test_deprecated_string_aliases_fail(self, deprecated_name: str):
        from pyrung.core import TagType

        with pytest.raises(ValueError):
            TagType(deprecated_name)


class TestTagCreation:
    """Test Tag construction."""

    def test_create_with_name_only(self):
        """Tag with just name defaults to BOOL type."""
        from pyrung.core import Tag, TagType

        tag = Tag("Button")

        assert tag.name == "Button"
        assert tag.type == TagType.BOOL
        assert tag.retentive is False

    def test_create_with_type(self):
        """Tag with explicit type."""
        from pyrung.core import Tag, TagType

        tag = Tag("Counter", TagType.INT)

        assert tag.name == "Counter"
        assert tag.type == TagType.INT

    def test_create_with_retentive(self):
        """Tag with retentive flag."""
        from pyrung.core import Tag, TagType

        tag = Tag("SavedValue", TagType.INT, retentive=True)

        assert tag.retentive is True

    def test_create_with_default_value(self):
        """Tag with custom default value."""
        from pyrung.core import Tag, TagType

        tag = Tag("StartCount", TagType.INT, default=100)

        assert tag.default == 100


class TestTagHelpers:
    """Test convenience constructors."""

    def test_bool_helper(self):
        """Bool() creates a BOOL tag."""
        from pyrung.core import Bool, TagType

        tag = Bool("Light")

        assert tag.name == "Light"
        assert tag.type == TagType.BOOL
        assert tag.retentive is False
        assert tag.default is False

    def test_bool_retentive(self):
        """Bool() can be retentive."""
        from pyrung.core import Bool

        tag = Bool("Latched", retentive=True)

        assert tag.retentive is True

    def test_int_helper(self):
        """Int() creates an INT tag, retentive by default."""
        from pyrung.core import Int, TagType

        tag = Int("Counter")

        assert tag.name == "Counter"
        assert tag.type == TagType.INT
        assert tag.retentive is True
        assert tag.default == 0

    def test_real_helper(self):
        """Real() creates a REAL tag."""
        from pyrung.core import Real, TagType

        tag = Real("Temperature")

        assert tag.name == "Temperature"
        assert tag.type == TagType.REAL
        assert tag.retentive is True
        assert tag.default == 0.0


class TestTagEquality:
    """Test Tag equality and hashing."""

    def test_tags_equal_by_name(self):
        """Tags with same name are equal."""
        from pyrung.core import Tag

        tag1 = Tag("X1")
        tag2 = Tag("X1")

        assert tag1 == tag2

    def test_tags_hashable(self):
        """Tags can be used in sets and as dict keys."""
        from pyrung.core import Tag

        tag1 = Tag("X1")
        tag2 = Tag("X1")
        tag3 = Tag("X2")

        tag_set = {tag1, tag2, tag3}

        assert len(tag_set) == 2  # X1 appears once


class TestWordHelper:
    """Test Word() convenience constructor."""

    def test_word_creates_word_tag(self):
        """Word() creates a WORD tag."""
        from pyrung.core import TagType
        from pyrung.core.tag import Word

        tag = Word("StatusReg")
        assert tag.name == "StatusReg"
        assert tag.type == TagType.WORD
        assert tag.default == 0

    def test_word_default_not_retentive(self):
        """Word() defaults to non-retentive."""
        from pyrung.core.tag import Word

        tag = Word("Flags")
        assert tag.retentive is False

    def test_word_retentive(self):
        """Word() can be made retentive."""
        from pyrung.core.tag import Word

        tag = Word("Saved", retentive=True)
        assert tag.retentive is True


class TestInputTag:
    """Test InputTag subclass."""

    def test_input_tag_is_tag(self):
        """InputTag is an instance of Tag."""
        from pyrung.core.tag import InputTag, TagType

        tag = InputTag("X1", TagType.BOOL)
        assert isinstance(tag, InputTag)
        from pyrung.core.tag import Tag

        assert isinstance(tag, Tag)

    def test_input_tag_has_immediate(self):
        """InputTag has .immediate property returning ImmediateRef."""
        from pyrung.core.tag import ImmediateRef, InputTag, TagType

        tag = InputTag("X1", TagType.BOOL)
        ref = tag.immediate
        assert isinstance(ref, ImmediateRef)
        assert ref.tag is tag

    def test_input_tag_frozen(self):
        """InputTag is frozen."""
        from pyrung.core.tag import InputTag, TagType

        tag = InputTag("X1", TagType.BOOL)
        with pytest.raises(AttributeError):
            setattr(tag, "name", "X2")


class TestOutputTag:
    """Test OutputTag subclass."""

    def test_output_tag_is_tag(self):
        """OutputTag is an instance of Tag."""
        from pyrung.core.tag import OutputTag, TagType

        tag = OutputTag("Y1", TagType.BOOL)
        assert isinstance(tag, OutputTag)
        from pyrung.core.tag import Tag

        assert isinstance(tag, Tag)

    def test_output_tag_has_immediate(self):
        """OutputTag has .immediate property returning ImmediateRef."""
        from pyrung.core.tag import ImmediateRef, OutputTag, TagType

        tag = OutputTag("Y1", TagType.BOOL)
        ref = tag.immediate
        assert isinstance(ref, ImmediateRef)
        assert ref.tag is tag

    def test_output_tag_frozen(self):
        """OutputTag is frozen."""
        from pyrung.core.tag import OutputTag, TagType

        tag = OutputTag("Y1", TagType.BOOL)
        with pytest.raises(AttributeError):
            setattr(tag, "name", "Y2")


class TestImmediateRef:
    """Test ImmediateRef wrapper."""

    def test_immediate_ref_wraps_tag(self):
        """ImmediateRef wraps a tag."""
        from pyrung.core.tag import ImmediateRef, InputTag, TagType

        tag = InputTag("X1", TagType.BOOL)
        ref = ImmediateRef(tag)
        assert ref.tag is tag

    def test_plain_tag_no_immediate(self):
        """Plain Tag does NOT have .immediate."""
        from pyrung.core.tag import Tag

        tag = Tag("C1")
        with pytest.raises(AttributeError):
            getattr(tag, "immediate")


class TestTagComparison:
    """Test Tag comparison operators create Conditions."""

    def test_tag_eq_literal_creates_condition(self):
        """tag == value creates a CompareEq condition."""
        from pyrung.core import Int

        Step = Int("Step")
        cond = Step == 0

        # Should return a Condition, not a bool
        assert not isinstance(cond, bool)
        assert hasattr(cond, "evaluate")

    def test_tag_ne_creates_condition(self):
        """tag != value creates a CompareNe condition."""
        from pyrung.core import Int

        Step = Int("Step")
        cond = Step != 0

        assert not isinstance(cond, bool)
        assert hasattr(cond, "evaluate")

    def test_tag_lt_creates_condition(self):
        """tag < value creates a CompareLt condition."""
        from pyrung.core import Int

        Count = Int("Count")
        cond = Count < 10

        assert not isinstance(cond, bool)
        assert hasattr(cond, "evaluate")

    def test_tag_gt_creates_condition(self):
        """tag > value creates a CompareGt condition."""
        from pyrung.core import Int

        Count = Int("Count")
        cond = Count > 10

        assert not isinstance(cond, bool)
        assert hasattr(cond, "evaluate")

    def test_tag_bool_raises(self):
        """Using Tag as bool raises TypeError."""
        from pyrung.core import Bool

        Button = Bool("Button")

        with pytest.raises(TypeError):
            if Button:  # noqa: F634
                pass

    def test_bool_tag_as_condition(self):
        """BOOL tags can be used directly as conditions."""
        from pyrung.core import Bool, TagType

        Button = Bool("Button")

        # When used in Rung(), a BOOL tag becomes a normally open contact.
        # Rung behavior is tested in test_rung.py.
        assert Button.type == TagType.BOOL
