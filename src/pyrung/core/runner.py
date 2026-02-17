"""PLCRunner - Generator-driven PLC execution engine.

The runner orchestrates scan cycle execution with inversion of control.
The consumer drives execution via step(), allowing input injection,
inspection, and pause at any point.

Uses ScanContext to batch all tag/memory updates within a scan cycle,
reducing object allocation from O(instructions) to O(1) per scan.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal

from pyrung.core.context import ScanContext
from pyrung.core.live_binding import reset_active_runner, set_active_runner
from pyrung.core.state import SystemState
from pyrung.core.system_points import SystemPointRuntime
from pyrung.core.time_mode import TimeMode

if TYPE_CHECKING:
    from collections.abc import Generator, Iterator

    from pyrung.core.instruction import Instruction
    from pyrung.core.rung import Rung
    from pyrung.core.tag import Tag


@dataclass(frozen=True)
class ScanStep:
    """Debug scan step emitted at rung boundaries."""

    rung_index: int
    rung: Rung
    ctx: ScanContext
    kind: Literal["rung", "branch", "subroutine"]
    subroutine_name: str | None
    depth: int
    call_stack: tuple[str, ...]


class PLCRunner:
    """Generator-driven PLC execution engine.

    Executes PLC logic as pure functions: Logic(state) -> new_state.
    The consumer controls execution via step(), enabling:
    - Input injection via patch()
    - Inspection of any historical state
    - Pause/resume at any scan boundary

    Attributes:
        current_state: The current SystemState snapshot.
        simulation_time: Current simulation clock (seconds).
        time_mode: Current time mode (FIXED_STEP or REALTIME).
    """

    def __init__(
        self,
        logic: list[Any] | Any = None,
        initial_state: SystemState | None = None,
    ) -> None:
        """Create a new PLCRunner.

        Args:
            logic: Program, list of rungs, or None for empty logic.
            initial_state: Starting state. Defaults to SystemState().
        """
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
        self._pending_patches: dict[str, bool | int | float | str] = {}
        self._forces: dict[str, bool | int | float | str] = {}
        self._time_mode = TimeMode.FIXED_STEP
        self._dt = 0.1  # Default: 100ms per scan
        self._last_step_time: float | None = None  # For REALTIME mode
        self._system_runtime = SystemPointRuntime(
            time_mode_getter=lambda: self._time_mode,
            fixed_step_dt_getter=lambda: self._dt,
        )

    @property
    def current_state(self) -> SystemState:
        """Current state snapshot."""
        return self._state

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

    def _normalize_tag_name(self, tag: str | Tag, *, method: str) -> str:
        from pyrung.core.tag import Tag as TagClass

        if isinstance(tag, TagClass):
            return tag.name
        if isinstance(tag, str):
            return tag
        raise TypeError(f"{method}() keys must be str or Tag, got {type(tag).__name__}")

    def _normalize_tag_updates(
        self,
        tags: Mapping[str, bool | int | float | str]
        | Mapping[Tag, bool | int | float | str]
        | Mapping[str | Tag, bool | int | float | str],
        *,
        method: str,
    ) -> dict[str, bool | int | float | str]:
        normalized: dict[str, bool | int | float | str] = {}
        for key, value in tags.items():
            name = self._normalize_tag_name(key, method=method)
            if self._system_runtime.is_read_only(name):
                raise ValueError(f"Tag '{name}' is read-only system point and cannot be written")
            normalized[name] = value
        return normalized

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
        self._pending_patches.update(self._normalize_tag_updates(tags, method="patch"))

    def add_force(self, tag: str | Tag, value: bool | int | float | str) -> None:
        """Persistently override a tag until removed."""
        name = self._normalize_tag_name(tag, method="add_force")
        if self._system_runtime.is_read_only(name):
            raise ValueError(f"Tag '{name}' is read-only system point and cannot be written")
        self._forces[name] = value

    def remove_force(self, tag: str | Tag) -> None:
        """Remove a single forced tag override."""
        name = self._normalize_tag_name(tag, method="remove_force")
        if name not in self._forces:
            raise KeyError(name)
        del self._forces[name]

    def clear_forces(self) -> None:
        """Remove all forced tag overrides."""
        self._forces = {}

    @contextmanager
    def force(
        self,
        overrides: Mapping[str, bool | int | float | str]
        | Mapping[Tag, bool | int | float | str]
        | Mapping[str | Tag, bool | int | float | str],
    ) -> Iterator[PLCRunner]:
        """Temporarily apply forces within a context manager."""
        snapshot = self._forces.copy()
        try:
            for tag, value in overrides.items():
                self.add_force(tag, value)
            yield self
        finally:
            self._forces = snapshot

    @property
    def forces(self) -> Mapping[str, bool | int | float | str]:
        """Read-only view of active persistent overrides."""
        return MappingProxyType(self._forces)

    def _peek_live_tag_value(self, name: str, default: Any) -> Any:
        """Read a tag as seen by live Tag.value access."""
        if name in self._pending_patches:
            return self._pending_patches[name]
        if name in self._forces:
            return self._forces[name]

        resolved, value = self._system_runtime.resolve(name, self._state)
        if resolved:
            return value

        return self._state.tags.get(name, default)

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
        # Create ScanContext for batched updates + system resolver
        ctx = ScanContext(
            self._state,
            resolver=self._system_runtime.resolve,
            read_only_tags=self._system_runtime.read_only_tags,
        )

        # System lifecycle hooks run before patching and logic
        self._system_runtime.on_scan_start(ctx)

        # Apply one-shot patches to context
        if self._pending_patches:
            ctx.set_tags(self._pending_patches)
            self._pending_patches = {}

        # Apply persistent force overrides before logic evaluation
        if self._forces:
            ctx.set_tags(self._forces)

        # Calculate dt based on time mode (needed for timers)
        if self._time_mode == TimeMode.REALTIME:
            now = time.perf_counter()
            if self._last_step_time is None:
                self._last_step_time = now
            dt = now - self._last_step_time
            self._last_step_time = now
        else:
            dt = self._dt

        # Inject dt into context so timer instructions can access it
        ctx.set_memory("_dt", dt)

        # Evaluate logic rung-by-rung, yielding at rung boundaries.
        for i, rung in enumerate(self._logic):
            rung.evaluate(ctx)
            yield i, rung, ctx

        # Re-apply forces after logic so they persist across scans
        if self._forces:
            ctx.set_tags(self._forces)

        # Batch _prev:* updates for edge detection
        # Store current tag values as previous for next scan
        # Need to include both original tags and any newly created tags
        for name in self._state.tags:
            ctx.set_memory(f"_prev:{name}", ctx.get_tag(name))
        # Also capture newly created tags from pending writes
        for name in ctx._tags_pending:
            if name not in self._state.tags:
                ctx.set_memory(f"_prev:{name}", ctx.get_tag(name))

        # Final system updates before commit
        self._system_runtime.on_scan_end(ctx)

        # Single commit: apply all changes and advance scan
        self._state = ctx.commit(dt=dt)

    def scan_steps_debug(self) -> Generator[ScanStep, None, None]:
        """Execute one scan cycle and yield at top-level, branch, and subroutine boundaries."""
        # Create ScanContext for batched updates + system resolver
        ctx = ScanContext(
            self._state,
            resolver=self._system_runtime.resolve,
            read_only_tags=self._system_runtime.read_only_tags,
        )

        # System lifecycle hooks run before patching and logic
        self._system_runtime.on_scan_start(ctx)

        # Apply one-shot patches to context
        if self._pending_patches:
            ctx.set_tags(self._pending_patches)
            self._pending_patches = {}

        # Apply persistent force overrides before logic evaluation
        if self._forces:
            ctx.set_tags(self._forces)

        # Calculate dt based on time mode (needed for timers)
        if self._time_mode == TimeMode.REALTIME:
            now = time.perf_counter()
            if self._last_step_time is None:
                self._last_step_time = now
            dt = now - self._last_step_time
            self._last_step_time = now
        else:
            dt = self._dt

        # Inject dt into context so timer instructions can access it
        ctx.set_memory("_dt", dt)

        # Evaluate logic rung-by-rung with nested yield points for debugger stepping.
        for i, rung in enumerate(self._logic):
            yield from self._iter_rung_steps(
                rung_index=i,
                rung=rung,
                ctx=ctx,
                kind="rung",
                depth=0,
                subroutine_name=None,
                call_stack=(),
                force_false=False,
            )

        # Re-apply forces after logic so they persist across scans
        if self._forces:
            ctx.set_tags(self._forces)

        # Batch _prev:* updates for edge detection
        # Store current tag values as previous for next scan
        # Need to include both original tags and any newly created tags
        for name in self._state.tags:
            ctx.set_memory(f"_prev:{name}", ctx.get_tag(name))
        # Also capture newly created tags from pending writes
        for name in ctx._tags_pending:
            if name not in self._state.tags:
                ctx.set_memory(f"_prev:{name}", ctx.get_tag(name))

        # Final system updates before commit
        self._system_runtime.on_scan_end(ctx)

        # Single commit: apply all changes and advance scan
        self._state = ctx.commit(dt=dt)

    def _iter_rung_steps(
        self,
        *,
        rung_index: int,
        rung: Rung,
        ctx: ScanContext,
        kind: Literal["rung", "branch", "subroutine"],
        depth: int,
        subroutine_name: str | None,
        call_stack: tuple[str, ...],
        force_false: bool,
    ) -> Generator[ScanStep, None, None]:
        from pyrung.core.instruction import SubroutineReturnSignal

        if force_false:
            self._handle_rung_false_debug(rung, ctx)
            for branch in rung._branches:
                yield from self._iter_rung_steps(
                    rung_index=rung_index,
                    rung=branch,
                    ctx=ctx,
                    kind="branch",
                    depth=depth + 1,
                    subroutine_name=subroutine_name,
                    call_stack=call_stack,
                    force_false=True,
                )
            if kind != "branch":
                yield ScanStep(
                    rung_index=rung_index,
                    rung=rung,
                    ctx=ctx,
                    kind=kind,
                    subroutine_name=subroutine_name,
                    depth=depth,
                    call_stack=call_stack,
                )
            return

        conditions_true = rung._evaluate_conditions(ctx)
        if conditions_true:
            try:
                for instruction in rung._instructions:
                    yield from self._iter_instruction_steps(
                        rung_index=rung_index,
                        instruction=instruction,
                        ctx=ctx,
                        depth=depth,
                        call_stack=call_stack,
                    )
                for branch in rung._branches:
                    yield from self._iter_rung_steps(
                        rung_index=rung_index,
                        rung=branch,
                        ctx=ctx,
                        kind="branch",
                        depth=depth + 1,
                        subroutine_name=subroutine_name,
                        call_stack=call_stack,
                        force_false=False,
                    )
            except SubroutineReturnSignal:
                if kind != "branch":
                    yield ScanStep(
                        rung_index=rung_index,
                        rung=rung,
                        ctx=ctx,
                        kind=kind,
                        subroutine_name=subroutine_name,
                        depth=depth,
                        call_stack=call_stack,
                    )
                raise
        else:
            self._handle_rung_false_debug(rung, ctx)
            for branch in rung._branches:
                yield from self._iter_rung_steps(
                    rung_index=rung_index,
                    rung=branch,
                    ctx=ctx,
                    kind="branch",
                    depth=depth + 1,
                    subroutine_name=subroutine_name,
                    call_stack=call_stack,
                    force_false=True,
                )

        if kind != "branch" or conditions_true:
            yield ScanStep(
                rung_index=rung_index,
                rung=rung,
                ctx=ctx,
                kind=kind,
                subroutine_name=subroutine_name,
                depth=depth,
                call_stack=call_stack,
            )

    def _iter_instruction_steps(
        self,
        *,
        rung_index: int,
        instruction: Instruction,
        ctx: ScanContext,
        depth: int,
        call_stack: tuple[str, ...],
    ) -> Generator[ScanStep, None, None]:
        from pyrung.core.instruction import (
            CallInstruction,
            ForLoopInstruction,
            SubroutineReturnSignal,
            resolve_tag_or_value_ctx,
        )

        if isinstance(instruction, CallInstruction):
            if instruction.subroutine_name not in instruction._program.subroutines:
                raise KeyError(f"Subroutine '{instruction.subroutine_name}' not defined")
            next_stack = (*call_stack, instruction.subroutine_name)
            try:
                for sub_rung in instruction._program.subroutines[instruction.subroutine_name]:
                    yield from self._iter_rung_steps(
                        rung_index=rung_index,
                        rung=sub_rung,
                        ctx=ctx,
                        kind="subroutine",
                        depth=depth + 1,
                        subroutine_name=instruction.subroutine_name,
                        call_stack=next_stack,
                        force_false=False,
                    )
            except SubroutineReturnSignal:
                return
            return

        if isinstance(instruction, ForLoopInstruction):
            if not instruction.should_execute():
                return

            count_value = resolve_tag_or_value_ctx(instruction.count, ctx)
            iterations = max(0, int(count_value))

            for i in range(iterations):
                # Keep loop index in tag space so indirect refs resolve via ctx.get_tag().
                ctx.set_tag(instruction.idx_tag.name, i)
                for child in instruction.instructions:
                    yield from self._iter_instruction_steps(
                        rung_index=rung_index,
                        instruction=child,
                        ctx=ctx,
                        depth=depth,
                        call_stack=call_stack,
                    )
            return

        instruction.execute(ctx)

    def _handle_rung_false_debug(self, rung: Rung, ctx: ScanContext) -> None:
        """Mirror Rung._handle_rung_false without recursively walking branches."""
        rung._execute_always_instructions(ctx)

        for tag in rung._coils:
            ctx.set_tag(tag.name, tag.default)

        for instruction in rung._instructions:
            reset_oneshot = getattr(instruction, "reset_oneshot", None)
            if callable(reset_oneshot):
                reset_oneshot()

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
