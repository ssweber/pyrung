"""ScanContext - Batched write context for a single scan cycle.

Optimizes performance by batching all tag/memory updates within a scan,
reducing object allocation from O(instructions) to O(1) per scan while
preserving read-after-write visibility.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, NamedTuple

from pyrsistent import PMap, pmap

if TYPE_CHECKING:
    from pyrung.core.scan_log import IoResultRecord, IoSubmitRecord
    from pyrung.core.state import SystemState

TagResolver = Callable[[str, Any], tuple[bool, Any]]

# Read-path sentinel: hot lookups cache the ``state.tags``/``state.memory``
# pmaps once (each ``state.<field>`` access is itself a PRecord bucket walk)
# and probe them a single time via try/except instead of ``in`` + ``[]``.
_MISSING = object()


def _commit_changed(base: PMap, pending: Mapping[str, Any]) -> PMap:
    """Publish only final values that differ from the immutable scan base."""
    evolver = None
    for key, value in pending.items():
        if base.get(key, _MISSING) == value:
            continue
        if evolver is None:
            evolver = base.evolver()
        evolver[key] = value
    return base if evolver is None else evolver.persistent()


class RungId(NamedTuple):
    """Identity of an executed rung for node-granular firing capture.

    ``subroutine`` is ``None`` for a top-level (main) rung and the
    subroutine name for a rung executed inside a ``call()``.  ``rung_index``
    is 0-based within that scope.  Used as the key of the node-level firing
    timeline and to build user-facing labels like ``"MySub:3"``.
    """

    subroutine: str | None
    rung_index: int


class ConditionView:
    """Frozen read-only view of tag/memory state for condition evaluation.

    Created at rung entry so that all branch conditions — at every nesting
    depth — evaluate against the same snapshot, regardless of mutations made
    by instructions that execute between branch evaluations.
    """

    __slots__ = (
        "_state",
        "_tags",
        "_memory",
        "_tags_snapshot",
        "_memory_snapshot",
        "_resolver",
        "_scope_token",
    )

    def __init__(self, ctx: ScanContext) -> None:
        self._state: SystemState = ctx._state
        self._tags: Mapping[str, Any] = ctx._state_tags_read
        self._memory: PMap = ctx._state_memory
        self._tags_snapshot: dict[str, Any] = dict(ctx._tags_pending)
        self._memory_snapshot: dict[str, Any] = dict(ctx._memory_pending)
        self._resolver = ctx._resolver
        self._scope_token = ctx._condition_scope_token

    def get_tag(self, name: str, default: Any = None) -> Any:
        snap = self._tags_snapshot
        if name in snap:
            return snap[name]
        try:
            return self._tags[name]
        except KeyError:
            pass
        if self._resolver is not None:
            resolved, value = self._resolver(name, self)
            if resolved:
                return value
        return default

    def get_memory(self, key: str, default: Any = None) -> Any:
        snap = self._memory_snapshot
        if key in snap:
            return snap[key]
        try:
            return self._memory[key]
        except KeyError:
            return default

    def _get_tag_internal(self, name: str, default: Any = None) -> Any:
        snap = self._tags_snapshot
        if name in snap:
            return snap[name]
        try:
            return self._tags[name]
        except KeyError:
            return default

    def _has_tag_internal(self, name: str) -> bool:
        return name in self._tags_snapshot or name in self._tags

    def _get_memory_internal(self, key: str, default: Any = None) -> Any:
        snap = self._memory_snapshot
        if key in snap:
            return snap[key]
        try:
            return self._memory[key]
        except KeyError:
            return default

    def _has_memory_internal(self, key: str) -> bool:
        return key in self._memory_snapshot or key in self._memory

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
        _tags_pending: Fast lookup dict for pending tag writes.
        _memory_pending: Fast lookup dict for pending memory writes.
    """

    __slots__ = (
        "_state",
        "_state_tags",
        "_state_tags_read",
        "_state_memory",
        "_tags_pending",
        "_memory_pending",
        "_capture_stack",
        "_current_node_id",
        "_read_sink",
        "_resolver",
        "_read_only_tags",
        "_condition_snapshot",
        "_condition_scope_token",
        "_rung_firings",
        "_node_firings",
        "_consumed_tags_getter",
        "_io_submit_staging",
        "_io_drain_staging",
        "_replay_io_submits",
        "_replay_io_drains",
        "_is_replay_io",
    )

    def __init__(
        self,
        state: SystemState,
        *,
        resolver: TagResolver | None = None,
        read_only_tags: frozenset[str] = frozenset(),
        consumed_tags_getter: Callable[[], frozenset[str] | None] | None = None,
        replay_io: tuple[Mapping[str, IoSubmitRecord], Mapping[str, IoResultRecord]] | None = None,
        state_tags_read: Mapping[str, Any] | None = None,
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
            replay_io: When replaying, a pair of (submits, drains)
                for this scan.  ``None`` during live execution.
        """
        self._state = state
        self._state_tags: PMap = state.tags
        self._state_tags_read: Mapping[str, Any] = (
            self._state_tags if state_tags_read is None else state_tags_read
        )
        self._state_memory: PMap = state.memory
        self._tags_pending: dict[str, Any] = {}
        self._memory_pending: dict[str, Any] = {}
        self._capture_stack: list[dict[str, Any]] = []
        # Identity of the subroutine rung whose ``capturing_node`` scope is
        # currently open, or ``None`` at main scope.  Read by observers
        # (ConditionViewCapture) so they key subroutine rungs by the same
        # ``RungId`` as the node firing timeline — one source of truth.
        self._current_node_id: RungId | None = None
        # Optional data-read sink (Crossings Tier 2).  When non-None, every
        # ``get_tag`` read appends its tag name here — an observer points this
        # at a per-node bucket during the on-demand interpreted replay so the
        # recorded read-diff sees the operands the writer actually read
        # (resolved indirect addresses, only the firing branch).  ``None`` on
        # every normal scan, so the hot path pays a single attribute check.
        self._read_sink: set[str] | None = None
        self._resolver = resolver
        self._read_only_tags = read_only_tags
        self._condition_snapshot: ConditionView | None = None
        self._condition_scope_token = object()
        self._rung_firings: dict[int, dict[str, Any]] = {}
        self._node_firings: dict[RungId, dict[str, Any]] = {}
        self._consumed_tags_getter = consumed_tags_getter
        self._io_submit_staging: dict[str, IoSubmitRecord] = {}
        self._io_drain_staging: dict[str, IoResultRecord] = {}
        self._is_replay_io: bool = replay_io is not None
        self._replay_io_submits: Mapping[str, IoSubmitRecord] = (
            replay_io[0] if replay_io is not None else {}
        )
        self._replay_io_drains: Mapping[str, IoResultRecord] = (
            replay_io[1] if replay_io is not None else {}
        )

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
        if self._read_sink is not None:
            self._read_sink.add(name)
        pending = self._tags_pending
        if name in pending:
            return pending[name]
        try:
            return self._state_tags_read[name]
        except KeyError:
            pass
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
        pending = self._memory_pending
        if key in pending:
            return pending[key]
        try:
            return self._state_memory[key]
        except KeyError:
            return default

    # =========================================================================
    # Write operations (batched)
    # =========================================================================

    def _journal_capture(self, name: str) -> None:
        """Record *name*'s pre-write pending value in every open capture scope."""
        stack = self._capture_stack
        if not stack:
            return
        pending = self._tags_pending
        for journal in stack:
            if name not in journal:
                journal[name] = pending.get(name, _MISSING)

    def set_tag(self, name: str, value: Any) -> None:
        """Set a tag value (batched, committed at end of scan).

        Args:
            name: The tag name to set.
            value: The value to set.
        """
        if name in self._read_only_tags:
            raise ValueError(f"Tag '{name}' is read-only system point and cannot be written")
        self._journal_capture(name)
        self._tags_pending[name] = value

    def set_tags(self, updates: dict[str, Any]) -> None:
        """Set multiple tag values (batched, committed at end of scan).

        Args:
            updates: Dict of tag names to values.
        """
        for name in updates:
            if name in self._read_only_tags:
                raise ValueError(f"Tag '{name}' is read-only system point and cannot be written")
        if self._capture_stack:
            for name in updates:
                self._journal_capture(name)
        self._tags_pending.update(updates)

    def _set_tag_internal(self, name: str, value: Any) -> None:
        """Set a tag while bypassing read-only guards (runtime-only use)."""
        self._journal_capture(name)
        self._tags_pending[name] = value

    def _set_tags_internal(self, updates: dict[str, Any]) -> None:
        """Set multiple tags while bypassing read-only guards (runtime-only use)."""
        if self._capture_stack:
            for name in updates:
                self._journal_capture(name)
        self._tags_pending.update(updates)

    def set_memory(self, key: str, value: Any) -> None:
        """Set a memory value (batched, committed at end of scan).

        Args:
            key: The memory key to set.
            value: The value to set.
        """
        self._memory_pending[key] = value

    def set_memory_bulk(self, updates: dict[str, Any]) -> None:
        """Set multiple memory values (batched, committed at end of scan).

        Args:
            updates: Dict of memory keys to values.
        """
        self._memory_pending.update(updates)

    def _get_tag_internal(self, name: str, default: Any = None) -> Any:
        """Read tag value without resolver fallback."""
        pending = self._tags_pending
        if name in pending:
            return pending[name]
        try:
            return self._state_tags_read[name]
        except KeyError:
            return default

    def _has_tag_internal(self, name: str) -> bool:
        """Check for a pending or persisted tag without resolver fallback."""
        return name in self._tags_pending or name in self._state_tags_read

    def _get_memory_internal(self, key: str, default: Any = None) -> Any:
        """Read memory value without side effects."""
        pending = self._memory_pending
        if key in pending:
            return pending[key]
        try:
            return self._state_memory[key]
        except KeyError:
            return default

    def _has_memory_internal(self, key: str) -> bool:
        """Check for a pending or persisted memory key."""
        return key in self._memory_pending or key in self._state_memory

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

    def _begin_capture(self) -> dict[str, Any]:
        """Open a write journal for the interpreter's allocation-sensitive path."""
        journal: dict[str, Any] = {}
        self._capture_stack.append(journal)
        return journal

    def _finish_rung_capture(self, rung_index: int, journal: dict[str, Any]) -> None:
        """Close a hot-path main-rung journal and retain its writes."""
        self._capture_stack.pop()
        writes = self._finalize_capture(journal)
        if writes is not None:
            self._rung_firings[rung_index] = writes

    def _begin_node_capture(self, rung_id: RungId) -> tuple[dict[str, Any], RungId | None]:
        """Open a hot-path subroutine journal and publish its node identity."""
        journal = self._begin_capture()
        previous_node_id = self._current_node_id
        self._current_node_id = rung_id
        return journal, previous_node_id

    def _finish_node_capture(
        self,
        rung_id: RungId,
        journal: dict[str, Any],
        previous_node_id: RungId | None,
    ) -> None:
        """Close a hot-path subroutine journal and merge repeated calls."""
        self._current_node_id = previous_node_id
        self._capture_stack.pop()
        writes = self._finalize_capture(journal)
        if writes is None:
            return
        previous = self._node_firings.get(rung_id)
        if previous is not None:
            merged = dict(previous)
            merged.update(writes)
            self._node_firings[rung_id] = merged
        else:
            self._node_firings[rung_id] = writes

    @contextmanager
    def capturing_rung(self, rung_index: int) -> Iterator[None]:
        """Attribute all tag writes made inside this block to ``rung_index``.

        Produces the input data for :attr:`rung_firings` from a write
        journal: setters record each name's pre-write pending value
        while a scope is open, so the exit diff costs O(writes in this
        rung) instead of one full ``_tags_pending`` copy per rung.
        Wrap each top-level rung evaluation in this context manager; both
        the non-debug and debug scan paths rely on it to populate the
        firing log used by causal-chain analysis.

        Scopes nest via a stack: :meth:`capturing_node` opens inner scopes
        for subroutine rungs while this outer scope stays open, so a write
        is attributed to every open scope (the outer top-level rung still
        sees the whole subtree — the main-rung firing is unchanged — while
        the inner scope records the subroutine rung's own slice).  Writes
        made outside any scope (e.g. pre-force, system runtime) are
        intentionally unattributed.
        """
        journal = self._begin_capture()
        try:
            yield
        finally:
            self._finish_rung_capture(rung_index, journal)

    @contextmanager
    def capturing_node(self, rung_id: RungId) -> Iterator[None]:
        """Attribute writes made inside this block to ``rung_id`` (a subroutine rung).

        Opens an inner scope stacked under the enclosing
        :meth:`capturing_rung`, recording the subroutine rung's own write
        slice for the node-level firing timeline (``cold_rungs`` /
        ``hot_rungs`` for subroutine rungs).  Multiple calls of the same
        subroutine in one scan reuse the same ``rung_id`` key — writes
        are merged (dict union) so all calls' tags are visible to
        ``cause()`` and ``cold_rungs``.  If per-call-site attribution is
        needed in the future, add a ``call_site`` field to ``RungId``
        keyed by the main rung that triggered the ``call()``.

        While the scope is open, :attr:`_current_node_id` names this rung so
        observers can key it by the same ``RungId`` (nested calls save and
        restore the enclosing id).
        """
        journal, previous_node_id = self._begin_node_capture(rung_id)
        try:
            yield
        finally:
            self._finish_node_capture(rung_id, journal, previous_node_id)

    def _finalize_capture(self, journal: dict[str, Any]) -> dict[str, Any] | None:
        """Diff a closed capture scope's journal into its firing writes.

        Returns the (PDG-filtered) ``{tag: value}`` written during the
        scope, or ``None`` if the scope made no write at all.  A non-empty
        raw diff that the consumed-tags filter empties still returns ``{}``
        — the rung fired, which ``query.cold_rungs`` / ``query.hot_rungs``
        and ``effect()``'s PDG fallback both need; consumers that care
        about per-tag values (like ``cause()``'s value-match) see the
        filtered view and fall through cleanly when it's empty.
        """
        pending = self._tags_pending
        raw_writes = {
            name: pending[name]
            for name, old in journal.items()
            if old is _MISSING or old != pending[name]
        }
        if not raw_writes:
            return None
        consumed = self._consumed_tags_getter() if self._consumed_tags_getter is not None else None
        if consumed is None:
            return raw_writes
        return {name: val for name, val in raw_writes.items() if name in consumed}

    @property
    def rung_firings(self) -> PMap:
        """Per-rung tag writes captured via :meth:`capturing_rung`.

        ``PMap[int, PMap[str, Any]]`` — rung index to ``{tag: value_written}``.
        Empty if no rung scopes were opened during the scan.
        """
        return pmap({i: pmap(w) for i, w in self._rung_firings.items()})

    @property
    def node_firings(self) -> PMap:
        """Per-subroutine-rung write slices captured via :meth:`capturing_node`.

        ``PMap[RungId, PMap[str, Any]]`` — keyed by
        ``RungId(subroutine, rung_index)``.  Feeds the node-level firing
        timeline that makes ``cold_rungs`` / ``hot_rungs`` see subroutine
        rungs.  Empty when no subroutine ran during the scan.
        """
        return pmap({k: pmap(w) for k, w in self._node_firings.items()})

    # =========================================================================
    # I/O replay recording and lookup
    # =========================================================================

    @property
    def is_replay_io(self) -> bool:
        return self._is_replay_io

    def record_io_submit(self, key: str, record: IoSubmitRecord) -> None:
        self._io_submit_staging[key] = record

    def record_io_drain(self, key: str, record: IoResultRecord) -> None:
        self._io_drain_staging[key] = record

    def has_replay_io_submit(self, key: str) -> bool:
        return key in self._replay_io_submits

    def get_replay_io_drain(self, key: str) -> IoResultRecord | None:
        return self._replay_io_drains.get(key)

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

        # Within-scan reads use the pending dicts, so publishing can wait until
        # the final value of each key is known.  Avoid touching the PMap for
        # writes that finish equal to the immutable scan base; otherwise apply
        # all genuinely changed values through one deferred evolver.
        new_tags = _commit_changed(self._state_tags, self._tags_pending)
        new_memory = _commit_changed(self._state_memory, self._memory_pending)

        # Create new state with updated tags/memory and advance scan
        new_state = self._state.set(tags=new_tags, memory=new_memory)
        return new_state.next_scan(dt=dt)
