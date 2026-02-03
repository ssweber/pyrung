"""Tests for MemoryBank, MemoryBlock, and IndirectTag.

Milestone 6: MemoryBank & Pointer Addressing
"""

import pytest

from pyrung.core import (
    Bool,
    Char,
    Dint,
    IndirectTag,
    Int,
    MemoryBank,
    MemoryBlock,
    Program,
    Real,
    Rung,
    SystemState,
    Tag,
    TagType,
    copy,
    out,
)
from tests.conftest import evaluate_condition, evaluate_program

# =============================================================================
# TagType Renaming Tests
# =============================================================================


class TestTagTypeRenaming:
    """Test IEC 61131-3 naming for TagType."""

    def test_new_names_exist(self):
        """New IEC 61131-3 names should exist."""
        assert TagType.BOOL.value == "bool"
        assert TagType.INT.value == "int"
        assert TagType.DINT.value == "dint"
        assert TagType.REAL.value == "real"
        assert TagType.WORD.value == "word"
        assert TagType.CHAR.value == "char"

    def test_deprecated_aliases_work(self):
        """Deprecated aliases should resolve to new names."""
        assert TagType("bit") == TagType.BOOL
        assert TagType("int2") == TagType.DINT
        assert TagType("float") == TagType.REAL
        assert TagType("hex") == TagType.WORD
        assert TagType("txt") == TagType.CHAR

    def test_new_helper_functions(self):
        """New helper functions create correct types."""
        assert Bool("x").type == TagType.BOOL
        assert Int("x").type == TagType.INT
        assert Dint("x").type == TagType.DINT
        assert Real("x").type == TagType.REAL
        assert Char("x").type == TagType.CHAR

    def test_deprecated_helper_functions(self):
        """Deprecated helper functions still work."""
        from pyrung.core.tag import Bit, Float, Int2, Txt

        assert Bit("x").type == TagType.BOOL
        assert Int2("x").type == TagType.DINT
        assert Float("x").type == TagType.REAL
        assert Txt("x").type == TagType.CHAR

    def test_default_values(self):
        """Default values are set correctly for each type."""
        assert Bool("x").default is False
        assert Int("x").default == 0
        assert Dint("x").default == 0
        assert Real("x").default == 0.0
        assert Tag("x", TagType.WORD).default == 0
        assert Char("x").default == ""


# =============================================================================
# MemoryBank Tests
# =============================================================================


class TestMemoryBank:
    """Test MemoryBank factory class."""

    def test_single_tag_access(self):
        """MemoryBank[addr] returns a Tag with correct properties."""
        DS = MemoryBank("DS", TagType.INT, range(1, 4501), retentive=True)
        tag = DS[100]

        assert isinstance(tag, Tag)
        assert tag.name == "DS100"
        assert tag.type == TagType.INT
        assert tag.retentive is True

    def test_tag_caching(self):
        """Same address returns same Tag instance."""
        DS = MemoryBank("DS", TagType.INT, range(1, 4501))
        tag1 = DS[100]
        tag2 = DS[100]

        assert tag1 is tag2

    def test_different_addresses_different_tags(self):
        """Different addresses return different Tag instances."""
        DS = MemoryBank("DS", TagType.INT, range(1, 4501))
        tag1 = DS[100]
        tag2 = DS[101]

        assert tag1 is not tag2
        assert tag1.name == "DS100"
        assert tag2.name == "DS101"

    def test_range_validation_low(self):
        """Address below range raises ValueError."""
        DS = MemoryBank("DS", TagType.INT, range(1, 4501))

        with pytest.raises(ValueError, match="out of range"):
            DS[0]

    def test_range_validation_high(self):
        """Address above range raises ValueError."""
        DS = MemoryBank("DS", TagType.INT, range(1, 4501))

        with pytest.raises(ValueError, match="out of range"):
            DS[4501]

    def test_range_boundaries(self):
        """Boundary addresses work correctly."""
        DS = MemoryBank("DS", TagType.INT, range(1, 4501))

        # Valid boundaries
        tag_low = DS[1]
        tag_high = DS[4500]

        assert tag_low.name == "DS1"
        assert tag_high.name == "DS4500"

    def test_different_bank_types(self):
        """Different banks have correct types."""
        DS = MemoryBank("DS", TagType.INT, range(1, 4501))
        DD = MemoryBank("DD", TagType.DINT, range(1, 1001))
        C = MemoryBank("C", TagType.BOOL, range(1, 2001))
        DF = MemoryBank("DF", TagType.REAL, range(1, 501))

        assert DS[1].type == TagType.INT
        assert DD[1].type == TagType.DINT
        assert C[1].type == TagType.BOOL
        assert DF[1].type == TagType.REAL

    def test_retentive_default(self):
        """Non-retentive bank creates non-retentive tags."""
        C = MemoryBank("C", TagType.BOOL, range(1, 2001), retentive=False)
        assert C[1].retentive is False

    def test_retentive_explicit(self):
        """Retentive bank creates retentive tags."""
        DS = MemoryBank("DS", TagType.INT, range(1, 4501), retentive=True)
        assert DS[1].retentive is True


# =============================================================================
# MemoryBlock Tests
# =============================================================================


class TestMemoryBlock:
    """Test MemoryBlock for block operations."""

    def test_slice_creates_block(self):
        """MemoryBank[start:stop] creates MemoryBlock."""
        DS = MemoryBank("DS", TagType.INT, range(1, 4501))
        block = DS[100:110]

        assert isinstance(block, MemoryBlock)
        assert block.start == 100
        assert block.length == 10

    def test_block_length(self):
        """len(block) returns correct length."""
        DS = MemoryBank("DS", TagType.INT, range(1, 4501))
        block = DS[100:110]

        assert len(block) == 10

    def test_block_addresses(self):
        """block.addresses returns correct range."""
        DS = MemoryBank("DS", TagType.INT, range(1, 4501))
        block = DS[100:110]

        assert block.addresses == range(100, 110)

    def test_block_tags(self):
        """block.tags() returns list of Tags."""
        DS = MemoryBank("DS", TagType.INT, range(1, 4501))
        block = DS[100:103]
        tags = block.tags()

        assert len(tags) == 3
        assert tags[0].name == "DS100"
        assert tags[1].name == "DS101"
        assert tags[2].name == "DS102"

    def test_block_iteration(self):
        """Iterating over block yields Tags."""
        DS = MemoryBank("DS", TagType.INT, range(1, 4501))
        block = DS[100:103]

        tags = list(block)
        assert len(tags) == 3
        assert all(isinstance(t, Tag) for t in tags)

    def test_block_range_validation(self):
        """Block with out-of-range addresses raises ValueError."""
        DS = MemoryBank("DS", TagType.INT, range(1, 4501))

        with pytest.raises(ValueError, match="out of range"):
            DS[4500:4502]  # 4501 is out of range

    def test_block_repr(self):
        """Block has useful repr."""
        DS = MemoryBank("DS", TagType.INT, range(1, 4501))
        block = DS[100:110]

        assert "DS" in repr(block)
        assert "100" in repr(block)


# =============================================================================
# IndirectTag Tests
# =============================================================================


class TestIndirectTag:
    """Test IndirectTag for pointer addressing."""

    def test_tag_key_creates_indirect(self):
        """MemoryBank[Tag] creates IndirectTag."""
        DS = MemoryBank("DS", TagType.INT, range(1, 4501))
        Index = Int("Index")
        indirect = DS[Index]

        assert isinstance(indirect, IndirectTag)
        assert indirect.bank is DS
        assert indirect.pointer is Index

    def test_resolve_basic(self):
        """IndirectTag.resolve() returns correct Tag."""
        DS = MemoryBank("DS", TagType.INT, range(1, 4501))
        Index = Int("Index")
        indirect = DS[Index]

        state = SystemState().with_tags({"Index": 100})
        resolved = indirect.resolve(state)

        assert resolved.name == "DS100"

    def test_resolve_default_pointer(self):
        """resolve() uses pointer default when not in state."""
        DS = MemoryBank("DS", TagType.INT, range(1, 4501))
        Index = Tag("Index", TagType.INT, default=50)
        indirect = DS[Index]

        state = SystemState()  # No Index in state
        resolved = indirect.resolve(state)

        assert resolved.name == "DS50"

    def test_resolve_out_of_range(self):
        """resolve() raises ValueError for out-of-range pointer."""
        DS = MemoryBank("DS", TagType.INT, range(1, 4501))
        Index = Int("Index")
        indirect = DS[Index]

        state = SystemState().with_tags({"Index": 5000})  # Out of range

        with pytest.raises(ValueError, match="out of range"):
            indirect.resolve(state)

    def test_indirect_repr(self):
        """IndirectTag has useful repr."""
        DS = MemoryBank("DS", TagType.INT, range(1, 4501))
        Index = Int("Index")
        indirect = DS[Index]

        assert "DS" in repr(indirect)
        assert "Index" in repr(indirect)


# =============================================================================
# Indirect Condition Tests
# =============================================================================


class TestIndirectConditions:
    """Test comparison operators on IndirectTag."""

    def test_indirect_eq(self):
        """IndirectTag == value creates condition."""
        DS = MemoryBank("DS", TagType.INT, range(1, 4501))
        Index = Int("Index")
        indirect = DS[Index]

        cond = indirect == 100
        assert hasattr(cond, "evaluate")

        # Test evaluation
        state = SystemState().with_tags({"Index": 50, "DS50": 100})
        assert evaluate_condition(cond, state) is True

        state = SystemState().with_tags({"Index": 50, "DS50": 99})
        assert evaluate_condition(cond, state) is False

    def test_indirect_ne(self):
        """IndirectTag != value creates condition."""
        DS = MemoryBank("DS", TagType.INT, range(1, 4501))
        Index = Int("Index")
        indirect = DS[Index]

        cond = indirect != 100
        state = SystemState().with_tags({"Index": 50, "DS50": 99})
        assert evaluate_condition(cond, state) is True

    def test_indirect_lt(self):
        """IndirectTag < value creates condition."""
        DS = MemoryBank("DS", TagType.INT, range(1, 4501))
        Index = Int("Index")
        indirect = DS[Index]

        cond = indirect < 100
        state = SystemState().with_tags({"Index": 50, "DS50": 50})
        assert evaluate_condition(cond, state) is True

        state = SystemState().with_tags({"Index": 50, "DS50": 100})
        assert evaluate_condition(cond, state) is False

    def test_indirect_le(self):
        """IndirectTag <= value creates condition."""
        DS = MemoryBank("DS", TagType.INT, range(1, 4501))
        Index = Int("Index")
        indirect = DS[Index]

        cond = indirect <= 100
        state = SystemState().with_tags({"Index": 50, "DS50": 100})
        assert evaluate_condition(cond, state) is True

        state = SystemState().with_tags({"Index": 50, "DS50": 101})
        assert evaluate_condition(cond, state) is False

    def test_indirect_gt(self):
        """IndirectTag > value creates condition."""
        DS = MemoryBank("DS", TagType.INT, range(1, 4501))
        Index = Int("Index")
        indirect = DS[Index]

        cond = indirect > 100
        state = SystemState().with_tags({"Index": 50, "DS50": 101})
        assert evaluate_condition(cond, state) is True

        state = SystemState().with_tags({"Index": 50, "DS50": 100})
        assert evaluate_condition(cond, state) is False

    def test_indirect_ge(self):
        """IndirectTag >= value creates condition."""
        DS = MemoryBank("DS", TagType.INT, range(1, 4501))
        Index = Int("Index")
        indirect = DS[Index]

        cond = indirect >= 100
        state = SystemState().with_tags({"Index": 50, "DS50": 100})
        assert evaluate_condition(cond, state) is True

        state = SystemState().with_tags({"Index": 50, "DS50": 99})
        assert evaluate_condition(cond, state) is False


# =============================================================================
# Indirect Instruction Tests
# =============================================================================


class TestIndirectInstructions:
    """Test instructions with IndirectTag."""

    def test_copy_indirect_source(self):
        """copy(DD[Index], Result) copies from indirect source."""
        DD = MemoryBank("DD", TagType.DINT, range(1, 1001))
        Index = Int("Index")
        Result = Dint("Result")

        with Program() as logic:
            with Rung():
                copy(DD[Index], Result)

        state = SystemState().with_tags({"Index": 100, "DD100": 42})
        state = evaluate_program(logic, state)

        assert state.tags["Result"] == 42

    def test_copy_indirect_target(self):
        """copy(Value, DD[Index]) copies to indirect target."""
        DD = MemoryBank("DD", TagType.DINT, range(1, 1001))
        Index = Int("Index")
        Value = Dint("Value")

        with Program() as logic:
            with Rung():
                copy(Value, DD[Index])

        state = SystemState().with_tags({"Index": 100, "Value": 42})
        state = evaluate_program(logic, state)

        assert state.tags["DD100"] == 42

    def test_copy_indirect_both(self):
        """copy(DD[Src], DD[Dst]) copies between indirect tags."""
        DD = MemoryBank("DD", TagType.DINT, range(1, 1001))
        Src = Int("Src")
        Dst = Int("Dst")

        with Program() as logic:
            with Rung():
                copy(DD[Src], DD[Dst])

        state = SystemState().with_tags({"Src": 100, "Dst": 200, "DD100": 42})
        state = evaluate_program(logic, state)

        assert state.tags["DD200"] == 42

    def test_copy_literal_to_indirect(self):
        """copy(literal, DD[Index]) copies literal to indirect target."""
        DD = MemoryBank("DD", TagType.DINT, range(1, 1001))
        Index = Int("Index")

        with Program() as logic:
            with Rung():
                copy(999, DD[Index])

        state = SystemState().with_tags({"Index": 50})
        state = evaluate_program(logic, state)

        assert state.tags["DD50"] == 999


# =============================================================================
# Integration Tests
# =============================================================================


class TestIntegration:
    """Integration tests for complete workflows."""

    def test_indirect_condition_in_rung(self):
        """Rung(DD[Index] > 100) evaluates indirect condition."""
        DD = MemoryBank("DD", TagType.DINT, range(1, 1001))
        Index = Int("Index")
        Flag = Bool("Flag")

        with Program() as logic:
            with Rung(DD[Index] > 100):
                out(Flag)

        # Condition true
        state = SystemState().with_tags({"Index": 50, "DD50": 150})
        state = evaluate_program(logic, state)
        assert state.tags["Flag"] is True

        # Condition false
        state = SystemState().with_tags({"Index": 50, "DD50": 50})
        state = evaluate_program(logic, state)
        assert state.tags["Flag"] is False

    def test_pointer_change_between_scans(self):
        """Pointer can change between scans affecting resolved address."""
        DD = MemoryBank("DD", TagType.DINT, range(1, 1001))
        Index = Int("Index")
        Result = Dint("Result")

        with Program() as logic:
            with Rung():
                copy(DD[Index], Result)

        # First scan: Index=100
        state = SystemState().with_tags({"Index": 100, "DD100": 111, "DD200": 222})
        state = evaluate_program(logic, state)
        assert state.tags["Result"] == 111

        # Second scan: Index changed to 200
        state = state.with_tags({"Index": 200})
        state = evaluate_program(logic, state)
        assert state.tags["Result"] == 222

    def test_array_iteration_pattern(self):
        """Indirect addressing enables array-like iteration."""
        DS = MemoryBank("DS", TagType.INT, range(1, 4501))
        Index = Int("Index")
        Sum = Int("Sum")

        # Manually increment index and accumulate
        with Program() as logic:
            with Rung():
                # In real use, you'd have counter logic here
                # For test, we just verify indirect access works
                copy(DS[Index], Sum)

        # Access different array elements by changing Index
        state = SystemState().with_tags({"Index": 10, "DS10": 100})
        state = evaluate_program(logic, state)
        assert state.tags["Sum"] == 100

        state = state.with_tags({"Index": 11, "DS11": 200})
        state = evaluate_program(logic, state)
        assert state.tags["Sum"] == 200

    def test_complete_click_style_banks(self):
        """Define Click-style memory banks."""
        # Click PLC memory banks
        DS = MemoryBank("DS", TagType.INT, range(1, 4501), retentive=True)
        DD = MemoryBank("DD", TagType.DINT, range(1, 1001), retentive=True)
        DH = MemoryBank("DH", TagType.WORD, range(1, 501), retentive=True)
        DF = MemoryBank("DF", TagType.REAL, range(1, 501), retentive=True)
        C = MemoryBank("C", TagType.BOOL, range(1, 2001))

        # Verify types
        assert DS[1].type == TagType.INT
        assert DD[1].type == TagType.DINT
        assert DH[1].type == TagType.WORD
        assert DF[1].type == TagType.REAL
        assert C[1].type == TagType.BOOL

        # Verify retentive
        assert DS[1].retentive is True
        assert C[1].retentive is False


# =============================================================================
# Edge Cases
# =============================================================================


class TestEdgeCases:
    """Test edge cases and error handling."""

    def test_invalid_key_type(self):
        """Invalid key type raises TypeError."""
        DS = MemoryBank("DS", TagType.INT, range(1, 4501))

        with pytest.raises(TypeError, match="Invalid key type"):
            DS[3.14]  # float is not a valid key type

    def test_unregistered_nickname_raises_keyerror(self):
        """Accessing unregistered nickname raises KeyError."""
        DS = MemoryBank("DS", TagType.INT, range(1, 4501))

        with pytest.raises(KeyError, match="Nickname 'invalid' not registered"):
            DS["invalid"]

    def test_indirect_hash(self):
        """IndirectTag is hashable."""
        DS = MemoryBank("DS", TagType.INT, range(1, 4501))
        Index = Int("Index")
        indirect = DS[Index]

        # Should not raise
        hash(indirect)

        # Can be used in sets
        s = {indirect}
        assert indirect in s

    def test_memory_block_empty_slice(self):
        """Empty slice creates zero-length block."""
        DS = MemoryBank("DS", TagType.INT, range(1, 4501))
        block = DS[100:100]

        assert len(block) == 0
        assert list(block) == []

    def test_indirect_equality_same(self):
        """Two IndirectTags with same bank and pointer are equal."""
        DS = MemoryBank("DS", TagType.INT, range(1, 4501))
        Index = Int("Index")

        indirect1 = DS[Index]
        indirect2 = DS[Index]

        # Note: == returns IndirectCompareEq condition, not bool
        # For actual equality comparison, we check attributes
        assert indirect1.bank == indirect2.bank
        assert indirect1.pointer == indirect2.pointer

    def test_bank_repr(self):
        """MemoryBank has useful repr."""
        DS = MemoryBank("DS", TagType.INT, range(1, 4501))
        r = repr(DS)

        assert "DS" in r
        assert "INT" in r
        assert "1" in r
        assert "4501" in r


# =============================================================================
# Register and Nickname Tests
# =============================================================================


class TestRegister:
    """Test tag registration with nicknames and configuration."""

    def test_register_basic(self):
        """Register creates a tag with nickname."""
        DS = MemoryBank("DS", TagType.INT, range(1, 4501))
        tag = DS.register("Motor1Speed", 500)

        assert tag.name == "DS500"
        assert DS.nicknames["Motor1Speed"] == 500

    def test_register_with_initial_value(self):
        """Register with initial_value sets tag default."""
        DS = MemoryBank("DS", TagType.INT, range(1, 4501))
        tag = DS.register("Setpoint", 100, initial_value=1500)

        assert tag.default == 1500
        assert DS.initial_values[100] == 1500

    def test_register_retentive_override(self):
        """Register can override bank's retentive default."""
        # Bank defaults to non-retentive
        DS = MemoryBank("DS", TagType.INT, range(1, 4501), retentive=False)
        tag = DS.register("Counter", 200, retentive=True)

        assert tag.retentive is True
        assert 200 in DS.retentive_exceptions

    def test_register_retentive_matches_default(self):
        """Register with retentive matching default doesn't add to exceptions."""
        DS = MemoryBank("DS", TagType.INT, range(1, 4501), retentive=False)
        tag = DS.register("Counter", 200, retentive=False)

        assert tag.retentive is False
        assert 200 not in DS.retentive_exceptions

    def test_register_retentive_ignores_initial_value(self):
        """Retentive tags don't store initial_value."""
        DS = MemoryBank("DS", TagType.INT, range(1, 4501))

        # Suppress expected warning for this test
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            DS.register("Counter", 200, initial_value=100, retentive=True)

        # initial_value should not be stored for retentive tags
        assert 200 not in DS.initial_values

    def test_register_retentive_warns_on_meaningful_initial_value(self):
        """Warning issued when retentive tag has meaningful initial_value."""
        DS = MemoryBank("DS", TagType.INT, range(1, 4501))

        with pytest.warns(UserWarning, match="retentive.*initial_value"):
            DS.register("Counter", 200, initial_value=100, retentive=True)

    def test_register_retentive_no_warn_on_zero_initial_value(self):
        """No warning when retentive tag has initial_value=0."""
        DS = MemoryBank("DS", TagType.INT, range(1, 4501))

        # Should not warn
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("error")
            DS.register("Counter", 200, initial_value=0, retentive=True)

    def test_register_retentive_no_warn_on_empty_string_initial_value(self):
        """No warning when retentive tag has initial_value=''."""
        DS = MemoryBank("DS", TagType.CHAR, range(1, 4501))

        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("error")
            DS.register("Text", 200, initial_value="", retentive=True)

    def test_register_out_of_range(self):
        """Register with out-of-range address raises ValueError."""
        DS = MemoryBank("DS", TagType.INT, range(1, 4501))

        with pytest.raises(ValueError, match="out of range"):
            DS.register("Invalid", 5000)

    def test_register_re_registration_clears_cache(self):
        """Re-registering an address clears the cached tag."""
        DS = MemoryBank("DS", TagType.INT, range(1, 4501))

        # First registration
        tag1 = DS.register("OldName", 100, initial_value=10)
        assert tag1.default == 10

        # Re-register with different config
        tag2 = DS.register("NewName", 100, initial_value=20)
        assert tag2.default == 20
        assert tag1 is not tag2


class TestNicknameAccess:
    """Test accessing tags by nickname."""

    def test_getitem_string_access(self):
        """Access tag by nickname via __getitem__."""
        DS = MemoryBank("DS", TagType.INT, range(1, 4501))
        DS.register("Motor1Speed", 500)

        tag = DS["Motor1Speed"]
        assert tag.name == "DS500"

    def test_getitem_string_with_spaces(self):
        """Access tag with spaces in nickname."""
        DS = MemoryBank("DS", TagType.INT, range(1, 4501))
        DS.register("Motor 1 Speed", 500)

        tag = DS["Motor 1 Speed"]
        assert tag.name == "DS500"

    def test_getattr_access(self):
        """Access tag by nickname via attribute."""
        DS = MemoryBank("DS", TagType.INT, range(1, 4501))
        DS.register("Motor1Speed", 500)

        tag = DS.Motor1Speed
        assert tag.name == "DS500"

    def test_getattr_unregistered_raises_attributeerror(self):
        """Accessing unregistered nickname via attr raises AttributeError."""
        DS = MemoryBank("DS", TagType.INT, range(1, 4501))

        with pytest.raises(AttributeError, match="no nickname 'Unknown'"):
            _ = DS.Unknown

    def test_getattr_internal_attributes_still_work(self):
        """Internal attributes like 'name' still work."""
        DS = MemoryBank("DS", TagType.INT, range(1, 4501))

        assert DS.name == "DS"
        assert DS.tag_type == TagType.INT
        assert DS.retentive is False


class TestRetentiveExceptions:
    """Test retentive_exceptions behavior."""

    def test_bank_default_retentive_false(self):
        """Non-retentive bank with retentive exceptions."""
        DS = MemoryBank("DS", TagType.INT, range(1, 4501), retentive=False)
        DS.retentive_exceptions.add(100)

        tag_normal = DS[50]
        tag_exception = DS[100]

        assert tag_normal.retentive is False
        assert tag_exception.retentive is True

    def test_bank_default_retentive_true(self):
        """Retentive bank with non-retentive exceptions."""
        DS = MemoryBank("DS", TagType.INT, range(1, 4501), retentive=True)
        DS.retentive_exceptions.add(100)

        tag_normal = DS[50]
        tag_exception = DS[100]

        assert tag_normal.retentive is True
        assert tag_exception.retentive is False

    def test_initial_value_only_for_non_retentive(self):
        """initial_values only apply to non-retentive tags."""
        DS = MemoryBank("DS", TagType.INT, range(1, 4501), retentive=True)
        DS.initial_values[100] = 999
        DS.retentive_exceptions.add(100)  # Make 100 non-retentive

        tag_retentive = DS[50]  # Retentive, no initial_value
        tag_non_retentive = DS[100]  # Non-retentive, has initial_value

        assert tag_retentive.default == 0  # Type default
        assert tag_non_retentive.default == 999
