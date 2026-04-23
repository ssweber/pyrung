"""PLC - Generator-driven PLC execution engine.

The runner orchestrates scan cycle execution with inversion of control.
The consumer drives execution via step(), allowing input injection,
inspection, and pause at any point.

Uses ScanContext to batch all tag/memory updates within a scan cycle,
reducing object allocation from O(instructions) to O(1) per scan.
"""

from __future__ import annotations

import time
import warnings
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping
from contextlib import contextmanager
from contextvars import Token
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal, TypeGuard

from pyrsistent import PMap

from pyrung.core.bounds import BoundsViolation, build_constraint_index, check_bounds
from pyrung.core.compiled_plc import CompiledPLC
from pyrung.core.condition_trace import ConditionTraceEngine
from pyrung.core.context import ConditionView, ScanContext
from pyrung.core.debug_trace import RungTrace, RungTraceEvent, TraceEvent
from pyrung.core.debugger import PLCDebugger
from pyrung.core.history import History
from pyrung.core.input_overrides import InputOverrideManager
from pyrung.core.kernel import CompiledKernel
from pyrung.core.live_binding import reset_active_runner, set_active_runner
from pyrung.core.rung_firings import RungFiringTimelines
from pyrung.core.scan_log import LifecycleEvent, LifecycleKind, ScanLog, ScanLogSnapshot
from pyrung.core.state import SystemState
from pyrung.core.system_points import (
    _BATTERY_PRESENT_KEY,
    _MODE_RUN_KEY,
    READ_ONLY_SYSTEM_TAG_NAMES,
    SYSTEM_TAGS_BY_NAME,
    SystemPointRuntime,
)
from pyrung.core.time_mode import TimeMode
from pyrung.core.trace_formatter import TraceFormatter
from pyrung.core.validation._common import _collect_write_sites
from pyrung.core.validation.readonly_write import _any_write_targets

if TYPE_CHECKING:
    from collections.abc import Generator, Iterator

    from pyrung.core.analysis.causal import CausalChain
    from pyrung.core.condition import Condition
    from pyrung.core.rung import Rung
    from pyrung.core.tag import Tag

_SENTINEL = object()  # distinguishes "not passed" from None/False

_CHECKPOINT_INTERVAL_DEFAULT = 200

# Byte budget for the recent-state cache (default for ``history_budget``).
_HISTORY_BUDGET_BYTES_DEFAULT = 100 * 1024 * 1024  # 100 MB

# Floor: never evict below this many entries regardless of byte budget.
# Monitor ``previous_value`` / ``_prev:*`` reads assume N-1 is always
# present; the recent-state cache floor must not regress under budget pressure.
_RECENT_STATE_CACHE_MIN_ENTRIES = 20


def _parse_retention(value: str | int | None, dt_seconds: float) -> int | None:
    """Convert a retention parameter to a scan count.

    Accepts ``None`` (unlimited), ``int`` (literal scan count), or a
    duration string parseable by ``parse_duration`` (e.g. ``"1h"``).
    """
    if value is None:
        return None
    if isinstance(value, int):
        return value
    from pyrung.core.physical import parse_duration

    ms = parse_duration(value)
    dt_ms = dt_seconds * 1000.0
    return max(1, int(ms / dt_ms))


# Conservative per-entry byte estimate for PMap HAMT nodes.
_PER_PMAP_ENTRY_BYTES = 200

# Fixed overhead for PRecord shell (scan_id, timestamp, two PMap roots).
_STATE_BASE_BYTES = 200


def _estimate_state_bytes(state: SystemState) -> int:
    """Conservative ceiling estimate of a ``SystemState``'s memory footprint.

    Uses PMap entry count times a fixed per-entry constant.  States are
    structurally shared, so summing independent sizes would massively
    overcount; this estimator is deliberately coarse.
    """
    entries = len(state.tags) + len(state.memory)
    return _STATE_BASE_BYTES + entries * _PER_PMAP_ENTRY_BYTES


def _validate_assume(logic: list[Any], assume: dict[str, Any]) -> None:
    """Raise ``ValueError`` if *assume* targets a readonly tag."""
    from pyrung.core.analysis.query import find_tag_object

    for name in assume:
        tag_obj = find_tag_object(logic, name)
        if tag_obj is not None and tag_obj.readonly:
            raise ValueError(f"cannot assume value for readonly tag {name!r}")


@dataclass(frozen=True)
class ScanStep:
    """Debug scan step emitted at rung boundaries."""

    rung_index: int
    rung: Rung
    ctx: ScanContext
    kind: Literal["rung", "branch", "subroutine", "instruction"]
    subroutine_name: str | None
    depth: int
    call_stack: tuple[str, ...]
    source_file: str | None
    source_line: int | None
    end_line: int | None
    enabled_state: Literal["enabled", "disabled_local", "disabled_parent"] | None
    trace: TraceEvent | None
    instruction_kind: str | None


@dataclass
class _MonitorRegistration:
    id: int
    tag_name: str
    callback: Callable[[Any, Any], None]
    enabled: bool = True
    removed: bool = False


@dataclass
class _BreakpointRegistration:
    id: int
    predicate: Callable[[SystemState], bool]
    action: Literal["pause", "snapshot"]
    label: str | None = None
    enabled: bool = True
    removed: bool = False


class _RunnerHandle:
    """Mutable registration handle used by monitor/breakpoint APIs."""

    __slots__ = ("_id", "_remove", "_enable", "_disable")

    def __init__(
        self,
        *,
        handle_id: int,
        remove: Callable[[int], None],
        enable: Callable[[int], None],
        disable: Callable[[int], None],
    ) -> None:
        self._id = handle_id
        self._remove = remove
        self._enable = enable
        self._disable = disable

    @property
    def id(self) -> int:
        return self._id

    def remove(self) -> None:
        self._remove(self._id)

    def enable(self) -> None:
        self._enable(self._id)

    def disable(self) -> None:
        self._disable(self._id)


class _BreakpointBuilder:
    """Fluent builder returned by ``runner.when(...)``."""

    __slots__ = ("_runner", "_predicate")

    def __init__(self, runner: PLC, predicate: Callable[[SystemState], bool]) -> None:
        self._runner = runner
        self._predicate = predicate

    def pause(self) -> _RunnerHandle:
        return self._runner._register_breakpoint(
            predicate=self._predicate,
            action="pause",
            label=None,
        )

    def snapshot(self, label: str) -> _RunnerHandle:
        return self._runner._register_breakpoint(
            predicate=self._predicate,
            action="snapshot",
            label=label,
        )


def _iter_referenced_tags(root: Any) -> tuple[Tag, ...]:
    """Collect Tag objects reachable from a logic object graph."""
    from pyrung.core.tag import Tag as TagClass

    found_by_name: dict[str, TagClass] = {}
    visited: set[int] = set()
    queue: list[Any] = [root]

    while queue:
        current = queue.pop()
        if current is None:
            continue
        if isinstance(current, TagClass):
            found_by_name[current.name] = current
            continue
        if isinstance(current, (str, bytes, bytearray, int, float, bool)):
            continue

        current_id = id(current)
        if current_id in visited:
            continue
        visited.add(current_id)

        if isinstance(current, Mapping):
            queue.extend(current.keys())
            queue.extend(current.values())
            continue
        if isinstance(current, tuple | list | set | frozenset):
            queue.extend(current)
            continue

        if hasattr(current, "__dict__"):
            queue.extend(vars(current).values())
            continue
        if hasattr(current, "__slots__"):
            for slot in current.__slots__:
                if slot in {"__weakref__", "__dict__"}:
                    continue
                if hasattr(current, slot):
                    queue.append(getattr(current, slot))

    return tuple(found_by_name.values())


def _apply_lifecycle_to_replay(replay: Any, event: LifecycleEvent) -> None:
    """Apply a captured lifecycle event to a replay PLC.

    ``reboot`` never appears in a live log snapshot because
    ``PLC.reboot()`` resets the scan log — surfacing it here flags a
    regression in that invariant.
    """
    if event.kind == "stop":
        replay.stop()
    elif event.kind == "battery_present":
        replay.battery_present = bool(event.value)
    elif event.kind == "clear_forces":
        replay.clear_forces()
    elif event.kind == "reboot":
        raise AssertionError(
            "unexpected reboot lifecycle event in replay log "
            "(reboot() should reset the log — see PLC.reboot())"
        )
    else:  # pragma: no cover - exhaustive
        raise AssertionError(f"unknown lifecycle kind: {event.kind!r}")


def _program_writes_read_only_system_tags(program: Any) -> bool:
    from pyrung.core.program import Program

    if not isinstance(program, Program):
        return False
    for site in _collect_write_sites(program, target_extractor=_any_write_targets):
        if site.target_name in READ_ONLY_SYSTEM_TAG_NAMES:
            return True
    return False


def _looks_like_compiled_replay_gap(exc: Exception) -> bool:
    if isinstance(exc, NotImplementedError):
        return True
    if not isinstance(exc, ValueError | TypeError):
        return False
    message = str(exc)
    return any(
        needle in message
        for needle in (
            "requires generate_circuitpy",
            "Could not inspect source for callable",
            "Unsupported",
        )
    )


class _DebugNamespace:
    """Namespace exposing debugger-facing methods on ``plc.debug``."""

    __slots__ = ("_plc",)

    def __init__(self, plc: PLC) -> None:
        self._plc = plc

    # -- DebugRunner protocol (used by PLCDebugger) --

    def prepare_scan(self) -> tuple[ScanContext, float]:
        return self._plc._prepare_scan()

    def commit_scan(self, ctx: ScanContext, dt: float) -> None:
        self._plc._commit_scan(ctx, dt)

    def iter_top_level_rungs(self) -> Iterable[Rung]:
        return self._plc._logic

    def evaluate_condition_value(
        self,
        condition: Any,
        ctx: ScanContext | ConditionView,
    ) -> tuple[bool, list[dict[str, Any]]]:
        return self._plc._evaluate_condition_value(condition, ctx)

    def condition_term_text(self, condition: Any, details: list[dict[str, Any]]) -> str:
        return self._plc._condition_term_text(condition, details)

    def condition_annotation(self, *, status: str, expression: str, summary: str) -> str:
        return self._plc._condition_annotation(
            status=status, expression=expression, summary=summary
        )

    def condition_expression(self, condition: Any) -> str:
        return self._plc._condition_expression(condition)

    # -- Public debug surface --

    def scan_steps(self) -> Generator[tuple[int, Rung, ScanContext], None, None]:
        """Execute one scan cycle yielding after each rung."""
        return self._plc._scan_steps()

    def scan_steps_debug(self) -> Generator[ScanStep, None, None]:
        """Execute one scan cycle yielding fine-grained debug steps."""
        return self._plc._scan_steps_debug()

    def rung_trace(self, rung_id: int) -> RungTrace:
        """Return rung-level debug trace for the most recently committed scan.

        Only the most recent debug scan's traces are retained — historical
        scan inspection is not supported.
        """
        return self._plc._inspect(rung_id)

    def rung_firings(self, scan_id: int | None = None) -> PMap:
        """Return rung firings for the given scan (default: playhead)."""
        return self._plc.rung_firings(scan_id)

    def last_event(self) -> tuple[int, int, RungTraceEvent] | None:
        """Return the latest debug-trace event."""
        return self._plc._inspect_event()

    @property
    def system_runtime(self) -> SystemPointRuntime:
        """System point runtime component."""
        return self._plc._system_runtime


class PLC:
    """Generator-driven PLC execution engine.

    Executes PLC logic as pure functions: Logic(state) -> new_state.
    The consumer controls execution via step(), enabling:
    - Input injection via patch()
    - Inspection of retained historical state via runner.history
    - Pause/resume at any scan boundary

    Attributes:
        current_state: The current SystemState snapshot.
        history: Query interface for retained SystemState snapshots.
        simulation_time: Current simulation clock (seconds).
        time_mode: Current time mode (FIXED_STEP or REALTIME).
    """

    def __init__(
        self,
        logic: list[Any] | Any = None,
        initial_state: SystemState | None = None,
        *,
        dt: float | None = None,
        realtime: bool = False,
        history: str | int | None = None,
        cache: str | int | None = None,
        history_budget: int | None = None,
        checkpoint_interval: int | None = None,
        record_all_tags: bool = False,
    ) -> None:
        """Create a new PLC.

        Args:
            logic: Program, list of rungs, or None for empty logic.
            initial_state: Starting state. Defaults to SystemState().
            dt: Time delta per scan in seconds (default 0.010).
                Only used in fixed-step mode.
            realtime: Use wall-clock timing instead of fixed step.
                Mutually exclusive with dt.
            history: Retention window for the scan log, checkpoints,
                and firing timelines.  Duration string (``"1h"``,
                ``"30m"``), scan count (int), or ``None`` (unlimited).
            cache: Instant-lookup window for full ``SystemState``
                snapshots.  Same formats as *history*.  ``None`` uses
                byte-budget-only eviction.
            history_budget: Byte ceiling for the recent-state cache.
                Defaults to 100 MB.  Raises ``ValueError`` below 1 MB.
            checkpoint_interval: Number of scans between replay checkpoints.
                Defaults to ``_CHECKPOINT_INTERVAL_DEFAULT``.
            record_all_tags: Bypass the PDG-based rung-firing capture
                filter.  By default the firing log drops writes to tags
                that no rung reads, since the simulator's analysis APIs
                don't need them.  Set this to True when a diagnostic
                session needs the unfiltered firing history (e.g. when
                the PDG is suspected of misclassifying a consumer).
        """
        if realtime and dt is not None:
            raise ValueError("Cannot specify dt= with realtime=True")
        if dt is None:
            dt = 0.010
        if checkpoint_interval is None:
            checkpoint_interval = _CHECKPOINT_INTERVAL_DEFAULT
        if checkpoint_interval < 1:
            raise ValueError("checkpoint_interval must be >= 1")
        if history_budget is None:
            history_budget = _HISTORY_BUDGET_BYTES_DEFAULT
        if history_budget < 1_048_576:
            raise ValueError("history_budget must be >= 1 MB (1048576)")

        history_scans = _parse_retention(history, dt)
        cache_scans = _parse_retention(cache, dt)
        history_floor = checkpoint_interval * 2
        if history_scans is not None:
            history_scans = max(history_scans, history_floor)
        if cache_scans is not None:
            cache_scans = max(cache_scans, _RECENT_STATE_CACHE_MIN_ENTRIES)
            if history_scans is not None:
                cache_scans = min(cache_scans, history_scans)
        self._history_retention_scans: int | None = history_scans
        self._cache_retention_scans: int | None = cache_scans

        self._logic: list[Rung]
        self._program: Any = None
        # Handle different logic types
        # Import Program here to avoid circular import at module level
        from pyrung.core.program import Program

        if logic is None:
            self._logic = []
        elif isinstance(logic, Program):
            self._logic = logic.rungs
            self._program = logic
        elif isinstance(logic, list):
            self._logic = logic
        else:
            self._logic = [logic]

        self._state = initial_state if initial_state is not None else SystemState()
        self._running = True
        self._battery_present = True
        self._state = self._apply_runtime_memory_flags(
            self._state,
            mode_run=self._running,
            battery_present=self._battery_present,
        )
        # Byte-bounded recent-state cache feeding ``History.at()`` on
        # the hot path.  ``History`` itself is a stateless facade.
        # Keyed by scan_id → (SystemState, estimated_bytes).
        self._recent_state_cache_budget = history_budget
        self._recent_state_cache: OrderedDict[int, tuple[SystemState, int]] = OrderedDict()
        self._recent_state_cache_bytes = 0
        self._cache_state(self._state)
        # The "implicit anchor" for replay walks below the earliest
        # checkpoint.  Pinned at construction and refreshed only on
        # ``_reset_runtime_scope`` (reboot), so it remains valid even
        # after the recent-state window rotates past the initial scan.
        self._initial_scan_id: int = self._state.scan_id
        self._initial_state: SystemState = self._state
        self._history = History(self)
        self._current_rung_traces: dict[int, RungTrace] = {}
        self._current_rung_traces_scan_id: int | None = None
        # Per-rung range-encoded firing timelines.  Replaces the
        # ``scan_id -> PMap`` shape that paid one dict entry per scan
        # for every firing rung; now a stable rung costs one range
        # regardless of how long it fires, and a period-2 alternator
        # collapses into a single ``AlternatingRun``.
        self._rung_firing_timelines = RungFiringTimelines()
        self._inflight_scan_id: int | None = None
        self._inflight_rung_events: dict[int, list[RungTraceEvent]] = {}
        self._latest_inflight_trace_event: tuple[int, int, RungTraceEvent] | None = None
        self._latest_committed_trace_event: tuple[int, int, RungTraceEvent] | None = None
        self._playhead = self._state.scan_id
        self._dt = dt
        self._last_step_time: float | None = None
        if realtime:
            self._time_mode = TimeMode.REALTIME
            self._last_step_time = time.perf_counter()
        else:
            self._time_mode = TimeMode.FIXED_STEP
        self._scan_log = ScanLog(time_mode=self._time_mode)
        self._checkpoint_interval = checkpoint_interval
        self._checkpoints: dict[int, SystemState] = {}
        self._forces_last_recorded: dict[str, bool | int | float | str] = {}
        self._this_scan_drained_patches: dict[str, bool | int | float | str] = {}
        # Replay plumbing. ``_dt_override_for_next_scan`` lets
        # ``replay_to`` inject the recorded dt for each replayed scan in
        # REALTIME mode; ``_replay_mode`` suppresses user monitors,
        # breakpoint labels, and the RTC setter during a replay walk.
        self._dt_override_for_next_scan: float | None = None
        self._replay_mode: bool = False
        self._compiled_replay_kernel: CompiledKernel | None | bool = None
        # PDG-filtered rung-firing capture.  When the filter is active
        # (``record_all_tags=False``), ``capturing_rung`` drops writes to
        # tags that no rung reads — the firing log is consumed only by
        # ``cause``/``effect``/``query`` which never ask about an unread
        # tag.  Populated lazily alongside ``_pdg_cache`` the first time
        # a scan captures; rebuilt atomically whenever the PDG is
        # invalidated.
        self._record_all_tags: bool = record_all_tags
        self._pdg_consumed_tags: frozenset[str] | None = None
        # One-slot cache for ``replay_trace_at``.  Reconstructing rung
        # traces for a historical scan costs one fork + up to K plain
        # scans + one debug scan; caching a back-to-back repeat query
        # (common when the user hovers/expands a scan in the UI) saves
        # that work.  Cleared on tip advance (``_run_single_scan``) and
        # on any reset that invalidates the log (reboot, stop→run).
        self._cached_replay_trace: tuple[int, dict[int, RungTrace]] | None = None
        self._rtc_base = self._normalize_rtc_datetime(datetime.now())
        self._rtc_base_sim_time = float(self._state.timestamp)
        self._system_runtime = SystemPointRuntime(
            time_mode_getter=lambda: self._time_mode,
            fixed_step_dt_getter=lambda: self._dt,
            rtc_now_getter=self._rtc_at_sim_time,
            rtc_setter=self._set_rtc_and_record,
        )
        self._input_overrides = InputOverrideManager(is_read_only=self._system_runtime.is_read_only)
        # Preserve direct access used in tests/live-tag helpers.
        self._pending_patches = self._input_overrides.pending_patches
        self._forces = self._input_overrides.forces_mutable
        self._condition_trace = ConditionTraceEngine(formatter=TraceFormatter())
        self._debugger = PLCDebugger(step_factory=ScanStep)
        self._debug_ns = _DebugNamespace(self)
        self._next_debug_handle_id = 1
        self._monitors_by_id: dict[int, _MonitorRegistration] = {}
        self._breakpoints_by_id: dict[int, _BreakpointRegistration] = {}
        self._pause_requested_this_scan = False
        self._active_tokens: list[Token[PLC | None]] = []
        self._pre_scan_callbacks: list[Any] = []
        self._known_tags_by_name: dict[str, Tag] = {}
        self._refresh_known_tags_from_logic()
        self._constrained_tags = build_constraint_index(self._known_tags_by_name)
        self._bounds_violations: dict[str, BoundsViolation] = {}
        # Seed initial state with tag defaults (skip tags already in state).
        seed = {
            t.name: t.default
            for t in self._known_tags_by_name.values()
            if t.name not in self._state.tags
        }
        if seed:
            self._state = self._state.with_tags(seed)
            self._reset_cache(self._state)
            self._initial_state = self._state

    @property
    def program(self) -> Any:
        """The Program object if the PLC was constructed from one, else None."""
        return self._program

    @property
    def current_state(self) -> SystemState:
        """Current state snapshot."""
        return self._state

    @property
    def bounds_violations(self) -> dict[str, BoundsViolation]:
        """Constraint violations from the most recent scan, if any."""
        return self._bounds_violations

    @property
    def history(self) -> History:
        """Read-only history query surface."""
        return self._history

    @property
    def playhead(self) -> int:
        """Current scan id used for inspection/time-travel queries."""
        return self._playhead

    def seek(self, scan_id: int) -> SystemState:
        """Move playhead to a retained scan and return that snapshot."""
        state = self.history.at(scan_id)
        self._playhead = state.scan_id
        return state

    def rewind(self, seconds: float) -> SystemState:
        """Move playhead backward in time by ``seconds`` and return snapshot."""
        if seconds < 0:
            raise ValueError("seconds must be >= 0")

        playhead_state = self.history.at(self._playhead)
        target_timestamp = playhead_state.timestamp - seconds

        target_state = self.history.at_or_before_timestamp(target_timestamp)
        if target_state is None:
            target_state = self.history.at(self.history.oldest_scan_id)

        self._playhead = target_state.scan_id
        return target_state

    def rung_firings(self, scan_id: int | None = None) -> PMap:
        """Return rung firings for the given scan (default: playhead).

        Returns ``PMap[int, PMap[str, Any]]`` mapping each rung index
        that fired (had any write, even if all were filtered by PDG)
        during the scan to the filtered ``{tag_name: value_written}``
        map.  Synthesized on demand from per-rung range-encoded
        timelines (:class:`RungFiringTimelines`); rungs with no
        timeline covering ``scan_id`` contribute nothing.

        Populated uniformly by both the non-debug (``step()`` /
        ``run()``) and debug (DAP ``pyrungStepScan`` / continue) scan
        paths via ``ScanContext.capturing_rung``.

        .. todo::

            A rung whose condition is True but whose writes are identical to
            the already-pending values will not appear here.  This is an
            acceptable approximation for causal-chain attribution; for
            accurate cold-rung detection a ``_last_condition_result`` field
            on ``Rung`` may be needed later.
        """
        target = self._playhead if scan_id is None else scan_id
        return self._rung_firing_timelines.at(target)

    def diff(self, scan_a: int, scan_b: int) -> dict[str, tuple[Any, Any]]:
        """Return changed tag values between two retained historical scans."""
        state_a = self.history.at(scan_a)
        state_b = self.history.at(scan_b)

        changed: dict[str, tuple[Any, Any]] = {}
        all_keys = sorted(set(state_a.tags.keys()) | set(state_b.tags.keys()))
        for key in all_keys:
            old_value = state_a.tags.get(key)
            new_value = state_b.tags.get(key)
            if old_value != new_value:
                changed[key] = (old_value, new_value)
        return changed

    def _ensure_pdg(self) -> Any:
        """Lazily build and cache the static program dependency graph.

        Also populates ``_pdg_consumed_tags`` — every tag any rung
        reads, combining condition-reads and data-reads via
        ``readers_of`` — unioned with every Bool-typed tag the PDG
        knows about.  The capture-layer filter keeps all Bools regardless
        of read/write role: they're low-cardinality and usually the
        tags users ask ``cause()`` about, so the direct-log path stays
        cheap for them.  Non-Bool churn (Timer.acc et al.) still gets
        filtered.  Any future site that invalidates ``_pdg_cache`` must
        clear ``_pdg_consumed_tags`` together — the two caches share a
        lifetime.
        """
        if not hasattr(self, "_pdg_cache") or self._pdg_cache is None:
            from pyrung.core.analysis.pdg import build_program_graph
            from pyrung.core.program import Program
            from pyrung.core.tag import TagType

            program = self._program
            if program is None:
                program = Program.__new__(Program)
                program.rungs = list(self._logic)
                program.subroutines = {}
            self._pdg_cache = build_program_graph(program)
            consumed = set(self._pdg_cache.readers_of.keys())
            # Union in every Bool-typed tag the PDG observed.  A mixed
            # rung (e.g. ``out(BoolFlag) + Timer.acc``) keeps the Bool
            # write intact while the counter acc still drops — the
            # intern pool stays small (only Bool patterns) so the rung
            # never hits the fired-only threshold and causal chains on
            # the Bool flag remain intact indefinitely.
            for name, tag in self._pdg_cache.tags.items():
                if getattr(tag, "type", None) == TagType.BOOL:
                    consumed.add(name)
            self._pdg_consumed_tags = frozenset(consumed)
        return self._pdg_cache

    def _consumed_tags_for_capture(self) -> frozenset[str] | None:
        """Capture-worthy tag set for ``ScanContext.capturing_rung``
        to filter against, or ``None`` when the filter should be
        bypassed.

        The set is consumed-tags (per PDG ``readers_of``) unioned with
        every Bool-typed tag — see :meth:`_ensure_pdg`.  ``None``
        bypasses the filter entirely — used for the
        ``record_all_tags=True`` escape hatch, for logic-less PLCs (no
        rung = no consumer, filter would silently drop every write),
        and for programs with rungs the PDG cannot model (synthetic
        test rungs that only implement ``evaluate(ctx)``).  Otherwise
        the PDG is built lazily on first call; every subsequent
        invocation is a single attribute read.
        """
        if self._record_all_tags or not self._logic:
            return None
        if self._pdg_consumed_tags is None:
            from pyrung.core.rung import Rung as RungClass

            if not all(isinstance(rung, RungClass) for rung in self._logic):
                return None
            self._ensure_pdg()
        return self._pdg_consumed_tags

    def cause(
        self,
        tag: Tag | str,
        scan: int | None = None,
        *,
        to: Any = _SENTINEL,
        assume: dict[str, Any] | None = None,
    ) -> CausalChain | None:
        """Explain what caused a tag to transition.

        **Recorded** (default, ``to`` omitted): walks recorded history
        backward from the transition.  Returns ``None`` if no transition
        was found.

        **Projected** (``to=value``): projects forward from the current
        state, finding reachable paths that would drive the tag to *value*.
        Returns a ``CausalChain`` with ``mode='projected'`` (reachable) or
        ``mode='unreachable'`` (stranded, with ``blockers``).  Never
        returns ``None`` in projected mode.

        Args:
            tag: Tag object or tag name string.
            scan: Specific scan to examine (recorded mode only).
            to: Target value for projected mode.  When provided, the
                method returns the path that would drive *tag* to this
                value from the current state.
            assume: Tag-to-value overrides for projected mode.  Pins
                the given tags to specified values during analysis.
                Raises ``ValueError`` if used without ``to=`` or if
                any key is a ``readonly`` tag.

        Returns:
            A :class:`~pyrung.core.analysis.causal.CausalChain`, or ``None``
            (recorded mode only, when no transition was found).
        """
        if assume and to is _SENTINEL:
            raise ValueError("assume= requires projected mode (provide to=)")

        if to is not _SENTINEL:
            if assume:
                _validate_assume(self._logic, assume)
            from pyrung.core.analysis.causal import projected_cause

            return projected_cause(
                logic=self._logic,
                history=self._history,
                tag=tag,
                to_value=to,
                pdg=self._ensure_pdg(),
                assume=assume,
                timelines=self._rung_firing_timelines,
            )

        from pyrung.core.analysis.causal import recorded_cause

        return recorded_cause(
            logic=self._logic,
            history=self._history,
            rung_firings_fn=self.rung_firings,
            tag=tag,
            scan_id=scan,
            pdg=self._ensure_pdg() if self._logic else None,
            timelines=self._rung_firing_timelines,
            state_in_cache_fn=self._state_in_cache,
        )

    def effect(
        self,
        tag: Tag | str,
        scan: int | None = None,
        *,
        from_: Any = _SENTINEL,
        assume: dict[str, Any] | None = None,
        steady_state_k: int = 3,
        max_scans: int = 1000,
    ) -> CausalChain | None:
        """Trace the downstream effects of a tag transition.

        **Recorded** (default, ``from_`` omitted): walks recorded
        history forward from an actual transition.  Returns ``None`` if
        no transition was found.

        **Projected** (``from_=value``): what-if analysis — if the tag
        transitioned from *value* right now, what downstream effects would
        follow?  Returns ``mode='projected'`` (possibly empty steps for
        dead-end) or ``mode='unreachable'`` if the trigger can't fire.
        Never returns ``None`` in projected mode.

        Args:
            tag: Tag object or tag name string.
            scan: Specific scan of the transition (recorded mode only).
            from_: Current value for projected what-if analysis.  For
                Bool tags the TO value is inferred as ``not from_``.
            assume: Tag-to-value overrides for projected mode.  Pins
                the given tags to specified values during analysis.
                Raises ``ValueError`` if used without ``from_=`` or if
                any key is a ``readonly`` tag.
            steady_state_k: Stop after this many consecutive scans with no
                new effects (recorded mode only, default 3).
            max_scans: Hard cap on forward scans (recorded mode only,
                default 1000).

        Returns:
            A :class:`~pyrung.core.analysis.causal.CausalChain`, or ``None``
            (recorded mode only, when no transition was found).
        """
        if assume and from_ is _SENTINEL:
            raise ValueError("assume= requires projected mode (provide from_=)")

        if from_ is not _SENTINEL:
            if assume:
                _validate_assume(self._logic, assume)
            from pyrung.core.analysis.causal import projected_effect

            return projected_effect(
                logic=self._logic,
                history=self._history,
                tag=tag,
                from_value=from_,
                pdg=self._ensure_pdg(),
                assume=assume,
            )

        from pyrung.core.analysis.causal import recorded_effect

        return recorded_effect(
            logic=self._logic,
            history=self._history,
            rung_firings_fn=self.rung_firings,
            tag=tag,
            scan_id=scan,
            steady_state_k=steady_state_k,
            max_scans=max_scans,
            pdg=self._ensure_pdg() if self._logic else None,
            timelines=self._rung_firing_timelines,
        )

    def recovers(self, tag: Tag | str, *, assume: dict[str, Any] | None = None) -> bool:
        """True if *tag* has a reachable clear path from the current state.

        Convenience predicate: ``cause(tag, to=resting).mode != 'unreachable'``.
        For the underlying chain (witness or blockers), call ``cause()`` directly.

        Tags marked ``external=True`` always return True — the recovery path
        exists outside the ladder by declaration.  When *assume* is provided
        the external shortcut is skipped so the analysis runs with the given
        overrides.

        Args:
            tag: Tag object or tag name string.
            assume: Tag-to-value overrides.  Pins the given tags to
                specified values during projected analysis.
        """
        from pyrung.core.analysis.query import find_tag_object
        from pyrung.core.tag import Tag as TagClass

        if isinstance(tag, TagClass):
            tag_obj = tag
            resting = tag.default
        else:
            tag_obj = find_tag_object(self._logic, tag)
            resting = self._resolve_resting_value(tag)

        # External tags recover by declaration — the external writer
        # handles it.  Skip when assume is provided so the caller can
        # exercise the actual recovery path.
        if not assume and tag_obj is not None and tag_obj.external:
            return True

        chain = self.cause(tag, to=resting, assume=assume)
        assert chain is not None  # projected mode never returns None
        return chain.mode != "unreachable"

    def _resolve_resting_value(self, tag_name: str) -> Any:
        """Resolve the resting (default) value for a tag name by searching logic."""
        from pyrung.core.analysis.query import find_tag_object

        tag_obj = find_tag_object(self._logic, tag_name)
        return tag_obj.default if tag_obj is not None else False

    @property
    def dataview(self) -> Any:
        """Chainable query over this program's tag dependency graph.

        Convenience shorthand for ``plc.program.dataview()`` — builds
        (and caches) the static program graph lazily on first access.
        """
        from pyrung.core.analysis.dataview import DataView

        return DataView.from_graph(self._ensure_pdg())

    @property
    def query(self) -> Any:
        """Survey namespace for whole-program dynamic analysis."""
        from pyrung.core.analysis.query import QueryNamespace

        return QueryNamespace(self)

    def _inspect(self, rung_id: int) -> RungTrace:
        """Return rung-level debug trace for the most recently committed scan.

        Only the most recent debug scan's traces are retained.

        Raises:
            KeyError: No debug trace for the current scan, or no trace for
                the requested rung.
        """
        if self._current_rung_traces_scan_id is None:
            raise KeyError(rung_id)

        try:
            return self._current_rung_traces[rung_id]
        except KeyError as exc:
            raise KeyError(rung_id) from exc

    def _inspect_event(self) -> tuple[int, int, RungTraceEvent] | None:
        """Return the latest debug-trace event for active/committed debug-path scans.

        Returns:
            A tuple of ``(scan_id, rung_id, event)``. In-flight debug-scan events
            are preferred when available. Otherwise, the latest retained committed
            debug-scan event is returned.

        Notes:
            - This API is populated by ``scan_steps_debug()`` only.
            - Scans produced through ``step()/run()/run_for()/run_until()`` do not
              contribute trace events here.
        """
        inflight = self._latest_inflight_trace_event
        if self._inflight_scan_id is not None and inflight is not None:
            return inflight

        committed = self._latest_committed_trace_event
        if committed is None:
            return None

        scan_id, rung_id, _event = committed
        if scan_id != self._current_rung_traces_scan_id:
            self._latest_committed_trace_event = None
            return None
        trace = self._current_rung_traces.get(rung_id)
        if trace is None:
            self._latest_committed_trace_event = None
            return None

        if not trace.events:
            self._latest_committed_trace_event = None
            return None

        latest_event = trace.events[-1]
        self._latest_committed_trace_event = (scan_id, rung_id, latest_event)
        return self._latest_committed_trace_event

    def fork(self, scan_id: int | None = None) -> PLC:
        """Create an independent runner from retained historical state.

        Args:
            scan_id: Snapshot to fork from. Defaults to current committed tip state.
        """
        target_scan_id = self._state.scan_id if scan_id is None else scan_id
        historical_state = self._state_at(target_scan_id)
        fork = PLC(
            logic=self._program if self._program is not None else list(self._logic),
            initial_state=historical_state,
            history=self._history_retention_scans,
            cache=self._cache_retention_scans,
            history_budget=self._recent_state_cache_budget,
            checkpoint_interval=self._checkpoint_interval,
            record_all_tags=self._record_all_tags,
        )
        fork._set_time_mode(self._time_mode, dt=self._dt)
        parent_rtc_at_fork_point = self._system_runtime._rtc_now(historical_state)
        fork._set_rtc_internal(parent_rtc_at_fork_point, fork.current_state.timestamp)
        return fork

    def fork_from(self, scan_id: int) -> PLC:
        """Create an independent runner from a retained historical snapshot."""
        return self.fork(scan_id=scan_id)

    def _state_at(self, scan_id: int) -> SystemState:
        """Return the ``SystemState`` for ``scan_id`` without recursing
        through ``History.at()``.

        Used by ``fork()`` and ``History.at()`` to avoid the
        ``replay_to → fork → history.at → replay_to`` loop.  Direct
        lookups (current tip, recent-state window, checkpoint dict,
        the pinned initial state) terminate immediately; the
        replay-reconstruction fallback only fires for scans that fall
        between addressable anchors.
        """
        if scan_id == self._state.scan_id:
            return self._state
        entry = self._recent_state_cache.get(scan_id)
        if entry is not None:
            return entry[0]
        if scan_id in self._checkpoints:
            return self._checkpoints[scan_id]
        if scan_id == self._initial_scan_id:
            return self._initial_state
        if self._initial_scan_id <= scan_id <= self._state.scan_id:
            return self.replay_to(scan_id).current_state
        raise KeyError(scan_id)

    def _cache_state(self, state: SystemState) -> None:
        """Add *state* to the recent-state cache, evicting if over budget."""
        est = _estimate_state_bytes(state)
        self._recent_state_cache[state.scan_id] = (state, est)
        self._recent_state_cache_bytes += est
        min_scan = (
            state.scan_id - self._cache_retention_scans
            if self._cache_retention_scans is not None
            else -1
        )
        while len(self._recent_state_cache) > _RECENT_STATE_CACHE_MIN_ENTRIES:
            oldest_sid = next(iter(self._recent_state_cache))
            over_budget = self._recent_state_cache_bytes > self._recent_state_cache_budget
            over_time = oldest_sid < min_scan
            if not (over_budget or over_time):
                break
            _, (_, evicted_est) = self._recent_state_cache.popitem(last=False)
            self._recent_state_cache_bytes -= evicted_est

    def _reset_cache(self, state: SystemState) -> None:
        """Clear cache and seed with a single *state*."""
        self._recent_state_cache.clear()
        self._recent_state_cache_bytes = 0
        self._cache_state(state)

    def _state_in_cache(self, scan_id: int) -> bool:
        """True if *scan_id* is in the recent-state cache."""
        return scan_id in self._recent_state_cache

    def _cache_oldest_scan_id(self) -> int | None:
        """Scan_id of the oldest cached entry, or None if empty."""
        if not self._recent_state_cache:
            return None
        return next(iter(self._recent_state_cache))

    def _nearest_checkpoint_at_or_before(self, scan_id: int) -> int | None:
        """Largest retained checkpoint scan_id <= ``scan_id``, or None."""
        return max((c for c in self._checkpoints if c <= scan_id), default=None)

    def _nearest_checkpoint_at_or_after(self, scan_id: int) -> int | None:
        """Smallest retained checkpoint scan_id >= ``scan_id``, or None."""
        return min((c for c in self._checkpoints if c >= scan_id), default=None)

    def _trim_history_before(self, scan_id: int) -> None:
        """Advance the replay horizon to *scan_id*.

        Trims the scan log, rung-firing timelines, and checkpoints in
        lockstep and advances ``_initial_scan_id`` so that
        ``History.oldest_scan_id`` / ``contains()`` / ``scan_ids()``
        reflect the narrowed range.
        """
        self._scan_log.trim_before(scan_id)
        self._rung_firing_timelines.trim_before(scan_id)
        for cp in [k for k in self._checkpoints if k < scan_id]:
            del self._checkpoints[cp]
        if scan_id > self._initial_scan_id:
            self._initial_scan_id = scan_id
            if scan_id in self._checkpoints:
                self._initial_state = self._checkpoints[scan_id]
            while self._recent_state_cache:
                oldest_sid = next(iter(self._recent_state_cache))
                if oldest_sid >= scan_id:
                    break
                _, (_, evicted_est) = self._recent_state_cache.popitem(last=False)
                self._recent_state_cache_bytes -= evicted_est

    def _compiled_replay_supported_kernel(self) -> CompiledKernel | None:
        from pyrung.circuitpy.codegen import compile_kernel

        cached = self._compiled_replay_kernel
        if isinstance(cached, CompiledKernel):
            return cached
        if cached is False:
            return None
        if self._time_mode != TimeMode.FIXED_STEP or self._program is None:
            self._compiled_replay_kernel = False
            return None
        if _program_writes_read_only_system_tags(self._program):
            self._compiled_replay_kernel = False
            return None
        try:
            kernel = compile_kernel(self._program)
        except Exception as exc:
            if _looks_like_compiled_replay_gap(exc):
                self._compiled_replay_kernel = False
                return None
            raise
        self._compiled_replay_kernel = kernel
        return kernel

    def _fork_from_reconstructed_state(
        self,
        state: SystemState,
        *,
        rtc_at_state: datetime,
        forces: Mapping[str, bool | int | float | str],
        replay_mode: bool,
    ) -> PLC:
        fork = PLC(
            logic=self._program if self._program is not None else list(self._logic),
            initial_state=state,
            history=self._history_retention_scans,
            cache=self._cache_retention_scans,
            history_budget=self._recent_state_cache_budget,
            checkpoint_interval=self._checkpoint_interval,
            record_all_tags=self._record_all_tags,
        )
        fork._state = state
        fork._reset_cache(state)
        fork._initial_scan_id = state.scan_id
        fork._initial_state = state
        fork._playhead = state.scan_id
        fork._set_time_mode(TimeMode.FIXED_STEP, dt=self._dt)
        fork._set_rtc_internal(rtc_at_state, state.timestamp)
        fork._input_overrides._forces.clear()
        fork._input_overrides._forces.update(forces)
        fork._replay_mode = replay_mode
        fork._sync_runtime_flags_from_state()
        return fork

    def _build_replay_fork(
        self, anchor: int | None
    ) -> tuple[PLC, ScanLogSnapshot, int, dict[int, list[LifecycleEvent]]]:
        """Construct a replay fork anchored at the given checkpoint scan_id.

        ``anchor`` is either a key in ``self._checkpoints`` (from
        ``_nearest_checkpoint_at_or_before``) or ``None``.  When ``None``
        the fork anchors at ``self._initial_scan_id``.  The returned
        fork has ``_replay_mode=True`` and (when anchored at a real
        checkpoint) its force map seeded from the log.  The checkpoint
        bypass guarantees every checkpoint scan carries a full force
        snapshot; anchoring at ``_initial_scan_id`` starts with an empty
        force map, matching default PLC construction.
        """
        log = self._scan_log.snapshot()
        anchor_scan_id = anchor if anchor is not None else self._initial_scan_id
        replay = self.fork(scan_id=anchor_scan_id)
        replay._replay_mode = True
        if anchor is not None:
            replay._input_overrides._forces.clear()
            replay._input_overrides._forces.update(log.force_changes_by_scan[anchor])
        lifecycle_by_scan: dict[int, list[LifecycleEvent]] = {}
        for event in log.lifecycle_events:
            lifecycle_by_scan.setdefault(event.at_scan_id, []).append(event)
        return replay, log, anchor_scan_id, lifecycle_by_scan

    def _apply_log_entries_for_scan(
        self,
        replay: PLC,
        scan_id: int,
        log: ScanLogSnapshot,
        lifecycle_by_scan: dict[int, list[LifecycleEvent]],
    ) -> None:
        """Prepare ``replay`` to step scan ``scan_id`` from the log.

        Applies captured lifecycle events, force-map replacements, RTC
        base changes, patches, and per-scan ``dt`` (REALTIME).  Does not
        call ``replay.step()`` — the caller decides whether to advance
        via ``step()`` (plain path) or by driving ``_scan_steps_debug``
        directly (trace-regeneration path).
        """
        for event in lifecycle_by_scan.get(scan_id, []):
            _apply_lifecycle_to_replay(replay, event)
        if scan_id in log.force_changes_by_scan:
            replay._input_overrides._forces.clear()
            replay._input_overrides._forces.update(log.force_changes_by_scan[scan_id])
        if scan_id in log.rtc_base_changes:
            base, base_sim_time = log.rtc_base_changes[scan_id]
            replay._set_rtc_internal(base, base_sim_time)
        if scan_id in log.patches_by_scan:
            replay.patch(log.patches_by_scan[scan_id])
        if log.dts is not None:
            replay._dt_override_for_next_scan = float(log.dts[scan_id - log.base_scan])
        submits = log.io_submits_by_scan.get(scan_id, {})
        drains = log.io_drains_by_scan.get(scan_id, {})
        if submits or drains:
            replay._next_scan_replay_io = (submits, drains)

    def _replay_to_interpreted(self, target_scan_id: int) -> PLC:
        """Reconstruct historical state by forking and replaying the scan log.

        Anchors at the nearest retained checkpoint ``<= target_scan_id``
        (falling back to scan 0 when no earlier checkpoint exists) and
        walks the scan log forward to ``target_scan_id``, applying
        captured lifecycle events, force-map replacements, RTC base
        changes, patches, and per-scan ``dt`` in REALTIME mode.

        Returns a fork positioned at ``target_scan_id`` with
        ``_replay_mode=True``.  The returned fork is primarily for
        inspection; callers who want to continue it as a live
        investigation session can clear ``fork._replay_mode = False``.
        """
        if target_scan_id < self._initial_scan_id:
            raise ValueError(
                f"target_scan_id must be >= {self._initial_scan_id}, got {target_scan_id}"
            )
        if target_scan_id > self._state.scan_id:
            raise ValueError(
                f"target_scan_id {target_scan_id} is beyond current tip {self._state.scan_id}"
            )
        log_base = self._scan_log.base_scan
        if target_scan_id < log_base:
            raise ValueError(
                f"target_scan_id {target_scan_id} predates the log horizon "
                f"({log_base}); those scans have been trimmed"
            )

        anchor = self._nearest_checkpoint_at_or_before(target_scan_id)
        replay, log, anchor_scan_id, lifecycle_by_scan = self._build_replay_fork(anchor)

        for scan_id in range(anchor_scan_id + 1, target_scan_id + 1):
            self._apply_log_entries_for_scan(replay, scan_id, log, lifecycle_by_scan)
            replay.step()

        # Trailing lifecycle events that fired after the last committed
        # scan (e.g. a trailing stop() with no subsequent step).
        for event in lifecycle_by_scan.get(target_scan_id + 1, []):
            _apply_lifecycle_to_replay(replay, event)

        return replay

    def _replay_to_compiled(self, target_scan_id: int, kernel: CompiledKernel) -> PLC:
        anchor = self._nearest_checkpoint_at_or_before(target_scan_id)
        log = self._scan_log.snapshot()
        anchor_scan_id = anchor if anchor is not None else self._initial_scan_id
        anchor_state = self._checkpoints[anchor] if anchor is not None else self._initial_state
        lifecycle_by_scan: dict[int, list[LifecycleEvent]] = {}
        for event in log.lifecycle_events:
            lifecycle_by_scan.setdefault(event.at_scan_id, []).append(event)

        replay = CompiledPLC(
            self._program,
            initial_state=anchor_state,
            dt=self._dt,
            compiled=kernel,
        )
        replay._set_rtc_internal(
            self._system_runtime._rtc_now(anchor_state), anchor_state.timestamp
        )
        if anchor is not None:
            replay._input_overrides._forces.clear()
            replay._input_overrides._forces.update(log.force_changes_by_scan[anchor])

        for scan_id in range(anchor_scan_id + 1, target_scan_id + 1):
            for event in lifecycle_by_scan.get(scan_id, []):
                _apply_lifecycle_to_replay(replay, event)
            if scan_id in log.force_changes_by_scan:
                replay._input_overrides._forces.clear()
                replay._input_overrides._forces.update(log.force_changes_by_scan[scan_id])
            if scan_id in log.rtc_base_changes:
                base, base_sim_time = log.rtc_base_changes[scan_id]
                replay._set_rtc_internal(base, base_sim_time)
            if scan_id in log.patches_by_scan:
                replay.patch(log.patches_by_scan[scan_id])
            replay.step_replay()
            for record in log.io_submits_by_scan.get(scan_id, {}).values():
                for tag_name, value in record.tag_writes:
                    replay._kernel.tags[tag_name] = value
            for record in log.io_drains_by_scan.get(scan_id, {}).values():
                for tag_name, value in record.tag_writes:
                    replay._kernel.tags[tag_name] = value

        for event in lifecycle_by_scan.get(target_scan_id + 1, []):
            _apply_lifecycle_to_replay(replay, event)

        state = replay._materialize_replay_state()
        return self._fork_from_reconstructed_state(
            state,
            rtc_at_state=replay._rtc_at_sim_time(state.timestamp),
            forces=replay._input_overrides.forces_mutable,
            replay_mode=True,
        )

    def replay_to(self, target_scan_id: int) -> PLC:
        """Reconstruct historical state, preferring compiled replay when supported."""
        if target_scan_id < self._initial_scan_id:
            raise ValueError(
                f"target_scan_id must be >= {self._initial_scan_id}, got {target_scan_id}"
            )
        if target_scan_id > self._state.scan_id:
            raise ValueError(
                f"target_scan_id {target_scan_id} is beyond current tip {self._state.scan_id}"
            )
        log_base = self._scan_log.base_scan
        if target_scan_id < log_base:
            raise ValueError(
                f"target_scan_id {target_scan_id} predates the log horizon "
                f"({log_base}); those scans have been trimmed"
            )

        kernel = self._compiled_replay_supported_kernel()
        if kernel is None:
            return self._replay_to_interpreted(target_scan_id)
        return self._replay_to_compiled(target_scan_id, kernel)

    def replay_trace_at(self, target_scan_id: int) -> dict[int, RungTrace]:
        """Reconstruct the rung-trace dict for a historical scan.

        Runs the same replay walk as ``replay_to`` up to
        ``target_scan_id - 1`` on the plain scan path, then drives
        ``_scan_steps_debug`` for ``target_scan_id`` so the replay
        fork's ``_current_rung_traces`` gets populated.  Returns a copy
        of that dict; the replay fork is discarded.

        The ``_replay_mode`` guards in ``_commit_scan`` (monitors,
        breakpoints) and ``_set_rtc_and_record`` cover the debug path
        too — both generators funnel through the same commit sink.

        A one-slot cache (``_cached_replay_trace``) hits when the same
        ``target_scan_id`` is requested back-to-back.  It is cleared on
        any tip advance (``_run_single_scan``) and on reset paths that
        reset the log (reboot, stop→run, via
        ``_clear_retained_debug_trace_caches``).

        Traces only exist for scans that actually executed — the fork
        anchor / initial scan was never stepped in debug mode — so
        ``target_scan_id`` must be strictly greater than
        ``_initial_scan_id``.
        """
        if target_scan_id <= self._initial_scan_id:
            raise ValueError(
                f"target_scan_id must be > {self._initial_scan_id} "
                f"(no traces exist for the initial scan), got {target_scan_id}"
            )
        if target_scan_id > self._state.scan_id:
            raise ValueError(
                f"target_scan_id {target_scan_id} is beyond current tip {self._state.scan_id}"
            )

        cached = self._cached_replay_trace
        if cached is not None and cached[0] == target_scan_id:
            return dict(cached[1])

        anchor = self._nearest_checkpoint_at_or_before(target_scan_id)
        replay, log, anchor_scan_id, lifecycle_by_scan = self._build_replay_fork(anchor)

        for scan_id in range(anchor_scan_id + 1, target_scan_id):
            self._apply_log_entries_for_scan(replay, scan_id, log, lifecycle_by_scan)
            replay.step()

        # Final scan: drive the debug generator directly so the replay
        # fork's ``_current_rung_traces`` / ``_current_rung_traces_scan_id``
        # slots land populated post-commit.
        self._apply_log_entries_for_scan(replay, target_scan_id, log, lifecycle_by_scan)
        for _step in replay._scan_steps_debug():
            pass

        traces = dict(replay._current_rung_traces)
        self._cached_replay_trace = (target_scan_id, traces)
        return dict(traces)

    def _replay_range_interpreted(self, start_scan_id: int, end_scan_id: int) -> list[SystemState]:
        """Reconstruct ``SystemState`` for every scan in ``[start, end]``.

        Anchors once at the nearest checkpoint ``<= start`` (falling
        back to scan 0) and walks the scan log forward to
        ``end_scan_id``, accumulating the committed state after each
        scan in the requested range.  Cheaper than N independent
        ``replay_to`` calls because it pays the fork-from-checkpoint
        cost once.

        Used by ``History.range`` / ``History.latest`` when the
        requested range falls outside the live recent-state window.
        """
        if start_scan_id < self._initial_scan_id or end_scan_id < start_scan_id:
            return []
        tip = self._state.scan_id
        if start_scan_id > tip:
            return []
        end_scan_id = min(end_scan_id, tip)

        anchor = self._nearest_checkpoint_at_or_before(start_scan_id)
        replay, log, anchor_scan_id, lifecycle_by_scan = self._build_replay_fork(anchor)

        results: list[SystemState] = []
        if anchor_scan_id >= start_scan_id:
            results.append(replay.current_state)

        for scan_id in range(anchor_scan_id + 1, end_scan_id + 1):
            self._apply_log_entries_for_scan(replay, scan_id, log, lifecycle_by_scan)
            replay.step()
            if scan_id >= start_scan_id:
                results.append(replay.current_state)

        return results

    def _replay_range_compiled(
        self,
        start_scan_id: int,
        end_scan_id: int,
        kernel: CompiledKernel,
    ) -> list[SystemState]:
        anchor = self._nearest_checkpoint_at_or_before(start_scan_id)
        log = self._scan_log.snapshot()
        anchor_scan_id = anchor if anchor is not None else self._initial_scan_id
        anchor_state = self._checkpoints[anchor] if anchor is not None else self._initial_state
        lifecycle_by_scan: dict[int, list[LifecycleEvent]] = {}
        for event in log.lifecycle_events:
            lifecycle_by_scan.setdefault(event.at_scan_id, []).append(event)

        replay = CompiledPLC(
            self._program,
            initial_state=anchor_state,
            dt=self._dt,
            compiled=kernel,
        )
        replay._set_rtc_internal(
            self._system_runtime._rtc_now(anchor_state), anchor_state.timestamp
        )
        if anchor is not None:
            replay._input_overrides._forces.clear()
            replay._input_overrides._forces.update(log.force_changes_by_scan[anchor])

        results: list[SystemState] = []
        if anchor_scan_id >= start_scan_id:
            results.append(replay.current_state)

        for scan_id in range(anchor_scan_id + 1, end_scan_id + 1):
            for event in lifecycle_by_scan.get(scan_id, []):
                _apply_lifecycle_to_replay(replay, event)
            if scan_id in log.force_changes_by_scan:
                replay._input_overrides._forces.clear()
                replay._input_overrides._forces.update(log.force_changes_by_scan[scan_id])
            if scan_id in log.rtc_base_changes:
                base, base_sim_time = log.rtc_base_changes[scan_id]
                replay._set_rtc_internal(base, base_sim_time)
            if scan_id in log.patches_by_scan:
                replay.patch(log.patches_by_scan[scan_id])
            if scan_id >= start_scan_id:
                replay.step()
                results.append(replay.current_state)
            else:
                replay.step_replay()

        return results

    def _replay_range(self, start_scan_id: int, end_scan_id: int) -> list[SystemState]:
        kernel = self._compiled_replay_supported_kernel()
        if kernel is None:
            return self._replay_range_interpreted(start_scan_id, end_scan_id)
        return self._replay_range_compiled(start_scan_id, end_scan_id, kernel)

    @property
    def simulation_time(self) -> float:
        """Current simulation clock in seconds."""
        return self._state.timestamp

    @property
    def time_mode(self) -> TimeMode:
        """Current time mode."""
        return self._time_mode

    @property
    def debug(self) -> _DebugNamespace:
        """Debugger-facing methods and internal runtime access."""
        return self._debug_ns

    def stop(self) -> None:
        """Transition PLC to STOP mode."""
        if not self._running:
            return
        self._running = False
        self._state = self._apply_runtime_memory_flags(
            self._state,
            mode_run=False,
            battery_present=self._battery_present,
        )
        self._record_lifecycle("stop")

    @property
    def battery_present(self) -> bool:
        """Simulated backup battery presence."""
        return self._battery_present

    @battery_present.setter
    def battery_present(self, value: bool) -> None:
        new_value = bool(value)
        if new_value == self._battery_present:
            return
        self._battery_present = new_value
        self._state = self._apply_runtime_memory_flags(
            self._state,
            mode_run=self._running,
            battery_present=self._battery_present,
        )
        self._record_lifecycle("battery_present", value=new_value)

    def reboot(self) -> SystemState:
        """Power-cycle the runner and return the reset state.

        Reboot is destructive: tags reset to defaults (except
        battery-preserved retentive values), ``state.scan_id`` and
        ``state.timestamp`` return to 0.  Because post-reboot scan_ids
        would alias pre-reboot entries in every sparse channel
        (patches, forces, rtc_base_changes, dts), the scan log and
        checkpoints are reset to a fresh recording session rooted at
        the post-reboot scan 0.  Pre-reboot history is not
        replay-addressable — users who need that should ``fork()``
        before rebooting.
        """
        tag_values = self._rebuild_tags_for_reset(
            preserve_all=self._battery_present,
            preserve_retentive=False,
        )
        self._reset_runtime_scope(
            tag_values=tag_values,
            mode_run=True,
            preserve_rtc_continuity=self._battery_present,
        )
        self._running = True
        self._scan_log = ScanLog(time_mode=self._time_mode, base_scan=0)
        self._checkpoints = {}
        self._forces_last_recorded = {}
        self._this_scan_drained_patches = {}
        return self._state

    def set_rtc(self, value: datetime) -> None:
        """Set the current RTC value for the runner."""
        self._set_rtc_and_record(self._normalize_rtc_datetime(value), self._state.timestamp)

    def _set_time_mode(self, mode: TimeMode, *, dt: float = 0.010) -> None:
        """Set the time mode (internal, used by fork)."""
        self._time_mode = mode
        self._dt = dt
        if mode == TimeMode.REALTIME:
            self._last_step_time = time.perf_counter()
        # Reinitialize scan log so ``dts`` is present in REALTIME and absent
        # in FIXED_STEP.  Only called early in fork() so the log is empty
        # and no recorded history is lost.
        self._scan_log = ScanLog(time_mode=self._time_mode, base_scan=self._state.scan_id)
        self._checkpoints = {}
        self._forces_last_recorded = dict(self._input_overrides.forces)

    @staticmethod
    def _apply_runtime_memory_flags(
        state: SystemState,
        *,
        mode_run: bool,
        battery_present: bool,
    ) -> SystemState:
        memory = state.memory.set(_MODE_RUN_KEY, bool(mode_run)).set(
            _BATTERY_PRESENT_KEY, bool(battery_present)
        )
        return state.set(memory=memory)

    def _refresh_known_tags_from_logic(self) -> None:
        for rung in self._logic:
            for tag in _iter_referenced_tags(rung):
                self._register_known_tag(tag)

    def _register_known_tag(self, tag: Tag) -> None:
        if tag.name in SYSTEM_TAGS_BY_NAME:
            return
        existing = self._known_tags_by_name.get(tag.name)
        if existing is None:
            self._known_tags_by_name[tag.name] = tag
            return
        if (
            existing.type != tag.type
            or existing.retentive != tag.retentive
            or existing.default != tag.default
        ):
            raise ValueError(
                f"Conflicting tag metadata for {tag.name!r}: existing "
                f"(type={existing.type.name}, retentive={existing.retentive}, "
                f"default={existing.default!r}) vs new "
                f"(type={tag.type.name}, retentive={tag.retentive}, default={tag.default!r})."
            )

    def _register_known_tags_from_mapping_keys(
        self,
        tags: Mapping[str, bool | int | float | str]
        | Mapping[Tag, bool | int | float | str]
        | Mapping[str | Tag, bool | int | float | str],
    ) -> None:
        from pyrung.core.tag import Tag as TagClass

        for key in tags:
            if isinstance(key, TagClass):
                self._register_known_tag(key)

    def _rebuild_tags_for_reset(
        self,
        *,
        preserve_all: bool,
        preserve_retentive: bool,
    ) -> dict[str, Any]:
        current_tags = self._state.tags
        rebuilt: dict[str, Any] = {}
        for tag in self._known_tags_by_name.values():
            if preserve_all:
                rebuilt[tag.name] = current_tags.get(tag.name, tag.default)
                continue
            if preserve_retentive and tag.retentive and tag.name in current_tags:
                rebuilt[tag.name] = current_tags[tag.name]
                continue
            rebuilt[tag.name] = tag.default
        return rebuilt

    def _clear_retained_debug_trace_caches(self) -> None:
        self._current_rung_traces = {}
        self._current_rung_traces_scan_id = None
        self._clear_inflight_debug_scan()
        self._latest_committed_trace_event = None
        self._cached_replay_trace = None

    def _normalize_rtc_datetime(self, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value
        return value.astimezone().replace(tzinfo=None)

    def _set_rtc_internal(self, value: datetime, sim_time: float) -> None:
        self._rtc_base = value
        self._rtc_base_sim_time = float(sim_time)

    def _set_rtc_and_record(self, value: datetime, sim_time: float) -> None:
        """Set RTC base and record the change for replay.

        Used for user-initiated (``set_rtc()``) and in-scan
        (``_apply_rtc_date/time`` via the system-points ``rtc_setter``
        callback) paths.  Internal lifecycle paths (reboot,
        stop-to-run) use ``_set_rtc_internal`` directly; the lifecycle
        event itself implies the RTC transition at replay time.

        No-op in replay mode — the log's ``rtc_base_changes`` entry is
        load-bearing, and ``replay_to`` applies it via
        ``_set_rtc_internal`` directly.  Re-firing here would
        duplicate-record and (worse) shift the recorded scan_id.
        """
        if self._replay_mode:
            return
        self._set_rtc_internal(value, sim_time)
        self._scan_log.record_rtc_base_change(self._state.scan_id + 1, value, float(sim_time))

    def _record_lifecycle(self, kind: LifecycleKind, value: bool | None = None) -> None:
        self._scan_log.record_lifecycle(
            LifecycleEvent(
                at_sim_time=float(self._state.timestamp),
                at_scan_id=self._state.scan_id + 1,
                kind=kind,
                value=value,
            )
        )

    def _rtc_at_sim_time(self, sim_time: float) -> datetime:
        return self._rtc_base + timedelta(seconds=float(sim_time) - self._rtc_base_sim_time)

    def _reset_runtime_scope(
        self,
        *,
        tag_values: Mapping[str, Any],
        mode_run: bool,
        preserve_rtc_continuity: bool = True,
    ) -> None:
        rtc_after_reset = (
            self._rtc_at_sim_time(self._state.timestamp)
            if preserve_rtc_continuity
            else self._normalize_rtc_datetime(datetime.now())
        )
        next_state = SystemState().with_tags(dict(tag_values))
        self._state = self._apply_runtime_memory_flags(
            next_state,
            mode_run=mode_run,
            battery_present=self._battery_present,
        )
        self._set_rtc_internal(rtc_after_reset, self._state.timestamp)
        self._reset_cache(self._state)
        self._initial_scan_id = self._state.scan_id
        self._initial_state = self._state
        self._history._reset_labels()
        self._playhead = self._state.scan_id

        self._pending_patches.clear()
        self._forces.clear()
        self._pause_requested_this_scan = False
        self._clear_retained_debug_trace_caches()
        # Reboot drops the firing timelines together with the scan log
        # and checkpoints — Option B treats reboot like a fresh session
        # (see stage-4 notes in the design doc).
        self._rung_firing_timelines.reset()

        if self._time_mode == TimeMode.REALTIME:
            self._last_step_time = time.perf_counter()
        else:
            self._last_step_time = None

    def _stop_to_run_transition(self) -> None:
        if self._running:
            return
        tag_values = self._rebuild_tags_for_reset(
            preserve_all=False,
            preserve_retentive=True,
        )
        self._reset_runtime_scope(
            tag_values=tag_values,
            mode_run=True,
            preserve_rtc_continuity=True,
        )
        self._running = True

    def _ensure_running(self) -> None:
        if not self._running:
            self._stop_to_run_transition()

    def _sync_runtime_flags_from_state(self) -> None:
        self._running = bool(self._state.memory.get(_MODE_RUN_KEY, True))
        self._battery_present = bool(
            self._state.memory.get(_BATTERY_PRESENT_KEY, self._battery_present)
        )

    def __enter__(self) -> PLC:
        """Bind this runner as active for live Tag.value access."""
        self._active_tokens.append(set_active_runner(self))
        return self

    def __exit__(self, *exc: object) -> None:
        if self._active_tokens:
            reset_active_runner(self._active_tokens.pop())

    def patch(
        self,
        tags: Mapping[str, bool | int | float | str]
        | Mapping[Tag, bool | int | float | str]
        | Mapping[str | Tag, bool | int | float | str],
    ) -> None:
        """Queue tag values for next scan (one-shot).

        Values are applied at the start of the next step() call,
        then cleared. Use for momentary inputs like button presses.

        Args:
            tags: Dict of tag names or Tag objects to values.
        """
        self._register_known_tags_from_mapping_keys(tags)
        self._input_overrides.patch(tags)

    def force(self, tag: str | Tag, value: bool | int | float | str) -> None:
        """Persistently override a tag value until explicitly removed.

        The forced value is applied at the pre-logic force pass (phase 3) and
        re-applied at the post-logic force pass (phase 5) every scan.  Logic
        may temporarily diverge the value mid-scan, but the post-logic pass
        restores it before outputs are written.

        Forces persist across scans until `unforce()` or `clear_forces()`
        is called.  Multiple forces may be active simultaneously.

        If a tag is both patched and forced in the same scan, the force
        overwrites the patch during the pre-logic pass.

        Args:
            tag: Tag name or `Tag` object to override.
            value: Value to hold. Must be compatible with the tag's type.

        Raises:
            ValueError: If the tag is a read-only system point.
        """
        from pyrung.core.tag import Tag as TagClass

        if isinstance(tag, TagClass):
            self._register_known_tag(tag)
        self._input_overrides.add_force(tag, value)

    def unforce(self, tag: str | Tag) -> None:
        """Remove a single persistent force override.

        After removal the tag resumes its logic-computed value starting
        from the next scan.

        Args:
            tag: Tag name or `Tag` object whose force to remove.
        """
        self._input_overrides.remove_force(tag)

    def clear_forces(self) -> None:
        """Remove all active persistent force overrides.

        All forced tags resume their logic-computed values starting
        from the next scan.
        """
        had_forces = bool(self._input_overrides.forces)
        self._input_overrides.clear_forces()
        if had_forces:
            self._record_lifecycle("clear_forces")

    @contextmanager
    def forced(
        self,
        overrides: Mapping[str, bool | int | float | str]
        | Mapping[Tag, bool | int | float | str]
        | Mapping[str | Tag, bool | int | float | str],
    ) -> Iterator[PLC]:
        """Temporarily apply forces for the duration of the context.

        On entry, saves the current force map and adds the given overrides.
        On exit (normally or on exception), the exact previous force map is
        restored — forces that existed before the context are reinstated, and
        forces added inside the context are removed.

        Safe for nesting: inner ``forced()`` contexts layer on top of outer ones
        without disrupting them.

        Args:
            overrides: Mapping of tag name / ``Tag`` object to forced value.

        Example:
            ```python
            with plc.forced({"AutoMode": True, "Fault": False}):
                plc.run(5)
            # AutoMode and Fault forces released here
            ```
        """
        self._register_known_tags_from_mapping_keys(overrides)
        with self._input_overrides.force(overrides):
            yield self

    @property
    def forces(self) -> Mapping[str, bool | int | float | str]:
        """Read-only view of active persistent overrides."""
        return self._input_overrides.forces

    def monitor(self, tag: str | Tag, callback: Callable[[Any, Any], None]) -> _RunnerHandle:
        """Call ``callback(current, previous)`` after commit when tag value changes."""
        tag_name = self._normalize_tag_name(tag, method="monitor")
        monitor_id = self._next_handle_id()
        self._monitors_by_id[monitor_id] = _MonitorRegistration(
            id=monitor_id,
            tag_name=tag_name,
            callback=callback,
        )
        return _RunnerHandle(
            handle_id=monitor_id,
            remove=self._remove_monitor,
            enable=self._enable_monitor,
            disable=self._disable_monitor,
        )

    def when(
        self,
        *conditions: Condition
        | Tag
        | Callable[[SystemState], bool]
        | tuple[Condition | Tag, ...]
        | list[Condition | Tag],
    ) -> _BreakpointBuilder:
        """Create a breakpoint builder evaluated after each committed scan.

        Accepts ``Tag``/``Condition`` expressions (implicit AND) or a single
        callable predicate receiving ``SystemState``.
        """
        if self._is_fn_predicate(conditions):
            return _BreakpointBuilder(self, conditions[0])
        predicate = self._compile_condition_predicate(*conditions, method="when")  # ty: ignore[invalid-argument-type]
        return _BreakpointBuilder(self, predicate)

    @staticmethod
    def _is_fn_predicate(
        conditions: tuple[Any, ...],
    ) -> TypeGuard[tuple[Callable[[SystemState], bool]]]:
        """Return True if conditions is a single callable predicate (not a Tag/Condition)."""
        if len(conditions) != 1 or not callable(conditions[0]):
            return False
        from pyrung.core.condition import Condition as ConditionClass
        from pyrung.core.tag import Tag as TagClass

        return not isinstance(conditions[0], (TagClass, ConditionClass))

    def _compile_condition_predicate(
        self,
        *conditions: Condition | Tag | tuple[Condition | Tag, ...] | list[Condition | Tag],
        method: Literal["run_until", "when"],
    ) -> Callable[[SystemState], bool]:
        """Compile a Tag/Condition expression into a ``SystemState`` predicate."""
        if not conditions:
            raise TypeError(f"{method}() requires at least one condition")

        from pyrung.core.condition import _as_condition, _normalize_and_condition

        normalized = _normalize_and_condition(
            *conditions,
            coerce=_as_condition,
            empty_error=f"{method}() requires at least one condition",
            group_empty_error=f"{method}() condition group cannot be empty",
        )

        def _predicate(state: SystemState) -> bool:
            ctx = ScanContext(
                state,
                resolver=self._system_runtime.resolve,
                read_only_tags=self._system_runtime.read_only_tags,
            )
            return normalized.evaluate(ctx)

        return _predicate

    def _normalize_tag_name(self, tag: str | Tag, *, method: str) -> str:
        from pyrung.core.tag import Tag as TagClass

        if isinstance(tag, TagClass):
            return tag.name
        if isinstance(tag, str):
            return tag
        raise TypeError(f"{method}() tag must be str or Tag, got {type(tag).__name__}")

    def _next_handle_id(self) -> int:
        handle_id = self._next_debug_handle_id
        self._next_debug_handle_id += 1
        return handle_id

    def _remove_monitor(self, monitor_id: int) -> None:
        registration = self._monitors_by_id.get(monitor_id)
        if registration is None or registration.removed:
            return
        registration.removed = True
        registration.enabled = False

    def _enable_monitor(self, monitor_id: int) -> None:
        registration = self._monitors_by_id.get(monitor_id)
        if registration is None or registration.removed:
            return
        registration.enabled = True

    def _disable_monitor(self, monitor_id: int) -> None:
        registration = self._monitors_by_id.get(monitor_id)
        if registration is None or registration.removed:
            return
        registration.enabled = False

    def _register_breakpoint(
        self,
        *,
        predicate: Callable[[SystemState], bool],
        action: Literal["pause", "snapshot"],
        label: str | None,
    ) -> _RunnerHandle:
        breakpoint_id = self._next_handle_id()
        self._breakpoints_by_id[breakpoint_id] = _BreakpointRegistration(
            id=breakpoint_id,
            predicate=predicate,
            action=action,
            label=label,
        )
        return _RunnerHandle(
            handle_id=breakpoint_id,
            remove=self._remove_breakpoint,
            enable=self._enable_breakpoint,
            disable=self._disable_breakpoint,
        )

    def _remove_breakpoint(self, breakpoint_id: int) -> None:
        registration = self._breakpoints_by_id.get(breakpoint_id)
        if registration is None or registration.removed:
            return
        registration.removed = True
        registration.enabled = False

    def _enable_breakpoint(self, breakpoint_id: int) -> None:
        registration = self._breakpoints_by_id.get(breakpoint_id)
        if registration is None or registration.removed:
            return
        registration.enabled = True

    def _disable_breakpoint(self, breakpoint_id: int) -> None:
        registration = self._breakpoints_by_id.get(breakpoint_id)
        if registration is None or registration.removed:
            return
        registration.enabled = False

    def _consume_pause_request(self) -> bool:
        pause_requested = self._pause_requested_this_scan
        self._pause_requested_this_scan = False
        return pause_requested

    def _peek_live_tag_value(self, name: str, default: Any) -> Any:
        """Read a tag as seen by live Tag.value access."""
        has_override, value = self._input_overrides.get_live_override(name)
        if has_override:
            return value

        resolved, value = self._system_runtime.resolve(name, self._state)
        if resolved:
            return value

        return self._state.tags.get(name, default)

    def _calculate_dt(self) -> float:
        """Calculate scan delta time based on current time mode."""
        if self._dt_override_for_next_scan is not None:
            dt_override = self._dt_override_for_next_scan
            self._dt_override_for_next_scan = None
            return dt_override
        if self._time_mode == TimeMode.REALTIME:
            now = time.perf_counter()
            if self._last_step_time is None:
                self._last_step_time = now
            dt = now - self._last_step_time
            self._last_step_time = now
            return dt
        return self._dt

    def _prepare_scan(self) -> tuple[ScanContext, float]:
        """Create and initialize scan context before logic evaluation."""
        replay_io = getattr(self, "_next_scan_replay_io", None)
        self._next_scan_replay_io = None
        ctx = ScanContext(
            self._state,
            resolver=self._system_runtime.resolve,
            read_only_tags=self._system_runtime.read_only_tags,
            consumed_tags_getter=self._consumed_tags_for_capture,
            replay_io=replay_io,
        )

        for cb in self._pre_scan_callbacks:
            cb()
        self._system_runtime.on_scan_start(ctx)
        self._this_scan_drained_patches = self._input_overrides.apply_pre_scan(ctx)

        dt = self._calculate_dt()
        if self._state.memory.get("_dt", _SENTINEL) != dt:
            ctx.set_memory("_dt", dt)
        return ctx, dt

    def _capture_previous_states(self, ctx: ScanContext) -> None:
        """Batch _prev:* updates used by edge detection conditions.

        Skips writes when the stored ``_prev:{name}`` already equals the
        current tag value — so idle scans (nothing changed) leave the
        memory PMap untouched and structurally shared with the prior scan.
        """
        state_memory = self._state.memory
        for name in self._state.tags:
            prev_key = f"_prev:{name}"
            current = ctx.get_tag(name)
            if state_memory.get(prev_key, _SENTINEL) != current:
                ctx.set_memory(prev_key, current)
        for name in ctx._tags_pending:
            if name in self._state.tags:
                continue
            prev_key = f"_prev:{name}"
            current = ctx.get_tag(name)
            if state_memory.get(prev_key, _SENTINEL) != current:
                ctx.set_memory(prev_key, current)

    def _commit_scan(self, ctx: ScanContext, dt: float) -> None:
        """Finalize one scan and commit all batched writes.

        Rung firings are read from ``ctx.rung_firings`` — both the non-debug
        and debug scan paths populate it via ``ctx.capturing_rung(i)`` around
        each top-level rung evaluation.  Scans with no firings (e.g. manual
        commits from tests) simply record nothing.
        """
        previous_state = self._state
        previous_tip_scan_id = previous_state.scan_id
        self._input_overrides.apply_post_logic(ctx)

        self._capture_previous_states(ctx)
        self._system_runtime.on_scan_end(ctx)
        if self._constrained_tags:
            self._bounds_violations = check_bounds(ctx._tags_pending, self._constrained_tags)
            for v in self._bounds_violations.values():
                warnings.warn(str(v), stacklevel=2)
        else:
            self._bounds_violations = {}
        self._state = ctx.commit(dt=dt)
        # Replay recorder: capture nondeterminism for this scan.
        new_scan_id = self._state.scan_id
        # Checkpoint bypass: the force-map write at checkpoint boundaries
        # is unconditional — replay reads force state from the checkpoint
        # scan's log entry, so diff-eliding it would strand reconstruction.
        is_checkpoint = new_scan_id > 0 and new_scan_id % self._checkpoint_interval == 0
        if self._this_scan_drained_patches:
            self._scan_log.record_patches(new_scan_id, self._this_scan_drained_patches)
            self._this_scan_drained_patches = {}
        current_forces = dict(self._input_overrides.forces)
        if is_checkpoint or current_forces != self._forces_last_recorded:
            self._scan_log.record_force_changes(new_scan_id, current_forces)
            self._forces_last_recorded = current_forces
        if is_checkpoint:
            self._checkpoints[new_scan_id] = self._state
        if self._scan_log.records_dt:
            self._scan_log.record_dt(new_scan_id, dt)
        if ctx._io_submit_staging:
            for key, record in ctx._io_submit_staging.items():
                self._scan_log.record_io_submit(new_scan_id, key, record)
        if ctx._io_drain_staging:
            for key, record in ctx._io_drain_staging.items():
                self._scan_log.record_io_drain(new_scan_id, key, record)
        # Per-rung timeline append.  Only rungs that fired this scan
        # get a timeline update — stable rungs extend the tail range
        # (no allocation), period-2 oscillators collapse into a single
        # ``AlternatingRun`` entry.  Rungs that didn't fire contribute
        # nothing to the timeline for this scan.
        new_firings = ctx.rung_firings
        for rung_index, writes in new_firings.items():
            self._rung_firing_timelines.append(rung_index, new_scan_id, writes)
        # Rung traces are per-commit, not per-history. The debug path
        # repopulates _current_rung_traces after commit_scan returns; any
        # other commit path leaves the slot empty for this scan.
        if self._current_rung_traces_scan_id != self._state.scan_id:
            self._current_rung_traces = {}
            self._current_rung_traces_scan_id = None
            self._latest_committed_trace_event = None
        self._cache_state(self._state)

        # Retention-policy auto-trim, piggybacked on checkpoint cadence.
        if (
            self._history_retention_scans is not None
            and is_checkpoint
            and new_scan_id > self._history_retention_scans
        ):
            horizon = new_scan_id - self._history_retention_scans
            surviving_cp = self._nearest_checkpoint_at_or_after(horizon)
            if surviving_cp is not None:
                self._trim_history_before(surviving_cp)
            total = (
                self._recent_state_cache_bytes
                + sum(_estimate_state_bytes(s) for s in self._checkpoints.values())
                + self._scan_log.bytes_estimate()
            )
            if total > self._recent_state_cache_budget:
                extra_horizon = horizon + self._checkpoint_interval
                extra_cp = self._nearest_checkpoint_at_or_after(extra_horizon)
                if extra_cp is not None:
                    self._trim_history_before(extra_cp)

        # Keep playhead following newest state unless manually moved.
        if self._playhead == previous_tip_scan_id:
            self._playhead = self._state.scan_id

        if not self._replay_mode:
            self._evaluate_monitors(previous_state=previous_state, current_state=self._state)
            self._evaluate_breakpoints(state=self._state)
        self._sync_runtime_flags_from_state()

    def _evaluate_monitors(
        self, *, previous_state: SystemState, current_state: SystemState
    ) -> None:
        for monitor_id in sorted(self._monitors_by_id):
            registration = self._monitors_by_id[monitor_id]
            if registration.removed or not registration.enabled:
                continue

            previous_value = previous_state.tags.get(registration.tag_name)
            current_value = current_state.tags.get(registration.tag_name)
            if current_value != previous_value:
                registration.callback(current_value, previous_value)

    def _evaluate_breakpoints(self, *, state: SystemState) -> None:
        self._pause_requested_this_scan = False
        for breakpoint_id in sorted(self._breakpoints_by_id):
            registration = self._breakpoints_by_id[breakpoint_id]
            if registration.removed or not registration.enabled:
                continue

            if not registration.predicate(state):
                continue

            if registration.action == "snapshot":
                assert registration.label is not None
                self._history._label_scan(
                    registration.label,
                    state.scan_id,
                    metadata=self._snapshot_metadata_for_state(state),
                )
                continue

            self._pause_requested_this_scan = True

    def _snapshot_metadata_for_state(self, state: SystemState) -> dict[str, Any]:
        rtc_now = self._rtc_at_sim_time(state.timestamp)
        wall_now = self._normalize_rtc_datetime(datetime.now())
        return {
            "rtc_iso": rtc_now.isoformat(),
            "rtc_offset_seconds": float((rtc_now - wall_now).total_seconds()),
        }

    def _scan_steps(self) -> Generator[tuple[int, Rung, ScanContext], None, None]:
        """Execute one scan cycle and yield after each rung evaluation.

        Scan phases:
        1. Create ScanContext from current state
        2. Apply pending patches to context
        3. Apply persistent force overrides (pre-logic)
        4. Calculate dt and inject into context
        5. Evaluate all logic (writes batched in context), yielding after each rung
        6. Re-apply force overrides (post-logic)
        7. Batch _prev:* updates for edge detection
        8. Commit all changes in single operation

        The commit in phase 8 only happens when the generator is exhausted.
        """
        self._ensure_running()
        ctx, dt = self._prepare_scan()

        for i, rung in enumerate(self._logic):
            with ctx.capturing_rung(i):
                rung.evaluate(ctx)
            yield i, rung, ctx

        self._commit_scan(ctx, dt)

    def _scan_steps_debug(self) -> Generator[ScanStep, None, None]:
        """Execute one scan cycle and yield ``ScanStep`` objects at all boundaries.

        Yields a `ScanStep` at each:

        - Top-level rung boundary (``kind="rung"``)
        - Branch entry / exit (``kind="branch"``)
        - Subroutine call and body steps (``kind="subroutine"``)
        - Individual instruction boundaries (``kind="instruction"``)

        Each `ScanStep` carries source location metadata (``source_file``,
        ``source_line``, ``end_line``), rung enable state, and a trace of
        evaluated conditions and instructions.

        This is the API used by the DAP adapter via ``plc.debug.scan_steps_debug()``.
        Prefer ``_scan_steps()`` for non-debug consumers — it has less overhead
        and a simpler yield type.

        Note:
            The scan is committed only when the generator is **fully exhausted**.
        """
        self._ensure_running()
        pending_scan_id = self._state.scan_id + 1
        events_by_rung: dict[int, list[RungTraceEvent]] = {}
        self._start_inflight_debug_scan(scan_id=pending_scan_id, events_by_rung=events_by_rung)

        try:
            for step in self._debugger.scan_steps_debug(self._debug_ns):
                event = self._rung_trace_event_from_step(step)
                events_by_rung.setdefault(step.rung_index, []).append(event)
                self._latest_inflight_trace_event = (pending_scan_id, step.rung_index, event)
                yield step

            scan_id = self._state.scan_id
            self._current_rung_traces = {
                rung_id: RungTrace(scan_id=scan_id, rung_id=rung_id, events=tuple(events))
                for rung_id, events in sorted(events_by_rung.items())
            }
            self._current_rung_traces_scan_id = scan_id
            if self._latest_inflight_trace_event is not None:
                _inflight_scan_id, latest_rung_id, latest_event = self._latest_inflight_trace_event
                self._latest_committed_trace_event = (scan_id, latest_rung_id, latest_event)
        finally:
            self._clear_inflight_debug_scan()

    def _start_inflight_debug_scan(
        self,
        *,
        scan_id: int,
        events_by_rung: dict[int, list[RungTraceEvent]],
    ) -> None:
        self._inflight_scan_id = scan_id
        self._inflight_rung_events = events_by_rung
        self._latest_inflight_trace_event = None

    def _clear_inflight_debug_scan(self) -> None:
        self._inflight_scan_id = None
        self._inflight_rung_events = {}
        self._latest_inflight_trace_event = None

    def _rung_trace_event_from_step(self, step: ScanStep) -> RungTraceEvent:
        return RungTraceEvent(
            kind=step.kind,
            source_file=step.source_file,
            source_line=step.source_line,
            end_line=step.end_line,
            subroutine_name=step.subroutine_name,
            depth=step.depth,
            call_stack=step.call_stack,
            enabled_state=step.enabled_state,
            instruction_kind=step.instruction_kind,
            trace=step.trace,
        )

    def _evaluate_condition_value(
        self,
        condition: Any,
        ctx: ScanContext | ConditionView,
    ) -> tuple[bool, list[dict[str, Any]]]:
        return self._condition_trace.evaluate(condition, ctx)

    def _condition_term_text(self, condition: Any, details: list[dict[str, Any]]) -> str:
        return self._condition_trace.summary(condition, details)

    def _condition_annotation(self, *, status: str, expression: str, summary: str) -> str:
        return self._condition_trace.annotation(
            status=status,
            expression=expression,
            summary=summary,
        )

    def _condition_expression(self, condition: Any) -> str:
        return self._condition_trace.expression(condition)

    def step(self) -> SystemState:
        """Execute one full scan cycle and return the committed state."""
        self._ensure_running()
        return self._run_single_scan(consume_pause_request=True)

    def _run_single_scan(self, *, consume_pause_request: bool) -> SystemState:
        self._cached_replay_trace = None
        for _ in self._scan_steps():
            pass

        if consume_pause_request:
            self._consume_pause_request()
        return self._state

    def run(self, cycles: int) -> SystemState:
        """Execute up to ``cycles`` scans, stopping early on pause breakpoints.

        Args:
            cycles: Number of scans to execute.

        Returns:
            The final SystemState after all cycles.
        """
        self._ensure_running()
        for _ in range(cycles):
            self._consume_pause_request()
            self._run_single_scan(consume_pause_request=False)
            if self._consume_pause_request():
                break
        return self._state

    def run_for(self, seconds: float) -> SystemState:
        """Run until simulation time advances by N seconds or a pause breakpoint fires.

        Args:
            seconds: Minimum simulation time to advance.

        Returns:
            The final SystemState after reaching the target time.
        """
        self._ensure_running()
        target_time = self._state.timestamp + seconds
        while self._state.timestamp < target_time:
            self._consume_pause_request()
            self._run_single_scan(consume_pause_request=False)
            if self._consume_pause_request():
                break
        return self._state

    def run_until(
        self,
        *conditions: Condition
        | Tag
        | Callable[[SystemState], bool]
        | tuple[Condition | Tag, ...]
        | list[Condition | Tag],
        max_cycles: int = 10000,
    ) -> SystemState:
        """Run until condition is true, pause breakpoint fires, or max_cycles reached.

        Accepts ``Tag``/``Condition`` expressions (implicit AND) or a single
        callable predicate receiving ``SystemState``.

        Args:
            conditions: Condition expressions or a single callable predicate.
            max_cycles: Maximum scans before giving up (default 10000).

        Returns:
            The state that matched the condition, or final state if max reached.
        """
        if self._is_fn_predicate(conditions):
            predicate = conditions[0]
        else:
            predicate = self._compile_condition_predicate(*conditions, method="run_until")  # ty: ignore[invalid-argument-type]
        self._ensure_running()
        for _ in range(max_cycles):
            self._consume_pause_request()
            self._run_single_scan(consume_pause_request=False)
            pause_requested = self._consume_pause_request()
            if predicate(self._state) or pause_requested:
                break
        return self._state
