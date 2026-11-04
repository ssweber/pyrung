"""Tests for SystemState immutable dataclass.

TDD: Write tests first, then implement to pass.
"""

import copy
import pickle
import weakref
from collections.abc import Mapping
from typing import Any, cast

import pytest
from pyrsistent import PMap, PTypeError, pmap
from pyrsistent._checked_types import _restore_pickle


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

    def test_with_tags_accepts_string_values(self):
        """with_tags() supports CHAR-like string values."""
        from pyrung.core import SystemState

        state = SystemState().with_tags({"TXT1": "A"})

        assert state.tags["TXT1"] == "A"

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
    """Test that evolving a state shares what it did not change."""

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


def _full_state():
    from pyrung.core import SystemState

    return SystemState(scan_id=3, timestamp=1.5, tags=pmap({"A": 1}), memory=pmap({"m": 2}))


def _fields_of(state) -> dict[str, Any]:
    return {
        "scan_id": state.scan_id,
        "timestamp": state.timestamp,
        "tags": state.tags,
        "memory": state.memory,
    }


class TestEquality:
    """A state compares and hashes as the mapping of its four fields."""

    def test_equal_fields_are_equal_and_hash_alike(self):
        assert _full_state() == _full_state()
        assert hash(_full_state()) == hash(_full_state())
        assert len({_full_state(), _full_state()}) == 1

    @pytest.mark.parametrize(
        "changed",
        [
            {"scan_id": 4},
            {"timestamp": 1.25},
            {"tags": pmap({"A": 2})},
            {"memory": pmap()},
        ],
    )
    def test_any_field_differing_makes_them_differ(self, changed):
        assert _full_state().set(**changed) != _full_state()

    def test_equals_a_mapping_of_its_fields_either_way_round(self):
        state = _full_state()
        fields = _fields_of(state)
        assert state == fields and fields == state
        assert state == pmap(fields) and pmap(fields) == state
        assert state != {"scan_id": 3}
        assert state != 3

    def test_hash_is_the_hash_of_a_map_of_its_fields(self):
        # What a key built from a state was worth before it stopped being a map.
        state = _full_state()
        assert hash(state) == hash(pmap(_fields_of(state)))

    def test_unhashable_memory_makes_the_state_unhashable(self):
        from pyrung.core import SystemState

        with pytest.raises(TypeError):
            hash(SystemState(memory=pmap({"x": []})))

    def test_scan_id_compares_by_value(self):
        from pyrung.core import SystemState

        assert SystemState(scan_id=1) == SystemState(scan_id=True)

    def test_a_shared_nan_compares_equal_by_identity(self):
        from pyrung.core import SystemState

        nan = float("nan")
        assert SystemState(timestamp=nan) == SystemState(timestamp=nan)
        assert SystemState(timestamp=float("nan")) != SystemState(timestamp=float("nan"))


class TestImmutability:
    @pytest.mark.parametrize("name", ["scan_id", "timestamp", "tags", "memory", "nope"])
    def test_assignment_and_deletion_raise(self, name):
        state: Any = _full_state()
        before = _fields_of(state)
        with pytest.raises(AttributeError):
            setattr(state, name, 9)
        with pytest.raises(AttributeError):
            delattr(state, name)
        assert _fields_of(state) == before

    def test_item_assignment_raises(self):
        state: Any = _full_state()
        with pytest.raises(TypeError):
            state["scan_id"] = 9
        with pytest.raises(TypeError):
            del state["scan_id"]

    def test_calling_init_again_changes_nothing(self):
        state: Any = _full_state()
        state.__init__(scan_id=9)
        assert state.scan_id == 3

    def test_has_no_instance_dict(self):
        assert not hasattr(_full_state(), "__dict__")


class TestEvolving:
    def test_set_by_keyword_and_by_position(self):
        state = _full_state()
        assert state.set(scan_id=4).scan_id == 4
        assert state.set("scan_id", 4).scan_id == 4
        moved = state.set(scan_id=4, timestamp=2.0)
        assert (moved.scan_id, moved.timestamp, state.scan_id) == (4, 2.0, 3)

    def test_update_merges_maps_in_order(self):
        state = _full_state()
        moved = state.update({"scan_id": 8}, {"timestamp": 2.0, "scan_id": 5})
        assert (moved.scan_id, moved.timestamp) == (5, 2.0)

    def test_nothing_changed_returns_the_same_object(self):
        state = _full_state()
        assert state.set() is state
        assert state.update() is state
        assert state.set(tags=state.tags, memory=state.memory, scan_id=state.scan_id) is state
        assert state.with_tags({}) is state
        assert state.with_memory({}) is state
        assert state.scanned(state.scan_id, state.timestamp, state.tags, state.memory) is state

    def test_an_equal_map_that_is_not_the_same_object_makes_a_new_state(self):
        state = _full_state()
        assert state.set(tags=pmap(dict(state.tags))) is not state

    @pytest.mark.parametrize(
        "evolve",
        [
            lambda s: s.set(foo=1),
            lambda s: s.set("foo", 1),
            lambda s: s.update({"foo": 1}),
            lambda s: type(s)(foo=1),
            lambda s: type(s).create({"foo": 1}),
        ],
    )
    def test_an_unknown_field_raises_attribute_error(self, evolve):
        with pytest.raises(AttributeError, match="foo"):
            evolve(_full_state())

    @pytest.mark.parametrize(
        "evolve",
        [
            lambda s: s.set(timestamp=1),
            lambda s: s.set(scan_id="1"),
            lambda s: s.set(tags={"a": 1}),
            lambda s: s.set(memory={}),
            lambda s: type(s)(tags=None),
            lambda s: s.committed({}, s.memory, 0.1),
            lambda s: s.scanned("x", 9.0, s.tags, s.memory),
            lambda s: s.scanned(1, 9, s.tags, s.memory),
            lambda s: s.scanned(1, 9.0, s.tags, {}),
        ],
    )
    def test_a_wrong_type_raises_on_every_path(self, evolve):
        with pytest.raises(PTypeError):
            evolve(_full_state())

    def test_a_subclass_evolves_into_itself(self):
        from pyrung.core import SystemState

        class Sub(SystemState):
            __slots__ = ()

        assert type(Sub(scan_id=1).set(scan_id=2)) is Sub
        assert type(Sub().with_tags({"a": 1})) is Sub
        assert type(Sub().with_memory({"a": 1})) is Sub
        assert type(Sub().next_scan(0.1)) is Sub


class TestMappingSurface:
    """Reading a state as a mapping of its fields keeps working."""

    def test_reads_as_a_mapping(self):
        state = _full_state()
        assert isinstance(state, Mapping)
        assert dict(state) == _fields_of(state)
        assert dict(**state) == _fields_of(state)
        assert sorted(state) == ["memory", "scan_id", "tags", "timestamp"]
        assert sorted(state.keys()) == ["memory", "scan_id", "tags", "timestamp"]
        assert len(state) == 4
        assert state["scan_id"] == 3
        assert state.get("tags") is state.tags
        assert state.get("nope", 7) == 7
        assert "tags" in state
        assert "nope" not in state
        assert dict(state.items()) == _fields_of(state)
        assert list(state.values()) == [_fields_of(state)[key] for key in state]

    def test_a_missing_item_raises_key_error_and_a_missing_attribute_attribute_error(self):
        state: Any = _full_state()
        with pytest.raises(KeyError):
            state["nope"]
        with pytest.raises(AttributeError):
            _ = state.nope

    def test_the_map_methods_gone_with_the_map(self):
        # Reported in the CHANGELOG: a state reads as a mapping, it no longer is a PMap.
        state = _full_state()
        assert not isinstance(state, PMap)
        for name in ("remove", "discard", "transform", "evolver", "update_with", "serialize"):
            assert not hasattr(state, name)

    def test_repr_names_every_field(self):
        text = repr(_full_state())
        assert text.startswith("SystemState(scan_id=3, timestamp=1.5, tags=")
        assert "memory=" in text


class TestCopying:
    def test_copy_and_deepcopy_are_equal_states(self):
        state = _full_state()
        assert copy.copy(state) == state
        assert copy.copy(state).tags is state.tags
        assert copy.deepcopy(state) == state
        assert type(copy.deepcopy(state)) is type(state)

    @pytest.mark.parametrize("protocol", range(pickle.HIGHEST_PROTOCOL + 1))
    def test_pickles_at_every_protocol(self, protocol):
        state = _full_state()
        loaded = pickle.loads(pickle.dumps(state, protocol=protocol))
        assert type(loaded) is type(state)
        assert loaded == state
        assert hash(loaded) == hash(state)

    def test_reduces_to_what_the_map_form_reduced_to(self):
        # The format a state pickled in when it was a PRecord, both ways: an old pickle
        # still loads, and a new one loads on an older pyrung.
        from pyrung.core import SystemState

        state = _full_state()
        restore, (cls, fields) = state.__reduce__()
        assert restore is _restore_pickle
        assert cls is SystemState
        assert fields == _fields_of(state)
        assert _restore_pickle(SystemState, _fields_of(state)) == state

    def test_a_weak_reference_can_hold_a_state(self):
        state = _full_state()
        assert weakref.ref(state)() is state
