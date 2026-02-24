"""Tests for Block, BlockRange, IndirectRef, InputBlock, OutputBlock.

Aligned to types.md spec: inclusive bounds, .select(), no Click-specific features.
"""

import pytest

from pyrung.core import (
    Block,
    BlockRange,
    Bool,
    Dint,
    ImmediateRef,
    IndirectRef,
    InputBlock,
    InputTag,
    Int,
    OutputBlock,
    OutputTag,
    Program,
    Rung,
    SystemState,
    Tag,
    TagType,
    copy,
    out,
)
from pyrung.core.memory_block import IndirectBlockRange
from tests.conftest import evaluate_condition, evaluate_program

# =============================================================================
# Block Tests (replaces MemoryBank)
# =============================================================================


class TestBlock:
    """Test Block factory class."""

    def test_single_tag_access(self):
        """Block[addr] returns a Tag with correct properties."""
        DS = Block("DS", TagType.INT, 1, 4500, retentive=True)
        tag = DS[100]

        assert isinstance(tag, Tag)
        assert tag.name == "DS100"
        assert tag.type == TagType.INT
        assert tag.retentive is True

    def test_tag_caching(self):
        """Same address returns same Tag instance."""
        DS = Block("DS", TagType.INT, 1, 4500)
        tag1 = DS[100]
        tag2 = DS[100]

        assert tag1 is tag2

    def test_different_addresses_different_tags(self):
        """Different addresses return different Tag instances."""
        DS = Block("DS", TagType.INT, 1, 4500)
        tag1 = DS[100]
        tag2 = DS[101]

        assert tag1 is not tag2
        assert tag1.name == "DS100"
        assert tag2.name == "DS101"

    def test_zero_index_out_of_range_when_start_is_1(self):
        """Address 0 is out of range when block starts at 1."""
        DS = Block("DS", TagType.INT, 1, 4500)

        with pytest.raises(IndexError, match="out of range"):
            DS[0]

    def test_range_validation_low(self):
        """Address below range raises IndexError."""
        DS = Block("DS", TagType.INT, 2, 4500)

        with pytest.raises(IndexError, match="out of range"):
            DS[1]

    def test_range_validation_high(self):
        """Address above range raises IndexError."""
        DS = Block("DS", TagType.INT, 1, 4500)

        with pytest.raises(IndexError, match="out of range"):
            DS[4501]

    def test_range_boundaries(self):
        """Boundary addresses work correctly (inclusive)."""
        DS = Block("DS", TagType.INT, 1, 4500)

        tag_low = DS[1]
        tag_high = DS[4500]

        assert tag_low.name == "DS1"
        assert tag_high.name == "DS4500"

    def test_different_block_types(self):
        """Different blocks have correct types."""
        DS = Block("DS", TagType.INT, 1, 4500)
        DD = Block("DD", TagType.DINT, 1, 1000)
        C = Block("C", TagType.BOOL, 1, 2000)
        DF = Block("DF", TagType.REAL, 1, 500)

        assert DS[1].type == TagType.INT
        assert DD[1].type == TagType.DINT
        assert C[1].type == TagType.BOOL
        assert DF[1].type == TagType.REAL

    def test_block_identity_hashing(self):
        """Blocks with identical fields are distinct identity keys."""
        block_a = Block("DS", TagType.INT, 1, 100)
        block_b = Block("DS", TagType.INT, 1, 100)

        mapping = {block_a: "A", block_b: "B"}

        assert len(mapping) == 2
        assert mapping[block_a] == "A"
        assert mapping[block_b] == "B"

    def test_retentive_default(self):
        """Non-retentive block creates non-retentive tags."""
        C = Block("C", TagType.BOOL, 1, 2000, retentive=False)
        assert C[1].retentive is False

    def test_retentive_explicit(self):
        """Retentive block creates retentive tags."""
        DS = Block("DS", TagType.INT, 1, 4500, retentive=True)
        assert DS[1].retentive is True

    def test_start_must_be_at_least_0(self):
        """start < 0 raises ValueError."""
        with pytest.raises(ValueError, match="start must be >= 0"):
            Block("DS", TagType.INT, -1, 100)

    def test_zero_start_allows_zero_address(self):
        """Blocks can start at 0 and allow address 0."""
        DS = Block("DS", TagType.INT, 0, 100)
        tag = DS[0]

        assert tag.name == "DS0"
        assert tag.type == TagType.INT

    def test_end_must_be_ge_start(self):
        """end < start raises ValueError."""
        with pytest.raises(ValueError, match="end.*must be >= start"):
            Block("DS", TagType.INT, 10, 5)

    def test_valid_ranges_segment_must_be_ordered(self):
        """Sparse valid_ranges segments must have lo <= hi."""
        with pytest.raises(ValueError, match="lo <= hi"):
            Block("X", TagType.BOOL, 1, 100, valid_ranges=((10, 9),))

    def test_valid_ranges_segment_must_fit_block_window(self):
        """Sparse valid_ranges segments must stay within start/end."""
        with pytest.raises(ValueError, match="must be within"):
            Block("X", TagType.BOOL, 1, 100, valid_ranges=((1, 101),))

    def test_slice_raises_type_error(self):
        """Slice syntax raises TypeError directing to .select()."""
        DS = Block("DS", TagType.INT, 1, 4500)

        with pytest.raises(TypeError, match="Use .select"):
            DS[100:110]

    def test_repr(self):
        """Block has useful repr."""
        DS = Block("DS", TagType.INT, 1, 4500)
        r = repr(DS)

        assert "DS" in r
        assert "INT" in r


# =============================================================================
# Select Tests (replaces slice syntax)
# =============================================================================


class TestSelect:
    """Test Block.select() for creating BlockRange."""

    def test_select_creates_memory_block(self):
        """select(start, end) creates BlockRange with inclusive bounds."""
        DS = Block("DS", TagType.INT, 1, 4500)
        block = DS.select(100, 109)

        assert isinstance(block, BlockRange)
        assert block.start == 100
        assert block.end == 109

    def test_select_length(self):
        """len(block) returns correct length for inclusive bounds."""
        DS = Block("DS", TagType.INT, 1, 4500)
        block = DS.select(100, 109)

        assert len(block) == 10

    def test_select_addresses(self):
        """block.addresses returns correct range (inclusive)."""
        DS = Block("DS", TagType.INT, 1, 4500)
        block = DS.select(100, 109)

        assert block.addresses == range(100, 110)

    def test_select_reverse_addresses(self):
        """block.reverse() iterates the same window in reverse order."""
        DS = Block("DS", TagType.INT, 1, 4500)
        block = DS.select(100, 103).reverse()

        assert tuple(block.addresses) == (103, 102, 101, 100)

    def test_select_reverse_tags(self):
        """block.reverse().tags() follows reversed address order."""
        DS = Block("DS", TagType.INT, 1, 4500)
        tags = DS.select(100, 102).reverse().tags()

        assert [tag.name for tag in tags] == ["DS102", "DS101", "DS100"]

    def test_select_tags(self):
        """block.tags() returns list of Tags."""
        DS = Block("DS", TagType.INT, 1, 4500)
        block = DS.select(100, 102)
        tags = block.tags()

        assert len(tags) == 3
        assert tags[0].name == "DS100"
        assert tags[1].name == "DS101"
        assert tags[2].name == "DS102"

    def test_select_iteration(self):
        """Iterating over block yields Tags."""
        DS = Block("DS", TagType.INT, 1, 4500)
        block = DS.select(100, 102)

        tags = list(block)
        assert len(tags) == 3
        assert all(isinstance(t, Tag) for t in tags)

    def test_select_range_validation(self):
        """select with out-of-range addresses raises IndexError."""
        DS = Block("DS", TagType.INT, 1, 4500)

        with pytest.raises(IndexError, match="out of range"):
            DS.select(4500, 4501)

    def test_select_start_must_be_le_end(self):
        """select(start, end) rejects reversed bounds."""
        DS = Block("DS", TagType.INT, 1, 4500)

        with pytest.raises(ValueError, match="must be <="):
            DS.select(21, 1)

    def test_select_single_address(self):
        """select(n, n) creates single-address block."""
        DS = Block("DS", TagType.INT, 1, 4500)
        block = DS.select(100, 100)

        assert len(block) == 1
        tags = block.tags()
        assert tags[0].name == "DS100"

    def test_select_allows_zero_when_start_is_zero(self):
        """select(0, n) is valid when block start is 0."""
        DS = Block("DS", TagType.INT, 0, 100)
        block = DS.select(0, 2)

        assert tuple(block.addresses) == (0, 1, 2)

    def test_select_repr(self):
        """BlockRange has useful repr."""
        DS = Block("DS", TagType.INT, 1, 4500)
        block = DS.select(100, 109)

        assert "DS" in repr(block)
        assert "100" in repr(block)


class TestIndirectSelect:
    """Test Block.select() with Tag/Expression bounds."""

    def test_select_with_tag_bounds(self):
        """select(Tag, Tag) creates IndirectBlockRange."""
        DS = Block("DS", TagType.INT, 1, 4500)
        start_tag = Int("Start")
        end_tag = Int("End")

        block = DS.select(start_tag, end_tag)
        assert isinstance(block, IndirectBlockRange)

    def test_select_with_mixed_bounds(self):
        """select(int, Tag) creates IndirectBlockRange."""
        DS = Block("DS", TagType.INT, 1, 4500)
        end_tag = Int("End")

        block = DS.select(100, end_tag)
        assert isinstance(block, IndirectBlockRange)

    def test_indirect_memory_block_resolve(self):
        """IndirectBlockRange resolves to BlockRange at scan time."""
        from pyrung.core import ScanContext

        DS = Block("DS", TagType.INT, 1, 4500)
        start_tag = Int("Start")
        end_tag = Int("End")

        indirect_block = DS.select(start_tag, end_tag)
        state = SystemState().with_tags({"Start": 100, "End": 105})
        ctx = ScanContext(state)

        resolved = indirect_block.resolve_ctx(ctx)
        assert isinstance(resolved, BlockRange)
        assert resolved.start == 100
        assert resolved.end == 105
        assert len(resolved) == 6

    def test_indirect_memory_block_reverse_resolve(self):
        """IndirectBlockRange.reverse() preserves reverse ordering on resolve."""
        from pyrung.core import ScanContext

        DS = Block("DS", TagType.INT, 1, 4500)
        start_tag = Int("Start")
        end_tag = Int("End")

        indirect_block = DS.select(start_tag, end_tag).reverse()
        state = SystemState().with_tags({"Start": 100, "End": 102})
        ctx = ScanContext(state)

        resolved = indirect_block.resolve_ctx(ctx)
        assert isinstance(resolved, BlockRange)
        assert [tag.name for tag in resolved.tags()] == ["DS102", "DS101", "DS100"]

    def test_indirect_memory_block_resolve_rejects_reversed_bounds(self):
        """IndirectBlockRange uses block.select() validation for resolved bounds."""
        from pyrung.core import ScanContext

        DS = Block("DS", TagType.INT, 1, 4500)
        start_tag = Int("Start")
        end_tag = Int("End")

        indirect_block = DS.select(start_tag, end_tag)
        state = SystemState().with_tags({"Start": 21, "End": 1})
        ctx = ScanContext(state)

        with pytest.raises(ValueError, match="must be <="):
            indirect_block.resolve_ctx(ctx)


class TestSparseSelect:
    """Test sparse range addressing behavior."""

    def test_sparse_getitem_allows_segment_addresses_and_rejects_gaps(self):
        """Sparse blocks reject holes even when inside min/max bounds."""
        X = Block(
            "X",
            TagType.BOOL,
            1,
            816,
            valid_ranges=((1, 16), (21, 36)),
        )

        assert X[1].name == "X1"
        assert X[16].name == "X16"
        assert X[21].name == "X21"
        with pytest.raises(IndexError):
            X[17]

    def test_sparse_select_filters_to_valid_addresses(self):
        """Sparse select(start, end) returns only valid addresses in that window."""
        X = Block(
            "X",
            TagType.BOOL,
            1,
            816,
            valid_ranges=((1, 16), (21, 36)),
        )

        block = X.select(1, 21)
        expected_addresses = tuple(range(1, 17)) + (21,)

        assert tuple(block.addresses) == expected_addresses
        assert len(block) == len(expected_addresses)
        assert [int(tag.name[1:]) for tag in block.tags()] == list(expected_addresses)
        assert [int(tag.name[1:]) for tag in block] == list(expected_addresses)


class TestAddressFormatter:
    """Test optional address formatting hook for block tags."""

    def test_block_uses_default_formatter(self):
        """Without formatter hook, block uses prefix+address naming."""
        DS = Block("DS", TagType.INT, 1, 4500)
        assert DS[1].name == "DS1"

    def test_block_uses_custom_formatter(self):
        """Custom formatter is used when provided."""
        fmt = lambda name, addr: f"{name}:{addr:03d}"  # noqa: E731
        DS = Block("DS", TagType.INT, 1, 4500, address_formatter=fmt)
        assert DS[1].name == "DS:001"

    def test_input_output_blocks_use_custom_formatter(self):
        """InputBlock/OutputBlock pass formatter through to tag creation."""
        fmt = lambda name, addr: f"{name}{addr:03d}"  # noqa: E731
        X = InputBlock("X", TagType.BOOL, 1, 100, address_formatter=fmt)
        Y = OutputBlock("Y", TagType.BOOL, 1, 100, address_formatter=fmt)

        assert X[1].name == "X001"
        assert Y[1].name == "Y001"


# =============================================================================
# IndirectRef Tests (replaces IndirectTag)
# =============================================================================


class TestIndirectRef:
    """Test IndirectRef for pointer addressing."""

    def test_tag_key_creates_indirect(self):
        """Block[Tag] creates IndirectRef."""
        DS = Block("DS", TagType.INT, 1, 4500)
        Index = Int("Index")
        indirect = DS[Index]

        assert isinstance(indirect, IndirectRef)
        assert indirect.block is DS
        assert indirect.pointer is Index

    def test_resolve_basic(self):
        """IndirectRef.resolve() returns correct Tag."""
        DS = Block("DS", TagType.INT, 1, 4500)
        Index = Int("Index")
        indirect = DS[Index]

        state = SystemState().with_tags({"Index": 100})
        resolved = indirect.resolve(state)

        assert resolved.name == "DS100"

    def test_resolve_default_pointer(self):
        """resolve() uses pointer default when not in state."""
        DS = Block("DS", TagType.INT, 1, 4500)
        Index = Tag("Index", TagType.INT, default=50)
        indirect = DS[Index]

        state = SystemState()
        resolved = indirect.resolve(state)

        assert resolved.name == "DS50"

    def test_resolve_zero_pointer_when_block_starts_at_zero(self):
        """Pointer value 0 resolves when block start is 0."""
        DS = Block("DS", TagType.INT, 0, 100)
        Index = Tag("Index", TagType.INT, default=0)
        indirect = DS[Index]

        resolved = indirect.resolve(SystemState())
        assert resolved.name == "DS0"

    def test_resolve_out_of_range(self):
        """resolve() raises IndexError for out-of-range pointer."""
        DS = Block("DS", TagType.INT, 1, 4500)
        Index = Int("Index")
        indirect = DS[Index]

        state = SystemState().with_tags({"Index": 5000})

        with pytest.raises(IndexError, match="out of range"):
            indirect.resolve(state)

    def test_indirect_repr(self):
        """IndirectRef has useful repr."""
        DS = Block("DS", TagType.INT, 1, 4500)
        Index = Int("Index")
        indirect = DS[Index]

        assert "DS" in repr(indirect)
        assert "Index" in repr(indirect)


# =============================================================================
# Indirect Condition Tests
# =============================================================================


class TestIndirectConditions:
    """Test comparison operators on IndirectRef."""

    def test_indirect_eq(self):
        """IndirectRef == value creates condition."""
        DS = Block("DS", TagType.INT, 1, 4500)
        Index = Int("Index")
        indirect = DS[Index]

        cond = indirect == 100
        assert hasattr(cond, "evaluate")

        state = SystemState().with_tags({"Index": 50, "DS50": 100})
        assert evaluate_condition(cond, state) is True

        state = SystemState().with_tags({"Index": 50, "DS50": 99})
        assert evaluate_condition(cond, state) is False

    def test_indirect_eq_rhs_missing_tag_uses_default(self):
        """IndirectRef == Tag uses tag defaults when rhs tag is missing."""
        DS = Block("DS", TagType.INT, 1, 4500)
        Index = Int("Index")
        Step = Int("Step")
        cond = DS[Index] == Step

        state = SystemState().with_tags({"Index": 1, "DS1": 0})
        assert evaluate_condition(cond, state) is True

    def test_indirect_ne(self):
        """IndirectRef != value creates condition."""
        DS = Block("DS", TagType.INT, 1, 4500)
        Index = Int("Index")
        indirect = DS[Index]

        cond = indirect != 100
        state = SystemState().with_tags({"Index": 50, "DS50": 99})
        assert evaluate_condition(cond, state) is True

    def test_indirect_ne_rhs_missing_tag_uses_default(self):
        """IndirectRef != Tag uses tag defaults when rhs tag is missing."""
        DS = Block("DS", TagType.INT, 1, 4500)
        Index = Int("Index")
        Step = Int("Step")
        cond = DS[Index] != Step

        state = SystemState().with_tags({"Index": 1, "DS1": 0})
        assert evaluate_condition(cond, state) is False

    def test_indirect_lt(self):
        """IndirectRef < value creates condition."""
        DS = Block("DS", TagType.INT, 1, 4500)
        Index = Int("Index")
        indirect = DS[Index]

        cond = indirect < 100
        state = SystemState().with_tags({"Index": 50, "DS50": 50})
        assert evaluate_condition(cond, state) is True

        state = SystemState().with_tags({"Index": 50, "DS50": 100})
        assert evaluate_condition(cond, state) is False

    def test_indirect_le(self):
        """IndirectRef <= value creates condition."""
        DS = Block("DS", TagType.INT, 1, 4500)
        Index = Int("Index")
        indirect = DS[Index]

        cond = indirect <= 100
        state = SystemState().with_tags({"Index": 50, "DS50": 100})
        assert evaluate_condition(cond, state) is True

        state = SystemState().with_tags({"Index": 50, "DS50": 101})
        assert evaluate_condition(cond, state) is False

    def test_indirect_gt(self):
        """IndirectRef > value creates condition."""
        DS = Block("DS", TagType.INT, 1, 4500)
        Index = Int("Index")
        indirect = DS[Index]

        cond = indirect > 100
        state = SystemState().with_tags({"Index": 50, "DS50": 101})
        assert evaluate_condition(cond, state) is True

        state = SystemState().with_tags({"Index": 50, "DS50": 100})
        assert evaluate_condition(cond, state) is False

    def test_indirect_ge(self):
        """IndirectRef >= value creates condition."""
        DS = Block("DS", TagType.INT, 1, 4500)
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
    """Test instructions with IndirectRef."""

    def test_copy_indirect_source(self):
        """copy(DD[Index], Result) copies from indirect source."""
        DD = Block("DD", TagType.DINT, 1, 1000)
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
        DD = Block("DD", TagType.DINT, 1, 1000)
        Index = Int("Index")
        Value = Dint("Value")

        with Program() as logic:
            with Rung():
                copy(Value, DD[Index])

        state = SystemState().with_tags({"Index": 100, "Value": 42})
        state = evaluate_program(logic, state)

        assert state.tags["DD100"] == 42

    def test_copy_indirect_both(self):
        """copy(DD[Src], DD[Dst]) copies between indirect refs."""
        DD = Block("DD", TagType.DINT, 1, 1000)
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
        DD = Block("DD", TagType.DINT, 1, 1000)
        Index = Int("Index")

        with Program() as logic:
            with Rung():
                copy(999, DD[Index])

        state = SystemState().with_tags({"Index": 50})
        state = evaluate_program(logic, state)

        assert state.tags["DD50"] == 999


# =============================================================================
# InputBlock Tests
# =============================================================================


class TestInputBlock:
    """Test InputBlock creates InputTag instances."""

    def test_input_block_creates_input_tags(self):
        """InputBlock[addr] returns InputTag."""
        X = InputBlock("X", TagType.BOOL, 1, 100)
        tag = X[1]

        assert isinstance(tag, InputTag)
        assert tag.name == "X1"
        assert tag.type == TagType.BOOL

    def test_input_block_not_retentive(self):
        """InputBlock tags are always non-retentive."""
        X = InputBlock("X", TagType.BOOL, 1, 100)
        tag = X[1]

        assert tag.retentive is False

    def test_input_tag_has_immediate(self):
        """InputBlock tags have .immediate property."""
        X = InputBlock("X", TagType.BOOL, 1, 100)
        tag = X[1]
        ref = tag.immediate

        assert isinstance(ref, ImmediateRef)
        assert ref.tag is tag

    def test_input_block_is_block(self):
        """InputBlock is a subclass of Block."""
        X = InputBlock("X", TagType.BOOL, 1, 100)
        assert isinstance(X, Block)

    def test_input_block_caching(self):
        """InputBlock caches tags like Block."""
        X = InputBlock("X", TagType.BOOL, 1, 100)
        tag1 = X[1]
        tag2 = X[1]
        assert tag1 is tag2


# =============================================================================
# OutputBlock Tests
# =============================================================================


class TestOutputBlock:
    """Test OutputBlock creates OutputTag instances."""

    def test_output_block_creates_output_tags(self):
        """OutputBlock[addr] returns OutputTag."""
        Y = OutputBlock("Y", TagType.BOOL, 1, 100)
        tag = Y[1]

        assert isinstance(tag, OutputTag)
        assert tag.name == "Y1"
        assert tag.type == TagType.BOOL

    def test_output_block_not_retentive(self):
        """OutputBlock tags are always non-retentive."""
        Y = OutputBlock("Y", TagType.BOOL, 1, 100)
        tag = Y[1]

        assert tag.retentive is False

    def test_output_tag_has_immediate(self):
        """OutputBlock tags have .immediate property."""
        Y = OutputBlock("Y", TagType.BOOL, 1, 100)
        tag = Y[1]
        ref = tag.immediate

        assert isinstance(ref, ImmediateRef)
        assert ref.tag is tag

    def test_output_block_is_block(self):
        """OutputBlock is a subclass of Block."""
        Y = OutputBlock("Y", TagType.BOOL, 1, 100)
        assert isinstance(Y, Block)


# =============================================================================
# Integration Tests
# =============================================================================


class TestIntegration:
    """Integration tests for complete workflows."""

    def test_indirect_condition_in_rung(self):
        """Rung(DD[Index] > 100) evaluates indirect condition."""
        DD = Block("DD", TagType.DINT, 1, 1000)
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
        DD = Block("DD", TagType.DINT, 1, 1000)
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
        DS = Block("DS", TagType.INT, 1, 4500)
        Index = Int("Index")
        Sum = Int("Sum")

        with Program() as logic:
            with Rung():
                copy(DS[Index], Sum)

        state = SystemState().with_tags({"Index": 10, "DS10": 100})
        state = evaluate_program(logic, state)
        assert state.tags["Sum"] == 100

        state = state.with_tags({"Index": 11, "DS11": 200})
        state = evaluate_program(logic, state)
        assert state.tags["Sum"] == 200

    def test_complete_style_blocks(self):
        """Define typed memory blocks."""
        DS = Block("DS", TagType.INT, 1, 4500, retentive=True)
        DD = Block("DD", TagType.DINT, 1, 1000, retentive=True)
        DH = Block("DH", TagType.WORD, 1, 500, retentive=True)
        DF = Block("DF", TagType.REAL, 1, 500, retentive=True)
        C = Block("C", TagType.BOOL, 1, 2000)

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
        DS = Block("DS", TagType.INT, 1, 4500)

        with pytest.raises(TypeError, match="Invalid key type"):
            DS[3.14]  # float is not a valid key type

    def test_indirect_hash(self):
        """IndirectRef is hashable."""
        DS = Block("DS", TagType.INT, 1, 4500)
        Index = Int("Index")
        indirect = DS[Index]

        hash(indirect)

        s = {indirect}
        assert indirect in s

    def test_indirect_equality_same(self):
        """Two IndirectRefs with same block and pointer share attributes."""
        DS = Block("DS", TagType.INT, 1, 4500)
        Index = Int("Index")

        indirect1 = DS[Index]
        indirect2 = DS[Index]

        assert indirect1.block == indirect2.block
        assert indirect1.pointer == indirect2.pointer


class TestBlockRangeCopyModifierHelpers:
    def test_block_range_as_value(self):
        from pyrung.core.copy_modifiers import CopyModifier

        CH = Block("CH", TagType.CHAR, 1, 10)
        wrapped = CH.select(1, 3).as_value()
        assert isinstance(wrapped, CopyModifier)
        assert wrapped.mode == "value"

    def test_indirect_block_range_as_ascii(self):
        from pyrung.core.copy_modifiers import CopyModifier

        CH = Block("CH", TagType.CHAR, 1, 10)
        Start = Int("Start")
        wrapped = CH.select(Start, 3).as_ascii()
        assert isinstance(wrapped, CopyModifier)
        assert wrapped.mode == "ascii"
