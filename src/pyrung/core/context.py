"""ScanContext - Batched write context for a single scan cycle.

Optimizes performance by batching all tag/memory updates within a scan,
reducing object allocation from O(instructions) to O(1) per scan while
preserving read-after-write visibility.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pyrung.core.state import SystemState

TagResolver = Callable[[str, Any], tuple[bool, Any]]


class ScanContext:
    """Batched write context for a single scan cycle.

    Collects all tag and memory writes during a scan cycle, then commits
    them all at once to produce a new SystemState. Provides read-after-write
    visibility so subsequent instructions in the same scan see updated values.

    Attributes:
        _state: The original SystemState (immutable, not modified).
        _tags_evolver: Pyrsistent evolver for final tag commit.
        _memory_evolver: Pyrsistent evolver for final memory commit.
        _tags_pending: Fast lookup dict for pending tag writes.
        _memory_pending: Fast lookup dict for pending memory writes.
    """

    __slots__ = (
        "_state",
        "_tags_evolver",
        "_memory_evolver",
        "_tags_pending",
        "_memory_pending",
        "_resolver",
        "_read_only_tags",
    )

    def __init__(
        self,
        state: SystemState,
        *,
        resolver: TagResolver | None = None,
        read_only_tags: frozenset[str] = frozenset(),
    ) -> None:
        """Create a new ScanContext from a SystemState.

        Args:
            state: The current system state to build upon.
        """
        self._state = state
        self._tags_evolver = state.tags.evolver()
        self._memory_evolver = state.memory.evolver()
        self._tags_pending: dict[str, Any] = {}
        self._memory_pending: dict[str, Any] = {}
        self._resolver = resolver
        self._read_only_tags = read_only_tags

    # =========================================================================
    # Read operations (with pending visibility)
    # =========================================================================

    def get_tag(self, name: str, default: Any = None) -> Any:
        """Get a tag value, checking pending writes first.

        Provides read-after-write visibility within the same scan cycle.

        Args:
            name: The tag name to retrieve.
            default: Value to return if tag not found.

        Returns:
            The tag value from pending writes, original state, or default.
        """
        if name in self._tags_pending:
            return self._tags_pending[name]
        if name in self._state.tags:
            return self._state.tags[name]
        if self._resolver is not None:
            resolved, value = self._resolver(name, self)
            if resolved:
                return value
        return default

    def get_memory(self, key: str, default: Any = None) -> Any:
        """Get a memory value, checking pending writes first.

        Provides read-after-write visibility within the same scan cycle.

        Args:
            key: The memory key to retrieve.
            default: Value to return if key not found.

        Returns:
            The memory value from pending writes, original state, or default.
        """
        if key in self._memory_pending:
            return self._memory_pending[key]
        return self._state.memory.get(key, default)

    # =========================================================================
    # Write operations (batched)
    # =========================================================================

    def set_tag(self, name: str, value: Any) -> None:
        """Set a tag value (batched, committed at end of scan).

        Args:
            name: The tag name to set.
            value: The value to set.
        """
        if name in self._read_only_tags:
            raise ValueError(f"Tag '{name}' is read-only system point and cannot be written")
        self._tags_pending[name] = value
        self._tags_evolver[name] = value

    def set_tags(self, updates: dict[str, Any]) -> None:
        """Set multiple tag values (batched, committed at end of scan).

        Args:
            updates: Dict of tag names to values.
        """
        for name in updates:
            if name in self._read_only_tags:
                raise ValueError(f"Tag '{name}' is read-only system point and cannot be written")
        self._tags_pending.update(updates)
        for name, value in updates.items():
            self._tags_evolver[name] = value

    def _set_tag_internal(self, name: str, value: Any) -> None:
        """Set a tag while bypassing read-only guards (runtime-only use)."""
        self._tags_pending[name] = value
        self._tags_evolver[name] = value

    def _set_tags_internal(self, updates: dict[str, Any]) -> None:
        """Set multiple tags while bypassing read-only guards (runtime-only use)."""
        self._tags_pending.update(updates)
        for name, value in updates.items():
            self._tags_evolver[name] = value

    def set_memory(self, key: str, value: Any) -> None:
        """Set a memory value (batched, committed at end of scan).

        Args:
            key: The memory key to set.
            value: The value to set.
        """
        self._memory_pending[key] = value
        self._memory_evolver[key] = value

    def set_memory_bulk(self, updates: dict[str, Any]) -> None:
        """Set multiple memory values (batched, committed at end of scan).

        Args:
            updates: Dict of memory keys to values.
        """
        self._memory_pending.update(updates)
        for key, value in updates.items():
            self._memory_evolver[key] = value

    def _get_tag_internal(self, name: str, default: Any = None) -> Any:
        """Read tag value without resolver fallback."""
        if name in self._tags_pending:
            return self._tags_pending[name]
        return self._state.tags.get(name, default)

    def _has_tag_internal(self, name: str) -> bool:
        """Check for a pending or persisted tag without resolver fallback."""
        return name in self._tags_pending or name in self._state.tags

    def _get_memory_internal(self, key: str, default: Any = None) -> Any:
        """Read memory value without side effects."""
        if key in self._memory_pending:
            return self._memory_pending[key]
        return self._state.memory.get(key, default)

    def _has_memory_internal(self, key: str) -> bool:
        """Check for a pending or persisted memory key."""
        return key in self._memory_pending or key in self._state.memory

    # =========================================================================
    # Passthrough properties
    # =========================================================================

    @property
    def scan_id(self) -> int:
        """Current scan ID from the original state."""
        return self._state.scan_id

    @property
    def timestamp(self) -> float:
        """Current timestamp from the original state."""
        return self._state.timestamp

    @property
    def original_state(self) -> SystemState:
        """Access to the original (unmodified) state.

        Useful for operations that need to read original values,
        such as computing _prev:* for edge detection.
        """
        return self._state

    # =========================================================================
    # Commit
    # =========================================================================

    def commit(self, dt: float) -> SystemState:
        """Commit all pending changes and advance to next scan.

        Creates a new SystemState with all batched tag and memory updates,
        then advances scan_id and timestamp.

        Args:
            dt: Time delta in seconds to add to timestamp.

        Returns:
            New SystemState with all changes applied.
        """

        # Build final tags and memory from evolvers
        new_tags = self._tags_evolver.persistent()
        new_memory = self._memory_evolver.persistent()

        # Create new state with updated tags/memory and advance scan
        new_state = self._state.set(tags=new_tags, memory=new_memory)
        return new_state.next_scan(dt=dt)
