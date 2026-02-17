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
    kind: Literal["rung", "branch", "subroutine", "instruction"]
    subroutine_name: str | None
    depth: int
    call_stack: tuple[str, ...]
    source_file: str | None
    source_line: int | None
    end_line: int | None
    enabled_state: Literal["enabled", "disabled_local", "disabled_parent"] | None
    trace: dict[str, Any] | None
    instruction_kind: str | None


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
            enabled, rung_condition_traces = self._evaluate_conditions_with_trace(rung._conditions, ctx)
            yield from self._iter_rung_steps(
                rung_index=i,
                rung=rung,
                ctx=ctx,
                kind="rung",
                depth=0,
                subroutine_name=None,
                call_stack=(),
                enabled=enabled,
                parent_enabled=True,
                rung_condition_traces=rung_condition_traces,
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
        enabled: bool,
        parent_enabled: bool,
        rung_condition_traces: list[dict[str, Any]],
    ) -> Generator[ScanStep, None, None]:
        from pyrung.core.instruction import SubroutineReturnSignal
        from pyrung.core.rung import Rung as RungClass

        enabled_state = self._enabled_state_for(kind=kind, enabled=enabled, parent_enabled=parent_enabled)
        branch_enable_map: dict[int, bool] = {}
        branch_trace_map: dict[int, dict[str, Any]] = {}
        for item in rung._execution_items:
            if not isinstance(item, RungClass):
                continue
            local_conditions = item._conditions[item._branch_condition_start :]
            if enabled:
                local_enabled, local_traces = self._evaluate_conditions_with_trace(local_conditions, ctx)
                branch_state: Literal["enabled", "disabled_local", "disabled_parent"]
                branch_state = "enabled" if local_enabled else "disabled_local"
            else:
                local_enabled = False
                local_traces = [self._skipped_condition_trace(cond) for cond in local_conditions]
                branch_state = "disabled_parent"
            branch_enable_map[id(item)] = local_enabled
            branch_trace_map[id(item)] = {"enabled_state": branch_state, "conditions": local_traces}

        step_trace = self._build_step_trace(
            kind=kind,
            rung=rung,
            enabled_state=enabled_state,
            rung_condition_traces=rung_condition_traces,
            branch_trace_map=branch_trace_map,
        )
        try:
            for item in rung._execution_items:
                if isinstance(item, RungClass):
                    branch_enabled = branch_enable_map.get(id(item), False)
                    branch_trace = branch_trace_map.get(id(item), {})
                    yield from self._iter_rung_steps(
                        rung_index=rung_index,
                        rung=item,
                        ctx=ctx,
                        kind="branch",
                        depth=depth + 1,
                        subroutine_name=subroutine_name,
                        call_stack=call_stack,
                        enabled=branch_enabled,
                        parent_enabled=enabled,
                        rung_condition_traces=list(branch_trace.get("conditions", [])),
                    )
                else:
                    yield from self._iter_instruction_steps(
                        rung_index=rung_index,
                        rung=rung,
                        kind=kind,
                        subroutine_name=subroutine_name,
                        instruction=item,
                        ctx=ctx,
                        depth=depth,
                        call_stack=call_stack,
                        enabled=enabled,
                        enabled_state=enabled_state,
                        step_trace=step_trace,
                    )
        except SubroutineReturnSignal:
            if kind != "branch":
                yield self._make_scan_step(
                    rung_index=rung_index,
                    rung=rung,
                    ctx=ctx,
                    kind=kind,
                    subroutine_name=subroutine_name,
                    depth=depth,
                    call_stack=call_stack,
                    source_file=rung.source_file,
                    source_line=rung.source_line,
                    end_line=rung.end_line,
                    enabled_state=enabled_state,
                    trace=step_trace,
                    instruction_kind=None,
                )
            raise

        if kind != "branch" or enabled:
            yield self._make_scan_step(
                rung_index=rung_index,
                rung=rung,
                ctx=ctx,
                kind=kind,
                subroutine_name=subroutine_name,
                depth=depth,
                call_stack=call_stack,
                source_file=rung.source_file,
                source_line=rung.source_line,
                end_line=rung.end_line,
                enabled_state=enabled_state,
                trace=step_trace,
                instruction_kind=None,
            )

    def _iter_instruction_steps(
        self,
        *,
        rung_index: int,
        rung: Rung,
        kind: Literal["rung", "branch", "subroutine"],
        subroutine_name: str | None,
        instruction: Instruction,
        ctx: ScanContext,
        depth: int,
        call_stack: tuple[str, ...],
        enabled: bool,
        enabled_state: Literal["enabled", "disabled_local", "disabled_parent"],
        step_trace: dict[str, Any],
    ) -> Generator[ScanStep, None, None]:
        from pyrung.core.instruction import (
            CallInstruction,
            ForLoopInstruction,
            SubroutineReturnSignal,
            resolve_tag_or_value_ctx,
        )

        if isinstance(instruction, CallInstruction):
            if not enabled:
                instruction.execute(ctx, enabled)
                return
            if instruction.subroutine_name not in instruction._program.subroutines:
                raise KeyError(f"Subroutine '{instruction.subroutine_name}' not defined")
            yield self._make_scan_step(
                rung_index=rung_index,
                rung=rung,
                ctx=ctx,
                kind="instruction",
                subroutine_name=subroutine_name,
                depth=depth,
                call_stack=call_stack,
                source_file=getattr(instruction, "source_file", None) or rung.source_file,
                source_line=getattr(instruction, "source_line", None) or rung.source_line,
                end_line=(
                    getattr(instruction, "end_line", None)
                    or getattr(instruction, "source_line", None)
                    or rung.source_line
                ),
                enabled_state=enabled_state,
                trace=step_trace,
                instruction_kind=instruction.__class__.__name__,
            )
            next_stack = (*call_stack, instruction.subroutine_name)
            try:
                for sub_rung in instruction._program.subroutines[instruction.subroutine_name]:
                    sub_enabled, sub_condition_traces = self._evaluate_conditions_with_trace(
                        sub_rung._conditions, ctx
                    )
                    yield from self._iter_rung_steps(
                        rung_index=rung_index,
                        rung=sub_rung,
                        ctx=ctx,
                        kind="subroutine",
                        depth=depth + 1,
                        subroutine_name=instruction.subroutine_name,
                        call_stack=next_stack,
                        enabled=sub_enabled,
                        parent_enabled=True,
                        rung_condition_traces=sub_condition_traces,
                    )
            except SubroutineReturnSignal:
                return
            return

        if isinstance(instruction, ForLoopInstruction):
            if not enabled:
                instruction.execute(ctx, enabled)
                return

            if not instruction.should_execute(enabled):
                return

            count_value = resolve_tag_or_value_ctx(instruction.count, ctx)
            iterations = max(0, int(count_value))

            for i in range(iterations):
                # Keep loop index in tag space so indirect refs resolve via ctx.get_tag().
                ctx.set_tag(instruction.idx_tag.name, i)
                for child in instruction.instructions:
                    yield from self._iter_instruction_steps(
                        rung_index=rung_index,
                        rung=rung,
                        kind=kind,
                        subroutine_name=subroutine_name,
                        instruction=child,
                        ctx=ctx,
                        depth=depth,
                        call_stack=call_stack,
                        enabled=True,
                        enabled_state="enabled",
                        step_trace=step_trace,
                    )
            return

        if not enabled and instruction.is_inert_when_disabled():
            instruction.execute(ctx, enabled)
            return

        yield self._make_scan_step(
            rung_index=rung_index,
            rung=rung,
            ctx=ctx,
            kind="instruction",
            subroutine_name=subroutine_name,
            depth=depth,
            call_stack=call_stack,
            source_file=getattr(instruction, "source_file", None) or rung.source_file,
            source_line=getattr(instruction, "source_line", None) or rung.source_line,
            end_line=(
                getattr(instruction, "end_line", None)
                or getattr(instruction, "source_line", None)
                or rung.source_line
            ),
            enabled_state=enabled_state,
            trace=step_trace,
            instruction_kind=instruction.__class__.__name__,
        )
        instruction.execute(ctx, enabled)

    def _make_scan_step(
        self,
        *,
        rung_index: int,
        rung: Rung,
        ctx: ScanContext,
        kind: Literal["rung", "branch", "subroutine", "instruction"],
        subroutine_name: str | None,
        depth: int,
        call_stack: tuple[str, ...],
        source_file: str | None,
        source_line: int | None,
        end_line: int | None,
        enabled_state: Literal["enabled", "disabled_local", "disabled_parent"] | None,
        trace: dict[str, Any] | None,
        instruction_kind: str | None,
    ) -> ScanStep:
        return ScanStep(
            rung_index=rung_index,
            rung=rung,
            ctx=ctx,
            kind=kind,
            subroutine_name=subroutine_name,
            depth=depth,
            call_stack=call_stack,
            source_file=source_file,
            source_line=source_line,
            end_line=end_line,
            enabled_state=enabled_state,
            trace=trace,
            instruction_kind=instruction_kind,
        )

    def _enabled_state_for(
        self,
        *,
        kind: Literal["rung", "branch", "subroutine"],
        enabled: bool,
        parent_enabled: bool,
    ) -> Literal["enabled", "disabled_local", "disabled_parent"]:
        if enabled:
            return "enabled"
        if kind == "branch" and not parent_enabled:
            return "disabled_parent"
        return "disabled_local"

    def _build_step_trace(
        self,
        *,
        kind: Literal["rung", "branch", "subroutine"],
        rung: Rung,
        enabled_state: Literal["enabled", "disabled_local", "disabled_parent"],
        rung_condition_traces: list[dict[str, Any]],
        branch_trace_map: dict[int, dict[str, Any]],
    ) -> dict[str, Any]:
        from pyrung.core.rung import Rung as RungClass

        regions: list[dict[str, Any]] = [
            {
                "kind": "branch" if kind == "branch" else "rung",
                "source_file": rung.source_file,
                "source_line": rung.source_line,
                "end_line": self._effective_region_end_line(rung),
                "enabled_state": enabled_state,
                "conditions": rung_condition_traces,
            }
        ]

        for item in rung._execution_items:
            if not isinstance(item, RungClass):
                continue
            branch_trace = branch_trace_map.get(id(item), {})
            regions.append(
                {
                    "kind": "branch",
                    "source_file": item.source_file,
                    "source_line": item.source_line,
                    "end_line": self._effective_region_end_line(item),
                    "enabled_state": branch_trace.get("enabled_state", "disabled_local"),
                    "conditions": list(branch_trace.get("conditions", [])),
                }
            )

        return {"regions": regions}

    def _effective_region_end_line(self, rung: Rung) -> int | None:
        if rung.end_line is not None:
            return int(rung.end_line)

        lines: list[int] = []
        if rung.source_line is not None:
            lines.append(int(rung.source_line))
        self._collect_rung_instruction_lines(rung, lines)
        if not lines:
            return None
        return max(lines)

    def _collect_rung_instruction_lines(self, rung: Rung, lines: list[int]) -> None:
        from pyrung.core.instruction import Instruction
        from pyrung.core.rung import Rung as RungClass

        for item in rung._execution_items:
            if isinstance(item, RungClass):
                self._collect_rung_instruction_lines(item, lines)
                continue
            if isinstance(item, Instruction):
                line = getattr(item, "source_line", None)
                if line is not None:
                    lines.append(int(line))
                end_line = getattr(item, "end_line", None)
                if end_line is not None:
                    lines.append(int(end_line))
                nested = getattr(item, "instructions", None)
                if isinstance(nested, list):
                    for child in nested:
                        if not isinstance(child, Instruction):
                            continue
                        child_line = getattr(child, "source_line", None)
                        if child_line is not None:
                            lines.append(int(child_line))
                        child_end_line = getattr(child, "end_line", None)
                        if child_end_line is not None:
                            lines.append(int(child_end_line))

    def _evaluate_conditions_with_trace(
        self,
        conditions: list[Any],
        ctx: ScanContext,
    ) -> tuple[bool, list[dict[str, Any]]]:
        if not conditions:
            return True, []

        traces: list[dict[str, Any]] = []
        enabled = True
        for condition in conditions:
            if not enabled:
                traces.append(self._skipped_condition_trace(condition))
                continue
            value, details = self._evaluate_condition_value(condition, ctx)
            if not value:
                enabled = False
            traces.append(
                {
                    "source_file": getattr(condition, "source_file", None),
                    "source_line": getattr(condition, "source_line", None),
                    "expression": self._condition_expression(condition),
                    "status": "true" if value else "false",
                    "value": value,
                    "details": details,
                }
            )
        return enabled, traces

    def _skipped_condition_trace(self, condition: Any) -> dict[str, Any]:
        return {
            "source_file": getattr(condition, "source_file", None),
            "source_line": getattr(condition, "source_line", None),
            "expression": self._condition_expression(condition),
            "status": "skipped",
            "value": None,
            "details": [],
        }

    def _evaluate_condition_value(
        self,
        condition: Any,
        ctx: ScanContext,
    ) -> tuple[bool, list[dict[str, Any]]]:
        from pyrung.core.condition import (
            AllCondition,
            AnyCondition,
            BitCondition,
            CompareEq,
            CompareGe,
            CompareGt,
            CompareLe,
            CompareLt,
            CompareNe,
            FallingEdgeCondition,
            IndirectCompareEq,
            IndirectCompareGe,
            IndirectCompareGt,
            IndirectCompareLe,
            IndirectCompareLt,
            IndirectCompareNe,
            IntTruthyCondition,
            NormallyClosedCondition,
            RisingEdgeCondition,
        )
        from pyrung.core.expression import (
            ExprCompareEq,
            ExprCompareGe,
            ExprCompareGt,
            ExprCompareLe,
            ExprCompareLt,
            ExprCompareNe,
            Expression,
        )
        from pyrung.core.memory_block import IndirectExprRef, IndirectRef
        from pyrung.core.tag import Tag

        def _detail(name: str, value: Any) -> dict[str, Any]:
            return {"name": name, "value": value}

        def _resolve_operand(value: Any) -> Any:
            if isinstance(value, Expression):
                return value.evaluate(ctx)
            if isinstance(value, Tag):
                return ctx.get_tag(value.name, value.default)
            if isinstance(value, (IndirectRef, IndirectExprRef)):
                target = value.resolve_ctx(ctx)
                return ctx.get_tag(target.name, target.default)
            return value

        if isinstance(condition, BitCondition):
            value = bool(ctx.get_tag(condition.tag.name, False))
            return value, [_detail("tag", condition.tag.name), _detail("value", value)]

        if isinstance(condition, IntTruthyCondition):
            raw = ctx.get_tag(condition.tag.name, condition.tag.default)
            value = int(raw) != 0
            return value, [_detail("tag", condition.tag.name), _detail("value", raw)]

        if isinstance(condition, NormallyClosedCondition):
            raw = bool(ctx.get_tag(condition.tag.name, False))
            value = not raw
            return value, [_detail("tag", condition.tag.name), _detail("value", raw)]

        if isinstance(condition, RisingEdgeCondition):
            current = bool(ctx.get_tag(condition.tag.name, False))
            previous = bool(ctx.get_memory(f"_prev:{condition.tag.name}", False))
            value = current and not previous
            return value, [
                _detail("tag", condition.tag.name),
                _detail("current", current),
                _detail("previous", previous),
            ]

        if isinstance(condition, FallingEdgeCondition):
            current = bool(ctx.get_tag(condition.tag.name, False))
            previous = bool(ctx.get_memory(f"_prev:{condition.tag.name}", False))
            value = (not current) and previous
            return value, [
                _detail("tag", condition.tag.name),
                _detail("current", current),
                _detail("previous", previous),
            ]

        compare_ops: tuple[type[Any], ...] = (
            CompareEq,
            CompareNe,
            CompareLt,
            CompareLe,
            CompareGt,
            CompareGe,
            IndirectCompareEq,
            IndirectCompareNe,
            IndirectCompareLt,
            IndirectCompareLe,
            IndirectCompareGt,
            IndirectCompareGe,
        )
        if isinstance(condition, compare_ops):
            if hasattr(condition, "tag"):
                left_label = condition.tag.name
                left_value = ctx.get_tag(condition.tag.name, condition.tag.default)
            else:
                target = condition.indirect_ref.resolve_ctx(ctx)
                left_label = target.name
                left_value = ctx.get_tag(target.name, target.default)
            right_value = _resolve_operand(condition.value)
            value = bool(condition.evaluate(ctx))
            return value, [
                _detail("left", left_label),
                _detail("left_value", left_value),
                _detail("right_value", right_value),
            ]

        expr_compare_ops: tuple[type[Any], ...] = (
            ExprCompareEq,
            ExprCompareNe,
            ExprCompareLt,
            ExprCompareLe,
            ExprCompareGt,
            ExprCompareGe,
        )
        if isinstance(condition, expr_compare_ops):
            left_value = condition.left.evaluate(ctx)
            right_value = condition.right.evaluate(ctx)
            value = bool(condition.evaluate(ctx))
            return value, [
                _detail("left", repr(condition.left)),
                _detail("left_value", left_value),
                _detail("right", repr(condition.right)),
                _detail("right_value", right_value),
            ]

        if isinstance(condition, AllCondition):
            child_results: list[str] = []
            result = True
            for child in condition.conditions:
                child_result, _ = self._evaluate_condition_value(child, ctx)
                child_results.append(f"{self._condition_expression(child)}={child_result}")
                if not child_result:
                    result = False
                    break
            return result, [_detail("terms", " & ".join(child_results))]

        if isinstance(condition, AnyCondition):
            child_results = []
            result = False
            for child in condition.conditions:
                child_result, _ = self._evaluate_condition_value(child, ctx)
                child_results.append(f"{self._condition_expression(child)}={child_result}")
                if child_result:
                    result = True
                    break
            return result, [_detail("terms", " | ".join(child_results))]

        value = bool(condition.evaluate(ctx))
        return value, []

    def _condition_expression(self, condition: Any) -> str:
        from pyrung.core.condition import (
            AllCondition,
            AnyCondition,
            BitCondition,
            CompareEq,
            CompareGe,
            CompareGt,
            CompareLe,
            CompareLt,
            CompareNe,
            FallingEdgeCondition,
            IndirectCompareEq,
            IndirectCompareGe,
            IndirectCompareGt,
            IndirectCompareLe,
            IndirectCompareLt,
            IndirectCompareNe,
            IntTruthyCondition,
            NormallyClosedCondition,
            RisingEdgeCondition,
        )
        from pyrung.core.expression import (
            ExprCompareEq,
            ExprCompareGe,
            ExprCompareGt,
            ExprCompareLe,
            ExprCompareLt,
            ExprCompareNe,
        )
        from pyrung.core.tag import Tag

        def _value_text(value: Any) -> str:
            if isinstance(value, Tag):
                return value.name
            return repr(value)

        if isinstance(condition, BitCondition):
            return condition.tag.name
        if isinstance(condition, IntTruthyCondition):
            return f"{condition.tag.name} != 0"
        if isinstance(condition, NormallyClosedCondition):
            return f"!{condition.tag.name}"
        if isinstance(condition, RisingEdgeCondition):
            return f"rise({condition.tag.name})"
        if isinstance(condition, FallingEdgeCondition):
            return f"fall({condition.tag.name})"
        if isinstance(condition, CompareEq):
            return f"{condition.tag.name} == {_value_text(condition.value)}"
        if isinstance(condition, CompareNe):
            return f"{condition.tag.name} != {_value_text(condition.value)}"
        if isinstance(condition, CompareLt):
            return f"{condition.tag.name} < {_value_text(condition.value)}"
        if isinstance(condition, CompareLe):
            return f"{condition.tag.name} <= {_value_text(condition.value)}"
        if isinstance(condition, CompareGt):
            return f"{condition.tag.name} > {_value_text(condition.value)}"
        if isinstance(condition, CompareGe):
            return f"{condition.tag.name} >= {_value_text(condition.value)}"
        if isinstance(condition, IndirectCompareEq):
            return f"{condition.indirect_ref!r} == {_value_text(condition.value)}"
        if isinstance(condition, IndirectCompareNe):
            return f"{condition.indirect_ref!r} != {_value_text(condition.value)}"
        if isinstance(condition, IndirectCompareLt):
            return f"{condition.indirect_ref!r} < {_value_text(condition.value)}"
        if isinstance(condition, IndirectCompareLe):
            return f"{condition.indirect_ref!r} <= {_value_text(condition.value)}"
        if isinstance(condition, IndirectCompareGt):
            return f"{condition.indirect_ref!r} > {_value_text(condition.value)}"
        if isinstance(condition, IndirectCompareGe):
            return f"{condition.indirect_ref!r} >= {_value_text(condition.value)}"
        if isinstance(condition, ExprCompareEq):
            return f"{condition.left!r} == {condition.right!r}"
        if isinstance(condition, ExprCompareNe):
            return f"{condition.left!r} != {condition.right!r}"
        if isinstance(condition, ExprCompareLt):
            return f"{condition.left!r} < {condition.right!r}"
        if isinstance(condition, ExprCompareLe):
            return f"{condition.left!r} <= {condition.right!r}"
        if isinstance(condition, ExprCompareGt):
            return f"{condition.left!r} > {condition.right!r}"
        if isinstance(condition, ExprCompareGe):
            return f"{condition.left!r} >= {condition.right!r}"
        if isinstance(condition, AllCondition):
            terms = " & ".join(self._condition_expression(child) for child in condition.conditions)
            return f"({terms})"
        if isinstance(condition, AnyCondition):
            terms = " | ".join(self._condition_expression(child) for child in condition.conditions)
            return f"({terms})"
        return condition.__class__.__name__

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
