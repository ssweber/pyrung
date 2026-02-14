"""Tests for Condition classes.

Conditions are pure functions that evaluate against SystemState.
"""

from pyrung.core import Bool, Int, SystemState
from tests.conftest import evaluate_condition


class TestCompareEq:
    """Test equality comparison condition."""

    def test_eq_true_when_equal(self):
        """Condition is true when tag value equals comparison value."""
        Step = Int("Step")
        cond = Step == 0

        state = SystemState().with_tags({"Step": 0})

        assert evaluate_condition(cond, state) is True

    def test_eq_false_when_not_equal(self):
        """Condition is false when tag value differs."""
        Step = Int("Step")
        cond = Step == 0

        state = SystemState().with_tags({"Step": 5})

        assert evaluate_condition(cond, state) is False

    def test_eq_with_missing_tag_is_none(self):
        """Missing tag returns None, which != most values."""
        Step = Int("Step")
        cond = Step == 0

        state = SystemState()  # No "Step" tag

        assert evaluate_condition(cond, state) is False  # None != 0


class TestCompareNe:
    """Test inequality comparison condition."""

    def test_ne_true_when_not_equal(self):
        """Condition is true when values differ."""
        Step = Int("Step")
        cond = Step != 0

        state = SystemState().with_tags({"Step": 5})

        assert evaluate_condition(cond, state) is True

    def test_ne_false_when_equal(self):
        """Condition is false when values match."""
        Step = Int("Step")
        cond = Step != 0

        state = SystemState().with_tags({"Step": 0})

        assert evaluate_condition(cond, state) is False


class TestCompareLt:
    """Test less-than comparison condition."""

    def test_lt_true(self):
        Count = Int("Count")
        cond = Count < 10

        state = SystemState().with_tags({"Count": 5})

        assert evaluate_condition(cond, state) is True

    def test_lt_false_when_equal(self):
        Count = Int("Count")
        cond = Count < 10

        state = SystemState().with_tags({"Count": 10})

        assert evaluate_condition(cond, state) is False

    def test_lt_false_when_greater(self):
        Count = Int("Count")
        cond = Count < 10

        state = SystemState().with_tags({"Count": 15})

        assert evaluate_condition(cond, state) is False


class TestCompareGt:
    """Test greater-than comparison condition."""

    def test_gt_true(self):
        Count = Int("Count")
        cond = Count > 10

        state = SystemState().with_tags({"Count": 15})

        assert evaluate_condition(cond, state) is True

    def test_gt_false_when_equal(self):
        Count = Int("Count")
        cond = Count > 10

        state = SystemState().with_tags({"Count": 10})

        assert evaluate_condition(cond, state) is False


class TestCompareLe:
    """Test less-than-or-equal comparison condition."""

    def test_le_true_when_less(self):
        Count = Int("Count")
        cond = Count <= 10

        state = SystemState().with_tags({"Count": 5})

        assert evaluate_condition(cond, state) is True

    def test_le_true_when_equal(self):
        Count = Int("Count")
        cond = Count <= 10

        state = SystemState().with_tags({"Count": 10})

        assert evaluate_condition(cond, state) is True

    def test_le_false_when_greater(self):
        Count = Int("Count")
        cond = Count <= 10

        state = SystemState().with_tags({"Count": 15})

        assert evaluate_condition(cond, state) is False


class TestCompareGe:
    """Test greater-than-or-equal comparison condition."""

    def test_ge_true_when_greater(self):
        Count = Int("Count")
        cond = Count >= 10

        state = SystemState().with_tags({"Count": 15})

        assert evaluate_condition(cond, state) is True

    def test_ge_true_when_equal(self):
        Count = Int("Count")
        cond = Count >= 10

        state = SystemState().with_tags({"Count": 10})

        assert evaluate_condition(cond, state) is True

    def test_ge_false_when_less(self):
        Count = Int("Count")
        cond = Count >= 10

        state = SystemState().with_tags({"Count": 5})

        assert evaluate_condition(cond, state) is False


class TestBitCondition:
    """Test bit tag as condition (normally open contact)."""

    def test_bit_true_when_on(self):
        """BitCondition is true when bit is True/1."""
        from pyrung.core.condition import BitCondition

        Button = Bool("Button")
        cond = BitCondition(Button)

        state = SystemState().with_tags({"Button": True})

        assert evaluate_condition(cond, state) is True

    def test_bit_false_when_off(self):
        """BitCondition is false when bit is False/0."""
        from pyrung.core.condition import BitCondition

        Button = Bool("Button")
        cond = BitCondition(Button)

        state = SystemState().with_tags({"Button": False})

        assert evaluate_condition(cond, state) is False

    def test_bit_false_when_missing(self):
        """BitCondition is false when tag is missing (defaults to False)."""
        from pyrung.core.condition import BitCondition

        Button = Bool("Button")
        cond = BitCondition(Button)

        state = SystemState()

        assert evaluate_condition(cond, state) is False


class TestNormallyClosedCondition:
    """Test normally closed contact (NC) - inverted bit check."""

    def test_nc_true_when_off(self):
        """NC is true when bit is False/0."""
        from pyrung.core.condition import NormallyClosedCondition

        Button = Bool("Button")
        cond = NormallyClosedCondition(Button)

        state = SystemState().with_tags({"Button": False})

        assert evaluate_condition(cond, state) is True

    def test_nc_false_when_on(self):
        """NC is false when bit is True/1."""
        from pyrung.core.condition import NormallyClosedCondition

        Button = Bool("Button")
        cond = NormallyClosedCondition(Button)

        state = SystemState().with_tags({"Button": True})

        assert evaluate_condition(cond, state) is False


class TestRisingEdgeCondition:
    """Test rising edge detection (one-shot on 0->1 transition)."""

    def test_rise_true_on_transition(self):
        """Rising edge is true when current=True, previous=False."""
        from pyrung.core.condition import RisingEdgeCondition

        Button = Bool("Button")
        cond = RisingEdgeCondition(Button)

        state = SystemState().with_tags({"Button": True}).with_memory({"_prev:Button": False})

        assert evaluate_condition(cond, state) is True

    def test_rise_false_when_already_on(self):
        """Rising edge is false when already on (no transition)."""
        from pyrung.core.condition import RisingEdgeCondition

        Button = Bool("Button")
        cond = RisingEdgeCondition(Button)

        state = SystemState().with_tags({"Button": True}).with_memory({"_prev:Button": True})

        assert evaluate_condition(cond, state) is False

    def test_rise_false_when_off(self):
        """Rising edge is false when current is off."""
        from pyrung.core.condition import RisingEdgeCondition

        Button = Bool("Button")
        cond = RisingEdgeCondition(Button)

        state = SystemState().with_tags({"Button": False}).with_memory({"_prev:Button": False})

        assert evaluate_condition(cond, state) is False


class TestFallingEdgeCondition:
    """Test falling edge detection (one-shot on 1->0 transition)."""

    def test_fall_true_on_transition(self):
        """Falling edge is true when current=False, previous=True."""
        from pyrung.core.condition import FallingEdgeCondition

        Button = Bool("Button")
        cond = FallingEdgeCondition(Button)

        state = SystemState().with_tags({"Button": False}).with_memory({"_prev:Button": True})

        assert evaluate_condition(cond, state) is True

    def test_fall_false_when_already_off(self):
        """Falling edge is false when already off (no transition)."""
        from pyrung.core.condition import FallingEdgeCondition

        Button = Bool("Button")
        cond = FallingEdgeCondition(Button)

        state = SystemState().with_tags({"Button": False}).with_memory({"_prev:Button": False})

        assert evaluate_condition(cond, state) is False

    def test_fall_false_when_on(self):
        """Falling edge is false when current is on."""
        from pyrung.core.condition import FallingEdgeCondition

        Button = Bool("Button")
        cond = FallingEdgeCondition(Button)

        state = SystemState().with_tags({"Button": True}).with_memory({"_prev:Button": True})

        assert evaluate_condition(cond, state) is False


class TestAnyOf:
    """Test any_of() composite condition (OR logic)."""

    def test_any_of_true_when_first_true(self):
        """any_of is true when first condition is true."""
        from pyrung.core import any_of

        Start = Bool("Start")
        CmdStart = Bool("CmdStart")
        cond = any_of(Start, CmdStart)

        state = SystemState().with_tags({"Start": True, "CmdStart": False})

        assert evaluate_condition(cond, state) is True

    def test_any_of_true_when_second_true(self):
        """any_of is true when second condition is true."""
        from pyrung.core import any_of

        Start = Bool("Start")
        CmdStart = Bool("CmdStart")
        cond = any_of(Start, CmdStart)

        state = SystemState().with_tags({"Start": False, "CmdStart": True})

        assert evaluate_condition(cond, state) is True

    def test_any_of_true_when_both_true(self):
        """any_of is true when both conditions are true."""
        from pyrung.core import any_of

        Start = Bool("Start")
        CmdStart = Bool("CmdStart")
        cond = any_of(Start, CmdStart)

        state = SystemState().with_tags({"Start": True, "CmdStart": True})

        assert evaluate_condition(cond, state) is True

    def test_any_of_false_when_all_false(self):
        """any_of is false when all conditions are false."""
        from pyrung.core import any_of

        Start = Bool("Start")
        CmdStart = Bool("CmdStart")
        cond = any_of(Start, CmdStart)

        state = SystemState().with_tags({"Start": False, "CmdStart": False})

        assert evaluate_condition(cond, state) is False

    def test_any_of_with_comparisons(self):
        """any_of works with comparison conditions."""
        from pyrung.core import any_of

        Step = Int("Step")
        Mode = Int("Mode")
        cond = any_of(Step == 0, Mode == 1)

        # Step is 0, Mode is not 1
        state = SystemState().with_tags({"Step": 0, "Mode": 0})
        assert evaluate_condition(cond, state) is True

        # Step is not 0, Mode is 1
        state = SystemState().with_tags({"Step": 5, "Mode": 1})
        assert evaluate_condition(cond, state) is True

        # Neither matches
        state = SystemState().with_tags({"Step": 5, "Mode": 0})
        assert evaluate_condition(cond, state) is False

    def test_any_of_with_three_conditions(self):
        """any_of works with more than two conditions."""
        from pyrung.core import any_of

        A = Bool("A")
        B = Bool("B")
        C = Bool("C")
        cond = any_of(A, B, C)

        # Only C is true
        state = SystemState().with_tags({"A": False, "B": False, "C": True})
        assert evaluate_condition(cond, state) is True

        # All false
        state = SystemState().with_tags({"A": False, "B": False, "C": False})
        assert evaluate_condition(cond, state) is False

    def test_any_of_with_int_truthiness(self):
        """any_of treats INT tags as truthy when nonzero."""
        from pyrung.core import any_of

        Step = Int("Step")
        Start = Bool("Start")
        cond = any_of(Step, Start)

        state = SystemState().with_tags({"Step": 2, "Start": False})
        assert evaluate_condition(cond, state) is True

        state = SystemState().with_tags({"Step": 0, "Start": False})
        assert evaluate_condition(cond, state) is False

    def test_any_of_rejects_dint_direct_tag(self):
        """Direct non-INT numeric tags remain invalid in grouped helpers."""
        import pytest

        from pyrung.core import Dint, any_of

        Step32 = Dint("Step32")
        Start = Bool("Start")

        with pytest.raises(TypeError, match="BOOL and INT"):
            any_of(Step32, Start)


class TestBitwiseOrOperator:
    """Test | operator for combining conditions (OR logic)."""

    def test_tag_or_tag(self):
        """Bool tags can be ORed with | operator."""
        Start = Bool("Start")
        CmdStart = Bool("CmdStart")
        cond = Start | CmdStart

        state = SystemState().with_tags({"Start": False, "CmdStart": True})
        assert evaluate_condition(cond, state) is True

        state = SystemState().with_tags({"Start": False, "CmdStart": False})
        assert evaluate_condition(cond, state) is False

    def test_condition_or_tag(self):
        """Conditions can be ORed with tags."""
        Step = Int("Step")
        Start = Bool("Start")
        cond = (Step == 0) | Start

        # Step is 0
        state = SystemState().with_tags({"Step": 0, "Start": False})
        assert evaluate_condition(cond, state) is True

        # Start is True
        state = SystemState().with_tags({"Step": 5, "Start": True})
        assert evaluate_condition(cond, state) is True

        # Neither
        state = SystemState().with_tags({"Step": 5, "Start": False})
        assert evaluate_condition(cond, state) is False

    def test_tag_or_condition(self):
        """Tags can be ORed with conditions."""
        Start = Bool("Start")
        Step = Int("Step")
        cond = Start | (Step == 0)

        state = SystemState().with_tags({"Start": True, "Step": 5})
        assert evaluate_condition(cond, state) is True

    def test_chained_or(self):
        """Multiple | operators chain correctly."""
        A = Bool("A")
        B = Bool("B")
        C = Bool("C")
        cond = A | B | C

        # Only C is true
        state = SystemState().with_tags({"A": False, "B": False, "C": True})
        assert evaluate_condition(cond, state) is True

        # All false
        state = SystemState().with_tags({"A": False, "B": False, "C": False})
        assert evaluate_condition(cond, state) is False


class TestOrPrecedenceErrors:
    """Test helpful errors for | operator precedence mistakes."""

    def test_int_or_tag_raises_error(self):
        """0 | Tag raises helpful error about parentheses."""
        import pytest

        Start = Bool("Start")

        with pytest.raises(TypeError, match="add parentheses"):
            _ = 0 | Start

    def test_tag_or_int_raises_error(self):
        """Tag | 0 raises helpful error about parentheses."""
        import pytest

        Start = Bool("Start")

        with pytest.raises(TypeError, match="add parentheses"):
            _ = Start | 0

    def test_condition_eq_int_raises_error(self):
        """AnyCondition == 0 raises helpful error about parentheses."""
        import pytest

        A = Bool("A")
        B = Bool("B")
        cond = A | B

        with pytest.raises(TypeError, match="add parentheses"):
            _ = cond == 0

    def test_condition_eq_condition_works(self):
        """Condition == Condition uses identity comparison."""
        A = Bool("A")
        B = Bool("B")
        cond1 = A | B
        cond2 = A | B

        # Different objects, so not equal
        assert (cond1 == cond1) is True
        assert (cond1 == cond2) is False


class TestAllOf:
    """Test all_of() composite condition (AND logic)."""

    def test_all_of_true_when_all_true(self):
        """all_of is true when all conditions are true."""
        from pyrung.core import all_of

        Ready = Bool("Ready")
        Auto = Bool("Auto")
        cond = all_of(Ready, Auto)

        state = SystemState().with_tags({"Ready": True, "Auto": True})
        assert evaluate_condition(cond, state) is True

    def test_all_of_false_when_any_false(self):
        """all_of is false when any condition is false."""
        from pyrung.core import all_of

        Ready = Bool("Ready")
        Auto = Bool("Auto")
        cond = all_of(Ready, Auto)

        state = SystemState().with_tags({"Ready": True, "Auto": False})
        assert evaluate_condition(cond, state) is False

    def test_all_of_with_comparisons(self):
        """all_of works with comparison conditions."""
        from pyrung.core import all_of

        Step = Int("Step")
        Mode = Int("Mode")
        cond = all_of(Step == 1, Mode == 2)

        state = SystemState().with_tags({"Step": 1, "Mode": 2})
        assert evaluate_condition(cond, state) is True

        state = SystemState().with_tags({"Step": 1, "Mode": 1})
        assert evaluate_condition(cond, state) is False

    def test_all_of_with_int_truthiness(self):
        """all_of treats INT tags as truthy when nonzero."""
        from pyrung.core import all_of

        Step = Int("Step")
        Ready = Bool("Ready")
        cond = all_of(Step, Ready)

        state = SystemState().with_tags({"Step": 1, "Ready": True})
        assert evaluate_condition(cond, state) is True

        state = SystemState().with_tags({"Step": 0, "Ready": True})
        assert evaluate_condition(cond, state) is False


class TestGroupedAnyOf:
    """Test explicit grouped AND terms inside any_of()."""

    def test_any_of_with_explicit_all_of_group(self):
        """Grouped AND terms require explicit all_of()."""
        from pyrung.core import all_of, any_of

        Start = Bool("Start")
        Ready = Bool("Ready")
        Auto = Bool("Auto")
        Remote = Bool("Remote")
        cond = any_of(Start, all_of(Ready, Auto), Remote)

        state = SystemState().with_tags(
            {"Start": False, "Ready": True, "Auto": True, "Remote": False}
        )
        assert evaluate_condition(cond, state) is True

        state = SystemState().with_tags(
            {"Start": False, "Ready": True, "Auto": False, "Remote": False}
        )
        assert evaluate_condition(cond, state) is False

    def test_any_of_rejects_tuple_group(self):
        """Tuple groups must be written explicitly with all_of() or &."""
        import pytest

        from pyrung.core import any_of

        A = Bool("A")
        B = Bool("B")
        C = Bool("C")

        with pytest.raises(TypeError, match="all_of\\(\\.\\.\\.\\) or '&'"):
            any_of((A, B), C)  # type: ignore[arg-type]

    def test_any_of_rejects_list_group(self):
        """List groups must be written explicitly with all_of() or &."""
        import pytest

        from pyrung.core import any_of

        A = Bool("A")
        B = Bool("B")
        C = Bool("C")

        with pytest.raises(TypeError, match="all_of\\(\\.\\.\\.\\) or '&'"):
            any_of([A, B], C)  # type: ignore[arg-type]


class TestBitwiseAndOperator:
    """Test & operator for combining conditions (AND logic)."""

    def test_tag_and_tag(self):
        """Bool tags can be ANDed with & operator."""
        A = Bool("A")
        B = Bool("B")
        cond = A & B

        state = SystemState().with_tags({"A": True, "B": True})
        assert evaluate_condition(cond, state) is True

        state = SystemState().with_tags({"A": True, "B": False})
        assert evaluate_condition(cond, state) is False

    def test_condition_and_condition(self):
        """Comparison conditions can be ANDed with & operator."""
        Step = Int("Step")
        Mode = Int("Mode")
        cond = (Step == 1) & (Mode == 2)

        state = SystemState().with_tags({"Step": 1, "Mode": 2})
        assert evaluate_condition(cond, state) is True

        state = SystemState().with_tags({"Step": 1, "Mode": 3})
        assert evaluate_condition(cond, state) is False

    def test_tag_and_condition(self):
        """Bool tags and comparison conditions can be mixed with &."""
        Enable = Bool("Enable")
        Step = Int("Step")
        cond = Enable & (Step == 1)

        state = SystemState().with_tags({"Enable": True, "Step": 1})
        assert evaluate_condition(cond, state) is True

        state = SystemState().with_tags({"Enable": False, "Step": 1})
        assert evaluate_condition(cond, state) is False

    def test_chained_and(self):
        """Multiple & operators chain correctly."""
        A = Bool("A")
        B = Bool("B")
        C = Bool("C")
        cond = A & B & C

        state = SystemState().with_tags({"A": True, "B": True, "C": True})
        assert evaluate_condition(cond, state) is True

        state = SystemState().with_tags({"A": True, "B": False, "C": True})
        assert evaluate_condition(cond, state) is False
