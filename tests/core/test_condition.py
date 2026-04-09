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

    def test_eq_with_missing_tag_uses_default(self):
        """Missing tag uses tag default, so Int('Step') == 0 is True."""
        Step = Int("Step")
        cond = Step == 0

        state = SystemState()  # No "Step" tag

        assert evaluate_condition(cond, state) is True  # default 0 == 0


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


class TestOr:
    """Test Or() composite condition (OR logic)."""

    def test_or_true_when_first_true(self):
        """Or is true when first condition is true."""
        from pyrung.core import Or

        Start = Bool("Start")
        CmdStart = Bool("CmdStart")
        cond = Or(Start, CmdStart)

        state = SystemState().with_tags({"Start": True, "CmdStart": False})

        assert evaluate_condition(cond, state) is True

    def test_or_true_when_second_true(self):
        """Or is true when second condition is true."""
        from pyrung.core import Or

        Start = Bool("Start")
        CmdStart = Bool("CmdStart")
        cond = Or(Start, CmdStart)

        state = SystemState().with_tags({"Start": False, "CmdStart": True})

        assert evaluate_condition(cond, state) is True

    def test_or_true_when_both_true(self):
        """Or is true when both conditions are true."""
        from pyrung.core import Or

        Start = Bool("Start")
        CmdStart = Bool("CmdStart")
        cond = Or(Start, CmdStart)

        state = SystemState().with_tags({"Start": True, "CmdStart": True})

        assert evaluate_condition(cond, state) is True

    def test_or_false_when_all_false(self):
        """Or is false when all conditions are false."""
        from pyrung.core import Or

        Start = Bool("Start")
        CmdStart = Bool("CmdStart")
        cond = Or(Start, CmdStart)

        state = SystemState().with_tags({"Start": False, "CmdStart": False})

        assert evaluate_condition(cond, state) is False

    def test_or_with_comparisons(self):
        """Or works with comparison conditions."""
        from pyrung.core import Or

        Step = Int("Step")
        Mode = Int("Mode")
        cond = Or(Step == 0, Mode == 1)

        # Step is 0, Mode is not 1
        state = SystemState().with_tags({"Step": 0, "Mode": 0})
        assert evaluate_condition(cond, state) is True

        # Step is not 0, Mode is 1
        state = SystemState().with_tags({"Step": 5, "Mode": 1})
        assert evaluate_condition(cond, state) is True

        # Neither matches
        state = SystemState().with_tags({"Step": 5, "Mode": 0})
        assert evaluate_condition(cond, state) is False

    def test_or_with_three_conditions(self):
        """Or works with more than two conditions."""
        from pyrung.core import Or

        A = Bool("A")
        B = Bool("B")
        C = Bool("C")
        cond = Or(A, B, C)

        # Only C is true
        state = SystemState().with_tags({"A": False, "B": False, "C": True})
        assert evaluate_condition(cond, state) is True

        # All false
        state = SystemState().with_tags({"A": False, "B": False, "C": False})
        assert evaluate_condition(cond, state) is False

    def test_or_with_int_truthiness(self):
        """Or treats INT tags as truthy when nonzero."""
        from pyrung.core import Or

        Step = Int("Step")
        Start = Bool("Start")
        cond = Or(Step, Start)

        state = SystemState().with_tags({"Step": 2, "Start": False})
        assert evaluate_condition(cond, state) is True

        state = SystemState().with_tags({"Step": 0, "Start": False})
        assert evaluate_condition(cond, state) is False

    def test_or_rejects_dint_direct_tag(self):
        """Direct non-INT numeric tags remain invalid in grouped helpers."""
        import pytest

        from pyrung.core import Dint, Or

        Step32 = Dint("Step32")
        Start = Bool("Start")

        with pytest.raises(TypeError, match="BOOL and INT"):
            Or(Step32, Start)


class TestOperatorRemovalErrors:
    """Test that | and & operators raise helpful migration errors for conditions."""

    def test_bool_or_bool_raises(self):
        """Bool | Bool raises TypeError directing to Or()."""
        import pytest

        A = Bool("A")
        B = Bool("B")

        with pytest.raises(TypeError, match="Use Or"):
            _ = A | B

    def test_bool_and_bool_raises(self):
        """Bool & Bool raises TypeError directing to And()."""
        import pytest

        A = Bool("A")
        B = Bool("B")

        with pytest.raises(TypeError, match="Use And"):
            _ = A & B

    def test_condition_or_tag_raises(self):
        """Condition | Tag raises TypeError directing to Or()."""
        import pytest

        Step = Int("Step")
        Start = Bool("Start")

        with pytest.raises(TypeError, match="Use Or"):
            _ = (Step == 0) | Start

    def test_condition_and_tag_raises(self):
        """Condition & Tag raises TypeError directing to And()."""
        import pytest

        Step = Int("Step")
        Start = Bool("Start")

        with pytest.raises(TypeError, match="Use And"):
            _ = (Step == 0) & Start

    def test_bool_or_condition_raises(self):
        """Tag | Condition raises TypeError directing to Or()."""
        import pytest

        Start = Bool("Start")
        Step = Int("Step")

        with pytest.raises(TypeError, match="Use Or"):
            _ = Start | (Step == 0)

    def test_bool_and_condition_raises(self):
        """Tag & Condition raises TypeError directing to And()."""
        import pytest

        Start = Bool("Start")
        Step = Int("Step")

        with pytest.raises(TypeError, match="Use And"):
            _ = Start & (Step == 0)

    def test_int_or_bool_precedence_error(self):
        """0 | BoolTag raises TypeError (precedence mistake)."""
        import pytest

        Start = Bool("Start")

        with pytest.raises(TypeError, match="precedence"):
            _ = 0 | Start

    def test_int_and_bool_precedence_error(self):
        """0 & BoolTag raises helpful precedence error."""
        import pytest

        Start = Bool("Start")

        with pytest.raises(TypeError, match="precedence"):
            _ = 0 & Start

    def test_condition_eq_int_raises_error(self):
        """Condition == 0 raises helpful error."""
        import pytest

        from pyrung.core import Or

        A = Bool("A")
        B = Bool("B")
        cond = Or(A, B)

        with pytest.raises(TypeError, match="Or\\(\\) or And\\(\\)"):
            _ = cond == 0

    def test_condition_eq_condition_works(self):
        """Condition == Condition uses identity comparison."""
        from pyrung.core import Or

        A = Bool("A")
        B = Bool("B")
        cond1 = Or(A, B)
        cond2 = Or(A, B)

        assert (cond1 == cond1) is True
        assert (cond1 == cond2) is False


class TestAnd:
    """Test And() composite condition (AND logic)."""

    def test_and_true_when_all_true(self):
        """And is true when all conditions are true."""
        from pyrung.core import And

        Ready = Bool("Ready")
        Auto = Bool("Auto")
        cond = And(Ready, Auto)

        state = SystemState().with_tags({"Ready": True, "Auto": True})
        assert evaluate_condition(cond, state) is True

    def test_and_false_when_any_false(self):
        """And is false when any condition is false."""
        from pyrung.core import And

        Ready = Bool("Ready")
        Auto = Bool("Auto")
        cond = And(Ready, Auto)

        state = SystemState().with_tags({"Ready": True, "Auto": False})
        assert evaluate_condition(cond, state) is False

    def test_and_with_comparisons(self):
        """And works with comparison conditions."""
        from pyrung.core import And

        Step = Int("Step")
        Mode = Int("Mode")
        cond = And(Step == 1, Mode == 2)

        state = SystemState().with_tags({"Step": 1, "Mode": 2})
        assert evaluate_condition(cond, state) is True

        state = SystemState().with_tags({"Step": 1, "Mode": 1})
        assert evaluate_condition(cond, state) is False

    def test_and_with_int_truthiness(self):
        """And treats INT tags as truthy when nonzero."""
        from pyrung.core import And

        Step = Int("Step")
        Ready = Bool("Ready")
        cond = And(Step, Ready)

        state = SystemState().with_tags({"Step": 1, "Ready": True})
        assert evaluate_condition(cond, state) is True

        state = SystemState().with_tags({"Step": 0, "Ready": True})
        assert evaluate_condition(cond, state) is False


class TestGroupedOr:
    """Test explicit grouped AND terms inside Or()."""

    def test_or_with_explicit_and_group(self):
        """Grouped AND terms require explicit And()."""
        from pyrung.core import And, Or

        Start = Bool("Start")
        Ready = Bool("Ready")
        Auto = Bool("Auto")
        Remote = Bool("Remote")
        cond = Or(Start, And(Ready, Auto), Remote)

        state = SystemState().with_tags(
            {"Start": False, "Ready": True, "Auto": True, "Remote": False}
        )
        assert evaluate_condition(cond, state) is True

        state = SystemState().with_tags(
            {"Start": False, "Ready": True, "Auto": False, "Remote": False}
        )
        assert evaluate_condition(cond, state) is False

    def test_or_rejects_tuple_group(self):
        """Tuple groups must be written explicitly with And()."""
        import pytest

        from pyrung.core import Or

        A = Bool("A")
        B = Bool("B")
        C = Bool("C")

        with pytest.raises(TypeError, match="And\\(\\.\\.\\.\\)"):
            Or((A, B), C)  # ty: ignore[invalid-argument-type]

    def test_or_rejects_list_group(self):
        """List groups must be written explicitly with And()."""
        import pytest

        from pyrung.core import Or

        A = Bool("A")
        B = Bool("B")
        C = Bool("C")

        with pytest.raises(TypeError, match="And\\(\\.\\.\\.\\)"):
            Or([A, B], C)  # ty: ignore[invalid-argument-type]
