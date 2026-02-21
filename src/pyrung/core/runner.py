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
from typing import TYPE_CHECKING, Any, Literal

from pyrung.core.context import ScanContext
from pyrung.core.debugger import PLCDebugger
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
        self._trace_formatter = TraceFormatter()
        self._debugger = PLCDebugger(step_factory=ScanStep)

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
        """Persistently override a tag until removed."""
        self._input_overrides.add_force(tag, value)

    def remove_force(self, tag: str | Tag) -> None:
        """Remove a single forced tag override."""
        self._input_overrides.remove_force(tag)

    def clear_forces(self) -> None:
        """Remove all forced tag overrides."""
        self._input_overrides.clear_forces()

    @contextmanager
    def force(
        self,
        overrides: Mapping[str, bool | int | float | str]
        | Mapping[Tag, bool | int | float | str]
        | Mapping[str | Tag, bool | int | float | str],
    ) -> Iterator[PLCRunner]:
        """Temporarily apply forces within a context manager."""
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
        self._input_overrides.apply_post_logic(ctx)

        self._capture_previous_states(ctx)
        self._system_runtime.on_scan_end(ctx)
        self._state = ctx.commit(dt=dt)

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
        """Execute one scan cycle and yield at top-level, branch, and subroutine boundaries."""
        yield from self._debugger.scan_steps_debug(self)

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
                extra_details: list[dict[str, Any]] = []
            else:
                target = condition.indirect_ref.resolve_ctx(ctx)
                left_label = target.name
                left_value = ctx.get_tag(target.name, target.default)
                pointer_name = condition.indirect_ref.pointer.name
                pointer_value = ctx.get_tag(pointer_name, condition.indirect_ref.pointer.default)
                extra_details = [
                    _detail(
                        "left_pointer_expr", f"{condition.indirect_ref.block.name}[{pointer_name}]"
                    ),
                    _detail("left_pointer", pointer_name),
                    _detail("left_pointer_value", pointer_value),
                ]
            right_details: list[dict[str, Any]] = []
            right_operand = condition.value
            if isinstance(right_operand, Tag):
                right_details.append(_detail("right", right_operand.name))
            elif isinstance(right_operand, IndirectRef):
                right_target = right_operand.resolve_ctx(ctx)
                right_pointer_name = right_operand.pointer.name
                right_pointer_value = ctx.get_tag(right_pointer_name, right_operand.pointer.default)
                right_details.extend(
                    [
                        _detail("right", right_target.name),
                        _detail(
                            "right_pointer_expr",
                            f"{right_operand.block.name}[{right_pointer_name}]",
                        ),
                        _detail("right_pointer", right_pointer_name),
                        _detail("right_pointer_value", right_pointer_value),
                    ]
                )
            elif isinstance(right_operand, IndirectExprRef):
                right_target = right_operand.resolve_ctx(ctx)
                # Collapse expression refs to concrete resolved tag labels (for concise trace display).
                right_details.append(_detail("right", right_target.name))
            elif isinstance(right_operand, Expression):
                right_details.append(_detail("right", repr(right_operand)))
            right_value = _resolve_operand(condition.value)
            value = bool(condition.evaluate(ctx))
            return value, [
                _detail("left", left_label),
                _detail("left_value", left_value),
                _detail("right_value", right_value),
                *extra_details,
                *right_details,
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
            for idx, child in enumerate(condition.conditions):
                child_result, child_details = self._evaluate_condition_value(child, ctx)
                child_text = self._condition_term_text(child, child_details)
                child_results.append(f"{child_text}({str(child_result).lower()})")
                if not child_result:
                    result = False
                    for skipped in condition.conditions[idx + 1 :]:
                        child_results.append(f"{self._condition_expression(skipped)}(skipped)")
                    break
            return result, [_detail("terms", " & ".join(child_results))]

        if isinstance(condition, AnyCondition):
            child_results = []
            result = False
            for idx, child in enumerate(condition.conditions):
                child_result, child_details = self._evaluate_condition_value(child, ctx)
                child_text = self._condition_term_text(child, child_details)
                child_results.append(f"{child_text}({str(child_result).lower()})")
                if child_result:
                    result = True
                    for skipped in condition.conditions[idx + 1 :]:
                        child_results.append(f"{self._condition_expression(skipped)}(skipped)")
                    break
            return result, [_detail("terms", " | ".join(child_results))]

        value = bool(condition.evaluate(ctx))
        return value, []

    def _condition_term_text(self, condition: Any, details: list[dict[str, Any]]) -> str:
        expression = self._condition_expression(condition)
        return self._trace_formatter.condition_term_text(expression=expression, details=details)

    def _condition_annotation(self, *, status: str, expression: str, summary: str) -> str:
        return self._trace_formatter.condition_annotation(
            status=status,
            expression=expression,
            summary=summary,
        )

    def _condition_detail_map(self, details: list[dict[str, Any]]) -> dict[str, Any]:
        return self._trace_formatter.condition_detail_map(details)

    def _comparison_parts(self, expression: str) -> tuple[str, str, str] | None:
        return self._trace_formatter.comparison_parts(expression)

    def _comparison_right_text(self, right: str, details: dict[str, Any]) -> str:
        return self._trace_formatter.comparison_right_text(right, details)

    def _is_literal_operand(self, text: str) -> bool:
        return self._trace_formatter.is_literal_operand(text)

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
        from pyrung.core.memory_block import IndirectExprRef, IndirectRef
        from pyrung.core.tag import Tag

        def _value_text(value: Any) -> str:
            if isinstance(value, Tag):
                return value.name
            if isinstance(value, IndirectRef):
                return f"{value.block.name}[{value.pointer.name}]"
            if isinstance(value, IndirectExprRef):
                return f"{value.block.name}[{value.expr!r}]"
            return repr(value)

        def _indirect_ref_text(value: Any) -> str:
            return f"{value.block.name}[{value.pointer.name}]"

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
            return f"{_indirect_ref_text(condition.indirect_ref)} == {_value_text(condition.value)}"
        if isinstance(condition, IndirectCompareNe):
            return f"{_indirect_ref_text(condition.indirect_ref)} != {_value_text(condition.value)}"
        if isinstance(condition, IndirectCompareLt):
            return f"{_indirect_ref_text(condition.indirect_ref)} < {_value_text(condition.value)}"
        if isinstance(condition, IndirectCompareLe):
            return f"{_indirect_ref_text(condition.indirect_ref)} <= {_value_text(condition.value)}"
        if isinstance(condition, IndirectCompareGt):
            return f"{_indirect_ref_text(condition.indirect_ref)} > {_value_text(condition.value)}"
        if isinstance(condition, IndirectCompareGe):
            return f"{_indirect_ref_text(condition.indirect_ref)} >= {_value_text(condition.value)}"
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
