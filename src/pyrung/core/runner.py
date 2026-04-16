"""PLC - Generator-driven PLC execution engine.

The runner orchestrates scan cycle execution with inversion of control.
The consumer drives execution via step(), allowing input injection,
inspection, and pause at any point.

Uses ScanContext to batch all tag/memory updates within a scan cycle,
reducing object allocation from O(instructions) to O(1) per scan.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping
from contextlib import contextmanager
from contextvars import Token
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal, TypeGuard

from pyrsistent import PMap, pmap

from pyrung.core.condition_trace import ConditionTraceEngine
from pyrung.core.context import ConditionView, ScanContext
from pyrung.core.debug_trace import RungTrace, RungTraceEvent, TraceEvent
from pyrung.core.debugger import PLCDebugger
from pyrung.core.history import History
from pyrung.core.input_overrides import InputOverrideManager
from pyrung.core.live_binding import reset_active_runner, set_active_runner
from pyrung.core.state import SystemState
from pyrung.core.system_points import (
    _BATTERY_PRESENT_KEY,
    _MODE_RUN_KEY,
    SYSTEM_TAGS_BY_NAME,
    SystemPointRuntime,
)
from pyrung.core.time_mode import TimeMode
from pyrung.core.trace_formatter import TraceFormatter

if TYPE_CHECKING:
    from collections.abc import Generator, Iterator

    from pyrung.core.analysis.causal import CausalChain
    from pyrung.core.condition import Condition
    from pyrung.core.rung import Rung
    from pyrung.core.tag import Tag

_SENTINEL = object()  # distinguishes "not passed" from None/False


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

    def rung_trace(self, rung_id: int, scan_id: int | None = None) -> RungTrace:
        """Return retained rung-level debug trace for one scan."""
        return self._plc._inspect(rung_id, scan_id)

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
        history_limit: int | None = None,
    ) -> None:
        """Create a new PLC.

        Args:
            logic: Program, list of rungs, or None for empty logic.
            initial_state: Starting state. Defaults to SystemState().
            dt: Time delta per scan in seconds (default 0.010).
                Only used in fixed-step mode.
            realtime: Use wall-clock timing instead of fixed step.
                Mutually exclusive with dt.
            history_limit: Max retained snapshots including initial state.
                Use None for unbounded history.
        """
        if realtime and dt is not None:
            raise ValueError("Cannot specify dt= with realtime=True")
        if dt is None:
            dt = 0.010
        if history_limit is not None and history_limit < 1:
            raise ValueError("history_limit must be >= 1 or None")

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
        self._history_limit = history_limit
        self._running = True
        self._battery_present = True
        self._state = self._apply_runtime_memory_flags(
            self._state,
            mode_run=self._running,
            battery_present=self._battery_present,
        )
        self._history = History(self._state, limit=history_limit)
        self._rung_traces_by_scan: dict[int, dict[int, RungTrace]] = {}
        self._rung_firings_by_scan: dict[int, PMap] = {}
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
        self._rtc_base = self._normalize_rtc_datetime(datetime.now())
        self._rtc_base_sim_time = float(self._state.timestamp)
        self._system_runtime = SystemPointRuntime(
            time_mode_getter=lambda: self._time_mode,
            fixed_step_dt_getter=lambda: self._dt,
            rtc_now_getter=self._rtc_at_sim_time,
            rtc_setter=self._set_rtc_internal,
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
        self._known_tags_by_name: dict[str, Tag] = {}
        self._refresh_known_tags_from_logic()
        # Seed initial state with tag defaults (skip tags already in state).
        seed = {
            t.name: t.default
            for t in self._known_tags_by_name.values()
            if t.name not in self._state.tags
        }
        if seed:
            self._state = self._state.with_tags(seed)
            self._history = History(self._state, limit=history_limit)

    @property
    def program(self) -> Any:
        """The Program object if the PLC was constructed from one, else None."""
        return self._program

    @property
    def current_state(self) -> SystemState:
        """Current state snapshot."""
        return self._state

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

        Returns ``PMap[int, PMap[str, Any]]`` mapping each rung index that
        wrote at least one tag during the scan to a map of
        ``{tag_name: value_written}``.  Populated uniformly by both the
        non-debug (``step()`` / ``run()``) and debug (DAP ``pyrungStepScan`` /
        continue) scan paths via ``ScanContext.capturing_rung``.

        .. todo::

            A rung whose condition is True but whose writes are identical to
            the already-pending values will not appear here.  This is an
            acceptable approximation for causal-chain attribution; for
            accurate cold-rung detection a ``_last_condition_result`` field
            on ``Rung`` may be needed later.
        """
        target = self._playhead if scan_id is None else scan_id
        return self._rung_firings_by_scan.get(target, pmap())

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
        """Lazily build and cache the static program dependency graph."""
        if not hasattr(self, "_pdg_cache") or self._pdg_cache is None:
            from pyrung.core.analysis.pdg import build_program_graph
            from pyrung.core.program import Program

            program = self._program
            if program is None:
                program = Program.__new__(Program)
                program.rungs = list(self._logic)
                program.subroutines = {}
            self._pdg_cache = build_program_graph(program)
        return self._pdg_cache

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
            )

        from pyrung.core.analysis.causal import recorded_cause

        return recorded_cause(
            logic=self._logic,
            history=self._history,
            rung_firings_fn=self.rung_firings,
            tag=tag,
            scan_id=scan,
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

    def _inspect(self, rung_id: int, scan_id: int | None = None) -> RungTrace:
        """Return retained rung-level debug trace for one scan.

        If ``scan_id`` is omitted, the current playhead scan is inspected.
        Raises:
            KeyError: Missing scan id, or missing rung trace for retained scan.
        """
        target_scan_id = self._playhead if scan_id is None else scan_id
        self.history.at(target_scan_id)

        scan_traces = self._rung_traces_by_scan.get(target_scan_id)
        if scan_traces is None:
            raise KeyError(rung_id)

        try:
            return scan_traces[rung_id]
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
        trace = self._rung_traces_by_scan.get(scan_id, {}).get(rung_id)
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
        historical_state = self.history.at(target_scan_id)
        fork = PLC(
            logic=self._program if self._program is not None else list(self._logic),
            initial_state=historical_state,
            history_limit=self._history_limit,
        )
        fork._set_time_mode(self._time_mode, dt=self._dt)
        parent_rtc_at_fork_point = self._system_runtime._rtc_now(historical_state)
        fork._set_rtc_internal(parent_rtc_at_fork_point, fork.current_state.timestamp)
        return fork

    def fork_from(self, scan_id: int) -> PLC:
        """Create an independent runner from a retained historical snapshot."""
        return self.fork(scan_id=scan_id)

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

    @property
    def battery_present(self) -> bool:
        """Simulated backup battery presence."""
        return self._battery_present

    @battery_present.setter
    def battery_present(self, value: bool) -> None:
        self._battery_present = bool(value)
        self._state = self._apply_runtime_memory_flags(
            self._state,
            mode_run=self._running,
            battery_present=self._battery_present,
        )

    def reboot(self) -> SystemState:
        """Power-cycle the runner and return the reset state."""
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
        return self._state

    def set_rtc(self, value: datetime) -> None:
        """Set the current RTC value for the runner."""
        self._set_rtc_internal(self._normalize_rtc_datetime(value), self._state.timestamp)

    def _set_time_mode(self, mode: TimeMode, *, dt: float = 0.010) -> None:
        """Set the time mode (internal, used by fork)."""
        self._time_mode = mode
        self._dt = dt
        if mode == TimeMode.REALTIME:
            self._last_step_time = time.perf_counter()

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
        self._rung_traces_by_scan.clear()
        self._clear_inflight_debug_scan()
        self._latest_committed_trace_event = None

    def _normalize_rtc_datetime(self, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value
        return value.astimezone().replace(tzinfo=None)

    def _set_rtc_internal(self, value: datetime, sim_time: float) -> None:
        self._rtc_base = value
        self._rtc_base_sim_time = float(sim_time)

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
        self._history = History(self._state, limit=self._history_limit)
        self._playhead = self._state.scan_id

        self._pending_patches.clear()
        self._forces.clear()
        self._pause_requested_this_scan = False
        self._clear_retained_debug_trace_caches()

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
        self._input_overrides.clear_forces()

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
        ctx = ScanContext(
            self._state,
            resolver=self._system_runtime.resolve,
            read_only_tags=self._system_runtime.read_only_tags,
        )

        self._system_runtime.on_scan_start(ctx)
        self._input_overrides.apply_pre_scan(ctx)

        dt = self._calculate_dt()
        ctx.set_memory("_dt", dt)
        return ctx, dt

    def _capture_previous_states(self, ctx: ScanContext) -> None:
        """Batch _prev:* updates used by edge detection conditions."""
        for name in self._state.tags:
            ctx.set_memory(f"_prev:{name}", ctx.get_tag(name))
        for name in ctx._tags_pending:
            if name not in self._state.tags:
                ctx.set_memory(f"_prev:{name}", ctx.get_tag(name))

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
        self._state = ctx.commit(dt=dt)
        # Always record firings for scans that executed logic — even an
        # empty pmap distinguishes "rung didn't fire this scan" from "no
        # data for this scan" (important for ``query.hot_rungs`` etc.).
        self._rung_firings_by_scan[self._state.scan_id] = ctx.rung_firings
        evicted_scan_ids = self._history._append(self._state)
        for evicted_scan_id in evicted_scan_ids:
            self._rung_traces_by_scan.pop(evicted_scan_id, None)
            self._rung_firings_by_scan.pop(evicted_scan_id, None)
            if (
                self._latest_committed_trace_event is not None
                and self._latest_committed_trace_event[0] == evicted_scan_id
            ):
                self._latest_committed_trace_event = None

        # Keep playhead following newest state unless manually moved.
        if self._playhead == previous_tip_scan_id:
            self._playhead = self._state.scan_id

        if not self._history.contains(self._playhead):
            self._playhead = self._history.oldest_scan_id

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
            self._rung_traces_by_scan[scan_id] = {
                rung_id: RungTrace(scan_id=scan_id, rung_id=rung_id, events=tuple(events))
                for rung_id, events in sorted(events_by_rung.items())
            }
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
