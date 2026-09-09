"""Pointer evidence belongs to the access, including instruction/evaluation order."""

import pytest

from pyrung import Bool, Int, Or, Program, Rung, branch, copy
from pyrung.core import Block, TagType


def _codes(program):
    return {f.code for f in program.check(select={"PTR"})}


@pytest.mark.parametrize("index", [0, 101])
def test_invalid_literal_write_before_read_is_not_sanitization(index):
    block = Block("Data", TagType.INT, 1, 100)
    pointer = Int("Index")
    with Program() as program:
        with Rung():
            copy(index, pointer)
            copy(block[pointer], Int("Result"))
    assert _codes(program) == {"PTR_MAY_ESCAPE_BLOCK"}


def test_range_guard_accepts_open_external_pointer_with_zero_default():
    block = Block("Data", TagType.INT, 1, 100)
    pointer = Int("Index", external=True)
    with Program() as program:
        with Rung(pointer >= 1, pointer <= 100):
            copy(block[pointer], Int("Result"))
    assert not _codes(program)


def test_open_pointer_without_guard_is_advisory_when_default_is_valid():
    block = Block("Data", TagType.INT, 1, 100)
    pointer = Int("Index", external=True, default=1)
    with Program() as program:
        with Rung():
            copy(block[pointer], Int("Result"))
    assert _codes(program) == {"PTR_UNGUARDED_ACCESS"}
    assert not program.check()  # The uncertain rule is opt-in.


def test_known_assignment_needs_no_guard():
    block = Block("Data", TagType.INT, 1, 100)
    pointer = Int("Index")
    with Program() as program:
        with Rung():
            copy(5, pointer)
            copy(block[pointer], Int("Result"))
    assert not _codes(program)


def test_overwrite_invalidates_earlier_guard():
    block = Block("Data", TagType.INT, 1, 100)
    pointer = Int("Index", external=True)
    with Program() as program:
        with Rung(pointer >= 1, pointer <= 100):
            copy(101, pointer)
            copy(block[pointer], Int("Result"))
    assert _codes(program) == {"PTR_MAY_ESCAPE_BLOCK"}


@pytest.mark.parametrize("guard_first", [True, False])
def test_guard_must_precede_condition_dereference(guard_first):
    block = Block("Data", TagType.INT, 1, 100)
    pointer = Int("Index", external=True)
    conditions = [pointer >= 1, pointer <= 100, block[pointer] == 7]
    if not guard_first:
        conditions.reverse()
    with Program() as program:
        with Rung(*conditions):
            copy(1, Int("Result"))
    assert bool(_codes(program)) is not guard_first


def test_or_does_not_turn_partial_bounds_into_a_guard():
    block = Block("Data", TagType.INT, 1, 100)
    pointer = Int("Index", external=True)
    with Program() as program:
        with Rung(Or(pointer >= 1, pointer <= 100)):
            copy(block[pointer], Int("Result"))
    assert _codes(program)


def test_branch_write_before_access_invalidates_guard():
    block = Block("Data", TagType.INT, 1, 100)
    pointer = Int("Index", external=True)
    with Program() as program:
        with Rung(pointer >= 1, pointer <= 100):
            with branch(Bool("Change")):
                copy(101, pointer)
            copy(block[pointer], Int("Result"))
    assert _codes(program)


def test_branch_guard_was_evaluated_before_parent_write():
    block = Block("Data", TagType.INT, 1, 100)
    pointer = Int("Index", external=True, default=5)
    with Program() as program:
        with Rung():
            copy(101, pointer)
            with branch(pointer == 5):
                copy(block[pointer], Int("Result"))
    assert _codes(program) == {"PTR_MAY_ESCAPE_BLOCK"}


def test_continued_guard_was_evaluated_before_prior_rung_write():
    block = Block("Data", TagType.INT, 1, 100)
    pointer = Int("Index", external=True, default=5)
    with Program() as program:
        with Rung():
            copy(101, pointer)
        with Rung().continued():
            with branch(pointer == 5):
                copy(block[pointer], Int("Result"))
    assert _codes(program) == {"PTR_MAY_ESCAPE_BLOCK"}


def test_branch_condition_read_uses_snapshot_before_instruction_assignment():
    block = Block("Data", TagType.INT, 1, 100)
    pointer = Int("Index")
    with Program() as program:
        with Rung():
            copy(5, pointer)
            with branch(block[pointer] == 7):
                copy(1, Int("Result"))
    assert _codes(program) == {"PTR_DEFAULT_BEFORE_BLOCK_START"}


def test_continued_condition_read_uses_original_snapshot():
    block = Block("Data", TagType.INT, 1, 100)
    pointer = Int("Index")
    with Program() as program:
        with Rung():
            copy(5, pointer)
        with Rung(block[pointer] == 7).continued():
            copy(1, Int("Result"))
    assert _codes(program) == {"PTR_DEFAULT_BEFORE_BLOCK_START"}
