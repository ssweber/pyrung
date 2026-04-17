"""ScanContext - Batched write context for a single scan cycle.

Optimizes performance by batching all tag/memory updates within a scan,
reducing object allocation from O(instructions) to O(1) per scan while
preserving read-after-write visibility.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from pyrsistent import PMap, pmap

if TYPE_CHECKING:
    from pyrung.core.state import SystemState

TagResolver = Callable[[str, Any], tuple[bool, Any]]


class ConditionView:
    """Frozen read-only view of tag/memory state for condition evaluation.

    Created at rung entry so that all branch conditions — at every nesting
    depth — evaluate against the same snapshot, regardless of mutations made
    by instructions that execute between branch evaluations.
    """

    __slots__ = ("_state", "_tags_snapshot", "_memory_snapshot", "_resolver", "_scope_token")

    def __init__(self, ctx: ScanContext) -> None:
        self._state: SystemState = ctx._state
        self._tags_snapshot: dict[str, Any] = dict(ctx._tags_pending)
        self._memory_snapshot: dict[str, Any] = dict(ctx._memory_pending)
        self._resolver = ctx._resolver
        self._scope_token = ctx._condition_scope_token

    def get_tag(self, name: str, default: Any = None) -> Any:
        if name in self._tags_snapshot:
            return self._tags_snapshot[name]
        if name in self._state.tags:
            return self._state.tags[name]
        if self._resolver is not None:
            resolved, value = self._resolver(name, self)
            if resolved:
                return value
        return default

    def get_memory(self, key: str, default: Any = None) -> Any:
        if key in self._memory_snapshot:
            return self._memory_snapshot[key]
        return self._state.memory.get(key, default)

    def _get_tag_internal(self, name: str, default: Any = None) -> Any:
        if name in self._tags_snapshot:
            return self._tags_snapshot[name]
        return self._state.tags.get(name, default)

    def _has_tag_internal(self, name: str) -> bool:
        return name in self._tags_snapshot or name in self._state.tags

    def _get_memory_internal(self, key: str, default: Any = None) -> Any:
        if key in self._memory_snapshot:
            return self._memory_snapshot[key]
        return self._state.memory.get(key, default)

    def _has_memory_internal(self, key: str) -> bool:
        return key in self._memory_snapshot or key in self._state.memory

    @property
    def scan_id(self) -> int:
        return self._state.scan_id

    @property
    def timestamp(self) -> float:
        return self._state.timestamp

    @property
    def original_state(self) -> SystemState:
        return self._state

    @property
    def scope_token(self) -> object:
        return self._scope_token


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
        "_condition_snapshot",
        "_condition_scope_token",
        "_rung_firings",
        "_consumed_tags_getter",
    )

    def __init__(
        self,
        state: SystemState,
        *,
        resolver: TagResolver | None = None,
        read_only_tags: frozenset[str] = frozenset(),
        consumed_tags_getter: Callable[[], frozenset[str] | None] | None = None,
    ) -> None:
        """Create a new ScanContext from a SystemState.

        Args:
            state: The current system state to build upon.
            resolver: Optional fallback for unresolved tag reads.
            read_only_tags: System points that must not be written.
            consumed_tags_getter: Optional callable returning the set of
                tag names that at least one rung reads.  When provided
                and non-None, :meth:`capturing_rung` drops writes to
                tags outside the set — the firing log is consumed by
                the simulator's own analysis APIs, which by definition
                don't ask about unread tags.  Returning ``None`` from
                the callable bypasses the filter (escape hatch).
        """
        self._state = state
        self._tags_evolver = state.tags.evolver()
        self._memory_evolver = state.memory.evolver()
        self._tags_pending: dict[str, Any] = {}
        self._memory_pending: dict[str, Any] = {}
        self._resolver = resolver
        self._read_only_tags = read_only_tags
        self._condition_snapshot: ConditionView | None = None
        self._condition_scope_token = object()
        self._rung_firings: dict[int, dict[str, Any]] = {}
        self._consumed_tags_getter = consumed_tags_getter

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
    # Rung-scoped firing capture
    # =========================================================================

    @contextmanager
    def capturing_rung(self, rung_index: int) -> Iterator[None]:
        """Attribute all tag writes made inside this block to ``rung_index``.

        Produces the input data for :attr:`rung_firings` by diffing
        ``_tags_pending`` at the scope boundary.  Wrap each top-level
        rung evaluation in this context manager; both the non-debug and
        debug scan paths rely on it to populate the firing log used by
        causal-chain analysis.

        Nesting is not supported — each scope must close before the next
        opens.  Writes made outside any scope (e.g. pre-force, system
        runtime) are intentionally unattributed.
        """
        before = dict(self._tags_pending)
        try:
            yield
        finally:
            pending = self._tags_pending
            raw_writes = {
                name: pending[name]
                for name in pending
                if name not in before or before[name] != pending[name]
            }
            if raw_writes:
                consumed = (
                    self._consumed_tags_getter() if self._consumed_tags_getter is not None else None
                )
                if consumed is None:
                    writes = raw_writes
                else:
                    writes = {name: val for name, val in raw_writes.items() if name in consumed}
                # Record the rung_index even when the filter emptied
                # ``writes`` — the non-empty ``raw_writes`` establishes
                # that the rung fired, which ``query.cold_rungs`` /
                # ``query.hot_rungs`` and ``effect()``'s PDG fallback
                # both need.  Consumers that care about per-tag values
                # (like ``cause()``'s value-match) see the filtered view
                # and fall through cleanly when it's empty.
                self._rung_firings[rung_index] = writes

    @property
    def rung_firings(self) -> PMap:
        """Per-rung tag writes captured via :meth:`capturing_rung`.

        ``PMap[int, PMap[str, Any]]`` — rung index to ``{tag: value_written}``.
        Empty if no rung scopes were opened during the scan.
        """
        return pmap({i: pmap(w) for i, w in self._rung_firings.items()})

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
