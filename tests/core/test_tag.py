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

    def test_word_default_retentive(self):
        """Word() defaults to retentive."""
        from pyrung.core.tag import Word

        tag = Word("Flags")
        assert tag.retentive is True

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
            setattr(tag, "name", "X2")  # noqa: B010


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
            setattr(tag, "name", "Y2")  # noqa: B010


class TestImmediateRef:
    """Test ImmediateRef wrapper."""

    def test_immediate_ref_wraps_tag(self):
        """ImmediateRef wraps a tag."""
        from pyrung.core.tag import ImmediateRef, InputTag, TagType

        tag = InputTag("X1", TagType.BOOL)
        ref = ImmediateRef(tag)
        assert ref.tag is tag

    def test_immediate_helper_wraps_tag(self):
        from pyrung.core import InputBlock, TagType, immediate

        X = InputBlock("X", TagType.BOOL, 1, 8)
        tag = X[1]
        ref = immediate(tag)
        assert ref.tag is tag

    def test_immediate_helper_wraps_block_range(self):
        from pyrung.core import OutputBlock, TagType, immediate

        Y = OutputBlock("Y", TagType.BOOL, 1, 8)
        target = Y.select(1, 4)
        ref = immediate(target)
        assert ref.value == target

    def test_plain_tag_no_immediate(self):
        """Plain Tag does NOT have .immediate."""
        from pyrung.core.tag import Tag

        tag = Tag("C1")
        with pytest.raises(AttributeError):
            getattr(tag, "immediate")  # noqa: B009


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


class TestTagInvertOperator:
    def test_bool_invert_creates_normally_closed_condition(self):
        from pyrung.core import Bool
        from pyrung.core.condition import NormallyClosedCondition

        button = Bool("Button")
        cond = ~button

        assert isinstance(cond, NormallyClosedCondition)
        assert cond.tag is button
        assert cond.source_line is not None

    def test_int_invert_stays_expression(self):
        from pyrung.core import Int
        from pyrung.core.expression import UnaryExpr

        step = Int("Step")
        expr = ~step

        assert isinstance(expr, UnaryExpr)
        assert expr.symbol == "~"


class TestTagDefaultSeeding:
    """Tag defaults must be seeded into initial SystemState."""

    def test_bool_default_true_in_initial_state(self, runner_factory):
        """Bool(default=True) should appear in state at construction."""
        from pyrung import Bool, Program, Rung, reset

        StopBtn = Bool("StopBtn", default=True)
        Running = Bool("Running")

        with Program(strict=False) as logic:
            with Rung(~StopBtn):
                reset(Running)

        runner = runner_factory(logic)
        assert runner.current_state.tags["StopBtn"] is True

    def test_bool_default_true_condition_agrees_with_value(self, runner_factory):
        """~Bool(default=True) should evaluate False — condition must agree with .value."""
        from pyrung import Bool, Program, Rung, out

        Flag = Bool("Flag", default=True)
        Result = Bool("Result")

        with Program(strict=False) as logic:
            with Rung(~Flag):
                out(Result)

        runner = runner_factory(logic)
        runner.step()
        # ~Flag is NormallyClosed: True when Flag is off. Flag is True, so condition is False.
        assert runner.current_state.tags["Result"] is False

    def test_standard_defaults_also_seeded(self, runner_factory):
        """Even tags with standard defaults (Bool=False) should be in state."""
        from pyrung import Bool, Int, Program, Rung, out

        X = Bool("X")
        Y = Int("Y")

        with Program(strict=False) as logic:
            with Rung(X):
                out(Y)

        runner = runner_factory(logic)
        assert "X" in runner.current_state.tags
        assert runner.current_state.tags["X"] is False
        assert "Y" in runner.current_state.tags
        assert runner.current_state.tags["Y"] == 0

    def test_initial_state_not_overwritten_by_defaults(self, runner_factory):
        """User-provided initial_state values take precedence over tag defaults."""
        from pyrung import Bool, Program, Rung, out
        from pyrung.core.state import SystemState

        X = Bool("X")  # default=False

        with Program(strict=False) as logic:
            with Rung(X):
                out(Bool("Out"))

        state = SystemState().with_tags({"X": True})
        runner = runner_factory(logic, initial_state=state)
        assert runner.current_state.tags["X"] is True

    def test_named_array_symbol_default_normalizes_to_scalar(self):
        """default=NamedArray.FIELD should store the field's scalar default."""
        from pyrung import Int, named_array

        @named_array(Int, stride=2, readonly=True)
        class SortState:
            IDLE = 0
            RUNNING = 1

        state = Int("State", choices=SortState, default=SortState.IDLE)
        assert state.default == 0


class TestTagNameInference:
    """Test name inference from assignment target via the executing library."""

    def test_infer_in_function(self):
        from pyrung.core import Bool

        Light = Bool()

        assert Light.name == "Light"

    def test_infer_int(self):
        from pyrung.core import Int

        Step = Int()

        assert Step.name == "Step"

    def test_infer_real(self):
        from pyrung.core import Real

        Temp = Real()

        assert Temp.name == "Temp"

    def test_infer_dint(self):
        from pyrung.core import Dint

        Total = Dint()

        assert Total.name == "Total"

    def test_infer_word(self):
        from pyrung.core import Word

        Status = Word()

        assert Status.name == "Status"

    def test_infer_char(self):
        from pyrung.core import Char

        Mode = Char()

        assert Mode.name == "Mode"

    def test_infer_with_kwargs(self):
        from pyrung.core import Bool

        Latch = Bool(retentive=True)

        assert Latch.name == "Latch"
        assert Latch.retentive is True

    def test_explicit_name_still_works(self):
        from pyrung.core import Bool

        tag = Bool("Explicit")

        assert tag.name == "Explicit"

    def test_explicit_name_wins_on_mismatch(self):
        import warnings

        from pyrung.core import Bool
        from pyrung.core._naming import PyrungNameWarning

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            Foo = Bool("Bar")

        assert Foo.name == "Bar"
        assert any(issubclass(x.category, PyrungNameWarning) for x in w)

    def test_matching_names_no_warning(self):
        import warnings

        from pyrung.core import Bool
        from pyrung.core._naming import PyrungNameWarning

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            Light = Bool("Light")

        assert Light.name == "Light"
        assert not any(issubclass(x.category, PyrungNameWarning) for x in w)

    def test_no_assignment_raises(self):
        import pytest

        from pyrung.core import Bool
        from pyrung.core._naming import PyrungNameError

        with pytest.raises(PyrungNameError):
            Bool()

    def test_attribute_assignment(self):
        from pyrung.core import Bool

        class Obj:
            pass

        obj = Obj()
        obj.flag = Bool()

        assert obj.flag.name == "flag"

    def test_annotated_assignment(self):
        from pyrung.core import Bool
        from pyrung.core.tag import LiveTag

        Ready: LiveTag = Bool()

        assert Ready.name == "Ready"


class TestTypedBlockInference:
    """Test name inference for typed block constructors."""

    def test_int_block_infer(self):
        from pyrung.core import IntBlock, TagType

        DS = IntBlock(1, 100)

        assert DS.name == "DS"
        assert DS.type == TagType.INT
        assert DS.start == 1
        assert DS.end == 100

    def test_bool_block_infer(self):
        from pyrung.core import BoolBlock, TagType

        Flags = BoolBlock(1, 16)

        assert Flags.name == "Flags"
        assert Flags.type == TagType.BOOL

    def test_dint_block_infer(self):
        from pyrung.core import DintBlock, TagType

        Totals = DintBlock(1, 50)

        assert Totals.name == "Totals"
        assert Totals.type == TagType.DINT

    def test_real_block_infer(self):
        from pyrung.core import RealBlock, TagType

        Temps = RealBlock(1, 10)

        assert Temps.name == "Temps"
        assert Temps.type == TagType.REAL

    def test_word_block_infer(self):
        from pyrung.core import TagType, WordBlock

        Regs = WordBlock(1, 32)

        assert Regs.name == "Regs"
        assert Regs.type == TagType.WORD

    def test_char_block_infer(self):
        from pyrung.core import CharBlock, TagType

        Chars = CharBlock(1, 8)

        assert Chars.name == "Chars"
        assert Chars.type == TagType.CHAR

    def test_typed_block_explicit_name(self):
        from pyrung.core import IntBlock

        DS = IntBlock(1, 100, name="DS")

        assert DS.name == "DS"

    def test_typed_block_name_mismatch_warning(self):
        import warnings

        from pyrung.core import IntBlock
        from pyrung.core._naming import PyrungNameWarning

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            DS = IntBlock(1, 100, name="Foo")

        assert DS.name == "Foo"
        assert any(issubclass(x.category, PyrungNameWarning) for x in w)

    def test_typed_block_no_assignment_raises(self):
        import pytest

        from pyrung.core import IntBlock
        from pyrung.core._naming import PyrungNameError

        with pytest.raises(PyrungNameError):
            IntBlock(1, 100)

    def test_typed_block_retentive_default(self):
        from pyrung.core import BoolBlock, IntBlock

        DS = IntBlock(1, 100)
        Flags = BoolBlock(1, 16)

        assert DS.retentive is True
        assert Flags.retentive is False

    def test_typed_block_retentive_override(self):
        from pyrung.core import IntBlock

        DS = IntBlock(1, 100, retentive=False)

        assert DS.retentive is False

    def test_typed_block_indexing(self):
        from pyrung.core import IntBlock, TagType

        DS = IntBlock(1, 10)

        tag = DS[1]
        assert tag.name == "DS1"
        assert tag.type == TagType.INT
