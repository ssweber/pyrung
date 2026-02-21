"""PLCRunner - Generator-driven PLC execution engine.

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
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from pyrung.core.condition_trace import ConditionTraceEngine
from pyrung.core.context import ScanContext
from pyrung.core.debug_trace import RungTrace, RungTraceEvent, TraceEvent
from pyrung.core.debugger import PLCDebugger
from pyrung.core.history import History
from pyrung.core.input_overrides import InputOverrideManager
from pyrung.core.live_binding import reset_active_runner, set_active_runner
from pyrung.core.state import SystemState
from pyrung.core.system_points import SystemPointRuntime
from pyrung.core.time_mode import TimeMode
from pyrung.core.trace_formatter import TraceFormatter

if TYPE_CHECKING:
    from collections.abc import Generator, Iterator

    from pyrung.core.rung import Rung
    from pyrung.core.tag import Tag


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


class PLCRunner:
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
        history_limit: int | None = None,
    ) -> None:
        """Create a new PLCRunner.

        Args:
            logic: Program, list of rungs, or None for empty logic.
            initial_state: Starting state. Defaults to SystemState().
            history_limit: Max retained snapshots including initial state.
                Use None for unbounded history.
        """
        if history_limit is not None and history_limit < 1:
            raise ValueError("history_limit must be >= 1 or None")

        self._logic: list[Rung]
        # Handle different logic types
        # Import Program here to avoid circular import at module level
        from pyrung.core.program import Program

        if logic is None:
            self._logic = []
        elif isinstance(logic, Program):
            self._logic = logic.rungs
        elif isinstance(logic, list):
            self._logic = logic
        else:
            self._logic = [logic]

        self._state = initial_state if initial_state is not None else SystemState()
        self._history_limit = history_limit
        self._history = History(self._state, limit=history_limit)
        self._rung_traces_by_scan: dict[int, dict[int, RungTrace]] = {}
        self._playhead = self._state.scan_id
        self._time_mode = TimeMode.FIXED_STEP
        self._dt = 0.1  # Default: 100ms per scan
        self._last_step_time: float | None = None  # For REALTIME mode
        self._system_runtime = SystemPointRuntime(
            time_mode_getter=lambda: self._time_mode,
            fixed_step_dt_getter=lambda: self._dt,
        )
        self._input_overrides = InputOverrideManager(is_read_only=self._system_runtime.is_read_only)
        # Preserve direct access used in tests/live-tag helpers.
        self._pending_patches = self._input_overrides.pending_patches
        self._forces = self._input_overrides.forces_mutable
        self._condition_trace = ConditionTraceEngine(formatter=TraceFormatter())
        self._debugger = PLCDebugger(step_factory=ScanStep)

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

    def inspect(self, rung_id: int, scan_id: int | None = None) -> RungTrace:
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

    def fork_from(self, scan_id: int) -> PLCRunner:
        """Create an independent runner from a retained historical snapshot."""
        historical_state = self.history.at(scan_id)
        fork = PLCRunner(
            logic=list(self._logic),
            initial_state=historical_state,
            history_limit=self._history_limit,
        )
        fork.set_time_mode(self._time_mode, dt=self._dt)
        return fork

    @property
    def simulation_time(self) -> float:
        """Current simulation clock in seconds."""
        return self._state.timestamp

    @property
    def time_mode(self) -> TimeMode:
        """Current time mode."""
        return self._time_mode

    @property
    def system_runtime(self) -> SystemPointRuntime:
        """System point runtime component."""
        return self._system_runtime

    def set_time_mode(self, mode: TimeMode, dt: float = 0.1) -> None:
        """Set the time mode for simulation.

        Args:
            mode: TimeMode.FIXED_STEP or TimeMode.REALTIME.
            dt: Time delta per scan (only used for FIXED_STEP mode).
        """
        self._time_mode = mode
        self._dt = dt
        if mode == TimeMode.REALTIME:
            self._last_step_time = time.perf_counter()

    @contextmanager
    def active(self) -> Iterator[PLCRunner]:
        """Bind this runner as active for live Tag.value access."""
        token = set_active_runner(self)
        try:
            yield self
        finally:
            reset_active_runner(token)

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
        self._input_overrides.patch(tags)

    def add_force(self, tag: str | Tag, value: bool | int | float | str) -> None:
        """Persistently override a tag value until explicitly removed.

        The forced value is applied at the pre-logic force pass (phase 3) and
        re-applied at the post-logic force pass (phase 5) every scan.  Logic
        may temporarily diverge the value mid-scan, but the post-logic pass
        restores it before outputs are written.

        Forces persist across scans until `remove_force()` or `clear_forces()`
        is called.  Multiple forces may be active simultaneously.

        If a tag is both patched and forced in the same scan, the force
        overwrites the patch during the pre-logic pass.

        Args:
            tag: Tag name or `Tag` object to override.
            value: Value to hold. Must be compatible with the tag's type.

        Raises:
            ValueError: If the tag is a read-only system point.
        """
        self._input_overrides.add_force(tag, value)

    def remove_force(self, tag: str | Tag) -> None:
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
    def force(
        self,
        overrides: Mapping[str, bool | int | float | str]
        | Mapping[Tag, bool | int | float | str]
        | Mapping[str | Tag, bool | int | float | str],
    ) -> Iterator[PLCRunner]:
        """Temporarily apply forces for the duration of the context.

        On entry, saves the current force map and adds the given overrides.
        On exit (normally or on exception), the exact previous force map is
        restored — forces that existed before the context are reinstated, and
        forces added inside the context are removed.

        Safe for nesting: inner ``force()`` contexts layer on top of outer ones
        without disrupting them.

        Args:
            overrides: Mapping of tag name / ``Tag`` object to forced value.

        Example:
            ```python
            with runner.force({"AutoMode": True, "Fault": False}):
                runner.run(5)
            # AutoMode and Fault forces released here
            ```
        """
        with self._input_overrides.force(overrides):
            yield self

    @property
    def forces(self) -> Mapping[str, bool | int | float | str]:
        """Read-only view of active persistent overrides."""
        return self._input_overrides.forces

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
        """Finalize one scan and commit all batched writes."""
        previous_tip_scan_id = self._state.scan_id
        self._input_overrides.apply_post_logic(ctx)

        self._capture_previous_states(ctx)
        self._system_runtime.on_scan_end(ctx)
        self._state = ctx.commit(dt=dt)
        evicted_scan_ids = self._history._append(self._state)
        for evicted_scan_id in evicted_scan_ids:
            self._rung_traces_by_scan.pop(evicted_scan_id, None)

        # Keep playhead following newest state unless manually moved.
        if self._playhead == previous_tip_scan_id:
            self._playhead = self._state.scan_id

        if not self._history.contains(self._playhead):
            self._playhead = self._history.oldest_scan_id

    def scan_steps(self) -> Generator[tuple[int, Rung, ScanContext], None, None]:
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
        ctx, dt = self._prepare_scan()

        # Evaluate logic rung-by-rung, yielding at rung boundaries.
        for i, rung in enumerate(self._logic):
            rung.evaluate(ctx)
            yield i, rung, ctx

        self._commit_scan(ctx, dt)

    def scan_steps_debug(self) -> Generator[ScanStep, None, None]:
        """Execute one scan cycle and yield ``ScanStep`` objects at all boundaries.

        Yields a `ScanStep` at each:

        - Top-level rung boundary (``kind="rung"``)
        - Branch entry / exit (``kind="branch"``)
        - Subroutine call and body steps (``kind="subroutine"``)
        - Individual instruction boundaries (``kind="instruction"``)

        Each `ScanStep` carries source location metadata (``source_file``,
        ``source_line``, ``end_line``), rung enable state, and a trace of
        evaluated conditions and instructions.

        This is the API used by the DAP adapter.  Prefer `scan_steps()` for
        non-debug consumers — it has less overhead and a simpler yield type.

        Note:
            Like `scan_steps()`, the scan is committed only when the generator
            is **fully exhausted**.
        """
        events_by_rung: dict[int, list[RungTraceEvent]] = {}
        for step in self._debugger.scan_steps_debug(self):
            events_by_rung.setdefault(step.rung_index, []).append(
                RungTraceEvent(
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
            )
            yield step

        scan_id = self._state.scan_id
        self._rung_traces_by_scan[scan_id] = {
            rung_id: RungTrace(scan_id=scan_id, rung_id=rung_id, events=tuple(events))
            for rung_id, events in sorted(events_by_rung.items())
        }

    def prepare_scan(self) -> tuple[ScanContext, float]:
        """Debugger-facing scan preparation API."""
        return self._prepare_scan()

    def commit_scan(self, ctx: ScanContext, dt: float) -> None:
        """Debugger-facing scan commit API."""
        self._commit_scan(ctx, dt)

    def iter_top_level_rungs(self) -> Iterable[Rung]:
        """Debugger-facing top-level rung iterator."""
        return self._logic

    def evaluate_condition_value(
        self,
        condition: Any,
        ctx: ScanContext,
    ) -> tuple[bool, list[dict[str, Any]]]:
        """Debugger-facing condition evaluation API."""
        return self._evaluate_condition_value(condition, ctx)

    def condition_term_text(self, condition: Any, details: list[dict[str, Any]]) -> str:
        """Debugger-facing condition summary API."""
        return self._condition_term_text(condition, details)

    def condition_annotation(self, *, status: str, expression: str, summary: str) -> str:
        """Debugger-facing annotation API."""
        return self._condition_annotation(
            status=status,
            expression=expression,
            summary=summary,
        )

    def condition_expression(self, condition: Any) -> str:
        """Debugger-facing expression rendering API."""
        return self._condition_expression(condition)

    def _evaluate_condition_value(
        self,
        condition: Any,
        ctx: ScanContext,
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
        for _ in self.scan_steps():
            pass

        return self._state

    def run(self, cycles: int) -> SystemState:
        """Execute multiple scan cycles.

        Args:
            cycles: Number of scans to execute.

        Returns:
            The final SystemState after all cycles.
        """
        for _ in range(cycles):
            self.step()
        return self._state

    def run_for(self, seconds: float) -> SystemState:
        """Run until simulation time advances by at least N seconds.

        Args:
            seconds: Minimum simulation time to advance.

        Returns:
            The final SystemState after reaching the target time.
        """
        target_time = self._state.timestamp + seconds
        while self._state.timestamp < target_time:
            self.step()
        return self._state

    def run_until(
        self,
        predicate: Callable[[SystemState], bool],
        max_cycles: int = 10000,
    ) -> SystemState:
        """Run until predicate returns True or max_cycles reached.

        Args:
            predicate: Function that takes SystemState and returns bool.
            max_cycles: Maximum scans before giving up (default 10000).

        Returns:
            The state that matched the predicate, or final state if max reached.
        """
        for _ in range(max_cycles):
            self.step()
            if predicate(self._state):
                break
        return self._state
