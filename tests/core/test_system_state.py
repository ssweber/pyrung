"""Tests for SystemState immutable dataclass.

TDD: Write tests first, then implement to pass.
"""

from typing import Any, cast

import pytest
from pyrsistent import pmap


class TestSystemStateCreation:
    """Test SystemState construction and defaults."""

    def test_empty_creates_valid_state(self):
        """SystemState() creates a state with scan_id=0, timestamp=0.0."""
        from pyrung.core import SystemState

        state = SystemState()

        assert state.scan_id == 0
        assert state.timestamp == 0.0
        assert dict(state.tags) == {}
        assert dict(state.memory) == {}

    def test_create_with_initial_tags(self):
        """Can create state with initial tag values."""
        from pyrung.core import SystemState

        state = SystemState().with_tags({"Motor": False, "Speed": 0})

        assert state.tags["Motor"] is False
        assert state.tags["Speed"] == 0

    def test_create_with_scan_id(self):
        """Can create state with specific scan_id."""
        from pyrung.core import SystemState

        state = SystemState(
            scan_id=42,
            timestamp=1.5,
            tags=pmap(),
            memory=pmap(),
        )

        assert state.scan_id == 42
        assert state.timestamp == 1.5


class TestSystemStateImmutability:
    """Test that SystemState is truly immutable."""

    def test_frozen_dataclass_prevents_attribute_mutation(self):
        """Cannot assign to attributes of frozen dataclass."""
        from pyrung.core import SystemState

        state = SystemState()

        with pytest.raises(AttributeError):
            state.scan_id = 1

    def test_tags_are_immutable(self):
        """Tags dict cannot be mutated."""
        from pyrung.core import SystemState

        state = SystemState().with_tags({"Motor": False})

        with pytest.raises(TypeError):
            tags = cast(Any, state.tags)
            tags["Motor"] = True

    def test_memory_is_immutable(self):
        """Memory dict cannot be mutated."""
        from pyrung.core import SystemState

        state = SystemState().with_memory({"timer_1": 0})

        with pytest.raises(TypeError):
            memory = cast(Any, state.memory)
            memory["timer_1"] = 100


class TestSystemStateTransitions:
    """Test pure state transitions (state -> new state)."""

    def test_with_tags_returns_new_state(self):
        """with_tags() returns a new state, original unchanged."""
        from pyrung.core import SystemState

        original = SystemState()
        new_state = original.with_tags({"Motor": True})

        assert original.tags.get("Motor") is None
        assert new_state.tags["Motor"] is True
        assert original is not new_state

    def test_with_tags_merges_existing(self):
        """with_tags() merges new tags with existing tags."""
        from pyrung.core import SystemState

        state1 = SystemState().with_tags({"A": 1, "B": 2})
        state2 = state1.with_tags({"B": 99, "C": 3})

        assert state2.tags["A"] == 1  # Preserved
        assert state2.tags["B"] == 99  # Updated
        assert state2.tags["C"] == 3  # Added

    def test_with_memory_returns_new_state(self):
        """with_memory() returns a new state, original unchanged."""
        from pyrung.core import SystemState

        original = SystemState()
        new_state = original.with_memory({"timer_acc": 500})

        assert original.memory.get("timer_acc") is None
        assert new_state.memory["timer_acc"] == 500

    def test_next_scan_increments_scan_id(self):
        """next_scan() increments scan_id and updates timestamp."""
        from pyrung.core import SystemState

        state = SystemState()
        next_state = state.next_scan(dt=0.1)

        assert next_state.scan_id == 1
        assert next_state.timestamp == pytest.approx(0.1)
        assert state.scan_id == 0  # Original unchanged

    def test_next_scan_preserves_tags_and_memory(self):
        """next_scan() preserves existing tags and memory."""
        from pyrung.core import SystemState

        state = SystemState().with_tags({"X": True}).with_memory({"Y": 42})
        next_state = state.next_scan(dt=0.1)

        assert next_state.tags["X"] is True
        assert next_state.memory["Y"] == 42


class TestStructuralSharing:
    """Test that PRecord provides structural sharing."""

    def test_unchanged_fields_share_memory(self):
        """When only tags change, memory should be the same object."""
        from pyrung.core import SystemState

        s1 = SystemState().with_memory({"x": 1})
        s2 = s1.with_tags({"y": 2})

        assert s1.memory is s2.memory  # Structural sharing

    def test_unchanged_fields_share_memory_for_tags(self):
        """When only memory changes, tags should be the same object."""
        from pyrung.core import SystemState

        s1 = SystemState().with_tags({"x": 1})
        s2 = s1.with_memory({"y": 2})

        assert s1.tags is s2.tags  # Structural sharing
